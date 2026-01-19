import os, sqlite3
from datetime import datetime
from flask import Flask, request, abort, g
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

GROUP_LINK = "https://line.me/R/ti/g/XXXXXXXX"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}

# ================= DB =================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT,
        price TEXT,
        people INT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS match_confirm(
        user_id TEXT,
        price TEXT,
        people INT,
        confirmed INT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS ledger(
        user_id TEXT,
        amount INT,
        time TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
        name TEXT,
        open INT,
        approved INT
    )""")
    db.commit()

# ================= MENU =================

def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="📒 輸贏記事本", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🏪 店家後台", text="店家後台")),
    ]
    if user_id in ADMIN_IDS:
        items.append(QuickReplyButton(action=MessageAction(label="🛠 店家管理", text="店家管理")))
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

    if text in ["選單","menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ================= 成桌確認 =================

    if text == "加入":
        db.execute("UPDATE match_confirm SET confirmed=1 WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已加入成功"))
        return

    if text == "放棄":
        cur = db.execute("SELECT price,people FROM match_confirm WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            price, people = row
            db.execute("DELETE FROM match_confirm WHERE user_id=?", (user_id,))
            db.execute("INSERT INTO match_users VALUES(?,?,?)",(user_id,price,people))
            db.commit()

            cur = db.execute("SELECT SUM(people) FROM match_users WHERE price=?", (price,))
            total = cur.fetchone()[0]

            cur = db.execute("SELECT user_id FROM match_users WHERE price=?", (price,))
            users = cur.fetchall()

            for u, in users:
                line_bot_api.push_message(u,
                    TextSendMessage(f"⚠ 有人放棄\n目前 {total}/4\n繼續等待配桌"))

        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄"))
        return

    # ================= 管理員 =================

    if user_id in ADMIN_IDS and text == "店家管理":
        cur = db.execute("SELECT shop_id,name,open,approved FROM shops")
        rows = cur.fetchall()

        msgs = []
        for sid,n,o,a in rows:
            status = f"{'營業' if o else '停用'} / {'核准' if a else '未核准'}"
            msgs.append(TextSendMessage(
                f"🏪 {n}\n{status}",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="✅ 核准", text=f"核准:{sid}")),
                    QuickReplyButton(action=MessageAction(label="⛔ 停用", text=f"停用:{sid}")),
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

    if user_id in ADMIN_IDS and text.startswith("刪除:"):
        sid = text.split(":")[1]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return

    # ================= 配桌 =================

    if text == "配桌":
        cur = db.execute("SELECT * FROM match_users WHERE user_id=?", (user_id,))
        if cur.fetchone():
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("你已在配桌中", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="👀 查看目前配桌", text="查看目前配桌")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
            return

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇遊戲金額", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="30 / 10", text="30/10")),
                QuickReplyButton(action=MessageAction(label="50 / 20", text="50/20")),
                QuickReplyButton(action=MessageAction(label="100 / 20", text="100/20")),
                QuickReplyButton(action=MessageAction(label="100 / 50", text="100/50")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text in ["30/10","50/20","100/20","100/50"]:
        user_state[user_id] = text
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇人數", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="我1人", text="我1人")),
                QuickReplyButton(action=MessageAction(label="我2人", text="我2人")),
                QuickReplyButton(action=MessageAction(label="我3人", text="我3人")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text in ["我1人","我2人","我3人"] and user_id in user_state:
        people = int(text[1])
        price = user_state[user_id]

        db.execute("INSERT INTO match_users VALUES(?,?,?)",(user_id,price,people))
        db.commit()

        cur = db.execute("SELECT SUM(people) FROM match_users WHERE price=?", (price,))
        total = cur.fetchone()[0]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 已加入 {price}\n目前 {total}/4", quick_reply=back_menu()))

        if total >= 4:
            cur = db.execute("SELECT user_id,price,people FROM match_users WHERE price=?", (price,))
            users = cur.fetchall()

            for u,p,peo in users:
                db.execute("INSERT INTO match_confirm VALUES(?,?,?,0)",(u,p,peo))
                line_bot_api.push_message(u,
                    TextSendMessage(
                        f"🎉 成桌成功\n點此加入群組👇\n{GROUP_LINK}\n\n請輸入：加入 或 放棄"
                    ))

            db.execute("DELETE FROM match_users WHERE price=?", (price,))
            db.commit()
        return

    if text == "查看目前配桌":
        cur = db.execute("SELECT price,SUM(people) FROM match_users GROUP BY price")
        rows = cur.fetchall()
        msg = "📋 配桌狀態\n\n"
        for p,t in rows:
            msg += f"{p}：{t}/4\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "取消配桌":
        db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已取消配桌", quick_reply=back_menu()))
        return

    # ================= 店家配桌 =================

    if text == "店家配桌":
        cur = db.execute("SELECT name FROM shops WHERE open=1 AND approved=1")
        shops = cur.fetchall()
        msg = "🏪 營業中店家\n\n"
        for s, in shops:
            msg += f"✅ {s}\n"
        if not shops:
            msg += "目前沒有營業店家"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    # ================= 店家後台 =================

    if text == "店家後台":
        cur = db.execute("SELECT * FROM shops WHERE shop_id=?", (user_id,))
        shop = cur.fetchone()

        if not shop:
            user_state[user_id] = "register_shop"
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入麻將館名稱"))
            return

        if shop[3] == 0:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("⏳ 審核中，請等待管理員通過", quick_reply=back_menu()))
            return

        status = "營業中" if shop[2] else "休息中"

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"🏪 {shop[1]}\n目前狀態：{status}", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if user_state.get(user_id) == "register_shop":
        db.execute("INSERT INTO shops VALUES(?,?,?,?)",(user_id,text,0,0))
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return

    if text == "今日休息":
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return

    # ================= 記事本 =================

    if text == "記事本":
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("📒 輸贏記事本", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📄 查看紀錄", text="查看紀錄")),
                QuickReplyButton(action=MessageAction(label="🧹 清除當月紀錄", text="清除當月")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text == "新增紀錄":
        user_state[user_id] = "add_money"
        line_bot_api.reply_message(event.reply_token, TextSendMessage("輸入金額 (+/-)"))
        return

    if user_state.get(user_id) == "add_money":
        amt = int(text)
        db.execute("INSERT INTO ledger VALUES(?,?,?)",(user_id,amt,datetime.now().strftime("%Y-%m-%d")))
        db.commit()
        user_state[user_id] = None
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已紀錄", quick_reply=back_menu()))
        return

    if text == "查看紀錄":
        cur = db.execute("SELECT amount,time FROM ledger WHERE user_id=?", (user_id,))
        rows = cur.fetchall()
        msg = "📄 紀錄\n\n"
        for a,t in rows:
            msg += f"{t} : {a}\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "清除當月":
        month = datetime.now().strftime("%Y-%m")
        db.execute("DELETE FROM ledger WHERE user_id=? AND time LIKE ?", (user_id,f"{month}%"))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🧹 本月已清除", quick_reply=back_menu()))
        return

    line_bot_api.reply_message(event.reply_token, main_menu(user_id))


# ================= RUN =================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

