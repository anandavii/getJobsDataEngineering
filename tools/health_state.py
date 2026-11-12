"""
Persistent source health state backed by Supabase.

Follows the same PostgREST upsert pattern as push_to_supabase.py:
  POST + Prefer: resolution=merge-duplicates

Functions
---------
load_from_supabase(url, key) -> dict   keyed by source name, {} on error
save_to_supabase(url, key, state)      upserts all rows, returns True if all ok
"""

import logging

import requests

log = logging.getLogger(__name__)

_TABLE = "source_health_state"
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


def load_from_supabase(supabase_url: str, service_key: str) -> dict:
    """Load all source health rows from Supabase.

    Returns a dict keyed by source name, or {} on any error.
    Errors are logged as warnings — health state loss is non-fatal.
    """
    if not supabase_url or not service_key:
        return {}
    url = f"{supabase_url.rstrip('/')}/rest/v1/{_TABLE}?select=*"
    try:
        resp = requests.get(url, headers=_headers(service_key), timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json()
        return {row["source"]: row for row in rows if "source" in row}
    except Exception as e:
        log.warning(f"health_state: failed to load from Supabase: {e}")
        return {}


def save_to_supabase(supabase_url: str, service_key: str, state: dict) -> bool:
    """Upsert all source rows to Supabase.

    Sends one POST per source (upsert via merge-duplicates).
    Returns True if every row saved successfully, False if any failed.
    Partial failures are logged but never raise.
    """
    if not supabase_url or not service_key or not state:
        return False
    url = f"{supabase_url.rstrip('/')}/rest/v1/{_TABLE}"
    all_ok = True
    for source, row in state.items():
        payload = {
            "source": source,
            "runs": int(row.get("runs", 0) or 0),
            "successes": int(row.get("successes", 0) or 0),
            "failures": int(row.get("failures", 0) or 0),
            "empties": int(row.get("empties", 0) or 0),
            "avg_duration_s": float(row.get("avg_duration_s", 0.0) or 0.0),
            "last_success_utc": row.get("last_success_utc"),
            "consecutive_failures": int(row.get("consecutive_failures", 0) or 0),
        }
        try:
            resp = requests.post(
                url,
                json=payload,
                headers=_headers(
                    service_key,
                    prefer="resolution=merge-duplicates,return=minimal",
                ),
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"health_state: failed to save source '{source}': {e}")
            all_ok = False
    return all_ok
