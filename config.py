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
AUTO_FREE_DEVICE = os.getenv("AUTO_FREE_DEVICE", "1") != "0"   # при «Добавить в Happ/Incy» освобождать слот, если лимит устройств исчерпан

# --- Пополнение баланса пользователем ---
TOPUP_MIN = int(os.getenv("TOPUP_MIN", "50"))
TOPUP_MAX = int(os.getenv("TOPUP_MAX", "15000"))
TOPUP_BONUS_PERCENT = int(os.getenv("TOPUP_BONUS_PERCENT", "0"))   # бонус к пополнению, % (0 = выключен)

# --- Бэкапы базы ---
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "4"))      # во сколько (по TZ_HOURS) делать бэкап и слать админам; -1 = выключить
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))     # сколько последних копий хранить на сервере
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")

# --- Вебхук ЮKassa (нужны WEB_PORT и OPEN_BASE) ---
YK_WEBHOOK_SECRET = os.getenv("YK_WEBHOOK_SECRET", "")   # секрет в адресе: {OPEN_BASE}/yk/<секрет>; пусто = вебхук выключен

DAY_RATE = PLANS["1"][0] / PLANS["1"][1]   # ₽ за 1 день по месячному тарифу (249/30 = 8.3 ₽): по нему баланс обменивается на дни
CONVERT_MIN = int(-(-DAY_RATE // 1))       # минимум для обмена: ~1 день (9 ₽)

def plan_name(key):
    """Название тарифа для отображения; 'topup' = пополнение баланса."""
    if key in PLANS: return PLANS[key][2]
    return {"topup": "пополнение баланса", "days": "дни подписки (с баланса)"}.get(key, str(key))

# --- Вывод реферальных денег и прочее (extras.py) ---
PAYOUT_MIN = int(os.getenv("PAYOUT_MIN", "250"))                      # минимальная сумма вывода, ₽
PAYOUT_HOLD_DAYS = int(os.getenv("PAYOUT_HOLD_DAYS", "14"))           # сколько дней бонус «вылёживается» перед выводом
PAYOUT_COOLDOWN_DAYS = int(os.getenv("PAYOUT_COOLDOWN_DAYS", "7"))    # не чаще одного вывода за N дней
SURVEY_AFTER_DAYS = int(os.getenv("SURVEY_AFTER_DAYS", "2"))          # через сколько дней после конца подписки спросить «почему не продлил»
DEVICE_RESET_COOLDOWN_MIN = int(os.getenv("DEVICE_RESET_COOLDOWN_MIN", "10"))   # как часто человек может сбрасывать устройства сам


# --- Автопродление (ЮKassa: сохранённая карта). В кабинете ЮKassa должны быть подключены «Автоплатежи» (рекуррентные платежи) ---
AUTORENEW = os.getenv("AUTORENEW", "0") == "1"                       # 1 = показывать людям «Автопродление»
AUTORENEW_NOTICE_DAYS = int(os.getenv("AUTORENEW_NOTICE_DAYS", "2"))  # за сколько дней предупреждать о списании

# --- Другие способы оплаты ---
STARS_RUB_PER_STAR = float(os.getenv("STARS_RUB_PER_STAR", "0"))    # сколько ₽ стоит одна звезда; 0 = оплата Stars выключена
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "")                         # токен Crypto Pay (@CryptoBot → Crypto Pay → Создать приложение); пусто = выключено
CRYPTO_ASSETS = os.getenv("CRYPTO_ASSETS", "USDT,TON")               # какими монетами можно платить
CRYPTO_API = os.getenv("CRYPTO_API", "https://pay.crypt.bot/api")    # для тестовой сети: https://testnet-pay.crypt.bot/api

# --- Возврат ушедших (скидка тем, у кого подписка закончилась давно) ---
WINBACK_PERCENT = int(os.getenv("WINBACK_PERCENT", "20"))            # размер скидки, %; 0 = выключено
WINBACK_AFTER_DAYS = int(os.getenv("WINBACK_AFTER_DAYS", "14"))      # через сколько дней после конца подписки писать
WINBACK_WINDOW_DAYS = int(os.getenv("WINBACK_WINDOW_DAYS", "7"))     # кому писать: ушли не раньше, чем AFTER+WINDOW дней назад (старых не трогаем)
WINBACK_HOURS = int(os.getenv("WINBACK_HOURS", "72"))                # сколько часов действует скидка
