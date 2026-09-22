#!/usr/bin/env bash
#
# test.sh — 日志系统改造的单元测试入口（手动执行）
#
# 覆盖范围：
#   1. 语法检查（无需依赖，任何环境都能跑）
#   2. tests/utils/test_metrics.py    — 耗时打点 / 指标注册表
#   3. tests/utils/test_logging.py    — request_id 中间件、log() 兼容性、
#                                       JSON 结构化输出、审计日志、慢查询、缓存计时
#   4. tests/admin/test_statistics.py — statistics 聚合查询耗时打点
#
# 用法：
#   ./test.sh            # 运行全部
#   ./test.sh metrics    # 只跑 metrics 单元
#   ./test.sh logging    # 只跑 logging 单元
#   ./test.sh statistics # 只跑 statistics 单元

set -euo pipefail
cd "$(dirname "$0")"

PYTEST=${PYTEST:-pytest}
PYTEST_OPTS=${PYTEST_OPTS:--v -rf}

syntax_check() {
    echo "==> [1/4] 语法检查（py_compile）"
    python3 -m py_compile \
        CTFd/utils/metrics.py \
        CTFd/utils/logging/__init__.py \
        CTFd/utils/initialization/__init__.py \
        CTFd/cache/__init__.py \
        CTFd/admin/__init__.py \
        CTFd/admin/statistics.py \
        CTFd/config.py \
        CTFd/__init__.py \
        tests/utils/test_metrics.py \
        tests/utils/test_logging.py \
        tests/admin/test_statistics.py
    echo "    语法检查通过"
}

run_metrics() {
    echo "==> [2/4] metrics 单元测试（耗时打点注册表）"
    $PYTEST $PYTEST_OPTS tests/utils/test_metrics.py
}

run_logging() {
    echo "==> [3/4] logging 单元测试（request_id / 结构化 / 审计 / 慢查询 / 缓存）"
    $PYTEST $PYTEST_OPTS tests/utils/test_logging.py
}

run_statistics() {
    echo "==> [4/4] statistics 单元测试（聚合查询耗时打点）"
    $PYTEST $PYTEST_OPTS tests/admin/test_statistics.py
}

case "${1:-all}" in
    metrics)    syntax_check; run_metrics ;;
    logging)    syntax_check; run_logging ;;
    statistics) syntax_check; run_statistics ;;
    all)        syntax_check; run_metrics; run_logging; run_statistics ;;
    *)
        echo "未知目标: $1（可选: all | metrics | logging | statistics）" >&2
        exit 1
        ;;
esac

echo "==> 全部通过"
