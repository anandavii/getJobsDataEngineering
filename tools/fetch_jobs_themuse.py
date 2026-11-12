"""
Fetch active-domain jobs from The Muse public API (no API key required).

Endpoint: GET https://www.themuse.com/api/public/jobs
- categories are supplied by the active domain configuration
- page: 0-indexed pagination
- 500 req/hour unregistered; well within our usage

Usage:
    results = run(config)
    # returns list of canonical job dicts
"""

import logging
from datetime import datetime

import requests

from domain_relevance import is_domain_relevant

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-search-bot/1.0)",
    "Accept": "application/json",
}

DEFAULT_CATEGORIES = ["Data Science", "Engineering"]
_MAX_PAGES = 10


def categories_for(config: dict) -> list[str]:
    configured = config.get("search", {}).get("free_sources", {}).get("themuse_categories")
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


def _fetch_page(category: str, page: int) -> tuple[list[dict], int]:
    """Fetch one page; returns (results, page_count)."""
    url = "https://www.themuse.com/api/public/jobs"
    params = {"category": category, "page": page}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", []), int(data.get("page_count", 0))
    except Exception as e:
        log.error(f"The Muse fetch failed (page {page}): {e}")
        return [], 0


def run(config: dict) -> list[dict]:
    """
    Fetch active-domain jobs from The Muse. Filters by the configured domain rules.
    Location filtering is handled downstream by orchestrate.filter_by_location().
    Returns deduplicated list of canonical job dicts.
    """
    raw_jobs: list[dict] = []
    seen_ids: set[int] = set()
    categories = categories_for(config)
    pages_fetched = 0
    for category in categories:
        page = 0
        page_count = None
        while True:
            if page_count is not None and page >= min(page_count, _MAX_PAGES):
                break
            results, total_pages = _fetch_page(category, page)
            if page_count is None:
                page_count = total_pages
            if not results:
                break
            for job in results:
                jid = job.get("id")
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    raw_jobs.append(job)
            page += 1
            pages_fetched += 1

    log.info(f"The Muse: {len(raw_jobs)} raw jobs fetched ({pages_fetched} pages, categories={categories!r})")

    results_out: list[dict] = []
    seen_urls: set[str] = set()

    for job in raw_jobs:
        title = str(job.get("name") or "").strip()
        company = str((job.get("company") or {}).get("name") or "").strip()
        url = str(((job.get("refs") or {}).get("landing_page")) or "").strip().lower()
        if not url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        keep, _ = is_domain_relevant(
            {"job_title": title, "description_snippet": ""}, config.get("domain_filter", {})
        )
        if not keep:
            continue

        locations = job.get("locations") or []
        location_names = [str(loc.get("name") or "") for loc in locations]
        location = ", ".join(filter(None, location_names)) or "Remote"

        matched_keyword = next(
            (keyword for keyword in config.get("search", {}).get("keywords", []) if keyword.lower() in title.lower()),
            "themuse",
        )

        results_out.append(
            {
                "job_title": title,
                "company": company,
                "location": location,
                "job_type": "Remote" if "remote" in location.lower() else "Unknown",
                "job_url": url,
                "description_snippet": "",  # The Muse landing page; no description in list API
                "date_posted": _parse_date(job.get("publication_date")),
                "source": "themuse",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
                "_query": matched_keyword,
            }
        )

    log.info(f"The Muse: {len(results_out)} relevant jobs after domain filter")
    return results_out
