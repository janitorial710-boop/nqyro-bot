import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "nqyro_bot")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "nqyro_support").lstrip("@")
DB_PATH = os.getenv("DB_PATH", "data.db")
DEV_PAY = os.getenv("DEV_PAY", "1") == "1"   # 1 = тестовая оплата без платёжного сервиса
SUB_BASE = os.getenv("SUB_BASE", "https://sub.example.com/sub")
OPEN_BASE = os.getenv("OPEN_BASE", "").rstrip("/")   # публичный https-адрес бота, например https://bot.nqyrovpn.site
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")        # на каком адресе слушает встроенная страница-редирект
WEB_PORT = int(os.getenv("WEB_PORT", "0"))            # 0 = страница выключена
SUB_HOSTS = {x for x in os.getenv("SUB_HOSTS", "").replace(" ", "").split(",") if x}   # разрешённые домены подписки (кроме домена панели)
# шаблон https://.../{app}?sub={sub}; по умолчанию собирается из OPEN_BASE; пусто = кнопок «Добавить в …» не будет
OPEN_URL = os.getenv("OPEN_URL", "") or (f"{OPEN_BASE}/open/{{app}}?sub={{sub}}" if OPEN_BASE else "")
HAPP_URL = os.getenv("HAPP_URL", "https://www.happ.su")
INCY_URL = os.getenv("INCY_URL", "https://incy.cc")
TRIAL_DAYS = 5
REF_PERCENT = 20
# ключ: (цена, дней, название)
PLANS = {"1": (249, 30, "1 месяц"), "3": (599, 90, "3 месяца"), "12": (1990, 365, "12 месяцев")}

# --- ЮKassa ---
YK_SHOP_ID = os.getenv("YK_SHOP_ID", "")
YK_SECRET = os.getenv("YK_SECRET", "")
RECEIPT_MODE = os.getenv("RECEIPT_MODE", "none")      # none | email (бот спросит email для чека)
VAT_CODE = int(os.getenv("VAT_CODE", "1"))             # 1 = без НДС
RETURN_URL = os.getenv("RETURN_URL", f"https://t.me/{BOT_USERNAME}")
if not DEV_PAY and not (YK_SHOP_ID and YK_SECRET):
    raise RuntimeError("Задай YK_SHOP_ID и YK_SECRET или включи DEV_PAY=1")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}   # только числовые ID

# --- Панель Remnawave ---
PANEL_URL = os.getenv("PANEL_URL", "")        # например https://panel.example.com
PANEL_TOKEN = os.getenv("PANEL_TOKEN", "")    # API-токен: Remnawave -> Settings -> API Tokens
PANEL_SQUAD = os.getenv("PANEL_SQUAD", "")    # UUID internal squad (необязательно)
HWID_LIMIT = int(os.getenv("HWID_LIMIT", "3"))
