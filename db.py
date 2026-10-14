import sqlite3, time
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

for _sql in ("ALTER TABLE payments ADD COLUMN yk_id TEXT", "ALTER TABLE users ADD COLUMN email TEXT"):
    try:
        con.execute(_sql); con.commit()
    except sqlite3.OperationalError:
        pass

con.executescript("""
CREATE TABLE IF NOT EXISTS promo_codes(
  code TEXT PRIMARY KEY, days INTEGER, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS promo_uses(
  code TEXT, user_id INTEGER, ts INTEGER, PRIMARY KEY(code, user_id));
""")

def create_promo(code, days, max_uses=0):
    con.execute("INSERT OR REPLACE INTO promo_codes(code,days,max_uses,used,created) VALUES(?,?,?,COALESCE((SELECT used FROM promo_codes WHERE code=?),0),?)",
                (code.upper(), days, max_uses, code.upper(), int(time.time()))); con.commit()

def delete_promo(code):
    cur = con.execute("DELETE FROM promo_codes WHERE code=?", (code.upper(),)); con.commit(); return cur.rowcount

def list_promos():
    return con.execute("SELECT * FROM promo_codes ORDER BY created DESC").fetchall()

def use_promo(uid, code):
    """Возвращает (статус, дней, до_какого_числа). Один код можно применить один раз на аккаунт."""
    code = code.strip().upper()
    p = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
    if not p: return ("not_found", 0, 0)
    if p["max_uses"] and p["used"] >= p["max_uses"]: return ("exhausted", 0, 0)
    try:
        con.execute("INSERT INTO promo_uses(code,user_id,ts) VALUES(?,?,?)", (code, uid, int(time.time())))
    except sqlite3.IntegrityError:
        return ("already", 0, 0)
    con.execute("UPDATE promo_codes SET used=used+1 WHERE code=?", (code,)); con.commit()
    return ("ok", p["days"], extend(uid, p["days"]))

def set_yk_id(pid, yk_id):
    con.execute("UPDATE payments SET yk_id=? WHERE id=?", (yk_id, pid)); con.commit()

def get_payment(pid):
    return con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()

def cancel_payment(pid):
    con.execute("UPDATE payments SET status='canceled' WHERE id=? AND status='pending'", (pid,)); con.commit()

def pending_payments(max_age=86400):
    return con.execute("SELECT * FROM payments WHERE status='pending' AND yk_id IS NOT NULL AND created>?",
                       (int(time.time()) - max_age,)).fetchall()

def set_email(uid, email):
    con.execute("UPDATE users SET email=? WHERE id=?", (email, uid)); con.commit()

def get_user(uid, ref=None):
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        ref = ref if ref and ref != uid else None
        con.execute("INSERT INTO users(id, ref, created) VALUES(?,?,?)", (uid, ref, int(time.time())))
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

def create_payment(uid, plan, method):
    price = config.PLANS[plan][0]
    cur = con.execute("INSERT INTO payments(user_id,plan,amount,method,status,created) VALUES(?,?,?,?,?,?)",
                      (uid, plan, price, method, "pending", int(time.time())))
    con.commit(); return cur.lastrowid

def complete_payment(pid):
    """Идемпотентно: повторный вызов ничего не делает."""
    cur = con.execute("UPDATE payments SET status='paid' WHERE id=? AND status!='paid'", (pid,))
    con.commit()
    if cur.rowcount == 0:
        return None
    p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    until = extend(p["user_id"], config.PLANS[p["plan"]][1])
    u = get_user(p["user_id"])
    if u["ref"]:
        con.execute("UPDATE users SET balance=balance+? WHERE id=?",
                    (p["amount"] * config.REF_PERCENT / 100, u["ref"])); con.commit()
    return p["user_id"], p["plan"], until

def friends_stats(uid):
    n = con.execute("SELECT COUNT(*) c FROM users WHERE ref=?", (uid,)).fetchone()["c"]
    return n, get_user(uid)["balance"]

def due_reminders(now, within_days=3):
    return con.execute("SELECT * FROM users WHERE sub_until>? AND sub_until<? AND reminded_for!=sub_until",
                       (now, now + within_days * 86400)).fetchall()

def mark_reminded(uid, until):
    con.execute("UPDATE users SET reminded_for=? WHERE id=?", (until, uid)); con.commit()
