-- یک‌بار در Supabase (SQL Editor) اجرا کنید
-- مدت انتخاب‌شده برای جوین اجباری (ثانیه) — برای نوشتن «مدت جوین اجباری : 24 ساعت» در کپشن گزارش پایان

alter table channels add column if not exists duration_seconds bigint;
