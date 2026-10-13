"""Платежи. Сейчас тестовый режим (DEV_PAY=1): оплата подтверждается кнопкой в боте.
Для боя: создай счёт у платёжного сервиса, верни ссылку, а на вебхуке вызови db.complete_payment(pid)."""
import config, db

async def create_invoice(uid: int, plan: str, method: str):
    pid = db.create_payment(uid, plan, method)
    if config.DEV_PAY:
        return pid, None
    # TODO: запрос к API платёжного сервиса -> вернуть (pid, ссылка_на_оплату)
    raise NotImplementedError("Подключи платёжный сервис")
