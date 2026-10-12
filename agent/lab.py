"""Источники данных лаборатории: lab-mcp (MikroTik) с fallback на заглушки.

get_dhcp_leases() — /ip/dhcp-server/lease/print на MikroTik (через tool
mikrotik_query шлюза lab-mcp). Отдаёт ВСЕ lease'ы (bound и waiting).

scan_network() — ARP-таблица роутера (/ip/arp/print). Это L2-реальность:
каждая запись с complete=true — устройство, которое точно было в сети,
независимо от DHCP. Сравнение leases vs ARP и даёт «теневые» устройства.

Формат ответов MikroTik в шлюзе — построчный str(dict), парсим через
ast.literal_eval. Если lab-mcp недоступен — прозрачно падаем на
заглушки (демо-режим без сети).
"""
import ast
import ipaddress
import random
import time

from . import config
from .mcp_client import McpClient, McpError, SseMcpClient


# ---------------------------------------------------------------------------
# Нормализация сырых ответов MikroTik -> наш формат {ip, mac, hostname, ...}
# ---------------------------------------------------------------------------

def _parse_ros_table(text: str) -> list[dict]:
    """Построчный вывод RouterOS (str(dict) на строку) -> список dict."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(ast.literal_eval(line))
        except (SyntaxError, ValueError):
            continue  # не разобрали строку — пропускаем, не падаем
    return rows


def _norm_lease(row: dict) -> dict:
    return {
        "ip": row.get("active-address") or row.get("address", ""),
        "mac": (row.get("active-mac-address") or row.get("mac-address", "")).lower(),
        "hostname": row.get("host-name") or row.get("comment", ""),
        "vendor": "",                       # OUI-lookup — идея для развития
        "lease_status": row.get("status", ""),        # bound | waiting
        "expires_after": row.get("expires-after", ""),
        "last_seen": row.get("last-seen", ""),
        "source": "dhcp",
    }


def _norm_arp(row: dict) -> dict | None:
    # complete=false — MAC не подтверждён, это шум, а не устройство
    if row.get("complete") != "true":
        return None
    return {
        "ip": row.get("address", ""),
        "mac": row.get("mac-address", "").lower(),
        "hostname": "",
        "vendor": "",
        "arp_status": row.get("status", ""),          # reachable | stale | permanent
        "interface": row.get("interface", ""),
        "source": "arp",
    }


# ---------------------------------------------------------------------------
# Границы пользовательской сети
# ---------------------------------------------------------------------------

def _in_lan_subnets(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(n, strict=False) for n in config.LAN_SUBNETS)


# ---------------------------------------------------------------------------
# Реальные источники через lab-mcp
# ---------------------------------------------------------------------------

def _query_mikrotik(mcp: McpClient, command: str) -> list[dict]:
    texts = mcp.call_tool("mikrotik_query", {"command": command})
    return _parse_ros_table("\n".join(texts))


def get_dhcp_leases() -> list[dict]:
    with SseMcpClient(config.LAB_MCP_URL, timeout=60) as mcp:
        rows = [_norm_lease(r) for r in _query_mikrotik(mcp, "/ip/dhcp-server/lease/print")]
    kept = [r for r in rows if _in_lan_subnets(r["ip"])]
    if len(kept) != len(rows):
        print(f"[lab] leases: отброшено вне LAN-подсетей: {len(rows) - len(kept)}")
    return kept


def scan_network() -> list[dict]:
    with SseMcpClient(config.LAB_MCP_URL, timeout=60) as mcp:
        rows = [_norm_arp(r) for r in _query_mikrotik(mcp, "/ip/arp/print")]
    devices = [r for r in rows if r]
    # два независимых фильтра: интерфейс должен быть LAN-мостом,
    # адрес — из LAN-подсети (отсекает WAN, туннели, APIPA, CGNAT)
    kept = [d for d in devices
            if d.get("interface") in config.LAN_INTERFACES and _in_lan_subnets(d["ip"])]
    dropped = len(devices) - len(kept)
    if dropped:
        print(f"[lab] arp: отброшено вне LAN (интерфейс/подсеть): {dropped}")
    return kept


# ---------------------------------------------------------------------------
# Заглушки (демо-режим, когда lab-mcp недоступен)
# ---------------------------------------------------------------------------

_LAB_DEVICES = [
    {"ip": "10.0.0.11", "mac": "3c:52:82:1a:4f:01", "hostname": "hp-printer-2floor", "vendor": "HP"},
    {"ip": "10.0.0.12", "mac": "f4:03:2a:9b:77:c2", "hostname": "ivanov-laptop", "vendor": "Intel"},
    {"ip": "10.0.0.13", "mac": "60:ab:d2:44:10:9e", "hostname": "petrov-iphone", "vendor": "Apple"},
    {"ip": "10.0.0.14", "mac": "28:39:26:5c:b1:0a", "hostname": "switch-core-1", "vendor": "D-Link"},
    {"ip": "10.0.0.15", "mac": "a4:83:e7:21:6d:55", "hostname": "android-tv-auditory", "vendor": "Google"},
    {"ip": "10.0.0.16", "mac": "8c:16:45:d3:92:7b", "hostname": "smirova-notebook", "vendor": "Lenovo"},
]

_UNKNOWN_POOL = [
    {"ip": "10.0.0.71", "mac": "de:ad:be:ef:00:01", "hostname": "", "vendor": ""},
    {"ip": "10.0.0.72", "mac": "aa:bb:cc:dd:ee:02", "hostname": "", "vendor": ""},
]


def _stub_leases() -> list[dict]:
    return [{**d, "expires": "2026-10-12 09:00", "source": "dhcp-stub"}
            for d in _LAB_DEVICES]


def _stub_scan() -> list[dict]:
    time.sleep(0.2)
    alive = list(_LAB_DEVICES)
    if alive and random.random() < 0.7:
        alive.remove(random.choice(alive))
    if random.random() < 0.6:
        intruder = dict(random.choice(_UNKNOWN_POOL))
        intruder["mac"] = intruder["mac"][:-2] + f"{random.randint(0, 255):02x}"
        alive.append(intruder)
    return [{**d, "source": "scan-stub"} for d in alive]


# ---------------------------------------------------------------------------
# Единая точка входа: lab-mcp, при недоступности — заглушки
# ---------------------------------------------------------------------------

def fetch_leases() -> tuple[list[dict], str]:
    try:
        return get_dhcp_leases(), "mikrotik"
    except (McpError, OSError) as exc:
        print(f"[lab] lab-mcp недоступен ({exc}) — демо-заглушки")
        return _stub_leases(), "stub"


def fetch_scan() -> tuple[list[dict], str]:
    try:
        return scan_network(), "arp"
    except (McpError, OSError) as exc:
        print(f"[lab] lab-mcp недоступен ({exc}) — демо-заглушки")
        return _stub_scan(), "stub"
