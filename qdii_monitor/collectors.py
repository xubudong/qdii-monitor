from __future__ import annotations

import importlib
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .configuration import FundTarget, MonitorConfig, ReferenceTarget
from .settings import AKSHARE_PROXY_HOOK_DOMAINS, AKSHARE_PROXY_HOST, AKSHARE_PROXY_RETRY, AKSHARE_PROXY_TOKEN
from .utils import calculate_premium, content_hash, now_iso, parse_float


def _column(columns: list[str], *tokens: str) -> str | None:
    for token in tokens:
        for candidate in columns:
            if token in candidate:
                return candidate
    return None


class QuoteCollector:
    QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"

    def __init__(
        self,
        fetcher: Callable[[], Any] | None = None,
        fallback_fetcher: Callable[[FundTarget], Any] | None = None,
        session: requests.Session | None = None,
    ):
        self.has_custom_fetcher = fetcher is not None
        self.fetcher = fetcher or self._fetch_akshare
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 QDII-Monitor/1.0"})
        self.fallback_fetcher = (
            fallback_fetcher
            if fallback_fetcher is not None
            else None if self.has_custom_fetcher else self._fetch_eastmoney_quote
        )

    @staticmethod
    def _fetch_akshare() -> Any:
        ak = importlib.import_module("akshare")
        etf = ak.fund_etf_spot_em()
        try:
            lof = ak.fund_lof_spot_em()
        except Exception:
            return etf
        frames = [frame for frame in (etf, lof) if frame is not None and not frame.empty]
        return pd.concat(frames, ignore_index=True) if frames else etf

    def _fetch_eastmoney_quote(self, fund: FundTarget) -> dict[str, Any] | None:
        market = "1" if fund.exchange == "SSE" else "0"
        response = self.session.get(
            self.QUOTE_URL,
            params={
                "secid": f"{market}.{fund.code}",
                "fields": "f43,f47,f48,f57,f58,f60,f169,f170",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("data")

    def collect(self, funds: tuple[FundTarget, ...]) -> list[dict[str, Any]]:
        raw = self.fetcher()
        if raw is None or raw.empty:
            raise RuntimeError("行情源未返回 ETF 数据")
        columns = [str(value).strip() for value in raw.columns]
        raw = raw.copy()
        raw.columns = columns
        code_col = _column(columns, "代码")
        price_col = _column(columns, "最新价", "最新")
        change_col = _column(columns, "涨跌幅")
        iopv_col = _column(columns, "IOPV实时估值", "IOPV")
        turnover_col = _column(columns, "成交额")
        share_col = _column(columns, "最新份额", "基金份额")
        if not code_col or not price_col:
            raise RuntimeError("行情源缺少代码或最新价字段")
        wanted = {fund.code for fund in funds}
        captured_at = now_iso()
        result = []
        matched: set[str] = set()
        for _, row in raw.iterrows():
            code = "".join(char for char in str(row[code_col]) if char.isdigit())[-6:]
            if code not in wanted:
                continue
            matched.add(code)
            price = parse_float(row[price_col])
            change_percent = parse_float(row[change_col]) if change_col else None
            iopv = parse_float(row[iopv_col]) if iopv_col else None
            turnover_amount = parse_float(row[turnover_col]) if turnover_col else None
            latest_shares = parse_float(row[share_col]) if share_col else None
            result.append(
                {
                    "code": code,
                    "captured_at": captured_at,
                    "latest_price": price,
                    "change_rate": change_percent / 100 if change_percent is not None else None,
                    "iopv": iopv,
                    "premium_rate": calculate_premium(price, iopv),
                    "turnover_amount": turnover_amount,
                    "latest_shares": latest_shares,
                    "source": "akshare.fund_etf_spot_em/fund_lof_spot_em",
                }
            )
        if self.fallback_fetcher is not None:
            for fund in funds:
                if fund.code in matched:
                    continue
                try:
                    fallback = self.fallback_fetcher(fund)
                except Exception:
                    continue
                if not fallback:
                    continue
                raw_price = parse_float(fallback.get("f43"))
                raw_change = parse_float(fallback.get("f170"))
                price = raw_price / 1000 if raw_price is not None else None
                if price is None:
                    continue
                result.append(
                    {
                        "code": fund.code,
                        "captured_at": captured_at,
                        "latest_price": price,
                        "change_rate": raw_change / 10000 if raw_change is not None else None,
                        "iopv": None,
                        "premium_rate": None,
                        "turnover_amount": parse_float(fallback.get("f48")),
                        "latest_shares": None,
                        "source": "eastmoney.stock_quote_fallback",
                    }
                )
        if not result:
            raise RuntimeError("行情源未匹配到配置中的 ETF 代码")
        return result


class FundMetadataCollector:
    def __init__(
        self,
        detail_fetcher: Callable[[str], Any] | None = None,
        sse_scale_fetcher: Callable[[], Any] | None = None,
        szse_scale_fetcher: Callable[[], Any] | None = None,
    ):
        self.detail_fetcher = detail_fetcher or self._fetch_detail
        self.sse_scale_fetcher = sse_scale_fetcher or self._fetch_sse_scale
        self.szse_scale_fetcher = szse_scale_fetcher or self._fetch_szse_scale

    @staticmethod
    def _fetch_detail(code: str) -> Any:
        ak = importlib.import_module("akshare")
        return ak.fund_individual_basic_info_xq(symbol=code, timeout=15)

    @staticmethod
    def _fetch_sse_scale() -> Any:
        ak = importlib.import_module("akshare")
        return ak.fund_etf_scale_sse(date=datetime.now().strftime("%Y%m%d"))

    @staticmethod
    def _fetch_szse_scale() -> Any:
        ak = importlib.import_module("akshare")
        return ak.fund_etf_scale_szse()

    def collect(self, funds: tuple[FundTarget, ...]) -> tuple[list[dict[str, Any]], list[str]]:
        fetched_at = now_iso()
        scale_rows, scale_warnings = self._exchange_scale_rows()
        rows: list[dict[str, Any]] = []
        warnings: list[str] = list(scale_warnings)
        for fund in funds:
            detail: dict[str, Any] = {}
            try:
                detail = self._parse_detail(self.detail_fetcher(fund.code))
            except Exception as exc:
                warnings.append(f"{fund.code}基金档案采集失败: {exc}")
            scale = scale_rows.get(fund.code, {})
            asset_size = detail.get("asset_size_cny")
            size_source = "资产规模" if asset_size is not None else None
            metadata_source = detail.get("metadata_source") or ""
            if asset_size is None and scale.get("share_size") is not None:
                price = scale.get("latest_price")
                if price is not None:
                    asset_size = scale["share_size"] * price
                    size_source = "份额估算"
                else:
                    asset_size = scale["share_size"]
                    size_source = "交易所份额"
                metadata_source = scale.get("metadata_source") or metadata_source
            rows.append(
                {
                    "code": fund.code,
                    "inception_date": detail.get("inception_date") or scale.get("listing_date"),
                    "asset_size_cny": asset_size,
                    "size_source": size_source,
                    "metadata_source": metadata_source or detail.get("metadata_source") or scale.get("metadata_source"),
                    "metadata_fetched_at": fetched_at,
                }
            )
        return rows, warnings

    def _exchange_scale_rows(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        result: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        for source, fetcher in (("SSE ETF规模", self.sse_scale_fetcher), ("SZSE ETF规模", self.szse_scale_fetcher)):
            try:
                frame = fetcher()
                if frame is None or frame.empty:
                    continue
                for _, row in frame.iterrows():
                    parsed = self._parse_scale_row(row, source)
                    if parsed.get("code"):
                        result[parsed["code"]] = {**result.get(parsed["code"], {}), **parsed}
            except Exception as exc:
                warnings.append(f"{source}采集失败: {exc}")
        return result, warnings

    def _parse_detail(self, frame: Any) -> dict[str, Any]:
        if frame is None or frame.empty:
            return {}
        result: dict[str, Any] = {"metadata_source": "雪球基金档案"}
        if {"item", "value"}.issubset(set(frame.columns)):
            pairs = {str(row["item"]): row["value"] for _, row in frame.iterrows()}
        else:
            row = frame.iloc[0]
            pairs = {str(column): row[column] for column in frame.columns}
        inception = _value_by_tokens(pairs, ("成立", "成立时间", "成立日期"))
        size = _value_by_tokens(pairs, ("最新规模", "资产规模", "规模"))
        result["inception_date"] = _date_text(inception)
        result["asset_size_cny"] = _size_to_cny(size)
        return result

    def _parse_scale_row(self, row: Any, source: str) -> dict[str, Any]:
        values = {str(key): row[key] for key in row.index}
        code = _digits(_value_by_tokens(values, ("基金代码", "SEC_CODE")))
        share_size = parse_float(_value_by_tokens(values, ("基金份额", "当前规模", "TOT_VOL")))
        latest_price = parse_float(_value_by_tokens(values, ("净值", "最新价")))
        listing_date = _date_text(_value_by_tokens(values, ("上市日期", "成立日期", "成立时间")))
        return {
            "code": code,
            "share_size": share_size,
            "latest_price": latest_price,
            "listing_date": listing_date,
            "metadata_source": source,
        }


def _value_by_tokens(values: dict[str, Any], tokens: tuple[str, ...]) -> Any:
    for token in tokens:
        for key, value in values.items():
            if token in key:
                return value
    return None


def _digits(value: Any) -> str | None:
    if value is None:
        return None
    text = "".join(char for char in str(value) if char.isdigit())
    return text[-6:] if text else None


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    try:
        return str(datetime.fromisoformat(text[:10]).date())
    except ValueError:
        return text[:10]


def _size_to_cny(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value)
    number = parse_float(value)
    if number is None:
        numeric_text = "".join(char for char in text if char.isdigit() or char in ".-")
        number = parse_float(numeric_text)
    if number is None:
        return None
    if "万亿" in text:
        return number * 1_000_000_000_000
    if "亿" in text:
        return number * 100_000_000
    if "万" in text:
        return number * 10_000
    return number


class DailyPremiumCollector:
    """Build daily close/NAV premium history for context, separate from live IOPV."""

    def __init__(
        self,
        price_fetcher: Callable[..., Any] | None = None,
        nav_fetcher: Callable[..., Any] | None = None,
        official_nav_fetcher: Callable[[FundTarget], Any] | None = None,
    ):
        self.price_fetcher = price_fetcher or self._fetch_price
        self.nav_fetcher = nav_fetcher or self._fetch_nav
        self.official_nav_fetcher = official_nav_fetcher or self._fetch_official_nav
        self._proxy_ready = False

    def _prepare_proxy(self) -> None:
        if self._proxy_ready or not AKSHARE_PROXY_HOST:
            return
        try:
            patch = importlib.import_module("akshare_proxy_patch")
        except ImportError as exc:
            raise RuntimeError("已配置历史行情代理，但未安装 akshare_proxy_patch") from exc
        patch.install_patch(
            AKSHARE_PROXY_HOST,
            auth_token=AKSHARE_PROXY_TOKEN,
            retry=AKSHARE_PROXY_RETRY,
            hook_domains=list(AKSHARE_PROXY_HOOK_DOMAINS),
        )
        self._proxy_ready = True

    @staticmethod
    def _fetch_price(code: str, start_date: str, end_date: str) -> Any:
        ak = importlib.import_module("akshare")
        fetch_args = {
            "symbol": code,
            "period": "daily",
            "start_date": start_date,
            "end_date": end_date,
            "adjust": "",
        }
        try:
            frame = ak.fund_etf_hist_em(**fetch_args)
            if frame is not None and not frame.empty:
                return frame
        except Exception as etf_exc:
            try:
                return ak.fund_lof_hist_em(**fetch_args)
            except Exception as lof_exc:
                raise RuntimeError(f"ETF/LOF 历史价格均采集失败: ETF={etf_exc}; LOF={lof_exc}") from lof_exc
        return ak.fund_lof_hist_em(**fetch_args)

    @staticmethod
    def _fetch_nav(code: str) -> Any:
        ak = importlib.import_module("akshare")
        return ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")

    @staticmethod
    def _fetch_official_nav(fund: FundTarget) -> Any:
        if not fund.official_url:
            return pd.DataFrame()
        response = requests.get(
            fund.official_url,
            headers={"User-Agent": "Mozilla/5.0 QDII-Monitor/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
        if "fsfund.com" in fund.official_url:
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*单位净值\((\d{4}-\d{2}-\d{2})\)", text)
        elif "efunds.com.cn" in fund.official_url:
            match = re.search(
                r"([0-9]+(?:\.[0-9]+)?)\s*单位净值\(元\).*?基金净值日期[:：]\s*(\d{4}-\d{2}-\d{2})",
                text,
                re.S,
            )
        else:
            match = None
        if not match:
            return pd.DataFrame()
        return pd.DataFrame([{"净值日期": match.group(2), "单位净值": float(match.group(1))}])

    @staticmethod
    def _target_date(today: date | None = None) -> date:
        target = (today or datetime.now().date()) - timedelta(days=1)
        while target.weekday() >= 5:
            target -= timedelta(days=1)
        return target

    def collect(
        self,
        funds: tuple[FundTarget, ...],
        days: int = 1095,
        latest_dates: dict[str, str] | None = None,
        date_bounds: dict[str, dict[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        latest_dates = latest_dates or {}
        date_bounds = date_bounds or {}
        target = self._target_date()
        initial_start = target - timedelta(days=days)
        pending: list[tuple[FundTarget, date]] = []
        backfill_codes: list[str] = []
        skipped_codes: list[str] = []
        for fund in funds:
            bounds = date_bounds.get(fund.code)
            latest_text = (bounds or {}).get("latest") or latest_dates.get(fund.code)
            earliest_text = (bounds or {}).get("earliest")
            latest = datetime.strptime(latest_text, "%Y-%m-%d").date() if latest_text else None
            earliest = datetime.strptime(earliest_text, "%Y-%m-%d").date() if earliest_text else None
            needs_backfill = bool(bounds) and (earliest is None or earliest > initial_start)
            if latest and latest >= target and not needs_backfill:
                skipped_codes.append(fund.code)
                continue
            if needs_backfill:
                backfill_codes.append(fund.code)
            pending.append((fund, initial_start if needs_backfill else latest + timedelta(days=1) if latest else initial_start))
        detail = {
            "target_date": target.isoformat(),
            "requested_codes": len(pending),
            "skipped_codes": len(skipped_codes),
            "backfill_codes": len(backfill_codes),
            "price_requests": len(pending),
            "nav_requests": len(pending),
        }
        if not pending:
            return [], [], detail
        self._prepare_proxy()
        fetched_at = now_iso()
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for fund, start in pending:
            try:
                prices = self.price_fetcher(fund.code, start.strftime("%Y%m%d"), target.strftime("%Y%m%d"))
                nav = self.nav_fetcher(fund.code)
                if prices is None or prices.empty or nav is None or nav.empty:
                    raise RuntimeError("未返回历史价格或净值")
                official_nav = self.official_nav_fetcher(fund)
                if official_nav is not None and not official_nav.empty:
                    nav = pd.concat([nav, official_nav], ignore_index=True)
                prices = prices[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "close_price"}).copy()
                nav = nav[["净值日期", "单位净值"]].rename(columns={"净值日期": "trade_date", "单位净值": "nav"}).copy()
                prices["trade_date"] = prices["trade_date"].astype(str).str.slice(0, 10)
                nav["trade_date"] = nav["trade_date"].astype(str).str.slice(0, 10)
                nav = nav.dropna(subset=["nav"]).drop_duplicates(subset=["trade_date"], keep="last")
                merged = prices.merge(nav, on="trade_date", how="left").sort_values("trade_date")
                merged["nav"] = merged["nav"].ffill()
                for _, item in merged.iterrows():
                    close_price = parse_float(item["close_price"])
                    nav_value = parse_float(item["nav"])
                    premium_rate = calculate_premium(close_price, nav_value)
                    if premium_rate is None:
                        continue
                    rows.append(
                        {
                            "code": fund.code,
                            "trade_date": item["trade_date"],
                            "close_price": close_price,
                            "nav": nav_value,
                            "premium_rate": premium_rate,
                            "source": "akshare.close_nav_daily",
                            "fetched_at": fetched_at,
                        }
                    )
            except Exception as exc:
                warnings.append(f"{fund.code}: {exc}")
        if not rows and warnings:
            raise RuntimeError("历史日线溢价采集失败: " + "; ".join(warnings))
        if not rows:
            warnings.append("No new daily premium rows were produced; official NAV may not be available yet.")
        return rows, warnings, detail


class ReferenceCollector:
    """Collect optional market context; these quotes never affect ETF ranking."""

    FUTURES_URL = "https://futsseapi.eastmoney.com/list/COMEX,NYMEX,COBOT,SGX,NYBOT,LME,MDEX,TOCOM,IPE"
    INDEX_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"
    TRENDS_URL = "https://push2delay.eastmoney.com/api/qt/stock/trends2/get"

    def __init__(self, fetcher: Callable[[], Any] | None = None, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 QDII-Monitor/1.0"})
        self.has_custom_fetcher = fetcher is not None
        self.fetcher = fetcher or self._fetch_eastmoney

    def _fetch_eastmoney(self) -> list[dict[str, Any]]:
        response = self.session.get(
            self.FUTURES_URL,
            params={
                "orderBy": "dm",
                "sort": "desc",
                "pageSize": "1000",
                "pageIndex": "0",
                "token": "58b2fa8f54638b60b87d69b31969089c",
                "field": "dm,sc,name,p,zde,zdf,o,h,l,zjsj",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("list") or []

    def _fetch_eastmoney_index(self, symbol: str) -> dict[str, Any]:
        response = self.session.get(
            self.INDEX_URL,
            params={
                "secid": f"100.{symbol}",
                "fields": "f43,f57,f58,f60,f169,f170",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        latest_price = parse_float(data.get("f43"))
        previous_close = parse_float(data.get("f60"))
        change_percent = parse_float(data.get("f170"))
        return {
            "code": symbol,
            "latest_price": latest_price / 100 if latest_price is not None else None,
            "previous_settle": previous_close / 100 if previous_close is not None else None,
            "change_rate": change_percent / 10000 if change_percent is not None else None,
        } if data else {}

    def _fetch_eastmoney_intraday(self, secid: str, symbol: str, source: str) -> list[dict[str, Any]]:
        response = self.session.get(
            self.TRENDS_URL,
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ndays": "1",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        previous_settle = parse_float(data.get("preSettlement") or data.get("preClose"))
        result = []
        for item in data.get("trends") or []:
            values = str(item).split(",")
            latest_price = parse_float(values[2] if len(values) > 2 else None)
            if not values or latest_price is None:
                continue
            result.append(
                {
                    "code": symbol,
                    "captured_at": values[0].replace(" ", "T") + "+08:00",
                    "latest_price": latest_price,
                    "change_rate": calculate_premium(latest_price, previous_settle),
                    "previous_settle": previous_settle,
                    "source": source,
                }
            )
        return result

    def collect(self, references: tuple[ReferenceTarget, ...]) -> tuple[list[dict[str, Any]], list[str]]:
        if not references:
            return [], []
        captured_at = now_iso()
        result: list[dict[str, Any]] = []
        warnings: list[str] = []
        eastmoney = {reference.code for reference in references if reference.source == "eastmoney_global_futures"}
        if eastmoney:
            try:
                raw = self.fetcher()
                rows = raw.to_dict("records") if hasattr(raw, "to_dict") else raw
                if not rows:
                    raise RuntimeError("未返回国际期货数据")
                for row in rows:
                    code = str(row.get("dm") or row.get("代码") or "").upper()
                    if code not in eastmoney:
                        continue
                    change_percent = parse_float(row.get("zdf") if "zdf" in row else row.get("涨跌幅"))
                    if not self.has_custom_fetcher:
                        result.extend(
                            self._fetch_eastmoney_intraday(
                                f"103.{code}", code, "eastmoney.futures_global_intraday"
                            )
                        )
                    result.append({
                        "code": code,
                        "captured_at": captured_at,
                        "latest_price": parse_float(row.get("p") if "p" in row else row.get("最新价")),
                        "change_rate": change_percent / 100 if change_percent is not None else None,
                        "previous_settle": parse_float(row.get("zjsj") if "zjsj" in row else row.get("昨结")),
                        "source": "eastmoney.futures_global_spot",
                    })
            except Exception as exc:
                warnings.append(f"东方财富国际期货采集失败: {exc}")
        for reference in references:
            if reference.source != "eastmoney_global_index":
                continue
            try:
                row = self._fetch_eastmoney_index(reference.code)
                if not row:
                    raise RuntimeError("未返回指数行情")
                if not self.has_custom_fetcher:
                    result.extend(
                        self._fetch_eastmoney_intraday(
                            f"100.{reference.code}", reference.code, "eastmoney.global_index_intraday"
                        )
                    )
                result.append(
                    {
                        **row,
                        "captured_at": captured_at,
                        "source": "eastmoney.global_index",
                    }
                )
            except Exception as exc:
                warnings.append(f"{reference.display_name}采集失败: {exc}")
        wanted = {reference.code for reference in references}
        missing = sorted(wanted - {row["code"] for row in result})
        if missing:
            warnings.append(f"参考行情源未匹配品种: {', '.join(missing)}")
        return result, warnings


def classify_notice(title: str) -> str | None:
    text = title.replace(" ", "")
    if "恢复" in text and "申购" in text:
        return "恢复申购"
    if "暂停" in text and "申购" in text:
        return "暂停申购"
    if ("限制" in text or "限额" in text or "大额" in text) and "申购" in text:
        return "限制申购"
    return None


class NoticeCollector:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 QDII-Monitor/1.0"})

    def collect(self, config: MonitorConfig) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for fund in config.funds:
            try:
                raw_items = self._sse(fund) if fund.exchange == "SSE" else self._szse(fund)
                for item in raw_items:
                    notice_type = classify_notice(item["title"])
                    if notice_type:
                        rows.append(
                            {
                                "source": fund.exchange,
                                "code": fund.code,
                                "exchange": fund.exchange,
                                "title": item["title"],
                                "notice_type": notice_type,
                                "published_at": item.get("published_at") or "",
                                "url": item["url"],
                                "fetched_at": now_iso(),
                            }
                        )
            except Exception as exc:
                errors.append(f"{fund.code}: {exc}")
        if errors and not rows:
            raise RuntimeError("公告采集失败: " + "; ".join(errors))
        unique = {
            (row["source"], row["code"], row["title"], row["published_at"], row["url"]): row
            for row in rows
        }
        return list(unique.values())

    def _sse(self, fund: FundTarget) -> list[dict[str, Any]]:
        url = "https://query.sse.com.cn/commonQuery.do"
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "type": "inParams",
            "sqlId": "COMMON_PL_JJXX_JJGG_NEW_L",
            "TITLE": "",
            "SECURITY_CODE": fund.code,
            "BULLETIN_TYPE": "",
            "START_DATE": "",
            "END_DATE": "",
            "DATE_DESC": "1",
            "DATE_ASC": "",
            "CODE_DESC": "",
            "CODE_ASC": "",
        }
        response = self.session.get(
            url,
            params=params,
            headers={"Referer": "https://www.sse.com.cn/disclosure/fund/announcement/index.shtml"},
            timeout=15,
        )
        response.raise_for_status()
        return _json_notices(response.json(), "https://www.sse.com.cn")

    def _szse(self, fund: FundTarget) -> list[dict[str, Any]]:
        url = "https://www.szse.cn/api/disc/announcement/annList"
        response = self.session.post(
            url,
            json={
                "type": 2,
                "pageSize": 100,
                "pageNum": 1,
                "stock": [fund.code],
                "channelCode": ["fundinfoNotice_disc"],
            },
            headers={"Referer": "https://www.szse.cn/disclosure/fund/notice/index.html"},
            timeout=15,
        )
        response.raise_for_status()
        return _json_notices(response.json(), "https://disc.static.szse.cn")


class PurchaseStatusCollector:
    """Collect indicative current subscription status from Eastmoney/Tiantian Fund."""

    def __init__(self, fetcher: Callable[[], Any] | None = None):
        self.fetcher = fetcher or self._fetch_akshare

    @staticmethod
    def _fetch_akshare() -> Any:
        ak = importlib.import_module("akshare")
        return ak.fund_purchase_em()

    def collect(self, funds: tuple[FundTarget, ...]) -> list[dict[str, Any]]:
        raw = self.fetcher()
        if raw is None or raw.empty:
            raise RuntimeError("天天基金申购状态源未返回数据")
        columns = [str(value).strip() for value in raw.columns]
        raw = raw.copy()
        raw.columns = columns
        code_col = _column(columns, "基金代码", "代码")
        status_col = _column(columns, "申购状态")
        limit_col = _column(columns, "日累计限定金额", "日累计限额")
        next_open_col = _column(columns, "下一开放日")
        if not code_col or not status_col:
            raise RuntimeError("天天基金申购状态源缺少基金代码或申购状态字段")
        wanted = {fund.code for fund in funds}
        captured_at = now_iso()
        rows = []
        for _, row in raw.iterrows():
            code = "".join(char for char in str(row[code_col]) if char.isdigit())[-6:]
            if code not in wanted:
                continue
            status_value = str(row[status_col]).strip() if row[status_col] is not None else ""
            rows.append(
                {
                    "code": code,
                    "purchase_status": status_value if status_value and status_value.lower() != "nan" else None,
                    "daily_limit": parse_float(row[limit_col]) if limit_col else None,
                    "next_open_date": (
                        str(row[next_open_col]).strip()
                        if next_open_col and row[next_open_col] is not None and str(row[next_open_col]).lower() != "nan"
                        else None
                    ),
                    "source": "akshare.fund_purchase_em",
                    "captured_at": captured_at,
                }
            )
        if not rows:
            raise RuntimeError("天天基金申购状态源未匹配到配置中的 ETF 代码")
        return rows


def _json_notices(payload: Any, base_url: str) -> list[dict[str, Any]]:
    candidate_dicts: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            keys = {str(key).lower() for key in value}
            if any("title" in key for key in keys):
                candidate_dicts.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    items = []
    for item in candidate_dicts:
        title = next((str(value) for key, value in item.items() if "title" in str(key).lower() and value), "")
        if not title:
            continue
        url_value = next(
            (str(value) for key, value in item.items() if any(token in str(key).lower() for token in ("url", "path", "attach")) and value),
            "",
        )
        published = next(
            (str(value) for key, value in item.items() if any(token in str(key).lower() for token in ("date", "time")) and value),
            None,
        )
        items.append({"title": title, "published_at": published, "url": urljoin(base_url, url_value)})
    return items


class QuotaCollector:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 QDII-Monitor/1.0"})

    def collect(self, source: dict[str, str], previous: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(source["url"], timeout=20)
        response.raise_for_status()
        content = response.content
        soup = BeautifulSoup(content, "html.parser")
        items = []
        for anchor in soup.find_all("a", href=True):
            title = anchor.get_text(" ", strip=True)
            href = urljoin(source["url"], anchor["href"])
            combined = f"{title} {href}".lower()
            if "qdii" in combined or ("投资额度" in title and "审批" in title):
                items.append({"title": title or href.rsplit("/", 1)[-1], "url": href})
        if not items:
            page_title = soup.title.get_text(strip=True) if soup.title else source["name"]
            items = [{"title": page_title, "url": source["url"]}]
        unique = {(item["title"], item["url"]): item for item in items}
        items = list(unique.values())
        old_keys = {(item["title"], item["url"]) for item in (previous or {}).get("items", [])}
        return {
            "title": source["name"],
            "url": source["url"],
            "document_hash": content_hash(content),
            "fetched_at": now_iso(),
            "items": items,
            "new_items": [item for item in items if (item["title"], item["url"]) not in old_keys],
        }
