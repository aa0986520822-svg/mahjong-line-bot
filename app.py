import os, sqlite3, threading, time, re
from datetime import datetime, timedelta
from flask import Flask, request, abort, g
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("⚠️ 請確認已設定環境變數 LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ⚠️ 這個通常不會是有效群組連結（你要換成 LINE 群組產生的邀請連結）
SYSTEM_GROUP_LINK = "https://line.me/R/ti/g/一般玩家群"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}

COUNTDOWN_READY = 20


# -------------------------
# DB Helpers
# -------------------------
def _connect_db(path=DB_PATH):
    # timeout 防止 database is locked 時直接噴掉
    db = sqlite3.connect(path, timeout=10, check_same_thread=False)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_db():
    if "db" not in g:
        g.db = _connect_db(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def _table_has_pk(db, table_name, col_name):
    rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    for cid, name, ctype, notnull, dflt, pk in rows:
        if name == col_name and pk == 1:
            return True
    return False


def migrate_db_if_needed(db):
    """
    目標：
    - match_users.user_id 必須是 PRIMARY KEY，否則 INSERT OR REPLACE 不會如預期覆蓋，狀態會亂
    - tables.id / shops.shop_id 也建議 PK（避免重複）
    """
    # ---- match_users migration ----
    db.execute("""
    CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT PRIMARY KEY,
        people INT,
        shop_id TEXT,
        amount TEXT,
        status TEXT,
        expire REAL,
        table_id TEXT,
        table_index INT
    )
    """)
    # 如果舊表已存在但沒有 PK，要做搬移
    # 做法：若 match_users 存在且 user_id 非 PK，建立新表、把每個 user_id 取最新 rowid 搬過去
    try:
        # 若舊表存在但 schema 不同，這裡檢查會失敗/或沒有 PK
        has_pk = _table_has_pk(db, "match_users", "user_id")
        if not has_pk:
            # 先把舊表改名
            db.execute("ALTER TABLE match_users RENAME TO match_users_old")
            db.execute("""
            CREATE TABLE match_users(
                user_id TEXT PRIMARY KEY,
                people INT,
                shop_id TEXT,
                amount TEXT,
                status TEXT,
                expire REAL,
                table_id TEXT,
                table_index INT
            )
            """)
            # 搬移：每個 user_id 選最後一筆
            db.execute("""
            INSERT OR REPLACE INTO match_users(user_id,people,shop_id,amount,status,expire,table_id,table_index)
            SELECT m.user_id, m.people, m.shop_id, m.amount, m.status, m.expire, m.table_id, m.table_index
            FROM match_users_old m
            JOIN (
                SELECT user_id, MAX(rowid) AS rid
                FROM match_users_old
                GROUP BY user_id
            ) x
            ON m.user_id = x.user_id AND m.rowid = x.rid
            """)
            db.execute("DROP TABLE match_users_old")
    except sqlite3.OperationalError:
        # 可能是第一次建立或 table_info 查不到等情況，略過
        pass

    # ---- tables ----
    db.execute("""
    CREATE TABLE IF NOT EXISTS tables(
        id TEXT PRIMARY KEY,
        shop_id TEXT,
        amount TEXT,
        table_index INT
    )
    """)

    # ---- notes ----
    db.execute("""
    CREATE TABLE IF NOT EXISTS notes(
        user_id TEXT,
        content TEXT,
        amount INT,
        time TEXT
    )
    """)

    # ---- shops ----
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


def init_db():
    db = get_db()
    migrate_db_if_needed(db)
    db.commit()


def init_db_standalone():
    """給背景 thread 用，不依賴 Flask g"""
    db = _connect_db(DB_PATH)
    migrate_db_if_needed(db)
    db.commit()
    db.close()


# -------------------------
# Menus
# -------------------------
def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌 🏪", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="📒 記事本 📒", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🗺 店家地圖 🗺", text="店家地圖")),
        QuickReplyButton(action=MessageAction(label="🏪 店家合作", text="店家合作")),
    ]

    if user_id in ADMIN_IDS:
        items.append(
            QuickReplyButton(action=MessageAction(label="🛠 店家管理", text="店家管理"))
        )

    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=items))


def back_menu():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))
    ])


# -------------------------
# Table helpers
# -------------------------
def get_group_link(shop_id):
    db = get_db()
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    return row[0] if row and row[0] else SYSTEM_GROUP_LINK


def get_next_table_index(db, shop_id):
    row = db.execute("SELECT MAX(table_index) FROM tables WHERE shop_id=?", (shop_id,)).fetchone()
    return (row[0] or 0) + 1


def get_table_users(db, table_id):
    rows = db.execute(
        "SELECT user_id FROM match_users WHERE table_id=?",
        (table_id,)
    ).fetchall()
    return [r[0] for r in rows]


def build_table_status_msg(db, table_id, title="🀄 桌況更新"):
    # ⚠️ 原本 ORDER BY table_index 會亂（所有人 table_index 都等於桌號）
    # 改成 rowid 穩定排序
    rows = db.execute("""
        SELECT user_id, status, people
        FROM match_users
        WHERE table_id=?
        ORDER BY rowid
    """, (table_id,)).fetchall()

    if not rows:
        return None

    total = sum(r[2] for r in rows)

    msg = f"{title}\n\n"
    msg += f"👥 人數：{total} / 4\n\n"

    for i, (uid, status, p) in enumerate(rows, 1):
        if status == "ready":
            icon = "📩"
        elif status == "confirmed":
            icon = "✅"
        else:
            icon = "⏳"
        msg += f"{i}. {p}人 {icon} {status}\n"

    return msg


def push_table(db, table_id, title="🀄 桌況更新"):
    msg = build_table_status_msg(db, table_id, title)
    if not msg:
        return

    for uid in get_table_users(db, table_id):
        try:
            line_bot_api.push_message(uid, TextSendMessage(msg))
        except Exception as e:
            print("push error:", e)


def try_make_table(db, shop_id, amount):
        # ✅ 店家下線：強制取消所有該店該金額的配桌
    shop_open = db.execute("SELECT open FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    if not shop_open or shop_open[0] != 1:
        rows2 = db.execute(
            "SELECT user_id FROM match_users WHERE shop_id=? AND amount=?",
            (shop_id, amount)
        ).fetchall()
        for (uid,) in rows2:
            force_cancel_matching(db, uid, "⚠️ 店家已下線，系統已自動取消配桌")
        return
    rows = db.execute("""
        SELECT user_id,people FROM match_users 
        WHERE shop_id=? AND amount=? AND status='waiting'
        ORDER BY rowid
    """, (shop_id, amount)).fetchall()

    total = 0
    selected = []

    for u, p in rows:
        if total + p > 4:
            continue
        total += p
        selected.append(u)
        if total == 4:
            break

    if total != 4:
        return

    table_id = f"{shop_id}_{int(time.time()*1000)}"
    expire = time.time() + COUNTDOWN_READY
    table_index = get_next_table_index(db, shop_id)

    db.execute("INSERT OR REPLACE INTO tables(id,shop_id,amount,table_index) VALUES(?,?,?,?)",
               (table_id, shop_id, amount, table_index))

    for u in selected:
        db.execute("""
            UPDATE match_users 
            SET status='ready', expire=?, table_id=?, table_index=? 
            WHERE user_id=?
        """, (expire, table_id, table_index, u))

    db.commit()

    msg = f"🎉 成桌完成\n🪑 桌號 {table_index}\n💰 金額 {amount}\n⏱ {COUNTDOWN_READY} 秒內確認"

    for u in selected:
        try:
            line_bot_api.push_message(u, TextSendMessage(
                msg,
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                    QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
                    QuickReplyButton(action=MessageAction(label="🚪 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])
            ))
        except Exception as e:
            print("push make_table error:", e)

    push_table(db, table_id, "🪑 桌子成立")


def check_confirm(db, table_id):
    rows = db.execute("""
        SELECT user_id FROM match_users 
        WHERE table_id=? AND status='confirmed'
    """, (table_id,)).fetchall()

    if len(rows) < 4:
        return

    shop_id, amount, table_index = db.execute(
        "SELECT shop_id,amount,table_index FROM tables WHERE id=?",
        (table_id,)
    ).fetchone()

    group = get_group_link(shop_id)

    for (u,) in rows:
        try:
            line_bot_api.push_message(u, TextSendMessage(
                f"🎉 配桌成功\n\n🪑 桌號：{table_index}\n💰 金額：{amount}\n\n"
                f"進入群組後請輸入：【{table_index}】\n\n🔗 {group}",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])
            ))
        except Exception as e:
            print("push confirm error:", e)

    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()

def force_cancel_matching(db, user_id, reason="⚠️ 店家已下線，已自動取消配桌"):
    row = db.execute("""
        SELECT shop_id, amount, table_id, status
        FROM match_users
        WHERE user_id=?
    """, (user_id,)).fetchone()

    if not row:
        user_state.pop(user_id, None)
        return False

    shop_id, amount, table_id, status = row

    db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))

    if table_id:
        db.execute("""
            UPDATE match_users
            SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
            WHERE table_id=?
        """, (table_id,))

    db.commit()
    user_state.pop(user_id, None)

    try:
        try_make_table(db, shop_id, amount)
    except Exception as e:
        print("force_cancel try_make_table error:", e)

    try:
        line_bot_api.push_message(user_id, TextSendMessage(reason))
    except Exception as e:
        print("force_cancel push error:", e)

    return True
    
def force_cancel_matching(db, user_id, reason="⚠️ 店家已下線，已自動取消配桌"):
    row = db.execute("""
        SELECT shop_id, amount, table_id, status
        FROM match_users
        WHERE user_id=?
    """, (user_id,)).fetchone()

    # 沒有配桌紀錄就不用取消
    if not row:
        user_state.pop(user_id, None)
        return False

    shop_id, amount, table_id, status = row

    # 刪掉本人配桌
    db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))

    # 如果是 ready/confirmed，還要把同桌的人退回 waiting
    if table_id:
        db.execute("""
            UPDATE match_users
            SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
            WHERE table_id=?
        """, (table_id,))

    db.commit()

    # 清掉暫存狀態
    user_state.pop(user_id, None)

    # 重新湊桌（只針對原店家同金額）
    try:
        try_make_table(db, shop_id, amount)
    except Exception as e:
        print("force_cancel try_make_table error:", e)

    # 通知玩家
    try:
        line_bot_api.push_message(user_id, TextSendMessage(reason))
    except Exception as e:
        print("force_cancel push error:", e)

    return True

# -------------------------
# Timeout checker (thread-safe)
# -------------------------
def timeout_checker():
    # ✅ 背景 thread 不用 Flask g
    init_db_standalone()

    while True:
        try:
            db = _connect_db(DB_PATH)
            now = time.time()

            rows = db.execute("""
                SELECT user_id,shop_id,amount,table_id 
                FROM match_users 
                WHERE status='ready' AND expire IS NOT NULL AND expire < ?
            """, (now,)).fetchall()

            for user_id, shop_id, amount, table_id in rows:
                # 移除超時的那個玩家
                db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))

                # 同桌其他人退回 waiting
                db.execute("""
                    UPDATE match_users 
                    SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
                    WHERE table_id=?
                """, (table_id,))

                # 再嘗試湊桌
                try_make_table(db, shop_id, amount)

            db.commit()
            db.close()
        except Exception as e:
            print("timeout error:", e)

        time.sleep(3)


def get_shop_id_by_user(db, user_id):
    row = db.execute(
        "SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC",
        (user_id,)
    ).fetchone()
    return row[0] if row else None


# -------------------------
# Flask callback
# -------------------------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400)

    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# -------------------------
# Shop cooperation
# -------------------------
def show_shop_menu(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("🏪 店家合作", quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
            QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
            QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]))
    )
    return True


def handle_shop_logic(event, user_id, text, db):
    mode = user_state.get(user_id, {}).get("mode")

    # ✅ 回主畫面：要 reply，不能只 return False
    if text == "選單":
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return True

    # 進入店家合作
    if text == "店家合作":
        user_state.pop(user_id, None)

        row = db.execute(
            "SELECT shop_id, approved FROM shops WHERE owner_id=? ORDER BY rowid DESC",
            (user_id,),
        ).fetchone()

        # 尚未申請
        if not row:
            user_state[user_id] = {"mode": "shop_input"}
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("請輸入店家名稱", quick_reply=back_menu())
            )
            return True

        sid, ap = row
        user_state[user_id] = {
            "mode": "shop_menu" if ap == 1 else "shop_wait",
            "shop_id": sid
        }

        if ap == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("⏳ 尚未審核通過，請等待管理員審核", quick_reply=back_menu())
            )
            return True

        return show_shop_menu(event)

    # 新增店家名稱
    if mode == "shop_input":
        name = text.strip()
        if not name:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入店家名稱", quick_reply=back_menu()))
            return True

        shop_id = f"{user_id}_{int(time.time())}"
        db.execute(
            "INSERT OR REPLACE INTO shops (shop_id,name,open,approved,group_link,owner_id,partner_map) VALUES (?,?,?,?,?,?,?)",
            (shop_id, name, 0, 0, None, user_id, None)
        )
        db.commit()

        user_state[user_id] = {"mode": "shop_wait", "shop_id": shop_id}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"🏪 {name}\n\n✅ 已送出申請，等待審核", quick_reply=back_menu())
        )
        return True

    # 等待審核
    if mode == "shop_wait":
        sid = user_state.get(user_id, {}).get("shop_id")
        if not sid:
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return True

        ap = db.execute("SELECT approved FROM shops WHERE shop_id=?", (sid,)).fetchone()
        if ap and ap[0] == 1:
            user_state[user_id]["mode"] = "shop_menu"
            return show_shop_menu(event)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("⏳ 尚未審核通過，請稍候管理員審核", quick_reply=back_menu())
        )
        return True

    # 開始營業
    if text == "開始營業":
        sid = user_state.get(user_id, {}).get("shop_id") or get_shop_id_by_user(db, user_id)
        if not sid:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return True

        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return True

    # 今日休息
    if text == "今日休息":
        sid = user_state.get(user_id, {}).get("shop_id") or get_shop_id_by_user(db, user_id)
        if not sid:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return True

        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return True

    # 設定群組
    if text == "設定群組":
        sid = user_state.get(user_id, {}).get("shop_id") or get_shop_id_by_user(db, user_id)
        if not sid:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return True

        user_state[user_id] = {"mode": "shop_set_group", "shop_id": sid}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入群組連結", quick_reply=back_menu()))
        return True

    if mode == "shop_set_group":
        sid = user_state.get(user_id, {}).get("shop_id")
        if not sid:
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return True

        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text.strip(), sid))
        db.commit()
        user_state[user_id]["mode"] = "shop_menu"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已設定群組", quick_reply=back_menu()))
        return True

    return False
    # -------------------------
# Admin
# -------------------------
def handle_admin_logic(event, user_id, text, db):
    # ✅ 管理員按選單也要回覆
    if text == "選單" and user_id in ADMIN_IDS:
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return True

    if user_id in ADMIN_IDS and text == "店家管理":
        user_state[user_id] = {"mode": "admin_menu"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🛠 店家管理", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="📋 查看", text="查看")),
                QuickReplyButton(action=MessageAction(label="✅ 審核", text="審核")),
                QuickReplyButton(action=MessageAction(label="🗑 刪除", text="刪除")),
                QuickReplyButton(action=MessageAction(label="🗺 地圖設定", text="地圖設定")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ]))
        )
        return True

    if user_id in ADMIN_IDS and text == "查看":
        rows = db.execute("SELECT shop_id,name,open,approved FROM shops").fetchall()
        msg = "🏪 店家列表\n\n"
        for sid, name, open_, ap in rows:
            msg += f"{name}\n狀態：{'營業中' if open_ else '未營業'} | {'✅通過' if ap else '❌未審核'}\nID:{sid}\n\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return True

    # 審核
    if user_id in ADMIN_IDS and text == "審核":
        rows = db.execute("SELECT shop_id,name,approved FROM shops").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return True

        items = []
        for sid, name, ap in rows:
            label = f"🏪 {name}"
            items.append(QuickReplyButton(action=MessageAction(label=label[:20], text=f"審核:{sid}")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        user_state[user_id] = {"mode": "admin_review_select"}

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🛠 選擇要審核的店家", quick_reply=QuickReply(items=items))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_review_select" and text.startswith("審核:"):
        sid = text.split(":", 1)[1]
        user_state[user_id] = {"mode": "admin_review_confirm", "sid": sid}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請選擇審核結果", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 通過", text="同意審核")),
                QuickReplyButton(action=MessageAction(label="❌ 不通過", text="不同意審核")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_review_confirm":
        if text == "選單":
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return True

        sid = user_state[user_id]["sid"]
        if text == "同意審核":
            db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
            row = db.execute("SELECT owner_id FROM shops WHERE shop_id=?", (sid,)).fetchone()
            if row:
                user_state.pop(row[0], None)
        elif text == "不同意審核":
            db.execute("UPDATE shops SET approved=0 WHERE shop_id=?", (sid,))
        db.commit()

        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新", quick_reply=back_menu()))
        return True

    # 刪除
    if user_id in ADMIN_IDS and text == "刪除":
        rows = db.execute("SELECT shop_id,name FROM shops").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return True

        items = []
        for sid, name in rows:
            items.append(QuickReplyButton(action=MessageAction(label=f"🏪 {name}"[:20], text=f"刪除:{sid}")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        user_state[user_id] = {"mode": "admin_delete_select"}

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🗑 選擇要刪除的店家", quick_reply=QuickReply(items=items))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_delete_select" and text.startswith("刪除:"):
        sid = text.split(":", 1)[1]
        user_state[user_id] = {"mode": "admin_delete_confirm", "sid": sid}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("⚠ 確定刪除？", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 確定刪除", text="確認刪除")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_delete_confirm":
        if text == "選單":
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return True

        if text == "確認刪除":
            sid = user_state[user_id]["sid"]
            db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
            db.commit()

        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已處理", quick_reply=back_menu()))
        return True

    # 地圖設定
    if user_id in ADMIN_IDS and text == "地圖設定":
        rows = db.execute("SELECT shop_id,name FROM shops WHERE approved=1").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有已核准店家", quick_reply=back_menu()))
            return True

        items = []
        for sid, name in rows:
            items.append(QuickReplyButton(action=MessageAction(label=f"🏪 {name}"[:20], text=f"地圖:{sid}")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        user_state[user_id] = {"mode": "admin_map_select"}

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🗺 選擇要設定地圖的店家", quick_reply=QuickReply(items=items))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_map_select" and text.startswith("地圖:"):
        sid = text.split(":", 1)[1]
        user_state[user_id] = {"mode": "admin_map_input", "sid": sid}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請貼上 Google Map 連結", quick_reply=back_menu()))
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_map_input":
        sid = user_state[user_id]["sid"]
        db.execute("UPDATE shops SET partner_map=? WHERE shop_id=?", (text.strip(), sid))
        db.commit()
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新店家地圖", quick_reply=back_menu()))
        return True

    return False


# -------------------------
# Main message handler
# -------------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    # 先處理選單（所有人通用）
    if text == "選單":
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # admin 最先
    if handle_admin_logic(event, user_id, text, db):
        return

    # shop 第二
    if handle_shop_logic(event, user_id, text, db):
        return

    # -------------------------
    # 店家配桌
    # -------------------------
    if text == "店家配桌":
        row = db.execute("SELECT status FROM match_users WHERE user_id=?", (user_id,)).fetchone()

        if row:
            items = [
                QuickReplyButton(action=MessageAction(label="🔍 查看進度", text="查看進度")),
                QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你目前已有配桌紀錄", quick_reply=QuickReply(items=items)))
            return

        rows = db.execute("SELECT shop_id,name FROM shops WHERE open=1 AND approved=1").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
            return

        items = [QuickReplyButton(action=MessageAction(label=n, text=f"店家:{sid}")) for sid, n in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    if text == "查看進度":
        row = db.execute("""
            SELECT shops.name, match_users.amount, match_users.people, match_users.status
            FROM match_users
            JOIN shops ON match_users.shop_id = shops.shop_id
            WHERE match_users.user_id=?
        """, (user_id,)).fetchone()

        if not row:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        name, amount, people, status = row
        # ✅ 店家下線就強制取消
    if open_ != 1:
        force_cancel_matching(db, user_id, f"⚠️ 店家「{name}」已下線/休息\n已自動取消配桌，請重新選擇店家")
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            f"📌 配桌狀態\n\n🏪 {name}\n💰 {amount}\n👥 {people} 人\n📍 {status}",
            quick_reply=back_menu()
        )
    )
    return

    if text.startswith("店家:"):
        shop_id = text.split(":", 1)[1]
        user_state[user_id] = {"shop_id": shop_id}

        items = [
            QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
            QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
            QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
            QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("金額:"):
        amount = text.split(":", 1)[1]
        user_state.setdefault(user_id, {})["amount"] = amount

        items = [
            QuickReplyButton(action=MessageAction(label="我1人", text="人數:1")),
            QuickReplyButton(action=MessageAction(label="我2人", text="人數:2")),
            QuickReplyButton(action=MessageAction(label="我3人", text="人數:3")),
            QuickReplyButton(action=MessageAction(label="我4人", text="人數:4")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇人數", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("人數:"):
        try:
            people = int(text.split(":", 1)[1])
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("人數格式錯誤，請重新選擇", quick_reply=back_menu()))
            return

        data = user_state.get(user_id) or {}
        shop_id = data.get("shop_id")
        amount = data.get("amount")

        if not shop_id or not amount:
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("流程已失效，請重新從「店家配桌」開始", quick_reply=main_menu(user_id).quick_reply))
            return

        db.execute("""
            INSERT OR REPLACE INTO match_users 
            (user_id, people, shop_id, amount, status, expire, table_id, table_index)
            VALUES (?, ?, ?, ?, 'waiting', NULL, NULL, NULL)
        """, (user_id, people, shop_id, amount))
        db.commit()

        try_make_table(db, shop_id, amount)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已加入配桌等待中", quick_reply=back_menu()))
        return

    if text == "加入":
        row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        table_id = row[0]
        db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
        db.commit()

        push_table(db, table_id, "✅ 有玩家加入")
        check_confirm(db, table_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已確認加入", quick_reply=back_menu()))
        return

    if text == "放棄":
        row = db.execute("SELECT shop_id,amount,table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            shop_id, amount, table_id = row
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.execute("""
                UPDATE match_users 
                SET status='waiting',expire=NULL,table_id=NULL,table_index=NULL 
                WHERE table_id=?
            """, (table_id,))
            db.commit()

            push_table(db, table_id, "❌ 有玩家離開")
            try_make_table(db, shop_id, amount)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄配桌", quick_reply=back_menu()))
        return

    if text == "取消配桌":
        row = db.execute("SELECT shop_id,amount FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            shop_id, amount = row
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.commit()
            try_make_table(db, shop_id, amount)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("🚪 已取消配桌", quick_reply=back_menu()))
        return

    # -------------------------
    # 記事本
    # -------------------------
    if text == "記事本":
        user_state[user_id] = {"mode": "note_menu"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("📒 記事本", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📅 查看當月", text="查看當月")),
                QuickReplyButton(action=MessageAction(label="⏪ 查看上月", text="查看上月")),
                QuickReplyButton(action=MessageAction(label="🧹 清除紀錄", text="清除紀錄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]))
        )
        return

    if text == "新增紀錄":
        user_state[user_id] = {"mode": "note_amount"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額，例如：1000 或 -500", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "note_amount":
        val = text.strip()
        if not re.fullmatch(r"-?\d+", val):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請直接輸入金額，例如：1000 或 -500", quick_reply=back_menu()))
            return

        amount = int(val)
        db.execute("INSERT INTO notes (user_id, content, amount, time) VALUES (?,?,?,?)",
                   (user_id, "", amount, datetime.now().strftime("%Y-%m-%d")))
        db.commit()
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已新增：{amount:+}", quick_reply=back_menu()))
        return

    if text == "查看當月":
        today = datetime.now()
        month_start = today.strftime("%Y-%m-01")
        rows = db.execute("""
            SELECT amount, time FROM notes
            WHERE user_id=? AND time >= ?
            ORDER BY time DESC
        """, (user_id, month_start)).fetchall()

        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("📅 本月尚無紀錄", quick_reply=back_menu()))
            return

        total = 0
        msg = "📅 本月紀錄\n\n"
        for amt, t in rows:
            total += amt
            msg += f"{t}｜{amt:+}\n"
        msg += f"\n💰 合計：{total:+}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "查看上月":
        today = datetime.now()
        first = today.replace(day=1)
        last_month_end = first - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        rows = db.execute("""
            SELECT amount, time FROM notes
            WHERE user_id=? AND time BETWEEN ? AND ?
            ORDER BY time DESC
        """, (user_id, last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d"))).fetchall()

        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⏪ 上月尚無紀錄", quick_reply=back_menu()))
            return

        total = 0
        msg = "⏪ 上月紀錄\n\n"
        for amt, t in rows:
            total += amt
            msg += f"{t}｜{amt:+}\n"
        msg += f"\n💰 合計：{total:+}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "清除紀錄":
        db.execute("DELETE FROM notes WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🧹 已清除所有紀錄", quick_reply=back_menu())
        )
        return

    # -------------------------
    # 店家地圖
    # -------------------------
    if text == "店家地圖":
        rows = db.execute("""
            SELECT name, partner_map 
            FROM shops 
            WHERE approved=1 AND open=1 AND partner_map IS NOT NULL
        """).fetchall()

        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("🚫 未有營業店家", quick_reply=back_menu()))
            return

        items = []
        for name, link in rows:
            if not link or not str(link).startswith("http"):
                continue
            items.append(QuickReplyButton(action=URIAction(label=f"🏪 {name}"[:20], uri=link)))

        items.append(QuickReplyButton(action=MessageAction(label="🏠 回主畫面", text="選單")))

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("📍 請選擇店家地圖：", quick_reply=QuickReply(items=items))
        )
        return

    # 最後兜底：未知指令回主選單
    line_bot_api.reply_message(event.reply_token, main_menu(user_id))


# -------------------------
# MAIN
# -------------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()

    # ✅ 在 main 裡啟動 timeout thread（此時 DB 已初始化完成）
    threading.Thread(target=timeout_checker, daemon=True).start()

    app.run(host="0.0.0.0", port=5000)


