"""
Fetch remote jobs from Jobicy (no API key required).

Jobicy endpoint: GET https://jobicy.com/api/v2/remote-jobs?tag=<tag>&count=50
Fetches up to 50 jobs per tag. Filtered client-side by config keywords.

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

DEFAULT_TAGS = ["data-science", "engineering"]


def tags_for(config: dict) -> list[str]:
    configured = config.get("search", {}).get("free_sources", {}).get("jobicy_tags")
    if isinstance(configured, list) and configured:
        return [str(tag) for tag in configured if str(tag).strip()]
    return list(DEFAULT_TAGS)


def _parse_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(date_str[:19], fmt).date().isoformat()
        except ValueError:
            pass
    return date_str[:10] if len(date_str) >= 10 else None


def _fetch_tag(tag: str) -> list[dict]:
    url = f"https://jobicy.com/api/v2/remote-jobs?tag={tag}&count=50"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("jobs", [])
    except Exception as e:
        log.error(f"Jobicy fetch failed for tag '{tag}': {e}")
        return []


def run(config: dict) -> list[dict]:
    """
    Fetch remote active-domain jobs from Jobicy. Filters by config keywords client-side.
    Returns deduplicated list of canonical job dicts.
    """
    keywords = config.get("search", {}).get("keywords", [])

    raw_jobs: list[dict] = []
    seen_ids: set[int] = set()
    tags = tags_for(config)
    for tag in tags:
        for job in _fetch_tag(tag):
            jid = job.get("id")
            if jid and jid not in seen_ids:
                seen_ids.add(jid)
                raw_jobs.append(job)

    log.info(f"Jobicy: {len(raw_jobs)} raw jobs fetched across {len(tags)} tags")

    results: list[dict] = []
    seen_urls: set[str] = set()

    for job in raw_jobs:
        title = str(job.get("jobTitle") or "")
        description = str(job.get("jobDescription") or job.get("jobExcerpt") or "")
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

        location = str(job.get("jobGeo") or "Remote")
        job_type_raw = str(job.get("jobType") or "").lower()
        if "contract" in job_type_raw:
            job_type = "Contract"
        elif "part" in job_type_raw:
            job_type = "Part-time"
        else:
            job_type = "Remote"

        salary_min = job.get("annualSalaryMin")
        salary_max = job.get("annualSalaryMax")
        salary_currency = job.get("salaryCurrency") or ("USD" if (salary_min or salary_max) else None)

        results.append(
            {
                "job_title": title.strip(),
                "company": str(job.get("companyName") or "").strip(),
                "location": location,
                "job_type": job_type,
                "job_url": job_url,
                "description_snippet": description[:500],
                "date_posted": _parse_date(job.get("pubDate")),
                "source": "jobicy",
                "salary_min": salary_min,
                "salary_max": salary_max,
                "salary_currency": salary_currency,
                "_query": matched_kw,
            }
        )

    log.info(f"Jobicy: {len(results)} relevant jobs after keyword filter")
    return results
