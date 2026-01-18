import os
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

from datetime import datetime, timedelta

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("rWopP+p7jWSDT488yHxb5NWxi7ETwf3CCtleIWXbElaVZKkH+hpOCVheG9Hwo/KvgDLUy5RrSbPX1qj5pSqd9vXVKVkMPT31e4jrNx/VInx3SJpQPcEDOZstH7AbTKvokkVycfXcT0T0aveNKy2kZAdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.getenv("21ed83b842e88ced83a9f551a595390d")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

user_state = {}
ledger = {}

# ------------------- MENU -------------------

def main_menu():
    return TemplateSendMessage(
        alt_text="主選單",
        template=ButtonsTemplate(
            title="🀄 麻將 AI 助手",
            text="請選擇功能",
            actions=[
                MessageAction(label="🎯 配桌", text="配桌"),
                MessageAction(label="📸 麻將計算機", text="麻將計算機"),
                MessageAction(label="📒 輸贏記事本", text="輸贏記事本"),
                MessageAction(label="📊 本月結算", text="本月結算"),
            ]
        )
    )

# ------------------- CALLBACK -------------------

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"

# ------------------- TEXT -------------------

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    if text == "麻將計算機":
        user_state[user_id] = "mahjong_ai"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="📸 請拍照上傳你的手牌，我幫你算聽什麼牌")
        )
        return

    if text == "配桌":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🎯 配桌功能尚未擴充"))
        return

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📒 記帳功能尚未擴充"))
        return

    if text == "本月結算":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📊 本月結算尚未擴充"))
        return

    line_bot_api.reply_message(event.reply_token, main_menu())


# ------------------- IMAGE -------------------

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id

    if user_state.get(user_id) != "mahjong_ai":
        return

    # 之後這裡可以接 AI 辨識
    result = "🀄 分析完成\n\n➡ 聽牌：\n3萬、6萬、白板"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=result)
    )

    user_state[user_id] = None


# ------------------- RUN -------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
