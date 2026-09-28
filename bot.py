import os
import re
import sqlite3
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

VERSION = "1.0.0"
DB_PATH = os.getenv("DB_PATH", "/app/data/uploader.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA foreign_keys=ON")

# Existing database compatibility is intentionally kept.
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
db.execute("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    downloads INTEGER DEFAULT 0)""")
db.execute("""CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT)""")
db.execute("""CREATE TABLE IF NOT EXISTS mandatory_channel(
    id INTEGER PRIMARY KEY CHECK(id=1),
    chat_id TEXT NOT NULL,
    title TEXT,
    invite_link TEXT,
    input_value TEXT NOT NULL)""")

cols = {row[1] for row in db.execute("PRAGMA table_info(files)").fetchall()}
if "media_type" not in cols:
    db.execute("ALTER TABLE files ADD COLUMN media_type TEXT DEFAULT 'document'")

default_welcome = (
    "👋 {username} عزیز، به Uploaderbot زیرمجموعه IRANBOT خوش آمدی.\n\n"
    "📁 از این ربات می‌توانی فایل‌های منتشرشده را دریافت کنی.\n"
    "🔗 اگر لینک اختصاصی یک فایل را داشته باشی، همان فایل برایت ارسال می‌شود.\n\n"
    "🆔 شناسه شما: {ids}"
)
if db.execute("SELECT 1 FROM settings WHERE key='welcome'").fetchone() is None:
    db.execute("INSERT INTO settings(key,value) VALUES('welcome',?)", (default_welcome,))
if db.execute("SELECT 1 FROM settings WHERE key='global_caption'").fetchone() is None:
    db.execute("INSERT INTO settings(key,value) VALUES('global_caption','')")
db.commit()


def get_setting(key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def is_admin(uid):
    return uid in ADMINS


def register_user(user):
    if not user:
        return
    username = user.username or ""
    first_name = user.first_name or ""
    db.execute(
        """INSERT INTO users(id,username,first_name) VALUES(?,?,?)
           ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
           last_seen=CURRENT_TIMESTAMP""",
        (user.id, username, first_name),
    )
    db.commit()


def welcome_text(user):
    username = f"@{user.username}" if user.username else (user.first_name or "کاربر")
    text = get_setting("welcome", default_welcome)
    return text.replace("{username}", username).replace("{ids}", str(user.id))


def main_kb(admin=False):
    buttons = [[InlineKeyboardButton("📁 فایل‌های آپلود شده", callback_data="menu:files")]]
    if admin:
        buttons.append([InlineKeyboardButton("⚙️ مدیریت", callback_data="menu:admin")])
    return InlineKeyboardMarkup(buttons)


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 دسته‌بندی‌ها", callback_data="admin:categories"),
         InlineKeyboardButton("📁 فایل ها", callback_data="admin:products")],
        [InlineKeyboardButton("📤 آپلود", callback_data="admin:upload"),
         InlineKeyboardButton("🔗 لینک فایل‌ها", callback_data="admin:links")],
        [InlineKeyboardButton("👥 کاربران", callback_data="admin:users"),
         InlineKeyboardButton("📢 عضویت اجباری", callback_data="admin:membership")],
        [InlineKeyboardButton("💬 پیام خوش‌آمد", callback_data="admin:welcome"),
         InlineKeyboardButton("📝 کپشن همگانی", callback_data="admin:global_caption")],
        [InlineKeyboardButton("📁 فایل‌های آپلود شده", callback_data="menu:files")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")],
    ])


def cancel_kb(target="menu:admin"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data=target)]])


def category_buttons(prefix="cat"):
    cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
    return [[InlineKeyboardButton(name, callback_data=f"{prefix}:{cid}")] for cid, name in cats]


def membership_record():
    return db.execute("SELECT chat_id,title,invite_link,input_value FROM mandatory_channel WHERE id=1").fetchone()


def parse_channel_input(raw):
    value = raw.strip()
    # Numeric Telegram channel/supergroup ID.
    if re.fullmatch(r"-100\d+|-\d+", value):
        return value
    # @username or public t.me links.
    m = re.search(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z0-9_]{5,})/?$", value)
    if m:
        return "@" + m.group(1)
    if value.startswith("@"):
        return value
    return None


async def channel_check(bot, user_id):
    rec = membership_record()
    if not rec:
        return True
    chat_id, _, _, _ = rec
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in {"member", "administrator", "creator"}:
            return True
        if member.status == "restricted" and getattr(member, "is_member", False):
            return True
    except Exception as exc:
        print("MEMBERSHIP CHECK ERROR:", exc)
    return False


async def membership_prompt(update, context):
    rec = membership_record()
    if not rec:
        return True
    _, title, invite_link, input_value = rec
    link = invite_link or (input_value if input_value.startswith("http") else None)
    rows = []
    if link:
        rows.append([InlineKeyboardButton(f"📢 عضویت در {title or 'کانال'}", url=link)])
    elif input_value.startswith("@"):
        rows.append([InlineKeyboardButton("📢 ورود به کانال", url=f"https://t.me/{input_value[1:]}")])
    rows.append([InlineKeyboardButton("🔄 بررسی عضویت", callback_data="membership:check")])
    markup = InlineKeyboardMarkup(rows)
    text = f"🔒 برای استفاده از امکانات ربات ابتدا در {title or 'کانال تعیین‌شده'} عضو شوید.\n\nبعد از عضویت روی «🔄 بررسی عضویت» بزنید."
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup)
    return False


async def require_membership(update, context):
    uid = update.effective_user.id
    if is_admin(uid):
        return True
    if await channel_check(context.bot, uid):
        return True
    await membership_prompt(update, context)
    return False


async def send_saved_file(bot, chat_id, telegram_file_id, media_type, caption=None):
    kwargs = {"chat_id": chat_id}
    if caption:
        kwargs["caption"] = caption
    if media_type == "video":
        return await bot.send_video(video=telegram_file_id, **kwargs)
    if media_type == "audio":
        return await bot.send_audio(audio=telegram_file_id, **kwargs)
    return await bot.send_document(document=telegram_file_id, **kwargs)


async def increment_download(uid):
    db.execute("UPDATE users SET downloads=downloads+1,last_seen=CURRENT_TIMESTAMP WHERE id=?", (uid,))
    db.commit()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    context.user_data.clear()
    uid = update.effective_user.id
    admin = is_admin(uid)

    # Public file deep-link.
    if context.args and context.args[0].startswith("file_"):
        if not await require_membership(update, context):
            return
        try:
            file_id = int(context.args[0].split("_", 1)[1])
        except (ValueError, IndexError):
            await update.message.reply_text("❌ لینک فایل نامعتبر است.")
            return
        row = db.execute("SELECT telegram_file_id,media_type,caption FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            await update.message.reply_text("❌ این فایل دیگر وجود ندارد یا لینک آن نامعتبر است.")
            return
        await send_saved_file(update.get_bot(), update.effective_chat.id, row[0], row[1], row[2])
        await increment_download(uid)
        return

    if not await require_membership(update, context):
        return
    await update.message.reply_text(welcome_text(update.effective_user), reply_markup=main_kb(admin))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    register_user(update.effective_user)
    t = update.message.text.strip()
    uid = update.effective_user.id
    admin = is_admin(uid)
    state = context.user_data.get("state")

    if not admin:
        if not await require_membership(update, context):
            return
        return

    if state == "add_cat":
        if not t:
            await update.message.reply_text("لطفاً یک نام برای دسته‌بندی بفرست.", reply_markup=cancel_kb())
            return
        try:
            db.execute("INSERT INTO categories(name) VALUES(?)", (t,))
            db.commit()
            context.user_data.clear()
            await update.message.reply_text(f"✅ دسته‌بندی «{t}» ساخته شد.", reply_markup=admin_kb())
        except sqlite3.IntegrityError:
            await update.message.reply_text("⚠️ این دسته‌بندی قبلاً وجود دارد.", reply_markup=cancel_kb())
        return

    if state == "caption":
        if t == "0":
            cap = ""
        elif t == "1":
            cap = get_setting("global_caption", "")
            if not cap:
                await update.message.reply_text(
                    "❌ کپشن همگانی تنظیم نشده است.\nیک کپشن بنویس یا `0` بفرست تا بدون کپشن ثبت شود.",
                    reply_markup=cancel_kb(),
                )
                return
        else:
            cap = t
        context.user_data["caption"] = cap
        cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
        buttons = [[InlineKeyboardButton(name, callback_data=f"savecat:{cid}")] for cid, name in cats]
        buttons.append([InlineKeyboardButton("📦 بدون دسته‌بندی", callback_data="savecat:0")])
        buttons.append([InlineKeyboardButton("❌ لغو", callback_data="menu:admin")])
        context.user_data["state"] = "choose_cat"
        await update.message.reply_text("📂 دسته‌بندی فایل را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if state == "membership_setup":
        parsed = parse_channel_input(t)
        if not parsed:
            await update.message.reply_text("❌ آیدی عددی، @username یا لینک عمومی t.me کانال را بفرست.", reply_markup=cancel_kb())
            return
        try:
            chat = await context.bot.get_chat(parsed)
            me = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(chat.id, me.id)
            if bot_member.status not in {"administrator", "creator"}:
                await update.message.reply_text(
                    "❌ من هنوز ادمین این کانال نیستم.\nابتدا ربات را با دسترسی ادمین به کانال اضافه کن، بعد دوباره آیدی یا لینک کانال را بفرست.",
                    reply_markup=cancel_kb(),
                )
                return
            invite = None
            try:
                invite = await context.bot.create_chat_invite_link(chat.id)
                invite = invite.invite_link
            except Exception:
                # Public channels can still use their public URL.
                if chat.username:
                    invite = f"https://t.me/{chat.username}"
            db.execute(
                "INSERT INTO mandatory_channel(id,chat_id,title,invite_link,input_value) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET chat_id=excluded.chat_id,title=excluded.title,invite_link=excluded.invite_link,input_value=excluded.input_value",
                (str(chat.id), chat.title or chat.username or "کانال", invite, t),
            )
            db.commit()
            context.user_data.clear()
            await update.message.reply_text(f"✅ عضویت اجباری برای «{chat.title or chat.username or chat.id}» فعال شد.", reply_markup=admin_kb())
        except Exception as exc:
            print("MEMBERSHIP SETUP ERROR:", exc)
            await update.message.reply_text("❌ کانال پیدا نشد یا دسترسی لازم وجود ندارد. آیدی/لینک را بررسی کن.", reply_markup=cancel_kb())
        return

    if state == "welcome_edit":
        if t == "0":
            context.user_data.clear()
            await update.message.reply_text("❌ لغو شد.", reply_markup=admin_kb())
            return
        set_setting("welcome", t)
        context.user_data.clear()
        await update.message.reply_text("✅ پیام خوش‌آمد ذخیره شد.", reply_markup=admin_kb())
        return

    if state == "global_caption_edit":
        if t == "0":
            set_setting("global_caption", "")
            context.user_data.clear()
            await update.message.reply_text("✅ کپشن همگانی حذف شد.", reply_markup=admin_kb())
            return
        set_setting("global_caption", t)
        context.user_data.clear()
        await update.message.reply_text("✅ کپشن همگانی ذخیره شد.", reply_markup=admin_kb())
        return


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(update.effective_user)
    if not is_admin(uid) or context.user_data.get("state") != "upload_wait":
        return
    msg = update.message
    media = msg.document or msg.video or msg.audio
    if not media:
        await msg.reply_text("❌ فایل موردنظر را ارسال کن.", reply_markup=cancel_kb())
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
        "📝 کپشن فایل را ارسال کنید.\n\n"
        "اگر `1` بفرستی، کپشن همگانی اعمال می‌شود.\n"
        "اگر `0` بفرستی، فایل بدون کپشن ثبت می‌شود.\n"
        "یا متن دلخواهت را به‌عنوان کپشن بفرست.",
        reply_markup=cancel_kb(),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""
    admin = is_admin(uid)
    register_user(q.from_user)

    # Every ordinary user's button/action is checked against mandatory membership.
    if not admin and data != "membership:check":
        if not await require_membership(update, context):
            return

    if data == "membership:check":
        if await channel_check(context.bot, uid):
            await q.edit_message_text("✅ عضویت شما تأیید شد.", reply_markup=main_kb(admin))
        else:
            await q.answer("❌ هنوز عضو کانال نیستی.", show_alert=True)
        return

    if data == "menu:home":
        context.user_data.clear()
        await q.edit_message_text(welcome_text(q.from_user), reply_markup=main_kb(admin))
        return

    if data == "menu:admin":
        if not admin:
            return
        context.user_data.clear()
        await q.edit_message_text(f"⚙️ مدیریت Uploaderbot\nنسخه {VERSION}", reply_markup=admin_kb())
        return

    if data == "admin:upload":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "upload_wait"
        await q.edit_message_text("📤 فایل مورد نظر را ارسال کنید.", reply_markup=cancel_kb())
        return

    if data == "admin:categories":
        if not admin:
            return
        cats = db.execute("SELECT id,name FROM categories ORDER BY id DESC").fetchall()
        buttons = [[InlineKeyboardButton(f"📂 {name}", callback_data=f"admincat:{cid}")] for cid, name in cats]
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
        await q.edit_message_text("➕ نام دسته‌بندی جدید را ارسال کنید.", reply_markup=cancel_kb())
        return

    if data.startswith("admincat:"):
        if not admin:
            return
        cid = int(data.split(":", 1)[1])
        row = db.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
        if not row:
            await q.answer("دسته‌بندی پیدا نشد.", show_alert=True)
            return
        count = db.execute("SELECT COUNT(*) FROM files WHERE category_id=?", (cid,)).fetchone()[0]
        await q.edit_message_text(
            f"📂 دسته‌بندی: {row[0]}\n📦 تعداد فایل‌ها: {count}\n\nبا حذف این دسته‌بندی، فایل‌های داخل آن حذف نمی‌شوند و به «بدون دسته‌بندی» منتقل می‌شوند.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 حذف دسته‌بندی", callback_data=f"delcatask:{cid}")],
                [InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="admin:categories")],
            ]),
        )
        return

    if data.startswith("delcatask:"):
        if not admin:
            return
        cid = int(data.split(":", 1)[1])
        row = db.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
        if not row:
            await q.answer("پیدا نشد.", show_alert=True)
            return
        await q.edit_message_text(
            f"⚠️ مطمئنی دسته‌بندی «{row[0]}» لغو/حذف شود؟\n\nفایل‌های داخل آن به «بدون دسته‌بندی» منتقل می‌شوند.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ مطمئنم، حذف کن", callback_data=f"delcat:{cid}"),
                 InlineKeyboardButton("❌ لغو", callback_data=f"admincat:{cid}")],
            ]),
        )
        return

    if data.startswith("delcat:"):
        if not admin:
            return
        cid = int(data.split(":", 1)[1])
        db.execute("UPDATE files SET category_id=NULL WHERE category_id=?", (cid,))
        db.execute("DELETE FROM categories WHERE id=?", (cid,))
        db.commit()
        await q.edit_message_text("✅ دسته‌بندی حذف شد و فایل‌های آن به «بدون دسته‌بندی» منتقل شدند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="admin:categories")]]))
        return

    if data == "admin:products":
        if not admin:
            return
        rows = db.execute("SELECT f.id,f.name,c.name FROM files f LEFT JOIN categories c ON c.id=f.category_id ORDER BY f.id DESC").fetchall()
        if not rows:
            await q.edit_message_text("📦 هنوز محصول/فایلی ثبت نشده است.", reply_markup=cancel_kb())
            return
        buttons = [[InlineKeyboardButton(f"🗑 {((name or f'فایل {fid}')[:38])}", callback_data=f"delprodask:{fid}")] for fid, name, _ in rows]
        buttons.append([InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")])
        await q.edit_message_text("📦 محصولات\n\nبرای حذف هر فایل روی آن بزن:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("delprodask:"):
        if not admin:
            return
        fid = int(data.split(":", 1)[1])
        row = db.execute("SELECT name FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            await q.answer("فایل پیدا نشد.", show_alert=True)
            return
        await q.edit_message_text(
            f"⚠️ مطمئنی فایل «{row[0] or fid}» حذف شود؟\n\nبعد از حذف، لینک اختصاصی آن هم دیگر کار نمی‌کند.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ مطمئنم، حذف کن", callback_data=f"delprod:{fid}"), InlineKeyboardButton("❌ لغو", callback_data="admin:products")]
            ]),
        )
        return

    if data.startswith("delprod:"):
        if not admin:
            return
        fid = int(data.split(":", 1)[1])
        db.execute("DELETE FROM files WHERE id=?", (fid,))
        db.commit()
        await q.edit_message_text("✅ فایل حذف شد و لینک آن غیرفعال شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 محصولات", callback_data="admin:products")]]))
        return

    if data == "admin:links":
        if not admin:
            return
        rows = db.execute("SELECT f.id,f.name,c.name FROM files f LEFT JOIN categories c ON c.id=f.category_id ORDER BY f.id DESC").fetchall()
        if not rows:
            await q.edit_message_text("🔗 هنوز هیچ فایلی برای ساخت لینک وجود ندارد.", reply_markup=cancel_kb())
            return
        me = await context.bot.get_me()
        buttons = []
        lines = ["🔗 لینک فایل‌ها", "", "برای هر فایل لینک اختصاصی ساخته شده است:", ""]
        for fid, name, cat_name in rows:
            display = (name or f"فایل {fid}").strip()
            url = f"https://t.me/{me.username}?start=file_{fid}"
            lines.append(f"📦 {display} | {cat_name or 'بدون دسته‌بندی'}")
            lines.append(url)
            lines.append("")
            buttons.append([InlineKeyboardButton(f"🔗 {display[:40]}", url=url)])
        buttons.append([InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")])
        await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
        return

    if data == "admin:users":
        if not admin:
            return
        rows = db.execute("SELECT id,username,first_name,downloads FROM users ORDER BY last_seen DESC").fetchall()
        if not rows:
            text = "👥 هنوز کاربری ربات را شروع نکرده است."
        else:
            lines = ["👥 کاربران", ""]
            for user_id, username, first_name, downloads in rows:
                name = f"@{username}" if username else (first_name or "بدون نام")
                lines.append(f"👤 {name}\n🆔 {user_id}\n📥 دانلودها: {downloads}\n")
            text = "\n".join(lines)
        buttons = [[InlineKeyboardButton("🔄 بروزرسانی", callback_data="admin:users")], [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")]]
        await q.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "admin:membership":
        if not admin:
            return
        rec = membership_record()
        if rec:
            chat_id, title, invite, inp = rec
            text = f"📢 عضویت اجباری فعال است.\n\nکانال: {title}\nID: {chat_id}\nورودی ذخیره‌شده: {inp}"
            buttons = [
                [InlineKeyboardButton("✏️ تغییر کانال", callback_data="membership:setup")],
                [InlineKeyboardButton("🗑 حذف عضویت اجباری", callback_data="membership:deleteask")],
                [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")],
            ]
        else:
            text = "📢 عضویت اجباری\n\nهنوز کانالی تنظیم نشده است."
            buttons = [[InlineKeyboardButton("➕ تنظیم کانال", callback_data="membership:setup")], [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")]]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "membership:setup":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "membership_setup"
        await q.edit_message_text(
            "📢 من را با تمام دسترسی‌های لازم ادمین کانالت کن.\n\n"
            "بعد ID عددی کانال، @username یا لینک عمومی کانال را بفرست.",
            reply_markup=cancel_kb("admin:membership"),
        )
        return

    if data == "membership:deleteask":
        if not admin:
            return
        await q.edit_message_text("⚠️ مطمئنی عضویت اجباری حذف شود؟", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ مطمئنم، حذف کن", callback_data="membership:delete"), InlineKeyboardButton("❌ لغو", callback_data="admin:membership")]
        ]))
        return

    if data == "membership:delete":
        if not admin:
            return
        db.execute("DELETE FROM mandatory_channel WHERE id=1")
        db.commit()
        await q.edit_message_text("✅ عضویت اجباری حذف شد.", reply_markup=admin_kb())
        return

    if data == "admin:welcome":
        if not admin:
            return
        current = get_setting("welcome", default_welcome)
        await q.edit_message_text(
            "💬 پیام خوش‌آمد فعلی:\n\n" + current + "\n\n"
            "متغیرهای قابل استفاده: {username} و {ids}\n\n"
            "برای تغییر، متن جدید را بفرست. برای لغو 0 بفرست.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تغییر پیام", callback_data="welcome:edit")],
                [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")],
            ]),
        )
        return

    if data == "welcome:edit":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "welcome_edit"
        await q.edit_message_text("💬 متن پیام خوش‌آمد جدید را بنویس.\n\nمتغیرها: {username} و {ids}", reply_markup=cancel_kb())
        return

    if data == "admin:global_caption":
        if not admin:
            return
        cap = get_setting("global_caption", "")
        await q.edit_message_text(
            "📝 کپشن همگانی\n\n"
            + (cap if cap else "❌ تنظیم نشده")
            + "\n\nکپشن جدید را بنویس. اگر `0` بفرستی، کپشن همگانی حذف می‌شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تنظیم/تغییر کپشن", callback_data="global_caption:edit")],
                [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")],
            ]),
        )
        return

    if data == "global_caption:edit":
        if not admin:
            return
        context.user_data.clear()
        context.user_data["state"] = "global_caption_edit"
        await q.edit_message_text("📝 متن کپشن همگانی آماده‌ات را بنویس.\nبرای حذف کپشن همگانی `0` بفرست.", reply_markup=cancel_kb())
        return

    if data == "savecat:0" or data.startswith("savecat:"):
        if not admin or context.user_data.get("state") != "choose_cat":
            return
        cid = int(data.split(":", 1)[1])
        pending = context.user_data.get("pending_file")
        if not pending:
            await q.edit_message_text("❌ فایل در انتظار پیدا نشد.", reply_markup=admin_kb())
            context.user_data.clear()
            return
        telegram_fid, name, media_type = pending
        cap = context.user_data.get("caption", "")
        if cid != 0 and not db.execute("SELECT id FROM categories WHERE id=?", (cid,)).fetchone():
            await q.edit_message_text("❌ این دسته‌بندی دیگر وجود ندارد.", reply_markup=admin_kb())
            context.user_data.clear()
            return
        category_id = None if cid == 0 else cid
        db.execute("INSERT INTO files(telegram_file_id,name,caption,category_id,uploaded_by,media_type) VALUES(?,?,?,?,?,?)", (telegram_fid, name, cap, category_id, uid, media_type))
        db.commit()
        fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        context.user_data.clear()
        me = await context.bot.get_me()
        url = f"https://t.me/{me.username}?start=file_{fid}"
        await q.edit_message_text(
            f"✅ فایل با موفقیت ثبت شد.\n\n🔗 لینک اختصاصی:\n{url}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 باز کردن لینک", url=url)], [InlineKeyboardButton("🔙 مدیریت", callback_data="menu:admin")]]),
            disable_web_page_preview=True,
        )
        return

    if data == "menu:files":
        if not await require_membership(update, context):
            return
        cats = db.execute("SELECT id,name FROM categories ORDER BY id").fetchall()
        uncategorized = db.execute("SELECT id,name FROM files WHERE category_id IS NULL ORDER BY id DESC").fetchall()
        total = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if total == 0:
            await q.edit_message_text("📁 هنوز هیچ فایلی آپلود نشده است.", reply_markup=main_kb(admin))
            return
        buttons = [[InlineKeyboardButton(f"📂 {name}", callback_data=f"cat:{cid}")] for cid, name in cats]
        buttons.extend([[InlineKeyboardButton(f"📄 {name or f'فایل {fid}'}", callback_data=f"file:{fid}")] for fid, name in uncategorized])
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu:home")])
        await q.edit_message_text("📂 فایل‌ها:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("cat:"):
        if not await require_membership(update, context):
            return
        cid = int(data.split(":", 1)[1])
        rows = db.execute("SELECT id,name,telegram_file_id,caption,media_type FROM files WHERE category_id=? ORDER BY id DESC", (cid,)).fetchall()
        if not rows:
            await q.edit_message_text("📂 این دسته‌بندی خالی است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="menu:files")]]))
            return
        buttons = [[InlineKeyboardButton((name or f"فایل {fid}")[:50], callback_data=f"file:{fid}")] for fid, name, _, _, _ in rows]
        buttons.append([InlineKeyboardButton("🔙 دسته‌بندی‌ها", callback_data="menu:files")])
        context.user_data["browse"] = {fid: (tfid, name, cap, mt) for fid, name, tfid, cap, mt in rows}
        await q.edit_message_text("📄 فایل‌ها:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("file:"):
        if not await require_membership(update, context):
            return
        fid = int(data.split(":", 1)[1])
        row = db.execute("SELECT telegram_file_id,name,caption,media_type FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            await q.answer("فایل پیدا نشد.", show_alert=True)
            return
        await send_saved_file(context.bot, q.message.chat_id, row[0], row[3], row[2])
        await increment_download(uid)
        return


async def error(update, context):
    print("ERROR:", context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO, file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error)
    app.run_polling()


if __name__ == "__main__":
    main()
