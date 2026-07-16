-- Per-send personalized content for the Xovera outbound engine. Its emails
-- carry a unique Sonnet-written subject + body per prospect, so template
-- campaigns can't render them; these stay null for every other send.
alter table sends
    add column subject_override text,
    add column content_override jsonb;
