import os, sqlite3, threading, time, re
from datetime import datetime, timedelta
from flask import Flask, request, abort, g
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction,
    TemplateSendMessage, ButtonsTemplate
)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    # Render logs 會看到，方便排查
    print("⚠️ Missing LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DB_PATH = "data.db"

# ---- 設定 ----
COUNTDOWN_READY = 30  # ✅ 30秒確認
REMIND_AT = (20, 10)  # ✅ 每10秒提醒一次（只提醒兩次）
SYSTEM_GROUP_LINK = ""  # 沒設定店家連結時，可留空或放預設

ADMIN_IDS = {
    # 你的 admin userId
    "Ua5794a5932d2427fcaa42ee039a2067a",
}

user_state = {}
# 避免同一桌重複提醒
reminded = set()  # {(table_id, seconds_left)}

# ---------------- DB ----------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
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
        status TEXT,          -- waiting/ready/confirmed
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
        table_index INT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT PRIMARY KEY,
        name TEXT,
        open INT,
        approved INT,
        group_link TEXT,
        owner_id TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS nicknames(
        user_id TEXT PRIMARY KEY,
        nickname TEXT
    )
    """)
    db.commit()

def get_nickname(db, user_id):
    row = db.execute("SELECT nickname FROM nicknames WHERE user_id=?", (user_id,)).fetchone()
    if row and row["nickname"]:
        return row["nickname"]
    return f"玩家{user_id[-4:]}"

def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="👤 設定暱稱", text="設定暱稱")),
        QuickReplyButton(action=MessageAction(label="🗺 店家地圖", text="店家地圖")),
        QuickReplyButton(action=MessageAction(label="🤝 店家合作", text="店家合作")),
    ]
    if user_id in ADMIN_IDS:
        items.append(QuickReplyButton(action=MessageAction(label="🛠 管理", text="管理")))
    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=items))

def shop_menu():
    return TextSendMessage(
        "店家合作選單",
        quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
            QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
            QuickReplyButton(action=MessageAction(label="🔗 設定群組連結", text="設定群組")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ])
    )

def get_group_link(db, shop_id):
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    link = (row["group_link"] if row else "") or ""
    link = link.strip()
    if link.startswith("http"):
        return link
    return SYSTEM_GROUP_LINK.strip()

def get_next_table_index(db, shop_id):
    row = db.execute("SELECT MAX(table_index) AS m FROM tables WHERE shop_id=?", (shop_id,)).fetchone()
    return (row["m"] or 0) + 1

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
    msg += f"👥 人數：{total} / 4\n"
    confirmed = sum(1 for r in rows if r[1] == "confirmed")
    msg += f"✅ 已確認：{confirmed} / 4\n\n"

    for i, (uid, status, p) in enumerate(rows, 1):
        if status == "ready":
            icon = "📩 待確認"
        elif status == "confirmed":
            icon = "✅ 已確認"
        else:
            icon = "⏳ 等待中"
        msg += f"{i}. {p}人 {icon}\n"

    return msg

def push_table(db, table_id, title="🪑 桌子成立"):
    msg = build_table_status_msg(db, table_id, title)
    if not msg:
        return
    users = db.execute("SELECT user_id FROM match_users WHERE table_id=?", (table_id,)).fetchall()
    for u in users:
        try:
            line_bot_api.push_message(u["user_id"], TextSendMessage(msg))
        except Exception as e:
            print("push_table error:", e)

def send_confirm_buttons_reply(reply_token, table_index, amount):
    msg = (
        f"🎉 成桌確認\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n\n"
        f"⏱ {COUNTDOWN_READY} 秒內未確認視同放棄"
    )
    buttons = TemplateSendMessage(
        alt_text="成桌確認",
        template=ButtonsTemplate(
            title="成桌確認",
            text=msg[:160],
            actions=[
                MessageAction(label="✅ 加入", text="加入"),
                MessageAction(label="❌ 放棄", text="放棄"),
            ],
        ),
    )
    line_bot_api.reply_message(reply_token, buttons)

def send_confirm_buttons_push(user_id, table_index, amount):
    msg = (
        f"🎉 成桌確認\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n\n"
        f"⏱ {COUNTDOWN_READY} 秒內未確認視同放棄"
    )
    buttons = TemplateSendMessage(
        alt_text="成桌確認",
        template=ButtonsTemplate(
            title="成桌確認",
            text=msg[:160],
            actions=[
                MessageAction(label="✅ 加入", text="加入"),
                MessageAction(label="❌ 放棄", text="放棄"),
            ],
        ),
    )
    line_bot_api.push_message(user_id, buttons)

def try_make_table(db, shop_id, amount, reply_token=None, trigger_user_id=None):
    """
    湊滿4人後：
    - 產生 table
    - 全員 status=ready, expire=now+30
    - 觸發者用 reply(最穩顯示按鈕)，其他人用 push
    """
    rows = db.execute("""
        SELECT user_id, people FROM match_users
        WHERE shop_id=? AND amount=? AND status='waiting'
        ORDER BY rowid
    """, (shop_id, amount)).fetchall()

    total = 0
    picked = []
    for r in rows:
        p = int(r["people"])
        if total + p > 4:
            continue
        total += p
        picked.append(r["user_id"])
        if total == 4:
            break

    if total != 4:
        return None

    table_id = f"{shop_id}_{int(time.time()*1000)}"
    expire = time.time() + COUNTDOWN_READY
    table_index = get_next_table_index(db, shop_id)

    db.execute("INSERT INTO tables(id, shop_id, amount, table_index) VALUES(?,?,?,?)",
               (table_id, shop_id, amount, table_index))
    for uid in picked:
        db.execute("""
            UPDATE match_users
            SET status='ready', expire=?, table_id=?, table_index=?
            WHERE user_id=?
        """, (expire, table_id, table_index, uid))
    db.commit()

    # 桌況先推
    push_table(db, table_id, "🪑 桌子成立")

    # 成桌提醒（按鈕）
    for uid in picked:
        try:
            if reply_token and trigger_user_id and uid == trigger_user_id:
                # ✅ 觸發者用 reply：私訊最穩、一定顯示按鈕
                send_confirm_buttons_reply(reply_token, table_index, amount)
            else:
                send_confirm_buttons_push(uid, table_index, amount)
        except Exception as e:
            print("send_confirm_buttons error:", e)
            # 失敗就退而求其次，至少給文字指令
            try:
                line_bot_api.push_message(uid, TextSendMessage(
                    f"🎉 成桌確認\n桌號：{table_index}\n請輸入「加入」或「放棄」\n⏱ {COUNTDOWN_READY}秒內未確認視同放棄"
                ))
            except Exception as e2:
                print("fallback text error:", e2)

    return {"table_id": table_id, "table_index": table_index}

def check_confirm(db, table_id):
    rows = db.execute("""
        SELECT user_id, status, people, shop_id, amount, table_index
        FROM match_users
        WHERE table_id=?
    """, (table_id,)).fetchall()
    if not rows:
        return False

    total_people = sum(int(r["people"]) for r in rows)
    confirmed_people = sum(int(r["people"]) for r in rows if r["status"] == "confirmed")
    if total_people != 4 or confirmed_people != 4:
        return False

    shop_id = rows[0]["shop_id"]
    amount = rows[0]["amount"]
    table_index = rows[0]["table_index"]
    group = get_group_link(db, shop_id)

    msg = (
        f"🎉 配桌成功\n\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n"
        f"🔗 連結：{group}\n\n"
        f"🔔 提示：進群後請回報桌號【{table_index}】"
    )

    users = [r["user_id"] for r in rows]
    for uid in users:
        try:
            line_bot_api.push_message(uid, TextSendMessage(msg))
        except Exception as e:
            print("success push error:", e)

    # ✅ 成功後回到未配桌狀態：清掉該桌資料
    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()

    return True

def cancel_table(db, table_id, reason="⏳ 超過 30 秒未確認，視同放棄，已取消配桌"):
    rows = db.execute("SELECT user_id FROM match_users WHERE table_id=?", (table_id,)).fetchall()
    for r in rows:
        uid = r["user_id"]
        try:
            line_bot_api.push_message(uid, TextSendMessage(reason))
        except:
            pass

    # ✅ 不要重新倒數：直接把這桌的 ready 全部退回 waiting
    db.execute("""
        UPDATE match_users
        SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
        WHERE table_id=?
    """, (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()

    # 清理提醒旗標
    for s in list(REMIND_AT) + [0]:
        reminded.discard((table_id, s))

def timeout_worker():
    with app.app_context():
        init_db()
    while True:
        try:
            with app.app_context():
                db = get_db()
                now = time.time()

                # 找所有 ready 桌
                tables = db.execute("""
                    SELECT DISTINCT table_id, expire
                    FROM match_users
                    WHERE status='ready' AND table_id IS NOT NULL AND expire IS NOT NULL
                """).fetchall()

                for t in tables:
                    table_id = t["table_id"]
                    expire = float(t["expire"] or 0)
                    left = int(expire - now)

                    # 20秒、10秒提醒一次
                    for sec in REMIND_AT:
                        if left <= sec and (table_id, sec) not in reminded and left > 0:
                            reminded.add((table_id, sec))
                            try:
                                push_table(db, table_id, f"⏳ 剩餘 {sec} 秒未確認視同放棄")
                            except Exception as e:
                                print("remind push error:", e)

                    # 超時取消
                    if left <= 0:
                        cancel_table(db, table_id)
        except Exception as e:
            print("timeout_worker error:", e)

        time.sleep(1)

threading.Thread(target=timeout_worker, daemon=True).start()

# ---------------- Flask routes ----------------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ---------------- LINE handler ----------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    with app.app_context():
        init_db()
        db = get_db()

        user_id = event.source.user_id
        text = (event.message.text or "").strip()

        # ---- 主選單 ----
        if text in ("選單", "menu", "主選單"):
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return

        # ---- 暱稱設定（獨立）----
        if text == "設定暱稱":
            user_state[user_id] = {"mode": "set_nick"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入暱稱（最多10字）"))
            return

        if user_state.get(user_id, {}).get("mode") == "set_nick":
            nick = text[:10].strip()
            if not nick:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("暱稱不可空白，請重新輸入"))
                return
            db.execute("INSERT OR REPLACE INTO nicknames(user_id, nickname) VALUES(?,?)", (user_id, nick))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    f"✅ 已設定暱稱：{nick}",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="✏️ 修改暱稱", text="設定暱稱")),
                        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                    ])
                )
            )
            return

        
        # ---- 管理（僅管理員可用）----
        if text == "管理":
            if user_id not in ADMIN_IDS:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("你沒有管理權限", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    "🛠 店家管理",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="📋 查看店家", text="管理:查看")),
                        QuickReplyButton(action=MessageAction(label="✅ 審核店家", text="管理:審核")),
                        QuickReplyButton(action=MessageAction(label="🗑 刪除店家", text="管理:刪除")),
                        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                    ])
                )
            )
            return

        if text == "管理:查看":
            if user_id not in ADMIN_IDS:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("你沒有管理權限", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            rows = db.execute("SELECT shop_id,name,open,approved FROM shops ORDER BY rowid DESC").fetchall()
            if not rows:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            msg = "🏪 店家列表\n\n"
            for r in rows:
                msg += f"🏪 {r['name']}\n狀態：{'營業中' if r['open'] else '未營業'} | {'✅通過' if r['approved'] else '❌未審核'}\nID: {r['shop_id']}\n\n"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        if text == "管理:審核":
            if user_id not in ADMIN_IDS:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("你沒有管理權限", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            rows = db.execute("SELECT shop_id,name,approved FROM shops ORDER BY rowid DESC").fetchall()
            if not rows:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            items = []
            for r in rows:
                label = ("✅" if r["approved"] else "⏳") + " " + (r["name"] or "")[:16]
                items.append(QuickReplyButton(action=MessageAction(label=label, text=f"管理:審核:{r['shop_id']}")))
            items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
            user_state[user_id] = {"mode": "admin_review"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要審核的店家", quick_reply=QuickReply(items=items)))
            return

        if text.startswith("管理:審核:") and user_id in ADMIN_IDS:
            sid = text.split(":", 2)[2]
            user_state[user_id] = {"mode": "admin_review_confirm", "sid": sid}
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    f"審核店家：{sid}\n請選擇結果",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label="✅ 通過", text="管理:同意")),
                        QuickReplyButton(action=MessageAction(label="❌ 不通過", text="管理:不同意")),
                        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                    ])
                )
            )
            return

        if user_state.get(user_id, {}).get("mode") == "admin_review_confirm" and text in ("管理:同意", "管理:不同意") and user_id in ADMIN_IDS:
            sid = user_state[user_id]["sid"]
            ap = 1 if text == "管理:同意" else 0
            db.execute("UPDATE shops SET approved=? WHERE shop_id=?", (ap, sid))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新審核狀態", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        if text == "管理:刪除":
            if user_id not in ADMIN_IDS:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("你沒有管理權限", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            rows = db.execute("SELECT shop_id,name FROM shops ORDER BY rowid DESC").fetchall()
            if not rows:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有店家", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            items = []
            for r in rows:
                items.append(QuickReplyButton(action=MessageAction(label=(r["name"] or "")[:20], text=f"管理:刪除:{r['shop_id']}")))
            items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
            line_bot_api.reply_message(event.reply_token, TextSendMessage("選擇要刪除的店家", quick_reply=QuickReply(items=items)))
            return

        if text.startswith("管理:刪除:") and user_id in ADMIN_IDS:
            sid = text.split(":", 2)[2]
            db.execute("DELETE FROM shops WHERE shop_id=?", (sid,))
            db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage("🗑 已刪除店家", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return
# ---- 店家配桌 ----
        if text == "店家配桌":
            # 已經在配桌 / 成桌中
            row = db.execute("SELECT status FROM match_users WHERE user_id=?", (user_id,)).fetchone()
            if row:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        "你目前已有配桌紀錄",
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                            QuickReplyButton(action=MessageAction(label="🚪 取消配桌", text="取消配桌")),
                        ])
                    )
                )
                return

            shops = db.execute("SELECT shop_id, name FROM shops WHERE open=1 AND approved=1").fetchall()
            if not shops:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))
                ])))
                return

            items = [QuickReplyButton(action=MessageAction(label=s["name"][:20], text=f"店家:{s['shop_id']}")) for s in shops]
            items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items)))
            return

        if text.startswith("店家:"):
            shop_id = text.split(":", 1)[1]
            user_state[user_id] = {"mode": "pick_amount", "shop_id": shop_id}
            items = [
                QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
                QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
                QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
                QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items+[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        if text.startswith("金額:"):
            amount = text.split(":", 1)[1]
            st = user_state.get(user_id, {})
            if not st.get("shop_id"):
                line_bot_api.reply_message(event.reply_token, TextSendMessage("流程已重置，請重新選擇店家配桌", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
                return
            st["amount"] = amount
            st["mode"] = "pick_people"
            user_state[user_id] = st
            items = [
                QuickReplyButton(action=MessageAction(label="我1人", text="人數:1")),
                QuickReplyButton(action=MessageAction(label="我2人", text="人數:2")),
                QuickReplyButton(action=MessageAction(label="我3人", text="人數:3")),
                QuickReplyButton(action=MessageAction(label="我4人", text="人數:4")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
            ]
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請選擇人數", quick_reply=QuickReply(items=items)))
            return

        if text.startswith("人數:"):
            st = user_state.get(user_id, {})
            shop_id = st.get("shop_id")
            amount = st.get("amount")
            if not shop_id or not amount:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("流程已重置，請重新配桌", quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label="🏪 店家配桌", text="店家配桌")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ])))
                return

            try:
                people = int(text.split(":", 1)[1])
                if people < 1 or people > 4:
                    raise ValueError()
            except:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("人數格式錯誤"))
                return

            db.execute("""
                INSERT OR REPLACE INTO match_users(user_id, people, shop_id, amount, status, expire, table_id, table_index)
                VALUES(?,?,?,?, 'waiting', NULL, NULL, NULL)
            """, (user_id, people, shop_id, amount))
            db.commit()

            user_state.pop(user_id, None)

            # ✅ 嘗試成桌：若成桌，觸發者用 reply 顯示卡片按鈕
            created = try_make_table(db, shop_id, amount, reply_token=event.reply_token, trigger_user_id=user_id)
            if created:
                # 成桌時已 reply 卡片，這裡不要再 reply 文字，避免覆蓋
                return

            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已加入配桌等待中", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        # ---- 加入 / 放棄 ----
        if text == "加入":
            row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
            if not row:
                line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                return
            table_id = row["table_id"]
            db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
            db.commit()
            push_table(db, table_id, "✅ 有玩家加入")
            if check_confirm(db, table_id):
                return
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已確認加入"))
            return

        if text == "放棄":
            # 視同取消配桌：從 ready 退回 waiting（你要取消也可改成 DELETE）
            row = db.execute("SELECT table_id FROM match_users WHERE user_id=? AND status='ready'", (user_id,)).fetchone()
            if row:
                table_id = row["table_id"]
                db.execute("""
                    UPDATE match_users
                    SET status='waiting', expire=NULL, table_id=NULL, table_index=NULL
                    WHERE user_id=?
                """, (user_id,))
                db.commit()
                try:
                    push_table(db, table_id, "❌ 有玩家放棄（繼續等待補人）")
                except:
                    pass
            line_bot_api.reply_message(event.reply_token, TextSendMessage("❌ 已放棄，已退回等待池"))
            return

        if text == "取消配桌":
            db.execute("DELETE FROM match_users WHERE user_id=?", (user_id,))
            db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage("🚪 已取消配桌"))
            return

        # ---- 店家合作（簡化版）----
        if text == "店家合作":
            row = db.execute("SELECT shop_id, approved FROM shops WHERE owner_id=? ORDER BY shop_id DESC", (user_id,)).fetchone()
            if not row:
                user_state[user_id] = {"mode": "shop_apply"}
                line_bot_api.reply_message(event.reply_token, TextSendMessage("請輸入店家名稱", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            if row["approved"] != 1:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("⏳ 尚未審核通過", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            line_bot_api.reply_message(event.reply_token, shop_menu())
            return

        if user_state.get(user_id, {}).get("mode") == "shop_apply":
            name = text.strip()[:30]
            sid = f"{user_id}_{int(time.time())}"
            db.execute("""
                INSERT OR REPLACE INTO shops(shop_id, name, open, approved, group_link, owner_id)
                VALUES(?,?,0,0,'',?)
            """, (sid, name, user_id))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已送出申請，等待管理員審核", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        if text == "開始營業":
            row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY shop_id DESC", (user_id,)).fetchone()
            if row:
                db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (row["shop_id"],))
                db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage("🟢 已開始營業", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        if text == "今日休息":
            row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY shop_id DESC", (user_id,)).fetchone()
            if row:
                db.execute("UPDATE shops SET open=0 WHERE shop_id=?", (row["shop_id"],))
                db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage("🔴 今日休息", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        if text == "設定群組":
            user_state[user_id] = {"mode": "set_group"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage("請貼上群組邀請連結（https://line.me/...）"))
            return

        if user_state.get(user_id, {}).get("mode") == "set_group":
            link = text.strip()
            row = db.execute("SELECT shop_id FROM shops WHERE owner_id=? ORDER BY shop_id DESC", (user_id,)).fetchone()
            if not row:
                user_state.pop(user_id, None)
                line_bot_api.reply_message(event.reply_token, TextSendMessage("你尚未綁定店家", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            sid = row["shop_id"]
            db.execute("UPDATE shops SET group_link=? WHERE shop_id=?", (link, sid))
            db.commit()
            user_state.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已設定群組連結", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

        
        # ---- 店家地圖（列表版，確保有回應）----
        if text == "店家地圖":
            shops = db.execute("SELECT name, shop_id FROM shops WHERE open=1 AND approved=1").fetchall()
            if not shops:
                line_bot_api.reply_message(event.reply_token, TextSendMessage("目前沒有營業店家", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
                return
            msg = "🗺 營業店家列表\n\n"
            for s in shops:
                msg += f"🏪 {s['name']}\nID: {s['shop_id']}\n\n"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單"))])))
            return

# fallback
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))

# ---- Render 啟動 ----
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # 確保啟動前建表
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=port)
