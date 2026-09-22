#!/usr/bin/env bash
#
# Manual test runner for the logging / request_id / audit / timing changes.
#
# Usage:
#   ./test.sh            # run the new logging unit tests + related suites
#   ./test.sh all        # run the entire test suite
#
set -euo pipefail

cd "$(dirname "$0")"

if [ "${1:-}" = "all" ]; then
    echo "== Running full test suite =="
    python -m pytest tests/ -x -q
else
    echo "== Logging system unit tests =="
    python -m pytest tests/utils/test_logging.py -v

    echo
    echo "== Related regression tests (admin / auth / cache / statistics) =="
    python -m pytest \
        tests/admin/test_statistics.py \
        tests/admin/test_views.py \
        tests/cache/ \
        tests/users/test_auth.py \
        -q
fi
