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
