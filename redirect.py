"""Мини-веб-сервер для кнопок «Добавить в Happ / Incy».

Telegram не принимает ссылки happ:// и incy:// в кнопках, поэтому кнопка ведёт на https-страницу
{OPEN_BASE}/open/<app>?sub=<ссылка подписки>, а страница уже открывает приложение через deeplink.
Нужны: WEB_PORT и публичный https-адрес OPEN_BASE (обратный прокси Nginx/Caddy на этот порт).
"""
import asyncio, hmac, html, logging
from urllib.parse import urlparse
from aiohttp import web
import config, db, panel

ON_PAYMENT = None   # сюда bot.py кладёт функцию проверки платежа (она сама сверяется с ЮKassa)
_tasks = set()

SCHEMES = {"happ": "happ://add/", "incy": "incy://import/"}
TITLES = {"happ": "Happ", "incy": "Incy"}

def _allowed_host(host: str) -> bool:
    hosts = set(config.SUB_HOSTS)
    if config.PANEL_URL:
        hosts.add(urlparse(config.PANEL_URL).hostname or "")
    return not hosts or host in hosts   # если ничего не задано, пускаем любой https-хост

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NQYRO: добавить в {title}</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b1317;
color:#e8f1f4;font-family:-apple-system,system-ui,sans-serif}}.c{{max-width:380px;padding:24px;text-align:center}}
a.b{{display:block;margin:18px 0 10px;padding:14px;border-radius:12px;background:#2ad4b0;color:#06201a;
font-weight:600;text-decoration:none}}p{{color:#9db3ba;font-size:14px;line-height:1.5}}
code{{display:block;word-break:break-all;background:#142229;border-radius:8px;padding:10px;font-size:12px;color:#cfe3e8}}</style></head>
<body><div class="c"><h2>Открываем {title}…</h2>
<a class="b" href="{deeplink}">Добавить подписку в {title}</a>
<p>Если приложение не открылось, установите {title} и нажмите кнопку ещё раз. Или скопируйте ссылку и добавьте её в приложении вручную:</p>
<code>{sub}</code></div>
<script>setTimeout(function(){{location.href={deeplink_js}}},300)</script></body></html>"""

async def open_app(request: web.Request) -> web.Response:
    app = request.match_info["app"].lower()
    sub = request.query.get("sub", "").strip()
    u = urlparse(sub)
    if app not in SCHEMES or u.scheme != "https" or not u.hostname or not _allowed_host(u.hostname):
        return web.Response(status=400, text="Bad request")
    if config.AUTO_FREE_DEVICE:   # старая запись устройства от удалённой подписки не должна блокировать повторное добавление
        try: await asyncio.wait_for(panel.free_device_slot_by_sub(sub), 8)
        except Exception: logging.exception("free device slot failed")
    deeplink = SCHEMES[app] + sub
    body = PAGE.format(title=TITLES[app], deeplink=html.escape(deeplink, quote=True),
                       deeplink_js='"' + deeplink.replace("\\", "\\\\").replace('"', '\\"').replace("<", "%3C") + '"',
                       sub=html.escape(sub))
    return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

async def yk_hook(request: web.Request) -> web.Response:
    """Вебхук ЮKassa. Данным из тела не верим: бот только узнаёт номер платежа и сам спрашивает статус у ЮKassa."""
    secret = config.YK_WEBHOOK_SECRET
    if not secret or not hmac.compare_digest(request.match_info["secret"], secret):
        return web.Response(status=404)
    try:
        data = await request.json()
        yk_id = (data.get("object") or {}).get("id")
    except Exception:
        return web.Response(status=400)
    p = db.get_payment_by_yk(yk_id) if yk_id else None
    if p and ON_PAYMENT:
        t = asyncio.create_task(ON_PAYMENT(p["id"]))
        _tasks.add(t); t.add_done_callback(_tasks.discard)
    return web.Response(text="ok")

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")

async def start_server():
    """Запускает сервер, если задан WEB_PORT. Возвращает runner (или None)."""
    if not config.WEB_PORT:
        return None
    app = web.Application()
    app.add_routes([web.get("/open/{app}", open_app), web.get("/health", health), web.post("/yk/{secret}", yk_hook)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    logging.info("redirect server on %s:%s", config.WEB_HOST, config.WEB_PORT)
    return runner
