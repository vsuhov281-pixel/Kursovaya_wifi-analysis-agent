"""SQLite-хранилище результатов агента.

Два справочника:
  runs    — история опросов (каждый цикл агента = одна запись);
  devices — накопленное знание об устройствах (upsert по MAC).
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total INTEGER DEFAULT 0,
    summary TEXT DEFAULT '{}'          -- JSON: счётчики по статусам
);
CREATE TABLE IF NOT EXISTS devices (
    mac TEXT PRIMARY KEY,
    ip TEXT,
    hostname TEXT,
    vendor TEXT,
    category TEXT,
    status TEXT,                       -- active | unknown_device | stale_lease
    first_seen TEXT,
    last_seen TEXT,
    seen_runs INTEGER DEFAULT 1
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- запись -----------------------------------------------------------
    def record_run(self, findings: list[dict]) -> int:
        """Сохранить результаты одного цикла агента. Возвращает id опроса."""
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (utcnow(),)
        )
        run_id = cur.lastrowid
        for f in findings:
            self.conn.execute(
                """INSERT INTO devices (mac, ip, hostname, vendor, category, status,
                                        first_seen, last_seen, seen_runs)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(mac) DO UPDATE SET
                       ip=excluded.ip, hostname=excluded.hostname,
                       vendor=excluded.vendor, category=excluded.category,
                       status=excluded.status, last_seen=excluded.last_seen,
                       seen_runs=seen_runs + 1""",
                (f.get("mac", ""), f.get("ip", ""), f.get("hostname", ""),
                 f.get("vendor", ""), f.get("category", ""), f.get("status", ""),
                 utcnow(), utcnow()),
            )
        counts: dict[str, int] = {}
        for f in findings:
            counts[f["status"]] = counts.get(f["status"], 0) + 1
        self.conn.execute(
            "UPDATE runs SET finished_at=?, total=?, summary=? WHERE id=?",
            (utcnow(), len(findings), json.dumps(counts, ensure_ascii=False), run_id),
        )
        self.conn.commit()
        return run_id

    # --- чтение (для веб-API) ---------------------------------------------
    def stats(self) -> dict:
        by_status = {
            r["status"]: r["n"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) n FROM devices GROUP BY status")
        }
        timeline = [
            {"run_id": r["id"], "started_at": r["started_at"],
             "total": r["total"], **json.loads(r["summary"])}
            for r in self.conn.execute(
                "SELECT id, started_at, total, summary FROM runs ORDER BY id DESC LIMIT 30")
        ]
        timeline.reverse()
        totals = {
            "devices": self.conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
            "runs": self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        }
        return {"totals": totals, "by_status": by_status, "timeline": timeline}

    def devices(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM devices"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY status, ip"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, started_at, finished_at, total, summary FROM runs ORDER BY id DESC LIMIT 50")]
