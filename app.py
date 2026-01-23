import os, sqlite3, threading, time, re
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

COUNTDOWN_READY = 20


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

    db.execute("""
    CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
        name TEXT,
        open INT,
        approved INT,
        group_link TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT,
        people INT,
        shop_id TEXT,
        amount TEXT,
        status TEXT,
        expire REAL,
        table_id TEXT,
        table_index INT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS tables(
        id TEXT,
        shop_id TEXT,
        amount TEXT,
        table_index INT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS notes(
        user_id TEXT,
        content TEXT,
        amount INT,
        time TEXT
    )
    """)

    db.commit()


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


def get_group_link(shop_id):
    db = get_db()
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    return row[0] if row and row[0] else SYSTEM_GROUP_LINK


def get_next_table_index(shop_id):
    db = get_db()
    row = db.execute("SELECT MAX(table_index) FROM tables WHERE shop_id=?", (shop_id,)).fetchone()
    return (row[0] or 0) + 1


def get_table_users(table_id):
    db = get_db()
    rows = db.execute(
        "SELECT user_id FROM match_users WHERE table_id=?",
        (table_id,)
    ).fetchall()
    return [r[0] for r in rows]


def build_table_status_msg(table_id, title="🀄 桌況更新"):
    db = get_db()
    rows = db.execute("""
        SELECT user_id, status, people
        FROM match_users
        WHERE table_id=?
        ORDER BY table_index
    """, (table_id,)).fetchall()

    if not rows:
        return None

    total = sum(r[2] for r in rows)

    msg = f"{title}\n\n"
    msg += f"👥 人數：{total} / 4\n\n"

    for i, (uid, status, p) in enumerate(rows, 1):
        if status == "ready":
            icon = "📩"
        elif status == "confirmed":
            icon = "✅"
        else:
            icon = "⏳"

        msg += f"{i}. {p}人 {icon} {status}\n"

    return msg


def push_table(table_id, title="🀄 桌況更新"):
    msg = build_table_status_msg(table_id, title)
    if not msg:
        return

    for uid in get_table_users(table_id):
        try:
            line_bot_api.push_message(uid, TextSendMessage(msg))
        except Exception as e:
            print("push error:", e)


def try_make_table(shop_id, amount):
    db = get_db()

    rows = db.execute("""
        SELECT user_id,people FROM match_users 
        WHERE shop_id=? AND amount=? AND status='waiting'
        ORDER BY rowid
    """, (shop_id, amount)).fetchall()

    total = 0
    selected = []

    for u, p in rows:
        if total + p > 4:
            continue
        total += p
        selected.append(u)
        if total == 4:
            break

    if total != 4:
        return

    table_id = f"{shop_id}_{int(time.time()*1000)}"
    expire = time.time() + COUNTDOWN_READY
    table_index = get_next_table_index(shop_id)

    db.execute("INSERT INTO tables VALUES(?,?,?,?)",
               (table_id, shop_id, amount, table_index))

    for u in selected:
        db.execute("""
            UPDATE match_users 
            SET status='ready', expire=?, table_id=?, table_index=? 
            WHERE user_id=?
        """, (expire, table_id, table_index, u))

    db.commit()

    msg = f"🎉 成桌完成\n🪑 桌號 {table_index}\n💰 金額 {amount}\n⏱ {COUNTDOWN_READY} 秒內確認"
    for u in selected:
        line_bot_api.push_message(u, TextSendMessage(
            msg,
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
                QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
                QuickReplyButton(action=MessageAction(label="🚪 取消配桌", text="取消配桌")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))

    push_table(table_id, "🪑 桌子成立")


def check_confirm(table_id):
    db = get_db()

    rows = db.execute("""
        SELECT user_id FROM match_users 
        WHERE table_id=? AND status='confirmed'
    """, (table_id,)).fetchall()

    if len(rows) < 4:
        return

    shop_id, amount, table_index = db.execute(
        "SELECT shop_id,amount,table_index FROM tables WHERE id=?",
        (table_id,)
    ).fetchone()

    group = get_group_link(shop_id)

    for (u,) in rows:
        line_bot_api.push_message(u, TextSendMessage(
            f"🎉 配桌成功\n\n🪑 桌號：{table_index}\n💰 金額：{amount}\n\n"
            f"進入群組後請輸入：【{table_index}】\n\n🔗 {group}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ])
        ))

    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()


def timeout_checker():
    init_db()

    while True:
        try:
            db = sqlite3.connect(DB_PATH, check_same_thread=False)
            now = time.time()

            rows = db.execute("""
                SELECT user_id,shop_id,amount,table_id 
                FROM match_users 
                WHERE status='ready' AND expire IS NOT NULL AND expire < ?
            """, (now,)).fetchall()

            for user_id, shop_id, amount, table_id in rows:
                db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
                db.execute("""
                    UPDATE match_users 
                    SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
                    WHERE table_id=?
                """, (table_id,))

                try_make_table(shop_id, amount)

            db.commit()
            db.close()
        except Exception as e:
            print("timeout error:", e)

        time.sleep(3)


threading.Thread(target=timeout_checker, daemon=True).start()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    text = event.message.text.strip()

    # ✅ admin 最先
    if handle_admin_logic(event, user_id, text, db):
        return

    # ✅ shop 第二
    if handle_shop_logic(event, user_id, text, db):
        return


    # ===== 回主選單 =====
    if text == "選單":
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ===== 任意輸入回主選單 =====
    if user_id not in user_state and text not in [
        "指定店家","記事本","店家後台","店家管理",
        "新增紀錄","查看當月","查看上月","清除紀錄",
        "開始營業","今日休息","設定群組",
        "我1人","我2人","我3人",
        "加入","放棄","取消配桌"
    ]:
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return



    # ===== 指定店家 =====
    if text == "指定店家":
        rows = db.execute("SELECT shop_id,name FROM shops WHERE open=1 AND approved=1").fetchall()

        if not rows:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
            return

        items = [QuickReplyButton(action=MessageAction(label=n, text=f"店家:{sid}")) for sid, n in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    # ===== 選店 =====
    if text.startswith("店家:"):
        shop_id = text.split(":", 1)[1]
        user_state[user_id] = {"shop_id": shop_id}

        items = [
            QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
            QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
            QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
            QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items)))
        return

    # ===== 金額 =====
    if text.startswith("金額:"):
        amount = text.split(":", 1)[1]
        user_state.setdefault(user_id, {})["amount"] = amount

        items = [
            QuickReplyButton(action=MessageAction(label="1人", text="人數:1")),
            QuickReplyButton(action=MessageAction(label="2人", text="人數:2")),
            QuickReplyButton(action=MessageAction(label="3人", text="人數:3")),
            QuickReplyButton(action=MessageAction(label="4人", text="人數:4")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("請選擇人數", quick_reply=QuickReply(items=items)))
        return

    # ===== 人數 =====
    if text.startswith("人數:"):
        people = int(text.split(":", 1)[1])
        data = user_state.get(user_id)

        shop_id = data.get("shop_id")
        amount = data.get("amount")

        db.execute("""
            INSERT OR REPLACE INTO match_users 
            (user_id, people, shop_id, amount, status, expire, table_id, table_index)
            VALUES (?, ?, ?, ?, 'waiting', NULL, NULL, NULL)
        """, (user_id, people, shop_id, amount))
        db.commit()

        try_make_table(shop_id, amount)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已加入配桌等待中", quick_reply=back_menu()))
        return

    # ===== 加入 =====
    if text == "加入":
        row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        table_id = row[0]
        db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
        db.commit()

        push_table(table_id, "✅ 有玩家加入")
        check_confirm(table_id)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("✅ 已確認加入", quick_reply=back_menu()))
        return

    # ===== 放棄 =====
    if text == "放棄":
        row = db.execute("SELECT shop_id,amount,table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()

        if row:
            shop_id, amount, table_id = row
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.execute("""
                UPDATE match_users 
                SET status='waiting',expire=NULL,table_id=NULL,table_index=NULL 
                WHERE table_id=?
            """, (table_id,))
            db.commit()

            push_table(table_id, "❌ 有玩家離開")
            try_make_table(shop_id, amount)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("❌ 已放棄配桌", quick_reply=back_menu()))
        return

    # ===== 取消配桌 =====
    if text == "取消配桌":
        row = db.execute("SELECT shop_id,amount FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            shop_id, amount = row
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.commit()
            try_make_table(shop_id, amount)

        line_bot_api.reply_message(event.reply_token,
            TextSendMessage("🚪 已取消配桌", quick_reply=back_menu()))
        return
    

    # ===== 記事本選單 =====
    if text == "記事本":
        user_state[user_id] = {"mode": "note_menu"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("📒 記事本", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📅 查看當月", text="查看當月")),
                QuickReplyButton(action=MessageAction(label="⏪ 查看上月", text="查看上月")),
                QuickReplyButton(action=MessageAction(label="🧹 清除紀錄", text="清除紀錄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]))
        )
        return


    # ===== 新增紀錄 =====
    if text == "新增紀錄":
        user_state[user_id] = {"mode": "note_amount"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請輸入金額，例如：1000 或 -500", quick_reply=back_menu())
        )
        return


    # ===== 記事本輸入金額 =====
    if user_state.get(user_id, {}).get("mode") == "note_amount":
        val = text.strip()

        if not re.fullmatch(r"-?\d+", val):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("請直接輸入金額，例如：1000 或 -500", quick_reply=back_menu())
            )
            return

        amount = int(val)

        db.execute(
            "INSERT INTO notes (user_id, content, amount, time) VALUES (?,?,?,?)",
            (user_id, "", amount, datetime.now().strftime("%Y-%m-%d"))
        )
        db.commit()

        user_state.pop(user_id, None)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"✅ 已新增：{amount:+}", quick_reply=back_menu())
        )
        return


    # ===== 查看當月 =====
    if text == "查看當月":
        today = datetime.now()
        month_start = today.strftime("%Y-%m-01")

        rows = db.execute("""
            SELECT amount, time FROM notes
            WHERE user_id=? AND time >= ?
            ORDER BY time DESC
        """, (user_id, month_start)).fetchall()

        if not rows:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("📅 本月尚無紀錄", quick_reply=back_menu())
            )
            return

        total = 0
        msg = "📅 本月紀錄\n\n"

        for amt, t in rows:
            total += amt
            msg += f"{t}｜{amt:+}\n"

        msg += f"\n💰 合計：{total:+}"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(msg, quick_reply=back_menu())
        )
        return


    # ===== 查看上月 =====
    if text == "查看上月":
        today = datetime.now()
        first = today.replace(day=1)
        last_month_end = first - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        rows = db.execute("""
            SELECT amount, time FROM notes
            WHERE user_id=? AND time BETWEEN ? AND ?
            ORDER BY time DESC
        """, (
            user_id,
            last_month_start.strftime("%Y-%m-%d"),
            last_month_end.strftime("%Y-%m-%d")
        )).fetchall()

        if not rows:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("⏪ 上月尚無紀錄", quick_reply=back_menu())
            )
            return

        total = 0
        msg = "⏪ 上月紀錄\n\n"

        for amt, t in rows:
            total += amt
            msg += f"{t}｜{amt:+}\n"

        msg += f"\n💰 合計：{total:+}"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(msg, quick_reply=back_menu())
        )
        return


    # ===== 清除紀錄 =====
    if text == "清除紀錄":
        db.execute("DELETE FROM notes WHERE user_id=?", (user_id,))
        db.commit()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🧹 已清除所有記事本紀錄", quick_reply=back_menu())
        )
        return
        
# ================= 店家後台 ================= #  

def show_shop_menu(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("🏪 店家後台", quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
            QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
            QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]))
    )
        return True

def handle_shop_logic(event, user_id, text, db):

    # === 回主畫面 ===
    if text == "選單":
        user_state.pop(user_id, None)
        return False

    # === 進入後台 ===
    if text == "店家後台":
        row = db.execute(
            "SELECT shop_id,approved FROM shops WHERE owner_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            user_state[user_id] = {"mode": "shop_input"}
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("請輸入店家名稱", quick_reply=back_menu())
            )
            return True

        sid, ap = row
        user_state[user_id] = {"mode": "shop_menu", "shop_id": sid}

        if ap == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("⏳ 尚未審核通過", quick_reply=back_menu())
            )
            return True

        return show_shop_menu(event)

    # === 新增店家 ===
    if user_state.get(user_id, {}).get("mode") == "shop_input":
        name = text
        shop_id = f"{user_id}_{int(time.time())}"

        db.execute(
            "INSERT INTO shops (shop_id,name,open,approved,group_link,owner_id) VALUES (?,?,?,?,?,?)",
            (shop_id, name, 0, 0, None, user_id)
        )
        db.commit()

        user_state[user_id] = {"mode": "shop_wait", "shop_id": shop_id}

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"🏪 {name}\n\n已送出申請，等待審核", quick_reply=back_menu())
        )
        return True

    # === 等待審核 ===
    if user_state.get(user_id, {}).get("mode") == "shop_wait":
        sid = user_state[user_id]["shop_id"]
        ap = db.execute(
            "SELECT approved FROM shops WHERE shop_id=?",
            (sid,)
        ).fetchone()

        if ap and ap[0] == 1:
            user_state[user_id]["mode"] = "shop_menu"
            return show_shop_menu(event)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("⏳ 尚未審核", quick_reply=back_menu())
        )
        return True

    # === 開始營業 ===
    if text == "開始營業" and user_state.get(user_id, {}).get("shop_id"):
        sid = user_state[user_id]["shop_id"]
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (sid,))
        db.commit()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🟢 已開始營業", quick_reply=back_menu())
        )
        return True

    # === 今日休息 ===
    if text == "今日休息" and user_state.get(user_id, {}).get("shop_id"):
        sid = user_state[user_id]["shop_id"]
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (sid,))
        db.commit()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🔴 今日休息", quick_reply=back_menu())
        )
        return True

    # === 設定群組 ===
    if text == "設定群組" and user_state.get(user_id, {}).get("shop_id"):
        user_state[user_id]["mode"] = "shop_set_group"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請輸入群組連結", quick_reply=back_menu())
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "shop_set_group":
        sid = user_state[user_id]["shop_id"]
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (text, sid))
        db.commit()

        user_state[user_id]["mode"] = "shop_menu"

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("✅ 已設定群組", quick_reply=back_menu())
        )
        return True

    return False


   
# ================= 店家管理 =================

def handle_admin_logic(event, user_id, text, db):

    # === 回主畫面直接離開 ===
    if text == "選單":
        user_state.pop(user_id, None)
        return False

    # === 管理選單 ===
    if user_id in ADMIN_IDS and text == "店家管理":
        user_state[user_id] = {"mode": "admin_menu"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🛠 店家管理", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="📋 查看店家", text="查看店家")),
                QuickReplyButton(action=MessageAction(label="✅ 店家審核", text="店家審核")),
                QuickReplyButton(action=MessageAction(label="🗑 店家刪除", text="店家刪除")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ]))
        )
        return True

    # === 查看 ===
    if user_id in ADMIN_IDS and text == "查看店家":
        rows = db.execute("SELECT shop_id,name,open,approved FROM shops").fetchall()
        msg = "🏪 店家列表\n\n"

        for sid, name, open_, ap in rows:
            msg += f"{name}\n狀態：{'營業中' if open_ else '未營業'} | {'✅通過' if ap else '❌未審核'}\nID:{sid}\n\n"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return True

    # === 審核 ===
    if user_id in ADMIN_IDS and text == "店家審核":
        user_state[user_id] = {"mode": "admin_review"}
        rows = db.execute("SELECT shop_id,name,approved FROM shops").fetchall()

        msg = "請輸入要審核的店家ID\n\n"
        for sid, name, ap in rows:
            msg += f"{name} | {'已通過' if ap else '未審核'}\nID:{sid}\n\n"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_review":
        if text == "選單":
            user_state.pop(user_id, None)
            return False

        user_state[user_id] = {"mode": "admin_review_confirm", "sid": text}

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請選擇審核結果", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 同意", text="同意審核")),
                QuickReplyButton(action=MessageAction(label="❌ 不同意", text="不同意審核")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ]))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_review_confirm":
        if text == "選單":
            user_state.pop(user_id, None)
            return False

        sid = user_state[user_id]["sid"]

        if text == "同意審核":
            db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
        elif text == "不同意審核":
            db.execute("UPDATE shops SET approved=0 WHERE shop_id=?", (sid,))

        db.commit()
        user_state.pop(user_id, None)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新", quick_reply=back_menu()))
        return True

    # === 刪除 ===
    if user_id in ADMIN_IDS and text == "店家刪除":
        user_state[user_id] = {"mode": "admin_delete"}
        rows = db.execute("SELECT shop_id,name FROM shops").fetchall()

        msg = "請輸入要刪除的店家ID\n\n"
        for sid, name in rows:
            msg += f"{name}\nID:{sid}\n\n"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_delete":
        if text == "選單":
            user_state.pop(user_id, None)
            return False

        user_state[user_id] = {"mode": "admin_delete_confirm", "sid": text}

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("⚠ 確定刪除？", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 確定刪除", text="確認刪除")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ]))
        )
        return True

    if user_state.get(user_id, {}).get("mode") == "admin_delete_confirm":
        if text == "選單":
            user_state.pop(user_id, None)
            return False

        if text == "確認刪除":
            sid = user_state[user_id]["sid"]
            db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
            db.commit()

        user_state.pop(user_id, None)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已處理", quick_reply=back_menu()))
        return True

    return False



# ================= MAIN =================

if __name__ == "__main__":
    with app.app_context():
        init_db()

    app.run(host="0.0.0.0", port=5000)






























