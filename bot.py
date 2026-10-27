import asyncio, html, logging, re, time
from datetime import datetime, timedelta, timezone
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, FSInputFile, InputMediaPhoto, Message

import config, db, monitor, panel, payments, redirect, screens

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
        u = getattr(event, "from_user", None)
        if u is not None and u.id not in config.ADMIN_IDS and db.is_banned(u.id):
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ ограничен. Напишите в поддержку.", show_alert=True)
                else:
                    await event.answer("Доступ ограничен. Напишите в поддержку.")
            except Exception:
                pass
            return
        track(u)
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
    if key == "gift": return screens.gift_plans(u)
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
    if command.args and command.args.startswith("gift_"):
        uid = m.from_user.id
        status, days, until = db.use_promo(uid, command.args[5:])
        if status == "ok":
            await panel.set_expiry(uid, until)
            return await show(m, screens.promo_ok(db.get_user(uid), days, until))
        return await show(m, screens.promo_bad(db.get_user(uid), status))
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

@dp.callback_query(F.data.startswith("gbuy:"))
async def cb_gbuy(c: CallbackQuery):
    plan = c.data[5:]
    if plan not in config.PLANS:
        return await c.answer()
    await show(c, screens.payment(db.get_user(c.from_user.id), plan, gift=True)); await c.answer()

@dp.callback_query(F.data.startswith("paybal:") | F.data.startswith("gpaybal:"))
async def cb_paybal(c: CallbackQuery):
    gift = c.data.startswith("g")
    plan = c.data.split(":")[1]
    if plan not in config.PLANS:
        return await c.answer()
    u = db.get_user(c.from_user.id)
    price = db.price_for(u, plan, gift)
    if db.total_balance(u) < price:
        return await c.answer(f"Не хватает {price - db.total_balance(u):.0f} ₽. Выбери другой способ оплаты.", show_alert=True)
    res = db.pay_with_balance(c.from_user.id, plan, gift)
    if res is None:
        return await c.answer("Не хватает средств на балансе.", show_alert=True)
    kind, val = res
    if kind == "gift":
        await show(c, screens.gift_ready(db.get_user(c.from_user.id), val))
    else:
        await panel.set_expiry(c.from_user.id, val)
        await show(c, screens.success(db.get_user(c.from_user.id), val))
    await c.answer("Оплачено с баланса")

WAIT_EMAIL = {}   # user_id -> (plan, method, gift): ждём email для чека

def done_screen(uid, until, code):
    """Экран после оплаты: подарок (код) или обычная подписка."""
    u = db.get_user(uid)
    return screens.gift_ready(u, code) if code else screens.success(u, until)
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
        logging.exception("status check failed")
        await alert_once("yk_status", "🔴 ЮKassa не отвечает на проверку платежа (см. логи). Оплаты могут зависнуть, пока связь не восстановится.")
        return False
    if st == "succeeded":
        res = db.complete_payment(pid)
        if res:
            uid, plan, until, code = res
            if until: await panel.set_expiry(uid, until)
            name, text, markup = done_screen(uid, until, code)
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

async def start_payment(target, uid, plan, method, gift=False):
    u = db.get_user(uid)
    if config.RECEIPT_MODE == "email" and not config.DEV_PAY and not u["email"]:
        WAIT_EMAIL[uid] = (plan, method, gift)
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer("Пришли свой email. На него придёт чек об оплате.")
        return
    try:
        pid, url = await payments.create_invoice(uid, plan, method, u["email"], gift)
    except Exception:
        logging.exception("invoice failed"); await show(target, screens.error(u))
        await alert_once("yk_invoice", "🔴 Не удалось создать счёт в ЮKassa (см. логи). Проверь YK_SHOP_ID, YK_SECRET и баланс магазина.")
        return
    if url is None:
        await show(target, screens.pay_dev(u, pid))
    else:
        asyncio.create_task(watch(pid))
        await show(target, screens.pay_link(u, url, pid))

@dp.callback_query(F.data.startswith("pay:") | F.data.startswith("gpay:"))
async def cb_pay(c: CallbackQuery):
    gift = c.data.startswith("g")
    _, plan, method = c.data.split(":")
    if plan not in config.PLANS:
        return await c.answer()
    await start_payment(c, c.from_user.id, plan, method, gift); await c.answer()

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
    plan, method, gift = WAIT_EMAIL.pop(m.from_user.id)
    await start_payment(m, m.from_user.id, plan, method, gift)

@dp.callback_query(F.data.startswith("devpay:"))
async def cb_devpay(c: CallbackQuery):
    if not config.DEV_PAY:
        return await c.answer("Недоступно", show_alert=True)
    res = db.complete_payment(int(c.data.split(":")[1]))
    if res:
        uid, plan, until, code = res
        if until: await panel.set_expiry(uid, until)
        await show(c, done_screen(uid, until, code))
    await c.answer()

WAIT_PROMO = set()   # пользователи, от которых ждём промокод

def is_admin(m: Message): return m.from_user.id in config.ADMIN_IDS
def can_stats(m: Message): return m.from_user.id in config.ADMIN_IDS or m.from_user.id in config.ACCOUNTANT_IDS      # владелец и бухгалтер
def can_support(m: Message): return m.from_user.id in config.ADMIN_IDS or m.from_user.id in config.SUPPORT_IDS        # владелец и поддержка
def can_staff(m: Message): return can_stats(m) or can_support(m)

def alog(m, action, target="", details=""):
    """Записывает действие админа в журнал (смотреть: /log)."""
    try: db.log_action(m.from_user.id, action, str(target), details)
    except Exception: logging.exception("alog")

async def notify_admins(text):
    for aid in config.ADMIN_IDS:
        try: await bot.send_message(aid, text)
        except Exception: logging.warning("не удалось отправить алерт админу %s", aid)

_ALERTED = {}
async def alert_once(key, text, every=600):
    """Алерт админам не чаще раза в every секунд на один ключ (чтобы ошибки платёжки не заваливали чат)."""
    now = time.time()
    if now - _ALERTED.get(key, 0) < every: return
    _ALERTED[key] = now
    await notify_admins(text)

# ---------- помощники для админских команд ----------
TZ = timezone(timedelta(hours=config.TZ_HOURS))
DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")

def tfmt(ts, fmt="%d.%m.%Y %H:%M"): return datetime.fromtimestamp(ts, TZ).strftime(fmt)
def dfmt(ts): return tfmt(ts, "%d.%m.%Y")

def end_of_day(s):
    """'31.12.2026' -> метка времени конца этого дня в часовом поясе TZ_HOURS. None, если дата кривая."""
    try:
        d, mth, y = (int(x) for x in s.split("."))
        return int(datetime(y, mth, d, 23, 59, 59, tzinfo=TZ).timestamp())
    except Exception:
        return None

def sub_status(u):
    s = u["sub_until"] or 0
    if s > time.time(): return "до " + dfmt(s)
    if s: return "истекла " + dfmt(s)
    return "нет подписки"

def who(u):
    name = html.escape(u["name"] or "") or "без имени"
    link = f'https://t.me/{u["username"]}' if u["username"] else f'tg://user?id={u["id"]}'
    return f'<a href="{link}">{name}</a>' + (f' @{html.escape(u["username"])}' if u["username"] else "")

async def fill_profiles(rows):
    """Подтягивает из Telegram имена тех, кого бот ещё не «видел». Возвращает свежие строки в том же порядке."""
    for uid in db.users_without_profile([r["id"] for r in rows]):
        try:
            ch = await bot.get_chat(uid)
            db.touch_user(uid, ch.username or "", ((ch.first_name or "") + " " + (ch.last_name or "")).strip() or "(без имени)")
        except Exception:
            db.touch_user(uid, "", "(профиль недоступен)")
    return [db.peek_user(r["id"]) or r for r in rows]

async def send_lines(m, header, lines, footer=""):
    """Шлёт длинный список несколькими сообщениями."""
    chunk = header + "\n\n"
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await m.answer(chunk, disable_web_page_preview=True); chunk = ""
        chunk += line + "\n"
    await m.answer(chunk + footer, disable_web_page_preview=True)

def resolve_user(arg):
    """ID или @username -> строка пользователя (без создания). None, если не нашли или неоднозначно."""
    arg = (arg or "").strip()
    if not arg: return None
    if arg.lstrip("-").isdigit(): return db.peek_user(int(arg))
    found = db.find_users(arg)
    exact = [r for r in found if (r["username"] or "").lower() == arg.lstrip("@").lower()]
    if exact: return exact[0]
    return found[0] if len(found) == 1 else None

NOT_FOUND = "Не нашёл такого пользователя. Укажи числовой ID или @username (человек должен был писать боту)."

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
    elif status == "disc":
        await show(m, screens.promo_disc(u))
    else:
        await show(m, screens.promo_bad(u, status))

@dp.message(Command("newpromo"), is_admin)
async def cmd_newpromo(m: Message, command: CommandObject):
    usage = ("Формат: /newpromo КОД ДНИ [МАКС_АКТИВАЦИЙ] [ДО_ДАТЫ]\n"
             "Примеры:\n/newpromo WELCOME 7 100\n/newpromo SALE 14 0 31.12.2026\n"
             "Дата в формате ДД.ММ.ГГГГ, код действует до конца этого дня.")
    try:
        a = command.args.split(); code, days = a[0], int(a[1]); assert days > 0
        uses, exp = 0, 0
        for t in a[2:]:
            if DATE_RE.match(t):
                exp = end_of_day(t); assert exp and exp > time.time()
            else:
                uses = int(t); assert uses >= 0
    except Exception:
        return await m.answer(usage)
    db.create_promo(code, days, uses, exp)
    alog(m, "newpromo", code.upper(), f"+{days} дн., активаций {uses or '∞'}")
    await m.answer(f"Готово: {code.upper()} даёт +{days} дн." + (f", максимум {uses} активаций" if uses else ", без лимита активаций")
                   + (f", действует до {dfmt(exp)} включительно." if exp else ", без срока."))

@dp.message(Command("genpromo"), is_admin)
async def cmd_genpromo(m: Message, command: CommandObject):
    """Создаёт пачку одноразовых кодов: /genpromo ДНИ КОЛИЧЕСТВО [ПРЕФИКС] [ДО_ДАТЫ]"""
    try:
        a = command.args.split(); days, n = int(a[0]), int(a[1]); prefix, exp = "NQ", 0
        for t in a[2:]:
            if DATE_RE.match(t):
                exp = end_of_day(t); assert exp and exp > time.time()
            else:
                prefix = t.upper()
        assert 1 <= n <= 200 and days > 0
    except Exception:
        return await m.answer("Формат: /genpromo ДНИ КОЛИЧЕСТВО [ПРЕФИКС] [ДО_ДАТЫ]\nПример: /genpromo 7 50 NQ 31.12.2026\n(до 200 кодов за раз)")
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = []
    while len(codes) < n:
        code = f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if code not in codes:
            codes.append(code)
    for code in codes:
        db.create_promo(code, days, 1, exp)
    alog(m, "genpromo", f"{n} шт.", f"+{days} дн., префикс {prefix}")
    text = f"Создано {n} одноразовых кодов по +{days} дн." + (f", действуют до {dfmt(exp)}" if exp else "") + ":\n\n" + "\n".join(codes)
    for i in range(0, len(text), 3500):
        await m.answer(text[i:i + 3500])

async def _bal_cmd(m: Message, command: CommandObject, kind: str, usage: str):
    try:
        a = command.args.split(); uid, amount = int(a[0]), float(a[1].replace(",", "."))
    except Exception:
        return await m.answer(usage)
    u = db.add_balance(uid, amount, kind)
    alog(m, "addbal" if kind == "main" else "addref", uid, f"{amount:+.0f} ₽")
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

@dp.message(Command("newdisc"), is_admin)
async def cmd_newdisc(m: Message, command: CommandObject):
    usage = ("Формат: /newdisc КОД СКИДКА [МАКС_АКТИВАЦИЙ] [ДО_ДАТЫ]\n"
             "Примеры:\n/newdisc SALE20 20% 100\n/newdisc MINUS100 100 0 31.12.2026\n"
             "Скидка с % в процентах, без % в рублях. Человек вводит код как обычный промокод, и скидка применяется к его следующей оплате подписки.")
    try:
        a = command.args.split(); code, raw = a[0], a[1]
        kind = "percent" if raw.endswith("%") else "rub"
        val = float(raw.rstrip("%").replace(",", "."))
        assert 0 < val <= (95 if kind == "percent" else 5000)
        uses, exp = 0, 0
        for t in a[2:]:
            if DATE_RE.match(t):
                exp = end_of_day(t); assert exp and exp > time.time()
            else:
                uses = int(t); assert uses >= 0
    except Exception:
        return await m.answer(usage)
    db.create_promo(code, 0, uses, exp, kind, val)
    alog(m, "newdisc", code.upper(), f"{val:.0f}{'%' if kind == 'percent' else ' ₽'}, активаций {uses or '∞'}")
    await m.answer(f"Готово: {code.upper()} даёт скидку {val:.0f}{'%' if kind == 'percent' else ' ₽'}"
                   + (f", максимум {uses} активаций" if uses else ", без лимита активаций")
                   + (f", действует до {dfmt(exp)} включительно." if exp else ", без срока."))

@dp.message(Command("abandoned"), can_stats)
async def cmd_abandoned(m: Message):
    rows = db.abandoned_list(int(time.time()))
    if not rows:
        return await m.answer("За последние сутки брошенных оплат нет.")
    lines = []
    for r in rows:
        plan = config.PLANS.get(r["plan"], (0, 0, r["plan"]))[2]
        lines.append(f'{tfmt(r["created"], "%d.%m %H:%M")} · {r["amount"]} ₽ · {plan}' + (" · подарок" if r["gift"] else "")
                     + (" · напомнили" if r["nudged"] else "") + f'\n   {who(r)} · <code>{r["user_id"]}</code>')
    await send_lines(m, f"<b>Брошенные оплаты за сутки ({len(rows)})</b>", lines)

@dp.message(Command("auto"), is_admin)
async def cmd_auto(m: Message):
    names = {"welcome": "приветствие через час", "day3": "«как дела?» на 3-й день", "miss": "«скучаем» через неделю после конца"}
    rows = db.auto_counts()
    text = "<b>Автосообщения</b> " + ("включены" if config.AUTO_MSGS else "выключены (AUTO_MSGS=0)") + "\n\n"
    text += "\n".join(f"{names.get(r['k'], r['k'])}: отправлено {r['c']}" for r in rows) if rows else "Пока ничего не отправлено."
    await m.answer(text)

@dp.message(Command("delpromo"), is_admin)
async def cmd_delpromo(m: Message, command: CommandObject):
    ok = command.args and db.delete_promo(command.args.strip())
    if ok: alog(m, "delpromo", command.args.strip().upper())
    await m.answer("Удалён." if ok else "Не найден. Формат: /delpromo КОД")

@dp.message(Command("delallpromo"), is_admin)
async def cmd_delallpromo(m: Message, command: CommandObject):
    """/delallpromo [used] [да] : без «да» только показывает, сколько будет удалено."""
    a = (command.args or "").lower().split()
    only_spent = "used" in a
    n = db.count_promos(only_spent)
    what = "полностью использованных промокодов" if only_spent else "ВСЕХ промокодов"
    if "да" not in a:
        if n == 0:
            return await m.answer("Удалять нечего: подходящих промокодов нет.")
        cmd = "/delallpromo " + ("used " if only_spent else "") + "да"
        return await m.answer(f"Будет удалено {what}: {n}.\nУже активированные дни у людей останутся.\n\nПодтвердить: {cmd}")
    cnt = db.delete_all_promos(only_spent)
    alog(m, "delallpromo", f"{cnt} шт.", "только использованные" if only_spent else "все")
    await m.answer(f"Удалено {what}: {cnt}.")

@dp.message(Command("promos"), is_admin)
async def cmd_promos(m: Message):
    rows = db.list_promos()
    if not rows:
        return await m.answer("Промокодов нет. Создай: /newpromo КОД ДНИ")
    lines = []
    for r in rows:
        exp = r["expires"] or 0
        tail = "" if not exp else (f", до {dfmt(exp)}" if exp > time.time() else f", истёк {dfmt(exp)}")
        what = (f"+{r['days']} дн." if (r["kind"] or "days") == "days"
                else f"скидка {r['value']:.0f}%" if r["kind"] == "percent" else f"скидка {r['value']:.0f} ₽")
        lines.append(f"<code>{r['code']}</code>: {what}, использовано {r['used']}" + (f" из {r['max_uses']}" if r['max_uses'] else "") + tail)
    await send_lines(m, f"<b>Промокоды ({len(rows)})</b>", lines)

@dp.message(Command("stats"), can_stats)
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
        f"Сумма балансов у пользователей: {s['balances']:.0f} ₽"
        + (f"\nЗаблокировано: {s['banned']}" if s['banned'] else ""))

@dp.message(Command("users"), can_support)
async def cmd_users(m: Message, command: CommandObject):
    try: page = max(1, int((command.args or "1").strip()))
    except ValueError: page = 1
    size = 30
    total, rows = db.list_users((page - 1) * size, size)
    if not rows:
        return await m.answer(f"Пользователей: {total}. На странице {page} пусто.")
    rows = await fill_profiles(rows)
    lines = [f'{who(u)} · <code>{u["id"]}</code> · {sub_status(u)}' + (" · ⛔ бан" if u["banned"] else "") for u in rows]
    pages = (total + size - 1) // size
    tail = f"\nСтраница {page} из {pages}." + (f" Дальше: /users {page + 1}" if page < pages else "")
    await send_lines(m, f"<b>Пользователи ({total})</b>", lines, tail)

@dp.message(Command("find"), can_support)
async def cmd_find(m: Message, command: CommandObject):
    q = (command.args or "").strip()
    if not q:
        return await m.answer("Формат: /find ИМЯ, @ник или ID\nПример: /find вася")
    rows = db.find_users(q)
    unknown = db.count_unknown_profiles()
    note = f"\nУ {unknown} чел. имя ещё не известно, они найдутся по ID или после /users." if unknown else ""
    if not rows:
        return await m.answer("Никого не нашёл." + note)
    lines = [f'{who(u)} · <code>{u["id"]}</code> · {sub_status(u)}' + (" · ⛔ бан" if u["banned"] else "") for u in rows]
    await send_lines(m, f"<b>Найдено: {len(rows)}</b>", lines, note)

@dp.message(Command("user"), can_support)
async def cmd_user(m: Message, command: CommandObject):
    if not (command.args or "").strip():
        return await m.answer("Формат: /user ID или @ник\nПример: /user 123456789")
    u = resolve_user(command.args)
    if not u:
        return await m.answer(NOT_FOUND)
    u = (await fill_profiles([u]))[0]
    uid = u["id"]
    n_fr, earned, _ = db.friends_stats(uid)
    pay_n, pay_sum = db.user_pay_totals(uid)
    L = [f'{who(u)}', f'ID: <code>{uid}</code>', f'Подписка: {sub_status(u)}']
    if u["banned"]: L.append("⛔ Заблокирован")
    L.append(f'Пробник: {"использован" if u["trial_used"] else "не брал"}')
    L.append(f'Регистрация: {tfmt(u["created"])}' if u["created"] else "Регистрация: неизвестно")
    L.append(f'Баланс: основной {u["topup"] or 0:.0f} ₽, реферальный {u["balance"] or 0:.0f} ₽')
    if u["ref"]:
        r = db.peek_user(u["ref"])
        L.append("Пригласил: " + (who(r) if r and (r["name"] or r["username"]) else f'<code>{u["ref"]}</code>'))
    L.append(f'Привёл друзей: {n_fr}, заработал {earned:.0f} ₽')
    L.append(f'Промокодов активировано: {db.promo_use_count(uid)}')
    L.append(f'Оплат деньгами: {pay_n} на {pay_sum:.0f} ₽')
    pays = db.user_payments(uid, 5)
    if pays:
        L.append("\nПоследние оплаты:")
        for p in pays:
            plan = config.PLANS.get(p["plan"], (0, 0, p["plan"]))[2]
            L.append(f'• {tfmt(p["created"])} · {p["amount"]} ₽ · {plan} · {"с баланса" if p["method"] == "balance" else p["method"]}')
    info = await panel.get_info(uid)
    if info:
        L.append("\nПанель:")
        L.append(f'Статус: {info.get("status", "?")}')
        tr = (info.get("userTraffic") or {}).get("usedTrafficBytes", info.get("usedTrafficBytes"))
        if tr is not None:
            L.append(f'Трафик: {tr / 1024 ** 3:.2f} ГБ')
        if info.get("hwidDeviceLimit") is not None:
            L.append(f'Лимит устройств: {info["hwidDeviceLimit"]}')
    elif config.PANEL_URL:
        L.append("\nПанель: пользователя там нет или она недоступна.")
    await m.answer("\n".join(L), disable_web_page_preview=True)

@dp.message(Command("give"), can_support)
async def cmd_give(m: Message, command: CommandObject):
    usage = "Формат: /give ID_или_@ник ДНИ [тихо]\nПример: /give 123456789 7\n«тихо» = не писать человеку."
    a = (command.args or "").split()
    try:
        days = int(a[1]); assert 1 <= days <= 3650
    except Exception:
        return await m.answer(usage)
    if m.from_user.id not in config.ADMIN_IDS and days > config.SUPPORT_GIVE_MAX:
        return await m.answer(f"Поддержка может выдавать не больше {config.SUPPORT_GIVE_MAX} дн. за раз.")
    u = resolve_user(a[0])
    if not u:
        return await m.answer(NOT_FOUND)
    until = db.extend(u["id"], days)
    alog(m, "give", u["id"], f"+{days} дн.")
    await panel.set_expiry(u["id"], until)
    sent = ""
    if "тихо" not in [x.lower() for x in a[2:]]:
        try:
            await bot.send_message(u["id"], f"Вам начислено +{days} дн. Подписка активна до {dfmt(until)}.")
            sent = " Человеку отправлено сообщение."
        except Exception:
            sent = " Сообщение человеку не дошло."
    await m.answer(f"Готово: {u['id']} +{days} дн., подписка до {dfmt(until)}." + sent)

async def _ban(m: Message, command: CommandObject, flag: bool):
    word = "ban" if flag else "unban"
    if not (command.args or "").strip():
        return await m.answer(f"Формат: /{word} ID или @ник")
    first = command.args.strip().split()[0]
    if flag and first.lstrip("-").isdigit() and int(first) in config.ADMIN_IDS:
        return await m.answer("Админа заблокировать нельзя.")
    u = resolve_user(command.args)
    if not u:
        return await m.answer(NOT_FOUND)
    if flag and u["id"] in config.ADMIN_IDS:
        return await m.answer("Админа заблокировать нельзя.")
    db.set_banned(u["id"], flag)
    alog(m, "ban" if flag else "unban", u["id"])
    try:
        await panel.set_banned(u["id"])
        tail = " В панели доступ " + ("отключён." if flag else "восстановлен по сроку подписки.")
    except Exception:
        logging.exception("panel ban failed")
        tail = " В панели не получилось: проверь вручную."
    await m.answer(("⛔ Заблокирован: " if flag else "✅ Разблокирован: ") + f'{who(u)} · <code>{u["id"]}</code>.' + tail, disable_web_page_preview=True)

@dp.message(Command("ban"), is_admin)
async def cmd_ban(m: Message, command: CommandObject): await _ban(m, command, True)

@dp.message(Command("unban"), is_admin)
async def cmd_unban(m: Message, command: CommandObject): await _ban(m, command, False)

@dp.message(Command("payments"), can_stats)
async def cmd_payments(m: Message, command: CommandObject):
    try: n = min(40, max(1, int((command.args or "15").strip())))
    except ValueError: n = 15
    rows = db.recent_payments(n)
    if not rows:
        return await m.answer("Оплат пока нет.")
    lines = []
    for r in rows:
        plan = config.PLANS.get(r["plan"], (0, 0, r["plan"]))[2]
        how = "с баланса" if r["method"] == "balance" else r["method"]
        lines.append(f'{tfmt(r["created"], "%d.%m %H:%M")} · {r["amount"]} ₽ · {plan} · {how}\n   {who(r)} · <code>{r["id"]}</code>')
    await send_lines(m, f"<b>Последние оплаты ({len(rows)})</b>", lines)

@dp.message(Command("expiring"), can_support)
async def cmd_expiring(m: Message, command: CommandObject):
    try: days = min(30, max(1, int((command.args or "3").strip())))
    except ValueError: days = 3
    rows = db.expiring_users(days)
    if not rows:
        return await m.answer(f"В ближайшие {days} дн. подписки ни у кого не заканчиваются.")
    rows = await fill_profiles(rows)
    lines = [f'{who(u)} · <code>{u["id"]}</code> · до {tfmt(u["sub_until"])}' for u in rows]
    await send_lines(m, f"<b>Заканчивается в ближайшие {days} дн.: {len(rows)}</b>", lines)

SEGMENTS = {"all": "всем", "active": "с активной подпиской", "expired": "с закончившейся подпиской", "never": "без подписки"}
BCAST = {}          # id админа -> (сегмент, текст), ждёт подтверждения
BCAST_BUSY = False

@dp.message(Command("broadcast"), is_admin)
async def cmd_broadcast(m: Message, command: CommandObject):
    usage = ("Формат: /broadcast [СЕГМЕНТ] ТЕКСТ\nСегменты: all (всем, по умолчанию), active (с подпиской), "
             "expired (подписка закончилась), never (без подписки).\n"
             "Пример: /broadcast active Завтра работы на серверах, возможны перебои.\n"
             "Поддерживается HTML: <b>жирный</b>, <i>курсив</i>. Сначала придёт предпросмотр.")
    raw = (command.args or "").strip()
    seg, text = "all", raw
    parts = raw.split(None, 1)
    if parts and parts[0].lower() in SEGMENTS:
        seg, text = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    if not text.strip() or len(text) > 3800:
        return await m.answer(usage)
    try:
        await m.answer(text, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        return await m.answer("Ошибка в оформлении текста: " + html.escape(str(e)) + "\nПроверь HTML-теги или убери их.")
    BCAST[m.from_user.id] = (seg, text)
    n = len(db.segment_ids(seg))
    await m.answer(f"☝️ Предпросмотр. Получат {SEGMENTS[seg]}: {n} чел.\n\nОтправить: /broadcastok\nОтменить: /broadcastcancel")

@dp.message(Command("broadcastcancel"), is_admin)
async def cmd_broadcastcancel(m: Message):
    await m.answer("Рассылка отменена." if BCAST.pop(m.from_user.id, None) else "Нечего отменять.")

async def _broadcast(admin_id, ids, text):
    global BCAST_BUSY
    BCAST_BUSY = True
    ok = blocked = failed = 0
    try:
        for uid in ids:
            for attempt in (1, 2):
                try:
                    await bot.send_message(uid, text, disable_web_page_preview=True); ok += 1; break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    if attempt == 2: failed += 1
                except TelegramForbiddenError:
                    blocked += 1; break
                except Exception:
                    failed += 1; break
            await asyncio.sleep(0.05)   # около 20 сообщений в секунду, в пределах лимита Telegram
    finally:
        BCAST_BUSY = False
    try:
        await bot.send_message(admin_id, f"Рассылка закончена.\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибок: {failed}")
    except Exception:
        logging.exception("broadcast report")

@dp.message(Command("broadcastok"), is_admin)
async def cmd_broadcastok(m: Message):
    if BCAST_BUSY:
        return await m.answer("Другая рассылка ещё идёт. Дождись отчёта.")
    pending = BCAST.pop(m.from_user.id, None)
    if not pending:
        return await m.answer("Нет подготовленной рассылки. Сначала: /broadcast ТЕКСТ")
    seg, text = pending
    ids = db.segment_ids(seg)
    if not ids:
        return await m.answer("В этом сегменте никого нет.")
    alog(m, "broadcast", f"{len(ids)} чел.", f"сегмент {seg}: {text[:80]}")
    asyncio.create_task(_broadcast(m.from_user.id, ids, text))
    await m.answer(f"Рассылка запущена: {len(ids)} чел. Примерно {max(1, len(ids) // 20)} сек. Отчёт пришлю сюда.")

ADMIN_HELP = (
    "<b>Команды админа</b>\n\n"
    "<b>Люди</b>\n"
    "/stats: сводка\n/users [стр]: список\n/find ЗАПРОС: поиск по имени, @нику или ID\n"
    "/user ID: карточка человека\n/expiring [дни]: у кого скоро конец подписки\n/payments [N]: последние оплаты\n\n"
    "<b>Управление</b>\n"
    "/give ID ДНИ [тихо]: выдать дни\n/ban ID и /unban ID: заблокировать и разблокировать\n"
    "/addbal ID СУММА и /addref ID СУММА: баланс\n/balance [ID]: показать баланс\n\n"
    "<b>Промокоды</b>\n"
    "/newpromo КОД ДНИ [АКТИВАЦИЙ] [ДД.ММ.ГГГГ]\n/newdisc КОД 20% (или 100 ₽) [АКТИВАЦИЙ] [ДД.ММ.ГГГГ]: скидка\n/genpromo ДНИ КОЛ-ВО [ПРЕФИКС] [ДД.ММ.ГГГГ]\n"
    "/promos: список\n/delpromo КОД\n/delallpromo [used] да: удалить все или только использованные\n\n"
    "<b>Продажи</b>\n"
    "/abandoned: кто начал оплату и не оплатил\n/auto: сколько автосообщений отправлено\n\n"
    "<b>Рассылка</b>\n"
    "/broadcast [all|active|expired|never] ТЕКСТ: предпросмотр\n/broadcastok: отправить\n/broadcastcancel: отменить\n\n"
    "<b>Контроль</b>\n"
    "/report: отчёт за сутки (утром приходит сам)\n/log [N]: журнал действий админов и поддержки\n"
    "Алерты о падении панели, нод и ошибках ЮKassa приходят сами.\n"
    "Роли: SUPPORT_IDS (поддержка) и ACCOUNTANT_IDS (бухгалтер) в .env.")
SUPPORT_HELP = ("<b>Тебе доступна поддержка</b>\n\n/find ЗАПРОС: поиск по имени, @нику или ID\n/user ID: карточка человека\n"
                "/users [стр]: список\n/expiring [дни]: у кого скоро конец подписки\n"
                f"/give ID ДНИ [тихо]: выдать дни (до {config.SUPPORT_GIVE_MAX} за раз)")
ACCOUNTANT_HELP = "<b>Тебе доступна бухгалтерия</b>\n\n/stats: сводка\n/payments [N]: последние оплаты\n/abandoned: брошенные оплаты\n/report: отчёт за сутки"

def _ago(ts): return tfmt(ts, "%d.%m %H:%M")

@dp.message(Command("log"), is_admin)
async def cmd_log(m: Message, command: CommandObject):
    try: n = min(50, max(1, int((command.args or "20").strip())))
    except ValueError: n = 20
    rows = db.recent_log(n)
    if not rows:
        return await m.answer("Журнал пока пуст.")
    lines = [f'{_ago(r["ts"])} · <b>{html.escape(r["action"])}</b> · {html.escape(r["target"] or "-")}'
             + (f' · {html.escape(r["details"])}' if r["details"] else "") + f'\n   {who(r)}' for r in rows]
    await send_lines(m, f"<b>Журнал действий ({len(rows)})</b>", lines)

async def build_report():
    now = int(time.time())
    r = db.report_data(now)
    text = (f"<b>Отчёт за сутки</b> · {tfmt(now)}\n\n"
            f"Новых пользователей: +{r['new']}\n"
            f"Оплат: {r['pay_n']} на {r['pay_sum']:.0f} ₽" + (f" (из них подарков: {r['gifts']})" if r['gifts'] else "") + "\n"
            f"Оплат с баланса: {r['bal_sum']:.0f} ₽\n"
            f"Активаций промокодов: {r['promo']}\n"
            f"Брошенных оплат: {r['abandoned']}\n\n"
            f"Подписка активна: {r['active']}\n"
            f"Закончится в ближайшие сутки: {r['exp_day']} · за 3 дня: {r['exp_3d']}\n\n")
    if not config.PANEL_URL:
        return text + "Панель не подключена."
    try:
        nodes = await panel.get_nodes()
        text += "Панель: отвечает\n"
        for n in nodes:
            ok = n.get("isDisabled") or n.get("isConnected")
            state = "отключена вручную" if n.get("isDisabled") else ("в сети" if n.get("isConnected") else "НЕ В СЕТИ")
            text += f"{'✅' if ok else '🔴'} {html.escape(str(n.get('name') or n.get('address') or '?'))}: {state}\n"
    except Exception as e:
        text += f"🔴 Панель не отвечает: {html.escape(str(e))}"
    return text

@dp.message(Command("report"), can_stats)
async def cmd_report(m: Message):
    await m.answer(await build_report())

async def morning_report():
    """Раз в сутки после REPORT_HOUR (по TZ_HOURS) шлёт отчёт владельцу и бухгалтеру. Не повторяется после перезапуска."""
    if config.REPORT_HOUR < 0: return
    while True:
        try:
            now = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            if now.hour >= config.REPORT_HOUR and db.kv_get("last_report") != today:
                db.kv_set("last_report", today)
                text = await build_report()
                for aid in config.ADMIN_IDS | config.ACCOUNTANT_IDS:
                    try: await bot.send_message(aid, text)
                    except Exception: logging.warning("отчёт не дошёл до %s", aid)
        except Exception:
            logging.exception("morning_report")
        await asyncio.sleep(300)

@dp.message(Command("admin"), can_staff)
async def cmd_admin(m: Message):
    uid = m.from_user.id
    if uid in config.ADMIN_IDS:
        return await m.answer(ADMIN_HELP)
    parts = ([SUPPORT_HELP] if uid in config.SUPPORT_IDS else []) + ([ACCOUNTANT_HELP] if uid in config.ACCOUNTANT_IDS else [])
    await m.answer("\n\n".join(parts))

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

async def safe_send(uid, scr):
    """Шлёт экран; False, если человек заблокировал бота или отправка не удалась."""
    name, text, markup = scr
    try:
        msg = await bot.send_photo(uid, photo(name), caption=text, reply_markup=markup)
        remember(name, msg)
        return True
    except TelegramForbiddenError:
        return False
    except Exception:
        logging.exception("auto send failed (uid=%s)", uid)
        return False

async def autos():
    """Каждые 5 минут: напоминания о брошенной оплате и автосообщения (приветствие, «как дела?», «скучаем»)."""
    while True:
        try:
            now = int(time.time())
            for p in db.abandoned_to_nudge(now, config.ABANDON_AFTER_MIN * 60):
                db.mark_nudged(p["user_id"])
                if not p["gift"]:
                    await safe_send(p["user_id"], screens.abandoned(db.get_user(p["user_id"]), p["plan"]))
                else:
                    await safe_send(p["user_id"], screens.gift_plans(db.get_user(p["user_id"])))
                await asyncio.sleep(0.05)
            for kind, scr in (("welcome", screens.auto_welcome), ("day3", screens.auto_day3), ("miss", screens.auto_miss)):
                for u in db.auto_candidates(kind, now):
                    db.mark_auto(u["id"], db.auto_key(kind, u))   # сначала отмечаем, чтобы не слать дважды при сбое
                    await safe_send(u["id"], scr(u))
                    await asyncio.sleep(0.05)
        except Exception:
            logging.exception("autos loop")
        await asyncio.sleep(300)

async def main():
    logging.basicConfig(level=logging.INFO)
    public_cmds = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="connect", description="Подключиться"),
        BotCommand(command="plans", description="Тарифы"),
        BotCommand(command="profile", description="Профиль"),
        BotCommand(command="friends", description="Пригласить друга"),
        BotCommand(command="promo", description="Ввести промокод"),
        BotCommand(command="support", description="Поддержка")]
    await bot.set_my_commands(public_cmds)
    admin_cmds = public_cmds + [
        BotCommand(command="admin", description="Команды админа"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="users", description="Список пользователей"),
        BotCommand(command="find", description="Найти человека"),
        BotCommand(command="user", description="Карточка человека"),
        BotCommand(command="expiring", description="Скоро закончится подписка"),
        BotCommand(command="payments", description="Последние оплаты"),
        BotCommand(command="give", description="Выдать дни"),
        BotCommand(command="broadcast", description="Рассылка")]
    for aid in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            logging.warning("не удалось задать меню админу %s (он ещё не писал боту?)", aid)
    role_cmds = {
        "support": [BotCommand(command="admin", description="Команды"), BotCommand(command="find", description="Найти человека"),
                    BotCommand(command="user", description="Карточка человека"), BotCommand(command="expiring", description="Скоро конец подписки"),
                    BotCommand(command="give", description="Выдать дни")],
        "accountant": [BotCommand(command="admin", description="Команды"), BotCommand(command="stats", description="Статистика"),
                       BotCommand(command="payments", description="Последние оплаты"), BotCommand(command="report", description="Отчёт за сутки")]}
    for ids, key in ((config.SUPPORT_IDS - config.ADMIN_IDS, "support"), (config.ACCOUNTANT_IDS - config.ADMIN_IDS, "accountant")):
        for aid in ids:
            try:
                await bot.set_my_commands(public_cmds + role_cmds[key], scope=BotCommandScopeChat(chat_id=aid))
            except Exception:
                logging.warning("не удалось задать меню для %s", aid)
    await redirect.start_server()
    asyncio.create_task(reminders())
    asyncio.create_task(monitor.run(notify_admins))
    asyncio.create_task(morning_report())
    if config.AUTO_MSGS:
        asyncio.create_task(autos())
    for p in db.pending_payments():
        asyncio.create_task(watch(p['id']))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
