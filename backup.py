"""Ежедневный бэкап базы: снимок SQLite, сжатие, ротация и отправка админам в Telegram.

Настройки: BACKUP_HOUR (час по TZ_HOURS, -1 = выключить), BACKUP_KEEP (сколько копий хранить), BACKUP_DIR.
Снимок делается штатным backup API SQLite, поэтому он целостный, даже если бот в этот момент пишет в базу.
"""
import asyncio, glob, gzip, logging, os, shutil, sqlite3
from datetime import datetime, timedelta, timezone
import config, db

def _tz():
    return timezone(timedelta(hours=config.TZ_HOURS))

def _snapshot() -> str:
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    raw = os.path.join(config.BACKUP_DIR, "data_" + datetime.now(_tz()).strftime("%Y%m%d_%H%M%S") + ".db")
    dst = sqlite3.connect(raw)
    try:
        db.con.backup(dst)
    finally:
        dst.close()
    return raw

def _pack(raw: str) -> str:
    gz = raw + ".gz"
    with open(raw, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g)
    os.remove(raw)
    files = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "data_*.db.gz")))
    for old in files[:-max(1, config.BACKUP_KEEP)]:
        try: os.remove(old)
        except OSError: pass
    return gz

async def backup_now() -> str:
    """Делает бэкап и возвращает путь к сжатому файлу."""
    raw = _snapshot()
    return await asyncio.to_thread(_pack, raw)

async def run(send):
    """Раз в сутки после BACKUP_HOUR делает бэкап и вызывает send(path). Не повторяется после перезапуска."""
    if config.BACKUP_HOUR < 0:
        return
    while True:
        try:
            now = datetime.now(_tz())
            today = now.strftime("%Y-%m-%d")
            if now.hour >= config.BACKUP_HOUR and db.kv_get("last_backup") != today:
                db.kv_set("last_backup", today)
                await send(await backup_now())
        except Exception:
            logging.exception("backup")
        await asyncio.sleep(300)
