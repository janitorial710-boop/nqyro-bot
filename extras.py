"""Дополнения владельца: устройства, вывод денег, обращения и FAQ, опрос «почему не продлил», техработы, админ-команды.

Подключается из bot.py: extras.init(модуль_bot), dp.include_router(extras.router), extras.start_loops().
Всё, что нужно от bot.py (show, who, resolve_user и т.д.), берётся через B. Свои таблицы создаются здесь же.
"""
import asyncio, csv, html, io, logging, re, time
from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import config, db, panel, screens

router = Router()
B = None   # модуль bot.py, заполняется в init()
con = db.con

def init(mod):
    global B
    B = mod
    con.executescript("""
    CREATE TABLE IF NOT EXISTS payouts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, method TEXT,
        details TEXT, status TEXT, created INTEGER, done_at INTEGER, reason TEXT);
    CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, created INTEGER,
        closed_at INTEGER, rating INTEGER);
    CREATE TABLE IF NOT EXISTS ticket_msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, staff INTEGER, text TEXT, ts INTEGER);
    CREATE TABLE IF NOT EXISTS survey(user_id INTEGER PRIMARY KEY, asked_at INTEGER, answer TEXT, answered_at INTEGER);
    CREATE TABLE IF NOT EXISTS user_notes(user_id INTEGER PRIMARY KEY, note TEXT, ts INTEGER);
    """)
    con.commit()
    B.dp.message.outer_middleware(Maintenance())
    B.dp.callback_query.outer_middleware(Maintenance())
    B.dp.message.outer_middleware(ClearWaits())
    B.dp.callback_query.outer_middleware(ClearWaits())

# ---------- права и общие мелочи ----------
def is_owner(m): return m.from_user.id in config.ADMIN_IDS
def is_support(m): return m.from_user.id in config.ADMIN_IDS or m.from_user.id in config.SUPPORT_IDS
def support_ids(): return set(config.ADMIN_IDS) | set(config.SUPPORT_IDS)
def staff_all(): return set(config.ADMIN_IDS) | set(config.SUPPORT_IDS) | set(config.ACCOUNTANT_IDS)

async def tell(ids, text, markup=None):
    for i in ids:
        try:
            await B.bot.send_message(i, text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            logging.warning("не удалось отправить сообщение сотруднику %s", i)

def esc(s): return html.escape(str(s if s is not None else ""))
def rub(x): return f"{x:,.0f}".replace(",", " ")
def args_of(command): return (command.args or "").split()

def kb(*rows): return screens.kb(*rows)

async def answer_cb(c, text=None, alert=False):
    try: await c.answer(text, show_alert=alert)
    except Exception: pass

# ---------- ожидание текста от человека: состояния и их сброс ----------
WD = {}        # id человека -> состояние мастера вывода
TK_WAIT = {}   # id человека -> id обращения (0 = новое), ждём текст обращения
TK_REPLY = {}  # id сотрудника -> (id обращения, время): ждём текст ответа
WD_REJ = {}    # id владельца -> (id заявки, время): ждём причину отказа
XNOTE = {}     # id владельца -> (id человека, время): ждём текст заметки
MY_PREFIX = ("wd:", "tk:", "tkr:", "tkc:", "wdp:", "dev:", "sv:", "faq:", "x:")

def clear_waits(uid):
    for d in (WD, TK_WAIT, TK_REPLY, WD_REJ, XNOTE): d.pop(uid, None)

class ClearWaits(BaseMiddleware):
    """Если человек нажал чужую кнопку или написал команду, наши ожидания текста сбрасываются."""
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u is not None:
            if isinstance(event, CallbackQuery):
                if not (event.data or "").startswith(MY_PREFIX): clear_waits(u.id)
            elif (getattr(event, "text", None) or "").startswith("/"):
                clear_waits(u.id)
        return await handler(event, data)

# ================= Режим техработ =================
def maint_on(): return db.kv_get("maint") == "1"
def maint_text(): return db.kv_get("maint_text") or "Идут технические работы. Скоро вернёмся."
_MAINT_SEEN = {}

class Maintenance(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u is None or not maint_on() or u.id in staff_all():
            return await handler(event, data)
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(maint_text()[:190], show_alert=True)
            elif time.time() - _MAINT_SEEN.get(u.id, 0) > 600:
                _MAINT_SEEN[u.id] = time.time()
                await B.show(event, ("27_maint.png", "<b>Технические работы</b>\n\n" + esc(maint_text()), None))
        except Exception:
            logging.exception("maintenance")
        return None

@router.message(Command("maintenance"), is_owner)
async def cmd_maintenance(m: Message, command: CommandObject):
    a = (command.args or "").strip()
    word = a.split(None, 1)[0].lower() if a else ""
    if word == "on":
        text = a.split(None, 1)[1] if len(a.split(None, 1)) > 1 else "Идут технические работы. Скоро вернёмся."
        db.kv_set("maint", "1"); db.kv_set("maint_text", text); B.alog(m, "maintenance", "on", text[:100])
        return await m.answer(f"Режим техработ включён. Обычным пользователям бот отвечает:\n\n{esc(text)}\n\nВыключить: /maintenance off")
    if word == "off":
        db.kv_set("maint", ""); B.alog(m, "maintenance", "off")
        return await m.answer("Режим техработ выключен.")
    await m.answer(("Сейчас включён.\n" if maint_on() else "Сейчас выключен.\n")
                   + "Включить: /maintenance on [ТЕКСТ]\nВыключить: /maintenance off\nВладелец, поддержка и бухгалтер пользуются ботом как обычно.")

# ================= Устройства (для человека) =================
def _dev_name(d_):
    return " ".join(x for x in (d_.get("platform"), d_.get("deviceModel"), d_.get("osVersion")) if x) or "без названия"

def _why(uid, e):
    """Причина сбоя для всплывающего уведомления: видна только владельцу."""
    return (f"\n\nПричина: {e}"[:150]) if uid in config.ADMIN_IDS else ""

def _dh(hwid):
    """Короткая метка устройства для кнопки (в callback_data нельзя длинный hwid)."""
    import hashlib
    return hashlib.sha1(str(hwid).encode()).hexdigest()[:10]

def _dev_since(d_):
    s = str(d_.get("createdAt") or "")[:10]   # 2026-10-12 -> 12.10
    return f"{s[8:10]}.{s[5:7]}" if len(s) == 10 and s[4] == "-" else ""

async def dev_screen(uid, note=""):
    """Экран «Устройства». Если панель не ответила, показывает понятное сообщение."""
    try:
        devs = await panel.hwid_list(uid)
    except Exception as e:
        logging.warning("devices uid=%s: %s", uid, e)
        if "ещё нет в панели" in str(e) or "не настроена" in str(e):
            text = ("<b>Устройства</b>\n\nПока нечего показывать: сначала нажми «Подключиться» и добавь подписку в приложение.")
        else:
            text = ("<b>Устройства</b>\n\nНе получилось получить список устройств: сервер подключения не ответил. "
                    "Нажми «Обновить» через минуту. Если не пройдёт, напиши в поддержку.")
        if uid in config.ADMIN_IDS: text += f"\n\nПричина (видно только тебе): {esc(e)}"
        return ("22_devices.png", text, kb([("Обновить", "dev:open")], [("Назад", "go:profile")]))
    try: limit = await panel.hwid_limit(uid)
    except Exception: limit = config.HWID_LIMIT
    L = [f"<b>Устройства: {len(devs)} из {limit}</b>", ""]
    L += [f"{i}. {esc(_dev_name(x))}" + (f" · с {_dev_since(x)}" if _dev_since(x) else "") for i, x in enumerate(devs, 1)] or ["Пока ни одно устройство не подключено."]
    L += ["", "Чтобы убрать чужое или старое устройство, нажми на него ниже. Оно отвяжется и будет заблокировано: снова подключиться оно не сможет, пока ты сам не разрешишь.",
              "Если подпиской пользуется тот, кому ты её не давал, напиши в поддержку: мы выпустим новую ссылку."]
    if note: L = [note, ""] + L
    rows = [[(f"Отвязать {i}. {_dev_name(x)[:30]}", f"dev:rm:{_dh(x.get('hwid'))}")] for i, x in enumerate(devs[:10], 1)]
    blocked = db.blocked_list(uid)
    if blocked:
        L += ["", "<b>Заблокированные</b>: они не смогут снова подключиться, пока ты не разрешишь:"] + [f"• {esc(b_['name'] or 'устройство')}" for b_ in blocked[:10]]
        rows += [[(f"Разрешить: {(b_['name'] or 'устройство')[:30]}", f"dev:al:{_dh(b_['hwid'])}")] for b_ in blocked[:10]]
    rows += [[("Сбросить все", "dev:ask"), ("Обновить", "dev:open")]] if devs else [[("Обновить", "dev:open")]]
    rows += [[("Назад", "go:profile")]]
    return ("22_devices.png", "\n".join(L), kb(*rows))

@router.callback_query(F.data.startswith("dev:rm:"))
async def cb_dev_rm(c: CallbackQuery):
    uid = c.from_user.id; h = c.data.split(":", 2)[2]
    last = int(db.kv_get(f"devrm:{uid}") or 0)
    if time.time() - last < 5:
        return await answer_cb(c, "Подожди пару секунд.", False)
    hit = None
    try:
        devs = await panel.hwid_list(uid)
        hit = next((x for x in devs if _dh(x.get("hwid")) == h), None)
        if hit:
            db.block_device(uid, hit["hwid"], _dev_name(hit))   # сначала в чёрный список: даже если удаление не пройдёт, цикл проверки добьёт
            await panel.hwid_delete(uid, hit["hwid"])
    except Exception as e:
        logging.warning("device remove uid=%s: %s", uid, e)
        if hit: db.unblock_device(uid, hit["hwid"])   # не вышло отвязать: не держим его в чёрном списке, чтобы сообщение об ошибке не врало
        return await answer_cb(c, "Не получилось отвязать. Попробуй ещё раз или напиши в поддержку." + _why(uid, e), True)
    db.kv_set(f"devrm:{uid}", str(int(time.time())))
    await answer_cb(c, "Готово" if hit else "Это устройство уже отвязано")
    await B.show(c, await dev_screen(uid, f"Отвязано и заблокировано: {esc(_dev_name(hit))}. Если оно снова появится, бот уберёт его в течение минуты." if hit else "Это устройство уже было отвязано."))

@router.callback_query(F.data.startswith("dev:al:"))
async def cb_dev_allow(c: CallbackQuery):
    uid = c.from_user.id; h = c.data.split(":", 2)[2]
    hit = next((b_ for b_ in db.blocked_list(uid) if _dh(b_["hwid"]) == h), None)
    if hit: db.unblock_device(uid, hit["hwid"])
    await answer_cb(c, "Разрешено" if hit else "Уже разрешено")
    await B.show(c, await dev_screen(uid, f"Разрешено: {esc(hit['name'] or 'устройство')}. Добавь подписку на нём в приложении заново." if hit else "Это устройство уже разрешено."))

async def block_pass():
    """Один проход: если заблокированное устройство снова появилось в панели, отвязывает его. Возвращает, сколько убрано."""
    n = 0
    for uid in db.blocked_users():
        try:
            bad = {b_["hwid"] for b_ in db.blocked_list(uid)}
            for d_ in await panel.hwid_list(uid):
                if str(d_.get("hwid")) in bad:
                    await panel.hwid_delete(uid, d_["hwid"]); n += 1; logging.info("blocked device removed again uid=%s", uid)
        except Exception as e:
            logging.warning("block_loop uid=%s: %s", uid, e)
        await asyncio.sleep(0.3)
    return n

async def block_loop():
    """Раз в минуту следит за заблокированными устройствами."""
    while True:
        await asyncio.sleep(60)
        try: await block_pass()
        except Exception: logging.exception("block_loop")

@router.callback_query(F.data == "dev:nl")
async def cb_dev_nl(c: CallbackQuery):
    await answer_cb(c)
    await B.show(c, ("22_devices.png", "<b>Выпустить новую ссылку?</b>\n\nСтарая ссылка перестанет работать, все устройства отвяжутся, в том числе твои. "
                     "Срок подписки не изменится.\n\nПотом открой «Подключиться» и добавь подписку в приложение заново.",
                     kb([("Да, выпустить", "dev:nlyes"), ("Отмена", "dev:open")])))

@router.callback_query(F.data == "dev:nlyes")
async def cb_dev_nlyes(c: CallbackQuery):
    uid = c.from_user.id
    last = int(db.kv_get(f"newlink:{uid}") or 0)
    wait = config.DEVICE_RESET_COOLDOWN_MIN * 60 - (time.time() - last)
    if wait > 0:
        return await answer_cb(c, f"Подожди ещё {int(wait // 60) + 1} мин. и повтори.", True)
    try:
        await panel.revoke_sub(uid)
    except Exception as e:
        logging.warning("revoke uid=%s: %s", uid, e)
        return await answer_cb(c, "Не получилось выпустить ссылку. Напиши в поддержку." + _why(uid, e), True)
    reset_err = None
    try: await panel.hwid_reset(uid)
    except Exception as e:
        reset_err = e; logging.warning("hwid reset after revoke failed uid=%s: %s", uid, e)
    db.kv_set(f"newlink:{uid}", str(int(time.time())))
    await answer_cb(c, "Готово")
    await B.show(c, ("22_devices.png", "<b>Новая ссылка выпущена</b>\n\nСтарая больше не работает. "
                     + ("Устройства отвязать не получилось: открой «Устройства» и убери их вручную. " if reset_err else "Устройства отвязаны. ")
                     + "Открой «Подключиться» и добавь подписку в приложение заново."
                     + (f"\n\nПричина (видно только тебе): {esc(reset_err)}" if reset_err and uid in config.ADMIN_IDS else ""),
                     kb([("Подключиться", "go:connect")], [("Назад", "dev:open")])))

@router.callback_query(F.data == "dev:open")
async def cb_dev_open(c: CallbackQuery):
    await answer_cb(c); await B.show(c, await dev_screen(c.from_user.id))

@router.callback_query(F.data == "dev:ask")
async def cb_dev_ask(c: CallbackQuery):
    await answer_cb(c)
    await B.show(c, ("22_devices.png", "<b>Сбросить все устройства?</b>\n\nВсе подключённые устройства отвяжутся. "
                     "Потом открой приложение, нажми «Обновить» у подписки, и устройство снова зарегистрируется.",
                     kb([("Да, сбросить", "dev:yes"), ("Отмена", "dev:open")])))

@router.callback_query(F.data == "dev:yes")
async def cb_dev_yes(c: CallbackQuery):
    uid = c.from_user.id
    last = int(db.kv_get(f"devreset:{uid}") or 0)
    wait = config.DEVICE_RESET_COOLDOWN_MIN * 60 - (time.time() - last)
    if wait > 0:
        return await answer_cb(c, f"Подожди ещё {int(wait // 60) + 1} мин. и повтори.", True)
    try:
        await panel.hwid_reset(uid)
    except Exception as e:
        logging.warning("device reset uid=%s: %s", uid, e)
        return await answer_cb(c, "Не получилось сбросить. Напиши в поддержку." + _why(uid, e), True)
    db.kv_set(f"devreset:{uid}", str(int(time.time())))
    await answer_cb(c, "Готово")
    await B.show(c, await dev_screen(uid, "Устройства отвязаны. Обнови подписку в приложении."))

# ================= Вывод реферальных денег =================
def wd_info(uid):
    """Что человек может вывести сейчас, что ещё на холде и что мешает. Деньги считаем только по оплатам деньгами."""
    now = int(time.time()); hold = config.PAYOUT_HOLD_DAYS * 86400
    u = db.get_user(uid); bal = float(u["balance"] or 0)
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
    base = "FROM payments WHERE ref_to=? AND status='paid' AND method!='balance'"
    total = q(f"SELECT SUM(ref_bonus) {base}", uid)
    matured = q(f"SELECT SUM(ref_bonus) {base} AND created<=?", uid, now - hold)
    reserved = q("SELECT SUM(amount) FROM payouts WHERE user_id=? AND status IN ('pending','paid')", uid)
    avail = max(0.0, min(bal, matured - reserved))
    held = max(0.0, min(bal, total - reserved) - avail)
    next_ts = q(f"SELECT MIN(created) {base} AND created>?", uid, now - hold)
    pending = con.execute("SELECT * FROM payouts WHERE user_id=? AND status='pending'", (uid,)).fetchone()
    last = q("SELECT MAX(created) FROM payouts WHERE user_id=? AND status IN ('pending','paid')", uid)
    cool_until = last + config.PAYOUT_COOLDOWN_DAYS * 86400 if last else 0
    blocked = bool(con.execute("SELECT 1 FROM kv WHERE k=?", (f"wdblock:{uid}",)).fetchone())
    if uid in config.ADMIN_IDS:   # владелец забирает весь реферальный баланс: без холда, минимума, кулдауна и очереди
        return {"balance": bal, "avail": round(bal, 2), "held": 0.0, "next_free": 0, "pending": None, "cool_until": 0}
    return {"balance": bal, "avail": round(avail, 2), "held": round(held, 2),
            "next_free": (next_ts + hold) if next_ts else 0, "pending": pending, "cool_until": cool_until if cool_until > now else 0, "blocked": blocked}

def _min(uid):
    return 1 if uid in config.ADMIN_IDS else config.PAYOUT_MIN

def mask(method, details):
    d_ = str(details)
    if method == "card": return "карта •••• " + re.sub(r"\D", "", d_)[-4:]
    if method == "usdt": return f"USDT {d_[:4]}…{d_[-4:]}"
    return "СБП •••• " + re.sub(r"\D", "", d_)[-4:]

METHODS = {"card": "Карта", "sbp": "СБП", "usdt": "USDT (TRC20)"}

def luhn_ok(num):
    s, alt = 0, False
    for ch in reversed(num):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9: n -= 9
        s += n; alt = not alt
    return s % 10 == 0

def wd_screen(uid):
    i = wd_info(uid)
    L = ["<b>Вывод денег</b>", "",
         f"Можно вывести сейчас: <b>{rub(i['avail'])} ₽</b>",
         f"Ждёт {config.PAYOUT_HOLD_DAYS} дней после оплаты друга: {rub(i['held'])} ₽" + (f" (ближайшая сумма освободится {B.dfmt(i['next_free'])})" if i["next_free"] and i["held"] else ""),
         f"Минимум: {_min(uid)} ₽. Не чаще раза в {config.PAYOUT_COOLDOWN_DAYS} дн.",
         "Выплачиваем вручную: карта, СБП или USDT."]
    rows = []
    if i.get("blocked"):
        L += ["", "Вывод для твоего аккаунта сейчас недоступен. Если это ошибка, напиши в поддержку."]
    elif i["pending"]:
        L += ["", f"Заявка #{i['pending']['id']} на {rub(i['pending']['amount'])} ₽ уже в обработке."]
    elif i["cool_until"]:
        L += ["", f"Следующий вывод будет доступен {B.dfmt(i['cool_until'])}."]
    elif i["avail"] < _min(uid):
        L += ["", f"Пока меньше минимума ({_min(uid)} ₽). Приглашай друзей, бонус копится."]
    else:
        rows.append([("Карта", "wd:m:card"), ("СБП", "wd:m:sbp")]); rows.append([("USDT", "wd:m:usdt")])
    rows.append([("Назад", "go:start")])
    return ("16_withdraw.png", "\n".join(L), kb(*rows))

@router.callback_query(F.data == "wd:start")
async def cb_wd_start(c: CallbackQuery):
    WD.pop(c.from_user.id, None); await answer_cb(c); await B.show(c, wd_screen(c.from_user.id))

PROMPTS = {"card": "Пришли номер карты одним сообщением (16 цифр). Отмена: напиши «отмена».",
           "sbp": "Пришли номер телефона и название банка одним сообщением, например:\n+79991234567 Сбербанк\nОтмена: напиши «отмена».",
           "usdt": "Пришли адрес кошелька USDT в сети TRC20 (начинается с буквы T). Другие сети не принимаем. Отмена: напиши «отмена»."}

@router.callback_query(F.data.startswith("wd:m:"))
async def cb_wd_method(c: CallbackQuery):
    uid = c.from_user.id; method = c.data.split(":")[2]
    i = wd_info(uid)
    if method not in METHODS or i.get("blocked") or i["pending"] or i["cool_until"] or i["avail"] < _min(uid):
        return await answer_cb(c, "Сейчас вывод недоступен.", True)
    B.WAIT_TOPUP.discard(uid); B.WAIT_PROMO.discard(uid); B.WAIT_EMAIL.pop(uid, None)
    WD[uid] = {"step": "details", "method": method, "ts": time.time()}
    await answer_cb(c)
    await B.show(c, ("16_withdraw.png", f"<b>{METHODS[method]}</b>\n\n{PROMPTS[method]}", kb([("Отмена", "wd:start")])))

def _wd_alive(m):
    st = WD.get(m.from_user.id)
    return bool(st) and not (m.text or "").startswith("/")

@router.message(F.text, _wd_alive)
async def got_wd(m: Message):
    uid = m.from_user.id; st = WD[uid]; txt = m.text.strip()
    if time.time() - st["ts"] > 900:
        WD.pop(uid, None); return await m.answer("Время ввода вышло. Открой «Друзья → Вывести деньги» ещё раз.")
    if txt.lower() in ("отмена", "cancel"):
        WD.pop(uid, None); return await B.show(m, wd_screen(uid))
    st["ts"] = time.time()
    if st["step"] == "details":
        method = st["method"]; value = None
        if method == "card":
            digits = re.sub(r"[\s-]", "", txt)
            if digits.isdigit() and 16 <= len(digits) <= 19 and luhn_ok(digits): value = digits
            else: return await m.answer("Номер карты не похож на настоящий. Пришли 16 цифр без ошибок.")
        elif method == "sbp":
            mt = re.match(r"^\+?[78]?[\s(-]*\d[\d\s()-]{8,14}\d\s+(\S.{1,40})$", txt)
            if mt: value = txt[:80]
            else: return await m.answer("Нужны телефон и банк, например: +79991234567 Сбербанк")
        else:
            if re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", txt): value = txt
            else: return await m.answer("Это не адрес TRC20. Он начинается с T и состоит из 34 символов. Проверь и пришли ещё раз.")
        st["details"] = value; st["step"] = "amount"
        i = wd_info(uid)
        return await m.answer(f"Сколько вывести? Доступно {rub(i['avail'])} ₽, минимум {_min(uid)} ₽.\nПришли сумму числом или слово «всё».")
    if st["step"] == "amount":
        i = wd_info(uid)
        if txt.lower() in ("всё", "все", "all"): amount = i["avail"] if uid in config.ADMIN_IDS else int(i["avail"])
        else:
            try: amount = float(txt.replace(",", ".").replace(" ", "")); assert amount == amount
            except Exception: return await m.answer("Пришли сумму числом, например 500, или слово «всё».")
        amount = round(amount, 2)
        if amount < _min(uid): return await m.answer(f"Минимум для вывода {_min(uid)} ₽.")
        if amount > i["avail"] + 1e-9: return await m.answer(f"Можно вывести не больше {rub(i['avail'])} ₽.")
        st["amount"] = amount; st["step"] = "confirm"
        return await B.show(m, ("16_withdraw.png", f"<b>Проверь заявку</b>\n\nСумма: {rub(amount)} ₽\nКуда: {esc(mask(st['method'], st['details']))}\n\n"
                                 "Деньги спишутся с реферального баланса сразу. Если заявку отклонят, вернутся.",
                                 kb([("Подтвердить", "wd:ok"), ("Отмена", "wd:start")])))

@router.callback_query(F.data == "wd:ok")
async def cb_wd_ok(c: CallbackQuery):
    uid = c.from_user.id; st = WD.get(uid)
    if not st or st.get("step") != "confirm":
        return await answer_cb(c, "Заявка устарела. Начни заново.", True)
    i = wd_info(uid)
    if i.get("blocked") or i["pending"] or i["cool_until"] or st["amount"] > i["avail"] + 1e-9 or st["amount"] < _min(uid):
        WD.pop(uid, None); return await answer_cb(c, "Сумма изменилась, начни заново.", True)
    amount = st["amount"]
    con.execute("UPDATE users SET balance=MAX(0, balance-?) WHERE id=?", (amount, uid))
    cur = con.execute("INSERT INTO payouts(user_id,amount,method,details,status,created) VALUES(?,?,?,?, 'pending', ?)",
                      (uid, amount, st["method"], st["details"], int(time.time())))
    con.commit(); pid = cur.lastrowid; WD.pop(uid, None)
    await answer_cb(c)
    await B.show(c, ("17_withdraw_ok.png", f"<b>Заявка #{pid} принята</b>\n\nСумма: {rub(amount)} ₽\nКуда: {esc(mask(st['method'], st['details']))}\n\n"
                     "Мы выплатим вручную и напишем тебе сюда.", kb([("Главное меню", "go:start")])))
    u = db.peek_user(uid)
    prev = con.execute("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0]
    await tell(config.ADMIN_IDS,
               f"<b>Заявка на вывод #{pid}</b>\n{B.who(u)} · <code>{uid}</code>\nСумма: <b>{rub(amount)} ₽</b>\nСпособ: {METHODS[st['method']]}\n"
               f"Реквизиты: <code>{esc(st['details'])}</code>\nВыплачено ему раньше: {rub(prev)} ₽\n\n{B.features.fraud_line(uid)}",
               kb([("Выплачено", f"wdp:ok:{pid}"), ("Отклонить", f"wdp:no:{pid}")]))

async def payout_paid(pid, actor_id):
    p = con.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
    if not p or p["status"] != "pending": return None
    con.execute("UPDATE payouts SET status='paid', done_at=?, details=? WHERE id=?",
                (int(time.time()), mask(p["method"], p["details"]), pid)); con.commit()
    try:
        await B.show(_Target(p["user_id"]), ("26_payout_paid.png", f"<b>Выплата отправлена</b>\n\nЗаявка #{pid}: {rub(p['amount'])} ₽, {METHODS[p['method']]}.\nСпасибо, что приглашаешь друзей.",
                                              kb([("Главное меню", "go:start")])))
    except Exception:
        logging.warning("не удалось сообщить о выплате %s", p["user_id"])
    return p

async def payout_reject(pid, reason):
    p = con.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
    if not p or p["status"] != "pending": return None
    con.execute("UPDATE users SET balance=balance+? WHERE id=?", (p["amount"], p["user_id"]))
    con.execute("UPDATE payouts SET status='rejected', done_at=?, reason=?, details=? WHERE id=?",
                (int(time.time()), reason, mask(p["method"], p["details"]), pid)); con.commit()
    try:
        await B.show(_Target(p["user_id"]), ("16_withdraw.png", f"<b>Заявка #{pid} отклонена</b>\n\nПричина: {esc(reason)}\nДеньги вернулись на реферальный баланс ({rub(p['amount'])} ₽).",
                                              kb([("Поддержка", "go:support")])))
    except Exception:
        logging.warning("не удалось сообщить об отказе %s", p["user_id"])
    return p

class _Target:
    """Минимальная замена Message для B.show: шлёт экран человеку по ID."""
    def __init__(self, uid): self.uid = uid
    async def answer_photo(self, photo, caption=None, reply_markup=None):
        return await B.bot.send_photo(self.uid, photo, caption=caption, reply_markup=reply_markup)
    async def answer(self, text, **k):
        return await B.bot.send_message(self.uid, text, **k)

@router.callback_query(F.data.startswith("wdp:"), lambda c: c.from_user.id in config.ADMIN_IDS)
async def cb_wdp(c: CallbackQuery):
    _, act, pid = c.data.split(":"); pid = int(pid)
    if act == "ok":
        p = await payout_paid(pid, c.from_user.id)
        if not p: return await answer_cb(c, "Заявка уже обработана.", True)
        db.log_action(c.from_user.id, "payout_paid", str(p["user_id"]), f"#{pid} {p['amount']}")
        await answer_cb(c, "Отмечено как выплачено")
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        await c.message.answer(f"Заявка #{pid} выплачена, человеку отправлено сообщение.")
    else:
        WD_REJ[c.from_user.id] = (pid, time.time())
        await answer_cb(c)
        await c.message.answer(f"Напиши причину отказа по заявке #{pid} одним сообщением (её увидит человек). Отмена: «отмена».")

def _rej_alive(m): return m.from_user.id in WD_REJ and not (m.text or "").startswith("/")

@router.message(F.text, _rej_alive)
async def got_reject_reason(m: Message):
    pid, ts = WD_REJ.pop(m.from_user.id); txt = m.text.strip()
    if txt.lower() in ("отмена", "cancel") or time.time() - ts > 900:
        return await m.answer("Отменено. Заявка осталась в ожидании.")
    p = await payout_reject(pid, txt[:300])
    if not p: return await m.answer("Заявка уже обработана.")
    db.log_action(m.from_user.id, "payout_rejected", str(p["user_id"]), f"#{pid}: {txt[:100]}")
    await m.answer(f"Заявка #{pid} отклонена, деньги вернулись на баланс человека.")

@router.message(Command("payouts"), is_owner)
async def cmd_payouts(m: Message):
    rows = con.execute("SELECT * FROM payouts ORDER BY (status='pending') DESC, id DESC LIMIT 15").fetchall()
    if not rows: return await m.answer("Заявок на вывод пока нет.")
    mark = {"pending": "Ждёт", "paid": "Выплачено", "rejected": "Отклонено"}
    L = [f"{mark[r['status']]} #{r['id']} · {B.tfmt(r['created'], '%d.%m %H:%M')} · {rub(r['amount'])} ₽ · {METHODS.get(r['method'], r['method'])} · <code>{r['user_id']}</code>"
         + (f"\n    <code>{esc(r['details'])}</code>" if r["status"] == "pending" else "") for r in rows]
    paid = con.execute("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM payouts WHERE status='paid'").fetchone()
    await m.answer("<b>Выводы</b>\n\n" + "\n".join(L) + f"\n\nВыплачено всего: {rub(paid[0])} ₽ ({paid[1]})\nОтметить: /payoutok НОМЕР или /payoutno НОМЕР причина")

@router.message(Command("payoutok"), is_owner)
async def cmd_payoutok(m: Message, command: CommandObject):
    a = args_of(command)
    if not a or not a[0].isdigit(): return await m.answer("Формат: /payoutok НОМЕР")
    p = await payout_paid(int(a[0]), m.from_user.id)
    if not p: return await m.answer("Заявка не найдена или уже обработана.")
    B.alog(m, "payout_paid", p["user_id"], f"#{p['id']} {p['amount']}")
    await m.answer(f"Заявка #{p['id']} выплачена.")

@router.message(Command("payoutno"), is_owner)
async def cmd_payoutno(m: Message, command: CommandObject):
    parts = (command.args or "").split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit(): return await m.answer("Формат: /payoutno НОМЕР ПРИЧИНА")
    p = await payout_reject(int(parts[0]), parts[1][:300])
    if not p: return await m.answer("Заявка не найдена или уже обработана.")
    B.alog(m, "payout_rejected", p["user_id"], f"#{p['id']}: {parts[1][:100]}")
    await m.answer(f"Заявка #{p['id']} отклонена, деньги вернулись.")

# ================= FAQ и обращения в поддержку =================
FAQ = [
    ("Не подключается",
     "1. Обнови подписку в приложении (кнопка обновления рядом с подпиской).\n2. Проверь, что на телефоне верные дата и время.\n"
     "3. Если удалял подписку и добавлял заново, открой «Профиль → Устройства» и нажми «Сбросить все».\n"
     "4. Попробуй другую сеть: Wi-Fi вместо мобильной или наоборот."),
    ("Нет серверов в списке",
     "Чаще всего старая запись устройства занимает место в лимите. Открой «Профиль → Устройства», нажми «Сбросить все», "
     "потом обнови подписку в приложении.\nЕсли не помогло, проверь, что подписка активна: «Профиль»."),
    ("Медленно работает",
     "Выбери в приложении другой сервер, лучше ближе к тебе. Перезапусти приложение и переподключись. "
     "Проверь скорость без VPN: если и она низкая, дело в сети."),
    ("Оплата и чек",
     "Оплатить можно картой или СБП, либо балансом. Если оплатил, а подписка не продлилась, подожди пару минут: бот проверяет платёж сам. "
     "Если не пришло, напиши в поддержку и приложи скриншот оплаты."),
    ("Новый телефон",
     f"Одна подписка работает на нескольких устройствах (лимит: {config.HWID_LIMIT}). На новом телефоне просто добавь подписку через «Подключиться». "
     "Если лимит занят, сбрось старые в разделе «Профиль → Устройства»."),
]

def faq_screen():
    rows = [[(q, f"faq:{i}")] for i, (q, _) in enumerate(FAQ)] + [[("Создать обращение", "tk:new")], [("Назад", "go:support")]]
    return ("19_faq.png", "<b>Частые вопросы</b>\n\nВыбери, что случилось. Если ответа нет, напиши нам.", kb(*rows))

@router.callback_query(F.data == "faq:open")
async def cb_faq_open(c: CallbackQuery):
    await answer_cb(c); await B.show(c, faq_screen())

@router.callback_query(F.data.regexp(r"^faq:\d+$"))
async def cb_faq_item(c: CallbackQuery):
    i = int(c.data.split(":")[1])
    if i >= len(FAQ): return await answer_cb(c)
    await answer_cb(c)
    await B.show(c, ("19_faq.png", f"<b>{FAQ[i][0]}</b>\n\n{FAQ[i][1]}",
                     kb([("Не помогло, написать", "tk:new")], [("Назад", "faq:open")])))

def tk_open_for(uid):
    return con.execute("SELECT * FROM tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()

@router.callback_query(F.data.in_({"tk:new"}) | F.data.startswith("tk:cont:"))
async def cb_tk_new(c: CallbackQuery):
    uid = c.from_user.id
    B.WAIT_TOPUP.discard(uid); B.WAIT_PROMO.discard(uid); B.WAIT_EMAIL.pop(uid, None); WD.pop(uid, None)
    tid = int(c.data.split(":")[2]) if c.data.startswith("tk:cont:") else 0
    TK_WAIT[uid] = tid
    await answer_cb(c)
    await B.show(c, ("18_ticket.png", "<b>Обращение в поддержку</b>\n\nОпиши проблему одним сообщением. Можно приложить скриншот: пришли фото с подписью.\n"
                     "Отмена: напиши «отмена».", kb([("Назад", "go:support")])))

def _tk_alive(m): return m.from_user.id in TK_WAIT and not (m.text or "").startswith("/")

@router.message(_tk_alive)
async def got_ticket(m: Message):
    uid = m.from_user.id; tid = TK_WAIT.pop(uid)
    text = (m.text or m.caption or "").strip()
    if text.lower() in ("отмена", "cancel"):
        return await B.show(m, B.screens.support(db.get_user(uid)))
    if not text and not m.photo:
        TK_WAIT[uid] = tid
        return await m.answer("Напиши проблему текстом или пришли фото с подписью.")
    t = tk_open_for(uid)
    if t: tid = t["id"]
    else:
        tid = con.execute("INSERT INTO tickets(user_id,status,created) VALUES(?, 'open', ?)", (uid, int(time.time()))).lastrowid
    con.execute("INSERT INTO ticket_msgs(ticket_id,staff,text,ts) VALUES(?,0,?,?)", (tid, text[:2000] or "[фото]", int(time.time()))); con.commit()
    u = db.peek_user(uid)
    head = f"<b>Обращение #{tid}</b> · {B.who(u)} · <code>{uid}</code>\n\n{esc(text) or '[фото без подписи]'}"
    markup = kb([("Ответить", f"tkr:{tid}"), ("Закрыть", f"tkc:{tid}")])
    for sid in support_ids():
        try:
            if m.photo: await B.bot.send_photo(sid, m.photo[-1].file_id, caption=head[:1000], reply_markup=markup)
            else: await B.bot.send_message(sid, head, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            logging.warning("не удалось передать обращение сотруднику %s", sid)
    await B.show(m, ("18_ticket.png", f"<b>Обращение #{tid} отправлено</b>\n\nОтветим прямо здесь. Если хочешь дописать, нажми «Дополнить».",
                     kb([("Дополнить", f"tk:cont:{tid}")], [("Главное меню", "go:start")])))

@router.callback_query(F.data.startswith("tkr:"), lambda c: c.from_user.id in support_ids())
async def cb_tk_reply(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    t = con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t: return await answer_cb(c, "Обращение не найдено.", True)
    clear_waits(c.from_user.id); TK_REPLY[c.from_user.id] = (tid, time.time())
    await answer_cb(c)
    await c.message.answer(f"Напиши ответ по обращению #{tid} одним сообщением. Отмена: «отмена».")

def _rp_alive(m): return m.from_user.id in TK_REPLY and not (m.text or "").startswith("/")

async def send_reply(tid, staff_id, text):
    t = con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t: return "Обращение не найдено."
    con.execute("INSERT INTO ticket_msgs(ticket_id,staff,text,ts) VALUES(?,?,?,?)", (tid, staff_id, text[:2000], int(time.time()))); con.commit()
    try:
        await B.bot.send_message(t["user_id"], f"<b>Ответ поддержки</b> · обращение #{tid}\n\n{esc(text)}",
                                 reply_markup=kb([("Ответить", f"tk:cont:{tid}")]))
    except TelegramForbiddenError:
        return "Не доставлено: человек заблокировал бота."
    db.log_action(staff_id, "ticket_reply", str(t["user_id"]), f"#{tid}")
    return None

@router.message(F.text, _rp_alive)
async def got_staff_reply(m: Message):
    tid, ts = TK_REPLY.pop(m.from_user.id); txt = m.text.strip()
    if txt.lower() in ("отмена", "cancel") or time.time() - ts > 1800:
        return await m.answer("Отменено.")
    err = await send_reply(tid, m.from_user.id, txt)
    await m.answer(err or f"Ответ по #{tid} отправлен.", reply_markup=None if err else kb([("Закрыть обращение", f"tkc:{tid}")]))

async def close_ticket(tid, staff_id):
    t = con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t or t["status"] != "open": return None
    con.execute("UPDATE tickets SET status='closed', closed_at=? WHERE id=?", (int(time.time()), tid)); con.commit()
    db.log_action(staff_id, "ticket_close", str(t["user_id"]), f"#{tid}")
    await B.safe_send(t["user_id"], ("25_ticket_closed.png", f"<b>Обращение #{tid} закрыто</b>\n\nПомогли ли мы тебе?",
                                      kb([("Помогли", f"tk:rate:{tid}:1"), ("Не помогли", f"tk:rate:{tid}:0")])))
    return t

@router.callback_query(F.data.startswith("tkc:"), lambda c: c.from_user.id in support_ids())
async def cb_tk_close(c: CallbackQuery):
    t = await close_ticket(int(c.data.split(":")[1]), c.from_user.id)
    await answer_cb(c, "Закрыто" if t else "Уже закрыто", not t)
    if t:
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass

@router.callback_query(F.data.startswith("tk:rate:"))
async def cb_tk_rate(c: CallbackQuery):
    _, _, tid, val = c.data.split(":")
    t = con.execute("SELECT * FROM tickets WHERE id=?", (int(tid),)).fetchone()
    if not t or t["user_id"] != c.from_user.id or t["rating"] is not None:
        return await answer_cb(c, "Оценка уже учтена.")
    con.execute("UPDATE tickets SET rating=? WHERE id=?", (int(val), int(tid))); con.commit()
    await answer_cb(c)
    await B.show(c, ("25_ticket_closed.png", "<b>Спасибо за оценку</b>\n\n" + ("Рады, что помогли." if val == "1" else "Жаль. Напиши ещё раз, разберёмся."),
                     kb(*([[("Написать снова", "tk:new")]] if val == "0" else []), [("Главное меню", "go:start")])))

@router.message(Command("tickets"), is_support)
async def cmd_tickets(m: Message):
    rows = con.execute("SELECT * FROM tickets WHERE status='open' ORDER BY id").fetchall()
    if not rows: return await m.answer("Открытых обращений нет.")
    L = []
    for t in rows:
        last = con.execute("SELECT text FROM ticket_msgs WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        L.append(f"#{t['id']} · {B.tfmt(t['created'], '%d.%m %H:%M')} · <code>{t['user_id']}</code>\n{esc((last['text'] if last else '')[:120])}")
    await B.send_lines(m, f"<b>Открытые обращения: {len(rows)}</b>", L, "\nОтветить: /reply НОМЕР текст\nЗакрыть: /close НОМЕР")

@router.message(Command("reply"), is_support)
async def cmd_reply(m: Message, command: CommandObject):
    parts = (command.args or "").split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit(): return await m.answer("Формат: /reply НОМЕР ТЕКСТ")
    err = await send_reply(int(parts[0]), m.from_user.id, parts[1])
    await m.answer(err or "Отправлено.")

@router.message(Command("close"), is_support)
async def cmd_close(m: Message, command: CommandObject):
    a = args_of(command)
    if not a or not a[0].isdigit(): return await m.answer("Формат: /close НОМЕР")
    await m.answer("Закрыто." if await close_ticket(int(a[0]), m.from_user.id) else "Обращение не найдено или уже закрыто.")

@router.message(Command("ticketstats"), is_owner)
async def cmd_ticketstats(m: Message):
    q = lambda sql: con.execute(sql).fetchone()
    total, opened = q("SELECT COUNT(*), SUM(status='open') FROM tickets")
    good, bad = q("SELECT COALESCE(SUM(rating=1),0), COALESCE(SUM(rating=0),0) FROM tickets")
    await m.answer(f"<b>Обращения</b>\n\nВсего: {total}\nОткрыто: {opened or 0}\nОценок «помогли»: {good}, «не помогли»: {bad}")

# ================= Опрос «почему не продлил» =================
SURVEY = {"price": "Дорого", "noneed": "VPN больше не нужен", "broken": "Не работало или медленно", "other_vpn": "Перешёл на другой сервис", "other": "Другая причина"}

def survey_screen():
    return ("21_survey.png", "<b>Почему не продлил?</b>\n\nПодписка закончилась. Ответ займёт секунду и поможет сделать сервис лучше.",
            kb(*[[(v, f"sv:{k}")] for k, v in SURVEY.items()]))

@router.callback_query(F.data.startswith("sv:"))
async def cb_survey(c: CallbackQuery):
    key = c.data.split(":")[1]
    if key not in SURVEY: return await answer_cb(c)
    con.execute("INSERT INTO survey(user_id,asked_at,answer,answered_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET answer=excluded.answer, answered_at=excluded.answered_at",
                (c.from_user.id, int(time.time()), key, int(time.time()))); con.commit()
    await answer_cb(c)
    await B.show(c, ("21_survey.png", "<b>Спасибо за ответ</b>\n\nЕсли захочешь вернуться, подписка продлится в пару нажатий.",
                     kb([("Тарифы", "go:plans")], [("Главное меню", "go:start")])))

async def survey_loop():
    while True:
        try:
            if config.AUTO_MSGS:
                now = int(time.time())
                rows = con.execute("SELECT u.id FROM users u LEFT JOIN survey s ON s.user_id=u.id WHERE s.user_id IS NULL AND u.sub_until>? "
                                   "AND u.sub_until<? AND COALESCE(u.banned,0)=0 LIMIT 30",
                                   (now - 14 * 86400, now - config.SURVEY_AFTER_DAYS * 86400)).fetchall()
                for r in rows:
                    con.execute("INSERT OR IGNORE INTO survey(user_id,asked_at) VALUES(?,?)", (r["id"], now)); con.commit()
                    await B.safe_send(r["id"], survey_screen())
                    await asyncio.sleep(0.1)
        except Exception:
            logging.exception("survey loop")
        await asyncio.sleep(1800)

@router.message(Command("surveystats"), lambda m: m.from_user.id in config.ADMIN_IDS or m.from_user.id in config.ACCOUNTANT_IDS)
async def cmd_surveystats(m: Message):
    asked = con.execute("SELECT COUNT(*) FROM survey").fetchone()[0]
    rows = con.execute("SELECT answer, COUNT(*) n FROM survey WHERE answer IS NOT NULL GROUP BY answer ORDER BY n DESC").fetchall()
    total = sum(r["n"] for r in rows)
    await m.answer(f"<b>Почему не продлевают</b>\n\nСпросили: {asked}, ответили: {total}\n\n"
                   + ("\n".join(f"{SURVEY.get(r['answer'], r['answer'])}: {r['n']} ({r['n'] * 100 // total}%)" for r in rows) if total else "Ответов пока нет."))

# ================= Команды владельца =================
async def pick_user(m: Message, command: CommandObject, usage: str):
    a = args_of(command)
    if not a:
        await m.answer(usage); return None, []
    u = B.resolve_user(a[0])
    if not u:
        await m.answer(B.NOT_FOUND); return None, a[1:]
    return u, a[1:]

async def panel_do(m: Message, coro):
    try:
        return True, await coro
    except Exception as e:
        logging.warning("panel command failed: %s", e)
        await m.answer("Панель: " + esc(e))
        return False, None

def gb(n): return f"{(n or 0) / 1024 ** 3:.1f} ГБ"
def pct(a, b): return f"{a * 100 / b:.0f}%" if b else "0%"

def set_sub_until(uid, ts):
    db.get_user(uid); ts = max(0, int(ts))
    con.execute("UPDATE users SET sub_until=? WHERE id=?", (ts, uid)); con.commit(); return ts

@router.message(Command("take"), is_owner)
async def cmd_take(m: Message, command: CommandObject):
    u, rest = await pick_user(m, command, "Формат: /take ID ДНИ\nСнимает дни с подписки. Пример: /take 123456789 7")
    if not u: return
    try: days = int(rest[0]); assert 1 <= days <= 3650
    except Exception: return await m.answer("Формат: /take ID ДНИ (число от 1 до 3650)")
    cur = u["sub_until"] or 0
    if cur <= time.time(): return await m.answer("У этого человека подписка и так не активна.")
    until = set_sub_until(u["id"], cur - days * 86400)
    await panel.set_expiry(u["id"], until)
    B.alog(m, "take", u["id"], f"-{days} дн.")
    await m.answer(f"Готово: {u['id']} −{days} дн. " + (f"Подписка до {B.dfmt(until)}." if until > time.time() else "Подписка закончилась."))

@router.message(Command("setsub"), is_owner)
async def cmd_setsub(m: Message, command: CommandObject):
    usage = "Формат: /setsub ID ДД.ММ.ГГГГ\nСтавит точную дату окончания (в конце этого дня). /setsub ID 0 обнуляет подписку."
    u, rest = await pick_user(m, command, usage)
    if not u: return
    if not rest: return await m.answer(usage)
    if rest[0] == "0": ts = set_sub_until(u["id"], 0)
    else:
        ts = B.end_of_day(rest[0]) if B.DATE_RE.match(rest[0]) else None
        if not ts: return await m.answer(usage)
        set_sub_until(u["id"], ts)
    await panel.set_expiry(u["id"], ts)
    B.alog(m, "setsub", u["id"], rest[0])
    await m.answer(f"Готово: {u['id']}, " + (f"подписка до {B.dfmt(ts)}." if ts else "подписка обнулена."))

@router.message(Command("resettrial"), is_owner)
async def cmd_resettrial(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /resettrial ID\nВозвращает человеку возможность взять пробник.")
    if not u: return
    con.execute("UPDATE users SET trial_used=0 WHERE id=?", (u["id"],)); con.commit()
    B.alog(m, "resettrial", u["id"])
    await m.answer(f"Пробник снова доступен: {B.who(u)} · <code>{u['id']}</code>", disable_web_page_preview=True)

@router.message(Command("msg"), is_support)
async def cmd_msg(m: Message, command: CommandObject):
    parts = (command.args or "").split(None, 1)
    photo = B.photo_of(m)
    if len(parts) < 2 and not (photo and parts):
        return await m.answer("Формат: /msg ID ТЕКСТ\nЛичное сообщение от бота. Поддерживается HTML.\nС фото: пришли фото с подписью /msg ID ТЕКСТ (или ответь командой на фото).")
    u = B.resolve_user(parts[0])
    if not u: return await m.answer(B.NOT_FOUND)
    text = parts[1] if len(parts) > 1 else ""
    if photo and len(text) > 1000: return await m.answer("Подпись к фото: до 1000 символов.")
    try:
        await B.send_maybe_photo(u["id"], text, photo)
        B.alog(m, "msg", u["id"], ("[фото] " if photo else "") + text[:100]); await m.answer(f"Отправлено: <code>{u['id']}</code>")
    except TelegramForbiddenError: await m.answer("Не доставлено: человек заблокировал бота.")
    except TelegramBadRequest as e: await m.answer("Не доставлено: " + esc(e))

def get_note(uid):
    r = con.execute("SELECT note, ts FROM user_notes WHERE user_id=?", (uid,)).fetchone()
    return (r["note"], r["ts"]) if r else (None, None)

def set_note(uid, note):
    if note: con.execute("INSERT OR REPLACE INTO user_notes(user_id,note,ts) VALUES(?,?,?)", (uid, note, int(time.time())))
    else: con.execute("DELETE FROM user_notes WHERE user_id=?", (uid,))
    con.commit()

@router.message(Command("note"), is_support)
async def cmd_note(m: Message, command: CommandObject):
    usage = "Формат: /note ID ТЕКСТ: записать\n/note ID: показать\n/note ID -: удалить"
    parts = (command.args or "").split(None, 1)
    if not parts: return await m.answer(usage)
    u = B.resolve_user(parts[0])
    if not u: return await m.answer(B.NOT_FOUND)
    if len(parts) == 1:
        note, ts = get_note(u["id"])
        return await m.answer(f"Заметка: {esc(note)}\n({B.tfmt(ts)})" if note else "Заметки нет.")
    if parts[1].strip() == "-": set_note(u["id"], None); return await m.answer("Заметка удалена.")
    set_note(u["id"], parts[1].strip()[:1000]); B.alog(m, "note", u["id"])
    await m.answer("Заметка сохранена. Она видна в карточке человека.")

@router.message(Command("refs"), is_owner)
async def cmd_refs(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /refs ID\nКого пригласил человек и сколько они заплатили.")
    if not u: return
    rows = con.execute("SELECT u.*, COALESCE((SELECT SUM(amount) FROM payments p WHERE p.user_id=u.id AND p.status='paid' AND p.method!='balance'),0) AS paid "
                       "FROM users u WHERE u.ref=? ORDER BY u.created DESC LIMIT 40", (u["id"],)).fetchall()
    if not rows: return await m.answer("Этот человек никого не пригласил.")
    n, earned, bal = db.friends_stats(u["id"])
    lines = [f'{B.who(r)} · <code>{r["id"]}</code> · {B.sub_status(r)} · заплатил {r["paid"]:.0f} ₽' for r in rows]
    await B.send_lines(m, f"<b>Приглашённые: {n}</b> · заработал {earned:.0f} ₽ · на реф. балансе {bal:.0f} ₽", lines)

@router.message(Command("top"), is_owner)
async def cmd_top(m: Message):
    refs = con.execute("SELECT r.id, r.username, r.name, COUNT(u.id) n, COALESCE(r.ref_earned,0) earned FROM users u JOIN users r ON r.id=u.ref "
                       "GROUP BY r.id ORDER BY n DESC, earned DESC LIMIT 10").fetchall()
    pays = con.execute("SELECT u.id, u.username, u.name, COUNT(*) n, SUM(p.amount) total FROM payments p JOIN users u ON u.id=p.user_id "
                       "WHERE p.status='paid' AND p.method!='balance' AND p.plan!='topup' GROUP BY u.id ORDER BY total DESC LIMIT 10").fetchall()
    L = ["<b>Топ приглашающих</b>"] + [f'{i}. {B.who(r)} · <code>{r["id"]}</code> · {r["n"]} чел. · {r["earned"]:.0f} ₽' for i, r in enumerate(refs, 1)]
    if not refs: L.append("Пока никого.")
    L += ["", "<b>Топ по оплатам</b>"] + [f'{i}. {B.who(r)} · <code>{r["id"]}</code> · {r["total"]:.0f} ₽ ({r["n"]} оплат)' for i, r in enumerate(pays, 1)]
    if not pays: L.append("Пока никого.")
    await m.answer("\n".join(L), disable_web_page_preview=True)

@router.message(Command("funnel"), lambda m: m.from_user.id in config.ADMIN_IDS or m.from_user.id in config.ACCOUNTANT_IDS)
async def cmd_funnel(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if arg.isdigit() and 1 <= int(arg) <= 3650:   # /funnel 30: только те, кто пришёл за последние N дней
        return await m.answer(B.features.funnel_cohort(int(arg)))
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
    now = int(time.time()); real = "p.status='paid' AND p.method!='balance' AND p.plan!='topup'"
    users = q("SELECT COUNT(*) FROM users"); trial = q("SELECT COUNT(*) FROM users WHERE trial_used=1")
    payers = q(f"SELECT COUNT(DISTINCT p.user_id) FROM payments p WHERE {real}")
    tp = q(f"SELECT COUNT(DISTINCT p.user_id) FROM payments p JOIN users u ON u.id=p.user_id WHERE {real} AND u.trial_used=1")
    repeat = q(f"SELECT COUNT(*) FROM (SELECT p.user_id FROM payments p WHERE {real} GROUP BY p.user_id HAVING COUNT(*)>1)")
    active = q(f"SELECT COUNT(DISTINCT u.id) FROM users u JOIN payments p ON p.user_id=u.id WHERE {real} AND u.sub_until>?", now)
    await m.answer("<b>Воронка</b>\n\n"
                   f"Пришли: {users}\nВзяли пробник: {trial} ({pct(trial, users)} от пришедших)\n"
                   f"Заплатили хоть раз: {payers} ({pct(payers, users)} от пришедших)\n"
                   f"Из них с пробника: {tp} ({pct(tp, trial)} от взявших пробник)\n"
                   f"Платят повторно: {repeat} ({pct(repeat, payers)} от платящих)\n"
                   f"Платящие с активной подпиской сейчас: {active} ({pct(active, payers)})")

@router.message(Command("export"), is_owner)
async def cmd_export(m: Message, command: CommandObject):
    kind = (command.args or "users").strip().lower() or "users"
    if kind != "users":
        return await B.features.send_export(m, kind)   # payments | payouts | balance
    rows = con.execute("SELECT * FROM users ORDER BY created").fetchall()
    cols = ["id", "username", "name", "created", "sub_until", "trial_used", "topup", "balance", "ref_earned", "ref", "src", "banned", "email"]
    cols = [c for c in cols if rows and c in rows[0].keys()] or ["id"]
    buf = io.StringIO(); w = csv.writer(buf, delimiter=";"); w.writerow(cols)
    for r in rows:
        w.writerow([(B.tfmt(r[c]) if c in ("created", "sub_until") and r[c] else r[c]) if r[c] is not None else "" for c in cols])
    B.alog(m, "export", "", f"{len(rows)} польз.")
    await m.answer_document(BufferedInputFile(("\ufeff" + buf.getvalue()).encode("utf-8"), filename=f"nqyro_users_{B.tfmt(time.time(), '%Y%m%d')}.csv"),
                            caption=f"Пользователей: {len(rows)}. В файле почта и балансы, не пересылай его.")

@router.message(Command("sub"), is_owner)
async def cmd_sub(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /sub ID\nПоказывает ссылку-подписку человека из панели.")
    if not u: return
    info = await panel.get_info(u["id"])
    if info and info.get("subscriptionUrl"): await m.answer(f"Ссылка {u['id']}:\n<code>{esc(info['subscriptionUrl'])}</code>")
    else: await m.answer("В панели такого пользователя нет (или панель недоступна).")

@router.message(Command("diag"), is_owner)
async def cmd_diag(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /diag ID\nПочему человек не подключается: сверяет бота и панель и называет причину.")
    if not u: return
    ok, d = await panel_do(m, panel.diagnose(u["id"]))
    if not ok: return
    now = time.time(); active = (u["sub_until"] or 0) > now
    L = [f"<b>Диагностика {u['id']}</b>", "",
         f"Бот: подписка {B.sub_status(u)}", ""]
    problems = []
    p = d.get("picked")
    if not p:
        L.append("Панель: такого пользователя нет")
        if active: problems.append("В панели человека нет. Пусть нажмёт «Подключиться», бот создаст его. Если он уже нажимал и не создался, смотри /health и логи.")
    else:
        try: exp = panel._parse_iso(p["expireAt"]) if p.get("expireAt") else 0
        except Exception: exp = 0
        sq = p.get("activeInternalSquads")
        L.append(f"Панель: <code>{esc(p.get('username'))}</code>, статус {esc(p.get('status'))}")
        L.append("Срок в панели: " + (B.tfmt(exp) if exp else "не задан") + (f" (в боте {B.tfmt(u['sub_until'])})" if u["sub_until"] else ""))
        L.append("Сквады: " + (", ".join(esc(x.get("name") or x.get("uuid")) if isinstance(x, dict) else esc(x) for x in sq) if sq else ("нет" if sq is not None else "панель не сказала")))
        lim = p.get("hwidDeviceLimit")
        if d.get("devices") is not None:
            L.append(f"Устройства: {len(d['devices'])} из {lim if lim is not None else '?'}")
        elif d.get("devices_err"):
            L.append("Устройства: не удалось получить (" + esc(d["devices_err"])[:120] + ")")
        L.append("Ссылка: " + (f"<code>{esc(p['subscriptionUrl'])}</code>" if p.get("subscriptionUrl") else "нет"))
        if active and p.get("status") != "ACTIVE": problems.append(f"В боте подписка активна, а в панели статус {p.get('status')}. Пусть нажмёт «Подключиться»: бот выровняет. Если статус DISABLED, проверь /unban.")
        if not active and p.get("status") == "ACTIVE": problems.append("Подписка в боте закончилась, а в панели ещё активна. Это самопроходит, или /revoke.")
        if active and exp and abs(exp - u["sub_until"]) > 120: problems.append("Срок в панели расходится с ботом больше чем на 2 минуты. «Подключиться» выровняет.")
        if sq is not None and not sq and active: problems.append("У пользователя нет внутреннего сквада: приложение откроется, но серверов не будет. Назначь сквад в панели.")
        if d.get("devices") is not None and lim and len(d["devices"]) >= lim: problems.append(f"Лимит устройств исчерпан ({len(d['devices'])} из {lim}): новое устройство не получит серверы. /resetdevices {u['id']} освободит.")
    if len(d.get("matches") or []) > 1:
        L.append("")
        L.append(f"С его Telegram ID в панели {len(d['matches'])} записи: " + ", ".join(f"{esc(x.get('username'))} ({esc(x.get('status'))})" for x in d["matches"]))
        problems.append("Дубликаты в панели. Бот берёт запись tg_<id>, лишние лучше удалить в панели.")
    L.append("")
    L.append("<b>Что не так:</b>\n" + "\n".join("• " + x for x in problems) if problems else "Явных проблем не вижу. Если человек всё равно не подключается, пусть пришлёт скриншот ошибки в приложении.")
    await m.answer("\n".join(L), disable_web_page_preview=True)

@router.message(Command("revoke"), is_owner)
async def cmd_revoke(m: Message, command: CommandObject):
    u, rest = await pick_user(m, command, "Формат: /revoke ID да\nПеревыпускает ссылку: старая перестанет работать, человеку нужно добавить новую.")
    if not u: return
    if "да" not in [x.lower() for x in rest]:
        return await m.answer(f"Старая ссылка {u['id']} перестанет работать. Подтвердить: /revoke {u['id']} да")
    ok, url = await panel_do(m, panel.revoke_sub(u["id"]))
    if ok:
        B.alog(m, "revoke", u["id"])
        await m.answer("Ссылка перевыпущена." + (f"\nНовая:\n<code>{esc(url)}</code>" if url else " Новая будет в разделе «Подключиться»."))

@router.message(Command("devices"), is_support)
async def cmd_devices(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /devices ID\nСписок устройств человека.")
    if not u: return
    ok, devs = await panel_do(m, panel.hwid_list(u["id"]))
    if not ok: return
    if not devs: return await m.answer("Устройств нет.")
    await m.answer(f"<b>Устройства {u['id']}: {len(devs)}</b>\n" + "\n".join(f"• {esc(_dev_name(d_))} · <code>{esc(str(d_.get('hwid', ''))[:12])}</code>" for d_ in devs))

@router.message(Command("resetdevices"), is_support)
async def cmd_resetdevices(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /resetdevices ID\nОтвязывает все устройства человека.")
    if not u: return
    ok, _r = await panel_do(m, panel.hwid_reset(u["id"]))
    if ok: B.alog(m, "resetdevices", u["id"]); await m.answer(f"Устройства отвязаны: <code>{u['id']}</code>")

@router.message(Command("devlimit"), is_owner)
async def cmd_devlimit(m: Message, command: CommandObject):
    u, rest = await pick_user(m, command, "Формат: /devlimit ID ЧИСЛО\nЛичный лимит устройств. Пример: /devlimit 123456789 5")
    if not u: return
    try: n = int(rest[0]); assert 1 <= n <= 100
    except Exception: return await m.answer("Формат: /devlimit ID ЧИСЛО (от 1 до 100)")
    ok, _r = await panel_do(m, panel.set_hwid_limit(u["id"], n))
    if ok: B.alog(m, "devlimit", u["id"], str(n)); await m.answer(f"Лимит устройств {u['id']}: {n}")

@router.message(Command("resettraffic"), is_owner)
async def cmd_resettraffic(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /resettraffic ID\nОбнуляет счётчик трафика человека в панели.")
    if not u: return
    ok, _r = await panel_do(m, panel.reset_traffic(u["id"]))
    if ok: B.alog(m, "resettraffic", u["id"]); await m.answer(f"Трафик обнулён: <code>{u['id']}</code>")

@router.message(Command("delete"), is_owner)
async def cmd_delete(m: Message, command: CommandObject):
    u, rest = await pick_user(m, command, "Формат: /delete ID да\nУдаляет человека из базы и из панели. Платежи остаются для учёта.")
    if not u: return
    if u["id"] in config.ADMIN_IDS: return await m.answer("Админа удалить нельзя.")
    if "да" not in [x.lower() for x in rest]:
        return await m.answer(f"Будет удалён {B.who(u)} · <code>{u['id']}</code>: подписка, баланс и доступ в панели пропадут.\nПодтвердить: /delete {u['id']} да", disable_web_page_preview=True)
    try:
        gone = await panel.delete_user(u["id"]); tail = " Из панели удалён." if gone else " В панели его не было."
    except Exception:
        logging.exception("panel delete failed"); tail = " Из панели не удалился: проверь вручную."
    uid = u["id"]
    for sql in ("DELETE FROM promo_uses WHERE user_id=?", "DELETE FROM user_notes WHERE user_id=?", "DELETE FROM survey WHERE user_id=?", "DELETE FROM users WHERE id=?"):
        con.execute(sql, (uid,))
    con.execute("UPDATE users SET ref=NULL WHERE ref=?", (uid,)); con.commit()
    B.alog(m, "delete", uid)
    await m.answer(f"Удалён: <code>{uid}</code>." + tail)

@router.message(Command("panel"), is_owner)
async def cmd_panel(m: Message):
    ok, s = await panel_do(m, panel.system_stats())
    if not ok: return
    s = s or {}; on = s.get("onlineStats") or {}; users = s.get("users") or {}; counts = users.get("statusCounts") or {}; mem = s.get("memory") or {}
    L = ["<b>Панель Remnawave</b>", ""]
    if on:
        L.append(f"Онлайн сейчас: {on.get('onlineNow', '?')}")
        L.append(f"За сутки: {on.get('lastDay', '?')} · за неделю: {on.get('lastWeek', '?')} · ни разу не заходили: {on.get('neverOnline', '?')}")
    if users: L.append(f"Пользователей в панели: {users.get('totalUsers', '?')}" + ("" if not counts else " (" + ", ".join(f"{k}: {v}" for k, v in counts.items()) + ")"))
    if mem.get("total"): L.append(f"Память сервера панели: {gb(mem.get('used'))} из {gb(mem.get('total'))}")
    if s.get("uptime"): L.append(f"Аптайм: {int(s['uptime']) // 86400} дн. {int(s['uptime']) % 86400 // 3600} ч.")
    await m.answer("\n".join(L))

@router.message(Command("nodes"), is_owner)
async def cmd_nodes(m: Message):
    ok, ns = await panel_do(m, panel.get_nodes())
    if not ok: return
    if not ns: return await m.answer("Нод нет.")
    L = [f"<b>Ноды ({len(ns)})</b>", ""]
    for n in ns:
        mark = "выключена" if n.get("isDisabled") else ("работает" if n.get("isConnected") else "нет связи")
        L.append(f"{esc(n.get('name', '?'))} · {mark} · онлайн {n.get('usersOnline') or 0} · трафик {gb(n.get('trafficUsedBytes'))}")
    await m.answer("\n".join(L))

GIVEALL_BUSY = False

async def _giveall(admin_id, ids, days, silent):
    global GIVEALL_BUSY
    GIVEALL_BUSY = True; sent = blocked = ok = bad = 0
    try:
        items = [(uid, db.extend(uid, days)) for uid in ids]
        ok, bad = await panel.sync_many(items) if config.PANEL_URL else (len(items), 0)
        if not silent:
            for uid, until in items:
                if await B.safe_send(uid, ("01_start.png", f"<b>Подарок: +{days} дн.</b>\n\nМы добавили тебе дни к подписке. Теперь она активна до {B.dfmt(until)}.",
                                           kb([("Главное меню", "go:start")]))): sent += 1
                else: blocked += 1
                await asyncio.sleep(0.05)
    finally:
        GIVEALL_BUSY = False
    await tell([admin_id], f"Начисление закончено: +{days} дн. {len(ids)} чел.\nПанель: ок {ok}, ошибок {bad}." + ("" if silent else f"\nСообщений доставлено: {sent}, не дошло: {blocked}."))

@router.message(Command("giveall"), is_owner)
async def cmd_giveall(m: Message, command: CommandObject):
    usage = ("Формат: /giveall ДНИ [active|all] [тихо] да\nНачисляет дни сразу многим (компенсация за сбой).\n"
             "active (по умолчанию): у кого подписка активна, all: всем, кроме заблокированных.\nБез слова «да» только покажет, сколько человек затронет.")
    a = [x.lower() for x in args_of(command)]
    try: days = int(a[0]); assert 1 <= days <= 365
    except Exception: return await m.answer(usage)
    seg = "all" if "all" in a else "active"
    sql = "SELECT id FROM users WHERE COALESCE(banned,0)=0" + (" AND sub_until>?" if seg == "active" else "")
    ids = [r["id"] for r in con.execute(sql, (int(time.time()),) if seg == "active" else ()).fetchall()]
    if "да" not in a: return await m.answer(f"Будет начислено +{days} дн. {'активным' if seg == 'active' else 'всем'}: {len(ids)} чел.\n\nПодтвердить: /giveall {days} {seg} да")
    if GIVEALL_BUSY: return await m.answer("Предыдущее начисление ещё идёт.")
    if not ids: return await m.answer("Некому начислять.")
    B.alog(m, "giveall", seg, f"+{days} дн., {len(ids)} чел.")
    asyncio.create_task(_giveall(m.from_user.id, ids, days, "тихо" in a))
    await m.answer(f"Начисляю +{days} дн.: {len(ids)} чел. Отчёт пришлю сюда.")

# ----- кнопки под карточкой человека (/user) -----
def card_rows(uid):
    return [[("Устройства", f"x:dev:{uid}"), ("Заметка", f"x:note:{uid}")]]

def card_note_line(uid):
    note, _ = get_note(uid)
    return f"Заметка: {esc(note)}" if note else None

@router.callback_query(F.data.startswith("x:"), lambda c: c.from_user.id in config.ADMIN_IDS)
async def cb_x(c: CallbackQuery):
    _, act, uid = c.data.split(":"); uid = int(uid)
    if act == "dev":
        try: devs = await panel.hwid_list(uid)
        except Exception as e: return await answer_cb(c, str(e)[:190], True)
        await answer_cb(c)
        text = f"<b>Устройства {uid}: {len(devs)}</b>\n" + ("\n".join(f"• {esc(_dev_name(d_))}" for d_ in devs) or "Устройств нет.")
        await c.message.answer(text, reply_markup=kb([("Сбросить все", f"x:devreset:{uid}")]) if devs else None)
    elif act == "devreset":
        try: await panel.hwid_reset(uid)
        except Exception as e: return await answer_cb(c, str(e)[:190], True)
        db.log_action(c.from_user.id, "resetdevices", str(uid)); await answer_cb(c, "Устройства отвязаны")
    elif act == "note":
        clear_waits(c.from_user.id); XNOTE[c.from_user.id] = (uid, time.time()); await answer_cb(c)
        await c.message.answer(f"Пришли текст заметки о {uid} одним сообщением. «-» удалит заметку. Отмена: «отмена».")

def _xn_alive(m): return m.from_user.id in XNOTE and not (m.text or "").startswith("/")

@router.message(F.text, _xn_alive)
async def got_xnote(m: Message):
    uid, ts = XNOTE.pop(m.from_user.id); txt = m.text.strip()
    if txt.lower() in ("отмена", "cancel") or time.time() - ts > 900: return await m.answer("Отменено.")
    if txt == "-": set_note(uid, None); return await m.answer("Заметка удалена.")
    set_note(uid, txt[:1000]); await m.answer("Заметка сохранена.")

def start_loops():
    asyncio.create_task(survey_loop())
    asyncio.create_task(block_loop())

# ---------- новые люди, именные промокоды партнёров ----------
def _origin(r):
    """Откуда пришёл человек: кем приглашён, из какого источника или сам."""
    if r["ref"]:
        ru = db.peek_user(r["ref"])
        return "пригласил " + (B.who(ru) if ru else f"<code>{r['ref']}</code>")
    if r["src"]:
        s = db.src_get(r["src"])
        return "источник «" + esc(s["title"] if s else r["src"]) + "»"
    return "сам (без ссылки)"

async def notify_new_user(uid):
    """Короткое сообщение владельцу о новом человеке, если включено: /newnotify on."""
    try:
        if db.kv_get("new_notify") != "1": return
        r = db.peek_user(uid)
        if not r: return
        r = (await B.fill_profiles([r]))[0]
        await tell(config.ADMIN_IDS, f"Новый человек: {B.who(r)} · <code>{uid}</code>\nОткуда: {_origin(r)}")
    except Exception:
        logging.exception("notify_new_user")

@router.message(Command("newnotify"), is_owner)
async def cmd_newnotify(m: Message, command: CommandObject):
    a = (command.args or "").strip().lower()
    if a in ("on", "off"):
        db.kv_set("new_notify", "1" if a == "on" else "0")
        return await m.answer("Сообщения о новых людях включены." if a == "on" else "Сообщения о новых людях выключены.")
    await m.answer("Сейчас: " + ("включены" if db.kv_get("new_notify") == "1" else "выключены") + ".\nФормат: /newnotify on или /newnotify off")

@router.message(Command("new"), is_owner)
async def cmd_new(m: Message, command: CommandObject):
    a = (command.args or "").strip().lower()
    since, limit, label = int(time.time()) - 86400, 60, "за сутки"
    mt = re.fullmatch(r"(\d+)\s*(дн|[чдh])?", a) if a else None
    if a and not mt:
        return await m.answer("Формат:\n/new: новые за сутки\n/new 12ч или /new 7д: за период\n/new 30: последние 30 человек")
    if mt:
        n, unit = int(mt.group(1)), mt.group(2)
        if unit in ("ч", "h"): since, label = int(time.time()) - n * 3600, f"за {n} ч"
        elif unit:             since, label = int(time.time()) - n * 86400, f"за {n} дн."
        else:                  since, limit, label = 0, min(max(n, 1), 100), f"последние {min(max(n, 1), 100)}"
    if since: limit = 100
    rows = db.new_users(since, limit)
    if not rows: return await m.answer(f"Новых людей {label} нет.")
    rows = await B.fill_profiles(rows)
    by = {}
    for r in rows:
        k = "приглашены друзьями" if r["ref"] else (("«" + (db.src_get(r["src"])["title"] if db.src_get(r["src"]) else r["src"]) + "»") if r["src"] else "сами")
        by[k] = by.get(k, 0) + 1
    head = f"<b>Новые {label}: {len(rows)}</b>\n" + " · ".join(f"{esc(k)}: {v}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
    lines = [f'{B.tfmt(r["created"], "%d.%m %H:%M")} · {B.who(r)} · <code>{r["id"]}</code>\n    {_origin(r)} · {B.sub_status(r)}' for r in rows]
    await B.send_lines(m, head, lines, "\n/newnotify on: сообщать о каждом новом сразу", markup=B.card_buttons(rows))

def _find_src(token):
    """Источник по коду, названию или партнёру (@ник / ID). Возвращает (источник, текст_ошибки)."""
    s = db.src_get(token)
    if s: return s, None
    low = token.lower().lstrip("@")
    hit = [x for x in db.src_list() if (x["title"] or "").lower() == token.lower()]
    if len(hit) == 1: return hit[0], None
    u = B.resolve_user(token)
    if u:
        own = db.partner_sources(u["id"])
        if len(own) == 1: return own[0], None
        if len(own) > 1: return None, "У этого партнёра несколько ссылок, укажи код нужной: /sources"
    return None, "Не нашёл такую партнёрскую ссылку. Код, название или партнёр (@ник, ID). Список: /sources"

@router.message(Command("partnerpromo", "pp"), is_owner)
async def cmd_partnerpromo(m: Message, command: CommandObject):
    usage = ("Формат: /partnerpromo КОД ДНИ ПАРТНЁР [МАКС_АКТИВАЦИЙ] [ДО_ДАТЫ]\n"
             "ПАРТНЁР: код ссылки, её название или @ник/ID партнёра (ссылку создать: /sources).\n"
             "Примеры:\n/partnerpromo IVAN7 7 @ivan\n/partnerpromo IVAN7 7 @ivan 200 31.12.2026\n"
             "Кто введёт код, засчитается этому партнёру. Статистика: /promostat")
    a = (command.args or "").split()
    try:
        code, days, who_ = a[0].upper(), int(a[1]), a[2]; assert days > 0 and re.fullmatch(r"[A-Z0-9_-]{3,32}", code)
        uses, exp, name = 0, 0, [a[2]]
        for t in a[3:]:
            if B.DATE_RE.match(t):
                exp = B.end_of_day(t); assert exp and exp > time.time()
            elif t.isdigit():
                uses = int(t)
            else:
                name.append(t)   # название ссылки из нескольких слов
        who_ = " ".join(name)
    except Exception:
        return await m.answer(usage)
    s, err = _find_src(who_)
    if err: return await m.answer(err)
    old = con.execute("SELECT src FROM promo_codes WHERE code=?", (code,)).fetchone()
    if old and not old["src"]: return await m.answer("Такой обычный промокод уже есть. Возьми другой код или удали старый: /delpromo.")
    if old and old["src"] != s["code"]: return await m.answer("Этот код уже привязан к другому партнёру. Возьми другой.")
    db.create_promo(code, days, uses, exp, src=s["code"])
    B.alog(m, "partnerpromo", code, f"+{days} дн. · {s['title']}")
    await m.answer(f"Готово: <code>{code}</code> даёт +{days} дн." + (f", максимум {uses} активаций" if uses else ", без лимита активаций")
                   + (f", до {B.dfmt(exp)} включительно" if exp else ", без срока")
                   + f".\nПартнёр: {esc(s['title'])}. Все, кто введёт код, попадут в его статистику (если их никто не приглашал и они не пришли по другой ссылке).\n"
                   "Смотреть: /promostat")

@router.message(Command("promostat"), is_owner)
async def cmd_promostat(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    srcs = db.src_list()
    if arg:
        s, err = _find_src(arg)
        if err: return await m.answer(err)
        srcs = [s]
    L, found = [], 0
    for s in srcs:
        prs = db.src_promos(s["code"])
        if not prs: continue
        found += 1
        st = db.src_stats(s["code"])
        L.append(f"<b>{esc(s['title'])}</b>: пришло {st['regs']}, оплатили {st['payers']}, выручка {rub(st['revenue'])} ₽")
        for p in prs:
            exp = f", до {B.dfmt(p['expires'])}" if p["expires"] else ""
            L.append(f"    <code>{p['code']}</code> +{p['days']} дн.: активаций {p['used']}" + (f" из {p['max_uses']}" if p["max_uses"] else "")
                     + f", оплатили {p['payers']} на {rub(p['revenue'])} ₽{exp}")
        L.append("")
    if not found:
        return await m.answer("Именных промокодов пока нет. Создай: /partnerpromo КОД ДНИ ПАРТНЁР")
    await m.answer("\n".join(L).strip(), disable_web_page_preview=True)

@router.message(Command("panelraw"), is_owner)
async def cmd_panelraw(m: Message, command: CommandObject):
    u, _ = await pick_user(m, command, "Формат: /panelraw ID\nПоказывает, что именно панель отвечает про человека (ничего не меняет). Нужно, чтобы разобраться со сбоями устройств.")
    if not u: return
    try:
        rows = await panel.raw_dump(u["id"])
    except Exception as e:
        return await m.answer(f"Панель не ответила: {esc(e)}")
    await m.answer("\n\n".join(f"<b>{esc(a)}</b> · HTTP {b}\n<code>{esc(c)}</code>" for a, b, c in rows))

@router.message(Command("ykcheck"), is_owner)
async def cmd_ykcheck(m: Message):
    import payments
    if config.DEV_PAY: return await m.answer("Включён тестовый режим (DEV_PAY=1), ЮKassa не используется.")
    L = []
    try:
        d = await payments.account_info()
        L += [f"Магазин: <code>{esc(d.get('account_id'))}</code>, статус: {esc(d.get('status'))}" + (", ТЕСТОВЫЙ" if d.get("test") else ""),
              "Способы оплаты, разрешённые магазину: " + (", ".join(esc(x) for x in d.get("payment_methods", [])) or "список не пришёл"),
              "Чеки (54-ФЗ) в ЮKassa: " + ("включены" if d.get("fiscalization_enabled") else "выключены")]
    except Exception as e:
        L.append(f"ЮKassa не принимает ключи: {esc(e)}\nПроверь YK_SHOP_ID и YK_SECRET.")
    le = payments.LAST_ERROR
    L.append("Последняя ошибка создания счёта: " + (f"{B.tfmt(le['ts'])}: {esc(le['text'])}" if le["ts"] else "не было с запуска бота"))
    L.append(f"Режим чеков бота: RECEIPT_MODE={esc(config.RECEIPT_MODE)} · автопродление: {'вкл' if config.AUTORENEW else 'выкл'}")
    await m.answer("\n".join(L))

# ----- меню команд -----
OWNER_MENU = [("take", "Снять дни"), ("giveall", "Дни всем (компенсация)"), ("payouts", "Заявки на вывод"), ("tickets", "Обращения"),
              ("panel", "Панель: онлайн"), ("ykcheck", "Проверка ЮKassa"), ("panelraw", "Что отвечает панель про человека"), ("nodes", "Состояние нод"), ("devices", "Устройства человека"), ("funnel", "Воронка"),
              ("top", "Топы"), ("new", "Новые люди и откуда"), ("partnerpromo", "Именной промокод партнёра"),
              ("promostat", "Статистика промокодов партнёров"), ("maintenance", "Режим техработ"), ("export", "Выгрузка в CSV")]
SUPPORT_MENU = [("tickets", "Обращения"), ("reply", "Ответить на обращение"), ("devices", "Устройства человека"), ("resetdevices", "Сбросить устройства")]

HELP_OWNER = (
    "\n<b>Подписка и люди</b>\n/take ID ДНИ: снять дни\n/setsub ID ДД.ММ.ГГГГ: точная дата конца (0 = обнулить)\n"
    "/giveall ДНИ [active|all] да: дни всем (компенсация)\n/resettrial ID: вернуть пробник\n/msg ID ТЕКСТ: личное сообщение\n"
    "/note ID [текст]: заметка (видна в /user)\n/refs ID: кого пригласил человек\n/delete ID да: удалить человека\n"
    "\n<b>Вывод денег</b>\n/payouts: заявки\n/payoutok НОМЕР: отметить выплаченной\n/payoutno НОМЕР причина: отклонить, деньги вернутся\n"
    "Заявки приходят сами с кнопками. Условия: минимум и холд задаются PAYOUT_MIN и PAYOUT_HOLD_DAYS.\n"
    "\n<b>Поддержка</b>\n/tickets: открытые обращения\n/reply НОМЕР текст · /close НОМЕР · /ticketstats\n/surveystats: почему не продлевают\n"
    "\n<b>VPN и панель</b>\n/panel: онлайн и пользователи\n/nodes: состояние нод\n/sub ID: ссылка человека\n/revoke ID да: перевыпустить ссылку\n"
    "/devices ID · /resetdevices ID · /devlimit ID N · /resettraffic ID\n"
    "\n<b>Новые люди и партнёры</b>\n/new [12ч|7д|30]: кто пришёл и откуда (приглашён, источник или сам)\n/newnotify on|off: сообщать о каждом новом сразу\n"
    "/partnerpromo КОД ДНИ ПАРТНЁР [МАКС] [ДАТА]: именной промокод партнёра\n/promostat [ПАРТНЁР]: сколько пришло и заплатило по его кодам\n"
    "\n/panelraw ID: сырые ответы панели про человека (для разбора сбоев)\n\n<b>Оплата</b>\n/ykcheck: проверить ключи ЮKassa и показать причину последней ошибки счёта\n"
    "\n<b>Бот</b>\n/maintenance on|off [текст]: режим техработ\n/funnel: воронка\n/top: топ приглашающих и платящих\n/export: пользователи в CSV")
HELP_SUPPORT = "\n/tickets: открытые обращения\n/reply НОМЕР текст: ответить\n/close НОМЕР: закрыть\n/devices ID · /resetdevices ID\n/note ID [текст]: заметка\n/msg ID текст: сообщение человеку"
