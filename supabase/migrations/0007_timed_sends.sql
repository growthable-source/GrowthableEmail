-- Timezone-targeted ramped sends: contacts carry country/timezone from GHL,
-- campaigns can carry a per-day / per-hour ramp (set conversationally via the
-- bot at approval), and each queued send knows the timezone its ideal-local-time
-- window was computed in.
alter table contacts_cache
    add column country text not null default '',
    add column timezone text not null default '';

alter table campaigns
    add column per_day int,   -- null = uncapped (global DAILY_SEND_CAP applies)
    add column per_hour int;  -- null = no hourly limit

alter table sends add column timezone text not null default '';
create index sends_campaign_sent_at_idx on sends (campaign_id, sent_at);
