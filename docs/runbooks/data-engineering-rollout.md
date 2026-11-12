# Data Engineering Tracker Rollout

## Purpose

Switch the existing shared Supabase-backed tracker from retired QA jobs to Data Engineering jobs without exposing credentials or deleting data accidentally.

## Preconditions

- Local code verification is complete.
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and the optional Discord/email values are present only in local `.env` or GitHub Actions secrets.
- No credentials are committed to this project.
- A human has reviewed the Data Engineering taxonomy in `config/domains/data_engineering.json`.

## Manual validation before deployment

1. Add the approved secrets to a local `.env` file.
2. Run one manual pipeline execution:

   ```bash
   uv run --with-requirements requirements.txt tools/orchestrate.py
   ```

3. Inspect `.tmp/run_summary.json`, `.tmp/domain_filtered_jobs.json`, `.tmp/rejected_out_of_domain_jobs.json`, and `.tmp/qualified_jobs.json`.
4. Check the dashboard displays Data Engineering roles and the configured Discord/email notification uses the Data Engineering title.
5. Do not enable the scheduled GitHub Actions workflow until this manual validation is satisfactory.

## Shared Supabase cleanup gate

This is a destructive, separately authorized operation. Do not perform it during development or as part of a code deployment.

1. Take read-only counts of every QA-era table/state to be removed: `matched_jobs`, `rejected_jobs`, applied-job state, dismissed-job state, and source-health state.
2. Present the exact table names and counts to the owner.
3. Request fresh explicit confirmation that names the agreed targets and authorizes their deletion.
4. After confirmation, perform only the approved cleanup using the Supabase project credentials outside source control.
5. Re-run the read-only counts and record zero QA-era rows for each approved target.

## Schedule enablement

1. Add the approved secrets to GitHub Actions.
2. Trigger `Daily Data Engineering Job Search` manually once from GitHub Actions.
3. Review its artifacts and the Supabase/dashboard/notification results.
4. Enable the normal schedule only after that manual run is accepted.
