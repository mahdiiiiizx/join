-- یک‌بار در Supabase (SQL Editor) اجرا کنید

alter table channels add column if not exists report_sent boolean not null default false;

-- آیتم‌های قدیمیِ تمام‌شده گزارش تصویری نگیرند:
update channels set report_sent = true where quota_done or (expires_at is not null and expires_at <= now());
