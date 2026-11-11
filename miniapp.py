"""Админка владельца как Telegram Mini App.

Страница /app (файл app.html) и JSON-API /api/app/*. Доступ только у владельцев (ADMIN_IDS): каждый запрос подписан
Telegram (initData), подпись проверяется на сервере по токену бота, поэтому подделать её нельзя.
Нужны WEB_PORT и публичный https-адрес OPEN_BASE (те же, что для кнопок «Добавить в Happ / Incy»).
Открывается командой /app или кнопкой «Админка» рядом с полем ввода.
"""
import asyncio, hashlib, hmac, json, logging, os, re, time
from urllib.parse import parse_qsl
from aiohttp import web
import config, db, demo, panel, payments

BUILD = "2026-11-11.7"   # метка версии: видна на /health и внизу приложения, чтобы быстро понять, какая версия запущена
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
MAX_AGE = 24 * 3600   # initData старше суток не принимаем


def _verify(request: web.Request) -> int:
    """Проверяет подпись Telegram (initData) и возвращает Telegram ID того, кто открыл приложение. Иначе 401."""
    raw = request.headers.get("X-Init-Data", "")
    if not raw:
        raise web.HTTPUnauthorized(text="Открой приложение через бота")
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(calc, got):
        raise web.HTTPUnauthorized(text="Не удалось проверить подпись, открой приложение через бота")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > MAX_AGE:
            raise web.HTTPUnauthorized(text="Сессия устарела, открой приложение заново")
        return int(json.loads(pairs.get("user", "{}")).get("id"))
    except (ValueError, TypeError):
        raise web.HTTPUnauthorized(text="Не удалось определить пользователя")


def _auth(request: web.Request):
    """Админка: Telegram ID владельца или 401/403."""
    uid = _verify(request)
    if uid not in config.ADMIN_IDS:
        raise web.HTTPForbidden(text="not an owner")
    return uid


def api(handler):
    async def wrap(request: web.Request):
        uid = _auth(request)
        try:
            return web.json_response(await handler(request, uid), headers={"Cache-Control": "no-store"})
        except web.HTTPException:
            raise
        except Exception as e:
            logging.exception("miniapp %s", request.path)
            return web.json_response({"error": str(e)[:300]}, status=500)
    return wrap


def _u(r):
    return {"id": r["id"], "username": r["username"], "name": r["name"], "until": r["sub_until"] or 0,
            "banned": bool(r["banned"]), "balance": round((r["balance"] or 0) + (r["topup"] or 0), 2)}


async def overview(request, uid):
    now = int(time.time())
    day = now - now % 86400
    s = db.stats(now)
    p = {k: db.profit(now - d * 86400) for k, d in (("d1", 1), ("d7", 7), ("d30", 30))}
    rows = db.con.execute(
        "SELECT date(created,'unixepoch','localtime') d, COALESCE(SUM(amount),0) s FROM payments "
        "WHERE status='paid' AND method!='balance' AND created>? GROUP BY d ORDER BY d", (now - 14 * 86400,)).fetchall()
    return {"stats": s, "profit": p, "chart": [[r["d"], r["s"]] for r in rows],
            "expiring": len(db.expiring_users(3)), "forecast": db.forecast(now, 7)}


async def users(request, uid):
    q = request.query.get("q", "").strip()
    if q:
        rows = db.find_users(q, 30)
    else:
        rows = db.list_users(0, 30)[1]
    return {"users": [_u(r) for r in rows]}


async def user(request, uid):
    tid = int(request.match_info["id"])
    r = db.peek_user(tid)
    if not r:
        raise web.HTTPNotFound(text="no such user")
    n_pay, sum_pay = db.user_pay_totals(tid)
    out = {"user": _u(r), "src": r["src"], "ref": r["ref"], "created": r["created"], "trial": bool(r["trial_used"]),
           "pay_n": n_pay, "pay_sum": sum_pay,
           "payments": [{"ts": p["created"], "amount": p["amount"], "plan": config.plan_name(p["plan"]), "method": p["method"]}
                        for p in db.user_payments(tid, 8)],
           "per_device": bool(config.PER_DEVICE_KEYS), "devices": [], "devices_err": None}
    try:
        if config.PER_DEVICE_KEYS:
            for n, st, devs in await panel.keys_devices(tid):
                out["devices"].append({"n": n, "status": st, "devices": [_dev(d) for d in devs]})
        else:
            out["devices"].append({"n": 1, "status": None, "devices": [_dev(d) for d in await panel.hwid_list(tid)]})
    except Exception as e:
        out["devices_err"] = str(e)[:200]
    return out


def _dev(d):
    return {"name": " ".join(str(x) for x in (d.get("platform"), d.get("deviceModel"), d.get("osVersion")) if x) or "устройство",
            "since": d.get("createdAt") or d.get("created_at")}


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


async def give(request, uid):
    tid = int(request.match_info["id"])
    days = int((await _body(request)).get("days", 0))
    if not 1 <= days <= 3650:
        raise web.HTTPBadRequest(text="days 1..3650")
    if not db.peek_user(tid):
        raise web.HTTPNotFound(text="no such user")
    until = db.extend(tid, days)
    db.log_action(uid, "give", str(tid), f"+{days} дн. (мини-апп)")
    err = None
    try:
        await panel.set_expiry(tid, until)
    except Exception as e:
        err = str(e)[:200]
    return {"until": until, "panel_error": err}


async def ban(request, uid):
    tid = int(request.match_info["id"])
    flag = bool((await _body(request)).get("flag"))
    if flag and tid in config.ADMIN_IDS:
        raise web.HTTPBadRequest(text="админа заблокировать нельзя")
    if not db.peek_user(tid):
        raise web.HTTPNotFound(text="no such user")
    db.set_banned(tid, flag)
    db.log_action(uid, "ban" if flag else "unban", str(tid), "мини-апп")
    err = None
    try:
        await panel.set_banned(tid)
    except Exception as e:
        err = str(e)[:200]
    return {"banned": flag, "panel_error": err}


async def key_off(request, uid):
    if not config.PER_DEVICE_KEYS:
        raise web.HTTPBadRequest(text="отдельные ключи выключены (PER_DEVICE_KEYS=0)")
    tid, n = int(request.match_info["id"]), int(request.match_info["n"])
    if n == 1:
        await panel.revoke_main(tid)
    else:
        await panel.ban_key(tid, n)
    db.log_action(uid, "keyoff", str(tid), f"устройство {n} (мини-апп)")
    return {"ok": True}


async def _key_sub(tid, n):
    """Текущая ссылка подписки ключа n человека."""
    for kn, sub, _st in await panel.keys_info(tid):
        if kn == n:
            return sub
    return None


async def key_reissue(request, uid):
    """Новая ссылка и ключ для устройства n: у основного (1) меняется ключ, у остальных ключ пересоздаётся. Старая ссылка перестаёт работать."""
    tid, n = int(request.match_info["id"]), int(request.match_info["n"])
    if not db.peek_user(tid):
        raise web.HTTPNotFound(text="no such user")
    if n == 1:
        if config.PER_DEVICE_KEYS:
            await panel.revoke_main(tid)
        else:
            await panel.revoke_sub(tid)
        new_n = 1
    else:
        if not config.PER_DEVICE_KEYS:
            raise web.HTTPBadRequest(text="отдельные ключи выключены (PER_DEVICE_KEYS=0)")
        await panel.ban_key(tid, n)
        new_n, _ = await panel.create_key(tid)
    sub = await _key_sub(tid, new_n)
    db.log_action(uid, "reissue", str(tid), f"устройство {n} (мини-апп)")
    return {"n": new_n, "sub": sub, "happ": _open_url("happ", sub) if sub else "", "incy": _open_url("incy", sub) if sub else "",
            "deep_happ": f"happ://add/{sub}" if sub else "", "deep_incy": f"incy://import/{sub}" if sub else ""}


async def key_send(request, uid):
    """Отправляет человеку в бот сообщение с кнопками автодобавления текущей ссылки устройства n."""
    import extras
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    tid, n = int(request.match_info["id"]), int(request.match_info["n"])
    sub = await _key_sub(tid, n)
    if not sub:
        raise web.HTTPNotFound(text="ссылка не найдена")
    rows = []
    if config.OPEN_URL:
        rows = [[InlineKeyboardButton(text="Добавить в Incy", url=_open_url("incy", sub)),
                 InlineKeyboardButton(text="Добавить в Happ", url=_open_url("happ", sub))]]
    if config.client_app_visible(tid):
        from aiogram.types import WebAppInfo
        rows.append([InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=config.OPEN_BASE + "/me"))])
    await extras.B.bot.send_message(tid, "<b>Ключ обновлён</b>\n\nНажми кнопку ниже, и подписка добавится в приложение сама. "
                                         "Если не открылось, зайди в «Подключиться» в боте.",
                                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    db.log_action(uid, "sendlink", str(tid), f"устройство {n} (мини-апп)")
    return {"ok": True}


async def nodes(request, uid):
    out = []
    for n in await panel.get_nodes():
        out.append({"name": n.get("name"), "country": n.get("countryCode"), "connected": bool(n.get("isConnected")),
                    "xray": bool(n.get("isXrayRunning")), "online": n.get("usersOnline"), "disabled": bool(n.get("isDisabled"))})
    return {"nodes": out}


async def payments_list(request, uid):
    return {"payments": [{"id": r["pid"], "ts": r["created"], "amount": r["amount"], "plan": config.plan_name(r["plan"]),
                          "method": r["method"], "user": r["id"], "username": r["username"], "name": r["name"]}
                         for r in db.recent_payments(50)]}


async def log(request, uid):
    return {"log": [{"ts": r["ts"], "action": r["action"], "target": r["target"], "details": r["details"],
                     "admin": r["username"] or r["name"] or r["id"]} for r in db.recent_log(50)]}


async def page(request: web.Request):
    try:
        with open(HTML_PATH, encoding="utf-8") as f:
            body = f.read()
    except OSError:
        return web.Response(status=404, text="app.html not found")
    if request.query.get("demo"):   # демо с выдуманными данными, ничего реального не показывает
        body = demo.inject(body, "admin")
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# ======================= Клиентское приложение (/me) =======================
# Каждый видит только себя: Telegram ID берётся из подписанного initData, а не из запроса.
CLIENT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client.html")


def capi(handler):
    async def wrap(request: web.Request):
        uid = _verify(request)
        if not config.CLIENT_APP_PUBLIC and uid not in config.ADMIN_IDS:
            raise web.HTTPForbidden(text="Приложение скоро откроется для всех")
        r = db.peek_user(uid)
        if not r:
            raise web.HTTPNotFound(text="Сначала нажми /start в боте")
        try:
            return web.json_response(await handler(request, uid, r), headers={"Cache-Control": "no-store"})
        except web.HTTPException:
            raise
        except Exception as e:
            logging.exception("miniapp client %s uid=%s", request.path, uid)
            msg = "Что-то пошло не так, попробуй ещё раз через минуту"
            if uid in config.ADMIN_IDS:   # владелец сразу видит причину, не заглядывая в логи
                msg += f"\n\nПричина (видно только тебе): {type(e).__name__}: {str(e)[:200]}"
            raise web.HTTPInternalServerError(text=msg)
    return wrap


def _open_url(app, sub):
    from urllib.parse import quote
    return config.OPEN_URL.format(app=app, sub=quote(sub, safe="")) if config.OPEN_URL else ""


def _limit(uid, key, sec=4):
    if time.time() - int(db.kv_get(f"{key}:{uid}") or 0) < sec:
        raise web.HTTPTooManyRequests(text="Подожди пару секунд")
    db.kv_set(f"{key}:{uid}", str(int(time.time())))


def _stars(price):
    if config.STARS_RUB_PER_STAR <= 0:
        return 0
    import features
    return features.stars_for(price)


def _plans_for(u):
    """Тарифы с персональными ценами (скидки по промокоду, акции, скидка на первую покупку) и выгодой относительно месяца."""
    items = list(config.PLANS.items())
    mk, mv = items[0]
    mdaily = db.price_info(u, mk)[0] / mv[1]
    out = []
    for k, v in items:
        price, _code, label = db.price_info(u, k)
        daily = price / v[1]
        out.append({"key": k, "name": v[2], "days": v[1], "price": price, "base": v[0], "label": label,
                    "per_month": round(daily * 30), "save": max(0, round((1 - daily / mdaily) * 100)) if k != mk else 0,
                    "stars": _stars(price)})
    return out


async def me(request, uid, r):
    now = time.time(); until = r["sub_until"] or 0
    plans = _plans_for(r)
    best = max(plans, key=lambda p: p["save"]) if plans else None
    return {"id": uid, "name": r["name"], "banned": bool(r["banned"]), "until": until,
            "days": max(0, int((until - now) // 86400) + 1) if until > now else 0,
            "active": until > now and not r["banned"], "trial_available": not r["trial_used"],
            "trial_days": config.TRIAL_DAYS, "balance": round(db.total_balance(r), 2),
            "bot": config.BOT_USERNAME, "support": config.SUPPORT_USERNAME, "per_device": bool(config.PER_DEVICE_KEYS),
            "plans": plans, "best": best["key"] if best and best["save"] else None,
            "need_email": config.RECEIPT_MODE == "email" and not config.DEV_PAY and not r["email"] and await payments.receipts_on(),
            "email": r["email"] or "", "ref_link": f"https://t.me/{config.BOT_USERNAME}?start=ref_{uid}",
            "methods": _methods(), "slot": _slot_info(uid), "ar": _ar_info(r), "disc": db.disc_label(r),
            "limit": config.HWID_LIMIT + db.slots_active(uid), "created": r["created"], "ref_percent": config.REF_PERCENT,
            "topup": {"min": config.TOPUP_MIN, "max": config.TOPUP_MAX, "bonus": config.TOPUP_BONUS_PERCENT}}


def _methods():
    return ["sbp", "card"] + (["stars"] if config.STARS_RUB_PER_STAR > 0 else []) + (["crypto"] if config.CRYPTO_TOKEN else [])


def _slot_info(uid):
    if config.SLOT_PRICE <= 0:
        return None
    import extras
    return {"price": config.SLOT_PRICE, "days": config.SLOT_DAYS, "max": config.SLOT_MAX, "active": db.slots_active(uid),
            "can": extras._slot_block(uid) is None}


def _ar_info(r):
    if not config.AUTORENEW:
        return None
    return {"on": bool(r["autorenew"]), "card": (r["pm_title"] if r["pm_id"] else None), "days": config.AUTORENEW_NOTICE_DAYS}


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def set_email(request, uid, r):
    email = str((await _body(request)).get("email", "")).strip()
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise web.HTTPBadRequest(text="Это не похоже на email. Формат: name@mail.ru")
    db.set_email(uid, email)
    return {"ok": True}


async def _invoice_failed(uid, e, where):
    """Счёт не создался: пишем в лог, предупреждаем владельцев (как делает бот) и показываем причину владельцу в самом приложении."""
    logging.exception("miniapp %s uid=%s", where, uid)
    reason = payments.LAST_ERROR["text"] if time.time() - payments.LAST_ERROR["ts"] < 8 else str(e)
    try:
        import extras
        await extras.B.alert_once("yk_invoice", "Не удалось создать счёт в ЮKassa (из приложения)."
                                  + (f"\nПричина: {extras.esc(reason)}" if reason else " См. логи.") + "\nПроверить магазин: /ykcheck")
    except Exception:
        pass
    msg = "Не получилось создать счёт. Попробуй ещё раз через минуту."
    if uid in config.ADMIN_IDS and reason:
        msg += f"\n\nПричина (видно только тебе): {reason[:220]}"
    raise web.HTTPServiceUnavailable(text=msg)


async def pay(request, uid, r):
    """Оплата тарифа или докупка устройства: с баланса сразу; СБП, карта, Stars и крипта через те же платёжные функции, что и в боте.
    Дальше оплату заканчивает общий код бота (он же шлёт чек и зачисляет), поэтому приложение и бот не расходятся."""
    import extras, features
    body = await _body(request)
    plan, method = str(body.get("plan", "")), str(body.get("method", ""))
    slot = plan == "slot"
    allowed = ["sbp", "card", "balance"] + ([] if slot else _methods()[2:])
    if (not slot and plan not in config.PLANS) or method not in allowed:
        raise web.HTTPBadRequest(text="Выбери тариф и способ оплаты")
    if r["banned"]:
        raise web.HTTPForbidden(text="Доступ закрыт, напиши в поддержку")
    if slot:
        why = extras._slot_block(uid)
        if why:
            raise web.HTTPBadRequest(text=why)
    _limit(uid, "apipay", 3)
    if method == "balance":
        if slot:
            until = db.pay_slot_with_balance(uid)
            if until is None:
                raise web.HTTPBadRequest(text="На балансе не хватает средств. Выбери СБП или карту.")
            try:
                await panel.apply_limit(uid)
            except Exception:
                logging.exception("apply_limit after balance slot uid=%s", uid)
            return {"status": "paid", "until": until, "slot": True}
        res = db.pay_with_balance(uid, plan, False)
        if res is None:
            raise web.HTTPBadRequest(text="На балансе не хватает средств. Выбери СБП или карту.")
        _kind, until = res
        await panel.set_expiry(uid, until)
        return {"status": "paid", "until": until}
    label = config.plan_name(plan)
    try:
        if method == "stars":
            from aiogram.types import LabeledPrice
            pid = db.create_payment(uid, plan, "stars", False)
            stars = features.stars_for(db.get_payment(pid)["amount"])
            link = await extras.B.bot.create_invoice_link(
                title=f"NQYRO · {label}"[:32], description=f"{label}. Оплата Telegram Stars."[:255], payload=f"pid:{pid}",
                provider_token="", currency="XTR", prices=[LabeledPrice(label=label[:30], amount=stars)])
            return {"pid": pid, "stars_link": link, "stars": stars}
        if method == "crypto":
            pid = db.create_payment(uid, plan, "crypto", False)
            url = await features.crypto_create(pid, db.get_payment(pid)["amount"], f"NQYRO: {label}")
            asyncio.create_task(extras.B.watch(pid))
            return {"pid": pid, "url": url}
    except Exception:
        logging.exception("miniapp alt payment uid=%s method=%s", uid, method)
        raise web.HTTPServiceUnavailable(text="Не удалось создать счёт, попробуй другой способ или повтори через минуту")
    if config.RECEIPT_MODE == "email" and not config.DEV_PAY and not r["email"] and await payments.receipts_on():
        return {"need_email": True}
    try:
        pid, url = await payments.create_invoice(uid, plan, method, r["email"], False, None)
    except Exception as e:
        await _invoice_failed(uid, e, "invoice")
    if url is None:
        return {"pid": pid, "dev": True}
    if extras.B is not None:
        asyncio.create_task(extras.B.watch(pid))   # бот сам опросит ЮKassa и зачислит оплату, даже если приложение закроют
    return {"pid": pid, "url": url}

async def pay_status(request, uid, r):
    import extras
    pid = int(request.match_info["pid"])
    p = db.get_payment(pid)
    if p is None or p["user_id"] != uid:
        raise web.HTTPNotFound(text="Платёж не найден")
    if p["status"] != "paid" and extras.B is not None:
        try:
            await extras.B.finalize_if_paid(pid, quiet=True)
        except Exception:
            logging.exception("miniapp pay_status uid=%s pid=%s", uid, pid)
        p = db.get_payment(pid)
    u = db.peek_user(uid)
    return {"status": p["status"], "until": u["sub_until"] or 0}


async def connect(request, uid, r):
    if r["banned"] or (r["sub_until"] or 0) <= time.time():
        return {"active": False}
    sub1 = await panel.ensure_user(uid)
    if not sub1:
        raise web.HTTPServiceUnavailable(text="Не удалось получить ссылку, попробуй через минуту")
    if config.PER_DEVICE_KEYS:
        info = await panel.keys_info(uid)
        devs = {n: d for n, _, d in await panel.keys_devices(uid)}
    else:
        info = [(1, sub1, "ACTIVE")]
        devs = {1: await panel.hwid_list(uid)}
    keys = [{"n": n, "sub": s, "status": st, "devices": [_dev(x) for x in devs.get(n, [])],
             "happ": _open_url("happ", s), "incy": _open_url("incy", s),
             "deep_happ": f"happ://add/{s}", "deep_incy": f"incy://import/{s}"} for n, s, st in info if s]
    allowed = config.HWID_LIMIT + db.slots_active(uid)
    used = sum(max(1, len(x)) for x in devs.values())
    return {"active": True, "keys": keys, "allowed": allowed, "used": used,
            "per_device": bool(config.PER_DEVICE_KEYS), "can_add": bool(config.PER_DEVICE_KEYS) and used < allowed,
            "happ_dl": config.HAPP_URL, "incy_dl": config.INCY_URL}


async def device_add(request, uid, r):
    if not config.PER_DEVICE_KEYS:
        raise web.HTTPBadRequest(text="Добавление устройств выключено")
    if r["banned"] or (r["sub_until"] or 0) <= time.time():
        raise web.HTTPBadRequest(text="Подписка не активна")
    _limit(uid, "connnew")
    try:
        n, _ = await panel.create_key(uid)
    except Exception as e:
        if "лимит" in str(e):
            raise web.HTTPBadRequest(text="Лимит устройств исчерпан. Отключи ненужное или докупи устройство в боте.")
        raise
    return {"n": n}


async def device_off(request, uid, r):
    if not config.PER_DEVICE_KEYS:
        raise web.HTTPBadRequest(text="Отключение устройств выключено")
    n = int(request.match_info["n"])
    _limit(uid, "devrm", 5)
    cnt = 1
    if n == 1:
        try:
            cnt = len(next((d for nn, _, d in await panel.keys_devices(uid) if nn == 1), []))
        except Exception:
            cnt = 1
        await panel.revoke_main(uid)
    else:
        await panel.ban_key(uid, n)
    made = 0
    if n == 1 and cnt > 1:   # на общем старом ключе было несколько устройств: сразу заводим им отдельные
        for _ in range(cnt - 1):
            try:
                await panel.create_key(uid); made += 1
            except Exception:
                logging.exception("split keys uid=%s", uid); break
    return {"ok": True, "n": n, "split": made}


async def trial(request, uid, r):
    until = db.claim_trial(uid)
    if until is None:
        raise web.HTTPBadRequest(text="Пробный период уже использован")
    await panel.set_expiry(uid, until)
    return {"until": until}


import assets
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


async def static_file(request: web.Request):
    """Логотип и маскот. Сначала берём вшитую копию (assets.py), так картинки есть всегда; папка static/ запасная."""
    name = request.match_info["name"]
    hdr = {"Cache-Control": "public, max-age=86400"}
    if name in assets.ASSETS:
        ct, data = assets.ASSETS[name]
        return web.Response(body=data, content_type=ct, headers=hdr)
    p = os.path.join(STATIC_DIR, os.path.basename(name))
    if name.endswith((".png", ".jpg")) and os.path.isfile(p):
        return web.FileResponse(p, headers=hdr)
    raise web.HTTPNotFound()


# ---- оплата ----
PROMO_BAD = {"not_found": "Код не найден. Проверь, нет ли опечатки.", "already": "Этот промокод уже использован на твоём аккаунте.",
             "exhausted": "У этого промокода не осталось активаций.", "expired": "Срок действия промокода закончился.",
             "off": "Сейчас этот промокод не действует."}


async def withdraw(request, uid, r):
    """Заявка на вывод реферального баланса: те же проверки, та же запись и то же уведомление владельцам, что и в боте."""
    import extras
    body = await _body(request)
    method, details = str(body.get("method", "")), str(body.get("details", "")).strip()
    try:
        amount = round(float(str(body.get("amount", "")).replace(",", ".").replace(" ", "")), 2)
        assert amount == amount
    except Exception:
        raise web.HTTPBadRequest(text="Укажи сумму числом")
    _limit(uid, "apiwd", 5)
    i = extras.wd_info(uid); mn = extras._min(uid)
    if method not in extras.METHODS or i.get("blocked") or i["pending"] or i["cool_until"] or i["avail"] < mn:
        raise web.HTTPBadRequest(text="Сейчас вывод недоступен")
    if method == "card":
        digits = re.sub(r"[\s-]", "", details)
        if not (digits.isdigit() and 16 <= len(digits) <= 19 and extras.luhn_ok(digits)):
            raise web.HTTPBadRequest(text="Номер карты не похож на настоящий. Проверь 16 цифр.")
        details = digits
    elif method == "sbp":
        if not re.match(r"^\+?[78]?[\s(-]*\d[\d\s()-]{8,14}\d\s+(\S.{1,40})$", details):
            raise web.HTTPBadRequest(text="Нужны телефон и банк, например: +79991234567 Сбербанк")
        details = details[:80]
    elif not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", details):
        raise web.HTTPBadRequest(text="Это не адрес USDT TRC20: он начинается с T и состоит из 34 символов")
    if amount < mn:
        raise web.HTTPBadRequest(text=f"Минимум для вывода {mn} ₽")
    if amount > i["avail"] + 1e-9:
        raise web.HTTPBadRequest(text=f"Можно вывести не больше {int(i['avail'])} ₽")
    db.con.execute("UPDATE users SET balance=MAX(0, balance-?) WHERE id=?", (amount, uid))
    cur = db.con.execute("INSERT INTO payouts(user_id,amount,method,details,status,created) VALUES(?,?,?,?, 'pending', ?)",
                         (uid, amount, method, details, int(time.time())))
    db.con.commit(); pid = cur.lastrowid
    try:
        B = extras.B; u2 = db.peek_user(uid)
        prev = db.con.execute("SELECT COALESCE(SUM(amount),0) FROM payouts WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0]
        await extras.tell(config.ADMIN_IDS,
                          f"<b>Заявка на вывод #{pid}</b>\n{B.who(u2)} · <code>{uid}</code>\nСумма: <b>{extras.rub(amount)} ₽</b>\n"
                          f"Способ: {extras.METHODS[method]}\nРеквизиты: <code>{extras.esc(details)}</code>\n"
                          f"Выплачено ему раньше: {extras.rub(prev)} ₽\n\n{B.features.fraud_line(uid)}\n(заявка из приложения)",
                          extras.kb([("Выплачено", f"wdp:ok:{pid}"), ("Отклонить", f"wdp:no:{pid}")]))
    except Exception:
        logging.exception("withdraw notify pid=%s", pid)
    return {"id": pid, "amount": amount, "to": extras.mask(method, details)}


async def profile(request, uid, r):
    now = int(time.time())
    slots = [s[0] for s in db.con.execute("SELECT until FROM slots WHERE user_id=? AND until>? ORDER BY until", (uid, now)).fetchall()]
    return {"payments": [{"ts": p["created"], "amount": p["amount"], "plan": config.plan_name(p["plan"]), "method": p["method"]}
                         for p in db.user_payments(uid, 8)],
            "slots": slots, "created": r["created"], "promo_used": db.promo_use_count(uid)}


async def faq(request, uid, r):
    import extras
    return {"faq": [{"q": q, "a": a} for q, a in extras.FAQ]}


# ---- разделы, которые раньше были только в боте: приложение ничего не просит доделать там ----
PROMO_BAD = {"not_found": "Такого кода нет. Проверь, нет ли опечатки.", "off": "Этот код отключён.", "expired": "Срок действия кода закончился.",
             "exhausted": "Этот код уже разобрали.", "already": "Ты уже применял этот код."}


async def promo(request, uid, r):
    body = await _body(request)
    code = str(body.get("code", "")).strip()
    if not code or len(code) > 40:
        raise web.HTTPBadRequest(text="Введи промокод")
    _limit(uid, "apppromo", 2)
    status, val, extra = db.use_promo(uid, code)
    if status == "ok":
        await panel.set_expiry(uid, extra)
        return {"status": "ok", "days": val, "until": extra}
    if status == "disc":
        return {"status": "disc", "label": db.disc_label(db.get_user(uid))}
    raise web.HTTPBadRequest(text=PROMO_BAD.get(status, "Код не подошёл"))


async def friends(request, uid, r):
    n, earned, bal = db.friends_stats(uid)
    out = {"invited": n, "earned": round(earned or 0, 2), "balance": round(bal or 0, 2), "percent": config.REF_PERCENT,
           "ref_link": f"https://t.me/{config.BOT_USERNAME}?start=ref_{uid}", "wd": None, "methods": [], "payouts": []}
    try:   # вывод бонусов: те же правила и данные, что в боте (extras.wd_info), приложение ничего не считает по-своему
        import extras
        i = extras.wd_info(uid); pend = i.get("pending")
        rows = db.con.execute("SELECT id,amount,method,status,created FROM payouts WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,)).fetchall()
        out["wd"] = {"avail": round(i["avail"], 2), "held": round(i["held"], 2), "min": extras._min(uid), "hold_days": config.PAYOUT_HOLD_DAYS,
                     "pending": ({"id": pend["id"], "amount": pend["amount"]} if pend else None),
                     "cool_until": i.get("cool_until") or 0, "blocked": bool(i.get("blocked"))}
        out["methods"] = [{"key": k, "name": v} for k, v in extras.METHODS.items()]
        out["payouts"] = [{"id": p["id"], "amount": p["amount"], "method": extras.METHODS.get(p["method"], p["method"]),
                           "status": p["status"], "ts": p["created"]} for p in rows]
    except Exception:
        logging.exception("miniapp friends/withdraw info uid=%s", uid)
    return out


async def history(request, uid, r):
    return {"payments": [{"ts": p["created"], "amount": p["amount"], "plan": config.plan_name(p["plan"]), "method": p["method"]}
                         for p in db.user_payments(uid, 15)]}


async def topup(request, uid, r):
    import extras
    body = await _body(request)
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    method = str(body.get("method", "sbp"))
    if not config.TOPUP_MIN <= amount <= config.TOPUP_MAX:
        raise web.HTTPBadRequest(text=f"Сумма пополнения от {config.TOPUP_MIN} до {config.TOPUP_MAX} ₽")
    if method not in ("sbp", "card"):
        raise web.HTTPBadRequest(text="Выбери СБП или карту")
    if r["banned"]:
        raise web.HTTPForbidden(text="Доступ закрыт, напиши в поддержку")
    _limit(uid, "apptopup", 3)
    if config.RECEIPT_MODE == "email" and not config.DEV_PAY and not r["email"] and await payments.receipts_on():
        return {"need_email": True}
    try:
        pid, url = await payments.create_invoice(uid, "topup", method, r["email"], False, amount)
    except Exception as e:
        await _invoice_failed(uid, e, "topup")
    if url is None:
        return {"pid": pid, "dev": True}
    if extras.B is not None:
        asyncio.create_task(extras.B.watch(pid))
    return {"pid": pid, "url": url}


async def autorenew(request, uid, r):
    if not config.AUTORENEW:
        raise web.HTTPBadRequest(text="Автопродление сейчас выключено")
    on = 1 if (await _body(request)).get("on") else 0
    db.con.execute("UPDATE users SET autorenew=? WHERE id=?", (on, uid)); db.con.commit()
    return {"ar": _ar_info(db.get_user(uid))}


def _thread(uid):
    import extras
    t = extras.tk_open_for(uid)
    if not t:
        return {"ticket": None, "messages": []}
    rows = db.con.execute("SELECT staff,text,ts FROM ticket_msgs WHERE ticket_id=? ORDER BY ts, rowid LIMIT 40", (t["id"],)).fetchall()
    return {"ticket": t["id"], "messages": [{"staff": bool(x["staff"]), "text": x["text"], "ts": x["ts"]} for x in rows]}


async def support_get(request, uid, r):
    return {**_thread(uid), "support": config.SUPPORT_USERNAME}


async def support_send(request, uid, r):
    """Обращение в поддержку из приложения: та же таблица обращений, что и в боте, сотрудникам оно приходит в бота как обычно."""
    import extras
    text = str((await _body(request)).get("text", "")).strip()[:2000]
    if len(text) < 2:
        raise web.HTTPBadRequest(text="Опиши, что случилось")
    _limit(uid, "appticket", 5)
    t = extras.tk_open_for(uid)
    if t:
        tid = t["id"]
    else:
        tid = db.con.execute("INSERT INTO tickets(user_id,status,created) VALUES(?, 'open', ?)", (uid, int(time.time()))).lastrowid
    db.con.execute("INSERT INTO ticket_msgs(ticket_id,staff,text,ts) VALUES(?,0,?,?)", (tid, text, int(time.time()))); db.con.commit()
    head = f"<b>Обращение #{tid}</b> · {extras.B.who(db.peek_user(uid))} · <code>{uid}</code>\n\n{extras.esc(text)}"
    markup = extras.kb([("Ответить", f"tkr:{tid}"), ("Закрыть", f"tkc:{tid}")])
    for sid in extras.support_ids():
        try:
            await extras.B.bot.send_message(sid, head, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            logging.warning("не удалось передать обращение сотруднику %s", sid)
    return {**_thread(uid), "support": config.SUPPORT_USERNAME}


async def client_page(request: web.Request):
    try:
        with open(CLIENT_HTML, encoding="utf-8") as f:
            body = f.read()
    except OSError:
        return web.Response(status=404, text="client.html not found")
    import assets
    for name, uri in assets.INLINE.items():   # логотип и маскот кладём прямо в страницу
        body = body.replace("/static/" + name, uri)
    body = body.replace("</body>", f'<div style="text-align:center;color:#4a5550;font-size:10px;padding:2px 0 96px">build {BUILD}</div></body>', 1)
    if request.query.get("demo"):
        body = demo.inject(body, "client")
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def routes():
    return [web.get("/app", page),
            web.get("/me", client_page),
            web.get("/api/me", capi(me)),
            web.get("/api/me/connect", capi(connect)),
            web.post("/api/me/device/add", capi(device_add)),
            web.post("/api/me/device/{n:\\d+}/off", capi(device_off)),
            web.post("/api/me/trial", capi(trial)),
            web.post("/api/me/promo", capi(promo)),
            web.get("/api/me/friends", capi(friends)),
            web.get("/api/me/history", capi(history)),
            web.post("/api/me/topup", capi(topup)),
            web.post("/api/me/autorenew", capi(autorenew)),
            web.get("/api/me/support", capi(support_get)),
            web.post("/api/me/support", capi(support_send)),
            web.post("/api/me/withdraw", capi(withdraw)),
            web.get("/api/me/profile", capi(profile)),
            web.get("/api/me/faq", capi(faq)),
            web.post("/api/me/email", capi(set_email)),
            web.post("/api/me/pay", capi(pay)),
            web.get("/api/me/payment/{pid:\\d+}", capi(pay_status)),
            web.get("/static/{name}", static_file),
            web.get("/api/app/overview", api(overview)),
            web.get("/api/app/users", api(users)),
            web.get("/api/app/user/{id:\\d+}", api(user)),
            web.post("/api/app/user/{id:\\d+}/give", api(give)),
            web.post("/api/app/user/{id:\\d+}/ban", api(ban)),
            web.post("/api/app/user/{id:\\d+}/key/{n:\\d+}/off", api(key_off)),
            web.post("/api/app/user/{id:\\d+}/key/{n:\\d+}/reissue", api(key_reissue)),
            web.post("/api/app/user/{id:\\d+}/key/{n:\\d+}/send", api(key_send)),
            web.get("/api/app/nodes", api(nodes)),
            web.get("/api/app/payments", api(payments_list)),
            web.get("/api/app/log", api(log))]
