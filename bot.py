import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

DB_PATH = os.getenv("DB_PATH", "/app/data/uploader.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL)")
db.execute("""CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_file_id TEXT NOT NULL,
    name TEXT,
    caption TEXT,
    category_id INTEGER,
    uploaded_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(category_id) REFERENCES categories(id))""")
db.commit()


def main_kb(admin=False):
    buttons = [[InlineKeyboardButton("📁 فایل‌های آپلود شده", callback_data="menu:files")]]
    if admin:
        buttons.append([InlineKeyboardButton("⚙️ مدیریت", callback_data="menu:admin")])
    return InlineKeyboardMarkup(buttons)


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 دسته‌بندی‌ها", callback_data="admin:categories"),
         InlineKeyboardButton("📤 آپلود", callback_data="admin:upload")],
        [InlineKeyboardButton("📁 فایل‌های آپلود شده", callback_data="menu:files")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
    ])


def category_buttons(prefix="cat"):
    cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
    buttons = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{cid}")] for cid, name in cats]
    return buttons


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    admin = update.effective_user.id in ADMINS
    await update.message.reply_text(
        "سلام 👋\nبه ربات آپلودر خوش آمدید.",
        reply_markup=main_kb(admin),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    t = update.message.text.strip()
    uid = update.effective_user.id
    admin = uid in ADMINS
    state = context.user_data.get("state")

    # Category name entry
    if state == "add_cat" and admin:
        if not t:
            await update.message.reply_text("لطفاً یک نام برای دسته‌بندی بفرست.")
            return
        try:
            db.execute("INSERT INTO categories(name) VALUES(?)", (t,))
            db.commit()
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ دسته‌بندی «{t}» با موفقیت اضافه شد.",
                reply_markup=admin_kb(),
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text("⚠️ این دسته‌بندی قبلاً وجود دارد. یک نام دیگر بفرست.")
        return

    # Caption entry
    if state == "caption" and admin:
        context.user_data["caption"] = "" if t == "0" else t
        cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
        buttons = [[InlineKeyboardButton(name, callback_data=f"savecat:{cid}")] for cid, name in cats]
        buttons.append([InlineKeyboardButton("🚫 بدون دسته‌بندی", callback_data="savecat:0")])
        context.user_data["state"] = "choose_cat"
        await update.message.reply_text(
            "📂 کدام دسته‌بندی؟",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS or context.user_data.get("state") != "upload_wait":
        return

    msg = update.message
    media = msg.document or msg.video or msg.audio
    if not media:
        await msg.reply_text("لطفاً فایل را به‌صورت Document، Video یا Audio ارسال کنید.")
        return

    file_name = getattr(media, "file_name", None) or f"file_{media.file_unique_id}"
    context.user_data["pending_file"] = (media.file_id, file_name)
    context.user_data["state"] = "caption"
    await msg.reply_text(
        "📝 کپشن می‌خواهی؟\n\nمتن کپشن را بفرست.\nاگر کپشن نمی‌خواهی، `0` بفرست."
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""
    admin = uid in ADMINS

    if data == "menu:home":
        context.user_data.clear()
        await q.edit_message_text("منوی اصلی", reply_markup=main_kb(admin))
        return

    if data == "menu:admin":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "admin"
        await q.edit_message_text("⚙️ مدیریت", reply_markup=admin_kb())
        return

    if data == "admin:upload":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "upload_wait"
        await q.edit_message_text(
            "📤 فایل را ارسال کنید.\n\n"
            "محدودیت حجمی اضافه‌ای توسط ربات اعمال نمی‌شود و محدودیت واقعی Telegram Bot API ملاک است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")]
            ]),
        )
        return

    if data == "admin:categories":
        if not admin:
            return
        cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
        buttons = [[InlineKeyboardButton(name, callback_data=f"noop:{cid}")] for cid, name in cats]
        buttons.append([InlineKeyboardButton("➕ ساخت دسته‌بندی", callback_data="admin:add_category")])
        buttons.append([InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")])
        text = "📂 دسته‌بندی‌ها"
        if not cats:
            text += "\n\nهنوز هیچ دسته‌بندی ساخته نشده است."
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "admin:add_category":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "add_cat"
        await q.edit_message_text(
            "➕ نام دسته‌بندی جدید را ارسال کنید.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو", callback_data="menu:admin")]
            ]),
        )
        return

    if data == "menu:files":
        cats = db.execute("SELECT id,name FROM categories ORDER BY id").fetchall()
        total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if total == 0:
            await q.edit_message_text("📁 هیچ فایلی آپلود نشده است.", reply_markup=main_kb(admin))
            return
        buttons = [[InlineKeyboardButton(name, callback_data=f"cat:{cid}")] for cid, name in cats]
        buttons.append([InlineKeyboardButton("📁 بدون دسته‌بندی", callback_data="cat:0")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")])
        await q.edit_message_text("📂 دسته‌بندی را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("savecat:"):
        if not admin or context.user_data.get("state") != "choose_cat":
            return
        cid = int(data.split(":", 1)[1])
        pending = context.user_data.get("pending_file")
        if not pending:
            await q.edit_message_text("❌ فایل در انتظار پیدا نشد.", reply_markup=admin_kb())
            context.user_data.clear()
            return
        fid, name = pending
        cap = context.user_data.get("caption", "")
        db.execute(
            "INSERT INTO files(telegram_file_id,name,caption,category_id,uploaded_by) VALUES(?,?,?,?,?)",
            (fid, name, cap, cid or None, uid),
        )
        db.commit()
        context.user_data.clear()
        await q.edit_message_text("✅ فایل با موفقیت آپلود و ثبت شد.", reply_markup=admin_kb())
        return

    if data.startswith("cat:"):
        cid = int(data.split(":", 1)[1])
        rows = db.execute(
            "SELECT id,name,telegram_file_id,caption FROM files WHERE category_id IS ? ORDER BY id DESC",
            (cid or None,),
        ).fetchall()
        if not rows:
            await q.edit_message_text(
                "📂 این دسته‌بندی خالی است.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="menu:files")]]),
            )
            return
        buttons = [[InlineKeyboardButton((name or f"فایل {fid}")[:50], callback_data=f"file:{fid}")] for fid, name, _, _ in rows]
        buttons.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="menu:files")])
        context.user_data["browse"] = {fid: (telegram_fid, name, caption) for fid, name, telegram_fid, caption in rows}
        await q.edit_message_text("📄 فایل‌ها:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("file:"):
        fid = int(data.split(":", 1)[1])
        item = context.user_data.get("browse", {}).get(fid)
        if not item:
            await q.answer("فایل پیدا نشد. دوباره دسته‌بندی را باز کنید.", show_alert=True)
            return
        telegram_fid, name, cap = item
        await context.bot.send_document(q.message.chat_id, telegram_fid, caption=cap or None)
        return

    if data.startswith("noop:"):
        return


async def error(update, context):
    print("ERROR:", context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    # One text handler only: the previous version registered caption_handler
    # before text_handler, which intercepted every text update and made the
    # normal menu appear unresponsive.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO, file_handler))
    app.add_error_handler(error)
    app.run_polling()


if __name__ == "__main__":
    main()
