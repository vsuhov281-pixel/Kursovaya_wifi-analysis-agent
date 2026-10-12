"""Planner — «мозг», выбирающий следующее действие агента.

Два взаимозаменяемых планировщика с одинаковым интерфейсом:

  ScriptedPlanner — детерминированный план (без LLM). Работает всегда,
      идеален для разработки, тестов и запуска без API-ключа.
  LlmPlanner      — тот же цикл, но решения принимает OpenAI-совместимая
      модель: ей видны схемы tools, она просит их вызвать.

Важный принцип, заложенный в архитектуру: планировщик управляет только
СБОРОМ данных (какие tools опросить). «Скучная» детерминированная часть
(сравнение таблиц, сохранение в БД) выполняется агентом кодом в agent.py,
а LLM привлекается там, где нужен смысл — в Categorizer.
"""
import json
import os
import re
import urllib.request

from . import config, oui

SYSTEM_PROMPT = """Ты агент мониторинга локальной сети лаборатории.
Твоя задача — собрать данные: получи DHCP lease'ы и просканируй сеть.
Используй только доступные инструменты. Когда оба источника собраны —
заверши цикл коротким итогом."""


# ---------------------------------------------------------------------------
# Классификатор устройств — единственное место, где LLM реально нужен
# ---------------------------------------------------------------------------

_KEYWORD_RULES = [
    (("printer", "hp-", "mfp", "canon", "hewlett", "epson", "xerox"), "принтер/МФУ"),
    (("iphone", "ipad", "apple"), "мобильное устройство"),
    (("android", "tv", "pixel"), "ТВ/медиа"),
    (("switch", "router", "ap-", "core", "mikrotik", "tp-link", "d-link",
      "cisco", "ubiquiti", "netgear", "zyxel"), "сетевое оборудование"),
    (("laptop", "notebook", "pc"), "ноутбук/ПК"),
    (("raspberry",), "одноплатный компьютер"),
    (("espressif",), "IoT-устройство/микроконтроллер"),
    (("vmware", "virtualbox", "qemu", "parallels"), "виртуальная машина"),
    (("synology", "qnap", "nas"), "сетевое хранилище (NAS)"),
]


class Categorizer:
    """Определяет тип устройства по всему известному о нём контексту.

    Слои (сначала дешёвые и детерминированные):
    1. эвристика по ключевым словам hostname и вендора по OUI (MAC);
    2. LLM — только если контекст не пуст и эвристика не сработала,
       с полным контекстом: hostname, вендор по MAC, подсеть.

    Результаты кэшируются: экземпляр живёт вместе с агентом, кэш
    действует между опросами. Ключ кэша включает OUI и подсеть.
    """

    def __init__(self, llm: "LlmClient | None" = None):
        self.llm = llm
        self._cache: dict[tuple, str] = {}
        # полный список IEEE, если положили data/oui.txt — иначе встроенный
        oui_path = os.path.join(os.path.dirname(config.DB_PATH) or ".", "oui.txt")
        self._oui_table = oui.load_table(oui_path) or None

    def oui_vendor(self, mac: str) -> str:
        return oui.lookup(mac, self._oui_table)

    def category(self, hostname: str, vendor: str = "",
                 mac: str = "", ip: str = "") -> str:
        oui_vendor = self.oui_vendor(mac)
        subnet = ".".join(ip.split(".")[:3]) + ".0/24" if ip.count(".") == 3 else ""
        key = (hostname.lower(), vendor.lower(), oui_vendor, subnet)
        if key in self._cache:
            return self._cache[key]
        result = self._classify(hostname, vendor, oui_vendor, subnet)
        self._cache[key] = result
        return result

    def _classify(self, hostname: str, vendor: str,
                  oui_vendor: str, subnet: str) -> str:
        text = f"{hostname} {vendor} {oui_vendor}".strip().lower()
        if len(text) < 3:
            return "неизвестно"
        for keywords, category in _KEYWORD_RULES:
            if any(k in text for k in keywords):
                return category
        if self.llm:
            guess = self._llm_category(hostname, vendor, oui_vendor, subnet)
            if guess:
                return guess
        return "неизвестно"

    def _llm_category(self, hostname: str, vendor: str,
                      oui_vendor: str, subnet: str) -> str | None:
        facts = []
        if hostname:
            facts.append(f"hostname: {hostname}")
        if vendor:
            facts.append(f"заявленный вендор: {vendor}")
        if oui_vendor:
            facts.append(f"вендор по MAC: {oui_vendor}")
        if subnet:
            facts.append(f"подсеть: {subnet}")
        try:
            resp = self.llm.chat([
                {"role": "system", "content":
                 "Классифицируй тип устройства локальной сети по известным "
                 "признакам. Ответь строго JSON: "
                 '{"category": "одна-две слова на русском"}'},
                {"role": "user", "content": "; ".join(facts)},
            ])
            # модель может обернуть JSON в пояснения/markdown — достаём
            # первый {...} вместо строгого разбора всего ответа
            match = re.search(r"\{.*\}", resp, re.DOTALL)
            if match:
                return json.loads(match.group(0)).get("category")
        except Exception:
            pass  # любая ошибка LLM → молча падаем на эвристику
        return None


# ---------------------------------------------------------------------------
# Клиент OpenAI-совместимого API (только стандартная библиотека)
# ---------------------------------------------------------------------------

class LlmClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # тело ошибки API — главная зацепка для диагностики
            detail = exc.read()[:500].decode(errors="replace")
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc

    def chat(self, messages: list[dict]) -> str:
        # temperature не шлём: некоторые модели Kimi принимают только 1
        data = self._post({"model": self.model, "messages": messages})
        return data["choices"][0]["message"].get("content") or ""

    def chat_tool_calls(self, messages: list[dict], tools: list[dict]) -> list[dict]:
        data = self._post({"model": self.model, "messages": messages,
                           "tools": tools, "tool_choice": "auto"})
        return data["choices"][0]["message"].get("tool_calls") or []


def build_llm() -> LlmClient | None:
    if not config.LLM_API_KEY:
        return None
    return LlmClient(config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL)


# ---------------------------------------------------------------------------
# Планировщики
# ---------------------------------------------------------------------------

class Action:
    """Единое решение планировщика: либо вызов tool, либо финальный ответ."""
    def __init__(self, tool: str | None = None, args: dict | None = None,
                 final: str | None = None):
        self.tool, self.args, self.final = tool, args or {}, final

    @classmethod
    def use(cls, tool: str, **args):
        return cls(tool=tool, args=args)

    @classmethod
    def finish(cls, text: str):
        return cls(final=text)


class ScriptedPlanner:
    """Фиксированный план: leases → scan → финал. Работает без сети и токенов."""
    def __init__(self):
        self._queue = ["get_dhcp_leases", "scan_network"]

    def next_action(self, messages: list, registry) -> Action:
        if self._queue:
            return Action.use(self._queue.pop(0))
        return Action.finish("Данные собраны (scripted planner).")


class LlmPlanner:
    """Решения принимает модель по ходу цикла (классический agentic loop)."""
    def __init__(self, llm: LlmClient):
        self.llm = llm

    def next_action(self, messages: list, registry) -> Action:
        tool_calls = self.llm.chat_tool_calls(messages, registry.schemas(collection_only=True))
        if not tool_calls:
            return Action.finish("(модель не запросила инструменты)")
        call = tool_calls[0]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        return Action.use(call["function"]["name"], **args)
