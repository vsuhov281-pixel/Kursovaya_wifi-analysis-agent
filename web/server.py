from datetime import datetime, timezone
"""Веб-сервер и планировщик — всё в одном процессе (только stdlib).

  * HTTP-сервер на http.server: раздаёт дашборд и JSON API;
  * фоновый поток по расписанию (SCAN_INTERVAL_MINUTES) гоняет агента.

Зачем stdlib вместо FastAPI: ноль зависимостей → Docker-образ на
python:3.12-slim без pip, проект запускается где угодно без установки.
"""
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from agent import config
from agent.agent import Agent
from agent.db import DB
from agent.planner import build_llm
import re

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

db = DB(config.DB_PATH)
agent = Agent(db)


# ---------------------------------------------------------------------------
# Планировщик опросов (фоновый поток)
# ---------------------------------------------------------------------------

def scheduler_loop(interval_minutes: int) -> None:
    while True:
        try:
            agent.run()
        except Exception as exc:  # агент не должен убить сервер
            print(f"[scheduler] ошибка цикла: {exc}")
        time.sleep(interval_minutes * 60)




def build_wifi_anomalies(clients: list[dict]) -> list[dict]:
    """Найти проблемы по последнему снимку клиентов UniFi."""
    anomalies = []

    for client in clients:
        causes = []
        recommendations = []
        severity = "info"

        def number(field):
            value = client.get(field)
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        signal = number("signal_dbm")
        retries = number("wifi_tx_retries_percentage")
        satisfaction = number("satisfaction")

        if signal is not None:
            if signal <= -75:
                severity = "high"
                causes.append(f"слабый сигнал ({signal:g} dBm)")
                recommendations.append(
                    "Проверить расположение клиента, препятствия и покрытие ближайших AP"
                )
            elif signal <= -68:
                if severity != "high":
                    severity = "medium"
                causes.append(f"сигнал ниже желательного ({signal:g} dBm)")
                recommendations.append(
                    "Наблюдать за сигналом; проверить покрытие и расположение клиента"
                )

        if retries is not None:
            if retries >= 30:
                severity = "high"
                causes.append(f"высокие повторные передачи ({retries:g}%)")
                recommendations.append(
                    "Проверить загрузку канала, помехи и статистику радио на AP"
                )
            elif retries >= 15:
                if severity != "high":
                    severity = "medium"
                causes.append(f"повышенные повторные передачи ({retries:g}%)")
                recommendations.append(
                    "Сравнить повторные измерения и проверить радиоканал"
                )

        if satisfaction is not None:
            if satisfaction <= 70:
                severity = "high"
                causes.append(f"низкий Satisfaction ({satisfaction:g})")
                recommendations.append(
                    "Изучить качество соединения, потери и события клиента"
                )
            elif satisfaction <= 85:
                if severity != "high":
                    severity = "medium"
                causes.append(f"пониженный Satisfaction ({satisfaction:g})")
                recommendations.append(
                    "Наблюдать за качеством соединения в следующих снимках"
                )

        if not causes:
            continue

        anomalies.append({
            "severity": severity,
            "hostname": client.get("hostname") or "Без имени",
            "ip": client.get("ip"),
            "mac": client.get("mac"),
            "ap_name": client.get("ap_name"),
            "signal_dbm": signal,
            "retries": retries,
            "satisfaction": satisfaction,
            "causes": causes,
            "recommendations": list(dict.fromkeys(recommendations)),
            "collected_at": client.get("collected_at"),
        })

    order = {"high": 0, "medium": 1, "info": 2}
    anomalies.sort(key=lambda item: (
        order.get(item["severity"], 3),
        item.get("hostname") or ""
    ))
    return anomalies


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "NetAgent/0.1"

    # --- helpers -----------------------------------------------------------
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        file_path = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not file_path.startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(file_path):
            self._json({"error": "not found"}, 404)
            return
        body = open(file_path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(file_path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # тихий лог
        pass

    # --- routes -------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        route, query = url.path, parse_qs(url.query)
        if route == "/api/health":
            self._json({"ok": True, "time": time.time()})
        elif route == "/api/stats":
            self._json(db.stats())
        elif route == "/api/devices":
            status = query.get("status", [None])[0]
            self._json({"devices": db.devices(status)})
        elif route == "/api/runs":
            self._json({"runs": db.runs()})
        elif route == "/api/wifi/report":
            clients = db.wifi_clients(500)
            snapshots = db.wifi_snapshots(100)
            anomalies = build_wifi_anomalies(clients)
            stale = db.wifi_stale_clients(24)

            def numeric(value):
                try:
                    return float(value) if value is not None else None
                except (TypeError, ValueError):
                    return None

            def average(values):
                values = [v for v in values if v is not None]
                return round(sum(values) / len(values), 1) if values else None

            signal_values = [numeric(c.get("signal_dbm")) for c in clients]
            retry_values = [
                numeric(c.get("wifi_tx_retries_percentage")) for c in clients
            ]
            satisfaction_values = [
                numeric(c.get("satisfaction")) for c in clients
            ]

            ap_counts = {}
            for c in clients:
                ap = c.get("ap_name") or c.get("ap_mac") or "Неизвестная AP"
                ap_counts[ap] = ap_counts.get(ap, 0) + 1

            radios_cursor = db.conn.execute("""
                SELECT collected_at, ap_name, ap_mac, radio, channel,
                       channel_utilization, client_count, satisfaction,
                       tx_retries
                FROM wifi_radio_samples
                WHERE collected_at = (
                    SELECT MAX(collected_at) FROM wifi_radio_samples
                )
                ORDER BY ap_name, radio
            """)
            radios = [dict(row) for row in radios_cursor.fetchall()]

            roaming_cursor = db.conn.execute("""
                SELECT COUNT(*) AS total
                FROM wifi_events
                WHERE event_type IN ('EVT_WU_Roam', 'EVT_WU_RoamRadio')
            """)
            roaming_total = roaming_cursor.fetchone()["total"]

            latest = snapshots[0] if snapshots else None
            report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "latest_snapshot": latest,
                "summary": {
                    "clients": len(clients),
                    "access_points": len(ap_counts),
                    "anomalies": len(anomalies),
                    "high_anomalies": sum(
                        1 for a in anomalies if a.get("severity") == "high"
                    ),
                    "stale_clients_24h": len(stale),
                    "roaming_events_total": roaming_total,
                    "average_signal_dbm": average(signal_values),
                    "average_retries_percent": average(retry_values),
                    "average_satisfaction": average(satisfaction_values),
                },
                "access_points": [
                    {"name": name, "clients": count}
                    for name, count in sorted(
                        ap_counts.items(), key=lambda item: item[1], reverse=True
                    )
                ],
                "radios": radios,
                "anomalies": anomalies,
                "stale_clients": stale[:100],
                "recommendations": [],
            }

            recommendations = report["recommendations"]
            weak_signal = sum(
                1 for value in signal_values
                if value is not None and value <= -75
            )
            high_retries = sum(
                1 for value in retry_values
                if value is not None and value >= 30
            )
            low_satisfaction = sum(
                1 for value in satisfaction_values
                if value is not None and value <= 70
            )

            if weak_signal:
                recommendations.append(
                    f"Проверить покрытие: {weak_signal} клиент(ов) имеют "
                    "сигнал -75 dBm или хуже. Сопоставить их расположение "
                    "с точками доступа."
                )
            if high_retries:
                recommendations.append(
                    f"Проверить помехи и качество радиоканала: у "
                    f"{high_retries} клиент(ов) повторы передачи составляют "
                    "30% или больше."
                )
            if low_satisfaction:
                recommendations.append(
                    f"Проверить качество обслуживания: у {low_satisfaction} "
                    "клиент(ов) Satisfaction не выше 70."
                )
                        # Конкретные устройства, требующие внимания
            weak_clients = [
                item for item in anomalies
                if item.get("signal_dbm") is not None
                and item["signal_dbm"] <= -75
            ]

            retry_clients = [
                item for item in anomalies
                if item.get("retries") is not None
                and item["retries"] >= 30
            ]

            def describe_clients(items):
                result = []
                for item in items[:5]:
                    name = item.get("hostname") or "Без имени"
                    ip = item.get("ip") or "IP неизвестен"
                    ap = item.get("ap_name") or "AP неизвестна"
                    result.append(
                        f"{name} (IP: {ip}, AP: {ap})"
                    )
                if len(items) > 5:
                    result.append(
                        f"и ещё {len(items) - 5} устройств"
                    )
                return "; ".join(result)

            if weak_clients:
                recommendations.append(
                    "Устройства со слабым сигналом (≤ -75 dBm): "
                    + describe_clients(weak_clients)
                    + ". Проверить расположение и покрытие."
                )

            if retry_clients:
                recommendations.append(
                    "Устройства с повторными передачами ≥ 30%: "
                    + describe_clients(retry_clients)
                    + ". Проверить радиоканал и повторить измерения."
                )
            if radios:
                utilization = [
                    numeric(r.get("channel_utilization")) for r in radios
                ]
                busy = [
                    r for r in radios
                    if numeric(r.get("channel_utilization")) is not None
                    and numeric(r.get("channel_utilization")) >= 70
                ]
                if busy:
                    recommendations.append(
                        f"Проверить загруженность радиоканалов: "
                        f"{len(busy)} радиоинтерфейс(ов) показывают "
                        "утилизацию канала не ниже 70%."
                    )
                else:
                    recommendations.append(
                        "Данные радиоинтерфейсов доступны. Сравните каналы "
                        "и их загрузку перед изменением настроек."
                    )
            else:
                recommendations.append(
                    "Для рекомендаций по загрузке каналов пока недостаточно "
                    "данных радиоинтерфейсов."
                )

            if stale:
                recommendations.append(
                    f"Проверить список давно не наблюдавшихся устройств: "
                    f"{len(stale)} клиент(ов) не видны более 24 часов. "
                    "Это само по себе не означает неисправность."
                )
            if not recommendations:
                recommendations.append(
                    "По текущему снимку явных проблем по заданным порогам "
                    "не обнаружено. Продолжайте наблюдение."
                )

            self._json(report)
        elif route == "/api/wifi":
            self._json({"snapshots": db.wifi_snapshots(100)})
        elif route == "/api/wifi/clients":
            self._json({"clients": db.wifi_clients(500)})
        elif route == "/api/wifi/history/clients":
            self._json({"clients": db.wifi_history_clients(5000)})
        elif route == "/api/wifi/roaming/clients":
            cursor = db.conn.cursor()
            cursor.execute("""
                SELECT
                    e.client_mac AS mac,
                    COUNT(*) AS event_count,
                    MAX(COALESCE(e.event_time, e.collected_at)) AS last_event,
                    (
                        SELECT s.hostname
                        FROM wifi_client_samples s
                        WHERE lower(s.mac) = lower(e.client_mac)
                          AND s.hostname IS NOT NULL
                          AND trim(s.hostname) != ''
                        ORDER BY s.collected_at DESC
                        LIMIT 1
                    ) AS hostname
                FROM wifi_events e
                WHERE e.event_type IN ('EVT_WU_Roam', 'EVT_WU_RoamRadio')
                  AND e.client_mac IS NOT NULL
                  AND trim(e.client_mac) != ''
                GROUP BY lower(e.client_mac)
                ORDER BY last_event DESC
            """)
            columns = [item[0] for item in cursor.description]
            clients = [dict(zip(columns, row)) for row in cursor.fetchall()]
            self._json({"clients": clients})

        elif route == "/api/wifi/roaming":
            mac = query.get("mac", [""])[0].strip()
            if not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac):
                self._json({"error": "укажи корректный MAC-адрес"}, 400)
                return

            cursor = db.conn.cursor()
            cursor.execute("""
                SELECT event_id, collected_at, event_time, event_type,
                       client_mac, message
                FROM wifi_events
                WHERE lower(client_mac) = lower(?)
                  AND event_type IN ('EVT_WU_Roam', 'EVT_WU_RoamRadio')
                ORDER BY COALESCE(event_time, collected_at) ASC
            """, (mac,))
            columns = [item[0] for item in cursor.description]
            events = [dict(zip(columns, row)) for row in cursor.fetchall()]

            cursor.execute("""
                SELECT ap_mac, ap_name
                FROM wifi_radio_samples
                WHERE ap_mac IS NOT NULL AND ap_name IS NOT NULL
                ORDER BY collected_at DESC
            """)
            ap_names = {}
            for ap_mac, ap_name in cursor.fetchall():
                if ap_mac and ap_name:
                    ap_names.setdefault(ap_mac.lower(), ap_name)

            parsed_events = []
            for event in events:
                message = event.get("message") or ""
                ap_macs = re.findall(r"AP\[([0-9a-fA-F:]{17})\]", message)
                channels = re.findall(r"channel\s*([0-9]+)\(([^)]+)\)", message)
                ssids = re.findall(r'\bon\s+"([^"]+)"', message)
                event_type = event.get("event_type") or ""

                source_ap = ap_macs[0] if event_type == "EVT_WU_Roam" and len(ap_macs) >= 2 else None
                target_ap = (
                    ap_macs[1] if event_type == "EVT_WU_Roam" and len(ap_macs) >= 2
                    else ap_macs[0] if event_type == "EVT_WU_RoamRadio" and ap_macs
                    else None
                )

                event.update({
                    "time": event.get("event_time") or event.get("collected_at"),
                    "source_ap_mac": source_ap,
                    "source_ap_name": ap_names.get(source_ap.lower()) if source_ap else None,
                    "target_ap_mac": target_ap,
                    "target_ap_name": ap_names.get(target_ap.lower()) if target_ap else None,
                    "channel_from": f"{channels[0][0]} ({channels[0][1]})" if channels else None,
                    "channel_to": f"{channels[1][0]} ({channels[1][1]})" if len(channels) > 1 else None,
                    "ssid": ssids[-1] if ssids else None,
                    "is_ap_transition": event_type == "EVT_WU_Roam" and len(ap_macs) >= 2,
                })
                parsed_events.append(event)

            self._json({"mac": mac, "events": parsed_events})
        elif route == "/api/wifi/analyze-client":
            mac = query.get("mac", [""])[0].strip()

            if not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac):
                self._json({"error": "укажи корректный MAC-адрес"}, 400)
                return

            history = db.wifi_client_history(mac, 20)
            if not history:
                self._json({"error": "история измерений клиента не найдена"}, 404)
                return

            llm = build_llm()
            if llm is None:
                self._json({"error": "LLM не настроена: API-ключ отсутствует"}, 503)
                return

            fields = (
                "collected_at", "hostname", "ip", "ssid", "ap_name",
                "signal_dbm", "channel", "radio", "tx_rate", "rx_rate",
                "wifi_tx_retries_percentage", "satisfaction", "client_status"
            )
            facts = [
                {key: row.get(key) for key in fields}
                for row in history
            ]

            messages = [
                {
                    "role": "system",
                    "content": (
                        "Ты аналитик Wi-Fi-сети. Анализируй только переданные "
                        "измерения. Отвечай по-русски. Не выдумывай факты и "
                        "не выдавай гипотезы за доказанные причины. Если данных "
                        "недостаточно, прямо скажи об этом. Структура ответа: "
                        "1. Краткий вывод. 2. Подтверждённые факты и динамика. "
                        "3. Возможные объяснения. 4. Практические рекомендации. "
                        "5. Что проверить при следующем измерении. "
                        "Не утверждай, что проблема решена без новых измерений."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        "Проанализируй историю Wi-Fi-клиента. "
                        "Измерения в хронологическом порядке (JSON):\n"
                        + json.dumps(facts, ensure_ascii=False, default=str)
                    )
                }
            ]

            try:
                analysis = llm.chat(messages)
            except Exception as exc:
                print(f"[llm-wifi-analysis] Ошибка: {type(exc).__name__}")
                self._json({"error": "LLM не смогла выполнить анализ"}, 502)
                return

            if not analysis.strip():
                self._json({"error": "LLM вернула пустой ответ"}, 502)
                return

            latest = facts[-1]
            self._json({
                "mac": mac,
                "hostname": latest.get("hostname"),
                "history_count": len(facts),
                "latest": latest,
                "analysis": analysis,
            })
        elif route == "/api/wifi/client-history":
            mac = query.get("mac", [""])[0].strip()
            if not mac or len(mac) > 64:
                self._json({"error": "укажи MAC в параметре mac"}, 400)
            else:
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    limit = 100
                self._json({
                    "mac": mac,
                    "history": db.wifi_client_history(mac, limit),
                })
        elif route == "/api/wifi/ap-history":
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self._json({"history": db.wifi_ap_history(limit)})
        elif route == "/api/wifi/stale-clients":
            try:
                hours = int(query.get("hours", ["24"])[0])
            except ValueError:
                hours = 24
            self._json({
                "threshold_hours": max(1, min(hours, 8760)),
                "clients": db.wifi_stale_clients(hours),
            })
        elif route == "/api/wifi/anomalies":
            self._json({"anomalies": build_wifi_anomalies(db.wifi_clients(500))})
        elif route == "/api/wifi/anomaly-history":
            self._json({"history": db.wifi_anomaly_history(200)})
        elif route == "/api/scan_now":
            threading.Thread(target=agent.run, daemon=True).start()
            self._json({"ok": True, "message": "цикл агента запущен в фоне"})
        elif route.startswith("/api/"):
            self._json({"error": "unknown api route"}, 404)
        else:
            self._static(route)

    do_HEAD = do_GET


def main() -> None:
    threading.Thread(target=scheduler_loop, args=(config.SCAN_INTERVAL_MINUTES,),
                     daemon=True).start()
    server = ThreadingHTTPServer((config.WEB_HOST, config.WEB_PORT), Handler)
    print(f"[web] дашборд: http://localhost:{config.WEB_PORT} "
          f"(опрос сети каждые {config.SCAN_INTERVAL_MINUTES} мин)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
