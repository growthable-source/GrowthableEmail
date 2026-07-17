-- Inbound replies (Resend receiving) + sending-domain pool for the
-- Xovera outbound engine.

create table replies (
  id               uuid primary key default gen_random_uuid(),
  resend_email_id  text unique not null,     -- received-email id (body via API)
  from_email       text not null,
  to_email         text,
  subject          text,
  body_text        text,                     -- filled by the classify job
  classification   text,                     -- interested|not_interested|unsubscribe|ooo|auto_reply|other
  summary          text,
  send_id          uuid references sends(id),
  campaign_id      uuid references campaigns(id),
  processed        boolean not null default false,
  created_at       timestamptz not null default now()
);
create index idx_replies_created on replies (created_at desc);

create table sending_domains (
  id           uuid primary key default gen_random_uuid(),
  domain       text unique not null,          -- e.g. mail.tryxovera.com
  from_user    text not null default 'ryan',
  from_name    text not null default 'Ryan at Xovera',
  daily_cap    integer not null default 30,
  max_cap      integer not null default 250,
  active       boolean not null default true,
  paused_reason text,
  created_at   timestamptz not null default now()
);

alter table sends add column from_domain text;
create index idx_sends_from_domain on sends (from_domain, sent_at);
