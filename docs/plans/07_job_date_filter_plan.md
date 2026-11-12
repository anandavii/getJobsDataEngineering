# Calendar-Day Job Date Filtering (Option 1)

## Summary
Add a global, calendar-day filter that applies across all sources after merging results. This enables `today`, `yesterday`, and `last_days` filters based on a chosen timezone (default: America/New_York). The filter will operate on each job’s `date_posted` (ISO date), which is already produced by each source.

**Updated window semantics:**
- `today`: only local today
- `yesterday`: local yesterday **plus** local today (i.e., `date_posted >= local_yesterday`)
- `last_days`: local today plus the previous `N-1` days

## Goals
- Allow simple, consistent calendar-day filtering across all sources.
- Make the behavior explicit and configurable via `config/job_search_config.json`.
- Keep current JSearch API parameters intact, and apply the calendar-day filter in the orchestrator.

## Non-Goals
- Hour-level filtering (will be a separate option).
- Changing keyword logic or QA relevance filters.

---

## Public Interfaces / Config Changes
Update [job_search_config.json](/Users/anandavii/agenticProjects/getJobsWorktree/config/job_search_config.json) `search` block:

```json
"search": {
  "date_posted": "today",
  "calendar_day_filter": {
    "mode": "today",        // "today" | "yesterday" | "last_days" | "none"
    "days": 1,              // only used when mode == "last_days"
    "timezone": "America/New_York",
    "drop_if_missing_date": true
  }
}
```

Behavior:
- `mode: "today"` → keep jobs where `date_posted == local_today`
- `mode: "yesterday"` → keep jobs where `date_posted >= local_yesterday` (yesterday + today)
- `mode: "last_days"` → keep jobs where `date_posted >= local_today - (days - 1)` (includes today)
- `mode: "none"` → disable the filter
- `drop_if_missing_date`: drop jobs that lack `date_posted` (recommended).

**Note:** `search.date_posted` remains as-is and continues to shape JSearch API results. The new calendar-day filter is applied after aggregation, so it can tighten or broaden results independent of source.

---

## Implementation Steps

1. **Add calendar-day filter helper in orchestrator**
   - Create a function in [tools/orchestrate.py](/Users/anandavii/agenticProjects/getJobsWorktree/tools/orchestrate.py) such as:
     - `filter_by_calendar_day(jobs: list[dict], config: dict) -> list[dict]`
   - Use `zoneinfo.ZoneInfo` to compute local “today” and “yesterday” (timezone from config).
   - Parse each job’s `date_posted` as an ISO date (YYYY-MM-DD).
   - Apply mode logic and return filtered list.
   - Log a summary: kept vs dropped and the active filter mode/timezone.

2. **Integrate filter into pipeline**
   - Call `filter_by_calendar_day` after location filter and before QA relevance filtering:
     - `merge_and_deduplicate`
     - `filter_by_location`
     - `filter_by_calendar_day`
     - `qa_relevance.filter_qa_jobs`
   - Save intermediate output to `.tmp/all_jobs.json` **after** applying the calendar-day filter so debugging reflects the filtered set.

3. **Update documentation**
   - Add a section to [workflows/daily_job_search.md](/Users/anandavii/agenticProjects/getJobsWorktree/workflows/daily_job_search.md) describing:
     - new `calendar_day_filter` config
     - timezone behavior
     - examples for today/yesterday/last_days (with inclusive behavior)

4. **Add tests**
   - Create a unit test file (e.g., `tests/test_calendar_day_filter.py`) to validate:
     - `today` keeps only today’s jobs
     - `yesterday` keeps yesterday + today
     - `last_days` keeps today + previous N-1 days
     - `drop_if_missing_date` true drops missing dates
     - `drop_if_missing_date` false keeps missing dates
   - Use fixed dates by injecting a reference date (e.g., patch `datetime.now`) or by constructing jobs with fixed `date_posted` and computing expected based on known timezone.

---

## Edge Cases / Failure Modes
- Invalid timezone: fall back to UTC with a warning.
- Invalid `days` value: default to `1`.
- `date_posted` not parseable: drop when `drop_if_missing_date` is true.

---

## Test Scenarios (Acceptance)
- With `mode: "today"` and timezone `America/New_York`, only jobs dated to local “today” are included.
- With `mode: "yesterday"`, jobs dated to local yesterday **and** local today are included.
- With `mode: "last_days", days: 3`, jobs dated to local today, yesterday, and the day before yesterday are included.
- Log lines clearly indicate the filter applied and counts.

---

## Assumptions / Defaults
- Default timezone is `America/New_York`.
- `drop_if_missing_date` defaults to true.
- `search.date_posted` remains unchanged and continues to filter JSearch results at the API level.
