#!/usr/bin/env python3
"""
ربات جوین اجباری تلگرام
نیازمندی‌ها: python-telegram-bot==22.8  (پایتون 3.10+)
دیتابیس: Supabase (Postgres)
نصب: pip install -r requirements.txt

ساختار:
- فقط در یک گروه کار می‌کند (گروهی که ادمین تنظیم می‌کند)
- ادمین‌ها از طریق پیوی ربات: گروه، آیتم‌های عضویت (کانال/گروه/ربات) و متن پیام‌ها را مدیریت می‌کنند
- هر پیام در گروه بلافاصله و به‌صورت موازی برای همه‌ی آیتم‌ها چک عضویت می‌شود (سریع، بدون از دست رفتن پیام)
- در صورت عدم عضویت: پیام حذف و پیام هشدار با دکمه‌های شیشه‌ای ارسال می‌شود
- زیر متنِ سفارشیِ ادمین، همیشه به‌صورت خودکار لیست موارد لازم (کانال/گروه/ربات) چاپ می‌شود
- با کلیک روی دکمه «بررسی عضویت» وضعیت مجدد چک و پیام ادیت می‌شود

جدول‌های مورد نیاز در Supabase (SQL):

    create table if not exists settings (
        key text primary key,
        value text
    );

    create table if not exists channels (
        id bigint generated always as identity primary key,
        kind text not null default 'channel',   -- 'channel' | 'group' | 'bot'
        chat_id text not null unique,           -- آیدی عددی کانال/گروه یا یوزرنیم ربات (بدون @)
        title text not null,
        invite_link text not null
    );

    create table if not exists texts (
        key text primary key,
        value text
    );

    -- برای تایید عضویت در ربات‌ها. هر رباتِ دیگری که به‌عنوان آیتم اجباری
    -- اضافه می‌شود، باید در هندلر /start خودش یک ردیف در این جدول ثبت کند
    -- تا این ربات بتواند تشخیص دهد کاربر آن را استارت کرده یا نه.
    create table if not exists bot_starts (
        bot_username text not null,
        user_id bigint not null,
        started_at timestamptz not null default now(),
        primary key (bot_username, user_id)
    );

نکته‌ی مهم درباره‌ی آیتم از نوع «ربات»:
دکمه‌ی ربات یک لینک مستقیم به t.me/BOT?start=... است. بررسی واقعی ممکن نیست؛
وقتی کاربر دکمه‌ی «بررسی عضویت» را می‌زند، ورود او به همه‌ی ربات‌ها تایید و در
جدول bot_starts ثبت می‌شود. نیازی به تغییر در ربات مقصد یا دیتابیس مشترک نیست.
"""

import asyncio
import html
import logging
import os
import re
import secrets
from typing import Optional

from aiohttp import web

from supabase import create_client, Client, ClientOptions
from postgrest import SyncPostgrestClient  # noqa: F401  (فقط برای وضوح وابستگی)

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Chat,
    User,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, TelegramError, TimedOut, NetworkError
from telegram.request import HTTPXRequest
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# آیدی عددی ادمین‌های ربات (اجباری، چند ادمین پشتیبانی می‌شود)
ADMIN_IDS = {601668306, 8977934490}

DEFAULT_WARN_TEXT = "<b>کاربر {mention}\nبرای ارسال پیام باید در موارد زیر عضو شوید:</b>"
DEFAULT_JOINED_TEXT = "<b>کاربر {mention} در همه‌ی موارد عضو شد ✅</b>"

KIND_LABEL = {"channel": "کانال", "group": "گروه", "bot": "ربات"}
KIND_ICON = {"channel": "📢", "group": "👥", "bot": "🤖"}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("forcejoin_bot")

# ---------------------------------------------------------------------------
# لایه دیتابیس (Supabase) — با هندل خطا تا یک خطای موقت دیتابیس ربات را نخواباند
# ---------------------------------------------------------------------------

# مهم: تمام تماس‌های supabase-py «سینک/بلاک‌کننده» هستند (روی httpx sync ساخته شده‌اند).
# اگر این توابع مستقیم داخل هندلرهای async صدا زده شوند، هر تماس دیتابیس کل
# event loop تک‌رشته‌ای aiohttp/PTB را برای مدت تایم‌اوت (یا حتی بی‌نهایت،
# اگر شبکه هنگ کند) فریز می‌کند. در حالت وبهوک یعنی هیچ آپدیت دیگری از تلگرام
# پردازش نمی‌شود؛ از بیرون دقیقاً همین‌طور دیده می‌شود: «Render روشن است ولی
# ربات جواب نمی‌دهد». برای همین:
#   ۱) روی کلاینت supabase یک تایم‌اوت صریح و معقول ست شده (به‌جای رفتار پیش‌فرض نامشخص).
#   ۲) هر متد این لایه از asyncio.to_thread استفاده می‌کند تا تماس بلاک‌کننده
#      در ترد جدا اجرا شود و event loop اصلی هیچ‌وقت قفل نشود.

DB_TIMEOUT_SECONDS = 10

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY,
    options=ClientOptions(postgrest_client_timeout=DB_TIMEOUT_SECONDS),
)


def _safe_db(fn, default=None):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس: %s", exc)
        return default


async def get_setting(key: str) -> Optional[str]:
    def _run():
        res = supabase.table("settings").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else None

    return await asyncio.to_thread(_safe_db, _run, None)


async def set_setting(key: str, value: str) -> Optional[str]:
    def _run():
        supabase.table("settings").upsert({"key": key, "value": value}).execute()

    try:
        await asyncio.to_thread(_run)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام ذخیره تنظیمات: %s", exc)
        return str(exc)


async def get_group_id() -> Optional[int]:
    val = await get_setting("group_id")
    return int(val) if val else None


async def set_group_id(chat_id: int) -> Optional[str]:
    return await set_setting("group_id", str(chat_id))


DEFAULT_WARN_DELETE_SECONDS = 5


async def get_warn_delete_seconds() -> float:
    val = await get_setting("warn_delete_seconds")
    if val is None:
        return DEFAULT_WARN_DELETE_SECONDS
    try:
        return float(val)
    except (TypeError, ValueError):
        return DEFAULT_WARN_DELETE_SECONDS


async def set_warn_delete_seconds(seconds: float) -> Optional[str]:
    return await set_setting("warn_delete_seconds", str(seconds))


async def add_item(kind: str, chat_id: str, title: str, invite_link: str) -> Optional[str]:
    """در صورت موفقیت None برمی‌گرداند، در صورت خطا متن خطا را برمی‌گرداند."""

    def _run():
        supabase.table("channels").upsert(
            {"kind": kind, "chat_id": chat_id, "title": title, "invite_link": invite_link},
            on_conflict="chat_id",
        ).execute()

    try:
        await asyncio.to_thread(_run)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام افزودن آیتم: %s", exc)
        return str(exc)


async def update_item_link(item_id: int, invite_link: str) -> Optional[str]:
    def _run():
        supabase.table("channels").update({"invite_link": invite_link}).eq("id", item_id).execute()

    try:
        await asyncio.to_thread(_run)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام ویرایش لینک: %s", exc)
        return str(exc)


async def remove_item_by_id(item_id: int) -> bool:
    res = await asyncio.to_thread(
        _safe_db, lambda: supabase.table("channels").delete().eq("id", item_id).execute()
    )
    return bool(res and res.data)


async def list_items() -> list:
    res = await asyncio.to_thread(
        _safe_db, lambda: supabase.table("channels").select("*").order("id").execute()
    )
    return res.data if res and res.data else []


async def get_item(item_id: int) -> Optional[dict]:
    for it in await list_items():
        if it["id"] == item_id:
            return it
    return None


async def get_text(key: str, default: str) -> str:
    def _run():
        res = supabase.table("texts").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else default

    return await asyncio.to_thread(_safe_db, _run, default)


async def set_text(key: str, value: str) -> Optional[str]:
    def _run():
        supabase.table("texts").upsert({"key": key, "value": value}).execute()

    try:
        await asyncio.to_thread(_run)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام ذخیره متن: %s", exc)
        return str(exc)


async def has_started_bot(bot_username: str, user_id: int) -> bool:
    def _run():
        res = (
            supabase.table("bot_starts")
            .select("user_id")
            .eq("bot_username", bot_username.lower())
            .eq("user_id", user_id)
            .execute()
        )
        return bool(res.data)

    return await asyncio.to_thread(_safe_db, _run, False)


async def count_bot_starts(bot_username: str) -> int:
    def _run():
        res = (
            supabase.table("bot_starts")
            .select("user_id", count="exact")
            .eq("bot_username", bot_username.lower())
            .execute()
        )
        return res.count or 0

    return await asyncio.to_thread(_safe_db, _run, 0)


async def record_bot_start(bot_username: str, user_id: int) -> None:
    def _run():
        supabase.table("bot_starts").upsert(
            {"bot_username": bot_username.lower(), "user_id": user_id}
        ).execute()

    await asyncio.to_thread(_safe_db, _run)


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
    جایگزینی متغیرها در متن سفارشی ادمین.
    متغیرهای پشتیبانی‌شده: {mention} و منشن_کاربر (فرمت فارسی)
    """
    mention = render_mention(user)
    text = template.replace("{mention}", mention)
    text = text.replace("منشن_کاربر", mention)
    if mention not in text:
        text = f"{mention}\n{text}"
    return text


def build_items_list_text(items: list) -> str:
    """لیست خودکار موارد باقی‌مانده؛ داخل نقل قول (blockquote) چاپ می‌شود."""
    if not items:
        return ""
    lines = []
    for it in items:
        label = KIND_LABEL.get(it.get("kind", "channel"), "کانال")
        lines.append(f"{label}: {html.escape(it['title'])}")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


async def build_full_warn_text(user: User, items: list, use_default: bool = False) -> str:
    intro_template = DEFAULT_WARN_TEXT if use_default else await get_text("warn_text", DEFAULT_WARN_TEXT)
    intro = render_template(intro_template, user)
    items_text = build_items_list_text(items)
    if items_text:
        return f"{intro}\n\n{items_text}"
    return intro


def build_items_keyboard(items: list, user_id: int, check_text: str = "✅ بررسی عضویت") -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        icon = KIND_ICON.get(it.get("kind", "channel"), "📢")
        rows.append([InlineKeyboardButton(text=f"{icon} {it['title']}", url=it["invite_link"], style="primary")])
    rows.append([InlineKeyboardButton(check_text, callback_data=f"check_membership_{user_id}", style="success")])
    return InlineKeyboardMarkup(rows)


async def _check_single_item(bot, item: dict, user_id: int) -> bool:
    kind = item.get("kind", "channel")
    try:
        if kind == "bot":
            return await has_started_bot(item["chat_id"], user_id)
        member = await bot.get_chat_member(chat_id=item["chat_id"], user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning("خطا در بررسی عضویت %s (%s): %s", item.get("title"), item.get("chat_id"), exc)
        return False
    except (TimedOut, NetworkError) as exc:
        logger.warning("تایم‌اوت شبکه هنگام بررسی %s: %s", item.get("title"), exc)
        return False


async def get_missing_items(bot, user_id: int, items: Optional[list] = None) -> list:
    """آیتم‌هایی که کاربر هنوز در آن‌ها عضو نیست (بررسی موازی)."""
    if items is None:
        items = await list_items()
    if not items:
        return []
    results = await asyncio.gather(*[_check_single_item(bot, it, user_id) for it in items])
    return [it for it, ok in zip(items, results) if not ok]


async def is_member_of_all_items(bot, user_id: int) -> bool:
    """بررسی موازی و سریع همه‌ی آیتم‌ها به‌جای حلقه‌ی ترتیبی."""
    items = await list_items()
    if not items:
        return True
    results = await asyncio.gather(*[_check_single_item(bot, it, user_id) for it in items])
    return all(results)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# حالت‌های موقت پیوی ادمین
PENDING_TEXT_EDIT: dict = {}
PENDING_ITEM_ADD: set = set()
PENDING_BOT_ADD: set = set()
PENDING_LINK_EDIT: dict = {}
PENDING_GROUP_SET: set = set()
PENDING_DELETE_TIMER_SET: set = set()


async def _delete_message_after(bot, chat_id: int, message_id: int, delay: float) -> None:
    """بعد از delay ثانیه، پیام را از گروه پاک می‌کند (برای پیام‌های هشدار جوین اجباری)."""
    if delay <= 0:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (BadRequest, Forbidden) as exc:
        logger.info("حذف خودکار پیام هشدار ناموفق بود (احتمالاً قبلاً حذف شده): %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("خطا در حذف خودکار پیام هشدار: %s", exc)

# ---------------------------------------------------------------------------
# هندلرهای گروه
# ---------------------------------------------------------------------------


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if user is None or user.is_bot or message is None:
        return

    group_id = await get_group_id()
    if group_id is None or chat.id != group_id:
        return  # ربات فقط در گروه تعیین‌شده توسط ادمین فعال است

    items = await list_items()
    if not items:
        return  # هیچ آیتمی تنظیم نشده، محدودیتی اعمال نمی‌شود

    missing = await get_missing_items(context.bot, user.id, items)
    if not missing:
        return

    try:
        await message.delete()
    except (BadRequest, Forbidden) as exc:
        logger.warning("عدم امکان حذف پیام: %s", exc)

    keyboard = build_items_keyboard(missing, user.id)
    warn_msg = None
    for use_default in (False, True):
        text = await build_full_warn_text(user, missing, use_default=use_default)
        try:
            warn_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            break
        except BadRequest as exc:
            logger.error("ارسال پیام هشدار ناموفق بود (use_default=%s): %s", use_default, exc)
        except (Forbidden, TimedOut, NetworkError) as exc:
            logger.error("ارسال پیام هشدار ناموفق بود: %s", exc)
            return
    if warn_msg is None:
        return

    delay = await get_warn_delete_seconds()
    if delay > 0:
        task = asyncio.create_task(
            _delete_message_after(context.bot, chat.id, warn_msg.message_id, delay)
        )
        task.add_done_callback(_log_task_exception)


async def _guard_button_owner(query, owner_part: str) -> bool:
    """دکمه‌های پیام هشدار فقط برای کاربرِ هدفِ پیام فعال هستند. دکمه‌های قدیمی (بدون آیدی) رد نمی‌شوند."""
    if owner_part.isdigit() and int(owner_part) != query.from_user.id:
        await query.answer("این دکمه برای شما نیست، پیام خودتان را بفرستید.", show_alert=True)
        return False
    return True


async def on_check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    if not await _guard_button_owner(query, query.data.rpartition("_")[2]):
        return

    try:
        # ربات‌ها بررسی ندارند: زدن دکمه‌ی «بررسی عضویت» = تایید ورود به همه‌ی ربات‌ها
        all_items = await list_items()
        await asyncio.gather(
            *[record_bot_start(it["chat_id"], user.id) for it in all_items if it.get("kind") == "bot"]
        )
        missing = await get_missing_items(context.bot, user.id, all_items)
    except Exception as exc:  # noqa: BLE001
        logger.error("خطا در بررسی عضویت: %s", exc)
        await query.answer("خطای موقت، دوباره تلاش کنید.", show_alert=True)
        return

    if missing:
        # فقط موارد باقی‌مانده نمایش داده می‌شود؛ دکمه‌ی مواردی که عضو شده حذف می‌شود
        for use_default in (False, True):
            text = await build_full_warn_text(user, missing, use_default=use_default)
            try:
                await query.edit_message_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_items_keyboard(missing, user.id),
                    disable_web_page_preview=True,
                )
                await query.answer("هنوز در موارد باقی‌مانده عضو نشده‌اید ❌")
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    await query.answer("هنوز در موارد باقی‌مانده عضو نشده‌اید ❌", show_alert=True)
                    return
                logger.warning("ادیت پیام هشدار ناموفق (use_default=%s): %s", use_default, exc)
        await query.answer("هنوز در همه‌ی موارد عضو نشده‌اید ❌", show_alert=True)
        return

    for use_default in (False, True):
        joined_template = DEFAULT_JOINED_TEXT if use_default else await get_text("joined_text", DEFAULT_JOINED_TEXT)
        text = render_template(joined_template, user)
        try:
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
            break
        except BadRequest as exc:
            logger.info("ادیت پیام تایید ناموفق (use_default=%s): %s", use_default, exc)

    await query.answer("عضویت شما تایید شد ✅")


# ---------------------------------------------------------------------------
# پنل مدیریت ادمین (پیوی ربات)
# ---------------------------------------------------------------------------

MAIN_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🏠 تنظیم گروه", callback_data="menu_set_group", style="primary")],
        [InlineKeyboardButton("📋 مدیریت آیتم‌ها", callback_data="menu_channels", style="primary")],
        [InlineKeyboardButton("✏️ ویرایش متن‌ها", callback_data="menu_texts", style="primary")],
        [InlineKeyboardButton("⏱ زمان حذف پیام هشدار", callback_data="menu_delete_timer", style="primary")],
        [InlineKeyboardButton("ℹ️ وضعیت فعلی", callback_data="menu_status", style="success")],
    ]
)

CHANNELS_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("➕ افزودن کانال/گروه", callback_data="ch_add", style="success")],
        [InlineKeyboardButton("🤖 افزودن ربات", callback_data="ch_add_bot", style="success")],
        [InlineKeyboardButton("📋 لیست کانال‌ها", callback_data="ch_list", style="primary")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main")],
    ]
)

TEXTS_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✏️ ویرایش متن هشدار", callback_data="txt_warn", style="primary")],
        [InlineKeyboardButton("✏️ ویرایش متن تایید عضویت", callback_data="txt_joined", style="primary")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_main")],
    ]
)


def back_kb(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=callback_data)]])


def build_list_keyboard(items: list) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        icon = KIND_ICON.get(it.get("kind", "channel"), "📢")
        rows.append([InlineKeyboardButton(f"{icon} {it['title']}", callback_data=f"ch_view_{it['id']}", style="primary")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu_channels")])
    return InlineKeyboardMarkup(rows)


def build_item_manage_keyboard(item: dict, member_count_label: str = "👥 کاربران عضو شده") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ تغییر لینک", callback_data=f"ch_editlink_{item['id']}", style="primary")],
        [InlineKeyboardButton(member_count_label, callback_data=f"ch_count_{item['id']}", style="primary")],
        [InlineKeyboardButton("🗑 حذف", callback_data=f"ch_del_{item['id']}", style="danger")],
        [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="ch_list")],
    ]
    return InlineKeyboardMarkup(rows)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != Chat.PRIVATE:
        return
    user = update.effective_user

    # اگر یکی از ربات‌های دیگرِ همین مجموعه هستید و کاربر عادی /start زده،
    # این خط برای خودِ همین ربات هم ثبت می‌شود (در صورتی که این ربات خودش
    # به‌عنوان آیتم «ربات» در جای دیگری استفاده شود).
    if user is not None and context.bot.username:
        await record_bot_start(context.bot.username, user.id)

    if not is_admin(user.id):
        await update.message.reply_text("این ربات فقط توسط ادمین‌ها قابل مدیریت است.")
        return
    await update.message.reply_text(
        "به پنل مدیریت ربات جوین اجباری خوش آمدید 👋",
        reply_markup=MAIN_MENU,
    )


async def owner_panel_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مدیریت کلیک‌های دکمه‌های پنل ادمین در پیوی."""
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("شما ادمین ربات نیستید.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "menu_main":
        await query.edit_message_text("پنل مدیریت:", reply_markup=MAIN_MENU)

    elif data == "menu_set_group":
        PENDING_GROUP_SET.add(user_id)
        await query.edit_message_text(
            "ربات را در گروه مورد نظر ادمین کنید.\n\n"
            "روش مطمئن: آیدی عددی گروه را مستقیماً اینجا ارسال کنید، مثل: "
            "<code>-1001234567890</code>\n\n"
            "(یا پیامی که توسط «ادمین ناشناس» در گروه ارسال شده را فوروارد کنید.)",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_main"),
        )

    elif data == "menu_channels":
        await query.edit_message_text("مدیریت آیتم‌های جوین اجباری:", reply_markup=CHANNELS_MENU)

    elif data == "ch_add":
        PENDING_ITEM_ADD.add(user_id)
        await query.edit_message_text(
            "برای افزودن <b>کانال</b> یا <b>گروه</b> (ربات باید ادمین آن باشد)، یکی از راه‌های زیر:\n\n"
            "۱) فوروارد یک پیام از آن کانال/گروه\n"
            "۲) ارسال مستقیم یوزرنیم: <code>@channel_username</code>\n"
            "۳) ارسال مستقیم لینک عمومی: <code>https://t.me/channel_username</code>\n"
            "۴) ارسال مستقیم آیدی عددی (اگر ربات از قبل عضو/ادمین آن است): <code>-1001234567890</code>\n\n"
            "برای کانال/گروه <b>خصوصی</b> که فقط لینک دعوت (+…) دارید، لطفاً فوروارد کنید؛ "
            "یا اگر آیدی عددی را می‌دانید:\n"
            "<code>-1001234567890 | https://t.me/+AbCdEfGhIj</code>\n\n"
            "اگر لینک ندارید ولی ربات ادمین است، فقط یوزرنیم/آیدی/فوروارد کافیست؛ لینک خودکار ساخته می‌شود.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_channels"),
        )

    elif data == "ch_add_bot":
        PENDING_BOT_ADD.add(user_id)
        await query.edit_message_text(
            "یکی از این‌ها را ارسال کنید:\n\n"
            "۱) یوزرنیم ربات: <code>@id_bot</code>\n"
            "۲) لینک ربات: <code>https://t.me/id_bot</code>\n"
            "۳) لینک دعوت با پارامتر استارت: <code>https://t.me/id_bot?start=ref123</code>\n\n"
            "دکمه‌ی این ربات یک لینک مستقیم است و کاربر با زدن آن به پیوی ربات هدایت می‌شود "
            "(دکمه‌ی Start با همان پارامتر آماده است). ورودش وقتی تایید می‌شود که «بررسی عضویت» را بزند. "
            "نیازی به تغییر در ربات مقصد یا دیتابیس مشترک نیست.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_channels"),
        )

    elif data == "ch_list":
        items = await list_items()
        if not items:
            await query.edit_message_text("هیچ آیتمی ثبت نشده است.", reply_markup=back_kb("menu_channels"))
        else:
            await query.edit_message_text("یکی از موارد زیر را برای مدیریت انتخاب کنید:", reply_markup=build_list_keyboard(items))

    elif data.startswith("ch_view_"):
        item_id = int(data.split("_")[-1])
        item = await get_item(item_id)
        if not item:
            await query.edit_message_text("این آیتم دیگر وجود ندارد.", reply_markup=back_kb("ch_list"))
        else:
            label = KIND_LABEL.get(item.get("kind", "channel"), "کانال")
            text = (
                f"مدیریت {label}: <b>{item['title']}</b>\n"
                f"شناسه: <code>{item['chat_id']}</code>\n"
                f"لینک: {item['invite_link']}"
            )
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_item_manage_keyboard(item))

    elif data.startswith("ch_editlink_"):
        item_id = int(data.split("_")[-1])
        PENDING_LINK_EDIT[user_id] = item_id
        await query.edit_message_text(
            "لینک جدید را ارسال کنید:",
            reply_markup=back_kb(f"ch_view_{item_id}"),
        )

    elif data.startswith("ch_count_"):
        item_id = int(data.split("_")[-1])
        item = await get_item(item_id)
        if not item:
            await query.answer("این آیتم دیگر وجود ندارد.", show_alert=True)
            return
        if item.get("kind") == "bot":
            clicks = await count_bot_starts(item["chat_id"])
            await query.answer(f"تعداد کاربران تاییدشده برای این ربات: {clicks}", show_alert=True)
            return
        try:
            count = await context.bot.get_chat_member_count(item["chat_id"])
            await query.answer(f"تعداد اعضا: {count}", show_alert=True)
        except (BadRequest, Forbidden, TimedOut, NetworkError) as exc:
            await query.answer(f"خطا در دریافت تعداد اعضا: {exc}", show_alert=True)

    elif data.startswith("ch_del_"):
        item_id = int(data.split("_")[-1])
        removed = await remove_item_by_id(item_id)
        items = await list_items()
        msg = "حذف شد ✅" if removed else "حذف نشد، دوباره تلاش کنید."
        if items:
            await query.edit_message_text(f"{msg}\n\nیکی از موارد زیر را برای مدیریت انتخاب کنید:", reply_markup=build_list_keyboard(items))
        else:
            await query.edit_message_text(f"{msg}\n\nهیچ آیتمی ثبت نشده است.", reply_markup=back_kb("menu_channels"))

    elif data == "menu_texts":
        await query.edit_message_text("ویرایش متن‌ها:", reply_markup=TEXTS_MENU)

    elif data == "txt_warn":
        PENDING_TEXT_EDIT[user_id] = "warn_text"
        current = await get_text("warn_text", DEFAULT_WARN_TEXT)
        await query.edit_message_text(
            "متن فعلی هشدار عضویت (لیست موارد باقی‌مانده خودکار زیرش، داخل نقل قول، اضافه می‌شود):\n\n"
            f"<code>{html.escape(current)}</code>\n\n"
            "متن جدید را با همان قالب‌بندی دلخواه (بولد، نقل قول، کج، زیرخط، اسپویلر و...) ارسال کنید؛ "
            "دقیقاً همان‌طور در گروه ارسال می‌شود. متغیرها: <code>{mention}</code> یا <code>منشن_کاربر</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_texts"),
        )

    elif data == "txt_joined":
        PENDING_TEXT_EDIT[user_id] = "joined_text"
        current = await get_text("joined_text", DEFAULT_JOINED_TEXT)
        await query.edit_message_text(
            "متن فعلی تایید عضویت:\n\n"
            f"<code>{html.escape(current)}</code>\n\n"
            "متن جدید را با همان قالب‌بندی دلخواه ارسال کنید؛ دقیقاً همان‌طور در گروه ارسال می‌شود. "
            "متغیرها: <code>{mention}</code> یا <code>منشن_کاربر</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_texts"),
        )

    elif data == "menu_delete_timer":
        PENDING_DELETE_TIMER_SET.add(user_id)
        current = await get_warn_delete_seconds()
        await query.edit_message_text(
            f"زمان فعلی حذف خودکار پیام هشدار جوین اجباری: <b>{current:g} ثانیه</b>\n\n"
            "عدد جدید (ثانیه) را ارسال کنید، مثلاً <code>5</code> یا <code>10</code>.\n"
            "برای غیرفعال‌کردن حذف خودکار، عدد <code>0</code> را ارسال کنید.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb("menu_main"),
        )

    elif data == "menu_status":
        group_id = await get_group_id()
        items = await list_items()
        delete_seconds = await get_warn_delete_seconds()
        text = (
            f"گروه فعال: <code>{group_id if group_id else 'تنظیم نشده'}</code>\n"
            f"تعداد آیتم‌ها: {len(items)}\n"
            f"زمان حذف پیام هشدار: {delete_seconds:g} ثانیه"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb("menu_main"))


def _extract_link_or_generate(chat_full, username: Optional[str]) -> Optional[str]:
    if chat_full.invite_link:
        return chat_full.invite_link
    if username:
        return f"https://t.me/{username}"
    return None


def parse_chat_ref(text: str):
    """
    تلاش برای استخراج یک شناسه‌ی قابل استفاده در get_chat از متن ورودی ادمین.
    ورودی‌های پشتیبانی‌شده: @username ، t.me/username ، https://t.me/username ،
    آیدی عددی (-100...) و یوزرنیم بدون @ .
    خروجی: (ref, private_invite_link)
        ref: چیزی که مستقیم می‌شود به get_chat داد (str یا int) یا None
        private_invite_link: اگر ورودی یک لینک دعوت خصوصی (t.me/+... یا joinchat) بود،
            همان لینک برگردانده می‌شود چون get_chat نمی‌تواند آن را resolve کند.
    """
    text = text.strip()
    if not text:
        return None, None

    m = re.match(r"^(?:https?://)?t\.me/(\+|joinchat/)[\w-]+/?$", text, re.IGNORECASE)
    if m:
        return None, text

    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})/?$", text, re.IGNORECASE)
    if m:
        return f"@{m.group(1)}", None

    m = re.match(r"^@([A-Za-z0-9_]{5,32})$", text)
    if m:
        return text, None

    m = re.match(r"^-?\d+$", text)
    if m:
        return int(text), None

    m = re.match(r"^[A-Za-z0-9_]{5,32}$", text)
    if m:
        return f"@{text}", None

    return None, None


async def owner_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام‌های متنی ادمین در پیوی که برای مراحل pending استفاده می‌شوند."""
    user = update.effective_user
    message = update.effective_message

    if update.effective_chat.type != Chat.PRIVATE or not is_admin(user.id):
        return

    user_id = user.id

    # حالت: ثبت گروه
    if user_id in PENDING_GROUP_SET:
        PENDING_GROUP_SET.discard(user_id)
        chat_id = None
        origin = message.forward_origin
        if origin is not None:
            origin_chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
            if origin_chat is not None and origin_chat.type in (Chat.GROUP, Chat.SUPERGROUP):
                chat_id = origin_chat.id

        if chat_id is None and message.text:
            ref, private_link = parse_chat_ref(message.text)
            if ref is not None:
                try:
                    chat_full = await context.bot.get_chat(ref)
                except (BadRequest, Forbidden) as exc:
                    await message.reply_text(f"خطا در دسترسی به گروه: {exc}")
                    return
                if chat_full.type not in (Chat.GROUP, Chat.SUPERGROUP):
                    await message.reply_text("این یک گروه نیست. لطفاً لینک/آیدی/فوروارد یک گروه را ارسال کنید.")
                    return
                chat_id = chat_full.id
            elif private_link is not None:
                await message.reply_text(
                    "لینک دعوت خصوصی را نمی‌توان مستقیماً resolve کرد. "
                    "لطفاً آیدی عددی گروه را ارسال کنید یا پیامی از «ادمین ناشناس» در آن گروه را فوروارد کنید."
                )
                return

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

        set_error = await set_group_id(chat_id)
        if set_error:
            await message.reply_text(
                f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{set_error}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await message.reply_text(f"گروه با آیدی {chat_id} ثبت شد ✅", reply_markup=MAIN_MENU)
        return

    # حالت: افزودن کانال/گروه
    if user_id in PENDING_ITEM_ADD:
        PENDING_ITEM_ADD.discard(user_id)

        async def _finish_add(chat_full, forced_link: Optional[str] = None) -> None:
            invite_link = forced_link or _extract_link_or_generate(chat_full, chat_full.username)
            if not invite_link:
                try:
                    invite_link = await context.bot.export_chat_invite_link(chat_full.id)
                except (BadRequest, Forbidden) as exc:
                    await message.reply_text(
                        "لینک دعوت پیدا نشد و ربات نتوانست خودش لینک بسازد. "
                        f"مطمئن شوید ربات ادمین با دسترسی دعوت کاربران است. ({exc})"
                    )
                    return
            kind = "channel" if chat_full.type == Chat.CHANNEL else "group"
            err = await add_item(kind, str(chat_full.id), chat_full.title, invite_link)
            if err:
                await message.reply_text(
                    f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>\n\n"
                    "احتمالاً جدول channels در Supabase ساختار درستی ندارد "
                    "(نیاز به UNIQUE روی ستون chat_id دارد). از کوئری‌های ساخت جدول استفاده کنید.",
                    parse_mode=ParseMode.HTML,
                )
                return
            await message.reply_text(
                f"{KIND_LABEL[kind]} «{chat_full.title}» با موفقیت اضافه شد ✅", reply_markup=CHANNELS_MENU
            )

        origin = message.forward_origin
        origin_chat = getattr(origin, "chat", None) if origin is not None else None
        if origin_chat is not None and origin_chat.type in (Chat.CHANNEL, Chat.GROUP, Chat.SUPERGROUP):
            try:
                chat_full = await context.bot.get_chat(origin_chat.id)
            except (BadRequest, Forbidden) as exc:
                await message.reply_text(f"خطا: {exc}")
                return
            await _finish_add(chat_full)
            return

        text = (message.text or "").strip()

        if "|" in text:
            chat_part, link_part = [p.strip() for p in text.split("|", 1)]
            ref, _private_link = parse_chat_ref(chat_part)
            if ref is None:
                ref = chat_part  # اجازه بده get_chat خودش خطا بدهد اگر واقعاً نامعتبر بود
            try:
                chat_full = await context.bot.get_chat(ref)
            except (BadRequest, Forbidden) as exc:
                await message.reply_text(f"خطا در دسترسی: {exc}")
                return
            if chat_full.type not in (Chat.CHANNEL, Chat.GROUP, Chat.SUPERGROUP):
                await message.reply_text("این یک کانال یا گروه نیست.")
                return
            await _finish_add(chat_full, forced_link=link_part)
            return

        # ورودی تک‌خطی: می‌تواند @username، لینک t.me عمومی یا آیدی عددی باشد
        ref, private_link = parse_chat_ref(text)
        if ref is not None:
            try:
                chat_full = await context.bot.get_chat(ref)
            except (BadRequest, Forbidden) as exc:
                await message.reply_text(f"خطا در دسترسی: {exc}")
                return
            if chat_full.type not in (Chat.CHANNEL, Chat.GROUP, Chat.SUPERGROUP):
                await message.reply_text("این یک کانال یا گروه نیست.")
                return
            await _finish_add(chat_full)
            return

        if private_link is not None:
            await message.reply_text(
                "این یک لینک دعوت خصوصی است و ربات نمی‌تواند فقط از روی آن، کانال/گروه را resolve کند "
                "(محدودیت API تلگرام). یکی از راه‌های زیر را امتحان کنید:\n\n"
                "۱) ربات را ادمین آن کانال/گروه کنید و یک پیام از آن را برای من فوروارد کنید.\n"
                "۲) اگر آیدی عددی کانال/گروه را می‌دانید، به این شکل بفرستید:\n"
                f"<code>-1001234567890 | {private_link}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        await message.reply_text(
            "فرمت نامعتبر است. یکی از این‌ها را بفرستید: یوزرنیم (@channel)، لینک عمومی t.me/... ، "
            "آیدی عددی، یا فوروارد یک پیام از آن کانال/گروه.\n\n"
            "برای لینک خصوصی: <code>-1001234567890 | https://t.me/+AbCdEfGhIj</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # حالت: افزودن ربات
    if user_id in PENDING_BOT_ADD:
        PENDING_BOT_ADD.discard(user_id)
        raw = (message.text or "").strip()
        m = re.match(
            r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})/?(?:\?(.*))?$",
            raw,
            re.IGNORECASE,
        )
        if m:
            username = m.group(1)
            query_string = m.group(2) or ""
            deep_link = f"https://t.me/{username}" + (f"?{query_string}" if "start=" in query_string else "?start=verify_join")
        else:
            username = raw.lstrip("@")
            if not re.match(r"^[A-Za-z0-9_]{5,32}$", username):
                await message.reply_text("یوزرنیم یا لینک نامعتبر است.")
                return
            deep_link = f"https://t.me/{username}?start=verify_join"

        title = username
        try:
            bot_chat = await context.bot.get_chat(f"@{username}")
            title = bot_chat.first_name or bot_chat.title or username
        except (BadRequest, Forbidden) as exc:
            logger.info("گرفتن اطلاعات ربات %s ممکن نبود، از یوزرنیم به‌عنوان عنوان استفاده شد: %s", username, exc)

        err = await add_item("bot", username, title, deep_link)
        if err:
            await message.reply_text(
                f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>", parse_mode=ParseMode.HTML
            )
            return
        await message.reply_text(
            f"ربات «{title}» اضافه شد ✅\n"
            f"لینک استارت: {deep_link}\n\n"
            "با زدن «بررسی عضویت»، ورود کاربر به این ربات تایید می‌شود.",
            reply_markup=CHANNELS_MENU,
            disable_web_page_preview=True,
        )
        return

    # حالت: ویرایش لینک آیتم
    if user_id in PENDING_LINK_EDIT:
        item_id = PENDING_LINK_EDIT.pop(user_id)
        new_link = (message.text or "").strip()
        if not new_link:
            await message.reply_text("لینک نامعتبر است.")
            return
        err = await update_item_link(item_id, new_link)
        if err:
            await message.reply_text(f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>", parse_mode=ParseMode.HTML)
            return
        item = await get_item(item_id)
        await message.reply_text("لینک به‌روزرسانی شد ✅", reply_markup=build_item_manage_keyboard(item) if item else CHANNELS_MENU)
        return

    # حالت: تنظیم زمان حذف خودکار پیام هشدار
    if user_id in PENDING_DELETE_TIMER_SET:
        PENDING_DELETE_TIMER_SET.discard(user_id)
        raw = (message.text or "").strip().replace(",", ".")
        try:
            seconds = float(raw)
            if seconds < 0:
                raise ValueError
        except ValueError:
            await message.reply_text(
                "عدد نامعتبر است. یک عدد غیرمنفی ارسال کنید (مثلاً 5 یا 0).",
                reply_markup=back_kb("menu_main"),
            )
            return
        err = await set_warn_delete_seconds(seconds)
        if err:
            await message.reply_text(f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>", parse_mode=ParseMode.HTML)
            return
        note = "حذف خودکار غیرفعال شد." if seconds == 0 else f"پیام‌های هشدار بعد از {seconds:g} ثانیه پاک می‌شوند."
        await message.reply_text(f"ذخیره شد ✅\n{note}", reply_markup=MAIN_MENU)
        return

    # حالت: ویرایش متن‌ها
    if user_id in PENDING_TEXT_EDIT:
        key = PENDING_TEXT_EDIT.pop(user_id)
        new_text = message.text_html or ""  # قالب‌بندی (بولد، نقل قول، ...) به‌صورت HTML ذخیره می‌شود
        err = await set_text(key, new_text)
        if err:
            await message.reply_text(f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>", parse_mode=ParseMode.HTML)
            return
        await message.reply_text("متن با موفقیت به‌روزرسانی شد ✅", reply_markup=TEXTS_MENU)
        return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هندلر سراسری خطا؛ جلوگیری از کرش کل ربات به‌خاطر یک آپدیت خراب."""
    if isinstance(context.error, Conflict):
        logger.error(
            "خطای Conflict: یک نمونه‌ی دیگر از همین ربات (همین توکن) هم‌زمان در حال polling است. "
            "مطمئن شوید فقط یک instance از سرویس روی Render (یا هر جای دیگر) در حال اجراست "
            "و دیپلوی قبلی کاملاً متوقف شده. این خطا خودش هیچ کرشی ایجاد نمی‌کند و ربات تلاش "
            "می‌کند دوباره وصل شود."
        )
        return
    logger.error("خطای پردازش‌نشده: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# اجرای ربات (Webhook روی aiohttp)
# ---------------------------------------------------------------------------

# مسیر مخفی وبهوک؛ اگر در env ست نشود، یک مقدار تصادفی ثابتِ فرآیند ساخته می‌شود
# (بهتر است WEBHOOK_SECRET را در Render ست کنید تا بین دیپلوی‌ها ثابت بماند).
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or secrets.token_urlsafe(24)
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

# Render این مقدار را خودش و به‌صورت خودکار در env ست می‌کند.
BASE_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL", "")).rstrip("/")


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _telegram_webhook(request: web.Request) -> web.Response:
    application: Application = request.app["application"]

    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad request")

    update = Update.de_json(data, application.bot)
    if update:
        await application.update_queue.put(update)
    return web.Response(text="OK")


async def _on_startup(app: web.Application) -> None:
    application: Application = app["application"]
    await application.initialize()
    await application.start()

    webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    logger.info("وبهوک روی %s تنظیم شد", webhook_url)


async def _on_cleanup(app: web.Application) -> None:
    application: Application = app["application"]
    await application.bot.delete_webhook()
    await application.stop()
    await application.shutdown()


def main() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("متغیرهای محیطی SUPABASE_URL و SUPABASE_KEY باید تنظیم شوند.")
    if not BASE_URL:
        raise RuntimeError(
            "آدرس عمومی سرویس پیدا نشد. اگر روی Render نیستید، متغیر محیطی "
            "WEBHOOK_URL را برابر آدرس عمومی سرویس‌تان (بدون / انتهایی) ست کنید."
        )

    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=15.0,
        pool_timeout=15.0,
    )

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)  # پردازش موازی پیام‌ها => سرعت بالا، پیامی جا نمی‌ماند
        .build()
    )

    application.add_error_handler(on_error)

    application.add_handler(CommandHandler("start", start_cmd))

    application.add_handler(CallbackQueryHandler(on_check_membership, pattern=r"^check_membership(_\d+)?$"))
    application.add_handler(CallbackQueryHandler(owner_panel_router, pattern="^(menu_|ch_|txt_)"))

    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, on_group_message)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.ALL, owner_private_message)
    )

    web_app = web.Application()
    web_app["application"] = application
    web_app.router.add_get("/", _health)
    web_app.router.add_get("/healthz", _health)
    web_app.router.add_post(WEBHOOK_PATH, _telegram_webhook)
    web_app.on_startup.append(_on_startup)
    web_app.on_cleanup.append(_on_cleanup)

    port = int(os.environ.get("PORT", "10000"))
    logger.info("ربات در حال اجراست (webhook)...")

    async def _run_with_processor() -> None:
        runner = web.AppRunner(web_app)
        await runner.setup()  # اینجا _on_startup صدا زده می‌شود: initialize()+start()+set_webhook
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        # پردازشگر آپدیت‌ها: چون اینجا run_polling استفاده نمی‌شود،
        # خودمان باید صف update_queue را مصرف کنیم.
        queue_task = asyncio.create_task(_process_update_queue(application))
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            queue_task.cancel()
            await runner.cleanup()  # اینجا _on_cleanup صدا زده می‌شود: delete_webhook()+stop()+shutdown()

    asyncio.run(_run_with_processor())


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("خطای پردازش‌نشده در تسک آپدیت: %s", exc, exc_info=exc)


async def _process_update_queue(application: Application) -> None:
    """
    مصرف‌کننده‌ی صف آپدیت‌ها. حیاتی‌ست که این حلقه هرگز کامل متوقف نشود، وگرنه
    aiohttp همچنان سالم و 'روشن' جواب می‌دهد ولی هیچ آپدیتی از تلگرام دیگر
    پردازش نمی‌شود (دقیقاً همان علامتی که گزارش شده: رندر روشن، ربات ساکت).
    برای همین هر خطای احتمالی این‌جا catch می‌شود تا خودِ حلقه هیچ‌وقت نمیرد.
    """
    while True:
        try:
            update = await application.update_queue.get()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("خطا در دریافت از صف آپدیت: %s", exc, exc_info=exc)
            continue

        try:
            task = application.create_task(application.process_update(update))
            task.add_done_callback(_log_task_exception)
        except Exception as exc:  # noqa: BLE001
            logger.error("خطا در ایجاد تسک پردازش آپدیت: %s", exc, exc_info=exc)


if __name__ == "__main__":
    main()
