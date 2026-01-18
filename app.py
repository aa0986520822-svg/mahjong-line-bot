import os, sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DB_PATH = "data.db"
GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"

user_state = {}
tables = {}

# ================= DB =================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        user_id TEXT,
        date TEXT,
        amount INTEGER
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS shops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        owner TEXT,
        approved INTEGER
    )
    """)

    conn.commit()
    conn.close()

# ================= MENU =================

def main_menu():
    return TextSendMessage("請選擇功能：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
    ]))

def match_money_menu():
    return TextSendMessage("選擇遊戲金額：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="30 / 10", text="30/10")),
        QuickReplyButton(action=MessageAction(label="50 / 20", text="50/20")),
        QuickReplyButton(action=MessageAction(label="100 / 20", text="100/20")),
        QuickReplyButton(action=MessageAction(label="100 / 50", text="100/50")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))

def match_menu():
    return TextSendMessage("🎯 配桌選單：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🪑 點桌加入", text="點桌加入")),
        QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))

def people_menu():
    return TextSendMessage("選擇人數：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="我1人", text="我1人")),
        QuickReplyButton(action=MessageAction(label="我2人", text="我2人")),
        QuickReplyButton(action=MessageAction(label="我3人", text="我3人")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))

def ledger_menu():
    return TextSendMessage("📒 輸贏記事本：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
        QuickReplyButton(action=MessageAction(label="📊 本月結算", text="本月結算")),
        QuickReplyButton(action=MessageAction(label="📊 上月結算", text="上月結算")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))

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
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    # ===== 配桌 =====

    if text == "配桌":
        user_state[user_id] = "choose_money"
        line_bot_api.reply_message(event.reply_token, match_money_menu())
        return

    if user_state.get(user_id) == "choose_money":
        tables.setdefault(text, {})
        user_state[user_id] = ("match_menu", text)
        line_bot_api.reply_message(event.reply_token, match_menu())
        return

    if text == "點桌加入":
        user_state[user_id] = ("choose_people", user_state[user_id][1])
        line_bot_api.reply_message(event.reply_token, people_menu())
        return

    if isinstance(user_state.get(user_id), tuple) and user_state[user_id][0] == "choose_people":
        money = user_state[user_id][1]
        add = {"我1人":1,"我2人":2,"我3人":3}.get(text)
        if not add:
            line_bot_api.reply_message(event.reply_token, main_menu())
            return

        tables[money][user_id] = add
        total = sum(tables[money].values())

        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已加入 {add} 人\n目前 {total}/4",
            quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))

        if total >= 4:
            for u in tables[money]:
                line_bot_api.push_message(u, TextSendMessage(
                    f"🎉 成桌成功 ({money})\n👉 {GROUP_LINK}",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                        QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
                    ])
                ))
            tables[money] = {}

        user_state[user_id] = None
        return

    if text == "查看目前配桌":
        msg = "\n".join([f"{k}：{sum(v.values())}/4" for k,v in tables.items()]) or "目前沒有配桌"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg,
            quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
        return

    if text == "加入":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已加入",
            quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
        return

    if text == "放棄":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄",
            quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
        return

    # ===== 記帳 =====

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, ledger_menu())
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額",
            quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
        return

    if user_state.get(user_id) == "add_money":
        try:
            amt = int(text)
            conn = get_db()
            conn.execute("INSERT INTO ledger VALUES (?,?,?)",
                         (user_id, datetime.now().strftime("%Y-%m-%d"), amt))
            conn.commit()
            conn.close()
            user_state[user_id] = None
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已紀錄",
                quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入數字"))
        return

    line_bot_api.reply_message(event.reply_token, main_menu())

# ================= RUN =================

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
