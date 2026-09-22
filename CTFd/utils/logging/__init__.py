import json
import logging
import logging.handlers
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from flask import g, has_request_context, request, session

from CTFd.utils.user import get_ip

# Header used to propagate request ids across proxies and back to clients
REQUEST_ID_HEADER = "X-Request-ID"

# Context local storage for the current request id. A ContextVar is used so
# that the id flows through the whole request lifecycle (middleware, views,
# DB queries, cache operations) without having to pass it explicitly.
_request_id_var = ContextVar("ctfd_request_id", default=None)


def generate_request_id():
    """
    Generate a new opaque request id.
    """
    return uuid.uuid4().hex


def get_request_id():
    """
    Return the request id bound to the current context, or None outside of a
    request (e.g. CLI commands, background threads).
    """
    if has_request_context():
        rid = getattr(g, "request_id", None)
        if rid:
            return rid
    return _request_id_var.get()


def set_request_id(request_id):
    """
    Bind a request id to the current context.
    """
    _request_id_var.set(request_id)
    if has_request_context():
        g.request_id = request_id
    return request_id


def init_request_id(app):
    """
    Middleware chain entry point for request id propagation.

    Registers before/after request handlers that:
      - honor an incoming X-Request-ID header (or generate a fresh id)
      - bind the id to a ContextVar and flask.g for the request lifetime
      - echo the id back to the client on the response
    """

    @app.before_request
    def _ctfd_assign_request_id():
        request_id = request.headers.get(REQUEST_ID_HEADER) or generate_request_id()
        set_request_id(request_id)

    @app.after_request
    def _ctfd_attach_request_id(response):
        request_id = get_request_id()
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.teardown_request
    def _ctfd_reset_request_id(exception=None):
        # Prevent the id from leaking into subsequent requests or background
        # work that runs on the same thread outside of a request context.
        _request_id_var.set(None)


class JSONFormatter(logging.Formatter):
    """
    Formatter that emits one JSON object per line so logs can be shipped
    directly to ELK / Loki / CloudWatch without grok patterns.
    """

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def log(logger, format, **kwargs):
    """
    Backwards compatible logging helper.

    Existing call sites use log("logins", "[{date}] {ip} - ...", name=...).
    The signature is unchanged; callers may additionally pass:
      - level: a logging level (default logging.INFO)
      - request_id: overrides the auto-detected request id
    """
    level = kwargs.pop("level", logging.INFO)
    logger = logging.getLogger(logger)
    props = {
        "id": session.get("id"),
        "date": time.strftime("%m/%d/%Y %X"),
        "ip": get_ip(),
        "request_id": get_request_id(),
    }
    props.update(kwargs)
    msg = format.format(**props)
    logger.log(level, msg, extra={"request_id": props["request_id"], "extra": props})


def audit(action, level=logging.INFO, **fields):
    """
    Emit a structured audit record on the "audit" logger.

    Used for admin operations and authentication events so they can be
    filtered and alerted on independently of free-form text logs.
    """
    record = {
        "action": action,
        "user_id": session.get("id") if has_request_context() else None,
        "ip": get_ip() if has_request_context() else None,
        "request_id": get_request_id(),
    }
    record.update(fields)
    logging.getLogger("audit").log(
        level, action, extra={"request_id": record["request_id"], "extra": record}
    )


def log_timing(name, duration_ms, logger="performance", level=logging.INFO, **fields):
    """
    Emit a structured timing record on the "performance" logger.

    The flat shape (name/duration_ms/request_id) is chosen so it maps 1:1 to a
    Prometheus histogram or an ELK document.
    """
    record = {
        "name": name,
        "duration_ms": round(duration_ms, 3),
        "request_id": get_request_id(),
    }
    record.update(fields)
    logging.getLogger(logger).log(
        level,
        "{name} took {duration_ms:.3f}ms".format(**record),
        extra={"request_id": record["request_id"], "extra": record},
    )


@contextmanager
def timed_block(name, logger="performance", level=logging.INFO, **fields):
    """
    Context manager that times a block of code and emits a structured timing
    record, e.g. around expensive DB aggregation queries.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        duration_ms = (time.monotonic() - start) * 1000.0
        log_timing(name, duration_ms, logger=logger, level=level, **fields)


def init_db_logging(app):
    """
    Attach SQLAlchemy event listeners that record per-query durations.

    Queries slower than app.config["SLOW_QUERY_MS"] (default 500ms) are logged
    at WARNING; all queries are logged at DEBUG. Every record carries the
    current request id so slow queries can be traced back to their request.
    """
    from sqlalchemy import event

    from CTFd.models import db

    slow_query_ms = app.config.get("SLOW_QUERY_MS") or 500
    query_start = ContextVar("ctfd_query_start", default=None)

    @event.listens_for(db.engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        query_start.set(time.monotonic())

    @event.listens_for(db.engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        start = query_start.get()
        if start is None:
            return
        duration_ms = (time.monotonic() - start) * 1000.0
        level = logging.DEBUG
        if duration_ms >= slow_query_ms:
            level = logging.WARNING
        logger = logging.getLogger("performance")
        if logger.isEnabledFor(level):
            log_timing(
                "db.query",
                duration_ms,
                level=level,
                statement=statement[:1024],
            )
