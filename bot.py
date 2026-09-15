#!/usr/bin/env python3
"""
ربات جوین اجباری تلگرام
نیازمندی‌ها: python-telegram-bot==20.7  (پایتون 3.8+)
نصب: pip install python-telegram-bot==20.7

ساختار:
- فقط در یک گروه کار می‌کند (گروهی که مالک تنظیم می‌کند)
- مالک از طریق پیوی ربات: گروه، کانال‌ها و متن پیام‌ها را مدیریت می‌کند
- هر پیام در گروه چک عضویت در همه کانال‌ها انجام می‌شود
- در صورت عدم عضویت: پیام حذف و پیام هشدار با دکمه‌های شیشه‌ای کانال‌ها ارسال می‌شود
- با کلیک روی دکمه «بررسی عضویت» پیام ادیت شده و وضعیت «عضو شد» نمایش داده می‌شود
"""

import asyncio
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Chat,
    User,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات ثابت
# ---------------------------------------------------------------------------

BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"
OWNER_ID = 123456789  # آیدی عددی مالک ربات (اجباری)
DB_PATH = "forcejoin.db"

DEFAULT_WARN_TEXT = (
    "کاربر {mention} \n"
    "برای ارسال پیام باید در کانال های زیر عضو شوید"
)
DEFAULT_JOINED_TEXT = "کاربر {mention} در کانال ها عضو شد ✅"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forcejoin_bot")

# ---------------------------------------------------------------------------
# لایه دیتابیس
# ---------------------------------------------------------------------------


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                title TEXT NOT NULL,
                invite_link TEXT NOT NULL,
                UNIQUE(chat_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS texts (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )


def get_setting(key: str) -> Optional[str]:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_group_id() -> Optional[int]:
    val = get_setting("group_id")
    return int(val) if val else None


def set_group_id(chat_id: int) -> None:
    set_setting("group_id", str(chat_id))


def add_channel(chat_id: str, title: str, invite_link: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO channels(chat_id, title, invite_link) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, invite_link = excluded.invite_link",
            (chat_id, title, invite_link),
        )


def remove_channel(identifier: str) -> bool:
    with closing(db_connect()) as conn, conn:
        cur = conn.execute(
            "DELETE FROM channels WHERE chat_id = ? OR title = ?",
            (identifier, identifier),
        )
        return cur.rowcount > 0


def list_channels() -> list:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT * FROM channels").fetchall()


def get_text(key: str, default: str) -> str:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT value FROM texts WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_text(key: str, value: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO texts(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# ابزارهای کمکی
# ---------------------------------------------------------------------------


def render_mention(user: User) -> str:
    """منشن HTML امن برای کاربر (اجباری در همه‌ی پیام‌ها)."""
    name = user.first_name or "کاربر"
    name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def render_template(template: str, user: User) -> str:
    """
    جایگزینی متغیرها در متن سفارشی مالک.
    متغیرهای پشتیبانی‌شده: {mention} و منشن_کاربر (فرمت فارسی)
    """
    mention = render_mention(user)
    text = template.replace("{mention}", mention)
    text = text.replace("منشن_کاربر", mention)
    # اگر مالک هیچ متغیری نگذاشته باشد، منشن اجباری در ابتدای پیام اضافه می‌شود
    if mention not in text:
        text = f"{mention}\n{text}"
    return text


def build_channels_keyboard(check_text: str = "✅ بررسی عضویت") -> InlineKeyboardMarkup:
    channels = list_channels()
    rows = []
    for ch in channels:
        rows.append([InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["invite_link"])])
    rows.append([InlineKeyboardButton(text=check_text, callback_data="check_membership")])
    return InlineKeyboardMarkup(rows)


async def is_member_of_all_channels(bot, user_id: int) -> bool:
    channels = list_channels()
    if not channels:
        return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
        except (BadRequest, Forbidden) as exc:
            logger.warning("خطا در بررسی عضویت کانال %s: %s", ch["chat_id"], exc)
            return False
        if member.status not in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return False
    return True


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ذخیره موقت پیام‌های در حال ادیت (مالک در حال ارسال متن جدید در پیوی)
PENDING_TEXT_EDIT: dict = {}
PENDING_CHANNEL_ADD: set = set()
PENDING_CHANNEL_REMOVE: set = set()
PENDING_GROUP_SET: set = set()


# ---------------------------------------------------------------------------
# هندلرهای گروه
# ---------------------------------------------------------------------------


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if user is None or user.is_bot:
        return

    group_id = get_group_id()
    if group_id is None or chat.id != group_id:
        return  # ربات فقط در گروه تعیین‌شده توسط مالک فعال است

    if not list_channels():
        return  # کانالی تنظیم نشده، محدودیتی اعمال نمی‌شود

    if await is_member_of_all_channels(context.bot, user.id):
        return

    try:
        await message.delete()
    except (BadRequest, Forbidden) as exc:
        logger.warning("عدم امکان حذف پیام: %s", exc)

    warn_template = get_text("warn_text", DEFAULT_WARN_TEXT)
    text = render_template(warn_template, user)
    keyboard = build_channels_keyboard()

    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def on_check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    ok = await is_member_of_all_channels(context.bot, user.id)
    if not ok:
        await query.answer("هنوز در همه‌ی کانال‌ها عضو نشده‌اید ❌", show_alert=True)
        return

    joined_template = get_text("joined_text", DEFAULT_JOINED_TEXT)
    text = render_template(joined_template, user)

    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as exc:
        # پیام از قبل همین محتوا را دارد یا حذف شده
        logger.info("ادیت پیام ناموفق: %s", exc)

    await query.answer("عضویت شما تایید شد ✅")


# ---------------------------------------------------------------------------
# پنل مدیریت مالک (پیوی ربات)
# ---------------------------------------------------------------------------

MAIN_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🏠 تنظیم گروه", callback_data="menu_set_group")],
        [InlineKeyboardButton("📢 مدیریت کانال‌ها", callback_data="menu_channels")],
        [InlineKeyboardButton("✏️ ویرایش متن‌ها", callback_data="menu_texts")],
        [InlineKeyboardButton("ℹ️ وضعیت فعلی", callback_data="menu_status")],
    ]
)

CHANNELS_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("➕ افزودن کانال", callback_data="ch_add")],
        [InlineKeyboardButton("➖ حذف کانال", callback_data="ch_remove")],
        [InlineKeyboardButton("📋 لیست کانال‌ها", callback_data="ch_list")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main")],
    ]
)

TEXTS_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✏️ ویرایش متن هشدار", callback_data="txt_warn")],
        [InlineKeyboardButton("✏️ ویرایش متن تایید عضویت", callback_data="txt_joined")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main")],
    ]
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != Chat.PRIVATE:
        return
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("این ربات فقط توسط مالک قابل مدیریت است.")
        return
    await update.message.reply_text(
        "به پنل مدیریت ربات جوین اجباری خوش آمدید 👋",
        reply_markup=MAIN_MENU,
    )


async def owner_panel_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مدیریت کلیک‌های دکمه‌های پنل مالک در پیوی."""
    query = update.callback_query
    user_id = query.from_user.id

    if not is_owner(user_id):
        await query.answer("شما مالک ربات نیستید.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "menu_main":
        await query.edit_message_text("پنل مدیریت:", reply_markup=MAIN_MENU)

    elif data == "menu_set_group":
        PENDING_GROUP_SET.add(user_id)
        await query.edit_message_text(
            "ربات را در گروه مورد نظر ادمین کنید، سپس یک پیام از داخل همان گروه "
            "فوروارد کنید و اینجا برای من ارسال کنید تا گروه ثبت شود.\n\n"
            "(یا آیدی عددی گروه را مستقیماً ارسال کنید، مثل: -1001234567890)",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main")]]
            ),
        )

    elif data == "menu_channels":
        await query.edit_message_text("مدیریت کانال‌ها:", reply_markup=CHANNELS_MENU)

    elif data == "ch_add":
        PENDING_CHANNEL_ADD.add(user_id)
        await query.edit_message_text(
            "ربات را ادمین کانال کنید، سپس یک پیام از کانال فوروارد کنید یا "
            "آیدی/یوزرنیم کانال را همراه با لینک دعوت به این شکل ارسال کنید:\n\n"
            "<code>@channel_username | https://t.me/channel_username</code>\n"
            "یا برای کانال خاص (بدون یوزرنیم):\n"
            "<code>-1001234567890 | https://t.me/+AbCdEfGhIj</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_channels")]]
            ),
        )

    elif data == "ch_remove":
        PENDING_CHANNEL_REMOVE.add(user_id)
        await query.edit_message_text(
            "آیدی عددی یا عنوان کانالی که می‌خواهید حذف کنید را ارسال کنید:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_channels")]]
            ),
        )

    elif data == "ch_list":
        channels = list_channels()
        if not channels:
            text = "هیچ کانالی ثبت نشده است."
        else:
            lines = [f"• {ch['title']} — {ch['invite_link']} — <code>{ch['chat_id']}</code>" for ch in channels]
            text = "لیست کانال‌های جوین اجباری:\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_channels")]]
            ),
        )

    elif data == "menu_texts":
        await query.edit_message_text("ویرایش متن‌ها:", reply_markup=TEXTS_MENU)

    elif data == "txt_warn":
        PENDING_TEXT_EDIT[user_id] = "warn_text"
        current = get_text("warn_text", DEFAULT_WARN_TEXT)
        await query.edit_message_text(
            "متن فعلی هشدار عضویت:\n\n"
            f"<code>{current}</code>\n\n"
            "متن جدید را ارسال کنید. متغیرهای قابل استفاده: <code>{mention}</code> یا <code>منشن_کاربر</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_texts")]]
            ),
        )

    elif data == "txt_joined":
        PENDING_TEXT_EDIT[user_id] = "joined_text"
        current = get_text("joined_text", DEFAULT_JOINED_TEXT)
        await query.edit_message_text(
            "متن فعلی تایید عضویت:\n\n"
            f"<code>{current}</code>\n\n"
            "متن جدید را ارسال کنید. متغیرهای قابل استفاده: <code>{mention}</code> یا <code>منشن_کاربر</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_texts")]]
            ),
        )

    elif data == "menu_status":
        group_id = get_group_id()
        channels = list_channels()
        text = (
            f"گروه فعال: <code>{group_id if group_id else 'تنظیم نشده'}</code>\n"
            f"تعداد کانال‌ها: {len(channels)}"
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main")]]
            ),
        )


async def owner_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام‌های متنی مالک در پیوی که برای مراحل pending استفاده می‌شوند."""
    user = update.effective_user
    message = update.effective_message

    if update.effective_chat.type != Chat.PRIVATE or not is_owner(user.id):
        return

    user_id = user.id

    # حالت: ثبت گروه با فوروارد پیام یا آیدی مستقیم
    if user_id in PENDING_GROUP_SET:
        PENDING_GROUP_SET.discard(user_id)
        chat_id = None
        if message.forward_from_chat and message.forward_from_chat.type in (
            Chat.GROUP,
            Chat.SUPERGROUP,
        ):
            chat_id = message.forward_from_chat.id
        elif message.text and re.match(r"^-?\d+$", message.text.strip()):
            chat_id = int(message.text.strip())

        if chat_id is None:
            await message.reply_text("ورودی نامعتبر است. دوباره تلاش کنید.")
            return

        try:
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await message.reply_text("ربات باید در آن گروه ادمین باشد.")
                return
        except (BadRequest, Forbidden) as exc:
            await message.reply_text(f"خطا در دسترسی به گروه: {exc}")
            return

        set_group_id(chat_id)
        await message.reply_text(f"گروه با آیدی {chat_id} ثبت شد ✅", reply_markup=MAIN_MENU)
        return

    # حالت: افزودن کانال
    if user_id in PENDING_CHANNEL_ADD:
        PENDING_CHANNEL_ADD.discard(user_id)

        if message.forward_from_chat and message.forward_from_chat.type == Chat.CHANNEL:
            fwd_chat = message.forward_from_chat
            try:
                chat_full = await context.bot.get_chat(fwd_chat.id)
            except (BadRequest, Forbidden) as exc:
                await message.reply_text(f"خطا: {exc}")
                return
            invite_link = chat_full.invite_link or (
                f"https://t.me/{chat_full.username}" if chat_full.username else None
            )
            if not invite_link:
                await message.reply_text(
                    "لینک دعوت پیدا نشد. ربات باید ادمین با دسترسی ساخت لینک باشد، یا کانال باید یوزرنیم داشته باشد."
                )
                return
            add_channel(str(fwd_chat.id), chat_full.title, invite_link)
            await message.reply_text(f"کانال «{chat_full.title}» با موفقیت اضافه شد ✅", reply_markup=CHANNELS_MENU)
            return

        if message.text and "|" in message.text:
            chat_part, link_part = [p.strip() for p in message.text.split("|", 1)]
            try:
                chat_full = await context.bot.get_chat(chat_part)
            except (BadRequest, Forbidden) as exc:
                await message.reply_text(f"خطا در دسترسی به کانال: {exc}")
                return
            add_channel(str(chat_full.id), chat_full.title, link_part)
            await message.reply_text(f"کانال «{chat_full.title}» با موفقیت اضافه شد ✅", reply_markup=CHANNELS_MENU)
            return

        await message.reply_text("فرمت نامعتبر است. دوباره تلاش کنید.")
        return

    # حالت: حذف کانال
    if user_id in PENDING_CHANNEL_REMOVE:
        PENDING_CHANNEL_REMOVE.discard(user_id)
        identifier = (message.text or "").strip()
        removed = remove_channel(identifier)
        if removed:
            await message.reply_text("کانال حذف شد ✅", reply_markup=CHANNELS_MENU)
        else:
            await message.reply_text("کانالی با این مشخصات پیدا نشد.", reply_markup=CHANNELS_MENU)
        return

    # حالت: ویرایش متن‌ها
    if user_id in PENDING_TEXT_EDIT:
        key = PENDING_TEXT_EDIT.pop(user_id)
        new_text = message.text or ""
        set_text(key, new_text)
        await message.reply_text("متن با موفقیت به‌روزرسانی شد ✅", reply_markup=TEXTS_MENU)
        return


# ---------------------------------------------------------------------------
# اجرای ربات
# ---------------------------------------------------------------------------


def main() -> None:
    init_db()

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))

    application.add_handler(
        CallbackQueryHandler(on_check_membership, pattern="^check_membership$")
    )
    application.add_handler(
        CallbackQueryHandler(owner_panel_router, pattern="^(menu_|ch_|txt_)")
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
            on_group_message,
        )
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.ALL, owner_private_message)
    )

    logger.info("ربات در حال اجراست...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
