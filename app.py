import os, sqlite3, threading, time, re, random
from datetime import datetime, timedelta
from flask import Flask, request, abort, g, has_request_context
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction, URIAction,
    PostbackEvent, PostbackAction
)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LIFF_ID = os.getenv("LIFF_ID", "").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    # 讓 Render log 更好讀（仍會啟動，但 LineBotApi 會在呼叫時失敗）
    print("WARNING: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET not set")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SYSTEM_GROUP_LINK = "https://line.me/R/ti/g/一般玩家群"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}

COUNTDOWN_READY = 30  # ✅ 30 秒確認


def get_db():
    if "db" not in g:
        db = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()



def add_column_if_missing(db, table: str, column: str, coldef: str):
    """SQLite: add column if it doesn't exist (safe on existing DB)."""
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        db.commit()

def init_db():
    db = get_db()

    db.execute("""
    CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT PRIMARY KEY,
        people INT,
        shop_id TEXT,
        amount TEXT,
        status TEXT,
        expire REAL,
        table_id TEXT,
        table_index INT,
        hand TEXT,
        is_creator INT DEFAULT 0,
        sched_type TEXT,
        sched_period TEXT,
        sched_time TEXT,
        note TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS tables(
        id TEXT PRIMARY KEY,
        shop_id TEXT,
        amount TEXT,
        table_index INT,
        created REAL,
        r20 INT DEFAULT 0,
        r10 INT DEFAULT 0,
        hand TEXT,
        sched_type TEXT,
        sched_period TEXT,
        sched_time TEXT,
        note TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT PRIMARY KEY,
        name TEXT,
        open INT,
        approved INT,
        group_link TEXT,
        owner_id TEXT,
        partner_map TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS nicknames(
        user_id TEXT PRIMARY KEY,
        nickname TEXT
    )
    """)


    # ✅ 綁定手機號（用 user_id 當主鍵）
    db.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id TEXT PRIMARY KEY,
        phone TEXT,
        created REAL,
        updated REAL
    )
    """)

    # 使用者流程暫存（避免多進程/重啟造成記憶體 user_state 遺失）
    db.execute("""
    CREATE TABLE IF NOT EXISTS session_state(
        user_id TEXT PRIMARY KEY,
        shop_id TEXT,
        amount TEXT,
        hand TEXT,
        action TEXT,
        sched_type TEXT,
        sched_period TEXT,
        sched_time TEXT,
        note TEXT,
        pending_people INT,
        pending_amount TEXT,
        updated REAL
    )
    """)


    # ✅ 店家管理員（單店版）
    db.execute("""
    CREATE TABLE IF NOT EXISTS shop_admins(
        user_id TEXT PRIMARY KEY,
        role TEXT,
        created REAL
    )
    """)

    # ✅ 6位數邀請碼（新增管理員）
    db.execute("""
    CREATE TABLE IF NOT EXISTS invite_codes(
        code TEXT PRIMARY KEY,
        remaining INT,
        expires REAL,
        created REAL,
        created_by TEXT
    )
    """)

    # ✅ 店家設定（例如扣分設定、群連結等）
    db.execute("""
    CREATE TABLE IF NOT EXISTS shop_settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # ✅ 信用分 / 黑名單（單店版）
    db.execute("""
    CREATE TABLE IF NOT EXISTS user_scores(
        user_id TEXT PRIMARY KEY,
        score INT DEFAULT 100,
        no_show INT DEFAULT 0,
        blacklisted INT DEFAULT 0,
        is_frozen INT DEFAULT 0,
        frozen_reason TEXT,
        frozen_at REAL,
        updated REAL
    )
    """)
    # ✅ 信用分規則（固定事件）
    db.execute("""
    CREATE TABLE IF NOT EXISTS score_rules(
        event TEXT PRIMARY KEY,
        delta INT NOT NULL
    )
    """)

    # ✅ 信用分異動紀錄
    db.execute("""
    CREATE TABLE IF NOT EXISTS score_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        delta INT,
        event TEXT,
        note TEXT,
        operator_id TEXT,
        created REAL
    )
    """)

    # ✅ 成桌報表事件（用於報表統計）
    db.execute("""
    CREATE TABLE IF NOT EXISTS report_tables(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_id TEXT,
        shop_id TEXT,
        amount TEXT,
        table_index INT,
        success_time REAL
    )
    """)

    # ✅ 營業區間（開始營業→打烊）
    db.execute("""
    CREATE TABLE IF NOT EXISTS business_sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL,
        ended_at REAL,
        started_by TEXT,
        ended_by TEXT
    )
    """)

    
    # ---- 兼容舊資料庫：補欄位 ----
    add_column_if_missing(db, "match_users", "hand", "hand TEXT")
    add_column_if_missing(db, "tables", "hand", "hand TEXT")
    add_column_if_missing(db, "session_state", "hand", "hand TEXT")
    add_column_if_missing(db, "session_state", "action", "action TEXT")
    add_column_if_missing(db, "user_scores", "is_frozen", "is_frozen INT DEFAULT 0")
    add_column_if_missing(db, "user_scores", "frozen_reason", "frozen_reason TEXT")
    add_column_if_missing(db, "user_scores", "frozen_at", "frozen_at REAL")
    add_column_if_missing(db, "match_users", "is_creator", "is_creator INT DEFAULT 0")
    add_column_if_missing(db, "match_users", "sched_type", "sched_type TEXT")
    add_column_if_missing(db, "match_users", "sched_period", "sched_period TEXT")
    add_column_if_missing(db, "match_users", "sched_time", "sched_time TEXT")
    add_column_if_missing(db, "match_users", "note", "note TEXT")
    add_column_if_missing(db, "tables", "sched_type", "sched_type TEXT")
    add_column_if_missing(db, "tables", "sched_period", "sched_period TEXT")
    add_column_if_missing(db, "tables", "sched_time", "sched_time TEXT")
    add_column_if_missing(db, "tables", "note", "note TEXT")
    add_column_if_missing(db, "session_state", "sched_type", "sched_type TEXT")
    add_column_if_missing(db, "session_state", "sched_period", "sched_period TEXT")
    add_column_if_missing(db, "session_state", "sched_time", "sched_time TEXT")
    add_column_if_missing(db, "session_state", "note", "note TEXT")
    add_column_if_missing(db, "session_state", "pending_people", "pending_people INT")
    add_column_if_missing(db, "session_state", "pending_amount", "pending_amount TEXT")

    # ---- 信用分固定事件預設規則（可由店家後台修改）----
        # ---- 信用分固定扣分（僅供店家手動扣除）----
    defaults = {
        "放鳥": -20,
        "取消": -5,
        "遲到": -10,
        "玩家檢舉": -15,
    }
    for ev, dv in defaults.items():
        db.execute("INSERT OR IGNORE INTO score_rules(event, delta) VALUES(?,?)", (ev, dv))
    db.execute("INSERT OR IGNORE INTO shop_settings(key, value) VALUES('blacklist_threshold', '60')")
    db.commit()

# 預設把硬編的 ADMIN_IDS 視為系統管理員（仍可進入店家管理介面）
    now = time.time()
    for aid in ADMIN_IDS:
        db.execute(
            "INSERT OR IGNORE INTO shop_admins(user_id, role, created) VALUES(?,?,?)",
            (aid, "system", now)
        )
    db.execute("INSERT OR IGNORE INTO shop_settings(key, value) VALUES('blacklist_threshold', '60')")
    db.commit()



def ss_set(db, user_id, shop_id=None, amount=None, hand=None, action=None, sched_type=None, sched_period=None, sched_time=None, note=None, pending_people=None, pending_amount=None):
    now = time.time()
    row = db.execute("SELECT shop_id, amount, hand, action, sched_type, sched_period, sched_time, note, pending_people, pending_amount FROM session_state WHERE user_id=?", (user_id,)).fetchone()
    cur = dict(row) if row else {}
    def pick(key, val):
        return cur.get(key) if val is None else val
    shop_id = pick("shop_id", shop_id)
    amount = pick("amount", amount)
    hand = pick("hand", hand)
    action = pick("action", action)
    sched_type = pick("sched_type", sched_type)
    sched_period = pick("sched_period", sched_period)
    sched_time = pick("sched_time", sched_time)
    note = pick("note", note)
    pending_people = pick("pending_people", pending_people)
    pending_amount = pick("pending_amount", pending_amount)

    db.execute(
        "INSERT OR REPLACE INTO session_state(user_id, shop_id, amount, hand, action, sched_type, sched_period, sched_time, note, pending_people, pending_amount, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, shop_id, amount, hand, action, sched_type, sched_period, sched_time, note, pending_people, pending_amount, now)
    )
    db.commit()


def ss_get(db, user_id):
    row = db.execute("SELECT shop_id, amount, hand, action FROM session_state WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return (None, None, None, None)
    return (row["shop_id"], row["amount"], row["hand"], row["action"])

def ss_get_all(db, user_id):
    row = db.execute("SELECT shop_id, amount, hand, action, sched_type, sched_period, sched_time, note, pending_people, pending_amount FROM session_state WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else {}


def ss_clear(db, user_id):
    db.execute("DELETE FROM session_state WHERE user_id=?", (user_id,))
    db.commit()




# ====== 共用 QuickReply（每個選單都要有：返回 / 回主選單） ======
def make_qr(items=None, include_back=True, include_home=True):
    """Build LINE QuickReply with optional Back/Home buttons.
    items: list[QuickReplyButton]
    """
    buttons = list(items) if items else []
    if include_back and not any(getattr(getattr(b, "action", None), "text", None) == "返回" for b in buttons):
        buttons.append(QuickReplyButton(action=MessageAction(label="↩ 返回", text="返回")))
    if include_home and not any(getattr(getattr(b, "action", None), "text", None) == "選單" for b in buttons):
        buttons.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
    return QuickReply(items=buttons)

def back_menu():
    return make_qr([])

def confirm_menu():
    return make_qr([
        QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
        QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
    ])

# ====== 綁定手機號（簡化版） ======
PHONE_RE = re.compile(r"^(?:\+?886)?0?9\d{8}$")

def normalize_phone(p: str):
    p = (p or "").strip().replace(" ", "").replace("-", "")
    if p.startswith("+886"):
        p = "0" + p[4:]
    elif p.startswith("886"):
        p = "0" + p[3:]
    if len(p) == 10 and p.startswith("09"):
        return p
    return None

def is_phone_bound(db, user_id: str) -> bool:
    row = db.execute("SELECT phone FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and (row["phone"] or "").strip())

def require_phone_bound(event, db, user_id: str) -> bool:
    if is_phone_bound(db, user_id):
        return False
    user_state[user_id] = {"mode": "bind_phone"}
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            "📱 請先綁定手機號碼（例：09xxxxxxxx）\n\n輸入手機後即可使用所有功能。",
            quick_reply=back_menu()
        )
    )
    return True

def is_frozen(db, user_id: str) -> bool:
    row = db.execute("SELECT is_frozen FROM user_scores WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and int(row["is_frozen"] or 0) == 1)

def require_not_frozen(event, db, user_id: str) -> bool:
    if not is_frozen(db, user_id):
        return False
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("⛔ 你的帳號已被凍結（信用分不足或被店家凍結）。\n請聯絡店家解除後再使用。", quick_reply=back_menu())
    )
    return True

def table_quick_reply(db, table_id):
    # ✅ 以「倒數時間 expire」為準：只要未到期，就固定顯示加入/放棄，避免按鈕閃退/被覆蓋
    if not table_id:
        return back_menu()

    erow = db.execute(
        "SELECT MIN(expire) AS ex FROM match_users WHERE table_id=? AND expire IS NOT NULL",
        (table_id,)
    ).fetchone()

    if erow and erow["ex"]:
        remain = int(erow["ex"] - time.time())
        if remain > 0:
            return confirm_menu()

    return back_menu()



def get_nickname(db, user_id):
    row = db.execute("SELECT nickname FROM nicknames WHERE user_id=?", (user_id,)).fetchone()
    return row["nickname"] if row and row["nickname"] else None


def display_name(db, user_id):
    nk = get_nickname(db, user_id)
    if nk:
        return nk
    # 若未設定暱稱，用「玩家XXXX」末4碼
    return f"玩家{user_id[-4:]}"


def is_admin(db, user_id: str) -> bool:
    if user_id in ADMIN_IDS:
        return True
    row = db.execute("SELECT 1 FROM shop_admins WHERE user_id=?", (user_id,)).fetchone()
    return bool(row)


# ===== 信用分 / 凍結 =====
SCORE_EVENTS = ["放鳥", "取消", "遲到", "玩家檢舉"]

def ensure_user_score(db, user_id: str):
    db.execute("INSERT OR IGNORE INTO user_scores(user_id, score, no_show, blacklisted, is_frozen, updated) VALUES(?,100,0,0,0,?)",
               (user_id, time.time()))
    db.commit()

def get_phone_last3(db, user_id: str) -> str:
    row = db.execute("SELECT phone FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row["phone"]:
        return "---"
    p = row["phone"]
    return p[-3:] if len(p) >= 3 else p

def get_nickname_or_default(db, user_id: str) -> str:
    nk = get_nickname(db, user_id)
    if nk:
        return nk
    return default_nickname(user_id)

def get_score_row(db, user_id: str):
    ensure_user_score(db, user_id)
    return db.execute("SELECT score, is_frozen, frozen_reason FROM user_scores WHERE user_id=?", (user_id,)).fetchone()

def get_rule_delta(db, event: str) -> int:
    row = db.execute("SELECT delta FROM score_rules WHERE event=?", (event,)).fetchone()
    if not row:
        return 0
    return int(row["delta"])

def log_score(db, user_id: str, delta: int, event: str, operator_id: str = None, note: str = None):
    db.execute(
        "INSERT INTO score_logs(user_id, delta, event, note, operator_id, created) VALUES(?,?,?,?,?,?)",
        (user_id, int(delta), event, note, operator_id, time.time())
    )

def apply_score_change(db, user_id: str, delta: int, event: str, operator_id: str = None, note: str = None):
    """Apply score delta, auto-freeze if score < threshold."""
    ensure_user_score(db, user_id)
    row = db.execute("SELECT score, is_frozen FROM user_scores WHERE user_id=?", (user_id,)).fetchone()
    score = int(row["score"]) + int(delta)
    # clamp
    score = max(0, min(200, score))
    th_row = db.execute("SELECT value FROM shop_settings WHERE key='blacklist_threshold'").fetchone()
    threshold = int(th_row["value"]) if th_row else 60

    is_frozen = int(row["is_frozen"] or 0)
    frozen_reason = None
    frozen_at = None
    if score < threshold:
        is_frozen = 1
        frozen_reason = "score_below_threshold"
        frozen_at = time.time()

    db.execute(
        "UPDATE user_scores SET score=?, is_frozen=?, frozen_reason=COALESCE(?, frozen_reason), frozen_at=COALESCE(?, frozen_at), updated=? WHERE user_id=?",
        (score, is_frozen, frozen_reason, frozen_at, time.time(), user_id)
    )
    log_score(db, user_id, delta, event, operator_id, note)
    db.commit()
    return score, is_frozen

def freeze_user(db, user_id: str, operator_id: str = None, reason: str = "manual"):
    ensure_user_score(db, user_id)
    db.execute("UPDATE user_scores SET is_frozen=1, frozen_reason=?, frozen_at=?, updated=? WHERE user_id=?",
               (reason, time.time(), time.time(), user_id))
    log_score(db, user_id, 0, "凍結", operator_id, reason)
    db.commit()

def unfreeze_user(db, user_id: str, operator_id: str = None, set_score_to_threshold: bool = True):
    ensure_user_score(db, user_id)
    th_row = db.execute("SELECT value FROM shop_settings WHERE key='blacklist_threshold'").fetchone()
    threshold = int(th_row["value"]) if th_row else 60
    row = db.execute("SELECT score FROM user_scores WHERE user_id=?", (user_id,)).fetchone()
    score = int(row["score"]) if row else 100
    if set_score_to_threshold and score < threshold:
        score = threshold
    db.execute("UPDATE user_scores SET is_frozen=0, frozen_reason=NULL, frozen_at=NULL, score=?, updated=? WHERE user_id=?",
               (score, time.time(), user_id))
    log_score(db, user_id, 0, "解除凍結", operator_id, None)
    db.commit()
    return score

def main_menu(user_id=None):
    # 程式化主選單（可搭配圖文選單 / Rich Menu 使用）
    items = [
        QuickReplyButton(action=MessageAction(label="🀄 開桌/配桌", text="開桌/配桌")),
        QuickReplyButton(action=MessageAction(label="📋 桌況查詢", text="桌況查詢")),
        QuickReplyButton(action=MessageAction(label="👤 我的", text="我的")),
        QuickReplyButton(action=MessageAction(label="☎️ 店家聯絡", text="店家聯絡")),
        QuickReplyButton(action=MessageAction(label="🗺 地圖", text="地圖")),
    ]
    try:
        db = get_db()
        if user_id and is_admin(db, user_id):
            # 店家專用：客戶資訊（含報表）/ 店家後台
            items.insert(3, QuickReplyButton(action=MessageAction(label="📊 客戶資訊", text="客戶資訊")))
            items.insert(4, QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")))
    except Exception:
        pass
    return TextSendMessage("請選擇功能", quick_reply=make_qr(items))
def get_group_link(db, shop_id):
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    if row and (row["group_link"] or "").strip():
        return row["group_link"].strip()
    return SYSTEM_GROUP_LINK




def parse_hhmm(s: str):
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    hh = int(m.group(1)); mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"

def build_schedule_text(sched_type, sched_period, sched_time):
    if sched_type == "now":
        return "現在"
    if sched_type == "reserve":
        if sched_period and sched_time:
            return f"預約 {sched_period} {sched_time}"
        return "預約"
    if sched_type == "note":
        return "其他補充"
    return ""

def public_base_url():
    """Base URL used to build external links.
    - Prefer PUBLIC_BASE_URL env var (for custom domains)
    - Fallback to current request host (works on Render without extra settings)
    """
    env = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env:
        return env
    if has_request_context():
        # request.url_root includes trailing slash
        return request.url_root.rstrip("/")
    return ""

def liff_status_url(missing=None):
    """HTTP URL to LIFF status page (works in any browser)."""
    base = public_base_url()
    if not base:
        return None
    if missing is None:
        return f"{base}/liff/status"
    return f"{base}/liff/status?missing={missing}"

def liff_deeplink(missing=None):
    """Prefer opening inside LINE via LIFF deep link when LIFF_ID is set.
    Falls back to the normal https URL if LIFF_ID isn't configured.
    """
    if LIFF_ID:
        if missing is None:
            return f"https://liff.line.me/{LIFF_ID}"
        return f"https://liff.line.me/{LIFF_ID}?missing={missing}"
    return liff_status_url(missing)

def waiting_summary(db, missing_filter=None):
    """Summary of waiting pools. missing_filter: 1/2/3 or None for all."""
    rows = db.execute("""
         SELECT 
            m.shop_id, s.name AS shop_name, m.amount, COALESCE(m.hand,'不限') AS hand,
            COALESCE(SUM(m.people),0) AS total_people,
            (SELECT note FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS note,
            (SELECT sched_type FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS sched_type,
            (SELECT sched_period FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS sched_period,
            (SELECT sched_time FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS sched_time
         FROM match_users m
         LEFT JOIN shops s ON m.shop_id=s.shop_id
         WHERE m.status='waiting'
         GROUP BY m.shop_id, m.amount, COALESCE(m.hand,'不限')
         ORDER BY total_people DESC
     """).fetchall()
    lines = []
    for r in rows:
        total = int(r["total_people"] or 0)
        if total <= 0:
            continue
        modv = total % 4
        missing = 0 if modv == 0 else 4 - modv
        if missing_filter is not None and missing != missing_filter:
            continue
        shop = r["shop_name"] or "店家"
        hand = r["hand"] or "不限"
        tag = "✅ 可成桌" if missing == 0 else f"缺{missing}"
        sched = build_schedule_text(r["sched_type"], r["sched_period"], r["sched_time"])
        extra = ""
        if sched:
            extra += f"｜{sched}"
        note = (r["note"] or "").strip()
        if note:
            extra += f"｜備註:{note[:20]}"
        lines.append(f"🏪{shop}｜{r['amount']}｜{hand}｜等待:{total}人｜{tag}{extra}")
    if not lines:
        return "目前沒有待配桌的隊列。"
    header = "📋 待配桌總覽"
    if missing_filter is not None:
        header += f"（缺{missing_filter}）"
    return header + "\n\n" + "\n".join(lines[:30])

def get_next_table_index(db, shop_id):
    # ✅ 每月從 1 重新編號：只統計「本月建立」的桌號
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    row = db.execute(
        "SELECT MAX(table_index) AS mx FROM tables WHERE shop_id=? AND created>=?",
        (shop_id, month_start)
    ).fetchone()
    return (row["mx"] or 0) + 1


def get_table_users(db, table_id):
    rows = db.execute("SELECT user_id FROM match_users WHERE table_id=?", (table_id,)).fetchall()
    return [r["user_id"] for r in rows]


def build_table_status_msg(db, table_id, title="🀄 桌況更新"):
    rows = db.execute("""
        SELECT user_id, status, people
        FROM match_users
        WHERE table_id=?
        ORDER BY rowid
    """, (table_id,)).fetchall()

    if not rows:
        return None

    total = sum(int(r["people"]) for r in rows)
    confirmed = sum(1 for r in rows if r["status"] == "confirmed")

    msg = f"{title}\n\n"
    msg += f"👥 人數：{total} / 4\n"
    msg += f"✅ 已確認：{confirmed} / {len(rows)}\n\n"

    for i, r in enumerate(rows, 1):
        st = r["status"]
        if st == "ready":
            icon = "📩"
            st_label = "待確認"
        elif st == "confirmed":
            icon = "✅"
            st_label = "已加入"
        else:
            icon = "⏳"
            st_label = st

        msg += f"{i}. {display_name(db, r['user_id'])}｜{int(r['people'])}人 {icon} {st_label}\n"

    return msg.strip()


def push_table(table_id, title="🀄 桌況更新"):
    with app.app_context():
        db = get_db()
        msg = build_table_status_msg(db, table_id, title)
        if not msg:
            return
        for uid in get_table_users(db, table_id):
            try:
                line_bot_api.push_message(uid, TextSendMessage(msg, quick_reply=table_quick_reply(db, table_id)))
            except Exception as e:
                print("push_table error:", e)


def notify_table(table_id, text):
    with app.app_context():
        db = get_db()
        for uid in get_table_users(db, table_id):
            try:
                line_bot_api.push_message(uid, TextSendMessage(text, quick_reply=table_quick_reply(db, table_id)))
            except Exception as e:
                print("notify_table error:", e)


def try_make_table(shop_id, amount, hand, reply_token=None, trigger_user_id=None):
    db = get_db()
    rows = db.execute("""
        SELECT user_id, people FROM match_users
        WHERE shop_id=? AND amount=? AND status='waiting' AND ( ?='不限' OR hand=? OR hand='不限' OR hand IS NULL )
        ORDER BY rowid
    """, (shop_id, amount, hand, hand)).fetchall()

    total = 0
    selected = []
    for r in rows:
        uid = r["user_id"]
        p = int(r["people"])
        if total + p > 4:
            continue
        total += p
        selected.append((uid, p))
        if total == 4:
            break

    if total != 4:
        return None

    table_id = f"{shop_id}_{int(time.time()*1000)}"
    expire = time.time() + COUNTDOWN_READY
    table_index = get_next_table_index(db, shop_id)

    c = db.execute("""SELECT sched_type, sched_period, sched_time, note
                       FROM match_users
                       WHERE shop_id=? AND amount=? AND status='waiting' AND is_creator=1
                       ORDER BY rowid DESC LIMIT 1""", (shop_id, amount)).fetchone()

    db.execute(
        "INSERT INTO tables(id, shop_id, amount, table_index, created, r20, r10, hand, sched_type, sched_period, sched_time, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (table_id, shop_id, amount, table_index, time.time(), 0, 0, hand,
          (c["sched_type"] if c else None),
          (c["sched_period"] if c else None),
          (c["sched_time"] if c else None),
          (c["note"] if c else None))
    )

    for uid, _p in selected:
        db.execute("""
            UPDATE match_users
            SET status='ready', expire=?, table_id=?, table_index=?
            WHERE user_id=?
        """, (expire, table_id, table_index, uid))

    db.commit()

    msg = (
        "🎉 成桌確認\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n\n"
        f"⏱ {COUNTDOWN_READY} 秒內未確認視同放棄"
    )

    qr = make_qr([
        QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
        QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
        QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
    ])

    for uid, _p in selected:
        try:
            if reply_token and trigger_user_id and uid == trigger_user_id:
                line_bot_api.reply_message(reply_token, TextSendMessage(msg, quick_reply=qr))
            else:
                line_bot_api.push_message(uid, TextSendMessage(msg, quick_reply=qr))
        except Exception as e:
            print("confirm push error:", e)

    push_table(table_id, "🪑 桌子成立（等待確認）")
    return table_id


def finalize_success(table_id):
    db = get_db()
    trow = db.execute("SELECT shop_id, amount, table_index, sched_type, sched_period, sched_time, note FROM tables WHERE id=?", (table_id,)).fetchone()
    if not trow:
        return

    group = get_group_link(db, trow["shop_id"])
    table_index = trow["table_index"]
    amount = trow["amount"]

    # ✅ 記錄成桌事件（供報表使用）
    db.execute(
        "INSERT INTO report_tables(table_id, shop_id, amount, table_index, success_time) VALUES(?,?,?,?,?)",
        (table_id, trow["shop_id"], amount, table_index, time.time())
    )
    db.commit()

    rows = db.execute("SELECT user_id FROM match_users WHERE table_id=? AND status='confirmed'", (table_id,)).fetchall()
    for r in rows:
        uid = r["user_id"]
        try:
            line_bot_api.push_message(uid, TextSendMessage(
                "🎉 配桌成功\n\n"
                f"🪑 桌號：{table_index}\n"
                f"💰 金額：{amount}\n"
                + (f"🕒 時間：{build_schedule_text(trow['sched_type'], trow['sched_period'], trow['sched_time'])}\n" if (trow and trow['sched_type']) else "")
                + (f"📝 備註：{(trow['note'] or '').strip()}\n" if (trow and (trow['note'] or '').strip()) else "")
                + f"\n🔗 群組連結：{group}\n"
                + "🔔 進群後請回報桌號",
                quick_reply=back_menu()
            ))
        except Exception as e:
            print("success push error:", e)

    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()


def handle_abandon(user_id):
    db = get_db()
    row = db.execute("SELECT shop_id, amount, table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None

    shop_id = row["shop_id"]
    amount = row["amount"]
    table_id = row["table_id"]

    # 刪除放棄者
    db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
    db.commit()

    if table_id:
        # 有在確認桌：其餘玩家回到等待中，桌子作廢，繼續等待補人
        db.execute("UPDATE match_users SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL WHERE table_id=?", (table_id,))
        db.execute("DELETE FROM tables WHERE id=?", (table_id,))
        db.commit()

        notify_table(table_id, "⚠ 有玩家放棄，已回到等待池，繼續配桌中…")
        # 可能剛好補滿再成桌
        try_make_table(shop_id, amount, '不限')

    return (shop_id, amount)


def timeout_checker():
    while True:
        try:
            with app.app_context():
                db = get_db()
                now = time.time()

                # 先做提醒（20秒、10秒）
                tables = db.execute("SELECT * FROM tables").fetchall()
                for t in tables:
                    table_id = t["id"]
                    # 找該桌 expire（取任一 ready 的 expire）
                    erow = db.execute("SELECT MIN(expire) AS ex FROM match_users WHERE table_id=? AND status='ready'", (table_id,)).fetchone()
                    if not erow or not erow["ex"]:
                        continue
                    remain = int(erow["ex"] - now)

                    if remain <= 20 and remain > 10 and t["r20"] == 0:
                        db.execute("UPDATE tables SET r20=1 WHERE id=?", (table_id,))
                        db.commit()
                        notify_table(table_id, "⏳ 剩餘 20 秒未確認視同放棄")
                    if remain <= 10 and remain > 0 and t["r10"] == 0:
                        db.execute("UPDATE tables SET r10=1 WHERE id=?", (table_id,))
                        db.commit()
                        notify_table(table_id, "⏳ 剩餘 10 秒未確認視同放棄")

                # 到期處理：ready 到期 -> 視同放棄（只退未確認者）
                expired = db.execute("""
                    SELECT user_id, table_id FROM match_users
                    WHERE status='ready' AND expire IS NOT NULL AND expire < ?
                """, (now,)).fetchall()

                # 用 table_id 分組處理，避免重複
                handled_tables = set()
                for r in expired:
                    table_id = r["table_id"]
                    if not table_id or table_id in handled_tables:
                        continue
                    handled_tables.add(table_id)

                    # 未確認者全部放棄
                    unconfirmed = db.execute("SELECT user_id FROM match_users WHERE table_id=? AND status='ready'", (table_id,)).fetchall()
                    for u in unconfirmed:
                        db.execute("DELETE FROM match_users WHERE user_id=?", (u["user_id"],))

                    # 其餘玩家回等待池
                    db.execute("UPDATE match_users SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL WHERE table_id=?", (table_id,))
                    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
                    db.commit()

                    notify_table(table_id, "⛔ 超過 30 秒未確認，視同放棄，已取消本次成桌並回到等待池")
                    # 嘗試再成桌
                    # 取 shop/amount 用任一 match_users waiting
                    w = db.execute("SELECT shop_id, amount FROM match_users WHERE status='waiting' LIMIT 1").fetchone()
                    if w:
                        try_make_table(w["shop_id"], w["amount"], '快手'); try_make_table(w["shop_id"], w["amount"], '慢手'); try_make_table(w["shop_id"], w["amount"], '不限')

        except Exception as e:
            print("timeout_checker error:", e)

        time.sleep(2)


threading.Thread(target=timeout_checker, daemon=True).start()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(PostbackEvent)
def handle_postback(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    data = (event.postback.data or "").strip()

    # 選店家：使用 Postback，避免聊天室顯示「店家:shop_id」
    if data.startswith("shop="):
        sid = data.split("=", 1)[1].strip()
        user_state[user_id] = {"mode": "wait_amount", "shop_id": sid}
        ss_set(db, user_id, shop_id=sid, amount=None)
        items = [
            QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
            QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
            QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
            QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=make_qr(items)))
        return


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    # ===== 取得 userId（交付店家時使用一次）=====
    if text == "ID":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"你的 userId：{user_id}", quick_reply=back_menu()))
        return


    # ===== 新增管理員：輸入 6 位驗證碼加入 =====
    if re.fullmatch(r"\d{6}", text):
        row = db.execute("SELECT remaining, expires FROM invite_codes WHERE code=?", (text,)).fetchone()
        if row and int(row["remaining"] or 0) > 0 and float(row["expires"] or 0) > time.time():
            db.execute("INSERT OR IGNORE INTO shop_admins(user_id, role, created) VALUES(?,?,?)", (user_id, "admin", time.time()))
            db.execute("UPDATE invite_codes SET remaining=remaining-1 WHERE code=?", (text,))
            db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 你已成為店家管理員。", quick_reply=back_menu()))
            return



    # ===== 返回（上一層）=====
    if text == "返回":
        mode = user_state.get(user_id, {}).get("mode")
        # 常用返回路徑（不足時退回主選單）
        if mode in ("set_group",):
            # 回到店家合作頁（若有店家）
            row = db.execute("SELECT shop_id, name, approved, open FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
            if row and int(row["approved"] or 0) == 1:
                status = "🟢 營業中" if int(row["open"] or 0) == 1 else "🔴 未營業"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    f"🏪 {row['name']}\n{status}",
                    quick_reply=make_qr([
                        QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                        QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                        QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
                    ])
                ))
                return
        # 配桌流程返回
        if mode in ("wait_amount",):
            # 回到選店家
            ss_clear(db, user_id)
            shops = db.execute("SELECT shop_id, name FROM shops WHERE open=1 AND approved=1 ORDER BY rowid DESC").fetchall()
            if not shops:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
                return
            items = [QuickReplyButton(action=PostbackAction(label=(s["name"] or "")[:20], data=f"shop={s['shop_id']}")) for s in shops]
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇店家", quick_reply=make_qr(items)))
            return
        if mode in ("wait_people",):
            # 回到選金額（讀 session_state）
            sid, _amt, _hand, _act = ss_get(db, user_id)
            if not sid:
                line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                return
            items = [
                QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
                QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
                QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
                QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            ]
            user_state[user_id] = {"mode": "wait_amount", "shop_id": sid}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=make_qr(items)))
            return

        # 預設：回主選單
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ===== 綁定手機流程 =====
    if user_state.get(user_id, {}).get("mode") == "bind_phone":
        phone = normalize_phone(text)
        if not phone:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("格式不正確，請輸入手機號碼（例：09xxxxxxxx）", quick_reply=back_menu()))
            return
        now = time.time()
        db.execute("INSERT OR REPLACE INTO users(user_id, phone, created, updated) VALUES(?,?,COALESCE((SELECT created FROM users WHERE user_id=?), ?), ?)",
                   (user_id, phone, user_id, now, now))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已綁定手機：{phone}", quick_reply=back_menu()))
        return

    # ===== 自訂報表區間輸入 =====
    if user_state.get(user_id, {}).get("mode") == "report_custom":
        # 支援：2/3 10:00-2/4 13:00 或 10:00-13:00 或 2/3
        q = text.strip()
        now = datetime.now()
        def parse_dt(s):
            s=s.strip()
            # formats: M/D HH:MM or YYYY/MM/DD HH:MM or M/D
            m=re.match(r'^(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$', s)
            if not m:
                return None
            y = int(m.group(1)) if m.group(1) else now.year
            mo = int(m.group(2)); da = int(m.group(3))
            hh = int(m.group(4)) if m.group(4) else 0
            mi = int(m.group(5)) if m.group(5) else 0
            return datetime(y, mo, da, hh, mi)

        start_dt=None; end_dt=None
        # date range with dash
        m=re.match(r'^(.*?)-(.*)$', q)
        if m:
            a=m.group(1).strip(); b=m.group(2).strip()
            # time-only?
            if re.match(r'^\d{1,2}:\d{2}$', a) and re.match(r'^\d{1,2}:\d{2}$', b):
                start_dt=datetime(now.year, now.month, now.day, int(a.split(":")[0]), int(a.split(":")[1]))
                end_dt=datetime(now.year, now.month, now.day, int(b.split(":")[0]), int(b.split(":")[1]))
            else:
                start_dt=parse_dt(a)
                end_dt=parse_dt(b)
        else:
            # single date
            one=parse_dt(q)
            if one:
                start_dt=one
                end_dt=one + timedelta(days=1)

        if not start_dt or not end_dt or end_dt <= start_dt:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("格式看不懂，請再輸入一次，例如：2/3 10:00-2/4 13:00 或 10:00-13:00", quick_reply=back_menu()))
            return

        rows = db.execute(
            "SELECT amount, COUNT(*) c FROM report_tables WHERE success_time>=? AND success_time<? GROUP BY amount ORDER BY c DESC",
            (start_dt.timestamp(), end_dt.timestamp())
        ).fetchall()
        total = sum(int(r["c"]) for r in rows)
        lines = [f"📊 報表（{start_dt.strftime('%m/%d %H:%M')}~{end_dt.strftime('%m/%d %H:%M')}）", f"成桌總數：{total}桌"]
        for r in rows:
            lines.append(f"{r['amount']}：{r['c']}桌")
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr([])))
        return

    # ===== 放鳥扣分自訂輸入 =====
    if user_state.get(user_id, {}).get("mode") == "set_no_show_deduct":
        if not is_admin(db, user_id):
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=make_qr([])))
            return
        v = text.strip()
        if not v.isdigit():
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入數字（例如 20）", quick_reply=back_menu()))
            return
        db.execute("INSERT OR REPLACE INTO shop_settings(key, value) VALUES('no_show_deduct', ?)", (v,))
        db.commit()
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已更新放鳥扣分為：{v}", quick_reply=make_qr([])))
        return



    # ===== 查自己的 LINE User ID =====
    if text in ("賴ID", "賴id", "LINEID", "lineid"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"你的 LINE User ID：{user_id}", quick_reply=back_menu())
        )
        return

    # ===== 回主選單 =====
    if text in ("選單","主選單","回主選單","回主選單 "):
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    

    # ===== 主功能（新選單）=====
    if text == "開桌/配桌":
        if require_phone_bound(event, db, user_id):
            return
        user_state[user_id] = {"mode": "match_entry"}
        items = [
            QuickReplyButton(action=MessageAction(label="我要配桌", text="我要配桌")),
            QuickReplyButton(action=MessageAction(label="我要開桌", text="我要開桌")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇：", quick_reply=make_qr(items)))
        return

    
    if text == "我要配桌":
        if require_not_frozen(event, db, user_id):
            return
        user_state[user_id] = {"mode": "choose_hand", "action": "match"}
        ss_set(db, user_id, shop_id=None, amount=None, hand=None, action="match")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "請選擇快手/慢手（只影響配對，之後可再調整）",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="⚡ 快手", text="快手")),
                QuickReplyButton(action=MessageAction(label="🐢 慢手", text="慢手")),
                QuickReplyButton(action=MessageAction(label="不限", text="不限")),
            ])
        ))
        return

    if text == "我要開桌":
        if require_not_frozen(event, db, user_id):
            return
        user_state[user_id] = {"mode": "choose_hand", "action": "open"}
        ss_set(db, user_id, shop_id=None, amount=None, hand=None, action="open")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "請選擇快手/慢手（開桌會優先同類型配對）",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="⚡ 快手", text="快手")),
                QuickReplyButton(action=MessageAction(label="🐢 慢手", text="慢手")),
                QuickReplyButton(action=MessageAction(label="不限", text="不限")),
            ])
        ))
        return

    if text in ("快手", "慢手", "不限") and user_state.get(user_id, {}).get("mode") == "choose_hand":
        hand = text
        action = user_state[user_id].get("action") or "match"
        user_state[user_id] = {"mode": "choose_shop", "hand": hand, "action": action}
        ss_set(db, user_id, hand=hand, action=action)

        shops = db.execute("SELECT shop_id, name FROM shops WHERE open=1 AND approved=1 ORDER BY rowid DESC").fetchall()
        if not shops:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=PostbackAction(label=(s["name"] or "")[:20], data=f"shop={s['shop_id']}")) for s in shops]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇店家", quick_reply=make_qr(items, include_back=True, include_home=True)))
        return

    if text == "桌況查詢":
        msg = waiting_summary(db)
        url = liff_deeplink()
        items = [
            QuickReplyButton(action=MessageAction(label="缺1", text="桌況:缺1")),
            QuickReplyButton(action=MessageAction(label="缺2", text="桌況:缺2")),
            QuickReplyButton(action=MessageAction(label="缺3", text="桌況:缺3")),
        ]
        if url:
            items.append(QuickReplyButton(action=URIAction(label="開啟LIFF桌況", uri=url)))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=make_qr(items)))
        return

    if text in ("缺1","缺2","缺3"):
        n = int(text.replace("缺",""))
        msg = waiting_summary(db, n)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "我的":

        # 我的資訊不強制先綁定（未綁定會引導）
        nk = get_nickname(db, user_id) or display_name(event)
        phone = db.execute("SELECT phone FROM users WHERE user_id=?", (user_id,)).fetchone()
        phone = phone["phone"] if phone else None
        row = db.execute("SELECT people, shop_id, amount, status, table_index FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        st = "無"
        if row:
            st = f'{row["status"]}｜{row["amount"] or ""}｜{row["people"] or ""}人'
        msg = f"👤 我的資訊\n\n暱稱：{nk}\n手機：{phone or '未綁定'}\n狀態：{st}"
        items = []
        if not phone:
            items.append(QuickReplyButton(action=MessageAction(label="綁定手機", text="綁定手機")))
        items.append(QuickReplyButton(action=MessageAction(label="設定暱稱", text="設定暱稱")))
        items.append(QuickReplyButton(action=MessageAction(label="取消配桌", text="取消配桌")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=make_qr(items)))
        return

    if text == "綁定手機":
        user_state[user_id] = {"mode": "bind_phone"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("📱 請輸入手機號碼（例：09xxxxxxxx）", quick_reply=back_menu()))
        return

    if text == "店家聯絡":
        # 單店版：取第一筆已審核店家資訊（或你可改成固定文字）
        row = db.execute("SELECT name FROM shops WHERE approved=1 ORDER BY rowid DESC LIMIT 1").fetchone()
        name = row["name"] if row else "店家"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"☎️ {name}\n\n如需預約/客服，請直接聯繫店家。", quick_reply=make_qr([])))
        return

    if text == "地圖":
        row = db.execute("SELECT partner_map FROM shops WHERE approved=1 ORDER BY rowid DESC LIMIT 1").fetchone()
        murl = (row["partner_map"] or "").strip() if row else ""
        items = []
        if murl:
            items.append(QuickReplyButton(action=URIAction(label="開啟地圖", uri=murl)))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗺 店家地圖", quick_reply=make_qr(items)))
        return

    if text == "客戶資訊":
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=make_qr([])))
            return
        items = [
            QuickReplyButton(action=MessageAction(label="🔎 查客戶", text="查客戶")),
            QuickReplyButton(action=MessageAction(label="🚫 黑名單/凍結", text="今天黑名單")),
            QuickReplyButton(action=MessageAction(label="⚙️ 信用分設定", text="信用分設定")),
            QuickReplyButton(action=MessageAction(label="報表-當日", text="報表:當日")),
            QuickReplyButton(action=MessageAction(label="報表-本月", text="報表:本月")),
            QuickReplyButton(action=MessageAction(label="自訂區間", text="報表:自訂")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("📊 客戶 / 信用分 / 報表", quick_reply=make_qr(items)))
        return

    if text == "今天黑名單":
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=make_qr([])))
            return
        th = db.execute("SELECT value FROM shop_settings WHERE key='blacklist_threshold'").fetchone()
        th = int(th["value"]) if th else 60
        rows = db.execute("""
            SELECT user_id, score, is_frozen
            FROM user_scores
            WHERE is_frozen=1 OR score<?
            ORDER BY score ASC
            LIMIT 50
        """, (th,)).fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 目前沒有凍結/低信用名單。", quick_reply=back_menu()))
            return
        items = []
        lines = ["🚫 凍結/低信用名單（點選可管理）"]
        for r in rows:
            uid = r["user_id"]
            nk = get_nickname_or_default(db, uid)
            last3 = get_phone_last3(db, uid)
            flag = "🔒" if int(r["is_frozen"] or 0) == 1 else ""
            lines.append(f"- {nk}｜***{last3}｜{flag}分數:{r['score']}")
            items.append(QuickReplyButton(action=MessageAction(label=f"{nk}***{last3}", text=f"客戶:{uid}")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr(items)))
        return
        # 黑名單：blacklisted=1 或 score < threshold
        th = db.execute("SELECT value FROM shop_settings WHERE key='blacklist_threshold'").fetchone()
        th = int(th["value"]) if th else 60
        rows = db.execute("SELECT user_id, score, blacklisted FROM user_scores WHERE blacklisted=1 OR score<? ORDER BY score ASC LIMIT 50", (th,)).fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 目前沒有黑名單。", quick_reply=make_qr([])))
            return
        lines = ["🚫 黑名單/低信用名單："]
        for r in rows:
            uid = r["user_id"]
            lines.append(f"- {uid[:6]}…  分數:{r['score']}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr([])))
        return

    
    if text == "查客戶":
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=back_menu()))
            return
        user_state[user_id] = {"mode": "cust_search"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入『暱稱關鍵字』或『手機末三碼』來查詢客戶", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "cust_search":
        if not is_admin(db, user_id):
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return
        q = text.strip()
        candidates = []
        if q.isdigit() and len(q) == 3:
            rows = db.execute("SELECT user_id FROM users WHERE phone LIKE ? ORDER BY updated DESC LIMIT 20", (f"%{q}",)).fetchall()
            candidates = [r["user_id"] for r in rows]
        else:
            rows = db.execute("SELECT user_id FROM nicknames WHERE nickname LIKE ? ORDER BY rowid DESC LIMIT 20", (f"%{q}%",)).fetchall()
            candidates = [r["user_id"] for r in rows]

        if not candidates:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("查無符合客戶。可改用手機末三碼試試。", quick_reply=back_menu()))
            return

        items = []
        lines = ["🔎 查詢結果（點選進入）"]
        for uid in candidates:
            nk = get_nickname_or_default(db, uid)
            last3 = get_phone_last3(db, uid)
            sc = get_score_row(db, uid)
            flag = "🔒" if sc and int(sc["is_frozen"] or 0) == 1 else ""
            lines.append(f"- {nk}｜***{last3}｜{flag}分數:{sc['score'] if sc else 100}")
            items.append(QuickReplyButton(action=MessageAction(label=f"{nk}***{last3}", text=f"客戶:{uid}")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr(items)))
        return

    if text.startswith("客戶:"):
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=back_menu()))
            return
        target = text.split(":", 1)[1].strip()
        sc = get_score_row(db, target)
        nk = get_nickname_or_default(db, target)
        last3 = get_phone_last3(db, target)
        score = int(sc["score"]) if sc else 100
        frozen = int(sc["is_frozen"] or 0) if sc else 0
        status = "🔒 已凍結" if frozen else "✅ 正常"
        user_state[user_id] = {"mode": "cust_manage", "target": target}

        items = [
            QuickReplyButton(action=MessageAction(label="放鳥 -20", text="扣除:放鳥")),
            QuickReplyButton(action=MessageAction(label="取消 -5", text="扣除:取消")),
            QuickReplyButton(action=MessageAction(label="遲到 -10", text="扣除:遲到")),
            QuickReplyButton(action=MessageAction(label="玩家檢舉 -15", text="扣除:玩家檢舉")),
        ]
        freeze_btn = QuickReplyButton(action=MessageAction(label="凍結", text="凍結此人")) if not frozen else QuickReplyButton(action=MessageAction(label="解除凍結", text="解除凍結此人"))
        items.append(freeze_btn)
        items.append(QuickReplyButton(action=MessageAction(label="🔙 返回", text="返回")))
        items.append(QuickReplyButton(action=MessageAction(label="🏠 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"👤 客戶：{nk}\n手機：***{last3}\n信用分：{score}\n狀態：{status}\n\n（點原因即可扣分；低於 60 會自動凍結）",
            quick_reply=make_qr(items)
        ))
        return

    if text.startswith("事件:") and user_state.get(user_id, {}).get("mode") == "cust_manage":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除（僅保留固定扣分）。", quick_reply=back_menu()))
        return
        ev = text.split(":", 1)[1].strip()
        if ev not in SCORE_EVENTS:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("不支援的事件。", quick_reply=back_menu()))
            return
        target = user_state[user_id]["target"]
        delta = get_rule_delta(db, ev)
        score, frozen = apply_score_change(db, target, delta, ev, operator_id=user_id, note=None)
        msg = f"✅ 已套用事件『{ev}』：{delta:+d} 分\n目前分數：{score}" + ("（已凍結）" if frozen else "")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text.startswith("調分:") and user_state.get(user_id, {}).get("mode") == "cust_manage":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除（不提供手動加減分）。", quick_reply=back_menu()))
        return
        target = user_state[user_id]["target"]
        val = text.split(":", 1)[1].strip()
        if val == "自訂":
            user_state[user_id]["mode"] = "cust_adjust"
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入分數變動（5的倍數），例如：+15 或 -20", quick_reply=back_menu()))
            return
        try:
            delta = int(val)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("格式錯誤。", quick_reply=back_menu()))
            return
        if delta % 5 != 0:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("分數必須是 5 的倍數。", quick_reply=back_menu()))
            return
        score, frozen = apply_score_change(db, target, delta, "手動調分", operator_id=user_id, note=None)
        msg = f"✅ 已調整 {delta:+d} 分\n目前分數：{score}" + ("（已凍結）" if frozen else "")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "cust_adjust":
        user_state[user_id]["mode"] = "cust_manage"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除。", quick_reply=back_menu()))
        return
        target = user_state[user_id]["target"]
        t = text.strip().replace(" ", "")
        try:
            delta = int(t)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入例如：+15 或 -20", quick_reply=back_menu()))
            return
        if delta % 5 != 0:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("分數必須是 5 的倍數。", quick_reply=back_menu()))
            return
        score, frozen = apply_score_change(db, target, delta, "手動調分", operator_id=user_id, note=None)
        user_state[user_id]["mode"] = "cust_manage"
        msg = f"✅ 已調整 {delta:+d} 分\n目前分數：{score}" + ("（已凍結）" if frozen else "")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return


    if text.startswith("扣除:") and user_state.get(user_id, {}).get("mode") == "cust_manage":
        if not is_admin(db, user_id):
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return
        reason = text.split(":", 1)[1].strip()
        mapping = {"放鳥": -20, "取消": -5, "遲到": -10, "玩家檢舉": -15}
        if reason not in mapping:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("不支援的扣分原因。", quick_reply=back_menu()))
            return
        target = user_state[user_id]["target"]
        delta = mapping[reason]
        score, frozen = apply_score_change(db, target, delta, reason, operator_id=user_id, note=None)
        msg = f"✅ 已扣除（{reason}）：{delta} 分\n目前分數：{score}" + ("（已凍結）" if frozen else "")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return


    if text == "凍結此人" and user_state.get(user_id, {}).get("mode") == "cust_manage":
        if not is_admin(db, user_id):
            return
        target = user_state[user_id]["target"]
        freeze_user(db, target, operator_id=user_id, reason="manual")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已凍結此帳號。", quick_reply=back_menu()))
        return

    if text == "解除凍結此人" and user_state.get(user_id, {}).get("mode") == "cust_manage":
        if not is_admin(db, user_id):
            return
        target = user_state[user_id]["target"]
        score = unfreeze_user(db, target, operator_id=user_id, set_score_to_threshold=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已解除凍結（分數調整為 {score}）", quick_reply=back_menu()))
        return

    if text == "信用分設定":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除（規則固定）。", quick_reply=back_menu()))
        return
        # 顯示固定事件的目前設定
        rows = db.execute("SELECT event, delta FROM score_rules WHERE event IN ('放鳥','完成配桌','取消','遲到','玩家檢舉')").fetchall()
        mapping = {r["event"]: int(r["delta"]) for r in rows}
        lines = ["⚙️ 信用分規則（點選要修改的事件）"]
        items = []
        for ev in SCORE_EVENTS:
            dv = mapping.get(ev, 0)
            lines.append(f"- {ev}：{dv:+d}")
            items.append(QuickReplyButton(action=MessageAction(label=ev, text=f"設定事件:{ev}")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr(items)))
        return

    if text.startswith("設定事件:"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除（規則固定）。", quick_reply=back_menu()))
        return
        ev = text.split(":", 1)[1].strip()
        if ev not in SCORE_EVENTS:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("不支援的事件。", quick_reply=back_menu()))
            return
        cur = get_rule_delta(db, ev)
        user_state[user_id] = {"mode": "set_rule", "event": ev}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"目前『{ev}』為 {cur:+d} 分\n請輸入新分數（5的倍數，例如 -20 或 +10）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "set_rule":
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除（規則固定）。", quick_reply=back_menu()))
        return
        ev = user_state[user_id]["event"]
        t = text.strip().replace(" ", "")
        try:
            delta = int(t)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入數字，例如 -20 或 +10", quick_reply=back_menu()))
            return
        if delta % 5 != 0:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("必須是 5 的倍數。", quick_reply=back_menu()))
            return
        db.execute("INSERT OR REPLACE INTO score_rules(event, delta) VALUES(?,?)", (ev, delta))
        db.commit()
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已更新：{ev} → {delta:+d} 分", quick_reply=back_menu()))
        return

    if text.startswith("報表:"):
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=make_qr([])))
            return
        mode = text.split(":",1)[1]
        now = datetime.now()
        if mode == "當日":
            start = datetime(now.year, now.month, now.day)
            end = start + timedelta(days=1)
        elif mode == "本月":
            start = datetime(now.year, now.month, 1)
            if now.month == 12:
                end = datetime(now.year+1, 1, 1)
            else:
                end = datetime(now.year, now.month+1, 1)
        elif mode == "自訂":
            user_state[user_id] = {"mode": "report_custom"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入區間，例如：2/3 10:00-2/4 13:00 或 10:00-13:00", quick_reply=back_menu()))
            return
        else:
            start = datetime(now.year, now.month, now.day)
            end = start + timedelta(days=1)
        # 統計 report_tables
        rows = db.execute(
            "SELECT amount, COUNT(*) c FROM report_tables WHERE success_time>=? AND success_time<? GROUP BY amount ORDER BY c DESC",
            (start.timestamp(), end.timestamp())
        ).fetchall()
        total = sum(int(r["c"]) for r in rows)
        lines = [f"📊 報表（{start.strftime('%m/%d %H:%M')}~{end.strftime('%m/%d %H:%M')}）", f"成桌總數：{total}桌"]
        for r in rows:
            lines.append(f"{r['amount']}：{r['c']}桌")
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr([])))
        return

    if text == "店家後台":
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=back_menu()))
            return
        role = db.execute("SELECT role FROM shop_admins WHERE user_id=?", (user_id,)).fetchone()
        is_owner = bool(role and (role["role"] == "owner"))
        items = [
            QuickReplyButton(action=MessageAction(label="開始營業", text="開始營業")),
            QuickReplyButton(action=MessageAction(label="休息/打烊", text="休息/打烊")),
            QuickReplyButton(action=MessageAction(label="群設定", text="群設定")),
            QuickReplyButton(action=MessageAction(label="新增管理員驗證碼", text="新增管理員驗證碼")),
        ]
        if is_owner:
            items.append(QuickReplyButton(action=MessageAction(label="管理員管理", text="管理員管理")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🏪 店家後台", quick_reply=make_qr(items)))
        return
        items = [
            QuickReplyButton(action=MessageAction(label="開始營業", text="開始營業")),
            QuickReplyButton(action=MessageAction(label="休息/打烊", text="休息/打烊")),
            QuickReplyButton(action=MessageAction(label="群設定", text="群設定")),
            QuickReplyButton(action=MessageAction(label="新增管理員驗證碼", text="新增管理員驗證碼")),
            
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🏪 店家後台", quick_reply=make_qr(items)))
        return


    if text == "新增管理員驗證碼":
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=back_menu()))
            return
        role = db.execute("SELECT role FROM shop_admins WHERE user_id=?", (user_id,)).fetchone()
        if not role or role["role"] != "owner":
            line_bot_api.reply_message(event.reply_token, TextSendMessage("只有 owner 可以產生驗證碼。", quick_reply=back_menu()))
            return
        code6 = f"{random.randint(0, 999999):06d}"
        expires = time.time() + 3600
        db.execute("INSERT OR REPLACE INTO invite_codes(code, remaining, expires, created, created_by) VALUES(?,?,?,?,?)",
                   (code6, 20, expires, time.time(), user_id))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"✅ 管理員驗證碼：{code6}\n有效 60 分鐘，最多可用 20 次。\n\n讓管理員在官方帳號輸入此 6 位數即可加入。",
            quick_reply=back_menu()
        ))
        return

    if text == "管理員管理":
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=back_menu()))
            return
        role = db.execute("SELECT role FROM shop_admins WHERE user_id=?", (user_id,)).fetchone()
        if not role or role["role"] != "owner":
            line_bot_api.reply_message(event.reply_token, TextSendMessage("只有 owner 可以管理管理員。", quick_reply=back_menu()))
            return
        rows = db.execute("SELECT user_id, role FROM shop_admins ORDER BY created ASC").fetchall()
        items = []
        lines = ["👥 管理員列表（owner 才可刪除）"]
        for r in rows:
            uid = r["user_id"]
            rrole = r["role"] or "admin"
            tag = "👑owner" if rrole == "owner" else "admin"
            nk = get_nickname_or_default(db, uid)
            lines.append(f"- {nk}｜{tag}｜{uid}")
            if rrole != "owner":
                items.append(QuickReplyButton(action=MessageAction(label=f"刪除 {nk}", text=f"刪除管理員:{uid}")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 返回", text="返回")))
        items.append(QuickReplyButton(action=MessageAction(label="🏠 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines), quick_reply=make_qr(items)))
        return

    if text.startswith("刪除管理員:"):
        if not is_admin(db, user_id):
            return
        role = db.execute("SELECT role FROM shop_admins WHERE user_id=?", (user_id,)).fetchone()
        if not role or role["role"] != "owner":
            line_bot_api.reply_message(event.reply_token, TextSendMessage("只有 owner 可以刪除管理員。", quick_reply=back_menu()))
            return
        target = text.split(":", 1)[1].strip()
        trow = db.execute("SELECT role FROM shop_admins WHERE user_id=?", (target,)).fetchone()
        if not trow:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("找不到此管理員。", quick_reply=back_menu()))
            return
        if trow["role"] == "owner":
            line_bot_api.reply_message(event.reply_token, TextSendMessage("不可刪除 owner。", quick_reply=back_menu()))
            return
        db.execute("DELETE FROM shop_admins WHERE user_id=?", (target,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已刪除管理員。", quick_reply=back_menu()))
        return

    if text == "扣分設定":
        # 已改為信用分設定
        text = "信用分設定"

        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=make_qr([])))
            return
        cur = db.execute("SELECT value FROM shop_settings WHERE key='no_show_deduct'").fetchone()
        cur = cur["value"] if cur else "20"
        user_state[user_id] = {"mode": "set_no_show_deduct"}
        items = [
            QuickReplyButton(action=MessageAction(label="10分", text="扣分:10")),
            QuickReplyButton(action=MessageAction(label="20分", text="扣分:20")),
            QuickReplyButton(action=MessageAction(label="30分", text="扣分:30")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"目前放鳥扣分：{cur}\n請選擇或直接輸入數字：", quick_reply=make_qr(items)))
        return

    if text.startswith("扣分:"):
        if not is_admin(db, user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能僅限店家使用。", quick_reply=make_qr([])))
            return
        val = text.split(":",1)[1].strip()
        if val.isdigit():
            db.execute("INSERT OR REPLACE INTO shop_settings(key, value) VALUES('no_show_deduct', ?)", (val,))
            db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已更新放鳥扣分為：{val}", quick_reply=make_qr([])))
            return

    # ===== 需要先綁定手機（除：賴ID/選單/返回/加入/放棄 這些流程）=====
    if text not in ("選單", "賴ID", "賴id", "LINEID", "lineid", "ID", "返回", "加入", "放棄") and require_phone_bound(event, db, user_id):
        return

# ===== 管理入口 =====
    if user_id in ADMIN_IDS and text == "店家管理":
        user_state[user_id] = {"mode": "admin_menu"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "🛠 店家管理",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="📋 查看店家", text="管理:查看")),
                QuickReplyButton(action=MessageAction(label="✅ 審核店家", text="管理:審核")),
                QuickReplyButton(action=MessageAction(label="🗑 刪除店家", text="管理:刪除")),
                QuickReplyButton(action=MessageAction(label="🗺 地圖設定", text="管理:地圖設定")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    # 管理：查看
    if user_id in ADMIN_IDS and text == "管理:查看":
        rows = db.execute("SELECT shop_id, name, open, approved FROM shops ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return
        msg = "🏪 店家列表\n\n"
        for r in rows:
            msg += f"{r['name']}\n狀態：{'營業中' if r['open'] else '未營業'} | {'✅通過' if r['approved'] else '❌未審核'}\nID:{r['shop_id']}\n\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg.strip(), quick_reply=back_menu()))
        return

    # 管理：審核
    if user_id in ADMIN_IDS and text == "管理:審核":
        rows = db.execute("SELECT shop_id, name, approved FROM shops ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return
        items = []
        for r in rows:
            items.append(QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:審核:{r['shop_id']}")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要審核的店家", quick_reply=make_qr(items)))
        return

    if user_id in ADMIN_IDS and text.startswith("管理:審核:"):
        sid = text.split(":", 2)[2]
        user_state[user_id] = {"mode": "admin_review", "sid": sid}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "請選擇審核結果",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="✅ 通過", text="管理:同意")),
                QuickReplyButton(action=MessageAction(label="❌ 不通過", text="管理:不同意")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if user_id in ADMIN_IDS and user_state.get(user_id, {}).get("mode") == "admin_review":
        sid = user_state[user_id]["sid"]
        if text == "管理:同意":
            db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已通過", quick_reply=back_menu()))
            return
        if text == "管理:不同意":
            db.execute("UPDATE shops SET approved=0 WHERE shop_id=?", (sid,))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已設為不通過", quick_reply=back_menu()))
            return

    # 管理：刪除
    if user_id in ADMIN_IDS and text == "管理:刪除":
        rows = db.execute("SELECT shop_id, name FROM shops ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:刪除:{r['shop_id']}")) for r in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要刪除的店家", quick_reply=make_qr(items)))
        return

    if user_id in ADMIN_IDS and text.startswith("管理:刪除:"):
        sid = text.split(":", 2)[2]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return

    # 管理：地圖設定
    if user_id in ADMIN_IDS and text == "管理:地圖設定":
        rows = db.execute("SELECT shop_id, name FROM shops WHERE approved=1 ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有已核准店家", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:地圖:{r['shop_id']}")) for r in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要設定地圖的店家", quick_reply=make_qr(items)))
        return

    if user_id in ADMIN_IDS and text.startswith("管理:地圖:"):
        sid = text.split(":", 2)[2]
        user_state[user_id] = {"mode": "admin_map_input", "sid": sid}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請貼上地圖連結（Google Maps 連結）", quick_reply=back_menu()))
        return

    if user_id in ADMIN_IDS and user_state.get(user_id, {}).get("mode") == "admin_map_input":
        sid = user_state[user_id]["sid"]
        link = text.strip()
        db.execute("UPDATE shops SET partner_map=? WHERE shop_id=?", (link, sid))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新地圖連結", quick_reply=back_menu()))
        return

    # ===== 設定暱稱 =====
    if text == "設定暱稱":
        user_state[user_id] = {"mode": "nickname_input"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入你的暱稱（最多 12 字）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "nickname_input":
        nk = text.strip()[:12]
        db.execute("INSERT OR REPLACE INTO nicknames(user_id, nickname) VALUES(?,?)", (user_id, nk))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 暱稱已設定：{nk}", quick_reply=back_menu()))
        return

    # ===== 記事本（已移除）=====
    if text in ("記事本","新增紀錄","查看當月","查看上月","清除紀錄"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage("此功能已移除。", quick_reply=make_qr([])))
        return


    # ===== 店家合作 =====
    if text == "店家合作":
        row = db.execute("SELECT shop_id, name, approved, open, group_link FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            user_state[user_id] = {"mode": "shop_apply"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入店家名稱", quick_reply=back_menu()))
            return
        if int(row["approved"] or 0) != 1:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⏳ 尚未審核通過，請等待管理員審核", quick_reply=back_menu()))
            return

        status = "🟢 營業中" if int(row["open"] or 0) == 1 else "🔴 未營業"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"🏪 {row['name']}\n{status}",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if user_state.get(user_id, {}).get("mode") == "shop_apply":
        name = text.strip()[:30]
        sid = f"{user_id}_{int(time.time())}"
        db.execute(
            "INSERT OR REPLACE INTO shops(shop_id, name, open, approved, group_link, owner_id, partner_map) VALUES(?,?,0,0,'',?, '')",
            (sid, name, user_id)
        )
        db.execute("INSERT OR IGNORE INTO shop_admins(user_id, role, created) VALUES(?,?,?)", (user_id, "owner", time.time()))
        db.execute("INSERT OR REPLACE INTO shop_settings(key, value) VALUES('owner_id', ?)", (user_id,))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已送出申請，等待管理員審核", quick_reply=back_menu()))
        return

    if text == "開始營業":
        row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (row["shop_id"],))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return

    if text == "今日休息":
        row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (row["shop_id"],))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return

    if text == "設定群組":
        user_state[user_id] = {"mode": "set_group"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請貼上群組邀請連結（https://line.me/...）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "set_group":
        link = text.strip()
        row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (link, row["shop_id"]))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已設定群組連結", quick_reply=back_menu()))
        return

    # ===== 店家地圖 =====
    if text == "店家地圖":
        rows = db.execute("SELECT shop_id, name, partner_map FROM shops WHERE open=1 AND approved=1 ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業的店家", quick_reply=back_menu()))
            return
        rows_with_link = [r for r in rows if (r["partner_map"] or "").strip()]
        if not rows_with_link:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有可開啟的地圖（店家尚未設定地圖連結）", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"地圖:{r['shop_id']}")) for r in rows_with_link]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇要開啟地圖的店家", quick_reply=make_qr(items)))
        return

    if text.startswith("地圖:"):
        sid = text.split(":", 1)[1].strip()
        row = db.execute("SELECT name, partner_map FROM shops WHERE shop_id=? AND open=1 AND approved=1", (sid,)).fetchone()
        if not row or not (row["partner_map"] or "").strip():
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此店家尚未設定地圖連結", quick_reply=back_menu()))
            return
        name = row["name"] or "店家"
        link = row["partner_map"].strip()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"🗺 {name} 地圖\n{link}",
            quick_reply=make_qr([
                QuickReplyButton(action=URIAction(label="📍 開啟地圖", uri=link)),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    # ===== 店家配桌 =====
    if text == "店家配桌":
        row = db.execute("SELECT shop_id, amount, people, status, table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            # ✅ 若正在「成桌確認」階段，優先顯示「加入/放棄」
            if row["status"] == "ready":
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("你目前在成桌確認中，請選擇：", quick_reply=confirm_menu())
                )
                return

            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "你目前已有配桌紀錄\n(可查看進度/取消配桌)",
                quick_reply=make_qr([
                    QuickReplyButton(action=MessageAction(label="🔍 查看進度", text="查看進度")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
                ])
            ))
            return

        ss_clear(db, user_id)
        shops = db.execute("SELECT shop_id, name FROM shops WHERE open=1 AND approved=1 ORDER BY rowid DESC").fetchall()
        if not shops:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
            return

        items = [
            QuickReplyButton(action=PostbackAction(label=(s["name"] or "")[:20], data=f"shop={s['shop_id']}"))
            for s in shops
        ]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇店家", quick_reply=make_qr(items)))
        return


    if text.startswith("桌況:缺"):
        try:
            missing = int(text.replace("桌況:缺", "").strip())
        except:
            missing = None
        msg = waiting_summary(db, missing_filter=missing)
        url = liff_deeplink(missing=missing)
        items = [
            QuickReplyButton(action=MessageAction(label="全部", text="桌況查詢")),
            QuickReplyButton(action=MessageAction(label="缺1", text="桌況:缺1")),
            QuickReplyButton(action=MessageAction(label="缺2", text="桌況:缺2")),
            QuickReplyButton(action=MessageAction(label="缺3", text="桌況:缺3")),
        ]
        if url:
            items.append(QuickReplyButton(action=URIAction(label="開啟LIFF桌況", uri=url)))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=make_qr(items)))
        return

    if text == "查看進度":
        row = db.execute("""
            SELECT s.name, m.amount, m.people, m.status
            FROM match_users m
            LEFT JOIN shops s ON m.shop_id = s.shop_id
            WHERE m.user_id=?
        """, (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"📌 配桌狀態\n\n🏪 {row['name'] or '未知店家'}\n💰 {row['amount']}\n👥 {int(row['people'])} 人\n📍 {row['status']}",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if text.startswith("店家:"):
        sid = text.split(":", 1)[1].strip()
        user_state[user_id] = {"mode": "wait_amount", "shop_id": sid}
        ss_set(db, user_id, shop_id=sid, amount=None)
        items = [
            QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
            QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
            QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
            QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=make_qr(items)))
        return

    if text.startswith("金額:"):
        amount = text.split(":", 1)[1].strip()
        st = user_state.get(user_id, {})
        if not st.get("shop_id"):
            sid_db, _amt_db, _hand_db, _act_db = ss_get(db, user_id)
            if sid_db:
                st["shop_id"] = sid_db
                st["hand"] = st.get("hand") or _hand_db or st.get("hand")
                user_state[user_id] = st
        if not st.get("shop_id"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請先選擇店家", quick_reply=back_menu()))
            return
        st["amount"] = amount
        st["mode"] = "wait_people"
        user_state[user_id] = st
        ss_set(db, user_id, amount=amount)
        items = [
            QuickReplyButton(action=MessageAction(label="我1人", text="人數:1")),
            QuickReplyButton(action=MessageAction(label="我2人", text="人數:2")),
            QuickReplyButton(action=MessageAction(label="我3人", text="人數:3")),
            QuickReplyButton(action=MessageAction(label="我4人", text="人數:4")),
            QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇人數", quick_reply=make_qr(items)))
        return

    if text.startswith("人數:"):
        people = int(text.split(":", 1)[1].strip())
        st = user_state.get(user_id, {})
        shop_id = st.get("shop_id")
        amount = st.get("amount")
        hand_db = None
        action_db = None
        if not shop_id or not amount:
            sid_db, amt_db, hand_db, action_db = ss_get(db, user_id)
            shop_id = shop_id or sid_db
            amount = amount or amt_db
        if not shop_id or not amount:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("資料不足，請重新開始配桌", quick_reply=back_menu()))
            user_state.pop(user_id, None)
            return

        hand = (st.get('hand') or hand_db or '不限')
        action = (st.get('action') or action_db or ss_get_all(db, user_id).get('action') or 'match')

        if action == "open":
            user_state[user_id] = {"mode": "open_time", "shop_id": shop_id, "amount": amount, "hand": hand, "people": people}
            ss_set(db, user_id, pending_people=people, pending_amount=amount, hand=hand, action="open")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "開桌：請選擇時間/需求",
                quick_reply=make_qr([
                    QuickReplyButton(action=MessageAction(label="🟢 現在", text="時間:現在")),
                    QuickReplyButton(action=MessageAction(label="🕒 預約時間", text="時間:預約")),
                    QuickReplyButton(action=MessageAction(label="📝 其他補充", text="時間:其他補充")),
                    QuickReplyButton(action=MessageAction(label="🔙 返回", text="返回")),
                    QuickReplyButton(action=MessageAction(label="🏠 回主選單", text="選單")),
                ])
            ))
            return

        db.execute("""
            INSERT OR REPLACE INTO match_users(user_id, people, shop_id, amount, status, expire, table_id, table_index, hand, is_creator, sched_type, sched_period, sched_time, note)
            VALUES(?, ?, ?, ?, 'waiting', NULL, NULL, NULL, ?, 0, NULL, NULL, NULL, NULL)
        """, (user_id, people, shop_id, amount, hand))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)

        table_id = try_make_table(shop_id, amount, hand, reply_token=event.reply_token, trigger_user_id=user_id)
        if table_id:
            return

        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "✅ 已加入配桌等待中",
            quick_reply=make_qr([
                QuickReplyButton(action=MessageAction(label="🔍 查看進度", text="查看進度")),
                QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return


    # ===== 開桌：時間/需求流程 =====
    if text.startswith("時間:") and user_state.get(user_id, {}).get("mode") == "open_time":
        choice = text.split(":", 1)[1].strip()

        if choice == "現在":
            st = user_state.get(user_id, {})
            shop_id = st.get("shop_id"); amount = st.get("amount"); hand = st.get("hand","不限"); people = int(st.get("people") or 1)
            db.execute("""
                INSERT OR REPLACE INTO match_users(user_id, people, shop_id, amount, status, expire, table_id, table_index, hand, is_creator, sched_type, sched_period, sched_time, note)
                VALUES(?, ?, ?, ?, 'waiting', NULL, NULL, NULL, ?, 1, 'now', NULL, NULL, NULL)
            """, (user_id, people, shop_id, amount, hand))
            db.commit()
            user_state.pop(user_id, None)
            ss_clear(db, user_id)
            table_id = try_make_table(shop_id, amount, hand, reply_token=event.reply_token, trigger_user_id=user_id)
            if table_id:
                return
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已開桌（等待配桌中）", quick_reply=back_menu()))
            return

        if choice == "預約":
            user_state[user_id]["mode"] = "open_reserve_period"
            ss_set(db, user_id, sched_type="reserve")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "預約：先選時段",
                quick_reply=make_qr([
                    QuickReplyButton(action=MessageAction(label="早上", text="時段:早上")),
                    QuickReplyButton(action=MessageAction(label="下午", text="時段:下午")),
                    QuickReplyButton(action=MessageAction(label="晚上", text="時段:晚上")),
                    QuickReplyButton(action=MessageAction(label="半夜", text="時段:半夜")),
                    QuickReplyButton(action=MessageAction(label="🔙 返回", text="返回")),
                    QuickReplyButton(action=MessageAction(label="🏠 回主選單", text="選單")),
                ])
            ))
            return

        if choice == "其他補充":
            user_state[user_id]["mode"] = "open_note"
            ss_set(db, user_id, sched_type="note")
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入補充備註（可輸入「略」）", quick_reply=back_menu()))
            return

    if text.startswith("時段:") and user_state.get(user_id, {}).get("mode") == "open_reserve_period":
        period = text.split(":", 1)[1].strip()
        if period not in ("早上","下午","晚上","半夜"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇：早上/下午/晚上/半夜", quick_reply=back_menu()))
            return
        user_state[user_id]["mode"] = "open_reserve_time"
        user_state[user_id]["period"] = period
        ss_set(db, user_id, sched_period=period)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入時間（HH:MM，例如 19:00）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "open_reserve_time":
        t = parse_hhmm(text)
        if not t:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("時間格式錯誤，請輸入 HH:MM（例如 19:00）", quick_reply=back_menu()))
            return
        st = user_state.get(user_id, {})
        period = st.get("period")
        shop_id = st.get("shop_id"); amount = st.get("amount"); hand = st.get("hand","不限"); people = int(st.get("people") or 1)

        db.execute("""
            INSERT OR REPLACE INTO match_users(user_id, people, shop_id, amount, status, expire, table_id, table_index, hand, is_creator, sched_type, sched_period, sched_time, note)
            VALUES(?, ?, ?, ?, 'waiting', NULL, NULL, NULL, ?, 1, 'reserve', ?, ?, NULL)
        """, (user_id, people, shop_id, amount, hand, period, t))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        table_id = try_make_table(shop_id, amount, hand, reply_token=event.reply_token, trigger_user_id=user_id)
        if table_id:
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已開桌（預約 {period} {t}）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "open_note":
        note = text.strip()
        if note == "略":
            note = ""
        st = user_state.get(user_id, {})
        shop_id = st.get("shop_id"); amount = st.get("amount"); hand = st.get("hand","不限"); people = int(st.get("people") or 1)
        db.execute("""
            INSERT OR REPLACE INTO match_users(user_id, people, shop_id, amount, status, expire, table_id, table_index, hand, is_creator, sched_type, sched_period, sched_time, note)
            VALUES(?, ?, ?, ?, 'waiting', NULL, NULL, NULL, ?, 1, 'note', NULL, NULL, ?)
        """, (user_id, people, shop_id, amount, hand, note))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        table_id = try_make_table(shop_id, amount, hand, reply_token=event.reply_token, trigger_user_id=user_id)
        if table_id:
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已開桌（等待配桌中）", quick_reply=back_menu()))
        return

    if text == "取消配桌":
        # ✅ 若在「成桌確認」中，取消配桌等同於放棄：自己退出，其他人回等待池繼續配桌
        strow = db.execute("SELECT status FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if strow and (strow["status"] in ("ready", "confirmed")):
            handle_abandon(user_id)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄（等同取消配桌）", quick_reply=back_menu()))
            return

        # 其他狀態：維持原本取消
        row = db.execute("SELECT shop_id, amount FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            shop_id, amount = row["shop_id"], row["amount"]
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.commit()
            try_make_table(shop_id, amount, '不限')
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🚪 已取消配桌", quick_reply=back_menu()))
        return

    if text == "加入":
        if require_not_frozen(event, db, user_id):
            return
        row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
        if not row or not row["table_id"]:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        table_id = row["table_id"]
        db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
        db.commit()

        push_table(table_id, "✅ 有玩家加入")
        # ✅ 以 people 加總判斷（支援 2+2、3+1、或單人選 4 人）
        stats = db.execute("""
            SELECT
              COALESCE(SUM(people),0) AS total_people,
              COALESCE(SUM(CASE WHEN status='confirmed' THEN people ELSE 0 END),0) AS confirmed_people
            FROM match_users
            WHERE table_id=?
        """, (table_id,)).fetchone()

        if stats and int(stats["total_people"]) == 4 and int(stats["confirmed_people"]) == 4:
            finalize_success(table_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已確認加入", quick_reply=back_menu()))
        return

    if text == "放棄":
        # 放棄允許凍結者操作（只是退出）
        handle_abandon(user_id)
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄（等同取消配桌）", quick_reply=back_menu()))
        return

    # ===== 其他文字：回主選單 =====
    line_bot_api.reply_message(event.reply_token, main_menu(user_id))



@app.route("/liff/status", methods=["GET"])
def liff_status():
    init_db()
    db = get_db()
    missing = request.args.get("missing")
    missing_filter = int(missing) if (missing and missing.isdigit()) else None

    rows = db.execute("""
         SELECT 
            m.shop_id, s.name AS shop_name, m.amount, COALESCE(m.hand,'不限') AS hand,
            COALESCE(SUM(m.people),0) AS total_people,
            (SELECT note FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS note,
            (SELECT sched_type FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS sched_type,
            (SELECT sched_period FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS sched_period,
            (SELECT sched_time FROM match_users mm WHERE mm.shop_id=m.shop_id AND mm.amount=m.amount AND mm.status='waiting' AND mm.is_creator=1 ORDER BY rowid DESC LIMIT 1) AS sched_time
         FROM match_users m
         LEFT JOIN shops s ON m.shop_id=s.shop_id
         WHERE m.status='waiting'
         GROUP BY m.shop_id, m.amount, COALESCE(m.hand,'不限')
         ORDER BY total_people DESC
    """).fetchall()

    def esc(x):
        return (x or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    cards=[]
    for r in rows:
        total=int(r["total_people"] or 0)
        if total<=0: 
            continue
        modv=total%4
        miss=0 if modv==0 else 4-modv
        if missing_filter is not None and miss!=missing_filter:
            continue
        shop=esc(r["shop_name"] or "店家")
        amount=esc(r["amount"] or "")
        hand=esc(r["hand"] or "不限")
        tag="✅ 可成桌" if miss==0 else f"缺{miss}"
        sched=esc(build_schedule_text(r["sched_type"], r["sched_period"], r["sched_time"]))
        note=esc((r["note"] or "").strip())
        extra=""
        if sched:
            extra += f"<span class='pill'>{sched}</span>"
        if note:
            extra += f"<span class='note'>備註：{note}</span>"
        cards.append(f"""<div class='card'>
  <div class='title'>🏪 {shop}</div>
  <div class='meta'>💰 {amount}｜🀄 {hand}｜等待 {total} 人｜<b>{tag}</b></div>
  <div class='extra'>{extra}</div>
</div>""")
    body="\n".join(cards) if cards else "<p class='empty'>目前沒有等待中的桌。</p>"
    title = f"桌況查詢（缺{missing_filter}）" if missing_filter is not None else "桌況查詢"
    return f"""<!doctype html>
<html lang='zh-Hant'>
<head>
<meta charset='utf-8' />
<meta name='viewport' content='width=device-width, initial-scale=1' />
<title>{title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans TC',Arial; margin:16px; background:#fafafa;}}
h1{{font-size:18px; margin:0 0 12px;}}
.card{{background:white; border:1px solid #eee; border-radius:12px; padding:12px; margin:10px 0; box-shadow:0 1px 2px rgba(0,0,0,.04);}}
.title{{font-weight:700; margin-bottom:6px;}}
.meta{{color:#333; font-size:14px; line-height:1.4;}}
.extra{{margin-top:8px; font-size:13px; color:#555;}}
.pill{{display:inline-block; padding:2px 8px; border-radius:999px; background:#f0f0ff; margin-right:6px;}}
.note{{display:block; margin-top:6px;}}
.empty{{color:#666;}}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>"""

# ---- Render 啟動 ----
if __name__ == "__main__":
