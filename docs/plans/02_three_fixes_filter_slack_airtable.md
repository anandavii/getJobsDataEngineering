# Three Fixes: 3-Hour Filter, Slack Fix, Airtable Field Reliability

## Context
Three targeted improvements to the running job search automation:
1. Jobs should be filtered to those posted within the last 3 hours (JSearch has timestamps; other sources do not)
2. Slack notifications are sending HTTP 200 but messages are likely rendering incorrectly — the job links use standard Markdown `[text](url)` which Slack ignores; Slack's mrkdwn requires `<url|text>` format, and the payload is missing a required `text` fallback field
3. Records with empty Job Title or Job URL should be caught early and skipped with a log warning, rather than inserting blank/partial records into Airtable

---

## Change 1 — 3-Hour Post Filter

**Files:** `config/job_search_config.json`, `tools/fetch_jobs_jsearch.py`

**Why only JSearch:** JSearch returns `job_posted_at_timestamp` (epoch seconds) which allows sub-day filtering. Company career pages (Amazon, Netflix, etc.) return only a date, not a time — 3-hour filtering cannot be applied there. Those sources return real-time data anyway so including them is fine.

**Config change:** Add `"max_age_hours": 3` to the `search` block:
```json
"search": {
  "keywords": [...],
  "date_posted": "today",
  "max_age_hours": 3,
  ...
}
```

**Fetcher change** (`fetch_jobs_jsearch.py`): After building the canonical job list in `run()`, apply a timestamp filter using the raw `job_posted_at_timestamp` from the API response. Since `_to_canonical` discards the raw timestamp, store it temporarily as a `_ts` key, filter, then remove before returning.

```python
# In _to_canonical: store raw timestamp
"_ts": job.get("job_posted_at_timestamp"),

# In run(): after dedup, filter to max_age_hours
max_age_hours = config["search"].get("max_age_hours")
if max_age_hours:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)
    all_jobs = [
        j for j in all_jobs
        if j.get("_ts") and datetime.fromtimestamp(int(j["_ts"]), tz=timezone.utc) >= cutoff
    ]

# Strip the temp key before returning
for j in all_jobs:
    j.pop("_ts", None)
```

> **Note:** The cron runs once at 8 AM ET. With a 3-hour window, only jobs posted between 5–8 AM will appear. If you want broader daily coverage, either remove `max_age_hours` from the config or set it to `24`. To catch jobs all day, the cron schedule can later be changed to run every 3 hours: `0 */3 * * *`.

---

## Change 2 — Slack Notification Fix

**File:** `tools/notify_webhook.py`

**Two bugs:**

**Bug A — Wrong link syntax in `_build_job_lines`:**
```python
# Current (standard Markdown — does not render in Slack):
line = f"• [{_truncate(title, 60)}]({url})\n  {meta}{salary_str}"

# Fix (Slack mrkdwn format):
line = f"• <{url}|{_truncate(title, 60)}>\n  {meta}{salary_str}"
```

**Bug B — Missing `text` fallback in Slack payload:**
Slack's Block Kit API requires a top-level `"text"` field as a notification/fallback. Without it, mobile push notifications and some integrations show nothing. Add it alongside `"blocks"`:
```python
return {
    "text": f"Daily QA Job Search — {inserted} new job(s) found",
    "blocks": blocks
}
```

---

## Change 3 — Airtable Core Field Reliability

**File:** `tools/push_to_airtable.py`

**Current state:** Only records with a missing `job_url` are skipped. Records with an empty `job_title` are inserted as blank rows.

**Fix:** Expand the validation filter before upsert to also skip records missing `job_title`. Log each skipped record's source so data quality issues can be traced.

```python
# Replace current single-field check:
valid_jobs = [j for j in jobs if j.get("job_url")]

# With two-field check + logging:
valid_jobs = []
for j in jobs:
    if not j.get("job_url"):
        log.warning(f"Skipping record with no job_url — source: {j.get('source')}, title: {j.get('job_title')!r}")
        skipped += 1
    elif not j.get("job_title"):
        log.warning(f"Skipping record with no job_title — source: {j.get('source')}, url: {j.get('job_url')!r}")
        skipped += 1
    else:
        valid_jobs.append(j)
```

---

## Files to Edit (4 total)

| File | Change |
|------|--------|
| `config/job_search_config.json` | Add `"max_age_hours": 3` to `search` block |
| `tools/fetch_jobs_jsearch.py` | Store `_ts` in canonical dict; filter by cutoff in `run()`; strip `_ts` before return |
| `tools/notify_webhook.py` | Fix link format `[text](url)` → `<url|text>`; add `"text"` fallback to Slack payload |
| `tools/push_to_airtable.py` | Add `job_title` to the skip check; log which source/field caused the skip |

---

## Verification
1. Run `python tools/orchestrate.py` locally — check `.tmp/jsearch_jobs.json` and verify only jobs with timestamps from the last 3 hours appear
2. Check `.tmp/run_summary.json` — `source_counts.jsearch` will be lower than before (expected)
3. Check Slack — job titles should now be clickable hyperlinks
4. In Airtable — confirm no blank-title rows in the Jobs table after the run