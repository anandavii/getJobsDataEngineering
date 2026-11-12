# Daily QA Job Search Automation — Implementation Plan

## Context
Automated system that runs every day at 8 AM ET, searches for QA/automation engineering jobs in the USA, deduplicates results, stores them in Airtable, and sends a Slack/Discord notification. Greenfield build on the WAT framework.

---

## Architecture

```
GitHub Actions (cron: 0 13 * * *)
         ↓
tools/orchestrate.py  ← reads config/job_search_config.json
         ↓ (parallel intent, sequential execution)
  ├── tools/fetch_jobs_jsearch.py          → .tmp/jsearch_jobs.json
  ├── tools/fetch_jobs_indeed_rss.py       → .tmp/indeed_jobs.json
  ├── tools/fetch_jobs_dice.py             → .tmp/dice_jobs.json
  └── tools/fetch_jobs_company_careers.py  → .tmp/company_jobs.json
         ↓
  [merge + deduplicate by job_url]
         ↓
  tools/push_to_airtable.py               → .tmp/airtable_push_result.json
         ↓
  tools/notify_webhook.py                 → Slack or Discord
```

---

## Files to Create (11 total)

| # | File | Purpose |
|---|------|---------|
| 1 | `requirements.txt` | Python deps |
| 2 | `config/job_search_config.json` | All runtime config — keywords, companies, thresholds |
| 3 | `tools/fetch_jobs_jsearch.py` | JSearch RapidAPI (primary aggregator) |
| 4 | `tools/fetch_jobs_indeed_rss.py` | Indeed RSS feed parser |
| 5 | `tools/fetch_jobs_dice.py` | Dice.com scraper via `__NEXT_DATA__` JSON |
| 6 | `tools/fetch_jobs_company_careers.py` | FAANG + configurable company career pages |
| 7 | `tools/push_to_airtable.py` | Airtable upsert + old record cleanup |
| 8 | `tools/notify_webhook.py` | Slack / Discord webhook notification |
| 9 | `tools/orchestrate.py` | Main entry point — wires all tools |
| 10 | `.github/workflows/daily_job_search.yml` | GitHub Actions cron trigger |
| 11 | `workflows/daily_job_search.md` | SOP / documentation |

---

## Canonical Job Schema (shared across all tools)

```json
{
  "job_title": "string",
  "company": "string",
  "location": "string",
  "job_type": "Remote|Hybrid|On-site|Unknown",
  "job_url": "string (dedup key)",
  "description_snippet": "string (first 500 chars)",
  "date_posted": "ISO 8601 date",
  "source": "jsearch|indeed|dice|company_careers",
  "salary_min": "number|null",
  "salary_max": "number|null",
  "salary_currency": "USD|null"
}
```

---

## Airtable Table Schema

| Field | Type | Notes |
|-------|------|-------|
| Job Title | Single line text | Primary field |
| Company | Single line text | |
| Location | Single line text | |
| Job Type | Single select | Remote, Hybrid, On-site, Unknown |
| Job URL | URL | **Upsert key** — deduplication |
| Source | Single select | jsearch, indeed, dice, company_careers |
| Date Posted | Date | From the job listing |
| Date Found | Date | Auto-set on creation |
| Description Snippet | Long text | |
| Salary Min | Number | Nullable |
| Salary Max | Number | Nullable |
| Status | Single select | New, Applied, Interviewing, Rejected, Offer |
| Notes | Long text | User-only, automation never writes here |
| Applied Date | Date | User-only |

---

## Job Sources & How Each is Fetched

### 1. JSearch API (primary — covers LinkedIn, Indeed, Glassdoor, ZipRecruiter)
- Endpoint: `GET https://jsearch.p.rapidapi.com/search`
- Auth: `X-RapidAPI-Key` header
- Params: `query`, `date_posted=today`, `num_pages=1`
- Run only 4 keywords/day → 120 req/month (under 500 free limit)

### 2. Indeed RSS (no API key)
- URL: `https://www.indeed.com/rss?q={keyword}&l=United+States&limit=25`
- Parse with stdlib `xml.etree.ElementTree`
- Extract company from title pattern `"Job Title - Company"` using `rsplit(' - ', 1)`
- Add `time.sleep(2)` between keyword requests

### 3. Dice.com (scrape)
- URL: `https://www.dice.com/jobs?q={keyword}&location=United+States&datePosted=ONE`
- Extract JSON from `<script id="__NEXT_DATA__">` tag — more reliable than CSS selectors
- Path: `props.pageProps.initialState.jobSearch.searchResults` (fallback: HTML parsing)
- Note: May return 403 on GitHub Actions IPs — handled gracefully, not fatal

### 4. Company Career Pages (FAANG + configurable)

| Company | API Type | Endpoint |
|---------|----------|----------|
| Google | Custom JSON | `https://careers.google.com/api/v3/search/?location=United+States&q={keyword}` |
| Meta | Greenhouse public | `https://boards-api.greenhouse.io/v1/boards/meta/jobs?content=true` (filter by keyword in Python) |
| Amazon | Custom JSON | `https://www.amazon.jobs/en/search.json?base_query={keyword}&loc_query=United+States` |
| Apple | Custom JSON | `https://jobs.apple.com/api/role/search?filters=...keywords%3D{keyword}` |
| Microsoft | Custom JSON | `https://gcsservices.careers.microsoft.com/search/api/v1/search?q={keyword}&lc=United+States` |
| Netflix | Lever public | `https://api.lever.co/v0/postings/netflix?mode=json` (filter by keyword in Python) |

Config supports adding more companies via `ats` type: `greenhouse`, `lever`, or `custom` with `{keyword}` placeholder in URL.

---

## Deduplication Strategy
- Primary key: `job_url` (lowercase normalized)
- Step 1: Deduplicate within each source before writing `.tmp/` files
- Step 2: Merge all sources and deduplicate again by `job_url`
- Step 3: `pyairtable`'s `batch_upsert(key_fields=["Job URL"])` handles Airtable-level dedup

---

## Orchestrator Design (`tools/orchestrate.py`)

```python
def safe_fetch(source_name, fn, *args) -> list[dict]:
    """Wraps fetch call — returns [] on any exception, never aborts the run."""
    try:
        result = fn(*args)
        log.info(f"{source_name}: {len(result)} jobs")
        return result
    except Exception as e:
        log.error(f"{source_name}: FAILED — {e}")
        return []

# Execution order:
jsearch_jobs  = safe_fetch("jsearch",  fetch_jsearch.run,  config, api_key)
indeed_jobs   = safe_fetch("indeed",   fetch_indeed.run,   config)
dice_jobs     = safe_fetch("dice",     fetch_dice.run,     config)
company_jobs  = safe_fetch("company",  fetch_company.run,  config)
all_jobs      = merge_and_deduplicate([jsearch_jobs, indeed_jobs, dice_jobs, company_jobs])
push_result   = push_to_airtable.run(all_jobs, ...)
notify_webhook.run(webhook_url, push_result, new_jobs, config)
```

---

## GitHub Actions Workflow

```yaml
on:
  schedule:
    - cron: '0 13 * * *'   # 08:00 ET = 13:00 UTC
  workflow_dispatch:        # manual trigger for testing

jobs:
  fetch-jobs:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11', cache: 'pip' }
      - run: pip install -r requirements.txt
      - run: mkdir -p .tmp
      - run: python tools/orchestrate.py
        env:
          JSEARCH_API_KEY:  ${{ secrets.JSEARCH_API_KEY }}
          AIRTABLE_API_KEY: ${{ secrets.AIRTABLE_API_KEY }}
          AIRTABLE_BASE_ID: ${{ secrets.AIRTABLE_BASE_ID }}
          WEBHOOK_URL:      ${{ secrets.WEBHOOK_URL }}
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: "run-${{ github.run_id }}", path: .tmp/, retention-days: 7 }
```

---

## Python Dependencies

```
requests==2.31.0
beautifulsoup4==4.12.3
lxml==5.1.0
pyairtable==2.3.1
python-dotenv==1.0.0
```
No Playwright/Selenium — all scraping uses `requests` + `beautifulsoup4`.

---

## GitHub Secrets Required
| Secret | Source |
|--------|--------|
| `JSEARCH_API_KEY` | RapidAPI dashboard after subscribing to JSearch (free tier) |
| `AIRTABLE_API_KEY` | Airtable → Account → Personal Access Tokens |
| `AIRTABLE_BASE_ID` | From the Airtable base URL (starts with `app`) |
| `WEBHOOK_URL` | Slack app incoming webhook URL or Discord channel webhook URL |

---

## Free Tier Usage Estimates
| Service | Limit | Our Usage | Buffer |
|---------|-------|-----------|--------|
| JSearch (RapidAPI) | 500 req/month | ~120 req/month | 76% remaining |
| Airtable | 1,000 records | ~200-400 at any time | Auto-cleanup at 60 days |
| GitHub Actions | 2,000 min/month (private) | ~90-150 min/month | 93% remaining |
| Indeed RSS | Free, no limit | Unlimited | — |
| Dice.com | Free scraping | Unlimited | May block GH IPs |
| Slack/Discord webhooks | Free | Unlimited | — |

---

## Implementation Sequence
1. `requirements.txt` — install and verify locally
2. `config/job_search_config.json` — full config with keywords, FAANG companies
3. `tools/fetch_jobs_jsearch.py` — test with one keyword + real API key
4. `tools/fetch_jobs_indeed_rss.py` — test RSS parse locally
5. `tools/fetch_jobs_dice.py` — inspect `__NEXT_DATA__` in browser devtools first to confirm paths
6. `tools/fetch_jobs_company_careers.py` — test each company endpoint individually
7. Set up Airtable base + table manually (schema above), get credentials
8. `tools/push_to_airtable.py` — test upsert with sample data
9. `tools/notify_webhook.py` — test with a throwaway Discord/Slack webhook
10. `tools/orchestrate.py` — full local test run
11. Initialize GitHub repo, add Secrets, push code
12. `.github/workflows/daily_job_search.yml` — test with `workflow_dispatch`
13. `workflows/daily_job_search.md` — finalize SOP with any learnings

---

## Known Risks / Mitigations
- **Dice.com 403 from GitHub Actions IPs** — non-fatal; JSearch already covers many Dice jobs
- **Indeed RSS returning empty/403** — non-fatal; JSearch covers Indeed anyway
- **FAANG API endpoint changes** — each company wrapped in try/except; `.get()` used everywhere; raw response logged on failure for debugging
- **Airtable capacity** — cleanup runs before each insert; hard stop at 1,000 records with warning logged and included in notification
- **JSearch deduplication** — A job may match multiple keywords; deduplication by `job_url` before Airtable push handles this

---

## Verification
1. **Local test**: Run `python tools/orchestrate.py` locally with `.env` containing test credentials. Check `.tmp/run_summary.json` for source counts and errors.
2. **Airtable**: Open the base — verify records appear with correct fields and deduplication works on second run.
3. **Notification**: Verify Slack/Discord message is received with job count and links.
4. **GitHub Actions**: Trigger with `workflow_dispatch`, check Actions tab for logs and `.tmp/` artifact download.
5. **Cron**: Confirm it fires at 8 AM ET the next day.
