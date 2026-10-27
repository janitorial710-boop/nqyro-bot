"""Мониторинг панели Remnawave и нод. Шлёт алерты админам, когда что-то падает или оживает.

Проверка раз в MONITOR_SEC секунд (по умолчанию 120). Алерт приходит после двух неудачных проверок подряд,
чтобы не будить из-за одиночного сбоя сети. Если PANEL_URL не задан, мониторинг не запускается.
"""
import logging, asyncio
import config, panel

async def run(notify):
    if not config.PANEL_URL:
        return
    fails, panel_down = 0, False
    bad, alerted = {}, set()
    while True:
        try:
            try:
                nodes, err = await panel.get_nodes(), None
            except Exception as e:
                nodes, err = None, e
            if nodes is None:
                fails += 1
                if fails >= 2 and not panel_down:
                    panel_down = True
                    await notify(f"🔴 Панель Remnawave не отвечает.\nОшибка: {err}\nПроверь сервер панели и PANEL_TOKEN.")
            else:
                fails = 0
                if panel_down:
                    panel_down = False
                    await notify("🟢 Панель Remnawave снова отвечает.")
                for n in nodes:
                    key = n.get("uuid") or n.get("name")
                    name = f"{n.get('name') or key} ({n.get('address') or '?'})"
                    is_bad = not n.get("isDisabled") and not n.get("isConnected")
                    if is_bad:
                        bad[key] = bad.get(key, 0) + 1
                        if bad[key] >= 2 and key not in alerted:
                            alerted.add(key)
                            msg = n.get("lastStatusMessage")
                            await notify(f"🔴 Нода упала: {name}." + (f"\nПричина: {msg}" if msg else ""))
                    else:
                        bad[key] = 0
                        if key in alerted:
                            alerted.discard(key)
                            await notify(f"🟢 Нода снова в сети: {name}.")
        except Exception:
            logging.exception("monitor loop")
        await asyncio.sleep(config.MONITOR_SEC)
