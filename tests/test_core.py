from __future__ import annotations

import datetime
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from qdii_monitor.collectors import DailyPremiumCollector, FundMetadataCollector, NoticeCollector, PurchaseStatusCollector, QuotaCollector, QuoteCollector, ReferenceCollector, _json_notices, classify_notice
from qdii_monitor.configuration import FundTarget, ReferenceTarget
from qdii_monitor.configuration import load_config
from qdii_monitor.service import _estimate_nav_from_reference, _quote_with_daily_nav_fallback
from qdii_monitor.database import Database
from qdii_monitor.holdings import parse_holdings_text, value_holding
from qdii_monitor.settings import load_local_env
from qdii_monitor.utils import calculate_premium


def test_local_env_loads_values_without_overriding_shell_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "QDII_AKSHARE_PROXY_HOST=from-file\n"
        'QDII_AKSHARE_PROXY_TOKEN="quoted-token"\n'
        "QDII_AKSHARE_PROXY_RETRY=9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QDII_AKSHARE_PROXY_HOST", "from-shell")
    monkeypatch.delenv("QDII_AKSHARE_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("QDII_AKSHARE_PROXY_RETRY", raising=False)
    load_local_env(env_file)
    assert os.environ["QDII_AKSHARE_PROXY_HOST"] == "from-shell"
    assert os.environ["QDII_AKSHARE_PROXY_TOKEN"] == "quoted-token"
    assert os.environ["QDII_AKSHARE_PROXY_RETRY"] == "9"


def test_example_config_has_three_non_empty_groups() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    assert {group.id for group in config.groups} == {"nasdaq_100", "nikkei_225", "sp_500"}
    counts = {group.id: len(group.funds) for group in config.groups}
    assert counts == {"nasdaq_100": 15, "nikkei_225": 4, "sp_500": 4}
    assert config.fund_map["159509"].comparison_eligible is False
    assert config.fund_map["161130"].comparison_eligible is False
    assert config.fund_map["161130"].inception_date == "2017-06-23"
    assert config.fund_map["161130"].official_url == "https://www.efunds.com.cn/fund/161130.shtml"
    assert config.fund_map["161130"].nav_estimate is not None
    assert config.fund_map["161130"].nav_estimate.reference_code == "NQ00Y"
    assert config.fund_map["161130"].nav_estimate.reference_weight == pytest.approx(0.95)
    assert config.fund_map["513100"].inception_date == "2013-04-25"
    assert config.fund_map["513100"].asset_size_cny == pytest.approx(15_534_000_000)
    assert config.fund_map["159513"].inception_date == "2023-07-12"
    assert config.fund_map["513000"].asset_size_cny == pytest.approx(1_657_000_000)
    assert config.fund_map["513500"].asset_size_cny == pytest.approx(20_941_000_000)
    assert config.fund_map["159659"].manager == "招商基金管理有限公司"
    assert config.groups[0].reference == ReferenceTarget("NQ00Y", "小型纳指当月连续")
    assert config.groups[1].reference == ReferenceTarget("N225", "日经225指数", "eastmoney_global_index")
    assert config.groups[2].reference == ReferenceTarget("ES00Y", "小型标普当月连续")


def test_config_rejects_duplicate_code(tmp_path: Path) -> None:
    path = tmp_path / "funds.yaml"
    path.write_text(
        """
quota_source: {name: SAFE, url: https://example.test}
groups:
  - id: one
    name: One
    funds:
      - {code: '513100', exchange: SSE, display_name: A, fund_name: A}
  - id: two
    name: Two
    funds:
      - {code: '513100', exchange: SSE, display_name: B, fund_name: B}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        load_config(path)


def test_database_initialization_migrates_quote_turnover_and_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "monitor.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE quote_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                latest_price REAL,
                iopv REAL,
                premium_rate REAL,
                source TEXT NOT NULL,
                UNIQUE(code, captured_at)
            )
            """
        )

    db = Database(db_path)
    db.initialize()
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(quote_snapshots)").fetchall()}
        metadata_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fund_metadata'"
        ).fetchone()
        anchor_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='premarket_anchor_snapshots'"
        ).fetchone()
    assert "turnover_amount" in columns
    assert "latest_shares" in columns
    assert "change_rate" in columns
    assert metadata_exists is not None
    assert anchor_exists is not None


def test_calculate_premium_rejects_missing_or_zero_iopv() -> None:
    assert calculate_premium(1.05, 1.0) == pytest.approx(0.05)
    assert calculate_premium(1.0, None) is None
    assert calculate_premium(1.0, 0) is None


def test_501312_configures_weighted_reference_nav_estimate() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    fund = config.fund_map["501312"]

    assert fund.exchange == "SSE"
    assert fund.comparison_eligible is False
    assert fund.official_url == "https://www.fsfund.com/fund/501312/fundDetail.shtml"
    assert fund.nav_estimate is not None
    assert fund.nav_estimate.reference_code == "NQ00Y"
    assert fund.nav_estimate.reference_weight == pytest.approx(0.80)


def test_weighted_reference_nav_estimate_matches_501312_example() -> None:
    estimate = _estimate_nav_from_reference(
        {"nav": 2.2470, "trade_date": "2026-06-10"},
        {"latest_price": 699.9433},
        {"baseline_price": 693.7},
        0.80,
    )

    assert estimate is not None
    assert estimate["reference_change"] == pytest.approx(0.009)
    assert estimate["estimated_nav"] == pytest.approx(2.2631784)
    assert calculate_premium(2.231, estimate["estimated_nav"]) == pytest.approx(-0.01421823)


def test_weighted_reference_nav_estimate_requires_aligned_baseline() -> None:
    assert _estimate_nav_from_reference(
        {"nav": 2.2470, "trade_date": "2026-06-10"},
        {"latest_price": 699.9433},
        None,
        0.80,
    ) is None


def test_configured_nav_estimate_does_not_fall_back_to_stale_official_nav() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    result = _quote_with_daily_nav_fallback(
        {"latest_price": 2.231, "premium_rate": None, "iopv": None},
        {"trade_date": "2026-06-09", "nav": 2.2951, "close_price": 2.231},
        config.fund_map["501312"],
        {},
        None,
    )

    assert result["premium_rate"] is None
    assert result["iopv"] is None
    assert result["premium_source"] == "estimated_nav_unavailable"
    assert "暂不计算" in result["premium_note"]


def test_estimated_nav_quote_view_recomputes_with_latest_official_nav(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-16T04:00:00+08:00",
                "latest_price": 30000.0,
                "change_rate": 0.0,
                "previous_settle": 29900.0,
                "source": "test.us_close",
            },
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-16T14:30:00+08:00",
                "latest_price": 30300.0,
                "change_rate": 0.01,
                "previous_settle": 30000.0,
                "source": "test.current",
            },
        ]
    )

    result = _quote_with_daily_nav_fallback(
        {
            "latest_price": 2.393,
            "iopv": 2.3750,
            "premium_rate": 0.0075,
            "source": "test|estimated_nav",
            "captured_at": "2026-06-16T14:30:00+08:00",
        },
        {"trade_date": "2026-06-15", "nav": 2.4061, "close_price": 2.376},
        config.fund_map["501312"],
        {"NQ00Y": {"latest_price": 30300.0}},
        db,
    )

    expected_nav = 2.4061 * (1 + 0.80 * (30300.0 / 30000.0 - 1))
    assert result["iopv"] == pytest.approx(expected_nav)
    assert result["premium_rate"] == pytest.approx(2.393 / expected_nav - 1)
    assert "30300.00 / 30000.00" in result["premium_formula"]


def test_estimated_nav_quote_view_calculates_for_newly_configured_lof(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-13T04:00:00+08:00",
                "latest_price": 29700.0,
                "change_rate": 0.0,
                "previous_settle": 29400.0,
                "source": "test.us_close",
            },
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-16T14:30:00+08:00",
                "latest_price": 30600.0,
                "change_rate": 0.03,
                "previous_settle": 30500.0,
                "source": "test.current",
            },
        ]
    )

    result = _quote_with_daily_nav_fallback(
        {
            "latest_price": 4.804,
            "iopv": None,
            "premium_rate": None,
            "source": "eastmoney.stock_quote_fallback",
            "captured_at": "2026-06-16T14:30:00+08:00",
        },
        {"trade_date": "2026-06-12", "nav": 4.4804, "close_price": 4.548},
        config.fund_map["161130"],
        {"NQ00Y": {"latest_price": 30600.0}},
        db,
    )

    expected_nav = 4.4804 * (1 + 0.95 * (30600.0 / 29700.0 - 1))
    assert result["premium_source"] == "estimated_nav"
    assert result["iopv"] == pytest.approx(expected_nav)
    assert result["premium_rate"] == pytest.approx(4.804 / expected_nav - 1)
    assert "95%" in result["premium_formula"]


def test_quote_collector_uses_iopv_for_configured_funds() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    frame = pd.DataFrame(
        [
            {"代码": "513100", "最新价": 1.05, "IOPV实时估值": 1.0},
            {"代码": "513300", "最新价": 0.95, "IOPV实时估值": 1.0},
            {"代码": "000000", "最新价": 9.0, "IOPV实时估值": 1.0},
        ]
    )
    rows = QuoteCollector(fetcher=lambda: frame).collect(config.funds)
    assert [row["code"] for row in rows] == ["513100", "513300"]
    assert rows[0]["premium_rate"] == pytest.approx(0.05)
    assert rows[1]["premium_rate"] == pytest.approx(-0.05)
    assert rows[0]["turnover_amount"] is None
    assert rows[0]["latest_shares"] is None


def test_quote_collector_parses_turnover_amount() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    frame = pd.DataFrame(
        [
            {"代码": "513100", "最新价": 1.05, "IOPV实时估值": 1.0, "成交额": 123456789},
        ]
    )
    rows = QuoteCollector(fetcher=lambda: frame).collect(config.funds)
    assert rows[0]["turnover_amount"] == pytest.approx(123456789)


def test_quote_collector_parses_change_rate() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    frame = pd.DataFrame(
        [
            {"代码": "513100", "最新价": 1.05, "IOPV实时估值": 1.0, "涨跌幅": 2.35},
        ]
    )
    rows = QuoteCollector(fetcher=lambda: frame).collect(config.funds)
    assert rows[0]["change_rate"] == pytest.approx(0.0235)


def test_quote_collector_parses_latest_shares() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    frame = pd.DataFrame(
        [
            {"代码": "513100", "最新价": 1.05, "IOPV实时估值": 1.0, "最新份额": 200000000},
        ]
    )
    rows = QuoteCollector(fetcher=lambda: frame).collect(config.funds)
    assert rows[0]["latest_shares"] == pytest.approx(200000000)


def test_quote_collector_falls_back_to_single_symbol_for_missing_sse_lof() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "funds.yaml")
    frame = pd.DataFrame(
        [
            {"代码": "513100", "最新价": 1.05, "IOPV实时估值": 1.0},
        ]
    )

    def fallback(fund: FundTarget) -> dict[str, float] | None:
        if fund.code != "501312":
            return None
        return {"f43": 2238, "f48": 128706559, "f170": -53}

    rows = QuoteCollector(fetcher=lambda: frame, fallback_fetcher=fallback).collect(config.funds)
    lof = next(row for row in rows if row["code"] == "501312")

    assert lof["latest_price"] == pytest.approx(2.238)
    assert lof["change_rate"] == pytest.approx(-0.0053)
    assert lof["turnover_amount"] == pytest.approx(128706559)
    assert lof["iopv"] is None
    assert lof["premium_rate"] is None
    assert lof["source"] == "eastmoney.stock_quote_fallback"


def test_database_lists_and_reads_quote_snapshots(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_quotes(
        [
            {
                "code": "513100",
                "captured_at": "2026-06-08T09:35:00+08:00",
                "latest_price": 2.10,
                "change_rate": -0.01,
                "iopv": 2.00,
                "premium_rate": 0.05,
                "turnover_amount": 1000000,
                "latest_shares": None,
                "source": "test",
            },
            {
                "code": "161130",
                "captured_at": "2026-06-08T09:35:00+08:00",
                "latest_price": 4.50,
                "change_rate": -0.02,
                "iopv": 4.42,
                "premium_rate": 4.50 / 4.42 - 1,
                "premium_source": "estimated_nav",
                "premium_formula": "4.4000 × [1 + 95% × (30100.00 / 30000.00 - 1)] = 4.4139",
                "estimated_nav": 4.42,
                "nav_date": "2026-06-07",
                "nav_value": 4.40,
                "reference_code": "NQ00Y",
                "reference_captured_at": "2026-06-08T09:35:00+08:00",
                "reference_price": 30100.0,
                "baseline_captured_at": "2026-06-08T04:00:00+08:00",
                "baseline_price": 30000.0,
                "baseline_kind": "us_close_anchor",
                "reference_weight": 0.95,
                "reference_change": 30100.0 / 30000.0 - 1,
                "weighted_change": 0.95 * (30100.0 / 30000.0 - 1),
                "turnover_amount": 2000000,
                "latest_shares": None,
                "source": "test",
            },
        ]
    )

    snapshots = db.quote_snapshot_times()
    assert snapshots == [{"captured_at": "2026-06-08T09:35:00+08:00", "fund_count": 2}]
    rows = db.quotes_at("2026-06-08T09:35:00+08:00")
    assert rows["513100"]["premium_rate"] == pytest.approx(0.05)
    assert rows["161130"]["premium_source"] == "estimated_nav"
    assert rows["161130"]["estimated_nav"] == pytest.approx(4.42)
    assert rows["161130"]["baseline_kind"] == "us_close_anchor"
    assert "95%" in rows["161130"]["premium_formula"]


def test_reference_nav_baseline_requires_next_trading_weekday(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-11T13:30:00+08:00",
                "latest_price": 700.0,
                "change_rate": 0.01,
                "previous_settle": 693.0,
                "source": "test",
            }
        ]
    )

    assert db.reference_nav_baseline("NQ00Y", "2026-06-10") is not None
    assert db.reference_nav_baseline("NQ00Y", "2026-06-09") is None


def test_reference_nav_baseline_prefers_us_close_anchor(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-11T03:59:00+08:00",
                "latest_price": 695.0,
                "change_rate": 0.0029,
                "previous_settle": 690.0,
                "source": "test.intraday",
            },
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-11T06:00:00+08:00",
                "latest_price": 700.0,
                "change_rate": 0.01,
                "previous_settle": 693.0,
                "source": "test.previous_settle",
            },
        ]
    )

    baseline = db.reference_nav_baseline("NQ00Y", "2026-06-10")

    assert baseline is not None
    assert baseline["baseline_kind"] == "us_close_anchor"
    assert baseline["baseline_price"] == pytest.approx(695.0)
    assert baseline["captured_at"] == "2026-06-11T03:59:00+08:00"


def test_database_filters_quote_snapshot_times_by_daily_mode(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    rows = []
    for captured_at, price in (
        ("2026-06-05T03:58:00+08:00", 1.98),
        ("2026-06-05T09:35:00+08:00", 2.00),
        ("2026-06-05T15:00:00+08:00", 2.10),
        ("2026-06-08T04:02:00+08:00", 2.18),
        ("2026-06-08T09:35:00+08:00", 2.20),
        ("2026-06-08T15:00:00+08:00", 2.30),
    ):
        rows.append(
            {
                "code": "513100",
                "captured_at": captured_at,
                "latest_price": price,
                "change_rate": 0.0,
                "iopv": 2.0,
                "premium_rate": price / 2.0 - 1,
                "turnover_amount": 1000000,
                "latest_shares": None,
                "source": "test",
            }
        )
    db.insert_quotes(rows)

    assert [row["captured_at"] for row in db.quote_snapshot_times(mode="latest")] == [
        "2026-06-08T15:00:00+08:00",
        "2026-06-08T09:35:00+08:00",
        "2026-06-08T04:02:00+08:00",
        "2026-06-05T15:00:00+08:00",
        "2026-06-05T09:35:00+08:00",
        "2026-06-05T03:58:00+08:00",
    ]
    assert [row["captured_at"] for row in db.quote_snapshot_times(mode="open")] == [
        "2026-06-08T09:35:00+08:00",
        "2026-06-05T09:35:00+08:00",
    ]
    assert [row["captured_at"] for row in db.quote_snapshot_times(mode="close")] == [
        "2026-06-08T15:00:00+08:00",
        "2026-06-05T15:00:00+08:00",
    ]
    assert [row["captured_at"] for row in db.quote_snapshot_times(mode="us_close")] == [
        "2026-06-08T04:02:00+08:00",
        "2026-06-05T03:58:00+08:00",
    ]


def test_database_cn_close_reference_skips_weekend_snapshots(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-05T15:00:00+08:00",
                "latest_price": 28000.0,
                "change_rate": 0.0,
                "previous_settle": 28000.0,
                "source": "test",
            },
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-07T17:36:22+08:00",
                "latest_price": 28824.68,
                "change_rate": 0.0,
                "previous_settle": 28824.68,
                "source": "test.weekend",
            },
        ]
    )

    row = db.cn_close_reference_snapshot("NQ00Y", "2026-06-08T09:00:00+08:00")
    assert row is not None
    assert row["captured_at"] == "2026-06-05T15:00:00+08:00"
    assert row["latest_price"] == pytest.approx(28000.0)


def test_database_cn_close_reference_uses_today_close_after_close(tmp_path: Path) -> None:
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-05T15:00:00+08:00",
                "latest_price": 28000.0,
                "change_rate": 0.0,
                "previous_settle": 28000.0,
                "source": "test.friday_close",
            },
            {
                "code": "NQ00Y",
                "captured_at": "2026-06-08T15:00:00+08:00",
                "latest_price": 29200.0,
                "change_rate": 0.01,
                "previous_settle": 28910.0,
                "source": "test.monday_close",
            },
        ]
    )

    row = db.cn_close_reference_snapshot("NQ00Y", "2026-06-08T18:04:05+08:00")
    assert row is not None
    assert row["captured_at"] == "2026-06-08T15:00:00+08:00"
    assert row["latest_price"] == pytest.approx(29200.0)


def test_fund_metadata_collector_parses_detail_and_scale_fallback() -> None:
    detail = pd.DataFrame(
        [
            {"item": "成立时间", "value": "2023-01-02"},
            {"item": "最新规模", "value": "12.5亿"},
        ]
    )
    sse_scale = pd.DataFrame([{"基金代码": "513300", "基金份额": 100000000, "净值": 2.0}])
    collector = FundMetadataCollector(
        detail_fetcher=lambda code: detail if code == "513100" else pd.DataFrame(),
        sse_scale_fetcher=lambda: sse_scale,
        szse_scale_fetcher=lambda: pd.DataFrame(),
    )

    rows, warnings = collector.collect(
        (
            FundTarget("513100", "SSE", "纳指ETF", "纳指ETF"),
            FundTarget("513300", "SSE", "纳指ETF", "纳指ETF"),
        )
    )

    assert warnings == []
    first = next(row for row in rows if row["code"] == "513100")
    fallback = next(row for row in rows if row["code"] == "513300")
    assert first["inception_date"] == "2023-01-02"
    assert first["asset_size_cny"] == pytest.approx(1_250_000_000)
    assert first["size_source"] == "资产规模"
    assert fallback["asset_size_cny"] == pytest.approx(200_000_000)
    assert fallback["size_source"] == "份额估算"


def test_daily_premium_collector_uses_close_and_official_nav() -> None:
    fund = FundTarget("513100", "SSE", "纳指ETF", "纳指ETF")
    prices = pd.DataFrame([{"日期": "2026-05-22", "收盘": 1.10}, {"日期": "2026-05-25", "收盘": 1.12}])
    nav = pd.DataFrame([{"净值日期": "2026-05-22", "单位净值": 1.00}, {"净值日期": "2026-05-25", "单位净值": 1.02}])
    collector = DailyPremiumCollector(
        price_fetcher=lambda code, start_date, end_date: prices,
        nav_fetcher=lambda code: nav,
    )
    collector._target_date = lambda today=None: datetime.date(2026, 5, 25)  # type: ignore[method-assign]
    rows, warnings, detail = collector.collect((fund,), days=30)
    assert warnings == []
    assert detail["price_requests"] == 1
    assert rows[0]["trade_date"] == "2026-05-22"
    assert rows[0]["premium_rate"] == pytest.approx(0.10)
    assert rows[1]["premium_rate"] == pytest.approx(1.12 / 1.02 - 1)


def test_daily_premium_collector_merges_official_page_nav() -> None:
    fund = FundTarget(
        "501312",
        "SSE",
        "海外科技LOF华宝",
        "华宝海外科技股票型证券投资基金（QDII-LOF）A",
        official_url="https://www.fsfund.com/fund/501312/fundDetail.shtml",
    )
    prices = pd.DataFrame([{"日期": "2026-06-15", "收盘": 2.43}])
    stale_nav = pd.DataFrame([{"净值日期": "2026-06-12", "单位净值": 2.3205}])
    official_nav = pd.DataFrame([{"净值日期": "2026-06-15", "单位净值": 2.4061}])
    collector = DailyPremiumCollector(
        price_fetcher=lambda code, start_date, end_date: prices,
        nav_fetcher=lambda code: stale_nav,
        official_nav_fetcher=lambda current_fund: official_nav,
    )
    collector._target_date = lambda today=None: datetime.date(2026, 6, 15)  # type: ignore[method-assign]

    rows, warnings, _ = collector.collect((fund,), days=30)

    assert warnings == []
    assert rows[0]["trade_date"] == "2026-06-15"
    assert rows[0]["nav"] == pytest.approx(2.4061)
    assert rows[0]["premium_rate"] == pytest.approx(2.43 / 2.4061 - 1)


def test_daily_premium_collector_parses_efunds_official_nav(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        apparent_encoding = "utf-8"
        encoding = "utf-8"
        text = """
        4.4804
        单位净值(元)
        0.58%
        日涨跌
        基金净值日期：2026-06-12
        """

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("qdii_monitor.collectors.requests.get", lambda *args, **kwargs: FakeResponse())
    fund = FundTarget(
        "161130",
        "SZSE",
        "纳指LOF易方达",
        "易方达纳斯达克100ETF联接(QDII-LOF)A(人民币)",
        official_url="https://www.efunds.com.cn/fund/161130.shtml",
    )

    nav = DailyPremiumCollector._fetch_official_nav(fund)

    assert nav.to_dict("records") == [{"净值日期": "2026-06-12", "单位净值": 4.4804}]


def test_daily_premium_collector_falls_back_to_lof_price_history(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    lof_prices = pd.DataFrame([{"日期": "2026-05-22", "收盘": 1.10}])

    def etf_fetcher(**kwargs: str) -> pd.DataFrame:
        calls.append("etf")
        return pd.DataFrame()

    def lof_fetcher(**kwargs: str) -> pd.DataFrame:
        calls.append("lof")
        return lof_prices

    fake_akshare = SimpleNamespace(fund_etf_hist_em=etf_fetcher, fund_lof_hist_em=lof_fetcher)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    result = DailyPremiumCollector._fetch_price("161130", "20260501", "20260525")

    assert calls == ["etf", "lof"]
    assert result is lof_prices


def test_daily_premium_collector_skips_cached_target_without_requests() -> None:
    fund = FundTarget("513100", "SSE", "纳指ETF", "纳指ETF")
    called: list[str] = []
    collector = DailyPremiumCollector(
        price_fetcher=lambda *args: called.append("price"),
        nav_fetcher=lambda *args: called.append("nav"),
    )
    collector._target_date = lambda today=None: datetime.date(2026, 5, 25)  # type: ignore[method-assign]
    rows, warnings, detail = collector.collect((fund,), days=30, latest_dates={"513100": "2026-05-25"})
    assert rows == []
    assert warnings == []
    assert called == []
    assert detail["price_requests"] == 0
    assert detail["skipped_codes"] == 1


def test_daily_premium_collector_backfills_when_cached_window_is_too_short() -> None:
    fund = FundTarget("513100", "SSE", "纳指ETF", "纳指ETF")
    requested: list[tuple[str, str, str]] = []
    prices = pd.DataFrame([{"日期": "2026-05-22", "收盘": 1.10}])
    nav = pd.DataFrame([{"净值日期": "2026-05-22", "单位净值": 1.00}])

    def fetch_price(code: str, start_date: str, end_date: str) -> pd.DataFrame:
        requested.append((code, start_date, end_date))
        return prices

    collector = DailyPremiumCollector(price_fetcher=fetch_price, nav_fetcher=lambda code: nav)
    collector._target_date = lambda today=None: datetime.date(2026, 5, 25)  # type: ignore[method-assign]
    rows, warnings, detail = collector.collect(
        (fund,),
        days=365,
        date_bounds={"513100": {"earliest": "2026-01-01", "latest": "2026-05-25"}},
    )
    assert warnings == []
    assert requested == [("513100", "20250525", "20260525")]
    assert detail["backfill_codes"] == 1
    assert rows[0]["premium_rate"] == pytest.approx(0.10)


def test_daily_premium_collector_warns_when_no_new_nav_rows_are_available() -> None:
    fund = FundTarget("513100", "SSE", "纳指ETF", "纳指ETF")
    prices = pd.DataFrame([{"日期": "2026-05-25", "收盘": 1.10}])
    nav = pd.DataFrame([{"净值日期": "2026-05-22", "单位净值": None}])
    collector = DailyPremiumCollector(
        price_fetcher=lambda code, start_date, end_date: prices,
        nav_fetcher=lambda code: nav,
    )
    collector._target_date = lambda today=None: datetime.date(2026, 5, 25)  # type: ignore[method-assign]
    rows, warnings, detail = collector.collect((fund,), days=30)
    assert rows == []
    assert warnings == ["No new daily premium rows were produced; official NAV may not be available yet."]
    assert detail["requested_codes"] == 1


def test_reference_collector_normalizes_global_futures_change_rate() -> None:
    rows, warnings = ReferenceCollector(
        fetcher=lambda: [{"dm": "NQ00Y", "p": 24500.5, "zdf": 1.25, "zjsj": 24200.0}]
    ).collect((ReferenceTarget("NQ00Y", "小型纳指当月连续"),))
    assert warnings == []
    assert rows[0]["latest_price"] == pytest.approx(24500.5)
    assert rows[0]["change_rate"] == pytest.approx(0.0125)
    assert rows[0]["previous_settle"] == pytest.approx(24200)


class _EastmoneyIndexResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"data": {"f43": 6515819, "f60": 6333907, "f170": 287}}


class _EastmoneyIndexSession:
    headers: dict[str, str] = {}

    def get(
        self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int
    ) -> _EastmoneyIndexResponse:
        assert "push2delay.eastmoney.com" in url
        assert params["secid"] == "100.N225"
        return _EastmoneyIndexResponse()


def test_reference_collector_supports_eastmoney_nikkei_index() -> None:
    rows, warnings = ReferenceCollector(session=_EastmoneyIndexSession()).collect(  # type: ignore[arg-type]
        (ReferenceTarget("N225", "日经225指数", "eastmoney_global_index"),)
    )
    assert warnings == []
    assert rows[0]["latest_price"] == pytest.approx(65158.19)
    assert rows[0]["change_rate"] == pytest.approx(0.0287)
    assert rows[0]["source"] == "eastmoney.global_index"


class _EastmoneyTrendsResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "data": {
                "preSettlement": 29558.75,
                "trends": [
                    "2026-05-26 06:00,29965.65,29965.65,29965.65,29965.65,0,0.00,0.000",
                    "2026-05-26 06:01,29965.65,29924.62,29995.00,29676.25,0,0.00,0.000",
                ],
            }
        }


class _EastmoneyTrendsSession:
    headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> _EastmoneyTrendsResponse:
        assert "trends2/get" in url
        assert params["secid"] == "103.NQ00Y"
        return _EastmoneyTrendsResponse()


def test_reference_collector_normalizes_eastmoney_intraday_prices() -> None:
    collector = ReferenceCollector(session=_EastmoneyTrendsSession())  # type: ignore[arg-type]
    rows = collector._fetch_eastmoney_intraday("103.NQ00Y", "NQ00Y", "eastmoney.futures_global_intraday")
    assert rows[0]["captured_at"] == "2026-05-26T06:00+08:00"
    assert rows[0]["previous_settle"] == 29558.75
    assert rows[1]["latest_price"] == 29924.62
    assert rows[1]["change_rate"] == pytest.approx(29924.62 / 29558.75 - 1)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("关于恢复申购业务的公告", "恢复申购"),
        ("暂停申购及定期定额投资业务公告", "暂停申购"),
        ("限制大额申购业务的公告", "限制申购"),
        ("基金季度报告", None),
    ],
)
def test_notice_classification(title: str, expected: str | None) -> None:
    assert classify_notice(title) == expected


def test_official_notice_json_payload_is_normalized() -> None:
    rows = _json_notices(
        {
            "data": [
                {
                    "bulletinTitle": "关于恢复申购业务的公告",
                    "publishDate": "2026-05-20",
                    "docURL": "/docs/notice.pdf",
                }
            ]
        },
        "https://www.sse.com.cn",
    )
    assert rows == [
        {
            "title": "关于恢复申购业务的公告",
            "published_at": "2026-05-20",
            "url": "https://www.sse.com.cn/docs/notice.pdf",
        }
    ]


class _NoticeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class _NoticeSession:
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.get_params: dict[str, str] | None = None
        self.post_json: dict[str, object] | None = None

    def get(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> _NoticeResponse:
        self.get_params = params
        return _NoticeResponse(
            {
                "pageHelp": {
                    "data": [
                        {
                            "TITLE": "关于恢复申购业务的公告",
                            "SSEDATE": "2026-05-20",
                            "URL": "/documents/sse.pdf",
                        }
                    ]
                }
            }
        )

    def post(self, url: str, json: dict[str, object], headers: dict[str, str], timeout: int) -> _NoticeResponse:
        self.post_json = json
        return _NoticeResponse(
            {"data": [{"title": "关于暂停申购业务的公告", "publishTime": "2026-05-19", "attachPath": "/documents/szse.pdf"}]}
        )


def test_exchange_notice_requests_use_current_official_shapes() -> None:
    session = _NoticeSession()
    collector = NoticeCollector(session=session)  # type: ignore[arg-type]
    sse = collector._sse(FundTarget("513100", "SSE", "纳指ETF", "纳指ETF"))
    szse = collector._szse(FundTarget("159941", "SZSE", "纳指ETF", "纳指ETF"))
    assert session.get_params and session.get_params["sqlId"] == "COMMON_PL_JJXX_JJGG_NEW_L"
    assert session.get_params["SECURITY_CODE"] == "513100"
    assert session.post_json == {
        "type": 2,
        "pageSize": 100,
        "pageNum": 1,
        "stock": ["159941"],
        "channelCode": ["fundinfoNotice_disc"],
    }
    assert sse[0]["url"] == "https://www.sse.com.cn/documents/sse.pdf"
    assert szse[0]["url"] == "https://disc.static.szse.cn/documents/szse.pdf"


def test_purchase_status_collector_filters_configured_funds_and_keeps_limits() -> None:
    funds = (
        FundTarget("513100", "SSE", "纳指ETF", "纳指ETF"),
        FundTarget("513300", "SSE", "纳指ETF", "纳指ETF"),
    )
    frame = pd.DataFrame(
        [
            {"基金代码": "513100", "申购状态": "开放申购", "日累计限定金额": 100000000, "下一开放日": None},
            {"基金代码": "513300", "申购状态": "暂停申购", "日累计限定金额": 0, "下一开放日": "2026-05-27"},
            {"基金代码": "510300", "申购状态": "开放申购", "日累计限定金额": 100000000, "下一开放日": None},
        ]
    )
    rows = PurchaseStatusCollector(fetcher=lambda: frame).collect(funds)
    assert [row["code"] for row in rows] == ["513100", "513300"]
    assert rows[0]["daily_limit"] == 100000000
    assert rows[1]["purchase_status"] == "暂停申购"
    assert rows[1]["next_open_date"] == "2026-05-27"


class _Response:
    content = (
        '<html><title>合格投资者额度审批情况表</title><body>'
        '<a href="/files/qdii-20260430.xlsx">合格境内机构投资者（QDII）投资额度审批情况表</a>'
        "</body></html>"
    ).encode("utf-8")

    def raise_for_status(self) -> None:
        return None


class _Session:
    headers: dict[str, str] = {}

    def get(self, url: str, timeout: int) -> _Response:
        return _Response()


def test_quota_html_collects_official_document_links_and_changes() -> None:
    collector = QuotaCollector(session=_Session())  # type: ignore[arg-type]
    first = collector.collect({"name": "SAFE", "url": "https://www.safe.gov.cn/list/"})
    assert first["items"][0]["url"] == "https://www.safe.gov.cn/files/qdii-20260430.xlsx"
    assert len(first["new_items"]) == 1
    second = collector.collect({"name": "SAFE", "url": "https://www.safe.gov.cn/list/"}, first)
    assert second["new_items"] == []


def test_holdings_are_filtered_and_valued() -> None:
    content = (
        "header\n"
        "1 513100 纳指ETF 10000 1000 0 0 0 10 0 0 0 0 9.500\n"
        "2 510300 沪深300 20000 1000 0 0 0 10 0 0 0 0 3.500\n"
    ).encode("utf-8")
    rows = parse_holdings_text(content, {"513100"})
    assert rows == [{"code": "513100", "name": "纳指ETF", "shares": 1000.0, "average_cost": 9.5}]
    valued = value_holding(rows[0], 10.0)
    assert valued["market_value"] == 10000
    assert valued["unrealized_pnl"] == 500
