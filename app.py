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
table_users = set()
table_count = 0

GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"


# ================= MENU =================

def main_menu():
    return TextSendMessage("請選擇功能：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🀄 麻將計算機", text="麻將計算機")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="輸贏記事本")),
    ]))


def match_menu():
    return TextSendMessage("🎯 配桌功能：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🪑 點桌加入", text="點桌加入")),
        QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
        QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單")),
    ]))


def people_menu():
    return TextSendMessage("請選擇加入人數：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="👤 我1人", text="我1人")),
        QuickReplyButton(action=MessageAction(label="👥 我2人", text="我2人")),
        QuickReplyButton(action=MessageAction(label="👥 我3人", text="我3人")),
    ]))


def joined_menu():
    return TextSendMessage("已加入配桌：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
        QuickReplyButton(action=MessageAction(label="❌ 退出配桌", text="退出配桌")),
        QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單")),
    ]))


def mahjong_menu():
    return TextSendMessage("🀄 麻將計算機：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="📸 拍照辨識", text="拍照辨識")),
        QuickReplyButton(action=MessageAction(label="⌨ 手動輸入", text="手動輸入")),
        QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單")),
    ]))


def ledger_menu():
    return TextSendMessage("📒 輸贏記事本：", quick_reply=QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
        QuickReplyButton(action=MessageAction(label="📊 本月結算", text="本月結算")),
        QuickReplyButton(action=MessageAction(label="📊 上月結算", text="上月結算")),
        QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單")),
    ]))


# ================= UTILS =================

def parse_tiles(text):
    tiles = []
    num = ""
    for c in text.replace(" ", ""):
        if c.isdigit():
            num += c
        elif c in ["萬", "筒", "條"]:
            for n in num:
                tiles.append(n + c)
            num = ""
        elif c in ["東", "南", "西", "北", "中", "發", "白"]:
            tiles.append(c)
    return tiles


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

    ledger.setdefault(user_id, [])

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    if text == "配桌":
        line_bot_api.reply_message(event.reply_token, match_menu())
        return

    if text == "點桌加入":
        if user_id in table_users:
            line_bot_api.reply_message(event.reply_token, joined_menu())
            return
        user_state[user_id] = "choose_people"
        line_bot_api.reply_message(event.reply_token, people_menu())
        return

    if user_state.get(user_id) == "choose_people":
        add = {"我1人": 1, "我2人": 2, "我3人": 3}.get(text)
        if not add:
            return

        table_users.add(user_id)
        tables.append((user_id, add))
        table_count += add

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 已加入 {add} 人\n目前 {table_count}/4", quick_reply=joined_menu().quick_reply))

        if table_count >= 4:
            for u, _ in tables:
                line_bot_api.push_message(u, TextSendMessage(f"🎉 成桌成功！\n{GROUP_LINK}"))
            tables.clear()
            table_users.clear()
            table_count = 0

        user_state[user_id] = None
        return

    if text == "查看目前配桌":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"👀 目前等待人數：{table_count}/4", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🔙 返回主選單", text="選單"))
            ])))
        return

    if text == "退出配桌":
        if user_id in table_users:
            table_users.remove(user_id)
        line_bot_api.reply_message(event.reply_token, main_menu())
        return

    if text == "麻將計算機":
        line_bot_api.reply_message(event.reply_token, mahjong_menu())
        return

    if text == "拍照辨識":
        user_state[user_id] = "mahjong_photo"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("📸 請橫放拍照", quick_reply=mahjong_menu().quick_reply))
        return

    if text == "手動輸入":
        user_state[user_id] = "mahjong_manual"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請輸入：123567萬 345筒 789條 東東", quick_reply=mahjong_menu().quick_reply))
        return

    if user_state.get(user_id) == "mahjong_manual":
        tiles = parse_tiles(text)
        count = len(tiles)

        if count not in [16, 13, 10, 7, 4]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(f"{count} 張無法計算", quick_reply=mahjong_menu().quick_reply))
            return

        melds = (16 - count) // 3
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🀄 張數:{count}\n副露:{melds}\n示範聽牌：5萬 8萬", quick_reply=mahjong_menu().quick_reply))
        user_state[user_id] = None
        return

    if text == "輸贏記事本":
        line_bot_api.reply_message(event.reply_token, ledger_menu())
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額"))
        return

    if user_state.get(user_id) == "add_money":
        try:
            amt = int(text)
            ledger[user_id].append({"date": datetime.now().strftime("%Y-%m-%d"), "amount": amt})
            user_state[user_id] = None
            line_bot_api.reply_message(event.reply_token, ledger_menu())
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入數字"))
        return

    line_bot_api.reply_message(event.reply_token, main_menu())


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    if user_state.get(user_id) == "mahjong_photo":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🀄 示範辨識完成 → 聽：3萬 6筒", quick_reply=mahjong_menu().quick_reply))
        user_state[user_id] = None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
