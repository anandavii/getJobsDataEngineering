# Plan: Fix Apple Jobs Fetcher — Root Cause Analysis & Fix

## Context

A "Software Development Engineer in Test" role at Apple (posted Mar 04, 2026, ID 200631617) was not surfaced by the daily job search automation. Investigation confirmed two root causes:

1. **Apple HTML scraper returns irrelevant retail jobs** — Searching `jobs.apple.com/en-us/search?q=SDET` returns Apple Store retail roles (Technical Specialist, Expert, etc.), not software engineering roles. The target SDET job was absent from all keyword searches.
2. **Date parsing bug** — `_parse_date()` lacks the `"%b %d, %Y"` format (e.g., "Mar 04, 2026"). The fallback `date_str[:10]` truncates to `"Mar 04, 20"` — invalid ISO. All Apple dates in the last run are stored as `"Mar 05, 20"` (confirmed in `.tmp/company_jobs.json`). Jobs survive the calendar filter only because `drop_if_missing_date: false`, but their dates are corrupted.

**Evidence from `.tmp/`:**
- `company_jobs.json`: 19+ Apple jobs all with `"date_posted": "Mar 05, 20"`
- `qa_filtered_jobs.json`: 0 Apple jobs (all dropped — only retail roles fetched)
- Job ID `200631617` absent from every `.tmp/` file

---

## Root Causes

### RC-1 (Primary): HTML Scraper Returns Wrong Jobs
The `_fetch_apple()` function scrapes `jobs.apple.com/en-us/search?q={keyword}` as HTML. Apple's search surfaces retail Store jobs for QA-adjacent keywords. The config already has the correct **JSON API URL** (`/api/role/search`) but it is ignored — the code has a hardcoded HTML scraper that bypasses it.

### RC-2 (Secondary): Date Format Not Supported
`_parse_date()` in `fetch_jobs_company_careers.py` (line 43–57) supports `"%B %d, %Y"` (full month: "March") but not `"%b %d, %Y"` (abbreviated: "Mar"). Apple's HTML returns the abbreviated form. The fallback produces `"Mar 04, 20"` — not a parseable date.

---

## Fix Plan

### Fix 1 — Switch Apple to JSON API (`fetch_jobs_company_careers.py`)

**File:** `tools/fetch_jobs_company_careers.py`

Replace `_fetch_apple()` to call the Apple Jobs JSON API (already in config as `url`):
```
GET https://jobs.apple.com/api/role/search?filters=location%3DUSA&query={keyword}&page={n}
```

The API returns JSON (no JS rendering needed). Handle pagination: loop pages until `postings` is empty or a max page limit (e.g., 5 pages × 20 results = 100 jobs per keyword).

Response fields to map:
- `positionId` → build URL: `https://jobs.apple.com/en-us/details/{positionId}/{slug}`
- `postingTitle` → `job_title`
- `team.teamName` / `locations` → `location`
- `postingDate` → `date_posted` (via `_parse_date`)
- `transformedPostingTitle` → slug for URL construction

Remove the `_APPLE_HTML_HEADERS` block (no longer needed).

### Fix 2 — Add `"%b %d, %Y"` to `_parse_date()` (`fetch_jobs_company_careers.py`)

**File:** `tools/fetch_jobs_company_careers.py`, lines 46–51

Add `"%b %d, %Y"` to the format list, before the fallback:
```python
for fmt in (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
    "%B %d, %Y",   # Full month: "March 04, 2026"
    "%b %d, %Y",   # Abbreviated: "Mar 04, 2026"  ← ADD THIS
):
```

---

## Files to Modify

| File | Change |
|------|--------|
| `tools/fetch_jobs_company_careers.py` | Replace `_fetch_apple()` with API-based fetcher; add `"%b %d, %Y"` to `_parse_date()` |

No other files need changes. Config already has the right API URL (used as reference). Orchestrate pipeline is unchanged.

---

## Verification

1. Run `uv run tools/fetch_jobs_company_careers.py` (or a quick test script) to confirm the Apple API fetcher returns the SDET job (ID 200631617).
2. Run `uv run tools/orchestrate.py` and check `.tmp/company_jobs.json` for:
   - Apple jobs with valid ISO dates (e.g., `"2026-03-04"`, not `"Mar 04, 20"`)
   - Presence of "Software Development Engineer in Test" job
3. Check `.tmp/qa_filtered_jobs.json` for Apple SDET jobs passing the QA filter.
4. Run existing tests: `uv run pytest tests/ -v` — ensure nothing regressed.
