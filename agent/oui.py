"""OUI-lookup: первые 3 байта MAC-адреса -> производитель.

Детерминированная подсказка для классификации устройств: работает даже
при пустом hostname и не требует LLM.

Источники данных (по приоритету):
1. Полный список IEEE — положите файл oui.txt рядом с БД (data/oui.txt):
       curl -o data/oui.txt https://standards-oui.ieee.org/oui/oui.txt
   В Docker data/ — volume, файл с хоста виден в контейнере.
2. Встроенный мини-словарь популярных вендоров (ниже) — fallback,
   покрывает типичную оргтехнику. Расширяйте свободно.
"""
import os
import re

# Встроенный fallback: 'AABBCC': 'Vendor' (регистр не важен)
BUILTIN = {
    "F01898": "Apple", "8C8590": "Apple", "A483E7": "Apple", "3C22FB": "Apple",
    "885E5A": "Huawei", "DC720B": "Huawei", "E848B8": "Huawei", "F02F74": "Huawei",
    "78A583": "Huawei", "F4C714": "Samsung", "BC1485": "Samsung", "F02765": "Samsung",
    "8C7712": "Samsung", "102F6B": "Samsung", "702AD5": "Samsung", "400E85": "Samsung",
    "E4F8EF": "Cisco", "289934": "Cisco", "001CB1": "Cisco-Linksys",
    "C864C7": "MikroTik", "ECD09F": "MikroTik", "4A5212": "MikroTik",
    "182DEA": "D-Link", "281F3B": "D-Link", "C4A81D": "D-Link",
    "50C7BF": "TP-Link", "30B49E": "TP-Link", "14CF92": "TP-Link",
    "D850E6": "ASUSTek", "AC220B": "ASUSTek", "04D9F5": "ASUSTek",
    "3C52D5": "Hewlett Packard", "10E7C6": "Hewlett Packard", "B499BA": "Hewlett Packard",
    "F49362": "Dell", "744AA4": "Dell", "84FB96": "Dell",
    "8C8CAA": "Lenovo", "F0761C": "Lenovo", "54EE75": "Lenovo",
    "F4F5D8": "Google", "3C5AB4": "Google",
    "DC4A9E": "Espressif", "240AC4": "Espressif", "8CAAE5": "Espressif",
    "B827EB": "Raspberry Pi Trading", "DCA632": "Raspberry Pi Trading",
    "E45F01": "Raspberry Pi Trading",
    "64B473": "Xiaomi", "F0B429": "Xiaomi", "34CE94": "Xiaomi",
    "8C3F0B": "Xiaomi",
    "B0359F": "Realtek Semiconductor", "00E04C": "Realtek Semiconductor",
    "525400": "QEMU (виртуальная машина)", "080027": "Oracle VirtualBox",
    "001C42": "Parallels", "005056": "VMware", "000C29": "VMware",
    "005F67": "Synology", "001132": "Synology",
    "001B11": "Canon", "9C32CE": "Canon",
    "0024A1": "Ubiquiti", "7483C2": "Ubiquiti", "E063DA": "Ubiquiti",
    "2047ED": "Netgear", "28C68E": "Netgear", "A040A0": "Netgear",
    "0022B0": "D-Link", "001CF0": "D-Link",
    "0014BF": "Askey Computer (модемы)", "C83EA7": "Askey Computer",
    "F0B0E7": "AzureWave Technology (Wi-Fi модули)", "74DA38": "AzureWave Technology",
    "647002": "Intel Corporate", "8CA982": "Intel Corporate",
    "708105": "Hon Hai Precision (Foxconn)",
}


def load_table(path: str) -> dict:
    """Загрузить полный список IEEE oui.txt. Вернуть {} при отсутствии."""
    if not os.path.isfile(path):
        return {}
    table = {}
    # строки вида: '8C-85-90   (hex)\t\tApple, Inc.'
    pattern = re.compile(r"^([0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2})\s+\(hex\)\s+(.+?)\s*$")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pattern.match(line)
            if m:
                table[m.group(1).replace("-", "").upper()] = m.group(2)
    return table


def oui_prefix(mac: str) -> str:
    """'BC:5F:F4:E4:09:73' -> 'BC5FF4' (6 hex-символов, верхний регистр)."""
    hexpart = re.sub(r"[^0-9A-Fa-f]", "", mac or "")[:6]
    return hexpart.upper()


def lookup(mac: str, table: dict | None = None) -> str:
    """Вендор по MAC или '' если неизвестен. table=None -> встроенный словарь."""
    prefix = oui_prefix(mac)
    if not prefix:
        return ""
    if table is None:
        table = BUILTIN
    return table.get(prefix, "")
