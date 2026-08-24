"""CLI 入口：参数解析 + 编排 + DB 初始化 + 落盘。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from benchmarks import __version__ as benchmark_version
from benchmarks.config import DEFAULT_TRIALS, SINGLE_SCENARIO_TRIALS, BenchmarkConfig
from benchmarks.models.factory import resolve_candidates
from benchmarks.progress import BatchProgress
from benchmarks.report.markdown import (
    render_axis_scenario_table,
    render_markdown_report,
    render_summary_table,
)
from benchmarks.report.schema import BenchmarkReport
from benchmarks.runner import run_benchmark
from benchmarks.scenarios.definitions import QualityAxis
from benchmarks.scenarios.registry import (
    get_scenario,
    list_scenarios,
    select_scenario_ids,
)
from benchmarks.scoring.base import MetricResult
from benchmarks.util import default_out_path, ensure_runtime_dir
from muika.database.db import init_db


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="benchmarks",
        description="Muika 对话体验、行动能力与失真率基准测试",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--models", nargs="+", default=[], help="models.yml 配置名；空则用 default 配置")
    parser.add_argument(
        "--trials",
        type=int,
        default=None,
        help="每 (模型, 场景) 试验次数；缺省：单场景 5，否则 10",
    )
    parser.add_argument("--scenarios", nargs="+", default=None, help="场景 id 子集；空则按 --core/全部")
    parser.add_argument(
        "--core",
        action="store_true",
        help="只跑三轴核心冒烟集；完整场景用于情感支持、关系连续性与专项行动评估",
    )
    parser.add_argument("--seed", type=int, default=0, help="场景选取 / 脚本模型 RNG 种子")
    parser.add_argument(
        "--fixed-time",
        type=str,
        default="2026-08-14T12:00:00+08:00",
        help="注入 prompt 的 ISO-8601 固定时间；传空字符串恢复真实时钟",
    )
    parser.add_argument(
        "--harness",
        choices=("brain", "loop"),
        default="brain",
        help="brain=单次主脑输出；loop=生产消息→Agent→观察迭代管线（工具为确定性 fixture）",
    )
    parser.add_argument(
        "--min-validity-rate",
        type=float,
        default=0.6,
        help="cell 可计分所需的最低有效生成比例；不足则 score=null/INVALID",
    )
    parser.add_argument("--concurrency", type=int, default=1, help="并发 cell 上限（真实模型建议 ≤8）")
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help=(
            "JSON 报告路径；同时写入同名 .md 报告。缺省为 " "benchmarks/results/<时间戳>.json（时间戳命名不覆盖历史）"
        ),
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Markdown 报告中每个指标显示的最高/最低模型场景单元格数量",
    )
    parser.add_argument(
        "--rescore",
        type=str,
        default=None,
        metavar="RESULT.json",
        help="离线重跑现有 JSON 的规则抽取和评分；不调用候选模型或 Judge",
    )
    parser.add_argument("--judge-model", type=str, default=None, help="启用 LLM judge 的模型配置名")
    parser.add_argument(
        "--judge-calibrate",
        action="store_true",
        help="校准 judge：验证结构化身份分类和场景化对话分数范围（需 --judge-model）",
    )
    parser.add_argument(
        "--audit-ambiguous",
        action="store_true",
        help=(
            "周期质检工具（需 --judge-model）：复核 rule 路径判为 ambiguous 的 self_awareness "
            "试验，报漏判好答案占比。它是定期核查（如大幅调整提示词后），不是 rule 路径运行时的"
            "标配步骤——它需要 judge，judge 不可用时无法自捄。"
        ),
    )
    parser.add_argument("--smoke", action="store_true", help="离线脚本化模式（无需 API key / DB）")
    parser.add_argument("--trial-timeout", type=float, default=180.0, help="单试验超时秒数；0 禁用")
    parser.add_argument(
        "--model-retries",
        type=int,
        default=2,
        help="候选模型发生临时调用错误后的重试次数",
    )
    parser.add_argument(
        "--judge-retries",
        type=int,
        default=2,
        help="Judge 调用或 JSON 解析失败后的重试次数",
    )
    parser.add_argument(
        "--resume-from",
        type=str,
        default=None,
        metavar="CHECKPOINT.json",
        help="从兼容的检查点或结果文件继续未完成的 cell",
    )
    parser.add_argument("--echo", action="store_true", help="逐试验回显模型回复（截断到 120 字符）")
    parser.add_argument("--log-level", type=str, default="WARNING", help="loguru 日志级别")
    # ad-hoc 单模型
    parser.add_argument("--provider", type=str, default=None, help="ad-hoc provider（如 openai/dashscope）")
    parser.add_argument("--model-name", type=str, default=None, help="ad-hoc 模型名")
    parser.add_argument("--api-key", type=str, default=None, help="ad-hoc API key")
    parser.add_argument("--api-host", type=str, default=None, help="ad-hoc API 地址")
    parser.add_argument("--temperature", type=float, default=None, help="ad-hoc 温度")
    parser.add_argument("--top-p", type=float, default=None, help="ad-hoc top_p")
    return parser


def _build_config(args: argparse.Namespace) -> BenchmarkConfig:
    """把解析结果组装为 BenchmarkConfig。"""
    adhoc = None
    if args.provider:
        adhoc = {
            "provider": args.provider,
            "model_name": args.model_name,
            "api_key": args.api_key,
            "api_host": args.api_host,
            "temperature": args.temperature,
            "top_p": args.top_p,
        }
    if args.trials is None:
        # 未显式指定：单场景快速验证用 5，其余 10
        scenario_ids = select_scenario_ids(
            tuple(args.scenarios) if args.scenarios else None,
            args.core,
            args.harness,
        )
        trials = SINGLE_SCENARIO_TRIALS if len(scenario_ids) == 1 else DEFAULT_TRIALS
    else:
        trials = args.trials

    out = Path(args.out) if args.out else default_out_path()

    return BenchmarkConfig(
        models=tuple(resolve_candidates(args.models, adhoc, args.smoke)),
        trials=trials,
        scenarios=tuple(args.scenarios) if args.scenarios else None,
        core_only=args.core,
        seed=args.seed,
        fixed_time=args.fixed_time,
        harness=args.harness,
        min_validity_rate=args.min_validity_rate,
        concurrency=args.concurrency,
        out=out,
        judge_model=args.judge_model,
        smoke=args.smoke,
        trial_timeout=args.trial_timeout,
        model_retries=args.model_retries,
        judge_retries=args.judge_retries,
        echo=args.echo,
        audit_ambiguous=args.audit_ambiguous,
        log_level=args.log_level,
    )


def _configure_logging(level: str) -> None:
    """替换 loguru 处理器为指定级别的 stderr 输出（压制敏感日志）。"""
    from loguru import logger as log

    log.remove()
    log.add(sys.stderr, level=level.upper(), format="<lvl>[{level}] {message}</lvl>")


async def _run(
    config: BenchmarkConfig,
    *,
    completed_results: Sequence[MetricResult] = (),
    checkpoint_callback: Callable[[BenchmarkReport], None] | None = None,
) -> BenchmarkReport:
    """真实模型跑批前初始化一次性 DB（usage 写库依赖全局 session）。"""
    if not config.smoke:
        await init_db(ensure_runtime_dir() / "bench.db")

    n_scenarios = len(select_scenario_ids(config.scenarios, config.core_only, config.harness))
    total_cells = len(config.models) * n_scenarios
    pending_cells = max(0, total_cells - len(completed_results))
    progress = BatchProgress(
        total_cells=max(1, pending_cells),
        trials=config.trials,
        use_inline=(config.concurrency == 1),
        echo=config.echo,
    )
    started = time.monotonic()
    if completed_results:
        print(f"[bench] resume: {len(completed_results)} completed cell(s) loaded", file=sys.stderr)
    report = await run_benchmark(
        config,
        progress=progress,
        completed_results=completed_results,
        checkpoint_callback=checkpoint_callback,
    )
    progress.finish(time.monotonic() - started)
    return report


def _write_report(report: BenchmarkReport, out: Path, *, top_n: int = 10) -> Path:
    """Write UTF-8 JSON and a sibling Markdown report."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_out = out.with_suffix(".md")
    markdown_out.write_text(render_markdown_report(report, top_n), encoding="utf-8")
    return markdown_out


def _write_checkpoint(report: BenchmarkReport, path: Path) -> None:
    """Atomically save a partial report after one cell completes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    audit = dict(report.audit) if isinstance(report.audit, dict) else {}
    audit["checkpoint"] = {
        "complete": False,
        "completed_cells": len(report.results),
        "total_cells": len(report.models) * len(report.scenarios),
    }
    report.audit = audit
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_resume_results(source: Path, config: BenchmarkConfig) -> list[MetricResult]:
    """Load completed cells only when the saved run matches the new run."""
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("检查点根节点必须是 JSON 对象")
    saved = BenchmarkReport.from_dict(data)
    scenario_ids = list(select_scenario_ids(config.scenarios, config.core_only, config.harness))
    expected_models = [model.name for model in config.models]
    if saved.models != expected_models:
        raise ValueError(f"模型不匹配: saved={saved.models}, expected={expected_models}")
    if saved.scenarios != scenario_ids:
        raise ValueError("场景列表不匹配")

    checks = {
        "benchmark_version": benchmark_version,
        "seed": config.seed,
        "fixed_time": config.fixed_time or None,
        "trials": config.trials,
        "harness": config.harness,
        "judge_model": config.judge_model,
        "min_validity_rate": config.min_validity_rate,
        "trial_timeout": config.trial_timeout,
        "model_retries": config.model_retries,
        "judge_retries": config.judge_retries,
    }
    mismatches = [
        f"{key}: saved={saved.config.get(key)!r}, expected={value!r}"
        for key, value in checks.items()
        if saved.config.get(key) != value
    ]
    if mismatches:
        raise ValueError("运行配置不匹配: " + "; ".join(mismatches))

    expected_keys = {(model, scenario) for model in expected_models for scenario in scenario_ids}
    result_by_key: dict[tuple[str, str], MetricResult] = {}
    for result in saved.results:
        key = (result.model, result.scenario_id)
        if key not in expected_keys:
            raise ValueError(f"检查点含未知 cell: {key}")
        if key in result_by_key:
            raise ValueError(f"检查点含重复 cell: {key}")
        result_by_key[key] = result
    return list(result_by_key.values())


def _print_results(report: BenchmarkReport) -> None:
    """Print the three-axis summary and compact per-axis scenario evidence."""
    print(render_summary_table(report))
    for axis in QualityAxis:
        print()
        print(render_axis_scenario_table(report, axis))


def _rescore_existing(source: Path, out: Path | None, *, top_n: int) -> tuple[BenchmarkReport, Path]:
    """Load, re-audit, and re-score one report without model calls."""
    from benchmarks.rescore import rescore_report

    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("结果文件的根节点必须是 JSON 对象")
    report = rescore_report(BenchmarkReport.from_dict(data), source=source)
    destination = out or source.with_name(f"{source.stem}_rescored.json")
    markdown = _write_report(report, destination, top_n=top_n)
    return report, markdown


def cli_main(argv: list[str] | None = None) -> int:
    """CLI 编排入口；返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    if args.rescore:
        if args.top_n < 1:
            parser.error("--top-n 必须大于或等于 1")
        source = Path(args.rescore)
        try:
            report, markdown = _rescore_existing(
                source,
                Path(args.out) if args.out else None,
                top_n=args.top_n,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"无法离线重评分 {source}: {exc}")
        print(f"[bench] offline rescore written: {markdown}", file=sys.stderr)
        _print_results(report)
        return 0

    known = set(list_scenarios())
    if args.scenarios:
        unknown = [s for s in args.scenarios if s not in known]
        if unknown:
            print(f"未知场景 id: {unknown}；可用: {sorted(known)}", file=sys.stderr)
            return 2
        incompatible = [
            scenario_id for scenario_id in args.scenarios if args.harness not in get_scenario(scenario_id).harnesses
        ]
        if incompatible:
            print(
                f"场景 {incompatible} 不支持 {args.harness!r} harness；请切换 --harness",
                file=sys.stderr,
            )
            return 2

    if args.judge_calibrate:
        if not args.judge_model:
            parser.error("--judge-calibrate 需要 --judge-model")
        from benchmarks.judge.calibrate import render_calibration, run_calibration
        from benchmarks.judge.client import JudgeClient

        async def _calibrate() -> dict:
            # judge 走 usage 写库，需先 init_db（与 _run 一致）
            await init_db(ensure_runtime_dir() / "bench.db")
            return await run_calibration(JudgeClient(args.judge_model, retries=args.judge_retries))

        result = asyncio.run(_calibrate())
        print(render_calibration(result))
        return 0

    config = _build_config(args)
    if not 0.0 <= config.min_validity_rate <= 1.0:
        parser.error("--min-validity-rate 必须在 0 到 1 之间")
    if args.top_n < 1:
        parser.error("--top-n 必须大于或等于 1")
    if config.model_retries < 0:
        parser.error("--model-retries 必须大于或等于 0")
    if config.judge_retries < 0:
        parser.error("--judge-retries 必须大于或等于 0")

    completed_results: list[MetricResult] = []
    if args.resume_from:
        resume_source = Path(args.resume_from)
        try:
            completed_results = _load_resume_results(resume_source, config)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(f"无法继续运行 {resume_source}: {exc}")

    checkpoint = config.out.with_suffix(".checkpoint.json")
    print(f"[bench] checkpoint: {checkpoint}", file=sys.stderr)
    report = asyncio.run(
        _run(
            config,
            completed_results=completed_results,
            checkpoint_callback=lambda partial: _write_checkpoint(partial, checkpoint),
        )
    )
    audit = dict(report.audit) if isinstance(report.audit, dict) else {}
    audit["checkpoint"] = {
        "complete": True,
        "completed_cells": len(report.results),
        "total_cells": len(report.models) * len(report.scenarios),
    }
    report.audit = audit
    _write_report(report, config.out, top_n=args.top_n)
    checkpoint.unlink(missing_ok=True)
    _print_results(report)
    return 0
