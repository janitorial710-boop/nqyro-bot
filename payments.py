"""Оплата через ЮKassa. DEV_PAY=1 включает тестовый режим без реальных денег."""
import logging, uuid
import aiohttp
import config, db

API = "https://api.yookassa.ru/v3/payments"

def _session():
    return aiohttp.ClientSession(auth=aiohttp.BasicAuth(config.YK_SHOP_ID, config.YK_SECRET),
                                 timeout=aiohttp.ClientTimeout(total=30))

async def create_invoice(uid: int, plan: str, method: str, email: str | None = None):
    pid = db.create_payment(uid, plan, method)
    if config.DEV_PAY:
        return pid, None
    price, _, name = config.PLANS[plan]
    amount = {"value": f"{price}.00", "currency": "RUB"}
    body = {
        "amount": amount, "capture": True,
        "confirmation": {"type": "redirect", "return_url": config.RETURN_URL},
        "description": f"NQYRO: {name}",
        "metadata": {"payment_id": str(pid), "user_id": str(uid)},
    }
    if method == "sbp":
        body["payment_method_data"] = {"type": "sbp"}
    elif method == "card":
        body["payment_method_data"] = {"type": "bank_card"}
    if config.RECEIPT_MODE == "email" and email:
        body["receipt"] = {"customer": {"email": email}, "items": [{
            "description": f"Подписка NQYRO, {name}", "quantity": "1.00", "amount": amount,
            "vat_code": config.VAT_CODE, "payment_mode": "full_payment", "payment_subject": "service"}]}
    async with _session() as s:
        async with s.post(API, json=body, headers={"Idempotence-Key": str(uuid.uuid4())}) as r:
            data = await r.json()
            if r.status >= 300:
                logging.error("YooKassa create error %s: %s", r.status, data)
                raise RuntimeError(f"YooKassa {r.status}")
    db.set_yk_id(pid, data["id"])
    return pid, data["confirmation"]["confirmation_url"]

async def status(yk_id: str) -> str:
    """pending | waiting_for_capture | succeeded | canceled"""
    async with _session() as s:
        async with s.get(f"{API}/{yk_id}") as r:
            data = await r.json()
            if r.status >= 300:
                logging.error("YooKassa status error %s: %s", r.status, data)
                raise RuntimeError(f"YooKassa {r.status}")
            return data["status"]
