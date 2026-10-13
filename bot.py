import asyncio, logging, time
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, InputMediaPhoto, Message

import config, db, panel, payments, screens

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
FILE_IDS = {}   # кэш file_id, чтобы не загружать картинки каждый раз

def photo(name):
    return FILE_IDS.get(name) or FSInputFile(name)

def remember(name, msg):
    if msg and msg.photo and name not in FILE_IDS:
        FILE_IDS[name] = msg.photo[-1].file_id

async def show(target, scr):
    """Показывает экран: редактирует сообщение (если это нажатие кнопки) или шлёт новое."""
    name, text, markup = scr
    if isinstance(target, CallbackQuery):
        try:
            res = await target.message.edit_media(InputMediaPhoto(media=photo(name), caption=text), reply_markup=markup)
            remember(name, res if hasattr(res, "photo") else None)
            return
        except TelegramBadRequest:
            pass
        target = target.message
    msg = await target.answer_photo(photo(name), caption=text, reply_markup=markup)
    remember(name, msg)

async def screen_for(key, uid):
    u = db.get_user(uid)
    if key == "start": return screens.start(u)
    if key == "plans": return screens.plans(u)
    if key == "profile": return screens.profile(u)
    if key == "friends": return screens.friends(u)
    if key == "support": return screens.support(u)
    if key == "connect":
        sub = await panel.ensure_user(uid) if u["sub_until"] > time.time() else None
        return screens.connect(u, sub)
    return screens.start(u)

@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    ref = None
    if command.args and command.args.startswith("ref_"):
        try: ref = int(command.args[4:])
        except ValueError: pass
    db.get_user(m.from_user.id, ref)
    await show(m, await screen_for("start", m.from_user.id))

@dp.message(Command("connect", "plans", "profile", "friends", "support"))
async def cmd_screen(m: Message, command: CommandObject):
    await show(m, await screen_for(command.command, m.from_user.id))

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await show(m, await screen_for("support", m.from_user.id))

@dp.callback_query(F.data.startswith("go:"))
async def cb_go(c: CallbackQuery):
    await show(c, await screen_for(c.data[3:], c.from_user.id)); await c.answer()

@dp.callback_query(F.data == "trial")
async def cb_trial(c: CallbackQuery):
    u = db.get_user(c.from_user.id)
    until = db.claim_trial(c.from_user.id)
    if until is None:
        await show(c, screens.trial(u, ok=False))
    else:
        await panel.set_expiry(c.from_user.id, until)
        await show(c, screens.trial(u))
    await c.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    await show(c, screens.payment(db.get_user(c.from_user.id), c.data[4:])); await c.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery):
    _, plan, method = c.data.split(":")
    u = db.get_user(c.from_user.id)
    try:
        pid, url = await payments.create_invoice(c.from_user.id, plan, method)
    except Exception:
        logging.exception("invoice failed"); await show(c, screens.error(u)); return await c.answer()
    await show(c, screens.pay_dev(u, pid) if url is None else screens.pay_link(u, url)); await c.answer()

@dp.callback_query(F.data.startswith("devpay:"))
async def cb_devpay(c: CallbackQuery):
    if not config.DEV_PAY:
        return await c.answer("Недоступно", show_alert=True)
    res = db.complete_payment(int(c.data.split(":")[1]))
    if res:
        uid, plan, until = res
        await panel.set_expiry(uid, until)
        await show(c, screens.success(db.get_user(uid), until))
    await c.answer()

@dp.callback_query(F.data == "later")
async def cb_later(c: CallbackQuery):
    await c.answer("Напомним позже"); await c.message.delete()

async def reminders():
    """Раз в час напоминает тем, у кого подписка заканчивается в ближайшие 3 дня."""
    while True:
        try:
            for u in db.due_reminders(int(time.time())):
                try:
                    name, text, markup = screens.expiring(u)
                    await bot.send_photo(u["id"], photo(name), caption=text, reply_markup=markup)
                except Exception:
                    logging.exception("remind failed")
                db.mark_reminded(u["id"], u["sub_until"])
        except Exception:
            logging.exception("reminder loop")
        await asyncio.sleep(3600)

async def main():
    logging.basicConfig(level=logging.INFO)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="connect", description="Подключиться"),
        BotCommand(command="plans", description="Тарифы"),
        BotCommand(command="profile", description="Профиль"),
        BotCommand(command="friends", description="Пригласить друга"),
        BotCommand(command="support", description="Поддержка")])
    asyncio.create_task(reminders())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
