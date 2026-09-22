import json
import logging

from CTFd.utils.logging import (
    JSONFormatter,
    RequestContextFilter,
    audit,
    generate_request_id,
    get_request_id,
    log,
    log_event,
    set_request_id,
)
from CTFd.utils.metrics import get_metrics, reset_metrics
from tests.helpers import create_ctfd, destroy_ctfd, login_as_user, register_user


class ListHandler(logging.Handler):
    """Capture log records in memory for assertions."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def attach_handler(logger_name):
    handler = ListHandler()
    handler.addFilter(RequestContextFilter())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return handler


def test_request_id_middleware_generates_and_echoes_header():
    """Every response should carry an X-Request-ID header"""
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            r = client.get("/")
            request_id = r.headers.get("X-Request-ID")
            assert request_id is not None
            assert len(request_id) == 32
    destroy_ctfd(app)


def test_request_id_middleware_accepts_inbound_header():
    """An inbound X-Request-ID should be propagated through the request"""
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            r = client.get("/", headers={"X-Request-ID": "test-request-id-123"})
            assert r.headers.get("X-Request-ID") == "test-request-id-123"
    destroy_ctfd(app)


def test_request_id_contextvar_roundtrip():
    request_id = generate_request_id()
    assert len(request_id) == 32
    set_request_id(request_id)
    assert get_request_id() == request_id


def test_log_signature_and_format_compat():
    """The legacy log() call signature and {date}/{ip}/{id} keys must keep working"""
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            client.get("/")
            handler = attach_handler("logins")
            try:
                log("logins", "[{date}] {ip} - {name} logged in", name="user")
                assert len(handler.records) == 1
                record = handler.records[0]
                assert record.levelno == logging.INFO
                assert "127.0.0.1 - user logged in" in record.getMessage()
                # request id is attached to every record
                assert record.request_id is not None
            finally:
                logging.getLogger("logins").removeHandler(handler)
    destroy_ctfd(app)


def test_log_supports_request_id_format_key():
    """New call sites may use {request_id} in the format string"""
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            client.get("/")
            handler = attach_handler("logins")
            try:
                log("logins", "{request_id} hello")
                record = handler.records[0]
                assert record.getMessage().endswith(" hello")
                assert get_request_id() in record.getMessage()
            finally:
                logging.getLogger("logins").removeHandler(handler)
    destroy_ctfd(app)


def test_log_supports_level_keyword():
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            client.get("/")
            handler = attach_handler("logins")
            try:
                log("logins", "failure", level=logging.WARNING)
                assert handler.records[0].levelno == logging.WARNING
            finally:
                logging.getLogger("logins").removeHandler(handler)
    destroy_ctfd(app)


def test_json_formatter_renders_structured_fields():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="audit",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="admin.operation",
        args=(),
        exc_info=None,
    )
    record.request_id = "abc123"
    record.endpoint = "admin.config"
    output = json.loads(formatter.format(record))
    assert output["logger"] == "audit"
    assert output["level"] == "INFO"
    assert output["request_id"] == "abc123"
    assert output["endpoint"] == "admin.config"
    assert output["message"] == "admin.operation"


def test_audit_writes_to_audit_logger():
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            client.get("/")
            handler = attach_handler("audit")
            try:
                audit("test.event", detail="value")
                assert len(handler.records) == 1
                record = handler.records[0]
                assert record.getMessage() == "test.event"
                assert record.detail == "value"
                assert record.request_id == get_request_id()
            finally:
                logging.getLogger("audit").removeHandler(handler)
    destroy_ctfd(app)


def test_auth_events_mirrored_to_audit_log():
    """log() calls on the logins logger must also produce an audit record"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        with app.test_client() as client:
            client.get("/")
            handler = attach_handler("audit")
            try:
                log("logins", "[{date}] {ip} - {name} logged in", name="user")
                assert len(handler.records) == 1
                record = handler.records[0]
                assert record.getMessage() == "logins"
                # `name` collides with LogRecord.name so it is namespaced
                assert record.ctfd_fields["name"] == "user"
                assert record.request_id is not None
            finally:
                logging.getLogger("audit").removeHandler(handler)
    destroy_ctfd(app)


def test_log_event_outside_request_context():
    """log_event must not blow up without a request context"""
    handler = attach_handler("audit")
    try:
        log_event("audit", "background.job", job="export")
        record = handler.records[0]
        assert record.job == "export"
    finally:
        logging.getLogger("audit").removeHandler(handler)


def test_db_query_timing_recorded():
    """SQL queries should be timed into the metrics registry"""
    app = create_ctfd()
    with app.app_context():
        reset_metrics()
        from CTFd.models import Users

        Users.query.count()
        metrics = get_metrics()
        assert "db.query" in metrics["timings"]
        assert metrics["timings"]["db.query"]["count"] >= 1
    destroy_ctfd(app)


def test_slow_query_logged_with_request_id():
    """Queries slower than the threshold emit a structured slow_query event"""
    app = create_ctfd()
    app.config["SLOW_QUERY_THRESHOLD"] = 0  # everything is "slow"
    with app.app_context():
        with app.test_client() as client:
            client.get("/")
            handler = attach_handler("performance")
            try:
                from CTFd.models import Users

                Users.query.count()
                slow = [
                    r for r in handler.records if r.getMessage() == "slow_query"
                ]
                assert len(slow) >= 1
                record = slow[0]
                assert record.levelno == logging.WARNING
                assert record.request_id == get_request_id()
                assert "SELECT" in record.statement.upper()
            finally:
                logging.getLogger("performance").removeHandler(handler)
    destroy_ctfd(app)


def test_cache_operations_timed():
    """Cache get/set should record timings and hit/miss counters"""
    app = create_ctfd()
    with app.app_context():
        reset_metrics()
        from CTFd.cache import cache

        cache.set("test_metrics_key", "value")
        cache.get("test_metrics_key")
        cache.get("test_metrics_missing_key")
        metrics = get_metrics()
        assert "cache.set" in metrics["timings"]
        assert "cache.get" in metrics["timings"]
        assert metrics["counters"]["cache.hit"] >= 1
        assert metrics["counters"]["cache.miss"] >= 1
    destroy_ctfd(app)


def test_admin_operations_audited():
    """Admin requests should produce structured audit records"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        with login_as_user(app, name="admin") as client:
            handler = attach_handler("audit")
            try:
                r = client.get("/admin/statistics")
                assert r.status_code == 200
                admin_records = [
                    rec
                    for rec in handler.records
                    if rec.getMessage() == "admin.operation"
                ]
                assert len(admin_records) >= 1
                record = admin_records[0]
                assert record.path == "/admin/statistics"
                assert record.status == 200
                assert record.request_id is not None
                assert record.duration >= 0
            finally:
                logging.getLogger("audit").removeHandler(handler)
    destroy_ctfd(app)
