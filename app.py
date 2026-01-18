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

tables = {}
table_users = set()

shops = {}      # shop_id -> info
pending_shops = {}

GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"


# ================= MENU =================

def main_menu():
    return TextSendMessage("請選擇功能：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="🀄 麻將計算機", text="麻將計算機")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
    ]))


def match_menu():
    return TextSendMessage("🎯 配桌選單：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🪑 點桌加入", text="點桌加入")),
        QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))


def people_menu():
    return TextSendMessage("選擇人數：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="👤 我1人", text="我1人")),
        QuickReplyButton(action=MessageAction(label="👥 我2人", text="我2人")),
        QuickReplyButton(action=MessageAction(label="👥 我3人", text="我3人")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))


def mahjong_menu():
    return TextSendMessage("🀄 麻將計算機：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="📸 拍照", text="拍照")),
        QuickReplyButton(action=MessageAction(label="✍️ 手動輸入", text="手動輸入")),
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
    ]))


def mahjong_state_menu():
    return TextSendMessage("請選擇狀態：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="✅ 無吃碰槓", text="無吃碰槓")),
        QuickReplyButton(action=MessageAction(label="🔄 有吃碰槓", text="有吃碰槓")),
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

    if user_id not in ledger:
        ledger[user_id] = []

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    # ===== 配桌 =====

    if text == "配桌":
        line_bot_api.reply_message(event.reply_token, match_menu())
        return

    if text == "點桌加入":
        user_state[user_id] = "choose_people"
        line_bot_api.reply_message(event.reply_token, people_menu())
        return

    if user_state.get(user_id) == "choose_people":
        add = {"我1人": 1, "我2人": 2, "我3人": 3}.get(text)
        if not add:
            line_bot_api.reply_message(event.reply_token, main_menu())
            return

        if user_id in table_users:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⚠️ 已加入配桌"))
            return

        table_users.add(user_id)
        tables[user_id] = add
        total = sum(tables.values())

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 已加入 {add} 人\n目前 {total}/4"))

        if total >= 4:
            for u in tables:
                line_bot_api.push_message(u, TextSendMessage(
                    f"🎉 成桌成功\n👉 {GROUP_LINK}\n請點選：加入 或 放棄",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                        QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
                    ])
                ))
            tables.clear()
            table_users.clear()

        user_state[user_id] = None
        return

    if text == "加入":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已確認加入"))
        return

    if text == "放棄":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄"))
        return

    # ===== 麻將 =====

    if text == "麻將計算機":
        line_bot_api.reply_message(event.reply_token, mahjong_menu())
        return

    if text == "拍照":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🚧 拍照辨識功能待更新"))
        return

    if text == "手動輸入":
        user_state[user_id] = "mahjong_manual"
        line_bot_api.reply_message(event.reply_token, mahjong_state_menu())
        return

    if user_state.get(user_id) == "mahjong_manual":
        remain = {"無吃碰槓": 14, "有吃碰槓": 11}.get(text)
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🀄 剩餘張數約 {remain} 張"))
        user_state[user_id] = None
        return

    # ===== 記帳 =====

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, ledger_menu())
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("輸入金額"))
        return

    if user_state.get(user_id) == "add_money":
        amt = int(text)
        ledger[user_id].append({"date": datetime.now().strftime("%Y-%m-%d"), "amount": amt})
        user_state[user_id] = None
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已紀錄"))
        return

    line_bot_api.reply_message(event.reply_token, main_menu())


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
