import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "nqyro_bot")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "nqyro_support")
DB_PATH = os.getenv("DB_PATH", "data.db")
DEV_PAY = os.getenv("DEV_PAY", "1") == "1"   # 1 = тестовая оплата без платёжного сервиса
SUB_BASE = os.getenv("SUB_BASE", "https://sub.example.com/sub")
OPEN_URL = os.getenv("OPEN_URL", "https://sub.example.com/open/{app}?sub={sub}")
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

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
