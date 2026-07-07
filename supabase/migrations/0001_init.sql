create table campaigns (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    subject text not null,
    template_ref text not null,
    template_version text not null,
    audience_filter jsonb not null default '[]',
    status text not null default 'draft', -- draft|ready|dispatching|paused|completed
    scheduled_at timestamptz,
    created_at timestamptz not null default now()
);

create table contacts_cache (
    ghl_contact_id text primary key,
    email text not null,
    first_name text not null default '',
    last_name text not null default '',
    custom jsonb not null default '{}',
    tags text[] not null default '{}',
    dnd boolean not null default false,
    synced_at timestamptz not null default now()
);

create table campaign_contacts (
    campaign_id uuid not null references campaigns(id) on delete cascade,
    ghl_contact_id text not null,
    primary key (campaign_id, ghl_contact_id)
);

create table sends (
    id uuid primary key default gen_random_uuid(),
    campaign_id uuid not null references campaigns(id),
    ghl_contact_id text not null,
    email text not null,
    resend_email_id text,
    status text not null default 'queued', -- queued|sending|sent|failed|suppressed
    error text,
    rendered_hash text,
    retry_count int not null default 0,
    next_attempt_at timestamptz not null default now(),
    sent_at timestamptz,
    created_at timestamptz not null default now(),
    unique (campaign_id, ghl_contact_id)
);
create index sends_campaign_status_idx on sends (campaign_id, status);
create index sends_queue_idx on sends (status, next_attempt_at);
create index sends_resend_email_id_idx on sends (resend_email_id);
create index sends_sent_at_idx on sends (sent_at);

create table events (
    id uuid primary key default gen_random_uuid(),
    send_id uuid references sends(id),
    type text not null,
    payload jsonb not null default '{}',
    occurred_at timestamptz not null default now()
);
create index events_send_idx on events (send_id);
create index events_type_time_idx on events (type, occurred_at);

create table suppressions (
    email text primary key,
    ghl_contact_id text,
    reason text not null, -- hard_bounce|complaint|unsubscribe|ghl_dnd
    source text not null, -- resend|ghl|unsub_page|manual
    created_at timestamptz not null default now()
);

create table jobs (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    data jsonb not null default '{}',
    state text not null default 'created', -- created|active|completed|failed
    retry_count int not null default 0,
    retry_limit int not null default 3,
    start_after timestamptz not null default now(),
    created_at timestamptz not null default now(),
    completed_at timestamptz
);
create index jobs_fetch_idx on jobs (name, state, start_after);
