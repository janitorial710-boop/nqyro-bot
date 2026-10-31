"""Оплата через ЮKassa. DEV_PAY=1 включает тестовый режим без реальных денег."""
import logging, time, uuid
import aiohttp
import config, db

API = "https://api.yookassa.ru/v3/payments"

def _session():
    return aiohttp.ClientSession(auth=aiohttp.BasicAuth(config.YK_SHOP_ID, config.YK_SECRET),
                                 timeout=aiohttp.ClientTimeout(total=30))

class YKError(RuntimeError):
    """Отказ ЮKassa с понятной причиной (код, описание, поле), чтобы её было видно в Telegram, а не только в логах."""
    def __init__(self, status, data):
        d = data if isinstance(data, dict) else {}
        self.status, self.code, self.parameter = status, d.get("code") or "", d.get("parameter") or ""
        self.description = d.get("description") or (str(data)[:200] if data else "")
        super().__init__(f"HTTP {status}: {self.code} {self.description}" + (f" (поле {self.parameter})" if self.parameter else ""))

LAST_ERROR = {"ts": 0, "text": ""}   # последняя ошибка создания счёта (смотреть: /ykcheck)

async def create_invoice(uid: int, plan: str, method: str, email: str | None = None, gift: bool = False, amount: int | None = None):
    topup = plan == "topup"
    pid = db.create_topup(uid, amount, method) if topup else db.create_payment(uid, plan, method, gift)
    if config.DEV_PAY:
        return pid, None
    name = config.plan_name(plan)
    price = db.get_payment(pid)["amount"]   # уже с учётом скидки
    amount = {"value": f"{price}.00", "currency": "RUB"}
    body = {
        "amount": amount, "capture": True,
        "confirmation": {"type": "redirect", "return_url": config.RETURN_URL},
        "description": f"NQYRO: {'подарок, ' if gift else ''}{name}",
        "metadata": {"payment_id": str(pid), "user_id": str(uid)},
    }
    if method == "sbp":
        body["payment_method_data"] = {"type": "sbp"}
    elif method == "card":
        body["payment_method_data"] = {"type": "bank_card"}
    if config.AUTORENEW and not gift and not topup and method != "sbp" and db.get_user(uid)["autorenew"]:
        body["save_payment_method"] = True   # карта сохранится для автопродления (СБП не сохраняется)
    if config.RECEIPT_MODE == "email" and email:
        body["receipt"] = {"customer": {"email": email}, "items": [{
            "description": "Пополнение баланса NQYRO" if topup else f"{'Подарочная подписка' if gift else 'Подписка'} NQYRO, {name}", "quantity": "1.00", "amount": amount,
            "vat_code": config.VAT_CODE, "payment_mode": "full_payment", "payment_subject": "service"}]}
    async with _session() as s:
        for attempt in (1, 2):
            async with s.post(API, json=body, headers={"Idempotence-Key": str(uuid.uuid4())}) as r:
                data = await r.json(content_type=None)
                if r.status < 300: break
                logging.error("YooKassa create error %s: %s", r.status, data)
                err = YKError(r.status, data)
                # Сохранение карты для автопродления в магазине может быть не разрешено: платим без него, а не теряем оплату
                if attempt == 1 and body.get("save_payment_method") and (err.parameter == "save_payment_method" or r.status == 403 or "save_payment_method" in str(data)):
                    body.pop("save_payment_method")
                    LAST_ERROR.update(ts=int(time.time()), text="ЮKassa не разрешает сохранять карты (автоплатежи не включены в магазине): " + str(err))
                    logging.warning("YooKassa: сохранение карты отклонено, создаю счёт без него")
                    continue
                LAST_ERROR.update(ts=int(time.time()), text=str(err))
                raise err
    db.set_yk_id(pid, data["id"])
    return pid, data["confirmation"]["confirmation_url"]

async def account_info() -> dict:
    """Данные магазина: проверка YK_SHOP_ID и YK_SECRET (GET /v3/me)."""
    async with _session() as s:
        async with s.get("https://api.yookassa.ru/v3/me") as r:
            data = await r.json(content_type=None)
            if r.status >= 300: raise YKError(r.status, data)
            return data

async def refund(yk_id: str, amount, email: str | None = None, name: str = "") -> None:
    """Возврат платежа целиком через ЮKassa. Бросает исключение, если ЮKassa отказала."""
    value = {"value": f"{int(amount)}.00", "currency": "RUB"}
    body = {"payment_id": yk_id, "amount": value}
    if config.RECEIPT_MODE == "email" and email:
        body["receipt"] = {"customer": {"email": email}, "items": [{
            "description": "Пополнение баланса NQYRO" if name.startswith("пополнение") else f"Подписка NQYRO, {name}", "quantity": "1.00", "amount": value,
            "vat_code": config.VAT_CODE, "payment_mode": "full_payment", "payment_subject": "service"}]}
    async with _session() as s:
        async with s.post("https://api.yookassa.ru/v3/refunds", json=body, headers={"Idempotence-Key": str(uuid.uuid4())}) as r:
            data = await r.json()
            if r.status >= 300 or data.get("status") == "canceled":
                logging.error("YooKassa refund error %s: %s", r.status, data)
                raise RuntimeError(f"YooKassa refund {r.status}: {data.get('description') or data.get('status')}")

async def status(yk_id: str) -> str:
    """pending | waiting_for_capture | succeeded | canceled"""
    async with _session() as s:
        async with s.get(f"{API}/{yk_id}") as r:
            data = await r.json()
            if r.status >= 300:
                logging.error("YooKassa status error %s: %s", r.status, data)
                raise RuntimeError(f"YooKassa {r.status}")
            return data["status"]


async def get(yk_id: str) -> dict:
    """Платёж целиком (нужен, чтобы забрать сохранённый способ оплаты)."""
    async with _session() as s:
        async with s.get(f"{API}/{yk_id}") as r:
            data = await r.json()
            if r.status >= 300:
                raise RuntimeError(f"YooKassa {r.status}")
            return data

async def charge_saved(pid: int, uid: int, pm_id: str, email: str | None = None) -> str:
    """Автосписание с сохранённой карты. Возвращает статус ЮKassa (succeeded / pending / canceled)."""
    p = db.get_payment(pid)
    name = config.plan_name(p["plan"])
    amount = {"value": f"{p['amount']}.00", "currency": "RUB"}
    body = {"amount": amount, "capture": True, "payment_method_id": pm_id,
            "description": f"NQYRO: автопродление, {name}",
            "metadata": {"payment_id": str(pid), "user_id": str(uid), "autorenew": "1"}}
    if config.RECEIPT_MODE == "email" and email:
        body["receipt"] = {"customer": {"email": email}, "items": [{
            "description": f"Подписка NQYRO, {name}", "quantity": "1.00", "amount": amount,
            "vat_code": config.VAT_CODE, "payment_mode": "full_payment", "payment_subject": "service"}]}
    async with _session() as s:
        async with s.post(API, json=body, headers={"Idempotence-Key": f"renew-{pid}"}) as r:
            data = await r.json()
            if r.status >= 300:
                logging.error("YooKassa autorenew error %s: %s", r.status, data)
                raise RuntimeError(f"YooKassa {r.status}")
    db.set_yk_id(pid, data["id"])
    return data["status"]
