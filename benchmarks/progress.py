"""跑批进度指示：向 stderr 打印回显与进度（不依赖 tqdm/rich）。"""

from __future__ import annotations

import sys
import time


class BatchProgress:
    """轻量跑批进度：批次头 + 细胞级回显 + 试验级进度。

    所有输出走 ``sys.stderr`` 的 plain print，不受 loguru ``--log-level`` 压制；
    stdout 保持干净给最终报告表。就地 ``\\r`` 进度条仅当 stderr 是 TTY 时启用，
    非 TTY / 并发时自动退化为纯行输出，避免管道与并发 interleave 乱码。
    """

    def __init__(
        self,
        total_cells: int,
        trials: int,
        *,
        use_inline: bool = True,
        echo: bool = False,
    ) -> None:
        self.total_cells = max(total_cells, 1)
        self.trials = trials
        self.echo = echo
        self.cell_index = 0
        self._inline = use_inline and sys.stderr.isatty()
        self._started_at: dict[int, float] = {}
        """cell_id → 启动时刻。并发下各 cell 独立记录，避免计时/编号互相污染。"""

    def summary(self, n_models: int, n_scenarios: int) -> None:
        """打印批次总览（cell / trial 总数）。"""
        cells = n_models * n_scenarios
        print(
            f"[bench] {n_models} model(s) x {n_scenarios} scenario(s) = "
            f"{cells} cell(s), {cells * self.trials} trial(s)",
            file=sys.stderr,
        )

    def start_cell(self, model: str, scenario: str) -> int:
        """打印细胞开始行（回显正在跑什么），返回该细胞的 cell_id 供 finish_cell 使用。"""
        self.cell_index += 1
        cell_id = self.cell_index
        self._started_at[cell_id] = time.monotonic()
        print(
            f"[bench] cell {cell_id}/{self.total_cells}  " f"{model} x {scenario}  ({self.trials} trials)",
            file=sys.stderr,
        )
        return cell_id

    def trial_done(self, done: int, reply: str | None = None) -> None:
        """报告一个 trial 完成：echo 时打印回复行，否则就地更新进度条。"""
        if self.echo:
            if reply is not None:
                shown = reply.replace("\n", " ")[:120]
                print(f"  [{done}/{self.trials}]  {shown}", file=sys.stderr)
            return
        if not self._inline:
            return
        pct = done / self.trials * 100
        sys.stderr.write(f"\r  trial {done}/{self.trials}  {pct:3.0f}%")
        sys.stderr.flush()

    def finish_cell(self, cell_id: int, score: float | None, n_failed: int) -> None:
        """打印细胞完成行（耗时 + 分数）。用 start_cell 返回的 cell_id 取回该细胞的启动时刻。"""
        if self._inline:
            sys.stderr.write("\r\x1b[K")  # 清掉就地进度条所在行
        started = self._started_at.pop(cell_id, time.monotonic())
        elapsed = time.monotonic() - started
        failed = f", {n_failed} failed" if n_failed else ""
        shown_score = f"{score:.2f}" if score is not None else "INVALID"
        print(
            f"[bench] cell {cell_id}/{self.total_cells} done in " f"{elapsed:.1f}s  score={shown_score}{failed}",
            file=sys.stderr,
        )

    def finish(self, total_elapsed: float) -> None:
        """打印整批完成行。"""
        print(f"[bench] finished in {total_elapsed:.1f}s", file=sys.stderr)
