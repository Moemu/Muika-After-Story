"""Core 候选 pytest 子进程探针。"""

from __future__ import annotations

import json
from typing import Any

_MARKER = "[CORE_PROBE_RESULT]"


class _ProbePlugin:
    """收集 pytest nodeid 和测试数量。"""

    def __init__(self) -> None:
        self.failures: set[str] = set()
        self.errors: set[str] = set()
        self.test_count = 0

    def pytest_collection_finish(self, session: Any) -> None:
        """记录测试收集数量。"""
        self.test_count = len(session.items)

    def pytest_collectreport(self, report: Any) -> None:
        """记录收集错误。"""
        if report.failed:
            self.errors.add(str(report.nodeid))

    def pytest_runtest_logreport(self, report: Any) -> None:
        """记录测试失败或运行错误。"""
        if not report.failed:
            return
        if report.when == "call":
            self.failures.add(str(report.nodeid))
        else:
            self.errors.add(str(report.nodeid))


def main() -> None:
    """运行 pytest 并输出结构化结果。"""
    try:
        import pytest
    except ImportError as exc:
        result = {
            "status": "unavailable",
            "reason": f"pytest is unavailable: {exc}",
            "timed_out": False,
            "failures": [],
            "errors": [],
            "test_count": 0,
        }
        print(_MARKER + json.dumps(result, ensure_ascii=False))
        return

    plugin = _ProbePlugin()
    try:
        exit_code = int(pytest.main(["-q", "-o", "addopts="], plugins=[plugin]))
        result = {
            "status": "completed",
            "reason": f"pytest exited with code {exit_code}.",
            "exit_code": exit_code,
            "timed_out": False,
            "failures": sorted(plugin.failures),
            "errors": sorted(plugin.errors),
            "test_count": plugin.test_count,
        }
    except Exception as exc:
        result = {
            "status": "unavailable",
            "reason": f"pytest could not run: {exc}",
            "timed_out": False,
            "failures": [],
            "errors": [],
            "test_count": 0,
        }
    print(_MARKER + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
