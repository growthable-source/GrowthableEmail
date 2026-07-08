-- Origin channel/thread so the worker can @channel-ping when something actually
-- goes out, even for campaigns/posts whose bot_sessions link has since moved on.
alter table campaigns add column thread_ts text;
alter table campaigns add column channel text;

alter table social_posts add column channel text;
alter table social_posts add column notified_at timestamptz;
