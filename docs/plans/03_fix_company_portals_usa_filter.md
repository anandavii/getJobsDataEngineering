# Plan: Fix Company Career Portal URLs + USA Location Filtering

## Context
Two issues need fixing:
1. **Company portal failures** — Some built-in fetchers (Google, Apple) use hardcoded URLs that may be stale or broken. The `config/job_search_config.json` defines URLs per company but they are **ignored** by `fetch_jobs_company_careers.py` — the builtin fetchers each hardcode their own URL. Notably, Apple's URL hardcodes `fieldOfWork%3D20306` which silently filters out QA roles.
2. **No location filtering** — Jobs from all sources may include non-USA listings. Meta and Netflix have no location filter at the API level. The orchestrator does zero post-fetch location filtering despite `config.search.location = "United States"` being defined. Jobs from "London, UK" or "Paris, France" can reach Airtable.

---

## Branch
`fix/company-portals-usa-filter`

---

## Files to Modify
- `tools/fetch_jobs_company_careers.py` — fix Apple URL, add USA location filtering for Meta and Netflix
- `tools/orchestrate.py` — add `filter_by_location()` step after merge/dedup
- `config/job_search_config.json` — fix Apple URL entry
- `workflows/daily_job_search.md` — document the location filtering behavior

---

## Implementation Steps

### 1. Fix Apple URL (remove broken `fieldOfWork` filter)

**`tools/fetch_jobs_company_careers.py` — `_fetch_apple()` (line 163)**

Current:
```python
url = (
    f"https://jobs.apple.com/api/role/search"
    f"?filters=fieldOfWork%3D20306&filters=location%3DUSA"
    f"&query={quote_plus(keyword)}&page=1"
)
```
Fix — remove the `fieldOfWork` filter:
```python
url = (
    f"https://jobs.apple.com/api/role/search"
    f"?filters=location%3DUSA"
    f"&query={quote_plus(keyword)}&page=1"
)
```

Also update `config/job_search_config.json` Apple entry to match:
```json
"url": "https://jobs.apple.com/api/role/search?filters=location%3DUSA&query={keyword}&page=1"
```

---

### 2. Add `_is_usa_or_remote()` helper in `fetch_jobs_company_careers.py`

Add near the top of the file, after `_contains_keyword`:

```python
_USA_PATTERNS = frozenset({
    "united states", "usa", "us", "u.s.", "u.s.a",
    # US state abbreviations (as suffix pattern ", XX")
    ", al", ", ak", ", az", ", ar", ", ca", ", co", ", ct", ", de",
    ", fl", ", ga", ", hi", ", id", ", il", ", in", ", ia", ", ks",
    ", ky", ", la", ", me", ", md", ", ma", ", mi", ", mn", ", ms",
    ", mo", ", mt", ", ne", ", nv", ", nh", ", nj", ", nm", ", ny",
    ", nc", ", nd", ", oh", ", ok", ", or", ", pa", ", ri", ", sc",
    ", sd", ", tn", ", tx", ", ut", ", vt", ", va", ", wa", ", wv",
    ", wi", ", wy", ", dc",
})

def _is_usa_or_remote(location: str) -> bool:
    """Return True if location is in the USA or fully remote."""
    if not location:
        return True  # no location set → assume US (came from an API already filtered)
    loc = location.lower().strip()
    if "remote" in loc:
        return True
    for pattern in _USA_PATTERNS:
        if pattern in loc:
            return True
    return False
```

Apply this filter inside `_fetch_meta` and `_fetch_netflix` — after `location` is parsed for each job, add:
```python
if not _is_usa_or_remote(location):
    continue
```

---

### 3. Add `filter_by_location()` in `orchestrate.py`

Add after `merge_and_deduplicate()`:

```python
def filter_by_location(jobs: list[dict]) -> list[dict]:
    """
    Keep only jobs in the USA or fully remote.
    Accepts: 'United States', 'USA', 'US', 'U.S.A', state abbreviations, 'Remote'.
    Jobs with no location default to passing (sourced from US-filtered APIs).
    """
    _USA = frozenset({
        "united states", "usa", "us", "u.s.", "u.s.a",
        ", al", ", ak", ", az", ", ar", ", ca", ", co", ", ct", ", de",
        ", fl", ", ga", ", hi", ", id", ", il", ", in", ", ia", ", ks",
        ", ky", ", la", ", me", ", md", ", ma", ", mi", ", mn", ", ms",
        ", mo", ", mt", ", ne", ", nv", ", nh", ", nj", ", nm", ", ny",
        ", nc", ", nd", ", oh", ", ok", ", or", ", pa", ", ri", ", sc",
        ", sd", ", tn", ", tx", ", ut", ", vt", ", va", ", wa", ", wv",
        ", wi", ", wy", ", dc",
    })

    def _keep(job: dict) -> bool:
        loc = (job.get("location") or "").lower().strip()
        if not loc:
            return True
        if "remote" in loc:
            return True
        return any(p in loc for p in _USA)

    before = len(jobs)
    filtered = [j for j in jobs if _keep(j)]
    log.info(f"Location filter: {len(filtered)} kept, {before - len(filtered)} dropped (non-USA)")
    return filtered
```

Call it in `main()` right after `merge_and_deduplicate`:
```python
all_jobs = merge_and_deduplicate([jsearch_jobs, indeed_jobs, dice_jobs, company_jobs])
all_jobs = filter_by_location(all_jobs)   # ← new line
log.info(f"After location filter: {len(all_jobs)} unique jobs")
save_tmp("all_jobs.json", all_jobs)
```

---

### 4. Update `workflows/daily_job_search.md`

Add a note in the post-processing / filtering section:
> **Location filter**: After merging all sources, jobs are filtered to USA-only (or fully remote). Accepted values: "United States", "USA", "US", "U.S.A", any US city+state combo (e.g. "New York, NY"), and any location containing "remote". Non-US jobs are dropped before Airtable push.

---

## Verification

1. Create branch: `git checkout -b fix/company-portals-usa-filter`
2. Apply changes
3. Run: `python tools/orchestrate.py` (with real `.env`)
4. Check `.tmp/all_jobs.json` — confirm no non-US locations remain
5. Check logs — `filter_by_location` line shows how many were dropped
6. Verify Apple now returns QA results (no `fieldOfWork` restriction)
7. Spot-check 10–20 Airtable records — all should be US cities or "Remote"
