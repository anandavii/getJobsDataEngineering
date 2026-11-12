create table applied_jobs (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  job_url    text not null,
  applied_at timestamptz not null default now(),
  unique (user_id, job_url)
);

alter table applied_jobs enable row level security;

create policy "Users can read own applied jobs"
  on applied_jobs for select using (auth.uid() = user_id);

create policy "Users can insert own applied jobs"
  on applied_jobs for insert with check (auth.uid() = user_id);

create policy "Users can delete own applied jobs"
  on applied_jobs for delete using (auth.uid() = user_id);
