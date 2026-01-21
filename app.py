import os, sqlite3, threading, time
from datetime import datetime, timedelta
from flask import Flask, request, abort, g
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SYSTEM_GROUP_LINK = "https://line.me/R/ti/g/一般玩家群"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}

COUNTDOWN_READY = 20


def get_db():
    try:
        if "db" not in g:
            g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        return g.db
    except:
        return sqlite3.connect(DB_PATH, check_same_thread=False)


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = get_db()

    db.execute("""CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
        name TEXT,
        open INT,
        approved INT,
        group_link TEXT
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT,
        people INT,
        shop_id TEXT,
        amount TEXT,
        status TEXT,
        expire REAL,
        table_id TEXT,
        table_index INT
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS tables(
        id TEXT,
        shop_id TEXT,
        amount TEXT,
        table_index INT
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS notes(
        user_id TEXT,
        content TEXT,
        amount INT,
        time TEXT
    )""")

    db.commit()


def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🏪 指定店家", text="指定店家")),
        QuickReplyButton(action=MessageAction(label="📒 記事本", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")),
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


def get_group_link(shop_id):
    db = get_db()
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    return row[0] if row and row[0] else SYSTEM_GROUP_LINK


def get_next_table_index(shop_id):
    db = get_db()
    row = db.execute("SELECT MAX(table_index) FROM tables WHERE shop_id=?", (shop_id,)).fetchone()
    return (row[0] or 0) + 1


def try_make_table(shop_id, amount):
    db = get_db()

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

    table_id = f"{shop_id}_{int(time.time())}"
    expire = time.time() + COUNTDOWN_READY
    table_index = get_next_table_index(shop_id)

    db.execute("INSERT INTO tables VALUES(?,?,?,?)",
               (table_id, shop_id, amount, table_index))

    for u in selected:
        db.execute("""
            UPDATE match_users 
            SET status='ready', expire=?, table_id=?, table_index=? 
            WHERE user_id=?
        """, (expire, table_id, table_index, u))

    db.commit()

    for u in selected:
        line_bot_api.push_message(u, TextSendMessage(
            f"🎉 成桌完成\n🪑 桌號 {table_index}\n💰 金額 {amount}\n⏱ {COUNTDOWN_READY} 秒內確認",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))


def check_confirm(table_id):
    db = get_db()

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
        line_bot_api.push_message(u, TextSendMessage(
            f"🎉 配桌成功\n\n🪑 桌號：{table_index}\n💰 金額：{amount}\n\n"
            f"進入群組後請輸入：【{table_index}】\n\n🔗 {group}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))

    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


def timeout_checker():
    while True:
        try:
            db = sqlite3.connect(DB_PATH, check_same_thread=False)
            now = time.time()

            rows = db.execute("""
                SELECT user_id,shop_id,amount,table_id 
                FROM match_users 
                WHERE status='ready' AND expire IS NOT NULL AND expire < ?
            """, (now,)).fetchall()

            for user_id, shop_id, amount, table_id in rows:
                db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
                db.execute("""
                    UPDATE match_users 
                    SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
                    WHERE table_id=?
                """, (table_id,))

                try_make_table(shop_id, amount)

            db.commit()
            db.close()
        except:
            pass

        time.sleep(3)


def start_timeout_thread():
    while True:
        with app.app_context():
            timeout_checker()


threading.Thread(target=start_timeout_thread, daemon=True).start()
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    text = event.message.text.strip()

    # ===== 任意輸入打開選單 =====
    if text.lower() in ["hi", "hello", "哈囉", "選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ===== 指定店家 =====
    if text == "指定店家":
        rows = db.execute("SELECT shop_id,name FROM shops WHERE open=1 AND approved=1").fetchall()

        if not rows:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
            return

        items = []
        for sid, name in rows:
            items.append(QuickReplyButton(action=MessageAction(label=name, text=f"店家:{sid}")))

        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    # ===== 選店 =====
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

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items)))
        return

    # ===== 金額 =====
    if text.startswith("金額:"):
        amount = text.split(":", 1)[1]
        if user_id not in user_state:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        user_state[user_id]["amount"] = amount

        items = [
            QuickReplyButton(action=MessageAction(label="1人", text="人數:1")),
            QuickReplyButton(action=MessageAction(label="2人", text="人數:2")),
            QuickReplyButton(action=MessageAction(label="3人", text="人數:3")),
            QuickReplyButton(action=MessageAction(label="4人", text="人數:4")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇人數", quick_reply=QuickReply(items=items)))
        return

    # ===== 人數 =====
    if text.startswith("人數:"):
        people = int(text.split(":", 1)[1])
        data = user_state.get(user_id)

        if not data:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        shop_id = data["shop_id"]
        amount = data["amount"]

        db.execute("""
            INSERT OR REPLACE INTO match_users 
            (user_id,people,shop_id,amount,status,expire,table_id,table_index)
            VALUES(?,?,?,?, 'waiting', NULL, NULL, NULL)
        """, (user_id, people, shop_id, amount))

        db.commit()

        try_make_table(shop_id, amount)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已加入配桌等待中", quick_reply=back_menu()))
        return

    # ===== 加入 =====
    if text == "加入":
        row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        table_id = row[0]

        db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
        db.commit()

        check_confirm(table_id)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已確認加入", quick_reply=back_menu()))
        return

    # ===== 放棄 =====
    if text == "放棄":
        row = db.execute("SELECT shop_id,amount,table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()

        if row:
            shop_id, amount, table_id = row
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.execute("UPDATE match_users SET status='waiting',expire=NULL,table_id=NULL,table_index=NULL WHERE table_id=?", (table_id,))
            db.commit()
            try_make_table(shop_id, amount)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("❌ 已放棄配桌", quick_reply=back_menu()))
        return

    # ===== 記事本 =====
    if text == "記事本":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請輸入： 金額 內容", quick_reply=back_menu()))
        return

    if " " in text and text.split(" ")[0].isdigit():
        amt, content = text.split(" ", 1)
        db.execute("INSERT INTO notes VALUES(?,?,?,?)",
                   (user_id, content, int(amt), datetime.now().strftime("%Y-%m-%d %H:%M")))
        db.commit()

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已存入記事本", quick_reply=back_menu()))
        return

    # ===== 預設 =====
    line_bot_api.reply_message(event.reply_token, main_menu(user_id))
# ================= TIMEOUT 檢查 =================

def timeout_checker():
    while True:
        try:
            db = sqlite3.connect(DB_PATH, check_same_thread=False)
            now = time.time()

            rows = db.execute("""
                SELECT user_id,shop_id,amount,table_id 
                FROM match_users 
                WHERE status='ready' AND expire IS NOT NULL AND expire < ?
            """, (now,)).fetchall()

            for user_id, shop_id, amount, table_id in rows:
                db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
                db.execute("""
                    UPDATE match_users 
                    SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
                    WHERE table_id=?
                """, (table_id,))
                try_make_table(shop_id, amount)

            db.commit()
            db.close()
        except Exception as e:
            print("timeout error:", e)

        time.sleep(3)


def start_timeout_thread():
    threading.Thread(target=timeout_checker, daemon=True).start()


start_timeout_thread()


# ================= 店家後台 =================

def handle_shop_logic(event, user_id, text, db):
    if text == "店家後台":
        user_state[user_id] = {"mode": "shop_input"}
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請輸入店家名稱", quick_reply=back_menu()))
        return True

    if user_state.get(user_id, {}).get("mode") == "shop_input":
        name = text
        shop_id = f"{user_id}_{int(time.time())}"

        db.execute("INSERT INTO shops VALUES(?,?,?,?,?)",
                   (shop_id, name, 0, 0, None))
        db.commit()

        user_state[user_id] = {"mode": "shop_menu", "shop_id": shop_id}

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🏪 {name}",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                    QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                    QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
                ])))
        return True

    if text == "開始營業":
        sid = user_state[user_id]["shop_id"]
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return True

    if text == "今日休息":
        sid = user_state[user_id]["shop_id"]
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return True

    if text == "設定群組":
        user_state[user_id]["set_group"] = True
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請輸入群組連結", quick_reply=back_menu()))
        return True

    if user_state.get(user_id, {}).get("set_group"):
        sid = user_state[user_id]["shop_id"]
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text, sid))
        db.commit()
        user_state[user_id]["set_group"] = False

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已設定群組", quick_reply=back_menu()))
        return True

    return False


# ================= 店家管理 =================

def handle_admin_logic(event, user_id, text, db):
    if user_id not in ADMIN_IDS:
        return False

    if text == "店家管理":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🛠 店家管理",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="📋 查看店家", text="查看店家")),
                    QuickReplyButton(action=MessageAction(label="🗑 刪除店家", text="刪除店家")),
                    QuickReplyButton(action=MessageAction(label="✅ 審核店家", text="審核店家")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
                ])))
        return True

    if text == "查看店家":
        rows = db.execute("SELECT shop_id,name,approved FROM shops").fetchall()
        msg = "🏪 店家列表\n\n"
        for sid, name, ap in rows:
            msg += f"{name} | {'✅' if ap else '❌'}\n"

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(msg, quick_reply=back_menu()))
        return True

    if text == "刪除店家":
        rows = db.execute("SELECT shop_id,name FROM shops").fetchall()
        items = [QuickReplyButton(action=MessageAction(label=n, text=f"刪:{sid}")) for sid,n in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇刪除", quick_reply=QuickReply(items=items)))
        return True

    if text.startswith("刪:"):
        sid = text.split(":",1)[1]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return True

    if text == "審核店家":
        rows = db.execute("SELECT shop_id,name FROM shops WHERE approved=0").fetchall()
        items = [QuickReplyButton(action=MessageAction(label=n, text=f"審:{sid}")) for sid,n in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇審核", quick_reply=QuickReply(items=items)))
        return True

    if text.startswith("審:"):
        sid = text.split(":",1)[1]
        user_state[user_id] = {"audit": sid}

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("是否同意？",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="✅ 同意", text="同意")),
                    QuickReplyButton(action=MessageAction(label="❌ 不同意", text="不同意")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
                ])))
        return True

    if text in ["同意","不同意"] and user_state.get(user_id,{}).get("audit"):
        sid = user_state[user_id]["audit"]
        ok = 1 if text == "同意" else 0

        db.execute("UPDATE shops SET approved=? WHERE shop_id=?", (ok,sid))
        db.commit()
        user_state.pop(user_id,None)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已完成審核", quick_reply=back_menu()))
        return True

    return False


# ================= MAIN =================

if __name__ == "__main__":
    with app.app_context():
        init_db()

    app.run(host="0.0.0.0", port=5000)
