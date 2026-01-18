import os
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

ledger = {}
user_state = {}
shops = {}

tables = []
table_count = 0
table_no = 1

GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"


# ================= MENU =================

def main_menu():
    return TextSendMessage(
        "請選擇功能：",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
            QuickReplyButton(action=MessageAction(label="📸 麻將計算機", text="麻將計算機")),
            QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
        ])
    )


def match_menu():
    return TextSendMessage(
        "🎯 配桌功能：",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🪑 點桌加入", text="點桌加入")),
            QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
        ])
    )


def people_menu():
    return TextSendMessage(
        "請選擇加入人數：",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="👤 我1人", text="我1人")),
            QuickReplyButton(action=MessageAction(label="👥 我2人", text="我2人")),
            QuickReplyButton(action=MessageAction(label="👥 我3人", text="我3人")),
        ])
    )


def ledger_menu():
    return TextSendMessage(
        "📒 輸贏記事本：",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
            QuickReplyButton(action=MessageAction(label="📊 本月結算", text="本月結算")),
            QuickReplyButton(action=MessageAction(label="📊 上月結算", text="上月結算")),
            QuickReplyButton(action=MessageAction(label="🔙 返回", text="選單")),
        ])
    )


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
    global table_count, table_no

    user_id = event.source.user_id
    text = event.message.text.strip()

    ledger.setdefault(user_id, [])

    # ===== 店家指令 =====

    if text.startswith("/註冊"):
        name = text.replace("/註冊", "").strip()
        shops[user_id] = {"name": name, "open": False}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"🏪 已註冊店家：{name}"))
        return

    if text == "/開店" and user_id in shops:
        shops[user_id]["open"] = True
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 今日營業中"))
        return

    if text == "/關店" and user_id in shops:
        shops[user_id]["open"] = False
        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 今日未營業"))
        return

    if text == "/狀態" and user_id in shops:
        s = "營業中" if shops[user_id]["open"] else "未營業"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"📌 狀態：{s}"))
        return

    # ===== 主選單 =====

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    if text == "配桌":
        line_bot_api.reply_message(event.reply_token, match_menu())
        return

    if text == "點桌加入":
        user_state[user_id] = "choose_people"
        line_bot_api.reply_message(event.reply_token, people_menu())
        return

    if user_state.get(user_id) == "choose_people":
        add = {"我1人":1,"我2人":2,"我3人":3}.get(text)
        if not add:
            line_bot_api.reply_message(event.reply_token, match_menu())
            return

        if user_id not in tables:
            tables.append(user_id)
            table_count += add

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 配桌編號 #{table_no}\n目前人數 {table_count}/4"))

        if table_count >= 4:
            for u in tables:
                line_bot_api.push_message(u, TextSendMessage(f"🎉 成桌成功 #{table_no}\n{GROUP_LINK}"))
            table_count = 0
            tables.clear()
            table_no += 1

        user_state[user_id] = None
        return

    if text == "查看目前配桌":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"👀 目前人數 {table_count}/4"))
        return

    # ===== 麻將 =====

    if text == "麻將計算機":
        user_state[user_id] = "mahjong"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("📸 上傳照片 或 輸入手牌"))
        return

    # ===== 記事本 =====

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, ledger_menu())
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("輸入金額 正負皆可"))
        return

    if user_state.get(user_id) == "add_money":
        try:
            amt = int(text)
            ledger[user_id].append({"date":datetime.now().strftime("%Y-%m-%d"),"amount":amt})
            user_state[user_id] = None
            line_bot_api.reply_message(event.reply_token,
                [TextSendMessage(f"✅ 已存 {amt}"), ledger_menu()])
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入數字"))
        return

    if text == "本月結算":
        now = datetime.now()
        total = sum(r["amount"] for r in ledger[user_id]
            if datetime.strptime(r["date"],"%Y-%m-%d").month==now.month)
        line_bot_api.reply_message(event.reply_token,
            [TextSendMessage(f"📊 本月 {total}"), ledger_menu()])
        return

    if text == "上月結算":
        last = (datetime.now().replace(day=1)-timedelta(days=1))
        total = sum(r["amount"] for r in ledger[user_id]
            if datetime.strptime(r["date"],"%Y-%m-%d").month==last.month)
        line_bot_api.reply_message(event.reply_token,
            [TextSendMessage(f"📊 上月 {total}"), ledger_menu()])
        return

    line_bot_api.reply_message(event.reply_token, main_menu())


# ================= IMAGE =================

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    if user_state.get(event.source.user_id) == "mahjong":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🀄 示範：聽 三萬 六筒"))
        user_state[event.source.user_id] = None


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
