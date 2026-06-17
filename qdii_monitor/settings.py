from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def load_local_env(path: Path) -> None:
    """Load simple KEY=value configuration without overriding shell variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_env(ROOT / ".env")

CONFIG_FILE = Path(os.getenv("QDII_CONFIG_FILE", ROOT / "config" / "funds.yaml"))
DATA_DIR = Path(os.getenv("QDII_DATA_DIR", ROOT / "data"))
DB_FILE = Path(os.getenv("QDII_DB_FILE", DATA_DIR / "qdii_monitor.sqlite"))
STATIC_DIR = ROOT / "static"
TIMEZONE = ZoneInfo("Asia/Shanghai")
DISABLE_SCHEDULER = os.getenv("QDII_DISABLE_SCHEDULER", "").lower() in {"1", "true", "yes"}
DISABLE_STARTUP_REFRESH = os.getenv("QDII_DISABLE_STARTUP_REFRESH", "").lower() in {"1", "true", "yes"}
AKSHARE_PROXY_HOST = os.getenv("QDII_AKSHARE_PROXY_HOST", "").strip()
AKSHARE_PROXY_TOKEN = os.getenv("QDII_AKSHARE_PROXY_TOKEN", "").strip()
AKSHARE_PROXY_RETRY = int(os.getenv("QDII_AKSHARE_PROXY_RETRY", "30"))
AKSHARE_PROXY_HOOK_DOMAINS = tuple(
    item.strip()
    for item in os.getenv("QDII_AKSHARE_PROXY_HOOK_DOMAINS", "push2his.eastmoney.com").split(",")
    if item.strip()
)
