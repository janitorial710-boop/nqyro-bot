"""Связка с панелью Remnawave по API.

Переменные окружения: PANEL_URL, PANEL_TOKEN (см. config.py), по желанию PANEL_SQUAD, HWID_LIMIT.
Пользователь в панели: username = tg_<telegram_id>, telegramId = <telegram_id>.
Сбой панели не роняет бота: ошибка пишется в лог, а при следующем «Подключиться» дата синхронизируется заново.
"""
import logging, time
from urllib.parse import urlparse
from datetime import datetime, timezone
import aiohttp
import config, db

def _headers():
    return {"Authorization": f"Bearer {config.PANEL_TOKEN}", "Accept": "application/json"}

def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _parse_iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())

async def _call(method: str, path: str, **kw):
    """Возвращает (status, json). Разворачивает {"response": ...}."""
    url = config.PANEL_URL.rstrip("/") + path
    async with aiohttp.ClientSession(headers=_headers(), timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.request(method, url, **kw) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            if isinstance(data, dict) and "response" in data:
                data = data["response"]
            return r.status, data

def _as_user(x):
    """Приводит ответ панели к одному пользователю. Разные версии панели отдают его по-разному:
    сам объект, список, {"user": {...}} или {"users": [...]}; id иногда лежит в поле id вместо uuid."""
    if isinstance(x, list):
        x = x[0] if x else None
    if isinstance(x, dict):
        if not x.get("uuid"):
            for k in ("user", "users", "items"):
                if x.get(k): return _as_user(x[k])
            for k in ("id", "userUuid", "user_uuid"):
                if x.get(k):
                    return dict(x, uuid=x[k])
    return x

def _as_users(data):
    if isinstance(data, dict) and not data.get("uuid"):
        for k in ("users", "items", "user"):
            if data.get(k): data = data[k]; break
    lst = data if isinstance(data, list) else [data]
    return [u for u in (_as_user(x) for x in lst) if isinstance(u, dict) and u.get("uuid")]

def _num_id(u):
    """Числовой id пользователя (панель 3.x), если он есть."""
    for k in ("id", "uuid"):
        v = u.get(k)
        if isinstance(v, int) and not isinstance(v, bool): return v
        if isinstance(v, str) and v.isdigit(): return int(v)
    return None

def _who_body(u, **extra):
    """Как назвать пользователя в теле запроса к устройствам: панель 3.x ждёт userId (число), 2.x ждёт userUuid."""
    n = _num_id(u)
    body = {"userId": n} if n is not None else {"userUuid": u["uuid"]}
    body.update(extra)
    return body

async def _patch_user(u, fields: dict):
    """Обновляет пользователя. Разные версии панели называют его по-разному, поэтому пробуем по очереди:
    uuid (2.x), username, числовой id (3.x). Бросает исключение с ответом панели, если ни один вариант не подошёл."""
    idents = []
    n = _num_id(u)
    if u.get("uuid") and n is None: idents.append({"uuid": u["uuid"]})
    if u.get("username"): idents.append({"username": u["username"]})
    if n is not None: idents.append({"id": n})
    last = None
    for ident in idents or [{"uuid": u.get("uuid")}]:
        st, data = await _call("PATCH", "/api/users", json={**ident, **fields})
        if st < 300:
            return data
        last = (st, data)
        if st != 400:   # 400 = «не то поле», пробуем следующее; остальное (401/404/500) от имени не зависит
            break
    raise RuntimeError(f"panel update {last[0]}: {last[1]}")

def _is_outage(st: int) -> bool:
    """Ответ панели, по которому нельзя судить, есть ли пользователь (сбой, перегрузка, нет доступа)."""
    return st >= 500 or st in (401, 403, 408, 429)

async def _find(uid: int):
    """Ищет пользователя: сначала по имени tg_<id>, затем по Telegram ID. None, если его точно нет.
    Если панель ответила ошибкой, бросает исключение: иначе бот решит, что человека нет, и попробует создать дубликат."""
    st, data = await _call("GET", f"/api/users/by-username/tg_{uid}")
    if st == 200 and data:
        u = _as_user(data)
        if isinstance(u, dict) and u.get("uuid"):
            return u
        raise RuntimeError("в ответе панели нет uuid пользователя; поля: " + ", ".join(sorted(u)[:20] if isinstance(u, dict) else [type(u).__name__]))
    if _is_outage(st):
        raise RuntimeError(f"panel find by username HTTP {st}")
    st, data = await _call("GET", f"/api/users/by-telegram-id/{uid}")
    if st == 200 and data:
        lst = _as_users(data)
        if not lst:
            raise RuntimeError("в ответе панели нет uuid пользователя (поиск по Telegram ID)")
        # с одним Telegram ID в панели может оказаться несколько записей (заведены вручную при тестах):
        # берём свою tg_<id>, затем активную, затем с самым поздним сроком
        lst.sort(key=lambda x: (x.get("username") == f"tg_{uid}", x.get("status") == "ACTIVE", x.get("expireAt") or ""), reverse=True)
        return lst[0]
    if _is_outage(st):
        raise RuntimeError(f"panel find by telegram id HTTP {st}")
    return None

async def _create(uid: int, until_ts: int):
    body = {"username": f"tg_{uid}", "telegramId": uid, "expireAt": _iso(until_ts),
            "hwidDeviceLimit": config.HWID_LIMIT, "trafficLimitBytes": 0,
            "trafficLimitStrategy": "NO_RESET", "status": "ACTIVE"}
    if config.PANEL_SQUAD:
        body["activeInternalSquads"] = [config.PANEL_SQUAD]
    st, data = await _call("POST", "/api/users", json=body)
    if st >= 300:
        raise RuntimeError(f"panel create {st}: {data}")
    return _as_user(data)

async def _sync(uid: int, until_ts: int):
    """Создаёт пользователя или выставляет ему дату окончания и статус. Возвращает объект пользователя.
    Заблокированный админом (/ban) остаётся в статусе DISABLED, пока его не разблокируют."""
    banned = db.is_banned(uid)
    u = await _find(uid)
    if u is None:
        if banned:
            return None
        return await _create(uid, until_ts)
    active = until_ts > time.time()
    want = "DISABLED" if banned else ("ACTIVE" if active else "EXPIRED")
    cur = _parse_iso(u["expireAt"]) if u.get("expireAt") else 0
    st_now = u.get("status")
    need = (abs(cur - until_ts) > 60
            or (want in ("ACTIVE", "DISABLED") and st_now != want)
            or (want == "EXPIRED" and st_now == "DISABLED"))
    if need:
        return _as_user(await _patch_user(u, {"expireAt": _iso(until_ts), "status": want}))
    return u

def _fallback_link(uid: int):
    """Запасная ссылка только если SUB_BASE настроен по-настоящему. Значение по умолчанию (example.com) нерабочее."""
    base = (config.SUB_BASE or "").rstrip("/")
    if not base or "example.com" in base:
        return None
    return f"{base}/{uid}"

async def ensure_user(uid: int):
    """Возвращает реальную ссылку-подписку из панели (создаёт пользователя при необходимости).
    None, если получить ссылку не удалось: бот тогда честно попросит повторить, а не выдаст нерабочую ссылку."""
    until = db.get_user(uid)["sub_until"]
    try:
        u = await _sync(uid, until)
        if u and u.get("subscriptionUrl"):
            return u["subscriptionUrl"]
    except Exception:
        logging.exception("panel ensure_user failed")
    # синхронизация не удалась, но человек мог уже быть в панели: тогда отдаём его настоящую ссылку как есть
    try:
        u = await _find(uid)
        if u and u.get("subscriptionUrl"):
            return u["subscriptionUrl"]
    except Exception:
        logging.exception("panel ensure_user: повторный поиск тоже не удался")
    return _fallback_link(uid)

async def set_expiry(uid: int, until_ts: int) -> None:
    try:
        await _sync(uid, until_ts)
    except Exception:
        logging.exception("panel set_expiry failed (uid=%s)", uid)

async def set_banned(uid: int) -> None:
    """Применяет в панели текущее состояние блокировки из базы (флаг banned уже выставлен). Бросает исключение при сбое."""
    await _sync(uid, db.get_user(uid)["sub_until"])

async def get_nodes():
    """Список нод из панели. Бросает исключение, если панель не ответила или токен не подошёл."""
    st, data = await _call("GET", "/api/nodes")
    if st != 200 or not isinstance(data, list):
        raise RuntimeError(f"panel nodes HTTP {st}")
    return data

async def get_info(uid: int):
    """Объект пользователя из панели или None (для /user)."""
    if not config.PANEL_URL:
        return None
    try:
        return await _find(uid)
    except Exception:
        logging.exception("panel get_info failed")
        return None


# ---------- устройства, ссылка, трафик, удаление (дополнения владельца) ----------
async def _need_user(uid: int):
    if not config.PANEL_URL or not config.PANEL_TOKEN:
        raise RuntimeError("панель не настроена (PANEL_URL / PANEL_TOKEN)")
    u = await _find(uid)
    if not u:
        raise RuntimeError("человека ещё нет в панели (он не нажимал «Подключиться»)")
    return u

async def _ok(method: str, path: str, **kw):
    st, data = await _call(method, path, **kw)
    if st >= 300:
        raise RuntimeError(f"панель ответила {st}: {str(data)[:200]}")
    return data

async def hwid_list(uid: int):
    """Список устройств человека (список словарей). Бросает исключение, если панель не ответила."""
    u = await _need_user(uid)
    data = await _ok("GET", f"/api/hwid/devices/{u['uuid']}")
    if isinstance(data, dict):
        return data.get("devices") or []
    return data or []

async def hwid_reset(uid: int):
    """Отвязывает все устройства человека."""
    u = await _need_user(uid)
    await _ok("POST", "/api/hwid/devices/delete-all", json=_who_body(u))

async def hwid_delete(uid: int, hwid: str):
    """Отвязывает одно устройство."""
    u = await _need_user(uid)
    await _ok("POST", "/api/hwid/devices/delete", json=_who_body(u, hwid=hwid))

async def hwid_limit(uid: int):
    """Текущий лимит устройств человека в панели (или HWID_LIMIT из настроек, если панель не сказала)."""
    u = await _need_user(uid)
    return u.get("hwidDeviceLimit") or config.HWID_LIMIT

async def set_hwid_limit(uid: int, limit: int):
    u = await _need_user(uid)
    await _patch_user(u, {"hwidDeviceLimit": limit})

async def revoke_sub(uid: int):
    """Перевыпускает ссылку подписки (старая перестаёт работать). Возвращает новую ссылку или None."""
    u = await _need_user(uid)
    data = await _ok("POST", f"/api/users/{u['uuid']}/actions/revoke", json={})
    return data.get("subscriptionUrl") if isinstance(data, dict) else None

async def reset_traffic(uid: int):
    u = await _need_user(uid)
    await _ok("POST", f"/api/users/{u['uuid']}/actions/reset-traffic")

async def delete_user(uid: int) -> bool:
    """Удаляет человека из панели. False, если его там не было."""
    if not config.PANEL_URL or not config.PANEL_TOKEN:
        raise RuntimeError("панель не настроена (PANEL_URL / PANEL_TOKEN)")
    u = await _find(uid)
    if not u:
        return False
    await _ok("DELETE", f"/api/users/{u['uuid']}")
    return True

async def system_stats():
    if not config.PANEL_URL or not config.PANEL_TOKEN:
        raise RuntimeError("панель не настроена (PANEL_URL / PANEL_TOKEN)")
    return await _ok("GET", "/api/system/stats")

async def sync_many(items, pause: float = 0.15):
    """items: [(uid, until_ts)]. Возвращает (успешно, ошибок). Для массовых начислений."""
    import asyncio
    ok = bad = 0
    for uid, until in items:
        try:
            await _sync(uid, until); ok += 1
        except Exception:
            logging.exception("sync_many uid=%s", uid); bad += 1
        await asyncio.sleep(pause)
    return ok, bad


_freed = {}   # uuid человека -> когда последний раз освобождали слот (защита от бесконечных повторов)
FREE_PAUSE = 30   # секунд

async def _free_slots(u) -> int:
    """Если лимит устройств исчерпан, отвязывает самые давние, чтобы осталось место под новое. Возвращает, сколько отвязано."""
    limit = u.get("hwidDeviceLimit") or config.HWID_LIMIT
    if limit <= 0 or time.time() - _freed.get(u["uuid"], 0) < FREE_PAUSE:
        return 0
    data = await _ok("GET", f"/api/hwid/devices/{u['uuid']}")
    devs = (data.get("devices") if isinstance(data, dict) else data) or []
    excess = len(devs) - (limit - 1)
    if excess <= 0:
        return 0
    devs.sort(key=lambda d: d.get("updatedAt") or d.get("createdAt") or "")   # самые давние первыми
    n = 0
    for d in devs[:excess]:
        await _ok("POST", "/api/hwid/devices/delete", json=_who_body(u, hwid=d["hwid"]))
        n += 1
    _freed[u["uuid"]] = time.time()
    logging.info("freed %s device slot(s) for %s", n, u.get("username"))
    return n

async def free_device_slot(uid: int) -> int:
    """Вызывается ботом, когда человек открывает «Подключиться»: к моменту нажатия «Добавить в Happ/Incy» место под устройство уже есть."""
    if not config.PANEL_URL or not config.PANEL_TOKEN:
        return 0
    u = await _find(uid)
    return await _free_slots(u) if u else 0

async def free_device_slot_by_sub(sub: str) -> int:
    """Вариант для страницы-редиректа бота (redirect.py): владельца определяем по самой ссылке-подписке (она секретная)."""
    if not config.PANEL_URL or not config.PANEL_TOKEN:
        return 0
    short = urlparse(sub).path.rstrip("/").rsplit("/", 1)[-1]
    if not short:
        return 0
    st, u = await _call("GET", f"/api/users/by-short-uuid/{short}")
    u = _as_user(u)
    if st != 200 or not isinstance(u, dict) or not u.get("uuid") or (u.get("subscriptionUrl") or "").rstrip("/") != sub.rstrip("/"):
        return 0
    return await _free_slots(u)

async def raw_dump(uid: int) -> list:
    """Сырые ответы панели про человека для /panelraw (только чтение). Список (подпись, статус, текст)."""
    import json, re
    def fmt(data):
        t = json.dumps(data, ensure_ascii=False)
        t = re.sub(r'("(?:subscriptionUrl|shortUuid|subscriptionUuid|vlessUuid|trojanPassword|ssPassword|password)"\s*:\s*")([^"]{4})[^"]*', r'\1\2…', t)
        return t[:900]
    out = []
    st, d = await _call("GET", f"/api/users/by-username/tg_{uid}"); out.append(("by-username", st, fmt(d)))
    st2, d2 = await _call("GET", f"/api/users/by-telegram-id/{uid}"); out.append(("by-telegram-id", st2, fmt(d2)))
    u = _as_user(d) if st == 200 and d else (_as_users(d2) or [None])[0] if st2 == 200 and d2 else None
    if isinstance(u, dict) and u.get("uuid"):
        st3, d3 = await _call("GET", f"/api/hwid/devices/{u['uuid']}"); out.append(("hwid devices", st3, fmt(d3)))
    else:
        out.append(("hwid devices", 0, "пропущено: в ответе пользователя нет uuid"))
    return out

async def diagnose(uid: int) -> dict:
    """Всё, что нужно, чтобы понять, почему человек не подключается (для /diag). Ничего не меняет."""
    out = {"matches": [], "devices": None, "devices_err": None}
    st, data = await _call("GET", f"/api/users/by-telegram-id/{uid}")
    if st == 200 and data:
        out["matches"] = _as_users(data)
    out["by_telegram_status"] = st
    st2, d2 = await _call("GET", f"/api/users/by-username/tg_{uid}")
    out["by_username_status"] = st2
    out["by_username"] = _as_user(d2) if st2 == 200 and isinstance(d2, (dict, list)) else None
    u = out["by_username"] or (out["matches"][0] if out["matches"] else None)
    out["picked"] = u
    if u:
        try:
            if not u.get("uuid"):
                raise RuntimeError("в ответе панели нет uuid; поля: " + ", ".join(sorted(u)[:20]))
            data = await _ok("GET", f"/api/hwid/devices/{u['uuid']}")
            out["devices"] = (data.get("devices") if isinstance(data, dict) else data) or []
        except Exception as e:
            out["devices_err"] = str(e)
    return out
