# app.py
# Mahjong Table System (Python/Flask + LINE OA + LIFF) - Render deploy-ready
# ===================================================
# ✅ 本版重點：
# 1) 開桌可設定：房名 + 時間（現在/早中晚半夜/精確時間）
# 2) 配桌進隱藏池（match_pool），不顯示在 LIFF
# 3) 自動匹配條件：只有「開桌時間=現在」且「開桌備註=空」才匹配配桌池
#    匹配項：金額/手速/將數/缺口人數（手速允許不限）
# 4) 移除 Flex 白色卡片：主選單只回文字+QuickReply
# 5) 保留：四人確認、30秒未點選=視為放棄+自動扣分-5（僅此情況自動扣）
#
# Render -> Environment 需設定：
#   CHANNEL_ACCESS_TOKEN
#   CHANNEL_SECRET
# 建議設定：
#   OWNER_USER_ID = 店家老闆 userId（Uxxxxxxxx...）
# 可選：
#   DATABASE_PATH (default: /tmp/mahjong.db)
#   BASE_URL (default: https://mahjong-line-bot.onrender.com)
#   LIFF_ID (default: 2009050373-HHA8grO4)

import os
import re
import json
import sqlite3
import random
import secrets
from datetime import datetime, timedelta

from flask import Flask, request, abort, jsonify, render_template_string, send_file

from openpyxl import Workbook

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage,
    TextSendMessage,
    QuickReply, QuickReplyButton,
    MessageAction, URIAction,
)

# ----------------------------
# Config
# ----------------------------
CHANNEL_ACCESS_TOKEN = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or os.getenv("CHANNEL_ACCESS_TOKEN", "")).strip()
CHANNEL_SECRET = (os.getenv("LINE_CHANNEL_SECRET") or os.getenv("CHANNEL_SECRET", "")).strip()

DATABASE_PATH = os.getenv("DATABASE_PATH", "/tmp/mahjong.db")
LIFF_ID = os.getenv("LIFF_ID", "2009050373-HHA8grO4").strip()
BASE_URL = os.getenv("BASE_URL", "https://mahjong-line-bot.onrender.com").strip().rstrip("/")
OWNER_USER_ID = os.getenv("OWNER_USER_ID", "").strip()

TABLE_SIZE = 4
CREDIT_FREEZE_THRESHOLD = 60

MATCH_SPEEDS = ["快手", "慢手", "不限"]
MATCH_PARTY_SIZES = ["我1人", "我2人", "我3人"]
MATCH_AMOUNTS = ["50/20", "100/20", "100/50", "200/50"]
MATCH_ROUNDS = ["2將", "3將"]

# 開桌時間選項
TIME_MODE_OPTIONS = ["現在", "早", "中", "晚", "半夜", "精確時間"]
TIME_MODE_MAP = {
    "現在": "NOW",
    "早": "PERIOD",
    "中": "PERIOD",
    "晚": "PERIOD",
    "半夜": "PERIOD",
    "精確時間": "EXACT",
}

PHONE_RE = re.compile(r"^09\d{8}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")  # HH:MM

ADMIN_MAX_COUNT = 5
ADMIN_CODE_EXPIRE_MIN = 10

EXPORT_EXPIRE_MIN = 15
EXPORT_DIR = "/tmp/exports"

DEDUCTION_OPTIONS = [
    ("放鳥", -20),
    ("取消", -5),
    ("遲到", -10),
    ("玩家檢舉", -15),
]

# confirming 逾時秒數
CONFIRM_TIMEOUT_SEC = 30
AUTO_CONFIRM_TIMEOUT_DEDUCT = -5
AUTO_CONFIRM_TIMEOUT_REASON = "確認逾時未點選"

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ---- sanity check: avoid silent no-response when env vars missing ----
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    print("⚠️ [CONFIG] Missing LINE channel credentials. Check Render env vars: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET (or CHANNEL_ACCESS_TOKEN / CHANNEL_SECRET).")

# ----------------------------
# Time helpers
# ----------------------------
def iso_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def dt_to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def iso_to_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")

def month_key_utc(dt: datetime = None) -> str:
    dt = dt or datetime.utcnow()
    return dt.strftime("%Y-%m")

# ----------------------------
# DB
# ----------------------------
def db_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _try_alter(cur, sql: str):
    try:
        cur.execute(sql)
    except Exception:
        pass

def init_db():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        nickname TEXT,
        phone TEXT,
        role TEXT DEFAULT 'customer',
        credit INTEGER DEFAULT 100,
        frozen INTEGER DEFAULT 0,
        manual_nickname INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS shops (
        shop_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT DEFAULT '店家',
        group_link TEXT,
        map_link TEXT,
        shop_line_link TEXT,
        is_open INTEGER DEFAULT 1,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS shop_admins (
        shop_id INTEGER,
        user_id TEXT,
        PRIMARY KEY (shop_id, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_invite_codes (
        code TEXT PRIMARY KEY,
        shop_id INTEGER,
        created_by TEXT,
        created_at TEXT,
        expire_at TEXT,
        used_by TEXT,
        used_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_state (
        user_id TEXT PRIMARY KEY,
        state TEXT,
        data TEXT,
        updated_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS table_sequences (
        month_key TEXT PRIMARY KEY,
        next_no INTEGER
    )
    """)

    # match_requests: 只放「開桌」(公開桌) + confirming/fill 狀態
    cur.execute("""
    CREATE TABLE IF NOT EXISTS match_requests (
        req_id INTEGER PRIMARY KEY AUTOINCREMENT,
        month_key TEXT,
        table_no INTEGER,
        creator_user_id TEXT,
        req_type TEXT,                   -- open
        speed TEXT,
        amount TEXT,
        rounds TEXT,
        remark TEXT,
        room_name TEXT,                  -- ✅ 房名
        time_mode TEXT,                  -- ✅ NOW / PERIOD / EXACT
        time_period TEXT,                -- ✅ 早/中/晚/半夜
        time_exact TEXT,                 -- ✅ HH:MM
        status TEXT DEFAULT 'waiting',   -- waiting / confirming / filled / cancelled
        confirm_started_at TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS request_participants (
        req_id INTEGER,
        user_id TEXT,
        party_size INTEGER,
        confirmed INTEGER DEFAULT 0,
        joined_at TEXT,
        PRIMARY KEY (req_id, user_id)
    )
    """)

    # ✅ 配桌隱藏池
    cur.execute("""
    CREATE TABLE IF NOT EXISTS match_pool (
        pool_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        speed TEXT,
        amount TEXT,
        rounds TEXT,
        party_size INTEGER,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS credit_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_user_id TEXT,
        delta INTEGER,
        reason TEXT,
        by_user_id TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS export_files (
        export_id TEXT PRIMARY KEY,
        file_path TEXT,
        created_by TEXT,
        created_at TEXT,
        expire_at TEXT
    )
    """)

    conn.commit()

    # 舊 DB 遷移欄位（盡量不爆）
    _try_alter(cur, "ALTER TABLE match_requests ADD COLUMN room_name TEXT")
    _try_alter(cur, "ALTER TABLE match_requests ADD COLUMN time_mode TEXT")
    _try_alter(cur, "ALTER TABLE match_requests ADD COLUMN time_period TEXT")
    _try_alter(cur, "ALTER TABLE match_requests ADD COLUMN time_exact TEXT")
    _try_alter(cur, "ALTER TABLE match_requests ADD COLUMN confirm_started_at TEXT")
    conn.commit()

    # Ensure one shop exists
    cur.execute("SELECT shop_id FROM shops ORDER BY shop_id LIMIT 1")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO shops (name, created_at) VALUES (?, ?)", ("店家", iso_now()))
        conn.commit()

    conn.close()

def get_shop():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM shops ORDER BY shop_id LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row

def shop_is_open() -> bool:
    shop = get_shop()
    if not shop:
        return True
    return int(shop["is_open"] or 0) == 1

def update_shop_field(field: str, value):
    if field not in ("group_link", "map_link", "shop_line_link", "is_open", "name"):
        return False
    shop = get_shop()
    if not shop:
        return False
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE shops SET {field}=? WHERE shop_id=?", (value, shop["shop_id"]))
    conn.commit()
    conn.close()
    return True

def get_user(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def set_user_role(user_id: str, role: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
    conn.commit()
    conn.close()

def ensure_owner(user_id: str):
    shop = get_shop()
    if not shop:
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO shop_admins (shop_id, user_id) VALUES (?, ?)", (shop["shop_id"], user_id))
    conn.commit()
    conn.close()
    set_user_role(user_id, "shop_owner")

def get_or_create_user(user_id: str, display_name: str = ""):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO users (user_id, nickname, created_at) VALUES (?, ?, ?)",
            (user_id, display_name or "", iso_now())
        )
        conn.commit()
    else:
        manual = int(row["manual_nickname"] or 0)
        if manual == 0:
            current_nick = (row["nickname"] or "").strip()
            if (not current_nick) and display_name:
                cur.execute("UPDATE users SET nickname=? WHERE user_id=?", (display_name, user_id))
                conn.commit()

    conn.close()

    if OWNER_USER_ID and user_id == OWNER_USER_ID:
        ensure_owner(user_id)

def set_user_phone(user_id: str, phone: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET phone=? WHERE user_id=?", (phone, user_id))
    conn.commit()
    conn.close()

def set_user_nickname_manual(user_id: str, nickname: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET nickname=?, manual_nickname=1 WHERE user_id=?", (nickname, user_id))
    conn.commit()
    conn.close()

def get_state(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_state WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, {}
    try:
        data = json.loads(row["data"] or "{}")
    except Exception:
        data = {}
    return row["state"], data

def upsert_state(user_id: str, state: str, data: dict):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO user_state (user_id, state, data, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        state = excluded.state,
        data = excluded.data,
        updated_at = excluded.updated_at
    """, (user_id, state, json.dumps(data or {}, ensure_ascii=False), iso_now()))
    conn.commit()
    conn.close()

def clear_state(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def count_shop_admins() -> int:
    shop = get_shop()
    if not shop:
        return 0
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM shop_admins WHERE shop_id=?", (shop["shop_id"],))
    row = cur.fetchone()
    conn.close()
    return int(row["c"] if row else 0)

def add_shop_admin(user_id: str) -> (bool, str):
    shop = get_shop()
    if not shop:
        return False, "店家資料不存在。"
    if count_shop_admins() >= ADMIN_MAX_COUNT:
        return False, f"⚠️ 管理員已達上限（{ADMIN_MAX_COUNT}位）"
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO shop_admins (shop_id, user_id) VALUES (?, ?)", (shop["shop_id"], user_id))
    conn.commit()
    conn.close()
    u = get_user(user_id)
    if u and u["role"] != "shop_owner":
        set_user_role(user_id, "shop_admin")
    return True, "✅ 已新增為管理員"

def is_shop_admin(user_id: str) -> bool:
    if OWNER_USER_ID and user_id == OWNER_USER_ID:
        return True
    u = get_user(user_id)
    if u and u["role"] in ("shop_admin", "shop_owner"):
        return True
    shop = get_shop()
    if not shop:
        return False
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM shop_admins WHERE shop_id=? AND user_id=?", (shop["shop_id"], user_id))
    row = cur.fetchone()
    conn.close()
    return True if row else False

def list_shop_admin_user_ids():
    shop = get_shop()
    if not shop:
        return []
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM shop_admins WHERE shop_id=? ORDER BY user_id", (shop["shop_id"],))
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

def remove_shop_admin(user_id: str) -> bool:
    shop = get_shop()
    if not shop:
        return False
    if OWNER_USER_ID and user_id == OWNER_USER_ID:
        return False
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM shop_admins WHERE shop_id=? AND user_id=?", (shop["shop_id"], user_id))
    conn.commit()
    conn.close()
    u = get_user(user_id)
    if u and u["role"] == "shop_admin":
        set_user_role(user_id, "customer")
    return True

# ----------------------------
# Admin code (6 digits)
# ----------------------------
def cleanup_expired_codes():
    shop = get_shop()
    if not shop:
        return
    now = datetime.utcnow()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT code, expire_at, used_by FROM admin_invite_codes WHERE shop_id=?", (shop["shop_id"],))
    rows = cur.fetchall()
    for r in rows:
        if r["used_by"]:
            continue
        try:
            exp = iso_to_dt(r["expire_at"])
        except Exception:
            exp = now - timedelta(days=1)
        if exp < now:
            cur.execute("DELETE FROM admin_invite_codes WHERE code=?", (r["code"],))
    conn.commit()
    conn.close()

def create_invite_code(created_by: str) -> (bool, str):
    shop = get_shop()
    if not shop:
        return False, "店家資料不存在。"
    if count_shop_admins() >= ADMIN_MAX_COUNT:
        return False, f"⚠️ 管理員已達上限（{ADMIN_MAX_COUNT}位）"
    cleanup_expired_codes()

    for _ in range(30):
        code = f"{random.randint(0, 999999):06d}"
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admin_invite_codes WHERE code=?", (code,))
        if cur.fetchone():
            conn.close()
            continue

        now = datetime.utcnow()
        expire_at = now + timedelta(minutes=ADMIN_CODE_EXPIRE_MIN)
        cur.execute("""
        INSERT INTO admin_invite_codes (code, shop_id, created_by, created_at, expire_at)
        VALUES (?, ?, ?, ?, ?)
        """, (code, shop["shop_id"], created_by, dt_to_iso(now), dt_to_iso(expire_at)))
        conn.commit()
        conn.close()
        return True, f"✅ 6位數驗證碼：{code}\n有效期限：{ADMIN_CODE_EXPIRE_MIN} 分鐘（一次性）"

    return False, "產生失敗，請再試一次。"

def redeem_invite_code(code: str, user_id: str) -> (bool, str):
    shop = get_shop()
    if not shop:
        return False, "店家資料不存在。"
    cleanup_expired_codes()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM admin_invite_codes WHERE code=? AND shop_id=?", (code, shop["shop_id"]))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "無效的 6 位碼或已過期。"
    if row["used_by"]:
        conn.close()
        return False, "此 6 位碼已使用。"

    try:
        exp = iso_to_dt(row["expire_at"])
    except Exception:
        exp = datetime.utcnow() - timedelta(days=1)
    if exp < datetime.utcnow():
        cur.execute("DELETE FROM admin_invite_codes WHERE code=?", (code,))
        conn.commit()
        conn.close()
        return False, "此 6 位碼已過期。"

    if count_shop_admins() >= ADMIN_MAX_COUNT:
        conn.close()
        return False, f"⚠️ 管理員已達上限（{ADMIN_MAX_COUNT}位）"

    cur.execute("""
    UPDATE admin_invite_codes
    SET used_by=?, used_at=?
    WHERE code=?
    """, (user_id, iso_now(), code))
    conn.commit()
    conn.close()

    ok, msg = add_shop_admin(user_id)
    if not ok:
        return False, msg
    return True, "✅ 已新增為管理員。"

# ----------------------------
# Gate checks
# ----------------------------
def must_bind_phone(user_id: str) -> bool:
    u = get_user(user_id)
    return (not u) or (not (u["phone"] or "").strip())

def is_frozen(user_id: str) -> bool:
    u = get_user(user_id)
    if not u:
        return False
    return int(u["frozen"] or 0) == 1

def reply_main(reply_token: str, user_id: str, text: str):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=text, quick_reply=main_menu_qr(user_id)))
    except Exception as e:
        print("⚠️ [reply_main] reply error:", e)

def ensure_shop_open_or_message(reply_token: str, user_id: str) -> bool:
    if shop_is_open():
        return True
    reply_main(reply_token, user_id, "⚠️ 目前未有店家上線（休息中）\n暫停配桌服務")
    return False

def ensure_not_frozen_or_message(reply_token: str, user_id: str) -> bool:
    if not is_frozen(user_id):
        return True
    reply_main(reply_token, user_id, "⚠️ 你的帳號目前凍結，暫時無法配桌/開桌\n請聯絡店家處理")
    return False

# ----------------------------
# Monthly table number sequence
# ----------------------------
def allocate_table_no() -> (str, int):
    mk = month_key_utc()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT next_no FROM table_sequences WHERE month_key=?", (mk,))
    row = cur.fetchone()
    if row is None:
        next_no = 1
        cur.execute("INSERT INTO table_sequences (month_key, next_no) VALUES (?, ?)", (mk, 2))
        conn.commit()
        conn.close()
        return mk, next_no
    next_no = int(row["next_no"] or 1)
    cur.execute("UPDATE table_sequences SET next_no=? WHERE month_key=?", (next_no + 1, mk))
    conn.commit()
    conn.close()
    return mk, next_no

# ----------------------------
# Request helpers
# ----------------------------
def request_participant_sum(req_id: int) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(party_size),0) AS s FROM request_participants WHERE req_id=?", (int(req_id),))
    row = cur.fetchone()
    conn.close()
    return int(row["s"] if row else 0)

def request_participant_count(req_id: int) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM request_participants WHERE req_id=?", (int(req_id),))
    row = cur.fetchone()
    conn.close()
    return int(row["c"] if row else 0)

def request_confirmed_count(req_id: int) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM request_participants WHERE req_id=? AND confirmed=1", (int(req_id),))
    row = cur.fetchone()
    conn.close()
    return int(row["c"] if row else 0)

def list_participants(req_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM request_participants WHERE req_id=? ORDER BY joined_at", (int(req_id),))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_request(req_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM match_requests WHERE req_id=?", (int(req_id),))
    row = cur.fetchone()
    conn.close()
    return row

def set_request_status(req_id: int, status: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE match_requests SET status=? WHERE req_id=?", (status, int(req_id)))
    conn.commit()
    conn.close()

def set_confirm_started(req_id: int, started_at: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE match_requests SET confirm_started_at=? WHERE req_id=?", (started_at, int(req_id)))
    conn.commit()
    conn.close()

def reset_confirmations(req_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE request_participants SET confirmed=0 WHERE req_id=?", (int(req_id),))
    conn.commit()
    conn.close()

def display_table_no(req_row) -> str:
    return f"{req_row['month_key']}-{int(req_row['table_no'])}"

def display_time(req_row) -> str:
    mode = (req_row["time_mode"] or "").strip()
    if mode == "NOW":
        return "現在"
    if mode == "PERIOD":
        p = (req_row["time_period"] or "").strip()
        return p if p else "時段"
    if mode == "EXACT":
        t = (req_row["time_exact"] or "").strip()
        return f"{t}" if t else "時間"
    return "-"

def create_open_request(
    creator_user_id: str,
    speed: str,
    party_size: int,
    amount: str,
    rounds: str,
    room_name: str,
    time_mode: str,
    time_period: str,
    time_exact: str,
    remark: str
):
    mk, table_no = allocate_table_no()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO match_requests (
        month_key, table_no, creator_user_id, req_type,
        speed, amount, rounds, remark,
        room_name, time_mode, time_period, time_exact,
        status, confirm_started_at, created_at
    )
    VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', NULL, ?)
    """, (
        mk, int(table_no), creator_user_id,
        speed, amount, rounds, remark or "",
        (room_name or "").strip(),
        (time_mode or "").strip(),
        (time_period or "").strip(),
        (time_exact or "").strip(),
        iso_now()
    ))
    req_id = cur.lastrowid
    cur.execute("""
    INSERT OR REPLACE INTO request_participants (req_id, user_id, party_size, confirmed, joined_at)
    VALUES (?, ?, ?, 0, ?)
    """, (req_id, creator_user_id, int(party_size), iso_now()))
    conn.commit()
    conn.close()
    return req_id

def list_open_lobby_tables(limit: int = 200):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM match_requests
    WHERE req_type='open' AND status IN ('waiting','confirming')
    ORDER BY req_id DESC
    LIMIT ?
    """, (int(limit),))
    rows = cur.fetchall()
    conn.close()
    return rows

def find_active_open_request_for_user(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT mr.*
    FROM request_participants rp
    JOIN match_requests mr ON mr.req_id = rp.req_id
    WHERE rp.user_id = ? AND mr.req_type='open' AND mr.status IN ('waiting','confirming')
    ORDER BY mr.req_id DESC
    LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def user_in_pool(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM match_pool WHERE user_id=? ORDER BY pool_id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def remove_pool(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM match_pool WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def add_to_pool(user_id: str, speed: str, amount: str, rounds: str, party_size: int):
    # 先清掉舊的（避免一個人重複入池）
    remove_pool(user_id)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO match_pool (user_id, speed, amount, rounds, party_size, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, speed, amount, rounds, int(party_size), iso_now()))
    conn.commit()
    conn.close()

def give_up(req_id: int, user_id: str) -> (bool, str):
    req = get_request(req_id)
    if not req:
        return False, "找不到此桌。"
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM request_participants WHERE req_id=? AND user_id=?", (int(req_id), user_id))
    conn.commit()
    conn.close()

    remaining = request_participant_count(req_id)
    if remaining <= 0:
        set_request_status(req_id, "cancelled")
        return True, "已取消。"

    set_request_status(req_id, "waiting")
    set_confirm_started(req_id, None)
    reset_confirmations(req_id)
    return True, "已放棄，其他人繼續等待中。"

def cancel_all_for_user(user_id: str) -> (bool, str):
    # 取消開桌/等待中的桌（若在桌內）
    active = find_active_open_request_for_user(user_id)
    if active:
        ok, msg = give_up(int(active["req_id"]), user_id)
        return True, f"已退出桌（{msg}）"
    # 取消配桌池
    if user_in_pool(user_id):
        remove_pool(user_id)
        return True, "已取消配桌（退出隱藏等待池）"
    return False, "你目前沒有進行中的開桌/配桌。"

def apply_deduction(target_user_id: str, delta: int, reason: str, by_user_id: str) -> (bool, str):
    u = get_user(target_user_id)
    if not u:
        return False, "找不到用戶。"
    new_credit = int(u["credit"] or 0) + int(delta)
    if new_credit < 0:
        new_credit = 0
    frozen = int(u["frozen"] or 0)
    if new_credit < CREDIT_FREEZE_THRESHOLD:
        frozen = 1

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET credit=?, frozen=? WHERE user_id=?", (int(new_credit), int(frozen), target_user_id))
    cur.execute("""
    INSERT INTO credit_logs (target_user_id, delta, reason, by_user_id, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (target_user_id, int(delta), reason, by_user_id, iso_now()))
    conn.commit()
    conn.close()

    return True, f"✅ 已扣分：{reason}（{delta}）\n目前信用分：{new_credit}\n狀態：{'凍結' if frozen==1 else '正常'}"

# ✅ confirming 逾時處理：30 秒未點選 => 視為放棄 + 扣 -5
def process_expired_confirmations():
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
        SELECT req_id, confirm_started_at
        FROM match_requests
        WHERE status='confirming' AND confirm_started_at IS NOT NULL AND TRIM(confirm_started_at) != ''
        """)
        rows = cur.fetchall()
        now = datetime.utcnow()
        expired_req_ids = []
        for r in rows:
            try:
                started = iso_to_dt(r["confirm_started_at"])
            except Exception:
                started = now - timedelta(days=1)
            if (now - started).total_seconds() >= CONFIRM_TIMEOUT_SEC:
                expired_req_ids.append(int(r["req_id"]))

        if not expired_req_ids:
            conn.close()
            return

        for req_id in expired_req_ids:
            cur.execute("SELECT user_id FROM request_participants WHERE req_id=? AND confirmed=0", (req_id,))
            unconfirmed = [x["user_id"] for x in cur.fetchall()]

            for uid in unconfirmed:
                cur.execute("DELETE FROM request_participants WHERE req_id=? AND user_id=?", (req_id, uid))
                try:
                    apply_deduction(uid, AUTO_CONFIRM_TIMEOUT_DEDUCT, AUTO_CONFIRM_TIMEOUT_REASON, by_user_id="SYSTEM")
                except Exception:
                    pass
                try:
                    line_bot_api.push_message(uid, TextSendMessage(text="⚠️ 你在確認階段超過30秒未點選，已視為放棄並扣分 -5"))
                except Exception:
                    pass

            cur.execute("SELECT COUNT(*) AS c FROM request_participants WHERE req_id=?", (req_id,))
            remaining = int(cur.fetchone()["c"])
            if remaining <= 0:
                cur.execute("UPDATE match_requests SET status='cancelled', confirm_started_at=NULL WHERE req_id=?", (req_id,))
            else:
                cur.execute("UPDATE match_requests SET status='waiting', confirm_started_at=NULL WHERE req_id=?", (req_id,))
                cur.execute("UPDATE request_participants SET confirmed=0 WHERE req_id=?", (req_id,))

        conn.commit()
        conn.close()
    except Exception:
        return

def push_confirm_to_participants(req_id: int):
    req = get_request(req_id)
    if not req:
        return
    participants = list_participants(req_id)
    if not participants:
        return
    confirm_uri = f"https://liff.line.me/{LIFF_ID}?view=confirm&req_id={req_id}"
    room = (req["room_name"] or "").strip()
    room_txt = f"｜房名 {room}" if room else ""
    msg = (
        f"✅ 人數已滿（桌號 {display_table_no(req)}{room_txt}）\n"
        f"請在 30 秒內完成確認：加入確認 / 放棄\n"
        f"（所有人都確認後才算成桌）\n\n"
        f"👉 確認連結：{confirm_uri}"
    )
    for p in participants:
        try:
            line_bot_api.push_message(p["user_id"], TextSendMessage(text=msg))
        except Exception:
            pass

def push_filled_info(req_id: int):
    req = get_request(req_id)
    if not req:
        return
    shop = get_shop()
    participants = list_participants(req_id)
    if not participants:
        return

    shop_name = (shop["name"] or "店家") if shop else "店家"
    group_link = (shop["group_link"] or "").strip() if shop else ""
    room = (req["room_name"] or "").strip()
    room_line = f"🏷️ 房名：{room}\n" if room else ""

    msg = (
        f"🎉 成桌成功\n"
        f"🏪 店家：{shop_name}\n"
        f"🪑 桌號：{display_table_no(req)}\n"
        f"{room_line}"
        f"⏰ 時間：{display_time(req)}\n"
        f"💰 金額：{req['amount'] or '-'}\n"
        f"⚡ 手速：{req['speed'] or '-'}\n"
        f"🀄 將數：{req['rounds'] or '-'}\n\n"
        f"🔗 群組連結：{group_link if group_link else '（尚未設定）'}\n\n"
        f"⏱️ 請於 20 分鐘內到店家\n"
        f"💬 進群後 3 分鐘內回報桌號"
    )
    for p in participants:
        try:
            line_bot_api.push_message(p["user_id"], TextSendMessage(text=msg))
        except Exception:
            pass

def set_request_status_confirming(req_id: int):
    set_request_status(req_id, "confirming")
    reset_confirmations(req_id)
    set_confirm_started(req_id, iso_now())
    push_confirm_to_participants(req_id)

def join_open_request(req_id: int, user_id: str, party_size: int) -> (bool, str, bool):
    process_expired_confirmations()
    req = get_request(req_id)
    if not req:
        return False, "找不到此桌。", False
    if req["status"] in ("filled", "cancelled"):
        return False, "此桌已結束或無法加入。", False
    if req["status"] == "confirming":
        return False, "此桌正在確認中，暫無法加入。", False

    current = request_participant_sum(req_id)
    missing = TABLE_SIZE - current
    if missing <= 0:
        return False, "此桌已滿。", False
    if int(party_size) > missing:
        return False, "人數超過 無法入桌", False

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO request_participants (req_id, user_id, party_size, confirmed, joined_at)
    VALUES (?, ?, ?, 0, ?)
    """, (int(req_id), user_id, int(party_size), iso_now()))
    conn.commit()
    conn.close()

    current2 = request_participant_sum(req_id)
    if current2 >= TABLE_SIZE:
        set_request_status_confirming(req_id)
        return True, "加入成功，已進入確認階段（30秒內需確認）。", True
    return True, "加入成功。", False

def confirm_join(req_id: int, user_id: str) -> (bool, str, bool):
    process_expired_confirmations()
    req = get_request(req_id)
    if not req:
        return False, "找不到此桌。", False
    if req["status"] != "confirming":
        return False, "此桌不在確認階段（可能已逾時回到等待）。", False

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM request_participants WHERE req_id=? AND user_id=?", (int(req_id), user_id))
    if not cur.fetchone():
        conn.close()
        return False, "你已不在此桌（可能逾時未確認已視為放棄）。", False

    cur.execute("UPDATE request_participants SET confirmed=1 WHERE req_id=? AND user_id=?", (int(req_id), user_id))
    conn.commit()
    conn.close()

    joined_cnt = request_participant_count(req_id)
    confirmed_cnt = request_confirmed_count(req_id)
    if joined_cnt > 0 and confirmed_cnt == joined_cnt:
        set_request_status(req_id, "filled")
        set_confirm_started(req_id, None)
        push_filled_info(req_id)
        return True, "✅ 已確認，成桌成功！", True

    return True, f"✅ 已確認（{confirmed_cnt}/{joined_cnt}）等待其他人確認…", False

# ----------------------------
# Auto match (pool -> open table)
# ----------------------------
def speed_compatible(pool_speed: str, table_speed: str) -> bool:
    pool_speed = (pool_speed or "").strip()
    table_speed = (table_speed or "").strip()
    if pool_speed == "不限" or table_speed == "不限":
        return True
    return pool_speed == table_speed

def is_open_table_eligible_for_auto(req_row) -> bool:
    # 只有開桌選「現在」+ 備註空 才能自動匹配
    if (req_row["time_mode"] or "").strip() != "NOW":
        return False
    if (req_row["remark"] or "").strip() != "":
        return False
    if req_row["status"] != "waiting":
        return False
    return True

def auto_fill_from_pool(req_id: int):
    """
    嘗試用 pool 補滿指定開桌（只在 eligible 時執行）
    """
    req = get_request(req_id)
    if not req or req["req_type"] != "open":
        return
    if not is_open_table_eligible_for_auto(req):
        return

    # 算缺口
    current = request_participant_sum(req_id)
    missing = max(0, TABLE_SIZE - current)
    if missing <= 0:
        return

    conn = db_conn()
    cur = conn.cursor()
    # pool 先到先配
    cur.execute("""
    SELECT * FROM match_pool
    ORDER BY created_at ASC, pool_id ASC
    """)
    pools = cur.fetchall()

    for p in pools:
        if missing <= 0:
            break
        # 條件比對：金額/將數/手速/缺口
        if (p["amount"] or "").strip() != (req["amount"] or "").strip():
            continue
        if (p["rounds"] or "").strip() != (req["rounds"] or "").strip():
            continue
        if not speed_compatible(p["speed"], req["speed"]):
            continue
        party = int(p["party_size"] or 1)
        if party > missing:
            continue

        # 加入桌
        try:
            ok, msg, to_confirm = join_open_request(int(req_id), p["user_id"], party)
            if ok:
                # 移除 pool
                cur.execute("DELETE FROM match_pool WHERE pool_id=?", (int(p["pool_id"]),))
                conn.commit()
                missing = max(0, TABLE_SIZE - request_participant_sum(req_id))
                # 通知玩家已自動加入
                try:
                    tno = display_table_no(get_request(req_id))
                    line_bot_api.push_message(p["user_id"], TextSendMessage(text=f"✅ 已自動加入符合條件的開桌：{tno}\n（若人數滿會進入確認階段）"))
                except Exception:
                    pass
        except Exception:
            continue

    conn.close()

def auto_match_pool_user(user_id: str):
    """
    當某人進 pool 後，掃描所有 eligible 的 open 桌，找到第一個可塞進去就塞
    """
    pool = user_in_pool(user_id)
    if not pool:
        return

    # 依桌建立時間（最早的優先），你也可以改成缺人最多優先
    tables = list_open_lobby_tables(limit=200)
    # 反轉成最早優先
    tables = list(reversed(tables))

    for t in tables:
        if not is_open_table_eligible_for_auto(t):
            continue
        if (pool["amount"] or "").strip() != (t["amount"] or "").strip():
            continue
        if (pool["rounds"] or "").strip() != (t["rounds"] or "").strip():
            continue
        if not speed_compatible(pool["speed"], t["speed"]):
            continue

        req_id = int(t["req_id"])
        missing = max(0, TABLE_SIZE - request_participant_sum(req_id))
        party = int(pool["party_size"] or 1)
        if party > missing:
            continue

        ok, msg, to_confirm = join_open_request(req_id, user_id, party)
        if ok:
            remove_pool(user_id)
            try:
                tno = display_table_no(get_request(req_id))
                line_bot_api.push_message(user_id, TextSendMessage(text=f"✅ 已自動加入符合條件的開桌：{tno}\n（若人數滿會進入確認階段）"))
            except Exception:
                pass
            break

# ----------------------------
# Export
# ----------------------------
def cleanup_exports():
    now = datetime.utcnow()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT export_id, file_path, expire_at FROM export_files")
    rows = cur.fetchall()
    for r in rows:
        try:
            exp = iso_to_dt(r["expire_at"])
        except Exception:
            exp = now - timedelta(days=1)
        if exp < now:
            fp = r["file_path"]
            try:
                if fp and os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
            cur.execute("DELETE FROM export_files WHERE export_id=?", (r["export_id"],))
    conn.commit()
    conn.close()

def create_export_file(created_by: str) -> (bool, str):
    cleanup_exports()
    export_id = secrets.token_urlsafe(16)
    file_path = os.path.join(EXPORT_DIR, f"customers_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{export_id}.xlsx")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT nickname, phone, credit, frozen, created_at
    FROM users
    WHERE phone IS NOT NULL AND TRIM(phone) != ''
    ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "customers"
    ws.append(["暱稱", "手機", "信用分", "狀態", "建立時間"])
    for r in rows:
        ws.append([
            r["nickname"] or "",
            r["phone"] or "",
            int(r["credit"] or 0),
            "凍結" if int(r["frozen"] or 0) == 1 else "正常",
            r["created_at"] or ""
        ])
    wb.save(file_path)

    now = datetime.utcnow()
    exp = now + timedelta(minutes=EXPORT_EXPIRE_MIN)
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO export_files (export_id, file_path, created_by, created_at, expire_at)
    VALUES (?, ?, ?, ?, ?)
    """, (export_id, file_path, created_by, dt_to_iso(now), dt_to_iso(exp)))
    conn.commit()
    conn.close()

    link = f"{BASE_URL}/export/{export_id}"
    return True, f"✅ 已產生 Excel（有效 {EXPORT_EXPIRE_MIN} 分鐘）\n下載連結：{link}"

def get_export_path(export_id: str):
    cleanup_exports()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT file_path, expire_at FROM export_files WHERE export_id=?", (export_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        exp = iso_to_dt(row["expire_at"])
    except Exception:
        return None
    if exp < datetime.utcnow():
        return None
    fp = row["file_path"]
    if not fp or not os.path.exists(fp):
        return None
    return fp

# ----------------------------
# Quick Reply / Menu
# ----------------------------
def liff_lobby_url():
    return f"https://liff.line.me/{LIFF_ID}?view=lobby"

def qr(actions):
    return QuickReply(items=[QuickReplyButton(action=a) for a in actions])

def main_menu_qr(user_id: str):
    actions = [
        MessageAction(label="🎲 開桌", text="開桌"),
        MessageAction(label="🧩 配桌", text="配桌"),
        URIAction(label="📋 桌況查詢", uri=liff_lobby_url()),
        MessageAction(label="👤 我的", text="我的"),
        MessageAction(label="☎️ 聯絡店家", text="聯絡店家"),
    ]
    if is_shop_admin(user_id):
        actions.append(MessageAction(label="🏪 店家後台", text="店家後台"))
        actions.append(MessageAction(label="🧾 客戶資訊", text="客戶資訊"))
    return qr(actions)

def sub_menu_qr():
    return qr([MessageAction(label="主選單", text="主選單")])

def contact_shop_qr():
    shop = get_shop()
    line_link = (shop["shop_line_link"] or "").strip() if shop else ""
    map_link = (shop["map_link"] or "").strip() if shop else ""
    actions = []
    if line_link:
        actions.append(URIAction(label="店家LINE", uri=line_link))
    if map_link:
        actions.append(URIAction(label="地圖", uri=map_link))
    actions.append(MessageAction(label="主選單", text="主選單"))
    return qr(actions)

def speed_qr():
    return qr([
        MessageAction(label="快手", text="快手"),
        MessageAction(label="慢手", text="慢手"),
        MessageAction(label="不限", text="不限"),
        MessageAction(label="主選單", text="主選單"),
    ])

def party_qr():
    return qr([
        MessageAction(label="我1人", text="我1人"),
        MessageAction(label="我2人", text="我2人"),
        MessageAction(label="我3人", text="我3人"),
        MessageAction(label="主選單", text="主選單"),
    ])

def amount_qr():
    return qr([
        MessageAction(label="50/20", text="50/20"),
        MessageAction(label="100/20", text="100/20"),
        MessageAction(label="100/50", text="100/50"),
        MessageAction(label="200/50", text="200/50"),
        MessageAction(label="主選單", text="主選單"),
    ])

def rounds_qr():
    return qr([
        MessageAction(label="2將", text="2將"),
        MessageAction(label="3將", text="3將"),
        MessageAction(label="主選單", text="主選單"),
    ])

def time_qr():
    return qr([
        MessageAction(label="現在", text="現在"),
        MessageAction(label="早", text="早"),
        MessageAction(label="中", text="中"),
        MessageAction(label="晚", text="晚"),
        MessageAction(label="半夜", text="半夜"),
        MessageAction(label="精確時間", text="精確時間"),
        MessageAction(label="主選單", text="主選單"),
    ])

def skip_qr():
    return qr([
        MessageAction(label="略過", text="略過"),
        MessageAction(label="主選單", text="主選單"),
    ])

def my_qr():
    return qr([
        MessageAction(label="修改暱稱", text="修改暱稱"),
        MessageAction(label="修改手機", text="修改手機"),
        MessageAction(label="主選單", text="主選單"),
    ])

def shop_backend_qr():
    return qr([
        MessageAction(label="店名設定", text="店名設定"),
        MessageAction(label="群設定", text="群設定"),
        MessageAction(label="地圖設定", text="地圖設定"),
        MessageAction(label="店家LINE設定", text="店家LINE設定"),
        MessageAction(label="營業/休息", text="營業/休息"),
        MessageAction(label="新增管理員(6位碼)", text="新增管理員"),
        MessageAction(label="輸入6位碼", text="輸入6位碼"),
        MessageAction(label="管理員名單", text="管理員名單"),
        MessageAction(label="移除管理員", text="移除管理員"),
        MessageAction(label="主選單", text="主選單"),
    ])

def customer_info_qr():
    return qr([
        MessageAction(label="查詢", text="客戶查詢"),
        MessageAction(label="導出Excel", text="導出用戶"),
        MessageAction(label="主選單", text="主選單"),
    ])

def deduction_qr():
    actions = []
    for name, delta in DEDUCTION_OPTIONS:
        actions.append(MessageAction(label=f"{name}{delta}", text=f"扣分 {name}"))
    actions.append(MessageAction(label="返回客戶資訊", text="客戶資訊"))
    actions.append(MessageAction(label="主選單", text="主選單"))
    return qr(actions)

def active_block_qr():
    return qr([
        MessageAction(label="取消", text="取消"),
        URIAction(label="📋 查看桌況", uri=liff_lobby_url()),
        MessageAction(label="回主畫面", text="主選單"),
    ])

def reply_sub(reply_token: str, text: str):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=text, quick_reply=sub_menu_qr()))
    except Exception as e:
        print("⚠️ [reply_sub] reply error:", e)

def reply_custom(reply_token: str, text: str, quick_reply_obj):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=text, quick_reply=quick_reply_obj))
    except Exception as e:
        print("⚠️ [reply_custom] reply error:", e)

# ✅ 移除 Flex 卡片：只回文字+按鍵
def reply_menu(reply_token, owner=False):
    """Always reply with main menu (QuickReply buttons)."""
    try:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text='請用下方按鍵操作：', quick_reply=menu_main(owner))
        )
    except Exception:
        # reply_token may be invalid/used; ignore
        pass

def handle_shop_backend(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    shop = get_shop()
    status = "營業" if shop and int(shop["is_open"] or 0) == 1 else "休息"
    shop_name = (shop["name"] or "店家") if shop else "店家"
    msg = (
        f"🏪 店家後台（目前：{status}）\n"
        f"店名：{shop_name}\n\n"
        f"可設定：\n"
        f"- 店名\n"
        f"- 群連結 / 地圖 / 店家LINE（貼上連結即可）\n"
        f"- 營業/休息（休息會關閉開桌/配桌/桌況）\n"
        f"- 新增管理員：6位碼（一次性 / {ADMIN_CODE_EXPIRE_MIN} 分鐘 / 上限{ADMIN_MAX_COUNT}位）"
    )
    reply_custom(reply_token, msg, shop_backend_qr())

def prompt_set_link(reply_token: str, user_id: str, field: str, label: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    upsert_state(user_id, "SET_SHOP_LINK", {"field": field, "label": label})
    reply_custom(reply_token, f"請貼上「{label}」連結：", shop_backend_qr())

def prompt_set_shop_name(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    upsert_state(user_id, "SET_SHOP_NAME", {})
    reply_custom(reply_token, "請輸入新的店家名稱（1~30字）：", shop_backend_qr())

def toggle_open(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    shop = get_shop()
    cur_status = int(shop["is_open"] or 0) if shop else 1
    new_status = 0 if cur_status == 1 else 1
    update_shop_field("is_open", new_status)
    reply_custom(reply_token, f"已切換為：{'營業' if new_status==1 else '休息'}", shop_backend_qr())

def generate_admin_code(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    ok, msg = create_invite_code(user_id)
    reply_custom(reply_token, msg, shop_backend_qr())

def handle_admin_list(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    ids = list_shop_admin_user_ids()
    if not ids:
        reply_custom(reply_token, "目前尚無管理員。", shop_backend_qr())
        return
    msg = "管理員名單（userId）：\n" + "\n".join([f"- {x}" for x in ids]) + "\n\n（移除：點「移除管理員」後貼上 userId）"
    reply_custom(reply_token, msg, shop_backend_qr())

def prompt_remove_admin(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    upsert_state(user_id, "REMOVE_ADMIN", {})
    reply_custom(reply_token, "請貼上要移除的管理員 userId：", shop_backend_qr())

def handle_customer_info(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    reply_custom(reply_token, "🧾 客戶資訊：請選擇", customer_info_qr())

def start_customer_search(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    upsert_state(user_id, "CUSTOMER_SEARCH", {})
    reply_custom(reply_token, "請輸入：暱稱關鍵字 或 手機末三碼（例如 123 或 阿明）", customer_info_qr())

def do_customer_search(keyword: str):
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    conn = db_conn()
    cur = conn.cursor()
    if keyword.isdigit() and len(keyword) == 3:
        cur.execute("""
        SELECT user_id, nickname, phone, credit, frozen, created_at
        FROM users
        WHERE phone LIKE ?
        ORDER BY created_at DESC
        LIMIT 10
        """, (f"%{keyword}",))
    else:
        cur.execute("""
        SELECT user_id, nickname, phone, credit, frozen, created_at
        FROM users
        WHERE nickname LIKE ?
        ORDER BY created_at DESC
        LIMIT 10
        """, (f"%{keyword}%",))
    rows = cur.fetchall()
    conn.close()
    return rows

def show_customer_detail(reply_token: str, admin_user_id: str, target_user_id: str):
    if not is_shop_admin(admin_user_id):
        reply_sub(reply_token, "您不是店家管理員。")
        return
    u = get_user(target_user_id)
    if not u or not (u["phone"] or "").strip():
        reply_custom(reply_token, "找不到此用戶或未綁定手機。", customer_info_qr())
        return
    upsert_state(admin_user_id, "CUSTOMER_DEDUCT", {"target_user_id": target_user_id})
    msg = (
        f"客戶資料：\n"
        f"暱稱：{u['nickname'] or '-'}\n"
        f"手機：{u['phone']}\n"
        f"信用分：{int(u['credit'] or 0)}\n"
        f"狀態：{'凍結' if int(u['frozen'] or 0)==1 else '正常'}\n\n"
        f"請選擇扣分項目："
    )
    reply_custom(reply_token, msg, deduction_qr())

# ----------------------------
# LIFF page
# ----------------------------
LIFF_BASE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Mahjong LIFF</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;padding:14px;background:#fafafa;}
    .wrap{max-width:760px;margin:0 auto;}
    .card{background:white;border:1px solid #e7e7e7;border-radius:12px;padding:12px;margin:10px 0;}
    .row{display:flex;gap:10px;flex-wrap:wrap;}
    .badge{display:inline-block;padding:3px 8px;border-radius:999px;background:#f2f2f2;font-size:12px;}
    button{padding:10px 12px;border-radius:10px;border:0;background:#1a73e8;color:white;font-size:15px;cursor:pointer;}
    button.secondary{background:#666;}
    button:disabled{opacity:0.6;cursor:not-allowed;}
    select{padding:10px;border-radius:10px;border:1px solid #ddd;background:white;}
    .muted{color:#666;font-size:13px;white-space:pre-wrap;}
    .title{font-weight:700;font-size:20px;margin:6px 0;}
    .small{font-size:13px;color:#666;}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="title" id="pageTitle">載入中…</div>
    <div class="small" id="me"></div>
    <div id="content"></div>
    <div class="muted" id="status"></div>
  </div>
<script>
  const LIFF_ID = "{{LIFF_ID}}";
  const VIEW = "{{VIEW}}";
  const REQ_ID = "{{REQ_ID}}";
  const BASE_LIFF = "{{BASE_LIFF}}";

  const elTitle = document.getElementById("pageTitle");
  const elMe = document.getElementById("me");
  const elContent = document.getElementById("content");
  const elStatus = document.getElementById("status");

  function escapeHtml(s){
    return (s||"").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  async function api(path, method="GET", body=null){
    const opt = {method, headers: {"Content-Type":"application/json"}};
    if(body) opt.body = JSON.stringify(body);
    const res = await fetch(path, opt);
    const data = await res.json();
    return data;
  }

  function showError(msg){ elStatus.textContent = msg; }

  async function init(){
    await liff.init({ liffId: LIFF_ID });
    if(!liff.isLoggedIn()){ liff.login(); return null; }
    const profile = await liff.getProfile();
    elMe.textContent = `已登入：${profile.displayName}`;
    return profile;
  }

  async function viewLobby(profile){
    elTitle.textContent = "桌況（只顯示開桌）";
    elContent.innerHTML = "<div class='muted'>載入桌列表…</div>";

    const data = await api("/api/lobby?user_id="+encodeURIComponent(profile.userId));
    if(!data.ok){
      elContent.innerHTML = "";
      showError(data.message || "載入失敗");
      return;
    }
    if(!data.tables || data.tables.length===0){
      elContent.innerHTML = "<div class='card'>目前沒有等待中的開桌。</div>";
      return;
    }

    let html = "";
    data.tables.forEach(t=>{
      html += `
        <div class="card">
          <div><b>桌號：</b>${escapeHtml(t.table_no)}${t.room_name ? "｜<b>房名：</b>"+escapeHtml(t.room_name) : ""}</div>
          <div class="row" style="margin-top:6px;">
            <span class="badge">時間 ${escapeHtml(t.time_text)}</span>
            <span class="badge">金額 ${escapeHtml(t.amount)}</span>
            <span class="badge">將數 ${escapeHtml(t.rounds)}</span>
            <span class="badge">手速 ${escapeHtml(t.speed)}</span>
            <span class="badge">${escapeHtml(t.count_text)}</span>
            <span class="badge">狀態 ${escapeHtml(t.status_text)}</span>
          </div>
          <div class="muted" style="margin-top:8px;">備註：${escapeHtml(t.remark || "無")}</div>
          <div style="margin-top:10px;">
            <button onclick="location.href=BASE_LIFF+'?view=table&req_id='+t.req_id">選這桌</button>
          </div>
        </div>
      `;
    });
    elContent.innerHTML = html;
  }

  async function viewTable(profile, reqId){
    elTitle.textContent = "加入桌位";
    elContent.innerHTML = "<div class='muted'>載入桌況…</div>";

    const st = await api("/api/table_status?req_id="+encodeURIComponent(reqId)+"&user_id="+encodeURIComponent(profile.userId));
    if(!st.ok){
      elContent.innerHTML = "";
      showError(st.message || "載入失敗");
      return;
    }

    const t = st.table;
    elContent.innerHTML = `
      <div class="card">
        <div><b>桌號：</b>${escapeHtml(t.table_no)}${t.room_name ? "｜<b>房名：</b>"+escapeHtml(t.room_name) : ""}</div>
        <div class="row" style="margin-top:6px;">
          <span class="badge">時間 ${escapeHtml(t.time_text)}</span>
          <span class="badge">金額 ${escapeHtml(t.amount)}</span>
          <span class="badge">將數 ${escapeHtml(t.rounds)}</span>
          <span class="badge">手速 ${escapeHtml(t.speed)}</span>
          <span class="badge">${escapeHtml(t.count_text)}</span>
          <span class="badge">狀態 ${escapeHtml(t.status_text)}</span>
        </div>
        <div class="muted" style="margin-top:8px;">備註：${escapeHtml(t.remark || "無")}</div>
      </div>

      <div class="card">
        <div style="margin-bottom:8px;"><b>選擇人數</b></div>
        <div class="row">
          <select id="party">
            <option value="1">我1人</option>
            <option value="2">我2人</option>
            <option value="3">我3人</option>
          </select>
          <button id="btnJoin">加入</button>
          <button class="secondary" onclick="location.href=BASE_LIFF+'?view=lobby'">回桌況</button>
        </div>
        <div class="muted" id="joinMsg" style="margin-top:10px;"></div>
      </div>
    `;

    const btn = document.getElementById("btnJoin");
    const sel = document.getElementById("party");
    const joinMsg = document.getElementById("joinMsg");

    btn.addEventListener("click", async ()=>{
      btn.disabled = true;
      joinMsg.textContent = "加入中…";
      const partySize = parseInt(sel.value || "1", 10);
      const res = await api("/api/join", "POST", { req_id: reqId, user_id: profile.userId, party_size: partySize });
      joinMsg.textContent = res.message || "完成";
      if(res.ok){
        if(res.to_confirm){
          setTimeout(()=>{ location.href = BASE_LIFF+'?view=confirm&req_id='+reqId; }, 900);
        }else{
          setTimeout(()=>{ location.href = BASE_LIFF+'?view=lobby'; }, 900);
        }
      }else{
        btn.disabled = false;
      }
    });
  }

  async function viewConfirm(profile, reqId){
    elTitle.textContent = "最後確認（30秒內）";
    elContent.innerHTML = "<div class='muted'>載入確認狀態…</div>";

    const st = await api("/api/table_status?req_id="+encodeURIComponent(reqId)+"&user_id="+encodeURIComponent(profile.userId));
    if(!st.ok){
      elContent.innerHTML = "";
      showError(st.message || "載入失敗");
      return;
    }
    const t = st.table;

    elContent.innerHTML = `
      <div class="card">
        <div><b>桌號：</b>${escapeHtml(t.table_no)}${t.room_name ? "｜<b>房名：</b>"+escapeHtml(t.room_name) : ""}</div>
        <div class="row" style="margin-top:6px;">
          <span class="badge">時間 ${escapeHtml(t.time_text)}</span>
          <span class="badge">金額 ${escapeHtml(t.amount)}</span>
          <span class="badge">將數 ${escapeHtml(t.rounds)}</span>
          <span class="badge">手速 ${escapeHtml(t.speed)}</span>
          <span class="badge">${escapeHtml(t.count_text)}</span>
          <span class="badge">狀態 ${escapeHtml(t.status_text)}</span>
        </div>
        <div class="muted" style="margin-top:8px;">備註：${escapeHtml(t.remark || "無")}</div>
      </div>

      <div class="card">
        <div class="muted">人數已滿，請選擇是否確認成桌（所有人都要確認）：</div>
        <div class="row" style="margin-top:10px;">
          <button id="btnConfirm">加入確認</button>
          <button class="secondary" id="btnGiveUp">放棄</button>
          <button class="secondary" onclick="location.href=BASE_LIFF+'?view=lobby'">回桌況</button>
        </div>
        <div class="muted" id="cmsg" style="margin-top:10px;"></div>
      </div>
    `;

    const cmsg = document.getElementById("cmsg");
    const btnC = document.getElementById("btnConfirm");
    const btnG = document.getElementById("btnGiveUp");

    btnC.addEventListener("click", async ()=>{
      btnC.disabled = true; btnG.disabled = true;
      cmsg.textContent = "送出確認中…";
      const res = await api("/api/confirm", "POST", { req_id: reqId, user_id: profile.userId });
      cmsg.textContent = res.message || "完成";
      if(res.ok && res.filled){
        setTimeout(()=>{ liff.closeWindow(); }, 1000);
      }else{
        btnC.disabled = false; btnG.disabled = false;
      }
    });

    btnG.addEventListener("click", async ()=>{
      btnC.disabled = true; btnG.disabled = true;
      cmsg.textContent = "放棄中…";
      const res = await api("/api/giveup", "POST", { req_id: reqId, user_id: profile.userId });
      cmsg.textContent = res.message || "完成";
      if(res.ok){
        setTimeout(()=>{ location.href = BASE_LIFF+'?view=lobby'; }, 900);
      }else{
        btnC.disabled = false; btnG.disabled = false;
      }
    });
  }

  (async ()=>{
    try{
      const profile = await init();
      if(!profile) return;
      if(VIEW === "lobby") await viewLobby(profile);
      else if(VIEW === "table") await viewTable(profile, REQ_ID);
      else if(VIEW === "confirm") await viewConfirm(profile, REQ_ID);
      else await viewLobby(profile);
    }catch(e){
      showError("LIFF 初始化失敗：" + (e && e.message ? e.message : e));
    }
  })();
</script>
</body>
</html>
"""

# ----------------------------
# Routes
# ----------------------------
@app.route("/", methods=["GET"])
def home():
    return "OK"

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": iso_now()})

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@app.route("/liff", methods=["GET"])
def liff_page():
    view = (request.args.get("view") or "lobby").strip()
    req_id = (request.args.get("req_id") or "").strip()
    if view not in ("lobby", "table", "confirm"):
        view = "lobby"
    if view in ("table", "confirm") and not req_id.isdigit():
        view = "lobby"
        req_id = ""
    base_liff = f"https://liff.line.me/{LIFF_ID}"
    return render_template_string(
        LIFF_BASE_HTML,
        LIFF_ID=LIFF_ID,
        VIEW=view,
        REQ_ID=req_id,
        BASE_LIFF=base_liff,
    )

@app.route("/export/<export_id>", methods=["GET"])
def download_export(export_id):
    fp = get_export_path(export_id)
    if not fp:
        return "Link expired or not found", 404
    return send_file(fp, as_attachment=True, download_name=os.path.basename(fp))

# ----------------------------
# LIFF APIs
# ----------------------------
@app.route("/api/lobby", methods=["GET"])
def api_lobby():
    process_expired_confirmations()

    user_id = (request.args.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"ok": False, "message": "missing user_id"}), 400

    if not shop_is_open():
        return jsonify({"ok": False, "message": "目前未有店家上線（休息中）"}), 200

    get_or_create_user(user_id, display_name="")
    if must_bind_phone(user_id):
        return jsonify({"ok": False, "message": "請先回到 LINE 對話綁定手機後再使用桌況。"}), 200
    if is_frozen(user_id):
        return jsonify({"ok": False, "message": "你的帳號目前凍結，暫時無法使用桌況/加入。"}), 200

    tables = []
    for r in list_open_lobby_tables(limit=200):
        current = request_participant_sum(int(r["req_id"]))
        missing = max(0, TABLE_SIZE - current)
        status = r["status"]
        status_text = "等待中" if status == "waiting" else ("確認中" if status == "confirming" else status)
        tables.append({
            "req_id": int(r["req_id"]),
            "table_no": display_table_no(r),
            "room_name": (r["room_name"] or "").strip(),
            "time_text": display_time(r),
            "amount": r["amount"] or "-",
            "rounds": r["rounds"] or "-",
            "speed": r["speed"] or "-",
            "remark": r["remark"] or "",
            "count_text": f"{current}/{TABLE_SIZE} 缺{missing}",
            "status_text": status_text,
        })
    return jsonify({"ok": True, "tables": tables}), 200

@app.route("/api/table_status", methods=["GET"])
def api_table_status():
    process_expired_confirmations()

    req_id = (request.args.get("req_id") or "").strip()
    user_id = (request.args.get("user_id") or "").strip()
    if not req_id.isdigit() or not user_id:
        return jsonify({"ok": False, "message": "bad params"}), 400

    if not shop_is_open():
        return jsonify({"ok": False, "message": "目前未有店家上線（休息中）"}), 200

    get_or_create_user(user_id, display_name="")
    if must_bind_phone(user_id):
        return jsonify({"ok": False, "message": "請先回到 LINE 對話綁定手機後再使用。"}), 200

    req = get_request(int(req_id))
    if not req:
        return jsonify({"ok": False, "message": "找不到此桌"}), 200

    current = request_participant_sum(int(req_id))
    missing = max(0, TABLE_SIZE - current)
    status = req["status"]
    status_text = "等待中" if status == "waiting" else ("確認中" if status == "confirming" else status)

    table = {
        "req_id": int(req["req_id"]),
        "table_no": display_table_no(req),
        "room_name": (req["room_name"] or "").strip(),
        "time_text": display_time(req),
        "amount": req["amount"] or "-",
        "rounds": req["rounds"] or "-",
        "speed": req["speed"] or "-",
        "remark": req["remark"] or "",
        "count_text": f"{current}/{TABLE_SIZE} 缺{missing}",
        "status_text": status_text,
    }
    return jsonify({"ok": True, "table": table}), 200

@app.route("/api/join", methods=["POST"])
def api_join():
    process_expired_confirmations()

    data = request.get_json(silent=True) or {}
    req_id = data.get("req_id")
    user_id = (data.get("user_id") or "").strip()
    party_size = int(data.get("party_size") or 1)

    if not str(req_id).isdigit() or not user_id:
        return jsonify({"ok": False, "message": "參數錯誤"}), 400

    if not shop_is_open():
        return jsonify({"ok": False, "message": "目前未有店家上線（休息中）"}), 200

    get_or_create_user(user_id, display_name="")
    if must_bind_phone(user_id):
        return jsonify({"ok": False, "message": "請先回到 LINE 對話綁定手機後再加入。"}), 200
    if is_frozen(user_id):
        return jsonify({"ok": False, "message": "你的帳號目前凍結，暫時無法加入。"}), 200

    ok, msg, to_confirm = join_open_request(int(req_id), user_id, party_size=party_size)
    return jsonify({"ok": ok, "message": msg, "to_confirm": to_confirm}), 200

@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    process_expired_confirmations()

    data = request.get_json(silent=True) or {}
    req_id = data.get("req_id")
    user_id = (data.get("user_id") or "").strip()

    if not str(req_id).isdigit() or not user_id:
        return jsonify({"ok": False, "message": "參數錯誤"}), 400

    if not shop_is_open():
        return jsonify({"ok": False, "message": "目前未有店家上線（休息中）"}), 200

    get_or_create_user(user_id, display_name="")
    if must_bind_phone(user_id):
        return jsonify({"ok": False, "message": "請先回到 LINE 對話綁定手機。"}), 200

    ok, msg, filled = confirm_join(int(req_id), user_id)
    return jsonify({"ok": ok, "message": msg, "filled": filled}), 200

@app.route("/api/giveup", methods=["POST"])
def api_giveup():
    process_expired_confirmations()

    data = request.get_json(silent=True) or {}
    req_id = data.get("req_id")
    user_id = (data.get("user_id") or "").strip()

    if not str(req_id).isdigit() or not user_id:
        return jsonify({"ok": False, "message": "參數錯誤"}), 400

    get_or_create_user(user_id, display_name="")
    ok, msg = give_up(int(req_id), user_id)
    return jsonify({"ok": ok, "message": msg}), 200

# ----------------------------
# LINE webhook handler
# ----------------------------
@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    try:
        process_expired_confirmations()
    except Exception as e:
        print("⚠️ [process_expired_confirmations] error:", e)


    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    # profile
    try:
        profile = line_bot_api.get_profile(user_id)
        display_name = profile.display_name or ""
    except Exception:
        display_name = ""
    get_or_create_user(user_id, display_name=display_name)

    # 輸入ID => 顯示 userId
    if text.upper() == "ID" or text in ("輸入ID", "輸入Id", "輸入id"):
        reply_main(event.reply_token, user_id, f"你的 userId：\n{user_id}")
        return

    state, data = get_state(user_id)

    if text in ("主選單", "選單"):
        clear_state(user_id)
        reply_menu(event.reply_token, user_id)
        return

    # 強制綁手機
    if must_bind_phone(user_id):
        if state != "BIND_PHONE":
            upsert_state(user_id, "BIND_PHONE", {})
            reply_main(event.reply_token, user_id, "⚠️ 請先綁定手機號才能使用本系統。\n請輸入手機號（09xxxxxxxx）：")
            return
        if PHONE_RE.match(text):
            set_user_phone(user_id, text)
            clear_state(user_id)
            reply_main(event.reply_token, user_id, "✅ 綁定完成")
            return
        reply_main(event.reply_token, user_id, "⚠️ 手機格式不正確，請輸入 09xxxxxxxx：")
        return

    # ---- state machine
    if state == "EDIT_NICKNAME":
        if len(text) < 1 or len(text) > 20:
            reply_custom(event.reply_token, "暱稱長度需 1~20 字，請重新輸入：", my_qr())
            return
        set_user_nickname_manual(user_id, text)
        clear_state(user_id)
        reply_custom(event.reply_token, "✅ 暱稱更新完成", my_qr())
        return

    if state == "EDIT_PHONE":
        if PHONE_RE.match(text):
            set_user_phone(user_id, text)
            clear_state(user_id)
            reply_custom(event.reply_token, "✅ 手機更新完成", my_qr())
            return
        reply_custom(event.reply_token, "⚠️ 手機格式不正確，請輸入 09xxxxxxxx：", my_qr())
        return

    if state == "SET_SHOP_NAME":
        if not is_shop_admin(user_id):
            clear_state(user_id)
            reply_sub(event.reply_token, "您不是店家管理員。")
            return
        name = text.strip()
        if len(name) < 1 or len(name) > 30:
            reply_custom(event.reply_token, "店名需 1~30 字，請重新輸入：", shop_backend_qr())
            return
        update_shop_field("name", name)
        clear_state(user_id)
        reply_custom(event.reply_token, "✅ 店名設定完成", shop_backend_qr())
        return

    # 開桌/配桌共同：手速->人數->金額->將數
    if state == "FLOW_SPEED":
        if text not in MATCH_SPEEDS:
            reply_custom(event.reply_token, "請用按鍵選擇手速。", speed_qr())
            return
        data["speed"] = text
        upsert_state(user_id, "FLOW_PARTY", data)
        reply_custom(event.reply_token, "請選擇人數", party_qr())
        return

    if state == "FLOW_PARTY":
        if text not in MATCH_PARTY_SIZES:
            reply_custom(event.reply_token, "請用按鍵選擇人數。", party_qr())
            return
        data["party_size"] = int(text.replace("我", "").replace("人", "").strip())
        upsert_state(user_id, "FLOW_AMOUNT", data)
        reply_custom(event.reply_token, "請選擇金額", amount_qr())
        return

    if state == "FLOW_AMOUNT":
        if text not in MATCH_AMOUNTS:
            reply_custom(event.reply_token, "請用按鍵選擇金額。", amount_qr())
            return
        data["amount"] = text
        upsert_state(user_id, "FLOW_ROUNDS", data)
        reply_custom(event.reply_token, "請選擇將數", rounds_qr())
        return

    if state == "FLOW_ROUNDS":
        if text not in MATCH_ROUNDS:
            reply_custom(event.reply_token, "請用按鍵選擇將數。", rounds_qr())
            return
        data["rounds"] = text

        # ✅ 配桌：進隱藏池 + 立即嘗試自動匹配
        if data.get("req_type") == "match":
            add_to_pool(
                user_id=user_id,
                speed=data.get("speed", "不限"),
                amount=data.get("amount", "50/20"),
                rounds=data.get("rounds", "2將"),
                party_size=int(data.get("party_size", 1)),
            )
            clear_state(user_id)
            # 立刻嘗試配對
            auto_match_pool_user(user_id)

            # 若已配到桌（被移除 pool 代表成功加入桌）
            if not user_in_pool(user_id):
                reply_main(event.reply_token, user_id, "✅ 已找到符合的『現在開桌』，系統已幫你自動加入。\n若人數滿會進入確認階段（30秒內需確認）。")
            else:
                reply_main(event.reply_token, user_id, "✅ 已加入隱藏配桌等待池\n系統只會自動加入「時間=現在、且無備註」的開桌\n你可隨時按「取消」退出。")
            return

        # ✅ 開桌：接著選時間
        upsert_state(user_id, "OPEN_TIME", data)
        reply_custom(event.reply_token, "開桌時間：請選擇", time_qr())
        return

    # 開桌：選時間
    if state == "OPEN_TIME":
        if text not in TIME_MODE_OPTIONS:
            reply_custom(event.reply_token, "請用按鍵選擇時間。", time_qr())
            return
        mode = TIME_MODE_MAP.get(text, "")
        data["time_mode"] = mode
        data["time_period"] = ""
        data["time_exact"] = ""
        if mode == "PERIOD":
            data["time_period"] = text  # 早/中/晚/半夜
            upsert_state(user_id, "OPEN_ROOM", data)
            reply_custom(event.reply_token, "請輸入房名（可略過）：", skip_qr())
            return
        if mode == "NOW":
            upsert_state(user_id, "OPEN_ROOM", data)
            reply_custom(event.reply_token, "請輸入房名（可略過）：", skip_qr())
            return
        if mode == "EXACT":
            upsert_state(user_id, "OPEN_TIME_EXACT", data)
            reply_custom(event.reply_token, "請輸入精確時間（HH:MM，例如 21:30）：", skip_qr())
            return

    if state == "OPEN_TIME_EXACT":
        if text == "略過":
            # 若略過精確時間，視為不合法，改回選單
            clear_state(user_id)
            reply_main(event.reply_token, user_id, "⚠️ 精確時間不可略過，請重新開桌。")
            return
        if not TIME_RE.match(text):
            reply_custom(event.reply_token, "⚠️ 格式錯誤，請輸入 HH:MM（例如 21:30）：", skip_qr())
            return
        data["time_exact"] = text
        upsert_state(user_id, "OPEN_ROOM", data)
        reply_custom(event.reply_token, "請輸入房名（可略過）：", skip_qr())
        return

    # 開桌：房名
    if state == "OPEN_ROOM":
        room = "" if text == "略過" else text.strip()
        if room and len(room) > 20:
            reply_custom(event.reply_token, "房名最多 20 字，請重新輸入或略過：", skip_qr())
            return
        data["room_name"] = room

        # 備註（可略過）
        upsert_state(user_id, "OPEN_REMARK", data)
        reply_custom(event.reply_token, "開桌備註（可略過）：", skip_qr())
        return

    # 開桌：備註 -> 建桌 -> (若符合 NOW+空備註) 立刻用 pool 自動補人
    if state == "OPEN_REMARK":
        remark = "" if text == "略過" else text.strip()
        data["remark"] = remark
        req_id = create_open_request(
            creator_user_id=user_id,
            speed=data.get("speed", "不限"),
            party_size=int(data.get("party_size", 1)),
            amount=data.get("amount", "50/20"),
            rounds=data.get("rounds", "2將"),
            room_name=data.get("room_name", ""),
            time_mode=data.get("time_mode", "NOW"),
            time_period=data.get("time_period", ""),
            time_exact=data.get("time_exact", ""),
            remark=remark,
        )
        req = get_request(req_id)
        clear_state(user_id)

        # ✅ 建桌後嘗試 auto fill（只對 NOW + 空備註 生效）
        auto_fill_from_pool(req_id)

        room = (req["room_name"] or "").strip()
        room_txt = f"｜房名：{room}" if room else ""
        reply_main(
            event.reply_token, user_id,
            f"✅ 開桌成功\n"
            f"桌號：{display_table_no(req)}{room_txt}\n"
            f"時間：{display_time(req)}\n"
            f"手速：{req['speed']}｜金額：{req['amount']}｜將數：{req['rounds']}\n"
            f"備註：{remark if remark else '無'}\n\n"
            f"桌況查詢請點「📋 桌況查詢」（LIFF）"
        )
        return

    if state == "SET_SHOP_LINK":
        if not is_shop_admin(user_id):
            clear_state(user_id)
            reply_sub(event.reply_token, "您不是店家管理員。")
            return
        field = (data.get("field") or "").strip()
        label = (data.get("label") or "").strip()
        if not (text.startswith("http://") or text.startswith("https://")):
            reply_custom(event.reply_token, "⚠️ 請貼上有效連結（需 http/https）：", shop_backend_qr())
            return
        update_shop_field(field, text)
        clear_state(user_id)
        reply_custom(event.reply_token, f"✅ {label} 設定完成", shop_backend_qr())
        return

    if state == "REMOVE_ADMIN":
        if not is_shop_admin(user_id):
            clear_state(user_id)
            reply_sub(event.reply_token, "您不是店家管理員。")
            return
        target_id = text.strip()
        if not target_id:
            reply_custom(event.reply_token, "請貼上要移除的 userId：", shop_backend_qr())
            return
        ok = remove_shop_admin(target_id)
        clear_state(user_id)
        reply_custom(event.reply_token, "✅ 已移除管理員" if ok else "⚠️ 無法移除（可能是 owner）", shop_backend_qr())
        return

    if state == "REDEEM_ADMIN_CODE":
        if not text.isdigit() or len(text) != 6:
            reply_custom(event.reply_token, "請輸入 6 位數驗證碼：", shop_backend_qr())
            return
        ok, msg = redeem_invite_code(text, user_id)
        clear_state(user_id)
        reply_custom(event.reply_token, msg, shop_backend_qr())
        return

    if state == "CUSTOMER_SEARCH":
        rows = do_customer_search(text)
        if not rows:
            reply_custom(event.reply_token, "找不到符合的用戶（僅查已綁手機者）。", customer_info_qr())
            return
        if len(rows) == 1:
            clear_state(user_id)
            show_customer_detail(event.reply_token, user_id, rows[0]["user_id"])
            return

        mapping = []
        lines = ["查詢結果（回覆「選擇1/2/3...」）："]
        for i, r in enumerate(rows, start=1):
            phone = (r["phone"] or "")
            if not phone:
                continue
            lines.append(f"{i}) {r['nickname'] or '-'}｜{phone}｜信用{int(r['credit'] or 0)}｜{'凍結' if int(r['frozen'] or 0)==1 else '正常'}")
            mapping.append(r["user_id"])
        upsert_state(user_id, "CUSTOMER_PICK", {"candidates": mapping})
        reply_custom(event.reply_token, "\n".join(lines), customer_info_qr())
        return

    if state == "CUSTOMER_PICK":
        m = re.match(r"^選擇(\d+)$", text)
        candidates = data.get("candidates") or []
        if not m:
            reply_custom(event.reply_token, "請回覆格式：選擇1 / 選擇2 / ...", customer_info_qr())
            return
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(candidates):
            reply_custom(event.reply_token, "編號錯誤，請重新選擇。", customer_info_qr())
            return
        clear_state(user_id)
        show_customer_detail(event.reply_token, user_id, candidates[idx])
        return

    if state == "CUSTOMER_DEDUCT":
        target_user_id = (data.get("target_user_id") or "").strip()
        if not target_user_id:
            clear_state(user_id)
            reply_custom(event.reply_token, "目標用戶遺失，請重新查詢。", customer_info_qr())
            return
        if text.startswith("扣分 "):
            reason = text.replace("扣分 ", "", 1).strip()
            delta = None
            for name, d in DEDUCTION_OPTIONS:
                if name == reason:
                    delta = d
                    break
            if delta is None:
                reply_custom(event.reply_token, "扣分項目錯誤，請用按鍵操作。", deduction_qr())
                return
            ok, msg = apply_deduction(target_user_id, delta, reason, by_user_id=user_id)
            reply_custom(event.reply_token, msg, deduction_qr())
            return
        reply_custom(event.reply_token, "請用按鍵選擇扣分項目。", deduction_qr())
        return

    # ---- menu routing
    if text == "我的":
        u = get_user(user_id)
        active_open = find_active_open_request_for_user(user_id)
        pool = user_in_pool(user_id)
        status_txt = "無進行中"
        if active_open:
            status_txt = f"桌內（桌號 {display_table_no(active_open)}｜{active_open['status']})"
        elif pool:
            status_txt = "配桌等待池中（隱藏）"
        msg = (
            f"👤 我的資料\n"
            f"暱稱：{u['nickname'] or '-'}\n"
            f"手機：{u['phone'] or '-'}\n"
            f"狀態：{status_txt}\n"
            f"信用分數：{int(u['credit'] or 0)}\n"
            f"帳號狀態：{'凍結' if int(u['frozen'] or 0)==1 else '正常'}"
        )
        reply_custom(event.reply_token, msg, my_qr())
        return

    if text == "修改暱稱":
        upsert_state(user_id, "EDIT_NICKNAME", {})
        reply_custom(event.reply_token, "請輸入新的暱稱（1~20字）：", my_qr())
        return

    if text == "修改手機":
        upsert_state(user_id, "EDIT_PHONE", {})
        reply_custom(event.reply_token, "請輸入新的手機號（09xxxxxxxx）：", my_qr())
        return

    if text == "聯絡店家":
        shop = get_shop()
        has_any = False
        if shop:
            if (shop["shop_line_link"] or "").strip():
                has_any = True
            if (shop["map_link"] or "").strip():
                has_any = True
        if (not shop_is_open()) or (not has_any):
            reply_main(event.reply_token, user_id, "⚠️ 目前未有店家上線（尚未設定聯絡方式或休息中）")
            return
        reply_custom(event.reply_token, "☎️ 聯絡店家：", contact_shop_qr())
        return

    if text == "開桌":
        if not ensure_shop_open_or_message(event.reply_token, user_id):
            return
        if not ensure_not_frozen_or_message(event.reply_token, user_id):
            return
        # 避免重複：桌內或在pool都要擋
        if find_active_open_request_for_user(user_id) or user_in_pool(user_id):
            reply_custom(event.reply_token, "你目前已有進行中的開桌/配桌，請先取消。", active_block_qr())
            return
        upsert_state(user_id, "FLOW_SPEED", {"req_type": "open"})
        reply_custom(event.reply_token, "開桌：請選擇手速", speed_qr())
        return

    if text == "配桌":
        if not ensure_shop_open_or_message(event.reply_token, user_id):
            return
        if not ensure_not_frozen_or_message(event.reply_token, user_id):
            return
        if find_active_open_request_for_user(user_id):
            reply_custom(event.reply_token, "你目前已在某桌中，請先取消/退出該桌。", active_block_qr())
            return
        if user_in_pool(user_id):
            reply_custom(event.reply_token, "你目前已在配桌等待池中。\n如要退出請按「取消」。", active_block_qr())
            return
        upsert_state(user_id, "FLOW_SPEED", {"req_type": "match"})
        reply_custom(event.reply_token, "配桌：請選擇手速", speed_qr())
        return

    if text == "取消":
        ok, msg = cancel_all_for_user(user_id)
        reply_main(event.reply_token, user_id, f"{'✅' if ok else '⚠️'} {msg}")
        return

    if text == "店家後台":
        handle_shop_backend(event.reply_token, user_id)
        return

    if text == "店名設定":
        prompt_set_shop_name(event.reply_token, user_id)
        return

    if text == "群設定":
        prompt_set_link(event.reply_token, user_id, field="group_link", label="群連結")
        return
    if text == "地圖設定":
        prompt_set_link(event.reply_token, user_id, field="map_link", label="地圖")
        return
    if text == "店家LINE設定":
        prompt_set_link(event.reply_token, user_id, field="shop_line_link", label="店家LINE")
        return
    if text == "營業/休息":
        toggle_open(event.reply_token, user_id)
        return
    if text == "新增管理員":
        generate_admin_code(event.reply_token, user_id)
        return
    if text == "輸入6位碼":
        if not is_shop_admin(user_id):
            reply_sub(event.reply_token, "您不是店家管理員。")
            return
        upsert_state(user_id, "REDEEM_ADMIN_CODE", {})
        reply_custom(event.reply_token, "請輸入 6 位數驗證碼：", shop_backend_qr())
        return
    if text == "管理員名單":
        handle_admin_list(event.reply_token, user_id)
        return
    if text == "移除管理員":
        prompt_remove_admin(event.reply_token, user_id)
        return

    if text == "客戶資訊":
        handle_customer_info(event.reply_token, user_id)
        return
    if text == "客戶查詢":
        start_customer_search(event.reply_token, user_id)
        return
    if text == "導出用戶":
        if not is_shop_admin(user_id):
            reply_sub(event.reply_token, "您不是店家管理員。")
            return
        ok, msg = create_export_file(created_by=user_id)
        reply_custom(event.reply_token, msg, customer_info_qr())
        return

    reply_menu(event.reply_token, user_id)

# ----------------------------
# Boot
# ----------------------------
init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
