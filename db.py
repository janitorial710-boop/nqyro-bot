import secrets, sqlite3, time
import config

con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
con.row_factory = sqlite3.Row
con.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, ref INTEGER, trial_used INTEGER DEFAULT 0,
  sub_until INTEGER DEFAULT 0, balance REAL DEFAULT 0, reminded_for INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, amount INTEGER,
  method TEXT, status TEXT, created INTEGER);
""")

for _sql in ("ALTER TABLE payments ADD COLUMN yk_id TEXT", "ALTER TABLE users ADD COLUMN email TEXT",
             "ALTER TABLE users ADD COLUMN topup REAL DEFAULT 0",        # основной баланс (пополняет админ)
             "ALTER TABLE users ADD COLUMN ref_earned REAL DEFAULT 0",   # всего заработано на рефералах
             "ALTER TABLE users ADD COLUMN username TEXT",               # @username в Telegram
             "ALTER TABLE users ADD COLUMN name TEXT",                  # имя и фамилия в Telegram
             "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0"):   # 1 = заблокирован админом
    try:
        con.execute(_sql); con.commit()
    except sqlite3.OperationalError:
        pass

# для старых записей: «заработано» = текущий реферальный баланс (один раз)
con.execute("UPDATE users SET ref_earned=balance WHERE ref_earned=0 AND balance>0"); con.commit()

con.executescript("""
CREATE TABLE IF NOT EXISTS promo_codes(
  code TEXT PRIMARY KEY, days INTEGER, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS promo_uses(
  code TEXT, user_id INTEGER, ts INTEGER, PRIMARY KEY(code, user_id));
""")

try:
    con.execute("ALTER TABLE promo_codes ADD COLUMN expires INTEGER DEFAULT 0"); con.commit()   # до какого момента действует код (0 = бессрочно)
except sqlite3.OperationalError:
    pass

for _sql in ("ALTER TABLE promo_codes ADD COLUMN kind TEXT DEFAULT 'days'",     # days | percent | rub
             "ALTER TABLE promo_codes ADD COLUMN value REAL DEFAULT 0",           # размер скидки (для percent и rub)
             "ALTER TABLE users ADD COLUMN disc_kind TEXT",                       # активная скидка пользователя
             "ALTER TABLE users ADD COLUMN disc_value REAL DEFAULT 0",
             "ALTER TABLE users ADD COLUMN disc_code TEXT",
             "ALTER TABLE payments ADD COLUMN gift INTEGER DEFAULT 0",            # 1 = покупка подарочного кода
             "ALTER TABLE payments ADD COLUMN disc TEXT",                         # код скидки, применённый к платежу
             "ALTER TABLE payments ADD COLUMN nudged INTEGER DEFAULT 0"):         # 1 = о брошенной оплате уже напомнили
    try:
        con.execute(_sql); con.commit()
    except sqlite3.OperationalError:
        pass

con.executescript("""
CREATE TABLE IF NOT EXISTS auto_sent(
  user_id INTEGER, kind TEXT, ts INTEGER, PRIMARY KEY(user_id, kind));
""")

def create_promo(code, days, max_uses=0, expires=0, kind="days", value=0):
    con.execute("INSERT OR REPLACE INTO promo_codes(code,days,max_uses,used,created,expires,kind,value) VALUES(?,?,?,COALESCE((SELECT used FROM promo_codes WHERE code=?),0),?,?,?,?)",
                (code.upper(), days, max_uses, code.upper(), int(time.time()), expires, kind, value)); con.commit()

# ---------- скидки ----------
con.executescript("""
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY AUTOINCREMENT, percent REAL, start INTEGER, end INTEGER, title TEXT);
CREATE TABLE IF NOT EXISTS costs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, amount REAL, category TEXT, note TEXT);
""")
for _sql in ("ALTER TABLE payments ADD COLUMN code TEXT",            # код подарка, созданный этим платежом
             "ALTER TABLE payments ADD COLUMN refunded_at INTEGER",
             "ALTER TABLE payments ADD COLUMN bonus REAL DEFAULT 0"):   # бонус к пополнению баланса
    try:
        con.execute(_sql); con.commit()
    except sqlite3.OperationalError:
        pass

for _sql in ("ALTER TABLE users ADD COLUMN disc_until INTEGER DEFAULT 0",    # до какого момента действует скидка (0 = без срока)
             "ALTER TABLE users ADD COLUMN autorenew INTEGER DEFAULT 0",     # 1 = человек включил автопродление
             "ALTER TABLE users ADD COLUMN pm_id TEXT",                      # сохранённый способ оплаты в ЮKassa
             "ALTER TABLE users ADD COLUMN pm_title TEXT"):                  # подпись для человека: «•••• 1234»
    try:
        con.execute(_sql); con.commit()
    except sqlite3.OperationalError:
        pass

def first_purchase(uid):
    """True, если человек ещё ни разу не оплачивал подписку (подарки не считаются)."""
    return con.execute("SELECT 1 FROM payments WHERE user_id=? AND status='paid' AND COALESCE(gift,0)=0 AND plan!='topup' LIMIT 1", (uid,)).fetchone() is None

def active_sale(now=None):
    now = now or int(time.time())
    return con.execute("SELECT * FROM sales WHERE start<=? AND end>=? ORDER BY percent DESC LIMIT 1", (now, now)).fetchone()

def price_info(u, plan, gift=False):
    """(цена, код_промокода_если_он_потрачен, подпись_скидки). Из всех скидок берётся самая выгодная:
    личная по промокоду, общая акция, скидка на первую покупку. На подарки скидок нет. Минимум 1 ₽."""
    base = config.PLANS[plan][0]
    if gift:
        return base, None, ""
    cands = []
    if u["disc_kind"] and not (u["disc_until"] and u["disc_until"] < time.time()):   # просроченная скидка не действует
        val = u["disc_value"] or 0
        p = base * (1 - val / 100) if u["disc_kind"] == "percent" else base - val
        cands.append((p, u["disc_code"], disc_label(u)))
    sale = active_sale()
    if sale:
        cands.append((base * (1 - sale["percent"] / 100), None, f"акция −{sale['percent']:.0f}%"))
    if config.FIRST_DISC_PERCENT and first_purchase(u["id"]):
        cands.append((base * (1 - config.FIRST_DISC_PERCENT / 100), None, f"на первую покупку −{config.FIRST_DISC_PERCENT}%"))
    if not cands:
        return base, None, ""
    p, code, label = min(cands, key=lambda c: c[0])
    if p >= base:
        return base, None, ""
    return max(1, int(round(p))), code, label

def price_for(u, plan, gift=False):
    return price_info(u, plan, gift)[0]

def last_plan(uid):
    """Тариф последней оплаты (не подарок) или None."""
    r = con.execute("SELECT plan FROM payments WHERE user_id=? AND status='paid' AND COALESCE(gift,0)=0 AND plan!='topup' ORDER BY created DESC, id DESC LIMIT 1", (uid,)).fetchone()
    return r["plan"] if r else None

def create_sale(percent, start, end, title):
    cur = con.execute("INSERT INTO sales(percent,start,end,title) VALUES(?,?,?,?)", (percent, start, end, title)); con.commit(); return cur.lastrowid

def list_sales():
    return con.execute("SELECT * FROM sales ORDER BY start DESC").fetchall()

def delete_sale(sid):
    cur = con.execute("DELETE FROM sales WHERE id=?", (sid,)); con.commit(); return cur.rowcount

def disc_label(u):
    """'−20%' или '−100 ₽'; пустая строка, если скидки нет."""
    if u["disc_kind"] == "percent": return f"−{u['disc_value']:.0f}%"
    if u["disc_kind"] == "rub": return f"−{u['disc_value']:.0f} ₽"
    return ""

def _clear_disc(uid, code):
    con.execute("UPDATE users SET disc_kind=NULL, disc_value=0, disc_code=NULL WHERE id=? AND disc_code=?", (uid, code))

def make_gift_code(plan):
    """Одноразовый код на дни выбранного тарифа."""
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "GIFT-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if not con.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone(): break
    create_promo(code, config.PLANS[plan][1], 1, 0)
    return code

def delete_promo(code):
    cur = con.execute("DELETE FROM promo_codes WHERE code=?", (code.upper(),)); con.commit(); return cur.rowcount

def list_promos():
    return con.execute("SELECT * FROM promo_codes ORDER BY created DESC").fetchall()

def use_promo(uid, code):
    """Возвращает (статус, дней, до_какого_числа). Один код можно применить один раз на аккаунт."""
    code = code.strip().upper()
    p = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
    if not p: return ("not_found", 0, 0)
    if (p["expires"] or 0) and p["expires"] < time.time(): return ("expired", 0, 0)
    if p["max_uses"] and p["used"] >= p["max_uses"]: return ("exhausted", 0, 0)
    try:
        con.execute("INSERT INTO promo_uses(code,user_id,ts) VALUES(?,?,?)", (code, uid, int(time.time())))
    except sqlite3.IntegrityError:
        return ("already", 0, 0)
    con.execute("UPDATE promo_codes SET used=used+1 WHERE code=?", (code,)); con.commit()
    if (p["kind"] or "days") != "days":
        get_user(uid)
        con.execute("UPDATE users SET disc_kind=?, disc_value=?, disc_code=? WHERE id=?", (p["kind"], p["value"], code, uid)); con.commit()
        return ("disc", p["value"], p["kind"])
    return ("ok", p["days"], extend(uid, p["days"]))

# ---------- источники трафика и партнёры ----------
con.executescript("""
CREATE TABLE IF NOT EXISTS sources(code TEXT PRIMARY KEY, title TEXT, percent REAL DEFAULT 0, owner_id INTEGER, created INTEGER);
""")
for _sql in ("ALTER TABLE users ADD COLUMN src TEXT",              # код источника, откуда человек пришёл (первое касание)
             "ALTER TABLE payments ADD COLUMN ref_to INTEGER",     # кому начислен бонус за эту оплату
             "ALTER TABLE payments ADD COLUMN ref_bonus REAL"):    # сколько начислено (хранится, чтобы возврат откатил ровно это)
    try:
        con.execute(_sql); con.commit()
    except sqlite3.OperationalError:
        pass

def src_get(code):
    return con.execute("SELECT * FROM sources WHERE code=?", (code,)).fetchone() if code else None

def src_list():
    return con.execute("SELECT * FROM sources ORDER BY created DESC").fetchall()

def src_create(title, percent=0, owner_id=None):
    while True:
        code = "s" + secrets.token_hex(3)
        if not src_get(code): break
    con.execute("INSERT INTO sources(code,title,percent,owner_id,created) VALUES(?,?,?,?,?)",
                (code, title, percent or 0, owner_id, int(time.time()))); con.commit()
    return code

def src_update(code, **fields):
    for k, v in fields.items():
        assert k in ("title", "percent", "owner_id")
        con.execute(f"UPDATE sources SET {k}=? WHERE code=?", (v, code))
    con.commit()

def src_delete(code):
    con.execute("DELETE FROM sources WHERE code=?", (code,)); con.commit()

def src_stats(code, now=None):
    now = now or int(time.time())
    q = lambda sql, *a: con.execute(sql, a).fetchone()
    pay = q("SELECT COUNT(DISTINCT p.user_id), COUNT(*), COALESCE(SUM(p.amount),0) FROM payments p JOIN users u ON u.id=p.user_id "
            "WHERE u.src=? AND p.status='paid' AND p.method!='balance'", code)
    return {"regs": q("SELECT COUNT(*) FROM users WHERE src=?", code)[0],
            "trials": q("SELECT COUNT(*) FROM users WHERE src=? AND trial_used=1", code)[0],
            "active": q("SELECT COUNT(*) FROM users WHERE src=? AND sub_until>?", code, now)[0],
            "payers": pay[0], "pay_n": pay[1], "revenue": pay[2],
            "earned": q("SELECT COALESCE(SUM(p.ref_bonus),0) FROM payments p JOIN users u ON u.id=p.user_id "
                        "WHERE u.src=? AND p.status='paid' AND p.ref_to IS NOT NULL", code)[0]}

def partner_sources(uid):
    return con.execute("SELECT * FROM sources WHERE owner_id=? ORDER BY created DESC", (uid,)).fetchall()

def is_partner(uid):
    return con.execute("SELECT 1 FROM sources WHERE owner_id=? LIMIT 1", (uid,)).fetchone() is not None

def bonus_target(u, amount):
    """Кому и сколько начислить за оплату человека u: пригласившему друга (REF_PERCENT) или партнёру источника (его процент)."""
    if u["ref"]:
        return u["ref"], amount * config.REF_PERCENT / 100
    s = src_get(u["src"])
    if s and s["owner_id"] and s["percent"] and s["owner_id"] != u["id"]:
        return s["owner_id"], amount * s["percent"] / 100
    return None, 0

def set_yk_id(pid, yk_id):
    con.execute("UPDATE payments SET yk_id=? WHERE id=?", (yk_id, pid)); con.commit()

def get_payment(pid):
    return con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()

def cancel_payment(pid):
    """True, если платёж был в ожидании и мы его отменили (повторный вызов вернёт False)."""
    cur = con.execute("UPDATE payments SET status='canceled' WHERE id=? AND status='pending'", (pid,)); con.commit()
    return cur.rowcount > 0

def get_payment_by_yk(yk_id):
    return con.execute("SELECT * FROM payments WHERE yk_id=?", (yk_id,)).fetchone()

def pending_payments(max_age=86400):
    return con.execute("SELECT * FROM payments WHERE status='pending' AND yk_id IS NOT NULL AND created>?",
                       (int(time.time()) - max_age,)).fetchall()

def set_email(uid, email):
    con.execute("UPDATE users SET email=? WHERE id=?", (email, uid)); con.commit()

def get_user(uid, ref=None, src=None):
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        ref = ref if ref and ref != uid else None
        src = None if ref else src   # приглашение друга важнее метки источника
        con.execute("INSERT INTO users(id, ref, src, created) VALUES(?,?,?,?)", (uid, ref, src, int(time.time())))
        con.commit()
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row

def extend(uid, days):
    u = get_user(uid)
    new = int(max(u["sub_until"], time.time()) + days * 86400)
    con.execute("UPDATE users SET sub_until=? WHERE id=?", (new, uid)); con.commit()
    return new

def claim_trial(uid):
    u = get_user(uid)
    if u["trial_used"]:
        return None
    con.execute("UPDATE users SET trial_used=1 WHERE id=?", (uid,)); con.commit()
    return extend(uid, config.TRIAL_DAYS)

def create_payment(uid, plan, method, gift=False):
    u = get_user(uid)
    price, disc, _ = price_info(u, plan, gift)
    cur = con.execute("INSERT INTO payments(user_id,plan,amount,method,status,created,gift,disc) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, plan, price, method, "pending", int(time.time()), 1 if gift else 0, disc))
    con.commit(); return cur.lastrowid

def create_topup(uid, amount, method):
    """Счёт на пополнение основного баланса. Бонус считается сразу и хранится в платеже."""
    get_user(uid)
    amount = int(amount)
    bonus = int(amount * config.TOPUP_BONUS_PERCENT / 100)
    cur = con.execute("INSERT INTO payments(user_id,plan,amount,method,status,created,gift,bonus) VALUES(?,?,?,?,?,?,0,?)",
                      (uid, "topup", amount, method, "pending", int(time.time()), bonus))
    con.commit(); return cur.lastrowid

def complete_payment(pid):
    """Идемпотентно: повторный вызов ничего не делает."""
    cur = con.execute("UPDATE payments SET status='paid' WHERE id=? AND status!='paid'", (pid,))
    con.commit()
    if cur.rowcount == 0:
        return None
    p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if p["plan"] == "topup":   # пополнение баланса: деньги + бонус на основной баланс, реферального бонуса нет
        add = (p["amount"] or 0) + (p["bonus"] or 0)
        con.execute("UPDATE users SET topup=COALESCE(topup,0)+? WHERE id=?", (add, p["user_id"])); con.commit()
        return p["user_id"], "topup", None, None
    code = None
    if p["gift"]:
        code, until = make_gift_code(p["plan"]), None
        con.execute("UPDATE payments SET code=? WHERE id=?", (code, pid)); con.commit()
    else:
        until = extend(p["user_id"], config.PLANS[p["plan"]][1])
        if p["disc"]: _clear_disc(p["user_id"], p["disc"]); con.commit()
    u = get_user(p["user_id"])
    to, bonus = bonus_target(u, p["amount"])
    if to:
        con.execute("UPDATE users SET balance=balance+?, ref_earned=ref_earned+? WHERE id=?", (bonus, bonus, to))
        con.execute("UPDATE payments SET ref_to=?, ref_bonus=? WHERE id=?", (to, bonus, pid)); con.commit()
    return p["user_id"], p["plan"], until, code   # code != None для подарка (until тогда None)

def friends_stats(uid):
    """(приглашено, всего заработано, текущий реферальный баланс)"""
    n = con.execute("SELECT COUNT(*) c FROM users WHERE ref=?", (uid,)).fetchone()["c"]
    u = get_user(uid)
    return n, u["ref_earned"], u["balance"]

def total_balance(u):
    return round((u["balance"] or 0) + (u["topup"] or 0), 2)

def add_balance(uid, amount, kind):
    """kind: 'main' (основной) или 'ref' (реферальный). amount может быть отрицательным.
    Баланс не уходит ниже нуля. Возвращает обновлённую строку пользователя."""
    get_user(uid)
    col = "topup" if kind == "main" else "balance"
    con.execute(f"UPDATE users SET {col}=MAX(0, COALESCE({col},0)+?) WHERE id=?", (amount, uid))
    if kind == "ref" and amount > 0:
        con.execute("UPDATE users SET ref_earned=ref_earned+? WHERE id=?", (amount, uid))
    con.commit()
    return get_user(uid)

def pay_with_balance(uid, plan, gift=False):
    """Оплата тарифа с баланса: сначала реферальный, потом основной.
    Возвращает ("sub", дата_окончания) или ("gift", код); None, если денег не хватает."""
    days = config.PLANS[plan][1]
    u = get_user(uid)
    price, disc, _ = price_info(u, plan, gift)
    if total_balance(u) < price:
        return None
    from_ref = min(u["balance"] or 0, price)
    from_main = price - from_ref
    cur = con.execute("UPDATE users SET balance=balance-?, topup=topup-? WHERE id=? AND balance>=? AND topup>=?",
                      (from_ref, from_main, uid, from_ref, from_main - 1e-9))
    if cur.rowcount == 0:
        con.rollback(); return None
    ins = con.execute("INSERT INTO payments(user_id,plan,amount,method,status,created,gift,disc) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, plan, price, "balance", "paid", int(time.time()), 1 if gift else 0, disc))
    if gift:
        code = make_gift_code(plan)
        con.execute("UPDATE payments SET code=? WHERE id=?", (code, ins.lastrowid)); con.commit()
        return ("gift", code)
    if disc: _clear_disc(uid, disc)
    con.commit()
    return ("sub", extend(uid, days))

def due_reminders(now, within_days=3):
    return con.execute("SELECT * FROM users WHERE sub_until>? AND sub_until<? AND reminded_for!=sub_until",
                       (now, now + within_days * 86400)).fetchall()

# ---------- брошенные оплаты ----------
_ABANDON_SQL = ("SELECT p.*, u.username, u.name FROM payments p JOIN users u ON u.id=p.user_id "
                "WHERE p.status='pending' AND p.plan!='topup' AND p.created<? AND p.created>? AND COALESCE(u.banned,0)=0 {extra} "
                "AND NOT EXISTS (SELECT 1 FROM payments q WHERE q.user_id=p.user_id AND q.status='paid' AND q.created>=p.created) "
                "ORDER BY p.created DESC")

def _latest_per_user(rows):
    seen, out = set(), []
    for r in rows:
        if r["user_id"] not in seen:
            seen.add(r["user_id"]); out.append(r)
    return out

def abandoned_to_nudge(now, after_sec, max_age=86400):
    """Неоплаченные счета старше after_sec (но не старше суток), о которых ещё не напоминали. По одному на человека."""
    rows = con.execute(_ABANDON_SQL.format(extra="AND COALESCE(p.nudged,0)=0"), (now - after_sec, now - max_age)).fetchall()
    return _latest_per_user(rows)

def abandoned_list(now, max_age=86400):
    """Все неоплаченные счета за последние сутки (для админа), по одному на человека."""
    return _latest_per_user(con.execute(_ABANDON_SQL.format(extra=""), (now + 1, now - max_age)).fetchall())

def mark_nudged(uid):
    con.execute("UPDATE payments SET nudged=1 WHERE user_id=? AND status='pending'", (uid,)); con.commit()

# ---------- автосообщения ----------
def auto_key(kind, u):
    return f"miss:{u['sub_until']}" if kind == "miss" else kind

def auto_candidates(kind, now):
    """Кому пора отправить автосообщение. Окна по времени не дают завалить старых пользователей при первом запуске.
    welcome: через час после регистрации; day3: на 3-й день, если подписка активна;
    miss: через неделю после конца подписки."""
    if kind == "welcome":
        where, params, key = "u.created<? AND u.created>?", (now - 3600, now - 86400), "'welcome'"
    elif kind == "day3":
        where, params, key = "u.created<=? AND u.created>? AND u.sub_until>?", (now - 3 * 86400, now - 4 * 86400, now), "'day3'"
    elif kind == "miss":
        where, params, key = "u.sub_until>0 AND u.sub_until<=? AND u.sub_until>?", (now - 7 * 86400, now - 8 * 86400), "'miss:'||u.sub_until"
    else:
        return []
    return con.execute(f"SELECT u.* FROM users u WHERE COALESCE(u.banned,0)=0 AND {where} "
                       f"AND NOT EXISTS (SELECT 1 FROM auto_sent a WHERE a.user_id=u.id AND a.kind={key})", params).fetchall()

def mark_auto(uid, key):
    con.execute("INSERT OR IGNORE INTO auto_sent(user_id,kind,ts) VALUES(?,?,?)", (uid, key, int(time.time()))); con.commit()

def auto_counts():
    return con.execute("SELECT CASE WHEN instr(kind,':')>0 THEN substr(kind,1,instr(kind,':')-1) ELSE kind END k, COUNT(*) c "
                       "FROM auto_sent GROUP BY k").fetchall()

# ---------- лог действий админов, служебные значения, отчёт ----------
con.executescript("""
CREATE TABLE IF NOT EXISTS admin_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, admin_id INTEGER, action TEXT, target TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
""")

def log_action(admin_id, action, target="", details=""):
    con.execute("INSERT INTO admin_log(ts,admin_id,action,target,details) VALUES(?,?,?,?,?)",
                (int(time.time()), admin_id, action, target, details)); con.commit()

def recent_log(limit=20):
    return con.execute("SELECT l.ts, l.action, l.target, l.details, l.admin_id AS id, u.username, u.name "
                       "FROM admin_log l LEFT JOIN users u ON u.id=l.admin_id ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()

def kv_get(k):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None

def kv_set(k, v):
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v)); con.commit()

def report_data(now):
    """Цифры для отчёта за последние сутки."""
    since = now - 86400
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
    money = "FROM payments WHERE status='paid' AND method!='balance' AND created>?"
    return {
        "new": q("SELECT COUNT(*) FROM users WHERE created>?", since),
        "pay_n": q("SELECT COUNT(*) " + money, since),
        "pay_sum": q("SELECT SUM(amount) " + money, since),
        "gifts": q("SELECT COUNT(*) " + money + " AND gift=1", since),
        "bal_sum": q("SELECT SUM(amount) FROM payments WHERE status='paid' AND method='balance' AND created>?", since),
        "promo": q("SELECT COUNT(*) FROM promo_uses WHERE ts>?", since),
        "active": q("SELECT COUNT(*) FROM users WHERE sub_until>?", now),
        "exp_day": q("SELECT COUNT(*) FROM users WHERE sub_until>? AND sub_until<=?", now, now + 86400),
        "exp_3d": q("SELECT COUNT(*) FROM users WHERE sub_until>? AND sub_until<=?", now, now + 3 * 86400),
        "abandoned": len(abandoned_list(now)),
    }

# ---------- возвраты, расходы, прибыль, прогноз ----------
def _refund_bonus(p, info):
    """Какой бонус (пригласившему или партнёру) откатить при возврате. Берём сохранённое при оплате; для старых платежей считаем по REF_PERCENT."""
    if p["method"] == "balance": return
    if p["ref_to"]:
        info["ref"], info["bonus"] = p["ref_to"], p["ref_bonus"] or 0
    else:
        u = get_user(p["user_id"])
        if u["ref"]:
            info["ref"], info["bonus"] = u["ref"], p["amount"] * config.REF_PERCENT / 100

def refund_plan(pid):
    """Что произойдёт при возврате платежа. (статус, платёж, детали). Статус ok, если вернуть можно."""
    p = get_payment(pid)
    if not p: return "not_found", None, {}
    if p["status"] != "paid": return "not_paid", p, {}
    now = int(time.time()); info = {}
    if p["plan"] == "topup":
        add = (p["amount"] or 0) + (p["bonus"] or 0)
        if (get_user(p["user_id"])["topup"] or 0) < add - 1e-9:
            return "topup_spent", p, {}
        return "ok", p, {"topup": add}
    if p["gift"]:
        if p["code"] and con.execute("SELECT 1 FROM promo_uses WHERE code=?", (p["code"],)).fetchone():
            return "gift_used", p, {}
        info["gift_code"] = p["code"]
        _refund_bonus(p, info)
    else:
        days = config.PLANS.get(p["plan"], (0, 0, ""))[1]
        u = get_user(p["user_id"])
        info["days"] = days
        info["new_until"] = max(now, u["sub_until"] - days * 86400) if u["sub_until"] > now else u["sub_until"]
        _refund_bonus(p, info)
    return "ok", p, info

def apply_refund(pid):
    """Помечает платёж возвращённым и откатывает последствия: дни подписки, реферальный бонус, подарочный код.
    Платёж с баланса возвращается на основной баланс. (статус, детали)."""
    st, p, info = refund_plan(pid)
    if st != "ok": return st, info
    cur = con.execute("UPDATE payments SET status='refunded', refunded_at=? WHERE id=? AND status='paid'", (int(time.time()), pid))
    if cur.rowcount == 0: return "not_paid", {}
    if p["plan"] == "topup":
        con.execute("UPDATE users SET topup=MAX(0,COALESCE(topup,0)-?) WHERE id=?", (info["topup"], p["user_id"])); con.commit()
        return "ok", info
    if p["gift"]:
        if info.get("gift_code"): con.execute("DELETE FROM promo_codes WHERE code=?", (info["gift_code"],))
    else:
        con.execute("UPDATE users SET sub_until=? WHERE id=?", (info["new_until"], p["user_id"]))
    if info.get("ref"):
        con.execute("UPDATE users SET balance=MAX(0,balance-?), ref_earned=MAX(0,ref_earned-?) WHERE id=?",
                    (info["bonus"], info["bonus"], info["ref"]))
    if p["method"] == "balance":
        con.execute("UPDATE users SET topup=COALESCE(topup,0)+? WHERE id=?", (p["amount"], p["user_id"]))
    con.commit()
    return "ok", info

def add_cost(amount, category, note=""):
    cur = con.execute("INSERT INTO costs(ts,amount,category,note) VALUES(?,?,?,?)", (int(time.time()), amount, category, note)); con.commit()
    return cur.lastrowid

def recent_costs(limit=15):
    return con.execute("SELECT * FROM costs ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()

def delete_cost(cid):
    cur = con.execute("DELETE FROM costs WHERE id=?", (cid,)); con.commit(); return cur.rowcount

def profit(since):
    """Выручка деньгами (без оплат с баланса), расходы по категориям и возвраты за период."""
    q = lambda sql, *a: con.execute(sql, a).fetchone()
    rev = q("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM payments WHERE status='paid' AND method!='balance' AND created>=?", since)
    ref = q("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM payments WHERE status='refunded' AND method!='balance' AND refunded_at>=?", since)
    cats = con.execute("SELECT category, SUM(amount) s FROM costs WHERE ts>=? GROUP BY category ORDER BY s DESC", (since,)).fetchall()
    return {"revenue": rev[0], "pay_n": rev[1], "refunds": ref[0], "refund_n": ref[1],
            "costs": sum(c["s"] for c in cats), "cats": [(c["category"], c["s"]) for c in cats]}

def forecast(now, days=7):
    """Кто и на какую сумму может продлиться в ближайшие days дней (по прошлому тарифу каждого)."""
    rows = con.execute("SELECT u.id, (SELECT plan FROM payments p WHERE p.user_id=u.id AND p.status='paid' AND COALESCE(p.gift,0)=0 AND p.plan!='topup' "
                       "ORDER BY p.created DESC, p.id DESC LIMIT 1) AS plan FROM users u "
                       "WHERE COALESCE(u.banned,0)=0 AND u.sub_until>? AND u.sub_until<=?", (now, now + days * 86400)).fetchall()
    payers = [r for r in rows if r["plan"] in config.PLANS]
    last = con.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND method!='balance' AND created>?", (now - days * 86400,)).fetchone()[0]
    return {"total": len(rows), "payers": len(payers), "potential": sum(config.PLANS[r["plan"]][0] for r in payers), "last": last}

def mark_reminded(uid, until):
    con.execute("UPDATE users SET reminded_for=? WHERE id=?", (until, uid)); con.commit()

def stats(now=None):
    """Сводка для админа."""
    now = now or int(time.time())
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0] or 0
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "active": q("SELECT COUNT(*) FROM users WHERE sub_until>?", now),
        "expired": q("SELECT COUNT(*) FROM users WHERE sub_until>0 AND sub_until<=?", now),
        "never": q("SELECT COUNT(*) FROM users WHERE sub_until=0"),
        "trial": q("SELECT COUNT(*) FROM users WHERE trial_used=1"),
        "payers": q("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status='paid' AND method!='balance'"),
        "pay_n": q("SELECT COUNT(*) FROM payments WHERE status='paid' AND method!='balance'"),
        "pay_sum": q("SELECT SUM(amount) FROM payments WHERE status='paid' AND method!='balance'"),
        "bal_sum": q("SELECT SUM(amount) FROM payments WHERE status='paid' AND method='balance'"),
        "balances": q("SELECT SUM(COALESCE(balance,0)+COALESCE(topup,0)) FROM users"),
        "refs": q("SELECT COUNT(*) FROM users WHERE ref IS NOT NULL"),
        "promo_uses": q("SELECT COUNT(*) FROM promo_uses"),
        "new_day": q("SELECT COUNT(*) FROM users WHERE created>?", now - 86400),
        "new_week": q("SELECT COUNT(*) FROM users WHERE created>?", now - 7 * 86400),
        "banned": q("SELECT COUNT(*) FROM users WHERE COALESCE(banned,0)=1"),
    }

def list_users(offset=0, limit=30):
    """(всего, строки). Сначала у кого подписка дольше."""
    total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows = con.execute("SELECT * FROM users ORDER BY sub_until DESC, id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return total, rows

def touch_user(uid, username, name):
    """Запоминает имя и @username. Не создаёт пользователя (его создаёт get_user, чтобы не потерять реферала)."""
    con.execute("UPDATE users SET username=?, name=? WHERE id=? AND (COALESCE(username,'')!=? OR COALESCE(name,'')!=?)",
                (username, name, uid, username, name)); con.commit()

def users_without_profile(ids):
    """Из списка ID оставляет тех, чьё имя ещё не известно."""
    if not ids: return []
    q = ",".join("?" * len(ids))
    rows = con.execute(f"SELECT id FROM users WHERE id IN ({q}) AND name IS NULL AND username IS NULL", ids).fetchall()
    return [r["id"] for r in rows]

def delete_all_promos(only_spent=False):
    """Удаляет все промокоды (или только полностью использованные). Возвращает сколько удалено."""
    if only_spent:
        cur = con.execute("DELETE FROM promo_codes WHERE max_uses>0 AND used>=max_uses")
    else:
        cur = con.execute("DELETE FROM promo_codes")
    con.commit(); return cur.rowcount

def count_promos(only_spent=False):
    sql = "SELECT COUNT(*) FROM promo_codes" + (" WHERE max_uses>0 AND used>=max_uses" if only_spent else "")
    return con.execute(sql).fetchone()[0]


# ---------- админские выборки ----------
def peek_user(uid):
    """Пользователь без создания записи. None, если нет."""
    return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def is_banned(uid):
    r = con.execute("SELECT banned FROM users WHERE id=?", (uid,)).fetchone()
    return bool(r and r["banned"])

def set_banned(uid, flag):
    con.execute("UPDATE users SET banned=? WHERE id=?", (1 if flag else 0, uid)); con.commit()

def find_users(q, limit=20):
    """Поиск по ID (точно), @username и имени (по части, без учёта регистра). Среди известных профилей."""
    q = (q or "").strip().lstrip("@").lower()
    if not q: return []
    if q.isdigit():
        r = peek_user(int(q)); return [r] if r else []
    out = []
    for r in con.execute("SELECT * FROM users WHERE username IS NOT NULL OR name IS NOT NULL").fetchall():
        if q in (r["username"] or "").lower() or q in (r["name"] or "").lower():
            out.append(r)
            if len(out) >= limit: break
    return out

def count_unknown_profiles():
    return con.execute("SELECT COUNT(*) FROM users WHERE name IS NULL AND username IS NULL").fetchone()[0]

def expiring_users(days):
    now = int(time.time())
    return con.execute("SELECT * FROM users WHERE sub_until>? AND sub_until<? ORDER BY sub_until",
                       (now, now + days * 86400)).fetchall()

def recent_payments(limit=15):
    return con.execute("SELECT p.id AS pid, p.created, p.amount, p.method, p.plan, p.user_id AS id, u.username, u.name "
                       "FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE p.status='paid' "
                       "ORDER BY p.created DESC, p.id DESC LIMIT ?", (limit,)).fetchall()

def user_payments(uid, limit=5):
    return con.execute("SELECT * FROM payments WHERE user_id=? AND status='paid' ORDER BY created DESC, id DESC LIMIT ?",
                       (uid, limit)).fetchall()

def user_pay_totals(uid):
    """(число оплат деньгами, сумма)"""
    r = con.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE user_id=? AND status='paid' AND method!='balance'", (uid,)).fetchone()
    return r[0], r[1]

def promo_use_count(uid):
    return con.execute("SELECT COUNT(*) FROM promo_uses WHERE user_id=?", (uid,)).fetchone()[0]

def segment_ids(seg):
    """ID для рассылки: all | active | expired | never. Заблокированные не входят."""
    now = int(time.time())
    paid = "EXISTS (SELECT 1 FROM payments p WHERE p.user_id=users.id AND p.status='paid' AND p.method!='balance' AND p.plan!='topup')"
    cond = {"all": "1=1", "active": "sub_until>?", "expired": "sub_until>0 AND sub_until<=?", "never": "sub_until=0",
            "new7": "created>?", "new30": "created>?", "paid": paid, "trialonly": f"trial_used=1 AND NOT {paid}"}[seg]
    params = {"new7": (now - 7 * 86400,), "new30": (now - 30 * 86400,)}.get(seg, (now,) if "?" in cond else ())
    return [r["id"] for r in con.execute(f"SELECT id FROM users WHERE COALESCE(banned,0)=0 AND {cond}", params).fetchall()]
