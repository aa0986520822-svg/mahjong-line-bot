import os, sqlite3, threading, time, re
from datetime import datetime, timedelta
from flask import Flask, request, abort, g
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction, URIAction,
    PostbackEvent, PostbackAction
)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    # 讓 Render log 更好讀（仍會啟動，但 LineBotApi 會在呼叫時失敗）
    print("WARNING: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET not set")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SYSTEM_GROUP_LINK = "https://line.me/R/ti/g/一般玩家群"

ADMIN_IDS = {
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

DB_PATH = "data.db"
user_state = {}

COUNTDOWN_READY = 30  # ✅ 30 秒確認


def get_db():
    if "db" not in g:
        db = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = get_db()

    db.execute("""
    CREATE TABLE IF NOT EXISTS match_users(
        user_id TEXT PRIMARY KEY,
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
        id TEXT PRIMARY KEY,
        shop_id TEXT,
        amount TEXT,
        table_index INT,
        created REAL,
        r20 INT DEFAULT 0,
        r10 INT DEFAULT 0
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        content TEXT,
        amount INT,
        time TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT PRIMARY KEY,
        name TEXT,
        open INT,
        approved INT,
        group_link TEXT,
        owner_id TEXT,
        partner_map TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS nicknames(
        user_id TEXT PRIMARY KEY,
        nickname TEXT
    )
    """)

    # 使用者流程暫存（避免多進程/重啟造成記憶體 user_state 遺失）
    db.execute("""
    CREATE TABLE IF NOT EXISTS session_state(
        user_id TEXT PRIMARY KEY,
        shop_id TEXT,
        amount TEXT,
        updated REAL
    )
    """)

    db.commit()



def ss_set(db, user_id, shop_id=None, amount=None):
    now = time.time()
    row = db.execute("SELECT user_id, shop_id, amount FROM session_state WHERE user_id=?", (user_id,)).fetchone()
    cur_shop = row["shop_id"] if row else None
    cur_amt = row["amount"] if row else None
    if shop_id is None:
        shop_id = cur_shop
    if amount is None:
        amount = cur_amt
    db.execute(
        "INSERT OR REPLACE INTO session_state(user_id, shop_id, amount, updated) VALUES(?,?,?,?)",
        (user_id, shop_id, amount, now)
    )
    db.commit()

def ss_get(db, user_id):
    row = db.execute("SELECT shop_id, amount FROM session_state WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return (None, None)
    return (row["shop_id"], row["amount"])

def ss_clear(db, user_id):
    db.execute("DELETE FROM session_state WHERE user_id=?", (user_id,))
    db.commit()


def back_menu():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單"))
    ])

def confirm_menu():
    # 成桌確認階段：提供加入/放棄（避免被後續訊息蓋掉按鍵）
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
        QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
        QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
    ])


def table_quick_reply(db, table_id):
    # ✅ 以「倒數時間 expire」為準：只要未到期，就固定顯示加入/放棄，避免按鈕閃退/被覆蓋
    if not table_id:
        return back_menu()

    erow = db.execute(
        "SELECT MIN(expire) AS ex FROM match_users WHERE table_id=? AND expire IS NOT NULL",
        (table_id,)
    ).fetchone()

    if erow and erow["ex"]:
        remain = int(erow["ex"] - time.time())
        if remain > 0:
            return confirm_menu()

    return back_menu()



def get_nickname(db, user_id):
    row = db.execute("SELECT nickname FROM nicknames WHERE user_id=?", (user_id,)).fetchone()
    return row["nickname"] if row and row["nickname"] else None


def display_name(db, user_id):
    nk = get_nickname(db, user_id)
    if nk:
        return nk
    # 若未設定暱稱，用「玩家XXXX」末4碼
    return f"玩家{user_id[-4:]}"


def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🀄 店家配桌", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="📒 記事本", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🏷 設定暱稱", text="設定暱稱")),
        QuickReplyButton(action=MessageAction(label="🗺 店家地圖", text="店家地圖")),
        QuickReplyButton(action=MessageAction(label="🤝 店家合作", text="店家合作")),
    ]
    if user_id in ADMIN_IDS:
        items.append(QuickReplyButton(action=MessageAction(label="6️⃣ 店家管理", text="店家管理")))
    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=items))


def get_group_link(db, shop_id):
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    if row and (row["group_link"] or "").strip():
        return row["group_link"].strip()
    return SYSTEM_GROUP_LINK


def get_next_table_index(db, shop_id):
    row = db.execute("SELECT MAX(table_index) AS mx FROM tables WHERE shop_id=?", (shop_id,)).fetchone()
    return (row["mx"] or 0) + 1


def get_table_users(db, table_id):
    rows = db.execute("SELECT user_id FROM match_users WHERE table_id=?", (table_id,)).fetchall()
    return [r["user_id"] for r in rows]


def build_table_status_msg(db, table_id, title="🀄 桌況更新"):
    rows = db.execute("""
        SELECT user_id, status, people
        FROM match_users
        WHERE table_id=?
        ORDER BY rowid
    """, (table_id,)).fetchall()

    if not rows:
        return None

    total = sum(int(r["people"]) for r in rows)
    confirmed = sum(1 for r in rows if r["status"] == "confirmed")

    msg = f"{title}\n\n"
    msg += f"👥 人數：{total} / 4\n"
    msg += f"✅ 已確認：{confirmed} / {len(rows)}\n\n"

    for i, r in enumerate(rows, 1):
        st = r["status"]
        if st == "ready":
            icon = "📩"
            st_label = "待確認"
        elif st == "confirmed":
            icon = "✅"
            st_label = "已加入"
        else:
            icon = "⏳"
            st_label = st

        msg += f"{i}. {display_name(db, r['user_id'])}｜{int(r['people'])}人 {icon} {st_label}\n"

    return msg.strip()


def push_table(table_id, title="🀄 桌況更新"):
    with app.app_context():
        db = get_db()
        msg = build_table_status_msg(db, table_id, title)
        if not msg:
            return
        for uid in get_table_users(db, table_id):
            try:
                line_bot_api.push_message(uid, TextSendMessage(msg, quick_reply=table_quick_reply(db, table_id)))
            except Exception as e:
                print("push_table error:", e)


def notify_table(table_id, text):
    with app.app_context():
        db = get_db()
        for uid in get_table_users(db, table_id):
            try:
                line_bot_api.push_message(uid, TextSendMessage(text, quick_reply=table_quick_reply(db, table_id)))
            except Exception as e:
                print("notify_table error:", e)


def try_make_table(shop_id, amount, reply_token=None, trigger_user_id=None):
    db = get_db()
    rows = db.execute("""
        SELECT user_id, people FROM match_users
        WHERE shop_id=? AND amount=? AND status='waiting'
        ORDER BY rowid
    """, (shop_id, amount)).fetchall()

    total = 0
    selected = []
    for r in rows:
        uid = r["user_id"]
        p = int(r["people"])
        if total + p > 4:
            continue
        total += p
        selected.append((uid, p))
        if total == 4:
            break

    if total != 4:
        return None

    table_id = f"{shop_id}_{int(time.time()*1000)}"
    expire = time.time() + COUNTDOWN_READY
    table_index = get_next_table_index(db, shop_id)

    db.execute(
        "INSERT INTO tables(id, shop_id, amount, table_index, created, r20, r10) VALUES(?,?,?,?,?,?,?)",
        (table_id, shop_id, amount, table_index, time.time(), 0, 0)
    )

    for uid, _p in selected:
        db.execute("""
            UPDATE match_users
            SET status='ready', expire=?, table_id=?, table_index=?
            WHERE user_id=?
        """, (expire, table_id, table_index, uid))

    db.commit()

    msg = (
        "🎉 成桌確認\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n\n"
        f"⏱ {COUNTDOWN_READY} 秒內未確認視同放棄"
    )

    qr = QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="✅ 加入", text="加入")),
        QuickReplyButton(action=MessageAction(label="❌ 放棄", text="放棄")),
        QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
    ])

    for uid, _p in selected:
        try:
            if reply_token and trigger_user_id and uid == trigger_user_id:
                line_bot_api.reply_message(reply_token, TextSendMessage(msg, quick_reply=qr))
            else:
                line_bot_api.push_message(uid, TextSendMessage(msg, quick_reply=qr))
        except Exception as e:
            print("confirm push error:", e)

    push_table(table_id, "🪑 桌子成立（等待確認）")
    return table_id


def finalize_success(table_id, skip_user_id=None):
    db = get_db()
    trow = db.execute(
        "SELECT shop_id, amount, table_index FROM tables WHERE id=?",
        (table_id,)
    ).fetchone()
    if not trow:
        return None

    shop_id = trow["shop_id"]
    amount = trow["amount"]
    table_index = trow["table_index"]

    shop = db.execute("SELECT name, group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    shop_name = shop["name"] if shop and shop["name"] else "店家"
    group = (shop["group_link"] if shop and shop["group_link"] else None) or SYSTEM_GROUP_LINK

    rows = db.execute("SELECT user_id FROM match_users WHERE table_id=? AND status='confirmed'", (table_id,)).fetchall()

    msg = (
        "🎉 配桌成功\n\n"
        f"🏪 店家：{shop_name}\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n\n"
        f"🔗 群組連結：{group}\n"
        "🔔 進群後請回報桌號"
    )

    # 推播給其他已確認者（觸發者用 reply 送，避免同一事件重複 reply）
    for r in rows:
        uid = r["user_id"]
        if skip_user_id and uid == skip_user_id:
            continue
        try:
            line_bot_api.push_message(uid, TextSendMessage(msg, quick_reply=back_menu()))
        except Exception as e:
            print("success push error:", e)

    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()

    return msg



def handle_abandon(user_id):
    db = get_db()
    row = db.execute("SELECT shop_id, amount, table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None

    shop_id = row["shop_id"]
    amount = row["amount"]
    table_id = row["table_id"]

    # 刪除放棄者
    db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
    db.commit()

    if table_id:
        # 有在確認桌：其餘玩家回到等待中，桌子作廢，繼續等待補人
        db.execute("UPDATE match_users SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL WHERE table_id=?", (table_id,))
        db.execute("DELETE FROM tables WHERE id=?", (table_id,))
        db.commit()

        notify_table(table_id, "⚠ 有玩家放棄，已回到等待池，繼續配桌中…")
        # 可能剛好補滿再成桌
        try_make_table(shop_id, amount)

    return (shop_id, amount)


def timeout_checker():
    while True:
        try:
            with app.app_context():
                db = get_db()
                now = time.time()

                # 先做提醒（20秒、10秒）
                tables = db.execute("SELECT * FROM tables").fetchall()
                for t in tables:
                    table_id = t["id"]
                    # 找該桌 expire（取任一 ready 的 expire）
                    erow = db.execute("SELECT MIN(expire) AS ex FROM match_users WHERE table_id=? AND status='ready'", (table_id,)).fetchone()
                    if not erow or not erow["ex"]:
                        continue
                    remain = int(erow["ex"] - now)

                    if remain <= 20 and remain > 10 and t["r20"] == 0:
                        db.execute("UPDATE tables SET r20=1 WHERE id=?", (table_id,))
                        db.commit()
                        notify_table(table_id, "⏳ 剩餘 20 秒未確認視同放棄")
                    if remain <= 10 and remain > 0 and t["r10"] == 0:
                        db.execute("UPDATE tables SET r10=1 WHERE id=?", (table_id,))
                        db.commit()
                        notify_table(table_id, "⏳ 剩餘 10 秒未確認視同放棄")

                # 到期處理：ready 到期 -> 視同放棄（只退未確認者）
                expired = db.execute("""
                    SELECT user_id, table_id FROM match_users
                    WHERE status='ready' AND expire IS NOT NULL AND expire < ?
                """, (now,)).fetchall()

                # 用 table_id 分組處理，避免重複
                handled_tables = set()
                for r in expired:
                    table_id = r["table_id"]
                    if not table_id or table_id in handled_tables:
                        continue
                    handled_tables.add(table_id)

                    # 未確認者全部放棄
                    unconfirmed = db.execute("SELECT user_id FROM match_users WHERE table_id=? AND status='ready'", (table_id,)).fetchall()
                    for u in unconfirmed:
                        db.execute("DELETE FROM match_users WHERE user_id=?", (u["user_id"],))

                    # 其餘玩家回等待池
                    db.execute("UPDATE match_users SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL WHERE table_id=?", (table_id,))
                    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
                    db.commit()

                    notify_table(table_id, "⛔ 超過 30 秒未確認，視同放棄，已取消本次成桌並回到等待池")
                    # 嘗試再成桌
                    # 取 shop/amount 用任一 match_users waiting
                    w = db.execute("SELECT shop_id, amount FROM match_users WHERE status='waiting' LIMIT 1").fetchone()
                    if w:
                        try_make_table(w["shop_id"], w["amount"])

        except Exception as e:
            print("timeout_checker error:", e)

        time.sleep(2)


threading.Thread(target=timeout_checker, daemon=True).start()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(PostbackEvent)
def handle_postback(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    data = (event.postback.data or "").strip()

    # 選店家：使用 Postback，避免聊天室顯示「店家:shop_id」
    if data.startswith("shop="):
        sid = data.split("=", 1)[1].strip()
        user_state[user_id] = {"mode": "wait_amount", "shop_id": sid}
        ss_set(db, user_id, shop_id=sid, amount=None)
        items = [
            QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
            QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
            QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
            QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items)))
        return


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    init_db()
    db = get_db()

    user_id = event.source.user_id
    text = (event.message.text or "").strip()

    # ===== 查自己的 LINE User ID =====
    if text in ("賴ID", "賴id", "LINEID", "lineid"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"你的 LINE User ID：{user_id}", quick_reply=back_menu())
        )
        return

    # ===== 回主選單 =====
    if text == "選單":
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return

    # ===== 管理入口 =====
    if user_id in ADMIN_IDS and text == "店家管理":
        user_state[user_id] = {"mode": "admin_menu"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "🛠 店家管理",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="📋 查看店家", text="管理:查看")),
                QuickReplyButton(action=MessageAction(label="✅ 審核店家", text="管理:審核")),
                QuickReplyButton(action=MessageAction(label="🗑 刪除店家", text="管理:刪除")),
                QuickReplyButton(action=MessageAction(label="🗺 地圖設定", text="管理:地圖設定")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    # 管理：查看
    if user_id in ADMIN_IDS and text == "管理:查看":
        rows = db.execute("SELECT shop_id, name, open, approved FROM shops ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return
        msg = "🏪 店家列表\n\n"
        for r in rows:
            msg += f"{r['name']}\n狀態：{'營業中' if r['open'] else '未營業'} | {'✅通過' if r['approved'] else '❌未審核'}\nID:{r['shop_id']}\n\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg.strip(), quick_reply=back_menu()))
        return

    # 管理：審核
    if user_id in ADMIN_IDS and text == "管理:審核":
        rows = db.execute("SELECT shop_id, name, approved FROM shops ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return
        items = []
        for r in rows:
            items.append(QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:審核:{r['shop_id']}")))
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要審核的店家", quick_reply=QuickReply(items=items)))
        return

    if user_id in ADMIN_IDS and text.startswith("管理:審核:"):
        sid = text.split(":", 2)[2]
        user_state[user_id] = {"mode": "admin_review", "sid": sid}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "請選擇審核結果",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 通過", text="管理:同意")),
                QuickReplyButton(action=MessageAction(label="❌ 不通過", text="管理:不同意")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if user_id in ADMIN_IDS and user_state.get(user_id, {}).get("mode") == "admin_review":
        sid = user_state[user_id]["sid"]
        if text == "管理:同意":
            db.execute("UPDATE shops SET approved=1 WHERE shop_id=?", (sid,))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已通過", quick_reply=back_menu()))
            return
        if text == "管理:不同意":
            db.execute("UPDATE shops SET approved=0 WHERE shop_id=?", (sid,))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已設為不通過", quick_reply=back_menu()))
            return

    # 管理：刪除
    if user_id in ADMIN_IDS and text == "管理:刪除":
        rows = db.execute("SELECT shop_id, name FROM shops ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:刪除:{r['shop_id']}")) for r in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要刪除的店家", quick_reply=QuickReply(items=items)))
        return

    if user_id in ADMIN_IDS and text.startswith("管理:刪除:"):
        sid = text.split(":", 2)[2]
        db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除", quick_reply=back_menu()))
        return

    # 管理：地圖設定
    if user_id in ADMIN_IDS and text == "管理:地圖設定":
        rows = db.execute("SELECT shop_id, name FROM shops WHERE approved=1 ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有已核准店家", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:地圖:{r['shop_id']}")) for r in rows]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要設定地圖的店家", quick_reply=QuickReply(items=items)))
        return

    if user_id in ADMIN_IDS and text.startswith("管理:地圖:"):
        sid = text.split(":", 2)[2]
        user_state[user_id] = {"mode": "admin_map_input", "sid": sid}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請貼上地圖連結（Google Maps 連結）", quick_reply=back_menu()))
        return

    if user_id in ADMIN_IDS and user_state.get(user_id, {}).get("mode") == "admin_map_input":
        sid = user_state[user_id]["sid"]
        link = text.strip()
        db.execute("UPDATE shops SET partner_map=? WHERE shop_id=?", (link, sid))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新地圖連結", quick_reply=back_menu()))
        return

    # ===== 設定暱稱 =====
    if text == "設定暱稱":
        user_state[user_id] = {"mode": "nickname_input"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入你的暱稱（最多 12 字）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "nickname_input":
        nk = text.strip()[:12]
        db.execute("INSERT OR REPLACE INTO nicknames(user_id, nickname) VALUES(?,?)", (user_id, nk))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 暱稱已設定：{nk}", quick_reply=back_menu()))
        return

    # ===== 記事本（保留原本：新增 / 當月 / 上月 / 清除）=====
    if text == "記事本":
        user_state[user_id] = {"mode": "note_menu"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "📒 記事本",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="➕ 新增紀錄", text="新增紀錄")),
                QuickReplyButton(action=MessageAction(label="📅 查看當月", text="查看當月")),
                QuickReplyButton(action=MessageAction(label="⏪ 查看上月", text="查看上月")),
                QuickReplyButton(action=MessageAction(label="🧹 清除紀錄", text="清除紀錄")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if text == "新增紀錄":
        user_state[user_id] = {"mode": "note_amount"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入金額，例如：1000 或 -500", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "note_amount":
        val = text.strip()
        if not re.fullmatch(r"-?\d+", val):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請直接輸入金額，例如：1000 或 -500", quick_reply=back_menu()))
            return
        amount = int(val)
        db.execute("INSERT INTO notes(user_id, content, amount, time) VALUES(?,?,?,?)", (user_id, "", amount, datetime.now().strftime("%Y-%m-%d")))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(f"✅ 已新增：{amount:+}", quick_reply=back_menu()))
        return

    if text == "查看當月":
        today = datetime.now()
        month_start = today.strftime("%Y-%m-01")
        rows = db.execute("SELECT amount, time FROM notes WHERE user_id=? AND time >= ? ORDER BY time DESC", (user_id, month_start)).fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("📅 本月尚無紀錄", quick_reply=back_menu()))
            return
        total = 0
        msg = "📅 本月紀錄\n\n"
        for r in rows:
            total += int(r["amount"])
            msg += f"{r['time']}｜{int(r['amount']):+}\n"
        msg += f"\n💰 合計：{total:+}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "查看上月":
        today = datetime.now()
        first = today.replace(day=1)
        last_month_end = first - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        rows = db.execute(
            "SELECT amount, time FROM notes WHERE user_id=? AND time BETWEEN ? AND ? ORDER BY time DESC",
            (user_id, last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d"))
        ).fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⏪ 上月尚無紀錄", quick_reply=back_menu()))
            return
        total = 0
        msg = "⏪ 上月紀錄\n\n"
        for r in rows:
            total += int(r["amount"])
            msg += f"{r['time']}｜{int(r['amount']):+}\n"
        msg += f"\n💰 合計：{total:+}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return

    if text == "清除紀錄":
        db.execute("DELETE FROM notes WHERE user_id=?", (user_id,))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🧹 已清除紀錄", quick_reply=back_menu()))
        return

    # ===== 店家合作 =====
    if text == "店家合作":
        row = db.execute("SELECT shop_id, name, approved, open, group_link FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            user_state[user_id] = {"mode": "shop_apply"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入店家名稱", quick_reply=back_menu()))
            return
        if int(row["approved"] or 0) != 1:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("⏳ 尚未審核通過，請等待管理員審核", quick_reply=back_menu()))
            return

        status = "🟢 營業中" if int(row["open"] or 0) == 1 else "🔴 未營業"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"🏪 {row['name']}\n{status}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
                QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
                QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if user_state.get(user_id, {}).get("mode") == "shop_apply":
        name = text.strip()[:30]
        sid = f"{user_id}_{int(time.time())}"
        db.execute(
            "INSERT OR REPLACE INTO shops(shop_id, name, open, approved, group_link, owner_id, partner_map) VALUES(?,?,0,0,'',?, '')",
            (sid, name, user_id)
        )
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已送出申請，等待管理員審核", quick_reply=back_menu()))
        return

    if text == "開始營業":
        row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (row["shop_id"],))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=back_menu()))
        return

    if text == "今日休息":
        row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return
        db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (row["shop_id"],))
        db.commit()
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=back_menu()))
        return

    if text == "設定群組":
        user_state[user_id] = {"mode": "set_group"}
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請貼上群組邀請連結（https://line.me/...）", quick_reply=back_menu()))
        return

    if user_state.get(user_id, {}).get("mode") == "set_group":
        link = text.strip()
        row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC", (user_id,)).fetchone()
        if not row:
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=back_menu()))
            return
        db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (link, row["shop_id"]))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已設定群組連結", quick_reply=back_menu()))
        return

    # ===== 店家地圖 =====
    if text == "店家地圖":
        rows = db.execute("SELECT shop_id, name, partner_map FROM shops WHERE open=1 AND approved=1 ORDER BY rowid DESC").fetchall()
        if not rows:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業的店家", quick_reply=back_menu()))
            return
        rows_with_link = [r for r in rows if (r["partner_map"] or "").strip()]
        if not rows_with_link:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有可開啟的地圖（店家尚未設定地圖連結）", quick_reply=back_menu()))
            return
        items = [QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"地圖:{r['shop_id']}")) for r in rows_with_link]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇要開啟地圖的店家", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("地圖:"):
        sid = text.split(":", 1)[1].strip()
        row = db.execute("SELECT name, partner_map FROM shops WHERE shop_id=? AND open=1 AND approved=1", (sid,)).fetchone()
        if not row or not (row["partner_map"] or "").strip():
            line_bot_api.reply_message(event.reply_token, TextSendMessage("此店家尚未設定地圖連結", quick_reply=back_menu()))
            return
        name = row["name"] or "店家"
        link = row["partner_map"].strip()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"🗺 {name} 地圖\n{link}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=URIAction(label="📍 開啟地圖", uri=link)),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    # ===== 店家配桌 =====
    if text == "店家配桌":
        row = db.execute("SELECT shop_id, amount, people, status, table_id FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            # ✅ 若正在「成桌確認」階段，優先顯示「加入/放棄」
            if row["status"] == "ready":
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("你目前在成桌確認中，請選擇：", quick_reply=confirm_menu())
                )
                return

            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                "你目前已有配桌紀錄\n(可查看進度/取消配桌)",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🔍 查看進度", text="查看進度")),
                    QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
                ])
            ))
            return

        ss_clear(db, user_id)
        shops = db.execute("SELECT shop_id, name FROM shops WHERE open=1 AND approved=1 ORDER BY rowid DESC").fetchall()
        if not shops:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=back_menu()))
            return

        items = [
            QuickReplyButton(action=PostbackAction(label=(s["name"] or "")[:20], data=f"shop={s['shop_id']}"))
            for s in shops
        ]
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")))
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
        return

    if text == "查看進度":
        row = db.execute("""
            SELECT s.name, m.amount, m.people, m.status
            FROM match_users m
            LEFT JOIN shops s ON m.shop_id = s.shop_id
            WHERE m.user_id=?
        """, (user_id,)).fetchone()
        if not row:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            f"📌 配桌狀態\n\n🏪 {row['name'] or '未知店家'}\n💰 {row['amount']}\n👥 {int(row['people'])} 人\n📍 {row['status']}",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if text.startswith("店家:"):
        sid = text.split(":", 1)[1].strip()
        user_state[user_id] = {"mode": "wait_amount", "shop_id": sid}
        ss_set(db, user_id, shop_id=sid, amount=None)
        items = [
            QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
            QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
            QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
            QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
            QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("金額:"):
        amount = text.split(":", 1)[1].strip()
        st = user_state.get(user_id, {})
        if not st.get("shop_id"):
            sid_db, _amt_db = ss_get(db, user_id)
            if sid_db:
                st["shop_id"] = sid_db
                user_state[user_id] = st
        if not st.get("shop_id"):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請先選擇店家", quick_reply=back_menu()))
            return
        st["amount"] = amount
        user_state[user_id] = st
        ss_set(db, user_id, amount=amount)
        items = [
            QuickReplyButton(action=MessageAction(label="我1人", text="人數:1")),
            QuickReplyButton(action=MessageAction(label="我2人", text="人數:2")),
            QuickReplyButton(action=MessageAction(label="我3人", text="人數:3")),
            QuickReplyButton(action=MessageAction(label="我4人", text="人數:4")),
            QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
        ]
        line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇人數", quick_reply=QuickReply(items=items)))
        return

    if text.startswith("人數:"):
        people = int(text.split(":", 1)[1].strip())
        st = user_state.get(user_id, {})
        shop_id = st.get("shop_id")
        amount = st.get("amount")
        if not shop_id or not amount:
            sid_db, amt_db = ss_get(db, user_id)
            shop_id = shop_id or sid_db
            amount = amount or amt_db
        if not shop_id or not amount:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("資料不足，請重新開始配桌", quick_reply=back_menu()))
            user_state.pop(user_id, None)
            return

        db.execute("""
            INSERT OR REPLACE INTO match_users(user_id, people, shop_id, amount, status, expire, table_id, table_index)
            VALUES(?, ?, ?, ?, 'waiting', NULL, NULL, NULL)
        """, (user_id, people, shop_id, amount))
        db.commit()
        user_state.pop(user_id, None)
        ss_clear(db, user_id)

        # 嘗試成桌；把「當前使用者」用 reply 送出，避免多訊息順序問題
        table_id = try_make_table(shop_id, amount, reply_token=event.reply_token, trigger_user_id=user_id)
        if table_id:
            # 成桌訊息已送，這裡不要再回第二則
            return

        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            "✅ 已加入配桌等待中",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="🔍 查看進度", text="查看進度")),
                QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ])
        ))
        return

    if text == "取消配桌":
        # ✅ 若在「成桌確認」中，取消配桌等同於放棄：自己退出，其他人回等待池繼續配桌
        strow = db.execute("SELECT status FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if strow and (strow["status"] in ("ready", "confirmed")):
            handle_abandon(user_id)
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄（等同取消配桌）", quick_reply=back_menu()))
            return

        # 其他狀態：維持原本取消
        row = db.execute("SELECT shop_id, amount FROM match_users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            shop_id, amount = row["shop_id"], row["amount"]
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.commit()
            try_make_table(shop_id, amount)
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("🚪 已取消配桌", quick_reply=back_menu()))
        return

    if text == "加入":
        row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
        if not row or not row["table_id"]:
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        table_id = row["table_id"]
        db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
        db.commit()

        push_table(table_id, "✅ 有玩家加入")

        # 4 人都確認才成功
        cnt = db.execute("SELECT COUNT(*) AS c FROM match_users WHERE table_id=? AND status='confirmed'", (table_id,)).fetchone()["c"]
        if cnt >= 4:
            finalize_success(table_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已確認加入", quick_reply=back_menu()))
        return

    if text == "放棄":
        handle_abandon(user_id)
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄（等同取消配桌）", quick_reply=back_menu()))
        return

    # ===== 其他文字：回主選單 =====
    line_bot_api.reply_message(event.reply_token, main_menu(user_id))


# ---- Render 啟動 ----
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=port)
