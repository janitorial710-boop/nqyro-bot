import asyncio, html, logging, re, time
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, InputMediaPhoto, Message

import config, db, panel, payments, redirect, screens

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def track(u):
    """Запоминает имя и @username пользователя (нужно для /users)."""
    if u is None or u.is_bot: return
    try:
        db.touch_user(u.id, u.username or "", ((u.first_name or "") + " " + (u.last_name or "")).strip())
    except Exception:
        logging.exception("touch_user")

class Track(BaseMiddleware):
    async def __call__(self, handler, event, data):
        track(getattr(event, "from_user", None))
        return await handler(event, data)

dp.message.outer_middleware(Track())
dp.callback_query.outer_middleware(Track())
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
    WAIT_PROMO.discard(m.from_user.id); db.get_user(m.from_user.id, ref); track(m.from_user)
    await show(m, await screen_for("start", m.from_user.id))

@dp.message(Command("connect", "plans", "profile", "friends", "support"))
async def cmd_screen(m: Message, command: CommandObject):
    await show(m, await screen_for(command.command, m.from_user.id))

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await show(m, await screen_for("support", m.from_user.id))

@dp.callback_query(F.data.startswith("go:"))
async def cb_go(c: CallbackQuery):
    WAIT_PROMO.discard(c.from_user.id)
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

@dp.callback_query(F.data.startswith("paybal:"))
async def cb_paybal(c: CallbackQuery):
    plan = c.data.split(":")[1]
    if plan not in config.PLANS:
        return await c.answer()
    u = db.get_user(c.from_user.id)
    price = config.PLANS[plan][0]
    if db.total_balance(u) < price:
        return await c.answer(f"Не хватает {price - db.total_balance(u):.0f} ₽. Выбери другой способ оплаты.", show_alert=True)
    until = db.pay_with_balance(c.from_user.id, plan)
    if until is None:
        return await c.answer("Не хватает средств на балансе.", show_alert=True)
    await panel.set_expiry(c.from_user.id, until)
    await show(c, screens.success(db.get_user(c.from_user.id), until)); await c.answer("Оплачено с баланса")

WAIT_EMAIL = {}   # user_id -> (plan, method): ждём email для чека
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

async def finalize_if_paid(pid):
    """Проверяет платёж в ЮKassa. True, если платёж завершён (оплачен или отменён)."""
    p = db.get_payment(pid)
    if p is None: return True
    if p["status"] == "paid": return True
    if not p["yk_id"]: return False
    try:
        st = await payments.status(p["yk_id"])
    except Exception:
        logging.exception("status check failed"); return False
    if st == "succeeded":
        res = db.complete_payment(pid)
        if res:
            uid, plan, until = res
            await panel.set_expiry(uid, until)
            name, text, markup = screens.success(db.get_user(uid), until)
            msg = await bot.send_photo(uid, photo(name), caption=text, reply_markup=markup)
            remember(name, msg)
        return True
    if st == "canceled":
        db.cancel_payment(pid)
        name, text, markup = screens.error(db.get_user(p["user_id"]))
        await bot.send_photo(p["user_id"], photo(name), caption=text, reply_markup=markup)
        return True
    return False

async def watch(pid):
    """Опрашивает ЮKassa каждые 5 секунд до 20 минут (вебхук не нужен)."""
    end = time.time() + 20 * 60
    while time.time() < end:
        await asyncio.sleep(5)
        if await finalize_if_paid(pid): return

async def start_payment(target, uid, plan, method):
    u = db.get_user(uid)
    if config.RECEIPT_MODE == "email" and not config.DEV_PAY and not u["email"]:
        WAIT_EMAIL[uid] = (plan, method)
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer("Пришли свой email. На него придёт чек об оплате.")
        return
    try:
        pid, url = await payments.create_invoice(uid, plan, method, u["email"])
    except Exception:
        logging.exception("invoice failed"); await show(target, screens.error(u)); return
    if url is None:
        await show(target, screens.pay_dev(u, pid))
    else:
        asyncio.create_task(watch(pid))
        await show(target, screens.pay_link(u, url, pid))

@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery):
    _, plan, method = c.data.split(":")
    await start_payment(c, c.from_user.id, plan, method); await c.answer()

@dp.callback_query(F.data.startswith("chk:"))
async def cb_chk(c: CallbackQuery):
    if await finalize_if_paid(int(c.data[4:])):
        await c.answer()
    else:
        await c.answer("Оплата пока не найдена. Подожди минуту и нажми снова.", show_alert=True)

@dp.message(F.text, lambda m: m.from_user.id in WAIT_EMAIL)
async def got_email(m: Message):
    email = m.text.strip()
    if not EMAIL_RE.match(email):
        return await m.answer("Не похоже на email. Пришли адрес вида name@mail.ru")
    db.set_email(m.from_user.id, email)
    plan, method = WAIT_EMAIL.pop(m.from_user.id)
    await start_payment(m, m.from_user.id, plan, method)

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

WAIT_PROMO = set()   # пользователи, от которых ждём промокод

def is_admin(m: Message): return m.from_user.id in config.ADMIN_IDS

@dp.message(Command("promo"))
async def cmd_promo(m: Message):
    WAIT_PROMO.add(m.from_user.id)
    await show(m, screens.promo_prompt(db.get_user(m.from_user.id)))

@dp.callback_query(F.data == "promo")
async def cb_promo(c: CallbackQuery):
    WAIT_PROMO.add(c.from_user.id)
    await show(c, screens.promo_prompt(db.get_user(c.from_user.id))); await c.answer()

@dp.message(F.text, lambda m: m.from_user.id in WAIT_PROMO and not m.text.startswith("/"))
async def got_promo(m: Message):
    uid = m.from_user.id; WAIT_PROMO.discard(uid)
    status, days, until = db.use_promo(uid, m.text)
    u = db.get_user(uid)
    if status == "ok":
        await panel.set_expiry(uid, until); await show(m, screens.promo_ok(u, days, until))
    else:
        await show(m, screens.promo_bad(u, status))

@dp.message(Command("newpromo"), is_admin)
async def cmd_newpromo(m: Message, command: CommandObject):
    try:
        a = command.args.split(); code, days = a[0], int(a[1]); uses = int(a[2]) if len(a) > 2 else 0
    except Exception:
        return await m.answer("Формат: /newpromo КОД ДНИ [МАКС_АКТИВАЦИЙ]\nПример: /newpromo WELCOME 7 100")
    db.create_promo(code, days, uses)
    await m.answer(f"Готово: {code.upper()} даёт +{days} дн." + (f", максимум {uses} активаций." if uses else ", без лимита."))

@dp.message(Command("genpromo"), is_admin)
async def cmd_genpromo(m: Message, command: CommandObject):
    """Создаёт пачку одноразовых кодов: /genpromo ДНИ КОЛИЧЕСТВО [ПРЕФИКС]"""
    try:
        a = command.args.split(); days, n = int(a[0]), int(a[1]); prefix = a[2].upper() if len(a) > 2 else "NQ"
        assert 1 <= n <= 200 and days > 0
    except Exception:
        return await m.answer("Формат: /genpromo ДНИ КОЛИЧЕСТВО [ПРЕФИКС]\nПример: /genpromo 7 50 NQ\n(до 200 кодов за раз)")
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = []
    while len(codes) < n:
        code = f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if code not in codes:
            codes.append(code)
    for code in codes:
        db.create_promo(code, days, 1)
    text = f"Создано {n} одноразовых кодов по +{days} дн.:\n\n" + "\n".join(codes)
    for i in range(0, len(text), 3500):
        await m.answer(text[i:i + 3500])

async def _bal_cmd(m: Message, command: CommandObject, kind: str, usage: str):
    try:
        a = command.args.split(); uid, amount = int(a[0]), float(a[1].replace(",", "."))
    except Exception:
        return await m.answer(usage)
    u = db.add_balance(uid, amount, kind)
    await m.answer(f"Готово. Пользователь {uid}:\nОсновной баланс: {u['topup']:.0f} ₽\nРеферальный баланс: {u['balance']:.0f} ₽\nВсего: {db.total_balance(u):.0f} ₽")

@dp.message(Command("addbal"), is_admin)
async def cmd_addbal(m: Message, command: CommandObject):
    await _bal_cmd(m, command, "main", "Формат: /addbal ID_ПОЛЬЗОВАТЕЛЯ СУММА\nПример: /addbal 123456789 500\n(минус в сумме списывает)")

@dp.message(Command("addref"), is_admin)
async def cmd_addref(m: Message, command: CommandObject):
    await _bal_cmd(m, command, "ref", "Формат: /addref ID_ПОЛЬЗОВАТЕЛЯ СУММА\nПример: /addref 123456789 300\n(минус в сумме списывает)")

@dp.message(Command("balance"), is_admin)
async def cmd_balance(m: Message, command: CommandObject):
    try: uid = int(command.args.strip())
    except Exception: uid = m.from_user.id
    u = db.get_user(uid)
    await m.answer(f"Пользователь {uid}:\nОсновной баланс: {u['topup']:.0f} ₽\nРеферальный баланс: {u['balance']:.0f} ₽\nВсего: {db.total_balance(u):.0f} ₽")

@dp.message(Command("delpromo"), is_admin)
async def cmd_delpromo(m: Message, command: CommandObject):
    ok = command.args and db.delete_promo(command.args.strip())
    await m.answer("Удалён." if ok else "Не найден. Формат: /delpromo КОД")

@dp.message(Command("promos"), is_admin)
async def cmd_promos(m: Message):
    rows = db.list_promos()
    await m.answer("\n".join(f"{r['code']}: +{r['days']} дн., использовано {r['used']}" + (f" из {r['max_uses']}" if r['max_uses'] else "") for r in rows) or "Промокодов нет. Создай: /newpromo КОД ДНИ")

@dp.message(Command("stats"), is_admin)
async def cmd_stats(m: Message):
    s = db.stats()
    await m.answer(
        "<b>Статистика</b>\n\n"
        f"Всего пользователей: {s['users']}\n"
        f"За сутки: +{s['new_day']} · за неделю: +{s['new_week']}\n\n"
        f"Подписка активна: {s['active']}\n"
        f"Подписка закончилась: {s['expired']}\n"
        f"Ни разу не подключались: {s['never']}\n"
        f"Брали пробник: {s['trial']}\n"
        f"Пришли по рефералке: {s['refs']}\n"
        f"Активаций промокодов: {s['promo_uses']}\n\n"
        f"Оплат деньгами: {s['pay_n']} от {s['payers']} чел. на {s['pay_sum']:.0f} ₽\n"
        f"Оплат с баланса: {s['bal_sum']:.0f} ₽\n"
        f"Сумма балансов у пользователей: {s['balances']:.0f} ₽")

@dp.message(Command("users"), is_admin)
async def cmd_users(m: Message, command: CommandObject):
    try: page = max(1, int((command.args or "1").strip()))
    except ValueError: page = 1
    size = 30
    total, rows = db.list_users((page - 1) * size, size)
    if not rows:
        return await m.answer(f"Пользователей: {total}. На странице {page} пусто.")
    # те, кого бот ещё не «видел», подтягиваем из Telegram один раз
    for uid in db.users_without_profile([r["id"] for r in rows]):
        try:
            ch = await bot.get_chat(uid)
            db.touch_user(uid, ch.username or "", ((ch.first_name or "") + " " + (ch.last_name or "")).strip() or "(без имени)")
        except Exception:
            db.touch_user(uid, "", "(профиль недоступен)")
    total, rows = db.list_users((page - 1) * size, size)
    now = time.time()
    lines = []
    for u in rows:
        if u["sub_until"] > now:
            st = "до " + time.strftime("%d.%m.%Y", time.localtime(u["sub_until"]))
        elif u["sub_until"]:
            st = "истекла " + time.strftime("%d.%m.%Y", time.localtime(u["sub_until"]))
        else:
            st = "нет подписки"
        name = html.escape(u["name"] or "") or "без имени"
        link = f'https://t.me/{u["username"]}' if u["username"] else f'tg://user?id={u["id"]}'
        who = f'<a href="{link}">{name}</a>' + (f' @{html.escape(u["username"])}' if u["username"] else "")
        lines.append(f'{who} · <code>{u["id"]}</code> · {st}')
    pages = (total + size - 1) // size
    tail = f"\n\nСтраница {page} из {pages}." + (f" Дальше: /users {page + 1}" if page < pages else "")
    await m.answer(f"<b>Пользователи ({total})</b>\n\n" + "\n".join(lines) + tail, disable_web_page_preview=True)

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
        BotCommand(command="promo", description="Ввести промокод"),
        BotCommand(command="support", description="Поддержка")])
    await redirect.start_server()
    asyncio.create_task(reminders())
    for p in db.pending_payments():
        asyncio.create_task(watch(p['id']))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
