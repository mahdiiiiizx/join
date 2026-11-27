-- ساخت کامل جدول‌های دیتابیس (Supabase → SQL Editor). چند بار اجرا کردنش بی‌خطر است.
-- هیچ جدول/ستون/ردیف قبلی تغییر یا حذف نمی‌شود؛ آیتم‌های فعلی جوین اجباری دست‌نخورده می‌مانند.

create table if not exists settings (key text primary key, value text);
create table if not exists texts (key text primary key, value text);

create table if not exists channels (
    id bigint generated always as identity primary key,
    kind text not null default 'channel',
    chat_id text not null unique,
    title text not null,
    invite_link text not null,
    expires_at timestamptz,
    start_members bigint,
    member_limit bigint,
    quota_done boolean not null default false,
    peak_members bigint not null default 0,
    fake_members bigint not null default 0,
    report_sent boolean not null default false
);
alter table channels add column if not exists expires_at timestamptz;
alter table channels add column if not exists start_members bigint;
alter table channels add column if not exists member_limit bigint;
alter table channels add column if not exists quota_done boolean not null default false;
alter table channels add column if not exists peak_members bigint not null default 0;
alter table channels add column if not exists fake_members bigint not null default 0;
alter table channels add column if not exists report_sent boolean not null default false;

create table if not exists bot_starts (
    bot_username text not null,
    user_id bigint not null,
    started_at timestamptz not null default now(),
    primary key (bot_username, user_id)
);

-- ===================== فروش خودکار جوین اجباری (جدید) =====================

create table if not exists sale_users (
    user_id bigint primary key,
    first_name text,
    last_name text,
    username text,
    started_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now()
);

create table if not exists sale_reservations (
    id bigint primary key,
    user_id bigint not null,
    day date not null,
    hours integer not null,
    amount bigint not null,
    status text not null default 'awaiting_payment',
    chat_id text,
    chat_kind text,
    chat_title text,
    item_id bigint,
    payment_id bigint,
    hold_expires_at timestamptz,
    approved_at timestamptz,
    bound_at timestamptz,
    activated_at timestamptz,
    expires_at timestamptz,
    finished_at timestamptz,
    report_sent boolean not null default false,
    created_at timestamptz not null default now()
);
create index if not exists sale_res_day on sale_reservations (day, status);
create index if not exists sale_res_user on sale_reservations (user_id, created_at desc);

create table if not exists sale_payments (
    id bigint primary key,
    reservation_id bigint not null,
    user_id bigint not null,
    amount bigint not null,
    hours integer not null,
    receipt_type text,
    receipt_file_id text,
    receipt_text text,
    receipt_chat_id bigint,
    receipt_message_id bigint,
    status text not null default 'pending',
    created_at timestamptz not null default now(),
    reviewed_at timestamptz,
    reviewed_by bigint
);
create index if not exists sale_pay_res on sale_payments (reservation_id);
