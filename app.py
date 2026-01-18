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

tables = []
table_count = 0

GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"  # 換成你的群組連結


# ================= MENU =================

def main_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="📸 麻將計算機", text="麻將計算機")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
    ]
    return TextSendMessage("請選擇功能：", quick_reply=QuickReply(items=buttons))


def match_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="🪑 點桌加入", text="點桌加入")),
        QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
        QuickReplyButton(action=MessageAction(label="🔙 返回", text="選單")),
    ]
    return TextSendMessage("🎯 配桌功能：", quick_reply=QuickReply(items=buttons))


def people_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="👤 我1人", text="我1人")),
        QuickReplyButton(action=MessageAction(label="👥 我2人", text="我2人")),
        QuickReplyButton(action=MessageAction(label="👥 我3人", text="我3人")),
        QuickReplyButton(action=MessageAction(label="🔙 返回", text="配桌")),
    ]
    return TextSendMessage("請選擇加入人數：", quick_reply=QuickReply(items=buttons))


def ledger_menu():
    buttons = [
        QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
        QuickReplyButton(action=MessageAction(label="📊 本月結算", text="本月結算")),
        QuickReplyButton(action=MessageAction(label="📊 上月結算", text="上月結算")),
        QuickReplyButton(action=MessageAction(label="🔙 返回", text="選單")),
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
    global table_count

    user_id = event.source.user_id
    text = event.message.text.strip()

    if user_id not in ledger:
        ledger[user_id] = []

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

        if text == "我1人":
            add = 1
        elif text == "我2人":
            add = 2
        elif text == "我3人":
            add = 3
        else:
            line_bot_api.reply_message(event.reply_token, match_menu())
            return

        table_count += add
        tables.append(user_id)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"✅ 已加入 {add} 人\n目前人數：{table_count}/4\n等待成桌")
        )

        user_state[user_id] = None

        if table_count >= 4:
            for u in set(tables):
                line_bot_api.push_message(u, [
                    TextSendMessage("🎉 成桌成功！"),
                    TextSendMessage(f"👉 點擊加入群組：\n{GROUP_LINK}"),
                    TextSendMessage("請輸入：加入 或 放棄")
                ])
                user_state[u] = "confirm_join"

            table_count = 0
            tables.clear()

        return

    if text == "查看目前配桌":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"👀 目前等待人數：{table_count}/4")
        )
        return

    if user_state.get(user_id) == "confirm_join":
        if text == "加入":
            user_state[user_id] = None
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 請點連結加入群組"))
        elif text == "放棄":
            user_state[user_id] = None
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄本次配桌"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入：加入 或 放棄"))
        return

    if text == "麻將計算機":
        user_state[user_id] = "mahjong"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("📸 請上傳麻將照片"))
        return

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, ledger_menu())
        return

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
