import json
import logging

from CTFd.cache import cache
from CTFd.utils.logging import (
    JSONFormatter,
    REQUEST_ID_HEADER,
    audit,
    get_request_id,
    log,
    set_request_id,
    timed_block,
)
from tests.helpers import create_ctfd, destroy_ctfd, login_as_user, register_user


class ListHandler(logging.Handler):
    """Simple handler that collects records for assertions."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _attach(logger_name):
    logger = logging.getLogger(logger_name)
    handler = ListHandler()
    logger.addHandler(handler)
    return handler


def test_log_signature_backwards_compatible():
    """The legacy log(logger, format, **kwargs) call pattern keeps working."""
    app = create_ctfd()
    with app.test_request_context("/"):
        handler = _attach("logins")
        log("logins", "[{date}] {ip} - {name} logged in", name="user")
        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.levelno == logging.INFO
        assert record.getMessage().endswith(" - user logged in")
    destroy_ctfd(app)


def test_log_supports_level_kwarg():
    app = create_ctfd()
    with app.test_request_context("/"):
        handler = _attach("logins")
        log("logins", "{ip} failed", level=logging.WARNING)
        assert handler.records[0].levelno == logging.WARNING
    destroy_ctfd(app)


def test_log_carries_request_id():
    app = create_ctfd()
    with app.test_request_context("/"):
        set_request_id("test-request-id")
        handler = _attach("logins")
        log("logins", "{ip} something")
        record = handler.records[0]
        assert record.request_id == "test-request-id"
        assert record.extra["request_id"] == "test-request-id"
    destroy_ctfd(app)


def test_request_id_middleware_generates_and_echoes_id():
    app = create_ctfd()
    with app.test_client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert REQUEST_ID_HEADER in r.headers
        assert len(r.headers[REQUEST_ID_HEADER]) == 32
    destroy_ctfd(app)


def test_request_id_middleware_honors_incoming_header():
    app = create_ctfd()
    with app.test_client() as client:
        r = client.get("/", headers={REQUEST_ID_HEADER: "upstream-id-123"})
        assert r.headers[REQUEST_ID_HEADER] == "upstream-id-123"
    destroy_ctfd(app)


def test_request_id_available_during_request():
    app = create_ctfd()
    with app.test_client() as client:
        r = client.get("/", headers={REQUEST_ID_HEADER: "abc123"})
        assert r.headers[REQUEST_ID_HEADER] == "abc123"
    # Outside of a request the context var falls back to None
    with app.app_context():
        assert get_request_id() is None
    destroy_ctfd(app)


def test_audit_emits_structured_record():
    app = create_ctfd()
    with app.test_request_context("/"):
        set_request_id("audit-rid")
        handler = _attach("audit")
        audit("auth.login.success", user_id=1, name="user")
        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.request_id == "audit-rid"
        assert record.extra["action"] == "auth.login.success"
        assert record.extra["user_id"] == 1
        assert record.extra["request_id"] == "audit-rid"
    destroy_ctfd(app)


def test_json_formatter_outputs_parseable_json():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="audit",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.request_id = "rid-1"
    record.extra = {"action": "test.action"}
    payload = json.loads(formatter.format(record))
    assert payload["logger"] == "audit"
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"
    assert payload["request_id"] == "rid-1"
    assert payload["action"] == "test.action"


def test_timed_block_records_duration():
    app = create_ctfd()
    with app.test_request_context("/"):
        handler = _attach("performance")
        with timed_block("statistics.test_block"):
            sum(range(1000))
        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.extra["name"] == "statistics.test_block"
        assert record.extra["duration_ms"] >= 0
        assert "request_id" in record.extra
    destroy_ctfd(app)


def test_db_query_timing_logged():
    app = create_ctfd()
    app.config["SLOW_QUERY_MS"] = 0  # everything is a "slow" query
    handler = _attach("performance")
    with app.app_context():
        from CTFd.models import Users

        Users.query.count()
    query_records = [r for r in handler.records if r.extra.get("name") == "db.query"]
    assert len(query_records) > 0
    assert "duration_ms" in query_records[0].extra
    assert "statement" in query_records[0].extra
    destroy_ctfd(app)


def test_cache_operations_logged():
    app = create_ctfd()
    handler = _attach("performance")
    with app.app_context():
        cache.set("test_logging_key", "value")
        cache.get("test_logging_key")
        cache.delete("test_logging_key")
    names = [r.extra.get("name") for r in handler.records]
    assert "cache.set" in names
    assert "cache.get" in names
    assert "cache.delete" in names
    destroy_ctfd(app)


def test_admin_operations_are_audited():
    app = create_ctfd()
    handler = _attach("audit")
    with login_as_user(app, name="admin") as admin:
        r = admin.get("/admin/statistics")
        assert r.status_code == 200
    admin_records = [
        r for r in handler.records if r.extra.get("action") == "admin.operation"
    ]
    assert len(admin_records) > 0
    record = admin_records[0]
    assert record.extra["path"] == "/admin/statistics"
    assert record.extra["method"] == "GET"
    assert record.extra["request_id"]
    destroy_ctfd(app)


def test_statistics_queries_are_timed():
    app = create_ctfd()
    handler = _attach("performance")
    with login_as_user(app, name="admin") as admin:
        r = admin.get("/admin/statistics")
        assert r.status_code == 200
    names = [r.extra.get("name") for r in handler.records]
    assert "statistics.teams_registered" in names
    assert "statistics.users_registered" in names
    assert "statistics.solve_count" in names
    assert "statistics.solves_per_challenge" in names
    destroy_ctfd(app)


def test_auth_events_are_audited():
    app = create_ctfd()
    register_user(app)
    handler = _attach("audit")
    with app.test_client() as client:
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        r = client.post(
            "/login",
            data={"name": "user", "password": "password", "nonce": nonce},
        )
        assert r.status_code == 302
    actions = [r.extra.get("action") for r in handler.records]
    assert "auth.login.success" in actions
    destroy_ctfd(app)


def test_failed_login_is_audited():
    app = create_ctfd()
    register_user(app)
    handler = _attach("audit")
    with app.test_client() as client:
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        r = client.post(
            "/login",
            data={"name": "user", "password": "wrongpassword", "nonce": nonce},
        )
        assert r.status_code == 200
    actions = [r.extra.get("action") for r in handler.records]
    assert "auth.login.invalid_password" in actions
    destroy_ctfd(app)
