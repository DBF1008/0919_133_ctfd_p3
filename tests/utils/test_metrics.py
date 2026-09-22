from CTFd.utils.metrics import (
    get_metrics,
    increment,
    record_timing,
    reset_metrics,
    timed,
)


def setup_function():
    reset_metrics()


def teardown_function():
    reset_metrics()


def test_record_timing_aggregates():
    record_timing("test.op", 0.5)
    record_timing("test.op", 1.5)
    metrics = get_metrics()
    stat = metrics["timings"]["test.op"]
    assert stat["count"] == 2
    assert stat["total"] == 2.0
    assert stat["max"] == 1.5
    assert stat["min"] == 0.5
    assert stat["avg"] == 1.0


def test_record_timing_with_tags_namespaces_metric():
    record_timing("test.op", 0.1, endpoint="/admin", method="GET")
    metrics = get_metrics()
    assert "test.op{endpoint=/admin,method=GET}" in metrics["timings"]


def test_increment_counter():
    increment("test.counter")
    increment("test.counter", 4)
    metrics = get_metrics()
    assert metrics["counters"]["test.counter"] == 5


def test_timed_context_manager_records_duration():
    with timed("test.block"):
        sum(range(1000))
    metrics = get_metrics()
    stat = metrics["timings"]["test.block"]
    assert stat["count"] == 1
    assert stat["total"] >= 0


def test_timed_context_manager_records_on_exception():
    try:
        with timed("test.failing"):
            raise ValueError("boom")
    except ValueError:
        pass
    metrics = get_metrics()
    assert metrics["timings"]["test.failing"]["count"] == 1


def test_reset_metrics_clears_state():
    record_timing("test.op", 0.1)
    increment("test.counter")
    reset_metrics()
    metrics = get_metrics()
    assert metrics["timings"] == {}
    assert metrics["counters"] == {}
