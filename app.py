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
COUNTDOWN_READY = 30
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
    
    
    # add reminder flags (ignore if already exists)
    try:
        db.execute("ALTER TABLE match_users ADD COLUMN remind20 INT DEFAULT 0")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE match_users ADD COLUMN remind10 INT DEFAULT 0")
    except Exception:
        pass
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
    
    db.execute("""
    CREATE TABLE IF NOT EXISTS shops(
        shop_id TEXT,
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
    db.commit()
def main_menu(user_id=None):
    items = [
        QuickReplyButton(action=MessageAction(label="🏪 店家配桌 🏪", text="店家配桌")),
        QuickReplyButton(action=MessageAction(label="📒 記事本 📒", text="記事本")),
        QuickReplyButton(action=MessageAction(label="🗺 店家地圖 🗺", text="店家地圖")),
        QuickReplyButton(action=MessageAction(label="🏪 店家合作", text="店家合作")),
        QuickReplyButton(action=MessageAction(label="👤 設定暱稱", text="設定暱稱")),
    ]
    if user_id in ADMIN_IDS:
        items.append(
            QuickReplyButton(action=MessageAction(label="🛠 店家管理", text="店家管理"))
        )
    return TextSendMessage("請選擇功能", quick_reply=QuickReply(items=items))
def back_menu():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        QuickReplyButton(action=MessageAction(label="👤 設定暱稱", text="設定暱稱")),
    ])
def get_group_link(shop_id):
    db = get_db()
    row = db.execute("SELECT group_link FROM shops WHERE shop_id=?", (shop_id,)).fetchone()
    link = row[0].strip() if row and row[0] else None
    if not link or not link.startswith("http"):
        return None
    return link
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
def get_nickname(user_id):
    db = get_db()
    row = db.execute("SELECT nickname FROM nicknames WHERE user_id=?", (user_id,)).fetchone()
    if row and row[0]:
        return row[0]
    return f"玩家{user_id[-4:]}"
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
    total_people = sum(r[2] for r in rows)
    confirmed_people = sum(r[2] for r in rows if r[1] == "confirmed")
    msg = f"{title}\n\n"
    msg += f"👥 人數：{total_people} / 4\n"
    msg += f"✅ 已確認：{confirmed_people} / 4\n\n"
    for i, (uid, status, p) in enumerate(rows, 1):
        name = get_nickname(uid)
        if status == "ready":
            icon = "📩"
            st = "待確認"
        elif status == "confirmed":
            icon = "✅"
            st = "已加入"
        else:
            icon = "⏳"
            st = status
        msg += f"{i}. {name}｜{p}人 {icon} {st}\n"
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
    msg = (
        f"🎉 成桌完成（等待確認）\n"
        f"🪑 桌號：{table_index}\n"
        f"💰 金額：{amount}\n\n"
        f"⏱ {COUNTDOWN_READY} 秒內未確認視同【放棄】\n"
        f"請按下方按鈕：加入 / 放棄"
    )
    for u in selected:
        try:
            line_bot_api.push_message(u, TemplateSendMessage(
                alt_text="成桌確認",
                template=ButtonsTemplate(
                    title="成桌確認",
                    text=msg[:160],
                    actions=[
                        MessageAction(label="✅ 加入", text="加入"),
                        MessageAction(label="❌ 放棄", text="放棄"),
                    ]
                )
            ))
        except Exception as e:
            print("push ready error:", e)
    push_table(table_id, "🪑 桌子成立")
def check_confirm(table_id):
    db = get_db()
    rows = db.execute("""
        SELECT user_id, status, people
        FROM match_users
        WHERE table_id=?
    """, (table_id,)).fetchall()
    if not rows:
        return False
    total_people = sum(p for _, _, p in rows)
    confirmed_people = sum(p for _, st, p in rows if st == "confirmed")
    if total_people != 4 or confirmed_people != 4:
        return False
    t = db.execute(
        "SELECT shop_id,amount,table_index FROM tables WHERE id=?",
        (table_id,)
    ).fetchone()
    if not t:
        return False
    shop_id, amount, table_index = t
    group = get_group_link(shop_id)
    group_text = f"🔗 連結：{group}" if group else "🔗 連結：（店家尚未設定群組連結）"
    for (u, _, _) in rows:
        try:
            line_bot_api.push_message(
                u,
                TextSendMessage(
                    f"🎉 配桌成功\n\n"
                    f"🪑 桌號：{table_index}\n"
                    f"{group_text}\n\n"
                    f"🔔 提示：進群後請回報桌號【{table_index}】"
                )
            )
        except Exception as e:
            print("push success error:", e)
    # ✅ 清掉本桌資料，回到未配桌狀態
    db.execute("DELETE FROM match_users WHERE table_id=?", (table_id,))
    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
    db.commit()
    return True
def timeout_checker():
    # ✅ 背景執行緒需要 Flask app context，否則 get_db()/g 會報錯
    with app.app_context():
        init_db()
        while True:
            try:
                db = get_db()
                now = time.time()
                # ===== 10 秒提醒（只提醒兩次：剩 20s、剩 10s）=====
                # 20 秒提醒
                r20 = db.execute("""
                    SELECT user_id, table_id
                    FROM match_users
                    WHERE status='ready'
                      AND expire IS NOT NULL
                      AND (expire - ?) <= 20
                      AND (expire - ?) > 10
                      AND COALESCE(remind20,0)=0
                """, (now, now)).fetchall()
                for (uid, table_id) in r20:
                    db.execute("UPDATE match_users SET remind20=1 WHERE user_id=?", (uid,))
                    try:
                        line_bot_api.push_message(uid, TextSendMessage("⏳ 剩餘 20 秒未確認視同放棄"))
                    except Exception as e:
                        print("remind20 push error:", e)
                # 10 秒提醒
                r10 = db.execute("""
                    SELECT user_id, table_id
                    FROM match_users
                    WHERE status='ready'
                      AND expire IS NOT NULL
                      AND (expire - ?) <= 10
                      AND (expire - ?) > 0
                      AND COALESCE(remind10,0)=0
                """, (now, now)).fetchall()
                for (uid, table_id) in r10:
                    db.execute("UPDATE match_users SET remind10=1 WHERE user_id=?", (uid,))
                    try:
                        line_bot_api.push_message(uid, TextSendMessage("⏳ 剩餘 10 秒未確認視同放棄"))
                    except Exception as e:
                        print("remind10 push error:", e)
                db.commit()
                # ===== 超時處理：未確認視同放棄（取消本桌，回到等待池）=====
                rows = db.execute("""
                    SELECT DISTINCT table_id
                    FROM match_users
                    WHERE status='ready' AND expire IS NOT NULL AND expire < ?
                      AND table_id IS NOT NULL
                """, (now,)).fetchall()
                for (table_id,) in rows:
                    tinfo = db.execute("SELECT shop_id, amount FROM tables WHERE id=?", (table_id,)).fetchone()
                    if not tinfo:
                        continue
                    shop_id, amount = tinfo
                    users = db.execute("SELECT user_id FROM match_users WHERE table_id=?", (table_id,)).fetchall()
                    # 全桌退回 waiting（避免 3 人卡在 ready）
                    for (uid,) in users:
                        db.execute("""
                            UPDATE match_users
                            SET status='waiting',
                                expire=NULL,
                                table_id=NULL,
                                table_index=NULL,
                                remind20=0,
                                remind10=0
                            WHERE user_id=?
                        """, (uid,))
                    db.execute("DELETE FROM tables WHERE id=?", (table_id,))
                    db.commit()
                    # 通知桌上所有人
                    for (uid,) in users:
                        try:
                            line_bot_api.push_message(uid, TextSendMessage("⏳ 30 秒內未確認視同放棄\n本桌已取消，已回到等待池 ✅"))
                        except Exception as e:
                            print("timeout push error:", e)
                    # 重新嘗試湊桌
                    try:
                        try_make_table(shop_id, amount)
                    except Exception as e:
                        print("try_make_table after timeout error:", e)
            except Exception as e:
                print("timeout error:", e)
            time.sleep(3)
threading.Thread(target=timeout_checker, daemon=True).start()
def get_shop_id_by_user(db, user_id):
    row = db.execute(
        "SELECT shop_id FROM shops WHERE owner_id=? ORDER BY rowid DESC",
        (user_id,)
    ).fetchone()
    return row[0] if row else None
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
    try:
            init_db()
            db = get_db()
            user_id = event.source.user_id
            text = (event.message.text or "").strip()
            # ✅ 回主選單（全域）
            if text == "選單":
                user_state.pop(user_id, None)
                line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                return True
            # ✅ 暱稱設定
            if text == "設定暱稱":
                user_state[user_id] = {"mode": "set_nick"}
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("請輸入你的暱稱（最多 10 個字）", quick_reply=back_menu())
                )
                return True
            if user_state.get(user_id, {}).get("mode") == "set_nick":
                nick = (text or "").strip()
                if not nick:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage("請輸入暱稱（最多 10 個字），或按『選單』返回", quick_reply=back_menu())
                    )
                    return True
                nick = nick[:10]
                db.execute("INSERT OR REPLACE INTO nicknames(user_id, nickname) VALUES(?,?)", (user_id, nick))
                db.commit()
                user_state.pop(user_id, None)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(f"✅ 已設定暱稱：{nick}", quick_reply=back_menu())
                )
                return True
            
# ✅ admin 最先
            if handle_admin_logic(event, user_id, text, db):
                return True
            # ✅ shop 第二
            if handle_shop_logic(event, user_id, text, db):
                return True
        # === 店家配桌 ===
            if text == "店家配桌":
                # ✅ 進入配桌前先清掉可能卡住的狀態（避免被店家合作/記事本輸入模式攔截）
                user_state.pop(user_id, None)
                row = db.execute(
                    "SELECT status FROM match_users WHERE user_id=?",
                    (user_id,)
                ).fetchone()
                # === 已經在配桌中 ===
                if row:
                    items = [
                        QuickReplyButton(action=MessageAction(label="🔍 查看進度", text="查看進度")),
                        QuickReplyButton(action=MessageAction(label="❌ 取消配桌", text="取消配桌")),
                        QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                    ]
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage("你目前已有配桌紀錄", quick_reply=QuickReply(items=items))
                    )
                    return True
                # === 尚未配桌 ===
                rows = db.execute(
                    "SELECT shop_id,name FROM shops WHERE open=1 AND approved=1"
                ).fetchall()
                if not rows:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage("目前沒有營業店家", quick_reply=back_menu())
                    )
                    return True
                items = [
                    QuickReplyButton(action=MessageAction(label=n, text=f"店家:{sid}"))
                    for sid, n in rows
                ]
                items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("請選擇店家", quick_reply=QuickReply(items=items))
                )
                return True
            # === 查看進度 ===
            if text == "查看進度":
                row = db.execute("""
                    SELECT shops.name, match_users.amount, match_users.people, match_users.status
                    FROM match_users
                    JOIN shops ON match_users.shop_id = shops.shop_id
                    WHERE match_users.user_id=?
                """, (user_id,)).fetchone()
                if not row:
                    line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                    return True
                name, amount, people, status = row
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        f"📌 配桌狀態\n\n🏪 {name}\n💰 {amount}\n👥 {people} 人\n📍 {status}",
                        quick_reply=back_menu()
                    )
                )
                return True
            # ===== 選店 =====
            if text.startswith("店家:"):
                shop_id = text.split(":", 1)[1]
                user_state[user_id] = {"step": "wait_amount", "shop_id": shop_id}
                items = [
                    QuickReplyButton(action=MessageAction(label="50/20", text="金額:50/20")),
                    QuickReplyButton(action=MessageAction(label="100/20", text="金額:100/20")),
                    QuickReplyButton(action=MessageAction(label="100/50", text="金額:100/50")),
                    QuickReplyButton(action=MessageAction(label="200/50", text="金額:200/50")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ]
                line_bot_api.reply_message(event.reply_token,
                    TextSendMessage("請選擇金額", quick_reply=QuickReply(items=items)))
                return True
            # ===== 金額 =====
            if text.startswith("金額:"):
                amount = text.split(":", 1)[1]
                user_state.setdefault(user_id, {})["amount"] = amount
                items = [
                    QuickReplyButton(action=MessageAction(label="我1人", text="人數:1")),
                    QuickReplyButton(action=MessageAction(label="我2人", text="人數:2")),
                    QuickReplyButton(action=MessageAction(label="我3人", text="人數:3")),
                    QuickReplyButton(action=MessageAction(label="我4人", text="人數:4")),
                    QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
                ]
                line_bot_api.reply_message(event.reply_token,
                    TextSendMessage("請選擇人數", quick_reply=QuickReply(items=items)))
                return True
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
                return True
            # ===== 加入 =====
            if text == "加入":
                row = db.execute(
                    "SELECT table_id FROM match_users WHERE user_id=? AND status='ready'",
                    (user_id,)
                ).fetchone()
                if not row:
                    line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                    return True
                table_id = row[0]
                db.execute("UPDATE match_users SET status='confirmed' WHERE user_id=?", (user_id,))
                db.commit()
                push_table(table_id, "✅ 有玩家加入")
                if check_confirm(table_id):
                    user_state.pop(user_id, None)
                    line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                    return True
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("✅ 已確認加入（等待其他人）", quick_reply=back_menu())
                )
                return True
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
                return True
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
                return True
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
                return True
            # ===== 新增紀錄 =====
            if text == "新增紀錄":
                user_state[user_id] = {"mode": "note_amount"}
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("請輸入金額，例如：1000 或 -500", quick_reply=back_menu())
                )
                return True
            # ===== 記事本輸入金額 =====
            if user_state.get(user_id, {}).get("mode") == "note_amount":
                val = text.strip()
                if not re.fullmatch(r"-?\d+", val):
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage("請直接輸入金額，例如：1000 或 -500", quick_reply=back_menu())
                    )
                    return True
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
                return True
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
                    return True
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
                return True
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
                    return True
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
                return True
            # ===== 清除紀錄 =====
            if text == "清除紀錄":
                db.execute("DELETE FROM notes WHERE user_id=?", (user_id,))
                db.commit()
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("🧹 已清除所有記事本紀錄", quick_reply=back_menu())
                )
                return True
            # ===== 店家地圖 =====
            if text == "店家地圖":
                rows = db.execute("""
                    SELECT name, partner_map 
                    FROM shops 
                    WHERE approved=1 AND open=1 AND partner_map IS NOT NULL
                """).fetchall()
                # 沒店家
                if not rows:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(
                            "🚫 未有營業店家",
                            quick_reply=back_menu()
                        )
                    )
                    return True
                items = []
                for name, link in rows:
                    if not link:
                        continue
                    if not link.startswith("http"):
                        continue
                    items.append(
                        QuickReplyButton(
                            action=URIAction(label=f"🏪 {name}"[:20], uri=link)
                        )
                    )
                # 一定要有返回主畫面
                # ✅ 若店家有上線但沒有可用的地圖連結（partner_map 未設定或不是 http）
                if not items:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage("⚠️ 目前沒有可用的地圖連結（店家尚未設定地圖網址）", quick_reply=back_menu())
                    )
                    return True
                items.append(
                    QuickReplyButton(
                        action=MessageAction(label="🏠 回主畫面", text="選單")
                    )
                )
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        "📍 請選擇店家地圖：",
                        quick_reply=QuickReply(items=items)
                    )
                )
                return True
            # ===== 回主選單 =====
            if text == "選單":
                user_state.pop(user_id, None)
                line_bot_api.reply_message(event.reply_token, main_menu(user_id))
                return True
            # ===== 兜底：任何沒命中的文字都回主選單 =====
            # 注意：需要使用者輸入資料的流程（例如記事本金額、店家名稱、群組連結）
            # 在前面都應該已經 return True，走到這裡代表是未知指令。
            line_bot_api.reply_message(event.reply_token, main_menu(user_id))
            return True
        # ================= 店家合作 ================= #  
    except Exception as e:
        print('handle_message error:', e)
        try:
            # 兜底回主選單，避免使用者端看起來像「閃退/沒回應」
            uid = getattr(getattr(event, 'source', None), 'user_id', None)
            if uid and hasattr(event, 'reply_token'):
                line_bot_api.reply_message(event.reply_token, main_menu(uid))
        except Exception as e2:
            print('handle_message fallback error:', e2)
        return True
def show_shop_menu(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage("🏪 店家合作", quick_reply=QuickReply(items=[
            QuickReplyButton(action=MessageAction(label="🟢 開始營業", text="開始營業")),
            QuickReplyButton(action=MessageAction(label="🔴 今日休息", text="今日休息")),
            QuickReplyButton(action=MessageAction(label="🔗 設定群組", text="設定群組")),
            QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
        ]))
    )
    return True
    # ================= 兜底：未知文字回主選單 =================
    mode = user_state.get(user_id, {}).get("mode")
    input_modes = {"note_amount", "shop_input", "shop_set_group", "set_nick"}
    if mode in input_modes:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請依照提示輸入，或按『選單』返回主畫面", quick_reply=back_menu())
        )
        return True
    line_bot_api.reply_message(event.reply_token, main_menu(user_id))
    return True
def handle_shop_logic(event, user_id, text, db):
    mode = user_state.get(user_id, {}).get("mode")
    # ================= 新增店家名稱 =================
    if mode == "shop_input":
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
            TextSendMessage(f"🏪 {name}\n\n✅ 已送出申請，等待審核", quick_reply=back_menu())
        )
        return True
    # ================= 等待審核 =================
    if mode == "shop_wait":
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
            TextSendMessage("⏳ 尚未審核通過，請稍候管理員審核", quick_reply=back_menu())
        )
        return True
    # ================= 回主畫面 =================
    if text == "選單":
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return True
    # ================= 進入店家合作 =================
    if text == "店家合作":
        # 強制重置亂掉的 state
        user_state.pop(user_id, None)
        row = db.execute(
            "SELECT shop_id, approved FROM shops WHERE owner_id=? ORDER BY rowid DESC",
            (user_id,),
        ).fetchone()
        # 尚未申請
        if not row:
            user_state[user_id] = {"mode": "shop_input"}
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("請輸入店家名稱", quick_reply=back_menu())
            )
            return True
        sid, ap = row
        user_state[user_id] = {
            "mode": "shop_menu" if ap == 1 else "shop_wait",
            "shop_id": sid
        }
        # 尚未審核
        if ap == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("⏳ 尚未審核通過，請等待管理員審核", quick_reply=back_menu())
            )
            return True
        return show_shop_menu(event)
    # ================= 開始營業 =================
    if text == "開始營業":
        sid = user_state.get(user_id, {}).get("shop_id")
        if not sid:
            sid = get_shop_id_by_user(db, user_id)
        if not sid:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("你尚未綁定店家", quick_reply=back_menu())
            )
            return True
        db.execute("UPDATE shops SET open=1 WHERE shop_id=?", (sid,))
        db.commit()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🟢 已開始營業", quick_reply=back_menu())
        )
        return True
    # ================= 今日休息 =================
    if text == "今日休息":
        sid = user_state.get(user_id, {}).get("shop_id")
        if not sid:
            sid = get_shop_id_by_user(db, user_id)
        if not sid:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("你尚未綁定店家", quick_reply=back_menu())
            )
            return True
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
        line_bot_api.reply_message(event.reply_token, main_menu(user_id))
        return True
    # === 管理選單 ===
    if user_id in ADMIN_IDS and text == "店家管理":
        user_state[user_id] = {"mode": "admin_menu"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🛠 店家管理", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="📋 查看", text="查看")),
                QuickReplyButton(action=MessageAction(label="✅ 審核", text="審核")),
                QuickReplyButton(action=MessageAction(label="🗑 刪除", text="刪除")),
                QuickReplyButton(action=MessageAction(label="🗺 地圖設定", text="地圖設定")),
                QuickReplyButton(action=MessageAction(label="🔙 回主選單", text="選單")),
            ]))
        )
        return True
    # === 查看 ===
    if user_id in ADMIN_IDS and text == "查看":
        rows = db.execute("SELECT shop_id,name,open,approved FROM shops").fetchall()
        msg = "🏪 店家列表\n\n"
        for sid, name, open_, ap in rows:
            msg += f"{name}\n狀態：{'營業中' if open_ else '未營業'} | {'✅通過' if ap else '❌未審核'}\nID:{sid}\n\n"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg, quick_reply=back_menu()))
        return True
    # === 審核 ===
    if user_id in ADMIN_IDS and text == "審核":
        rows = db.execute("SELECT shop_id,name,approved FROM shops").fetchall()
        if not rows:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("目前沒有店家", quick_reply=back_menu())
            )
            return True
        items = []
        for sid, name, ap in rows:
            label = f"🏪 {name}"
            items.append(
                QuickReplyButton(
                    action=MessageAction(label=label[:20], text=f"審核:{sid}")
                )
            )
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        user_state[user_id] = {"mode": "admin_review_select"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🛠 選擇要審核的店家", quick_reply=QuickReply(items=items))
        )
        return True
    if user_state.get(user_id, {}).get("mode") == "admin_review_select" and text.startswith("審核:"):
        sid = text.split(":", 1)[1]
        user_state[user_id] = {"mode": "admin_review_confirm", "sid": sid}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請選擇審核結果", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 通過", text="同意審核")),
                QuickReplyButton(action=MessageAction(label="❌ 不通過", text="不同意審核")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
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
            # ✅ 清掉申請者卡死狀態
            row = db.execute("SELECT owner_id FROM shops WHERE shop_id=?", (sid,)).fetchone()
            if row:
                user_state.pop(row[0], None)
        elif text == "不同意審核":
            db.execute("UPDATE shops SET approved=0 WHERE shop_id=?", (sid,))
        db.commit()
        user_state.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ 已更新", quick_reply=back_menu()))
        return True
    # === 刪除 ===
    if user_id in ADMIN_IDS and text == "刪除":
        rows = db.execute("SELECT shop_id,name FROM shops").fetchall()
        if not rows:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("目前沒有店家", quick_reply=back_menu())
            )
            return True
        items = []
        for sid, name in rows:
            items.append(
                QuickReplyButton(
                    action=MessageAction(label=f"🏪 {name}"[:20], text=f"刪除:{sid}")
                )
            )
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        user_state[user_id] = {"mode": "admin_delete_select"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🗑 選擇要刪除的店家", quick_reply=QuickReply(items=items))
        )
        return True
    
    if user_state.get(user_id, {}).get("mode") == "admin_delete_select" and text.startswith("刪除:"):
        sid = text.split(":", 1)[1]
        user_state[user_id] = {"mode": "admin_delete_confirm", "sid": sid}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("⚠ 確定刪除？", quick_reply=QuickReply(items=[
                QuickReplyButton(action=MessageAction(label="✅ 確定刪除", text="確認刪除")),
                QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")),
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
        
    if user_id in ADMIN_IDS and text == "地圖設定":
        rows = db.execute("SELECT shop_id,name FROM shops WHERE approved=1").fetchall()
        if not rows:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage("目前沒有已核准店家", quick_reply=back_menu())
            )
            return True
        items = []
        for sid, name in rows:
            items.append(
                QuickReplyButton(
                    action=MessageAction(label=f"🏪 {name}"[:20], text=f"地圖:{sid}")
                )
            )
        items.append(QuickReplyButton(action=MessageAction(label="🔙 回主畫面", text="選單")))
        user_state[user_id] = {"mode": "admin_map_select"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("🗺 選擇要設定地圖的店家", quick_reply=QuickReply(items=items))
        )
        return True
    if user_state.get(user_id, {}).get("mode") == "admin_map_select" and text.startswith("地圖:"):
        sid = text.split(":", 1)[1]
        user_state[user_id] = {"mode": "admin_map_input", "sid": sid}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("請貼上 Google Map 連結", quick_reply=back_menu())
        )
        return True
    if user_state.get(user_id, {}).get("mode") == "admin_map_input":
        sid = user_state[user_id]["sid"]
        db.execute("UPDATE shops SET partner_map=? WHERE shop_id=?", (text, sid))
        db.commit()
        user_state.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("✅ 已更新店家地圖", quick_reply=back_menu())
        )
        return True
# ================= MAIN =================
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=5000)
