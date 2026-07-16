"""SQLite persistence: findings, metric samples, run log, PR ledger, kv.

Single-writer, small volumes (daily snapshots) — SQLite over the shared
Postgres on the loop VM keeps the agent decoupled from Umami's lifecycle.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import Finding, MetricSample

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    fingerprint TEXT PRIMARY KEY,
    "check" TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    url TEXT,
    source TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    key TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    sampled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS metrics_lookup ON metrics (source, metric, key, sampled_at);
CREATE TABLE IF NOT EXISTS pr_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.connect() has not been called")
        return self._db

    # -- runs ---------------------------------------------------------------

    async def record_run(self, kind: str, *, ok: bool, summary: str, started_at: str, finished_at: str) -> int:
        cur = await self.db.execute(
            "INSERT INTO runs (kind, ok, summary, started_at, finished_at) VALUES (?, ?, ?, ?, ?)",
            (kind, 1 if ok else 0, summary, started_at, finished_at),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def last_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT kind, ok, summary, started_at, finished_at FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    # -- findings -----------------------------------------------------------

    async def upsert_findings(self, findings: Sequence[Finding]) -> tuple[int, int]:
        """Insert new findings / refresh last_seen on known ones.

        Returns (new, seen). A finding that reappears after being resolved is
        re-opened (resolved_at cleared).
        """
        new = seen = 0
        now = _now()
        for f in findings:
            cur = await self.db.execute(
                "SELECT fingerprint FROM findings WHERE fingerprint = ?", (f.fingerprint,)
            )
            if await cur.fetchone() is None:
                await self.db.execute(
                    'INSERT INTO findings (fingerprint, "check", severity, message, url, source, evidence,'
                    " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f.fingerprint,
                        f.check,
                        f.severity,
                        f.message,
                        f.url,
                        f.source,
                        json.dumps(f.evidence),
                        now,
                        now,
                    ),
                )
                new += 1
            else:
                await self.db.execute(
                    "UPDATE findings SET last_seen = ?, severity = ?, message = ?, evidence = ?,"
                    " resolved_at = NULL WHERE fingerprint = ?",
                    (now, f.severity, f.message, json.dumps(f.evidence), f.fingerprint),
                )
                seen += 1
        await self.db.commit()
        return new, seen

    async def resolve_stale(self, source: str, current: set[str]) -> int:
        """Mark unresolved findings from ``source`` that are absent from the
        latest run as resolved. Returns the resolved count."""
        cur = await self.db.execute(
            "SELECT fingerprint FROM findings WHERE source = ? AND resolved_at IS NULL",
            (source,),
        )
        stale = [row["fingerprint"] for row in await cur.fetchall() if row["fingerprint"] not in current]
        now = _now()
        for fp in stale:
            await self.db.execute(
                "UPDATE findings SET resolved_at = ? WHERE fingerprint = ?", (now, fp)
            )
        await self.db.commit()
        return len(stale)

    async def active_findings(self) -> list[Finding]:
        cur = await self.db.execute(
            'SELECT fingerprint, "check", severity, message, url, source, evidence FROM findings'
            " WHERE resolved_at IS NULL"
        )
        rows = await cur.fetchall()
        return [
            Finding(
                fingerprint=row["fingerprint"],
                check=row["check"],
                severity=row["severity"],
                message=row["message"],
                url=row["url"],
                source=row["source"],
                evidence=json.loads(row["evidence"]),
            )
            for row in rows
        ]

    # -- metrics ------------------------------------------------------------

    async def add_metrics(self, samples: Sequence[MetricSample]) -> int:
        now = _now()
        for s in samples:
            await self.db.execute(
                "INSERT INTO metrics (source, metric, key, value, sampled_at) VALUES (?, ?, ?, ?, ?)",
                (s.source, s.metric, s.key, s.value, now),
            )
        await self.db.commit()
        return len(samples)

    async def latest_metric(self, source: str, metric: str, key: str = "") -> float | None:
        cur = await self.db.execute(
            "SELECT value FROM metrics WHERE source = ? AND metric = ? AND key = ?"
            " ORDER BY id DESC LIMIT 1",
            (source, metric, key),
        )
        row = await cur.fetchone()
        return float(row["value"]) if row else None

    # -- PR ledger ----------------------------------------------------------

    async def record_pr(self, branch: str, url: str) -> None:
        await self.db.execute(
            "INSERT INTO pr_ledger (branch, url, opened_at) VALUES (?, ?, ?)",
            (branch, url, _now()),
        )
        await self.db.commit()

    async def last_pr_opened_at(self) -> datetime | None:
        cur = await self.db.execute("SELECT opened_at FROM pr_ledger ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return datetime.fromisoformat(row["opened_at"]) if row else None

    # -- kv -----------------------------------------------------------------

    async def get_kv(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row else None

    async def set_kv(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()
