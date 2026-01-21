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
shop_match_state = {}

COUNTDOWN = 30


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
    db.execute("""CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT,
        price TEXT,
        people INT,
        shop_id TEXT,
        status TEXT,
        expire TEXT,
        table_no INT
    )""")

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

    db.execute("""CREATE TABLE IF NOT EXISTS tables(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        price TEXT,
        shop_id TEXT,
        created TEXT
    )""")

    db.commit()


def create_table_no(price, shop_id):
    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO tables(price,shop_id,created) VALUES(?,?,?)",
               (price, shop_id, now))
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_group_link(shop_id):
    db = get_db()

    if shop_id:
        row = db.execute(
            "SELECT group_link FROM shops WHERE shop_id=?",
            (shop_id,)
        ).fetchone()

        if row and row[0]:
            return row[0]

    return SYSTEM_GROUP_LINK

def get_group_link(shop_id):
    db = get_db()

    if shop_id:
        row = db.execute(
            "SELECT group_link FROM shops WHERE shop_id=?",
            (shop_id,)
        ).fetchone()

        if row and row[0]:
            return row[0]

    return SYSTEM_GROUP_LINK


def get_group_link(shop_id):
    db = get_db()

    if shop_id:
        row = db.execute(
            "SELECT group_link FROM shops WHERE shop_id=?",
            (shop_id,)
        ).fetchone()

        if row and row[0]:
            return row[0]

    return SYSTEM_GROUP_LINK


    def get_group_link(shop_id):
    db = get_db()

    if shop_id:
        row = db.execute(
            "SELECT group_link FROM shops WHERE shop_id=?",
            (shop_id,)
        ).fetchone()

     if row and row[0]:
            return row[0]

    return SYSTEM_GROUP_LINK


# ================= 倒數釋放 =================

def release_timeout():
    while True:
        time.sleep(5)
        db = sqlite3.connect(DB_PATH)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        rows = db.execute("""
            SELECT user_id FROM match_users
            WHERE status='ready' AND expire < ?
        """, (now,)).fetchall()

        for (u,) in rows:
            db.execute("DELETE FROM match_users WHERE user_id=?", (u,))
            try:
                line_bot_api.push_message(u, TextSendMessage("⏱ 超時未加入，已自動釋放"))
            except:
                pass

        db.commit()
        db.close()


threading.Thread(target=release_timeout, daemon=True).start()


# ================= 配桌邏輯 =================

def try_make_table(price, shop_id):
    db = get_db()

    rows = db.execute("""
        SELECT rowid,user_id,people 
        FROM match_users 
        WHERE price=? AND shop_id IS ? AND status='waiting'
        ORDER BY rowid
    """, (price, shop_id)).fetchall()

    pool = []
    total = 0

    for r in rows:
        pool.append(r)
        total += r[2]
        if total >= 4:
            break

    if total < 4:
        return

    table_no = create_table_no(price, shop_id)
    expire = (datetime.now() + timedelta(seconds=COUNTDOWN)).strftime("%Y-%m-%d %H:%M:%S")

    for rowid, uid, _ in pool:
        db.execute("""
            UPDATE match_users 
            SET status='ready',expire=?,table_no=? 
            WHERE rowid=?
        """, (expire, table_no, rowid))

        line_bot_api.push_message(uid, TextSendMessage(
            f"🎉 成桌完成\n🪑 桌號 {table_no}\n⏱ {COUNTDOWN} 秒內確認",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
            ])
        ))

    db.commit()


def check_confirm(table_no):
    db = get_db()
    rows = db.execute("""
        SELECT user_id FROM match_users 
        WHERE table_no=? AND status='confirmed'
    """, (table_no,)).fetchall()

    if len(rows) < 4:
        return

    for (u,) in rows:
        row = db.execute(
        "SELECT shop_id FROM tables WHERE id=?",
        (table_no,)
     ).fetchone()

    shop_id = row[0] if row else None
    group = get_group_link(shop_id)

    line_bot_api.push_message(u, TextSendMessage(
    f"🎉 成桌成功\n🪑 桌號 {table_no}\n{group}"
    ))

    db.execute("DELETE FROM match_users WHERE table_no=?", (table_no,))
    db.commit()


# ================= MENU =================

def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🎯 配桌", text="配桌")),
        QuickReplyButton(action=MessageAction(label="🏪 指定店家", text="指定店家")),
        QuickReplyButton(action=MessageAction(label="📒 記事本", text="記事本")),
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

    if event.source.type == "group":
        if text.lower() in ["群id", "groupid", "群組id"]:
            gid = event.source.group_id
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(f"📌 群組ID：\n{gid}")
            )
            return

            )
            return
      if user_state.get(user_id) == "set_group":
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text, user_id))
        db.commit()
        user_state[user_id] = None

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("✅ 群組連結已更新", quick_reply=back_menu())
        )
        return



       if user_state.get(user_id, "").startswith("admin_set_group"):
        sid = user_state[user_id].split(":")[1]
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text, sid))
        db.commit()
        user_state[user_id] = None

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("✅ 已更新群組", quick_reply=back_menu())
        )
        return

    if text in ["選單", "menu"]:
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return
        
    if text == "設定群組":
        user_state[user_id] = "set_group"
        line_bot_api.reply_message(event.reply_token,
        TextSendMessage("請貼上 LINE 群組邀請連結"))
        return

    if user_state.get(user_id) == "set_group":
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?",(text,user_id))
        db.commit()
        user_state[user_id] = None

        line_bot_api.reply_message(event.reply_token,
        TextSendMessage("✅ 群組連結已更新", quick_reply=back_menu()))
        return

    

    # ===== 成桌確認 =====
    if text == "加入":
        db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
        db.commit()

        row = db.execute("SELECT table_no FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            check_confirm(row[0])

        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已加入，等待其他人"))
        return

    if text == "放棄":
        db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("已放棄配桌", quick_reply=back_menu()))
        return


    # ===== 管理員 =====

    if user_id in ADMIN_IDS and text == "店家管理":
        rows = db.execute("SELECT shop_id,name,open,approved FROM shops").fetchall()

        msgs = []
        for sid,n,o,a in rows:
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


    if user_id in ADMIN_IDS and text.startswith("刪除:"):
        sid = text.split(":")[1]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return

    # ===== 配桌 =====

    if text == "配桌":
        if db.execute("SELECT 1 FROM match_users WHERE user_id=?", (user_id,)).fetchone():
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("你已在配桌中", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="👀 查看配桌", text="查看配桌")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔗 群組", text=f"群組:{sid}")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
            return

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇遊戲金額", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="30/10", text="30/10")),
                QuickReplyButton(action=MessageAction(label="50/20", text="50/20")),
                QuickReplyButton(action=MessageAction(label="100/20", text="100/20")),
                QuickReplyButton(action=MessageAction(label="100/50", text="100/50")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    # ===== 指定店家 =====

    if text == "指定店家":
        shops = db.execute("SELECT shop_id,name FROM shops WHERE open=1 AND approved=1").fetchall()

        if not shops:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有上線店家", quick_reply=back_menu()))
            return

        items = [QuickReplyButton(action=MessageAction(label=f"🏪 {n}", text=f"選店:{i}")) for i,n in shops]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("選店:"):
        shop_match_state[user_id] = text.split(":")[1]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇遊戲金額", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="30/10", text="30/10")),
                QuickReplyButton(action=MessageAction(label="50/20", text="50/20")),
                QuickReplyButton(action=MessageAction(label="100/20", text="100/20")),
                QuickReplyButton(action=MessageAction(label="100/50", text="100/50")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    # ===== 金額 =====

    if text in ["30/10","50/20","100/20","100/50"]:
        if db.execute("SELECT 1 FROM match_users WHERE user_id=?", (user_id,)).fetchone():
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("你已在配桌中", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="👀 查看配桌", text="查看配桌")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
            return

        user_state[user_id] = text

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇人數", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="我1人", text="我1人")),
                QuickReplyButton(action=MessageAction(label="我2人", text="我2人")),
                QuickReplyButton(action=MessageAction(label="我3人", text="我3人")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    # ===== 人數 =====

    if text in ["我1人","我2人","我3人"] and user_id in user_state:
        people = int(text[1])
        price = user_state[user_id]
        shop_id = shop_match_state.get(user_id)

        db.execute(
    "INSERT INTO match_users VALUES(?,?,?,?,?,?,?)",
    (user_id, price, people, shop_id, "waiting", None, None)
)


        total = db.execute(
            "SELECT SUM(people) FROM match_users WHERE price=? AND shop_id IS ?",
            (price,shop_id)
        ).fetchone()[0]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 已加入 {price}\n目前 {total}/4", quick_reply=back_menu()))

 if total >= 4:
                      users = db.execute(
                "SELECT user_id FROM match_users WHERE price=? AND shop_id IS ?",
                (price, shop_id)
            ).fetchall()

            group = get_group_link(shop_id)

            for (u,) in users:
                line_bot_api.push_message(u, TextSendMessage(f"🎉 成桌成功\n{group}"))

            if shop_id:
                line_bot_api.push_message(shop_id, TextSendMessage(f"🎉 玩家已成桌\n{group}"))


            db.execute("DELETE FROM match_users WHERE price=? AND shop_id IS ?", (price,shop_id))
            db.commit()
        return

    # ===== 查看 / 取消 =====

    if text == "查看配桌":
        row = db.execute("SELECT price,people FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(f"目前：{row[0]} / {row[1]}人", quick_reply=back_menu()))
        return

    if text == "取消配桌":
        db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("已取消配桌", quick_reply=back_menu()))
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
                    QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])
            )
        )
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

        if text == "設定群組":
        user_state[user_id] = "set_group"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請貼上 LINE 群組邀請連結"))
        return


    if text == "開始營業":
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return


        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
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

        # 只留兩個月
        db.execute("DELETE FROM ledger WHERE user_id=? AND time < date('now','-2 months')",(user_id,))
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
        for a,t in rows:
            msg += f"{t} : {a}\n"
        if not rows: msg += "尚無紀錄"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "查看上月":
        last = (datetime.now().replace(day=1)-timedelta(days=1)).strftime("%Y-%m")
        rows = db.execute(
            "SELECT amount,time FROM ledger WHERE user_id=? AND time LIKE ?",
            (user_id, f"{last}%")
        ).fetchall()

        msg = "⏪ 上月紀錄\n\n"
        for a,t in rows:
            msg += f"{t} : {a}\n"
        if not rows: msg += "尚無紀錄"

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






