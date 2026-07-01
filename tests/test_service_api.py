from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qdii_monitor.app import create_app
from qdii_monitor.collectors import QuoteCollector
from qdii_monitor.configuration import load_config
from qdii_monitor.database import Database
from qdii_monitor.service import MonitorService


ROOT = Path(__file__).parents[1]


class FakeNoticeCollector:
    def collect(self, config: Any) -> list[dict[str, Any]]:
        return [
            {
                "source": "SSE",
                "code": "513100",
                "exchange": "SSE",
                "title": "纳指ETF恢复申购业务的公告",
                "notice_type": "恢复申购",
                "published_at": "2026-05-20",
                "url": "https://example.test/notice.pdf",
                "fetched_at": "2026-05-25T10:00:00+08:00",
            },
            {
                "source": "SSE",
                "code": "513100",
                "exchange": "SSE",
                "title": "纳指ETF暂停申购业务的公告",
                "notice_type": "暂停申购",
                "published_at": "2026-04-01",
                "url": "https://example.test/old-notice.pdf",
                "fetched_at": "2026-05-25T10:00:00+08:00",
            },
            {
                "source": "SSE",
                "code": "513100",
                "exchange": "SSE",
                "title": "纳指ETF恢复申购业务的公告",
                "notice_type": "恢复申购",
                "published_at": "2026-05-20",
                "url": "https://example.test/notice.pdf",
                "fetched_at": "2026-05-25T10:00:00+08:00",
            },
        ]


class FakePurchaseStatusCollector:
    def collect(self, funds: Any) -> list[dict[str, Any]]:
        return [
            {
                "code": "513100",
                "purchase_status": "暂停申购",
                "daily_limit": 0,
                "next_open_date": "2026-05-27",
                "source": "akshare.fund_purchase_em",
                "captured_at": "2026-05-25T10:00:00+08:00",
            },
            {
                "code": "513300",
                "purchase_status": "开放申购",
                "daily_limit": 100000000,
                "next_open_date": None,
                "source": "akshare.fund_purchase_em",
                "captured_at": "2026-05-25T10:00:00+08:00",
            },
        ]


class ExchangeOnlyPurchaseStatusCollector:
    def collect(self, funds: Any) -> list[dict[str, Any]]:
        return [
            {
                "code": "513100",
                "purchase_status": "场内交易",
                "daily_limit": 0,
                "next_open_date": None,
                "source": "akshare.fund_purchase_em",
                "captured_at": "2026-05-25T10:00:00+08:00",
            }
        ]


class FailedPurchaseStatusCollector:
    def collect(self, funds: Any) -> list[dict[str, Any]]:
        raise RuntimeError("timeout")


class FakeQuotaCollector:
    def __init__(self, items: list[dict[str, str]] | None = None):
        self.items = items or [{"title": "QDII额度审批情况表 2026-04-30", "url": "https://example.test/q.pdf"}]

    def collect(self, source: dict[str, str], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        old = {(item["title"], item["url"]) for item in (previous or {}).get("items", [])}
        return {
            "title": source["name"],
            "url": source["url"],
            "document_hash": "|".join(item["title"] for item in self.items),
            "fetched_at": "2026-05-25T10:00:00+08:00",
            "items": self.items,
            "new_items": [item for item in self.items if (item["title"], item["url"]) not in old],
        }


class FakeReferenceCollector:
    def collect(self, references: Any) -> tuple[list[dict[str, Any]], list[str]]:
        return ([
            {
                "code": "NQ00Y",
                "captured_at": "2026-05-25T04:00:00+08:00",
                "latest_price": 24200.0,
                "change_rate": 0.0,
                "previous_settle": 24100.0,
                "source": "eastmoney.futures_global_intraday",
            },
            {
                "code": "NQ00Y",
                "captured_at": "2026-05-25T10:00:00+08:00",
                "latest_price": 24500.5,
                "change_rate": 0.0125,
                "previous_settle": 24200.0,
                "source": "eastmoney.futures_global_spot",
            },
            {
                "code": "N225",
                "captured_at": "2026-05-25T10:00:00+08:00",
                "latest_price": 65158.19,
                "change_rate": 0.0077,
                "previous_settle": 64660.3,
                "source": "eastmoney.global_index",
            },
            {
                "code": "ES00Y",
                "captured_at": "2026-05-25T10:00:00+08:00",
                "latest_price": 7500.0,
                "change_rate": 0.009,
                "previous_settle": 7433.1,
                "source": "eastmoney.futures_global_spot",
            },
        ] if references else [], [])


class FakeDailyPremiumCollector:
    def collect(
        self,
        funds: Any,
        days: int = 1095,
        latest_dates: dict[str, str] | None = None,
        date_bounds: dict[str, dict[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        return (
            [
                {
                    "code": "513100",
                    "trade_date": "2026-05-22",
                    "close_price": 1.08,
                    "nav": 1.00,
                    "premium_rate": 0.08,
                    "source": "akshare.close_nav_daily",
                    "fetched_at": "2026-05-25T10:00:00+08:00",
                },
                {
                    "code": "513100",
                    "trade_date": "2026-05-25",
                    "close_price": 1.10,
                    "nav": 1.00,
                    "premium_rate": 0.10,
                    "source": "akshare.close_nav_daily",
                    "fetched_at": "2026-05-25T10:00:00+08:00",
                },
            ],
            [],
            {"target_date": "2026-05-22", "requested_codes": 1, "skipped_codes": 20, "price_requests": 1, "nav_requests": 1},
        )


class FakeFundMetadataCollector:
    def collect(self, funds: Any) -> tuple[list[dict[str, Any]], list[str]]:
        rows = []
        for fund in funds:
            rows.append(
                {
                    "code": fund.code,
                    "inception_date": "2023-01-02" if fund.code == "513100" else None,
                    "asset_size_cny": 5_000_000_000 if fund.code == "513100" else 500_000_000,
                    "size_source": "资产规模",
                    "metadata_source": "test",
                    "metadata_fetched_at": "2026-05-25T10:00:00+08:00",
                }
            )
        return rows, []


class PartiallyFailedReferenceCollector:
    def collect(self, references: Any) -> tuple[list[dict[str, Any]], list[str]]:
        return (
            [
                {
                    "code": "NQ00Y",
                    "captured_at": "2026-05-25T10:00:00+08:00",
                    "latest_price": 24500.5,
                    "change_rate": 0.0125,
                    "previous_settle": 24200.0,
                    "source": "eastmoney.futures_global_spot",
                }
            ],
            ["日经225指数采集失败: timeout"],
        )


def quote_collector() -> QuoteCollector:
    return QuoteCollector(
        fetcher=lambda: pd.DataFrame(
            [
                {"代码": "513100", "最新价": 1.10, "涨跌幅": 1.23, "IOPV实时估值": 1.00, "成交额": 3000000, "最新份额": 200000000},
                {"代码": "513300", "最新价": 1.02, "涨跌幅": -0.15, "IOPV实时估值": 1.00, "成交额": 120000000, "最新份额": 100000000},
                {"代码": "513390", "最新价": 1.06, "涨跌幅": 0.35, "IOPV实时估值": 1.00, "成交额": 90000000, "最新份额": 100000000},
                {"代码": "159941", "最新价": 1.04, "涨跌幅": 0.52, "IOPV实时估值": None, "成交额": 5000000},
                {"代码": "159509", "最新价": 0.90, "涨跌幅": 3.20, "IOPV实时估值": 1.00, "成交额": 30000000},
                {"代码": "161130", "最新价": 1.052268347107438, "涨跌幅": 0.20, "IOPV实时估值": None, "成交额": 4000000},
                {"代码": "501312", "最新价": 2.231, "涨跌幅": -0.76, "IOPV实时估值": None, "成交额": 6000000},
                {"代码": "513880", "最新价": 2.01, "涨跌幅": 0.40, "IOPV实时估值": 2.00, "成交额": 20000000},
                {"代码": "513520", "最新价": 2.04, "涨跌幅": 0.70, "IOPV实时估值": 2.00, "成交额": 8000000},
                {"代码": "513500", "最新价": 1.50, "涨跌幅": 0.00, "IOPV实时估值": 1.50, "成交额": 7000000},
            ]
        )
    )


def build_service(tmp_path: Path, quota: FakeQuotaCollector | None = None) -> MonitorService:
    config = load_config(ROOT / "config" / "funds.yaml")
    db = Database(tmp_path / "monitor.sqlite")
    db.initialize()
    db.replace_daily_premiums(
        [
            {
                "code": "161130",
                "trade_date": "2026-05-24",
                "close_price": 1.04,
                "nav": 1.00,
                "premium_rate": 0.04,
                "source": "test",
                "fetched_at": "2026-05-25T15:00:00+08:00",
            },
            {
                "code": "501312",
                "trade_date": "2026-05-24",
                "close_price": 2.231,
                "nav": 2.247,
                "premium_rate": 2.231 / 2.247 - 1,
                "source": "test",
                "fetched_at": "2026-05-25T15:00:00+08:00",
            },
            {
                "code": "513100",
                "trade_date": "2026-05-24",
                "close_price": 1.08,
                "nav": 1.00,
                "premium_rate": 0.08,
                "source": "test",
                "fetched_at": "2026-05-25T15:00:00+08:00",
            },
            {
                "code": "513300",
                "trade_date": "2026-05-24",
                "close_price": 1.01,
                "nav": 1.00,
                "premium_rate": 0.01,
                "source": "test",
                "fetched_at": "2026-05-25T15:00:00+08:00",
            },
            {
                "code": "513390",
                "trade_date": "2026-05-24",
                "close_price": 1.04,
                "nav": 1.00,
                "premium_rate": 0.04,
                "source": "test",
                "fetched_at": "2026-05-25T15:00:00+08:00",
            }
        ]
    )
    db.insert_references(
        [
            {
                "code": "NQ00Y",
                "captured_at": "2026-05-22T15:00:00+08:00",
                "latest_price": 25000.0,
                "change_rate": -0.002,
                "previous_settle": 25050.0,
                "source": "test.previous_cn_close",
            },
            {
                "code": "N225",
                "captured_at": "2026-05-22T15:00:00+08:00",
                "latest_price": 65000.0,
                "change_rate": 0.001,
                "previous_settle": 64935.0,
                "source": "test.previous_cn_close",
            },
            {
                "code": "ES00Y",
                "captured_at": "2026-05-22T15:00:00+08:00",
                "latest_price": 7400.0,
                "change_rate": 0.001,
                "previous_settle": 7392.6,
                "source": "test.previous_cn_close",
            },
        ]
    )
    return MonitorService(
        config,
        db,
        quote_collector=quote_collector(),
        notice_collector=FakeNoticeCollector(),
        purchase_status_collector=FakePurchaseStatusCollector(),
        quota_collector=quota or FakeQuotaCollector(),
        reference_collector=FakeReferenceCollector(),
        daily_premium_collector=FakeDailyPremiumCollector(),
        fund_metadata_collector=FakeFundMetadataCollector(),
    )


def test_service_marks_lowest_premium_and_deduplicates_notices(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.refresh_quotes()
    service.refresh_metadata()
    service.refresh_notices()
    service.refresh_notices()
    dashboard = service.dashboard()
    nasdaq = next(group for group in dashboard["groups"] if group["id"] == "nasdaq_100")
    nikkei = next(group for group in dashboard["groups"] if group["id"] == "nikkei_225")
    sp500 = next(group for group in dashboard["groups"] if group["id"] == "sp_500")
    lowest = [row["code"] for row in nasdaq["rows"] if row["is_lowest_premium"]]
    assert lowest == ["513300"]
    assert len(dashboard["notices"]) == 2
    fund = next(row for row in nasdaq["rows"] if row["code"] == "513100")
    assert fund["latest_notice"]["notice_type"] == "恢复申购"
    assert fund["purchase_status"]["purchase_status"] == "暂停申购"
    assert fund["purchase_status"]["daily_limit"] == 0
    assert fund["purchase_verification"] == "与最新正式公告方向不同，待核验"
    assert fund["turnover_amount"] == pytest.approx(3_000_000)
    assert fund["change_rate"] == pytest.approx(0.0123)
    assert fund["metadata"]["inception_date"] == "2013-04-25"
    assert fund["metadata"]["asset_size_cny"] == pytest.approx(15_534_000_000)
    assert fund["metadata"]["size_source"] == "资产规模"
    assert fund["premium_factor"]["history_mean"] == pytest.approx(0.08)
    assert fund["premium_factor"]["time_deviation"] == pytest.approx(0.02)
    assert fund["premium_factor"]["pool_median"] == pytest.approx(0.06)
    assert fund["premium_factor"]["cross_deviation"] == pytest.approx(0.04)
    assert fund["premium_factor"]["combined_deviation"] == pytest.approx(0.7 * 0.02 + 0.3 * 0.04)
    assert nasdaq["reference"]["code"] == "NQ00Y"
    assert nasdaq["reference"]["change_rate"] == 0.0125
    assert nasdaq["premarket_anchor"]["reference_code"] == "NQ00Y"
    assert nasdaq["premarket_anchor"]["baseline_captured_at"] == "2026-05-22T15:00:00+08:00"
    assert nasdaq["premarket_anchor"]["expected_change_rate"] == pytest.approx(24500.5 / 25000.0 - 1)
    assert nasdaq["premarket_anchor"]["futures_change_rate"] == pytest.approx(0.0125)
    assert nasdaq["premarket_anchor"]["rate_impact_level"] == "not_tracked"
    assert dashboard["premarket_anchor_history"]["nasdaq_100"][0]["expected_change_rate"] == pytest.approx(24500.5 / 25000.0 - 1)
    assert dashboard["reference_history"]["NQ00Y"][-1]["latest_price"] == 24500.5
    assert dashboard["reference_history"]["NQ00Y"][-1]["previous_settle"] == 24200.0
    special = next(row for row in nasdaq["rows"] if row["code"] == "159509")
    assert special["premium_rate"] == pytest.approx(-0.1)
    assert special["comparison_eligible"] is False
    assert special["is_lowest_premium"] is False
    estimated_lof = next(row for row in nasdaq["rows"] if row["code"] == "501312")
    expected_nav = 2.247 * (1 + 0.80 * (24500.5 / 24200.0 - 1))
    assert estimated_lof["iopv"] == pytest.approx(expected_nav)
    assert estimated_lof["premium_rate"] == pytest.approx(2.231 / expected_nav - 1)
    assert estimated_lof["premium_source"] == "estimated_nav"
    assert estimated_lof["official_url"] == "https://www.fsfund.com/fund/501312/fundDetail.shtml"
    assert "2.2470 × [1 + 80% × (24500.50 / 24200.00 - 1)]" in estimated_lof["premium_formula"]
    assert "美股收盘锚点 2026-05-25T04:00:00+08:00" in estimated_lof["premium_note"]
    assert "80%" in estimated_lof["premium_note"]
    assert estimated_lof["comparison_eligible"] is False
    assert estimated_lof["is_lowest_premium"] is False
    stored_501312 = service.db.latest_quotes()["501312"]
    assert stored_501312["estimated_nav"] == pytest.approx(expected_nav)
    assert stored_501312["premium_source"] == "estimated_nav"
    assert stored_501312["nav_date"] == "2026-05-24"
    assert stored_501312["nav_value"] == pytest.approx(2.247)
    assert stored_501312["reference_code"] == "NQ00Y"
    assert stored_501312["reference_price"] == pytest.approx(24500.5)
    assert stored_501312["baseline_price"] == pytest.approx(24200.0)
    assert stored_501312["baseline_kind"] == "us_close_anchor"
    assert stored_501312["reference_weight"] == pytest.approx(0.80)
    assert stored_501312["reference_change"] == pytest.approx(24500.5 / 24200.0 - 1)
    assert "24500.50 / 24200.00" in stored_501312["premium_formula"]
    assert nasdaq["rotation_rule"]["benchmark_code"] == "513100"
    assert nasdaq["rotation_rule"]["candidate_code"] == "161130"
    assert nasdaq["rotation_rule"]["switch_threshold"] == pytest.approx(0.05)
    assert nasdaq["rotation_rule"]["return_threshold"] == pytest.approx(0.01)
    assert nasdaq["rotation_signal"]["level"] == "action"
    assert nasdaq["rotation_signal"]["lowest_code"] == "161130"
    assert nasdaq["rotation_signal"]["spread_to_lowest"] == pytest.approx(0.06)
    benchmark = next(row for row in nasdaq["rows"] if row["code"] == "513100")
    lowest_candidate = next(row for row in nasdaq["rows"] if row["code"] == "161130")
    assert benchmark["is_rotation_benchmark"] is True
    assert benchmark["rotation_gap_to_lowest"] == pytest.approx(0.06)
    assert benchmark["rotation_gap_level"] == "action"
    assert lowest_candidate["is_rotation_lowest_candidate"] is True
    assert lowest_candidate["rotation_gap_from_benchmark"] == pytest.approx(0.06)
    assert nikkei["reference"]["code"] == "N225"
    assert nikkei["reference"]["source"] == "eastmoney.global_index"
    assert sp500["reference"]["code"] == "ES00Y"


def test_service_uses_513100_161130_pair_rotation_rule(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.db.replace_daily_premiums(
        [
                {
                    "code": "161130",
                    "trade_date": "2026-05-24",
                    "close_price": 1.04,
                    "nav": 1.00,
                "premium_rate": 0.04,
                "source": "test",
                "fetched_at": "2026-05-25T15:00:00+08:00",
            }
        ]
    )
    service.refresh_quotes()
    dashboard = service.dashboard()
    nasdaq = next(group for group in dashboard["groups"] if group["id"] == "nasdaq_100")

    assert nasdaq["rotation_rule"]["benchmark_code"] == "513100"
    assert nasdaq["rotation_rule"]["candidate_code"] == "161130"
    assert nasdaq["rotation_rule"]["switch_threshold"] == pytest.approx(0.05)
    assert nasdaq["rotation_signal"]["lowest_code"] == "161130"
    assert nasdaq["rotation_signal"]["spread_to_lowest"] == pytest.approx(0.06)
    benchmark = next(row for row in nasdaq["rows"] if row["code"] == "513100")
    candidate = next(row for row in nasdaq["rows"] if row["code"] == "161130")
    assert benchmark["is_rotation_benchmark"] is True
    assert candidate["is_rotation_lowest_candidate"] is True
    assert candidate["premium_source"] == "estimated_nav"
    assert "95%" in candidate["premium_formula"]


def test_auxiliary_purchase_status_failure_keeps_last_successful_data(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.refresh_notices()
    service.purchase_status_collector = FailedPurchaseStatusCollector()
    detail = service.refresh_notices()
    nasdaq = next(group for group in service.dashboard()["groups"] if group["id"] == "nasdaq_100")
    fund = next(row for row in nasdaq["rows"] if row["code"] == "513100")
    assert "辅助申购状态采集失败" in detail["warnings"][0]
    assert fund["purchase_status"]["purchase_status"] == "暂停申购"
    assert service.db.task_statuses()[-1]["status"] == "warning"


def test_exchange_only_aggregate_status_is_not_interpreted_as_subscription_state(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.purchase_status_collector = ExchangeOnlyPurchaseStatusCollector()
    service.refresh_notices()
    nasdaq = next(group for group in service.dashboard()["groups"] if group["id"] == "nasdaq_100")
    fund = next(row for row in nasdaq["rows"] if row["code"] == "513100")
    assert fund["purchase_status"]["purchase_status"] == "场内交易"
    assert fund["purchase_verification"] == "聚合渠道未提供申购状态"


def test_service_saves_available_reference_when_another_source_fails(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.reference_collector = PartiallyFailedReferenceCollector()
    detail = service.refresh_quotes()
    dashboard = service.dashboard()
    nasdaq = next(group for group in dashboard["groups"] if group["id"] == "nasdaq_100")
    assert detail["references_inserted"] == 1
    assert detail["warnings"] == ["日经225指数采集失败: timeout"]
    assert nasdaq["reference"]["latest_price"] == 24500.5
    assert service.db.task_statuses()[-1]["status"] == "warning"


def test_quota_records_new_document_items(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.refresh_quota()
    service.quota_collector = FakeQuotaCollector(
        [
            {"title": "QDII额度审批情况表 2026-04-30", "url": "https://example.test/q.pdf"},
            {"title": "QDII额度审批情况表 2026-05-31", "url": "https://example.test/new.pdf"},
        ]
    )
    service.refresh_quota()
    assert service.dashboard()["quota"]["new_items"] == [
        {"title": "QDII额度审批情况表 2026-05-31", "url": "https://example.test/new.pdf"}
    ]


def test_api_refresh_and_holdings_upload(tmp_path: Path) -> None:
    overrides = {
        "quote_collector": quote_collector(),
        "notice_collector": FakeNoticeCollector(),
        "purchase_status_collector": FakePurchaseStatusCollector(),
        "quota_collector": FakeQuotaCollector(),
        "reference_collector": FakeReferenceCollector(),
        "daily_premium_collector": FakeDailyPremiumCollector(),
        "fund_metadata_collector": FakeFundMetadataCollector(),
    }
    app = create_app(ROOT / "config" / "funds.yaml", tmp_path / "api.sqlite", overrides, scheduler_enabled=False)
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["groups"] == 3
        assert health["refresh_config"]["quote_refresh_minutes"] >= 1
        blank = client.get("/api/dashboard").json()
        assert blank["refresh_config"]["frontend_auto_refresh_seconds"] >= 0
        assert blank["groups"][0]["rows"][0]["latest_price"] is None
        assert client.post("/api/refresh/quotes").status_code == 200
        first_dashboard = client.get("/api/dashboard").json()
        assert first_dashboard["snapshot_mode"] == "day"
        first_snapshot = first_dashboard["snapshots"][0]["captured_at"]
        assert first_dashboard["snapshots"][0]["fund_count"] > 0
        latest_dashboard = client.get("/api/dashboard?snapshot_mode=latest").json()
        assert latest_dashboard["snapshot_mode"] == "latest"
        assert len(latest_dashboard["snapshots"]) >= len(first_dashboard["snapshots"])
        selected_dashboard = client.get(f"/api/dashboard?snapshot_at={quote(first_snapshot)}").json()
        assert selected_dashboard["snapshot_at"] == first_snapshot
        assert client.post("/api/refresh/premarket").status_code == 200
        assert client.post("/api/refresh/metadata").status_code == 200
        assert client.post("/api/refresh/history?days=180").json()["received"] == 2
        history = client.get("/api/history?limit=80").json()
        assert history["limit"] == 80
        assert history["premium_history"]["513100"][0]["premium_rate"] == pytest.approx(0.1)
        assert history["reference_history"]["NQ00Y"][-1]["latest_price"] == 24500.5
        assert history["reference_history"]["NQ00Y"][-1]["previous_settle"] == 24200.0
        assert "premarket_anchor_history" in history
        assert history["daily_premium_history"]["513100"][1]["premium_rate"] == pytest.approx(0.1)
        assert client.get("/api/history?limit=9").status_code == 422
        assert client.post("/api/refresh/notices").status_code == 200
        invalid = client.post("/api/holdings/upload", files={"file": ("table.xls", b"header\n", "text/plain")})
        assert invalid.status_code == 400
        content = (
            "header\n"
            "1 513100 纳指ETF 10000 1000 0 0 0 10 0 0 0 0 1.000\n"
            "2 510300 沪深300 20000 1000 0 0 0 10 0 0 0 0 3.500\n"
        ).encode("utf-8")
        response = client.post("/api/holdings/upload", files={"file": ("table.xls", content, "text/plain")})
        assert response.json()["count"] == 1
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["holdings"]["rows"][0]["code"] == "513100"
        assert dashboard["holdings"]["rows"][0]["unrealized_pnl"] == 100
        fund = next(row for row in dashboard["groups"][0]["rows"] if row["code"] == "513100")
        assert fund["purchase_status"]["purchase_status"] == "暂停申购"
        assert fund["metadata"]["size_source"] == "资产规模"
