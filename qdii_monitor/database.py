from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

from .utils import now_iso


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quote_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    latest_price REAL,
                    change_rate REAL,
                    iopv REAL,
                    premium_rate REAL,
                    premium_source TEXT,
                    premium_note TEXT,
                    premium_formula TEXT,
                    estimated_nav REAL,
                    nav_date TEXT,
                    nav_value REAL,
                    reference_code TEXT,
                    reference_captured_at TEXT,
                    reference_price REAL,
                    baseline_captured_at TEXT,
                    baseline_price REAL,
                    baseline_kind TEXT,
                    reference_weight REAL,
                    reference_change REAL,
                    weighted_change REAL,
                    turnover_amount REAL,
                    latest_shares REAL,
                    source TEXT NOT NULL,
                    UNIQUE(code, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_quote_code_time
                    ON quote_snapshots(code, captured_at DESC);

                CREATE TABLE IF NOT EXISTS daily_premium_history (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    close_price REAL NOT NULL,
                    nav REAL NOT NULL,
                    premium_rate REAL NOT NULL,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_premium_code_date
                    ON daily_premium_history(code, trade_date DESC);

                CREATE TABLE IF NOT EXISTS reference_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    latest_price REAL,
                    change_rate REAL,
                    previous_settle REAL,
                    source TEXT NOT NULL,
                    UNIQUE(code, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_reference_code_time
                    ON reference_snapshots(code, captured_at DESC);

                CREATE TABLE IF NOT EXISTS premarket_anchor_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    reference_code TEXT NOT NULL,
                    reference_name TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    baseline_captured_at TEXT,
                    reference_price REAL,
                    baseline_price REAL,
                    expected_change_rate REAL,
                    futures_change_rate REAL,
                    overnight_change_rate REAL,
                    rate_change_bps REAL,
                    rate_impact_level TEXT,
                    note TEXT,
                    source TEXT NOT NULL,
                    UNIQUE(group_id, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_premarket_anchor_group_time
                    ON premarket_anchor_snapshots(group_id, captured_at DESC);

                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    code TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    title TEXT NOT NULL,
                    notice_type TEXT NOT NULL,
                    published_at TEXT,
                    url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    UNIQUE(source, code, title, published_at, url)
                );
                CREATE INDEX IF NOT EXISTS idx_notice_code_date
                    ON notices(code, published_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS purchase_status_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    purchase_status TEXT,
                    daily_limit REAL,
                    next_open_date TEXT,
                    source TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    UNIQUE(code, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_purchase_status_code_time
                    ON purchase_status_snapshots(code, id DESC);

                CREATE TABLE IF NOT EXISTS fund_metadata (
                    code TEXT PRIMARY KEY,
                    inception_date TEXT,
                    asset_size_cny REAL,
                    size_source TEXT,
                    metadata_source TEXT,
                    metadata_fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quota_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    document_hash TEXT NOT NULL UNIQUE,
                    fetched_at TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    new_items_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS holdings (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    shares REAL,
                    average_cost REAL,
                    imported_at TEXT NOT NULL,
                    source_file TEXT
                );

                CREATE TABLE IF NOT EXISTS task_status (
                    task TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_started_at TEXT,
                    last_succeeded_at TEXT,
                    last_error TEXT,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            self._ensure_column(conn, "quote_snapshots", "change_rate", "REAL")
            self._ensure_column(conn, "quote_snapshots", "premium_source", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "premium_note", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "premium_formula", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "estimated_nav", "REAL")
            self._ensure_column(conn, "quote_snapshots", "nav_date", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "nav_value", "REAL")
            self._ensure_column(conn, "quote_snapshots", "reference_code", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "reference_captured_at", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "reference_price", "REAL")
            self._ensure_column(conn, "quote_snapshots", "baseline_captured_at", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "baseline_price", "REAL")
            self._ensure_column(conn, "quote_snapshots", "baseline_kind", "TEXT")
            self._ensure_column(conn, "quote_snapshots", "reference_weight", "REAL")
            self._ensure_column(conn, "quote_snapshots", "reference_change", "REAL")
            self._ensure_column(conn, "quote_snapshots", "weighted_change", "REAL")
            self._ensure_column(conn, "quote_snapshots", "turnover_amount", "REAL")
            self._ensure_column(conn, "quote_snapshots", "latest_shares", "REAL")
            self._ensure_column(conn, "premarket_anchor_snapshots", "rate_change_bps", "REAL")
            self._ensure_column(conn, "premarket_anchor_snapshots", "rate_impact_level", "TEXT")
            self._ensure_column(conn, "premarket_anchor_snapshots", "note", "TEXT")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def task_started(self, task: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_status(task, status, last_started_at, detail_json)
                VALUES (?, 'running', ?, '{}')
                ON CONFLICT(task) DO UPDATE SET status='running',
                    last_started_at=excluded.last_started_at, last_error=NULL
                """,
                (task, now_iso()),
            )

    def task_succeeded(self, task: str, detail: dict[str, Any], warning: str | None = None) -> None:
        status = "warning" if warning else "ok"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_status(task, status, last_succeeded_at, last_error, detail_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task) DO UPDATE SET status=excluded.status,
                    last_succeeded_at=excluded.last_succeeded_at,
                    last_error=excluded.last_error, detail_json=excluded.detail_json
                """,
                (task, status, now_iso(), warning, json.dumps(detail, ensure_ascii=False)),
            )

    def task_failed(self, task: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_status(task, status, last_error, detail_json)
                VALUES (?, 'error', ?, '{}')
                ON CONFLICT(task) DO UPDATE SET status='error', last_error=excluded.last_error
                """,
                (task, error[:1000]),
            )

    def task_statuses(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM task_status ORDER BY task").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            result.append(item)
        return result

    def insert_quotes(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        for value in values:
            value.setdefault("change_rate", None)
            value.setdefault("premium_source", None)
            value.setdefault("premium_note", None)
            value.setdefault("premium_formula", None)
            value.setdefault("estimated_nav", None)
            value.setdefault("nav_date", None)
            value.setdefault("nav_value", None)
            value.setdefault("reference_code", None)
            value.setdefault("reference_captured_at", None)
            value.setdefault("reference_price", None)
            value.setdefault("baseline_captured_at", None)
            value.setdefault("baseline_price", None)
            value.setdefault("baseline_kind", None)
            value.setdefault("reference_weight", None)
            value.setdefault("reference_change", None)
            value.setdefault("weighted_change", None)
            value.setdefault("turnover_amount", None)
            value.setdefault("latest_shares", None)
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO quote_snapshots
                    (code, captured_at, latest_price, change_rate, iopv, premium_rate,
                     premium_source, premium_note, premium_formula, estimated_nav, nav_date, nav_value,
                     reference_code, reference_captured_at, reference_price, baseline_captured_at,
                     baseline_price, baseline_kind, reference_weight, reference_change, weighted_change,
                     turnover_amount, latest_shares, source)
                VALUES (:code, :captured_at, :latest_price, :change_rate, :iopv, :premium_rate,
                        :premium_source, :premium_note, :premium_formula, :estimated_nav, :nav_date, :nav_value,
                        :reference_code, :reference_captured_at, :reference_price, :baseline_captured_at,
                        :baseline_price, :baseline_kind, :reference_weight, :reference_change, :weighted_change,
                        :turnover_amount, :latest_shares, :source)
                """,
                values,
            )
            return conn.total_changes - before

    def latest_quotes(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT q.*
                FROM quote_snapshots q
                JOIN (
                    SELECT code, MAX(id) AS id FROM quote_snapshots GROUP BY code
                ) latest ON q.id = latest.id
                """
            ).fetchall()
        return {row["code"]: dict(row) for row in rows}

    def quotes_at(self, captured_at: str) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM quote_snapshots
                WHERE captured_at = ?
                ORDER BY code
                """,
                (captured_at,),
            ).fetchall()
        return {row["code"]: dict(row) for row in rows}

    def quote_snapshot_times(self, limit: int = 240, mode: str = "latest") -> list[dict[str, Any]]:
        if mode in {"day", "close"}:
            return self._daily_quote_snapshot_times(limit, prefer="close")
        if mode == "open":
            return self._daily_quote_snapshot_times(limit, prefer="open")
        if mode == "us_close":
            return self._daily_quote_snapshot_times(limit, prefer="us_close")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT captured_at, COUNT(*) AS fund_count
                FROM quote_snapshots
                GROUP BY captured_at
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _daily_quote_snapshot_times(self, limit: int, prefer: str) -> list[dict[str, Any]]:
        order = "ASC" if prefer == "open" else "DESC"
        with self.connect() as conn:
            days = conn.execute(
                """
                SELECT substr(captured_at, 1, 10) AS trade_date
                FROM quote_snapshots
                GROUP BY substr(captured_at, 1, 10)
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            result = []
            for day in days:
                trade_date = day["trade_date"]
                if prefer == "us_close":
                    row = conn.execute(
                        """
                        SELECT captured_at, COUNT(*) AS fund_count
                        FROM quote_snapshots
                        WHERE substr(captured_at, 1, 10) = ?
                        GROUP BY captured_at
                        ORDER BY ABS((julianday(substr(captured_at, 1, 19)) - julianday(?)) * 86400),
                                 captured_at ASC
                        LIMIT 1
                        """,
                        (trade_date, f"{trade_date}T04:00:00"),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"""
                        SELECT captured_at, COUNT(*) AS fund_count
                        FROM quote_snapshots
                        WHERE captured_at >= ? AND captured_at < ?
                        GROUP BY captured_at
                        ORDER BY captured_at {order}
                        LIMIT 1
                        """,
                        (f"{trade_date}T09:30:00", f"{trade_date}T15:30:00"),
                    ).fetchone()
                if row is None:
                    row = conn.execute(
                        f"""
                        SELECT captured_at, COUNT(*) AS fund_count
                        FROM quote_snapshots
                        WHERE substr(captured_at, 1, 10) = ?
                        GROUP BY captured_at
                        ORDER BY captured_at {order}
                        LIMIT 1
                        """,
                        (trade_date,),
                    ).fetchone()
                if row is not None:
                    item = dict(row)
                    item["snapshot_mode"] = prefer
                    result.append(item)
        return result

    def replace_fund_metadata(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO fund_metadata
                    (code, inception_date, asset_size_cny, size_source, metadata_source, metadata_fetched_at)
                VALUES (:code, :inception_date, :asset_size_cny, :size_source, :metadata_source, :metadata_fetched_at)
                ON CONFLICT(code) DO UPDATE SET
                    inception_date=excluded.inception_date,
                    asset_size_cny=excluded.asset_size_cny,
                    size_source=excluded.size_source,
                    metadata_source=excluded.metadata_source,
                    metadata_fetched_at=excluded.metadata_fetched_at
                """,
                values,
            )
            return conn.total_changes - before

    def fund_metadata(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM fund_metadata ORDER BY code").fetchall()
        return {row["code"]: dict(row) for row in rows}

    def insert_references(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO reference_snapshots
                    (code, captured_at, latest_price, change_rate, previous_settle, source)
                VALUES (:code, :captured_at, :latest_price, :change_rate, :previous_settle, :source)
                ON CONFLICT(code, captured_at) DO UPDATE SET
                    latest_price=excluded.latest_price,
                    change_rate=excluded.change_rate,
                    previous_settle=excluded.previous_settle,
                    source=excluded.source
                """,
                values,
            )
            return conn.total_changes - before

    def latest_references(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*
                FROM reference_snapshots r
                JOIN (
                    SELECT code, MAX(id) AS id FROM reference_snapshots GROUP BY code
                ) latest ON r.id = latest.id
                """
            ).fetchall()
        return {row["code"]: dict(row) for row in rows}

    def reference_nav_baseline(
        self,
        code: str,
        nav_date: str,
        captured_at: str | None = None,
    ) -> dict[str, Any] | None:
        target_close = datetime.combine(
            datetime.fromisoformat(nav_date[:10]).date() + timedelta(days=1),
            time(4, 0),
        )
        close_start = (target_close - timedelta(minutes=10)).isoformat()
        close_end = (target_close + timedelta(minutes=10)).isoformat()
        close_conditions = [
            "code = ?",
            "captured_at >= ?",
            "captured_at <= ?",
            "latest_price IS NOT NULL",
            "latest_price > 0",
        ]
        close_params: list[Any] = [code, close_start, close_end]
        if captured_at:
            close_conditions.append("captured_at <= ?")
            close_params.append(captured_at)
        with self.connect() as conn:
            close_row = conn.execute(
                f"""
                SELECT captured_at, latest_price AS baseline_price, latest_price, previous_settle, source,
                       'us_close_anchor' AS baseline_kind
                FROM reference_snapshots
                WHERE {' AND '.join(close_conditions)}
                ORDER BY ABS((julianday(substr(captured_at, 1, 19)) - julianday(?)) * 86400),
                         captured_at DESC, id DESC
                LIMIT 1
                """,
                (*close_params, target_close.isoformat()),
            ).fetchone()
        if close_row:
            return dict(close_row)

        target_date = datetime.fromisoformat(nav_date[:10]).date() + timedelta(days=1)
        while target_date.weekday() >= 5:
            target_date += timedelta(days=1)
        conditions = [
            "code = ?",
            "substr(captured_at, 1, 10) = ?",
            "previous_settle IS NOT NULL",
            "previous_settle > 0",
        ]
        params: list[Any] = [code, target_date.isoformat()]
        if captured_at:
            conditions.append("captured_at <= ?")
            params.append(captured_at)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT captured_at, previous_settle AS baseline_price, previous_settle, source,
                       'previous_settle_fallback' AS baseline_kind
                FROM reference_snapshots
                WHERE {' AND '.join(conditions)}
                ORDER BY captured_at ASC, id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return dict(row) if row else None

    def reference_history(self, codes: list[str], limit: int = 80) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
        with self.connect() as conn:
            for code in codes:
                rows = conn.execute(
                    """
                    SELECT captured_at, latest_price, change_rate, previous_settle
                    FROM reference_snapshots WHERE code = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (code, limit),
                ).fetchall()
                result[code] = [dict(row) for row in reversed(rows)]
        return result

    def previous_reference_snapshot(self, code: str, captured_at: str) -> dict[str, Any] | None:
        current_date = str(captured_at)[:10]
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT captured_at, latest_price, change_rate, previous_settle, source
                FROM reference_snapshots
                WHERE code = ? AND substr(captured_at, 1, 10) < ?
                ORDER BY captured_at DESC, id DESC LIMIT 1
                """,
                (code, current_date),
            ).fetchone()
        return dict(row) if row else None

    def cn_close_reference_snapshot(self, code: str, captured_at: str) -> dict[str, Any] | None:
        current = _parse_iso_datetime(captured_at)
        if current is None:
            return self.previous_reference_snapshot(code, captured_at)
        if current.date().weekday() < 5 and current.time() >= time(15, 0):
            target_date = current.date()
        else:
            target_date = _previous_weekday(current.date())
        target_cutoff = datetime.combine(target_date, time(15, 0), tzinfo=current.tzinfo).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT captured_at, latest_price, change_rate, previous_settle, source
                FROM reference_snapshots
                WHERE code = ? AND captured_at <= ?
                ORDER BY captured_at DESC, id DESC LIMIT 1000
                """,
                (code, target_cutoff),
            ).fetchall()
        for row in rows:
            row_dt = _parse_iso_datetime(row["captured_at"])
            if row_dt is not None and row_dt.date().weekday() < 5:
                return dict(row)
        return None

    def insert_premarket_anchors(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        for value in values:
            value.setdefault("rate_change_bps", None)
            value.setdefault("rate_impact_level", None)
            value.setdefault("note", None)
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO premarket_anchor_snapshots
                    (group_id, group_name, reference_code, reference_name, captured_at,
                     baseline_captured_at, reference_price, baseline_price,
                     expected_change_rate, futures_change_rate, overnight_change_rate,
                     rate_change_bps, rate_impact_level, note, source)
                VALUES
                    (:group_id, :group_name, :reference_code, :reference_name, :captured_at,
                     :baseline_captured_at, :reference_price, :baseline_price,
                     :expected_change_rate, :futures_change_rate, :overnight_change_rate,
                     :rate_change_bps, :rate_impact_level, :note, :source)
                ON CONFLICT(group_id, captured_at) DO UPDATE SET
                    group_name=excluded.group_name,
                    reference_code=excluded.reference_code,
                    reference_name=excluded.reference_name,
                    baseline_captured_at=excluded.baseline_captured_at,
                    reference_price=excluded.reference_price,
                    baseline_price=excluded.baseline_price,
                    expected_change_rate=excluded.expected_change_rate,
                    futures_change_rate=excluded.futures_change_rate,
                    overnight_change_rate=excluded.overnight_change_rate,
                    rate_change_bps=excluded.rate_change_bps,
                    rate_impact_level=excluded.rate_impact_level,
                    note=excluded.note,
                    source=excluded.source
                """,
                values,
            )
            return conn.total_changes - before

    def latest_premarket_anchors(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*
                FROM premarket_anchor_snapshots a
                JOIN (
                    SELECT group_id, MAX(id) AS id FROM premarket_anchor_snapshots GROUP BY group_id
                ) latest ON a.id = latest.id
                """
            ).fetchall()
        return {row["group_id"]: dict(row) for row in rows}

    def premarket_anchor_history(self, group_ids: list[str], limit: int = 240) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {group_id: [] for group_id in group_ids}
        with self.connect() as conn:
            for group_id in group_ids:
                rows = conn.execute(
                    """
                    SELECT group_id, group_name, reference_code, reference_name, captured_at,
                           baseline_captured_at, reference_price, baseline_price,
                           expected_change_rate, futures_change_rate, overnight_change_rate,
                           rate_change_bps, rate_impact_level, note, source
                    FROM premarket_anchor_snapshots
                    WHERE group_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (group_id, limit),
                ).fetchall()
                result[group_id] = [dict(row) for row in reversed(rows)]
        return result

    def premium_history(self, codes: list[str], limit: int = 80) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
        with self.connect() as conn:
            for code in codes:
                rows = conn.execute(
                    """
                    SELECT captured_at, premium_rate, latest_price, change_rate, iopv
                    FROM quote_snapshots WHERE code = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (code, limit),
                ).fetchall()
                result[code] = [dict(row) for row in reversed(rows)]
        return result

    def replace_daily_premiums(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO daily_premium_history
                    (code, trade_date, close_price, nav, premium_rate, source, fetched_at)
                VALUES (:code, :trade_date, :close_price, :nav, :premium_rate, :source, :fetched_at)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                    close_price=excluded.close_price,
                    nav=excluded.nav,
                    premium_rate=excluded.premium_rate,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                values,
            )
            return conn.total_changes - before

    def daily_premium_history(self, codes: list[str], limit: int = 760) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
        with self.connect() as conn:
            for code in codes:
                rows = conn.execute(
                    """
                    SELECT trade_date, close_price, nav, premium_rate
                    FROM daily_premium_history WHERE code = ?
                    ORDER BY trade_date DESC LIMIT ?
                    """,
                    (code, limit),
                ).fetchall()
                result[code] = [dict(row) for row in reversed(rows)]
        return result

    def latest_daily_premiums(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        if not codes:
            return {}
        result: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            for code in codes:
                row = conn.execute(
                    """
                    SELECT code, trade_date, close_price, nav, premium_rate
                    FROM daily_premium_history
                    WHERE code = ?
                    ORDER BY trade_date DESC LIMIT 1
                    """,
                    (code,),
                ).fetchone()
                if row:
                    result[code] = dict(row)
        return result

    def daily_premium_means(
        self,
        codes: list[str],
        before_date: str | None,
        window: int = 20,
    ) -> dict[str, float]:
        if not codes or not before_date:
            return {}
        result: dict[str, float] = {}
        with self.connect() as conn:
            for code in codes:
                rows = conn.execute(
                    """
                    SELECT premium_rate
                    FROM daily_premium_history
                    WHERE code = ? AND trade_date < ? AND premium_rate IS NOT NULL
                    ORDER BY trade_date DESC LIMIT ?
                    """,
                    (code, before_date, window),
                ).fetchall()
                values = [float(row["premium_rate"]) for row in rows]
                if values:
                    result[code] = sum(values) / len(values)
        return result

    def latest_daily_premium_dates(self, codes: list[str]) -> dict[str, str]:
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT code, MAX(trade_date) AS trade_date
                FROM daily_premium_history
                WHERE code IN ({placeholders})
                GROUP BY code
                """,
                codes,
            ).fetchall()
        return {row["code"]: row["trade_date"] for row in rows if row["trade_date"]}

    def daily_premium_date_bounds(self, codes: list[str]) -> dict[str, dict[str, str]]:
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT code, MIN(trade_date) AS earliest, MAX(trade_date) AS latest
                FROM daily_premium_history
                WHERE code IN ({placeholders})
                GROUP BY code
                """,
                codes,
            ).fetchall()
        return {
            row["code"]: {"earliest": row["earliest"], "latest": row["latest"]}
            for row in rows
            if row["earliest"] and row["latest"]
        }

    def insert_notices(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO notices
                    (source, code, exchange, title, notice_type, published_at, url, fetched_at)
                VALUES (:source, :code, :exchange, :title, :notice_type,
                        :published_at, :url, :fetched_at)
                """,
                values,
            )
            return conn.total_changes - before

    def notices(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM notices
                ORDER BY COALESCE(NULLIF(published_at, ''), fetched_at) DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_notice_by_code(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM (
                    SELECT n.*, ROW_NUMBER() OVER (
                        PARTITION BY code
                        ORDER BY COALESCE(NULLIF(published_at, ''), fetched_at) DESC, id DESC
                    ) AS row_number
                    FROM notices n
                )
                WHERE row_number = 1
                """
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item.pop("row_number", None)
            result[item["code"]] = item
        return result

    def insert_purchase_statuses(self, rows: Iterable[dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO purchase_status_snapshots
                    (code, purchase_status, daily_limit, next_open_date, source, captured_at)
                VALUES (:code, :purchase_status, :daily_limit, :next_open_date, :source, :captured_at)
                """,
                values,
            )
            return conn.total_changes - before

    def latest_purchase_statuses(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*
                FROM purchase_status_snapshots p
                JOIN (
                    SELECT code, MAX(id) AS id
                    FROM purchase_status_snapshots
                    GROUP BY code
                ) latest ON p.id = latest.id
                """
            ).fetchall()
        return {row["code"]: dict(row) for row in rows}

    def latest_quota_document(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM quota_documents ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        item = dict(row)
        item["items"] = json.loads(item.pop("items_json"))
        item["new_items"] = json.loads(item.pop("new_items_json"))
        return item

    def insert_quota_document(self, record: dict[str, Any]) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO quota_documents
                    (title, url, document_hash, fetched_at, items_json, new_items_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["title"],
                    record["url"],
                    record["document_hash"],
                    record["fetched_at"],
                    json.dumps(record["items"], ensure_ascii=False),
                    json.dumps(record["new_items"], ensure_ascii=False),
                ),
            )
            return cursor.rowcount > 0

    def replace_holdings(self, rows: Iterable[dict[str, Any]], source_file: str) -> int:
        values = list(rows)
        imported_at = now_iso()
        with self.connect() as conn:
            conn.execute("DELETE FROM holdings")
            conn.executemany(
                """
                INSERT INTO holdings(code, name, shares, average_cost, imported_at, source_file)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["code"],
                        row.get("name"),
                        row.get("shares"),
                        row.get("average_cost"),
                        imported_at,
                        source_file,
                    )
                    for row in values
                ],
            )
        return len(values)

    def holdings(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM holdings ORDER BY code").fetchall()
        return {row["code"]: dict(row) for row in rows}


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _previous_weekday(value: Any) -> Any:
    current = value - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current
