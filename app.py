import os, sqlite3, threading, time
from datetime import datetime, timedelta
from flask import Flask, request, abort, g
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SYSTEM_GROUP_LINK = "https://line.me/R/ti/g/一般玩家群"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}

# ================= DB =================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
        name TEXT,
        open INT,
        approved INT,
        group_link TEXT
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS ledger(
        user_id TEXT,
        amount INT,
        time TEXT
    )""")

    db.commit()


# ================= MENU =================

def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🏪 指定店家", text="指定店家")),
        QuickReplyButton(action=MessageAction(label="📒 記事本", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")),
    ]

    if user_id in ADMIN_IDS:
        items.append(
            QuickReplyButton(action=MessageAction(label="🛠 店家管理", text="店家管理"))
        )

    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=items))


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

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ===== 管理員 =====

    if user_id in ADMIN_IDS and text == "店家管理":
        rows = db.execute("SELECT shop_id,name,open,approved FROM shops").fetchall()

        msgs = []
        for sid, n, o, a in rows:
            status = f"{'營業' if o else '停用'} / {'核准' if a else '未核准'}"
            msgs.append(TextSendMessage(
                f"🏪 {n}\n{status}",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="✅ 核准", text=f"核准:{sid}")),
                    QuickReplyButton(action=MessageAction(label="⛔ 停用", text=f"停用:{sid}")),
                    QuickReplyButton(action=MessageAction(label="🔗 群組", text=f"群組:{sid}")),
                    QuickReplyButton(action=MessageAction(label="🗑 刪除", text=f"刪除:{sid}")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])
            ))

        if not msgs:
            msgs = [TextSendMessage("目前沒有店家", quick_reply=back_menu())]

        line_bot_api.reply_message(event.reply_token, msgs)
        return

    if user_id in ADMIN_IDS and text.startswith("核准:"):
        sid = text.split(":")[1]
        db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已核准", quick_reply=back_menu()))
        return

    if user_id in ADMIN_IDS and text.startswith("停用:"):
        sid = text.split(":")[1]
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("⛔ 已停用", quick_reply=back_menu()))
        return

    if user_id in ADMIN_IDS and text.startswith("群組:"):
        sid = text.split(":")[1]
        user_state[user_id] = f"admin_set_group:{sid}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入新的群組連結"))
        return

    if user_state.get(user_id, "").startswith("admin_set_group"):
        sid = user_state[user_id].split(":")[1]
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text, sid))
        db.commit()
        user_state[user_id] = None
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新群組", quick_reply=back_menu()))
        return

    if user_id in ADMIN_IDS and text.startswith("刪除:"):
        sid = text.split(":")[1]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return

    # ===== 指定店家 =====

    if text == "指定店家":
        shops = db.execute("SELECT shop_id,name FROM shops WHERE open=1 AND approved=1").fetchall()

        if not shops:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有上線店家", quick_reply=back_menu()))
            return

        items = [QuickReplyButton(action=MessageAction(label=f"🏪 {n}", text=f"進入:{i}")) for i, n in shops]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("進入:"):
        sid = text.split(":")[1]
        row = db.execute("SELECT group_link,name FROM shops WHERE shop_id=?", (sid,)).fetchone()

        if not row or not row[0]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此店尚未設定群組"))
            return

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🏪 {row[1]}\n\n🔗 {row[0]}", quick_reply=back_menu()))
        return

    # ===== 店家後台 =====

    if text == "店家後台":
        shop = db.execute("SELECT * FROM shops WHERE shop_id=?", (user_id,)).fetchone()

        if not shop:
            user_state[user_id] = "register_shop"
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入麻將館名稱"))
            return

        if shop[3] == 0:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("⏳ 審核中，請等待管理員通過", quick_reply=back_menu()))
            return

        status = "營業中" if shop[2] else "休息中"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                f"🏪 {shop[1]}\n目前狀態：{status}",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                    QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])
            )
        )
        return

    if user_state.get(user_id) == "register_shop":
        db.execute("INSERT INTO shops VALUES(?,?,?,?,?)", (user_id, text, 0, 0, None))
        db.commit()
        user_state[user_id] = None

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已送出申請，等待審核", quick_reply=back_menu()))

        for admin in ADMIN_IDS:
            line_bot_api.push_message(admin, TextSendMessage(
                f"📩 新店家申請\n\n店名：{text}\nID：{user_id}"
            ))
        return

    if text == "開始營業":
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return

    if text == "今日休息":
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return

    # ===== 記事本 =====

    if text == "記事本":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("📒 記事本", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📅 查看當月", text="查看當月")),
                QuickReplyButton(action=MessageAction(label="⏪ 查看上月", text="查看上月")),
                QuickReplyButton(action=MessageAction(label="🧹 清除紀錄", text="清除紀錄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額 (+ / -)"))
        return

    if user_state.get(user_id) == "add_money":
        try:
            amt = int(text)
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入正確數字"))
            return

        now = datetime.now()
        db.execute("INSERT INTO ledger VALUES(?,?,?)",
            (user_id, amt, now.strftime("%Y-%m-%d %H:%M:%S")))

        db.commit()
        user_state[user_id] = None
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已紀錄", quick_reply=back_menu()))
        return

    if text == "查看當月":
        month = datetime.now().strftime("%Y-%m")
        rows = db.execute(
            "SELECT amount,time FROM ledger WHERE user_id=? AND time LIKE ?",
            (user_id, f"{month}%")
        ).fetchall()

        msg = "📅 本月紀錄\n\n"
        for a, t in rows:
            msg += f"{t} : {a}\n"
        if not rows:
            msg += "尚無紀錄"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "查看上月":
        last = (datetime.now().replace(day=1)-timedelta(days=1)).strftime("%Y-%m")
        rows = db.execute(
            "SELECT amount,time FROM ledger WHERE user_id=? AND time LIKE ?",
            (user_id, f"{last}%")
        ).fetchall()

        msg = "⏪ 上月紀錄\n\n"
        for a, t in rows:
            msg += f"{t} : {a}\n"
        if not rows:
            msg += "尚無紀錄"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "清除紀錄":
        db.execute("DELETE FROM ledger WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🧹 已清除", quick_reply=back_menu()))
        return


# ================= RUN =================

@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
