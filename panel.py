"""
پنل جدید مدیریت (Reply Keyboard) + مسیریاب پیام‌های خصوصی + ثبت هندلرها.

- ادمین‌ها با /start یک پنل reply keyboard می‌گیرند.
- کاربران عادی (وقتی فروش جوین اجباری روشن است) وارد جریان خرید می‌شوند (sales.py).
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import bot as core
import sales

logger = logging.getLogger("forcejoin_bot.panel")

BTN_ITEMS = "📦 مدیریت آیتم‌ها"
BTN_SETTINGS = "⚙️ تنظیمات"
BTN_GROUP = "🏠 تنظیم گروه"
BTN_AVG = "📈 میانگین جذب"
BTN_STATUS = "ℹ️ وضعیت فعلی"


def admin_panel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ITEMS, style="primary"), KeyboardButton(BTN_SETTINGS, style="primary")],
            [KeyboardButton(BTN_GROUP, style="primary"), KeyboardButton(BTN_AVG, style="success")],
            [KeyboardButton(BTN_STATUS, style="success")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="یکی از گزینه‌های پنل را انتخاب کنید",
    )


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ ویرایش متن‌ها", callback_data="st_texts", style="primary")],
            [InlineKeyboardButton("⏱ زمان حذف هشدار پیام", callback_data="st_timer", style="primary")],
            [InlineKeyboardButton("💰 فعال‌سازی فروش جوین اجباری خودکار", callback_data="sale_open", style="success")],
            [InlineKeyboardButton("🔙 بستن", callback_data="menu_main")],
        ]
    )


def clear_admin_pending(user_id: int) -> None:
    """هر حالت «منتظر ورودی» قبلی ادمین را لغو می‌کند (وقتی دکمه‌ی پنل را می‌زند)."""
    core.PENDING_TEXT_EDIT.pop(user_id, None)
    core.PENDING_ITEM_ADD.discard(user_id)
    core.PENDING_BOT_ADD.discard(user_id)
    core.PENDING_LINK_EDIT.pop(user_id, None)
    core.PENDING_ITEM_TIME.pop(user_id, None)
    core.PENDING_ITEM_QUOTA.pop(user_id, None)
    core.PENDING_FAKE_MEMBERS.pop(user_id, None)
    core.PENDING_GROUP_SET.discard(user_id)
    core.PENDING_DELETE_TIMER_SET.discard(user_id)
    sales.clear_admin_state(user_id)


# ---------------------------------------------------------------------------
# /start و مسیریاب پیوی
# ---------------------------------------------------------------------------


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != Chat.PRIVATE:
        return
    user = update.effective_user
    if user is not None and context.bot.username:
        await core.record_bot_start(context.bot.username, user.id)

    if not core.is_admin(user.id):
        await sales.buyer_start(update, context)
        return

    clear_admin_pending(user.id)
    await update.message.reply_text(
        "به پنل مدیریت ربات جوین اجباری خوش آمدید 👋",
        reply_markup=admin_panel_kb(),
    )


async def on_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("مدیریت آیتم‌های جوین اجباری:", reply_markup=core.CHANNELS_MENU)


async def on_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("⚙️ تنظیمات:", reply_markup=settings_kb())


GROUP_PROMPT = (
    "🏠 <b>تنظیم گروه</b>\n\n"
    "ابتدا ربات را در گروه <b>ادمین</b> کنید، سپس یکی از این‌ها را بفرستید:\n"
    "• یوزرنیم: <code>@USERNAME</code>\n"
    "• لینک گروه: <code>https://t.me/USERNAME</code>\n"
    "• لینک یک پیام گروه: <code>https://t.me/c/1234567890/5</code>\n"
    "• آیدی عددی: <code>-1001234567890</code>\n"
    "• فوروارد یک پیام از گروه\n\n"
    "اگر گروه خصوصی است و فقط لینک دعوت (+…) دارید، داخل خود گروه دستور <code>/setgroup</code> را بفرستید."
)


async def on_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core.PENDING_GROUP_SET.add(update.effective_user.id)
    await update.effective_message.reply_text(GROUP_PROMPT, parse_mode=ParseMode.HTML)


async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """داخل گروه: ادمین ربات با /setgroup همان گروه را به‌عنوان گروه اصلی ثبت می‌کند (برای گروه‌های خصوصی)."""
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP) or user is None or not core.is_admin(user.id):
        return
    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
    except (BadRequest, Forbidden, TimedOut, NetworkError) as exc:
        await message.reply_text(f"خطا در دسترسی به گروه: {exc}")
        return
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await message.reply_text("اول ربات را در این گروه ادمین کنید، بعد دوباره /setgroup بفرستید.")
        return
    err = await core.set_group_id(chat.id)
    if err:
        await message.reply_text(f"❌ ذخیره در دیتابیس شکست خورد:\n{err[:200]}")
        return
    await message.reply_text("این گروه به‌عنوان گروه اصلی ثبت شد ✅")


# ---------------------------------------------------------------------------
# میانگین جذب
# ---------------------------------------------------------------------------


def _fmt_num(value: float) -> str:
    return f"{value:,.1f}" if abs(value - round(value)) > 0.05 else f"{int(round(value)):,}"


async def build_avg_text(bot) -> str:
    """میانگین جذب؛ اعداد دقیقاً همان عدد نمایشی پنل هستند (ورودی واقعی + تعدیل ممبر)."""
    items = await core.list_all_items()
    lines = ["📈 <b>میانگین جذب ربات</b>", ""]
    if not items:
        lines.append("هنوز هیچ آیتمی ثبت نشده است.")
        return "\n".join(lines)

    counts = await core.fetch_member_counts(bot, items, force=True)
    shown = {it["id"]: max((counts.get(it["id"]) or 0) + core.get_fake_members(it), 0) for it in items}
    total = sum(shown.values())
    finished = [it for it in items if core.item_is_expired(it)]
    active = [it for it in items if not core.item_is_expired(it)]

    lines.append(f"📦 تعداد آیتم‌ها: {len(items):,} (فعال: {len(active):,} | تمام‌شده: {len(finished):,})")
    lines.append(f"👥 مجموع ممبر جذب‌شده: {total:,}")
    lines.append(f"📊 میانگین جذب هر آیتم: <b>{_fmt_num(total / len(items))}</b>")
    if finished:
        fin_total = sum(shown[it["id"]] for it in finished)
        lines.append(f"🏁 میانگین جذب هر آیتمِ تمام‌شده: <b>{_fmt_num(fin_total / len(finished))}</b>")
    if active:
        act_total = sum(shown[it["id"]] for it in active)
        lines.append(f"🟢 میانگین جذب هر آیتمِ فعال: <b>{_fmt_num(act_total / len(active))}</b>")
    best = max(items, key=lambda it: shown[it["id"]])
    lines.append(f"🏆 بهترین آیتم: {html.escape(best['title'])} ({shown[best['id']]:,})")

    # فروش‌ها: زمانِ شروع و پایان مشخص است، پس میانگین ساعتی هم محاسبه می‌شود
    now = datetime.now(timezone.utc)
    rates = []
    sold_counts = []
    for res in sales.RES.values():
        if res["status"] not in ("active", "finished") or res.get("item_id") not in shown:
            continue
        start = core._parse_ts(res.get("activated_at"))
        end = core._parse_ts(res.get("expires_at"))
        if start is None:
            continue
        hours = max(((min(now, end) if end else now) - start).total_seconds() / 3600, 0.05)
        sold_counts.append(shown[res["item_id"]])
        rates.append(shown[res["item_id"]] / hours)
    if sold_counts:
        lines.append("")
        lines.append(f"💰 جوین‌های فروخته‌شده: {len(sold_counts):,}")
        lines.append(f"📊 میانگین جذب هر جوین فروخته‌شده: <b>{_fmt_num(sum(sold_counts) / len(sold_counts))}</b>")
        lines.append(f"🕐 میانگین جذب در ساعت (فروش‌ها): <b>{_fmt_num(sum(rates) / len(rates))}</b>")
    return "\n".join(lines)


async def on_avg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await build_avg_text(context.bot)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# وضعیت فعلی
# ---------------------------------------------------------------------------


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    group_id = await core.get_group_id()
    items = await core.list_items()
    delete_seconds = await core.get_warn_delete_seconds()
    text = (
        f"گروه فعال: <code>{group_id if group_id else 'تنظیم نشده'}</code>\n"
        f"تعداد آیتم‌ها: {len(items)}\n"
        f"زمان حذف پیام هشدار: {delete_seconds:g} ثانیه\n"
        f"💰 فروش جوین اجباری: {'🟢 روشن' if sales.sale_enabled() else '🔴 خاموش'}"
    )
    if sales.sale_enabled():
        open_count = sum(1 for r in sales.RES.values() if r["status"] in sales.OPEN_STATUSES)
        text += f"\nرزروهای باز: {open_count} | لاین روزانه: {sales.CFG['lanes']}"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


ACTIONS = {
    BTN_ITEMS: on_items,
    BTN_SETTINGS: on_settings,
    BTN_GROUP: on_group,
    BTN_AVG: on_avg,
    BTN_STATUS: on_status,
}


async def private_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or update.effective_chat.type != Chat.PRIVATE:
        return

    if not core.is_admin(user.id):
        await sales.buyer_message(update, context)
        return

    action = ACTIONS.get((message.text or "").strip())
    if action is not None:
        clear_admin_pending(user.id)
        await action(update, context)
        return

    if await sales.admin_sale_message(update, context):
        return
    await core.owner_private_message(update, context)


# ---------------------------------------------------------------------------
# منوی تنظیمات (inline)
# ---------------------------------------------------------------------------


async def st_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if not core.is_admin(user_id):
        await query.answer("شما ادمین ربات نیستید.", show_alert=True)
        return
    await query.answer()
    data = query.data
    clear_admin_pending(user_id)

    if data == "st_main":
        await query.edit_message_text("⚙️ تنظیمات:", reply_markup=settings_kb())
    elif data == "st_texts":
        await query.edit_message_text("ویرایش متن‌ها:", reply_markup=core.TEXTS_MENU)
    elif data == "st_timer":
        core.PENDING_DELETE_TIMER_SET.add(user_id)
        current = await core.get_warn_delete_seconds()
        await query.edit_message_text(
            f"زمان فعلی حذف خودکار پیام هشدار جوین اجباری: <b>{current:g} ثانیه</b>\n\n"
            "عدد جدید (ثانیه) را ارسال کنید، مثلاً <code>5</code> یا <code>10</code>.\n"
            "برای غیرفعال‌کردن حذف خودکار، عدد <code>0</code> را ارسال کنید.",
            parse_mode=ParseMode.HTML,
            reply_markup=core.back_kb("st_main"),
        )


# ---------------------------------------------------------------------------
# ثبت هندلرها
# ---------------------------------------------------------------------------


def register(application: Application) -> None:
    add = application.add_handler
    add(CommandHandler("start", start_cmd))
    add(CommandHandler("setgroup", cmd_setgroup, filters=filters.ChatType.GROUPS))
    add(ChatMemberHandler(core.on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    add(ChatMemberHandler(sales.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    add(CallbackQueryHandler(core.on_check_membership, pattern=r"^check_membership(_\d+)?$"))
    add(CallbackQueryHandler(sales.sale_callback, pattern=r"^sale_"))
    add(CallbackQueryHandler(st_callback, pattern=r"^st_"))
    add(CallbackQueryHandler(core.owner_panel_router, pattern="^(menu_|ch_|txt_)"))

    add(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, core.on_group_message))
    add(MessageHandler(filters.ChatType.PRIVATE & filters.ALL, private_router))

    core.STARTUP_HOOKS.append(sales.on_startup)
    core.SHUTDOWN_HOOKS.append(sales.on_shutdown)
    core.BACKGROUND_HOOKS.append(sales.sweep)
