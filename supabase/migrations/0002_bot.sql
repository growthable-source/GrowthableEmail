alter table campaigns add column content jsonb not null default '{}';
alter table campaigns add column seed_tested_at timestamptz;

create table bot_sessions (
    thread_ts text primary key,
    channel text not null,
    campaign_id uuid references campaigns(id),
    messages jsonb not null default '[]',
    updated_at timestamptz not null default now()
);

create table slack_events (
    event_id text primary key,
    created_at timestamptz not null default now()
);
