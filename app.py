import os
import re
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, abort, jsonify, Response

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton,
    MessageAction, URIAction
)

# =========================
# Config
# =========================
CHANNEL_ACCESS_TOKEN = (os.getenv("CHANNEL_ACCESS_TOKEN") or "").strip()
CHANNEL_SECRET = (os.getenv("CHANNEL_SECRET") or "").strip()
LIFF_ID = (os.getenv("2009050373-HHA8grO4") or "").strip()
BASE_URL = (os.getenv("https://mahjong-line-bot.onrender.com") or "").strip().rstrip("/")
OWNER_USER_ID = (os.getenv("Ua5794a5932d2427fcaa42ee039a2067a") or "").strip()

DB_PATH = os.getenv("DATABASE_PATH", "/tmp/mahjong.db")
TZ = ZoneInfo("Asia/Taipei")

TABLE_SIZE = 4
CONFIRM_TIMEOUT_SEC = 30

PHONE_RE = re.compile(r"^09\d{8}$")
TIME_RE_24H = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
TIME_RE_ZH = re.compile(r"^(上午|晚上)\s*(\d{1,2})\s*點$")

SPEEDS = ["快手", "慢手", "不限"]
AMOUNTS = ["50/20", "100/20", "100/50", "200/50"]
ROUNDS = ["2將", "3將"]
PARTY_CHOICES = ["我1人", "我2人", "我3人"]

DEDUCTIONS = [
    ("放鳥", -20),
    ("取消", -5),
    ("遲到", -10),
    ("玩家檢舉", -15),
]

AUTO_TIMEOUT_DEDUCT = -5
AUTO_TIMEOUT_REASON = "確認逾時未點選"

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

_lock = threading.Lock()

# =========================
# DB helpers
# =========================
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def now_tw():
    return datetime.now(TZ)

def iso(dt: datetime):
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_iso(s: str):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))

def month_key():
    return now_tw().strftime("%Y-%m")

def init_db():
    conn = db_conn()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id TEXT PRIMARY KEY,
        nickname TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        credit INTEGER DEFAULT 100,
        frozen INTEGER DEFAULT 0,
        created_at TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS user_state(
        user_id TEXT PRIMARY KEY,
        state TEXT,
        data TEXT,
        updated_at TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS shop(
        id INTEGER PRIMARY KEY CHECK (id=1),
        name TEXT DEFAULT '店家',
        group_link TEXT DEFAULT '',
        map_link TEXT DEFAULT '',
        shop_line_link TEXT DEFAULT '',
        is_open INTEGER DEFAULT 1,
        updated_at TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS tables(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT,                -- OPEN / MATCH
        room_name TEXT,
        speed TEXT,
        amount TEXT,
        rounds TEXT,
        time_mode TEXT,           -- NOW / RESERVE
        time_text TEXT,           -- '現在' or '晚上7點'
        status TEXT,              -- waiting / confirming / filled / cancelled
        display_no TEXT DEFAULT '', -- assigned only when filled
        month_key TEXT,
        confirm_started_at TEXT DEFAULT '',
        created_at TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS participants(
        table_id INTEGER,
        user_id TEXT,
        party_size INTEGER,
        confirmed INTEGER DEFAULT 0,
        joined_at TEXT,
        PRIMARY KEY(table_id, user_id)
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS match_pool(
        user_id TEXT PRIMARY KEY,
        speed TEXT,
        amount TEXT,
        rounds TEXT,
        party_size INTEGER,
        created_at TEXT
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS seq_open(
        month_key TEXT PRIMARY KEY,
        seq INTEGER DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS seq_match(
        month_key TEXT PRIMARY KEY,
        seq INTEGER DEFAULT 0
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS credit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        delta INTEGER,
        reason TEXT,
        by_user_id TEXT,
        created_at TEXT
    )""")

    # ensure shop row
    c.execute("SELECT id FROM shop WHERE id=1")
    if c.fetchone() is None:
        c.execute("INSERT INTO shop(id, updated_at) VALUES(1, ?)", (iso(now_tw()),))

    conn.commit()
    conn.close()

init_db()

def shop_get():
    conn = db_conn()
    r = conn.execute("SELECT * FROM shop WHERE id=1").fetchone()
    conn.close()
    return r

def shop_update(field, value):
    if field not in ("name", "group_link", "map_link", "shop_line_link", "is_open"):
        return False
    conn = db_conn()
    conn.execute(f"UPDATE shop SET {field}=?, updated_at=? WHERE id=1", (value, iso(now_tw())))
    conn.commit()
    conn.close()
    return True

def user_get(uid):
    conn = db_conn()
    r = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if r is None:
        conn.execute("INSERT INTO users(user_id, created_at) VALUES(?,?)", (uid, iso(now_tw())))
        conn.commit()
        r = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return r

def user_update(uid, **kwargs):
    if not kwargs:
        return
    cols = []
    vals = []
    for k,v in kwargs.items():
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(uid)
    conn = db_conn()
    conn.execute(f"UPDATE users SET {', '.join(cols)} WHERE user_id=?", vals)
    conn.commit()
    conn.close()

def credit_apply(target_uid, delta, reason, by_uid):
    # apply + log + freeze if <60
    conn = db_conn()
    u = conn.execute("SELECT credit, frozen FROM users WHERE user_id=?", (target_uid,)).fetchone()
    if not u:
        conn.close()
        return False
    new_credit = int(u["credit"]) + int(delta)
    frozen = int(u["frozen"])
    if new_credit < 60:
        frozen = 1
    conn.execute("UPDATE users SET credit=?, frozen=? WHERE user_id=?", (new_credit, frozen, target_uid))
    conn.execute(
        "INSERT INTO credit_logs(user_id, delta, reason, by_user_id, created_at) VALUES(?,?,?,?,?)",
        (target_uid, int(delta), reason, by_uid, iso(now_tw()))
    )
    conn.commit()
    conn.close()
    return True

def state_get(uid):
    conn = db_conn()
    r = conn.execute("SELECT * FROM user_state WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not r:
        return None
    try:
        data = json.loads(r["data"] or "{}")
    except Exception:
        data = {}
    return {"state": r["state"], "data": data}

def state_set(uid, state, data=None):
    conn = db_conn()
    conn.execute(
        "REPLACE INTO user_state(user_id, state, data, updated_at) VALUES(?,?,?,?)",
        (uid, state, json.dumps(data or {}), iso(now_tw()))
    )
    conn.commit()
    conn.close()

def state_clear(uid):
    conn = db_conn()
    conn.execute("DELETE FROM user_state WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

# =========================
# Table numbering
# =========================
def next_open_display_no():
    mk = month_key()
    conn = db_conn()
    r = conn.execute("SELECT seq FROM seq_open WHERE month_key=?", (mk,)).fetchone()
    seq = int(r["seq"]) if r else 0
    letter = chr(ord("A") + (seq % 26))
    num = (seq // 26) + 1
    conn.execute("REPLACE INTO seq_open(month_key, seq) VALUES(?,?)", (mk, seq + 1))
    conn.commit()
    conn.close()
    return f"{letter}{num}"

def next_match_display_no():
    mk = month_key()
    conn = db_conn()
    r = conn.execute("SELECT seq FROM seq_match WHERE month_key=?", (mk,)).fetchone()
    seq = int(r["seq"]) if r else 0
    seq += 1
    conn.execute("REPLACE INTO seq_match(month_key, seq) VALUES(?,?)", (mk, seq))
    conn.commit()
    conn.close()
    return str(seq)

# =========================
# Time text parse (for reserve input only)
# =========================
def normalize_reserve_text(s: str):
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    m = TIME_RE_ZH.match(s)
    if m:
        ap = m.group(1)
        h = int(m.group(2))
        if h <= 0 or h > 12:
            return None
        # keep original style
        return f"{ap}{h}點"
    m2 = TIME_RE_24H.match(s)
    if m2:
        hh = int(m2.group(1))
        mm = int(m2.group(2))
        return f"{hh:02d}:{mm:02d}"
    return None

# =========================
# LINE UI helpers
# =========================
def qr_buttons(buttons):
    return QuickReply(items=buttons)

def menu_main(is_owner=False):
    items = [
        QuickReplyButton(MessageAction("🎲 開桌", "開桌")),
        QuickReplyButton(MessageAction("🧩 配桌", "配桌")),
        QuickReplyButton(URIAction("📋 桌況查詢", f"https://liff.line.me/{LIFF_ID}")),
        QuickReplyButton(MessageAction("👤 我的", "我的")),
        QuickReplyButton(MessageAction("☎️ 聯絡店家", "聯絡店家")),
    ]
    if is_owner:
        items.append(QuickReplyButton(MessageAction("🏪 店家後台", "店家後台")))
    return qr_buttons(items)

def reply(token, text, quick_reply=None):
    line_bot_api.reply_message(token, TextSendMessage(text=text, quick_reply=quick_reply))

def push(uid, text, quick_reply=None):
    line_bot_api.push_message(uid, TextSendMessage(text=text, quick_reply=quick_reply))

def is_owner(uid):
    return bool(OWNER_USER_ID) and uid == OWNER_USER_ID

# =========================
# Core: table / participants
# =========================
def table_get(table_id):
    conn = db_conn()
    r = conn.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone()
    conn.close()
    return r

def table_participants(table_id):
    conn = db_conn()
    rows = conn.execute("SELECT * FROM participants WHERE table_id=?", (table_id,)).fetchall()
    conn.close()
    return rows

def table_headcount(table_id):
    rows = table_participants(table_id)
    return sum(int(r["party_size"]) for r in rows)

def user_current_activity(uid):
    conn = db_conn()
    # in pool?
    p = conn.execute("SELECT * FROM match_pool WHERE user_id=?", (uid,)).fetchone()
    # in any waiting/confirming table?
    t = conn.execute("""
        SELECT t.* FROM tables t
        JOIN participants p ON p.table_id=t.id
        WHERE p.user_id=? AND t.status IN ('waiting','confirming')
        ORDER BY t.id DESC LIMIT 1
    """, (uid,)).fetchone()
    conn.close()
    return {"pool": bool(p), "table": dict(t) if t else None}

def add_participant(table_id, uid, party_size):
    with _lock:
        t = table_get(table_id)
        if not t or t["status"] not in ("waiting",):
            return (False, "此桌已不可加入")
        if int(user_get(uid)["frozen"] or 0) == 1:
            return (False, "⚠️ 您的帳號已凍結，無法加入")
        # prevent multi-join
        cur = user_current_activity(uid)
        if cur["pool"] or cur["table"]:
            return (False, "⚠️ 您目前已在配桌/開桌中，請先取消或完成")
        # capacity check
        current = table_headcount(table_id)
        if current + party_size > TABLE_SIZE:
            return (False, "人數超過 無法入桌")
        conn = db_conn()
        conn.execute(
            "REPLACE INTO participants(table_id,user_id,party_size,confirmed,joined_at) VALUES(?,?,?,?,?)",
            (table_id, uid, party_size, 0, iso(now_tw()))
        )
        conn.commit()
        conn.close()
        # if full -> start confirming
        if current + party_size == TABLE_SIZE:
            start_confirm(table_id)
        return (True, "已加入此桌，等待確認")

def remove_participant(table_id, uid):
    conn = db_conn()
    conn.execute("DELETE FROM participants WHERE table_id=? AND user_id=?", (table_id, uid))
    conn.commit()
    conn.close()

def set_confirmed(table_id, uid, confirmed):
    conn = db_conn()
    conn.execute("UPDATE participants SET confirmed=? WHERE table_id=? AND user_id=?", (1 if confirmed else 0, table_id, uid))
    conn.commit()
    conn.close()

def all_confirmed(table_id):
    rows = table_participants(table_id)
    if not rows:
        return False
    return all(int(r["confirmed"]) == 1 for r in rows) and table_headcount(table_id) == TABLE_SIZE

def start_confirm(table_id):
    with _lock:
        t = table_get(table_id)
        if not t or t["status"] != "waiting":
            return
        conn = db_conn()
        conn.execute("UPDATE tables SET status='confirming', confirm_started_at=? WHERE id=?", (iso(now_tw()), table_id))
        conn.commit()
        conn.close()

        # push confirm links to all participants
        rows = table_participants(table_id)
        for r in rows:
            uid = r["user_id"]
            confirm_url = f"{BASE_URL}/liff/confirm?table_id={table_id}&uid={uid}"
            push(uid, f"✅ 桌已滿人，請在 {CONFIRM_TIMEOUT_SEC} 秒內確認：\n{confirm_url}")
        # start timeout watcher thread
        threading.Thread(target=confirm_timeout_worker, args=(table_id,), daemon=True).start()

def finalize_table(table_id):
    with _lock:
        t = table_get(table_id)
        if not t or t["status"] != "confirming":
            return
        if not all_confirmed(table_id):
            return
        # assign display_no based on kind
        display_no = next_open_display_no() if t["kind"] == "OPEN" else next_match_display_no()
        conn = db_conn()
        conn.execute("UPDATE tables SET status='filled', display_no=? WHERE id=?", (display_no, table_id))
        conn.commit()
        conn.close()

        # notify all participants with same table no
        shop = shop_get()
        rows = table_participants(table_id)
        for r in rows:
            uid = r["user_id"]
            push(uid, build_success_message(t, display_no, shop))

def confirm_timeout_worker(table_id):
    # sleep then handle any non-confirmed
    time.sleep(CONFIRM_TIMEOUT_SEC)
    with _lock:
        t = table_get(table_id)
        if not t or t["status"] != "confirming":
            return
        rows = table_participants(table_id)
        # anyone not confirmed => treat as abandon and deduct
        losers = [r for r in rows if int(r["confirmed"]) != 1]
        if losers:
            for r in losers:
                uid = r["user_id"]
                remove_participant(table_id, uid)
                credit_apply(uid, AUTO_TIMEOUT_DEDUCT, AUTO_TIMEOUT_REASON, OWNER_USER_ID or "system")
                push(uid, "⏱️ 逾時未確認，已視為放棄並扣分 -5\n已回到主選單", menu_main(is_owner(uid)))
            # revert table to waiting
            conn = db_conn()
            conn.execute("UPDATE tables SET status='waiting', confirm_started_at='' WHERE id=?", (table_id,))
            # reset others confirmed
            conn.execute("UPDATE participants SET confirmed=0 WHERE table_id=?", (table_id,))
            conn.commit()
            conn.close()
            # notify remaining users
            remaining = table_participants(table_id)
            for r in remaining:
                push(r["user_id"], "⚠️ 有人放棄/逾時，此桌繼續等待補人", menu_main(is_owner(r["user_id"])))
            return
        # all confirmed
        finalize_table(table_id)

def build_success_message(t, display_no, shop):
    shop_name = shop["name"] if shop else "店家"
    group_link = (shop["group_link"] if shop else "").strip()
    time_line = f"⏰ 時間：{t['time_text']}"
    if t["time_mode"] == "RESERVE":
        arrive_line = "⏱️ 請於成桌前 5 分鐘到店家"
    else:
        arrive_line = "⏱️ 請於 20 分鐘內到店家"
    msg = (
        "🎉 成桌成功\n\n"
        f"🏪 店家：{shop_name}\n"
        f"🪑 桌號：{display_no}\n"
    )
    room = (t["room_name"] or "").strip()
    if room:
        msg += f"🏷️ 房名：{room}\n"
    msg += (
        f"{time_line}\n\n"
        f"💰 金額：{t['amount']}\n"
        f"⚡ 手速：{t['speed']}\n"
        f"🀄 將數：{t['rounds']}\n\n"
    )
    if group_link:
        msg += f"🔗 群組連結：{group_link}\n\n"
    else:
        msg += "🔗 群組連結：尚未設定\n\n"
    msg += (
        f"{arrive_line}\n"
        "💬 進群後 3 分鐘內回報桌號"
    )
    return msg

# =========================
# Match pool (hidden)
# =========================
def pool_upsert(uid, speed, amount, rounds, party_size):
    conn = db_conn()
    conn.execute("""
        REPLACE INTO match_pool(user_id, speed, amount, rounds, party_size, created_at)
        VALUES(?,?,?,?,?,?)
    """, (uid, speed, amount, rounds, party_size, iso(now_tw())))
    conn.commit()
    conn.close()

def pool_remove(uid):
    conn = db_conn()
    conn.execute("DELETE FROM match_pool WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def try_auto_fill_open_table(open_table_id):
    # only when open table is NOW
    with _lock:
        t = table_get(open_table_id)
        if not t or t["status"] != "waiting":
            return
        if t["kind"] != "OPEN":
            return
        if t["time_mode"] != "NOW":
            return
        # match pool by 조건
        conn = db_conn()
        pool = conn.execute("""
            SELECT * FROM match_pool
            WHERE amount=? AND rounds=? AND (speed=? OR speed='不限')
            ORDER BY created_at ASC
        """, (t["amount"], t["rounds"], t["speed"])).fetchall()
        conn.close()

        for p in pool:
            uid = p["user_id"]
            party_size = int(p["party_size"])
            current = table_headcount(open_table_id)
            if current + party_size > TABLE_SIZE:
                continue
            # ensure not already active
            cur = user_current_activity(uid)
            if cur["table"]:
                continue
            # add participant
            ok, _ = add_participant(open_table_id, uid, party_size)
            if ok:
                pool_remove(uid)
                push(uid, "✅ 已自動加入符合條件的開桌，等待確認", menu_main(is_owner(uid)))
            # stop if full
            if table_headcount(open_table_id) == TABLE_SIZE:
                break

# =========================
# LINE main webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def on_message(event):
    uid = event.source.user_id
    text = (event.message.text or "").strip()
    u = user_get(uid)
    st = state_get(uid)
    owner = is_owner(uid)
    shop = shop_get()

    # Owner bootstrap (you)
    if owner:
        # ensure not frozen
        if int(u["frozen"] or 0) == 1:
            user_update(uid, frozen=0)

    # Force phone binding (unless already editing)
    if not (st and st["state"] in ("EDIT_PHONE",)):
        if not (u["phone"] or "").strip():
            if text == "取消":
                reply(event.reply_token, "請先綁定手機號碼（09xxxxxxxx）")
                return
            if PHONE_RE.match(text):
                user_update(uid, phone=text)
                reply(event.reply_token, "✅ 綁定完成", menu_main(owner))
                return
            reply(event.reply_token, "📱 請先輸入手機號碼（09xxxxxxxx）完成綁定")
            return

    # quick command: show user id
    if text.upper() in ("ID", "USERID", "輸入ID", "輸入 id", "輸入Id"):
        reply(event.reply_token, f"🆔 你的 User ID：{uid}", menu_main(owner))
        return

    # Cancel
    if text == "取消":
        state_clear(uid)
        reply(event.reply_token, "已回主選單", menu_main(owner))
        return

    # =========================
    # States: My edits
    # =========================
    if st and st["state"] == "EDIT_NICK":
        new_nick = text.strip()
        user_update(uid, nickname=new_nick)
        state_clear(uid)
        reply(event.reply_token, "✅ 暱稱已更新", menu_main(owner))
        return

    if st and st["state"] == "EDIT_PHONE":
        if not PHONE_RE.match(text):
            reply(event.reply_token, "請輸入正確手機號碼（09xxxxxxxx）", qr_buttons([
                QuickReplyButton(MessageAction("取消", "取消"))
            ]))
            return
        user_update(uid, phone=text)
        state_clear(uid)
        reply(event.reply_token, "✅ 手機已更新", menu_main(owner))
        return

    # =========================
    # States: OPEN flow
    # =========================
    if st and st["state"] == "OPEN_ROOM":
        room = "" if text == "略過" else text
        state_set(uid, "OPEN_SPEED", {"room_name": room})
        reply(event.reply_token, "⚡ 選擇手速", qr_buttons([QuickReplyButton(MessageAction(f"⚡ {x}", x)) for x in SPEEDS]))
        return

    if st and st["state"] == "OPEN_SPEED":
        if text not in SPEEDS:
            reply(event.reply_token, "請從按鍵選擇手速", qr_buttons([QuickReplyButton(MessageAction(f"⚡ {x}", x)) for x in SPEEDS]))
            return
        d = st["data"]
        d["speed"] = text
        state_set(uid, "OPEN_AMOUNT", d)
        reply(event.reply_token, "💰 選擇金額", qr_buttons([QuickReplyButton(MessageAction(f"💰 {x}", x)) for x in AMOUNTS]))
        return

    if st and st["state"] == "OPEN_AMOUNT":
        if text not in AMOUNTS:
            reply(event.reply_token, "請從按鍵選擇金額", qr_buttons([QuickReplyButton(MessageAction(f"💰 {x}", x)) for x in AMOUNTS]))
            return
        d = st["data"]
        d["amount"] = text
        state_set(uid, "OPEN_ROUNDS", d)
        reply(event.reply_token, "🀄 選擇將數", qr_buttons([QuickReplyButton(MessageAction(f"🀄 {x}", x)) for x in ROUNDS]))
        return

    if st and st["state"] == "OPEN_ROUNDS":
        if text not in ROUNDS:
            reply(event.reply_token, "請從按鍵選擇將數", qr_buttons([QuickReplyButton(MessageAction(f"🀄 {x}", x)) for x in ROUNDS]))
            return
        d = st["data"]
        d["rounds"] = text
        state_set(uid, "OPEN_TIME_MODE", d)
        reply(event.reply_token, "⏰ 選擇時間", qr_buttons([
            QuickReplyButton(MessageAction("⏰ 現在", "現在")),
            QuickReplyButton(MessageAction("🗓️ 預約", "預約")),
        ]))
        return

    if st and st["state"] == "OPEN_TIME_MODE":
        d = st["data"]
        if text == "現在":
            d["time_mode"] = "NOW"
            d["time_text"] = "現在"
            # create open table
            if int(shop["is_open"] or 0) != 1:
                state_clear(uid)
                reply(event.reply_token, "⚠️ 店家目前休息中，無法開桌", menu_main(owner))
                return
            create_open_table(uid, d)
            state_clear(uid)
            reply(event.reply_token, "✅ 開桌成功（可到桌況查詢讓其他玩家加入）", menu_main(owner))
            return
        if text == "預約":
            d["time_mode"] = "RESERVE"
            state_set(uid, "OPEN_RESERVE_TEXT", d)
            reply(event.reply_token, "🗓️ 請輸入時間（例：上午7點 / 晚上7點 / 19:00）", None)
            return
        reply(event.reply_token, "請用按鍵選擇時間", qr_buttons([
            QuickReplyButton(MessageAction("⏰ 現在", "現在")),
            QuickReplyButton(MessageAction("🗓️ 預約", "預約")),
        ]))
        return

    if st and st["state"] == "OPEN_RESERVE_TEXT":
        d = st["data"]
        ttxt = normalize_reserve_text(text)
        if not ttxt:
            reply(event.reply_token, "格式不正確，請輸入（上午7點 / 晚上7點 / 19:00）")
            return
        d["time_text"] = ttxt
        if int(shop["is_open"] or 0) != 1:
            state_clear(uid)
            reply(event.reply_token, "⚠️ 店家目前休息中，無法開桌", menu_main(owner))
            return
        create_open_table(uid, d)
        state_clear(uid)
        reply(event.reply_token, "✅ 預約開桌成功（可到桌況查詢讓其他玩家加入）", menu_main(owner))
        return

    # =========================
    # States: MATCH flow
    # =========================
    if st and st["state"] == "MATCH_SPEED":
        if text not in SPEEDS:
            reply(event.reply_token, "請從按鍵選擇手速", qr_buttons([QuickReplyButton(MessageAction(f"⚡ {x}", x)) for x in SPEEDS]))
            return
        d = st["data"]
        d["speed"] = text
        state_set(uid, "MATCH_AMOUNT", d)
        reply(event.reply_token, "💰 選擇金額", qr_buttons([QuickReplyButton(MessageAction(f"💰 {x}", x)) for x in AMOUNTS]))
        return

    if st and st["state"] == "MATCH_AMOUNT":
        if text not in AMOUNTS:
            reply(event.reply_token, "請從按鍵選擇金額", qr_buttons([QuickReplyButton(MessageAction(f"💰 {x}", x)) for x in AMOUNTS]))
            return
        d = st["data"]
        d["amount"] = text
        state_set(uid, "MATCH_ROUNDS", d)
        reply(event.reply_token, "🀄 選擇將數", qr_buttons([QuickReplyButton(MessageAction(f"🀄 {x}", x)) for x in ROUNDS]))
        return

    if st and st["state"] == "MATCH_ROUNDS":
        if text not in ROUNDS:
            reply(event.reply_token, "請從按鍵選擇將數", qr_buttons([QuickReplyButton(MessageAction(f"🀄 {x}", x)) for x in ROUNDS]))
            return
        d = st["data"]
        d["rounds"] = text
        state_set(uid, "MATCH_PARTY", d)
        reply(event.reply_token, "👥 選擇人數", qr_buttons([
            QuickReplyButton(MessageAction("👤 我1人", "我1人")),
            QuickReplyButton(MessageAction("👥 我2人", "我2人")),
            QuickReplyButton(MessageAction("👥 我3人", "我3人")),
        ]))
        return

    if st and st["state"] == "MATCH_PARTY":
        mapping = {"我1人": 1, "我2人": 2, "我3人": 3}
        if text not in mapping:
            reply(event.reply_token, "請從按鍵選擇人數", qr_buttons([
                QuickReplyButton(MessageAction("👤 我1人", "我1人")),
                QuickReplyButton(MessageAction("👥 我2人", "我2人")),
                QuickReplyButton(MessageAction("👥 我3人", "我3人")),
            ]))
            return
        if int(shop["is_open"] or 0) != 1:
            state_clear(uid)
            reply(event.reply_token, "⚠️ 店家目前休息中，無法配桌", menu_main(owner))
            return
        if int(u["frozen"] or 0) == 1:
            state_clear(uid)
            reply(event.reply_token, "⚠️ 您的帳號已凍結，無法配桌", menu_main(owner))
            return
        # prevent multi-activity
        cur = user_current_activity(uid)
        if cur["pool"] or cur["table"]:
            state_clear(uid)
            reply(event.reply_token, "⚠️ 您目前已在配桌/開桌中，請先取消或完成", menu_main(owner))
            return
        d = st["data"]
        party_size = mapping[text]
        pool_upsert(uid, d["speed"], d["amount"], d["rounds"], party_size)
        state_clear(uid)
        reply(event.reply_token, "✅ 已進入配桌等待池（隱藏）\n若有符合『現在開桌』條件會自動加入", menu_main(owner))
        return

    # =========================
    # Owner backend states
    # =========================
    if owner and st and st["state"] == "SHOP_SET_NAME":
        shop_update("name", text.strip())
        state_clear(uid)
        reply(event.reply_token, "✅ 店名已更新", menu_main(True))
        return

    if owner and st and st["state"] == "SHOP_SET_GROUP":
        shop_update("group_link", text.strip())
        state_clear(uid)
        reply(event.reply_token, "✅ 群連結已更新", menu_main(True))
        return

    if owner and st and st["state"] == "SHOP_SET_MAP":
        shop_update("map_link", text.strip())
        state_clear(uid)
        reply(event.reply_token, "✅ 地圖連結已更新", menu_main(True))
        return

    if owner and st and st["state"] == "SHOP_SET_LINE":
        shop_update("shop_line_link", text.strip())
        state_clear(uid)
        reply(event.reply_token, "✅ 店家LINE連結已更新", menu_main(True))
        return

    if owner and st and st["state"] == "CUST_QUERY":
        q = text.strip()
        conn = db_conn()
        if q.isdigit() and len(q) == 3:
            rows = conn.execute("SELECT * FROM users WHERE phone LIKE ?", (f"%{q}",)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM users WHERE nickname LIKE ?", (f"%{q}%",)).fetchall()
        conn.close()
        if not rows:
            state_clear(uid)
            reply(event.reply_token, "查無資料", menu_main(True))
            return
        # show first match only (simple v1)
        target = rows[0]
        state_set(uid, "CUST_DEDUCT", {"target_uid": target["user_id"]})
        msg = (
            "🧾 客戶資料\n\n"
            f"暱稱：{target['nickname']}\n"
            f"手機：{target['phone']}\n"
            f"信用分：{target['credit']}\n"
            f"凍結：{'是' if int(target['frozen'] or 0)==1 else '否'}\n\n"
            "請選擇扣分項目："
        )
        items = [QuickReplyButton(MessageAction(f"➖ {name}({delta})", f"{name}")) for name, delta in DEDUCTIONS]
        items.append(QuickReplyButton(MessageAction("取消", "取消")))
        reply(event.reply_token, msg, qr_buttons(items))
        return

    if owner and st and st["state"] == "CUST_DEDUCT":
        d = st["data"]
        target_uid = d.get("target_uid")
        delta = None
        reason = None
        for name, dd in DEDUCTIONS:
            if text == name:
                delta = dd
                reason = name
                break
        if delta is None:
            reply(event.reply_token, "請用按鍵選擇扣分項目", None)
            return
        credit_apply(target_uid, delta, reason, uid)
        state_clear(uid)
        reply(event.reply_token, f"✅ 已扣分：{reason} {delta}", menu_main(True))
        return

    # =========================
    # Commands
    # =========================
    if text == "開桌":
        if int(shop["is_open"] or 0) != 1:
            reply(event.reply_token, "⚠️ 店家目前休息中，無法開桌", menu_main(owner))
            return
        cur = user_current_activity(uid)
        if cur["pool"] or cur["table"]:
            reply(event.reply_token, "⚠️ 您目前已在配桌/開桌中\n可選：取消配桌/查看桌況/回主畫面", qr_buttons([
                QuickReplyButton(MessageAction("🛑 取消配桌", "取消配桌")),
                QuickReplyButton(URIAction("📋 查看桌況", f"https://liff.line.me/{LIFF_ID}")),
                QuickReplyButton(MessageAction("🏠 回主畫面", "回主畫面")),
            ]))
            return
        state_set(uid, "OPEN_ROOM", {})
        reply(event.reply_token, "🏷️ 請輸入房名（或輸入「略過」）")
        return

    if text == "配桌":
        if int(shop["is_open"] or 0) != 1:
            reply(event.reply_token, "⚠️ 店家目前休息中，無法配桌", menu_main(owner))
            return
        if int(u["frozen"] or 0) == 1:
            reply(event.reply_token, "⚠️ 您的帳號已凍結，無法配桌", menu_main(owner))
            return
        cur = user_current_activity(uid)
        if cur["pool"] or cur["table"]:
            reply(event.reply_token, "⚠️ 您目前已在配桌/開桌中\n可選：取消配桌/查看桌況/回主畫面", qr_buttons([
                QuickReplyButton(MessageAction("🛑 取消配桌", "取消配桌")),
                QuickReplyButton(URIAction("📋 查看桌況", f"https://liff.line.me/{LIFF_ID}")),
                QuickReplyButton(MessageAction("🏠 回主畫面", "回主畫面")),
            ]))
            return
        state_set(uid, "MATCH_SPEED", {})
        reply(event.reply_token, "⚡ 選擇手速", qr_buttons([QuickReplyButton(MessageAction(f"⚡ {x}", x)) for x in SPEEDS]))
        return

    if text == "取消配桌":
        pool_remove(uid)
        # also allow leave current table (waiting/confirming)
        act = user_current_activity(uid)
        if act["table"]:
            remove_participant(act["table"]["id"], uid)
        reply(event.reply_token, "已取消配桌/等待，回主選單", menu_main(owner))
        return

    if text == "回主畫面":
        reply(event.reply_token, "主選單", menu_main(owner))
        return

    if text == "我的":
        act = user_current_activity(uid)
        status = "無"
        if act["pool"]:
            status = "配桌等待中（隱藏）"
        elif act["table"]:
            status = f"在桌內（狀態：{act['table']['status']}）"
        msg = (
            "👤 我的\n\n"
            f"暱稱：{u['nickname']}\n"
            f"手機：{u['phone']}\n"
            f"信用分：{u['credit']}\n"
            f"狀態：{status}"
        )
        reply(event.reply_token, msg, qr_buttons([
            QuickReplyButton(MessageAction("✏️ 修改暱稱", "修改暱稱")),
            QuickReplyButton(MessageAction("📱 修改手機", "修改手機")),
            QuickReplyButton(MessageAction("取消", "取消")),
        ]))
        return

    if text == "修改暱稱":
        state_set(uid, "EDIT_NICK", {})
        reply(event.reply_token, "✏️ 請輸入新暱稱", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    if text == "修改手機":
        state_set(uid, "EDIT_PHONE", {})
        reply(event.reply_token, "📱 請輸入新手機號碼（09xxxxxxxx）", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    # Contact shop (final spec)
    if text == "聯絡店家":
        shop = shop_get()
        is_open = int(shop["is_open"] or 0) == 1
        line_link = (shop["shop_line_link"] or "").strip()
        map_link = (shop["map_link"] or "").strip()
        if (not is_open) or (not line_link) or (not map_link):
            reply(event.reply_token, "⚠️ 目前未有店家上線\n請稍後再試", menu_main(owner))
            return
        reply(event.reply_token, "☎️ 聯絡店家", qr_buttons([
            QuickReplyButton(URIAction("🏪 店家LINE", line_link)),
            QuickReplyButton(URIAction("📍 地圖", map_link)),
        ]))
        return

    # Owner backend
    if owner and text == "店家後台":
        shop = shop_get()
        status = "營業" if int(shop["is_open"] or 0) == 1 else "休息"
        msg = (
            "🏪 店家後台\n\n"
            f"店名：{shop['name']}\n"
            f"狀態：{status}\n\n"
            "請選擇操作："
        )
        reply(event.reply_token, msg, qr_buttons([
            QuickReplyButton(MessageAction("🏷️ 改店名", "改店名")),
            QuickReplyButton(MessageAction("🔗 設群連結", "設群連結")),
            QuickReplyButton(MessageAction("📍 設地圖連結", "設地圖連結")),
            QuickReplyButton(MessageAction("🏪 設店家LINE", "設店家LINE")),
            QuickReplyButton(MessageAction("✅ 切換營業/休息", "切換營業")),
            QuickReplyButton(MessageAction("🧾 查詢客戶", "查詢客戶")),
            QuickReplyButton(MessageAction("取消", "取消")),
        ]))
        return

    if owner and text == "改店名":
        state_set(uid, "SHOP_SET_NAME", {})
        reply(event.reply_token, "🏷️ 請輸入新店名", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    if owner and text == "設群連結":
        state_set(uid, "SHOP_SET_GROUP", {})
        reply(event.reply_token, "🔗 請貼上群組連結", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    if owner and text == "設地圖連結":
        state_set(uid, "SHOP_SET_MAP", {})
        reply(event.reply_token, "📍 請貼上地圖連結", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    if owner and text == "設店家LINE":
        state_set(uid, "SHOP_SET_LINE", {})
        reply(event.reply_token, "🏪 請貼上店家LINE連結", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    if owner and text == "切換營業":
        shop = shop_get()
        newv = 0 if int(shop["is_open"] or 0) == 1 else 1
        shop_update("is_open", newv)
        reply(event.reply_token, f"✅ 已切換為：{'營業' if newv==1 else '休息'}", menu_main(True))
        return

    if owner and text == "查詢客戶":
        state_set(uid, "CUST_QUERY", {})
        reply(event.reply_token, "🧾 請輸入「暱稱關鍵字」或「手機末三碼」", qr_buttons([QuickReplyButton(MessageAction("取消", "取消"))]))
        return

    # Default: show main menu
    reply(event.reply_token, "請使用下方選單操作", menu_main(owner))

def create_open_table(uid, d):
    # create OPEN table, creator joins with party_size=1 by default?
    # Your spec: open desk has creator party choice in earlier versions, but final spec didn't require here.
    # We keep creator as 1 person (最穩 v1). Others join via LIFF.
    conn = db_conn()
    conn.execute("""
        INSERT INTO tables(kind, room_name, speed, amount, rounds, time_mode, time_text,
                           status, display_no, month_key, created_at)
        VALUES('OPEN',?,?,?,?,?,?, 'waiting','', ?, ?)
    """, (d["room_name"], d["speed"], d["amount"], d["rounds"], d["time_mode"], d["time_text"], month_key(), iso(now_tw())))
    table_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    # creator joins as 1 (stable v1)
    add_participant(table_id, uid, 1)
    # auto-fill from pool if NOW
    try_auto_fill_open_table(table_id)

# =========================
# LIFF - minimal HTML
# =========================
LIFF_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>桌況查詢</title>
<style>
body{font-family:Arial, sans-serif; padding:14px;}
.card{border:1px solid #ddd; border-radius:10px; padding:12px; margin:10px 0;}
.btn{display:inline-block; padding:10px 12px; border:1px solid #333; border-radius:8px; text-decoration:none; margin-top:8px;}
.small{color:#555; font-size:13px;}
</style>
</head>
<body>
<h3>📋 桌況查詢（開桌）</h3>
<div id="status" class="small">載入中...</div>
<div id="list"></div>

<script>
async function load(){
  const res = await fetch('/api/open_tables');
  const data = await res.json();
  const st = document.getElementById('status');
  const list = document.getElementById('list');
  list.innerHTML = '';
  if(!data.ok){
    st.innerText = data.msg || '載入失敗';
    return;
  }
  st.innerText = '共 ' + data.tables.length + ' 桌';
  data.tables.forEach(t=>{
    const div = document.createElement('div');
    div.className='card';
    div.innerHTML = `
      <div><b>🏷️ 房名：</b>${t.room || '-'}</div>
      <div><b>⏰ 時間：</b>${t.time_text}</div>
      <div><b>💰 金額：</b>${t.amount}　<b>⚡</b>${t.speed}　<b>🀄</b>${t.rounds}</div>
      <div><b>👥 缺：</b>${t.missing}</div>
      <a class="btn" href="/liff/join?table_id=${t.id}">選這桌 → 加入</a>
    `;
    list.appendChild(div);
  });
}
load();
</script>
</body>
</html>"""

JOIN_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>加入桌</title>
<style>
body{font-family:Arial, sans-serif; padding:14px;}
.btn{display:block; width:100%; padding:12px; border:1px solid #333; border-radius:10px; text-align:center; margin:10px 0; text-decoration:none;}
.small{color:#555; font-size:13px;}
</style>
</head>
<body>
<h3>🧩 加入此桌</h3>
<div class="small">請選擇人數</div>
<a class="btn" href="#" onclick="join(1);return false;">👤 我1人</a>
<a class="btn" href="#" onclick="join(2);return false;">👥 我2人</a>
<a class="btn" href="#" onclick="join(3);return false;">👥 我3人</a>
<div id="msg" class="small"></div>

<script>
const tableId = new URLSearchParams(location.search).get('table_id');
async function join(n){
  const res = await fetch('/api/join_open', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({table_id: Number(tableId), party_size: n})
  });
  const data = await res.json();
  document.getElementById('msg').innerText = data.msg || '';
}
</script>
</body>
</html>"""

CONFIRM_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>確認</title>
<style>
body{font-family:Arial, sans-serif; padding:14px;}
.btn{display:block; width:100%; padding:12px; border:1px solid #333; border-radius:10px; text-align:center; margin:10px 0; text-decoration:none;}
.small{color:#555; font-size:13px;}
</style>
</head>
<body>
<h3>✅ 成桌確認</h3>
<div id="info" class="small">載入中...</div>
<a class="btn" href="#" onclick="act('join');return false;">✅ 加入</a>
<a class="btn" href="#" onclick="act('abandon');return false;">❌ 放棄</a>
<div id="msg" class="small"></div>
<script>
const qs = new URLSearchParams(location.search);
const tableId = Number(qs.get('table_id'));
const uid = qs.get('uid');

async function load(){
  const res = await fetch('/api/table_info?table_id=' + tableId);
  const data = await res.json();
  if(!data.ok){ document.getElementById('info').innerText = data.msg || '載入失敗'; return; }
  document.getElementById('info').innerText = `桌狀態：${data.table.status} / 目前人數：${data.table.headcount}/4`;
}
async function act(action){
  const res = await fetch('/api/confirm', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({table_id: tableId, uid: uid, action: action})
  });
  const data = await res.json();
  document.getElementById('msg').innerText = data.msg || '';
  load();
}
load();
</script>
</body>
</html>"""

@app.route("/liff")
def liff_root():
    return Response(LIFF_HTML, mimetype="text/html")

@app.route("/liff/join")
def liff_join():
    return Response(JOIN_HTML, mimetype="text/html")

@app.route("/liff/confirm")
def liff_confirm():
    return Response(CONFIRM_HTML, mimetype="text/html")

# =========================
# API for LIFF
# =========================
@app.route("/api/open_tables")
def api_open_tables():
    shop = shop_get()
    if int(shop["is_open"] or 0) != 1:
        return jsonify({"ok": False, "msg": "店家休息中，暫停桌況"})
    conn = db_conn()
    rows = conn.execute("""
        SELECT * FROM tables
        WHERE kind='OPEN' AND status IN ('waiting','confirming')
        ORDER BY id DESC
        LIMIT 50
    """).fetchall()
    conn.close()
    tables = []
    for r in rows:
        head = table_headcount(r["id"])
        missing = max(0, TABLE_SIZE - head)
        tables.append({
            "id": r["id"],
            "room": (r["room_name"] or ""),
            "speed": r["speed"],
            "amount": r["amount"],
            "rounds": r["rounds"],
            "time_text": r["time_text"],
            "status": r["status"],
            "headcount": head,
            "missing": missing,
        })
    return jsonify({"ok": True, "tables": tables})

@app.route("/api/join_open", methods=["POST"])
def api_join_open():
    data = request.get_json(silent=True) or {}
    table_id = int(data.get("table_id") or 0)
    party_size = int(data.get("party_size") or 1)
    # LIFF cannot know user id securely without LIFF SDK; v1: require user to join via LINE push from bot.
    # To keep system workable, we allow joining only by "last active user" is impossible here.
    # So: This endpoint returns instruction to join from LINE by typing "加入桌 <id> <人數>".
    return jsonify({"ok": False, "msg": "為了安全，請回到 LINE 對話輸入：\n加入桌 " + str(table_id) + " " + str(party_size)})

@app.route("/api/table_info")
def api_table_info():
    table_id = int(request.args.get("table_id") or 0)
    t = table_get(table_id)
    if not t:
        return jsonify({"ok": False, "msg": "找不到桌"})
    return jsonify({"ok": True, "table": {"status": t["status"], "headcount": table_headcount(table_id)}})

@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    data = request.get_json(silent=True) or {}
    table_id = int(data.get("table_id") or 0)
    uid = (data.get("uid") or "").strip()
    action = (data.get("action") or "").strip()
    if not table_id or not uid:
        return jsonify({"ok": False, "msg": "參數錯誤"})
    t = table_get(table_id)
    if not t or t["status"] != "confirming":
        return jsonify({"ok": False, "msg": "此桌目前不可確認"})
    if action == "join":
        set_confirmed(table_id, uid, True)
        if all_confirmed(table_id):
            finalize_table(table_id)
        return jsonify({"ok": True, "msg": "✅ 已確認加入"})
    if action == "abandon":
        remove_participant(table_id, uid)
        # revert table to waiting
        conn = db_conn()
        conn.execute("UPDATE tables SET status='waiting', confirm_started_at='' WHERE id=?", (table_id,))
        conn.execute("UPDATE participants SET confirmed=0 WHERE table_id=?", (table_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "msg": "❌ 已放棄（此桌繼續等待）"})
    return jsonify({"ok": False, "msg": "未知操作"})

# =========================
# Extra LINE commands for LIFF join (secure v1)
# =========================
@handler.add(MessageEvent, message=TextMessage)
def on_message_join(event):
    # NOTE: linebot handler triggers all added handlers; this one should be last.
    uid = event.source.user_id
    text = (event.message.text or "").strip()
    owner = is_owner(uid)

    m = re.match(r"^加入桌\s+(\d+)\s+(\d+)$", text)
    if m:
        shop = shop_get()
        if int(shop["is_open"] or 0) != 1:
            reply(event.reply_token, "⚠️ 店家目前休息中，無法加入", menu_main(owner))
            return
        table_id = int(m.group(1))
        party_size = int(m.group(2))
        if party_size not in (1,2,3):
            reply(event.reply_token, "人數錯誤（1~3）", menu_main(owner))
            return
        ok, msg = add_participant(table_id, uid, party_size)
        reply(event.reply_token, ("✅ " if ok else "⚠️ ") + msg, menu_main(owner))
        return

    # fallthrough: do nothing here, main handler already replied
    # We must not double-reply.

# =========================
# Health
# =========================
@app.route("/")
def home():
    return "OK"

if __name__ == "__main__":
    # Render uses gunicorn; local run:
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

