"""
Fetch QA/automation jobs from Indeed via RSS feeds.
No API key required. Uses stdlib xml parsing only.

Usage:
    results = run(config)
    # returns list of canonical job dicts
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)

INDEED_RSS_BASE = "https://www.indeed.com/rss"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_KEYWORDS = 2  # seconds — polite to Indeed, also avoids rate limiting


def _parse_indeed_date(date_str: str | None) -> str | None:
    """Parse RFC 2822 date (common in RSS) to ISO 8601 date string."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.date().isoformat()
    except Exception:
        pass
    # Fallback: try common formats
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _split_title_company(raw_title: str) -> tuple[str, str]:
    """
    Indeed RSS titles are often 'Job Title - Company Name'.
    Returns (job_title, company). Falls back to full title if no separator found.
    """
    if " - " in raw_title:
        parts = raw_title.rsplit(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return raw_title.strip(), ""


def _parse_location(description_html: str) -> str:
    """Extract location hint from description HTML if present."""
    # Indeed often embeds location in the description paragraph
    # Simple heuristic — look for common US state abbreviations or "Remote"
    lower = description_html.lower()
    if "remote" in lower:
        return "Remote"
    return "United States"


def _fetch_keyword(keyword: str, config: dict) -> list[dict]:
    params = {
        "q": keyword,
        "l": config["search"].get("location", "United States"),
        "limit": str(config["search"].get("indeed_results_per_keyword", 25)),
        "sort": "date",
    }
    # Build URL manually to match Indeed's expected format
    query_parts = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    url = f"{INDEED_RSS_BASE}?{query_parts}"

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-search-bot/1.0)"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Indeed RSS request failed for keyword '{keyword}': {e}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        log.error(f"Indeed RSS XML parse error for keyword '{keyword}': {e}")
        return []

    # Namespace-safe item extraction
    channel = root.find("channel")
    if channel is None:
        log.warning(f"Indeed RSS: no <channel> element for keyword '{keyword}'")
        return []

    jobs: list[dict] = []
    for item in channel.findall("item"):
        raw_title = (item.findtext("title") or "").strip()
        job_url = (item.findtext("link") or "").strip()
        pub_date = item.findtext("pubDate")
        description_html = item.findtext("description") or ""

        if not job_url:
            continue

        job_title, company = _split_title_company(raw_title)
        location = _parse_location(description_html)
        snippet = description_html.replace("<br>", " ").replace("</br>", " ")
        # Strip remaining HTML tags simply
        import re

        snippet = re.sub(r"<[^>]+>", "", snippet).strip()[:500]

        jobs.append(
            {
                "job_title": job_title,
                "company": company,
                "location": location,
                "job_type": "Remote" if location == "Remote" else "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": snippet,
                "date_posted": _parse_indeed_date(pub_date),
                "source": "indeed",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )

    return jobs


def run(config: dict) -> list[dict]:
    """
    Fetch jobs for all configured keywords from Indeed RSS.
    Returns deduplicated list of canonical job dicts.
    """
    keywords = config["search"]["keywords"]
    all_jobs: list[dict] = []
    seen_urls: set[str] = set()

    for i, keyword in enumerate(keywords):
        log.info(f"Indeed RSS: fetching keyword {i + 1}/{len(keywords)}: '{keyword}'")
        jobs = _fetch_keyword(keyword, config)

        for job in jobs:
            job["_query"] = keyword
            url = job["job_url"]
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_jobs.append(job)

        if i < len(keywords) - 1:
            time.sleep(SLEEP_BETWEEN_KEYWORDS)

    log.info(f"Indeed RSS: {len(all_jobs)} unique jobs fetched across {len(keywords)} keywords")
    return all_jobs
