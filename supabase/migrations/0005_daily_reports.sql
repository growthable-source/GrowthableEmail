create table daily_reports (
    report_type text primary key, -- 'email' | 'social'
    last_sent_date date not null
);
