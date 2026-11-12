# Plan: Source Health Visibility & Persistent Monitoring

## Context

The user asked whether failing job sources are detected and reported, or if they fail silently.
The pipeline already has sophisticated per-source health tracking — but almost none of it reaches the user.
This plan fixes that by addressing three layered problems, and adds a live health dashboard to the UI.

---

## Current State (what already exists)

- `safe_fetch_with_meta()` (`orchestrate.py:124`) wraps every fetch and captures rich metadata per source: success flag, error message, duration, fetched count, empty flag, timestamp.
- `update_source_health()` (`orchestrate.py:505`) accumulates per-source stats: runs, successes, failures, empties, avg_duration_s, consecutive_failures, last_success_utc.
- `is_circuit_open()` (`orchestrate.py:494`) skips a source after 3 consecutive failures.
- All errors are logged via `log.error()`.
- `.tmp/source_health.json` is saved per run and uploaded as GitHub Actions artifact (7-day retention).
- Frontend: two vanilla HTML pages (`docs/index.html`, `docs/rejected.html`) that query Supabase directly using the anon key + `@supabase/supabase-js@2` via CDN.

---

## The Problems (in order of severity)

### Problem 1 — CRITICAL BUG: Health state never persists between runs

`load_source_health_state()` reads from `.tmp/source_health_state.json`.
GitHub Actions runners are ephemeral — `.tmp/` is always empty at run start.
The workflow **never downloads** the previous artifact before running.
**Result:** `consecutive_failures` resets to 0 every single run.
The circuit breaker and multi-day failure tracking are effectively non-functional in production.

### Problem 2: Per-source health data never reaches the user

`notify_webhook.py` only receives `push_result` (inserted/updated/cleaned/error) and `new_jobs`.
The rich `source_execution` list — per-source success/failure details — is **never passed to the notification layer**.
The user gets zero visibility into which sources failed or returned zero jobs.

### Problem 3: No proactive alerting for degraded sources

Even after fixing persistence, there's no mechanism to prominently alert the user when a source has been failing across multiple consecutive runs.

### Problem 4: No live health UI

The health data is only visible in GitHub Actions artifacts (raw JSON). No dashboard exists.

---

## Solution

### Fix 1: Persist health state to Supabase (root cause fix)

Store the health state as rows in a new Supabase table. Supabase is already a required dependency — no new secrets needed.

**New Supabase table** (`source_health_state`):
```sql
CREATE TABLE source_health_state (
    source                TEXT PRIMARY KEY,
    runs                  INTEGER DEFAULT 0,
    successes             INTEGER DEFAULT 0,
    failures              INTEGER DEFAULT 0,
    empties               INTEGER DEFAULT 0,
    avg_duration_s        FLOAT DEFAULT 0.0,
    last_success_utc      TIMESTAMPTZ,
    consecutive_failures  INTEGER DEFAULT 0,
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);
```

**Changes to `tools/push_to_supabase.py`:**
- Add `load_health_state(supabase_url, service_key) -> dict` — SELECTs all rows, returns dict keyed by source name
- Add `save_health_state(supabase_url, service_key, state: dict)` — UPSERTs each source row (conflict on `source`)

**Changes to `tools/orchestrate.py`:**
- `load_source_health_state()` (line 459): Try Supabase first, fall back to local `.tmp/` file if Supabase unavailable
- `save_source_health_state()` (line 471): Save to Supabase first, local file as backup/debug artifact

---

### Fix 2: Source health summary in email notifications

Pass `source_execution: list[dict]` from orchestrate.py to `notify.run()`, then render a **Source Status** section in emails.

**Only shown when there are issues** (any failure or empty) — clean-run emails stay uncluttered.

**HTML section (before the bottom board link):**
```
Source       | Status | Jobs | Note
-------------|--------|------|--------------------
jsearch      |  ✅    |  47  |
greenhouse   |  ✅    |  12  |
workday      |  ⚠️   |   0  | returned 0 jobs
dice         |  ❌    |   0  | 403 Forbidden
lever        |  ⏭️   |   —  | skipped (circuit open)
```

Status key: ✅ success+jobs, ⚠️ success+empty, ❌ failure, ⏭️ skipped (circuit open)

Plain-text: bulleted list with same info.

**Changes to `tools/notify_webhook.py`:**
- `run()` (line 258): add `source_execution: list[dict] = None` and `health_state: dict = None` params
- Add `_build_source_status_html(source_execution, health_state, alert_threshold)` helper
- Add `_build_source_status_plain(source_execution, health_state, alert_threshold)` helper
- Inject section into both `_build_email_html()` and `_build_email_plain()` just before the bottom board link
- Discord: add a compact summary line if any source failed

**Changes to `tools/orchestrate.py`:**
- Pass `source_execution` and `health_state` to `notify.run()` call (~line 902)

---

### Fix 3: Consecutive failure alert banner

After loading persisted health state from Supabase, if any source has `consecutive_failures >= threshold`, show a prominent banner at the TOP of the email.

**New config key** (`config/job_search_config.json`):
```json
"health": {
  "max_consecutive_failures": 3,
  "alert_consecutive_failures_threshold": 2,   ← NEW
  "tier_activation_threshold": 20,
  "state_file": ".tmp/source_health_state.json"
}
```

Example banner: `⚠️ 2 source(s) have failed for 2+ consecutive runs — check Source Status below.`
Sources with persistent failures highlighted in red in the table (vs orange for single-run failures).

---

### Fix 4: Source Health dashboard in the UI (new `docs/health.html`)

Since health state is now in Supabase (Fix 1), the frontend can query it directly — same pattern as `index.html`.

**New file: `docs/health.html`**

Same structure/styling as `index.html` (light/dark theme, same design tokens, same nav header).

**Two sections:**

**Section 1 — Source Status table (uptime monitor view)**
```
Source       | Status  | Health  | Runs | Fails | Empties | Avg Time | Last Success
-------------|---------|---------|------|-------|---------|----------|--------------
jsearch      | ● Live  |  94%    |  17  |   1   |    0    |  12.3s   | 2h ago
greenhouse   | ● Live  |  100%   |  17  |   0   |    0    |   8.1s   | 2h ago
workday      | ▲ Warn  |  82%    |  17  |   1   |    2    | 342.0s   | 2h ago
dice         | ✕ Down  |  41%    |  17  |   6   |    2    |   3.0s   | 3d ago
lever        | ⊘ Off   |  —      |  —   |   —   |   —     |    —     | skipped
```

Status badge rules:
- `● Live` (green): consecutive_failures = 0
- `▲ Warn` (amber): consecutive_failures = 1 OR empty_rate > 50%
- `✕ Down` (red): consecutive_failures >= alert_threshold (2)
- `⊘ Off` (gray): skipped/circuit open

Health %: `(successes / runs) * 100`

**Section 2 — Per-source stats cards (collapsible)**
Click a source row → expands to show: total runs, success rate, empty rate, avg duration, last success timestamp.

**Navigation:**
- Add "Health" nav link to `docs/index.html` header
- Add "Health" nav link to `docs/rejected.html` header

**Implementation notes:**
- Query `source_health_state` Supabase table using same anon key already in `index.html`
- Add RLS policy: `SELECT` allowed for anon role on `source_health_state`
- "Last Success" shown as relative time ("2h ago", "3d ago") via small inline helper
- No build step — pure vanilla JS, CDN Supabase client

---

## Files to Modify / Create

| File | Change |
|------|--------|
| `tools/push_to_supabase.py` | Add `load_health_state()` and `save_health_state()` |
| `tools/orchestrate.py` | Update `load/save_source_health_state()` to use Supabase; pass `source_execution` + `health_state` to notify |
| `tools/notify_webhook.py` | Add `source_execution` + `health_state` params; add source status section builders |
| `config/job_search_config.json` | Add `health.alert_consecutive_failures_threshold: 2` |
| `docs/health.html` | NEW — health dashboard page (vanilla HTML, queries `source_health_state` table) |
| `docs/index.html` | Add "Health" nav link in header |
| `docs/rejected.html` | Add "Health" nav link in header |

## Files NOT to modify
- `.github/workflows/daily_job_search.yml` — no changes; Supabase creds already present as secrets
- All fetcher files — error handling is already correct

---

## Verification

1. **Persistence**: Run orchestrate.py locally → check `source_health_state` table populated in Supabase dashboard.
2. **Email**: Trigger `workflow_dispatch` → verify email shows Source Status section with correct statuses.
3. **Alert banner**: Manually set `consecutive_failures = 2` for a source in Supabase → verify next run email shows top-of-email warning.
4. **UI**: Open `docs/health.html` → verify table loads with live data from Supabase.
5. **Persistence across runs**: Run twice → verify `consecutive_failures` increments for a source that fails both runs.
