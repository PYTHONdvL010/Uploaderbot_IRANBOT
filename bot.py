import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.tl.custom import Message

# Telegram's public API ID/hash are used by the client library.
# These are public application credentials used by many Telegram clients.
API_ID = 2040
API_HASH = "b18441a1e2b9c2f1c3e5d4a6b7c8d9e0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "/app/data/uploader.db").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
db.execute("""CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    file_name TEXT,
    file_size INTEGER,
    caption TEXT,
    category_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(category_id) REFERENCES categories(id)
)""")
db.commit()

client = TelegramClient("uploader_bot", API_ID, API_HASH)

# Per-user temporary state. Persistent data is only categories/uploads.
states = {}

MAIN_BUTTON = "📁 فایل‌های آپلود شده"
ADMIN_BUTTON = "⚙️ مدیریت"

def is_admin(uid):
    return uid in ADMIN_IDS

def main_keyboard(uid):
    rows = [[Button.text(MAIN_BUTTON)]]
    if is_admin(uid):
        rows.append([Button.text(ADMIN_BUTTON)])
    return rows

def admin_keyboard():
    return [
        [Button.text("📂 دسته‌بندی‌ها"), Button.text("📤 Upload")],
        [Button.text("🔙 بازگشت")]
    ]

def category_keyboard(categories, include_none=True):
    rows = []
    for c in categories:
        rows.append([Button.inline(c["name"], data=f"cat:{c['id']}")])
    if include_none:
        rows.append([Button.inline("بدون دسته‌بندی", data="cat:0")])
    rows.append([Button.inline("🔙 بازگشت", data="back_admin")])
    return rows

def human_size(n):
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    x = float(n)
    while x >= 1024 and i < len(units)-1:
        x /= 1024
        i += 1
    return f"{x:.2f} {units[i]}"

def get_categories():
    return db.execute("SELECT * FROM categories ORDER BY id DESC").fetchall()

@client.on(events.NewMessage(pattern="/start"))
async def start(event):
    uid = event.sender_id
    states.pop(uid, None)
    text = "سلام 👋\nبه ربات آپلودر خوش اومدی."
    if is_admin(uid):
        text += "\n\nشما به بخش مدیریت دسترسی دارید."
    await event.respond(text, buttons=main_keyboard(uid))

@client.on(events.NewMessage)
async def text_handler(event):
    if not event.is_private or event.raw_text.startswith("/"):
        return

    uid = event.sender_id
    text = event.raw_text.strip()

    if text == MAIN_BUTTON:
        cats = get_categories()
        if not cats:
            await event.respond("📂 هیچ محصول و دسته‌بندی‌ای وجود ندارد.", buttons=main_keyboard(uid))
            return
        rows = [[Button.inline(c["name"], data=f"browse:{c['id']}")] for c in cats]
        rows.append([Button.inline("بدون دسته‌بندی", data="browse:0")])
        await event.respond("📁 دسته‌بندی موردنظر را انتخاب کن:", buttons=rows)
        return

    if text == ADMIN_BUTTON and is_admin(uid):
        states.pop(uid, None)
        await event.respond("⚙️ بخش مدیریت:", buttons=admin_keyboard())
        return

    if text == "🔙 بازگشت":
        states.pop(uid, None)
        await event.respond("منوی اصلی:", buttons=main_keyboard(uid))
        return

    if not is_admin(uid):
        return

    state = states.get(uid)

    if text == "📂 دسته‌بندی‌ها":
        cats = get_categories()
        msg = "📂 دسته‌بندی‌ها\n\n"
        if not cats:
            msg += "دسته‌بندی خالی است."
        else:
            msg += "\n".join(f"• {c['name']}" for c in cats)
        msg += "\n\nبرای ساخت دسته‌بندی جدید /addcat را بزن."
        await event.respond(msg, buttons=admin_keyboard())
        return

    if text == "📤 Upload":
        states[uid] = {"step": "wait_file"}
        await event.respond(
            "📤 فایل را ارسال کن.\n\n"
            "حداکثر حجم: 2 GB\n"
            "فایل‌های بزرگ‌تر از این مقدار تأیید نمی‌شوند."
        )
        return

    if text == "/addcat":
        states[uid] = {"step": "cat_name"}
        await event.respond("✏️ اسم دسته‌بندی را ارسال کن:")
        return

    if state and state.get("step") == "cat_name":
        name = text
        try:
            db.execute("INSERT INTO categories(name) VALUES (?)", (name,))
            db.commit()
            states.pop(uid, None)
            await event.respond(f"✅ دسته‌بندی «{name}» با موفقیت اضافه شد.", buttons=admin_keyboard())
        except sqlite3.IntegrityError:
            await event.respond("⚠️ این دسته‌بندی قبلاً وجود دارد. یک نام دیگر بفرست.")
        return

    if state and state.get("step") == "caption":
        if text == "0":
            caption = ""
        else:
            caption = text
        states[uid]["caption"] = caption
        states[uid]["step"] = "category"
        cats = get_categories()
        if cats:
            await event.respond(
                "📂 کدوم دسته‌بندی؟",
                buttons=category_keyboard(cats)
            )
        else:
            await event.respond(
                "⚠️ هنوز هیچ دسته‌بندی‌ای ساخته نشده.\n"
                "آپلود بدون دسته‌بندی انجام می‌شود.",
                buttons=[[Button.inline("بدون دسته‌بندی", data="cat:0")]]
            )
        return

@client.on(events.NewMessage)
async def file_handler(event):
    if not event.is_private or not event.message.media:
        return
    uid = event.sender_id
    if not is_admin(uid):
        return
    state = states.get(uid)
    if not state or state.get("step") != "wait_file":
        return

    msg = event.message
    size = getattr(msg.file, "size", None) or 0
    if size > 2 * 1024 * 1024 * 1024:
        await event.respond("❌ حجم فایل بیشتر از سقف 2 GB است و تأیید نمی‌شود.")
        return

    name = msg.file.name or f"file_{msg.id}"
    states[uid] = {
        "step": "caption",
        "source_chat_id": event.chat_id,
        "source_message_id": msg.id,
        "file_name": name,
        "file_size": size,
    }
    await event.respond(
        f"✅ فایل دریافت شد.\n"
        f"📄 نام: {name}\n"
        f"📦 حجم: {human_size(size)}\n\n"
        "کپشن می‌خواهی؟\n"
        "اگر 0 بفرستی، بدون کپشن ذخیره می‌شود.\n"
        "هر متنی بفرستی همان به‌عنوان کپشن ثبت می‌شود."
    )

@client.on(events.CallbackQuery)
async def callbacks(event):
    uid = event.sender_id
    data = event.data.decode()

    if data == "back_admin":
        await event.edit("⚙️ بخش مدیریت:", buttons=admin_keyboard())
        return

    if data.startswith("cat:") and is_admin(uid):
        cat_id = int(data.split(":")[1])
        state = states.get(uid)
        if not state or state.get("step") != "category":
            await event.answer("این مرحله منقضی شده.", alert=True)
            return

        caption = state.get("caption", "")
        category_id = cat_id if cat_id else None
        db.execute(
            """INSERT INTO uploads
               (owner_id, source_chat_id, source_message_id, file_name, file_size, caption, category_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, state["source_chat_id"], state["source_message_id"],
             state["file_name"], state["file_size"], caption, category_id)
        )
        db.commit()
        states.pop(uid, None)

        if category_id:
            c = db.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
            msg = f"✅ فایل با موفقیت آپلود شد.\n📂 دسته‌بندی: {c['name']}"
        else:
            msg = "✅ فایل با موفقیت آپلود شد.\n📂 بدون دسته‌بندی"

        await event.edit(msg, buttons=admin_keyboard())
        return

    if data.startswith("browse:"):
        cat_id = int(data.split(":")[1])
        if cat_id:
            c = db.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
            if not c:
                await event.answer("دسته‌بندی پیدا نشد.", alert=True)
                return
            uploads = db.execute(
                "SELECT * FROM uploads WHERE category_id=? ORDER BY id DESC", (cat_id,)
            ).fetchall()
            title = f"📂 {c['name']}"
        else:
            uploads = db.execute(
                "SELECT * FROM uploads WHERE category_id IS NULL ORDER BY id DESC"
            ).fetchall()
            title = "📂 بدون دسته‌بندی"

        if not uploads:
            await event.edit(f"{title}\n\nهیچ فایلی در این دسته وجود ندارد.")
            return

        rows = []
        for u in uploads:
            label = u["file_name"] or f"File #{u['id']}"
            if len(label) > 50:
                label = label[:47] + "..."
            rows.append([Button.inline(label, data=f"file:{u['id']}")])
        await event.edit(title, buttons=rows)
        return

    if data.startswith("file:"):
        file_id = int(data.split(":")[1])
        u = db.execute("SELECT * FROM uploads WHERE id=?", (file_id,)).fetchone()
        if not u:
            await event.answer("فایل پیدا نشد.", alert=True)
            return

        await event.answer("در حال ارسال فایل...")
        caption = u["caption"] or ""
        try:
            source = await client.get_messages(u["source_chat_id"], ids=u["source_message_id"])
            if not source:
                raise RuntimeError("source message unavailable")
            await client.send_file(
                uid,
                source,
                caption=caption
            )
        except Exception as exc:
            await event.answer("ارسال فایل انجام نشد.", alert=True)
            print("send error:", repr(exc))
        return

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"Uploader bot started: @{me.username}")
    print(f"Admins: {sorted(ADMIN_IDS)}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
