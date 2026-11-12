# Data Engineering Job Search

Personal job-search automation for United States, remote, hybrid, and on-site Data Engineering roles from entry through staff level.

The pipeline fetches jobs from direct company pages, public ATS APIs, JSearch, and free job boards; normalizes and deduplicates them; applies Data Engineering relevance and fit-score filters; writes qualified jobs to Supabase; and publishes a Discord/email summary. The authenticated static dashboard reads the same Supabase tables.

## What is included

- Data Engineering taxonomy: platform, warehouse/analytics, ETL/ELT/integration, and distributed/streaming roles.
- Ranking signals for Python, SQL, dbt, Airflow, Dagster, Spark, Kafka, Snowflake, BigQuery, Redshift, Databricks, AWS Glue, and Terraform.
- Rejection of adjacent roles such as Data Entry, Data Scientist, BI Analyst, and DBA.
- Source tiers and source-health circuit breaking to keep the daily run resilient.
- A conservative JSearch cap of three queries per run. At two runs daily, this is about 180 requests/month and fits the 200-request free tier.

## Local setup

Use `uv` for all Python commands.

```bash
# Create .env with the values below; never commit it.
uv run --with-requirements requirements.txt tools/orchestrate.py
```

The repository intentionally contains no credentials. Supply values only in `.env` for a local run and in GitHub Actions secrets for a scheduled run:

```env
JSEARCH_API_KEY=optional_rapidapi_key
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=service_role_key
DISCORD_WEBHOOK_URL=optional_discord_webhook
EMAIL_TO=optional_recipient@example.com
EMAIL_FROM=optional_sender@example.com
EMAIL_APP_PASSWORD=optional_email_app_password
```

Direct company/ATS sources and the free boards do not require API keys. JSearch is optional; disable it in `config/job_search_config.json` if you do not have a key.

## Configuration

`config/job_search_config.json` holds shared runtime settings and activates `data_engineering`. `config/domains/data_engineering.json` holds the search keywords, title families, relevance rules, free-board categories, and display identity.

The main product artifacts are written to `.tmp/`:

- `domain_filtered_jobs.json` — jobs passing the Data Engineering relevance filter.
- `rejected_out_of_domain_jobs.json` — sampled adjacent/non-Data-Engineering roles.
- `qualified_jobs.json` — jobs passing relevance and fit-score gates.
- `run_summary.json` — source counts, filter metrics, and persistence outcome.

## Commands

```bash
# Unit and integration tests without credentials or networked job fetches
uv run --with-requirements requirements.txt --with pytest pytest -q

# Lint
uvx ruff check tools tests

# Inspect one board using the active Data Engineering filter
uv run tools/test_job_board.py --source greenhouse --token example

# Discover additional ATS boards using active-domain keywords
uv run tools/discover_companies.py --ats greenhouse lever --min-relevant-jobs 2
```

## Deployment and shared Supabase data

This tracker will reuse the existing Supabase project only at rollout. Do not clear QA data during development. Immediately before the first Data Engineering deployment, take read-only counts of matched, rejected, applied/dismissed, and source-health rows; request fresh confirmation; then clear only the agreed rows and verify the counts afterwards. Configure the existing Supabase and Discord secrets outside source control, manually validate one run plus the dashboard/Discord output, and only then enable the GitHub Actions schedule.
