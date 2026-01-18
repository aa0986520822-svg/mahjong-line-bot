from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
from datetime import datetime, timedelta

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = "rWopP+p7jWSDT488yHxb5NWxi7ETwf3CCtleIWXbElaVZKkH+hpOCVheG9Hwo/KvgDLUy5RrSbPX1qj5pSqd9vXVKVkMPT31e4jrNx/VInx3SJpQPcEDOZstH7AbTKvokkVycfXcT0T0aveNKy2kZAdB04t89/1O/w1cDnyilFU="
LINE_CHANNEL_SECRET = "21ed83b842e88ced83a9f551a595390d"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

user_state = {}
ledger = {}


# ================= MENU =================

def main_menu():
    return TextSendMessage(
        text="請選擇功能：",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🀄 配桌", text="配桌")),
            QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
        ])
    )


def cancel_btn():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="❌ 取消返回", text="返回主選單"))
    ])


def ledger_menu():
    return TextSendMessage(
        text="📒 輸贏記事本",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="➕ 新增記帳", text="新增記帳")),
            QuickReplyButton(action=MessageAction(label="📅 當月結算", text="當月結算")),
            QuickReplyButton(action=MessageAction(label="📅 上月結算", text="上月結算")),
            QuickReplyButton(action=MessageAction(label="⬅ 返回", text="返回主選單")),
        ])
    )


# ================= WEBHOOK =================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if user_id not in user_state:
        user_state[user_id] = {}

    state = user_state[user_id]

    # ---------- global ----------
    if text == "返回主選單":
        state.clear()
        reply = main_menu()
        line_bot_api.reply_message(event.reply_token, reply)
        return

    # ---------- entry ----------
    if text in ["開始", "配桌"]:
        state.clear()
        reply = TextSendMessage(
            text="請輸入人數：",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="我1人", text="1")),
                QuickReplyButton(action=MessageAction(label="我2人", text="2")),
                QuickReplyButton(action=MessageAction(label="我3人", text="3")),
                QuickReplyButton(action=MessageAction(label="❌ 取消", text="返回主選單")),
            ])
        )
        state["step"] = "people"
        line_bot_api.reply_message(event.reply_token, reply)
        return

    # ---------- people ----------
    if state.get("step") == "people":
        state["people"] = text
        state["step"] = "done"
        reply = TextSendMessage(
            text=f"✅ 已選 {text} 人\n配桌完成！",
            quick_reply=cancel_btn()
        )
        line_bot_api.reply_message(event.reply_token, reply)
        return

    # ---------- ledger ----------
    if text == "輸贏記事本":
        state.clear()
        reply = ledger_menu()
        line_bot_api.reply_message(event.reply_token, reply)
        return

    if text == "新增記帳":
        state["step"] = "ledger_input"
        reply = TextSendMessage(
            text="請輸入金額（例如：1000 或 -500）",
            quick_reply=cancel_btn()
        )
        line_bot_api.reply_message(event.reply_token, reply)
        return

    if state.get("step") == "ledger_input":
        try:
            amount = int(text)
            today = datetime.now().strftime("%Y-%m-%d")

            if user_id not in ledger:
                ledger[user_id] = []

            ledger[user_id].append({
                "date": today,
                "amount": amount
            })

            state.clear()
            reply1 = TextSendMessage(text=f"✅ 已記錄 {today}：{amount}")
            reply2 = ledger_menu()
            line_bot_api.reply_message(event.reply_token, [reply1, reply2])
            return

        except:
            reply = TextSendMessage(text="請輸入正確數字，例如 1000 或 -300")
            line_bot_api.reply_message(event.reply_token, reply)
            return

    if text == "當月結算":
        now = datetime.now()
        total = 0

        for row in ledger.get(user_id, []):
            d = datetime.strptime(row["date"], "%Y-%m-%d")
            if d.year == now.year and d.month == now.month:
                total += row["amount"]

        reply1 = TextSendMessage(text=f"📅 本月結算：{total}")
        reply2 = ledger_menu()
        line_bot_api.reply_message(event.reply_token, [reply1, reply2])
        return

    if text == "上月結算":
        now = datetime.now()
        last_month = now.replace(day=1) - timedelta(days=1)
        total = 0

        for row in ledger.get(user_id, []):
            d = datetime.strptime(row["date"], "%Y-%m-%d")
            if d.year == last_month.year and d.month == last_month.month:
                total += row["amount"]

        reply1 = TextSendMessage(text=f"📅 上月結算：{total}")
        reply2 = ledger_menu()
        line_bot_api.reply_message(event.reply_token, [reply1, reply2])
        return

    # ---------- default ----------
    reply = main_menu()
    line_bot_api.reply_message(event.reply_token, reply)


# ================= RUN =================

if __name__ == "__main__":
    app.run(port=5000)
