# Debug Plan: Greenhouse, Lever & Ashby Not Fetching + API Call Audit

**Date:** 2026-02-28
**Status:** Diagnosis complete, fixes ready to implement

---

## Root Cause Analysis

### Bug 1 — Catalog adapters never run (`enabled: false` root issue)

**File:** [config/job_search_config.json](../config/job_search_config.json) (lines 221–239)
**File:** [tools/orchestrate.py](../tools/orchestrate.py) (lines 418–447)

The `sources_catalog` block in config has:
- `greenhouse`: one entry — `"name": "Meta"`, **`"enabled": false`**
- `lever`: one entry — `"name": "Netflix"`, **`"enabled": false`**
- `ashby`: **empty array `[]`**

In `_fetch_plan()` (orchestrate.py:418–422):
```python
has_catalog_entries = {
    key: any(bool(x.get("enabled", True)) for x in val)
    for key, val in catalog.items()
    if isinstance(val, list)
}
```
- `greenhouse` → `any([False])` = **False**
- `lever` → `any([False])` = **False**
- `ashby` → `any([])` = **False**

Then at lines 442–447:
```python
if has_catalog_entries.get("greenhouse"):  # evaluates False → skipped
    plan["greenhouse"] = ...
if has_catalog_entries.get("lever"):       # evaluates False → skipped
    plan["lever"] = ...
if has_catalog_entries.get("ashby"):       # evaluates False → skipped
    plan["ashby"] = ...
```

**These three adapters never appear in the fetch plan and never execute.**

Why were Meta and Netflix disabled in the catalog? Because both are already fetched
via `fetch_jobs_company_careers.py` (`_fetch_meta()` and `_fetch_netflix()`), so disabling
them in the catalog prevents duplication. The problem is that no *other* companies were
added with `enabled: true`.

---

### Bug 2 — Redundant API calls: O(keywords) instead of O(1)

**File:** [tools/fetch_jobs_company_careers.py](../tools/fetch_jobs_company_careers.py) (lines 543–581)

`company_careers` loops: `for company in companies: for keyword in keywords:`.

With query generation enabled, `effective_config` has **up to 60 generated keywords**.
For Meta and Netflix, each call fetches ALL jobs from the endpoint and filters client-side,
so the same endpoint is hit **60 times per run** for each static company:

| Company  | Endpoint type    | Calls/run (60 keywords) |
|----------|-----------------|------------------------|
| Meta     | Greenhouse (static, all jobs) | 60 identical GETs |
| Amazon   | Keyword search API | 60 unique GETs (intentional) |
| Apple    | Keyword HTML scrape | 60 unique GETs (intentional) |
| Netflix  | Lever (static, all jobs) | 60 identical GETs |

**Total company_careers calls: ~240/run** (120 of which are redundant duplicate calls)

**Total JSearch calls: 60/run** (one per generated keyword, $0.01–0.05 each on paid plans)

---

### Bug 3 — Calendar filter drops most catalog/company jobs

**File:** [config/job_search_config.json](../config/job_search_config.json) (lines 129–134)

```json
"calendar_day_filter": {
  "mode": "today",
  "drop_if_missing_date": true
}
```

Combined effect:
1. Only jobs posted **today** survive
2. Jobs with no `date_posted` field are **dropped**

Greenhouse catalog jobs report `updated_at` (last edited, not necessarily today).
Netflix/Lever jobs use `createdAt` epoch ms — old postings will all be dropped.
Result from last run: **59 company_careers raw → 6 total_unique → 1 after QA** — the date
filter is eliminating ~90% of otherwise valid jobs.

---

### Bug 4 — Greenhouse API: missing `?content=true` awareness

**Official Greenhouse Boards API docs:**
`GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs`

Key parameters:
- `?content=true` — includes `content` field (HTML job description). Without it, `content` is absent.
- No pagination on the public Boards API — all active jobs are returned in a single response.
- The `absolute_url` field is the canonical job application URL.
- `updated_at` is the last-modified timestamp (ISO 8601), not posting date.

The catalog adapter at [tools/fetch_jobs_catalog_adapters.py:172](../tools/fetch_jobs_catalog_adapters.py#L172)
correctly appends `?content=true`. This is good.

**However:** `updated_at` is used as `date_posted`. For active, long-standing roles this may
be months old, causing the `calendar_day_filter: today` to drop them. The `created_at` field
(also available in the Greenhouse API response) is the original posting date — also potentially
old. Neither field reliably reflects "posted today."

---

## API Call Budget per Run (Current State)

| Source          | Calls per run | Notes |
|-----------------|--------------|-------|
| JSearch         | 60           | 1 per generated keyword |
| company_careers – Amazon | 60 | 1 per keyword |
| company_careers – Apple  | 60 | 1 per keyword |
| company_careers – Meta   | 60 | **Redundant** — same endpoint each time |
| company_careers – Netflix | 60 | **Redundant** — same endpoint each time |
| Greenhouse catalog | 0 | **Never runs** (bug 1) |
| Lever catalog      | 0 | **Never runs** (bug 1) |
| Ashby catalog      | 0 | **Never runs** (bug 1) |
| **Total**          | **240** | |

---

## Fix Plan

### Fix 1 — Populate `sources_catalog` with real companies

Add enabled Greenhouse, Lever, and Ashby entries to `config/job_search_config.json`.
These companies use these ATS platforms and have active tech hiring:

**Greenhouse companies** (board tokens are the subdomain on `boards.greenhouse.io/<token>`):
```json
{ "name": "Stripe",      "board_token": "stripe",      "enabled": true },
{ "name": "Airbnb",      "board_token": "airbnb",      "enabled": true },
{ "name": "Lyft",        "board_token": "lyft",        "enabled": true },
{ "name": "Cloudflare",  "board_token": "cloudflare",  "enabled": true },
{ "name": "Twilio",      "board_token": "twilio",      "enabled": true },
{ "name": "Asana",       "board_token": "asana",       "enabled": true },
{ "name": "Coinbase",    "board_token": "coinbase",    "enabled": true },
{ "name": "Robinhood",   "board_token": "robinhood",   "enabled": true }
```

**Lever companies** (`api.lever.co/v0/postings/<token>?mode=json`):
```json
{ "name": "Dropbox",     "board_token": "dropbox",     "enabled": true },
{ "name": "Atlassian",   "board_token": "atlassian",   "enabled": true },
{ "name": "Reddit",      "board_token": "reddit",      "enabled": true },
{ "name": "Figma",       "board_token": "figma",       "enabled": true }
```

**Ashby companies** (`organizationHostedJobsPageName` slug):
```json
{ "name": "Linear",      "organization": "linear",     "enabled": true },
{ "name": "Vercel",      "organization": "vercel",     "enabled": true },
{ "name": "Retool",      "organization": "retool",     "enabled": true }
```

To find the correct board token for any company:
- Greenhouse: visit `https://boards.greenhouse.io/<company-name>` — the slug is the token
- Lever: visit `https://jobs.lever.co/<company-name>` — the slug is the token
- Ashby: visit `https://jobs.ashbyhq.com/<company-name>` — the slug is the org name

### Fix 2 — Deduplicate static API calls in company_careers

**File:** [tools/fetch_jobs_company_careers.py](../tools/fetch_jobs_company_careers.py)

For `_fetch_meta` and `_fetch_netflix`, the endpoint is static (returns all jobs, no keyword
in the URL). These should be **fetched once**, then filtered across all keywords in memory.

Pattern to apply:
```python
# Fetch once outside the keyword loop
meta_all_jobs_cache = None

def _fetch_meta_cached(keyword: str) -> list[dict]:
    global meta_all_jobs_cache
    if meta_all_jobs_cache is None:
        meta_all_jobs_cache = _fetch_meta_all()  # single HTTP call
    return [j for j in meta_all_jobs_cache if _contains_keyword(j["job_title"] + " " + j["description_snippet"], keyword)]
```

This reduces Meta from 60 calls → 1 call, Netflix from 60 calls → 1 call.

**Simpler fix:** Move static-endpoint companies (Meta, Netflix) out of the keyword loop
into a one-time "bulk fetch + multi-keyword filter" step before the keyword loop.

### Fix 3 — Relax the calendar filter

**File:** [config/job_search_config.json](../config/job_search_config.json)

Change:
```json
"calendar_day_filter": {
  "mode": "today",
  "drop_if_missing_date": true
}
```

To:
```json
"calendar_day_filter": {
  "mode": "last_days",
  "days": 3,
  "timezone": "America/New_York",
  "drop_if_missing_date": false
}
```

Rationale:
- `last_days: 3` — catches jobs that Greenhouse/Lever have as `updated_at` from yesterday/day-before
- `drop_if_missing_date: false` — keeps jobs with no date rather than throwing them away (catalog jobs often have no "today" date)

### Fix 4 — Greenhouse API correctness (minor)

The catalog adapter correctly uses `?content=true` and reads `job.get("content")` for
description. The official API field names are correct as implemented.

One improvement: use `created_at` as a fallback before `updated_at` for date_posted,
since `created_at` represents when the job was first listed:

```python
date_posted=job.get("updated_at") or job.get("created_at"),
```
This is already implemented correctly in [tools/fetch_jobs_catalog_adapters.py:196](../tools/fetch_jobs_catalog_adapters.py#L196).

---

## Free Job Search APIs to Add

The following are free, require no credit card, and are well-suited for QA role searches:

### Tier A — No API key required

| API | URL | Coverage | QA Filter |
|-----|-----|----------|-----------|
| **Remotive** | `https://remotive.io/api/remote-jobs` | Remote-first tech jobs globally | `?category=qa-testing&search=qa` |
| **Jobicy** | `https://jobicy.com/api/v2/remote-jobs` | Remote tech jobs | `?tag=qa&count=50` |
| **Arbeitnow** | `https://arbeitnow.com/api/job-board-api` | Global tech, has US remote | Filter by tag client-side |
| **Himalayas** | `https://himalayas.app/api/jobs` | Remote-only tech | `?q=qa+automation` |

### Tier B — Free API key (no billing required)

| API | Registration | Free quota | Notes |
|-----|-------------|-----------|-------|
| **Adzuna** | `developer.adzuna.com` | 10,000 calls/month | Best breadth; searches like Google for jobs |
| **The Muse** | `www.themuse.com/developers` | Unlimited read | Limited QA postings |
| **FindWork** | `findwork.dev` | 1,000 calls/month | Developer-focused, often has SDET/QA roles |

### Recommended additions for this project

1. **Remotive** — Zero-config, good QA/automation category, returns JSON with `date_published`.
   Endpoint: `https://remotive.io/api/remote-jobs?category=qa-testing&search=automation`

2. **Jobicy** — Zero-config, `date_published` available (ISO format), tag filtering.
   Endpoint: `https://jobicy.com/api/v2/remote-jobs?tag=qa+automation&count=50`

3. **Adzuna** — Requires free API key. Covers non-remote US jobs (supplements the remote-only
   free APIs). Endpoint: `https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=...&app_key=...&what=qa+automation+engineer`

---

## Implementation Checklist

- [ ] **Fix 1a:** Add 8 Greenhouse companies to `sources_catalog.greenhouse` in config
- [ ] **Fix 1b:** Add 4 Lever companies to `sources_catalog.lever` in config
- [ ] **Fix 1c:** Add 3 Ashby companies to `sources_catalog.ashby` in config
- [ ] **Fix 2:** Deduplicate static-endpoint calls (Meta, Netflix) in `fetch_jobs_company_careers.py`
- [ ] **Fix 3:** Change `calendar_day_filter` to `last_days: 3` + `drop_if_missing_date: false`
- [ ] **New source:** Add `tools/fetch_jobs_remotive.py` (no key needed)
- [ ] **New source:** Add `tools/fetch_jobs_jobicy.py` (no key needed)
- [ ] **Optional:** Add `tools/fetch_jobs_adzuna.py` once Adzuna API key is obtained
- [ ] Update `orchestrate.py` to include new sources in `tier2`
- [ ] Update `workflows/daily_job_search.md` with new sources and corrected call budget

---

## Expected Impact After Fixes

| Source | Before | After (estimated) |
|--------|--------|-------------------|
| Greenhouse catalog | 0 jobs | 10–50 QA jobs/run |
| Lever catalog | 0 jobs | 5–20 QA jobs/run |
| Ashby catalog | 0 jobs | 2–10 QA jobs/run |
| Remotive (new) | 0 | 5–15 QA jobs/run |
| Jobicy (new) | 0 | 3–10 QA jobs/run |
| API calls (total) | ~240 | ~140 (−100 from dedup fix) |

