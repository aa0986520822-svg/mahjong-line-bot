import os
import re
import sqlite3
import time
import random
from datetime import datetime

from flask import Flask, request, abort, g

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction, URIAction
)

app = Flask(__name__)

# ========= ENV =========
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
DB_PATH = os.getenv("DB_PATH", "mahjong.db")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("WARNING: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET is not set")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ========= STATE (in-memory) =========
# user_state[user_id] = {"mode": "...", "data": {...}}
user_state = {}

# ========= Helpers =========
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    conn = g.pop("db", None)
    if conn:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        nickname TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        credit INTEGER DEFAULT 100,
        frozen INTEGER DEFAULT 0,
        created_at TEXT DEFAULT ''
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS shops (
        shop_id TEXT PRIMARY KEY,
        name TEXT DEFAULT '店家',
        is_open INTEGER DEFAULT 1,
        group_link TEXT DEFAULT '',
        map_link TEXT DEFAULT '',
        shop_line_link TEXT DEFAULT ''
    )
    """)

    # one default shop
    c.execute("INSERT OR IGNORE INTO shops(shop_id, name, is_open, group_link, map_link, shop_line_link) VALUES (?,?,?,?,?,?)",
              ("default", "預設店家", 1, "", "", ""))

    c.execute("""
    CREATE TABLE IF NOT EXISTS owner (
        id INTEGER PRIMARY KEY CHECK (id=1),
        owner_user_id TEXT DEFAULT ''
    )
    """)
    c.execute("INSERT OR IGNORE INTO owner(id, owner_user_id) VALUES (1, '')")

    c.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY,
        joined_at TEXT DEFAULT ''
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS admin_invite (
        id INTEGER PRIMARY KEY CHECK (id=1),
        code TEXT DEFAULT ''
    )
    """)
    c.execute("INSERT OR IGNORE INTO admin_invite(id, code) VALUES (1, '')")

    c.execute("""
    CREATE TABLE IF NOT EXISTS tables (
        table_id TEXT PRIMARY KEY,
        created_by TEXT,
        people_need INTEGER DEFAULT 4,
        amount TEXT DEFAULT '',
        hand TEXT DEFAULT '不限',          -- 快手/慢手/不限
        time_type TEXT DEFAULT '現在',     -- 現在/預約/其他
        reserve_slot TEXT DEFAULT '',      -- 早上/下午/晚上/半夜
        reserve_time TEXT DEFAULT '',      -- 19:00
        note TEXT DEFAULT '',
        status TEXT DEFAULT 'waiting',     -- waiting/filled/canceled
        created_at TEXT DEFAULT ''
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS table_members (
        table_id TEXT,
        user_id TEXT,
        joined_at TEXT DEFAULT '',
        PRIMARY KEY(table_id, user_id)
    )
    """)

    conn.commit()
    conn.close()

init_db()

def ensure_user(user_id: str):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users(user_id, nickname, phone, credit, frozen, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, "", "", 100, 0, now_str())
        )
        conn.commit()

def get_owner_id():
    row = db().execute("SELECT owner_user_id FROM owner WHERE id=1").fetchone()
    return (row["owner_user_id"] or "").strip() if row else ""

def set_owner_if_empty(user_id: str):
    oid = get_owner_id()
    if not oid:
        db().execute("UPDATE owner SET owner_user_id=? WHERE id=1", (user_id,))
        db().commit()

def is_admin(user_id: str) -> bool:
    row = db().execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def add_admin(user_id: str):
    db().execute("INSERT OR IGNORE INTO admins(user_id, joined_at) VALUES (?,?)", (user_id, now_str()))
    db().commit()

def set_invite_code(code: str):
    db().execute("UPDATE admin_invite SET code=? WHERE id=1", (code,))
    db().commit()

def get_invite_code() -> str:
    row = db().execute("SELECT code FROM admin_invite WHERE id=1").fetchone()
    return (row["code"] or "").strip() if row else ""

def get_shop():
    return db().execute("SELECT * FROM shops WHERE shop_id='default'").fetchone()

def update_shop_field(field: str, value: str):
    if field not in ("group_link", "map_link", "shop_line_link", "is_open", "name"):
        return
    db().execute(f"UPDATE shops SET {field}=? WHERE shop_id='default'", (value,))
    db().commit()

def user_is_frozen(user_id: str) -> bool:
    row = db().execute("SELECT frozen FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row["frozen"]) if row else False

def user_credit(user_id: str) -> int:
    row = db().execute("SELECT credit FROM users WHERE user_id=?", (user_id,)).fetchone()
    return int(row["credit"]) if row else 0

def set_frozen(user_id: str, frozen: int):
    db().execute("UPDATE users SET frozen=? WHERE user_id=?", (int(frozen), user_id))
    db().commit()

def set_credit(user_id: str, credit: int):
    db().execute("UPDATE users SET credit=? WHERE user_id=?", (int(credit), user_id))
    db().commit()

def deduct_credit(user_id: str, delta: int):
    c = user_credit(user_id)
    c2 = c + int(delta)
    if c2 < 0:
        c2 = 0
    set_credit(user_id, c2)
    if c2 < 60:
        set_frozen(user_id, 1)
    return c2

def get_user_profile(user_id: str):
    row = db().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    return row

def set_nickname(user_id: str, nickname: str):
    db().execute("UPDATE users SET nickname=? WHERE user_id=?", (nickname.strip(), user_id))
    db().commit()

def set_phone(user_id: str, phone: str):
    db().execute("UPDATE users SET phone=? WHERE user_id=?", (phone.strip(), user_id))
    db().commit()

def current_table_status(user_id: str) -> str:
    # find latest waiting table where user is member
    row = db().execute("""
        SELECT t.table_id, t.status, t.amount, t.hand, t.time_type, t.reserve_slot, t.reserve_time
        FROM table_members m
        JOIN tables t ON t.table_id = m.table_id
        WHERE m.user_id=? AND t.status='waiting'
        ORDER BY t.created_at DESC
        LIMIT 1
    """, (user_id,)).fetchone()
    if not row:
        return "未配桌"
    tt = row["time_type"]
    if tt == "預約":
        when = f"{row['reserve_slot']} {row['reserve_time']}".strip()
    else:
        when = tt
    return f"等待中（桌號:{row['table_id']}｜{row['hand']}｜{row['amount']}｜{when}）"

def reply_text(event, text: str, quick_reply=None):
    if not line_bot_api:
        return
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text, quick_reply=quick_reply))

def push_text(user_id: str, text: str, quick_reply=None):
    if not line_bot_api:
        return
    line_bot_api.push_message(user_id, TextSendMessage(text=text, quick_reply=quick_reply))

# ========= Quick Replies =========
def qr_main(user_id: str) -> QuickReply:
    # 主選單：不放「回主選單」
    items = [
        QuickReplyButton(action=MessageAction(label="🎲 開桌/配桌", text="開桌/配桌")),
        QuickReplyButton(action=MessageAction(label="📊 桌況查詢", text="桌況查詢")),
        QuickReplyButton(action=MessageAction(label="👤 我的", text="我的")),
        QuickReplyButton(action=MessageAction(label="☎️ 聯絡店家", text="聯絡店家")),
    ]
    if is_admin(user_id):
        items.append(QuickReplyButton(action=MessageAction(label="🧾 客戶資訊", text="客戶資訊")))
        items.append(QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")))
    return QuickReply(items=items)

def qr_home(user_id: str) -> QuickReply:
    # 其他頁面才放「主選單」
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_contact() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="📲 店家LINE", text="店家LINE")),
        QuickReplyButton(action=MessageAction(label="🗺 地圖", text="地圖")),
    ])

def qr_open_hand() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="⚡ 快手", text="快手")),
        QuickReplyButton(action=MessageAction(label="🐢 慢手", text="慢手")),
        QuickReplyButton(action=MessageAction(label="♾ 不限", text="不限")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_people_need() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="2人桌", text="2")),
        QuickReplyButton(action=MessageAction(label="3人桌", text="3")),
        QuickReplyButton(action=MessageAction(label="4人桌", text="4")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_time_type() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="現在", text="現在")),
        QuickReplyButton(action=MessageAction(label="預約時間", text="預約時間")),
        QuickReplyButton(action=MessageAction(label="其他補充", text="其他補充")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_reserve_slot() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="早上", text="早上")),
        QuickReplyButton(action=MessageAction(label="下午", text="下午")),
        QuickReplyButton(action=MessageAction(label="晚上", text="晚上")),
        QuickReplyButton(action=MessageAction(label="半夜", text="半夜")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_table_status() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="缺1", text="桌況 缺1")),
        QuickReplyButton(action=MessageAction(label="缺2", text="桌況 缺2")),
        QuickReplyButton(action=MessageAction(label="缺3", text="桌況 缺3")),
        QuickReplyButton(action=MessageAction(label="全部", text="桌況 全部")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_shop_admin() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="群設定", text="群設定")),
        QuickReplyButton(action=MessageAction(label="地圖設定", text="地圖設定")),
        QuickReplyButton(action=MessageAction(label="店家LINE設定", text="店家LINE設定")),
        QuickReplyButton(action=MessageAction(label="營業/休息", text="營業/休息")),
        QuickReplyButton(action=MessageAction(label="產生管理員碼", text="產生管理員碼")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

def qr_credit_deduct() -> QuickReply:
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="放鳥 -20", text="扣分 放鳥")),
        QuickReplyButton(action=MessageAction(label="取消 -5", text="扣分 取消")),
        QuickReplyButton(action=MessageAction(label="遲到 -10", text="扣分 遲到")),
        QuickReplyButton(action=MessageAction(label="玩家檢舉 -15", text="扣分 玩家檢舉")),
        QuickReplyButton(action=MessageAction(label="凍結/解除", text="凍結切換")),
        QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
    ])

# ========= Business Logic =========
def can_play(user_id: str):
    ensure_user(user_id)
    if user_is_frozen(user_id):
        return False, "⛔ 你目前已被凍結（信用分低於60或店家手動凍結），無法開桌/配桌/加入桌。\n請聯絡店家處理。"
    shop = get_shop()
    if shop and int(shop["is_open"]) == 0:
        return False, "🚫 店家目前休息中，暫停配桌。"
    return True, ""

def create_table(user_id: str, people_need: int, amount: str, hand: str, time_type: str, slot: str, rtime: str, note: str):
    tid = f"T{int(time.time())}{random.randint(100,999)}"
    conn = db()
    conn.execute("""
        INSERT INTO tables(table_id, created_by, people_need, amount, hand, time_type, reserve_slot, reserve_time, note, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (tid, user_id, people_need, amount, hand, time_type, slot, rtime, note, "waiting", now_str()))
    conn.execute("""
        INSERT OR IGNORE INTO table_members(table_id, user_id, joined_at) VALUES (?,?,?)
    """, (tid, user_id, now_str()))
    conn.commit()
    return tid

def list_tables(missing=None):
    # missing: 1/2/3
    rows = db().execute("""
        SELECT t.*,
          (SELECT COUNT(1) FROM table_members m WHERE m.table_id=t.table_id) AS joined
        FROM tables t
        WHERE t.status='waiting'
        ORDER BY t.created_at DESC
        LIMIT 20
    """).fetchall()

    out = []
    for r in rows:
        joined = int(r["joined"])
        need = int(r["people_need"])
        miss = max(need - joined, 0)
        if missing is not None and miss != missing:
            continue
        tt = r["time_type"]
        when = tt
        if tt == "預約":
            when = f"{r['reserve_slot']} {r['reserve_time']}".strip()
        note = (r["note"] or "").strip()
        note_line = f"\n備註：{note}" if note else ""
        out.append(f"桌號:{r['table_id']}｜{r['hand']}｜{r['amount']}｜缺{miss}｜{when}{note_line}")
    return out

def find_user_by_nick_or_last3(query: str):
    q = query.strip()
    if not q:
        return None
    # last3 digits
    conn = db()
    if re.fullmatch(r"\d{3}", q):
        row = conn.execute("""
            SELECT * FROM users
            WHERE phone LIKE ? OR phone LIKE ?
            ORDER BY created_at DESC LIMIT 1
        """, (f"%{q}", f"%{q}%")).fetchone()
        return row
    # nickname contains
    row = conn.execute("""
        SELECT * FROM users
        WHERE nickname LIKE ?
        ORDER BY created_at DESC LIMIT 1
    """, (f"%{q}%",)).fetchone()
    return row

# ========= Flask Routes =========
@app.route("/")
def health():
    return "OK"

@app.route("/callback", methods=["POST"])
def callback():
    if not handler or not line_bot_api:
        return "LINE not configured", 500

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# ========= Message Handler =========
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    ensure_user(user_id)

    # ID 回傳 userId
    if text.upper() == "ID":
        reply_text(event, f"你的 userId：\n{user_id}", quick_reply=qr_home(user_id))
        return

    # 主選單
    if text == "主選單":
        reply_text(event, "請選擇功能：", quick_reply=qr_main(user_id))
        return

    # 讓一進來打任何字，也能看到主選單按鍵
    if text in ("選單", "menu", "開始"):
        reply_text(event, "請選擇功能：", quick_reply=qr_main(user_id))
        return

    # ====== State handling ======
    st = user_state.get(user_id, None)
    if st:
        mode = st.get("mode")
        data = st.get("data", {})

        # 綁定暱稱
        if mode == "WAIT_NICK":
            if len(text) < 1 or len(text) > 20:
                reply_text(event, "暱稱長度請在 1~20 字。", quick_reply=qr_home(user_id))
                return
            set_nickname(user_id, text)
            user_state.pop(user_id, None)
            reply_text(event, f"✅ 暱稱設定完成：{text}", quick_reply=qr_home(user_id))
            return

        # 綁定手機
        if mode == "WAIT_PHONE":
            phone = text.replace(" ", "")
            if not re.fullmatch(r"09\d{8}", phone):
                reply_text(event, "手機格式錯誤，請輸入 09xxxxxxxx（共10碼）。", quick_reply=qr_home(user_id))
                return
            set_phone(user_id, phone)
            user_state.pop(user_id, None)
            reply_text(event, f"✅ 綁定完成：{phone}", quick_reply=qr_home(user_id))
            return

        # 開桌流程
        if mode == "OPEN_HAND":
            if text not in ("快手", "慢手", "不限"):
                reply_text(event, "請選擇：快手 / 慢手 / 不限", quick_reply=qr_open_hand())
                return
            data["hand"] = text
            user_state[user_id] = {"mode": "OPEN_PEOPLE", "data": data}
            reply_text(event, "請選擇人數桌（2/3/4）：", quick_reply=qr_people_need())
            return

        if mode == "OPEN_PEOPLE":
            if text not in ("2", "3", "4"):
                reply_text(event, "請選擇 2 / 3 / 4", quick_reply=qr_people_need())
                return
            data["people_need"] = int(text)
            user_state[user_id] = {"mode": "OPEN_AMOUNT", "data": data}
            reply_text(event, "請輸入金額（例如：200/50 或 300）：", quick_reply=qr_home(user_id))
            return

        if mode == "OPEN_AMOUNT":
            if len(text) > 40:
                reply_text(event, "金額太長，請重新輸入。", quick_reply=qr_home(user_id))
                return
            data["amount"] = text
            user_state[user_id] = {"mode": "OPEN_TIME_TYPE", "data": data}
            reply_text(event, "請選擇時間/需求：", quick_reply=qr_time_type())
            return

        if mode == "OPEN_TIME_TYPE":
            if text == "現在":
                data["time_type"] = "現在"
                data["reserve_slot"] = ""
                data["reserve_time"] = ""
                user_state[user_id] = {"mode": "OPEN_NOTE", "data": data}
                reply_text(event, "（可選）其他補充備註，沒有就輸入「無」：", quick_reply=qr_home(user_id))
                return
            if text == "預約時間":
                data["time_type"] = "預約"
                user_state[user_id] = {"mode": "OPEN_RESERVE_SLOT", "data": data}
                reply_text(event, "請先選：早上/下午/晚上/半夜", quick_reply=qr_reserve_slot())
                return
            if text == "其他補充":
                data["time_type"] = "其他"
                user_state[user_id] = {"mode": "OPEN_NOTE", "data": data}
                reply_text(event, "請輸入備註內容：", quick_reply=qr_home(user_id))
                return
            reply_text(event, "請選擇：現在 / 預約時間 / 其他補充", quick_reply=qr_time_type())
            return

        if mode == "OPEN_RESERVE_SLOT":
            if text not in ("早上", "下午", "晚上", "半夜"):
                reply_text(event, "請選：早上/下午/晚上/半夜", quick_reply=qr_reserve_slot())
                return
            data["reserve_slot"] = text
            user_state[user_id] = {"mode": "OPEN_RESERVE_TIME", "data": data}
            reply_text(event, "請輸入時間（例如 19:00，24小時制）：", quick_reply=qr_home(user_id))
            return

        if mode == "OPEN_RESERVE_TIME":
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
                reply_text(event, "時間格式錯誤，請輸入例如 19:00", quick_reply=qr_home(user_id))
                return
            data["reserve_time"] = text
            user_state[user_id] = {"mode": "OPEN_NOTE", "data": data}
            reply_text(event, "（可選）其他補充備註，沒有就輸入「無」：", quick_reply=qr_home(user_id))
            return

        if mode == "OPEN_NOTE":
            note = "" if text == "無" else text
            data["note"] = note
            # create table
            ok, msg = can_play(user_id)
            if not ok:
                user_state.pop(user_id, None)
                reply_text(event, msg, quick_reply=qr_home(user_id))
                return
            tid = create_table(
                user_id=user_id,
                people_need=int(data.get("people_need", 4)),
                amount=data.get("amount", ""),
                hand=data.get("hand", "不限"),
                time_type=data.get("time_type", "現在"),
                slot=data.get("reserve_slot", ""),
                rtime=data.get("reserve_time", ""),
                note=data.get("note", "")
            )
            user_state.pop(user_id, None)
            reply_text(event, f"✅ 開桌成功！\n桌號：{tid}\n你已自動加入這桌。", quick_reply=qr_home(user_id))
            return

        # 店家設定（貼連結）
        if mode == "WAIT_GROUP_LINK":
            update_shop_field("group_link", text.strip())
            user_state.pop(user_id, None)
            reply_text(event, "✅ 群設定完成", quick_reply=qr_home(user_id))
            return

        if mode == "WAIT_MAP_LINK":
            update_shop_field("map_link", text.strip())
            user_state.pop(user_id, None)
            reply_text(event, "✅ 地圖設定完成", quick_reply=qr_home(user_id))
            return

        if mode == "WAIT_SHOP_LINE_LINK":
            update_shop_field("shop_line_link", text.strip())
            user_state.pop(user_id, None)
            reply_text(event, "✅ 店家LINE設定完成", quick_reply=qr_home(user_id))
            return

        # 客戶查詢
        if mode == "WAIT_CUSTOMER_SEARCH":
            row = find_user_by_nick_or_last3(text)
            if not row:
                reply_text(event, "找不到此客戶，請輸入「暱稱」或「手機末三碼」。", quick_reply=qr_home(user_id))
                return
            target_id = row["user_id"]
            user_state[user_id] = {"mode": "CUSTOMER_SELECTED", "data": {"target": target_id}}
            frozen_txt = "是" if int(row["frozen"]) == 1 else "否"
            reply_text(
                event,
                f"找到客戶：\n暱稱：{row['nickname'] or '未設定'}\n手機：{row['phone'] or '未綁定'}\n信用分：{row['credit']}\n凍結：{frozen_txt}\n\n請選擇要進行的動作：",
                quick_reply=qr_credit_deduct()
            )
            return

        # 管理員加入（輸入6位碼）
        if mode == "WAIT_ADMIN_CODE":
            code = get_invite_code()
            if text.strip() != code or not re.fullmatch(r"\d{6}", text.strip()):
                reply_text(event, "驗證碼錯誤，請重新輸入6位數驗證碼。", quick_reply=qr_home(user_id))
                return
            add_admin(user_id)
            user_state.pop(user_id, None)
            reply_text(event, "✅ 已加入管理員。", quick_reply=qr_home(user_id))
            return

    # ====== Commands ======
    # 聯絡店家
    if text == "聯絡店家":
        reply_text(event, "☎️ 請選擇：", quick_reply=qr_contact())
        return

    if text == "店家LINE":
        shop = get_shop()
        link = (shop["shop_line_link"] or "").strip() if shop else ""
        if link:
            qr = QuickReply(items=[
                QuickReplyButton(action=URIAction(label="開啟店家LINE", uri=link)),
                QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
            ])
            reply_text(event, "📲 店家LINE：", quick_reply=qr)
        else:
            reply_text(event, "⚠️ 店家LINE尚未設定（請店家到『店家後台 → 店家LINE設定』貼連結）。", quick_reply=qr_home(user_id))
        return

    if text == "地圖":
        shop = get_shop()
        link = (shop["map_link"] or "").strip() if shop else ""
        if link:
            qr = QuickReply(items=[
                QuickReplyButton(action=URIAction(label="開啟地圖", uri=link)),
                QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
            ])
            reply_text(event, "🗺 店家地圖：", quick_reply=qr)
        else:
            reply_text(event, "⚠️ 地圖尚未設定（請店家到『店家後台 → 地圖設定』貼連結）。", quick_reply=qr_home(user_id))
        return

    # 我的
    if text == "我的":
        p = get_user_profile(user_id)
        nick = (p["nickname"] or "").strip() if p else ""
        phone = (p["phone"] or "").strip() if p else ""
        credit = int(p["credit"]) if p else 0
        frozen = int(p["frozen"]) if p else 0
        status = current_table_status(user_id)

        frozen_txt = "（已凍結）" if frozen == 1 else ""
        msg = (
            f"👤 我的資料{frozen_txt}\n"
            f"暱稱：{nick or '未設定'}\n"
            f"手機號碼：{phone or '未綁定'}\n"
            f"配桌狀態：{status}\n"
            f"信用分數：{credit}\n\n"
            f"功能：設定暱稱 / 綁定手機"
        )
        qr = QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="設定暱稱", text="設定暱稱")),
            QuickReplyButton(action=MessageAction(label="綁定手機", text="綁定手機")),
            QuickReplyButton(action=MessageAction(label="🏠 主選單", text="主選單")),
        ])
        reply_text(event, msg, quick_reply=qr)
        return

    if text == "設定暱稱":
        user_state[user_id] = {"mode": "WAIT_NICK", "data": {}}
        reply_text(event, "請輸入暱稱（1~20字）：", quick_reply=qr_home(user_id))
        return

    if text == "綁定手機":
        user_state[user_id] = {"mode": "WAIT_PHONE", "data": {}}
        reply_text(event, "請輸入手機號碼（09xxxxxxxx）：", quick_reply=qr_home(user_id))
        return

    # 開桌/配桌
    if text == "開桌/配桌":
        ok, msg = can_play(user_id)
        if not ok:
            reply_text(event, msg, quick_reply=qr_home(user_id))
            return
        user_state[user_id] = {"mode": "OPEN_HAND", "data": {}}
        reply_text(event, "請選擇：快手 / 慢手 / 不限", quick_reply=qr_open_hand())
        return

    # 桌況查詢
    if text == "桌況查詢":
        reply_text(event, "📊 桌況查詢：", quick_reply=qr_table_status())
        return

    if text.startswith("桌況"):
        parts = text.split()
        missing = None
        if len(parts) >= 2 and parts[1].startswith("缺"):
            try:
                missing = int(parts[1].replace("缺", ""))
            except:
                missing = None

        if len(parts) >= 2 and parts[1] == "全部":
            missing = None

        lines = list_tables(missing=missing)
        if not lines:
            reply_text(event, "目前沒有符合條件的桌。", quick_reply=qr_home(user_id))
            return
        msg = "📊 目前桌況：\n\n" + "\n\n".join(lines)
        reply_text(event, msg, quick_reply=qr_home(user_id))
        return

    # ===== 店家/管理員 =====
    if text == "店家後台":
        # 第一次進入店家後台：若沒有 owner，直接把此人設為 owner + admin
        set_owner_if_empty(user_id)
        if not is_admin(user_id):
            add_admin(user_id)
        reply_text(event, "🏪 店家後台：", quick_reply=qr_shop_admin())
        return

    if text == "群設定":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        user_state[user_id] = {"mode": "WAIT_GROUP_LINK", "data": {}}
        reply_text(event, "請貼上群組邀請連結：", quick_reply=qr_home(user_id))
        return

    if text == "地圖設定":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        user_state[user_id] = {"mode": "WAIT_MAP_LINK", "data": {}}
        reply_text(event, "請貼上 Google Maps 連結：", quick_reply=qr_home(user_id))
        return

    if text == "店家LINE設定":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        user_state[user_id] = {"mode": "WAIT_SHOP_LINE_LINK", "data": {}}
        reply_text(event, "請貼上店家LINE連結（建議 https://line.me/R/ti/p/@xxxx）：", quick_reply=qr_home(user_id))
        return

    if text == "營業/休息":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        shop = get_shop()
        cur = int(shop["is_open"]) if shop else 1
        newv = 0 if cur == 1 else 1
        update_shop_field("is_open", str(newv))
        reply_text(event, f"✅ 已切換為：{'營業中' if newv==1 else '休息中'}", quick_reply=qr_home(user_id))
        return

    if text == "產生管理員碼":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        # 只有 owner 可產生（避免亂發）
        if get_owner_id() != user_id:
            reply_text(event, "只有 owner 可以產生管理員驗證碼。", quick_reply=qr_home(user_id))
            return
        code = f"{random.randint(0, 999999):06d}"
        set_invite_code(code)
        reply_text(event, f"✅ 管理員驗證碼：{code}\n（請新管理員輸入 6 位數驗證碼加入）", quick_reply=qr_home(user_id))
        return

    # 新管理員輸入 6 位碼加入
    if re.fullmatch(r"\d{6}", text):
        code = get_invite_code()
        if code and text == code:
            add_admin(user_id)
            reply_text(event, "✅ 已加入管理員。", quick_reply=qr_home(user_id))
        else:
            reply_text(event, "驗證碼錯誤。", quick_reply=qr_home(user_id))
        return

    if text == "客戶資訊":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        user_state[user_id] = {"mode": "WAIT_CUSTOMER_SEARCH", "data": {}}
        reply_text(event, "請輸入客戶「暱稱」或「手機末三碼」查詢：", quick_reply=qr_home(user_id))
        return

    # 扣分 / 凍結切換（需先選到客戶）
    if text.startswith("扣分") or text == "凍結切換":
        if not is_admin(user_id):
            reply_text(event, "你沒有店家權限。", quick_reply=qr_home(user_id))
            return
        st = user_state.get(user_id, {})
        if st.get("mode") != "CUSTOMER_SELECTED":
            reply_text(event, "請先到「客戶資訊」查詢並選定客戶。", quick_reply=qr_home(user_id))
            return
        target = st.get("data", {}).get("target", "")
        if not target:
            reply_text(event, "目標客戶不存在，請重新查詢。", quick_reply=qr_home(user_id))
            return

        if text == "凍結切換":
            frozen = user_is_frozen(target)
            set_frozen(target, 0 if frozen else 1)
            p = get_user_profile(target)
            reply_text(event, f"✅ 已{'解除凍結' if frozen else '凍結'}此客戶。\n目前信用分：{p['credit']}", quick_reply=qr_credit_deduct())
            return

        # 扣分原因
        reason = text.replace("扣分", "").strip()
        mapping = {
            "放鳥": -20,
            "取消": -5,
            "遲到": -10,
            "玩家檢舉": -15
        }
        if reason not in mapping:
            reply_text(event, "扣分原因不正確。", quick_reply=qr_credit_deduct())
            return
        new_credit = deduct_credit(target, mapping[reason])
        p = get_user_profile(target)
        frozen_txt = "（已凍結）" if int(p["frozen"]) == 1 else ""
        reply_text(
            event,
            f"✅ 已扣分：{reason} {mapping[reason]}\n目前信用分：{new_credit}{frozen_txt}",
            quick_reply=qr_credit_deduct()
        )
        return

    # ===== Default: 回主選單（不丟返回） =====
    reply_text(event, "請選擇功能：", quick_reply=qr_main(user_id))


if __name__ == "__main__":
    # Render / production 會用 gunicorn 啟動，這段只給本機測試用
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
