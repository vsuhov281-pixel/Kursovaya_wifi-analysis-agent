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
CREATE TABLE IF NOT EXISTS wifi_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    ap_count INTEGER DEFAULT 0,
    client_count INTEGER DEFAULT 0,
    wlan_count INTEGER DEFAULT 0,
    report TEXT DEFAULT '',
    error TEXT
);

CREATE TABLE IF NOT EXISTS wifi_client_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    mac TEXT NOT NULL,
    hostname TEXT,
    ip TEXT,
    ssid TEXT,
    signal_dbm INTEGER,
    tx_rate INTEGER,
    rx_rate INTEGER,
    channel INTEGER,
    radio TEXT,
    UNIQUE (collected_at, mac)
);

CREATE TABLE IF NOT EXISTS wifi_radio_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    ap_mac TEXT NOT NULL,
    ap_name TEXT,
    radio TEXT,
    channel INTEGER,
    channel_utilization INTEGER,
    client_count INTEGER,
    satisfaction INTEGER,
    tx_retries INTEGER,
    tx_packets INTEGER,
    UNIQUE (collected_at, ap_mac, radio)
);

CREATE TABLE IF NOT EXISTS wifi_events (
    event_id TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    event_time TEXT,
    event_type TEXT,
    client_mac TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS wifi_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_key TEXT NOT NULL UNIQUE,
    mac TEXT NOT NULL,
    hostname TEXT,
    ip TEXT,
    ap_name TEXT,
    anomaly_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    cause TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    resolved_at TEXT,
    last_signal_dbm REAL,
    last_retries REAL,
    last_satisfaction REAL
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

        # Безопасная миграция: старые записи сохраняются.
        columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(wifi_client_samples)"
            )
        }

        new_columns = {
            "ap_name": "TEXT",
            "ap_mac": "TEXT",
            "wifi_tx_retries_percentage": "REAL",
            "satisfaction": "INTEGER",
            "client_status": "TEXT",
        }

        for column_name, column_type in new_columns.items():
            if column_name not in columns:
                self.conn.execute(
                    f"ALTER TABLE wifi_client_samples "
                    f"ADD COLUMN {column_name} {column_type}"
                )

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

    def record_wifi_snapshot(self, data: dict) -> int:
        """Сохранить снимок UniFi, включая ошибку сбора при её наличии."""
        cur = self.conn.execute(
            """INSERT INTO wifi_snapshots
               (collected_at, status, ap_count, client_count,
                wlan_count, report, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data["collected_at"], data["status"],
                data.get("ap_count", 0), data.get("client_count", 0),
                data.get("wlan_count", 0), data.get("report", ""),
                data.get("error"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def wifi_snapshots(self, limit: int = 100) -> list[dict]:
        """Последние снимки UniFi, сначала самые новые."""
        rows = self.conn.execute(
            """SELECT id, collected_at, status, ap_count, client_count,
                      wlan_count, error
               FROM wifi_snapshots ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in rows]
    
    def record_wifi_client_samples(
        self, collected_at: str, clients: list[dict]
    ) -> int:
        """Сохранить измерения Wi-Fi-клиентов и подробности их подключения."""
        rows = [
            (
                collected_at,
                c.get("mac"),
                c.get("hostname"),
                c.get("ip"),
                c.get("essid") or c.get("ssid"),
                (
                    c.get("signal_dbm")
                    if c.get("signal_dbm") is not None
                    else c.get("signal")
                ),
                c.get("tx_rate"),
                c.get("rx_rate"),
                c.get("channel"),
                c.get("radio"),
                c.get("ap_name") or c.get("last_uplink_name"),
                c.get("ap_mac") or c.get("last_uplink_mac"),
                c.get("wifi_tx_retries_percentage"),
                c.get("satisfaction"),
                c.get("status"),
            )
            for c in clients
            if c.get("mac")
        ]

        self.conn.executemany(
            """INSERT OR IGNORE INTO wifi_client_samples
               (
                   collected_at, mac, hostname, ip, ssid, signal_dbm,
                   tx_rate, rx_rate, channel, radio, ap_name, ap_mac,
                   wifi_tx_retries_percentage, satisfaction, client_status
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def wifi_clients(self, limit: int = 500) -> list[dict]:
        """Вернуть последние измерения Wi-Fi-клиентов."""
        return [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT
                    collected_at,
                    mac,
                    hostname,
                    ip,
                    ssid,
                    ap_name,
                    ap_mac,
                    signal_dbm,
                    channel,
                    radio,
                    tx_rate,
                    rx_rate,
                    wifi_tx_retries_percentage,
                    satisfaction,
                    client_status
                FROM wifi_client_samples
                WHERE collected_at = (
                    SELECT MAX(collected_at)
                    FROM wifi_client_samples
                )
                ORDER BY
                    CASE WHEN signal_dbm IS NULL THEN 1 ELSE 0 END,
                    signal_dbm ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
    def update_wifi_anomaly_history(
        self, collected_at: str, clients: list[dict]
    ) -> int:
        """Обновить историю аномалий по успешному снимку UniFi."""
        current_keys = set()
        saved = 0

        def number(client, field):
            value = client.get(field)
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        rules = (
            (
                "weak_signal", "high", "слабый сигнал",
                "Проверить расположение клиента, препятствия и покрытие ближайших AP",
                lambda value: value is not None and value <= -75,
                "signal_dbm",
            ),
            (
                "high_retries", "high", "высокие повторные передачи",
                "Проверить загрузку канала, помехи и статистику радио на AP",
                lambda value: value is not None and value >= 30,
                "wifi_tx_retries_percentage",
            ),
            (
                "high_retries", "medium", "повышенные повторные передачи",
                "Сравнить повторные измерения и проверить радиоканал",
                lambda value: value is not None and 15 <= value < 30,
                "wifi_tx_retries_percentage",
            ),
            (
                "low_satisfaction", "high", "низкий Satisfaction",
                "Изучить качество соединения, потери и события клиента",
                lambda value: value is not None and value <= 70,
                "satisfaction",
            ),
            (
                "low_satisfaction", "medium", "пониженный Satisfaction",
                "Наблюдать за качеством соединения в следующих снимках",
                lambda value: value is not None and 70 < value <= 85,
                "satisfaction",
            ),
        )

        for client in clients:
            mac = (client.get("mac") or "").strip().lower()
            if not mac:
                continue

            signal = number(client, "signal_dbm")
            retries = number(client, "wifi_tx_retries_percentage")
            satisfaction = number(client, "satisfaction")

            values = {
                "signal_dbm": signal,
                "wifi_tx_retries_percentage": retries,
                "satisfaction": satisfaction,
            }

            for anomaly_type, severity, label, recommendation, condition, field in rules:
                if not condition(values[field]):
                    continue

                key = f"{mac}:{anomaly_type}"
                current_keys.add(key)
                measured = values[field]
                cause = f"{label} ({measured:g})"

                self.conn.execute(
                    """INSERT INTO wifi_anomalies (
                           anomaly_key, mac, hostname, ip, ap_name, anomaly_type,
                           severity, cause, recommendation, first_seen, last_seen,
                           occurrences, status, resolved_at, last_signal_dbm,
                           last_retries, last_satisfaction
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active',
                                 NULL, ?, ?, ?)
                       ON CONFLICT(anomaly_key) DO UPDATE SET
                           hostname=excluded.hostname,
                           ip=excluded.ip,
                           ap_name=excluded.ap_name,
                           severity=excluded.severity,
                           cause=excluded.cause,
                           recommendation=excluded.recommendation,
                           last_seen=excluded.last_seen,
                           occurrences=wifi_anomalies.occurrences + 1,
                           status='active',
                           resolved_at=NULL,
                           last_signal_dbm=excluded.last_signal_dbm,
                           last_retries=excluded.last_retries,
                           last_satisfaction=excluded.last_satisfaction""",
                    (
                        key, mac, client.get("hostname"), client.get("ip"),
                        client.get("ap_name") or client.get("last_uplink_name"),
                        anomaly_type, severity, cause, recommendation,
                        collected_at, collected_at, signal, retries, satisfaction,
                    ),
                )
                saved += 1

        # Не закрываем инциденты при пустом снимке: он может быть неполным.
        if clients:
            active = self.conn.execute(
                "SELECT anomaly_key FROM wifi_anomalies WHERE status='active'"
            ).fetchall()
            for row in active:
                if row["anomaly_key"] not in current_keys:
                    self.conn.execute(
                        """UPDATE wifi_anomalies
                           SET status='resolved', resolved_at=?
                           WHERE anomaly_key=? AND status='active'""",
                        (collected_at, row["anomaly_key"]),
                    )

        self.conn.commit()
        return saved

    def wifi_anomaly_history(self, limit: int = 200) -> list[dict]:
        """История инцидентов, сначала недавно обновлённые."""
        rows = self.conn.execute(
            """SELECT id, anomaly_key, mac, hostname, ip, ap_name,
                      anomaly_type, severity, cause, recommendation,
                      first_seen, last_seen, occurrences, status, resolved_at,
                      last_signal_dbm, last_retries, last_satisfaction
               FROM wifi_anomalies
               ORDER BY last_seen DESC, id DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in rows]

    def record_wifi_radio_samples(self, collected_at: str, radios: list[dict]) -> int:
        """Сохранить измерения радиоканалов точек доступа."""
        rows = [
            (
                collected_at,
                r.get("ap_mac"),
                r.get("ap_name"),
                r.get("radio"),
                r.get("current_channel", r.get("channel")),
                r.get("cu_total"),
                r.get("num_sta"),
                r.get("satisfaction"),
                r.get("tx_retries"),
                r.get("tx_packets"),
            )
            for r in radios if r.get("ap_mac") and r.get("radio")
        ]
        self.conn.executemany(
            """INSERT OR IGNORE INTO wifi_radio_samples
               (collected_at, ap_mac, ap_name, radio, channel,
                channel_utilization, client_count, satisfaction,
                tx_retries, tx_packets)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def record_wifi_events(self, events: list[dict]) -> int:
        """Сохранить новые события, не дублируя уже известные."""
        rows = [
            (
                e.get("id"),
                e.get("collected_at"),
                e.get("event_time"),
                e.get("key"),
                e.get("mac"),
                e.get("msg"),
            )
            for e in events if e.get("id")
        ]
        before = self.conn.total_changes
        self.conn.executemany(
            """INSERT OR IGNORE INTO wifi_events
               (event_id, collected_at, event_time, event_type, client_mac, message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    # --- чтение (для веб-API) ---------------------------------------------

    def wifi_client_history(
        self, mac: str, limit: int = 100
    ) -> list[dict]:
        """История измерений конкретного Wi-Fi-клиента."""
        rows = self.conn.execute(
            """
            SELECT collected_at, mac, hostname, ip, ssid,
                   ap_name, ap_mac, signal_dbm, channel, radio,
                   tx_rate, rx_rate, wifi_tx_retries_percentage,
                   satisfaction, client_status
            FROM (
                SELECT *
                FROM wifi_client_samples
                WHERE lower(mac) = lower(?)
                ORDER BY collected_at DESC
                LIMIT ?
            )
            ORDER BY collected_at ASC
            """,
            (mac, max(1, min(int(limit), 1000))),
        )
        return [dict(row) for row in rows]

    def wifi_ap_history(self, limit: int = 100) -> list[dict]:
        """Число клиентов на каждой AP по времени."""
        rows = self.conn.execute(
            """
            WITH recent_times AS (
                SELECT DISTINCT collected_at
                FROM wifi_client_samples
                ORDER BY collected_at DESC
                LIMIT ?
            )
            SELECT
                s.collected_at,
                COALESCE(
                    NULLIF(s.ap_name, ''),
                    NULLIF(s.ap_mac, ''),
                    'Неизвестная AP'
                ) AS ap_name,
                s.ap_mac,
                COUNT(DISTINCT s.mac) AS client_count
            FROM wifi_client_samples AS s
            JOIN recent_times AS r
              ON r.collected_at = s.collected_at
            GROUP BY s.collected_at, s.ap_name, s.ap_mac
            ORDER BY s.collected_at ASC, ap_name ASC
            """,
            (max(1, min(int(limit), 1000)),),
        )
        return [dict(row) for row in rows]

    def wifi_stale_clients(self, hours: int = 24) -> list[dict]:
        """
        Клиенты, давно не наблюдавшиеся в успешных снимках.
        Это не доказательство неисправности устройства.
        """
        hours = max(1, min(int(hours), 8760))

        rows = self.conn.execute(
            """
            WITH latest_success AS (
                SELECT MAX(collected_at) AS collected_at
                FROM wifi_snapshots
                WHERE status = 'ok'
            ),
            last_seen AS (
                SELECT
                    mac,
                    MAX(collected_at) AS last_seen,
                    MAX(hostname) AS hostname,
                    MAX(ip) AS ip,
                    MAX(ap_name) AS last_ap,
                    MAX(ssid) AS last_ssid
                FROM wifi_client_samples
                GROUP BY mac
            )
            SELECT
                l.mac,
                l.hostname,
                l.ip,
                l.last_ap,
                l.last_ssid,
                l.last_seen,
                ROUND(
                    (julianday('now') - julianday(l.last_seen)) * 24,
                    1
                ) AS hours_since_seen
            FROM last_seen AS l
            CROSS JOIN latest_success AS s
            WHERE s.collected_at IS NOT NULL
              AND l.last_seen < s.collected_at
              AND julianday(l.last_seen)
                    <= julianday('now', ?)
            ORDER BY l.last_seen ASC
            LIMIT 500
            """,
            (f"-{hours} hours",),
        )
        return [dict(row) for row in rows]

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
