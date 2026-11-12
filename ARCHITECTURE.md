# Data Engineering Job Search Architecture

```text
Company careers / ATS APIs / JSearch / free boards
  -> canonical job records
  -> URL deduplication
  -> United States + remote/hybrid/on-site + recency filters
  -> Data Engineering relevance filter
  -> taxonomy classification and fit scoring
  -> Supabase matched/rejected tables
  -> authenticated static dashboard + Discord/email notification
```

## Pipeline

`tools/orchestrate.py` is the only scheduled entry point. It loads the active configuration, generates a capped set of search queries, executes sources in reliability tiers, and records source health. If a source repeatedly fails, the circuit breaker skips it temporarily rather than failing the whole job search.

All adapters return the same canonical shape: title, company, location, URL, date, description snippet, source, and optional salary. The orchestrator then deduplicates by normalized URL, filters for the configured geography and posting window, and calls `tools/domain_relevance.py`.

The active domain is `data_engineering`. Its configuration lives in `config/domains/data_engineering.json` and defines role families, title variants, skills, filtering patterns, and free-board categories. `tools/role_horizon.py` derives `role_family`, matched titles/skills, seniority, and fit score from that configuration; it has no QA fallback taxonomy.

## Storage and notifications

Qualified jobs are upserted to `matched_jobs`; out-of-domain and low-fit jobs are recorded in `rejected_jobs`. Existing Supabase schema and dashboard behavior are deliberately preserved. The dashboard uses the public Supabase URL plus anon key; the service-role key is for the pipeline only and must never enter the static frontend.

`tools/notify_webhook.py` builds Discord and email summaries using the configured domain display name. It reports persistence counts and source-health problems but does not make a failed notification fatal to the pipeline.

## Operations

The workflow in `.github/workflows/daily_job_search.yml` runs the orchestrator twice daily and can be triggered manually. Its secrets are `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and optionally `JSEARCH_API_KEY`, `DISCORD_WEBHOOK_URL`, `EMAIL_TO`, `EMAIL_FROM`, and `EMAIL_APP_PASSWORD`.

Before switching the shared Supabase project from QA to Data Engineering, take read-only counts and obtain fresh confirmation for the destructive cleanup. That operation is intentionally separate from application code and is never performed automatically.
