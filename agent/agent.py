"""Агентный цикл — сердце проекта.

Структура одного цикла (run):

  1. Планировщик выбирает tool сбора данных (LLM или фиксированный план).
  2. Агент исполняет tool, результат кладёт в контекст и в историю сообщений.
  3. Пункты 1–2 повторяются, пока планировщик не скажет «хватит»
     (либо не сработает лимит MAX_STEPS — защита от зацикливания).
  4. «Скучная» часть кодом: compare(leases, scan) → save_findings.
  5. Итог цикла сохраняется в runs, печатается в лог.

Почему сравнение не отдаём LLM: это детерминированная операция над двумя
таблицами. Код делает её бесплатно, точно и тестируемо. LLM в этом проекте
— только классификатор (planner.Categorizer) и (опционально) выбор tools.
"""
from . import config, notify, pipeline
from .db import DB
from .planner import (Action, Categorizer, LlmPlanner, ScriptedPlanner,
                      SYSTEM_PROMPT, build_llm)
from .tools import ToolRegistry, build_registry
from .unifi import collect_unifi

GOAL = "Собрать данные о сети: DHCP lease'ы и живые устройства."


class Agent:
    def __init__(self, db: DB, registry: ToolRegistry | None = None):
        self.db = db
        self.registry = registry or build_registry(db)
        # Базовый сбор должен работать без внешней LLM.
        # Это исключает тайм-ауты модели из обязательного цикла мониторинга.
        self.categorizer = Categorizer(None)
        self.planner = ScriptedPlanner()

    def run(self, goal: str = GOAL) -> dict:
        # Планировщик хранится в Agent между запусками.
        # Сбрасываем фиксированную очередь перед каждым циклом.
        if isinstance(self.planner, ScriptedPlanner):
            self.planner = ScriptedPlanner()

        context: dict[str, list] = {}          # сырые результаты по именам tools
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        self._log(f"=== Цикл агента начат (planner: {type(self.planner).__name__}) ===")

        # --- этап 1: планировщик собирает данные ---------------------------
        for step in range(1, config.MAX_STEPS + 1):
            try:
                action = self.planner.next_action(messages, self.registry)
            except Exception as exc:
                # LLM недоступен/ошибся — не роняем цикл, доигрываем
                # оставшиеся шаги детерминированным планом
                self._log(f"планировщик {type(self.planner).__name__} ошибся: "
                          f"{exc} — откат на ScriptedPlanner")
                self.planner = ScriptedPlanner()
                action = self.planner.next_action(messages, self.registry)
            if action.final is not None:
                self._log(f"шаг {step}: планировщик завершил сбор — {action.final}")
                break
            self._log(f"шаг {step}: tool '{action.tool}' args={action.args}")
            result = self.registry.call(action.tool, action.args)
            if result.get("ok"):
                context[action.tool] = result["result"]
            messages.append({"role": "assistant",
                             "content": f"вызываю {action.tool}({action.args})"})
            messages.append({"role": "tool",
                             "name": action.tool, "content": str(result)})
        else:
            self._log(f"достигнут лимит MAX_STEPS={config.MAX_STEPS}")

        # --- этап 2: детерминированный анализ и сохранение -----------------
        findings: list[dict] = []
        if "get_dhcp_leases" in context and "scan_network" in context:
            findings = pipeline.compare(context["get_dhcp_leases"],
                                        context["scan_network"],
                                        self.categorizer)
            # уведомление ДО сохранения: иначе устройство уже в БД
            # и при следующем опросе не сработает детект «нового»
            new_shadows = self._new_shadow_devices(findings)
            if new_shadows:
                self._log(f"новых живых теневых устройств: {len(new_shadows)}")
                notify.send_shadow_alert(new_shadows)
            saved = self.registry.call("save_findings", {"findings": findings})
            self._log(f"сохранено finding'ов: {saved}")
        else:
            self._log("данных недостаточно для сравнения — пропуск сохранения")

        # --- этап Wi-Fi: сбор UniFi и сохранение снимка -------------------

        wifi_data = collect_unifi()
        try:
            wifi_id = self.db.record_wifi_snapshot(wifi_data)
            if wifi_data["status"] == "ok":
                clients_saved = self.db.record_wifi_client_samples(
                    wifi_data["collected_at"], wifi_data["clients"]
                )
                radios_saved = self.db.record_wifi_radio_samples(
                    wifi_data["collected_at"], wifi_data["radios"]
                )
                events_saved = self.db.record_wifi_events(
                    wifi_data["events"]
                )

                anomalies_saved = self.db.update_wifi_anomaly_history(
                    wifi_data["collected_at"], wifi_data["clients"]
                )
                self._log(
                    f"История Wi-Fi: обнаружений аномалий={anomalies_saved}"
                )

                self._log(
                    f"UniFi snapshot #{wifi_id}: "
                    f"AP={wifi_data['ap_count']}, "
                    f"clients={wifi_data['client_count']}, "
                    f"WLAN={wifi_data['wlan_count']}"
                )
                self._log(
                    f"Wi-Fi детали: клиентов={clients_saved}, "
                    f"радио={radios_saved}, "
                    f"новых событий={events_saved}"
                )
            else:
                self._log(
                    f"UniFi сбор не удался: {wifi_data['error']}"
                )
        except Exception as exc:
            self._log(f"ошибка сохранения снимка UniFi: {exc}")

        # --- этап 3: итог --------------------------------------------------
        summary = {
            "leases": len(context.get("get_dhcp_leases", [])),
            "scanned": len(context.get("scan_network", [])),
            "findings": len(findings),
            "by_status": _count_by(findings, "status"),
            "by_category": _count_by(findings, "category"),
        }
        self._log(f"=== Цикл завершён: {summary} ===")
        return summary

    def _new_shadow_devices(self, findings: list[dict]) -> list[dict]:
        """Живые теневые устройства, которых раньше в таком статусе не было.

        «Новое» = MAC отсутствует в БД либо числится под другим статусом
        (например, было unconfirmed_device, а стало живым unknown_device —
        это эскалация и тоже повод для алерта).
        """
        known = {d["mac"]: d["status"] for d in self.db.devices()}
        return [f for f in findings
                if f["status"] == "unknown_device"
                and known.get(f["mac"]) != "unknown_device"]

    @staticmethod
    def _log(msg: str) -> None:
        print(f"[agent] {msg}")


def _count_by(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for item in items:
        out[item[key]] = out.get(item[key], 0) + 1
    return out
