"""Реестр инструментов (tools) агента.

Идея MCP в миниатюре: каждый инструмент описан схемой (имя, описание,
параметры). Агент видит только эти описания и решает, что вызвать.
Инструменты сбора данных ходят в lab-mcp (шлюз с MikroTik) через agent/lab.py;
реализацию можно менять — сигнатуры и описания остаются, агент не меняется.
"""
from dataclasses import dataclass
from typing import Callable

from . import lab


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema параметров (как в MCP)
    func: Callable              # реальная реализация

    def schema(self) -> dict:
        """Схема, которую получает LLM — именно так это выглядит в MCP."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self, collection_only: bool = False) -> list[dict]:
        """Схемы для планировщика. collection_only=True прячет финализатор
        save_findings: сохранение агент выполняет сам, детерминированно."""
        tools = self._tools.values()
        if collection_only:
            tools = [t for t in tools if t.name != "save_findings"]
        return [t.schema() for t in tools]

    def call(self, name: str, args: dict) -> dict:
        """Валидация имени и аргументов перед исполнением — обязательна:
        ответ модели это данные, а не истина."""
        if name not in self._tools:
            return {"error": f"unknown tool: {name}"}
        tool = self._tools[name]
        required = tool.parameters.get("required", [])
        missing = [k for k in required if k not in args]
        if missing:
            return {"error": f"missing required args: {missing}"}
        try:
            return {"ok": True, "result": tool.func(**args)}
        except Exception as exc:  # ошибка tool не должна ронять агента
            return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Источники данных — lab-mcp (MikroTik), с автоматическим fallback
# на демо-заглушки, если шлюз недоступен. См. agent/lab.py.
# ---------------------------------------------------------------------------

def get_dhcp_leases() -> list[dict]:
    """Все DHCP lease'ы сети (ip, mac, hostname, lease_status, expires)."""
    leases, source = lab.fetch_leases()
    print(f"[tools] get_dhcp_leases: {len(leases)} шт. (источник: {source})")
    return leases


def scan_network() -> list[dict]:
    """Устройства, реально присутствующие в сети (ARP-таблица роутера)."""
    devices, source = lab.fetch_scan()
    print(f"[tools] scan_network: {len(devices)} шт. (источник: {source})")
    return devices


# ---------------------------------------------------------------------------
# Остальные tools — настоящие (не заглушки)
# ---------------------------------------------------------------------------

def make_save_findings(db):
    """Фабрика: save_findings привязываем к конкретной БД."""
    def save_findings(findings: list[dict]) -> dict:
        run_id = db.record_run(findings)
        return {"run_id": run_id, "saved": len(findings)}
    return save_findings


def build_registry(db) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        name="get_dhcp_leases",
        description="Получить все DHCP lease'ы сети: список устройств с IP, MAC и hostname, которым DHCP-сервер выдал адрес.",
        parameters={"type": "object", "properties": {}, "required": []},
        func=get_dhcp_leases,
    ))
    reg.register(Tool(
        name="scan_network",
        description="Просканировать сеть (ping/ARP) и вернуть все устройства, которые реально отвечают, включая устройства без DHCP lease.",
        parameters={"type": "object", "properties": {}, "required": []},
        func=scan_network,
    ))
    reg.register(Tool(
        name="save_findings",
        description="Сохранить результаты сравнения DHCP leases и сканирования в базу данных. Принимает список finding-объектов.",
        parameters={
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Список finding: {ip, mac, hostname, vendor, status, category}",
                }
            },
            "required": ["findings"],
        },
        func=make_save_findings(db),
    ))
    return reg
