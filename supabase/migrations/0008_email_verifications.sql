-- Mailbox-verification verdict cache (spec: docs/superpowers/specs/2026-07-18-list-cleaning-design.md).
-- Separate from suppressions, which stays reserved for bounces/complaints/unsubs.
create table email_verifications (
    email        text primary key,
    verdict      text not null check (verdict in ('valid', 'invalid', 'risky', 'unknown')),
    reason       text,
    provider     text not null default 'emailable',
    verified_at  timestamptz not null default now()
);
