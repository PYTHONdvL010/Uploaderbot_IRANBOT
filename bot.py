import os, sqlite3, asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

DB_PATH=os.getenv("DB_PATH","/app/data/uploader.db")
BOT_TOKEN=os.getenv("BOT_TOKEN","").strip()
ADMINS={int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db=sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL)")
db.execute("""CREATE TABLE IF NOT EXISTS files(
 id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_file_id TEXT NOT NULL,
 name TEXT, caption TEXT, category_id INTEGER, uploaded_by INTEGER,
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(category_id) REFERENCES categories(id))""")
db.commit()

def main_kb(admin=False):
    rows=[[KeyboardButton("📁 فایل‌های آپلود شده")]]
    if admin: rows.append([KeyboardButton("⚙️ مدیریت")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📂 دسته‌بندی‌ها"),KeyboardButton("📤 آپلود")],
        [KeyboardButton("📁 فایل‌های آپلود شده")],
        [KeyboardButton("🔙 بازگشت")]
    ], resize_keyboard=True)

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    admin=update.effective_user.id in ADMINS
    await update.message.reply_text("سلام 👋\nبه ربات آپلودر خوش آمدید.", reply_markup=main_kb(admin))

async def text_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    t=update.message.text
    uid=update.effective_user.id
    admin=uid in ADMINS
    if t=="⚙️ مدیریت" and admin:
        context.user_data.clear(); context.user_data["state"]="admin"
        await update.message.reply_text("⚙️ مدیریت",reply_markup=admin_kb()); return
    if t=="🔙 بازگشت":
        context.user_data.clear()
        await update.message.reply_text("منوی اصلی",reply_markup=main_kb(admin)); return
    if t=="📂 دسته‌بندی‌ها" and admin:
        context.user_data["state"]="cat_menu"
        cur=db.execute("SELECT id,name FROM categories ORDER BY id DESC")
        cats=cur.fetchall()
        if not cats: await update.message.reply_text("دسته‌بندی خالی است.")
        else:
            await update.message.reply_text("دسته‌بندی‌های ساخته‌شده:\n" + "\n".join(f"• {n}" for _,n in cats))
        await update.message.reply_text("برای ساخت دسته‌بندی، نام آن را ارسال کنید."); context.user_data["state"]="add_cat"; return
    if t=="📤 آپلود" and admin:
        context.user_data.clear(); context.user_data["state"]="upload_wait"
        await update.message.reply_text("📤 فایل را ارسال کنید.\n\nمحدودیت حجمی توسط محدودیت واقعی Telegram Bot API تعیین می‌شود و ربات محدودیت اضافه‌ای اعمال نمی‌کند."); return
    if t=="📁 فایل‌های آپلود شده":
        cur=db.execute("""SELECT f.id,f.name,c.name FROM files f LEFT JOIN categories c ON c.id=f.category_id ORDER BY f.id DESC""")
        rows=cur.fetchall()
        if not rows:
            await update.message.reply_text("هیچ محصول و دسته‌بندی وجود ندارد."); return
        cats=db.execute("SELECT id,name FROM categories ORDER BY id").fetchall()
        buttons=[[InlineKeyboardButton(n,callback_data=f"cat:{i}")] for i,n in cats]
        buttons.append([InlineKeyboardButton("بدون دسته‌بندی",callback_data="cat:0")])
        await update.message.reply_text("دسته‌بندی را انتخاب کنید:",reply_markup=InlineKeyboardMarkup(buttons)); return
    st=context.user_data.get("state")
    if st=="add_cat" and admin:
        name=t.strip()
        if not name: return
        try:
            db.execute("INSERT INTO categories(name) VALUES(?)",(name,)); db.commit()
            await update.message.reply_text(f"دسته‌بندی «{name}» با موفقیت اضافه شد.",reply_markup=admin_kb())
        except sqlite3.IntegrityError:
            await update.message.reply_text("این دسته‌بندی قبلاً وجود دارد.")
        context.user_data["state"]="admin"

async def file_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if uid not in ADMINS or context.user_data.get("state")!="upload_wait": return
    msg=update.message
    media=msg.document or msg.video or msg.audio
    if not media:
        await msg.reply_text("لطفاً فایل را به‌صورت Document/Video/Audio ارسال کنید."); return
    context.user_data["pending_file"]=(media.file_id, media.file_name if hasattr(media,"file_name") else None)
    context.user_data["state"]="caption"
    await msg.reply_text("کپشن می‌خواهی؟\nاگر بله، متن کپشن را بفرست. اگر نمی‌خواهی، `0` بفرست.")

async def caption_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS or context.user_data.get("state")!="caption": return
    text=update.message.text
    context.user_data["caption"]="" if text=="0" else text
    cats=db.execute("SELECT id,name FROM categories ORDER BY id").fetchall()
    buttons=[[InlineKeyboardButton(n,callback_data=f"savecat:{i}")] for i,n in cats]
    buttons.append([InlineKeyboardButton("بدون دسته‌بندی",callback_data="savecat:0")])
    context.user_data["state"]="choose_cat"
    await update.message.reply_text("دسته‌بندی را انتخاب کن:",reply_markup=InlineKeyboardMarkup(buttons))

async def callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    uid=q.from_user.id
    if uid not in ADMINS and not q.data.startswith("cat:"): return
    if q.data.startswith("savecat:"):
        cid=int(q.data.split(":")[1]); fid,name=context.user_data["pending_file"]; cap=context.user_data.get("caption","")
        db.execute("INSERT INTO files(telegram_file_id,name,caption,category_id,uploaded_by) VALUES(?,?,?,?,?)",(fid,name,cap,cid or None,uid)); db.commit()
        context.user_data.clear()
        await q.edit_message_text("✅ فایل با موفقیت آپلود و ثبت شد."); return
    if q.data.startswith("cat:"):
        cid=int(q.data.split(":")[1])
        rows=db.execute("SELECT id,name,telegram_file_id,caption FROM files WHERE category_id IS ? ORDER BY id DESC",(cid or None,)).fetchall()
        if not rows: await q.edit_message_text("این دسته‌بندی خالی است."); return
        buttons=[[InlineKeyboardButton((n or f"فایل {i}")[:50],callback_data=f"file:{i}")] for i,n,_,_ in rows]
        context.user_data["browse"]={i:(fid,n,cap) for i,n,fid,cap in rows}
        await q.edit_message_text("فایل‌ها:",reply_markup=InlineKeyboardMarkup(buttons)); return
    if q.data.startswith("file:"):
        i=int(q.data.split(":")[1]); item=context.user_data.get("browse",{}).get(i)
        if not item: await q.answer("فایل پیدا نشد.",show_alert=True); return
        fid,n,cap=item
        await context.bot.send_document(q.message.chat_id,fid,caption=cap or None)
        return

async def error(update,context): print("ERROR:",context.error)

async def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is not set")
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL|filters.VIDEO|filters.AUDIO,file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,caption_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_error_handler(error)
    await app.run_polling()

if __name__=="__main__": asyncio.run(main())
