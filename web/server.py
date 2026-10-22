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
        elif route == "/api/wifi":
            self._json({"snapshots": db.wifi_snapshots(100)})
        elif route == "/api/wifi/clients":
            self._json({"clients": db.wifi_clients(500)})
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
