import os
import json
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

# ================= MENU =================

def main_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="📸 麻將計算機", text="麻將計算機")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
    ]
    return TextSendMessage("請選擇功能：", quick_reply=QuickReply(items=buttons))


def ledger_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
        QuickReplyButton(action=MessageAction(label="📊 本月結算", text="本月結算")),
        QuickReplyButton(action=MessageAction(label="📊 上月結算", text="上月結算")),
        QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單")),
    ]
    return TextSendMessage("📒 輸贏記事本：", quick_reply=QuickReply(items=buttons))


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

    if user_id not in ledger:
        ledger[user_id] = []

    # ----- main -----
    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    def table_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="✅ 配桌", text="配桌_配桌")),
        QuickReplyButton(action=MessageAction(label="👥 人數", text="配桌_人數")),
        QuickReplyButton(action=MessageAction(label="📍 地點", text="配桌_地點")),
        QuickReplyButton(action=MessageAction(label="⏳ 等待通知", text="配桌_等待")),
        QuickReplyButton(action=MessageAction(label="📣 成桌通知", text="配桌_完成")),
        QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單")),
    ]
    return TextSendMessage("🎯 配桌設定：", quick_reply=QuickReply(items=buttons))


    if text == "麻將計算機":
        user_state[user_id] = "mahjong"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("📸 請上傳麻將照片"))
        return

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, ledger_menu())
        return

    # ----- ledger -----
    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額 (贏=正數 / 輸=-數字)"))
        return

    if text == "本月結算":
        now = datetime.now()
        total = 0
        for row in ledger[user_id]:
            d = datetime.strptime(row["date"], "%Y-%m-%d")
            if d.year == now.year and d.month == now.month:
                total += row["amount"]

        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(f"📊 本月結算：{total}"),
            ledger_menu()
        ])
        return

    if text == "上月結算":
        now = datetime.now()
        last = now.replace(day=1) - timedelta(days=1)
        total = 0

        for row in ledger[user_id]:
            d = datetime.strptime(row["date"], "%Y-%m-%d")
            if d.year == last.year and d.month == last.month:
                total += row["amount"]

        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(f"📊 上月結算：{total}"),
            ledger_menu()
        ])
        return

    # ----- input money -----
    if user_state.get(user_id) == "add_money":
        try:
            amt = int(text)
            ledger[user_id].append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "amount": amt
            })
            user_state[user_id] = None

            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(f"✅ 已紀錄：{amt}"),
                ledger_menu()
            ])
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入數字，例如：100 或 -50"))
        return

    line_bot_api.reply_message(event.reply_token, main_menu())


# ================= IMAGE =================

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id

    if user_state.get(user_id) == "mahjong":
        reply = TextSendMessage("🀄 辨識完成：\n目前示範 → 聽：三萬、六筒")
        line_bot_api.reply_message(event.reply_token, reply)
        user_state[user_id] = None
        return


# ================= RUN =================
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)



