import os, sqlite3
from datetime import datetime
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
    "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # 換成你的 LINE USER ID
}

DB_PATH = "data.db"
user_state = {}

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
        people INT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS ledger(
        user_id TEXT,
        amount INT,
        time TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
        name TEXT,
        open INT,
        approved INT
    )""")
    db.commit()

# ================= MENU =================

def main_menu():
    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
        QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")),
    ]))

def back_menu():
    return QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])

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

    if text in ["選單","menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    # ================= 管理員控管 =================

    if user_id in ADMIN_IDS and text == "店家管理":
        cur = db.execute("SELECT shop_id,name,open,approved FROM shops")
        rows = cur.fetchall()

        msg = "🛠 店家管理清單\n\n"
        for sid,n,o,a in rows:
            msg += f"{n}\nID:{sid}\n狀態:{'營業' if o else '停用'} / {'核准' if a else '未核准'}\n\n"

        msg += "指令：\n核准 ID\n停用 ID\n刪除 ID"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if user_id in ADMIN_IDS and text.startswith("核准"):
        sid = text.replace("核准","").strip()
        db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已核准"))
        return

    if user_id in ADMIN_IDS and text.startswith("停用"):
        sid = text.replace("停用","").strip()
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("⛔ 已停用"))
        return

    if user_id in ADMIN_IDS and text.startswith("刪除"):
        sid = text.replace("刪除","").strip()
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除"))
        return

    # ================= 店家配桌 =================

    if text == "店家配桌":
        cur = db.execute("SELECT name FROM shops WHERE open=1 AND approved=1")
        shops = cur.fetchall()
        msg = "🏪 營業中店家\n\n"
        for s, in shops:
            msg += f"✅ {s}\n"
        if not shops:
            msg += "目前沒有營業店家"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    # ================= 店家後台 =================

    if text == "店家後台":
        cur = db.execute("SELECT * FROM shops WHERE shop_id=?", (user_id,))
        shop = cur.fetchone()

        if not shop:
            user_state[user_id] = "register_shop"
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入麻將館名稱"))
            return

        if shop[3] == 0:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("⏳ 審核中", quick_reply=back_menu()))
            return

        status = "營業中" if shop[2] else "休息中"

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🏪 {shop[1]}\n目前狀態：{status}", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if user_state.get(user_id) == "register_shop":
        db.execute("INSERT INTO shops VALUES(?,?,?,?)",(user_id,text,0,0))
        db.commit()
        user_state[user_id] = None

        for admin in ADMIN_IDS:
            line_bot_api.push_message(admin, TextSendMessage(
                f"📩 新店家申請\n\n{text}\nID:{user_id}\n\n輸入：核准 {user_id}"
            ))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已送出申請，等待審核", quick_reply=back_menu()))
        return

    if text == "開始營業":
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return

    if text == "今日休息":
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return

    line_bot_api.reply_message(event.reply_token, main_menu())


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
