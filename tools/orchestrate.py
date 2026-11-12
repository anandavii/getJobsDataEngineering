"""
Main orchestrator for the daily job search automation.
Reads config, runs all fetch tools, merges and deduplicates results,
pushes to Supabase, and sends a webhook notification.

Usage:
    python tools/orchestrate.py

Environment variables (or .env file):
    JSEARCH_API_KEY      — RapidAPI key for JSearch
    SUPABASE_URL         — https://<ref>.supabase.co
    SUPABASE_SERVICE_KEY — Supabase service_role key
    DISCORD_WEBHOOK_URL  — Discord incoming webhook URL
"""

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone, UTC
from pathlib import Path
from collections.abc import Callable

from dotenv import load_dotenv
from config_loader import load_config as _load_config, ConfigLoadError

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - fallback for older Python
    ZoneInfo = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = ROOT / ".tmp"
# CONFIG_PATH = ROOT / "config" / "job_search_config.json"  # moved to config_loader

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("orchestrate")

# ---------------------------------------------------------------------------
# Tool imports (after sys.path is clean — tools/ is a sibling directory)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "tools"))
import fetch_jobs_jsearch as fetch_jsearch
import fetch_jobs_company_careers as fetch_company
import fetch_jobs_themuse as fetch_themuse
import fetch_jobs_greenhouse_generic as fetch_greenhouse
import fetch_jobs_lever_generic as fetch_lever
import fetch_jobs_ashby as fetch_ashby
import fetch_jobs_breezyhr as fetch_breezyhr
import fetch_jobs_smartrecruiters as fetch_smartrecruiters
import fetch_jobs_workable as fetch_workable
import fetch_jobs_icims as fetch_icims
import fetch_jobs_jobvite as fetch_jobvite
import fetch_jobs_workday as fetch_workday
import fetch_jobs_remotive as fetch_remotive
import fetch_jobs_jobicy as fetch_jobicy
import notify_webhook
import push_to_supabase
import domain_relevance
import role_horizon
import health_state as _health_state


DEFAULT_SOURCE_TIERS = {
    "tier1": [
        "jsearch",
        "company_careers",
        "greenhouse",
        "lever",
        "ashby",
        "breezyhr",
        "smartrecruiters",
        "workday",
        "remotive",
        "jobicy",
    ],
    "tier2": ["workable", "icims", "jobvite"],
    "tier3": ["themuse"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_config() -> dict:
    try:
        return _load_config()
    except ConfigLoadError as e:
        log.error(str(e))
        sys.exit(1)


def safe_fetch_with_meta(source_key: str, fn: Callable[[], list[dict]]) -> tuple[list[dict], dict]:
    started = time.monotonic()
    err: str | None = None
    jobs: list[dict] = []
    try:
        result = fn()
        if isinstance(result, list):
            jobs = result
        else:
            err = "non-list result"
            log.warning(f"{source_key}: returned non-list result — treating as empty")
    except Exception as e:
        err = str(e)
        log.error(f"{source_key}: FAILED — {e}", exc_info=True)

    duration = round(time.monotonic() - started, 3)
    meta = {
        "source": source_key,
        "success": err is None,
        "error": err,
        "duration_s": duration,
        "fetched_count": len(jobs),
        "empty": len(jobs) == 0,
        "skipped": False,
        "skip_reason": None,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }
    return jobs, meta


def merge_and_deduplicate(job_lists: list[list[dict]]) -> list[dict]:
    """
    Merge all source lists and deduplicate by job_url (lowercase).
    Source order determines priority (first occurrence wins).
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for jobs in job_lists:
        for job in jobs:
            url = (job.get("job_url") or "").strip().lower()
            if not url:
                continue  # skip jobs with no URL — can't dedup or upsert
            if url not in seen:
                seen.add(url)
                job["job_url"] = url  # normalize
                merged.append(job)
    return merged


def filter_by_location(jobs: list[dict]) -> list[dict]:
    """
    Keep only jobs in the USA or fully remote.
    Accepts: 'United States', 'USA', 'US', 'U.S.A', state abbreviations/names, 'Remote'.
    Jobs with no location default to passing (sourced from US-filtered APIs).
    """
    _USA_TEXT = frozenset({"united states", "usa", "u.s.", "u.s.a"})
    _US_STATE_CODES = frozenset(
        {
            "AL",
            "AK",
            "AZ",
            "AR",
            "CA",
            "CO",
            "CT",
            "DE",
            "FL",
            "GA",
            "HI",
            "ID",
            "IL",
            "IN",
            "IA",
            "KS",
            "KY",
            "LA",
            "ME",
            "MD",
            "MA",
            "MI",
            "MN",
            "MS",
            "MO",
            "MT",
            "NE",
            "NV",
            "NH",
            "NJ",
            "NM",
            "NY",
            "NC",
            "ND",
            "OH",
            "OK",
            "OR",
            "PA",
            "RI",
            "SC",
            "SD",
            "TN",
            "TX",
            "UT",
            "VT",
            "VA",
            "WA",
            "WV",
            "WI",
            "WY",
            "DC",
        }
    )
    _US_STATE_NAMES = frozenset(
        {
            "alabama",
            "alaska",
            "arizona",
            "arkansas",
            "california",
            "colorado",
            "connecticut",
            "delaware",
            "florida",
            "georgia",
            "hawaii",
            "idaho",
            "illinois",
            "indiana",
            "iowa",
            "kansas",
            "kentucky",
            "louisiana",
            "maine",
            "maryland",
            "massachusetts",
            "michigan",
            "minnesota",
            "mississippi",
            "missouri",
            "montana",
            "nebraska",
            "nevada",
            "new hampshire",
            "new jersey",
            "new mexico",
            "new york",
            "north carolina",
            "north dakota",
            "ohio",
            "oklahoma",
            "oregon",
            "pennsylvania",
            "rhode island",
            "south carolina",
            "south dakota",
            "tennessee",
            "texas",
            "utah",
            "vermont",
            "virginia",
            "washington",
            "west virginia",
            "wisconsin",
            "wyoming",
            "district of columbia",
        }
    )
    _US_COUNTRY_CODES = frozenset({"US", "USA"})

    def _keep(job: dict) -> bool:
        raw_loc = str(job.get("location") or "").strip()
        loc = raw_loc.lower()
        if not raw_loc:
            return True
        if "remote" in loc:
            return True
        if any(token in loc for token in _USA_TEXT):
            return True

        parts = [p.strip() for p in raw_loc.split(",") if p.strip()]
        if parts:
            first = parts[0]
            if len(first) in (2, 3) and first.isalpha():
                return first.upper() in _US_COUNTRY_CODES

            last = parts[-1]
            if len(last) == 2 and last.isalpha():
                return last.upper() in _US_STATE_CODES
            if last.lower() in _US_STATE_NAMES:
                return True

        if raw_loc.upper() in _US_COUNTRY_CODES:
            return True
        return False

    before = len(jobs)
    filtered = [j for j in jobs if _keep(j)]
    log.info(f"Location filter: {len(filtered)} kept, {before - len(filtered)} dropped (non-USA)")
    return filtered


def _load_timezone(tz_name: str | None) -> timezone:
    if not tz_name:
        return UTC
    if ZoneInfo is None:
        log.warning("Calendar-day filter: zoneinfo unavailable; using UTC")
        return UTC
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        log.warning(f"Calendar-day filter: invalid timezone '{tz_name}'; using UTC")
        return UTC


def filter_by_calendar_day(jobs: list[dict], config: dict) -> list[dict]:
    search_cfg = config.get("search", {}) if isinstance(config, dict) else {}
    filter_cfg = search_cfg.get("calendar_day_filter") or {}
    if not isinstance(filter_cfg, dict):
        filter_cfg = {}

    mode = str(filter_cfg.get("mode", "none")).strip().lower()
    if mode in {"", "none"}:
        log.info("Calendar-day filter: disabled")
        return jobs

    tz_name = filter_cfg.get("timezone") or "UTC"
    tzinfo = _load_timezone(tz_name)
    local_today = datetime.now(tz=tzinfo).date()
    drop_if_missing = bool(filter_cfg.get("drop_if_missing_date", True))

    if mode == "today":

        def _in_window(d: date) -> bool:
            return d == local_today

        window_label = f"today={local_today.isoformat()}"
    elif mode == "yesterday":
        cutoff = local_today - timedelta(days=1)

        def _in_window(d: date) -> bool:
            return d >= cutoff

        window_label = f">={cutoff.isoformat()}"
    elif mode == "last_days":
        try:
            days = int(filter_cfg.get("days", 1))
        except (TypeError, ValueError):
            days = 1
        if days < 1:
            days = 1
        cutoff = local_today - timedelta(days=days - 1)

        def _in_window(d: date) -> bool:
            return d >= cutoff

        window_label = f"last_days={days} (>= {cutoff.isoformat()})"
    else:
        log.warning(f"Calendar-day filter: unknown mode '{mode}', skipping")
        return jobs

    kept: list[dict] = []
    dropped_missing = 0
    dropped_outside = 0
    for job in jobs:
        date_str = job.get("date_posted")
        if not date_str:
            if drop_if_missing:
                dropped_missing += 1
                continue
            kept.append(job)
            continue

        try:
            posted = date.fromisoformat(str(date_str)[:10])
        except ValueError:
            if drop_if_missing:
                dropped_missing += 1
                continue
            kept.append(job)
            continue

        if _in_window(posted):
            kept.append(job)
        else:
            dropped_outside += 1

    log.info(
        "Calendar-day filter (%s, tz=%s, %s): %d kept, %d dropped_missing_date, %d dropped_outside_window",
        mode,
        tz_name,
        window_label,
        len(kept),
        dropped_missing,
        dropped_outside,
    )
    return kept


def _source_tiers(config: dict) -> dict:
    raw = config.get("source_tiers", {}) if isinstance(config, dict) else {}
    tiers = {
        "tier1": list(DEFAULT_SOURCE_TIERS["tier1"]),
        "tier2": list(DEFAULT_SOURCE_TIERS["tier2"]),
        "tier3": list(DEFAULT_SOURCE_TIERS["tier3"]),
    }
    if isinstance(raw, dict):
        for key in ("tier1", "tier2", "tier3"):
            if isinstance(raw.get(key), list):
                tiers[key] = [str(x) for x in raw[key] if str(x).strip()]

    # Remove duplicates while preserving first-tier priority.
    seen: set[str] = set()
    for key in ("tier1", "tier2", "tier3"):
        pruned: list[str] = []
        for src in tiers[key]:
            if src in seen:
                continue
            seen.add(src)
            pruned.append(src)
        tiers[key] = pruned

    return tiers


def _state_path(config: dict) -> Path:
    health = config.get("health", {}) if isinstance(config, dict) else {}
    rel = str(health.get("state_file", ".tmp/source_health_state.json"))
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_source_health_state(
    config: dict,
    supabase_url: str = "",
    service_key: str = "",
) -> dict:
    """Load health state. Tries Supabase first, falls back to local file."""
    if supabase_url and service_key:
        state = _health_state.load_from_supabase(supabase_url, service_key)
        if state:
            log.info(f"health: loaded state for {len(state)} sources from Supabase")
            return state
        log.warning("health: Supabase load returned empty — falling back to local file")
    path = _state_path(config)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_source_health_state(
    config: dict,
    state: dict,
    supabase_url: str = "",
    service_key: str = "",
) -> None:
    """Save health state to Supabase (primary) and local file (backup/debug artifact)."""
    if supabase_url and service_key:
        ok = _health_state.save_to_supabase(supabase_url, service_key, state)
        if ok:
            log.info("health: state saved to Supabase")
        else:
            log.warning("health: Supabase save had partial failures — check logs above")
    # Always write local file as backup (uploaded as GitHub Actions artifact)
    path = _state_path(config)
    try:
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"health: local backup write failed: {e}")
    # One-line summary in GitHub Actions log
    health_cfg = config.get("health", {}) if isinstance(config, dict) else {}
    alert_threshold = int(health_cfg.get("alert_consecutive_failures_threshold", 1) or 1)
    down = [s for s, r in state.items() if int(r.get("consecutive_failures", 0) or 0) >= alert_threshold]
    total = len(state)
    if down:
        log.info(f"Health: {total - len(down)}/{total} sources healthy, degraded: {', '.join(sorted(down))}")
    else:
        log.info(f"Health: {total}/{total} sources healthy")


def _health_row(state: dict, source: str) -> dict:
    row = state.get(source)
    if isinstance(row, dict):
        return row
    row = {
        "runs": 0,
        "successes": 0,
        "failures": 0,
        "empties": 0,
        "avg_duration_s": 0.0,
        "last_success_utc": None,
        "consecutive_failures": 0,
    }
    state[source] = row
    return row


def is_circuit_open(source: str, config: dict, state: dict) -> bool:
    health_cfg = config.get("health", {}) if isinstance(config, dict) else {}
    max_fail = health_cfg.get("max_consecutive_failures", 3)
    try:
        max_fail = int(max_fail)
    except (TypeError, ValueError):
        max_fail = 3
    row = _health_row(state, source)
    return int(row.get("consecutive_failures", 0) or 0) >= max_fail


def update_source_health(state: dict, meta: dict) -> None:
    source = str(meta.get("source") or "unknown")
    row = _health_row(state, source)

    if meta.get("skipped"):
        return

    runs = int(row.get("runs", 0) or 0) + 1
    row["runs"] = runs

    duration = float(meta.get("duration_s", 0.0) or 0.0)
    prev_avg = float(row.get("avg_duration_s", 0.0) or 0.0)
    row["avg_duration_s"] = round(((prev_avg * (runs - 1)) + duration) / runs, 4)

    if meta.get("success"):
        row["successes"] = int(row.get("successes", 0) or 0) + 1
        if meta.get("empty"):
            row["empties"] = int(row.get("empties", 0) or 0) + 1
        row["consecutive_failures"] = 0
        row["last_success_utc"] = meta.get("timestamp")
    else:
        row["failures"] = int(row.get("failures", 0) or 0) + 1
        row["consecutive_failures"] = int(row.get("consecutive_failures", 0) or 0) + 1


def save_tmp(filename: str, data) -> None:
    TMP_DIR.mkdir(exist_ok=True)
    path = TMP_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Saved: {path}")


def _fetch_plan(config: dict, effective_config: dict, jsearch_api_key: str) -> dict[str, Callable[[], list[dict]]]:
    src_flags = config.get("sources", {}) if isinstance(config, dict) else {}
    catalog = effective_config.get("sources_catalog", {}) if isinstance(effective_config, dict) else {}

    has_catalog_entries = {
        key: any(bool(x.get("enabled", True)) for x in val) for key, val in catalog.items() if isinstance(val, list)
    }

    plan: dict[str, Callable[[], list[dict]]] = {}

    if src_flags.get("jsearch", True):
        if jsearch_api_key:
            plan["jsearch"] = lambda: fetch_jsearch.run(effective_config, jsearch_api_key)
        else:
            log.warning("JSearch: JSEARCH_API_KEY not set — skipping")

    if src_flags.get("company_careers", True):
        plan["company_careers"] = lambda: fetch_company.run(effective_config)

    if has_catalog_entries.get("greenhouse"):
        plan["greenhouse"] = lambda: fetch_greenhouse.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("lever"):
        plan["lever"] = lambda: fetch_lever.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("ashby"):
        plan["ashby"] = lambda: fetch_ashby.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("breezyhr"):
        plan["breezyhr"] = lambda: fetch_breezyhr.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("smartrecruiters"):
        plan["smartrecruiters"] = lambda: fetch_smartrecruiters.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("workday"):
        plan["workday"] = lambda: fetch_workday.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("workable"):
        plan["workable"] = lambda: fetch_workable.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("icims"):
        plan["icims"] = lambda: fetch_icims.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )
    if has_catalog_entries.get("jobvite"):
        plan["jobvite"] = lambda: fetch_jobvite.run(
            effective_config, effective_config.get("search", {}).get("keywords", [])
        )

    # Free sources — no API key required; respect sources flag (default: enabled)
    if src_flags.get("remotive", True):
        plan["remotive"] = lambda: fetch_remotive.run(effective_config)
    if src_flags.get("jobicy", True):
        plan["jobicy"] = lambda: fetch_jobicy.run(effective_config)
    if src_flags.get("themuse", True):
        plan["themuse"] = lambda: fetch_themuse.run(effective_config)

    return plan


def _evaluate_pipeline(raw_source_jobs: dict[str, list[dict]], config: dict, source_health: dict) -> dict:
    merged = merge_and_deduplicate(list(raw_source_jobs.values()))
    after_location = filter_by_location(merged)
    after_calendar = filter_by_calendar_day(after_location, config)

    domain_rules = config.get("domain_filter", {})
    domain_filtered_jobs, rejected_out_of_domain_jobs, domain_metrics = domain_relevance.filter_domain_jobs(
        after_calendar, domain_rules
    )

    scored_jobs = role_horizon.annotate_and_score_jobs(domain_filtered_jobs, config, source_health)
    qualified_jobs, rejected_low_fit_jobs = role_horizon.filter_by_fit_score(scored_jobs, config)

    return {
        "merged_jobs": merged,
        "all_jobs": after_calendar,
        "domain_filtered_jobs": domain_filtered_jobs,
        "rejected_out_of_domain_jobs": rejected_out_of_domain_jobs,
        "domain_metrics": domain_metrics,
        "scored_jobs": scored_jobs,
        "qualified_jobs": qualified_jobs,
        "rejected_low_fit_jobs": rejected_low_fit_jobs,
    }


def _source_file_name(source_key: str) -> str:
    mapping = {
        "jsearch": "jsearch_jobs.json",
        "company_careers": "company_jobs.json",
        "remotive": "remotive_jobs.json",
        "jobicy": "jobicy_jobs.json",
        "themuse": "themuse_jobs.json",
    }
    return mapping.get(source_key, f"{source_key}_jobs.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(ROOT / ".env")

    jsearch_api_key = os.getenv("JSEARCH_API_KEY", "")
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    discord_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    email_to = os.getenv("EMAIL_TO", "")
    email_from = os.getenv("EMAIL_FROM", "")
    email_password = os.getenv("EMAIL_APP_PASSWORD", "")

    config = load_config()
    run_start = datetime.now(tz=UTC).isoformat()
    log.info(f"=== Daily {config.get('identity', {}).get('display_name', 'Job Search')} — {run_start} ===")

    generated_queries = role_horizon.generate_queries(config)
    effective_config = role_horizon.with_generated_queries(config, generated_queries)
    log.info(f"Query generation: {len(generated_queries)} queries active")
    save_tmp("generated_queries.json", generated_queries)

    health_state = load_source_health_state(effective_config, supabase_url=supabase_url, service_key=supabase_key)

    fetch_plan = _fetch_plan(config, effective_config, jsearch_api_key)
    tiers = _source_tiers(effective_config)

    source_results: dict[str, list[dict]] = {}
    source_execution: list[dict] = []
    source_to_tier: dict[str, str] = {}

    for tier_name, tier_sources in tiers.items():
        for source_key in tier_sources:
            if source_key in source_to_tier:
                continue
            source_to_tier[source_key] = tier_name

    # Execute sources tier-by-tier with fallback activation.
    for tier_name in ("tier1", "tier2", "tier3"):
        tier_sources = tiers.get(tier_name, [])
        if not tier_sources:
            continue

        log.info(f"Source tier execution: {tier_name} ({', '.join(tier_sources)})")

        futures: dict = {}
        circuit_open_metas: list[dict] = []

        with ThreadPoolExecutor() as executor:
            for source_key in tier_sources:
                if source_key not in fetch_plan:
                    continue
                if is_circuit_open(source_key, effective_config, health_state):
                    meta = {
                        "source": source_key,
                        "success": False,
                        "error": "circuit_open",
                        "duration_s": 0.0,
                        "fetched_count": 0,
                        "empty": True,
                        "skipped": True,
                        "skip_reason": "consecutive_failures_limit",
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                    }
                    circuit_open_metas.append(meta)
                    log.warning(f"{source_key}: skipped (circuit open)")
                    continue
                futures[executor.submit(safe_fetch_with_meta, source_key, fetch_plan[source_key])] = source_key

        for meta in circuit_open_metas:
            source_execution.append(meta)

        for future in as_completed(futures):
            source_key = futures[future]
            jobs, meta = future.result()
            for job in jobs:
                job["source"] = job.get("source") or source_key
                job["source_tier"] = tier_name
            source_results[source_key] = jobs
            source_execution.append(meta)
            update_source_health(health_state, meta)
            save_tmp(_source_file_name(source_key), jobs)
            log.info(f"{source_key}: {len(jobs)} jobs fetched")

        preview = _evaluate_pipeline(source_results, effective_config, {"state": health_state})
        preview_qualified = len(preview["qualified_jobs"])
        log.info(f"Tier checkpoint after {tier_name}: {preview_qualified} qualified jobs")

        if tier_name != "tier3" and not role_horizon.should_enable_next_tier(preview_qualified, effective_config):
            log.info(f"Tier fallback: stopping after {tier_name} (threshold reached)")
            break

    final_eval = _evaluate_pipeline(source_results, effective_config, {"state": health_state})

    all_jobs = final_eval["all_jobs"]
    domain_filtered_jobs = final_eval["domain_filtered_jobs"]
    rejected_out_of_domain_jobs = final_eval["rejected_out_of_domain_jobs"]
    domain_metrics = final_eval["domain_metrics"]
    scored_jobs = final_eval["scored_jobs"]
    qualified_jobs = final_eval["qualified_jobs"]
    rejected_low_fit_jobs = final_eval["rejected_low_fit_jobs"]

    pre_domain_filter_count = len(all_jobs)
    post_domain_filter_count = len(domain_filtered_jobs)
    dropped_out_of_domain_count = len(rejected_out_of_domain_jobs)
    post_fit_score_count = len(qualified_jobs)
    dropped_low_fit_count = len(rejected_low_fit_jobs)

    log.info(
        "Domain relevance filter: %d kept, %d dropped as out of domain",
        post_domain_filter_count,
        dropped_out_of_domain_count,
    )
    log.info(
        "Fit score gate: %d kept, %d dropped below threshold",
        post_fit_score_count,
        dropped_low_fit_count,
    )

    save_tmp("all_jobs.json", all_jobs)
    save_tmp("domain_filtered_jobs.json", domain_filtered_jobs)

    rejected_limit = (effective_config.get("domain_filter", {}) or {}).get("rejected_sample_limit", 200)
    try:
        rejected_limit = int(rejected_limit)
    except (TypeError, ValueError):
        rejected_limit = 200
    if rejected_limit < 0:
        rejected_limit = 0

    save_tmp("rejected_out_of_domain_jobs.json", rejected_out_of_domain_jobs[:rejected_limit])
    save_tmp("scored_jobs.json", scored_jobs)
    save_tmp("qualified_jobs.json", qualified_jobs)
    save_tmp("rejected_low_fit_jobs.json", rejected_low_fit_jobs[:rejected_limit])

    query_perf = role_horizon.build_query_performance(all_jobs, qualified_jobs)
    save_tmp("query_performance.json", query_perf)

    source_health = {
        "run": source_execution,
        "state": health_state,
    }
    save_tmp("source_health.json", source_health)
    save_source_health_state(effective_config, health_state, supabase_url=supabase_url, service_key=supabase_key)

    source_counts = {k: len(v) for k, v in source_results.items()}
    source_counts.update(
        {
            "total_unique": len(all_jobs),
            "total_after_domain_filter": len(domain_filtered_jobs),
            "total_after_fit_score": len(qualified_jobs),
        }
    )

    # ------------------------------------------------------------------
    # Push to Supabase
    # ------------------------------------------------------------------

    push_result = {
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "cleaned": 0,
        "error": None,
    }

    if not supabase_url or not supabase_key:
        log.warning("Supabase: SUPABASE_URL or SUPABASE_SERVICE_KEY not set — skipping push")
        push_result["error"] = "Supabase credentials not configured"
    else:
        push_result = push_to_supabase.run(qualified_jobs, supabase_url, supabase_key, effective_config)
        save_tmp("supabase_push_result.json", push_result)

    # ------------------------------------------------------------------
    # Push rejected jobs to Supabase rejected_jobs table (non-critical)
    # ------------------------------------------------------------------

    if supabase_url and supabase_key:
        rejected_jobs: list[dict] = []
        for job in rejected_out_of_domain_jobs:
            annotated = dict(job)
            annotated["rejection_type"] = "Out of Domain"
            annotated["rejection_reason"] = job.get("_domain_filter_reason") or ""
            rejected_jobs.append(annotated)
        for job in rejected_low_fit_jobs:
            annotated = dict(job)
            annotated["rejection_type"] = "Low Fit Score"
            annotated["rejection_reason"] = job.get("_fit_filter_reason") or ""
            rejected_jobs.append(annotated)

        log.info(
            f"Supabase rejected push: {len(rejected_jobs)} total ({len(rejected_out_of_domain_jobs)} out of domain, {len(rejected_low_fit_jobs)} low-fit)"
        )

        try:
            rejected_push_result = push_to_supabase.run(
                rejected_jobs,
                supabase_url,
                supabase_key,
                effective_config,
                table="rejected_jobs",
                cleanup_days=7,
            )
            log.info(
                f"Supabase rejected push: {rejected_push_result.get('inserted', 0)} inserted, "
                f"{rejected_push_result.get('cleaned', 0)} cleaned"
            )
            if rejected_push_result.get("error"):
                log.warning(f"Supabase rejected push error: {rejected_push_result['error']}")
            save_tmp("supabase_rejected_push_result.json", rejected_push_result)
        except Exception as e:
            log.warning(f"Supabase rejected push failed (non-critical): {e}")

    # ------------------------------------------------------------------
    # Send notifications (Email + Discord)
    # ------------------------------------------------------------------

    inserted_urls = set(push_result.get("inserted_urls") or [])
    if inserted_urls:
        new_jobs_for_notify = [j for j in qualified_jobs if j.get("job_url", "").lower() in inserted_urls]
    else:
        new_jobs_for_notify = []

    notify_webhook.run(
        push_result,
        new_jobs_for_notify,
        effective_config,
        discord_url=discord_url,
        email_to=email_to,
        email_from=email_from,
        email_password=email_password,
        source_execution=source_execution,
        health_state=health_state,
    )

    # ------------------------------------------------------------------
    # Write run summary
    # ------------------------------------------------------------------

    run_end = datetime.now(tz=UTC).isoformat()
    summary = {
        "run_start": run_start,
        "run_end": run_end,
        "source_counts": source_counts,
        "pre_domain_filter_count": pre_domain_filter_count,
        "post_domain_filter_count": post_domain_filter_count,
        "dropped_out_of_domain_count": dropped_out_of_domain_count,
        "post_fit_score_count": post_fit_score_count,
        "dropped_low_fit_count": dropped_low_fit_count,
        "dropped_out_of_domain_by_source": domain_metrics.get("dropped_out_of_domain_by_source", {}),
        "drop_reasons": domain_metrics.get("drop_reasons", {}),
        "query_count": len(generated_queries),
        "tiers_executed": sorted({source_to_tier.get(meta.get("source", ""), "unknown") for meta in source_execution}),
        "supabase": push_result,
    }
    save_tmp("run_summary.json", summary)

    log.info("=== Run complete ===")
    log.info(json.dumps(summary, indent=2, default=str))

    if (
        push_result.get("error")
        and "credentials not configured" not in (push_result["error"] or "")
        and "not configured" not in (push_result["error"] or "")
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
