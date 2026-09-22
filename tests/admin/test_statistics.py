from CTFd.utils.metrics import get_metrics, reset_metrics
from tests.helpers import create_ctfd, destroy_ctfd, login_as_user


def test_statistics_page_renders():
    """The admin statistics page should load for admins"""
    app = create_ctfd()
    with app.app_context():
        with login_as_user(app, name="admin") as client:
            r = client.get("/admin/statistics")
            assert r.status_code == 200
    destroy_ctfd(app)


def test_statistics_queries_are_timed():
    """Every aggregation query on the statistics page should record a timing"""
    app = create_ctfd()
    with app.app_context():
        reset_metrics()
        with login_as_user(app, name="admin") as client:
            r = client.get("/admin/statistics")
            assert r.status_code == 200
        timings = get_metrics()["timings"]
        expected = [
            "statistics.teams_registered",
            "statistics.users_registered",
            "statistics.wrong_count",
            "statistics.solve_count",
            "statistics.challenge_count",
            "statistics.total_points",
            "statistics.ip_count",
            "statistics.solves_by_challenge",
        ]
        for name in expected:
            assert name in timings, "missing timing for {}".format(name)
            assert timings[name]["count"] >= 1
        # DB queries themselves are also timed via the SQLAlchemy hooks
        assert "db.query" in timings
    destroy_ctfd(app)


def test_statistics_request_has_request_id():
    """The statistics response should carry the propagated request id"""
    app = create_ctfd()
    with app.app_context():
        with login_as_user(app, name="admin") as client:
            r = client.get("/admin/statistics")
            assert r.status_code == 200
            assert r.headers.get("X-Request-ID") is not None
    destroy_ctfd(app)
