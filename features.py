"""Дополнения v3: автопродление, оплата Stars и CryptoBot, возврат ушедших, антифрод рефералки, история баланса,
отложенные рассылки с кнопками, подарок по @нику, статус серверов и трафик в профиле.

Подключается из bot.py: features.init(модуль_bot), dp.include_router(features.router), features.start_loops().
Всё нужное от bot.py (show, deliver_paid, who, resolve_user и т.д.) берётся через B.
"""
import asyncio, calendar, csv, html, io, logging, math, time
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)

import config, db, monitor, panel, payments, screens

router = Router()
con = db.con
B = None   # модуль bot.py, заполняется в init()

def is_admin(m): return m.from_user.id in config.ADMIN_IDS
def esc(s): return html.escape(str(s if s is not None else ""))
def kb(*rows): return screens.kb(*rows)

def init(mod):
    global B
    B = mod
    con.executescript("""
    CREATE TABLE IF NOT EXISTS balance_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER, d_topup REAL, d_ref REAL,
      topup REAL, ref REAL, note TEXT);
    CREATE INDEX IF NOT EXISTS ix_balance_log_user ON balance_log(user_id, id);
    CREATE TRIGGER IF NOT EXISTS trg_balance_log AFTER UPDATE OF balance, topup ON users
    WHEN ROUND(COALESCE(OLD.balance,0) - COALESCE(NEW.balance,0), 2) != 0 OR ROUND(COALESCE(OLD.topup,0) - COALESCE(NEW.topup,0), 2) != 0
    BEGIN
      INSERT INTO balance_log(ts, user_id, d_topup, d_ref, topup, ref, note) VALUES(
        CAST(strftime('%s','now') AS INTEGER), NEW.id,
        ROUND(COALESCE(NEW.topup,0) - COALESCE(OLD.topup,0), 2), ROUND(COALESCE(NEW.balance,0) - COALESCE(OLD.balance,0), 2),
        COALESCE(NEW.topup,0), COALESCE(NEW.balance,0), (SELECT v FROM kv WHERE k='bal_ctx'));
    END;
    CREATE TABLE IF NOT EXISTS renew_try(
      user_id INTEGER, sub_until INTEGER, n INTEGER DEFAULT 0, ts INTEGER, PRIMARY KEY(user_id, sub_until));
    CREATE TABLE IF NOT EXISTS sched_bcast(
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_at INTEGER, seg TEXT, text TEXT, photo TEXT, admin_id INTEGER, status TEXT, created INTEGER);
    """)
    con.commit()

def start_loops():
    asyncio.create_task(renew_loop())
    asyncio.create_task(winback_loop())
    asyncio.create_task(sched_loop())

# ================= вспомогательное =================
def kv_get(k):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None

def kv_set(k, v):
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v))); con.commit()

def kv_del(k):
    con.execute("DELETE FROM kv WHERE k=?", (k,)); con.commit()

class bal_ctx:
    """Подпись для истории баланса: with features.bal_ctx("админ 123: /takeref"): ...изменение баланса..."""
    def __init__(self, note): self.note = note
    def __enter__(self):
        con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('bal_ctx',?)", (self.note,))
    def __exit__(self, *a):
        con.execute("DELETE FROM kv WHERE k='bal_ctx'"); con.commit()

def rub(x): return f"{x:,.0f}".replace(",", " ")

# ================= автопродление =================
def ar_screen(u):
    price = None
    last = db.last_plan(u["id"])
    if last in config.PLANS: price = db.price_for(u, last)
    on = bool(u["autorenew"]); card = u["pm_title"] if u["pm_id"] else None
    L = ["<b>Автопродление</b>", "",
         f"Статус: <b>{'включено' if on else 'выключено'}</b>",
         f"Карта: {esc(card) if card else 'не привязана'}"]
    if last in config.PLANS:
        L.append(f"Спишем при продлении: {config.PLANS[last][2]} · {rub(price)} ₽")
    L += ["", f"За {config.AUTORENEW_NOTICE_DAYS} дн. до конца подписки мы напишем, а в день окончания спишем деньги с карты и продлим подписку. "
              "Отключить можно в любой момент здесь же."]
    if not card:
        L += ["", "Чтобы привязать карту: включи автопродление и оплати подписку банковской картой. Карта сохранится у платёжного сервиса, мы её номер не видим."]
    rows = [[("Выключить" if on else "Включить", "ar:off" if on else "ar:on")]]
    if card: rows.append([("Отвязать карту", "ar:unlink")])
    rows.append([("Назад", "go:profile")])
    return ("07_profile.png", "\n".join(L), kb(*rows))

@router.callback_query(F.data.in_({"ar:open", "ar:on", "ar:off", "ar:unlink"}))
async def cb_ar(c: CallbackQuery):
    if not config.AUTORENEW: return await c.answer("Автопродление недоступно.", show_alert=True)
    uid = c.from_user.id; act = c.data[3:]; db.get_user(uid)
    if act in ("on", "off"):
        con.execute("UPDATE users SET autorenew=? WHERE id=?", (1 if act == "on" else 0, uid)); con.commit()
    elif act == "unlink":
        con.execute("UPDATE users SET pm_id=NULL, pm_title=NULL, autorenew=0 WHERE id=?", (uid,)); con.commit()
    await B.show(c, ar_screen(db.get_user(uid)))
    await c.answer({"on": "Включено", "off": "Выключено", "unlink": "Карта отвязана"}.get(act))

@router.callback_query(F.data.startswith("ar:t:"))
async def cb_ar_toggle(c: CallbackQuery):
    """Переключатель на экране оплаты."""
    if not config.AUTORENEW: return await c.answer()
    plan = c.data.split(":")[2]
    if plan not in config.PLANS: return await c.answer()
    uid = c.from_user.id; u = db.get_user(uid)
    con.execute("UPDATE users SET autorenew=? WHERE id=?", (0 if u["autorenew"] else 1, uid)); con.commit()
    await B.show(c, screens.payment(db.get_user(uid), plan)); await c.answer()

async def after_paid(pid):
    """После оплаты картой забираем у ЮKassa сохранённый способ оплаты (если человек включил автопродление)."""
    try:
        p = db.get_payment(pid)
        if not (p and config.AUTORENEW) or config.DEV_PAY: return
        if p["gift"] or p["plan"] == "topup" or p["method"] in ("balance", "stars", "autorenew"): return
        yk = p["yk_id"] or ""
        if not yk or yk[:3] in ("cb:", "tg:"): return
        data = await payments.get(yk)
        pm = data.get("payment_method") or {}
        if pm.get("saved") and pm.get("id"):
            last4 = (pm.get("card") or {}).get("last4")
            title = f"•••• {last4}" if last4 else (pm.get("title") or "карта")
            con.execute("UPDATE users SET pm_id=?, pm_title=?, autorenew=1 WHERE id=?", (pm["id"], title, p["user_id"])); con.commit()
            await B.safe_send(p["user_id"], ("07_profile.png",
                f"<b>Автопродление включено</b>\n\nКарта {esc(title)} привязана. За {config.AUTORENEW_NOTICE_DAYS} дн. до конца подписки мы напишем, а потом спишем оплату.",
                kb([("Настроить", "ar:open")], [("Главное меню", "go:start")])))
    except Exception:
        logging.exception("after_paid autorenew")

async def renew_failed(uid):
    """Автосписание не прошло: говорим человеку, после второй неудачи выключаем."""
    u = db.get_user(uid)
    r = con.execute("SELECT n FROM renew_try WHERE user_id=? AND sub_until=?", (uid, u["sub_until"])).fetchone()
    last_try = (r["n"] if r else 1) >= 2
    if last_try:
        con.execute("UPDATE users SET autorenew=0 WHERE id=?", (uid,)); con.commit()
    await B.safe_send(uid, ("11_error.png",
        "<b>Не удалось продлить автоматически</b>\n\nБанк отклонил списание" + (" второй раз, автопродление выключено." if last_try else ", попробуем ещё раз позже.")
        + " Чтобы доступ не прервался, продли вручную.", kb(*screens._renew(u), [("Автопродление", "ar:open")])))

async def renew_one(u):
    uid = u["id"]; plan = db.last_plan(uid)
    if plan not in config.PLANS: return
    if config.RECEIPT_MODE == "email" and not config.DEV_PAY and not u["email"]: return
    now = int(time.time())
    con.execute("INSERT INTO renew_try(user_id,sub_until,n,ts) VALUES(?,?,1,?) ON CONFLICT(user_id,sub_until) DO UPDATE SET n=n+1, ts=?",
                (uid, u["sub_until"], now, now)); con.commit()
    pid = db.create_payment(uid, plan, "autorenew")
    if config.DEV_PAY:
        await B.deliver_paid(pid); return
    try:
        st = await payments.charge_saved(pid, uid, u["pm_id"], u["email"])
    except Exception:
        logging.exception("autorenew charge failed uid=%s", uid)
        if db.cancel_payment(pid): await renew_failed(uid)
        return
    if st == "succeeded": await B.finalize_if_paid(pid)
    else: asyncio.create_task(B.watch(pid))

async def renew_loop():
    while True:
        try:
            if config.AUTORENEW:
                now = int(time.time())
                rows = con.execute("SELECT * FROM users WHERE autorenew=1 AND pm_id IS NOT NULL AND COALESCE(banned,0)=0 AND sub_until>? AND sub_until<?",
                                   (now - 3 * 86400, now + config.AUTORENEW_NOTICE_DAYS * 86400)).fetchall()
                for u in rows:
                    left = u["sub_until"] - now
                    t = con.execute("SELECT n, ts FROM renew_try WHERE user_id=? AND sub_until=?", (u["id"], u["sub_until"])).fetchone()
                    if left <= 12 * 3600:   # пора списывать: не больше двух попыток, вторая не раньше чем через 12 часов
                        if t is None or (t["n"] < 2 and now - t["ts"] > 12 * 3600):
                            await renew_one(u)
                    elif t is None:
                        key = f"arnotice:{u['sub_until']}"
                        if not con.execute("SELECT 1 FROM auto_sent WHERE user_id=? AND kind=?", (u["id"], key)).fetchone():
                            db.mark_auto(u["id"], key)
                            plan = db.last_plan(u["id"])
                            if plan in config.PLANS:
                                await B.safe_send(u["id"], ("09_expiring.png",
                                    f"<b>Скоро автопродление</b>\n\n{B.dfmt(u['sub_until'])} спишем {rub(db.price_for(u, plan))} ₽ с карты {esc(u['pm_title'] or '')} за «{config.PLANS[plan][2]}».",
                                    kb([("Отключить автопродление", "ar:off")], [("Главное меню", "go:start")])))
                    await asyncio.sleep(0.2)
        except Exception:
            logging.exception("renew_loop")
        await asyncio.sleep(600)

# ================= Telegram Stars и CryptoBot =================
def stars_for(price): return max(1, math.ceil(price / config.STARS_RUB_PER_STAR))

async def alt_payment(c, plan, method, gift=False, amount=None):
    """Оплата Stars или криптой. plan='topup' = пополнение баланса на amount ₽."""
    uid = c.from_user.id; u = db.get_user(uid)
    if (method == "stars" and config.STARS_RUB_PER_STAR <= 0) or (method == "crypto" and not config.CRYPTO_TOKEN):
        return await B.show(c, screens.error(u))
    pid = db.create_topup(uid, amount, method) if plan == "topup" else db.create_payment(uid, plan, method, gift)
    price = db.get_payment(pid)["amount"]; name = config.plan_name(plan)
    label = ("Подарок: " if gift else "") + name
    try:
        if method == "stars":
            stars = stars_for(price)
            await B.bot.send_invoice(chat_id=uid, title=f"NQYRO · {label}"[:32], description=f"{label}. Оплата Telegram Stars."[:255],
                                     payload=f"pid:{pid}", provider_token="", currency="XTR",
                                     prices=[LabeledPrice(label=label[:30], amount=stars)])
        else:
            url = await crypto_create(pid, price, f"NQYRO: {label}")
            asyncio.create_task(B.watch(pid))
            await B.show(c, screens.pay_link(u, url, pid))
    except Exception:
        logging.exception("alt payment failed")
        await B.alert_once("alt_pay", f"Не удалось создать счёт ({method}), см. логи. Проверь настройки оплаты: /ykcheck")
        await B.show(c, screens.error(u))

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    try:
        pid = int(q.invoice_payload.split(":")[1]); p = db.get_payment(pid)
        ok = bool(p) and p["status"] == "pending" and p["user_id"] == q.from_user.id and p["method"] == "stars"
    except Exception: pass
    await q.answer(ok=ok, error_message=None if ok else "Счёт устарел. Создай новый в боте.")

@router.message(F.successful_payment)
async def got_stars(m: Message):
    sp = m.successful_payment
    try: pid = int(sp.invoice_payload.split(":")[1])
    except Exception: return
    p = db.get_payment(pid)
    if not p or p["user_id"] != m.from_user.id: return
    db.set_yk_id(pid, "tg:" + sp.telegram_payment_charge_id)
    await B.deliver_paid(pid)

async def _crypto(method, path, **kw):
    async with aiohttp.ClientSession(headers={"Crypto-Pay-API-Token": config.CRYPTO_TOKEN}, timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.request(method, config.CRYPTO_API + path, **kw) as r:
            data = await r.json(content_type=None)
            if not data.get("ok"):
                raise RuntimeError(f"CryptoBot: {data.get('error') or r.status}")
            return data["result"]

async def crypto_create(pid, price, desc):
    res = await _crypto("POST", "/createInvoice", json={
        "currency_type": "fiat", "fiat": "RUB", "amount": f"{price}", "accepted_assets": config.CRYPTO_ASSETS,
        "description": desc[:1000], "payload": str(pid), "expires_in": 3600, "allow_comments": False, "allow_anonymous": False})
    db.set_yk_id(pid, "cb:" + str(res["invoice_id"]))
    return res.get("bot_invoice_url") or res.get("pay_url")

async def finalize_crypto(p, quiet=False):
    """Проверка счёта CryptoBot. True, если платёж завершён."""
    try:
        res = await _crypto("GET", "/getInvoices", params={"invoice_ids": p["yk_id"][3:]})
        items = res.get("items") if isinstance(res, dict) else res
        st = items[0]["status"] if items else None
    except Exception:
        logging.exception("crypto status failed")
        return False
    if st == "paid":
        await B.deliver_paid(p["id"]); return True
    if st == "expired":
        if quiet: return False
        if db.cancel_payment(p["id"]):
            await B.safe_send(p["user_id"], screens.error(db.get_user(p["user_id"])))
        return True
    return False

# ================= подарок по @нику =================
WAIT_NICK = {}   # id -> время: ждём @ник получателя

def gift_nick_prompt():
    return ("04_plans.png", "<b>Подарок по @нику</b>\n\nПришли @ник друга или его числовой ID. Друг должен хотя бы раз запускать бота, иначе я не смогу ему написать.\n\nОтмена: кнопка «Назад».",
            kb([("Назад", "go:gift")]))

@router.callback_query(F.data == "gn:start")
async def cb_gn_start(c: CallbackQuery):
    WAIT_NICK[c.from_user.id] = time.time()
    B.WAIT_TOPUP.discard(c.from_user.id); B.WAIT_PROMO.discard(c.from_user.id)
    await B.show(c, gift_nick_prompt()); await c.answer()

@router.message(F.text, lambda m: time.time() - WAIT_NICK.get(m.from_user.id, 0) < 600 and not m.text.startswith("/"))
async def got_nick(m: Message):
    uid = m.from_user.id; t = m.text.strip()
    if not (t.startswith("@") or t.lstrip("-").isdigit()):
        return await m.answer("Пришли @ник (с собакой) или числовой ID.")
    r = B.resolve_user(t)
    if not r: return await m.answer("Не нашёл такого человека. Он должен хотя бы раз запускать бота. Можно подарить кодом: «Подарить подписку» без ника.")
    if r["id"] == uid: return await m.answer("Себе подарок делать не нужно, купи подписку обычным способом.")
    WAIT_NICK.pop(uid, None)
    name = (r["name"] or "").strip() or ("@" + r["username"] if r["username"] else str(r["id"]))
    rid = r["id"]
    await B.show(m, ("04_plans.png", f"<b>Подарок для {esc(name)}</b>\n\nВыбери срок. После оплаты подписка добавится ему сама, а я ему напишу.",
        kb(*[[(f"{config.PLANS[k][2]} · {config.PLANS[k][0]} ₽", f"gn:p:{k}:{rid}")] for k in config.PLANS], [("Назад", "go:gift")])))

@router.callback_query(F.data.startswith("gn:p:"))
async def cb_gn_plan(c: CallbackQuery):
    try:
        _, _, plan, rid = c.data.split(":"); rid = int(rid)
    except ValueError: return await c.answer()
    if plan not in config.PLANS or not db.peek_user(rid) or rid == c.from_user.id: return await c.answer()
    kv_set(f"giftto:{c.from_user.id}", f"{rid}|{int(time.time())}")
    await B.show(c, screens.payment(db.get_user(c.from_user.id), plan, gift=True)); await c.answer()

def clear_gift_target(uid): kv_del(f"giftto:{uid}")

async def gift_after(uid, code, plan, scr):
    """Если подарок покупался «по @нику», сразу активирует код у получателя. Возвращает экран для дарителя."""
    v = kv_get(f"giftto:{uid}")
    if not v: return scr
    kv_del(f"giftto:{uid}")
    try:
        rid, ts = (int(x) for x in v.split("|"))
        if time.time() - ts > 3600: return scr
        status, days, until = db.use_promo(rid, code)
        if status != "ok": return scr
        await panel.set_expiry(rid, until)
        s = db.get_user(uid)
        frm = (s["name"] or "").strip() or ("@" + s["username"] if s["username"] else "друг")
        await B.bot.send_message(rid, f"<b>Тебе подарили подписку</b>\n\n{esc(frm)} дарит тебе {days} дн. NQYRO. Уже работает.")
        await B.safe_send(rid, screens.promo_ok(db.get_user(rid), days, until))
        r = db.get_user(rid); nm = (r["name"] or "").strip() or ("@" + r["username"] if r["username"] else str(rid))
        return ("06_success.png", f"<b>Подарок отправлен</b>\n\n{esc(nm)} получил {days} дн. подписки, я ему уже написал.", kb([("Главное меню", "go:start")]))
    except Exception:
        logging.exception("gift_after")
        return scr

# ================= возврат ушедших =================
async def winback_loop():
    while True:
        try:
            now = int(time.time())
            con.execute("UPDATE users SET disc_kind=NULL, disc_value=0, disc_code=NULL, disc_until=0 "
                        "WHERE disc_kind IS NOT NULL AND disc_until>0 AND disc_until<?", (now,)); con.commit()
            if config.WINBACK_PERCENT > 0:
                hi = now - config.WINBACK_AFTER_DAYS * 86400
                lo = hi - config.WINBACK_WINDOW_DAYS * 86400
                rows = con.execute(
                    "SELECT * FROM users u WHERE COALESCE(u.banned,0)=0 AND u.sub_until>? AND u.sub_until<=? AND u.disc_kind IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM auto_sent a WHERE a.user_id=u.id AND a.kind='winback:'||u.sub_until)", (lo, hi)).fetchall()
                for u in rows:
                    db.mark_auto(u["id"], f"winback:{u['sub_until']}")
                    until = now + config.WINBACK_HOURS * 3600
                    con.execute("UPDATE users SET disc_kind='percent', disc_value=?, disc_code=?, disc_until=? WHERE id=?",
                                (config.WINBACK_PERCENT, f"WB{u['sub_until']}", until, u["id"])); con.commit()
                    u2 = db.get_user(u["id"])
                    await B.safe_send(u["id"], ("09_expiring.png",
                        f"<b>Возвращайся со скидкой {config.WINBACK_PERCENT}%</b>\n\nСкидка только для тебя, действует до {B.tfmt(until, '%d.%m %H:%M')}. Цена на кнопках уже со скидкой.",
                        kb(*screens._renew(u2), [("Главное меню", "go:start")])))
                    await asyncio.sleep(0.05)
        except Exception:
            logging.exception("winback_loop")
        await asyncio.sleep(1800)

# ================= антифрод рефералки =================
def fraud_info(uid):
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
    inv = con.execute("SELECT id, created, trial_used, name, username FROM users WHERE ref=? ORDER BY created", (uid,)).fetchall()
    n = len(inv)
    real = "p.status='paid' AND p.method!='balance' AND p.plan!='topup'"
    paid_ids = {r[0] for r in con.execute(f"SELECT DISTINCT p.user_id FROM payments p JOIN users u ON u.id=p.user_id WHERE u.ref=? AND {real}", (uid,)).fetchall()}
    trial_only = sum(1 for r in inv if r["trial_used"] and r["id"] not in paid_ids)
    noname = sum(1 for r in inv if not (r["username"] or "") and (r["name"] or "") in ("", "(профиль недоступен)", "(без имени)"))
    burst, j = 0, 0   # максимум приглашённых в окне 10 минут
    ts = [r["created"] or 0 for r in inv]
    for i in range(len(ts)):
        while ts[i] - ts[j] > 600: j += 1
        burst = max(burst, i - j + 1)
    refunded = q("SELECT COUNT(*) FROM payments WHERE ref_to=? AND refunded_at IS NOT NULL", uid)
    why, score = [], 0
    if burst >= 3: score += 2; why.append(f"{burst} приглашённых за 10 минут")
    if n >= 5 and not paid_ids: score += 2; why.append(f"{n} приглашённых и никто не платил")
    if n >= 5 and trial_only / n > 0.8: score += 1; why.append(f"{trial_only} из {n} взяли только пробник")
    if n >= 4 and noname / n > 0.5: score += 1; why.append(f"у {noname} из {n} нет имени и ника")
    if refunded: score += 2; why.append(f"возвратов по их платежам: {refunded}")
    return {"n": n, "paid": len(paid_ids), "score": score, "why": why}

def fraud_line(uid):
    f = fraud_info(uid)
    head = f"Приглашено: {f['n']}, платили: {f['paid']}"
    if f["score"] >= 2: return f"<b>Антифрод: подозрительно</b> (балл {f['score']}). {head}.\n- " + "\n- ".join(f["why"])
    return f"Антифрод: без замечаний. {head}." + (f" Мелочь: {'; '.join(f['why'])}." if f["why"] else "")

@router.message(Command("fraud"), is_admin)
async def cmd_fraud(m: Message, command: CommandObject):
    u = B.resolve_user(command.args or "")
    if not u: return await m.answer("Формат: /fraud ID или @ник\nПроверяет, не накручивает ли человек рефералов.")
    blocked = "да" if kv_get("wdblock:%s" % u["id"]) else "нет"
    await m.answer(f"{B.who(u)} · <code>{u['id']}</code>\n{fraud_line(u['id'])}\nВывод заблокирован: {blocked}", disable_web_page_preview=True)

@router.message(Command("wdblock", "wdunblock"), is_admin)
async def cmd_wdblock(m: Message, command: CommandObject):
    u = B.resolve_user(command.args or "")
    if not u: return await m.answer("Формат: /wdblock ID или @ник: запретить человеку вывод\n/wdunblock ID: разрешить")
    if command.command == "wdblock": kv_set(f"wdblock:{u['id']}", 1)
    else: kv_del(f"wdblock:{u['id']}")
    B.alog(m, command.command, u["id"])
    await m.answer(("Вывод заблокирован: " if command.command == "wdblock" else "Вывод разрешён: ") + f"<code>{u['id']}</code>")

# ================= история баланса =================
def _infer(r):
    if r["note"]: return r["note"]
    a = con.execute("SELECT action FROM admin_log WHERE target=? AND ABS(ts-?)<=3 ORDER BY id DESC LIMIT 1", (str(r["user_id"]), r["ts"])).fetchone()
    if a: return f"админ: {a['action']}"
    dt, dr = r["d_topup"], r["d_ref"]
    if dr > 0 and not dt: return "реферальный бонус"
    if dt > 0 and not dr: return "пополнение"
    if dt < 0 or dr < 0: return "оплата с баланса / вывод"
    return ""

def _sg(x): return f"{x:+.0f}" if x else "0"

@router.message(Command("balog"), is_admin)
async def cmd_balog(m: Message, command: CommandObject):
    u = None; n = 20
    for x in (command.args or "").split():
        if x.isdigit() and len(x) <= 3: n = int(x)   # короткое число = сколько записей
        else:
            u = B.resolve_user(x)
            if not u: return await m.answer(B.NOT_FOUND)
    n = max(1, min(n, 60))
    rows = (con.execute("SELECT * FROM balance_log WHERE user_id=? ORDER BY id DESC LIMIT ?", (u["id"], n)) if u
            else con.execute("SELECT * FROM balance_log ORDER BY id DESC LIMIT ?", (n,))).fetchall()
    if not rows: return await m.answer("Записей пока нет. История ведётся с момента обновления бота.")
    L = []
    for r in rows:
        L.append(f"<b>#{r['id']}</b> {B.tfmt(r['ts'], '%d.%m %H:%M')} · <code>{r['user_id']}</code> · осн. {_sg(r['d_topup'])} (={r['topup']:.0f}) · реф. {_sg(r['d_ref'])} (={r['ref']:.0f}) · {esc(_infer(r))}")
    head = f"<b>История баланса{(' ' + str(u['id'])) if u else ''}</b>\n\n"
    text = head + "\n".join(L) + "\n\nОткатить запись: /balrevert НОМЕР"
    for i in range(0, len(text), 3800): await m.answer(text[i:i + 3800])

@router.message(Command("balrevert"), is_admin)
async def cmd_balrevert(m: Message, command: CommandObject):
    try: rid = int((command.args or "").strip())
    except ValueError: return await m.answer("Формат: /balrevert НОМЕР (номер из /balog)")
    r = con.execute("SELECT * FROM balance_log WHERE id=?", (rid,)).fetchone()
    if not r: return await m.answer("Нет такой записи.")
    if con.execute("SELECT 1 FROM balance_log WHERE note=?", (f"откат #{rid}",)).fetchone():
        return await m.answer("Эта запись уже откатывалась.")
    with bal_ctx(f"откат #{rid}"):
        con.execute("UPDATE users SET topup=MAX(0,COALESCE(topup,0)-?), balance=MAX(0,COALESCE(balance,0)-?) WHERE id=?",
                    (r["d_topup"], r["d_ref"], r["user_id"]))
    B.alog(m, "balrevert", r["user_id"], f"#{rid}")
    u = db.get_user(r["user_id"])
    await m.answer(f"Откатил #{rid}. У <code>{u['id']}</code> теперь основной {u['topup']:.0f} ₽, реферальный {u['balance']:.0f} ₽.")

# ================= рассылки: кнопки, отложенная отправка =================
BTN_MAP = {k: f"go:{k}" for k in ("plans", "topup", "profile", "friends", "connect", "support", "start", "gift")}

def split_buttons(text):
    """Строки вида «>> Текст | plans» (или «>> Текст | https://…») превращаются в кнопки. Возвращает (текст, разметка или None)."""
    keep, rows = [], []
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith(">>") and "|" in s:
            label, target = (x.strip() for x in s[2:].rsplit("|", 1))
            if label and target.lower() in BTN_MAP:
                rows.append([InlineKeyboardButton(text=label[:60], callback_data=BTN_MAP[target.lower()])]); continue
            if label and target.lower().startswith(("https://", "http://", "tg://")):
                rows.append([InlineKeyboardButton(text=label[:60], url=target)]); continue
        keep.append(line)
    return "\n".join(keep).strip(), (InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

def _parse_when(tokens):
    """['29.10', '12:30'] | ['29.10.2026', '12:30'] | ['завтра', '12:30'] -> метка времени (по TZ_HOURS) или None."""
    if len(tokens) != 2: return None
    d, t = tokens[0].lower(), tokens[1]
    now_local = datetime.fromtimestamp(time.time() + config.TZ_HOURS * 3600, timezone.utc).replace(tzinfo=None)
    try:
        hh, mm = (int(x) for x in t.split(":"))
        if d in ("сегодня", "today"): day = now_local.date()
        elif d in ("завтра", "tomorrow"): day = (now_local + timedelta(days=1)).date()
        else:
            parts = [int(x) for x in d.split(".")]
            day = datetime(parts[2] if len(parts) == 3 else now_local.year, parts[1], parts[0]).date()
        local = datetime(day.year, day.month, day.day, hh, mm)
    except Exception:
        return None
    return calendar.timegm(local.timetuple()) - config.TZ_HOURS * 3600

@router.message(Command("broadcastat"), is_admin)
async def cmd_broadcastat(m: Message, command: CommandObject):
    usage = "Формат: /broadcastat ДД.ММ ЧЧ:ММ (или «завтра 12:00»)\nСначала подготовь рассылку через /broadcast, посмотри предпросмотр, затем эта команда. Время по вашему поясу."
    ts = _parse_when((command.args or "").split())
    if ts is None: return await m.answer(usage)
    if ts < time.time() + 30: return await m.answer("Это время уже прошло. Укажи будущее.")
    pending = B.BCAST.pop(m.from_user.id, None)
    if not pending: return await m.answer("Нет подготовленной рассылки. Сначала: /broadcast ТЕКСТ")
    seg, text, photo = pending
    cur = con.execute("INSERT INTO sched_bcast(run_at,seg,text,photo,admin_id,status,created) VALUES(?,?,?,?,?,'wait',?)",
                      (ts, seg, text, photo, m.from_user.id, int(time.time()))); con.commit()
    B.alog(m, "broadcastat", cur.lastrowid, f"{B.tfmt(ts)} сегмент {seg}: {text[:60]}")
    await m.answer(f"Запланировано №{cur.lastrowid} на {B.tfmt(ts)}. Получателей посчитаю в момент отправки (сейчас в сегменте «{B.SEGMENTS[seg]}»: {len(db.segment_ids(seg))}).\nСписок: /scheduled · отменить: /unschedule {cur.lastrowid}")

@router.message(Command("scheduled"), is_admin)
async def cmd_scheduled(m: Message):
    rows = con.execute("SELECT * FROM sched_bcast WHERE status='wait' ORDER BY run_at").fetchall()
    if not rows: return await m.answer("Отложенных рассылок нет.")
    await m.answer("<b>Отложенные рассылки</b>\n\n" + "\n".join(
        f"№{r['id']} · {B.tfmt(r['run_at'])} · {B.SEGMENTS.get(r['seg'], r['seg'])}{' · с фото' if r['photo'] else ''} · {esc((r['text'] or '')[:50])}" for r in rows)
        + "\n\nОтменить: /unschedule НОМЕР")

@router.message(Command("unschedule"), is_admin)
async def cmd_unschedule(m: Message, command: CommandObject):
    try: sid = int((command.args or "").strip())
    except ValueError: return await m.answer("Формат: /unschedule НОМЕР")
    cur = con.execute("UPDATE sched_bcast SET status='canceled' WHERE id=? AND status='wait'", (sid,)); con.commit()
    await m.answer("Отменено." if cur.rowcount else "Не нашёл такую ожидающую рассылку.")

async def sched_loop():
    while True:
        try:
            now = int(time.time())
            for r in con.execute("SELECT * FROM sched_bcast WHERE status='wait' AND run_at<=? ORDER BY run_at", (now,)).fetchall():
                if B.BCAST_BUSY: break   # другая рассылка идёт, подождём
                con.execute("UPDATE sched_bcast SET status='sent' WHERE id=?", (r["id"],)); con.commit()
                ids = db.segment_ids(r["seg"])
                if not ids:
                    await B.notify_admins(f"Отложенная рассылка №{r['id']}: в сегменте никого нет, пропустил."); continue
                db.log_action(r["admin_id"], "broadcast", f"{len(ids)} чел.", f"отложенная №{r['id']}, сегмент {r['seg']}: {(r['text'] or '')[:60]}")
                asyncio.create_task(B._broadcast(r["admin_id"], ids, r["text"], r["photo"]))
                break
        except Exception:
            logging.exception("sched_loop")
        await asyncio.sleep(20)

# ================= выгрузки и воронка =================
def _csv_file(cols, rows, tsf, name):
    buf = io.StringIO(); w = csv.writer(buf, delimiter=";"); w.writerow(cols)
    for r in rows:
        w.writerow([(B.tfmt(r[c]) if c in tsf and r[c] else r[c]) if r[c] is not None else "" for c in cols])
    return BufferedInputFile(("﻿" + buf.getvalue()).encode("utf-8"), filename=f"nqyro_{name}_{B.tfmt(time.time(), '%Y%m%d')}.csv"), len(rows)

async def send_export(m, kind):
    if kind == "payments":
        rows = con.execute("SELECT * FROM payments ORDER BY id").fetchall()
        cols = ["id", "user_id", "plan", "amount", "method", "status", "created", "gift", "disc", "bonus", "ref_to", "ref_bonus", "refunded_at"]
    elif kind == "payouts":
        rows = con.execute("SELECT * FROM payouts ORDER BY id").fetchall()   # реквизиты (details) в файл не кладём
        cols = ["id", "user_id", "amount", "method", "status", "created", "done_at", "reason"]
    elif kind in ("balance", "balog"):
        rows = con.execute("SELECT * FROM balance_log ORDER BY id").fetchall()
        cols = ["id", "ts", "user_id", "d_topup", "d_ref", "topup", "ref", "note"]
    else:
        return await m.answer("Формат: /export [users|payments|payouts|balance]")
    f, n = _csv_file(cols, rows, {"created", "done_at", "refunded_at", "ts"}, kind)
    B.alog(m, "export", kind, f"{n} строк")
    await m.answer_document(f, caption=f"{kind}: {n} строк. Внутри личные данные, не пересылай файл.")

def funnel_cohort(days):
    now = int(time.time()); since = now - days * 86400
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
    real = "p.status='paid' AND p.method!='balance' AND p.plan!='topup'"
    regs = q("SELECT COUNT(*) FROM users WHERE created>?", since)
    trial = q("SELECT COUNT(*) FROM users WHERE created>? AND trial_used=1", since)
    payers = q(f"SELECT COUNT(DISTINCT p.user_id) FROM payments p JOIN users u ON u.id=p.user_id WHERE u.created>? AND {real}", since)
    day = q(f"SELECT COUNT(DISTINCT p.user_id) FROM payments p JOIN users u ON u.id=p.user_id WHERE u.created>? AND {real} AND p.created<=u.created+86400", since)
    repeat = q(f"SELECT COUNT(*) FROM (SELECT p.user_id FROM payments p JOIN users u ON u.id=p.user_id WHERE u.created>? AND {real} GROUP BY p.user_id HAVING COUNT(*)>1)", since)
    rev = q(f"SELECT SUM(p.amount) FROM payments p JOIN users u ON u.id=p.user_id WHERE u.created>? AND {real}", since)
    firsts = sorted(r[0] for r in con.execute(
        f"SELECT MIN(p.created)-u.created FROM payments p JOIN users u ON u.id=p.user_id WHERE u.created>? AND {real} GROUP BY u.id", (since,)).fetchall() if r[0] is not None)
    med = f"{firsts[len(firsts) // 2] / 3600:.1f} ч" if firsts else "-"
    pc = lambda a, b: f"{a * 100 / b:.0f}%" if b else "0%"
    return (f"<b>Воронка за {days} дн.</b> (кто пришёл за этот срок)\n\nПришли: {regs}\nВзяли пробник: {trial} ({pc(trial, regs)})\n"
            f"Заплатили: {payers} ({pc(payers, regs)}), из них за первые сутки: {day}\nМедиана до первой оплаты: {med}\n"
            f"Платят повторно: {repeat} ({pc(repeat, payers)})\nВыручка с этой группы: {rub(rev)} ₽, на одного пришедшего: {rev / regs:.0f} ₽" if regs else
            f"<b>Воронка за {days} дн.</b>\n\nЗа этот срок никто не пришёл.")

# ================= профиль и статус серверов =================
_TRAF = {}

async def _traffic_gb(uid):
    c = _TRAF.get(uid)
    if c and time.time() - c[0] < 120: return c[1]
    v = None
    try:
        info = await asyncio.wait_for(panel.get_info(uid), 3)
        if info:
            tr = (info.get("userTraffic") or {}).get("usedTrafficBytes", info.get("usedTrafficBytes"))
            if tr is not None: v = tr / 1024 ** 3
    except Exception:
        pass
    _TRAF[uid] = (time.time(), v)
    return v

def status_line():
    s = monitor.STATE
    if s["ok"] is None or time.time() - s["ts"] > 3 * max(config.MONITOR_SEC, 60): return None
    if s["ok"] is False: return "Сервер управления временно недоступен, мы уже разбираемся"
    if s["total"] and s["up"] == s["total"]: return "Все серверы работают"
    if s["up"] == 0 and s["total"]: return "Серверы недоступны, мы уже чиним"
    if s["total"]: return "Часть серверов недоступна, остальные работают"
    return None

async def profile_extra(uid, scr):
    """Добавляет в профиль трафик и статус серверов."""
    name, text, markup = scr
    extra = []
    u = db.get_user(uid)
    if config.PANEL_URL and u["sub_until"] > 0:
        gb = await _traffic_gb(uid)
        if gb is not None: extra.append(f"Трафик: {gb:.1f} ГБ")
    sl = status_line()
    if sl: extra.append(sl)
    return (name, text + ("\n" + "\n".join(extra) if extra else ""), markup)

@router.message(Command("status"))
async def cmd_status(m: Message):
    await m.answer(status_line() or "Статус серверов пока неизвестен. Если что-то не работает, напиши в поддержку.")

# ================= справка =================
MENU = [("balog", "История баланса"), ("fraud", "Антифрод: проверить человека"), ("scheduled", "Отложенные рассылки")]
HELP = ("\n<b>Дополнения</b>\n"
        "/balog [ID] [N]: история баланса · /balrevert НОМЕР: откатить запись\n"
        "/fraud ID: проверка рефералов · /wdblock ID · /wdunblock ID: запретить или разрешить вывод\n"
        "/broadcastat ДД.ММ ЧЧ:ММ: запланировать подготовленную рассылку · /scheduled · /unschedule НОМЕР\n"
        "Кнопки в рассылке: строки «>> Текст | plans» (plans, topup, profile, friends, connect, support, gift) или «>> Текст | https://…»\n"
        "Сегменты рассылки: new7, new30 (новые), paid (платили), trialonly (только пробник)\n"
        "/export [users|payments|payouts|balance] · /funnel ДНИ: воронка по пришедшим за период")
