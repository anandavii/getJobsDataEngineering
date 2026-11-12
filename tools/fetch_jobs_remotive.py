"""
Fetch remote jobs from Remotive.io (no API key required).

Remotive endpoint: GET https://remotive.io/api/remote-jobs?category=management-and-finance
Returns jobs in the management & finance category. Filtered client-side by config keywords.

Usage:
    results = run(config)
    # returns list of canonical job dicts
"""

import logging
from datetime import datetime

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-search-bot/1.0)",
    "Accept": "application/json",
}

DEFAULT_CATEGORIES = ["software-dev"]


def categories_for(config: dict) -> list[str]:
    configured = config.get("search", {}).get("free_sources", {}).get("remotive_categories")
    if isinstance(configured, list) and configured:
        return [str(category) for category in configured if str(category).strip()]
    return list(DEFAULT_CATEGORIES)


def _parse_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(date_str[:19], fmt).date().isoformat()
        except ValueError:
            pass
    return date_str[:10] if len(date_str) >= 10 else None


def _fetch_category(category: str) -> list[dict]:
    url = f"https://remotive.io/api/remote-jobs?category={category}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("jobs", [])
    except Exception as e:
        log.error(f"Remotive fetch failed for category '{category}': {e}")
        return []


def run(config: dict) -> list[dict]:
    """
    Fetch remote active-domain jobs from Remotive. Filters by config keywords client-side.
    Returns deduplicated list of canonical job dicts.
    """
    keywords = config.get("search", {}).get("keywords", [])

    raw_jobs: list[dict] = []
    seen_ids: set[int] = set()
    categories = categories_for(config)
    for cat in categories:
        for job in _fetch_category(cat):
            jid = job.get("id")
            if jid and jid not in seen_ids:
                seen_ids.add(jid)
                raw_jobs.append(job)

    log.info(f"Remotive: {len(raw_jobs)} raw jobs fetched across {len(categories)} categories")

    results: list[dict] = []
    seen_urls: set[str] = set()

    for job in raw_jobs:
        title = str(job.get("title") or "")
        description = str(job.get("description") or "")
        job_url = str(job.get("url") or "").strip().lower()
        if not job_url:
            continue

        # Keyword match: title or description must contain at least one keyword
        combined = f"{title} {description}".lower()
        matched_kw = None
        for kw in keywords:
            if kw.lower() in combined:
                matched_kw = kw
                break
        if not matched_kw:
            continue

        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        location = str(job.get("candidate_required_location") or "Remote")
        job_type_raw = str(job.get("job_type") or "").lower()
        if "contract" in job_type_raw:
            job_type = "Contract"
        elif "part" in job_type_raw:
            job_type = "Part-time"
        else:
            job_type = "Remote"

        results.append(
            {
                "job_title": title.strip(),
                "company": str(job.get("company_name") or "").strip(),
                "location": location,
                "job_type": job_type,
                "job_url": job_url,
                "description_snippet": description[:500],
                "date_posted": _parse_date(job.get("publication_date")),
                "source": "remotive",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
                "_query": matched_kw,
            }
        )

    log.info(f"Remotive: {len(results)} relevant jobs after keyword filter")
    return results
