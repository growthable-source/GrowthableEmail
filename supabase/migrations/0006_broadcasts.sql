-- Campaign sends go out via Resend Broadcasts (one shot for the whole audience,
-- up to the plan's contact limit). The per-email queue remains for seed tests
-- and GHL enrollment drips; the daily cap now only governs that queue.
alter table campaigns
    add column send_via text not null default 'queue',  -- queue|broadcast
    add column resend_segment_id text,
    add column resend_import_id text,
    add column resend_broadcast_id text;
create index campaigns_resend_broadcast_idx on campaigns (resend_broadcast_id);

alter table sends add column via text not null default 'queue';  -- queue|broadcast
