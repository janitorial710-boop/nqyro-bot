from datetime import datetime
from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, CopyTextButton
import config, db

def b(text, data):
    if data.startswith("url:"):
        return Btn(text=text, url=data[4:])
    return Btn(text=text, callback_data=data)

def kb(*rows):
    return Markup(inline_keyboard=[[b(*x) for x in row] for row in rows])

def d(ts): return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")

def start(u):
    return ("01_start.png",
        "<b>NQYRO (читается «найро»)</b>\n\nVPN, который не бесит. Включил и пользуешься.\n\nНовым пользователям 5 дней бесплатно.",
        kb([("Попробовать 5 дней", "trial")],
           [("Подключиться", "go:connect"), ("Тарифы", "go:plans")],
           [("Профиль", "go:profile"), ("Друзья", "go:friends")],
           [("Промокод", "promo"), ("Поддержка", "go:support")]))

def trial(u, ok=True):
    if not ok:
        return ("11_error.png", "<b>Пробный период уже использован</b>\n\nВыбери тариф, и подписка продолжится без перерыва.",
                kb([("Тарифы", "go:plans")], [("Главное меню", "go:start")]))
    return ("02_trial.png",
        "<b>5 дней бесплатно</b>\n\nПодписка уже активна. Нажми «Подключиться», чтобы получить ссылку.\n\nПробный период даётся один раз на аккаунт.",
        kb([("Подключиться", "go:connect")], [("Главное меню", "go:start")]))

def connect(u, sub=None):
    if not sub:
        return ("03_connect.png", "<b>Подписка не активна</b>\n\nПопробуй 5 дней бесплатно или выбери тариф.",
                kb([("Попробовать 5 дней", "trial")], [("Тарифы", "go:plans")]))
    return ("03_connect.png",
        "<b>Подключение за 3 шага</b>\n\n1. Установи Happ или Incy\n2. Нажми «Добавить подписку»\n3. Включи VPN и пользуйся",
        Markup(inline_keyboard=[
            [b("Скачать Happ", "url:" + config.HAPP_URL), b("Скачать Incy", "url:" + config.INCY_URL)],
            [b("Добавить в Happ", "url:" + config.OPEN_URL.format(app="happ", sub=sub))],
            [b("Добавить в Incy", "url:" + config.OPEN_URL.format(app="incy", sub=sub))],
            [Btn(text="Скопировать ссылку", copy_text=CopyTextButton(text=sub))],
            [b("Назад", "go:start")]]))

def plans(u):
    p = config.PLANS
    return ("04_plans.png",
        "<b>Выбери тариф</b>\n\n1 месяц: 249 ₽\n3 месяца: 599 ₽ (−20%)\n12 месяцев: 1 990 ₽ (−33%)\n\nДо 3 устройств на одну подписку.",
        kb([("1 месяц · 249 ₽", "buy:1")], [("3 месяца · 599 ₽", "buy:3")], [("12 месяцев · 1 990 ₽", "buy:12")], [("Назад", "go:start")]))

def payment(u, plan):
    price, _, name = config.PLANS[plan]
    return ("05_payment.png", f"<b>Оплата</b>\n\nТариф: {name}\nК оплате: {price} ₽\n\nВыбери способ оплаты.",
        kb([("СБП", f"pay:{plan}:sbp")], [("Банковская карта", f"pay:{plan}:card")],
           [("Другие способы", f"pay:{plan}:any")], [("Назад", "go:plans")]))

def pay_dev(u, pid):
    return ("05_payment.png", "<b>Тестовый режим</b>\n\nПлатёжный сервис не подключён. Нажми кнопку, чтобы имитировать оплату.",
            kb([("Имитировать оплату", f"devpay:{pid}")], [("Назад", "go:plans")]))

def pay_link(u, url, pid):
    return ("05_payment.png", "<b>Счёт создан</b>\n\nОплати по кнопке. Подписка продлится автоматически, обычно за минуту после оплаты.",
            Markup(inline_keyboard=[[b("Оплатить", "url:" + url)], [b("Проверить оплату", f"chk:{pid}")], [b("Назад", "go:plans")]]))

def success(u, until):
    return ("06_success.png", f"<b>Оплата прошла</b>\n\nПодписка активна до {d(until)}.\n\nСсылка для подключения ждёт в разделе «Подключиться».",
        kb([("Подключиться", "go:connect")], [("Профиль", "go:profile")]))

def profile(u):
    import time
    active = u["sub_until"] > time.time()
    text = (f"<b>Профиль</b>\n\nСтатус: {'активна' if active else 'не активна'}\n"
            f"Действует до: {d(u['sub_until']) if u['sub_until'] else '-'}\nУстройства: до 3")
    return ("07_profile.png", text,
        kb([("Продлить", "go:plans")], [("Подключиться", "go:connect"), ("Промокод", "promo")], [("Поддержка", "go:support"), ("Назад", "go:start")]))

def friends(u):
    n, bal = db.friends_stats(u["id"])
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{u['id']}"
    return ("08_friends.png",
        f"<b>Приглашай друзей</b>\n\nДруг получает 5 дней бесплатно, а ты {config.REF_PERCENT}% с каждой его оплаты на свой баланс.\n\n"
        f"Приглашено: {n} · Заработано: {bal:.0f} ₽\n\nТвоя ссылка:\n<code>{link}</code>",
        kb([("Назад", "go:start")]))

def expiring(u):
    return ("09_expiring.png", "<b>Подписка заканчивается через 3 дня</b>\n\nПродли сейчас, и доступ не прервётся.",
        kb([("Продлить", "go:plans")], [("Напомнить позже", "later")]))

def support(u):
    return ("10_support.png", "<b>Поддержка</b>\n\nНапиши нам, если не получается подключиться или оплатить. Приложи скриншот, так будет быстрее.",
        kb([("Написать в поддержку", "url:https://t.me/" + config.SUPPORT_USERNAME)], [("Назад", "go:start")]))

def error(u):
    return ("11_error.png", "<b>Что-то пошло не так</b>\n\nПлатёж не прошёл или соединение не удалось. Попробуй ещё раз или напиши нам.",
        kb([("Тарифы", "go:plans")], [("Поддержка", "go:support")]))

def promo_prompt(u):
    return ("12_promo.png", "<b>Промокод</b>\n\nПришли код одним сообщением, и бонусные дни добавятся к подписке.",
        kb([("Назад", "go:start")]))

def promo_ok(u, days, until):
    return ("13_promo_ok.png", f"<b>Промокод активирован</b>\n\nНачислено: +{days} дн.\nПодписка активна до {d(until)}.",
        kb([("Подключиться", "go:connect")], [("Профиль", "go:profile")]))

def promo_bad(u, reason):
    t = {"not_found": ("Код не найден", "Проверь, нет ли опечатки, и попробуй ещё раз."),
         "already": ("Код уже использован", "Этот промокод можно применить только один раз на аккаунт."),
         "exhausted": ("Промокод закончился", "У этого кода не осталось активаций.")}[reason]
    return ("11_error.png", f"<b>{t[0]}</b>\n\n{t[1]}", kb([("Ввести ещё раз", "promo")], [("Назад", "go:start")]))
