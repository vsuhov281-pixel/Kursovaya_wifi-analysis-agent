"""Детерминированная логика сравнения: DHCP leases vs реальный скан.

Это «скучная» часть, которую мы НЕ отдаём LLM — сравнение таблиц
дёшево и надёжно делать кодом. LLM привлекается только для
классификации устройств (см. planner.py).

Статусы finding:
  active             — есть и lease, и ответ на скан (норма);
  unknown_device     — ПРЯМО СЕЙЧАС живёт в сети без lease
                       (ARP reachable: статический IP? чужое? — тревожно);
  unconfirmed_device — раньше виделся без lease, сейчас молчит
                       (ARP stale: наблюдаем, не тревожимся);
  stale_lease        — lease есть, устройство молчит (выключено/ушло).
"""
from .planner import Categorizer


def compare(leases: list[dict], scan: list[dict], categorizer: Categorizer) -> list[dict]:
    leases_by_mac = {d["mac"].lower(): d for d in leases}
    scan_by_mac = {d["mac"].lower(): d for d in scan}

    findings: list[dict] = []
    for mac, lease in leases_by_mac.items():
        if mac in scan_by_mac:
            findings.append(_make(lease, "active", categorizer))
        else:
            findings.append(_make(lease, "stale_lease", categorizer))
    for mac, dev in scan_by_mac.items():
        if mac not in leases_by_mac:
            # «живость» по ARP: reachable/delay/permanent — устройство
            # подтверждено сейчас; stale — только когда-то виделось.
            live = dev.get("arp_status") in ("reachable", "delay", "permanent")
            findings.append(_make(dev, "unknown_device" if live
                                  else "unconfirmed_device", categorizer))
    return findings


def _make(dev: dict, status: str, categorizer: Categorizer) -> dict:
    return {
        "ip": dev.get("ip", ""),
        "mac": dev.get("mac", ""),
        "hostname": dev.get("hostname", ""),
        "vendor": dev.get("vendor", ""),
        "category": categorizer.category(dev.get("hostname", ""), dev.get("vendor", "")),
        "status": status,
    }
