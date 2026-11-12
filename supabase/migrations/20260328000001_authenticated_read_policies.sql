-- Allow authenticated users to read the job listing tables.
-- These tables previously only had anon SELECT policies, which blocked
-- signed-in users from fetching any rows.

create policy "Authenticated users can read matched jobs"
  on matched_jobs for select to authenticated using (true);

create policy "Authenticated users can read rejected jobs"
  on rejected_jobs for select to authenticated using (true);

create policy "Authenticated users can read source health"
  on source_health_state for select to authenticated using (true);
