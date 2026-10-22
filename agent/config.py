"""Конфигурация: все настройки через переменные окружения (12-factor).

Для Docker-запуска значения задаются в .env / docker-compose.yml.
"""
import os

# --- Хранилище ---
DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "agent.db"))

# --- Веб-сервер ---
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))

# --- Расписание опроса (в контейнере веб-сервер сам гоняет агента) ---
SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "15"))

# --- Источник данных: шлюз lab-mcp (MikroTik DHCP + ARP) ---
# Если сервер недоступен, агент автоматически переключается на демо-заглушки.
LAB_MCP_URL = os.environ.get("LAB_MCP_URL", "http://192.168.0.61:8100/sse")

# --- Границы пользовательской сети (фильтр «теневых» устройств) ---
# В ARP-таблицу роутера попадают аплинки (WAN), туннели и служебные
# диапазоны (169.254/16, CGNAT 100.64/10 и т.п.). Считаем «нашей сетью»
# только эти интерфейсы и подсети — иначе «теневых» устройств сотни.
# Формат: списки через запятую.
LAN_INTERFACES = tuple(
    s.strip() for s in os.environ.get("LAN_INTERFACES", "bridge1-lan").split(",")
    if s.strip())
LAN_SUBNETS = tuple(
    s.strip() for s in os.environ.get("LAN_SUBNETS", "192.168.0.0/16").split(",")
    if s.strip())

# --- LLM (опционально). OpenAI-совместимый API ---
# Если LLM_API_KEY не задан — агент работает в детерминированном режиме
# (ScriptedPlanner), весь функционал сохраняется.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# Дополнительные настройки Yandex Cloud AI Studio.
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")

# --- Уведомления по email (опционально) ---
# Без SMTP_PASSWORD уведомления выключены, остальное работает.
# Яндекс: smtp.yandex.ru, порт 465, пароль — «пароль приложения».
# Внимание: сети с блокировкой исходящего SMTP (25/465/587) не пропустят.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_TO = [s.strip() for s in os.environ.get("MAIL_TO", "").split(",") if s.strip()]

# --- Лимиты безопасности агентного цикла ---
MAX_STEPS = int(os.environ.get("MAX_STEPS", "10"))
