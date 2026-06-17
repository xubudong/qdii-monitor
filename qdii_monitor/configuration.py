from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class NavEstimateRule:
    reference_code: str
    reference_weight: float
    description: str | None = None


@dataclass(frozen=True)
class FundTarget:
    code: str
    exchange: str
    display_name: str
    fund_name: str
    manager: str = ""
    official_url: str | None = None
    comparison_eligible: bool = True
    inception_date: str | None = None
    asset_size_cny: float | None = None
    size_source: str | None = None
    metadata_source: str | None = None
    metadata_fetched_at: str | None = None
    nav_estimate: NavEstimateRule | None = None


@dataclass(frozen=True)
class ReferenceTarget:
    code: str
    display_name: str
    source: str = "eastmoney_global_futures"


@dataclass(frozen=True)
class FundGroup:
    id: str
    name: str
    funds: tuple[FundTarget, ...]
    reference: ReferenceTarget | None = None


@dataclass(frozen=True)
class MonitorConfig:
    groups: tuple[FundGroup, ...]
    notice_sources: dict[str, dict[str, str]]
    quota_source: dict[str, str]

    @property
    def funds(self) -> tuple[FundTarget, ...]:
        return tuple(fund for group in self.groups for fund in group.funds)

    @property
    def fund_map(self) -> dict[str, FundTarget]:
        return {fund.code: fund for fund in self.funds}

    @property
    def references(self) -> tuple[ReferenceTarget, ...]:
        unique = {
            group.reference.code: group.reference
            for group in self.groups
            if group.reference is not None
        }
        return tuple(unique.values())


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"配置字段不能为空: {field}")
    return text


def load_config(path: Path) -> MonitorConfig:
    if not path.exists():
        raise ValueError(f"配置文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("配置必须包含非空 groups 列表")

    seen_codes: set[str] = set()
    seen_ids: set[str] = set()
    groups: list[FundGroup] = []
    for raw_group in raw_groups:
        group_id = _require_text(raw_group.get("id"), "groups[].id")
        if group_id in seen_ids:
            raise ValueError(f"分组 id 重复: {group_id}")
        seen_ids.add(group_id)
        group_name = _require_text(raw_group.get("name"), f"{group_id}.name")
        raw_funds = raw_group.get("funds")
        if not isinstance(raw_funds, list) or not raw_funds:
            raise ValueError(f"分组不能为空: {group_name}")
        funds: list[FundTarget] = []
        for raw_fund in raw_funds:
            code = _require_text(raw_fund.get("code"), f"{group_id}.funds[].code").zfill(6)
            if not (code.isdigit() and len(code) == 6):
                raise ValueError(f"证券代码必须为 6 位数字: {code}")
            if code in seen_codes:
                raise ValueError(f"证券代码重复: {code}")
            seen_codes.add(code)
            exchange = _require_text(raw_fund.get("exchange"), f"{code}.exchange").upper()
            if exchange not in {"SSE", "SZSE"}:
                raise ValueError(f"{code} 的 exchange 必须是 SSE 或 SZSE")
            nav_estimate = None
            raw_nav_estimate = raw_fund.get("nav_estimate")
            if raw_nav_estimate is not None:
                if not isinstance(raw_nav_estimate, dict):
                    raise ValueError(f"{code}.nav_estimate 必须为对象")
                reference_weight = float(raw_nav_estimate.get("reference_weight", 0))
                if not 0 < reference_weight <= 1:
                    raise ValueError(f"{code}.nav_estimate.reference_weight 必须在 (0, 1] 区间")
                nav_estimate = NavEstimateRule(
                    reference_code=_require_text(
                        raw_nav_estimate.get("reference_code"),
                        f"{code}.nav_estimate.reference_code",
                    ).upper(),
                    reference_weight=reference_weight,
                    description=str(raw_nav_estimate.get("description") or "").strip() or None,
                )
            funds.append(
                FundTarget(
                    code=code,
                    exchange=exchange,
                    display_name=_require_text(raw_fund.get("display_name"), f"{code}.display_name"),
                    fund_name=_require_text(raw_fund.get("fund_name"), f"{code}.fund_name"),
                    manager=str(raw_fund.get("manager") or "").strip(),
                    official_url=str(raw_fund.get("official_url") or "").strip() or None,
                    comparison_eligible=bool(raw_fund.get("comparison_eligible", True)),
                    inception_date=str(raw_fund.get("inception_date") or "").strip() or None,
                    asset_size_cny=float(raw_fund["asset_size_cny"]) if raw_fund.get("asset_size_cny") is not None else None,
                    size_source=str(raw_fund.get("size_source") or "").strip() or None,
                    metadata_source=str(raw_fund.get("metadata_source") or "").strip() or None,
                    metadata_fetched_at=str(raw_fund.get("metadata_fetched_at") or "").strip() or None,
                    nav_estimate=nav_estimate,
                )
            )
        reference = None
        raw_reference = raw_group.get("reference")
        if raw_reference is not None:
            if not isinstance(raw_reference, dict):
                raise ValueError(f"{group_id}.reference 必须为对象")
            source = str(raw_reference.get("source") or "eastmoney_global_futures").strip()
            if source not in {"eastmoney_global_futures", "eastmoney_global_index"}:
                raise ValueError(
                    f"{group_id}.reference.source 暂仅支持 eastmoney_global_futures、"
                    "eastmoney_global_index"
                )
            reference = ReferenceTarget(
                code=_require_text(raw_reference.get("code"), f"{group_id}.reference.code").upper(),
                display_name=_require_text(raw_reference.get("display_name"), f"{group_id}.reference.display_name"),
                source=source,
            )
        groups.append(FundGroup(id=group_id, name=group_name, funds=tuple(funds), reference=reference))

    notice_sources = raw.get("notice_sources") or {}
    quota_source = raw.get("quota_source") or {}
    if not isinstance(notice_sources, dict):
        raise ValueError("notice_sources 必须为对象")
    if not quota_source.get("url"):
        raise ValueError("quota_source.url 不能为空")
    return MonitorConfig(groups=tuple(groups), notice_sources=notice_sources, quota_source=quota_source)
