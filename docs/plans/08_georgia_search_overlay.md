# Georgia Search Overlay Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a second JSearch pass that surfaces on-site/hybrid QA jobs specifically located anywhere in Georgia, USA, running alongside the existing US-wide search without duplicating records in Airtable.

**Architecture:** A `georgia_search` config block drives a second JSearch-only pass in `orchestrate.py`. Keywords are modified at runtime to append `"in Georgia, USA"`, the results are filtered to Georgia locations and Hybrid/On-site job types, then pushed to Airtable using the same upsert key (`Job URL`) so duplicates are silently ignored.

**Tech Stack:** Python 3.11+, uv, pytest, existing `fetch_jobs_jsearch`, `qa_relevance`, `role_horizon`, `push_to_airtable` modules.

---

### Task 1: Add `georgia_search` config block

**Files:**
- Modify: `config/job_search_config.json`

**Step 1: Add the block**

Insert a new top-level key `"georgia_search"` in `config/job_search_config.json`, after the closing brace of `"sources"`:

```json
"georgia_search": {
  "enabled": true,
  "location": "Georgia, USA",
  "location_aliases": [
    "Atlanta, GA", "Savannah, GA", "Augusta, GA",
    "Columbus, GA", "Macon, GA", "Athens, GA",
    "Alpharetta, GA", "Marietta, GA", "Roswell, GA",
    "Sandy Springs, GA", "Decatur, GA"
  ],
  "job_types": ["Hybrid", "On-site"]
}
```

**Step 2: Validate JSON is still valid**

```bash
uv run python -c "import json; json.load(open('config/job_search_config.json')); print('OK')"
```
Expected: `OK`

**Step 3: Commit**

```bash
git add config/job_search_config.json
git commit -m "feat(config): add georgia_search overlay block"
```

---

### Task 2: Add `filter_by_georgia` and `filter_by_job_type` helpers (TDD)

**Files:**
- Modify: `tests/test_orchestrate_helpers.py`
- Modify: `tools/orchestrate.py`

**Step 1: Write failing tests**

Append to `tests/test_orchestrate_helpers.py`:

```python
class FilterByGeorgiaTests(unittest.TestCase):
    def _job(self, url: str, location: str, job_type: str = "On-site") -> dict:
        return {"job_url": url, "location": location, "job_type": job_type}

    def test_keeps_atlanta_ga(self) -> None:
        jobs = [self._job("https://e/1", "Atlanta, GA")]
        georgia_cfg = {"location_aliases": ["Atlanta, GA"]}
        result = orchestrate.filter_by_georgia(jobs, georgia_cfg)
        self.assertEqual(len(result), 1)

    def test_keeps_georgia_state_name(self) -> None:
        jobs = [self._job("https://e/1", "Savannah, Georgia")]
        georgia_cfg = {"location_aliases": ["Atlanta, GA"]}
        result = orchestrate.filter_by_georgia(jobs, georgia_cfg)
        self.assertEqual(len(result), 1)

    def test_drops_texas_job(self) -> None:
        jobs = [self._job("https://e/1", "Austin, TX")]
        georgia_cfg = {"location_aliases": ["Atlanta, GA"]}
        result = orchestrate.filter_by_georgia(jobs, georgia_cfg)
        self.assertEqual(len(result), 0)

    def test_drops_empty_location(self) -> None:
        jobs = [self._job("https://e/1", "")]
        georgia_cfg = {"location_aliases": ["Atlanta, GA"]}
        result = orchestrate.filter_by_georgia(jobs, georgia_cfg)
        self.assertEqual(len(result), 0)

    def test_keeps_alias_city(self) -> None:
        jobs = [self._job("https://e/1", "Alpharetta, GA")]
        georgia_cfg = {"location_aliases": ["Alpharetta, GA"]}
        result = orchestrate.filter_by_georgia(jobs, georgia_cfg)
        self.assertEqual(len(result), 1)


class FilterByJobTypeTests(unittest.TestCase):
    def _job(self, url: str, job_type: str) -> dict:
        return {"job_url": url, "job_type": job_type}

    def test_keeps_hybrid(self) -> None:
        jobs = [self._job("https://e/1", "Hybrid")]
        result = orchestrate.filter_by_job_type(jobs, ["Hybrid", "On-site"])
        self.assertEqual(len(result), 1)

    def test_keeps_onsite(self) -> None:
        jobs = [self._job("https://e/1", "On-site")]
        result = orchestrate.filter_by_job_type(jobs, ["Hybrid", "On-site"])
        self.assertEqual(len(result), 1)

    def test_drops_remote(self) -> None:
        jobs = [self._job("https://e/1", "Remote")]
        result = orchestrate.filter_by_job_type(jobs, ["Hybrid", "On-site"])
        self.assertEqual(len(result), 0)

    def test_drops_unknown(self) -> None:
        jobs = [self._job("https://e/1", "Unknown")]
        result = orchestrate.filter_by_job_type(jobs, ["Hybrid", "On-site"])
        self.assertEqual(len(result), 0)
```

**Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_orchestrate_helpers.py::FilterByGeorgiaTests tests/test_orchestrate_helpers.py::FilterByJobTypeTests -v
```
Expected: `FAILED` with `AttributeError: module 'orchestrate' has no attribute 'filter_by_georgia'`

**Step 3: Implement the two helpers in `tools/orchestrate.py`**

Add after the `filter_by_location` function (around line 209):

```python
def filter_by_georgia(jobs: list[dict], georgia_cfg: dict) -> list[dict]:
    """
    Keep only jobs located in Georgia, USA.
    Matches on 'georgia' (state name) or any city alias from georgia_cfg['location_aliases'].
    Jobs with no location are dropped — they came from a geo-targeted query and
    an empty location is ambiguous.
    """
    aliases = [a.lower() for a in (georgia_cfg.get("location_aliases") or [])]

    def _is_georgia(job: dict) -> bool:
        raw = str(job.get("location") or "").strip()
        if not raw:
            return False
        loc = raw.lower()
        if "georgia" in loc:
            return True
        # Match ", GA" or "GA," at word boundaries
        if ", ga" in loc or loc.endswith(", ga") or loc.startswith("ga,"):
            return True
        for alias in aliases:
            if alias in loc:
                return True
        return False

    before = len(jobs)
    filtered = [j for j in jobs if _is_georgia(j)]
    log.info(f"Georgia filter: {len(filtered)} kept, {before - len(filtered)} dropped (non-Georgia)")
    return filtered


def filter_by_job_type(jobs: list[dict], allowed_types: list[str]) -> list[dict]:
    """
    Keep only jobs whose job_type (case-insensitive) is in allowed_types.
    """
    allowed = {t.lower() for t in allowed_types}
    before = len(jobs)
    filtered = [j for j in jobs if str(j.get("job_type") or "").lower() in allowed]
    log.info(f"Job-type filter ({allowed_types}): {len(filtered)} kept, {before - len(filtered)} dropped")
    return filtered
```

**Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_orchestrate_helpers.py::FilterByGeorgiaTests tests/test_orchestrate_helpers.py::FilterByJobTypeTests -v
```
Expected: all `PASSED`

**Step 5: Run full test suite to confirm no regressions**

```bash
uv run pytest tests/ -v
```
Expected: all existing tests still `PASSED`

**Step 6: Commit**

```bash
git add tools/orchestrate.py tests/test_orchestrate_helpers.py
git commit -m "feat(orchestrate): add filter_by_georgia and filter_by_job_type helpers"
```

---

### Task 3: Add `_build_georgia_config` and `run_georgia_pass` (TDD)

**Files:**
- Modify: `tests/test_orchestrate_helpers.py`
- Modify: `tools/orchestrate.py`

**Step 1: Write failing test for `_build_georgia_config`**

Append to `tests/test_orchestrate_helpers.py`:

```python
class BuildGeorgiaConfigTests(unittest.TestCase):
    def _base_config(self) -> dict:
        return {
            "search": {
                "keywords": ["QA automation engineer", "SDET"],
            },
            "georgia_search": {
                "enabled": True,
                "location": "Georgia, USA",
                "location_aliases": ["Atlanta, GA"],
                "job_types": ["Hybrid", "On-site"],
            },
        }

    def test_keywords_have_georgia_appended(self) -> None:
        cfg = self._base_config()
        georgia_cfg = cfg["georgia_search"]
        result = orchestrate._build_georgia_config(cfg, georgia_cfg)
        for kw in result["search"]["keywords"]:
            self.assertIn("Georgia, USA", kw)

    def test_original_config_keywords_unchanged(self) -> None:
        cfg = self._base_config()
        georgia_cfg = cfg["georgia_search"]
        orchestrate._build_georgia_config(cfg, georgia_cfg)
        self.assertNotIn("Georgia, USA", cfg["search"]["keywords"][0])

    def test_returns_dict(self) -> None:
        cfg = self._base_config()
        georgia_cfg = cfg["georgia_search"]
        result = orchestrate._build_georgia_config(cfg, georgia_cfg)
        self.assertIsInstance(result, dict)
```

**Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_orchestrate_helpers.py::BuildGeorgiaConfigTests -v
```
Expected: `FAILED` with `AttributeError`

**Step 3: Implement `_build_georgia_config` in `tools/orchestrate.py`**

Add after `filter_by_job_type` (the helpers added in Task 2):

```python
def _build_georgia_config(base_config: dict, georgia_cfg: dict) -> dict:
    """
    Build a JSearch-compatible config for the Georgia pass.
    Clones the base config and rewrites keywords to include
    'in Georgia, USA' so JSearch results are geographically targeted.
    Does NOT mutate base_config.
    """
    import copy
    georgia_config = copy.deepcopy(base_config)
    location = str(georgia_cfg.get("location", "Georgia, USA"))
    original_keywords = georgia_config["search"].get("keywords", [])
    georgia_config["search"]["keywords"] = [
        f"{kw} in {location}" for kw in original_keywords
    ]
    return georgia_config
```

**Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_orchestrate_helpers.py::BuildGeorgiaConfigTests -v
```
Expected: all `PASSED`

**Step 5: Run full test suite**

```bash
uv run pytest tests/ -v
```
Expected: all `PASSED`

**Step 6: Commit**

```bash
git add tools/orchestrate.py tests/test_orchestrate_helpers.py
git commit -m "feat(orchestrate): add _build_georgia_config helper"
```

---

### Task 4: Integrate Georgia pass into `main()`

**Files:**
- Modify: `tools/orchestrate.py`

**Step 1: Add `run_georgia_pass` function**

Add this function just before `main()` in `tools/orchestrate.py` (around line 500):

```python
def run_georgia_pass(
    config: dict,
    effective_config: dict,
    jsearch_api_key: str,
    health_state: dict,
) -> list[dict]:
    """
    Run a JSearch-only search for Georgia on-site/hybrid QA jobs.
    Returns the final qualified_jobs list for this pass.
    Returns [] if georgia_search is disabled or JSearch key is missing.
    """
    georgia_cfg = config.get("georgia_search", {})
    if not georgia_cfg.get("enabled", False):
        log.info("Georgia pass: disabled (georgia_search.enabled=false)")
        return []

    if not jsearch_api_key:
        log.warning("Georgia pass: skipped — JSEARCH_API_KEY not set")
        return []

    log.info("=== Georgia search pass start ===")

    # Build a modified config with "in Georgia, USA" appended to all keywords.
    # Use effective_config (which has generated queries) as the base.
    georgia_config = _build_georgia_config(effective_config, georgia_cfg)

    raw_jobs = safe_fetch(
        "jsearch_georgia",
        fetch_jsearch.run,
        georgia_config,
        jsearch_api_key,
    )
    log.info(f"Georgia pass: {len(raw_jobs)} raw jobs from JSearch")

    # Location + job-type filters specific to this pass
    georgia_jobs = filter_by_georgia(raw_jobs, georgia_cfg)
    allowed_types = georgia_cfg.get("job_types", ["Hybrid", "On-site"])
    georgia_jobs = filter_by_job_type(georgia_jobs, allowed_types)

    if not georgia_jobs:
        log.info("Georgia pass: 0 jobs after location+job-type filters")
        return []

    # Re-use the same QA + ranking pipeline
    qa_rules = config.get("qa_filter", {})
    qa_jobs, _, _ = qa_relevance.filter_qa_jobs(georgia_jobs, qa_rules)
    scored = role_horizon.annotate_and_score_jobs(qa_jobs, config, health_state)
    qualified, _ = role_horizon.filter_by_fit_score(scored, config)

    log.info(f"Georgia pass: {len(qualified)} qualified jobs")
    return qualified
```

**Step 2: Call `run_georgia_pass` from `main()`**

In `main()`, find the Airtable push block (around line 650). Insert the Georgia pass call **between** the main pipeline evaluation and the Airtable push:

Locate this comment:
```python
    # ------------------------------------------------------------------
    # Push to Airtable
    # ------------------------------------------------------------------
```

Insert **before** it:

```python
    # ------------------------------------------------------------------
    # Georgia search overlay pass
    # ------------------------------------------------------------------

    georgia_qualified = run_georgia_pass(config, effective_config, jsearch_api_key, health_state)
    if georgia_qualified:
        # Merge with main qualified jobs, dedup by URL (first-seen wins)
        combined = merge_and_deduplicate([qualified_jobs, georgia_qualified])
        log.info(
            f"Georgia pass merged: {len(georgia_qualified)} Georgia jobs + "
            f"{len(qualified_jobs)} main jobs → {len(combined)} unique"
        )
        qualified_jobs = combined
    else:
        log.info("Georgia pass: no new jobs to merge")
```

**Step 3: Smoke-test the import and config parsing (no API call)**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tools')
import orchestrate
cfg = orchestrate.load_config()
g = cfg.get('georgia_search', {})
print('georgia_search enabled:', g.get('enabled'))
print('location:', g.get('location'))
print('location_aliases count:', len(g.get('location_aliases', [])))
"
```
Expected output:
```
georgia_search enabled: True
location: Georgia, USA
location_aliases count: 11
```

**Step 4: Run full test suite**

```bash
uv run pytest tests/ -v
```
Expected: all `PASSED`

**Step 5: Commit**

```bash
git add tools/orchestrate.py
git commit -m "feat(orchestrate): integrate Georgia search overlay pass into main pipeline"
```

---

### Task 5: Update run summary to include Georgia stats

**Files:**
- Modify: `tools/orchestrate.py`

**Step 1: Update summary dict in `main()`**

Find the `summary` dict (around line 730). Add two new keys after `"dropped_low_fit_count"`:

```python
        "georgia_qualified_count": len(georgia_qualified),
        "georgia_pass_enabled": bool(config.get("georgia_search", {}).get("enabled", False)),
```

**Step 2: Run full test suite**

```bash
uv run pytest tests/ -v
```
Expected: all `PASSED`

**Step 3: Commit**

```bash
git add tools/orchestrate.py
git commit -m "feat(orchestrate): add georgia_qualified_count to run summary"
```

---

### Task 6: Final validation

**Step 1: Confirm JSON config still valid**

```bash
uv run python -c "import json; json.load(open('config/job_search_config.json')); print('Config JSON: OK')"
```

**Step 2: Dry-run orchestrate import (no API keys needed)**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'tools')
import orchestrate
print('orchestrate imports: OK')
print('filter_by_georgia:', callable(orchestrate.filter_by_georgia))
print('filter_by_job_type:', callable(orchestrate.filter_by_job_type))
print('run_georgia_pass:', callable(orchestrate.run_georgia_pass))
"
```
Expected: all `True`

**Step 3: Full test suite one final time**

```bash
uv run pytest tests/ -v
```
Expected: all `PASSED`

**Step 4: Final commit (if any uncommitted changes remain)**

```bash
git status
```
If clean, no commit needed.

---

## Summary of Changes

| File | Change |
|------|--------|
| `config/job_search_config.json` | Add `georgia_search` block |
| `tools/orchestrate.py` | Add `filter_by_georgia`, `filter_by_job_type`, `_build_georgia_config`, `run_georgia_pass`; update `main()` |
| `tests/test_orchestrate_helpers.py` | Add tests for the four new helpers |

No changes to `fetch_jobs_jsearch.py`, `push_to_airtable.py`, or any other tool — the existing pipeline handles everything downstream.
