"""
Fetch QA/automation jobs from JSearch RapidAPI.
JSearch aggregates LinkedIn, Indeed, Glassdoor, ZipRecruiter, and more.

Usage:
    results = run(config, api_key)
    # returns list of canonical job dicts
"""

import logging
import time
from datetime import datetime, timedelta, UTC

import requests

log = logging.getLogger(__name__)

JSEARCH_BASE_URL = "https://jsearch.p.rapidapi.com/search"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_KEYWORDS = 1  # seconds — stay well under rate limits


class _QuotaExhausted(Exception):
    """Raised when JSearch returns a 429 quota-exceeded response."""


def _normalize_job_type(employment_type: str | None) -> str:
    if not employment_type:
        return "Unknown"
    et = employment_type.upper()
    if "REMOTE" in et or "TELECOMMUTE" in et:
        return "Remote"
    if "HYBRID" in et:
        return "Hybrid"
    if "FULL" in et or "PART" in et or "CONTRACTOR" in et or "INTERN" in et:
        return "On-site"
    return "Unknown"


def _parse_salary(job: dict) -> tuple[float | None, float | None, str | None]:
    min_sal = job.get("job_min_salary")
    max_sal = job.get("job_max_salary")
    currency = job.get("job_salary_currency") or ("USD" if (min_sal or max_sal) else None)
    return (
        float(min_sal) if min_sal is not None else None,
        float(max_sal) if max_sal is not None else None,
        currency,
    )


def _normalize_location(job: dict) -> str:
    """
    Build a stable location string from JSearch fields.
    JSearch often returns city-only values; include state/country so downstream
    US filtering can classify locations correctly.
    """
    if job.get("job_is_remote"):
        return "Remote"

    city = (job.get("job_city") or "").strip()
    state = (job.get("job_state") or "").strip()
    country = (job.get("job_country") or "").strip()
    parts = [p for p in (city, state, country) if p]
    if parts:
        return ", ".join(parts)
    return "United States"


def _to_canonical(job: dict) -> dict:
    salary_min, salary_max, salary_currency = _parse_salary(job)

    # Prefer apply link, fall back to job listing URL
    job_url = (job.get("job_apply_link") or job.get("job_url") or "").strip()

    # date_posted: JSearch returns epoch seconds in job_posted_at_timestamp
    date_posted = None
    ts = job.get("job_posted_at_timestamp")
    if ts:
        try:
            date_posted = datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()
        except (ValueError, TypeError):
            pass
    if not date_posted:
        date_posted = job.get("job_posted_at_datetime_utc", "")[:10] or None

    description = (job.get("job_description") or "")[:500]

    return {
        "job_title": job.get("job_title", "").strip(),
        "company": job.get("employer_name", "").strip(),
        "location": _normalize_location(job),
        "job_type": _normalize_job_type(job.get("job_employment_type")),
        "job_url": job_url.lower(),
        "description_snippet": description,
        "date_posted": date_posted,
        "source": "jsearch",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "_ts": job.get("job_posted_at_timestamp"),
    }


def _fetch_keyword(keyword: str, api_key: str, config: dict) -> list[dict]:
    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query": keyword,
        "date_posted": config["search"].get("date_posted", "today"),
        "num_pages": str(config["search"].get("jsearch_num_pages", 1)),
        "country": "us",
    }

    try:
        resp = requests.get(JSEARCH_BASE_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            remaining = resp.headers.get("X-RateLimit-Requests-Remaining", "?")
            limit = resp.headers.get("X-RateLimit-Requests-Limit", "?")
            reset_secs = resp.headers.get("X-RateLimit-Requests-Reset", "?")
            try:
                body_msg = resp.json().get("message", "")
            except Exception:
                body_msg = resp.text[:200]
            log.error(
                f"JSearch QUOTA EXCEEDED for keyword '{keyword}': {body_msg} "
                f"(remaining {remaining} of {limit} monthly requests; "
                f"resets in {reset_secs}s). "
                f"Upgrade plan at https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch"
            )
            raise _QuotaExhausted(body_msg)
        resp.raise_for_status()
        data = resp.json()
    # _QuotaExhausted is not a subclass of requests.RequestException
    # and intentionally propagates to the caller.
    except requests.RequestException as e:
        log.error(f"JSearch request failed for keyword '{keyword}': {e}")
        return []

    jobs_raw = data.get("data", [])
    if not isinstance(jobs_raw, list):
        log.warning(f"JSearch: unexpected data format for keyword '{keyword}'")
        return []

    return [_to_canonical(j) for j in jobs_raw if j.get("job_title")]


def run(config: dict, api_key: str) -> list[dict]:
    """
    Fetch jobs for all configured keywords from JSearch.
    Returns deduplicated list of canonical job dicts.
    """
    keywords = config["search"]["keywords"]
    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    attempted = 0

    for i, keyword in enumerate(keywords):
        log.info(f"JSearch: fetching keyword {i + 1}/{len(keywords)}: '{keyword}'")
        try:
            jobs = _fetch_keyword(keyword, api_key, config)
        except _QuotaExhausted:
            log.warning("JSearch: quota exhausted — skipping remaining keywords")
            break
        attempted += 1

        for job in jobs:
            job["_query"] = keyword
            url = job["job_url"]
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_jobs.append(job)

        if i < len(keywords) - 1:
            time.sleep(SLEEP_BETWEEN_KEYWORDS)

    log.info(f"JSearch: {len(all_jobs)} unique jobs fetched across {attempted}/{len(keywords)} keywords")

    # Filter to max_age_hours if configured
    max_age_hours = config["search"].get("max_age_hours")
    if max_age_hours:
        cutoff = datetime.now(tz=UTC) - timedelta(hours=max_age_hours)
        before = len(all_jobs)
        all_jobs = [j for j in all_jobs if j.get("_ts") and datetime.fromtimestamp(int(j["_ts"]), tz=UTC) >= cutoff]
        log.info(
            f"JSearch: {len(all_jobs)} jobs remain after {max_age_hours}h filter (dropped {before - len(all_jobs)})"
        )

    # Strip temp timestamp key before returning
    for j in all_jobs:
        j.pop("_ts", None)

    return all_jobs
