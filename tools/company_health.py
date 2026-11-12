"""Per-company health tracking for job sources."""

import logging
from datetime import datetime, UTC
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_TABLE = "company_source_health"
_TIMEOUT = 10


def _headers(key: str, *, prefer: str = "") -> dict:
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _compute_days_empty(last_nonempty_at) -> int:
    if last_nonempty_at is None:
        return 0
    last = datetime.fromisoformat(last_nonempty_at.replace("Z", "+00:00"))
    return (datetime.now(UTC) - last).days


def _derive_status(days_empty: int) -> str:
    if days_empty >= 7:
        return "critical"
    elif days_empty >= 3:
        return "monitor"
    return "healthy"


def _update_daily_counts(existing_counts: list, jobs_found: int, updated_at) -> list:
    if not existing_counts:
        return [0, 0, 0, 0, 0, 0, jobs_found]
    counts = list(existing_counts)
    while len(counts) < 7:
        counts.insert(0, 0)
    counts = counts[-7:]
    now = datetime.now(UTC)
    same_day = False
    if updated_at:
        last = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        same_day = (now.year, now.month, now.day) == (last.year, last.month, last.day)
    if same_day:
        counts[-1] = jobs_found
    else:
        counts = counts[1:] + [jobs_found]
    return counts


def upsert_company_health(
    company: str,
    ats_type: str,
    ats_token: str,
    job_board_url: str,
    jobs_found: int,
    supabase_url: str,
    supabase_key: str,
) -> bool:
    if not supabase_url or not supabase_key:
        return False
    try:
        url = f"{supabase_url.rstrip('/')}/rest/v1/{_TABLE}"
        fetch_url = f"{url}?select=*&company=eq.{quote(company)}&ats_type=eq.{quote(ats_type)}"
        resp = requests.get(fetch_url, headers=_headers(supabase_key), timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
        existing = rows[0] if rows else None

        now = datetime.now(UTC).isoformat()
        if jobs_found > 0:
            last_nonempty_at = now
            days_empty = 0
        else:
            last_nonempty_at = existing.get("last_nonempty_at") if existing else None
            days_empty = _compute_days_empty(last_nonempty_at)

        existing_counts = existing.get("daily_counts", []) if existing else []
        existing_updated_at = existing.get("updated_at") if existing else None
        daily_counts = _update_daily_counts(existing_counts, jobs_found, existing_updated_at)
        status = _derive_status(days_empty)

        payload = {
            "company": company,
            "ats_type": ats_type,
            "ats_token": ats_token,
            "job_board_url": job_board_url,
            "last_nonempty_at": last_nonempty_at,
            "days_empty": days_empty,
            "daily_counts": daily_counts,
            "status": status,
            "updated_at": now,
        }

        post_resp = requests.post(
            url,
            json=payload,
            headers=_headers(
                supabase_key,
                prefer="resolution=merge-duplicates,return=minimal",
            ),
            timeout=_TIMEOUT,
        )
        post_resp.raise_for_status()

        logger.info(
            f"Updated company health: {company} ({ats_type}) - {jobs_found} jobs, {days_empty}d empty, status={status}"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to upsert company health for {company} ({ats_type}): {e}")
        return False


def load_company_health(supabase_url: str, supabase_key: str) -> list[dict]:
    if not supabase_url or not supabase_key:
        return []
    try:
        url = f"{supabase_url.rstrip('/')}/rest/v1/{_TABLE}?select=*&order=days_empty.desc"
        resp = requests.get(url, headers=_headers(supabase_key), timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
        return sorted(rows or [], key=lambda r: r.get("days_empty", 0), reverse=True)
    except Exception as e:
        logger.error(f"Failed to load company health: {e}")
        return []
