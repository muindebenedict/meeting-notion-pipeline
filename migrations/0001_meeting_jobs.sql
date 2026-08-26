-- meeting_jobs: one row per (client, meeting) webhook delivery.
-- Existence of a row is the dedupe lock; status is the outcome.
-- A row is re-claimable — a later redelivery flips it back to 'pending' and
-- runs again — when it is 'failed', or 'pending' and older than the app's
-- STALE_PENDING_AFTER (20 min), meaning its worker died with the process.
-- 'success', and 'pending' inside that window, block the redelivery.

create table if not exists public.meeting_jobs (
    id          bigint generated always as identity primary key,
    client_id   text        not null,
    meeting_id  text        not null,
    status      text        not null default 'pending'
                            check (status in ('pending', 'success', 'failed')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint meeting_jobs_client_meeting_key unique (client_id, meeting_id)
);

-- For "what failed recently?" ops queries. The lookup path in the webhook is
-- already covered by the unique constraint's index.
create index if not exists meeting_jobs_status_idx
    on public.meeting_jobs (status, created_at desc);

-- updated_at is the database's job, so no caller can forget it.
create or replace function public.meeting_jobs_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create or replace trigger meeting_jobs_set_updated_at
    before update on public.meeting_jobs
    for each row execute function public.meeting_jobs_touch_updated_at();

-- No policies: only the service key (what the app uses) can read or write.
alter table public.meeting_jobs enable row level security;
