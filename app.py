import os, json, uuid, datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
from collections import Counter

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DATA_FILE = "records.json"

# -------------------------
# 工具
# -------------------------

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# -------------------------
# 麻將算法
# -------------------------

def is_win(hand):
    if len(hand) % 3 != 2:
        return False

    counter = Counter(hand)

    def dfs(cnt):
        for k in list(cnt.keys()):
            if cnt[k] > 0:
                break
        else:
            return True

        if cnt[k] >= 3:
            cnt[k] -= 3
            if dfs(cnt):
                return True
            cnt[k] += 3

        if k[1] in "mps":
            n = int(k[0])
            k2 = f"{n+1}{k[1]}"
            k3 = f"{n+2}{k[1]}"
            if cnt[k2] > 0 and cnt[k3] > 0:
                cnt[k] -= 1
                cnt[k2] -= 1
                cnt[k3] -= 1
                if dfs(cnt):
                    return True
                cnt[k] += 1
                cnt[k2] += 1
                cnt[k3] += 1

        return False

    for k in counter:
        if counter[k] >= 2:
            counter[k] -= 2
            if dfs(counter):
                return True
            counter[k] += 2

    return False


def calculate_ting(hand_str):
    tiles = [hand_str[i:i+2] for i in range(0, len(hand_str), 2)]
    all_tiles = [f"{i}{s}" for s in "mps" for i in range(1,10)]

    result = []
    for t in all_tiles:
        test = tiles + [t]
        if is_win(test):
            result.append(t)

    return "、".join(result) if result else "尚未聽牌"


# ⚠️ 暫時模擬 AI（之後可換 YOLO）
def ai_detect_tiles(path):
    # 模擬回傳
    return "1m2m3m4m5m6m7m8m9m1p1p1p1s"


# -------------------------
# LINE Webhook
# -------------------------

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


# -------------------------
# 主選單
# -------------------------

def main_menu():
    return TemplateSendMessage(
        alt_text="選單",
        template=ButtonsTemplate(
            title="🀄 麻將 AI 助手",
            text="請選擇功能",
            actions=[
                MessageAction(label="🎯 配桌", text="配桌"),
                MessageAction(label="📸 麻將計算機", text="麻將計算機"),
                MessageAction(label="📒 輸贏記事本", text="輸贏記事本"),
                MessageAction(label="📊 本月結算", text="本月結算")
            ]
        )
    )


# -------------------------
# 文字事件
# -------------------------

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text
    user_id = event.source.user_id

    if text in ["選單", "開始"]:
        line_bot_api.reply_message(event.reply_token, main_menu())

    elif text == "麻將計算機":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text="📸 請直接拍照上傳你的手牌"))

    elif text == "配桌":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text="🎯 配桌功能開發中"))

    elif text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text="💰 請直接輸入金額，例如：1000 或 -500"))

    elif text == "本月結算":
        data = load_data()
        total = sum(data.get(user_id, []))
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(text=f"📊 本月結算：{total}"))

    else:
        try:
            money = int(text)
            data = load_data()
            data.setdefault(user_id, []).append(money)
            save_data(data)
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text=f"✅ 已記錄 {money}\n輸入 選單 返回"))
        except:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text="請輸入正確金額或點選選單"))


# -------------------------
# 圖片事件
# -------------------------

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    content = line_bot_api.get_message_content(event.message.id)

    path = f"/tmp/{uuid.uuid4()}.jpg"
    with open(path, "wb") as f:
        for chunk in content.iter_content():
            f.write(chunk)

    line_bot_api.reply_message(event.reply_token,
        TextSendMessage(text="📸 已收到，AI 分析中..."))

    tiles = ai_detect_tiles(path)
    ting = calculate_ting(tiles)

    line_bot_api.push_message(user_id,
        TextSendMessage(text=f"🀄 手牌：{tiles}\n🎯 聽牌：{ting}"))


# -------------------------

if __name__ == "__main__":
    app.run()
