from __future__ import annotations

from typing import Any

from .utils import parse_float


def parse_holdings_text(content: bytes, allowed_codes: set[str]) -> list[dict[str, Any]]:
    text = None
    for encoding in ("utf-8", "gb18030", "gbk", "gb2312"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("utf-8", errors="ignore")

    matched: dict[str, dict[str, Any]] = {}
    for line in text.splitlines()[1:]:
        parts = [part.strip() for part in line.replace("\t", " ").split() if part.strip()]
        code_index = next(
            (idx for idx, token in enumerate(parts[:5]) if token.isdigit() and len(token) == 6),
            None,
        )
        if code_index is None:
            continue
        code = parts[code_index]
        if code not in allowed_codes:
            continue
        shares = parse_float(parts[code_index + 3] if code_index + 3 < len(parts) else None)
        average_cost = parse_float(parts[code_index + 12] if code_index + 12 < len(parts) else None)
        if shares is None:
            continue
        matched[code] = {
            "code": code,
            "name": parts[code_index + 1] if code_index + 1 < len(parts) else code,
            "shares": shares,
            "average_cost": average_cost,
        }
    return list(matched.values())


def value_holding(holding: dict[str, Any], price: float | None) -> dict[str, Any]:
    row = dict(holding)
    shares = holding.get("shares")
    cost = holding.get("average_cost")
    if price is None or shares is None:
        row.update({"latest_price": price, "market_value": None, "unrealized_pnl": None, "pnl_rate": None})
        return row
    market_value = round(float(shares) * float(price), 2)
    pnl = None if cost is None else round((float(price) - float(cost)) * float(shares), 2)
    pnl_rate = None if cost in (None, 0) else float(price) / float(cost) - 1
    row.update(
        {
            "latest_price": price,
            "market_value": market_value,
            "unrealized_pnl": pnl,
            "pnl_rate": pnl_rate,
        }
    )
    return row
