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
COUNTDOWN = 60


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

    db.execute("""CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT,
        people INT,
        shop_id TEXT,
        status TEXT,
        expire REAL,
        table_no TEXT
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS tables(
        id TEXT,
        shop_id TEXT
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS notes(
        user_id TEXT,
        content TEXT,
        amount INT,
        time TEXT
    )""")

    db.commit()


# ================= 配桌 =================

def get_group_link(shop_id):
    db = get_db()
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    return row[0] if row and row[0] else SYSTEM_GROUP_LINK


def try_make_table(shop_id):
    db = get_db()

    rows = db.execute("""
        SELECT user_id,people FROM match_users 
        WHERE shop_id=? AND status='waiting'
        ORDER BY rowid
    """, (shop_id,)).fetchall()

    total = 0
    selected = []

    for u,p in rows:
        total += p
        selected.append(u)
        if total >= 4:
            break

    if total < 4:
        return

    table_no = f"{shop_id}_{int(time.time())}"
    expire = time.time() + COUNTDOWN

    db.execute("INSERT INTO tables VALUES(?,?)", (table_no,shop_id))

    for u in selected:
        db.execute("""
            UPDATE match_users 
            SET status='ready', expire=?, table_no=? 
            WHERE user_id=?
        """, (expire,table_no,u))

    db.commit()

    for u in selected:
        line_bot_api.push_message(u, TextSendMessage(
            f"🎉 成桌完成\n🪑 桌號 {table_no}\n⏱ {COUNTDOWN} 秒內確認",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))


def check_confirm(table_no):
    db = get_db()

    rows = db.execute("""
        SELECT user_id FROM match_users 
        WHERE table_no=? AND status='confirmed'
    """, (table_no,)).fetchall()

    if len(rows) < 4:
        return

    row = db.execute("SELECT shop_id FROM tables WHERE id=?", (table_no,)).fetchone()
    shop_id = row[0] if row else None
    group = get_group_link(shop_id)

    for (u,) in rows:
        line_bot_api.push_message(u, TextSendMessage(
            f"🎉 成桌成功\n🪑 桌號 {table_no}\n\n🔗 {group}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))

    db.execute("DELETE FROM match_users WHERE table_no=?", (table_no,))
    db.execute("DELETE FROM tables WHERE id=?", (table_no,))
    db.commit()


def release_timeout():
    while True:
        time.sleep(5)
        db = sqlite3.connect(DB_PATH, check_same_thread=False)
        now = time.time()

        rows = db.execute("""
            SELECT user_id,shop_id,table_no FROM match_users
            WHERE status='ready' AND expire < ?
        """, (now,)).fetchall()

        for u,shop_id,table_no in rows:
            db.execute("DELETE FROM match_users WHERE user_id=?", (u,))
            db.execute("""
                UPDATE match_users 
                SET status='waiting', expire=NULL, table_no=NULL
                WHERE table_no=?
            """,(table_no,))
            db.commit()
            try_make_table(shop_id)
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
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))
    ])


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


@handler.add(FollowEvent)
def handle_follow(event):
    line_bot_api.reply_message(event.reply_token, main_menu(event.source.user_id))
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    text = event.message.text.strip()
line_bot_api.reply_message(event.reply_token, main_menu())
return

    # ===== 回主畫面 =====
    if text in ["選單", "menu"]:
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ===== 記事本 =====
    if text == "記事本":
        user_state[user_id] = "note_menu"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "📒 記事本",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📅 查看當月", text="查看當月")),
                QuickReplyButton(action=MessageAction(label="⏪ 查看上月", text="查看上月")),
                QuickReplyButton(action=MessageAction(label="🧹 清除紀錄", text="清除紀錄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))
        return

    if text == "新增紀錄":
        user_state[user_id] = "note_add"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請輸入：內容 金額\n例如：吃飯 120", quick_reply=back_menu()))
        return

    if user_state.get(user_id) == "note_add":
        try:
            name, money = text.rsplit(" ", 1)
            money = int(money)
        except:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("格式錯誤，例如：飲料 50", quick_reply=back_menu()))
            return

        db.execute("INSERT INTO notes VALUES(?,?,?,?)",
                   (user_id, name, money, datetime.now().strftime("%Y-%m-%d")))
        db.commit()
        user_state[user_id] = "note_menu"

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已新增", quick_reply=back_menu()))
        return

    if text == "查看當月":
        now = datetime.now().strftime("%Y-%m")
        rows = db.execute(
            "SELECT content,amount,time FROM notes WHERE user_id=? AND time LIKE ?",
            (user_id, f"{now}%")
        ).fetchall()

        total = 0
        msg = f"📅 {now}\n\n"
        for r in rows:
            msg += f"{r[2]} {r[0]} ${r[1]}\n"
            total += r[1]

        msg += f"\n合計：${total}"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "查看上月":
        d = datetime.now().replace(day=1) - timedelta(days=1)
        ym = d.strftime("%Y-%m")

        rows = db.execute(
            "SELECT content,amount,time FROM notes WHERE user_id=? AND time LIKE ?",
            (user_id, f"{ym}%")
        ).fetchall()

        total = 0
        msg = f"⏪ {ym}\n\n"
        for r in rows:
            msg += f"{r[2]} {r[0]} ${r[1]}\n"
            total += r[1]

        msg += f"\n合計：${total}"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "清除紀錄":
        db.execute("DELETE FROM notes WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🧹 已清除", quick_reply=back_menu()))
        return

    # ===== 配桌 =====

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
        user_state[user_id] = sid

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇功能", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🎲 我要配桌", text=f"配桌:{sid}")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text.startswith("配桌:"):
        sid = text.split(":")[1]
        user_state[user_id] = sid

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇人數", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="我1人", text="我1人")),
                QuickReplyButton(action=MessageAction(label="我2人", text="我2人")),
                QuickReplyButton(action=MessageAction(label="我3人", text="我3人")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])))
        return

    if text in ["我1人", "我2人", "我3人"] and user_id in user_state:
        sid = user_state[user_id]
        people = int(text[1])

        db.execute("INSERT INTO match_users VALUES(?,?,?,?,?,?)",
            (user_id, people, sid, "waiting", None, None))
        db.commit()

        total = db.execute("""
            SELECT SUM(people) FROM match_users 
            WHERE shop_id=? AND status='waiting'
        """,(sid,)).fetchone()[0] or 0

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage(f"✅ 已加入配桌\n目前 {total}/4", quick_reply=back_menu()))

        try_make_table(sid)
        return

    if text == "加入":
        row = db.execute("SELECT table_no FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
            db.commit()
            check_confirm(row[0])
        return

    if text == "放棄":
        row = db.execute("SELECT shop_id,table_no FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            shop_id, table_no = row

            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.execute("""
                UPDATE match_users 
                SET status='waiting', expire=NULL, table_no=NULL
                WHERE table_no=?
            """,(table_no,))
            db.commit()

            try_make_table(shop_id)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("已放棄，系統補位中", quick_reply=back_menu()))
        return
    # ===== 店家後台 =====

    if text == "店家後台":
        shop = db.execute("SELECT * FROM shops WHERE shop_id=?", (user_id,)).fetchone()

        if not shop:
            user_state[user_id] = "register_shop"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("🏪 請輸入你的店家名稱", quick_reply=back_menu())
            )
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
                    QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
                ])
            )
        )
        return
    if user_state.get(user_id) == "register_shop":
        db.execute(
            "INSERT INTO shops VALUES(?,?,?,?,?)",
            (user_id, text, 0, 1, None)
        )
        db.commit()

        user_state.pop(user_id, None)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("✅ 店家建立完成", quick_reply=back_menu())
        )
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
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return
    if text == "設定群組":
        user_state[user_id] = "shop_set_group"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🔗 請貼上 LINE 群組邀請連結", quick_reply=back_menu())
        )
        return


    if user_state.get(user_id) == "shop_set_group":
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text, user_id))
        db.commit()
        user_state.pop(user_id, None)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 群組連結已設定", quick_reply=back_menu()))
        return

        
        # ===== 店家管理 ADMIN =====

   if text == "店家管理" and user_id in ADMIN_IDS:
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("🛠 店家管理", quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="📋 查看店家", text="查看店家")),
            QuickReplyButton(action=MessageAction(label="✅ 審核店家", text="審核店家")),
            QuickReplyButton(action=MessageAction(label="🗑 刪除店家", text="刪除店家")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]))
    )
    return



    if text == "查看店家" and user_id in ADMIN_IDS:
        rows = db.execute("SELECT shop_id,name,open FROM shops").fetchall()
        msg = "📋 店家列表\n\n"

        for r in rows:
            msg += f"{r[0]} | {r[1]} | {'營業' if r[2] else '休息'}\n"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(msg, quick_reply=back_menu())
        )
        return
        
    if text == "審核店家":
    shops = db.execute("SELECT shop_id,name FROM shops WHERE approved=0").fetchall()

    if not shops:
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("目前沒有待審店家", quick_reply=back_menu()))
        return

    items = [
        QuickReplyButton(action=MessageAction(label=f"🏪 {n}", text=f"審核:{i}"))
        for i,n in shops
    ]
    items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

    line_bot_api.reply_message(event.reply_token,
        TextSendMessage("請選擇要審核的店家", quick_reply=QuickReply(items=items)))
    return
       
if text.startswith("審核:"):
    sid = text.split(":")[1]
    user_state[user_id] = f"audit:{sid}"

    shop = db.execute("SELECT name FROM shops WHERE shop_id=?", (sid,)).fetchone()

    line_bot_api.reply_message(event.reply_token,
        TextSendMessage(f"審核店家：{shop[0]}", quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="✅ 同意", text="同意")),
            QuickReplyButton(action=MessageAction(label="❌ 不同意", text="不同意")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]))
    return
                               
if text in ["同意","不同意"] and user_state.get(user_id,"").startswith("audit:"):
    sid = user_state[user_id].split(":")[1]

    if text == "同意":
        db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
        msg = "✅ 已通過審核"
    else:
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        msg = "❌ 已拒絕並刪除"

    db.commit()
    user_state[user_id] = None

    line_bot_api.reply_message(event.reply_token,
        TextSendMessage(msg, quick_reply=back_menu()))
    return

        
    if text == "刪除店家" and user_id in ADMIN_IDS:
        rows = db.execute("SELECT shop_id,name FROM shops").fetchall()
        items = [
            QuickReplyButton(action=MessageAction(label=f"🗑 {n}", text=f"admin_del:{i}"))
            for i,n in rows
        ]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("選擇要刪除的店家", quick_reply=QuickReply(items=items))
        )
        return


    if text.startswith("admin_del:") and user_id in ADMIN_IDS:
        sid = text.split(":")[1]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🗑 已刪除", quick_reply=back_menu())
        )
        return



    if text == "開關店家" and user_id in ADMIN_IDS:
        rows = db.execute("SELECT shop_id,name,open FROM shops").fetchall()
        items = [
            QuickReplyButton(action=MessageAction(label=f"{'🟢' if o else '🔴'} {n}", text=f"切換:{i}"))
            for i,n,o in rows
        ]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇要切換的店家", quick_reply=QuickReply(items=items)))
        return


    if text.startswith("切換:") and user_id in ADMIN_IDS:
        sid = text.split(":")[1]
        db.execute("UPDATE shops SET open=1-open WHERE shop_id=?", (sid,))
        db.commit()

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🔁 已切換", quick_reply=back_menu()))
        return


    if text == "設定群組" and user_id in ADMIN_IDS:
        user_state[user_id] = "set_group"
        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請輸入：店家ID 群組連結", quick_reply=back_menu()))
        return


    if user_state.get(user_id) == "set_group" and user_id in ADMIN_IDS:
        try:
            sid, link = text.split(" ",1)
        except:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("格式錯誤", quick_reply=back_menu()))
            return

        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (link,sid))
        db.commit()
        user_state[user_id] = None

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🔗 群組已設定", quick_reply=back_menu()))
        return


    if text == "刪除店家" and user_id in ADMIN_IDS:
        rows = db.execute("SELECT shop_id,name FROM shops").fetchall()
        items = [
            QuickReplyButton(action=MessageAction(label=f"🗑 {n}", text=f"刪除:{i}"))
            for i,n in rows
        ]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("選擇要刪除的店家", quick_reply=QuickReply(items=items)))
        return


    if text.startswith("刪除:") and user_id in ADMIN_IDS:
        sid = text.split(":")[1]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return



@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    with app.app_context():
        init_db()

    threading.Thread(target=release_timeout, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


