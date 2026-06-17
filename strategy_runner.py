from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from qdii_monitor.settings import DB_FILE
from qdii_monitor.strategy import (
    BacktestResult,
    grid_search,
    load_daily_points,
    print_results,
    write_csv,
    write_equity_csv,
    write_svg_plot,
)


LIQUID_POOL = ("513100", "513110", "513390", "159660")

BALANCED_POOL = ("513100", "513110", "513390", "159660", "159632", "159659")

FULL_NASDAQ100_POOL = (
    "513100",  #   	49.72%	22.62%	41	大阈值 2.5%，小阈值 2.0%，持有 3 天
    "513110",
    "513300",
    "513390", # 	50.76%	21.80%	18	大阈值 2.5%，小阈值 1.2%，持有 10 天
    "513870",
    "159501",
    "159513",
    "159632",
    "159659",
    "159660",
    "159696",
    "159941",
)

NASDAQ_TECH_POOL = ("159509",)

# 纳指100 LOF 替代仓：161130 是易方达纳斯达克100 ETF 联接（QDII-LOF）A。
# 它不是 ETF，盘中 IOPV 口径不同，但历史收盘价/NAV 溢价可用于测试“高溢价时切到低溢价替代仓”。
NASDAQ100_LOF_CODE = "161130"

# 全纳指 ETF 池 + 161130：用于观察 161130 是否稳定成为低溢价替代仓。
NASDAQ100_WITH_161130_POOL = FULL_NASDAQ100_POOL + (NASDAQ100_LOF_CODE,)

# 直接二元池：只测试某个纳指 ETF 与 161130 之间的来回换仓。
NASDAQ100_LOF_PAIR_513390 = ("513390", NASDAQ100_LOF_CODE)
NASDAQ100_LOF_PAIR_513100 = ("513100", NASDAQ100_LOF_CODE)


def pct_range(start: float, stop: float, step: float) -> tuple[float, ...]:
    values: list[float] = []
    current = start
    while current <= stop + step / 2:
        values.append(round(current / 100, 6))
        current += step
    return tuple(values)

# 直接修改下面这些参数，然后运行：
# .\.venv\Scripts\python.exe strategy_runner.py
CONFIG = {
    # "min-premium"：只要当前持仓比最低溢价 ETF 贵到阈值，就换到最低溢价 ETF。
    # "benchmark-cycle"：基准 ETF 溢价过高时换出去，溢价差收敛后再换回基准 ETF。
    # dynamic-anchor-cycle
    "mode": "benchmark-cycle",

    # 选择一个标的池，也可以自己写一个 ETF 代码元组。
    # LIQUID_POOL：标的少，更偏实盘执行。
    # BALANCED_POOL：原来的 6 个测试标的。
    # FULL_NASDAQ100_POOL：配置中的全部标准纳指100 ETF，不包含 159509。
    # NASDAQ100_WITH_161130_POOL：全纳指 ETF + 161130，测试低溢价 LOF 替代仓。
    # NASDAQ100_LOF_PAIR_513390 / NASDAQ100_LOF_PAIR_513100：只做 ETF 与 161130 的直接换仓。
    "codes": NASDAQ100_LOF_PAIR_513100,
    "benchmark": "513100",
    "initial": None,
    "capital": 50000.0,

    # None 表示使用本地数据库里的全部历史。日期格式为 "YYYY-MM-DD"。
    "start": "2026-01-01",
    "end": None,

    # 大阈值：当前持仓或基准 ETF 的溢价差达到这个水平时，触发换仓。
    "switch_thresholds": pct_range(0.5, 10.0, 0.1),
    #"switch_thresholds": [0.05],

    # 小阈值：只在 benchmark-cycle 模式下使用。
    # 例：0.0% 到 1.5% 表示当 513100 的溢价优势基本消失后，再换回 513100。
    "return_thresholds": pct_range(0.0, 10.0, 0.1),
    #"return_thresholds": [0.01],

    # 最短持有天数：一次换仓后至少持有多少个交易日，才允许下一次换仓。
    "min_hold_days": (1, 2, 3, 5, 10, 20, 40),
    #"min_hold_days": [10],

    # 换仓成本：卖出万一 + 买入万一，完整换仓约等于 2 bps。
    "cost_bps": 2.0,

    # 可选买入限制。保持 (None,) 表示允许买入所有候选 ETF。
    # 例：(None, 0.05, 0.06) 会额外测试“只买入溢价 <= 5% 或 <= 6% 的 ETF”。
    "max_buy_premiums": (None,),

    # dynamic-anchor-cycle 专用：每天用过去窗口重算“谁更容易高溢价”。
    # 默认 60/5/40/0.5%：60日评分、连续5日胜出、锚点至少持有40日、分数领先0.5%才切换。
    "anchor_windows": [60],
    "anchor_confirm_days": [5],
    "anchor_min_hold_days": [40],
    "anchor_switch_margins": [0.005],

    "top": 20,
    "show_trades": True,
    "output_prefix": Path("data/strategy_custom"),

    # 每次运行都会额外生成一个独立目录，记录参数快照、Top 结果、交易明细和曲线。
    # 后续给大模型分析时，优先读取 data/strategy_runs 下的 manifest.json。
    "save_run": True,
    "run_log_dir": Path("data/strategy_runs"),
}


def persist_run(
    results: list[BacktestResult],
    config: dict[str, Any],
    data: dict[str, Any],
    created_at: datetime | None = None,
) -> Path:
    """把本次回测完整落盘，方便后续复盘或交给大模型分析。"""
    if not results:
        raise ValueError("results must not be empty")
    now = created_at or datetime.now()
    best = results[0]
    top = int(config["top"])
    base_run_id = now.strftime("%Y%m%d_%H%M%S")
    run_root = Path(config.get("run_log_dir") or "data/strategy_runs")
    run_id = base_run_id
    run_dir = run_root / run_id
    counter = 2
    while run_dir.exists():
        run_id = f"{base_run_id}_{counter:02d}"
        run_dir = run_root / run_id
        counter += 1
    run_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "config": run_dir / "config.json",
        "manifest": run_dir / "manifest.json",
        "grid_all": run_dir / "grid_all.csv",
        "top_results": run_dir / "top_results.csv",
        "top_results_json": run_dir / "top_results.json",
        "best_trades": run_dir / "best_trades.csv",
        "best_trades_json": run_dir / "best_trades.json",
        "event_attribution": run_dir / "event_attribution.csv",
        "event_attribution_json": run_dir / "event_attribution.json",
        "anchor_history": run_dir / "anchor_history.csv",
        "anchor_history_json": run_dir / "anchor_history.json",
        "best_curve": run_dir / "best_curve.csv",
        "best_plot": run_dir / "best_curve.svg",
    }

    write_csv(files["grid_all"], results)
    write_csv(files["top_results"], results[:top])
    write_equity_csv(files["best_curve"], best)
    write_svg_plot(files["best_plot"], best)
    _write_trades_csv(files["best_trades"], best)
    attribution_rows = _event_attribution_rows(best, data)
    _write_rows_csv(files["event_attribution"], attribution_rows)
    _write_json(files["event_attribution_json"], attribution_rows)
    _write_json(files["best_trades_json"], [asdict(trade) for trade in best.trades])
    _write_anchor_history_csv(files["anchor_history"], best)
    _write_json(files["anchor_history_json"], [asdict(point) for point in best.anchor_history])
    _write_json(files["top_results_json"], [_result_snapshot(result) for result in results[:top]])

    config_snapshot = _snapshot_config(config)
    _write_json(files["config"], config_snapshot)

    manifest = {
        "run_id": run_id,
        "created_at": now.isoformat(timespec="seconds"),
        "config": config_snapshot,
        "pool_constants": {
            "LIQUID_POOL": list(LIQUID_POOL),
            "BALANCED_POOL": list(BALANCED_POOL),
            "FULL_NASDAQ100_POOL": list(FULL_NASDAQ100_POOL),
            "NASDAQ_TECH_POOL": list(NASDAQ_TECH_POOL),
            "NASDAQ100_WITH_161130_POOL": list(NASDAQ100_WITH_161130_POOL),
            "NASDAQ100_LOF_PAIR_513390": list(NASDAQ100_LOF_PAIR_513390),
            "NASDAQ100_LOF_PAIR_513100": list(NASDAQ100_LOF_PAIR_513100),
        },
        "data": {
            "date_count": len(data),
            "first_date": min(data) if data else None,
            "last_date": max(data) if data else None,
            "codes": list(config["codes"]),
        },
        "result_count": len(results),
        "top_count": min(top, len(results)),
        "best": _result_snapshot(best),
        "files": {key: str(path) for key, path in files.items()},
    }
    _write_json(files["manifest"], manifest)
    return run_dir


def _snapshot_config(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_snapshot_config(item) for item in value]
    if isinstance(value, list):
        return [_snapshot_config(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _snapshot_config(item) for key, item in value.items()}
    return value


def _result_snapshot(result: BacktestResult) -> dict[str, Any]:
    row = result.summary_row()
    row.update(
        {
            "codes": list(result.codes),
            "benchmark_code": result.benchmark_code,
            "initial_code": result.initial_code,
            "final_code": result.final_code,
            "trade_count": len(result.trades),
            "trades": [asdict(trade) for trade in result.trades],
            "anchor_history_count": len(result.anchor_history),
            "anchor_switches": sum(1 for point in result.anchor_history if point.switched),
        }
    )
    return row


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _event_attribution_rows(
    result: BacktestResult,
    data: dict[str, Any],
    horizons: tuple[int, ...] = (5, 20, 60),
) -> list[dict[str, Any]]:
    dates = [point.trade_date for point in result.equity_curve]
    date_index = {date: index for index, date in enumerate(dates)}
    rows: list[dict[str, Any]] = []
    for sequence, trade in enumerate(result.trades, start=1):
        index = date_index.get(trade.trade_date)
        if index is None:
            continue
        curve_point = result.equity_curve[index]
        trade_points = data.get(trade.trade_date, {})
        benchmark_premium = _premium_at(trade_points, result.benchmark_code)
        row: dict[str, Any] = {
            "trade_no": sequence,
            "trade_date": trade.trade_date,
            "event_type": _event_type(trade.from_code, trade.to_code, trade.anchor_code or result.benchmark_code),
            "from_code": trade.from_code,
            "to_code": trade.to_code,
            "anchor_code": trade.anchor_code,
            "benchmark_code": result.benchmark_code,
            "from_premium_pct": trade.from_premium * 100,
            "to_premium_pct": trade.to_premium * 100,
            "benchmark_premium_pct": None if benchmark_premium is None else benchmark_premium * 100,
            "spread_pct": trade.spread * 100,
            "switch_threshold_pct": result.switch_threshold * 100,
            "return_threshold_pct": None if result.return_threshold is None else result.return_threshold * 100,
            "min_hold_days": result.min_hold_days,
            "cost_bps": result.switch_cost * 10000,
            "value_after_cost": trade.value_after_cost,
            "strategy_nav_before": curve_point.strategy_value / result.capital,
            "benchmark_nav_before": curve_point.benchmark_value / result.capital,
            "alpha_nav_before": _alpha_nav(curve_point, result.capital),
        }
        for horizon in horizons:
            horizon_index = min(index + horizon, len(result.equity_curve) - 1)
            horizon_point = result.equity_curve[horizon_index]
            horizon_points = data.get(horizon_point.trade_date, {})
            strategy_nav = horizon_point.strategy_value / result.capital
            benchmark_nav = horizon_point.benchmark_value / result.capital
            base_strategy_nav = curve_point.strategy_value / result.capital
            base_benchmark_nav = curve_point.benchmark_value / result.capital
            from_premium = _premium_at(horizon_points, trade.from_code)
            to_premium = _premium_at(horizon_points, trade.to_code)
            horizon_benchmark_premium = _premium_at(horizon_points, result.benchmark_code)
            row.update(
                {
                    f"h{horizon}_date": horizon_point.trade_date,
                    f"h{horizon}_strategy_return_pct": (strategy_nav / base_strategy_nav - 1) * 100,
                    f"h{horizon}_benchmark_return_pct": (benchmark_nav / base_benchmark_nav - 1) * 100,
                    f"h{horizon}_alpha_nav_change": _alpha_nav(horizon_point, result.capital) - row["alpha_nav_before"],
                    f"h{horizon}_from_premium_pct": None if from_premium is None else from_premium * 100,
                    f"h{horizon}_to_premium_pct": None if to_premium is None else to_premium * 100,
                    f"h{horizon}_benchmark_premium_pct": (
                        None if horizon_benchmark_premium is None else horizon_benchmark_premium * 100
                    ),
                    f"h{horizon}_spread_pct": (
                        None if from_premium is None or to_premium is None else (from_premium - to_premium) * 100
                    ),
                    f"h{horizon}_spread_change_pct": (
                        None if from_premium is None or to_premium is None else (from_premium - to_premium - trade.spread) * 100
                    ),
                }
            )
        rows.append(row)
    return rows


def _alpha_nav(point: Any, capital: float) -> float:
    return point.strategy_value / capital - point.benchmark_value / capital


def _premium_at(points: dict[str, Any], code: str) -> float | None:
    point = points.get(code)
    return None if point is None else point.premium_rate


def _event_type(from_code: str, to_code: str, benchmark_code: str) -> str:
    if from_code == benchmark_code:
        return "switch_out_of_benchmark"
    if to_code == benchmark_code:
        return "buyback_to_benchmark"
    return "rotate_between_alternatives"


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["trade_no"]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_trades_csv(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trade_date",
        "from_code",
        "to_code",
        "anchor_code",
        "from_premium_pct",
        "to_premium_pct",
        "spread_pct",
        "value_after_cost",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for trade in result.trades:
            writer.writerow(
                {
                    "trade_date": trade.trade_date,
                    "from_code": trade.from_code,
                    "to_code": trade.to_code,
                    "anchor_code": trade.anchor_code,
                    "from_premium_pct": trade.from_premium * 100,
                    "to_premium_pct": trade.to_premium * 100,
                    "spread_pct": trade.spread * 100,
                    "value_after_cost": trade.value_after_cost,
                }
            )


def _write_anchor_history_csv(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(point) for point in result.anchor_history]
    fieldnames = list(rows[0]) if rows else [
        "trade_date",
        "anchor_code",
        "candidate_code",
        "anchor_score",
        "candidate_score",
        "score_margin",
        "pending_code",
        "pending_days",
        "days_since_anchor_switch",
        "switched",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    codes = tuple(CONFIG["codes"])
    data = load_daily_points(
        DB_FILE,
        codes,
        start_date=CONFIG["start"],
        end_date=CONFIG["end"],
    )
    return_thresholds = CONFIG["return_thresholds"]
    if CONFIG["mode"] == "min-premium":
        return_thresholds = (None,)

    results = grid_search(
        data=data,
        codes=codes,
        benchmark_code=CONFIG["benchmark"],
        initial_code=CONFIG["initial"],
        capital=CONFIG["capital"],
        thresholds=CONFIG["switch_thresholds"],
        return_thresholds=return_thresholds,
        switch_cost=CONFIG["cost_bps"] / 10000,
        min_hold_days_values=CONFIG["min_hold_days"],
        max_buy_premiums=CONFIG["max_buy_premiums"],
        mode=CONFIG["mode"],
        anchor_windows=CONFIG["anchor_windows"],
        anchor_confirm_days_values=CONFIG["anchor_confirm_days"],
        anchor_min_hold_days_values=CONFIG["anchor_min_hold_days"],
        anchor_switch_margins=CONFIG["anchor_switch_margins"],
    )
    if not results:
        raise RuntimeError("没有生成回测结果，请检查 ETF 代码和日期过滤条件。")

    print_results(results, int(CONFIG["top"]))
    best = results[0]
    prefix = Path(CONFIG["output_prefix"])
    write_csv(prefix.with_name(prefix.name + "_grid.csv"), results)
    write_equity_csv(prefix.with_name(prefix.name + "_best_curve.csv"), best)
    write_svg_plot(prefix.with_name(prefix.name + "_best_curve.svg"), best)
    run_dir = persist_run(results, CONFIG, data) if CONFIG.get("save_run", True) else None

    print("\n最优参数：")
    print(f"mode={best.mode}")
    print(f"codes={','.join(best.codes)}")
    print(f"benchmark={best.benchmark_code}")
    print(f"start={best.start_date} end={best.end_date} days={best.days}")
    print(f"switch_threshold={best.switch_threshold * 100:.2f}%")
    if best.return_threshold is not None:
        print(f"return_threshold={best.return_threshold * 100:.2f}%")
    print(f"min_hold_days={best.min_hold_days}")
    if best.mode == "dynamic-anchor-cycle":
        print(
            "dynamic_anchor="
            f"window={best.anchor_window}, "
            f"confirm={best.anchor_confirm_days}, "
            f"anchor_min_hold={best.anchor_min_hold_days}, "
            f"margin={best.anchor_switch_margin * 100:.2f}%"
        )
    print(f"cost_bps={best.switch_cost * 10000:.1f}")
    print(f"alpha={best.alpha_return * 100:.2f}% alpha_value={best.alpha_value:.2f}")
    print(f"final={best.final_value:.2f} benchmark={best.benchmark_value:.2f}")
    print(f"switches={best.switches} final_code={best.final_code}")
    print(f"\n已写入：{prefix.with_name(prefix.name + '_grid.csv')}")
    print(f"已写入：{prefix.with_name(prefix.name + '_best_curve.csv')}")
    print(f"已写入：{prefix.with_name(prefix.name + '_best_curve.svg')}")
    if run_dir is not None:
        print(f"本次运行记录：{run_dir}")
        print(f"LLM 分析入口：{run_dir / 'manifest.json'}")

    if CONFIG["show_trades"]:
        print("\n最优结果交易明细：")
        for trade in best.trades:
            print(
                f"{trade.trade_date}\t{trade.from_code}->{trade.to_code}"
                f"\tanchor={trade.anchor_code or best.benchmark_code}"
                f"\tspread={trade.spread * 100:.2f}%"
                f"\tfrom={trade.from_premium * 100:.2f}%"
                f"\tto={trade.to_premium * 100:.2f}%"
                f"\tvalue={trade.value_after_cost:.2f}"
            )


if __name__ == "__main__":
    main()
