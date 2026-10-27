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
FIRST_DISC_PERCENT = int(os.getenv("FIRST_DISC_PERCENT", "0"))   # скидка на первую покупку, % (0 = выключена)
AUTO_MSGS = os.getenv("AUTO_MSGS", "1") == "1"                 # автосообщения и напоминания о брошенной оплате (0 = выключить)
ABANDON_AFTER_MIN = int(os.getenv("ABANDON_AFTER_MIN", "60"))  # через сколько минут напомнить о неоплаченном счёте
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

SUPPORT_IDS = {int(x) for x in os.getenv("SUPPORT_IDS", "").replace(" ", "").split(",") if x.isdigit()}   # роль «поддержка»: /find /user /users /expiring /give (до SUPPORT_GIVE_MAX дней)
SUPPORT_GIVE_MAX = int(os.getenv("SUPPORT_GIVE_MAX", "30"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}   # только числовые ID
TZ_HOURS = int(os.getenv("TZ_HOURS", "7"))   # часовой пояс для дат в админских командах (7 = Новосибирск)
STATS_IDS = {int(x) for x in os.getenv("STATS_IDS", "").replace(" ", "").split(",") if x.isdigit()}   # роль «бухгалтер» (прежнее название): /stats /payments /abandoned /report
ACCOUNTANT_IDS = STATS_IDS | {int(x) for x in os.getenv("ACCOUNTANT_IDS", "").replace(" ", "").split(",") if x.isdigit()}   # то же самое, можно задавать любой из двух переменных
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))        # во сколько (по TZ_HOURS) слать утренний отчёт; -1 = выключить
MONITOR_SEC = int(os.getenv("MONITOR_SEC", "120"))      # как часто проверять панель и ноды

# --- Панель Remnawave ---
PANEL_URL = os.getenv("PANEL_URL", "")        # например https://panel.example.com
PANEL_TOKEN = os.getenv("PANEL_TOKEN", "")    # API-токен: Remnawave -> Settings -> API Tokens
PANEL_SQUAD = os.getenv("PANEL_SQUAD", "")    # UUID internal squad (необязательно)
HWID_LIMIT = int(os.getenv("HWID_LIMIT", "3"))
