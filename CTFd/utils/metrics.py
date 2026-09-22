import threading
import time
from collections import defaultdict
from contextlib import contextmanager

_lock = threading.Lock()

# name -> {"count": int, "total": float, "max": float, "min": float}
_timings = defaultdict(
    lambda: {"count": 0, "total": 0.0, "max": 0.0, "min": float("inf")}
)
# name -> int
_counters = defaultdict(int)


def record_timing(name, duration, **tags):
    """
    Record a duration (in seconds) under a metric name.

    Tags are accepted for forward compatibility with metric backends
    (e.g. Prometheus labels) but are currently only used to namespace
    the metric name.
    """
    if tags:
        tag_suffix = ",".join("{}={}".format(k, tags[k]) for k in sorted(tags))
        name = "{}{{{}}}".format(name, tag_suffix)
    with _lock:
        stat = _timings[name]
        stat["count"] += 1
        stat["total"] += duration
        stat["max"] = max(stat["max"], duration)
        stat["min"] = min(stat["min"], duration)


def increment(name, value=1, **tags):
    """Increment a counter metric."""
    if tags:
        tag_suffix = ",".join("{}={}".format(k, tags[k]) for k in sorted(tags))
        name = "{}{{{}}}".format(name, tag_suffix)
    with _lock:
        _counters[name] += value


def get_metrics():
    """
    Return a snapshot of all recorded metrics.

    The returned structure is intentionally simple so it can be exported
    to Prometheus, StatsD, or shipped to ELK without further transformation:

    {
        "timings": {
            "db.query": {"count": 10, "total": 0.5, "max": 0.2, "min": 0.001, "avg": 0.05},
        },
        "counters": {"cache.hit": 3},
    }
    """
    with _lock:
        timings = {}
        for name, stat in _timings.items():
            count = stat["count"]
            timings[name] = {
                "count": count,
                "total": stat["total"],
                "max": stat["max"],
                "min": stat["min"] if count else 0.0,
                "avg": (stat["total"] / count) if count else 0.0,
            }
        counters = dict(_counters)
    return {"timings": timings, "counters": counters}


def reset_metrics():
    """Clear all recorded metrics. Primarily useful for tests."""
    with _lock:
        _timings.clear()
        _counters.clear()


@contextmanager
def timed(name, **tags):
    """
    Context manager that records the duration of the wrapped block.

    Usage:
        with timed("statistics.solve_count"):
            ...
    """
    start = time.monotonic()
    try:
        yield
    finally:
        record_timing(name, time.monotonic() - start, **tags)
