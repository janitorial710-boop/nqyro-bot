import html
from datetime import datetime
from urllib.parse import quote
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
    used = bool(u["trial_used"])
    return ("01_start.png",
        "<b>NQYRO (читается «найро»)</b>\n\nVPN, который не бесит. Включил и пользуешься."
        + ("" if used else "\n\nНовым пользователям 5 дней бесплатно."),
        kb(*([] if used else [[("Попробовать 5 дней", "trial")]]),
           [("Подключиться", "go:connect")],
           [("Тарифы", "go:plans"), ("Профиль", "go:profile")],
           [("Друзья", "go:friends"), ("Промокод", "promo")],
           [("Поддержка", "go:support")]))

def trial(u, ok=True):
    if not ok:
        return ("11_error.png", "<b>Пробный период уже использован</b>\n\nВыбери тариф, и подписка продолжится без перерыва.",
                kb([("Тарифы", "go:plans")], [("Главное меню", "go:start")]))
    return ("02_trial.png",
        "<b>5 дней бесплатно</b>\n\nПодписка уже активна. Нажми «Подключиться», чтобы получить ссылку.\n\nПробный период даётся один раз на аккаунт.",
        kb([("Подключиться", "go:connect")], [("Главное меню", "go:start")]))

def connect(u, sub=None):
    if not sub:
        if u["trial_used"]:
            return ("03_connect.png", "<b>Подписка не активна</b>\n\nВыбери тариф, чтобы подключиться.",
                    kb([("Тарифы", "go:plans")], [("Назад", "go:start")]))
        return ("03_connect.png", "<b>Подписка не активна</b>\n\nПопробуй 5 дней бесплатно или выбери тариф.",
                kb([("Попробовать 5 дней", "trial")], [("Тарифы", "go:plans")]))
    return ("03_connect.png",
        "<b>Подключение за 3 шага</b>\n\n1. Установи Happ или Incy\n2. Нажми «Добавить подписку»\n3. Включи VPN и пользуйся",
        Markup(inline_keyboard=[
            [b("Скачать Happ", "url:" + config.HAPP_URL), b("Скачать Incy", "url:" + config.INCY_URL)],
            *([[b("Добавить в Happ", "url:" + config.OPEN_URL.format(app="happ", sub=quote(sub, safe=""))),
                b("Добавить в Incy", "url:" + config.OPEN_URL.format(app="incy", sub=quote(sub, safe="")))]] if config.OPEN_URL else []),
            [Btn(text="Скопировать ссылку", copy_text=CopyTextButton(text=sub))],
            [b("Не работает?", "faq:open")],
            [b("Назад", "go:start")]]))

def connect_error(u):
    return ("03_connect.png",
        "<b>Не удалось получить ссылку</b>\n\nСервер подключения не ответил. Обычно это проходит за минуту. "
        "Нажми «Повторить»; если не получится, напиши в поддержку, подписка при этом не пропадёт.",
        kb([("Повторить", "go:connect")], [("Поддержка", "go:support"), ("Назад", "go:start")]))

def _rub(n): return f"{n:,}".replace(",", " ")

def _renew(u):
    """Строки кнопок «Продлить»: тем же тарифом, что в прошлый раз, если он известен."""
    last = db.last_plan(u["id"])
    if last in config.PLANS:
        return [[(f"Продлить · {config.PLANS[last][2]} · {_rub(db.price_for(u, last))} ₽", f"buy:{last}")], [("Другой тариф", "go:plans")]]
    return [[("Продлить", "go:plans")]]

def plans(u):
    dl = db.price_info(u, "1")[2]
    sale = db.active_sale()
    pr = {k: db.price_for(u, k) for k in config.PLANS}
    return ("04_plans.png",
        "<b>Выбери тариф</b>\n\n1 месяц: 249 ₽\n3 месяца: 599 ₽ (−20%)\n12 месяцев: 1 990 ₽ (−33%)\n\nДо 3 устройств на одну подписку.\nХочешь не на месяц, а на любой срок? Пополни баланс на любую сумму и обменяй на дни."
        + (f"\n\nАкция «{sale['title']}» до {d(sale['end'])}." if sale and sale["title"] else "")
        + (f"\n\nТвоя скидка ({dl}) уже учтена в цене на кнопках." if dl else ""),
        kb([(f"1 месяц · {_rub(pr['1'])} ₽", "buy:1")], [(f"3 месяца · {_rub(pr['3'])} ₽", "buy:3")], [(f"12 месяцев · {_rub(pr['12'])} ₽", "buy:12")],
           [("Пополнить баланс", "go:topup"), ("Подарить подписку", "go:gift")],
           [("Назад", "go:start")]))

def gift_plans(u):
    return ("04_plans.png",
        "<b>Подарить подписку</b>\n\nВыбери срок. После оплаты получишь код и ссылку: отправь их другу, и подписка добавится ему сама. Код одноразовый.",
        kb([("1 месяц · 249 ₽", "gbuy:1")], [("3 месяца · 599 ₽", "gbuy:3")], [("12 месяцев · 1 990 ₽", "gbuy:12")],
           [("Подарить по нику", "gn:start")], [("Назад", "go:plans")]))

def gift_ready(u, code):
    link = f"https://t.me/{config.BOT_USERNAME}?start=gift_{code}"
    share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote("Дарю тебе подписку NQYRO")
    return ("06_success.png",
        f"<b>Подарок готов</b>\n\nКод: <code>{code}</code>\n\nСсылка для друга:\n<code>{link}</code>\n\nДрузья нажимают на ссылку или вводят код в разделе «Промокод».",
        Markup(inline_keyboard=[[b("Отправить другу", "url:" + share)], [b("Главное меню", "go:start")]]))

def payment(u, plan, gift=False):
    name = config.PLANS[plan][2]
    price, _, dlabel = db.price_info(u, plan, gift)
    g = "g" if gift else ""
    bal = db.total_balance(u)
    rows = []
    bal_line = ""
    if bal > 0:
        bal_line = f"\nНа балансе: {bal:.0f} ₽"
        label = f"Оплатить балансом · {bal:.0f} ₽" if bal >= price else f"Баланс · {bal:.0f} ₽ (не хватает {price - bal:.0f} ₽)"
        rows.append([(label, f"{g}paybal:{plan}")])
    base = config.PLANS[plan][0]
    price_line = f"К оплате: {price} ₽" + (f" вместо {base} ₽ (скидка: {dlabel})" if price != base else "")
    return ("05_payment.png", f"<b>{'Оплата подарка' if gift else 'Оплата'}</b>\n\nТариф: {name}\n{price_line}{bal_line}\n\nВыбери способ оплаты.",
        kb(*rows, [("СБП", f"{g}pay:{plan}:sbp"), ("Банковская карта", f"{g}pay:{plan}:card")],
           *_alt_rows(f"{g}pay:{plan}"),
           *([] if gift or not config.AUTORENEW else [[("Автопродление картой: " + ("включено" if u["autorenew"] else "выключено"), f"ar:t:{plan}")]]),
           *([] if gift else [[("Пополнить баланс", "go:topup")]]),
           [("Назад", "go:gift" if gift else "go:plans")]))

def _alt_rows(prefix):
    """Кнопки Stars и CryptoBot отключены по решению владельца. Вернуть: убрать return [] ниже."""
    return []
    row = []
    if config.STARS_RUB_PER_STAR > 0: row.append(("Telegram Stars", f"{prefix}:stars"))
    if config.CRYPTO_TOKEN: row.append(("Крипта", f"{prefix}:crypto"))
    return [row] if row else []

def _dfmt(secs):
    """Секунды подписки -> «6 дн.» / «1 дн. 4 ч»."""
    days, hours = divmod(int(secs) // 3600, 24)
    return f"{days} дн." + (f" {hours} ч" if hours else "")

def convert(u):
    rub, secs = db.convert_quote(u)
    bal = db.total_balance(u)
    head = (f"<b>Обмен баланса на дни</b>\n\nНа балансе: {bal:.0f} ₽\nКурс: 1 день = {config.DAY_RATE:.1f} ₽ (как в тарифе на месяц)\n\n")
    if not rub:
        return ("07_profile.png", head + f"Для обмена нужно хотя бы {config.CONVERT_MIN} ₽ (это 1 день). Пополни баланс на любую сумму.",
                kb([("Пополнить баланс", "go:topup")], [("Назад", "go:profile")]))
    return ("07_profile.png", head + f"Обменяем {rub} ₽ → {_dfmt(secs)} подписки. Они добавятся к текущей дате окончания.\n\nДлинные тарифы выгоднее: там день дешевле.",
            kb([(f"Обменять {rub} ₽ → {_dfmt(secs)}", "cvt:go")], [("Тарифы со скидкой", "go:plans")], [("Назад", "go:profile")]))

def converted(u, rub, secs, until):
    return ("06_success.png", f"<b>Готово</b>\n\nСписали {rub} ₽, добавили {_dfmt(secs)}.\nПодписка активна до {d(until)}.",
            kb([("Подключиться", "go:connect")], [("Профиль", "go:profile")]))

def topup(u):
    bonus = config.TOPUP_BONUS_PERCENT
    amounts = [a for a in (50, 100, 300, 500, 1000, 2000) if config.TOPUP_MIN <= a <= config.TOPUP_MAX]
    rows = [[(f"{a} ₽", f"topup:{a}") for a in amounts[i:i + 2]] for i in range(0, len(amounts), 2)]
    text = (f"<b>Пополнение баланса</b>\n\nСейчас на балансе: {db.total_balance(u):.0f} ₽\n"
            + (f"Бонус к пополнению: +{bonus}%\n" if bonus else "")
            + f"\nВыбери сумму или пришли свою (от {config.TOPUP_MIN} до {config.TOPUP_MAX} ₽). "
              f"Любую сумму можно обменять на дни: 1 день = {config.DAY_RATE:.1f} ₽. Или потратить на тариф.")
    return ("07_profile.png", text, kb(*rows, [("Назад", "go:profile")]))

def topup_pay(u, amount):
    bonus = int(amount * config.TOPUP_BONUS_PERCENT / 100)
    line = f"Сумма: {amount} ₽" + (f"\nБонус: +{bonus} ₽\nНа баланс придёт: {amount + bonus} ₽" if bonus else "")
    return ("05_payment.png", f"<b>Пополнение баланса</b>\n\n{line}\n\nВыбери способ оплаты.",
            kb([("СБП", f"tpay:{amount}:sbp"), ("Банковская карта", f"tpay:{amount}:card")],
               *_alt_rows(f"tpay:{amount}"), [("Назад", "go:topup")]))

def topup_ok(u):
    rub, secs = db.convert_quote(u)
    return ("06_success.png", f"<b>Баланс пополнен</b>\n\nТеперь на балансе: {db.total_balance(u):.0f} ₽.\nМожно обменять на дни или оплатить тариф в один клик.",
            kb(*([[(f"Обменять на дни · {_dfmt(secs)}", "go:convert")]] if rub else []), [("Тарифы", "go:plans")], [("Профиль", "go:profile")]))

def partner(u):
    srcs = db.partner_sources(u["id"])
    if not srcs:
        return ("08_friends.png", "<b>Партнёрка</b>\n\nУ тебя пока нет партнёрских ссылок. Хочешь зарабатывать на подписках, напиши в поддержку.",
                kb([("Поддержка", "go:support")], [("Назад", "go:profile")]))
    L = ["<b>Партнёрка</b>\n"]
    for s in srcs:
        st = db.src_stats(s["code"])
        L.append(f"<b>{html.escape(s['title'])}</b> · {s['percent']:.0f}% с каждой оплаты\n"
                 f"Регистраций: {st['regs']} · оплатили: {st['payers']} · начислено: {st['earned']:.0f} ₽\n"
                 f"<code>https://t.me/{config.BOT_USERNAME}?start=src_{s['code']}</code>\n"
                 + "".join(f"Промокод <code>{p['code']}</code>: активаций {p['used']}, оплатили {p['payers']}\n" for p in db.src_promos(s["code"])))
    L.append(f"На балансе: {db.total_balance(u):.0f} ₽. Им можно оплатить подписку. Вывод на карту: через поддержку.")
    return ("08_friends.png", "\n".join(L), kb([("Поддержка", "go:support")], [("Назад", "go:profile")]))

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
            f"Действует до: {d(u['sub_until']) if u['sub_until'] else '-'}\nУстройства: до 3\n"
            f"Баланс: {db.total_balance(u):.0f} ₽"
            + (f"\nСкидка на следующую оплату: {db.disc_label(u)}" if u["disc_kind"] else ""))
    return ("07_profile.png", text,
        kb([("Подключиться", "go:connect"), ("Устройства", "dev:open")],
           [("Продлить", "go:plans"), ("Пополнить баланс", "go:topup")],
           [("Вывести деньги", "wd:start"), ("Промокод", "promo")],
           *([[("Обменять баланс на дни", "go:convert")]] if db.convert_quote(u)[0] else []),
           *([[("Автопродление", "ar:open")]] if config.AUTORENEW else []),
           *([[("Партнёрка", "go:partner")]] if db.is_partner(u["id"]) else []),
           [("Поддержка", "go:support")],
           [("Назад", "go:start")]))

def friends(u):
    n, earned, bal = db.friends_stats(u["id"])
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{u['id']}"
    return ("08_friends.png",
        f"<b>Приглашай друзей</b>\n\nДруг получает 5 дней бесплатно, а ты {config.REF_PERCENT}% с каждой его оплаты на свой баланс.\n\n"
        f"Приглашено: {n} · Заработано: {earned:.0f} ₽\nНа реферальном балансе: {bal:.0f} ₽\n"
        f"Им можно оплатить подписку.\n\nТвоя ссылка:\n<code>{link}</code>",
        kb([("Оплатить подписку балансом", "go:plans")], [("Вывести деньги", "wd:start")], [("Назад", "go:start")]))

def expiring(u):
    return ("09_expiring.png", "<b>Подписка заканчивается через 3 дня</b>\n\nПродли сейчас, и доступ не прервётся.",
        kb(*_renew(u), [("Напомнить позже", "later")]))

def support(u):
    return ("10_support.png", "<b>Поддержка</b>\n\nНапиши нам, если не получается подключиться или оплатить. Приложи скриншот, так будет быстрее.",
        kb([("Частые вопросы", "faq:open"), ("Создать обращение", "tk:new")],
           [("Написать в Telegram", "url:https://t.me/" + config.SUPPORT_USERNAME)], [("Назад", "go:start")]))

def error(u):
    return ("11_error.png", "<b>Что-то пошло не так</b>\n\nПлатёж не прошёл или соединение не удалось. Попробуй ещё раз или напиши нам.",
        kb([("Тарифы", "go:plans")], [("Поддержка", "go:support")]))

def promo_prompt(u):
    return ("12_promo.png", "<b>Промокод</b>\n\nПришли код одним сообщением, и бонусные дни добавятся к подписке.",
        kb([("Назад", "go:start")]))

def promo_ok(u, days, until):
    return ("13_promo_ok.png", f"<b>Промокод активирован</b>\n\nНачислено: +{days} дн.\nПодписка активна до {d(until)}.",
        kb([("Подключиться", "go:connect")], [("Профиль", "go:profile")]))

def promo_disc(u):
    return ("13_promo_ok.png", f"<b>Скидка активирована</b>\n\nТвоя скидка: {db.disc_label(u)}.\nОна применится к цене при следующей оплате подписки.",
        kb([("Выбрать тариф", "go:plans")], [("Главное меню", "go:start")]))

def abandoned(u, plan):
    name = config.PLANS[plan][2]
    return ("05_payment.png", f"<b>Оплата не завершена</b>\n\nТы начал оплачивать тариф «{name}», но счёт остался неоплаченным. Если что-то не получилось, выбери другой способ или напиши нам.",
        kb([("Вернуться к оплате", f"buy:{plan}")], [("Поддержка", "go:support")]))

def auto_welcome(u):
    if not u["trial_used"] and u["sub_until"] <= 0:
        return ("01_start.png", "<b>Привет! Это NQYRO</b>\n\nУ тебя есть 5 дней бесплатно, чтобы попробовать. Занимает минуту.",
                kb([("Попробовать 5 дней", "trial")], [("Главное меню", "go:start")]))
    return ("01_start.png", "<b>Привет! Это NQYRO</b>\n\nЕсли что-то не получилось с подключением, напиши нам, поможем.",
            kb([("Подключиться", "go:connect")], [("Поддержка", "go:support")]))

def auto_day3(u):
    return ("10_support.png", "<b>Как дела?</b>\n\nВсё ли работает так, как нужно? Если что-то тормозит или не подключается, напиши нам, разберёмся.",
            kb([("Написать в поддержку", "url:https://t.me/" + config.SUPPORT_USERNAME)], [("Главное меню", "go:start")]))

def auto_miss(u):
    return ("09_expiring.png", "<b>Скучаем по тебе</b>\n\nПодписка закончилась неделю назад. Вернись, и всё заработает сразу, как раньше.",
            kb(*_renew(u), [("Главное меню", "go:start")]))

def promo_bad(u, reason):
    t = {"not_found": ("Код не найден", "Проверь, нет ли опечатки, и попробуй ещё раз."),
         "already": ("Код уже использован", "Этот промокод можно применить только один раз на аккаунт."),
         "exhausted": ("Промокод закончился", "У этого кода не осталось активаций."),
         "expired": ("Промокод истёк", "Срок действия этого кода закончился.")}[reason]
    return ("11_error.png", f"<b>{t[0]}</b>\n\n{t[1]}", kb([("Ввести ещё раз", "promo")], [("Назад", "go:start")]))
