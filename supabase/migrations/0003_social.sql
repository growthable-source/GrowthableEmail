create table social_posts (
    id uuid primary key default gen_random_uuid(),
    thread_ts text,
    account_ids text[] not null default '{}',
    content jsonb not null default '{}',
    schedule_at timestamptz,
    status text not null default 'draft', -- draft|scheduled|published|cancelled
    ghl_post_id text,
    created_at timestamptz not null default now()
);

create table images (
    id uuid primary key default gen_random_uuid(),
    mime text not null default 'image/png',
    bytes bytea not null,
    created_at timestamptz not null default now()
);
