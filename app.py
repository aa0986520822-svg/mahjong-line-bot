import os, sqlite3
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

GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}
shop_match_state = {}

# ================= DB =================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT,
        price TEXT,
        people INT,
        shop_id TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
        name TEXT,
        open INT,
        approved INT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS ledger(
        user_id TEXT,
        amount INT,
        time TEXT
    )""")
    db.commit()

# ================= MENU =================

def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🏪 指定店家", text="指定店家")),
        QuickReplyButton(action=MessageAction(label="📒 記事本", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")),
    ]
    if user_id in ADMIN_IDS:
        items.append(QuickReplyButton(action=MessageAction(label="🛠 店家管理", text="店家管理")))
    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=items))

def back_menu():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))
    ])

# ================= WEBHOOK =================

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ================= MESSAGE =================

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()
    user_id = event.source.user_id
    text = event.message.text.strip()

    # ========= 主選單 =========

    if text in ["選單","menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ========= 配桌 =========

    if text == "配桌":
        if db.execute("SELECT 1 FROM match_users WHERE user_id=?", (user_id,)).fetchone():
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("你已在配桌中", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="📋 查看目前配桌", text="查看配桌")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
            return

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇金額", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="30/10", text="30/10")),
                QuickReplyButton(action=MessageAction(label="50/20", text="50/20")),
                QuickReplyButton(action=MessageAction(label="100/20", text="100/20")),
                QuickReplyButton(action=MessageAction(label="100/50", text="100/50")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    # ========= 指定店家 =========

    if text == "指定店家":
        shops = db.execute("SELECT shop_id,name FROM shops WHERE open=1 AND approved=1").fetchall()

        if not shops:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有上線店家", quick_reply=back_menu()))
            return

        items = [QuickReplyButton(action=MessageAction(label=f"🏪 {n}", text=f"選店:{i}")) for i,n in shops]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("選店:"):
        shop_match_state[user_id] = text.split(":")[1]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇金額", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="30/10", text="30/10")),
                QuickReplyButton(action=MessageAction(label="50/20", text="50/20")),
                QuickReplyButton(action=MessageAction(label="100/20", text="100/20")),
                QuickReplyButton(action=MessageAction(label="100/50", text="100/50")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    # ========= 金額 =========

    if text in ["30/10","50/20","100/20","100/50"]:
        if db.execute("SELECT 1 FROM match_users WHERE user_id=?", (user_id,)).fetchone():
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("你已在配桌中", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="📋 查看目前配桌", text="查看配桌")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
            return

        user_state[user_id] = text

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇人數", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="我1人", text="我1人")),
                QuickReplyButton(action=MessageAction(label="我2人", text="我2人")),
                QuickReplyButton(action=MessageAction(label="我3人", text="我3人")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    # ========= 人數 =========

    if text in ["我1人","我2人","我3人"] and user_id in user_state:
        people = int(text[1])
        price = user_state[user_id]
        shop_id = shop_match_state.get(user_id)

        db.execute("INSERT INTO match_users VALUES(?,?,?,?)",(user_id,price,people,shop_id))
        db.commit()

        total = db.execute(
            "SELECT SUM(people) FROM match_users WHERE price=? AND shop_id IS ?",
            (price,shop_id)
        ).fetchone()[0]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 已加入 {price}\n目前 {total}/4", quick_reply=back_menu()))

        if total >= 4:
            users = db.execute(
                "SELECT user_id FROM match_users WHERE price=? AND shop_id IS ?",
                (price,shop_id)
            ).fetchall()

            for (u,) in users:
                line_bot_api.push_message(u, TextSendMessage(f"🎉 成桌成功\n{GROUP_LINK}"))

            if shop_id:
                line_bot_api.push_message(shop_id, TextSendMessage(f"🎉 玩家已成桌\n{GROUP_LINK}"))

            db.execute("DELETE FROM match_users WHERE price=? AND shop_id IS ?", (price,shop_id))
            db.commit()
        return

    # ========= 查看 / 取消 =========

    if text == "查看配桌":
        row = db.execute("SELECT price,people FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(f"目前：{row[0]} / {row[1]}人", quick_reply=back_menu()))
        return

    if text == "取消配桌":
        db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("已取消配桌", quick_reply=back_menu()))
        return

    # ========= 記事本 =========

    if text == "記事本":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("📒 記事本", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📅 查看當月", text="查看當月")),
                QuickReplyButton(action=MessageAction(label="⏪ 查看上月", text="查看上月")),
                QuickReplyButton(action=MessageAction(label="🧹 清除紀錄", text="清除紀錄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額 (+ / -)"))
        return

    if user_state.get(user_id) == "add_money":
        try:
            amt = int(text)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入正確數字"))
            return

        now = datetime.now()
        db.execute("INSERT INTO ledger VALUES(?,?,?)",
            (user_id, amt, now.strftime("%Y-%m-%d %H:%M:%S")))

        db.commit()
        user_state[user_id] = None
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已紀錄", quick_reply=back_menu()))
        return

    if text == "查看當月":
        month = datetime.now().strftime("%Y-%m")
        rows = db.execute(
            "SELECT amount,time FROM ledger WHERE user_id=? AND time LIKE ?",
            (user_id, f"{month}%")
        ).fetchall()

        msg = "📅 本月紀錄\n\n"
        for a,t in rows:
            msg += f"{t} : {a}\n"
        if not rows: msg += "尚無紀錄"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "查看上月":
        last = (datetime.now().replace(day=1)-timedelta(days=1)).strftime("%Y-%m")
        rows = db.execute(
            "SELECT amount,time FROM ledger WHERE user_id=? AND time LIKE ?",
            (user_id, f"{last}%")
        ).fetchall()

        msg = "⏪ 上月紀錄\n\n"
        for a,t in rows:
            msg += f"{t} : {a}\n"
        if not rows: msg += "尚無紀錄"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "清除紀錄":
        db.execute("DELETE FROM ledger WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🧹 已清除", quick_reply=back_menu()))
        return


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
