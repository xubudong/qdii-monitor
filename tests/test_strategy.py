from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from strategy_runner import NASDAQ100_LOF_PAIR_513390, NASDAQ100_WITH_161130_POOL, persist_run
from pool_experiment_runner import FULL_NASDAQ100_POOL, POOLS, leave_one_out_pools, planned_experiments
from qdii_monitor.strategy import grid_search, load_daily_points, parse_range, simulate_rotation


def test_rotation_switches_to_lower_premium_and_counts_alpha() -> None:
    data = {
        "2026-01-01": {
            "A": point("A", "2026-01-01", 100, 0.05),
            "B": point("B", "2026-01-01", 100, 0.01),
        },
        "2026-01-02": {
            "A": point("A", "2026-01-02", 110, 0.06),
            "B": point("B", "2026-01-02", 120, 0.02),
        },
    }

    result = simulate_rotation(
        data,
        codes=("A", "B"),
        benchmark_code="A",
        capital=50000,
        switch_threshold=0.02,
        switch_cost=0.001,
    )

    assert result.final_code == "B"
    assert result.switches == 1
    assert result.final_value == pytest.approx(50000 * 0.999 * 1.2)
    assert result.benchmark_value == pytest.approx(50000 * 1.1)
    assert result.alpha_value == pytest.approx(result.final_value - result.benchmark_value)


def test_rotation_respects_min_hold_days_after_initial_switch() -> None:
    data = {
        "2026-01-01": {
            "A": point("A", "2026-01-01", 100, 0.05),
            "B": point("B", "2026-01-01", 100, 0.01),
        },
        "2026-01-02": {
            "A": point("A", "2026-01-02", 101, 0.00),
            "B": point("B", "2026-01-02", 101, 0.05),
        },
        "2026-01-03": {
            "A": point("A", "2026-01-03", 102, 0.00),
            "B": point("B", "2026-01-03", 102, 0.05),
        },
    }

    result = simulate_rotation(
        data,
        codes=("A", "B"),
        benchmark_code="A",
        switch_threshold=0.02,
        min_hold_days=5,
    )

    assert result.final_code == "B"
    assert result.switches == 1


def test_benchmark_cycle_switches_out_and_buys_back() -> None:
    data = {
        "2026-01-01": {
            "A": point("A", "2026-01-01", 100, 0.06),
            "B": point("B", "2026-01-01", 100, 0.02),
        },
        "2026-01-02": {
            "A": point("A", "2026-01-02", 101, 0.030),
            "B": point("B", "2026-01-02", 101, 0.025),
        },
        "2026-01-03": {
            "A": point("A", "2026-01-03", 102, 0.030),
            "B": point("B", "2026-01-03", 102, 0.025),
        },
    }

    result = simulate_rotation(
        data,
        codes=("A", "B"),
        benchmark_code="A",
        switch_threshold=0.03,
        return_threshold=0.01,
        min_hold_days=1,
        mode="benchmark-cycle",
    )

    assert result.final_code == "A"
    assert result.switches == 2
    assert [trade.to_code for trade in result.trades] == ["B", "A"]


def test_dynamic_anchor_scores_use_past_window_only() -> None:
    data = dynamic_anchor_data(days=64, b_high_until=60, c_high_from=60)

    result = simulate_rotation(
        data,
        codes=("A", "B", "C"),
        benchmark_code="A",
        switch_threshold=0.03,
        return_threshold=0.01,
        min_hold_days=10,
        mode="dynamic-anchor-cycle",
        anchor_window=60,
        anchor_confirm_days=1,
        anchor_min_hold_days=1,
        anchor_switch_margin=0.001,
    )

    switched = [point for point in result.anchor_history if point.switched]
    assert switched[0].trade_date == date_for(60)
    assert switched[0].candidate_code == "B"
    assert switched[0].anchor_code == "B"


def test_dynamic_anchor_requires_confirmation_days() -> None:
    data = dynamic_anchor_data(days=63, b_high_until=63)

    result = simulate_rotation(
        data,
        codes=("A", "B", "C"),
        benchmark_code="A",
        switch_threshold=0.03,
        return_threshold=0.01,
        min_hold_days=10,
        mode="dynamic-anchor-cycle",
        anchor_window=60,
        anchor_confirm_days=3,
        anchor_min_hold_days=1,
        anchor_switch_margin=0.001,
    )

    assert [point for point in result.anchor_history if point.switched] == []
    assert result.anchor_history[-1].anchor_code == "A"


def test_dynamic_anchor_requires_min_anchor_hold_days() -> None:
    data = dynamic_anchor_data(days=64, b_high_until=64)

    result = simulate_rotation(
        data,
        codes=("A", "B", "C"),
        benchmark_code="A",
        switch_threshold=0.03,
        return_threshold=0.01,
        min_hold_days=10,
        mode="dynamic-anchor-cycle",
        anchor_window=60,
        anchor_confirm_days=1,
        anchor_min_hold_days=80,
        anchor_switch_margin=0.001,
    )

    assert [point for point in result.anchor_history if point.switched] == []
    assert result.anchor_history[-1].anchor_code == "A"


def test_dynamic_anchor_trades_against_new_anchor_after_switch() -> None:
    data = {}
    for index in range(64):
        trade_date = date_for(index)
        b_premium = 0.07 if index <= 60 else 0.02
        c_premium = 0.01 if index <= 60 else 0.015
        data[trade_date] = {
            "A": point("A", trade_date, 100, 0.06),
            "B": point("B", trade_date, 100, b_premium),
            "C": point("C", trade_date, 100, c_premium),
        }

    result = simulate_rotation(
        data,
        codes=("A", "B", "C"),
        benchmark_code="A",
        switch_threshold=0.03,
        return_threshold=0.01,
        min_hold_days=1,
        mode="dynamic-anchor-cycle",
        anchor_window=60,
        anchor_confirm_days=1,
        anchor_min_hold_days=1,
        anchor_switch_margin=0.001,
    )

    assert [point.anchor_code for point in result.anchor_history if point.switched] == ["B"]
    assert result.trades[0].to_code == "C"
    assert result.trades[-1].trade_date == date_for(61)
    assert result.trades[-1].anchor_code == "B"
    assert result.trades[-1].to_code == "B"


def test_grid_search_orders_by_alpha() -> None:
    data = {
        "2026-01-01": {
            "A": point("A", "2026-01-01", 100, 0.05),
            "B": point("B", "2026-01-01", 100, 0.01),
        },
        "2026-01-02": {
            "A": point("A", "2026-01-02", 100, 0.05),
            "B": point("B", "2026-01-02", 110, 0.01),
        },
    }

    results = grid_search(
        data,
        codes=("A", "B"),
        benchmark_code="A",
        thresholds=(0.02, 0.06),
        min_hold_days_values=(1,),
    )

    assert results[0].switch_threshold == 0.02
    assert results[0].alpha_return > results[1].alpha_return


def test_benchmark_cycle_grid_requires_return_threshold_below_switch_threshold() -> None:
    data = {
        "2026-01-01": {
            "A": point("A", "2026-01-01", 100, 0.05),
            "B": point("B", "2026-01-01", 100, 0.01),
        },
        "2026-01-02": {
            "A": point("A", "2026-01-02", 100, 0.02),
            "B": point("B", "2026-01-02", 100, 0.01),
        },
    }

    results = grid_search(
        data,
        codes=("A", "B"),
        benchmark_code="A",
        thresholds=(0.02,),
        return_thresholds=(0.01, 0.02, 0.03),
        min_hold_days_values=(1,),
        mode="benchmark-cycle",
    )

    assert [result.return_threshold for result in results] == [0.01]


def test_load_daily_points_reads_sqlite_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "sample.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE daily_premium_history (
                code TEXT,
                trade_date TEXT,
                close_price REAL,
                nav REAL,
                premium_rate REAL,
                source TEXT,
                fetched_at TEXT,
                PRIMARY KEY(code, trade_date)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO daily_premium_history
            VALUES ('A', '2026-01-01', 1.0, 0.95, 0.0526, 'test', 'now')
            """
        )

    data = load_daily_points(db_path, ("A",))

    assert data["2026-01-01"]["A"].close_price == pytest.approx(1.0)
    assert data["2026-01-01"]["A"].premium_rate == pytest.approx(0.0526)


def test_parse_range_accepts_percent_notation() -> None:
    assert parse_range("1.0:2.0:0.5") == pytest.approx([0.01, 0.015, 0.02])
    assert parse_range("0.6") == pytest.approx([0.006])
    assert parse_range("0.01,0.02") == pytest.approx([0.01, 0.02])


def test_strategy_runner_persists_run_manifest_and_trades(tmp_path: Path) -> None:
    data = {
        "2026-01-01": {
            "A": point("A", "2026-01-01", 100, 0.05),
            "B": point("B", "2026-01-01", 100, 0.01),
        },
        "2026-01-02": {
            "A": point("A", "2026-01-02", 110, 0.06),
            "B": point("B", "2026-01-02", 120, 0.02),
        },
    }
    result = simulate_rotation(data, codes=("A", "B"), benchmark_code="A", switch_threshold=0.02)
    run_dir = persist_run(
        [result],
        {"top": 1, "codes": ("A", "B"), "run_log_dir": tmp_path},
        data,
        created_at=datetime(2026, 1, 3, 9, 30, 0),
    )

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert run_dir.name == "20260103_093000"
    assert manifest["config"]["codes"] == ["A", "B"]
    assert "FULL_NASDAQ100_POOL" in manifest["pool_constants"]
    assert "NASDAQ100_WITH_161130_POOL" in manifest["pool_constants"]
    assert manifest["pool_constants"]["NASDAQ100_LOF_PAIR_513390"] == ["513390", "161130"]
    assert manifest["best"]["trades"][0]["to_code"] == "B"
    assert (run_dir / "top_results.csv").exists()
    assert (run_dir / "best_trades.csv").read_text(encoding="utf-8-sig").count("\n") >= 2
    with (run_dir / "event_attribution.csv").open(encoding="utf-8-sig") as file:
        attribution = list(csv.DictReader(file))
    attribution_json = json.loads((run_dir / "event_attribution.json").read_text(encoding="utf-8"))
    assert attribution[0]["event_type"] == "switch_out_of_benchmark"
    assert float(attribution[0]["h20_alpha_nav_change"]) > 0
    assert float(attribution[0]["h20_spread_change_pct"]) == pytest.approx(0)
    assert attribution_json[0]["to_code"] == "B"


def test_strategy_runner_persists_dynamic_anchor_history(tmp_path: Path) -> None:
    data = dynamic_anchor_data(days=64, b_high_until=64)
    result = simulate_rotation(
        data,
        codes=("A", "B", "C"),
        benchmark_code="A",
        switch_threshold=0.03,
        return_threshold=0.01,
        mode="dynamic-anchor-cycle",
        anchor_window=60,
        anchor_confirm_days=1,
        anchor_min_hold_days=1,
        anchor_switch_margin=0.001,
    )
    run_dir = persist_run(
        [result],
        {"top": 1, "codes": ("A", "B", "C"), "run_log_dir": tmp_path},
        data,
        created_at=datetime(2026, 1, 3, 9, 30, 0),
    )

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    anchor_history = json.loads((run_dir / "anchor_history.json").read_text(encoding="utf-8"))

    assert manifest["best"]["anchor_history_count"] == len(result.anchor_history)
    assert manifest["best"]["anchor_switches"] == 1
    assert anchor_history[60]["switched"] is True
    assert anchor_history[60]["anchor_code"] == "B"
    assert (run_dir / "anchor_history.csv").exists()


def test_pool_experiment_plan_keeps_159513_as_control_only() -> None:
    experiments = planned_experiments()
    names = {item.pool_name for item in experiments}

    assert "explainable_core_no_159513" in names
    assert "full_with_159513_control" in names
    assert "159513" not in POOLS["explainable_core_no_159513"]
    assert "159513" in POOLS["full_with_159513_control"]
    assert all(item.benchmark in item.codes for item in experiments)


def test_leave_one_out_pools_drop_exactly_one_code() -> None:
    pools = leave_one_out_pools()
    experiments = planned_experiments(pools, ("513390", "513100"))

    assert len(pools) == len(FULL_NASDAQ100_POOL)
    assert all(len(codes) == len(FULL_NASDAQ100_POOL) - 1 for codes in pools.values())
    assert "159513" not in pools["leave_one_out_drop_159513"]
    assert "513100" not in pools["leave_one_out_drop_513100"]
    assert all(item.benchmark in item.codes for item in experiments)


def test_513390_core_pools_exclude_159513() -> None:
    assert "159513" not in POOLS["513390_core"]
    assert "159513" not in POOLS["513390_clean"]
    assert "513390" in POOLS["513390_core"]
    assert "513390" in POOLS["513390_clean"]


def test_161130_lof_pools_are_available_for_direct_rotation() -> None:
    assert "161130" in NASDAQ100_WITH_161130_POOL
    assert NASDAQ100_LOF_PAIR_513390 == ("513390", "161130")
    assert POOLS["full_with_161130_lof"] == NASDAQ100_WITH_161130_POOL
    assert POOLS["lof_pair_513390"] == NASDAQ100_LOF_PAIR_513390


def date_for(index: int) -> str:
    return (datetime(2026, 1, 1) + timedelta(days=index)).strftime("%Y-%m-%d")


def dynamic_anchor_data(days: int, b_high_until: int = 0, c_high_from: int = 10_000):
    data = {}
    for index in range(days):
        trade_date = date_for(index)
        data[trade_date] = {
            "A": point("A", trade_date, 100, 0.02),
            "B": point("B", trade_date, 100, 0.06 if index < b_high_until else 0.02),
            "C": point("C", trade_date, 100, 0.07 if index >= c_high_from else 0.01),
        }
    return data


def point(code: str, trade_date: str, close_price: float, premium_rate: float):
    from qdii_monitor.strategy import DailyPoint

    return DailyPoint(code, trade_date, close_price, premium_rate)
