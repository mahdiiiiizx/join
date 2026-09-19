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
        invite_link text not null,
        expires_at timestamptz,                 -- زمان پایان جوین اجباریِ این آیتم (خالی = بدون محدودیت)
        start_members bigint                    -- تعداد اعضا در لحظه‌ی ثبت آیتم (مبنای شمارش ورودی‌ها)
    );

    -- اگر جدول channels را قبلاً ساخته‌اید، فقط این را اجرا کنید:
    alter table channels add column if not exists expires_at timestamptz;
    alter table channels add column if not exists start_members bigint;

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
import time
from datetime import datetime, timedelta, timezone
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
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
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


# کش کوتاه‌مدت: هر پیام گروه قبلاً چندین تماس دیتابیس (گروه، آیتم‌ها، متن‌ها، ...) می‌زد و
# تماس‌ها در ترد‌های محدود صف می‌کشیدند؛ همین باعث کندی حذف پیام‌ها در حالت هجوم پیام بود.
# کش فقط نتیجه‌ی موفق را نگه می‌دارد و با هر تغییر ادمین بلافاصله پاک می‌شود.
CACHE_TTL_SECONDS = 15.0
_db_cache: dict = {}


def _cache_get(key: str):
    hit = _db_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return True, hit[1]
    return False, None


def _cache_put(key: str, value) -> None:
    _db_cache[key] = (time.monotonic(), value)


def _cache_clear() -> None:
    _db_cache.clear()
    _membership_cache.clear()


async def get_setting(key: str) -> Optional[str]:
    hit, cached = _cache_get(f"setting:{key}")
    if hit:
        return cached

    def _run():
        res = supabase.table("settings").select("value").eq("key", key).execute()
        value = res.data[0]["value"] if res.data else None
        if value is not None:
            _cache_put(f"setting:{key}", value)
        return value

    return await asyncio.to_thread(_safe_db, _run, None)


async def set_setting(key: str, value: str) -> Optional[str]:
    def _run():
        supabase.table("settings").upsert({"key": key, "value": value}).execute()

    try:
        await asyncio.to_thread(_run)
        _cache_clear()
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


async def add_item(kind: str, chat_id: str, title: str, invite_link: str, start_members: Optional[int] = None) -> Optional[str]:
    """در صورت موفقیت None برمی‌گرداند، در صورت خطا متن خطا را برمی‌گرداند."""

    def _run():
        payload = {"kind": kind, "chat_id": chat_id, "title": title, "invite_link": invite_link}
        existing = supabase.table("channels").select("id,start_members").eq("chat_id", chat_id).execute().data
        # مبنای شمارش فقط یک بار ثبت می‌شود؛ ثبت مجدد همان آیتم آن را ریست نمی‌کند
        if start_members is not None and not (existing and existing[0].get("start_members") is not None):
            payload["start_members"] = start_members
        supabase.table("channels").upsert(payload, on_conflict="chat_id").execute()

    try:
        await asyncio.to_thread(_run)
        _cache_clear()
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام افزودن آیتم: %s", exc)
        return str(exc)


async def update_item_link(item_id: int, invite_link: str) -> Optional[str]:
    def _run():
        supabase.table("channels").update({"invite_link": invite_link}).eq("id", item_id).execute()

    try:
        await asyncio.to_thread(_run)
        _cache_clear()
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام ویرایش لینک: %s", exc)
        return str(exc)


async def remove_item_by_id(item_id: int) -> bool:
    res = await asyncio.to_thread(
        _safe_db, lambda: supabase.table("channels").delete().eq("id", item_id).execute()
    )
    _cache_clear()
    return bool(res and res.data)


async def _fetch_items() -> list:
    hit, cached = _cache_get("items")
    if hit:
        return cached

    def _run():
        res = supabase.table("channels").select("*").order("id").execute()
        items = res.data or []
        _cache_put("items", items)
        return items

    return await asyncio.to_thread(_safe_db, _run, [])


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def item_is_expired(item: dict) -> bool:
    expires_at = _parse_ts(item.get("expires_at"))
    return expires_at is not None and expires_at <= datetime.now(timezone.utc)


def format_duration(delta: timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} روز")
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes and not days:
        parts.append(f"{minutes} دقیقه")
    return " و ".join(parts) if parts else "کمتر از یک دقیقه"


def describe_item_schedule(item: dict) -> str:
    expires_at = _parse_ts(item.get("expires_at"))
    if expires_at is None:
        return "فعال، بدون محدودیت زمانی"
    if item_is_expired(item):
        return "⛔️ منقضی شده (دیگر جوین اجباری نیست)"
    return f"فعال، {format_duration(expires_at - datetime.now(timezone.utc))} دیگر باقی مانده"


async def list_items() -> list:
    """آیتم‌های فعالِ جوین اجباری (آیتم‌های منقضی‌شده حذف می‌شوند)."""
    return [it for it in await _fetch_items() if not item_is_expired(it)]


async def list_all_items() -> list:
    """همه‌ی آیتم‌ها (شامل منقضی‌شده‌ها) برای پنل مدیریت."""
    return await _fetch_items()


async def get_item(item_id: int) -> Optional[dict]:
    for it in await list_all_items():
        if it["id"] == item_id:
            return it
    return None


async def get_item_by_chat_id(chat_id: str) -> Optional[dict]:
    for it in await list_all_items():
        if str(it["chat_id"]) == str(chat_id):
            return it
    return None


EXPIRES_AT_MISSING_HINT = (
    "ستون expires_at در جدول channels وجود ندارد. این کوئری را در Supabase اجرا کنید:\n"
    "alter table channels add column if not exists expires_at timestamptz;"
)


async def set_item_expiry(item_id: int, expires_at: Optional[datetime]) -> Optional[str]:
    """expires_at=None یعنی فعال‌سازی هم‌اکنون بدون محدودیت زمانی. در صورت خطا متن خطا را برمی‌گرداند."""
    value = expires_at.isoformat() if expires_at else None

    def _run():
        supabase.table("channels").update({"expires_at": value}).eq("id", item_id).execute()

    try:
        await asyncio.to_thread(_run)
        _cache_clear()
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("خطای دیتابیس هنگام تنظیم زمان آیتم: %s", exc)
        if "expires_at" in str(exc):
            return EXPIRES_AT_MISSING_HINT
        return str(exc)


_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_DURATION_RE = re.compile(
    r"(\d+)\s*(m|min|mins|minute|minutes|دقیقه|h|hr|hrs|hour|hours|ساعت|d|day|days|روز)?"
)


def parse_duration(text: str) -> Optional[timedelta]:
    """مثال‌ها: 30m ، 12h ، 7d ، 90 دقیقه ، 2 روز ، فقط عدد = ساعت."""
    match = _DURATION_RE.fullmatch(text.translate(_PERSIAN_DIGITS).strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2) or "h"
    if unit.startswith(("m", "دقیقه")):
        delta = timedelta(minutes=amount)
    elif unit.startswith(("d", "روز")):
        delta = timedelta(days=amount)
    else:
        delta = timedelta(hours=amount)
    if delta <= timedelta(0) or delta > timedelta(days=3650):
        return None
    return delta


async def get_text(key: str, default: str) -> str:
    hit, cached = _cache_get(f"text:{key}")
    if hit:
        return cached

    def _run():
        res = supabase.table("texts").select("value").eq("key", key).execute()
        value = res.data[0]["value"] if res.data else default
        _cache_put(f"text:{key}", value)
        return value

    return await asyncio.to_thread(_safe_db, _run, default)


async def set_text(key: str, value: str) -> Optional[str]:
    def _run():
        supabase.table("texts").upsert({"key": key, "value": value}).execute()

    try:
        await asyncio.to_thread(_run)
        _cache_clear()
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


def build_items_keyboard(items: list, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        icon = KIND_ICON.get(it.get("kind", "channel"), "📢")
        rows.append([InlineKeyboardButton(text=f"{icon} {it['title']}", url=it["invite_link"])])
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


MEMBERSHIP_OK_TTL_SECONDS = 20.0    # کاربری که همه‌چیز را کامل دارد: ۲۰ ثانیه دوباره چک نمی‌شود
MEMBERSHIP_MISS_TTL_SECONDS = 3.0   # کاربر ناقص: فقط برای هجوم پیام‌های هم‌زمان (چند ثانیه) کش می‌شود
_membership_cache: dict = {}        # user_id -> (زمان، مجموعه‌ی id آیتم‌های ناقص)
_membership_inflight: dict = {}     # user_id -> تسک در حال اجرای بررسی


def store_membership(user_id: int, missing: list) -> None:
    if len(_membership_cache) > 5000:
        _membership_cache.clear()
    _membership_cache[user_id] = (time.monotonic(), {it["id"] for it in missing})


async def get_missing_items_fast(bot, user_id: int, items: list) -> list:
    """
    نسخه‌ی سریع برای پیام‌های گروه: نتیجه‌ی اخیر را از کش می‌خواند و اگر چند پیام هم‌زمان
    از یک کاربر برسد، فقط یک بار get_chat_member می‌زند و همه از همان نتیجه استفاده می‌کنند.
    """
    hit = _membership_cache.get(user_id)
    if hit is not None:
        ts, missing_ids = hit
        ttl = MEMBERSHIP_MISS_TTL_SECONDS if missing_ids else MEMBERSHIP_OK_TTL_SECONDS
        if time.monotonic() - ts < ttl:
            return [it for it in items if it["id"] in missing_ids]

    task = _membership_inflight.get(user_id)
    if task is None:
        task = asyncio.ensure_future(get_missing_items(bot, user_id, items))
        _membership_inflight[user_id] = task

        def _cleanup(t: asyncio.Future, uid: int = user_id) -> None:
            if _membership_inflight.get(uid) is t:
                _membership_inflight.pop(uid, None)

        task.add_done_callback(_cleanup)

    missing = await asyncio.shield(task)
    store_membership(user_id, missing)
    return missing


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
PENDING_ITEM_TIME: dict = {}   # user_id -> item_id (منتظر مدت زمان)
PENDING_GROUP_SET: set = set()
PENDING_DELETE_TIMER_SET: set = set()


async def _delete_message_after(bot, chat_id: int, message_id: int, delay: float, user_id: Optional[int] = None) -> None:
    """بعد از delay ثانیه، پیام هشدار را پاک می‌کند و به کاربر اجازه می‌دهد اگر هنوز عضو نیست، هشدار جدید بگیرد."""
    if delay <= 0:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (BadRequest, Forbidden) as exc:
        logger.info("حذف خودکار پیام هشدار ناموفق بود (احتمالاً قبلاً حذف شده): %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("خطا در حذف خودکار پیام هشدار: %s", exc)
    finally:
        if user_id is not None:
            active = _active_warn.get(user_id)
            if active is not None and active[0] == message_id:
                _active_warn.pop(user_id, None)

# ---------------------------------------------------------------------------
# هندلرهای گروه
# ---------------------------------------------------------------------------


_warn_locks: dict = {}    # user_id -> Lock (فقط یک هشدار هم‌زمان برای هر کاربر)
_active_warn: dict = {}   # user_id -> (message_id هشدار فعال، زمان انقضای اطمینان، chat_id گروه)


def _get_warn_lock(user_id: int) -> asyncio.Lock:
    if len(_warn_locks) > 2000:
        for uid in [u for u, lk in _warn_locks.items() if not lk.locked()]:
            _warn_locks.pop(uid, None)
            _active_warn.pop(uid, None)
    lock = _warn_locks.get(user_id)
    if lock is None:
        lock = _warn_locks[user_id] = asyncio.Lock()
    return lock


async def delete_message_reliably(message, attempts: int = 5) -> bool:
    """حذف پیام با تلاش مجدد روی محدودیت نرخ (RetryAfter) و خطای شبکه؛ قبلاً همین خطاها باعث می‌شد بعضی پیام‌ها پاک نشوند."""
    for attempt in range(attempts):
        try:
            await message.delete()
            return True
        except RetryAfter as exc:
            wait = exc.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            logger.warning("محدودیت نرخ هنگام حذف پیام؛ %.1f ثانیه صبر و تلاش مجدد", wait)
            await asyncio.sleep(wait + 0.2)
        except (TimedOut, NetworkError) as exc:
            logger.warning("خطای شبکه هنگام حذف پیام (تلاش %d): %s", attempt + 1, exc)
            await asyncio.sleep(0.3 * (attempt + 1))
        except BadRequest as exc:
            if "not found" in str(exc).lower():
                return True  # قبلاً حذف شده
            logger.warning("عدم امکان حذف پیام: %s", exc)
            return False
        except Forbidden as exc:
            logger.warning("ربات اجازه‌ی حذف پیام ندارد: %s", exc)
            return False
    logger.error("حذف پیام بعد از %d تلاش ناموفق بود", attempts)
    return False


ANONYMOUS_ADMIN_BOT_ID = 1087968824       # GroupAnonymousBot: پیام ادمینِ ناشناس
GROUP_STAFF_TTL_SECONDS = 120.0
_group_staff_cache: dict = {}             # user_id -> (زمان، ادمین/مالک گروه است؟)
_bot_warned_users: set = set()            # کاربرانی که فقط آیتم «ربات» برایشان مانده و هشدار گرفته‌اند


async def is_group_staff(bot, chat_id: int, user_id: int) -> bool:
    """ادمین‌های ربات و ادمین/مالک گروه (با کش ۲ دقیقه‌ای)."""
    if is_admin(user_id) or user_id == ANONYMOUS_ADMIN_BOT_ID:
        return True
    now = time.monotonic()
    hit = _group_staff_cache.get(user_id)
    if hit and now - hit[0] < GROUP_STAFF_TTL_SECONDS:
        return hit[1]
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        staff = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except (BadRequest, Forbidden, TimedOut, NetworkError) as exc:
        logger.warning("بررسی ادمین بودن کاربر %s ممکن نبود: %s", user_id, exc)
        return False
    if len(_group_staff_cache) > 5000:
        _group_staff_cache.clear()
    _group_staff_cache[user_id] = (now, staff)
    return staff


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if user is None or user.is_bot or message is None:
        return

    group_id = await get_group_id()
    if group_id is None or chat.id != group_id:
        return  # ربات فقط در گروه تعیین‌شده توسط ادمین فعال است

    if message.sender_chat is not None:
        return  # ادمینِ ناشناس (پیام به نام گروه) یا پست کانالِ متصل: هیچ‌وقت بررسی نمی‌شود
    if await is_group_staff(context.bot, chat.id, user.id):
        return  # مالک و ادمین‌های گروه/ربات: پیام‌هایشان خوانده و حذف نمی‌شود

    items = await list_items()
    if not items:
        return  # هیچ آیتمی تنظیم نشده، محدودیتی اعمال نمی‌شود

    missing = await get_missing_items_fast(context.bot, user.id, items)
    if not missing:
        return

    bot_only_missing = all(it.get("kind") == "bot" for it in missing)
    if bot_only_missing and user.id in _bot_warned_users:
        # بررسی واقعی ورود به ربات ممکن نیست؛ بدون دکمه‌ی تایید، پیام بعد از هشدار = تایید خودکار
        _bot_warned_users.discard(user.id)
        await asyncio.gather(*[record_bot_start(it["chat_id"], user.id) for it in missing])
        _membership_cache.pop(user.id, None)
        return

    # اول حذف (هر پیام مستقل و هم‌زمان با بقیه‌ی پیام‌های همان کاربر)، بعد هشدار
    await delete_message_reliably(message)

    delay = await get_warn_delete_seconds()

    async with _get_warn_lock(user.id):
        active = _active_warn.get(user.id)
        if active is not None:
            if time.monotonic() < active[1]:
                return  # هشدار این کاربر هنوز در گروه است؛ پیام‌ها فقط پاک می‌شوند، هشدار تکراری نمی‌رود
            _active_warn.pop(user.id, None)  # حالت اطمینان: انقضا (مثلاً تسک حذف از بین رفته)

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
            except RetryAfter as exc:
                wait = exc.retry_after
                wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
                await asyncio.sleep(wait + 0.2)
                try:
                    warn_msg = await context.bot.send_message(
                        chat_id=chat.id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                    break
                except TelegramError as exc2:
                    logger.error("ارسال پیام هشدار ناموفق بود: %s", exc2)
                    return
            except BadRequest as exc:
                logger.error("ارسال پیام هشدار ناموفق بود (use_default=%s): %s", use_default, exc)
            except (Forbidden, TimedOut, NetworkError) as exc:
                logger.error("ارسال پیام هشدار ناموفق بود: %s", exc)
                return
        if warn_msg is None:
            return
        if bot_only_missing:
            if len(_bot_warned_users) > 5000:
                _bot_warned_users.clear()
            _bot_warned_users.add(user.id)
        # با حذف پیام هشدار (بعد از تایمر) این وضعیت آزاد می‌شود و پیام بعدیِ کاربر هشدار جدید می‌گیرد
        expires_in = delay + 15.0 if delay > 0 else 3600.0
        _active_warn[user.id] = (warn_msg.message_id, time.monotonic() + expires_in, chat.id)

    if delay > 0:
        task = asyncio.create_task(
            _delete_message_after(context.bot, chat.id, warn_msg.message_id, delay, user.id)
        )
        task.add_done_callback(_log_task_exception)


async def _edit_warning_message(bot, chat_id: int, message_id: int, user: User, missing: list) -> None:
    """پیام هشدار را ادیت می‌کند: اگر چیزی باقی نمانده متن تایید عضویت، وگرنه فقط موارد باقی‌مانده."""
    for use_default in (False, True):
        if missing:
            text = await build_full_warn_text(user, missing, use_default=use_default)
            markup = build_items_keyboard(missing, user.id)
        else:
            template = DEFAULT_JOINED_TEXT if use_default else await get_text("joined_text", DEFAULT_JOINED_TEXT)
            text = render_template(template, user)
            markup = None
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower() or "not found" in str(exc).lower():
                return
            logger.warning("ادیت خودکار پیام هشدار ناموفق (use_default=%s): %s", use_default, exc)
        except (Forbidden, TimedOut, NetworkError) as exc:
            logger.warning("ادیت خودکار پیام هشدار ناموفق: %s", exc)
            return


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    تایید خودکار: وقتی کاربر در یکی از کانال‌ها/گروه‌های جوین اجباری عضو می‌شود، بدون زدن «بررسی عضویت»
    پیام هشدارش ادیت می‌شود (دکمه‌ی همان مورد حذف می‌شود، و اگر همه کامل بود متن تایید عضویت می‌آید).
    ربات باید در آن کانال/گروه ادمین باشد تا تلگرام رویداد chat_member را بفرستد.
    """
    cmu = update.chat_member
    if cmu is None:
        return

    member = cmu.new_chat_member
    user = member.user
    if user.is_bot:
        return

    items = await list_items()
    chat_key = str(cmu.chat.id)
    if not any(it.get("kind") != "bot" and str(it["chat_id"]) == chat_key for it in items):
        return

    _membership_cache.pop(user.id, None)

    joined = member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ) or (member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))
    if not joined:
        return

    async with _get_warn_lock(user.id):
        real_items = [it for it in items if it.get("kind") != "bot"]
        bot_items = [it for it in items if it.get("kind") == "bot"]

        # کانال/گروه‌ها کامل شد → ورود به ربات‌ها هم خودکار تایید می‌شود (بررسی واقعی برای ربات ممکن نیست)
        if not await get_missing_items(context.bot, user.id, real_items) and bot_items:
            await asyncio.gather(*[record_bot_start(it["chat_id"], user.id) for it in bot_items])

        missing = await get_missing_items(context.bot, user.id, items)
        store_membership(user.id, missing)

        active = _active_warn.get(user.id)
        if active is None:
            return
        message_id, _expires, warn_chat_id = active
        await _edit_warning_message(context.bot, warn_chat_id, message_id, user, missing)
        if not missing:
            _active_warn.pop(user.id, None)


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
        store_membership(user.id, missing)
        if not missing:
            _active_warn.pop(user.id, None)
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


def build_schedule_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚡ تنظیم هم اکنون", callback_data=f"ch_now_{item_id}", style="success"),
                InlineKeyboardButton("⏰ تنظیم زمان", callback_data=f"ch_time_{item_id}", style="primary"),
            ],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="menu_channels")],
        ]
    )


def back_kb(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=callback_data)]])


MEMBER_COUNT_TTL_SECONDS = 60.0
_member_count_cache: dict = {}  # chat_id -> (زمان، تعداد)


def format_count(count: Optional[int]) -> str:
    return f"{count:,}" if isinstance(count, int) else "—"


async def _save_start_members(item_id: int, value: int) -> None:
    def _run():
        supabase.table("channels").update({"start_members": value}).eq("id", item_id).execute()

    await asyncio.to_thread(_safe_db, _run, None)
    _cache_clear()


async def fetch_member_count(bot, item: dict, force: bool = False) -> Optional[int]:
    """تعداد کاربرانی که از لحظه‌ی ثبت آیتم تا الان وارد شده‌اند = تعداد فعلی − تعداد اولیه. خطا = None."""
    key = str(item["chat_id"])
    now = time.monotonic()
    hit = _member_count_cache.get(key)
    if hit and not force and now - hit[0] < MEMBER_COUNT_TTL_SECONDS:
        current = hit[1]
    else:
        try:
            if item.get("kind") == "bot":
                current = await count_bot_starts(item["chat_id"])
            else:
                current = await bot.get_chat_member_count(item["chat_id"])
        except (BadRequest, Forbidden, TimedOut, NetworkError) as exc:
            logger.warning("خطا در دریافت تعداد اعضای %s: %s", item.get("title"), exc)
            if not hit:
                return None
            current = hit[1]
        else:
            _member_count_cache[key] = (now, current)

    baseline = item.get("start_members")
    if baseline is None:
        # آیتم‌های قدیمی که مبنا ندارند: از همین لحظه شروع به شمارش می‌کنند
        item["start_members"] = current
        await _save_start_members(item["id"], current)
        return 0
    return max(0, current - baseline)


async def fetch_member_counts(bot, items: list) -> dict:
    counts = await asyncio.gather(*(fetch_member_count(bot, it) for it in items))
    return {it["id"]: c for it, c in zip(items, counts)}


def build_list_keyboard(items: list, counts: Optional[dict] = None) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        icon = "⛔️" if item_is_expired(it) else KIND_ICON.get(it.get("kind", "channel"), "📢")
        suffix = f"  •  👥 {format_count(counts.get(it['id']))}" if counts is not None else ""
        rows.append([InlineKeyboardButton(f"{icon} {it['title']}{suffix}", callback_data=f"ch_view_{it['id']}", style="primary")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu_channels")])
    return InlineKeyboardMarkup(rows)


def build_item_manage_keyboard(item: dict, count: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⚡ تنظیم هم اکنون", callback_data=f"ch_now_{item['id']}", style="success"),
            InlineKeyboardButton("⏰ تنظیم زمان", callback_data=f"ch_time_{item['id']}", style="primary"),
        ],
        [InlineKeyboardButton("✏️ تغییر لینک", callback_data=f"ch_editlink_{item['id']}", style="primary")],
        [InlineKeyboardButton(f"👥 {format_count(count)}", callback_data=f"ch_count_{item['id']}", style="primary")],
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

    if not data.startswith("ch_time_"):
        PENDING_ITEM_TIME.pop(user_id, None)

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
        items = await list_all_items()
        if not items:
            await query.edit_message_text("هیچ آیتمی ثبت نشده است.", reply_markup=back_kb("menu_channels"))
        else:
            counts = await fetch_member_counts(context.bot, items)
            await query.edit_message_text("یکی از موارد زیر را برای مدیریت انتخاب کنید:", reply_markup=build_list_keyboard(items, counts))

    elif data.startswith("ch_view_"):
        item_id = int(data.split("_")[-1])
        item = await get_item(item_id)
        if not item:
            await query.edit_message_text("این آیتم دیگر وجود ندارد.", reply_markup=back_kb("ch_list"))
        else:
            label = KIND_LABEL.get(item.get("kind", "channel"), "کانال")
            text = (
                f"مدیریت {label}: <b>{html.escape(item['title'])}</b>\n"
                f"شناسه: <code>{item['chat_id']}</code>\n"
                f"لینک: {item['invite_link']}\n"
                f"وضعیت: {describe_item_schedule(item)}"
            )
            count = await fetch_member_count(context.bot, item)
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_item_manage_keyboard(item, count))

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
            await query.edit_message_text("این آیتم دیگر وجود ندارد.", reply_markup=back_kb("ch_list"))
            return
        count = await fetch_member_count(context.bot, item, force=True)
        try:
            await query.edit_message_reply_markup(reply_markup=build_item_manage_keyboard(item, count))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    elif data.startswith("ch_now_"):
        item_id = int(data.split("_")[-1])
        item = await get_item(item_id)
        if not item:
            await query.edit_message_text("این آیتم دیگر وجود ندارد.", reply_markup=back_kb("ch_list"))
            return
        PENDING_ITEM_TIME.pop(user_id, None)
        err = await set_item_expiry(item_id, None)
        if err:
            await query.edit_message_text(f"❌ ذخیره نشد:\n{err}", reply_markup=back_kb(f"ch_view_{item_id}"))
            return
        await query.edit_message_text(
            f"«{item['title']}» هم‌اکنون فعال شد و محدودیت زمانی ندارد ✅", reply_markup=CHANNELS_MENU
        )

    elif data.startswith("ch_time_"):
        item_id = int(data.split("_")[-1])
        item = await get_item(item_id)
        if not item:
            await query.edit_message_text("این آیتم دیگر وجود ندارد.", reply_markup=back_kb("ch_list"))
            return
        PENDING_ITEM_TIME[user_id] = item_id
        await query.edit_message_text(
            f"مدت فعال بودن جوین اجباریِ «{item['title']}» را بفرستید. بعد از این مدت، این مورد خودکار از جوین اجباری کنار می‌رود.\n\n"
            "مثال‌ها: <code>30m</code> (دقیقه) ، <code>12h</code> (ساعت) ، <code>7d</code> (روز) ، "
            "<code>90 دقیقه</code> ، <code>2 روز</code>\n"
            "فقط عدد = ساعت.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(f"ch_view_{item_id}"),
        )

    elif data.startswith("ch_del_"):
        item_id = int(data.split("_")[-1])
        removed = await remove_item_by_id(item_id)
        items = await list_all_items()
        msg = "حذف شد ✅" if removed else "حذف نشد، دوباره تلاش کنید."
        if items:
            counts = await fetch_member_counts(context.bot, items)
            await query.edit_message_text(f"{msg}\n\nیکی از موارد زیر را برای مدیریت انتخاب کنید:", reply_markup=build_list_keyboard(items, counts))
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
            try:
                baseline = await context.bot.get_chat_member_count(chat_full.id)
            except (BadRequest, Forbidden, TimedOut, NetworkError) as exc:
                logger.warning("گرفتن تعداد اولیه‌ی اعضا ممکن نبود: %s", exc)
                baseline = None
            err = await add_item(kind, str(chat_full.id), chat_full.title, invite_link, baseline)
            if err:
                await message.reply_text(
                    f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>\n\n"
                    "احتمالاً جدول channels در Supabase ساختار درستی ندارد "
                    "(نیاز به UNIQUE روی ستون chat_id دارد). از کوئری‌های ساخت جدول استفاده کنید.",
                    parse_mode=ParseMode.HTML,
                )
                return
            added = await get_item_by_chat_id(str(chat_full.id))
            if added:
                await message.reply_text(
                    f"{KIND_LABEL[kind]} «{chat_full.title}» با موفقیت اضافه شد ✅\n\n"
                    "همین حالا فعال شود یا برایش زمان تنظیم کنید؟",
                    reply_markup=build_schedule_keyboard(added["id"]),
                )
            else:
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

        err = await add_item("bot", username, title, deep_link, await count_bot_starts(username))
        if err:
            await message.reply_text(
                f"❌ ذخیره در دیتابیس شکست خورد:\n<code>{err}</code>", parse_mode=ParseMode.HTML
            )
            return
        added = await get_item_by_chat_id(username)
        await message.reply_text(
            f"ربات «{title}» اضافه شد ✅\n"
            f"لینک استارت: {deep_link}\n\n"
            "همین حالا فعال شود یا برایش زمان تنظیم کنید؟",
            reply_markup=build_schedule_keyboard(added["id"]) if added else CHANNELS_MENU,
            disable_web_page_preview=True,
        )
        return

    # حالت: تنظیم مدت فعال بودن آیتم
    if user_id in PENDING_ITEM_TIME:
        item_id = PENDING_ITEM_TIME[user_id]
        delta = parse_duration(message.text or "")
        if delta is None:
            await message.reply_text(
                "مدت نامعتبر است. مثال: 30m ، 12h ، 7d ، 90 دقیقه ، 2 روز (فقط عدد = ساعت). دوباره بفرستید."
            )
            return
        PENDING_ITEM_TIME.pop(user_id, None)
        err = await set_item_expiry(item_id, datetime.now(timezone.utc) + delta)
        if err:
            await message.reply_text(f"❌ ذخیره نشد:\n{err}")
            return
        item = await get_item(item_id)
        count = await fetch_member_count(context.bot, item) if item else None
        await message.reply_text(
            f"زمان تنظیم شد ✅ «{item['title'] if item else ''}» به مدت {format_duration(delta)} فعال است "
            "و بعد از آن خودکار از جوین اجباری کنار می‌رود.",
            reply_markup=build_item_manage_keyboard(item, count) if item else CHANNELS_MENU,
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
        count = await fetch_member_count(context.bot, item) if item else None
        await message.reply_text("لینک به‌روزرسانی شد ✅", reply_markup=build_item_manage_keyboard(item, count) if item else CHANNELS_MENU)
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
        max_connections=100,
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
    application.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

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
