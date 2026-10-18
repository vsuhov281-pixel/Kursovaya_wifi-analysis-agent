
"""Read-only сбор отчёта и подробных данных UniFi через MCP."""

import json
import re
from datetime import datetime, timezone

from .mcp_client import SseMcpClient
from . import config


def _call_json(client, name: str, arguments: dict) -> dict:
    """Вызвать инструмент UniFi через unifi_execute и разобрать JSON."""
    blocks = client.call_tool(
        "unifi_execute",
        {"tool": name, "arguments": arguments},
    )

    for block in blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict):
                if data.get("success") is False or data.get("error"):
                    raise RuntimeError(f"{name}: {data}")
                return data
        except json.JSONDecodeError:
            continue

    raise RuntimeError(f"{name}: ответ не содержит JSON")


def collect_unifi() -> dict:
    """Собрать общий отчёт, подробности клиентов, радио и события UniFi."""
    result = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "status": "error",
        "ap_count": 0,
        "client_count": 0,
        "wlan_count": 0,
        "report": "",
        "error": None,
        "clients": [],
        "radios": [],
        "events": [],
    }

    try:
        with SseMcpClient(config.LAB_MCP_URL, timeout=120) as client:
            # 1. Общий отчёт UniFi
            blocks = client.call_tool("unifi_network_report", {})
            report = "\n".join(blocks)

            if not report.strip():
                raise RuntimeError("UniFi вернул пустой отчёт")

            def count_section(section: str) -> int:
                match = re.search(
                    rf"(?m)^{section}\s*\((\d+)\)\s*:",
                    report,
                    re.IGNORECASE,
                )
                return int(match.group(1)) if match else 0

            result.update({
                "status": "ok",
                "ap_count": count_section("Devices"),
                "client_count": count_section("Clients"),
                "wlan_count": count_section("WLANs"),
                "report": report,
            })

            # 2. Список Wi-Fi-клиентов
            clients_data = _call_json(
                client,
                "unifi_list_clients",
                {"filter_type": "wireless", "limit": 500},
            )

            clients = clients_data.get("clients", [])
            detailed_clients = []

            # Получаем подробности для каждого клиента.
            for client_data in clients:
                mac = client_data.get("mac")

                if not mac:
                    continue

                try:
                    details_data = _call_json(
                        client,
                        "unifi_get_client_details",
                        {
                            "mac_address": mac,
                            "summary": False,
                        },
                    )

                    details = details_data.get("client", {})
                    merged = {**client_data, **details}

                    # Нормализуем поля для сохранения в БД.
                    merged["ap_name"] = (
                        details.get("last_uplink_name")
                        or details.get("ap_name")
                    )
                    merged["ap_mac"] = (
                        details.get("ap_mac")
                        or details.get("last_uplink_mac")
                    )

                    if details.get("signal") is not None:
                        merged["signal_dbm"] = details["signal"]

                    merged["wifi_tx_retries_percentage"] = details.get(
                        "wifi_tx_retries_percentage"
                    )
                    merged["satisfaction"] = details.get("satisfaction")

                    detailed_clients.append(merged)

                except Exception as exc:
                    # Ошибка подробностей одного клиента
                    # не должна останавливать сбор остальных.
                    print(
                        f"[unifi] не удалось получить подробности "
                        f"{mac}: {exc}"
                    )
                    detailed_clients.append(client_data)

            result["clients"] = detailed_clients

            # 3. Точки доступа и показатели их радиомодулей
            devices_data = _call_json(
                client,
                "unifi_list_devices",
                {"device_type": "ap", "limit": 100},
            )

            radios = []

            for ap in devices_data.get("devices", []):
                ap_mac = ap.get("mac")

                if not ap_mac:
                    continue

                try:
                    radio_data = _call_json(
                        client,
                        "unifi_get_device_radio",
                        {"mac_address": ap_mac},
                    )

                    for radio in radio_data.get("radios", []):
                        radios.append({
                            **radio,
                            "ap_mac": ap_mac,
                            "ap_name": (
                                ap.get("name")
                                or radio_data.get("name")
                            ),
                        })

                except Exception as exc:
                    print(
                        f"[unifi] не удалось получить радио "
                        f"точки {ap_mac}: {exc}"
                    )

            result["radios"] = radios

            # 4. События UniFi за последние 24 часа
            events_data = _call_json(
                client,
                "unifi_list_events",
                {"within_hours": 24, "limit": 500},
            )

            collected_at = result["collected_at"]

            for event in events_data.get("events", []):
                timestamp = event.get("time")
                event_time = None

                if isinstance(timestamp, (int, float)):
                    event_time = datetime.fromtimestamp(
                        timestamp / 1000,
                        timezone.utc,
                    ).isoformat()

                result["events"].append({
                    **event,
                    "collected_at": collected_at,
                    "event_time": event_time,
                })

    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result
