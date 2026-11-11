"""Мини-веб-сервер для кнопок «Добавить в Happ / Incy».

Telegram не принимает ссылки happ:// и incy:// в кнопках, поэтому кнопка ведёт на https-страницу
{OPEN_BASE}/open/<app>?sub=<ссылка подписки>, а страница уже открывает приложение через deeplink.
Нужны: WEB_PORT и публичный https-адрес OPEN_BASE (обратный прокси Nginx/Caddy на этот порт).
"""
import asyncio, hmac, html, json, logging
from urllib.parse import quote, urlparse
from aiohttp import web
import config, db, miniapp, panel

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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="dark"><meta name="referrer" content="no-referrer">
<title>NQYRO: добавить в __TITLE__</title>
<style>
*{box-sizing:border-box}html,body{margin:0;background:#06080a;color:#eef3ec;font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif}
body{min-height:100vh;display:flex;justify-content:center;padding:28px 18px calc(28px + env(safe-area-inset-bottom));
background:radial-gradient(520px 340px at 85% -60px,rgba(198,255,26,.16),transparent 70%),#06080a}
.c{width:100%;max-width:420px;text-align:center}
.lg{width:64px;height:64px;object-fit:contain;margin:8px auto 4px;filter:drop-shadow(0 0 18px rgba(198,255,26,.45))}
h1{font-size:25px;margin:14px 0 8px}.s{color:#8b978d;font-size:14px;line-height:1.5;margin:0 0 18px}
a.b{display:block;padding:17px;border-radius:16px;background:#c6ff1a;color:#0a1200;font-weight:800;font-size:17px;text-decoration:none;box-shadow:0 0 28px rgba(198,255,26,.3)}
a.b:active{transform:scale(.99);opacity:.85}a.g{display:block;margin-top:10px;padding:13px;border-radius:14px;border:1px solid #1f2b25;background:#141c18;color:#eef3ec;font-weight:700;font-size:15px;text-decoration:none}
.box{text-align:left;background:#0e1411;border:1px solid #1f2b25;border-radius:18px;padding:16px;margin-top:16px;font-size:14px;line-height:1.55}
.box b.t{display:block;font-size:15px;margin-bottom:8px}.box .n{display:flex;gap:10px;margin:10px 0}.box .n i{flex:none;width:24px;height:24px;border-radius:50%;background:#c6ff1a;color:#0a1200;font-style:normal;font-weight:800;font-size:12px;display:flex;align-items:center;justify-content:center}
.warn{border-color:rgba(255,200,60,.5)}.hide{display:none}.ok{color:#6dff9c}.lime{color:#c6ff1a}
code{display:block;word-break:break-all;background:#141c18;border-radius:10px;padding:10px;font-size:12px;color:#cfe3e8;margin-top:8px}
</style></head><body><div class="c">
<img class="lg" src="/static/n.png" alt="" onerror="this.style.display='none'">
<h1 id="h">Открываем __TITLE__…</h1>
<p class="s" id="s">Если появится вопрос «Открыть в __TITLE__?», нажми «Открыть».</p>
<a class="b" id="go" href="#">Добавить в __TITLE__</a>
<div id="inapp" class="box warn hide"><b class="t">Ты открыл страницу во встроенном браузере</b>
Из него приложения часто не открываются. Открой эту страницу в обычном браузере:
<div id="ios" class="hide"><div class="n"><i>1</i><div>Нажми «⋯» или значок компаса справа внизу</div></div><div class="n"><i>2</i><div>Выбери «Открыть в Safari»</div></div></div>
<div id="and" class="hide"><a class="g" id="chrome" href="#">Открыть в Chrome</a></div></div>
<div id="help" class="box hide"><b class="t">Не открылось? Есть три способа</b>
<div class="n"><i>1</i><div>Установи __TITLE__ и нажми кнопку выше ещё раз.<a class="g" href="__DL__" id="dl" target="_blank" rel="noopener">Скачать __TITLE__</a></div></div>
<div class="n"><i>2</i><div>Добавь вручную: ссылка уже скопирована. В приложении нажми «+» и «Вставить из буфера».<a class="g" href="#" id="cp">Скопировать ссылку ещё раз</a><code id="sub"></code></div></div>
<div class="n"><i>3</i><div>Попробуй другое приложение.<a class="g" href="__OTHERURL__">Добавить в __OTHER__</a></div></div>
<div class="s" style="margin:8px 0 0">Ничего не помогло? <a style="color:#c6ff1a" href="https://t.me/__SUPPORT__">Напиши в поддержку</a>.</div></div>
</div>
<script>
(function(){
var D=__DATA__, ua=navigator.userAgent||"", opened=false, helped=false;
var isAndroid=/Android/i.test(ua), isIOS=/iPhone|iPad|iPod/i.test(ua)||(navigator.platform==="MacIntel"&&navigator.maxTouchPoints>1);
var inApp=/(Telegram|Instagram|FBAN|FBAV|VKClient|VK\\/|Line\\/|MicroMessenger|TikTok|musical_ly|Snapchat|Twitter|YaBrowser\\/.*SA|GSA\\/)/i.test(ua);
var $=function(i){return document.getElementById(i)};
$("sub").textContent=D.sub;
function copy(t){try{if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(t).catch(function(){fb(t)})}}catch(e){}fb(t)}
function fb(t){try{var a=document.createElement("textarea");a.value=t;a.style.position="fixed";a.style.opacity="0";document.body.appendChild(a);a.focus();a.select();document.execCommand("copy");a.remove()}catch(e){}}
function help(){if(helped||opened)return;helped=true;$("help").classList.remove("hide");$("h").textContent="Приложение не открылось";$("s").textContent="Это бывает. Выбери любой способ ниже, подписка добавится."}
function done(){opened=true;$("h").innerHTML='<span class="ok">Готово</span>';$("s").textContent="Вернись в приложение: подписка добавлена. Эту страницу можно закрыть.";$("help").classList.add("hide")}
function launch(){location.href=D.deeplink;setTimeout(help,2400)}
$("go").href=D.deeplink;   /* настоящая ссылка на приложение: обычное нажатие по ссылке ОС и браузеры пропускают надёжнее, чем переход из скрипта */
$("go").onclick=function(){copy(D.sub);setTimeout(help,2400)};   /* ссылку копируем заранее, на случай ручного добавления */
$("cp").onclick=function(e){e.preventDefault();copy(D.sub);this.textContent="Скопировано"; var s=this;setTimeout(function(){s.textContent="Скопировать ссылку ещё раз"},1800)};
document.addEventListener("visibilitychange",function(){if(document.hidden)done()});
window.addEventListener("pagehide",function(){opened=true});
if(inApp){$("inapp").classList.remove("hide");if(isIOS)$("ios").classList.remove("hide");if(isAndroid){$("and").classList.remove("hide");
  $("chrome").href="intent://"+location.host+location.pathname+location.search+"#Intent;scheme=https;package=com.android.chrome;end"}}
setTimeout(launch,350);    /* автоматическая попытка; если браузер её заблокировал, остаётся кнопка и подсказки */
})();
</script></body></html>"""

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
    other = "incy" if app == "happ" else "happ"
    data = json.dumps({"deeplink": deeplink, "sub": sub}, ensure_ascii=False).replace("<", "\\u003c")
    body = (PAGE.replace("__DATA__", data).replace("__TITLE__", TITLES[app]).replace("__OTHER__", TITLES[other])
            .replace("__OTHERURL__", html.escape(f"/open/{other}?sub=" + quote(sub, safe=""), quote=True))
            .replace("__DL__", html.escape(config.HAPP_URL if app == "happ" else config.INCY_URL, quote=True))
            .replace("__SUPPORT__", html.escape(config.SUPPORT_USERNAME, quote=True)))
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
    return web.Response(text=f"ok build {miniapp.BUILD}")

async def start_server():
    """Запускает сервер, если задан WEB_PORT. Возвращает runner (или None)."""
    if not config.WEB_PORT:
        return None
    app = web.Application()
    app.add_routes([web.get("/open/{app}", open_app), web.get("/health", health), web.post("/yk/{secret}", yk_hook)])
    app.add_routes(miniapp.routes())   # админка-мини-приложение: /app и /api/app/*
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT).start()
    logging.info("redirect server on %s:%s", config.WEB_HOST, config.WEB_PORT)
    return runner
