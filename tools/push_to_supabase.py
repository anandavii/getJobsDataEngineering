"""
Push canonical job dicts to Supabase (Postgres via PostgREST REST API).
Deduplicates by 'url' column (UNIQUE constraint). Cleans up records older
than configured threshold before inserting.

Usage:
    result = run(jobs, supabase_url, supabase_key, config)
    # result: {"inserted": int, "updated": int, "skipped": int, "cleaned": int, "error": str|None}

Environment variables:
    SUPABASE_URL          — https://<ref>.supabase.co
    SUPABASE_SERVICE_KEY  — service_role key (bypasses RLS)
"""

import logging
from datetime import datetime, timedelta, UTC

import requests

log = logging.getLogger(__name__)

MATCHED_TABLE_DEFAULT = "matched_jobs"
CLEANUP_DAYS_DEFAULT = 90
BATCH_SIZE_DEFAULT = 50
REQUEST_TIMEOUT = 30


def _headers(supabase_key: str, prefer: str = "") -> dict:
    h = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _map_job_type(job_type: str) -> str:
    valid = {"Remote", "Hybrid", "On-site", "Unknown"}
    return job_type if job_type in valid else "Unknown"


def _to_supabase_row(job: dict, today: str) -> dict:
    """Convert a canonical job dict to a matched_jobs table row.
    All columns are always present (None for nullables) so PostgREST
    receives uniform key sets across every row in a batch (PGRST102 fix).
    """
    matched_titles = job.get("matched_titles")
    matched_skills = job.get("matched_skills")
    return {
        "url": (job.get("job_url") or "").strip().lower(),
        "job_title": job.get("job_title") or "",
        "company": job.get("company") or None,
        "location": job.get("location") or None,
        "job_type": _map_job_type(job.get("job_type", "Unknown")),
        "source": job.get("source") or None,
        "description_snippet": (job.get("description_snippet") or "")[:500] or None,
        "fetched_at": datetime.now(tz=UTC).isoformat(),
        "date_posted": job.get("date_posted") or None,
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        "salary_currency": job.get("salary_currency") or None,
        "role_family": job.get("role_family") or None,
        "seniority_guess": job.get("seniority_guess") or None,
        "fit_score": job.get("fit_score"),
        "source_tier": job.get("source_tier") or None,
        "matched_titles": ", ".join(str(x) for x in matched_titles) if matched_titles else None,
        "matched_skills": ", ".join(str(x) for x in matched_skills) if matched_skills else None,
    }


def _to_rejected_supabase_row(job: dict) -> dict:
    """Convert a canonical job dict (with rejection metadata) to a rejected_jobs table row.
    All columns always present for uniform batch key sets (PGRST102 fix).
    """
    return {
        "url": (job.get("job_url") or "").strip().lower(),
        "job_title": job.get("job_title") or "",
        "company": job.get("company") or None,
        "location": job.get("location") or None,
        "job_type": _map_job_type(job.get("job_type", "Unknown")),
        "source": job.get("source") or None,
        "description_snippet": (job.get("description_snippet") or "")[:500] or None,
        "fetched_at": datetime.now(tz=UTC).isoformat(),
        "date_posted": job.get("date_posted") or None,
        "fit_score": job.get("fit_score"),
        "rejection_type": job.get("rejection_type") or None,
        "rejection_reason": job.get("rejection_reason") or None,
    }


def _cleanup_old_records(
    base_url: str,
    key: str,
    table: str,
    cleanup_days: int,
) -> int:
    """
    Delete old records. Returns count of deleted records.
    """
    cutoff = (datetime.now(tz=UTC) - timedelta(days=cleanup_days)).isoformat()
    url = f"{base_url}/rest/v1/{table}"

    params: dict = {"fetched_at": f"lt.{cutoff}"}

    log.info(f"Supabase cleanup ({table}): deleting records with fetched_at < {cutoff[:10]}")
    try:
        resp = requests.delete(
            url,
            params=params,
            headers=_headers(key, prefer="return=representation"),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        deleted = len(resp.json()) if resp.text and resp.text != "[]" else 0
        log.info(f"Supabase cleanup ({table}): {deleted} records deleted")
        return deleted
    except Exception as e:
        log.warning(f"Supabase cleanup failed (non-fatal): {e}")
        return 0


def _fetch_existing_urls(
    base_url: str,
    key: str,
    table: str,
    urls: list[str],
    batch_size: int,
) -> set[str]:
    existing: set[str] = set()
    if not urls:
        return existing

    endpoint = f"{base_url}/rest/v1/{table}"
    chunk_size = max(1, min(batch_size, 100))
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        quoted = ",".join(f'"{url}"' for url in chunk)
        params = {"select": "url", "url": f"in.({quoted})"}
        try:
            resp = requests.get(
                endpoint,
                params=params,
                headers=_headers(key),
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            for row in resp.json() if resp.text else []:
                url = str((row or {}).get("url") or "").strip().lower()
                if url:
                    existing.add(url)
        except Exception as e:
            log.warning(f"Supabase existing-url check failed (non-fatal): {e}")
            return set()
    return existing


def _upsert_batch(
    base_url: str,
    key: str,
    table: str,
    rows: list[dict],
) -> tuple[list[dict], str | None]:
    """
    Upsert a batch of rows. Returns (upserted_rows, error_str).
    On conflict on `url`, existing rows are updated (merge-duplicates).
    """
    url = f"{base_url}/rest/v1/{table}?on_conflict=url"
    try:
        resp = requests.post(
            url,
            json=rows,
            headers=_headers(key, prefer="resolution=merge-duplicates,return=representation"),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json() if resp.text else [], None
    except requests.HTTPError as e:
        return [], f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return [], str(e)


def run(
    jobs: list[dict],
    supabase_url: str,
    supabase_key: str,
    config: dict,
    table: str | None = None,
    cleanup_days: int | None = None,
) -> dict:
    """
    Upsert jobs into Supabase. Returns a result summary dict.

    Pass table='rejected_jobs' and cleanup_days=3 for the rejected jobs table.
    """
    sb_cfg = config.get("supabase", {}) if config else {}
    is_rejected = table is not None and "rejected" in (table or "")

    if table is None:
        table = sb_cfg.get("matched_table", MATCHED_TABLE_DEFAULT)
    if cleanup_days is None:
        cleanup_days = sb_cfg.get("cleanup_older_than_days", CLEANUP_DAYS_DEFAULT)
    batch_size = sb_cfg.get("batch_size", BATCH_SIZE_DEFAULT)

    result: dict = {
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "cleaned": 0,
        "error": None,
        "inserted_urls": [],
    }

    if not jobs:
        log.info(f"Supabase ({table}): no jobs to push")
        return result

    if not supabase_url or not supabase_key:
        result["error"] = "Supabase credentials not configured"
        log.warning(result["error"])
        return result

    base_url = supabase_url.rstrip("/")

    # 1. Cleanup old records
    try:
        cleaned = _cleanup_old_records(base_url, supabase_key, table, cleanup_days)
        result["cleaned"] = cleaned
    except Exception as e:
        log.warning(f"Cleanup step failed (non-fatal): {e}")

    # 2. Filter out jobs missing required fields
    today = datetime.now(tz=UTC).date().isoformat()
    row_mapper = _to_rejected_supabase_row if is_rejected else lambda j: _to_supabase_row(j, today)

    valid_rows: list[dict] = []
    skipped = 0
    for j in jobs:
        url_val = (j.get("job_url") or "").strip()
        if not url_val:
            log.warning(f"Skipping record with no job_url — source: {j.get('source')}, title: {j.get('job_title')!r}")
            skipped += 1
        elif not j.get("job_title"):
            log.warning(f"Skipping record with no job_title — source: {j.get('source')}, url: {url_val!r}")
            skipped += 1
        else:
            valid_rows.append(row_mapper(j))
    result["skipped"] = skipped

    if not valid_rows:
        log.info(f"Supabase ({table}): no valid jobs after filtering")
        return result

    # Track which URLs are genuinely new before upsert so notifications only
    # announce newly inserted jobs.
    pushed_urls = [row["url"] for row in valid_rows]
    existing_urls = set()
    if not is_rejected:
        existing_urls = _fetch_existing_urls(base_url, supabase_key, table, pushed_urls, batch_size)
    new_urls = [url for url in pushed_urls if url not in existing_urls]

    # 3. Batch upsert — for matched_jobs, split into two uniform-key batches:
    #    new rows get first_seen_at stamped; existing rows omit it so
    #    merge-duplicates preserves the original value (PGRST102 requirement).
    if not is_rejected:
        now_iso = datetime.now(tz=UTC).isoformat()
        new_url_set = set(new_urls)
        new_valid_rows = [{**row, "first_seen_at": now_iso} for row in valid_rows if row["url"] in new_url_set]
        existing_valid_rows = [row for row in valid_rows if row["url"] not in new_url_set]
        batches_to_upsert = [new_valid_rows, existing_valid_rows]
    else:
        batches_to_upsert = [valid_rows]

    log.info(f"Supabase ({table}): upserting {len(valid_rows)} rows in batches of {batch_size}")
    all_returned: list[dict] = []
    for batch_rows in batches_to_upsert:
        for i in range(0, len(batch_rows), batch_size):
            batch = batch_rows[i : i + batch_size]
            returned, err = _upsert_batch(base_url, supabase_key, table, batch)
            if err:
                result["error"] = f"Supabase upsert failed: {err}"
                log.error(result["error"])
                return result
            all_returned.extend(returned)

    result["inserted"] = len(new_urls)
    result["updated"] = max(0, len(all_returned) - result["inserted"])
    result["inserted_urls"] = new_urls[: result["inserted"]]

    log.info(f"Supabase ({table}): {result['inserted']} rows upserted successfully")
    return result
