from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdii_monitor.settings import DB_FILE
from qdii_monitor.strategy import BacktestResult, grid_search, load_daily_points
from strategy_runner import (
    CONFIG,
    FULL_NASDAQ100_POOL,
    NASDAQ100_LOF_PAIR_513100,
    NASDAQ100_LOF_PAIR_513390,
    NASDAQ100_WITH_161130_POOL,
    persist_run,
    pct_range,
)


# 这组是你最初重点看的 6 只，先保留作基线。
POOL_INITIAL_6 = ("513100", "513110", "513390", "159660", "159632", "159659")

# 主线池：不含 159513，避免让一个你没有计划实盘配置的标的影响结论。
POOL_EXPLAINABLE_CORE = (
    "513100",
    "513110",
    "513300",
    "513390",
    "513870",
    "159632",
    "159659",
)

# 扩展池：在主线池基础上加入更常见的替代标的，仍不含 159513。
POOL_CORE_PLUS = POOL_EXPLAINABLE_CORE + ("159501", "159660", "159696")

# 全标准纳指100池，但排除 159513；用于判断不含 159513 时策略上限。
POOL_FULL_NO_159513 = tuple(code for code in FULL_NASDAQ100_POOL if code != "159513")

# 对照池：当前 FULL_NASDAQ100_POOL，含 159513；只用于观察它对结果的影响。
POOL_FULL_WITH_159513 = FULL_NASDAQ100_POOL

# 161130 低溢价替代仓测试池：全纳指 ETF 加 LOF，另保留两个直接二元池。
POOL_FULL_WITH_161130_LOF = NASDAQ100_WITH_161130_POOL
POOL_LOF_PAIR_513390 = NASDAQ100_LOF_PAIR_513390
POOL_LOF_PAIR_513100 = NASDAQ100_LOF_PAIR_513100

# 消融池：在不含 159513 的全池里再去掉 159660，观察弱贡献标的是否拖累。
POOL_FULL_NO_159513_NO_159660 = tuple(code for code in POOL_FULL_NO_159513 if code != "159660")

# 513390 主线池：根据 leave-one-out 结果保留贡献或解释性较强的标的，不含 159513。
POOL_513390_CORE = (
    "513390",
    "513110",
    "513300",
    "513870",
    "159501",
    "159659",
    "159696",
)

# 更窄的 513390 清洁池：只保留主基准和 leave-one-out 中贡献最明确的替代标的。
POOL_513390_CLEAN = (
    "513390",
    "513110",
    "159501",
    "159659",
)


POOLS = {
    "initial_6": POOL_INITIAL_6,
    "explainable_core_no_159513": POOL_EXPLAINABLE_CORE,
    "core_plus_no_159513": POOL_CORE_PLUS,
    "full_no_159513": POOL_FULL_NO_159513,
    "full_no_159513_no_159660": POOL_FULL_NO_159513_NO_159660,
    "full_with_159513_control": POOL_FULL_WITH_159513,
    "full_with_161130_lof": POOL_FULL_WITH_161130_LOF,
    "lof_pair_513390": POOL_LOF_PAIR_513390,
    "lof_pair_513100": POOL_LOF_PAIR_513100,
    "513390_core": POOL_513390_CORE,
    "513390_clean": POOL_513390_CLEAN,
}

DEFAULT_BENCHMARKS = ("513390", "513100")


@dataclass(frozen=True)
class Experiment:
    pool_name: str
    benchmark: str
    codes: tuple[str, ...]


def planned_experiments(
    pools: dict[str, tuple[str, ...]] = POOLS,
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS,
) -> list[Experiment]:
    experiments = []
    for pool_name, codes in pools.items():
        for benchmark in benchmarks:
            if benchmark in codes:
                experiments.append(Experiment(pool_name, benchmark, codes))
    return experiments


def leave_one_out_pools(base_codes: tuple[str, ...] = FULL_NASDAQ100_POOL) -> dict[str, tuple[str, ...]]:
    return {
        f"leave_one_out_drop_{dropped}": tuple(code for code in base_codes if code != dropped)
        for dropped in base_codes
    }


def compact_grid() -> dict[str, Any]:
    """快速探索用粗网格，适合先判断池子方向。"""
    return {
        "switch_thresholds": pct_range(0.5, 6.0, 0.5),
        "return_thresholds": pct_range(0.0, 3.0, 0.5),
        "min_hold_days": (1, 2, 3, 5, 10, 20),
    }


def run_experiment(
    experiment: Experiment,
    fast: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> tuple[BacktestResult, Path]:
    config = dict(CONFIG)
    config.update(
        {
            "codes": experiment.codes,
            "benchmark": experiment.benchmark,
            "experiment_type": "pool_ablation",
            "pool_name": experiment.pool_name,
            "pool_codes": experiment.codes,
            "output_prefix": Path("data/pool_experiments") / f"{experiment.pool_name}_{experiment.benchmark}",
            "run_log_dir": Path("data/strategy_runs"),
        }
    )
    if fast:
        config.update(compact_grid())
    if start is not None:
        config["start"] = start
    if end is not None:
        config["end"] = end

    data = load_daily_points(
        DB_FILE,
        experiment.codes,
        start_date=config["start"],
        end_date=config["end"],
    )
    return_thresholds = config["return_thresholds"]
    if config["mode"] == "min-premium":
        return_thresholds = (None,)

    results = grid_search(
        data=data,
        codes=experiment.codes,
        benchmark_code=experiment.benchmark,
        initial_code=config["initial"],
        capital=config["capital"],
        thresholds=config["switch_thresholds"],
        return_thresholds=return_thresholds,
        switch_cost=config["cost_bps"] / 10000,
        min_hold_days_values=config["min_hold_days"],
        max_buy_premiums=config["max_buy_premiums"],
        mode=config["mode"],
    )
    if not results:
        raise RuntimeError(f"{experiment.pool_name}/{experiment.benchmark} 没有生成结果")
    run_dir = persist_run(results, config, data)
    return results[0], run_dir


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    path.with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="批量测试纳指 ETF 池子组合。")
    parser.add_argument("--fast", action="store_true", help="使用粗网格快速探索。")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行回测。")
    parser.add_argument("--leave-one-out", action="store_true", help="从完整池轮流剔除一只 ETF，做 11 只池子的消融测试。")
    parser.add_argument("--pool", action="append", choices=sorted(POOLS), help="只运行指定池子，可重复传入。")
    parser.add_argument("--benchmark", action="append", choices=DEFAULT_BENCHMARKS, help="只运行指定 benchmark，可重复传入。")
    parser.add_argument("--start", default=None, help="覆盖 CONFIG 里的开始日期，格式 YYYY-MM-DD。")
    parser.add_argument("--end", default=None, help="覆盖 CONFIG 里的结束日期，格式 YYYY-MM-DD。")
    args = parser.parse_args()

    if args.leave_one_out:
        selected_pools = leave_one_out_pools()
    else:
        selected_pools = {name: POOLS[name] for name in (args.pool or POOLS)}
    selected_benchmarks = tuple(args.benchmark or DEFAULT_BENCHMARKS)
    experiments = planned_experiments(selected_pools, selected_benchmarks)

    print("计划运行：")
    for item in experiments:
        print(f"- {item.pool_name} / benchmark={item.benchmark} / codes={','.join(item.codes)}")
    if args.dry_run:
        return

    rows = []
    for index, experiment in enumerate(experiments, start=1):
        print(f"\n[{index}/{len(experiments)}] {experiment.pool_name} / benchmark={experiment.benchmark}")
        best, run_dir = run_experiment(experiment, fast=args.fast, start=args.start, end=args.end)
        row = {
            "pool_name": experiment.pool_name,
            "benchmark": experiment.benchmark,
            "codes_count": len(experiment.codes),
            "codes": ",".join(experiment.codes),
            "run_dir": str(run_dir),
            "final_value": best.final_value,
            "benchmark_value": best.benchmark_value,
            "total_return_pct": best.total_return * 100,
            "benchmark_return_pct": best.benchmark_return * 100,
            "alpha_pct": best.alpha_return * 100,
            "annual_alpha_pct": best.annual_alpha * 100,
            "max_drawdown_pct": best.max_drawdown * 100,
            "switches": best.switches,
            "final_code": best.final_code,
            "switch_threshold_pct": best.switch_threshold * 100,
            "return_threshold_pct": None if best.return_threshold is None else best.return_threshold * 100,
            "min_hold_days": best.min_hold_days,
        }
        rows.append(row)
        print(
            f"best alpha={row['alpha_pct']:.2f}% final={row['final_value']:.2f} "
            f"switches={row['switches']} run={run_dir}"
        )

    summary_path = Path("data/pool_experiments/summary_fast.csv" if args.fast else "data/pool_experiments/summary_full.csv")
    write_summary(summary_path, rows)
    print(f"\n汇总已写入：{summary_path}")
    print(f"JSON 汇总：{summary_path.with_suffix('.json')}")


if __name__ == "__main__":
    main()
