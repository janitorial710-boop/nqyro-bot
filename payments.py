"""Оплата через ЮKassa. DEV_PAY=1 включает тестовый режим без реальных денег."""
import logging, uuid
import aiohttp
import config, db

API = "https://api.yookassa.ru/v3/payments"

def _session():
    return aiohttp.ClientSession(auth=aiohttp.BasicAuth(config.YK_SHOP_ID, config.YK_SECRET),
                                 timeout=aiohttp.ClientTimeout(total=30))

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
    if config.RECEIPT_MODE == "email" and email:
        body["receipt"] = {"customer": {"email": email}, "items": [{
            "description": "Пополнение баланса NQYRO" if topup else f"{'Подарочная подписка' if gift else 'Подписка'} NQYRO, {name}", "quantity": "1.00", "amount": amount,
            "vat_code": config.VAT_CODE, "payment_mode": "full_payment", "payment_subject": "service"}]}
    async with _session() as s:
        async with s.post(API, json=body, headers={"Idempotence-Key": str(uuid.uuid4())}) as r:
            data = await r.json()
            if r.status >= 300:
                logging.error("YooKassa create error %s: %s", r.status, data)
                raise RuntimeError(f"YooKassa {r.status}")
    db.set_yk_id(pid, data["id"])
    return pid, data["confirmation"]["confirmation_url"]

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
