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

# Keep existing databases compatible with the new file-type aware delivery.
cols = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
if "media_type" not in cols:
    db.execute("ALTER TABLE files ADD COLUMN media_type TEXT DEFAULT 'document'")
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
        [InlineKeyboardButton("🔗 لینک فایل‌ها", callback_data="admin:links")],
        [InlineKeyboardButton("📁 فایل‌های آپلود شده", callback_data="menu:files")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
    ])


def category_buttons(prefix="cat"):
    cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
    return [[InlineKeyboardButton(name, callback_data=f"{prefix}:{cid}")] for cid, name in cats]


async def send_saved_file(bot, chat_id, telegram_file_id, media_type, caption=None):
    kwargs = {"chat_id": chat_id, "caption": caption or None}
    if media_type == "video":
        return await bot.send_video(video=telegram_file_id, **kwargs)
    if media_type == "audio":
        return await bot.send_audio(audio=telegram_file_id, **kwargs)
    return await bot.send_document(document=telegram_file_id, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid = update.effective_user.id
    admin = uid in ADMINS

    # Public file deep-link: https://t.me/BOT_USERNAME?start=file_<id>
    if context.args and context.args[0].startswith("file_"):
        try:
            file_id = int(context.args[0].split("_", 1)[1])
        except (ValueError, IndexError):
            await update.message.reply_text("❌ لینک فایل نامعتبر است.")
            return

        row = db.execute(
            "SELECT telegram_file_id, media_type, caption FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
        if not row:
            await update.message.reply_text("❌ این فایل دیگر وجود ندارد یا لینک آن نامعتبر است.")
            return

        telegram_file_id, media_type, caption = row
        await send_saved_file(update.get_bot(), update.effective_chat.id, telegram_file_id, media_type, caption)
        return

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

    if state == "caption" and admin:
        context.user_data["caption"] = "" if t == "0" else t
        cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
        if not cats:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ هنوز هیچ دسته‌بندی‌ای ساخته نشده است.\nابتدا یک دسته‌بندی بسازید.",
                reply_markup=admin_kb(),
            )
            return
        buttons = [[InlineKeyboardButton(name, callback_data=f"savecat:{cid}")] for cid, name in cats]
        context.user_data["state"] = "choose_cat"
        await update.message.reply_text(
            "📂 دسته‌بندی فایل را انتخاب کنید:",
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
        await msg.reply_text("❌ فایل مورد نظر را به‌صورت فایل، ویدیو یا صوت ارسال کنید.")
        return

    if msg.document:
        media_type = "document"
    elif msg.video:
        media_type = "video"
    else:
        media_type = "audio"

    file_name = getattr(media, "file_name", None) or f"file_{media.file_unique_id}"
    context.user_data["pending_file"] = (media.file_id, file_name, media_type)
    context.user_data["state"] = "caption"
    await msg.reply_text(
        "📝 کپشن فایل را ارسال کنید.\nاگر کپشن نمی‌خواهید، `0` بفرستید."
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
            "📤 فایل مورد نظر را ارسال کنید.",
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

    if data == "admin:links":
        if not admin:
            return
        rows = db.execute(
            "SELECT f.id, f.name, c.name FROM files f JOIN categories c ON c.id=f.category_id ORDER BY f.id DESC"
        ).fetchall()
        if not rows:
            await q.edit_message_text(
                "🔗 هنوز هیچ فایل دسته‌بندی‌شده‌ای برای ساخت لینک وجود ندارد.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")]
                ]),
            )
            return

        me = await context.bot.get_me()
        buttons = []
        for fid, name, cat_name in rows:
            label = f"🔗 {(name or f'فایل {fid}')[:35]} | {cat_name[:20]}"
            url = f"https://t.me/{me.username}?start=file_{fid}"
            buttons.append([InlineKeyboardButton(label, url=url)])
        buttons.append([InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")])
        await q.edit_message_text(
            "🔗 لینک فایل‌ها\n\nروی هر فایل بزنید تا لینک مستقیم آن باز شود.\nهر کسی لینک را باز کند، مستقیماً فایل را از ربات دریافت می‌کند.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data == "menu:files":
        cats = db.execute("SELECT id,name FROM categories ORDER BY id").fetchall()
        total = db.execute("SELECT COUNT(*) FROM files WHERE category_id IS NOT NULL").fetchone()[0]
        if total == 0 or not cats:
            await q.edit_message_text(
                "📁 هنوز هیچ فایل دسته‌بندی‌شده‌ای آپلود نشده است.",
                reply_markup=main_kb(admin),
            )
            return
        buttons = [[InlineKeyboardButton(name, callback_data=f"cat:{cid}")] for cid, name in cats]
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
        fid, name, media_type = pending
        cap = context.user_data.get("caption", "")
        exists = db.execute("SELECT id FROM categories WHERE id=?", (cid,)).fetchone()
        if not exists:
            await q.edit_message_text("❌ این دسته‌بندی دیگر وجود ندارد.", reply_markup=admin_kb())
            context.user_data.clear()
            return
        db.execute(
            "INSERT INTO files(telegram_file_id,name,caption,category_id,uploaded_by,media_type) VALUES(?,?,?,?,?,?)",
            (fid, name, cap, cid, uid, media_type),
        )
        db.commit()
        context.user_data.clear()
        await q.edit_message_text("✅ فایل با موفقیت آپلود و در دسته‌بندی ثبت شد.", reply_markup=admin_kb())
        return

    if data.startswith("cat:"):
        cid = int(data.split(":", 1)[1])
        rows = db.execute(
            "SELECT id,name,telegram_file_id,caption,media_type FROM files WHERE category_id=? ORDER BY id DESC",
            (cid,),
        ).fetchall()
        if not rows:
            await q.edit_message_text(
                "📂 این دسته‌بندی خالی است.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="menu:files")]]),
            )
            return
        buttons = [[InlineKeyboardButton((name or f"فایل {fid}")[:50], callback_data=f"file:{fid}")] for fid, name, _, _, _ in rows]
        buttons.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="menu:files")])
        context.user_data["browse"] = {
            fid: (telegram_fid, name, caption, media_type)
            for fid, name, telegram_fid, caption, media_type in rows
        }
        await q.edit_message_text("📄 فایل‌ها:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("file:"):
        fid = int(data.split(":", 1)[1])
        item = context.user_data.get("browse", {}).get(fid)
        if not item:
            row = db.execute(
                "SELECT telegram_file_id,name,caption,media_type FROM files WHERE id=?",
                (fid,),
            ).fetchone()
            if not row:
                await q.answer("فایل پیدا نشد.", show_alert=True)
                return
            item = row
        telegram_fid, name, cap, media_type = item
        await send_saved_file(context.bot, q.message.chat_id, telegram_fid, media_type, cap)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO, file_handler))
    app.add_error_handler(error)
    app.run_polling()


if __name__ == "__main__":
    main()
