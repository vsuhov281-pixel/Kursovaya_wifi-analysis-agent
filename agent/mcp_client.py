"""Минимальный MCP-клиент поверх SSE-транспорта (только stdlib).

Протокол: открываем GET /sse, сервер присылает event: endpoint с URL для
POST сообщений; дальше классический JSON-RPC 2.0:
initialize → notifications/initialized → tools/call.

Сессия живёт одним объектом; на один цикл агента — одно подключение.
"""
import json
import urllib.request
import uuid


class McpError(Exception):
    pass


class SseMcpClient:
    def __init__(self, base_url: str, timeout: int = 60):
        # "http://host:8100/sse" -> base "http://host:8100"
        self.base_url = base_url.rstrip("/").removesuffix("/sse")
        self.timeout = timeout
        self._resp = None
        self._messages_url = None
        self._buf = b""

    # --- жизненный цикл ----------------------------------------------------
    def connect(self) -> "SseMcpClient":
        req = urllib.request.Request(f"{self.base_url}/sse")
        self._resp = urllib.request.urlopen(req, timeout=self.timeout)
        endpoint = None
        while endpoint is None:
            chunk = self._resp.read1(4096)
            if not chunk:
                raise McpError("SSE stream closed before endpoint event")
            self._buf += chunk
            for part in self._buf.split(b"\n\n"):
                if part.startswith(b"event: endpoint"):
                    endpoint = part.split(b"data:", 1)[1].strip().decode()
        self._messages_url = self.base_url + endpoint
        self._buf = b""
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "netagent", "version": "0.1"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def close(self) -> None:
        if self._resp:
            self._resp.close()
            self._resp = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # --- транспорт ----------------------------------------------------------
    def _post(self, payload: dict) -> None:
        req = urllib.request.Request(
            self._messages_url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=self.timeout).read()

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        rid = str(uuid.uuid4())
        self._post({"jsonrpc": "2.0", "id": rid,
                    "method": method, "params": params or {}})
        pending = self._buf
        while True:
            for part in pending.split(b"\n\n"):
                if b"data:" not in part:
                    continue
                try:
                    data = json.loads(part.split(b"data:", 1)[1].strip())
                except json.JSONDecodeError:
                    continue
                if data.get("id") == rid:
                    return data
            chunk = self._resp.read1(65536)
            if not chunk:
                raise McpError("SSE stream closed while awaiting response")
            pending += chunk

    # --- публичный API --------------------------------------------------------
    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list")["result"]["tools"]

    def call_tool(self, name: str, arguments: dict | None = None) -> list[str]:
        """Вызвать tool, вернуть список текстовых блоков ответа."""
        r = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in r:
            raise McpError(f"{name}: {r['error']}")
        return [c.get("text", "") for c in r["result"].get("content", [])]


# псевдоним для удобства аннотаций типов
McpClient = SseMcpClient
