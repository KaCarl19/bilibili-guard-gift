"""SQLite 数据层。所有数据存放在 data/guard.db"""
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from .config import DATA_DIR, RULES

DB_PATH = DATA_DIR / "guard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    qq           TEXT NOT NULL,
    bili_uid     TEXT,
    bili_name    TEXT,
    guard_level  TEXT,               -- 舰长 / 提督 / 总督
    month        TEXT NOT NULL,      -- 上舰所属月份 YYYY-MM
    start_date   TEXT NOT NULL,      -- 上舰日期 YYYY-MM-DD
    status       TEXT NOT NULL,      -- verified 已验证 / pending 待审核 / rejected 已拒绝
    source       TEXT,               -- api / screenshot / manual
    screenshot   TEXT,
    shot_hash    TEXT,
    ocr_text     TEXT,
    note         TEXT,
    shipped      INTEGER DEFAULT 0,  -- 是否已发货
    created_at   TEXT NOT NULL,
    UNIQUE(qq, month)
);
CREATE TABLE IF NOT EXISTS addresses (
    qq          TEXT PRIMARY KEY,
    receiver    TEXT NOT NULL,
    phone       TEXT NOT NULL,
    address     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS address_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qq          TEXT NOT NULL,
    receiver    TEXT, phone TEXT, address TEXT,
    changed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token       TEXT PRIMARY KEY,
    qq          TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS joins (      -- 进群待验证记录
    qq          TEXT NOT NULL,
    group_id    TEXT NOT NULL,
    joined_at   TEXT NOT NULL,
    PRIMARY KEY (qq, group_id)
);
"""


# 私聊里观众直接发来的新地址/手机号，打开网页时预填（只存在内存里，保存后清除）
DRAFTS: dict[str, dict] = {}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def this_month() -> str:
    return date.today().strftime("%Y-%m")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


# ---------------- 舰长记录 ----------------
def upsert_guard(qq, month, start_date, status, *, bili_uid=None, bili_name=None,
                 guard_level=None, source=None, screenshot=None, shot_hash=None,
                 ocr_text=None, note=None):
    """同一个 QQ 同一个月只保留一条记录；已验证的记录不会被降级为待审核。"""
    qq = str(qq)
    with conn() as c:
        old = c.execute("SELECT * FROM guards WHERE qq=? AND month=?", (qq, month)).fetchone()
        if old and old["status"] == "verified" and status != "verified":
            return dict(old)
        if old:
            c.execute(
                """UPDATE guards SET bili_uid=COALESCE(?,bili_uid), bili_name=COALESCE(?,bili_name),
                   guard_level=COALESCE(?,guard_level), start_date=?, status=?, source=?,
                   screenshot=COALESCE(?,screenshot), shot_hash=COALESCE(?,shot_hash),
                   ocr_text=COALESCE(?,ocr_text), note=? WHERE id=?""",
                (bili_uid, bili_name, guard_level, start_date, status, source,
                 screenshot, shot_hash, ocr_text, note, old["id"]))
        else:
            c.execute(
                """INSERT INTO guards (qq,bili_uid,bili_name,guard_level,month,start_date,status,
                   source,screenshot,shot_hash,ocr_text,note,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (qq, bili_uid, bili_name, guard_level, month, start_date, status, source,
                 screenshot, shot_hash, ocr_text, note, now()))
        return dict(c.execute("SELECT * FROM guards WHERE qq=? AND month=?", (qq, month)).fetchone())


def get_guard(gid):
    with conn() as c:
        r = c.execute("SELECT * FROM guards WHERE id=?", (gid,)).fetchone()
        return dict(r) if r else None


def set_guard_status(gid, status, note=None):
    with conn() as c:
        c.execute("UPDATE guards SET status=?, note=COALESCE(?,note) WHERE id=?", (status, note, gid))


def set_shipped(gid, shipped: bool):
    with conn() as c:
        c.execute("UPDATE guards SET shipped=? WHERE id=?", (1 if shipped else 0, gid))


def delete_guard(gid):
    with conn() as c:
        c.execute("DELETE FROM guards WHERE id=?", (gid,))


def hash_used_by_other(shot_hash, qq):
    """同一张截图被别的 QQ 用过 -> 疑似盗图"""
    with conn() as c:
        r = c.execute("SELECT qq FROM guards WHERE shot_hash=? AND qq<>?", (shot_hash, str(qq))).fetchone()
        return r["qq"] if r else None


def uid_used_by_other(bili_uid, qq, month):
    with conn() as c:
        r = c.execute("SELECT qq FROM guards WHERE bili_uid=? AND month=? AND qq<>? AND status='verified'",
                      (str(bili_uid), month, str(qq))).fetchone()
        return r["qq"] if r else None


def guards_of_qq(qq):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM guards WHERE qq=? ORDER BY month DESC", (str(qq),))]


def editable_guard(qq):
    """返回该 QQ 当月（或宽限期内上月）已验证的舰长记录；没有则返回 None —— 不允许填写/修改地址"""
    months = [this_month()]
    grace = int(RULES.get("edit_grace_days", 0) or 0)
    today = date.today()
    if grace and today.day <= grace:
        months.append((today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    with conn() as c:
        for m in months:
            r = c.execute("SELECT * FROM guards WHERE qq=? AND month=? AND status='verified'",
                          (str(qq), m)).fetchone()
            if r:
                return dict(r)
    return None


def list_guards(month=None, status=None):
    sql = """SELECT g.*, a.receiver, a.phone, a.address, a.updated_at AS addr_updated
             FROM guards g LEFT JOIN addresses a ON a.qq = g.qq WHERE 1=1"""
    args = []
    if month:
        sql += " AND g.month=?"; args.append(month)
    if status:
        sql += " AND g.status=?"; args.append(status)
    sql += " ORDER BY g.start_date DESC, g.id DESC"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def all_months():
    with conn() as c:
        return [r["month"] for r in c.execute("SELECT DISTINCT month FROM guards ORDER BY month DESC")]


# ---------------- 地址 ----------------
def get_address(qq):
    with conn() as c:
        r = c.execute("SELECT * FROM addresses WHERE qq=?", (str(qq),)).fetchone()
        return dict(r) if r else None


def save_address(qq, receiver, phone, address):
    qq = str(qq)
    with conn() as c:
        old = c.execute("SELECT * FROM addresses WHERE qq=?", (qq,)).fetchone()
        if old:
            c.execute("INSERT INTO address_history (qq,receiver,phone,address,changed_at) VALUES (?,?,?,?,?)",
                      (qq, old["receiver"], old["phone"], old["address"], now()))
        c.execute("""INSERT INTO addresses (qq,receiver,phone,address,updated_at) VALUES (?,?,?,?,?)
                     ON CONFLICT(qq) DO UPDATE SET receiver=excluded.receiver, phone=excluded.phone,
                     address=excluded.address, updated_at=excluded.updated_at""",
                  (qq, receiver, phone, address, now()))
    DRAFTS.pop(qq, None)


# ---------------- 填写链接 token ----------------
def new_token(qq, hours):
    t = secrets.token_urlsafe(16)
    exp = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        c.execute("DELETE FROM tokens WHERE expires_at < ?", (now(),))
        c.execute("INSERT INTO tokens (token,qq,expires_at) VALUES (?,?,?)", (t, str(qq), exp))
    return t


def qq_of_token(token):
    with conn() as c:
        r = c.execute("SELECT qq FROM tokens WHERE token=? AND expires_at >= ?", (token, now())).fetchone()
        return r["qq"] if r else None


# ---------------- 进群待验证 ----------------
def add_join(qq, group_id):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO joins (qq,group_id,joined_at) VALUES (?,?,?)",
                  (str(qq), str(group_id), now()))


def remove_join(qq):
    with conn() as c:
        c.execute("DELETE FROM joins WHERE qq=?", (str(qq),))


def is_joining(qq):
    with conn() as c:
        return c.execute("SELECT 1 FROM joins WHERE qq=?", (str(qq),)).fetchone() is not None


def expired_joins(minutes):
    limit = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM joins WHERE joined_at < ?", (limit,))]
