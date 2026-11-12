# Plan: Debug & Fix All Job Source Failures

## Context

Run on 2026-02-27 produced only 21 jobs — nearly all from Amazon and Netflix. Every other source
either returned 0 or errored out. Root cause analysis across the logs reveals 7 distinct bugs:
some are trivial config mistakes, some require replacing broken API calls with scrapers, and some
warrant disabling dead endpoints entirely. This plan addresses each by root cause, not symptom.

---

## Issues Ranked by Impact

### Issue 1 — JSearch drops ALL jobs (CRITICAL, 1-line fix)

**Root cause:** `config/job_search_config.json` has `"max_age_hours": 3`. The JSearch API already
filters to `date_posted=today` (last 24h). The 3h post-fetch filter then drops every job not
posted in the last 3 hours — i.e., almost everything. Logs confirm: "26 unique jobs fetched"
followed by "0 jobs remain after 3h filter (dropped 26)".

**Fix:** Set `"max_age_hours": null` in `config/job_search_config.json`.
The API-level `date_posted=today` parameter is sufficient. The in-process filter was originally
intended as a tighter safeguard but the value of 3 is too aggressive for a cron that runs once
daily.

**File:** `config/job_search_config.json` — one key change.

---

### Issue 2 — Dice `__NEXT_DATA__` missing (HIGH, scraper rewrite)

**Root cause:** Dice.com migrated from Next.js page-level `__NEXT_DATA__` to React Server
Components (RSC) streaming format. The page now embeds data in `self.__next_f.push([id, json])`
script calls instead of a single `<script id="__NEXT_DATA__">` tag.

**Investigation confirms:** Job objects live inside `jobList.data` within one of the RSC push
payloads. Each job has: `detailsPageUrl`, `title`, `companyName`, `summary`, `salary`,
`jobLocation` (`city`, `state`, `country`, `displayName`), `postedDate`, `employmentType`,
`workplaceTypes`, `isRemote`.

**Fix — update `tools/fetch_jobs_dice.py`:**
1. Keep the same HTTP request (URL, headers, timeout) — it's working.
2. Replace the `__NEXT_DATA__` extraction block with RSC stream parser:
   - Find all `<script>` tags whose text contains `self.__next_f.push`
   - For each script, extract the content of the outermost `self.__next_f.push([...])` call
     using regex: `self.__next_f\.push\((\[.*\])\)`
   - JSON-parse the extracted array; the payload is `arr[1]` (a JSON string)
   - Scan all payloads: find the one that contains `"jobList"` key
   - Parse `jobList["data"]` as the raw job list
3. Update `_to_canonical()` to use the new field names:
   - `detailsPageUrl` → `job_url`
   - `jobLocation.displayName` → `location`
   - `postedDate` → `date_posted`
   - `isRemote` → contribute to `job_type`

**Files:** `tools/fetch_jobs_dice.py`

---

### Issue 3 — Meta Greenhouse 404 (MEDIUM, board token change)

**Root cause:** `boards-api.greenhouse.io/v1/boards/meta/jobs` returns 404. The board token
`meta` is invalid. Meta rebranded from Facebook in 2021 — their legacy Greenhouse board
was almost certainly registered under `facebook`, not `meta`.

**Fix — two-step approach:**
1. In `_fetch_meta()`, change the hardcoded URL from `…/boards/meta/jobs` to
   `…/boards/facebook/jobs` and test.
2. If `facebook` also 404s, Meta has likely migrated off Greenhouse entirely.
   In that case disable Meta in config and document it.

**File:** `tools/fetch_jobs_company_careers.py` — `_fetch_meta()` ~line 124.

---

### Issue 4 — Apple Jobs 404 (MEDIUM, replace API with HTML scraper)

**Root cause:** `jobs.apple.com/api/role/search` is not a public API. It redirects to
`www.apple.com/pagenotfound`. Apple's careers site is server-side rendered HTML with no
published API.

**Fix — replace `_fetch_apple()` with an HTML scraper:**
- Request: `GET https://jobs.apple.com/en-us/search?location=united-states-USA&q={keyword}`
  with a browser-like User-Agent and `Accept: text/html`.
- The page renders job listings in semantic HTML. Use BeautifulSoup to find job cards.
  Typical patterns: `<a>` tags with role-detail URLs like `/en-us/details/{id}`,
  adjacent elements with title, team, location, and date.
- Parse each card into the canonical schema.
- During implementation: do a live `requests.get` of the search page and inspect the HTML
  structure to confirm the exact CSS selectors/attributes before coding.
- Add `SLEEP_BETWEEN_REQUESTS` between keywords (already defined at module level).

**Note:** If Apple's HTML proves too dynamic or thin on metadata, fall back to disabling
Apple in config. Don't over-engineer.

**File:** `tools/fetch_jobs_company_careers.py` — `_fetch_apple()`.

---

### Issue 5 — Google Careers 404 (DISABLE — no public API exists)

**Root cause:** `careers.google.com/api/v3/search/` is an undocumented internal endpoint that
has been removed. Google has never published a public careers API. JSearch already aggregates
jobs from Google Jobs indexing, so this is redundant coverage at best.

**Fix:** Disable Google in config. No code deletion needed.
- Set `"enabled": false` for Google in `config/job_search_config.json`.

**File:** `config/job_search_config.json`

---

### Issue 6 — Microsoft 502 (DISABLE — no usable public API)

**Root cause:** `gcsservices.careers.microsoft.com` returns 502 Bad Gateway. This backend is
deprecated. Microsoft's current careers portal uses fully client-side JavaScript rendering —
job data is not present in the initial HTML response, making it unfetchable without a headless
browser.

**Fix:** Disable Microsoft in config.
- Set `"enabled": false` for Microsoft in `config/job_search_config.json`.

**File:** `config/job_search_config.json`

---

### Issue 7 — Indeed RSS 403 (NO CHANGE — expected failure, handled gracefully)

**Root cause:** Indeed deprecated RSS feeds and blocks automated requests. All 4 keywords
return 403. The code already handles this correctly (try/except → empty list → safe_fetch
logs it and continues). JSearch redundantly covers Indeed jobs.

**Fix:** No code change. Update `workflows/daily_job_search.md` Known Issues table to document
this as a known permanent block, not a transient error.

---

## Files to Modify

| File | Change |
|------|--------|
| `config/job_search_config.json` | `max_age_hours: null`, disable Google, disable Microsoft |
| `tools/fetch_jobs_dice.py` | Replace `__NEXT_DATA__` extraction with RSC stream parser |
| `tools/fetch_jobs_company_careers.py` | Fix Meta board token (`meta`→`facebook`), replace Apple API with HTML scraper |
| `workflows/daily_job_search.md` | Update Known Issues — document Indeed RSS as permanent block |

---

## Implementation Order

1. **Config fixes first** (`max_age_hours`, disable Google/Microsoft) — zero risk, immediate gain
2. **Meta board token** — single URL string change, test with a live request
3. **Dice RSC parser** — rewrite the `__NEXT_DATA__` extraction block
4. **Apple HTML scraper** — fetch live page, inspect HTML structure, then code the parser
5. **Workflow doc update**

---

## Verification

After implementing all changes, run `python tools/orchestrate.py` locally and check:

1. **JSearch** — `N jobs remain after Xh filter` line absent; JSearch contributes 20+ jobs
2. **Dice** — `Dice: N unique jobs fetched` where N > 0
3. **Meta** — no 404 error in logs; jobs fetched
4. **Apple** — no 404 error in logs; jobs fetched
5. **Google / Microsoft** — no fetch attempts logged (disabled in config)
6. **Total** — aim for 50+ unique jobs (currently 21 from Amazon+Netflix alone)
7. **Airtable** — `inserted` count > 0, `run_summary.json` healthy
