"""E2E 运行轨迹记录：事件、LLM 调用、外发消息与数据库快照。

每个场景产出一份 ``trace.jsonl``，既是调试材料，也是角色行为评审的输入工件。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


class TraceRecorder:
    """按序记录一次 E2E 运行的全部可观察行为。"""

    def __init__(self, artifact_dir: Optional[Path] = None) -> None:
        self._artifact_dir = artifact_dir
        self.entries: list[dict[str, Any]] = []
        self._seq = 0
        self._start = time.monotonic()

    def record(self, kind: str, **data: Any) -> None:
        """追加一条轨迹记录，自动附带序号与相对时间（秒）。"""
        self._seq += 1
        self.entries.append({"seq": self._seq, "t": round(time.monotonic() - self._start, 3), "kind": kind, **data})

    def write(self) -> Optional[Path]:
        """将轨迹写入 ``<artifact_dir>/trace.jsonl``，未配置目录时跳过。"""
        if self._artifact_dir is None:
            return None
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self._artifact_dir / "trace.jsonl"
        with path.open("w", encoding="utf-8") as fp:
            for entry in self.entries:
                fp.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return path
