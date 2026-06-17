from __future__ import annotations

from statistics import median
from typing import Any

from .collectors import DailyPremiumCollector, FundMetadataCollector, NoticeCollector, PurchaseStatusCollector, QuoteCollector, QuotaCollector, ReferenceCollector
from .configuration import MonitorConfig
from .database import Database
from .holdings import parse_holdings_text, value_holding
from .utils import calculate_premium


ROTATION_RULES: dict[str, dict[str, Any]] = {
    "nasdaq_100": {
        "name": "513100 ↔ 161130 溢价差观察",
        "benchmark_code": "513100",
        "candidate_code": "161130",
        "switch_threshold": 0.05,
        "return_threshold": 0.01,
        "min_hold_days": 3,
    }
}

PREMIUM_FACTOR_WINDOW = 20
PREMIUM_FACTOR_TIME_WEIGHT = 0.7
PREMIUM_FACTOR_CROSS_WEIGHT = 0.3


class MonitorService:
    def __init__(
        self,
        config: MonitorConfig,
        db: Database,
        quote_collector: QuoteCollector | None = None,
        notice_collector: NoticeCollector | None = None,
        purchase_status_collector: PurchaseStatusCollector | None = None,
        quota_collector: QuotaCollector | None = None,
        reference_collector: ReferenceCollector | None = None,
        daily_premium_collector: DailyPremiumCollector | None = None,
        fund_metadata_collector: FundMetadataCollector | None = None,
    ):
        self.config = config
        self.db = db
        self.quote_collector = quote_collector or QuoteCollector()
        self.notice_collector = notice_collector or NoticeCollector()
        self.purchase_status_collector = purchase_status_collector or PurchaseStatusCollector()
        self.quota_collector = quota_collector or QuotaCollector()
        self.reference_collector = reference_collector or ReferenceCollector()
        self.daily_premium_collector = daily_premium_collector or DailyPremiumCollector()
        self.fund_metadata_collector = fund_metadata_collector or FundMetadataCollector()

    def refresh_quotes(self) -> dict[str, Any]:
        return self._run_task("quotes", self._refresh_quotes)

    def refresh_references(self) -> dict[str, Any]:
        return self._run_task("references", self._refresh_references)

    def _refresh_quotes(self) -> dict[str, Any]:
        rows = self.quote_collector.collect(self.config.funds)
        detail: dict[str, Any] = {
            "received": len(rows),
            "source": "akshare.fund_etf_spot_em/fund_lof_spot_em",
        }
        warnings: list[str] = []
        try:
            references, warnings = self.reference_collector.collect(self.config.references)
            detail["references_received"] = len(references)
            detail["references_inserted"] = self.db.insert_references(references)
            detail["reference_sources"] = sorted({row["source"] for row in references})
            anchors = self._build_premarket_anchors()
            detail["premarket_anchors_written"] = self.db.insert_premarket_anchors(anchors)
            warnings = list(warnings)
        except Exception as exc:
            warnings = [f"参考行情采集失败: {exc}"]
        estimate_detail, estimate_warnings = self._apply_live_nav_estimates(rows)
        detail.update(estimate_detail)
        warnings.extend(estimate_warnings)
        detail["inserted"] = self.db.insert_quotes(rows)
        if warnings:
            detail["warnings"] = warnings
        return detail

    def _refresh_references(self) -> dict[str, Any]:
        references, warnings = self.reference_collector.collect(self.config.references)
        detail: dict[str, Any] = {
            "references_received": len(references),
            "references_inserted": self.db.insert_references(references),
            "reference_sources": sorted({row["source"] for row in references}),
        }
        anchors = self._build_premarket_anchors()
        detail["premarket_anchors_written"] = self.db.insert_premarket_anchors(anchors)
        if warnings:
            detail["warnings"] = warnings
        return detail

    def _apply_live_nav_estimates(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        estimate_funds = {
            fund.code: fund
            for fund in self.config.funds
            if fund.nav_estimate is not None
        }
        if not estimate_funds:
            return {"estimated_nav_count": 0}, []
        latest_daily = self.db.latest_daily_premiums(list(estimate_funds))
        references = self.db.latest_references()
        warnings: list[str] = []
        estimated = 0
        for row in rows:
            fund = estimate_funds.get(row["code"])
            if fund is None or fund.nav_estimate is None or row.get("premium_rate") is not None:
                continue
            daily = latest_daily.get(fund.code)
            reference = references.get(fund.nav_estimate.reference_code)
            if not daily:
                warnings.append(f"{fund.code} 缺少官方NAV历史，请先刷新历史日线")
                continue
            if not reference:
                warnings.append(f"{fund.code} 缺少参考行情 {fund.nav_estimate.reference_code}")
                continue
            baseline = self.db.reference_nav_baseline(
                fund.nav_estimate.reference_code,
                str(daily["trade_date"]),
                row.get("captured_at"),
            )
            estimate = _estimate_nav_from_reference(daily, reference, baseline, fund.nav_estimate.reference_weight)
            if estimate is None:
                warnings.append(
                    f"{fund.code} 无法对齐 {daily['trade_date']} NAV与"
                    f"{fund.nav_estimate.reference_code}美股收盘基准"
                )
                continue
            row["iopv"] = estimate["estimated_nav"]
            row["premium_rate"] = calculate_premium(row.get("latest_price"), estimate["estimated_nav"])
            row["premium_source"] = "estimated_nav"
            row["estimated_nav"] = estimate["estimated_nav"]
            row["nav_date"] = daily.get("trade_date")
            row["nav_value"] = daily.get("nav")
            row["reference_code"] = fund.nav_estimate.reference_code
            row["reference_captured_at"] = reference.get("captured_at")
            row["reference_price"] = estimate["reference_price"]
            row["baseline_captured_at"] = baseline.get("captured_at") if baseline else None
            row["baseline_price"] = estimate["baseline_price"]
            row["baseline_kind"] = baseline.get("baseline_kind") if baseline else None
            row["reference_weight"] = fund.nav_estimate.reference_weight
            row["reference_change"] = estimate["reference_change"]
            row["weighted_change"] = estimate["weighted_change"]
            row["premium_note"] = _estimated_nav_note(fund, daily, references, self.db, row)
            row["premium_formula"] = _estimated_nav_formula(fund, daily, references, self.db, row)
            row["source"] = f"{row['source']}|estimated_nav"
            estimated += 1
        return {"estimated_nav_count": estimated}, warnings

    def refresh_premarket_anchors(self) -> dict[str, Any]:
        return self._run_task("premarket_anchor", self._refresh_premarket_anchors)

    def _refresh_premarket_anchors(self) -> dict[str, Any]:
        references, warnings = self.reference_collector.collect(self.config.references)
        references_inserted = self.db.insert_references(references)
        anchors = self._build_premarket_anchors()
        detail: dict[str, Any] = {
            "references_received": len(references),
            "references_inserted": references_inserted,
            "received": len(anchors),
            "written": self.db.insert_premarket_anchors(anchors),
            "source": "reference_price / previous_cn_close_reference - 1",
        }
        if warnings:
            detail["warnings"] = warnings
        return detail

    def refresh_notices(self) -> dict[str, Any]:
        return self._run_task("notices", self._refresh_notices)

    def _refresh_notices(self) -> dict[str, Any]:
        rows = self.notice_collector.collect(self.config)
        inserted = self.db.insert_notices(rows)
        detail: dict[str, Any] = {"received": len(rows), "inserted": inserted, "sources": ["SSE", "SZSE"]}
        try:
            statuses = self.purchase_status_collector.collect(self.config.funds)
            detail["purchase_status_received"] = len(statuses)
            detail["purchase_status_inserted"] = self.db.insert_purchase_statuses(statuses)
            detail["auxiliary_source"] = "akshare.fund_purchase_em"
        except Exception as exc:
            detail["warnings"] = [f"辅助申购状态采集失败: {exc}"]
        return detail

    def refresh_daily_premiums(self, days: int = 1095) -> dict[str, Any]:
        return self._run_task("daily_premium", lambda: self._refresh_daily_premiums(days))

    def _refresh_daily_premiums(self, days: int) -> dict[str, Any]:
        codes = [fund.code for fund in self.config.funds]
        date_bounds = self.db.daily_premium_date_bounds(codes)
        rows, warnings, collection_detail = self.daily_premium_collector.collect(
            self.config.funds, days, date_bounds=date_bounds
        )
        detail: dict[str, Any] = {
            "received": len(rows),
            "written": self.db.replace_daily_premiums(rows),
            "days": days,
            "source": "akshare.close_nav_daily",
            **collection_detail,
        }
        if warnings:
            detail["warnings"] = warnings
        return detail

    def refresh_metadata(self) -> dict[str, Any]:
        return self._run_task("metadata", self._refresh_metadata)

    def _refresh_metadata(self) -> dict[str, Any]:
        rows, warnings = self.fund_metadata_collector.collect(self.config.funds)
        detail: dict[str, Any] = {
            "received": len(rows),
            "written": self.db.replace_fund_metadata(rows),
            "source": "akshare.fund_individual_basic_info_xq",
        }
        if warnings:
            detail["warnings"] = warnings
        return detail

    def refresh_quota(self) -> dict[str, Any]:
        return self._run_task("quota", self._refresh_quota)

    def _refresh_quota(self) -> dict[str, Any]:
        previous = self.db.latest_quota_document()
        document = self.quota_collector.collect(self.config.quota_source, previous)
        inserted = self.db.insert_quota_document(document)
        return {
            "updated": inserted,
            "items": len(document["items"]),
            "new_items": len(document["new_items"]),
            "source": document["url"],
        }

    def _run_task(self, name: str, callback: Any) -> dict[str, Any]:
        self.db.task_started(name)
        try:
            detail = callback()
            self.db.task_succeeded(name, detail, "; ".join(detail.get("warnings", [])) or None)
            return detail
        except Exception as exc:
            self.db.task_failed(name, str(exc))
            raise

    def _build_premarket_anchors(self) -> list[dict[str, Any]]:
        references = self.db.latest_references()
        rows: list[dict[str, Any]] = []
        for group in self.config.groups:
            if group.reference is None:
                continue
            current = references.get(group.reference.code)
            if not current:
                continue
            current_price = current.get("latest_price")
            captured_at = current.get("captured_at")
            if current_price is None or not captured_at:
                continue
            baseline = self.db.cn_close_reference_snapshot(group.reference.code, captured_at)
            baseline_price = baseline.get("latest_price") if baseline else None
            expected_change = calculate_premium(current_price, baseline_price)
            futures_change = current.get("change_rate")
            overnight_change = (
                expected_change - futures_change
                if expected_change is not None and futures_change is not None
                else None
            )
            rows.append(
                {
                    "group_id": group.id,
                    "group_name": group.name,
                    "reference_code": group.reference.code,
                    "reference_name": group.reference.display_name,
                    "captured_at": captured_at,
                    "baseline_captured_at": baseline.get("captured_at") if baseline else None,
                    "reference_price": current_price,
                    "baseline_price": baseline_price,
                    "expected_change_rate": expected_change,
                    "futures_change_rate": futures_change,
                    "overnight_change_rate": overnight_change,
                    "rate_change_bps": None,
                    "rate_impact_level": "not_tracked",
                    "note": _premarket_anchor_note(expected_change, futures_change, baseline),
                    "source": current.get("source") or "reference_snapshots",
                }
            )
        return rows

    def upload_holdings(self, content: bytes, filename: str) -> dict[str, Any]:
        rows = parse_holdings_text(content, set(self.config.fund_map))
        if not rows:
            raise ValueError("文件中没有匹配配置池的持仓，或份额字段无法解析")
        count = self.db.replace_holdings(rows, filename)
        return {"count": count, "source_file": filename}

    def groups_payload(self, snapshot_at: str | None = None) -> list[dict[str, Any]]:
        quotes = self.db.quotes_at(snapshot_at) if snapshot_at else self.db.latest_quotes()
        latest_daily = self.db.latest_daily_premiums([fund.code for fund in self.config.funds])
        metadata = self.db.fund_metadata()
        references = self.db.latest_references()
        premarket_anchors = self.db.latest_premarket_anchors()
        notices = self.db.latest_notice_by_code()
        purchase_statuses = self.db.latest_purchase_statuses()
        holdings = self.db.holdings()
        result = []
        for group in self.config.groups:
            rows = []
            for fund in group.funds:
                quote = quotes.get(fund.code, {})
                quote_view = _quote_with_daily_nav_fallback(
                    quote,
                    latest_daily.get(fund.code),
                    fund,
                    references,
                    self.db,
                )
                fund_metadata = _merge_fund_metadata(fund, quote, metadata.get(fund.code))
                holding = holdings.get(fund.code)
                purchase_status = purchase_statuses.get(fund.code)
                latest_notice = notices.get(fund.code)
                rows.append(
                    {
                        "code": fund.code,
                        "exchange": fund.exchange,
                        "display_name": fund.display_name,
                        "fund_name": fund.fund_name,
                        "manager": fund.manager,
                        "official_url": fund.official_url,
                        "comparison_eligible": fund.comparison_eligible,
                        "latest_price": quote_view.get("latest_price"),
                        "change_rate": quote_view.get("change_rate"),
                        "iopv": quote_view.get("iopv"),
                        "premium_rate": quote_view.get("premium_rate"),
                        "premium_source": quote_view.get("premium_source"),
                        "premium_note": quote_view.get("premium_note"),
                        "premium_formula": quote_view.get("premium_formula"),
                        "turnover_amount": quote_view.get("turnover_amount"),
                        "captured_at": quote_view.get("captured_at"),
                        "metadata": fund_metadata,
                        "holding": value_holding(holding, quote_view.get("latest_price")) if holding else None,
                        "latest_notice": latest_notice,
                        "purchase_status": purchase_status,
                        "purchase_verification": _purchase_verification(purchase_status, latest_notice),
                        "is_lowest_premium": False,
                        "is_rotation_benchmark": False,
                        "is_rotation_lowest_candidate": False,
                        "rotation_gap_from_benchmark": None,
                        "rotation_gap_to_lowest": None,
                        "rotation_gap_level": None,
                    }
                )
            rule = ROTATION_RULES.get(group.id)
            valid = [
                row for row in rows
                if row["comparison_eligible"] and row["premium_rate"] is not None
            ]
            lowest_premium = None
            if valid:
                lowest = min(valid, key=lambda row: row["premium_rate"])
                lowest["is_lowest_premium"] = True
                lowest_premium = lowest["premium_rate"]
            _apply_premium_factor_marks(rows, self.db)
            _apply_rotation_marks(rows, rule)
            rows.sort(key=lambda row: (row["premium_rate"] is None, row["premium_rate"] or 0))
            reference = None
            if group.reference is not None:
                reference = {
                    "code": group.reference.code,
                    "display_name": group.reference.display_name,
                    "source": group.reference.source,
                    **references.get(group.reference.code, {}),
                }
            result.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "reference": reference,
                    "premarket_anchor": premarket_anchors.get(group.id),
                    "rotation_rule": rule,
                    "rotation_signal": _rotation_signal(rows, rule),
                    "rows": rows,
                }
            )
        return result

    def holdings_payload(self) -> dict[str, Any]:
        quotes = self.db.latest_quotes()
        funds = self.config.fund_map
        rows = [
            value_holding(
                {**holding, "display_name": funds[code].display_name},
                quotes.get(code, {}).get("latest_price"),
            )
            for code, holding in self.db.holdings().items()
            if code in funds
        ]
        total_value = sum(row["market_value"] or 0 for row in rows)
        total_pnl = sum(row["unrealized_pnl"] or 0 for row in rows)
        return {"rows": rows, "total_value": total_value, "total_unrealized_pnl": total_pnl}

    def dashboard(self, snapshot_at: str | None = None, snapshot_mode: str = "day") -> dict[str, Any]:
        codes = [fund.code for fund in self.config.funds]
        reference_codes = [reference.code for reference in self.config.references]
        group_ids = [group.id for group in self.config.groups]
        return {
            "snapshot_at": snapshot_at,
            "snapshot_mode": snapshot_mode,
            "snapshots": self.db.quote_snapshot_times(mode=snapshot_mode),
            "groups": self.groups_payload(snapshot_at),
            "holdings": self.holdings_payload(),
            "premium_history": self.db.premium_history(codes),
            "reference_history": self.db.reference_history(reference_codes),
            "premarket_anchor_history": self.db.premarket_anchor_history(group_ids),
            "notices": self.db.notices(),
            "quota": self.db.latest_quota_document(),
            "tasks": self.db.task_statuses(),
            "disclaimer": "仅用于监控公开数据，不构成投资建议或交易指令。",
        }

    def history_payload(self, limit: int = 240, daily_limit: int = 760) -> dict[str, Any]:
        codes = [fund.code for fund in self.config.funds]
        reference_codes = [reference.code for reference in self.config.references]
        group_ids = [group.id for group in self.config.groups]
        return {
            "limit": limit,
            "groups": self.groups_payload(),
            "premium_history": self.db.premium_history(codes, limit),
            "daily_premium_history": self.db.daily_premium_history(codes, daily_limit),
            "reference_history": self.db.reference_history(reference_codes, limit),
            "premarket_anchor_history": self.db.premarket_anchor_history(group_ids, limit),
        }


def _purchase_verification(
    status: dict[str, Any] | None, notice: dict[str, Any] | None
) -> str | None:
    if not status:
        return "当前状态待获取"
    if "场内交易" in str(status.get("purchase_status") or ""):
        return "聚合渠道未提供申购状态"
    if not notice:
        return "无正式事件可对照"
    current = str(status.get("purchase_status") or "")
    latest = str(notice.get("notice_type") or "")
    if latest == "恢复申购" and ("暂停" in current or "关闭" in current):
        return "与最新正式公告方向不同，待核验"
    if latest in {"暂停申购", "限制申购"} and "开放" in current:
        return "与最新正式公告方向不同，待核验"
    return None


def _premarket_anchor_note(
    expected_change: float | None,
    futures_change: float | None,
    baseline: dict[str, Any] | None,
) -> str:
    if baseline is None:
        return "缺少最近A股收盘时的参考价格，暂不能形成盘前价格锚点。"
    parts = ["锚点=当前参考期货/指数价格 ÷ 最近A股收盘参考价格 - 1。"]
    if expected_change is not None and futures_change is not None:
        parts.append("隔夜美股与白天期货变化已合并到锚点；期货自身涨跌仅作拆解参考。")
    parts.append("利率是重要驱动，但通常已反映在期货价格里，当前不单独叠加，避免重复计价。")
    return "".join(parts)


def _apply_premium_factor_marks(rows: list[dict[str, Any]], db: Database) -> None:
    for row in rows:
        row["premium_factor"] = {
            "window_days": PREMIUM_FACTOR_WINDOW,
            "time_weight": PREMIUM_FACTOR_TIME_WEIGHT,
            "cross_weight": PREMIUM_FACTOR_CROSS_WEIGHT,
            "history_mean": None,
            "time_deviation": None,
            "pool_median": None,
            "cross_deviation": None,
            "combined_deviation": None,
        }
    eligible = [
        row for row in rows
        if row["comparison_eligible"] and row["premium_rate"] is not None
    ]
    if not eligible:
        return
    pool_median = median(float(row["premium_rate"]) for row in eligible)
    captured_dates = [
        str(row.get("captured_at"))[:10]
        for row in eligible
        if row.get("captured_at")
    ]
    before_date = min(captured_dates) if captured_dates else None
    history_means = db.daily_premium_means(
        [row["code"] for row in eligible],
        before_date,
        PREMIUM_FACTOR_WINDOW,
    )
    for row in eligible:
        premium = float(row["premium_rate"])
        history_mean = history_means.get(row["code"])
        time_deviation = None if history_mean is None else premium - history_mean
        cross_deviation = premium - pool_median
        combined_deviation = (
            None if time_deviation is None
            else PREMIUM_FACTOR_TIME_WEIGHT * time_deviation + PREMIUM_FACTOR_CROSS_WEIGHT * cross_deviation
        )
        row["premium_factor"] = {
            "window_days": PREMIUM_FACTOR_WINDOW,
            "time_weight": PREMIUM_FACTOR_TIME_WEIGHT,
            "cross_weight": PREMIUM_FACTOR_CROSS_WEIGHT,
            "history_mean": history_mean,
            "time_deviation": time_deviation,
            "pool_median": pool_median,
            "cross_deviation": cross_deviation,
            "combined_deviation": combined_deviation,
        }


def _apply_rotation_marks(rows: list[dict[str, Any]], rule: dict[str, Any] | None) -> None:
    if not rule:
        return
    benchmark_code = rule["benchmark_code"]
    candidate_code = rule.get("candidate_code")
    switch_threshold = float(rule["switch_threshold"])
    pair_codes = {benchmark_code, candidate_code} if candidate_code else None
    eligible = [
        row for row in rows
        if row["premium_rate"] is not None
        and (row["comparison_eligible"] or (pair_codes is not None and row["code"] in pair_codes))
    ]
    benchmark = next((row for row in eligible if row["code"] == benchmark_code), None)
    if not benchmark:
        return
    if candidate_code:
        lowest = next((row for row in eligible if row["code"] == candidate_code), None)
        if not lowest:
            benchmark["is_rotation_benchmark"] = True
            return
    else:
        lowest = min(eligible, key=lambda row: row["premium_rate"])
    benchmark_premium = float(benchmark["premium_rate"])
    lowest_premium = float(lowest["premium_rate"])
    spread_to_lowest = benchmark_premium - lowest_premium
    benchmark["is_rotation_benchmark"] = True
    benchmark["rotation_gap_to_lowest"] = spread_to_lowest
    benchmark["rotation_gap_level"] = _rotation_gap_level(spread_to_lowest, switch_threshold)
    lowest["is_rotation_lowest_candidate"] = True
    for row in eligible:
        gap = benchmark_premium - float(row["premium_rate"])
        row["rotation_gap_from_benchmark"] = gap
        if row["code"] != benchmark_code:
            row["rotation_gap_level"] = _rotation_gap_level(gap, switch_threshold)


def _rotation_gap_level(gap: float, switch_threshold: float) -> str:
    if gap >= switch_threshold:
        return "action"
    if gap >= switch_threshold - 0.005:
        return "watch"
    return "idle"


def _rotation_signal(rows: list[dict[str, Any]], rule: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rule:
        return None
    benchmark_code = rule["benchmark_code"]
    switch_threshold = float(rule["switch_threshold"])
    return_threshold = float(rule["return_threshold"])
    eligible = [
        row for row in rows
        if row["comparison_eligible"] and row["premium_rate"] is not None
    ]
    benchmark = next((row for row in eligible if row["code"] == benchmark_code), None)
    if not benchmark:
        return {
            "level": "missing",
            "title": "等待基准行情",
            "message": f"{benchmark_code} 暂无有效溢价，暂不判断轮动信号。",
            "benchmark_code": benchmark_code,
            "switch_threshold": switch_threshold,
            "return_threshold": return_threshold,
            "min_hold_days": rule["min_hold_days"],
        }
    lowest = min(eligible, key=lambda row: row["premium_rate"])
    spread_to_lowest = float(benchmark["premium_rate"]) - float(lowest["premium_rate"])
    held = next((row for row in rows if row.get("holding")), None)
    base = {
        "benchmark_code": benchmark_code,
        "benchmark_premium": benchmark["premium_rate"],
        "lowest_code": lowest["code"],
        "lowest_name": lowest["display_name"],
        "lowest_premium": lowest["premium_rate"],
        "spread_to_lowest": spread_to_lowest,
        "switch_threshold": switch_threshold,
        "return_threshold": return_threshold,
        "min_hold_days": rule["min_hold_days"],
        "held_code": held["code"] if held else None,
        "held_premium": held["premium_rate"] if held else None,
    }
    if held and held["code"] != benchmark_code and held.get("premium_rate") is not None:
        benchmark_vs_held = float(benchmark["premium_rate"]) - float(held["premium_rate"])
        if benchmark_vs_held <= return_threshold:
            return {
                **base,
                "level": "action",
                "title": "接近换回基准",
                "message": f"{benchmark_code} 与当前持仓 {held['code']} 的溢价差已收敛到 {benchmark_vs_held * 100:.2f}%，低于回归阈值 {return_threshold * 100:.2f}%。",
                "benchmark_vs_held": benchmark_vs_held,
            }
        if benchmark_vs_held <= return_threshold + 0.005:
            return {
                **base,
                "level": "watch",
                "title": "观察换回",
                "message": f"{benchmark_code} 与当前持仓 {held['code']} 的溢价差为 {benchmark_vs_held * 100:.2f}%，接近回归阈值 {return_threshold * 100:.2f}%。",
                "benchmark_vs_held": benchmark_vs_held,
            }
        return {
            **base,
            "level": "idle",
            "title": "继续持有替代标的",
            "message": f"{benchmark_code} 相对当前持仓 {held['code']} 仍高 {benchmark_vs_held * 100:.2f}%，尚未回归到 {return_threshold * 100:.2f}% 以内。",
            "benchmark_vs_held": benchmark_vs_held,
        }
    if lowest["code"] == benchmark_code:
        return {
            **base,
            "level": "idle",
            "title": "基准已是低溢价",
            "message": f"{benchmark_code} 当前就是组内最低有效溢价，无需切出观察。",
        }
    if spread_to_lowest >= switch_threshold:
        return {
            **base,
            "level": "action",
            "title": "触发切出观察",
            "message": f"{benchmark_code} 比最低溢价 {lowest['code']} 高 {spread_to_lowest * 100:.2f}%，超过大阈值 {switch_threshold * 100:.2f}%。",
        }
    if spread_to_lowest >= switch_threshold - 0.005:
        return {
            **base,
            "level": "watch",
            "title": "接近切出阈值",
            "message": f"{benchmark_code} 比最低溢价 {lowest['code']} 高 {spread_to_lowest * 100:.2f}%，接近大阈值 {switch_threshold * 100:.2f}%。",
        }
    return {
        **base,
        "level": "idle",
        "title": "未触发",
        "message": f"{benchmark_code} 比最低溢价 {lowest['code']} 高 {spread_to_lowest * 100:.2f}%，未达到大阈值 {switch_threshold * 100:.2f}%。",
    }


def _quote_with_daily_nav_fallback(
    quote: dict[str, Any],
    latest_daily: dict[str, Any] | None,
    fund: Any | None = None,
    references: dict[str, dict[str, Any]] | None = None,
    db: Database | None = None,
) -> dict[str, Any]:
    result = dict(quote or {})
    if result.get("premium_source") == "estimated_nav":
        result["estimated_nav"] = result.get("estimated_nav") or result.get("iopv")
        return result
    if result.get("premium_rate") is not None:
        if fund is not None and fund.nav_estimate is not None and "estimated_nav" in str(result.get("source") or ""):
            result["premium_source"] = "estimated_nav"
            reference = (references or {}).get(fund.nav_estimate.reference_code)
            baseline = (
                db.reference_nav_baseline(fund.nav_estimate.reference_code, str(latest_daily["trade_date"]), result.get("captured_at"))
                if db is not None and latest_daily
                else None
            )
            estimate = _estimate_nav_from_reference(
                latest_daily or {},
                reference or {},
                baseline,
                fund.nav_estimate.reference_weight,
            )
            if estimate is not None:
                result["iopv"] = estimate["estimated_nav"]
                result["premium_rate"] = calculate_premium(result.get("latest_price"), estimate["estimated_nav"])
            result["premium_note"] = _estimated_nav_note(fund, latest_daily, references or {}, db, result)
            result["premium_formula"] = _estimated_nav_formula(fund, latest_daily, references or {}, db, result)
        else:
            result.setdefault("premium_source", "iopv")
        return result
    if fund is not None and fund.nav_estimate is not None:
        reference = (references or {}).get(fund.nav_estimate.reference_code)
        baseline = (
            db.reference_nav_baseline(fund.nav_estimate.reference_code, str(latest_daily["trade_date"]), result.get("captured_at"))
            if db is not None and latest_daily
            else None
        )
        estimate = _estimate_nav_from_reference(
            latest_daily or {},
            reference or {},
            baseline,
            fund.nav_estimate.reference_weight,
        )
        if estimate is not None and result.get("latest_price") is not None:
            result["iopv"] = estimate["estimated_nav"]
            result["premium_rate"] = calculate_premium(result.get("latest_price"), estimate["estimated_nav"])
            result["premium_source"] = "estimated_nav"
            result["premium_note"] = _estimated_nav_note(fund, latest_daily, references or {}, db, result)
            result["premium_formula"] = _estimated_nav_formula(fund, latest_daily, references or {}, db, result)
            return result
        result["premium_source"] = "estimated_nav_unavailable"
        result["premium_note"] = (
            f"最新官方NAV日期 {latest_daily.get('trade_date')}，"
            f"但缺少与之对齐的 {fund.nav_estimate.reference_code} 昨结基准，暂不计算盘中折溢价"
            if latest_daily
            else "缺少最新官方NAV，暂不计算盘中折溢价"
        )
        return result
    if not latest_daily:
        result.setdefault("premium_source", None)
        return result
    latest_price = result.get("latest_price")
    if latest_price is None:
        latest_price = latest_daily.get("close_price")
        result["latest_price"] = latest_price
        result["captured_at"] = latest_daily.get("trade_date")
    nav = latest_daily.get("nav")
    result["iopv"] = result.get("iopv") or nav
    result["premium_rate"] = calculate_premium(latest_price, nav)
    result["premium_source"] = "latest_nav"
    result["premium_note"] = f"使用最新官方NAV估算：{latest_daily.get('trade_date')}"
    return result


def _estimate_nav_from_reference(
    latest_daily: dict[str, Any],
    reference: dict[str, Any],
    baseline: dict[str, Any] | None,
    reference_weight: float,
) -> dict[str, float] | None:
    nav = latest_daily.get("nav")
    latest_price = reference.get("latest_price")
    baseline_price = baseline.get("baseline_price") if baseline else None
    if baseline_price is None and baseline:
        baseline_price = baseline.get("previous_settle")
    if nav is None or latest_price is None or baseline_price is None or float(baseline_price) <= 0:
        return None
    reference_change = float(latest_price) / float(baseline_price) - 1
    weighted_change = reference_change * reference_weight
    estimated_nav = float(nav) * (1 + weighted_change)
    if estimated_nav <= 0:
        return None
    return {
        "estimated_nav": estimated_nav,
        "reference_change": reference_change,
        "weighted_change": weighted_change,
        "reference_price": float(latest_price),
        "baseline_price": float(baseline_price),
    }


def _estimated_nav_note(
    fund: Any,
    latest_daily: dict[str, Any] | None,
    references: dict[str, dict[str, Any]],
    db: Database | None,
    quote: dict[str, Any],
) -> str:
    rule = fund.nav_estimate
    if rule is None or not latest_daily:
        return "盘中估算NAV"
    reference = references.get(rule.reference_code)
    baseline = (
        db.reference_nav_baseline(rule.reference_code, str(latest_daily["trade_date"]), quote.get("captured_at"))
        if db is not None
        else None
    )
    estimate = _estimate_nav_from_reference(latest_daily, reference or {}, baseline, rule.reference_weight)
    if estimate is None:
        return rule.description or "盘中估算NAV"
    detail = (
        f"估算NAV = {float(latest_daily['nav']):.4f} × "
        f"[1 + {rule.reference_weight:.0%} × ({estimate['reference_price']:.2f} / "
        f"{estimate['baseline_price']:.2f} - 1)]"
        f" = {estimate['estimated_nav']:.4f}；官方NAV日期 {latest_daily['trade_date']}"
    )
    if baseline:
        baseline_kind = "美股收盘锚点" if baseline.get("baseline_kind") == "us_close_anchor" else "昨结兜底"
        detail = f"{detail}；{baseline_kind} {baseline.get('captured_at')}"
    if rule.description:
        detail = f"{detail}。{rule.description}"
    return detail


def _estimated_nav_formula(
    fund: Any,
    latest_daily: dict[str, Any] | None,
    references: dict[str, dict[str, Any]],
    db: Database | None,
    quote: dict[str, Any],
) -> str | None:
    rule = fund.nav_estimate
    if rule is None or not latest_daily:
        return None
    reference = references.get(rule.reference_code)
    baseline = (
        db.reference_nav_baseline(rule.reference_code, str(latest_daily["trade_date"]), quote.get("captured_at"))
        if db is not None
        else None
    )
    estimate = _estimate_nav_from_reference(latest_daily, reference or {}, baseline, rule.reference_weight)
    if estimate is None:
        return None
    return (
        f"{float(latest_daily['nav']):.4f} × "
        f"[1 + {rule.reference_weight:.0%} × ({estimate['reference_price']:.2f} / "
        f"{estimate['baseline_price']:.2f} - 1)]"
        f" = {estimate['estimated_nav']:.4f}"
    )


def _merge_fund_metadata(fund: Any, quote: dict[str, Any], stored: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(stored or {})
    if fund.inception_date:
        result["inception_date"] = fund.inception_date
    if fund.asset_size_cny is not None:
        result["asset_size_cny"] = fund.asset_size_cny
        result["size_source"] = fund.size_source or "资产规模"
        result["metadata_source"] = fund.metadata_source or "配置静态数据"
        result["metadata_fetched_at"] = fund.metadata_fetched_at or "-"
        return result
    latest_shares = quote.get("latest_shares")
    latest_price = quote.get("latest_price")
    if latest_shares is not None and latest_price is not None:
        result["asset_size_cny"] = float(latest_shares) * float(latest_price)
        result["size_source"] = "份额估算"
        result["metadata_source"] = "东方财富ETF行情"
        result["metadata_fetched_at"] = quote.get("captured_at")
    return result


def _rotation_signal(rows: list[dict[str, Any]], rule: dict[str, Any] | None) -> dict[str, Any] | None:  # type: ignore[no-redef]
    if not rule:
        return None
    benchmark_code = rule["benchmark_code"]
    candidate_code = rule.get("candidate_code")
    switch_threshold = float(rule["switch_threshold"])
    return_threshold = float(rule["return_threshold"])
    pair_codes = {benchmark_code, candidate_code} if candidate_code else None
    eligible = [
        row for row in rows
        if row["premium_rate"] is not None
        and (row["comparison_eligible"] or (pair_codes is not None and row["code"] in pair_codes))
    ]
    benchmark = next((row for row in eligible if row["code"] == benchmark_code), None)
    if not benchmark:
        return {
            "level": "missing",
            "title": "等待锚点行情",
            "message": f"{benchmark_code} 暂无有效溢价，暂不判断轮动信号。",
            "benchmark_code": benchmark_code,
            "candidate_code": candidate_code,
            "switch_threshold": switch_threshold,
            "return_threshold": return_threshold,
            "min_hold_days": rule["min_hold_days"],
        }
    if candidate_code:
        candidate = next((row for row in eligible if row["code"] == candidate_code), None)
        if not candidate:
            return {
                "level": "missing",
                "title": "等待候选行情",
                "message": f"{candidate_code} 暂无有效溢价，无法计算 {benchmark_code} - {candidate_code} 的轮动差值。",
                "benchmark_code": benchmark_code,
                "benchmark_premium": benchmark["premium_rate"],
                "candidate_code": candidate_code,
                "switch_threshold": switch_threshold,
                "return_threshold": return_threshold,
                "min_hold_days": rule["min_hold_days"],
            }
        lowest = candidate
    else:
        lowest = min(eligible, key=lambda row: row["premium_rate"])
    spread_to_lowest = float(benchmark["premium_rate"]) - float(lowest["premium_rate"])
    held = next((row for row in rows if row.get("holding")), None)
    base = {
        "benchmark_code": benchmark_code,
        "benchmark_premium": benchmark["premium_rate"],
        "lowest_code": lowest["code"],
        "lowest_name": lowest["display_name"],
        "lowest_premium": lowest["premium_rate"],
        "candidate_code": candidate_code,
        "candidate_name": lowest["display_name"] if candidate_code else None,
        "candidate_premium": lowest["premium_rate"] if candidate_code else None,
        "spread_to_lowest": spread_to_lowest,
        "switch_threshold": switch_threshold,
        "return_threshold": return_threshold,
        "min_hold_days": rule["min_hold_days"],
        "held_code": held["code"] if held else None,
        "held_premium": held["premium_rate"] if held else None,
    }
    if held and held["code"] != benchmark_code and held.get("premium_rate") is not None:
        benchmark_vs_held = float(benchmark["premium_rate"]) - float(held["premium_rate"])
        if benchmark_vs_held <= return_threshold:
            return {
                **base,
                "level": "action",
                "title": "接近换回锚点",
                "message": f"{benchmark_code} 与当前持仓 {held['code']} 的溢价差已收敛到 {benchmark_vs_held * 100:.2f}%，低于回归阈值 {return_threshold * 100:.2f}%。",
                "benchmark_vs_held": benchmark_vs_held,
            }
        if benchmark_vs_held <= return_threshold + 0.005:
            return {
                **base,
                "level": "watch",
                "title": "观察换回",
                "message": f"{benchmark_code} 与当前持仓 {held['code']} 的溢价差为 {benchmark_vs_held * 100:.2f}%，接近回归阈值 {return_threshold * 100:.2f}%。",
                "benchmark_vs_held": benchmark_vs_held,
            }
        return {
            **base,
            "level": "idle",
            "title": "继续观察替代仓",
            "message": f"{benchmark_code} 相对当前持仓 {held['code']} 仍高 {benchmark_vs_held * 100:.2f}%，尚未回归到 {return_threshold * 100:.2f}% 以内。",
            "benchmark_vs_held": benchmark_vs_held,
        }
    if lowest["code"] == benchmark_code:
        return {
            **base,
            "level": "idle",
            "title": "锚点已是低溢价",
            "message": f"{benchmark_code} 当前就是低溢价标的，无需切出观察。",
        }
    if spread_to_lowest >= switch_threshold:
        return {
            **base,
            "level": "action",
            "title": "触发切到161130观察" if candidate_code else "触发切出观察",
            "message": f"{benchmark_code} 比 {lowest['code']} 高 {spread_to_lowest * 100:.2f}%，超过大阈值 {switch_threshold * 100:.2f}%。",
        }
    if spread_to_lowest >= switch_threshold - 0.005:
        return {
            **base,
            "level": "watch",
            "title": "接近5%切换阈值" if candidate_code else "接近切出阈值",
            "message": f"{benchmark_code} 比 {lowest['code']} 高 {spread_to_lowest * 100:.2f}%，接近大阈值 {switch_threshold * 100:.2f}%。",
        }
    return {
        **base,
        "level": "idle",
        "title": "未触发",
        "message": f"{benchmark_code} 比 {lowest['code']} 高 {spread_to_lowest * 100:.2f}%，未达到大阈值 {switch_threshold * 100:.2f}%。",
    }
