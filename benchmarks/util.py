"""工具函数：密钥脱敏、运行目录、默认结果路径。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

BENCHMARKS_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BENCHMARKS_DIR / ".runtime"
"""运行期临时目录（存放 bench.db 等瞬态产物，已由 benchmarks/.gitignore 排除）。"""

RESULTS_DIR = BENCHMARKS_DIR / "results"
"""默认结果目录（审计 JSON，已由 benchmarks/.gitignore 排除）。"""

_SECRET_PATTERNS = re.compile(r"(sk-[A-Za-z0-9_-]{4,}|AIza[A-Za-z0-9_-]{4,}|Bearer\s+\S+)")


def redact(text: str) -> str:
    """掩码常见 API key（sk- / AIza / Bearer 前缀），防止明文泄露进日志/报告。"""
    return _SECRET_PATTERNS.sub(lambda m: m.group(0)[:7] + "***", text)


def ensure_runtime_dir() -> Path:
    """创建并返回 ``benchmarks/.runtime`` 运行目录（存放 bench.db 等瞬态产物）。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def default_out_path() -> Path:
    """默认报告路径：``benchmarks/results/<本地时间戳>.json``（时间戳命名不覆盖历史）。"""
    return RESULTS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
