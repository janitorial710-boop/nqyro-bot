"""Админка владельца как Telegram Mini App.

Страница /app (файл app.html) и JSON-API /api/app/*. Доступ только у владельцев (ADMIN_IDS): каждый запрос подписан
Telegram (initData), подпись проверяется на сервере по токену бота, поэтому подделать её нельзя.
Нужны WEB_PORT и публичный https-адрес OPEN_BASE (те же, что для кнопок «Добавить в Happ / Incy»).
Открывается командой /app или кнопкой «Админка» рядом с полем ввода.
"""
import hashlib, hmac, json, logging, os, time
from urllib.parse import parse_qsl
from aiohttp import web
import config, db, panel

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.html")
MAX_AGE = 24 * 3600   # initData старше суток не принимаем


def _auth(request: web.Request):
    """Возвращает Telegram ID владельца или бросает 401/403."""
    raw = request.headers.get("X-Init-Data", "")
    if not raw:
        raise web.HTTPUnauthorized(text="no initData")
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(calc, got):
        raise web.HTTPUnauthorized(text="bad signature")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > MAX_AGE:
            raise web.HTTPUnauthorized(text="expired")
        uid = int(json.loads(pairs.get("user", "{}")).get("id"))
    except (ValueError, TypeError):
        raise web.HTTPUnauthorized(text="bad user")
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


async def nodes(request, uid):
    out = []
    for n in await panel.get_nodes():
        out.append({"name": n.get("name"), "country": n.get("countryCode"), "connected": bool(n.get("isConnected")),
                    "xray": bool(n.get("isXrayRunning")), "online": n.get("usersOnline"), "disabled": bool(n.get("isDisabled"))})
    return {"nodes": out}


async def payments(request, uid):
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
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def routes():
    return [web.get("/app", page),
            web.get("/api/app/overview", api(overview)),
            web.get("/api/app/users", api(users)),
            web.get("/api/app/user/{id:\\d+}", api(user)),
            web.post("/api/app/user/{id:\\d+}/give", api(give)),
            web.post("/api/app/user/{id:\\d+}/ban", api(ban)),
            web.post("/api/app/user/{id:\\d+}/key/{n:\\d+}/off", api(key_off)),
            web.get("/api/app/nodes", api(nodes)),
            web.get("/api/app/payments", api(payments)),
            web.get("/api/app/log", api(log))]
