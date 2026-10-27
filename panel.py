"""Связка с панелью Remnawave по API.

Переменные окружения: PANEL_URL, PANEL_TOKEN (см. config.py), по желанию PANEL_SQUAD, HWID_LIMIT.
Пользователь в панели: username = tg_<telegram_id>, telegramId = <telegram_id>.
Сбой панели не роняет бота: ошибка пишется в лог, а при следующем «Подключиться» дата синхронизируется заново.
"""
import logging, time
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

async def _find(uid: int):
    """Ищет пользователя по Telegram ID, затем по username. None, если нет."""
    st, data = await _call("GET", f"/api/users/by-telegram-id/{uid}")
    if st == 200 and data:
        return data[0] if isinstance(data, list) else data
    st, data = await _call("GET", f"/api/users/by-username/tg_{uid}")
    if st == 200 and data:
        return data
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
    return data

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
        body = {"uuid": u["uuid"], "expireAt": _iso(until_ts), "status": want}
        st, data = await _call("PATCH", "/api/users", json=body)
        if st >= 300:
            raise RuntimeError(f"panel update {st}: {data}")
        return data
    return u

async def ensure_user(uid: int) -> str:
    """Возвращает реальную ссылку-подписку из панели (создаёт пользователя при необходимости)."""
    until = db.get_user(uid)["sub_until"]
    try:
        u = await _sync(uid, until)
        if u and u.get("subscriptionUrl"):
            return u["subscriptionUrl"]
    except Exception:
        logging.exception("panel ensure_user failed")
    return f"{config.SUB_BASE}/{uid}"   # запасной вариант, если панель недоступна

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
