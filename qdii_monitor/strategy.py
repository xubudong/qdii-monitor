from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from .settings import DB_FILE


DEFAULT_NASDAQ_POOL = (
    "513100",
    "513110",
    "513390",
    "159660",
    "159632",
    "159659",
)


@dataclass(frozen=True)
class DailyPoint:
    code: str
    trade_date: str
    close_price: float
    premium_rate: float


@dataclass(frozen=True)
class Trade:
    trade_date: str
    from_code: str
    to_code: str
    from_premium: float
    to_premium: float
    spread: float
    value_after_cost: float
    anchor_code: str | None = None


@dataclass(frozen=True)
class AnchorPoint:
    trade_date: str
    anchor_code: str
    candidate_code: str | None
    anchor_score: float | None
    candidate_score: float | None
    score_margin: float | None
    pending_code: str | None
    pending_days: int
    days_since_anchor_switch: int
    switched: bool


@dataclass(frozen=True)
class EquityPoint:
    trade_date: str
    strategy_value: float
    benchmark_value: float
    held_code: str

    @property
    def strategy_nav(self) -> float:
        return self.strategy_value

    @property
    def benchmark_nav(self) -> float:
        return self.benchmark_value


@dataclass(frozen=True)
class BacktestResult:
    mode: str
    codes: tuple[str, ...]
    benchmark_code: str
    initial_code: str
    final_code: str
    start_date: str
    end_date: str
    days: int
    capital: float
    switch_threshold: float
    return_threshold: float | None
    min_hold_days: int
    switch_cost: float
    max_buy_premium: float | None
    final_value: float
    benchmark_value: float
    total_return: float
    benchmark_return: float
    alpha_return: float
    alpha_value: float
    annual_return: float
    benchmark_annual_return: float
    annual_alpha: float
    max_drawdown: float
    switches: int
    trades: tuple[Trade, ...]
    equity_curve: tuple[EquityPoint, ...]
    anchor_history: tuple[AnchorPoint, ...] = ()
    anchor_window: int | None = None
    anchor_confirm_days: int | None = None
    anchor_min_hold_days: int | None = None
    anchor_switch_margin: float | None = None

    def summary_row(self) -> dict[str, float | int | str | None]:
        return {
            "mode": self.mode,
            "threshold_pct": self.switch_threshold * 100,
            "return_threshold_pct": None if self.return_threshold is None else self.return_threshold * 100,
            "min_hold_days": self.min_hold_days,
            "cost_bps": self.switch_cost * 10000,
            "max_buy_premium_pct": None if self.max_buy_premium is None else self.max_buy_premium * 100,
            "anchor_window": self.anchor_window,
            "anchor_confirm_days": self.anchor_confirm_days,
            "anchor_min_hold_days": self.anchor_min_hold_days,
            "anchor_switch_margin_pct": None if self.anchor_switch_margin is None else self.anchor_switch_margin * 100,
            "final_code": self.final_code,
            "final_value": self.final_value,
            "benchmark_value": self.benchmark_value,
            "total_return_pct": self.total_return * 100,
            "benchmark_return_pct": self.benchmark_return * 100,
            "alpha_pct": self.alpha_return * 100,
            "alpha_value": self.alpha_value,
            "annual_return_pct": self.annual_return * 100,
            "benchmark_annual_return_pct": self.benchmark_annual_return * 100,
            "annual_alpha_pct": self.annual_alpha * 100,
            "max_drawdown_pct": self.max_drawdown * 100,
            "switches": self.switches,
            "anchor_switches": sum(1 for point in self.anchor_history if point.switched),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "days": self.days,
        }


def load_daily_points(
    db_path: Path,
    codes: Iterable[str],
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, dict[str, DailyPoint]]:
    code_tuple = tuple(dict.fromkeys(codes))
    if not code_tuple:
        raise ValueError("codes must not be empty")
    placeholders = ",".join("?" for _ in code_tuple)
    params: list[str] = list(code_tuple)
    filters = [f"code IN ({placeholders})"]
    if start_date:
        filters.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("trade_date <= ?")
        params.append(end_date)
    sql = f"""
        SELECT code, trade_date, close_price, premium_rate
        FROM daily_premium_history
        WHERE {" AND ".join(filters)}
        ORDER BY trade_date, code
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    data: dict[str, dict[str, DailyPoint]] = {}
    for row in rows:
        data.setdefault(row["trade_date"], {})[row["code"]] = DailyPoint(
            code=row["code"],
            trade_date=row["trade_date"],
            close_price=float(row["close_price"]),
            premium_rate=float(row["premium_rate"]),
        )
    return data


def common_dates(data: dict[str, dict[str, DailyPoint]], codes: Iterable[str]) -> list[str]:
    code_tuple = tuple(codes)
    return sorted(date for date, points in data.items() if all(code in points for code in code_tuple))


def simulate_rotation(
    data: dict[str, dict[str, DailyPoint]],
    codes: Iterable[str] = DEFAULT_NASDAQ_POOL,
    benchmark_code: str = "513100",
    initial_code: str | None = None,
    capital: float = 50000.0,
    switch_threshold: float = 0.025,
    return_threshold: float | None = None,
    switch_cost: float = 0.001,
    min_hold_days: int = 1,
    max_buy_premium: float | None = None,
    mode: str = "min-premium",
    anchor_window: int = 60,
    anchor_confirm_days: int = 5,
    anchor_min_hold_days: int = 40,
    anchor_switch_margin: float = 0.005,
) -> BacktestResult:
    if mode not in {"min-premium", "benchmark-cycle", "dynamic-anchor-cycle"}:
        raise ValueError("mode must be 'min-premium', 'benchmark-cycle', or 'dynamic-anchor-cycle'")
    if anchor_window < 1:
        raise ValueError("anchor_window must be positive")
    if anchor_confirm_days < 1:
        raise ValueError("anchor_confirm_days must be positive")
    if anchor_min_hold_days < 1:
        raise ValueError("anchor_min_hold_days must be positive")
    code_tuple = tuple(dict.fromkeys(codes))
    if benchmark_code not in code_tuple:
        raise ValueError("benchmark_code must be included in codes")
    dates = common_dates(data, code_tuple)
    if len(dates) < 2:
        raise ValueError("not enough common daily history for the requested ETF pool")
    held = initial_code or benchmark_code
    if held not in code_tuple:
        raise ValueError("initial_code must be included in codes")

    value = capital
    benchmark_value = capital
    peak_value = capital
    max_drawdown = 0.0
    trades: list[Trade] = []
    anchor_history: list[AnchorPoint] = []
    equity_curve = [EquityPoint(dates[0], value, benchmark_value, held)]
    days_since_switch = min_hold_days
    anchor_code = benchmark_code
    days_since_anchor_switch = 0
    pending_anchor: str | None = None
    pending_anchor_days = 0

    for index in range(len(dates) - 1):
        date = dates[index]
        next_date = dates[index + 1]
        points = data[date]
        next_points = data[next_date]
        current = points[held]
        anchor_switched = False
        candidate_anchor: str | None = None
        anchor_score: float | None = None
        candidate_anchor_score: float | None = None
        score_margin: float | None = None
        if mode == "dynamic-anchor-cycle":
            (
                anchor_code,
                candidate_anchor,
                anchor_score,
                candidate_anchor_score,
                score_margin,
                pending_anchor,
                pending_anchor_days,
                days_since_anchor_switch,
                anchor_switched,
            ) = _update_anchor(
                data=data,
                dates=dates,
                index=index,
                codes=code_tuple,
                current_anchor=anchor_code,
                pending_anchor=pending_anchor,
                pending_anchor_days=pending_anchor_days,
                days_since_anchor_switch=days_since_anchor_switch,
                anchor_window=anchor_window,
                anchor_confirm_days=anchor_confirm_days,
                anchor_min_hold_days=anchor_min_hold_days,
                anchor_switch_margin=anchor_switch_margin,
            )
        if mode == "dynamic-anchor-cycle":
            anchor_history.append(
                AnchorPoint(
                    trade_date=date,
                    anchor_code=anchor_code,
                    candidate_code=candidate_anchor,
                    anchor_score=anchor_score,
                    candidate_score=candidate_anchor_score,
                    score_margin=score_margin,
                    pending_code=pending_anchor,
                    pending_days=pending_anchor_days,
                    days_since_anchor_switch=days_since_anchor_switch,
                    switched=anchor_switched,
                )
            )
        eligible = [
            code for code in code_tuple
            if max_buy_premium is None or points[code].premium_rate <= max_buy_premium
        ]
        if not eligible:
            eligible = list(code_tuple)
        trading_anchor = anchor_code if mode == "dynamic-anchor-cycle" else benchmark_code
        candidate_mode = "benchmark-cycle" if mode == "dynamic-anchor-cycle" else mode
        candidate = _choose_candidate(
            mode=candidate_mode,
            held=held,
            benchmark_code=trading_anchor,
            points=points,
            eligible=eligible,
            switch_threshold=switch_threshold,
            return_threshold=return_threshold,
        )
        spread = current.premium_rate - points[candidate].premium_rate
        if candidate != held and days_since_switch >= min_hold_days:
            value *= 1 - switch_cost
            held = candidate
            days_since_switch = 0
            trades.append(
                Trade(
                    trade_date=date,
                    from_code=current.code,
                    to_code=candidate,
                    from_premium=current.premium_rate,
                    to_premium=points[candidate].premium_rate,
                    spread=spread,
                    value_after_cost=value,
                    anchor_code=trading_anchor,
                )
            )

        value *= next_points[held].close_price / points[held].close_price
        benchmark_value *= next_points[benchmark_code].close_price / points[benchmark_code].close_price
        peak_value = max(peak_value, value)
        max_drawdown = max(max_drawdown, 1 - value / peak_value)
        days_since_switch += 1
        if mode == "dynamic-anchor-cycle":
            days_since_anchor_switch += 1
        equity_curve.append(EquityPoint(next_date, value, benchmark_value, held))
    if mode == "dynamic-anchor-cycle":
        last_date = dates[-1]
        anchor_history.append(
            AnchorPoint(
                trade_date=last_date,
                anchor_code=anchor_code,
                candidate_code=None,
                anchor_score=None,
                candidate_score=None,
                score_margin=None,
                pending_code=pending_anchor,
                pending_days=pending_anchor_days,
                days_since_anchor_switch=days_since_anchor_switch,
                switched=False,
            )
        )

    total_return = value / capital - 1
    benchmark_return = benchmark_value / capital - 1
    years = max((len(dates) - 1) / 252, 1 / 252)
    annual_return = _annualize(total_return, years)
    benchmark_annual_return = _annualize(benchmark_return, years)
    return BacktestResult(
        mode=mode,
        codes=code_tuple,
        benchmark_code=benchmark_code,
        initial_code=initial_code or benchmark_code,
        final_code=held,
        start_date=dates[0],
        end_date=dates[-1],
        days=len(dates),
        capital=capital,
        switch_threshold=switch_threshold,
        return_threshold=return_threshold,
        min_hold_days=min_hold_days,
        switch_cost=switch_cost,
        max_buy_premium=max_buy_premium,
        final_value=value,
        benchmark_value=benchmark_value,
        total_return=total_return,
        benchmark_return=benchmark_return,
        alpha_return=total_return - benchmark_return,
        alpha_value=value - benchmark_value,
        annual_return=annual_return,
        benchmark_annual_return=benchmark_annual_return,
        annual_alpha=annual_return - benchmark_annual_return,
        max_drawdown=max_drawdown,
        switches=len(trades),
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
        anchor_history=tuple(anchor_history),
        anchor_window=anchor_window if mode == "dynamic-anchor-cycle" else None,
        anchor_confirm_days=anchor_confirm_days if mode == "dynamic-anchor-cycle" else None,
        anchor_min_hold_days=anchor_min_hold_days if mode == "dynamic-anchor-cycle" else None,
        anchor_switch_margin=anchor_switch_margin if mode == "dynamic-anchor-cycle" else None,
    )


def _choose_candidate(
    mode: str,
    held: str,
    benchmark_code: str,
    points: dict[str, DailyPoint],
    eligible: list[str],
    switch_threshold: float,
    return_threshold: float | None,
) -> str:
    lowest = min(eligible, key=lambda code: points[code].premium_rate)
    if mode == "min-premium":
        spread = points[held].premium_rate - points[lowest].premium_rate
        return lowest if lowest != held and spread >= switch_threshold else held

    benchmark_spread = points[benchmark_code].premium_rate - points[lowest].premium_rate
    if held == benchmark_code:
        return lowest if lowest != held and benchmark_spread >= switch_threshold else held

    buyback_threshold = switch_threshold if return_threshold is None else return_threshold
    held_vs_benchmark = points[benchmark_code].premium_rate - points[held].premium_rate
    if held_vs_benchmark <= buyback_threshold:
        return benchmark_code

    held_vs_lowest = points[held].premium_rate - points[lowest].premium_rate
    if lowest != held and lowest != benchmark_code and held_vs_lowest >= switch_threshold:
        return lowest
    return held


def _update_anchor(
    data: dict[str, dict[str, DailyPoint]],
    dates: list[str],
    index: int,
    codes: tuple[str, ...],
    current_anchor: str,
    pending_anchor: str | None,
    pending_anchor_days: int,
    days_since_anchor_switch: int,
    anchor_window: int,
    anchor_confirm_days: int,
    anchor_min_hold_days: int,
    anchor_switch_margin: float,
) -> tuple[str, str | None, float | None, float | None, float | None, str | None, int, int, bool]:
    if index < anchor_window:
        return (
            current_anchor,
            None,
            None,
            None,
            None,
            pending_anchor,
            pending_anchor_days,
            days_since_anchor_switch,
            False,
        )
    scores = {
        code: _anchor_score(data, dates[index - anchor_window:index], codes, code)
        for code in codes
    }
    candidate_anchor, candidate_score = max(scores.items(), key=lambda item: item[1])
    current_score = scores[current_anchor]
    margin = candidate_score - current_score
    if candidate_anchor != current_anchor and margin >= anchor_switch_margin:
        if pending_anchor == candidate_anchor:
            pending_anchor_days += 1
        else:
            pending_anchor = candidate_anchor
            pending_anchor_days = 1
    else:
        pending_anchor = None
        pending_anchor_days = 0

    switched = False
    if (
        pending_anchor is not None
        and pending_anchor_days >= anchor_confirm_days
        and days_since_anchor_switch >= anchor_min_hold_days
    ):
        current_anchor = pending_anchor
        current_score = scores[current_anchor]
        days_since_anchor_switch = 0
        pending_anchor = None
        pending_anchor_days = 0
        switched = True

    return (
        current_anchor,
        candidate_anchor,
        current_score,
        candidate_score,
        margin,
        pending_anchor,
        pending_anchor_days,
        days_since_anchor_switch,
        switched,
    )


def _anchor_score(
    data: dict[str, dict[str, DailyPoint]],
    window_dates: list[str],
    codes: tuple[str, ...],
    code: str,
) -> float:
    excess_values: list[float] = []
    high_frequency = 0
    top_frequency = 0
    for date in window_dates:
        premiums = [data[date][item].premium_rate for item in codes]
        value = data[date][code].premium_rate
        excess = value - median(premiums)
        excess_values.append(excess)
        if excess >= 0.01:
            high_frequency += 1
        if value == max(premiums):
            top_frequency += 1
    count = len(window_dates)
    return mean(excess_values) + 0.003 * (high_frequency / count) + 0.002 * (top_frequency / count)


def grid_search(
    data: dict[str, dict[str, DailyPoint]],
    codes: Iterable[str] = DEFAULT_NASDAQ_POOL,
    benchmark_code: str = "513100",
    initial_code: str | None = None,
    capital: float = 50000.0,
    thresholds: Iterable[float] = (0.015, 0.02, 0.025, 0.03, 0.035),
    return_thresholds: Iterable[float | None] = (None,),
    switch_cost: float = 0.001,
    min_hold_days_values: Iterable[int] = (1, 3, 5, 10, 20),
    max_buy_premiums: Iterable[float | None] = (None,),
    mode: str = "min-premium",
    anchor_windows: Iterable[int] = (60,),
    anchor_confirm_days_values: Iterable[int] = (5,),
    anchor_min_hold_days_values: Iterable[int] = (40,),
    anchor_switch_margins: Iterable[float] = (0.005,),
) -> list[BacktestResult]:
    results = []
    if mode != "dynamic-anchor-cycle":
        anchor_windows = (60,)
        anchor_confirm_days_values = (5,)
        anchor_min_hold_days_values = (40,)
        anchor_switch_margins = (0.005,)
    for threshold in thresholds:
        for return_threshold in return_thresholds:
            if mode == "min-premium" and return_threshold is not None:
                continue
            if mode in {"benchmark-cycle", "dynamic-anchor-cycle"} and return_threshold is not None and return_threshold >= threshold:
                continue
            for min_hold_days in min_hold_days_values:
                for max_buy_premium in max_buy_premiums:
                    for anchor_window in anchor_windows:
                        for anchor_confirm_days in anchor_confirm_days_values:
                            for anchor_min_hold_days in anchor_min_hold_days_values:
                                for anchor_switch_margin in anchor_switch_margins:
                                    results.append(
                                        simulate_rotation(
                                            data=data,
                                            codes=codes,
                                            benchmark_code=benchmark_code,
                                            initial_code=initial_code,
                                            capital=capital,
                                            switch_threshold=threshold,
                                            return_threshold=return_threshold,
                                            switch_cost=switch_cost,
                                            min_hold_days=min_hold_days,
                                            max_buy_premium=max_buy_premium,
                                            mode=mode,
                                            anchor_window=anchor_window,
                                            anchor_confirm_days=anchor_confirm_days,
                                            anchor_min_hold_days=anchor_min_hold_days,
                                            anchor_switch_margin=anchor_switch_margin,
                                        )
                                    )
    return sorted(results, key=lambda result: (result.alpha_return, result.final_value), reverse=True)


def parse_range(text: str) -> list[float]:
    if ":" not in text:
        values = [float(item) for item in text.split(",") if item.strip()]
        return [value / 100 if value >= 0.5 else value for value in values]
    start, stop, step = (float(part) for part in text.split(":"))
    as_percent = max(start, stop, step) > 0.5
    values = []
    current = start
    while current <= stop + step / 2:
        values.append(current / 100 if as_percent else current)
        current += step
    return values


def parse_ints(text: str) -> list[int]:
    return [int(item) for item in text.split(",") if item.strip()]


def print_results(results: list[BacktestResult], top: int) -> None:
    headers = [
        "rank", "mode", "threshold%", "return%", "min_hold", "cost_bps", "max_buy%",
        "alpha%", "alpha_value", "final", "benchmark", "ann_alpha%", "mdd%", "switches", "final_code",
    ]
    print("\t".join(headers))
    for index, result in enumerate(results[:top], start=1):
        max_buy = "" if result.max_buy_premium is None else f"{result.max_buy_premium * 100:.2f}"
        return_threshold = "" if result.return_threshold is None else f"{result.return_threshold * 100:.2f}"
        print(
            "\t".join(
                [
                    str(index),
                    result.mode,
                    f"{result.switch_threshold * 100:.2f}",
                    return_threshold,
                    str(result.min_hold_days),
                    f"{result.switch_cost * 10000:.1f}",
                    max_buy,
                    f"{result.alpha_return * 100:.2f}",
                    f"{result.alpha_value:.2f}",
                    f"{result.final_value:.2f}",
                    f"{result.benchmark_value:.2f}",
                    f"{result.annual_alpha * 100:.2f}",
                    f"{result.max_drawdown * 100:.2f}",
                    str(result.switches),
                    result.final_code,
                ]
            )
        )


def write_csv(path: Path, results: list[BacktestResult]) -> None:
    rows = [result.summary_row() for result in results]
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_equity_csv(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "trade_date",
                "strategy_nav",
                "benchmark_nav",
                "alpha_nav",
                "held_code",
            ],
        )
        writer.writeheader()
        for point in result.equity_curve:
            strategy_nav = point.strategy_value / result.capital
            benchmark_nav = point.benchmark_value / result.capital
            writer.writerow(
                {
                    "trade_date": point.trade_date,
                    "strategy_nav": strategy_nav,
                    "benchmark_nav": benchmark_nav,
                    "alpha_nav": strategy_nav - benchmark_nav,
                    "held_code": point.held_code,
                }
            )


def write_svg_plot(path: Path, result: BacktestResult, width: int = 1100, height: int = 620) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    margin_left, margin_right, margin_top, margin_bottom = 72, 28, 44, 72
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom
    strategy = [point.strategy_value / result.capital for point in result.equity_curve]
    benchmark = [point.benchmark_value / result.capital for point in result.equity_curve]
    values = strategy + benchmark
    y_min = min(values)
    y_max = max(values)
    padding = (y_max - y_min) * 0.08 or 0.01
    y_min -= padding
    y_max += padding

    def x_at(index: int) -> float:
        if len(result.equity_curve) == 1:
            return margin_left
        return margin_left + plot_width * index / (len(result.equity_curve) - 1)

    def y_at(value: float) -> float:
        return margin_top + plot_height * (1 - (value - y_min) / (y_max - y_min))

    def path_for(series: list[float]) -> str:
        commands = []
        for index, value in enumerate(series):
            prefix = "M" if index == 0 else "L"
            commands.append(f"{prefix}{x_at(index):.2f},{y_at(value):.2f}")
        return " ".join(commands)

    y_ticks = [y_min + (y_max - y_min) * i / 5 for i in range(6)]
    x_tick_indexes = sorted(set(round((len(result.equity_curve) - 1) * i / 5) for i in range(6)))
    trade_markers = []
    date_to_index = {point.trade_date: index for index, point in enumerate(result.equity_curve)}
    for trade in result.trades:
        index = date_to_index.get(trade.trade_date)
        if index is None:
            continue
        trade_markers.append(
            f'<circle cx="{x_at(index):.2f}" cy="{y_at(strategy[index]):.2f}" r="4" fill="#ef4444">'
            f"<title>{trade.trade_date} {trade.from_code}->{trade.to_code} spread {trade.spread * 100:.2f}%</title>"
            "</circle>"
        )

    title = (
        f"Premium Rotation vs {result.benchmark_code} | "
        f"threshold {result.switch_threshold * 100:.2f}%, min hold {result.min_hold_days}d, "
        f"cost {result.switch_cost * 10000:.1f}bps"
    )
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{margin_left}" y="26" font-family="Arial, sans-serif" font-size="18" font-weight="700" fill="#111827">{_xml(title)}</text>',
        f'<text x="{margin_left}" y="{height - 18}" font-family="Arial, sans-serif" font-size="12" fill="#6b7280">'
        f'Alpha {result.alpha_return * 100:.2f}% ({result.alpha_value:.0f} CNY), switches {result.switches}, '
        f'{result.start_date} to {result.end_date}</text>',
    ]
    for tick in y_ticks:
        y = y_at(tick)
        svg.append(f'<line x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}" stroke="#e5e7eb"/>')
        svg.append(
            f'<text x="{margin_left - 10}" y="{y + 4:.2f}" text-anchor="end" '
            f'font-family="Arial, sans-serif" font-size="12" fill="#6b7280">{tick:.2f}</text>'
        )
    for index in x_tick_indexes:
        point = result.equity_curve[index]
        x = x_at(index)
        svg.append(f'<line x1="{x:.2f}" y1="{margin_top}" x2="{x:.2f}" y2="{height - margin_bottom}" stroke="#f3f4f6"/>')
        svg.append(
            f'<text x="{x:.2f}" y="{height - margin_bottom + 22}" text-anchor="middle" '
            f'font-family="Arial, sans-serif" font-size="12" fill="#6b7280">{point.trade_date}</text>'
        )
    svg.extend(
        [
            f'<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#9ca3af"/>',
            f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" stroke="#9ca3af"/>',
            f'<path d="{path_for(benchmark)}" fill="none" stroke="#64748b" stroke-width="2.2"/>',
            f'<path d="{path_for(strategy)}" fill="none" stroke="#2563eb" stroke-width="2.8"/>',
            *trade_markers,
            f'<rect x="{width - 280}" y="{margin_top + 8}" width="240" height="72" rx="6" fill="#ffffff" stroke="#e5e7eb"/>',
            f'<line x1="{width - 260}" y1="{margin_top + 30}" x2="{width - 220}" y2="{margin_top + 30}" stroke="#2563eb" stroke-width="3"/>',
            f'<text x="{width - 210}" y="{margin_top + 34}" font-family="Arial, sans-serif" font-size="13" fill="#111827">rotation strategy</text>',
            f'<line x1="{width - 260}" y1="{margin_top + 56}" x2="{width - 220}" y2="{margin_top + 56}" stroke="#64748b" stroke-width="3"/>',
            f'<text x="{width - 210}" y="{margin_top + 60}" font-family="Arial, sans-serif" font-size="13" fill="#111827">buy & hold {result.benchmark_code}</text>',
            "</svg>",
        ]
    )
    path.write_text("\n".join(svg), encoding="utf-8")


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _annualize(total_return: float, years: float) -> float:
    if total_return <= -1:
        return -1.0
    return math.pow(1 + total_return, 1 / years) - 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest QDII ETF premium rotation strategies.")
    parser.add_argument("--db", type=Path, default=DB_FILE, help="SQLite database path.")
    parser.add_argument("--codes", default=",".join(DEFAULT_NASDAQ_POOL), help="Comma-separated ETF codes.")
    parser.add_argument("--benchmark", default="513100", help="Benchmark buy-and-hold code.")
    parser.add_argument("--initial", default=None, help="Initial holding code. Defaults to benchmark.")
    parser.add_argument("--capital", type=float, default=50000.0, help="Initial capital.")
    parser.add_argument(
        "--mode",
        choices=("min-premium", "benchmark-cycle", "dynamic-anchor-cycle"),
        default="min-premium",
        help=(
            "min-premium always rotates to the current lowest-premium ETF; "
            "benchmark-cycle leaves benchmark on a large spread and buys it back when the spread narrows; "
            "dynamic-anchor-cycle chooses the high-premium anchor from recent history."
        ),
    )
    parser.add_argument("--thresholds", default="1.0:4.0:0.25", help="Threshold list or range. Percent values allowed.")
    parser.add_argument(
        "--return-thresholds",
        default="",
        help="Benchmark-cycle buyback threshold list/range. Percent values allowed. Defaults to threshold when empty.",
    )
    parser.add_argument("--min-hold-days", default="1,3,5,10,20", help="Comma-separated min hold days.")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="Full-switch cost in basis points.")
    parser.add_argument("--max-buy-premiums", default="", help="Comma-separated max buy premium filters, in percent or decimal.")
    parser.add_argument("--anchor-windows", default="60", help="Dynamic-anchor scoring windows in trading days.")
    parser.add_argument("--anchor-confirm-days", default="5", help="Dynamic-anchor consecutive confirmation days.")
    parser.add_argument("--anchor-min-hold-days", default="40", help="Dynamic-anchor minimum anchor holding days.")
    parser.add_argument("--anchor-switch-margins", default="0.5", help="Dynamic-anchor score margin, percent or decimal.")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD.")
    parser.add_argument("--top", type=int, default=10, help="Number of best rows to print.")
    parser.add_argument("--output", type=Path, default=None, help="Optional CSV output path.")
    parser.add_argument("--curve-output", type=Path, default=None, help="Optional best-result equity curve CSV path.")
    parser.add_argument("--plot", type=Path, default=None, help="Optional best-result SVG plot path.")
    parser.add_argument("--show-trades", action="store_true", help="Print trades for the best result.")
    args = parser.parse_args()

    codes = tuple(code.strip() for code in args.codes.split(",") if code.strip())
    data = load_daily_points(args.db, codes, start_date=args.start, end_date=args.end)
    max_buy_premiums: list[float | None] = [None]
    if args.max_buy_premiums.strip():
        max_buy_premiums = [None] + parse_range(args.max_buy_premiums)
    return_thresholds: list[float | None] = [None]
    if args.mode in {"benchmark-cycle", "dynamic-anchor-cycle"} and args.return_thresholds.strip():
        return_thresholds = parse_range(args.return_thresholds)
    results = grid_search(
        data=data,
        codes=codes,
        benchmark_code=args.benchmark,
        initial_code=args.initial,
        capital=args.capital,
        thresholds=parse_range(args.thresholds),
        return_thresholds=return_thresholds,
        switch_cost=args.cost_bps / 10000,
        min_hold_days_values=parse_ints(args.min_hold_days),
        max_buy_premiums=max_buy_premiums,
        mode=args.mode,
        anchor_windows=parse_ints(args.anchor_windows),
        anchor_confirm_days_values=parse_ints(args.anchor_confirm_days),
        anchor_min_hold_days_values=parse_ints(args.anchor_min_hold_days),
        anchor_switch_margins=parse_range(args.anchor_switch_margins),
    )
    print_results(results, args.top)
    if args.output:
        write_csv(args.output, results)
        print(f"\nWrote {len(results)} rows to {args.output}")
    if args.curve_output and results:
        write_equity_csv(args.curve_output, results[0])
        print(f"Wrote best equity curve to {args.curve_output}")
    if args.plot and results:
        write_svg_plot(args.plot, results[0])
        print(f"Wrote best curve plot to {args.plot}")
    if args.show_trades and results:
        best = results[0]
        print("\nBest result trades:")
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
