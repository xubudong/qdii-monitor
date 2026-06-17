from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .settings import TIMEZONE


def now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "--", "nan", "None"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def calculate_premium(last_price: Any, iopv: Any) -> float | None:
    price = parse_float(last_price)
    estimate = parse_float(iopv)
    if price is None or estimate is None or estimate <= 0:
        return None
    return price / estimate - 1


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

