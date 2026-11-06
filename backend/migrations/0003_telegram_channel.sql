alter type channel_t add value if not exists 'telegram';
-- Where to deliver the result. Telegram chat ids only (not phone numbers); removed with the customer on delete.
alter table generation_jobs add column if not exists reply_ref text;
