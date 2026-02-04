# app.py
# Mahjong Table System (Python/Flask + LINE OA) - Render deploy-ready
#
# ✅ Features (per your latest requirements)
# 1) 主選單 Quick Reply：開桌、配桌、桌況查詢(缺1/缺2/缺3)、我的、聯絡店家
#    （店家身分再多：店家後台、客戶資訊）
# 2) 聯絡店家：只有 店家LINE / 地圖（URI 連結）
# 3) 取消所有「返回」按鍵；主選單不需要回主選單按鍵；其他頁面可有「主選單」
# 4) 強制綁定手機：未綁定 → 只能先綁手機（成功回「綁定完成」）
#    我的頁：顯示暱稱/手機/配桌狀態/信用分數，且可修改暱稱/手機、查信用分
# 5) 店家後台：群設定/地圖設定/店家LINE設定、營業/休息、6位碼新增管理員
#    - 6位碼：自動產出、一次性、10分鐘有效、管理員上限5位、名單/移除
# 6) 開桌/配桌分開
#    - 配桌：快手/慢手/不限；人數：我1/2/3；金額：50/20 100/20 100/50 200/50；將數：2將/3將
#    - 備註：可略過（沒輸入可跳過）
# 7) 桌況查詢：缺1/缺2/缺3 → Flex 卡片，按「用LIFF加入」即可加入（預設加入1人）
#
# Required ENV on Render:
#   CHANNEL_ACCESS_TOKEN
#   CHANNEL_SECRET
# Optional ENV:
#   DATABASE_PATH (default: /tmp/mahjong.db)
#   LIFF_ID (default: 2009050373-HHA8grO4)
#   BASE_URL (default: https://mahjong-line-bot.onrender.com)

import os
import re
import json
import sqlite3
import random
from datetime import datetime, timedelta

from flask import Flask, request, abort, jsonify, render_template_string

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage,
    TextSendMessage,
    QuickReply, QuickReplyButton,
    MessageAction, URIAction,
    FlexSendMessage
)

# ----------------------------
# Config
# ----------------------------
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()

DATABASE_PATH = os.getenv("DATABASE_PATH", "/tmp/mahjong.db")
LIFF_ID = os.getenv("LIFF_ID", "2009050373-HHA8grO4").strip()
BASE_URL = os.getenv("BASE_URL", "https://mahjong-line-bot.onrender.com").strip().rstrip("/")

TABLE_SIZE = 4

MATCH_SPEEDS = ["快手", "慢手", "不限"]
MATCH_PARTY_SIZES = ["我1人", "我2人", "我3人"]
MATCH_AMOUNTS = ["50/20", "100/20", "100/50", "200/50"]
MATCH_ROUNDS = ["2將", "3將"]

PHONE_RE = re.compile(r"^09\d{8}$")

ADMIN_MAX_COUNT = 5
ADMIN_CODE_EXPIRE_MIN = 10

# Credit (kept for "我的" 查詢)
CREDIT_FREEZE_THRESHOLD = 60

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ----------------------------
# Time helpers
# ----------------------------
def iso_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def dt_to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def iso_to_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")

# ----------------------------
# DB helpers
# ----------------------------
def db_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        nickname TEXT,
        phone TEXT,
        role TEXT DEFAULT 'customer',   -- customer / shop_admin / shop_owner
        credit INTEGER DEFAULT 100,
        frozen INTEGER DEFAULT 0,
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
    CREATE TABLE IF NOT EXISTS match_requests (
        req_id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_user_id TEXT,
        req_type TEXT,               -- open / match
        speed TEXT,
        party_size INTEGER,
        amount TEXT,
        rounds TEXT,
        remark TEXT,
        status TEXT DEFAULT 'waiting', -- waiting/filled/cancelled
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS request_participants (
        req_id INTEGER,
        user_id TEXT,
        party_size INTEGER,
        joined_at TEXT,
        PRIMARY KEY (req_id, user_id)
    )
    """)

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

def get_or_create_user(user_id: str, nickname: str = ""):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (user_id, nickname, created_at) VALUES (?, ?, ?)",
            (user_id, nickname or "", iso_now())
        )
        conn.commit()
    else:
        if nickname is not None and nickname != row["nickname"]:
            cur.execute("UPDATE users SET nickname=? WHERE user_id=?", (nickname, user_id))
            conn.commit()
    conn.close()

def get_user(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def set_user_phone(user_id: str, phone: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET phone=? WHERE user_id=?", (phone, user_id))
    conn.commit()
    conn.close()

def set_user_nickname(user_id: str, nickname: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET nickname=? WHERE user_id=?", (nickname, user_id))
    conn.commit()
    conn.close()

def set_user_role(user_id: str, role: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
    conn.commit()
    conn.close()

def is_shop_admin(user_id: str) -> bool:
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

def list_shop_admin_user_ids():
    shop = get_shop()
    if not shop:
        return []
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM shop_admins WHERE shop_id=? ORDER BY used_by", (shop["shop_id"],))
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

def add_shop_admin(user_id: str) -> (bool, str):
    shop = get_shop()
    if not shop:
        return False, "店家資料不存在。"
    if count_shop_admins() >= ADMIN_MAX_COUNT:
        return False, f"⚠️ 管理員已達上限（{ADMIN_MAX_COUNT}位），請先移除後再新增。"

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO shop_admins (shop_id, user_id) VALUES (?, ?)", (shop["shop_id"], user_id))
    conn.commit()
    conn.close()
    set_user_role(user_id, "shop_admin")
    return True, "已新增為管理員。"

def remove_shop_admin(user_id: str) -> bool:
    shop = get_shop()
    if not shop:
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

def clear_state(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ----------------------------
# Match request helpers
# ----------------------------
def create_request(creator_user_id: str, req_type: str, speed: str, party_size: int, amount: str, rounds: str, remark: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO match_requests (creator_user_id, req_type, speed, party_size, amount, rounds, remark, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (creator_user_id, req_type, speed, int(party_size), amount, rounds, remark or "", iso_now()))
    req_id = cur.lastrowid
    cur.execute("""
    INSERT OR IGNORE INTO request_participants (req_id, user_id, party_size, joined_at)
    VALUES (?, ?, ?, ?)
    """, (req_id, creator_user_id, int(party_size), iso_now()))
    conn.commit()
    conn.close()
    mark_filled_if_needed(req_id)
    return req_id

def get_request(req_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM match_requests WHERE req_id=?", (int(req_id),))
    row = cur.fetchone()
    conn.close()
    return row

def participant_sum(req_id: int) -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(party_size),0) AS s FROM request_participants WHERE req_id=?", (int(req_id),))
    row = cur.fetchone()
    conn.close()
    return int(row["s"] if row else 0)

def mark_filled_if_needed(req_id: int):
    current = participant_sum(req_id)
    if current >= TABLE_SIZE:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE match_requests SET status='filled' WHERE req_id=?", (int(req_id),))
        conn.commit()
        conn.close()

def join_request(req_id: int, user_id: str, party_size: int = 1) -> (bool, str):
    req = get_request(req_id)
    if not req:
        return False, "找不到此桌。"
    if req["status"] != "waiting":
        return False, "此桌已結束或無法加入。"

    current = participant_sum(req_id)
    missing = TABLE_SIZE - current
    if missing <= 0:
        mark_filled_if_needed(req_id)
        return False, "此桌已滿。"
    if int(party_size) > missing:
        return False, f"目前只缺 {missing} 人，加入失敗。"

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO request_participants (req_id, user_id, party_size, joined_at)
    VALUES (?, ?, ?, ?)
    """, (int(req_id), user_id, int(party_size), iso_now()))
    conn.commit()
    conn.close()

    mark_filled_if_needed(req_id)
    return True, "加入成功。"

def list_waiting_requests_by_need(need: int, limit: int = 8):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM match_requests
    WHERE status='waiting'
    ORDER BY req_id DESC
    LIMIT ?
    """, (limit * 8,))
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        current = participant_sum(r["req_id"])
        missing = TABLE_SIZE - current
        if missing == int(need):
            result.append((r, current, missing))
        if len(result) >= limit:
            break
    return result

def get_my_latest_status_text(user_id: str) -> str:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT req_id, status FROM match_requests
    WHERE creator_user_id=?
    ORDER BY req_id DESC LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return "無進行中"
    if row["status"] == "waiting":
        return f"等待中（桌號 #{row['req_id']}）"
    if row["status"] == "filled":
        return f"已成桌（桌號 #{row['req_id']}）"
    if row["status"] == "cancelled":
        return f"已取消（桌號 #{row['req_id']}）"
    return "無進行中"

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
        return False, f"⚠️ 管理員已達上限（{ADMIN_MAX_COUNT}位），請先移除後再新增。"

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
        return True, f"✅ 6位數驗證碼：{code}\n有效期限：{ADMIN_CODE_EXPIRE_MIN} 分鐘\n（僅能使用一次）"

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

    # Expire check
    try:
        exp = iso_to_dt(row["expire_at"])
    except Exception:
        exp = datetime.utcnow() - timedelta(days=1)
    if exp < datetime.utcnow():
        cur.execute("DELETE FROM admin_invite_codes WHERE code=?", (code,))
        conn.commit()
        conn.close()
        return False, "此 6 位碼已過期。"

    # Admin limit check
    if count_shop_admins() >= ADMIN_MAX_COUNT:
        conn.close()
        return False, f"⚠️ 管理員已達上限（{ADMIN_MAX_COUNT}位），請先移除後再新增。"

    # Mark used
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
# Quick Reply builders
# ----------------------------
def qr(actions):
    return QuickReply(items=[QuickReplyButton(action=a) for a in actions])

def main_menu_qr(user_id: str):
    actions = [
        MessageAction(label="開桌", text="開桌"),
        MessageAction(label="配桌", text="配桌"),
        MessageAction(label="桌況查詢", text="桌況查詢"),
        MessageAction(label="我的", text="我的"),
        MessageAction(label="聯絡店家", text="聯絡店家"),
    ]
    if is_shop_admin(user_id):
        actions.append(MessageAction(label="店家後台", text="店家後台"))
        actions.append(MessageAction(label="客戶資訊", text="客戶資訊"))
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
    if not actions:
        actions = [MessageAction(label="主選單", text="主選單")]
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

def remark_qr():
    return qr([
        MessageAction(label="略過", text="略過"),
        MessageAction(label="主選單", text="主選單"),
    ])

def table_query_qr():
    return qr([
        MessageAction(label="缺1", text="缺1"),
        MessageAction(label="缺2", text="缺2"),
        MessageAction(label="缺3", text="缺3"),
        MessageAction(label="主選單", text="主選單"),
    ])

def my_qr():
    return qr([
        MessageAction(label="修改暱稱", text="修改暱稱"),
        MessageAction(label="修改手機", text="修改手機"),
        MessageAction(label="查信用分", text="查信用分"),
        MessageAction(label="主選單", text="主選單"),
    ])

def shop_backend_qr():
    return qr([
        MessageAction(label="群設定", text="群設定"),
        MessageAction(label="地圖設定", text="地圖設定"),
        MessageAction(label="店家LINE設定", text="店家LINE設定"),
        MessageAction(label="營業/休息", text="營業/休息"),
        MessageAction(label="新增管理員(6位碼)", text="新增管理員"),
        MessageAction(label="管理員名單", text="管理員名單"),
        MessageAction(label="移除管理員", text="移除管理員"),
        MessageAction(label="主選單", text="主選單"),
    ])

# ----------------------------
# Flex: table list with LIFF join
# ----------------------------
def flex_table_list(need: int):
    items = list_waiting_requests_by_need(need, limit=8)
    if not items:
        return None

    bubbles = []
    for r, current, missing in items:
        req_id = int(r["req_id"])
        join_uri = f"https://liff.line.me/{LIFF_ID}?action=join&req_id={req_id}"

        summary = f"缺{missing}｜{r['speed']}｜{r['amount']}｜{r['rounds']}"
        remark = (r["remark"] or "").strip()
        remark_text = f"備註：{remark}" if remark else "備註：無"

        bubbles.append({
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": f"桌號 #{req_id}", "weight": "bold", "size": "lg"},
                    {"type": "text", "text": summary, "size": "md", "wrap": True},
                    {"type": "text", "text": f"目前：{current}/{TABLE_SIZE}", "size": "sm", "color": "#666666"},
                    {"type": "text", "text": remark_text, "size": "sm", "wrap": True, "color": "#666666"},
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {"type": "uri", "label": "用LIFF加入", "uri": join_uri}
                    }
                ]
            }
        })

    payload = {"type": "carousel", "contents": bubbles}
    return FlexSendMessage(alt_text=f"桌況查詢 缺{need}", contents=payload)

# ----------------------------
# Core: phone binding enforcement
# ----------------------------
def must_bind_phone(user_id: str) -> bool:
    u = get_user(user_id)
    return (not u) or (not (u["phone"] or "").strip())

def start_bind_phone(reply_token: str, user_id: str):
    upsert_state(user_id, "BIND_PHONE", {})
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(
            text="⚠️ 請先綁定手機號才能使用本系統。\n請輸入手機號（09xxxxxxxx）："
        )
    )

# ----------------------------
# Menu actions
# ----------------------------
def show_main_menu(reply_token: str, user_id: str):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="主選單：", quick_reply=main_menu_qr(user_id))
    )

def handle_contact(reply_token: str):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="聯絡店家：", quick_reply=contact_shop_qr())
    )

def handle_table_query(reply_token: str):
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="桌況查詢：請選擇缺人數（點卡片可用LIFF加入）", quick_reply=table_query_qr())
    )

def handle_my(reply_token: str, user_id: str):
    u = get_user(user_id)
    if not u:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="系統忙碌，請稍後再試。", quick_reply=sub_menu_qr()))
        return

    msg = (
        f"我的資料：\n"
        f"暱稱：{u['nickname'] or '-'}\n"
        f"手機：{u['phone'] or '-'}\n"
        f"配桌狀態：{get_my_latest_status_text(user_id)}\n"
        f"信用分數：{int(u['credit'] or 0)}\n"
        f"狀態：{'凍結' if int(u['frozen'] or 0)==1 else '正常'}"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=my_qr()))

def handle_credit(reply_token: str, user_id: str):
    u = get_user(user_id)
    if not u:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="查詢失敗，請稍後再試。", quick_reply=sub_menu_qr()))
        return
    msg = f"您的信用分數：{int(u['credit'] or 0)}\n狀態：{'凍結' if int(u['frozen'] or 0)==1 else '正常'}"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=my_qr()))

# ----------------------------
# Flow: open/match
# ----------------------------
def start_flow(reply_token: str, user_id: str, req_type: str):
    # frozen users could be blocked; keep simple (still allow, but you can block if you want)
    upsert_state(user_id, "FLOW_SPEED", {"req_type": req_type})
    title = "開桌" if req_type == "open" else "配桌"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=f"{title}：請選擇節奏", quick_reply=speed_qr()))

def ask_party(reply_token: str, user_id: str, data: dict):
    upsert_state(user_id, "FLOW_PARTY", data)
    line_bot_api.reply_message(reply_token, TextSendMessage(text="請選擇人數", quick_reply=party_qr()))

def ask_amount(reply_token: str, user_id: str, data: dict):
    upsert_state(user_id, "FLOW_AMOUNT", data)
    line_bot_api.reply_message(reply_token, TextSendMessage(text="請選擇金額", quick_reply=amount_qr()))

def ask_rounds(reply_token: str, user_id: str, data: dict):
    upsert_state(user_id, "FLOW_ROUNDS", data)
    line_bot_api.reply_message(reply_token, TextSendMessage(text="請選擇將數", quick_reply=rounds_qr()))

def ask_remark(reply_token: str, user_id: str, data: dict):
    upsert_state(user_id, "FLOW_REMARK", data)
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="開桌備註（可略過）：\n直接輸入文字，或點「略過」", quick_reply=remark_qr())
    )

def finalize_request(reply_token: str, user_id: str, data: dict, remark: str):
    req_type = data.get("req_type", "match")
    speed = data.get("speed", "不限")
    party_size = int(data.get("party_size", 1))
    amount = data.get("amount", "50/20")
    rounds = data.get("rounds", "2將")
    req_id = create_request(user_id, req_type, speed, party_size, amount, rounds, remark or "")
    clear_state(user_id)

    title = "開桌成功" if req_type == "open" else "配桌成功"
    msg = (
        f"🎉 {title}\n"
        f"桌號：#{req_id}\n"
        f"節奏：{speed}\n"
        f"人數：{party_size}\n"
        f"金額：{amount}\n"
        f"將數：{rounds}\n"
        f"備註：{remark or '無'}\n\n"
        f"可到「桌況查詢」讓其他玩家用LIFF加入。"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=sub_menu_qr()))

# ----------------------------
# Shop backend handlers
# ----------------------------
def handle_shop_backend(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    shop = get_shop()
    status = "營業" if shop and int(shop["is_open"] or 0) == 1 else "休息"
    msg = (
        f"店家後台（目前：{status}）\n"
        f"- 群設定 / 地圖設定 / 店家LINE設定：貼上連結即可\n"
        f"- 新增管理員：產生 6 位碼（一次性 / {ADMIN_CODE_EXPIRE_MIN} 分鐘有效 / 上限{ADMIN_MAX_COUNT}位）\n"
        f"- 管理員名單 / 移除管理員：可查看與移除"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=shop_backend_qr()))

def handle_customer_info(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT nickname, phone, credit, frozen, role
    FROM users
    WHERE phone IS NOT NULL AND TRIM(phone)!=''
    ORDER BY created_at DESC
    LIMIT 10
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="目前沒有可顯示的客戶資料。", quick_reply=sub_menu_qr()))
        return

    lines = ["客戶資訊（最近10筆）："]
    for r in rows:
        lines.append(
            f"- {r['nickname'] or '-'}｜{r['phone']}｜信用{int(r['credit'] or 0)}｜{'凍結' if int(r['frozen'] or 0)==1 else '正常'}｜{r['role']}"
        )
    line_bot_api.reply_message(reply_token, TextSendMessage(text="\n".join(lines), quick_reply=sub_menu_qr()))

def handle_admin_list(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    ids = list_shop_admin_user_ids()
    if not ids:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="目前尚無管理員。", quick_reply=shop_backend_qr()))
        return
    msg = "管理員名單（userId）：\n" + "\n".join([f"- {x}" for x in ids])
    msg += "\n\n（如要移除：點「移除管理員」後貼上 userId）"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=shop_backend_qr()))

def prompt_remove_admin(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    upsert_state(user_id, "REMOVE_ADMIN", {})
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="請貼上要移除的管理員 userId：", quick_reply=shop_backend_qr())
    )

def prompt_set_link(reply_token: str, user_id: str, field: str, label: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    upsert_state(user_id, "SET_SHOP_LINK", {"field": field, "label": label})
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text=f"請貼上「{label}」連結：", quick_reply=shop_backend_qr())
    )

def toggle_open(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    shop = get_shop()
    cur_status = int(shop["is_open"] or 0) if shop else 1
    new_status = 0 if cur_status == 1 else 1
    update_shop_field("is_open", new_status)
    msg = f"已切換為：{'營業' if new_status==1 else '休息'}"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=shop_backend_qr()))

def generate_admin_code(reply_token: str, user_id: str):
    if not is_shop_admin(user_id):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
        return
    ok, msg = create_invite_code(user_id)
    line_bot_api.reply_message(reply_token, TextSendMessage(text=msg, quick_reply=shop_backend_qr()))

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

# LIFF: join page
LIFF_JOIN_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>LIFF Join</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;padding:18px;}
    .box{max-width:560px;margin:0 auto;}
    .btn{display:block;width:100%;padding:12px 14px;margin-top:10px;border:0;border-radius:10px;font-size:16px;}
  </style>
</head>
<body>
  <div class="box">
    <h3>加入桌位</h3>
    <div id="status">初始化中…</div>
    <button class="btn" id="btn" disabled>加入（預設 1 人）</button>
  </div>
<script>
  const LIFF_ID = "{{LIFF_ID}}";
  const reqId = "{{REQ_ID}}";
  const action = "{{ACTION}}";

  const statusEl = document.getElementById('status');
  const btn = document.getElementById('btn');

  function qs(name){
    const u = new URL(window.location.href);
    return u.searchParams.get(name);
  }

  async function init(){
    try{
      await liff.init({ liffId: LIFF_ID });
      if(!liff.isLoggedIn()){
        liff.login();
        return;
      }
      const profile = await liff.getProfile();
      statusEl.textContent = `已登入：${profile.displayName}，準備加入桌號 #${reqId}`;
      btn.disabled = false;

      btn.addEventListener('click', async ()=>{
        btn.disabled = true;
        statusEl.textContent = "加入中…";
        const res = await fetch("/api/join", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ req_id: reqId, user_id: profile.userId, party_size: 1 })
        });
        const data = await res.json();
        statusEl.textContent = data.message || "完成";
        if(data.ok){
          setTimeout(()=>{ liff.closeWindow(); }, 800);
        }else{
          btn.disabled = false;
        }
      });
    }catch(e){
      statusEl.textContent = "LIFF 初始化失敗：" + (e && e.message ? e.message : e);
    }
  }
  init();
</script>
</body>
</html>
"""

@app.route("/liff", methods=["GET"])
def liff_page():
    action = request.args.get("action", "join")
    req_id = request.args.get("req_id", "").strip()
    if not req_id.isdigit():
        return "Bad req_id", 400
    return render_template_string(LIFF_JOIN_HTML, LIFF_ID=LIFF_ID, REQ_ID=req_id, ACTION=action)

@app.route("/api/join", methods=["POST"])
def api_join():
    data = request.get_json(silent=True) or {}
    req_id = data.get("req_id")
    user_id = (data.get("user_id") or "").strip()
    party_size = int(data.get("party_size") or 1)

    if not str(req_id).isdigit() or not user_id:
        return jsonify({"ok": False, "message": "參數錯誤"}), 400

    # Ensure user exists
    get_or_create_user(user_id, nickname="")

    # Enforce phone binding for join
    if must_bind_phone(user_id):
        return jsonify({"ok": False, "message": "請先回到LINE對話綁定手機後再加入。"}), 200

    ok, msg = join_request(int(req_id), user_id, party_size=party_size)
    return jsonify({"ok": ok, "message": msg}), 200

# ----------------------------
# LINE webhook handler
# ----------------------------
@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    # Ensure user exists and keep nickname from LINE profile if available
    try:
        profile = line_bot_api.get_profile(user_id)
        nickname = profile.display_name or ""
    except Exception:
        nickname = ""
    get_or_create_user(user_id, nickname=nickname)

    # Read current state
    state, data = get_state(user_id)

    # ---------
    # Global commands
    # ---------
    if text == "主選單":
        clear_state(user_id)
        show_main_menu(event.reply_token, user_id)
        return

    # ---------
    # Forced phone binding (global gate)
    # Allow only binding-related states/actions if phone missing
    # ---------
    if must_bind_phone(user_id):
        # If user is not in binding state, start binding
        if state not in ("BIND_PHONE",):
            start_bind_phone(event.reply_token, user_id)
            return

        # Handle phone input
        if PHONE_RE.match(text):
            set_user_phone(user_id, text)
            clear_state(user_id)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="✅ 綁定完成", quick_reply=main_menu_qr(user_id))
            )
            return
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 手機格式不正確，請輸入 09xxxxxxxx：")
            )
            return

    # ---------
    # State machine handlers (after phone is bound)
    # ---------
    if state == "EDIT_NICKNAME":
        if len(text) < 1 or len(text) > 20:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="暱稱長度需 1~20 字，請重新輸入：", quick_reply=my_qr()))
            return
        set_user_nickname(user_id, text)
        clear_state(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 暱稱更新完成", quick_reply=my_qr()))
        return

    if state == "EDIT_PHONE":
        if PHONE_RE.match(text):
            set_user_phone(user_id, text)
            clear_state(user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 手機更新完成", quick_reply=my_qr()))
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 手機格式不正確，請輸入 09xxxxxxxx：", quick_reply=my_qr()))
        return

    if state == "FLOW_SPEED":
        if text not in MATCH_SPEEDS:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請用按鍵選擇節奏。", quick_reply=speed_qr()))
            return
        data["speed"] = text
        ask_party(event.reply_token, user_id, data)
        return

    if state == "FLOW_PARTY":
        if text not in MATCH_PARTY_SIZES:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請用按鍵選擇人數。", quick_reply=party_qr()))
            return
        data["party_size"] = int(text.replace("我", "").replace("人", "").strip())
        ask_amount(event.reply_token, user_id, data)
        return

    if state == "FLOW_AMOUNT":
        if text not in MATCH_AMOUNTS:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請用按鍵選擇金額。", quick_reply=amount_qr()))
            return
        data["amount"] = text
        ask_rounds(event.reply_token, user_id, data)
        return

    if state == "FLOW_ROUNDS":
        if text not in MATCH_ROUNDS:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請用按鍵選擇將數。", quick_reply=rounds_qr()))
            return
        data["rounds"] = text
        ask_remark(event.reply_token, user_id, data)
        return

    if state == "FLOW_REMARK":
        if text == "略過":
            finalize_request(event.reply_token, user_id, data, remark="")
            return
        # Any text is remark
        finalize_request(event.reply_token, user_id, data, remark=text)
        return

    if state == "SET_SHOP_LINK":
        if not is_shop_admin(user_id):
            clear_state(user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
            return
        field = (data.get("field") or "").strip()
        label = (data.get("label") or "").strip()
        # basic url check
        if not (text.startswith("http://") or text.startswith("https://")):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 請貼上有效連結（需 http/https）：", quick_reply=shop_backend_qr()))
            return
        update_shop_field(field, text)
        clear_state(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ {label} 設定完成", quick_reply=shop_backend_qr()))
        return

    if state == "REDEEM_ADMIN_CODE":
        if not text.isdigit() or len(text) != 6:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入 6 位數驗證碼：", quick_reply=sub_menu_qr()))
            return
        ok, msg = redeem_invite_code(text, user_id)
        clear_state(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg, quick_reply=main_menu_qr(user_id)))
        return

    if state == "REMOVE_ADMIN":
        if not is_shop_admin(user_id):
            clear_state(user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="您不是店家管理員。", quick_reply=sub_menu_qr()))
            return
        target_id = text.strip()
        if not target_id:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請貼上要移除的 userId：", quick_reply=shop_backend_qr()))
            return
        removed = remove_shop_admin(target_id)
        clear_state(user_id)
        if removed:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 已移除管理員（若存在）", quick_reply=shop_backend_qr()))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="移除失敗。", quick_reply=shop_backend_qr()))
        return

    # ---------
    # Normal menu routing (no active state)
    # ---------
    if text == "開桌":
        start_flow(event.reply_token, user_id, req_type="open")
        return

    if text == "配桌":
        start_flow(event.reply_token, user_id, req_type="match")
        return

    if text == "桌況查詢":
        handle_table_query(event.reply_token)
        return

    if text in ("缺1", "缺2", "缺3"):
        need = int(text.replace("缺", "").strip())
        flex = flex_table_list(need)
        if flex is None:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"目前沒有缺{need}的桌。", quick_reply=sub_menu_qr()))
        else:
            line_bot_api.reply_message(event.reply_token, flex)
        return

    if text == "我的":
        handle_my(event.reply_token, user_id)
        return

    if text == "修改暱稱":
        upsert_state(user_id, "EDIT_NICKNAME", {})
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入新的暱稱（1~20字）：", quick_reply=my_qr()))
        return

    if text == "修改手機":
        upsert_state(user_id, "EDIT_PHONE", {})
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入新的手機號（09xxxxxxxx）：", quick_reply=my_qr()))
        return

    if text == "查信用分":
        handle_credit(event.reply_token, user_id)
        return

    if text == "聯絡店家":
        handle_contact(event.reply_token)
        return

    # Shop backend
    if text == "店家後台":
        handle_shop_backend(event.reply_token, user_id)
        return

    if text == "客戶資訊":
        handle_customer_info(event.reply_token, user_id)
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

    if text == "管理員名單":
        handle_admin_list(event.reply_token, user_id)
        return

    if text == "移除管理員":
        prompt_remove_admin(event.reply_token, user_id)
        return

    # Redeem code shortcut (anyone can type "輸入6位碼" to redeem)
    if text == "輸入6位碼":
        upsert_state(user_id, "REDEEM_ADMIN_CODE", {})
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入 6 位數驗證碼：", quick_reply=sub_menu_qr()))
        return

    # Default fallback: show main menu
    show_main_menu(event.reply_token, user_id)

# ----------------------------
# Boot
# ----------------------------
init_db()

if __name__ == "__main__":
    # Local run
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
