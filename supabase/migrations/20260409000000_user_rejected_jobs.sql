-- supabase/migrations/20260409000000_user_rejected_jobs.sql

create table user_rejected_jobs (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  job_url     text not null,
  rejected_at timestamptz not null default now(),
  unique (user_id, job_url)
);

alter table user_rejected_jobs enable row level security;

create policy "Users can read own rejected jobs"
  on user_rejected_jobs for select
  using (auth.uid() = user_id);

create policy "Users can insert own rejected jobs"
  on user_rejected_jobs for insert
  with check (auth.uid() = user_id);
