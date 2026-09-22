import json
import logging
import logging.handlers
import time
import uuid
from contextvars import ContextVar

from flask import has_request_context, request, session

from CTFd.utils.metrics import record_timing
from CTFd.utils.user import get_ip

# Request ID propagation. Set once per request by the middleware installed in
# init_request_id_middleware() and read anywhere (views, DB hooks, cache hooks)
# without having to pass the value through every call signature.
_request_id_var = ContextVar("request_id", default=None)

# LogRecord attributes that are always present on a vanilla record. Anything
# else attached via logging's `extra=` is treated as structured context and
# included in JSON output.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "request_id", "taskName"}

# Attributes that may not be overridden via logging's `extra=` mechanism
_RESERVED_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "asctime",
}


def generate_request_id():
    return uuid.uuid4().hex


def set_request_id(request_id=None):
    """
    Bind a request id to the current execution context.

    Returns the effective request id so callers can propagate it
    (e.g. into response headers or background jobs).
    """
    request_id = request_id or generate_request_id()
    _request_id_var.set(request_id)
    return request_id


def get_request_id():
    """
    Return the request id for the current context.

    Falls back to the Flask request object (if the middleware stored the id
    there) so code running before the middleware executes still resolves
    consistently. Returns None outside of an instrumented request.
    """
    request_id = _request_id_var.get()
    if request_id is None and has_request_context():
        request_id = getattr(request, "request_id", None)
    return request_id


class RequestContextFilter(logging.Filter):
    """Inject the current request id into every log record."""

    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class JSONFormatter(logging.Formatter):
    """
    Render log records as single-line JSON for structured log pipelines
    (ELK, Loki, CloudWatch, ...).

    Any attribute attached to the record via `extra=` is merged into the
    top-level JSON object so fields stay queryable.
    """

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


# Legacy loggers whose events are also written to the structured audit log
_AUTH_AUDIT_LOGGERS = frozenset({"logins", "registrations"})


def log(logger, format, **kwargs):
    """
    Legacy logging entry point. Signature is unchanged so all existing call
    sites keep working.

    Enhancements over the original implementation:
      * `{request_id}` is available as a format key (old formats without it
        render exactly as before)
      * an optional `level=` keyword (default INFO) selects the log level
      * all keyword arguments are attached to the record as structured
        fields so JSON handlers emit them as queryable keys
    """
    level = kwargs.pop("level", logging.INFO)
    logger_name = logger
    logger = logging.getLogger(logger_name)
    props = {
        "id": session.get("id"),
        "date": time.strftime("%m/%d/%Y %X"),
        "ip": get_ip(),
        "request_id": get_request_id(),
    }
    props.update(kwargs)
    msg = format.format(**props)
    extra = {"ctfd_fields": dict(props)}
    logger.log(level, msg, extra=extra)
    # Mirror authentication related events into the structured audit log so
    # they can be shipped to a tamper-evident store. Existing call sites get
    # this behavior for free.
    if logger_name in _AUTH_AUDIT_LOGGERS:
        audit(logger_name, level=level, **props)


def log_event(logger, event, level=logging.INFO, **fields):
    """
    Emit a structured event log. Fields are attached to the record and are
    rendered as top-level JSON keys by JSONFormatter.

    All fields are always available under the record's `ctfd_fields` dict.
    Fields whose names do not collide with reserved LogRecord attributes
    (e.g. `name`, `message`) are also attached as top-level attributes.
    """
    logger = logging.getLogger(logger)
    fields.setdefault("request_id", get_request_id())
    if has_request_context():
        fields.setdefault("ip", get_ip())
        fields.setdefault("user_id", session.get("id"))
    extra = {"ctfd_fields": dict(fields)}
    for key, value in fields.items():
        if key not in _RESERVED_RECORD_ATTRS:
            extra[key] = value
    extra["event"] = event
    logger.log(level, event, extra=extra)


def audit(event, level=logging.INFO, **fields):
    """
    Write a structured audit record to the dedicated `audit` logger.

    Used for admin operations and authentication events so they can be
    shipped to a tamper-evident store independently of debug logs.
    """
    log_event("audit", event, level=level, **fields)


def log_slow(name, duration, threshold, **fields):
    """Emit a structured warning when an operation exceeds a threshold."""
    if duration >= threshold:
        log_event(
            "performance",
            "slow_operation",
            level=logging.WARNING,
            operation=name,
            duration=round(duration, 6),
            threshold=threshold,
            **fields,
        )
        return True
    return False


def init_request_id_middleware(app):
    """
    Install request id middleware.

    A request id is taken from the inbound X-Request-ID header when present
    (so upstream proxies can correlate) or generated otherwise. It is bound
    to a ContextVar for the duration of the request and echoed back on the
    X-Request-ID response header.
    """

    @app.before_request
    def _ctfd_assign_request_id():
        request_id = request.headers.get("X-Request-ID") or generate_request_id()
        set_request_id(request_id)
        request.request_id = request_id

    @app.after_request
    def _ctfd_attach_request_id(response):
        request_id = get_request_id()
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response


def init_query_timing(app, engine=None):
    """
    Instrument SQLAlchemy with per-query timing.

    Every query duration is recorded into the metrics registry (for later
    Prometheus/ELK export) and queries slower than SLOW_QUERY_THRESHOLD
    (seconds, default 0.5) emit a structured `slow_query` warning carrying
    the current request id.
    """
    from sqlalchemy import event

    if engine is None:
        from CTFd.models import db

        engine = db.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        conn.info.setdefault("_ctfd_query_start_time", []).append(time.monotonic())

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        start_times = conn.info.get("_ctfd_query_start_time")
        if not start_times:
            return
        duration = time.monotonic() - start_times.pop()
        record_timing("db.query", duration)
        threshold = float(app.config.get("SLOW_QUERY_THRESHOLD", 0.5))
        if duration >= threshold:
            log_event(
                "performance",
                "slow_query",
                level=logging.WARNING,
                duration=round(duration, 6),
                threshold=threshold,
                statement=statement[:1024],
                parameters=str(parameters)[:1024],
            )

    @event.listens_for(engine, "handle_error")
    def _handle_error(exception_context):
        conn = exception_context.connection
        if conn is not None:
            start_times = conn.info.get("_ctfd_query_start_time")
            if start_times:
                start_times.pop()
