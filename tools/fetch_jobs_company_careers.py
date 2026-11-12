"""
Fetch QA/automation jobs directly from company career pages.
Supports Google, Meta (Greenhouse), Amazon, Apple, Microsoft, Netflix (Lever),
plus any additional companies configured in job_search_config.json.

Usage:
    results = run(config)
    # returns list of canonical job dicts
"""

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus

import requests

from company_health import upsert_company_health

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_REQUESTS = 0.3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-search-bot/1.0)",
    "Accept": "application/json",
}

HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

WAYFAIR_API_HEADERS = {
    **HTML_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.wayfair.com",
    "Referer": "https://www.wayfair.com/careers/jobs?teamIds=4",
}

# Module-level cache for static endpoints that return all jobs regardless of keyword.
# Populated on first call; subsequent keyword iterations reuse the cached list.
_STATIC_CACHE: dict[str, list] = {
    "meta": None,
    "netflix": None,
    "apple": None,
    "shopify": None,
    "nulogy": None,
    "relex": None,
    "wayfair": None,
    "sidel": None,
    "komatsu": None,
    "volvo_group": None,
    "equipmentshare": None,
    "krones": None,
    "potter_global": None,
    "wacker_neuson": None,
    "fastenal": None,
    "dsj_global": None,
    "ecs_federal": None,
    "milliman": None,
    "govcio": None,
    "brotherhood_mutual": None,
    "ace": None,
}

# Maps company name → _STATIC_CACHE key for adapters that fetch all board jobs
# and filter locally. Health tracking uses the cache size (raw board count)
# rather than keyword-match count so that a healthy board with no QA
# keyword hits doesn't drift into monitor/critical.
_COMPANY_CACHE_KEYS: dict[str, str] = {
    "Meta": "meta",
    "Netflix": "netflix",
    "Apple": "apple",
    "Shopify": "shopify",
    "Nulogy": "nulogy",
    "RELEX Solutions": "relex",
    "Wayfair": "wayfair",
    "Sidel": "sidel",
    "Komatsu": "komatsu",
    "Volvo Group": "volvo_group",
    "EquipmentShare": "equipmentshare",
    "Krones": "krones",
    "Potter Global Technologies": "potter_global",
    "Wacker Neuson": "wacker_neuson",
    "Fastenal": "fastenal",
    "DSJ Global": "dsj_global",
    "ECS Federal": "ecs_federal",
    "Milliman": "milliman",
    "GovCIO": "govcio",
    "Brotherhood Mutual": "brotherhood_mutual",
    "ACE": "ace",
}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
        "%B %d, %Y",  # Full month: "March 04, 2026"
        "%b %d, %Y",  # Abbreviated: "Mar 04, 2026"
        "%m/%d/%y",  # Komatsu: "2/16/26"
    ):
        try:
            return datetime.strptime(date_str[:26], fmt).date().isoformat()
        except ValueError:
            pass
    return date_str[:10] if len(date_str) >= 10 else None


def _contains_keyword(text: str, keyword: str) -> bool:
    """
    Match keyword phrases with normalization so punctuation/hyphen variations
    do not cause false negatives. Example: "quality-assurance" ~= "quality assurance".
    """
    text = (text or "").strip()
    keyword = (keyword or "").strip()
    if not text or not keyword:
        return False

    # Phrase-aware regex: "test automation" -> r"\btest[\W_]+automation\b"
    keyword_tokens = re.findall(r"[a-z0-9]+", keyword.lower())
    if not keyword_tokens:
        return False
    phrase_re = re.compile(r"\b" + r"[\W_]+".join(re.escape(t) for t in keyword_tokens) + r"\b")
    if phrase_re.search(text.lower()):
        return True

    # Fallback: require all keyword tokens in normalized text.
    text_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return all(tok in text_tokens for tok in keyword_tokens)


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(text or ""))).strip()


def _canonical_job(
    *,
    title: str,
    company: str,
    location: str,
    job_url: str,
    description: str = "",
    date_posted: str | None = None,
) -> dict:
    return {
        "job_title": title.strip(),
        "company": company,
        "location": (location or "United States").strip(),
        "job_type": "Remote" if "remote" in (location or "").lower() else "Unknown",
        "job_url": job_url.lower().strip(),
        "description_snippet": description[:500],
        "date_posted": _parse_date(date_posted),
        "source": "company_careers",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
    }


def _build_shopify_date_map(html: str) -> dict[str, str]:
    """Return {uuid: iso-date} from Shopify's inline hydration JSON.

    The page serializes job data as escaped JSON-within-JSON. Each entry looks like:
      \"2026-01-15\",\"https://app.ashbyhq.com/api?ashby_jid=<uuid>\"
    Normalising the escaped quotes first lets a single regex handle both forms.
    """
    normalized = (html or "").replace('\\"', '"')
    pattern = re.compile(r'"(\d{4}-\d{2}-\d{2})","[^"]*ashby_jid=([0-9a-f-]{36})')
    return {uuid: date for date, uuid in pattern.findall(normalized)}


def _extract_shopify_jobs_from_html(html: str) -> list[dict]:
    date_map = _build_shopify_date_map(html)
    jobs: list[dict] = []
    pattern = re.compile(
        r'<a[^>]+href="(?P<href>/careers/[^"]+_(?P<uuid>[0-9a-f-]{36}))"[^>]*>(?P<text>.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    seen: set[str] = set()
    for match in pattern.finditer(html or ""):
        href = match.group("href")
        if href in seen:
            continue
        seen.add(href)
        text = _strip_html(match.group("text"))
        location = "United States"
        title = text
        if "Remote -" in text:
            title, location = text.split("Remote -", 1)
            title = title.strip()
            location = f"Remote - {location.strip()}"
        jobs.append(
            {
                "job_title": title,
                "location": location,
                "job_url": f"https://www.shopify.com{href}",
                "date_posted": date_map.get(match.group("uuid")),
            }
        )
    return jobs


def _extract_nulogy_jobs_from_html(html: str) -> list[dict]:
    jobs: list[dict] = []
    pattern = re.compile(
        r'<h2[^>]*>(?P<title>.*?)</h2>\s*<p[^>]*>(?P<location>.*?)</p>.*?<a[^>]+href="(?P<href>https://nulogy\.applytojob\.com/apply/[^"]+)"',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html or ""):
        jobs.append(
            {
                "job_title": _strip_html(match.group("title")),
                "location": _strip_html(match.group("location")),
                "job_url": match.group("href"),
            }
        )
    return jobs


def _fetch_relex_published_date_from_html(html: str) -> str | None:
    """Extract the posting date from a RELEX job detail page.

    Tries article:published_time meta tag first, then JSON-LD datePublished.
    """
    meta = re.search(
        r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"',
        html or "",
        re.IGNORECASE,
    )
    if meta:
        return _parse_date(meta.group(1))
    ld = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html or "")
    if ld:
        return _parse_date(ld.group(1))
    return None


def _extract_relex_jobs_from_payload(payload: dict) -> list[dict]:
    jobs: list[dict] = []
    html = payload.get("html", "") if isinstance(payload, dict) else ""
    pattern = re.compile(
        r'<div class="entry-meta-location">(?P<location>.*?)</div>.*?<h3[^>]*>.*?<a href="(?P<href>https://www\.relexsolutions\.com/careers/jobs/[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        jobs.append(
            {
                "job_title": _strip_html(match.group("title")),
                "location": _strip_html(match.group("location")),
                "job_url": match.group("href"),
            }
        )
    return jobs


def _extract_wayfair_detail_urls_from_html(html: str) -> list[str]:
    pattern = re.compile(r'href="(?P<href>/careers/job/[^"#?]+)"', re.IGNORECASE)
    urls: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(html or ""):
        url = f"https://www.wayfair.com{match.group('href')}"
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _extract_wayfair_folder_id(text: str) -> str | None:
    match = re.search(r"folderId=(\d+)", text or "", re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _build_wayfair_job_url_from_api_payload(job: dict) -> str:
    eid = str(job.get("eid") or "").strip()
    if not eid:
        return ""
    return f"https://wayfair.avature.net/careers?folderId={eid}"


def _extract_wayfair_label_value(html: str, label: str) -> str | None:
    pattern = re.compile(
        rf"<dt[^>]*>\s*{re.escape(label)}\s*</dt>\s*<dd[^>]*>(?P<value>.*?)</dd>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html or "")
    if not match:
        return None
    value = _strip_html(match.group("value"))
    return value or None


def _extract_wayfair_job_from_detail_html(html: str) -> dict | None:
    canonical_match = re.search(
        r'<link[^>]+rel="canonical"[^>]+href="(?P<url>https://www\.wayfair\.com/careers/job/[^"]+)"',
        html or "",
        re.IGNORECASE,
    )
    canonical_url = canonical_match.group("url").lower() if canonical_match else ""

    # Prefer the Avature apply URL from JSON-LD over the bot-blocked wayfair.com canonical URL
    avature_url = ""
    ld_match = re.search(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html or "",
        re.IGNORECASE | re.DOTALL,
    )
    if ld_match:
        try:
            ld_data = json.loads(ld_match.group(1))
            if isinstance(ld_data, dict) and ld_data.get("@type") == "JobPosting":
                avature_url = str(ld_data.get("url") or "")
        except Exception:
            pass

    job_url = avature_url or canonical_url

    title_match = re.search(r"<h1[^>]*>(?P<title>.*?)</h1>", html or "", re.IGNORECASE | re.DOTALL)
    title = _strip_html(title_match.group("title")) if title_match else ""

    location = _extract_wayfair_label_value(html, "Location:") or "United States"
    requisition_id = _extract_wayfair_label_value(html, "Requisition ID:")
    folder_id = _extract_wayfair_folder_id(avature_url or canonical_url or html)
    dedupe_key = requisition_id or folder_id or canonical_url

    description = ""
    desc_match = re.search(
        r'<div[^>]+class="[^"]*CareersJobDetailsDisplayDescription-jobDescription[^"]*"[^>]*>(?P<description>.*?)</div>',
        html or "",
        re.IGNORECASE | re.DOTALL,
    )
    if desc_match:
        description = _strip_html(desc_match.group("description"))

    if not title or not job_url or not dedupe_key:
        return None

    job = _canonical_job(
        title=title,
        company="Wayfair",
        location=location,
        job_url=job_url,
        description=description,
        date_posted=None,
    )
    # _canonical_job lowercases job_url, but Avature's folderId param is case-sensitive
    job["job_url"] = job_url
    job["_wayfair_dedupe_key"] = dedupe_key
    return job


def _fetch_wayfair_api_jobs() -> list[dict]:
    payload = {
        "categoryIds": [],
        "teamIds": [4],
        "locationIds": [],
        "countryIds": [],
        "teamCategoryIds": [],
        "stateIds": [],
        "selectedJobTypeIds": [],
        "keywords": "",
    }
    resp = requests.post(
        "https://www.wayfair.com/a/careers/careers/job_search_data",
        headers=WAYFAIR_API_HEADERS,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    records = data.get("jobListData", []) if isinstance(data, dict) else []

    jobs: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or "").strip()
        job_url = _build_wayfair_job_url_from_api_payload(record)
        if not title or not job_url:
            continue
        location_obj = record.get("location") or {}
        location_parts = [
            str(location_obj.get("city") or "").strip(),
            str(location_obj.get("state") or "").strip(),
            str(location_obj.get("country") or "").strip(),
        ]
        location = ", ".join(part for part in location_parts if part) or "United States"
        description = str(
            record.get("descriptionHTML") or record.get("briefDescription") or record.get("description") or ""
        )
        dedupe_key = str(
            record.get("requisitionId") or _extract_wayfair_folder_id(str(record)) or record.get("eid") or job_url
        )
        job = _canonical_job(
            title=title,
            company="Wayfair",
            location=location,
            job_url=job_url,
            description=_strip_html(description),
            date_posted=record.get("lastUpdatedDate") or record.get("updatedAt"),
        )
        # _canonical_job lowercases job_url, but Avature's folderId param is case-sensitive
        job["job_url"] = job_url
        job["_wayfair_dedupe_key"] = dedupe_key
        if record.get("minSalaryRange") is not None:
            job["salary_min"] = record.get("minSalaryRange")
        if record.get("maxSalaryRange") is not None:
            job["salary_max"] = record.get("maxSalaryRange")
        if record.get("currencyType"):
            job["salary_currency"] = record.get("currencyType")
        jobs.append(job)
    return jobs


def _fetch_wayfair(keyword: str) -> list[dict]:
    listing_url = "https://www.wayfair.com/careers/jobs?teamIds=4"
    cached_jobs = _STATIC_CACHE.get("wayfair")
    if cached_jobs is None:
        parsed_jobs: list[dict] = []
        try:
            parsed_jobs = _fetch_wayfair_api_jobs()
        except Exception as api_error:
            log.warning(f"Wayfair API fetch failed, falling back to HTML crawl: {api_error}")
            try:
                resp = requests.get(listing_url, headers=HTML_HEADERS, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                detail_urls = _extract_wayfair_detail_urls_from_html(resp.text)
            except Exception as listing_error:
                log.error(f"Wayfair fetch failed while reading listings: {listing_error}")
                _STATIC_CACHE["wayfair"] = []
                return []

            for detail_url in detail_urls:
                try:
                    resp = requests.get(detail_url, headers=HTML_HEADERS, timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    job = _extract_wayfair_job_from_detail_html(resp.text)
                except Exception as e:
                    log.warning(f"Wayfair detail fetch failed for {detail_url}: {e}")
                    continue

                if not job:
                    continue
                parsed_jobs.append(job)
                time.sleep(SLEEP_BETWEEN_REQUESTS)

        deduped_jobs: list[dict] = []
        seen_keys: set[str] = set()
        for job in parsed_jobs:
            dedupe_key = str(job.get("_wayfair_dedupe_key") or "")
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            deduped_jobs.append(job)

        _STATIC_CACHE["wayfair"] = deduped_jobs
        cached_jobs = deduped_jobs

    jobs: list[dict] = []
    for job in cached_jobs:
        haystack = f"{job.get('job_title', '')}\n{job.get('description_snippet', '')}"
        if not _contains_keyword(haystack, keyword):
            continue
        clean_job = dict(job)
        clean_job.pop("_wayfair_dedupe_key", None)
        jobs.append(clean_job)
    return jobs


# ---------------------------------------------------------------------------
# Location filtering helper
# ---------------------------------------------------------------------------

_USA_PATTERNS = frozenset(
    {
        "united states",
        "usa",
        "us",
        "u.s.",
        "u.s.a",
        # US state abbreviations (as suffix pattern ", XX")
        ", al",
        ", ak",
        ", az",
        ", ar",
        ", ca",
        ", co",
        ", ct",
        ", de",
        ", fl",
        ", ga",
        ", hi",
        ", id",
        ", il",
        ", in",
        ", ia",
        ", ks",
        ", ky",
        ", la",
        ", me",
        ", md",
        ", ma",
        ", mi",
        ", mn",
        ", ms",
        ", mo",
        ", mt",
        ", ne",
        ", nv",
        ", nh",
        ", nj",
        ", nm",
        ", ny",
        ", nc",
        ", nd",
        ", oh",
        ", ok",
        ", or",
        ", pa",
        ", ri",
        ", sc",
        ", sd",
        ", tn",
        ", tx",
        ", ut",
        ", vt",
        ", va",
        ", wa",
        ", wv",
        ", wi",
        ", wy",
        ", dc",
    }
)


def _is_usa_or_remote(location: str) -> bool:
    """Return True if location is in the USA or fully remote."""
    if not location:
        return True  # no location set → assume US (came from an API already filtered)
    loc = location.lower().strip()
    if "remote" in loc:
        return True
    for pattern in _USA_PATTERNS:
        if pattern in loc:
            return True
    return False


# ---------------------------------------------------------------------------
# Company-specific parsers
# ---------------------------------------------------------------------------


def _fetch_google(keyword: str) -> list[dict]:
    url = f"https://careers.google.com/api/v3/search/?location=United+States&q={quote_plus(keyword)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Google Careers fetch failed for '{keyword}': {e}")
        return []

    jobs = []
    for job in data.get("jobs", []):
        # locations is a list of dicts with 'address' key
        locations = job.get("locations", [])
        location_str = locations[0].get("address", "United States") if locations else "United States"

        job_url = f"https://careers.google.com/jobs/results/{job.get('id', '')}"
        jobs.append(
            {
                "job_title": job.get("title", "").strip(),
                "company": "Google",
                "location": location_str,
                "job_type": "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": (job.get("summary") or "")[:500],
                "date_posted": _parse_date(job.get("modified")),
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


def _fetch_meta(keyword: str) -> list[dict]:
    """Meta uses Greenhouse public API — fetch all once, filter by keyword in memory."""
    if _STATIC_CACHE["meta"] is None:
        url = "https://boards-api.greenhouse.io/v1/boards/facebook/jobs?content=true"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Meta (Greenhouse) fetch failed: {e}")
            _STATIC_CACHE["meta"] = []
            return []
        _STATIC_CACHE["meta"] = data.get("jobs", [])
        log.info(
            "Meta (Greenhouse): fetched %d raw jobs (cached)",
            len(_STATIC_CACHE["meta"]),
        )

    jobs = []
    for job in _STATIC_CACHE["meta"]:
        title = job.get("title", "")
        content = job.get("content", "")
        if not _contains_keyword(title, keyword) and not _contains_keyword(content, keyword):
            continue

        location = job.get("location", {}).get("name", "United States")
        if not _is_usa_or_remote(location):
            continue
        job_url = job.get("absolute_url", "")
        snippet = content[:500] if content else ""

        jobs.append(
            {
                "job_title": title.strip(),
                "company": "Meta",
                "location": location,
                "job_type": "Remote" if "remote" in location.lower() else "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": snippet,
                "date_posted": _parse_date(job.get("updated_at")),
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


def _fetch_amazon(keyword: str) -> list[dict]:
    url = f"https://www.amazon.jobs/en/search.json?base_query={quote_plus(keyword)}&loc_query=United+States&result_limit=25&sort=recent"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Amazon Jobs fetch failed for '{keyword}': {e}")
        return []

    jobs = []
    for job in data.get("jobs", []):
        job_path = job.get("job_path", "")
        job_url = f"https://www.amazon.jobs{job_path}" if job_path else ""
        location = job.get("location", "United States")

        jobs.append(
            {
                "job_title": job.get("title", "").strip(),
                "company": "Amazon",
                "location": location,
                "job_type": "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": (job.get("description_short") or "")[:500],
                "date_posted": _parse_date(job.get("posted_date")),
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


_APPLE_SSR_RE = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.+?)"\);\s*</script>',
    re.DOTALL,
)
_APPLE_SSR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_APPLE_SSR_MAX_PAGES = 5


def _fetch_apple_all_pages() -> list[dict]:
    """Fetch recent US Apple jobs by parsing SSR hydration data from the search page.

    Apple's search page embeds structured JSON in window.__staticRouterHydrationData.
    The keyword URL parameter has no server-side effect (always returns newest US jobs),
    so we fetch pages once and let the caller filter by keyword in memory.
    """
    if _STATIC_CACHE["apple"] is not None:
        return _STATIC_CACHE["apple"]

    all_jobs: list[dict] = []
    session = requests.Session()
    session.headers.update(_APPLE_SSR_HEADERS)

    for page in range(1, _APPLE_SSR_MAX_PAGES + 1):
        url = f"https://jobs.apple.com/en-us/search?location=united-states-USA&sort=newest&page={page}"
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            log.error(f"Apple Jobs SSR fetch failed page {page}: {e}")
            break

        m = _APPLE_SSR_RE.search(resp.text)
        if not m:
            log.warning(f"Apple: no SSR data found on page {page}")
            break

        try:
            # Two-stage decode: inner json.loads unescapes the JS string (\", \\n),
            # outer json.loads parses the actual JSON object.
            hydration = json.loads(json.loads('"' + m.group(1) + '"'))
            search_data = hydration["loaderData"]["search"]
            postings = search_data.get("searchResults", [])
        except Exception as e:
            log.error(f"Apple: SSR JSON parse failed page {page}: {e}")
            break

        if not postings:
            break

        for job in postings:
            position_id = str(job.get("positionId", ""))
            slug = job.get("transformedPostingTitle", "")
            job_url = f"https://jobs.apple.com/en-us/details/{position_id}/{slug}" if position_id else ""

            locations = job.get("locations", [])
            if isinstance(locations, list) and locations:
                loc = locations[0]
                city = loc.get("city", "")
                state = loc.get("stateProvince", "")
                country = loc.get("countryName", "United States")
                location = ", ".join(filter(None, [city, state, country]))
            else:
                location = "United States"

            all_jobs.append(
                {
                    "job_title": job.get("postingTitle", "").strip(),
                    "company": "Apple",
                    "location": location,
                    "job_type": "Remote" if job.get("homeOffice") else "Unknown",
                    "job_url": job_url.lower(),
                    "description_snippet": (job.get("jobSummary") or "")[:500],
                    "date_posted": _parse_date(job.get("postingDate")),
                    "source": "company_careers",
                    "salary_min": None,
                    "salary_max": None,
                    "salary_currency": None,
                }
            )

        log.debug(f"Apple SSR page {page}: {len(postings)} jobs")
        if page < _APPLE_SSR_MAX_PAGES:
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    _STATIC_CACHE["apple"] = all_jobs
    log.info("Apple (SSR): fetched %d raw jobs (cached)", len(all_jobs))
    return all_jobs


def _fetch_apple(keyword: str) -> list[dict]:
    """Return all cached Apple jobs (keyword filtering is a no-op).

    Apple's SSR search ignores keywords server-side, so we fetch all recent
    US jobs once and return them unfiltered.  The downstream QA relevance
    filter (domain_filter in config) handles title/description matching with
    patterns like "sdet", "software development engineer in test", etc.
    """
    return _fetch_apple_all_pages()


def _fetch_microsoft(keyword: str) -> list[dict]:
    url = (
        f"https://gcsservices.careers.microsoft.com/search/api/v1/search"
        f"?q={quote_plus(keyword)}&lc=United+States&exp=Experienced+professionals&pg=1&pgSz=25&o=Recent"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Microsoft Careers fetch failed for '{keyword}': {e}")
        return []

    jobs = []
    for job in data.get("operationResult", {}).get("result", {}).get("jobs", []):
        job_id = job.get("jobId", "")
        job_url = f"https://careers.microsoft.com/us/en/job/{job_id}" if job_id else ""

        locations = job.get("primaryWorkLocation", "") or job.get("workSiteFlexibility", "")

        jobs.append(
            {
                "job_title": job.get("title", "").strip(),
                "company": "Microsoft",
                "location": locations or "United States",
                "job_type": "Remote" if "remote" in (locations or "").lower() else "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": (job.get("jobSummary") or "")[:500],
                "date_posted": _parse_date(job.get("postingDate")),
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


def _fetch_netflix(keyword: str) -> list[dict]:
    """Netflix uses Lever public API — fetch all once, filter by keyword in memory."""
    if _STATIC_CACHE["netflix"] is None:
        url = "https://api.lever.co/v0/postings/netflix?mode=json"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Netflix (Lever) fetch failed: {e}")
            _STATIC_CACHE["netflix"] = []
            return []
        if not isinstance(data, list):
            log.warning("Netflix (Lever): unexpected response format")
            _STATIC_CACHE["netflix"] = []
            return []
        _STATIC_CACHE["netflix"] = data
        log.info(
            "Netflix (Lever): fetched %d raw jobs (cached)",
            len(_STATIC_CACHE["netflix"]),
        )

    jobs = []
    for job in _STATIC_CACHE["netflix"]:
        title = job.get("text", "")
        description = job.get("descriptionPlain") or job.get("description", "")
        if not _contains_keyword(title, keyword) and not _contains_keyword(description, keyword):
            continue

        job_url = job.get("hostedUrl") or job.get("applyUrl", "")
        location_data = job.get("workplaceType") or ""
        categories = job.get("categories", {})
        _commitment = categories.get("commitment", "")
        location = categories.get("location", "United States")
        if not _is_usa_or_remote(location):
            continue

        if "remote" in location_data.lower() or "remote" in location.lower():
            job_type = "Remote"
        else:
            job_type = "Unknown"

        # Lever uses epoch milliseconds for createdAt
        created_at = job.get("createdAt")
        date_posted = None
        if created_at:
            try:
                date_posted = datetime.fromtimestamp(int(created_at) / 1000).date().isoformat()
            except (ValueError, TypeError):
                pass

        jobs.append(
            {
                "job_title": title.strip(),
                "company": "Netflix",
                "location": location,
                "job_type": job_type,
                "job_url": job_url.lower(),
                "description_snippet": description[:500],
                "date_posted": date_posted,
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


def _fetch_shopify(keyword: str) -> list[dict]:
    if _STATIC_CACHE["shopify"] is None:
        try:
            resp = requests.get(
                "https://www.shopify.com/careers",
                headers={**HEADERS, "Accept": "text/html"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            _STATIC_CACHE["shopify"] = _extract_shopify_jobs_from_html(resp.text)
        except Exception as e:
            log.error(f"Shopify careers fetch failed: {e}")
            _STATIC_CACHE["shopify"] = []
    jobs = []
    for job in _STATIC_CACHE["shopify"]:
        if not _contains_keyword(job["job_title"], keyword):
            continue
        if not _is_usa_or_remote(job["location"]):
            continue
        jobs.append(
            _canonical_job(
                title=job["job_title"],
                company="Shopify",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job.get("date_posted"),
            )
        )
    return jobs


def _fetch_nulogy(keyword: str) -> list[dict]:
    if _STATIC_CACHE["nulogy"] is None:
        try:
            resp = requests.get(
                "https://nulogy.com/company/careers/",
                headers={**HEADERS, "Accept": "text/html"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            _STATIC_CACHE["nulogy"] = _extract_nulogy_jobs_from_html(resp.text)
        except Exception as e:
            log.error(f"Nulogy careers fetch failed: {e}")
            _STATIC_CACHE["nulogy"] = []
    jobs = []
    for job in _STATIC_CACHE["nulogy"]:
        if not _contains_keyword(job["job_title"], keyword):
            continue
        if not _is_usa_or_remote(job["location"]):
            continue
        jobs.append(
            _canonical_job(
                title=job["job_title"],
                company="Nulogy",
                location=job["location"],
                job_url=job["job_url"],
            )
        )
    return jobs


def _fetch_relex(keyword: str) -> list[dict]:
    if _STATIC_CACHE["relex"] is None:
        try:
            resp = requests.get(
                "https://www.relexsolutions.com/wp-json/wp_query/args/?post_type=job&posts_per_page=100&paged=1&s=&language_code=en_US",
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            _STATIC_CACHE["relex"] = _extract_relex_jobs_from_payload(resp.json())
        except Exception as e:
            log.error(f"RELEX careers fetch failed: {e}")
            _STATIC_CACHE["relex"] = []
    jobs = []
    for job in _STATIC_CACHE["relex"]:
        if not _contains_keyword(job["job_title"], keyword):
            continue
        if not _is_usa_or_remote(job["location"]):
            continue
        if "date_posted" not in job:
            try:
                resp = requests.get(job["job_url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                job["date_posted"] = _fetch_relex_published_date_from_html(resp.text)
            except Exception as e:
                log.warning(f"RELEX detail fetch failed for {job['job_url']}: {e}")
                job["date_posted"] = None
        jobs.append(
            _canonical_job(
                title=job["job_title"],
                company="RELEX Solutions",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job.get("date_posted"),
            )
        )
    return jobs


def _extract_sidel_jobs_from_html(html: str) -> list[dict]:
    """Parse all US jobs from Sidel's /about/careers/vacancies/ listing page.

    The page renders one <div class="job-opportunities-item" id="job_{req_id}">
    per job, with three child anchors: [0] title+url, [1] area, [2] country.
    All three share the same href; we filter for country == "United States".
    """
    from html import unescape as _unescape

    items = re.findall(
        r'<div class="job-opportunities-item" id="job_(\d+)">(.*?)(?=<div class="job-opportunities-item"|$)',
        html,
        re.DOTALL,
    )
    jobs: list[dict] = []
    for _req_id, block in items:
        links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', block, re.DOTALL)
        if len(links) < 3:
            continue
        country = _unescape(re.sub(r"<[^>]+>", "", links[2][1])).strip()
        if "United States" not in country:
            continue
        title = _unescape(re.sub(r"<[^>]+>", "", links[0][1])).strip()
        area = _unescape(re.sub(r"<[^>]+>", "", links[1][1])).strip()
        job_url = "https://www.sidel.com" + links[0][0]
        if not title or not job_url:
            continue
        jobs.append({"title": title, "area": area, "job_url": job_url})
    return jobs


def _fetch_komatsu(keyword: str) -> list[dict]:
    if _STATIC_CACHE["komatsu"] is None:
        # Step 1: GET listing page to acquire JSESSIONID cookie + CSRF token.
        # The POST endpoint rejects requests without a valid session cookie and
        # X-CSRF-Token header (returns 405 when called cold without them).
        listing_url = "https://komatsu.jobs/go/View-All-Jobs/8425800/"
        api_url = "https://komatsu.jobs/services/recruiting/v1/jobs"
        all_jobs: list[dict] = []
        try:
            sess = requests.Session()
            sess.headers.update(
                {
                    "User-Agent": HTML_HEADERS["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml",
                }
            )
            init_resp = sess.get(listing_url, timeout=REQUEST_TIMEOUT)
            init_resp.raise_for_status()
            csrf_match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', init_resp.text)
            csrf_token = csrf_match.group(1) if csrf_match else ""

            post_headers = {
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://komatsu.jobs",
                "Referer": listing_url,
                "X-CSRF-Token": csrf_token,
            }
            page = 0
            while True:
                resp = sess.post(
                    api_url,
                    headers=post_headers,
                    json={
                        "locale": "en_US",
                        "pageNumber": page,
                        "sortBy": "",
                        "keywords": "",
                        "location": "",
                        "facetFilters": {"jobLocationCountry": ["United States"]},
                        "brand": "",
                        "skills": [],
                        "categoryId": 8425800,
                        "alertId": "",
                        "rcmCandidateId": "",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("jobSearchResult", [])
                if not results:
                    break
                for item in results:
                    r = item.get("response", {})
                    job_id = r.get("id", "")
                    url_title = r.get("unifiedUrlTitle") or r.get("urlTitle", "")
                    title = r.get("unifiedStandardTitle", "")
                    location = (r.get("jobLocationShort") or ["United States"])[0].strip()
                    if job_id and url_title:
                        all_jobs.append(
                            {
                                "title": title,
                                "location": location,
                                "job_url": f"https://komatsu.jobs/job/{url_title}/{job_id}-en_US",
                                "date_posted": r.get("unifiedStandardStart"),
                            }
                        )
                total = data.get("totalJobs", 0)
                if len(all_jobs) >= total:
                    break
                page += 1
        except Exception as e:
            log.error(f"Komatsu careers fetch failed: {e}")
        _STATIC_CACHE["komatsu"] = all_jobs
    jobs = []
    for job in _STATIC_CACHE["komatsu"]:
        if not _contains_keyword(job["title"], keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Komatsu",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job.get("date_posted"),
            )
        )
    return jobs


def _fetch_sidel(keyword: str) -> list[dict]:
    if _STATIC_CACHE["sidel"] is None:
        try:
            resp = requests.get(
                "https://www.sidel.com/about/careers/vacancies/",
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            _STATIC_CACHE["sidel"] = _extract_sidel_jobs_from_html(resp.text)
        except Exception as e:
            log.error(f"Sidel careers fetch failed: {e}")
            _STATIC_CACHE["sidel"] = []
    jobs = []
    for job in _STATIC_CACHE["sidel"]:
        haystack = f"{job['title']} {job['area']}"
        if not _contains_keyword(haystack, keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Sidel",
                location="United States",
                job_url=job["job_url"],
                description=job["area"],
            )
        )
    return jobs


def _fetch_john_deere(keyword: str) -> list[dict]:
    """Fetch jobs from John Deere's eightfold.ai-powered careers API.

    Uses /api/pcsx/search (not /api/apply/v2/jobs which returns 403 from CI IPs).
    Response is nested under data.positions; job URLs are built from positionUrl.
    US filter: standardizedLocations entries end with ', US'.
    """
    url = (
        "https://careers.deere.com/api/pcsx/search"
        f"?domain=johndeere.com&query={quote_plus(keyword)}&location=united+states&num=100"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"John Deere careers fetch failed: {e}")
        return []

    jobs = []
    for pos in data.get("data", {}).get("positions", []):
        title = pos.get("name") or ""
        department = pos.get("department") or ""
        haystack = f"{title} {department}"
        if not _contains_keyword(haystack, keyword):
            continue
        std_locs = pos.get("standardizedLocations") or pos.get("locations") or []
        # Keep US and remote only
        if std_locs and not any(loc.strip().endswith(", US") or "remote" in loc.lower() for loc in std_locs):
            continue
        location_str = std_locs[0] if std_locs else "United States"
        relative_url = pos.get("positionUrl") or ""
        job_url = f"https://careers.deere.com{relative_url}" if relative_url else ""
        posted_ts = pos.get("postedTs")
        date_posted = datetime.utcfromtimestamp(posted_ts).date().isoformat() if posted_ts else None
        jobs.append(
            _canonical_job(
                title=title,
                company="John Deere",
                location=str(location_str),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_siemens(keyword: str) -> list[dict]:
    """Fetch jobs from Siemens' Avature-powered careers portal (US only, keyword search)."""
    # Avature portal: POST with search keyword + US country filter (42386[]=812209)
    url = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs"
    payload = {
        "search": keyword,
        "42386[]": "812209",  # United States of America
        "folderRecordsPerPage": "6",
        "listFilterMode": "true",
    }
    try:
        resp = requests.post(url, data=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        log.error(f"Siemens careers fetch failed: {e}")
        return []

    job_pattern = re.compile(
        r'<a class="link" href="(https://jobs\.siemens\.com/en_US/externaljobs/JobDetail/\d+)"[^>]*>'
        r"\s*(.*?)\s*</a>"
        r".*?<span class=\"list-item-location\">(.*?)</span>",
        re.DOTALL,
    )
    seen: set[str] = set()
    jobs = []
    for job_url, title, location in job_pattern.findall(html):
        title = re.sub(r"<[^>]+>", "", title).strip()
        location = re.sub(r"<[^>]+>", "", location).strip()
        if not title or title.lower() == "learn more" or len(title) > 120:
            continue
        if job_url in seen:
            continue
        seen.add(job_url)
        jobs.append(
            _canonical_job(
                title=title,
                company="Siemens",
                location=location or "United States",
                job_url=job_url,
            )
        )
    return jobs


def _fetch_honeywell(keyword: str) -> list[dict]:
    """Fetch jobs from Honeywell's Oracle HCM Recruiting Cloud careers portal."""
    base = "https://ibqbjb.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    params = (
        "?onlyData=true"
        "&expand=requisitionList.workLocation"
        f"&finder=findReqs%3BsiteNumber%3DCX_1%2Climit%3D100%2Ckeyword%3D%22{quote_plus(keyword)}%22"
        "%2Clocation%3DUnited+States%2CsortBy%3DRELEVANCY"
    )
    url = base + params
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Honeywell careers fetch failed: {e}")
        return []

    reqs = []
    for item in data.get("items", []):
        reqs.extend(item.get("requisitionList", []))

    jobs = []
    for req in reqs:
        title = req.get("Title") or ""
        if not _contains_keyword(title, keyword):
            continue
        # Only US postings
        if req.get("PrimaryLocationCountry") not in ("US", "United States", None):
            continue
        job_id = req.get("Id")
        job_url = f"https://careers.honeywell.com/en/sites/Honeywell/job/{job_id}" if job_id else ""
        location = req.get("PrimaryLocation") or "United States"
        date_posted = req.get("PostedDate") or None
        jobs.append(
            _canonical_job(
                title=title,
                company="Honeywell",
                location=str(location),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_eaton(keyword: str) -> list[dict]:
    """Fetch jobs from Eaton's Eightfold-powered careers site (PCSX API)."""
    url = f"https://eaton.eightfold.ai/api/pcsx/search?domain=eaton.com&query={quote_plus(keyword)}&start=0&num=100"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Eaton careers fetch failed: {e}")
        return []

    jobs = []
    for pos in data.get("data", {}).get("positions", []):
        title = pos.get("name") or ""
        department = pos.get("department") or ""
        haystack = f"{title} {department}"
        if not _contains_keyword(haystack, keyword):
            continue
        # Locations are a list of strings like ["Waukesha, WI, US"]
        std_locs = pos.get("standardizedLocations") or pos.get("locations") or []
        location_str = std_locs[0] if std_locs else "United States"
        # Only include US or remote positions
        if std_locs and not any(loc.endswith(", US") or "remote" in loc.lower() for loc in std_locs):
            continue
        relative_url = pos.get("positionUrl") or ""
        job_url = f"https://eaton.eightfold.ai{relative_url}" if relative_url else ""
        # postedTs is a Unix timestamp in seconds
        posted_ts = pos.get("postedTs")
        date_posted = datetime.utcfromtimestamp(posted_ts).date().isoformat() if posted_ts else None
        jobs.append(
            _canonical_job(
                title=title,
                company="Eaton",
                location=str(location_str),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_volvo_group(keyword: str) -> list[dict]:
    """Fetch jobs from Volvo Group's SAP SuccessFactors RSS feed (US jobs only).

    Populates a static cache on first call using several broad queries so that
    later keyword iterations reuse the same data without extra HTTP calls.
    RSS returns semantically matched results (titles rarely contain the exact
    keyword phrase), so filtering is left to the downstream domain relevance scorer.
    """
    if _STATIC_CACHE["volvo_group"] is None:
        # Title format: "Job Title (City, State, COUNTRY_CODE, ZipCode)"
        loc_re = re.compile(r"\s*\(([^()]+,\s*([A-Z]{2})\s*,[^()]*)\)\s*$")
        rss_headers = {**HEADERS, "Accept": "application/rss+xml, application/xml, text/xml"}
        broad_queries = [
            "supply chain",
            "logistics",
            "procurement",
            "inventory",
            "warehouse",
            "operations manager",
        ]
        seen_urls: set[str] = set()
        all_jobs: list[dict] = []
        for query in broad_queries:
            url = f"https://jobs.volvogroup.com/services/rss/job/?locale=en_US&keywords=({quote_plus(query)})"
            try:
                resp = requests.get(url, headers=rss_headers, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as e:
                log.error(f"Volvo Group careers fetch failed (query={query!r}): {e}")
                continue
            for item in root.findall("./channel/item"):
                raw_title = (item.findtext("title") or "").strip()
                job_url = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                description_html = item.findtext("description") or ""

                m = loc_re.search(raw_title)
                if m:
                    country_code = m.group(2)  # 2-letter code, e.g. "US", "CA", "FR"
                    if country_code != "US":
                        continue
                    location = m.group(1).strip()
                    title = raw_title[: m.start()].strip()
                else:
                    location = "United States"
                    title = raw_title

                job_url_lower = job_url.lower()
                if job_url_lower in seen_urls:
                    continue
                seen_urls.add(job_url_lower)

                date_posted = None
                if pub_date:
                    try:
                        date_posted = parsedate_to_datetime(pub_date).date().isoformat()
                    except Exception:
                        pass

                all_jobs.append(
                    {
                        "title": title,
                        "location": location,
                        "job_url": job_url,
                        "description": _strip_html(description_html),
                        "date_posted": date_posted,
                    }
                )
            time.sleep(SLEEP_BETWEEN_REQUESTS)
        _STATIC_CACHE["volvo_group"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["volvo_group"]:
        if not _contains_keyword(job["title"], keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Volvo Group",
                location=job["location"],
                job_url=job["job_url"],
                description=job["description"],
                date_posted=job["date_posted"],
            )
        )
    return jobs


def _fetch_equipmentshare(keyword: str) -> list[dict]:
    if _STATIC_CACHE["equipmentshare"] is None:
        all_jobs: list[dict] = []
        skip = 0
        take = 100
        try:
            while True:
                resp = requests.get(
                    "https://omnichannel-api.equipmentshare.com/jobs",
                    params={"skip": skip, "take": take},
                    headers=HEADERS,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                jobs_page = data.get("jobs", [])
                if not jobs_page:
                    break
                for item in jobs_page:
                    gj = item.get("google", {}).get("job", {})
                    title = gj.get("title", "")
                    addresses = gj.get("addresses", [])
                    location = addresses[0] if addresses else "United States"
                    job_url = item.get("absolute_url", "")
                    desc_html = gj.get("customAttributes", {}).get("job_description", {}).get("stringValues", [""])
                    desc = re.sub(r"<[^>]+>", " ", desc_html[0] if desc_html else "").strip()
                    publish_ts = gj.get("postingPublishTime", {})
                    date_posted = None
                    if publish_ts and publish_ts.get("seconds"):
                        try:
                            date_posted = datetime.fromtimestamp(int(publish_ts["seconds"])).strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    if job_url and title:
                        all_jobs.append(
                            {
                                "title": title,
                                "location": location,
                                "job_url": job_url,
                                "description": desc,
                                "date_posted": date_posted,
                            }
                        )
                total = data.get("metadata", {}).get("total", 0)
                skip += take
                if skip >= total:
                    break
        except Exception as e:
            log.error(f"EquipmentShare careers fetch failed: {e}")
        _STATIC_CACHE["equipmentshare"] = all_jobs
    jobs = []
    for job in _STATIC_CACHE["equipmentshare"]:
        haystack = f"{job['title']} {job['description']}"
        if not _contains_keyword(haystack, keyword):
            continue
        if not _is_usa_or_remote(job["location"]):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="EquipmentShare",
                location=job["location"],
                job_url=job["job_url"],
                description=job["description"],
                date_posted=job["date_posted"],
            )
        )
    return jobs


def _fetch_krones(keyword: str) -> list[dict]:
    """Fetch jobs from Krones Inc. SAP SuccessFactors RSS feed (US only).

    workfor.kronesusa.com is a SuccessFactors-hosted careers site for KRONES USA.
    The all-jobs RSS returns ~18 positions (small US-only company); static cache
    avoids repeated fetches across keyword iterations.
    Title format: "Job Title (State, US)"
    """
    if _STATIC_CACHE["krones"] is None:
        loc_re = re.compile(r"\s*\(([^()]+),\s*US\)\s*$", re.IGNORECASE)
        rss_headers = {**HEADERS, "Accept": "application/rss+xml, application/xml, text/xml"}
        all_jobs: list[dict] = []
        seen_urls: set[str] = set()
        try:
            resp = requests.get(
                "https://workfor.kronesusa.com/services/rss/job/?locale=en_US",
                headers=rss_headers,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            log.error(f"Krones careers fetch failed: {e}")
            _STATIC_CACHE["krones"] = []
            return []

        for item in root.findall("./channel/item"):
            raw_title = (item.findtext("title") or "").strip()
            job_url = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            description_html = item.findtext("description") or ""

            m = loc_re.search(raw_title)
            if m:
                location = m.group(1).strip() + ", US"
                title = raw_title[: m.start()].strip()
            else:
                location = "United States"
                title = raw_title

            job_url_lower = job_url.lower()
            if job_url_lower in seen_urls:
                continue
            seen_urls.add(job_url_lower)

            date_posted = None
            if pub_date:
                try:
                    date_posted = parsedate_to_datetime(pub_date).date().isoformat()
                except Exception:
                    pass

            all_jobs.append(
                {
                    "title": title,
                    "location": location,
                    "job_url": job_url,
                    "description": _strip_html(description_html),
                    "date_posted": date_posted,
                }
            )
        _STATIC_CACHE["krones"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["krones"]:
        if not _contains_keyword(job["title"], keyword) and not _contains_keyword(job["description"], keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Krones",
                location=job["location"],
                job_url=job["job_url"],
                description=job["description"],
                date_posted=job["date_posted"],
            )
        )
    return jobs


# ---------------------------------------------------------------------------
# Dispatcher — routes by ATS type
def _fetch_nyu_langone(keyword: str) -> list[dict]:
    """NYU Langone Health — SilkRoad ATS (SSR HTML, keyword search on page 1).

    Pagination is JavaScript-only; page 1 of the keyword search returns up to
    25 results. For supply-chain searches this covers all relevant postings.
    """
    base = "https://jobs.silkroad.com/NYULangone/NYULHCareers"
    try:
        resp = requests.get(
            base,
            params={"SearchString": keyword},
            headers=HTML_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        log.error(f"NYU Langone careers fetch failed: {e}")
        return []

    html = resp.text
    # Extract job IDs and titles: <div id="Jobs_PagedJobList_JobTitle-{id}">title</div>
    titles = {
        m[0]: unescape(m[1].strip())
        for m in re.findall(
            r'id="Jobs_PagedJobList_JobTitle-(\d+)"[^>]*>\s*(.*?)\s*</div>',
            html,
            re.DOTALL,
        )
    }
    # Locations: <div id="Jobs_PagedJobList_JobLocation-{id}">...<span class="sr-panel__meta">loc</span>...
    locations = {
        m[0]: unescape(m[1].strip())
        for m in re.findall(
            r'id="Jobs_PagedJobList_JobLocation-(\d+)"[^>]*>.*?<span[^>]*>(.*?)</span>',
            html,
            re.DOTALL,
        )
    }

    jobs = []
    for job_id, title in titles.items():
        if not _contains_keyword(title, keyword):
            continue
        location = locations.get(job_id, "New York, NY")
        jobs.append(
            _canonical_job(
                title=title,
                company="NYU Langone Health",
                location=location,
                job_url=f"{base}/jobs/{job_id}",
            )
        )
    return jobs


def _fetch_potter_global(keyword: str) -> list[dict]:
    """Fetch jobs from Potter Global Technologies (Paycom ATS).

    Paycom requires a session JWT obtained by loading the portal page, then
    POSTing to the job-posting-previews/search endpoint.
    """
    if _STATIC_CACHE["potter_global"] is None:
        client_key = "A026938E76FFE6716E20BF17FF3CDEBB"
        # /career-page is a client-side SPA route and returns 404 server-side.
        # Any specific job URL serves the full SPA HTML including the sessionJWT.
        portal_url = f"https://www.paycomonline.net/v4/ats/web.php/portal/{client_key}/jobs/121504"
        try:
            # Step 1: load portal page to obtain session JWT
            page_resp = requests.get(portal_url, headers=HTML_HEADERS, timeout=REQUEST_TIMEOUT)
            page_resp.raise_for_status()
            jwt_match = re.search(r'"sessionJWT":"([^"]+)"', page_resp.text)
            if not jwt_match:
                log.error("Potter Global Technologies: could not extract session JWT from portal page")
                _STATIC_CACHE["potter_global"] = []
                return []
            session_jwt = jwt_match.group(1)

            # Step 2: fetch all jobs (27 total — single page)
            search_resp = requests.post(
                "https://portal-applicant-tracking.us-cent.paycomonline.net/api/ats/job-posting-previews/search",
                headers={
                    **HTML_HEADERS,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": session_jwt,
                },
                json={
                    "skip": 0,
                    "take": 500,
                    "filtersForQuery": {
                        "distanceFrom": 0,
                        "workEnvironments": [],
                        "positionTypes": [],
                        "educationLevels": [],
                        "categories": [],
                        "travelTypes": [],
                        "shiftTypes": [],
                        "otherFilters": [],
                        "keywordSearchText": "",
                        "location": "",
                        "sortOption": "",
                    },
                },
                timeout=REQUEST_TIMEOUT,
            )
            search_resp.raise_for_status()
            data = search_resp.json()
            all_jobs = []
            for item in data.get("jobPostingPreviews", []):
                job_id = item.get("jobId")
                title = item.get("jobTitle", "").strip()
                location = (item.get("locations") or "").strip()
                description = (item.get("description") or "").strip()
                if job_id and title and _is_usa_or_remote(location):
                    all_jobs.append(
                        {
                            "title": title,
                            "location": location,
                            "description": description,
                            "job_url": f"https://www.paycomonline.net/v4/ats/web.php/portal/{client_key}/jobs/{job_id}",
                        }
                    )
            _STATIC_CACHE["potter_global"] = all_jobs
        except Exception as e:
            log.error(f"Potter Global Technologies careers fetch failed: {e}")
            _STATIC_CACHE["potter_global"] = []

    jobs = []
    for job in _STATIC_CACHE["potter_global"]:
        if not _contains_keyword(f"{job['title']} {job['description']}", keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Potter Global Technologies",
                location=job["location"],
                job_url=job["job_url"],
                description=job["description"],
            )
        )
    return jobs


def _parse_wacker_neuson_jobs(html: str) -> list[dict]:
    """Parse job entries from a Wacker Neuson Umantis listing page."""
    jobs: list[dict] = []
    # Each job is inside a <div class="tableaslist_cell"> ... </td>
    blocks = re.findall(
        r'<div class="tableaslist_cell">(.*?)</div>\s*</td>',
        html,
        re.DOTALL,
    )
    for block in blocks:
        # Title and URL from the HSTableLinkSubTitle anchor
        title_match = re.search(r'aria-label="([^"]+)"[^>]*id="link_3473_', block)
        url_match = re.search(r'href="(/Vacancies/\d+/Description/2)"', block)
        if not title_match or not url_match:
            continue
        title = unescape(title_match.group(1)).strip()
        job_url = "https://recruitingapp-60151.umantis.com" + url_match.group(1)

        # Location from tableaslist_element_26475 (prefixed with "&nbsp;|&nbsp;")
        loc_match = re.search(r'tableaslist_element_26475">(.*?)</span>', block, re.DOTALL)
        if loc_match:
            raw_loc = unescape(loc_match.group(1))
            # Strip "&nbsp;|&nbsp;" prefix: split on "|" and take last segment
            location = raw_loc.split("|")[-1].strip().strip("\xa0").strip()
            location = location or "United States"
        else:
            location = "United States"

        # Date: "Published Date: DD.MM.YYYY"
        date_posted = None
        date_match = re.search(r"Published Date: (\d{2}\.\d{2}\.\d{4})", block)
        if date_match:
            try:
                date_posted = datetime.strptime(date_match.group(1), "%d.%m.%Y").date().isoformat()
            except ValueError:
                pass

        # Description snippet
        desc_match = re.search(r'tableaslist_element_3476">(.*?)(?:</span>|$)', block, re.DOTALL)
        description = _strip_html(desc_match.group(1)) if desc_match else ""

        if title and job_url:
            jobs.append(
                {
                    "title": title,
                    "location": location,
                    "job_url": job_url,
                    "date_posted": date_posted,
                    "description": description,
                }
            )
    return jobs


def _fetch_wacker_neuson(keyword: str) -> list[dict]:
    """Fetch jobs from Wacker Neuson's Umantis-powered career portal.

    The portal is HTML-only (no public JSON API). Jobs are fetched across all
    pages, cached, then filtered locally by keyword and US location.
    Pagination loops back to page 1 when exhausted, so we use the
    getTableTotalCount endpoint to determine the exact page count.
    """
    if _STATIC_CACHE["wacker_neuson"] is None:
        import math

        all_jobs: list[dict] = []
        try:
            base_url = "https://recruitingapp-60151.umantis.com/Jobs/2"
            resp = requests.get(base_url, headers=HTML_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            html = resp.text

            # Extract pagination tokens from page HTML
            token_match = re.search(r"_search_token66856=(\d+)", html)
            search_token = token_match.group(1) if token_match else "2000749705"
            token_list_match = re.search(r"token_list_id&quot;:&quot;(\d+)&quot;", html)
            token_list_id = token_list_match.group(1) if token_list_match else None

            # Get total job count to avoid infinite loop on wrapping pagination
            total = 0
            if token_list_id:
                try:
                    count_resp = requests.get(
                        f"{base_url}/getTableTotalCount?token_list_id={token_list_id}",
                        headers=HTML_HEADERS,
                        timeout=10,
                    )
                    total = int(count_resp.json().get("data", 0))
                except Exception:
                    pass
            total_pages = math.ceil(total / 10) if total else 5  # safe fallback

            all_jobs.extend(_parse_wacker_neuson_jobs(html))

            for page in range(2, total_pages + 1):
                resp = requests.get(
                    f"{base_url}?tc66856=p{page}&_search_token66856={search_token}",
                    headers=HTML_HEADERS,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                all_jobs.extend(_parse_wacker_neuson_jobs(resp.text))
        except Exception as e:
            log.error(f"Wacker Neuson careers fetch failed: {e}")
        _STATIC_CACHE["wacker_neuson"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["wacker_neuson"]:
        haystack = f"{job['title']} {job['description']}"
        if not _contains_keyword(haystack, keyword):
            continue
        if not _is_usa_or_remote(job["location"]):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Wacker Neuson",
                location=job["location"],
                job_url=job["job_url"],
                description=job["description"],
                date_posted=job["date_posted"],
            )
        )
    return jobs


def _fetch_fastenal(keyword: str) -> list[dict]:
    """Fetch jobs from Fastenal's custom Spring MVC / DataTables portal.

    The /load-jobs endpoint requires a live JSESSIONID cookie and CSRF token
    obtained from a GET to /jobs first.  All jobs are fetched once (no keyword
    filter on the server side — search with empty query to get all US + intl
    jobs) and cached; client-side keyword filtering is applied each call.
    """
    if _STATIC_CACHE["fastenal"] is None:
        all_jobs: list[dict] = []
        try:
            session = requests.Session()
            session.headers.update(HTML_HEADERS)
            # Step 1: GET /jobs to acquire JSESSIONID + CSRF token
            init_resp = session.get("https://jobs.fastenal.com/jobs", timeout=REQUEST_TIMEOUT)
            init_resp.raise_for_status()
            csrf_match = re.search(r'<meta name="_csrf"\s+content="([^"]+)"', init_resp.text)
            if not csrf_match:
                log.error("Fastenal careers fetch failed: CSRF token not found")
                _STATIC_CACHE["fastenal"] = []
                return []
            csrf = csrf_match.group(1)

            # Step 2: Paginate through all jobs (server search left empty for full set)
            _DT_COLS = (
                "columns%5B0%5D%5Bdata%5D=jobId&columns%5B0%5D%5Bsearchable%5D=true&columns%5B0%5D%5Borderable%5D=false&"
                "columns%5B1%5D%5Bdata%5D=jobName&columns%5B1%5D%5Bsearchable%5D=true&columns%5B1%5D%5Borderable%5D=true&"
                "columns%5B2%5D%5Bdata%5D=type&columns%5B2%5D%5Bsearchable%5D=true&columns%5B2%5D%5Borderable%5D=true&"
                "columns%5B3%5D%5Bdata%5D=location&columns%5B3%5D%5Bsearchable%5D=true&columns%5B3%5D%5Borderable%5D=true&"
                "columns%5B4%5D%5Bdata%5D=department&columns%5B4%5D%5Bsearchable%5D=true&columns%5B4%5D%5Borderable%5D=true&"
                "columns%5B5%5D%5Bdata%5D=endDate&columns%5B5%5D%5Bsearchable%5D=true&columns%5B5%5D%5Borderable%5D=true"
            )
            start = 0
            page_size = 100
            total_records = None
            while True:
                body = (
                    f"draw=1&{_DT_COLS}&order%5B0%5D%5Bcolumn%5D=0&order%5B0%5D%5Bdir%5D=asc"
                    f"&start={start}&length={page_size}&search%5Bvalue%5D=&search%5Bregex%5D=false&query="
                )
                resp = session.post(
                    "https://jobs.fastenal.com/load-jobs",
                    data=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-CSRF-TOKEN": csrf,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Referer": "https://jobs.fastenal.com/jobs",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                if total_records is None:
                    total_records = data.get("recordsTotal", 0)
                for item in data.get("data", []):
                    city = item.get("city") or ""
                    state = item.get("state") or ""
                    location = f"{city}, {state}".strip(", ") if city or state else "United States"
                    if not _is_usa_or_remote(location):
                        continue
                    job_id = item.get("jobId")
                    title = item.get("title") or ""
                    approved_ms = item.get("approvedDate")
                    date_posted = (
                        datetime.utcfromtimestamp(approved_ms / 1000).date().isoformat() if approved_ms else None
                    )
                    if job_id and title:
                        all_jobs.append(
                            {
                                "title": title,
                                "location": location,
                                "job_url": f"https://jobs.fastenal.com/details/{job_id}",
                                "date_posted": date_posted,
                            }
                        )
                start += page_size
                if start >= (total_records or 0):
                    break
        except Exception as e:
            log.error(f"Fastenal careers fetch failed: {e}")
        _STATIC_CACHE["fastenal"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["fastenal"]:
        if not _contains_keyword(job["title"], keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Fastenal",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job.get("date_posted"),
            )
        )
    return jobs


def _fetch_applied_materials(keyword: str) -> list[dict]:
    """Fetch jobs from Applied Materials' Eightfold-powered careers site (PCSX API).

    Requires session initialization to obtain cookies + CSRF token before querying.
    Paginates through results (10 per page) filtered to US/Remote locations.
    """
    # Step 1: visit careers page to obtain session cookies and CSRF token
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        init_resp = session.get(
            "https://careers.appliedmaterials.com/careers",
            timeout=REQUEST_TIMEOUT,
        )
        init_resp.raise_for_status()
        csrf_match = re.search(r'name="_csrf"\s+content="([^"]+)"', init_resp.text)
        if not csrf_match:
            log.error("Applied Materials careers: CSRF token not found in page HTML")
            return []
        session.headers.update(
            {
                "x-csrf-token": csrf_match.group(1),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://careers.appliedmaterials.com/careers",
            }
        )
    except Exception as e:
        log.error(f"Applied Materials careers session init failed: {e}")
        return []

    # Step 2: paginate through PCSX search (US locations only, 10 per page)
    jobs: list[dict] = []
    start = 0
    page_size = 10
    total_count = None

    while True:
        url = (
            "https://careers.appliedmaterials.com/api/pcsx/search"
            f"?domain=appliedmaterials.com&query={quote_plus(keyword)}"
            f"&location=united+states&start={start}&num={page_size}"
        )
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Applied Materials careers fetch failed: {e}")
            break

        payload = data.get("data", {})
        if total_count is None:
            total_count = payload.get("count", 0)
        positions = payload.get("positions", [])
        if not positions:
            break

        for pos in positions:
            title = pos.get("name") or ""
            department = pos.get("department") or ""
            haystack = f"{title} {department}"
            if not _contains_keyword(haystack, keyword):
                continue
            std_locs = pos.get("standardizedLocations") or pos.get("locations") or []
            location_str = std_locs[0] if std_locs else "United States"
            # PCSX location filter is server-side; double-check for non-US slip-through
            if std_locs and not any(loc.endswith(", US") or "remote" in loc.lower() for loc in std_locs):
                continue
            relative_url = pos.get("positionUrl") or ""
            job_url = f"https://careers.appliedmaterials.com{relative_url}" if relative_url else ""
            posted_ts = pos.get("postedTs")
            date_posted = datetime.utcfromtimestamp(posted_ts).date().isoformat() if posted_ts else None
            jobs.append(
                _canonical_job(
                    title=title,
                    company="Applied Materials",
                    location=str(location_str),
                    job_url=job_url,
                    date_posted=date_posted,
                )
            )

        start += page_size
        if start >= (total_count or 0):
            break

    return jobs


# ---------------------------------------------------------------------------


def _fetch_dsj_global(keyword: str) -> list[dict]:
    """Fetch jobs from DSJ Global (supply chain recruitment agency).

    DSJ Global uses Next.js App Router (RSC streaming) behind Vercel's security
    checkpoint — plain HTTP clients get 429/blocked. Playwright is required.

    Static-cache pattern: browser launches ONCE on the first keyword call and
    fetches all US jobs from /en-us/jobs?country=USA (no per-keyword search —
    avoids 46 browser launches which all timeout). Subsequent keyword calls
    filter from the in-memory cache.

    wait_until="networkidle" always times out on RSC pages; use
    "domcontentloaded" + wait_for_selector instead.
    "Load more" button re-queried each iteration to avoid stale DOM handles.
    """
    if _STATIC_CACHE["dsj_global"] is None:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import]
        except ImportError:
            log.error("DSJ Global fetch skipped: playwright not installed")
            _STATIC_CACHE["dsj_global"] = []
            return []

        base_url = "https://www.dsjglobal.com"
        all_jobs: list[dict] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(
                    f"{base_url}/en-us/jobs?country=USA",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                # RSC pages never reach networkidle; wait for first card instead
                page.wait_for_selector('[class*="SearchResult_container"]', timeout=20_000)

                # Click "Load more" until gone, re-querying each time to avoid stale handles
                for _ in range(10):
                    load_more = page.query_selector('button[class*="JobSearchResults_loadButton"]')
                    if not load_more:
                        break
                    try:
                        load_more.click(timeout=5_000)
                        page.wait_for_timeout(2000)
                    except Exception:
                        break

                cards = page.query_selector_all('[class*="SearchResult_container"]')
                for card in cards:
                    title_el = card.query_selector("h5")
                    link_el = card.query_selector('a[href*="/en-us/job/"]')
                    desc_el = card.query_selector('p[class*="SearchResult_description"]')
                    tag_els = card.query_selector_all('[class*="SearchResult_tags"] span')
                    if not title_el or not link_el:
                        continue
                    title = (title_el.inner_text() or "").strip()
                    href = link_el.get_attribute("href") or ""
                    job_url = f"{base_url}{href}" if href.startswith("/") else href
                    description = (desc_el.inner_text() if desc_el else "").strip()
                    location = ""
                    for tag in tag_els:
                        text = (tag.inner_text() or "").strip()
                        if text and not text.startswith("US$") and not text.startswith("$"):
                            location = text
                            break
                    all_jobs.append(
                        {"title": title, "job_url": job_url, "description": description, "location": location}
                    )
                browser.close()
        except Exception as e:
            log.error(f"DSJ Global careers fetch failed: {e}")
        _STATIC_CACHE["dsj_global"] = all_jobs
        log.info(f"DSJ Global: cached {len(all_jobs)} US jobs")

    jobs = []
    for job in _STATIC_CACHE["dsj_global"]:
        if not _contains_keyword(f"{job['title']} {job['description']}", keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="DSJ Global",
                location=job["location"] or "United States",
                job_url=job["job_url"],
                description=job["description"],
            )
        )
    return jobs


def _fetch_ecs_federal(keyword: str) -> list[dict]:
    """Fetch jobs from ECS Federal (ADP myjobs platform).

    ADP's search-meta detail API requires a session ``myjobstoken`` generated by
    the Angular app — plain HTTP calls return 400 "postingChannelId not found".

    Strategy:
    1. GET all 236 job stubs via the public list API (one HTTP call; keyword
       param is ignored server-side so we fetch all and filter locally).
    2. Broad-filter stubs to QA-relevant titles to minimise Playwright loads.
    3. Launch ONE Playwright browser; for each candidate job, navigate to the
       ADP job-details page and intercept the search-meta XHR response (which
       the Angular app calls automatically with the proper session token).
    4. Cache the resulting structured job records; subsequent keyword calls
       filter entirely from the in-memory cache with no more browser use.
    """
    ADP_ORG_OID = "G3TR55XDKJB6R98A"
    LIST_URL = (
        "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1"
        "/job-requisitions?domain=ecsfederal&$top=500"
    )
    DETAIL_HOST = "my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/search-meta/"
    JOB_URL_TMPL = "https://myjobs.adp.com/ecsfederal/cx/job-details?reqId={}"
    # Broad terms to pre-filter stubs before Playwright phase
    BROAD_QA = ["test", "quality", "sdet", "qa", "assurance"]

    if _STATIC_CACHE["ecs_federal"] is None:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import]
        except ImportError:
            log.error("ECS Federal fetch skipped: playwright not installed")
            _STATIC_CACHE["ecs_federal"] = []
            return []

        # ── Step 1: fetch all stubs via HTTP ──────────────────────────────────
        stubs: list[dict] = []
        try:
            resp = requests.get(
                LIST_URL,
                headers={
                    "User-Agent": HTML_HEADERS["User-Agent"],
                    "Accept": "application/json",
                    "orgoid": ADP_ORG_OID,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            for item in resp.json().get("jobRequisitions", []):
                req_id = str(item.get("reqId", ""))
                title = item.get("jobTitle", "")
                if req_id and title:
                    stubs.append({"reqId": req_id, "title": title})
        except Exception as e:
            log.error(f"ECS Federal list fetch failed: {e}")
            _STATIC_CACHE["ecs_federal"] = []
            return []

        # ── Step 2: broad QA title filter ────────────────────────────────────
        qa_stubs = [
            s for s in stubs
            if any(k in s["title"].lower() for k in BROAD_QA)
        ]
        log.info(f"ECS Federal: {len(stubs)} stubs, {len(qa_stubs)} QA-relevant")

        # ── Step 3: Playwright detail fetch via XHR interception ──────────────
        all_jobs: list[dict] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                for stub in qa_stubs:
                    req_id = stub["reqId"]
                    captured: list[dict] = []

                    def _on_response(response, _rid=req_id, _buf=captured):
                        if DETAIL_HOST + _rid in response.url:
                            try:
                                _buf.append(response.json())
                            except Exception:
                                pass

                    page.on("response", _on_response)
                    try:
                        page.goto(JOB_URL_TMPL.format(req_id), timeout=25_000)
                        page.wait_for_timeout(5_000)
                    except Exception as e:
                        log.warning(f"ECS Federal page load failed reqId={req_id}: {e}")
                    finally:
                        page.remove_listener("response", _on_response)

                    if not captured:
                        log.warning(f"ECS Federal: no XHR captured for reqId={req_id}")
                        continue

                    data = captured[0]
                    reqs = data.get("jobRequisitions", [])
                    if not reqs:
                        continue
                    r = reqs[0]

                    loc_parts: list[str] = []
                    locs = r.get("requisitionLocations", [])
                    if locs:
                        addr = locs[0].get("address", {})
                        city = addr.get("cityName", "")
                        # Use state codeValue (e.g. "VA") not longName ("Virginia")
                        # so _is_usa_or_remote can match the ", va" patterns
                        state = addr.get("countrySubdivisionLevel1", {}).get("codeValue", "")
                        if city:
                            loc_parts.append(city)
                        if state:
                            loc_parts.append(state)
                    location = ", ".join(loc_parts) if loc_parts else "United States"

                    if not _is_usa_or_remote(location):
                        continue

                    date_posted = None
                    postings = r.get("postingInstructions", [])
                    if postings:
                        ts = postings[0].get("timestampLastPosted", "")
                        if ts:
                            date_posted = ts[:10]

                    # Strip HTML tags so description_snippet contains plain text
                    raw_desc = r.get("requisitionDescription", "")
                    plain_desc = re.sub(r"<[^>]+>", " ", raw_desc)
                    plain_desc = re.sub(r"\s+", " ", unescape(plain_desc)).strip()

                    all_jobs.append(
                        {
                            "title": r.get("requisitionTitle") or stub["title"],
                            "location": location,
                            "description": plain_desc,
                            "date_posted": date_posted,
                            "job_url": JOB_URL_TMPL.format(req_id),
                        }
                    )

                browser.close()
        except Exception as e:
            log.error(f"ECS Federal Playwright session failed: {e}")

        _STATIC_CACHE["ecs_federal"] = all_jobs
        log.info(f"ECS Federal: cached {len(all_jobs)} US QA jobs")

    jobs = []
    for job in _STATIC_CACHE["ecs_federal"]:
        if not _contains_keyword(f"{job['title']} {job['description']}", keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="ECS Federal",
                location=job["location"],
                job_url=job["job_url"],
                description=job["description"],
                date_posted=job["date_posted"],
            )
        )
    return jobs


def _fetch_morgan_stanley(keyword: str) -> list[dict]:
    """Fetch jobs from Morgan Stanley's Eightfold-powered careers site (PCSX API)."""
    url = (
        "https://morganstanley.eightfold.ai/api/pcsx/search"
        f"?domain=morganstanley.com&query={quote_plus(keyword)}&location=united+states&num=100"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Morgan Stanley careers fetch failed: {e}")
        return []

    jobs = []
    for pos in data.get("data", {}).get("positions", []):
        title = pos.get("name") or ""
        department = pos.get("department") or ""
        haystack = f"{title} {department}"
        if not _contains_keyword(haystack, keyword):
            continue
        std_locs = pos.get("standardizedLocations") or pos.get("locations") or []
        if std_locs and not any(loc.strip().endswith(", US") or "remote" in loc.lower() for loc in std_locs):
            continue
        location_str = std_locs[0] if std_locs else "United States"
        relative_url = pos.get("positionUrl") or ""
        job_url = f"https://morganstanley.eightfold.ai{relative_url}" if relative_url else ""
        posted_ts = pos.get("postedTs")
        date_posted = datetime.utcfromtimestamp(posted_ts).date().isoformat() if posted_ts else None
        jobs.append(
            _canonical_job(
                title=title,
                company="Morgan Stanley",
                location=str(location_str),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_oracle(keyword: str) -> list[dict]:
    """Fetch jobs from Oracle's Oracle HCM Recruiting Cloud careers portal."""
    base = "https://fa-ertb-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    params = (
        "?onlyData=true"
        "&expand=requisitionList.workLocation"
        f"&finder=findReqs%3BsiteNumber%3DCX_2%2Climit%3D100%2Ckeyword%3D%22{quote_plus(keyword)}%22"
        "%2Clocation%3DUnited+States%2CsortBy%3DRELEVANCY"
    )
    url = base + params
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Oracle careers fetch failed: {e}")
        return []

    reqs = []
    for item in data.get("items", []):
        reqs.extend(item.get("requisitionList", []))

    jobs = []
    for req in reqs:
        title = req.get("Title") or ""
        if not _contains_keyword(title, keyword):
            continue
        if req.get("PrimaryLocationCountry") not in ("US", "United States", None):
            continue
        job_id = req.get("Id")
        job_url = f"https://fa-ertb-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2/job/{job_id}" if job_id else ""
        location = req.get("PrimaryLocation") or "United States"
        date_posted = req.get("PostedDate") or None
        jobs.append(
            _canonical_job(
                title=title,
                company="Oracle",
                location=str(location),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_nov(keyword: str) -> list[dict]:
    """Fetch jobs from NOV's Oracle HCM Recruiting Cloud careers portal (us6 region)."""
    base = "https://egay.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    params = (
        "?onlyData=true"
        "&expand=requisitionList.workLocation"
        f"&finder=findReqs%3BsiteNumber%3DCX_4001%2Climit%3D100%2Ckeyword%3D%22{quote_plus(keyword)}%22"
        "%2Clocation%3DUnited+States%2CsortBy%3DRELEVANCY"
    )
    url = base + params
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"NOV careers fetch failed: {e}")
        return []

    reqs = []
    for item in data.get("items", []):
        reqs.extend(item.get("requisitionList", []))

    jobs = []
    for req in reqs:
        title = req.get("Title") or ""
        if not _contains_keyword(title, keyword):
            continue
        if req.get("PrimaryLocationCountry") not in ("US", "United States", None):
            continue
        job_id = req.get("Id")
        job_url = f"https://egay.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4001/job/{job_id}" if job_id else ""
        location = req.get("PrimaryLocation") or "United States"
        date_posted = req.get("PostedDate") or None
        jobs.append(
            _canonical_job(
                title=title,
                company="NOV",
                location=str(location),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_chase(keyword: str) -> list[dict]:
    """Fetch jobs from Chase/JPMC's Oracle HCM Recruiting Cloud careers portal."""
    base = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    params = (
        "?onlyData=true"
        "&expand=requisitionList.workLocation"
        f"&finder=findReqs%3BsiteNumber%3DCX_1001%2Climit%3D100%2Ckeyword%3D%22{quote_plus(keyword)}%22"
        "%2Clocation%3DUnited+States%2CsortBy%3DRELEVANCY"
    )
    url = base + params
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Chase careers fetch failed: {e}")
        return []

    reqs = []
    for item in data.get("items", []):
        reqs.extend(item.get("requisitionList", []))

    jobs = []
    for req in reqs:
        title = req.get("Title") or ""
        if not _contains_keyword(title, keyword):
            continue
        if req.get("PrimaryLocationCountry") not in ("US", "United States", None):
            continue
        job_id = req.get("Id")
        job_url = f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{job_id}" if job_id else ""
        location = req.get("PrimaryLocation") or "United States"
        date_posted = req.get("PostedDate") or None
        jobs.append(
            _canonical_job(
                title=title,
                company="Chase",
                location=str(location),
                job_url=job_url,
                date_posted=date_posted,
            )
        )
    return jobs


def _fetch_vertical_screen(keyword: str) -> list[dict]:
    """Fetch keyword-matched jobs from Vertical Screen's iCIMS careers portal (HTML scrape)."""
    url = (
        "https://careers-verticalscreen.icims.com/jobs/search"
        f"?ss=1&in_iframe=1&searchKeyword={quote_plus(keyword)}&searchRelation=keyword_all"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        log.error(f"Vertical Screen careers fetch failed: {e}")
        return []

    # Each job is in <li class="iCIMS_JobCardItem">
    card_pat = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.DOTALL)
    title_pat = re.compile(r'<h3[^>]*>\s*(.*?)\s*</h3>', re.DOTALL)
    url_pat = re.compile(r'href="(https://careers-verticalscreen\.icims\.com/jobs/\d+/[^"]+)"')
    loc_pat = re.compile(r'<span class="sr-only field-label">Job Locations</span>\s*<span[^>]*>\s*(.*?)\s*</span>', re.DOTALL)
    desc_pat = re.compile(r'<div class="col-xs-12 description">\s*(.*?)\s*</div>', re.DOTALL)

    seen: set[str] = set()
    jobs = []
    for card_html in card_pat.findall(html):
        title_m = title_pat.search(card_html)
        url_m = url_pat.search(card_html)
        if not title_m or not url_m:
            continue
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        job_url = url_m.group(1).split("?")[0]  # strip ?in_iframe=1
        loc_m = loc_pat.search(card_html)
        location = re.sub(r"<[^>]+>", "", loc_m.group(1)).strip() if loc_m else "United States"
        desc_m = desc_pat.search(card_html)
        desc = _strip_html(desc_m.group(1)) if desc_m else ""
        if not title or job_url in seen:
            continue
        if not _contains_keyword(f"{title}\n{desc}", keyword):
            continue
        if not _is_usa_or_remote(location):
            continue
        seen.add(job_url)
        jobs.append(
            _canonical_job(
                title=title,
                company="Vertical Screen",
                location=location or "United States",
                job_url=job_url,
                description=desc,
            )
        )
    return jobs


def _fetch_milliman(keyword: str) -> list[dict]:
    """Fetch jobs from Milliman's UltiPro/UKG Pro careers portal.

    UltiPro requires a session CSRF token (X-RequestVerificationToken) obtained
    from a GET to the board page before the POST to LoadSearchResults will work.
    All jobs are fetched once (QueryString="") and cached; keyword filtering is
    done in memory.
    """
    _ORG = "mil1017"
    _BOARD = "f54234e9-dfde-b183-fd20-4fbdb19cba7a"
    _BASE = f"https://recruiting2.ultipro.com/{_ORG}/JobBoard/{_BOARD}"

    if _STATIC_CACHE["milliman"] is None:
        all_jobs: list[dict] = []
        try:
            session = requests.Session()
            session.headers.update(HTML_HEADERS)

            # Step 1: GET the board page to acquire session cookies + CSRF token
            init_resp = session.get(f"{_BASE}/", timeout=REQUEST_TIMEOUT)
            init_resp.raise_for_status()
            csrf_match = re.search(
                r'name="__RequestVerificationToken"[^>]+value="([^"]+)"',
                init_resp.text,
            )
            if not csrf_match:
                log.error("Milliman careers fetch failed: CSRF token not found")
                _STATIC_CACHE["milliman"] = []
                return []
            csrf = csrf_match.group(1)

            # Step 2: Paginate through all jobs (empty QueryString = all results)
            page_size = 50
            skip = 0
            total = None
            while True:
                payload = {
                    "opportunitySearch": {
                        "Top": page_size,
                        "Skip": skip,
                        "QueryString": "",
                        "OrderBy": [
                            {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}
                        ],
                        "Filters": [
                            {"t": "TermsSearchFilterDto", "fieldName": 4, "extra": None, "values": []},
                            {"t": "TermsSearchFilterDto", "fieldName": 5, "extra": None, "values": []},
                            {"t": "TermsSearchFilterDto", "fieldName": 6, "extra": None, "values": []},
                            {"t": "TermsSearchFilterDto", "fieldName": 37, "extra": None, "values": []},
                        ],
                    },
                    "matchCriteria": {
                        "PreferredJobs": [],
                        "Educations": [],
                        "LicenseAndCertifications": [],
                        "Skills": [],
                        "hasNoLicenses": False,
                        "SkippedSkills": [],
                    },
                }
                resp = session.post(
                    f"{_BASE}/JobBoardView/LoadSearchResults",
                    json=payload,
                    headers={
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-RequestVerificationToken": csrf,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Referer": f"{_BASE}/?q=&o=postedDateDesc",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                if total is None:
                    total = data.get("totalCount", 0)

                for job in data.get("opportunities", []):
                    title = (job.get("Title") or "").strip()
                    job_id = job.get("Id", "")
                    job_url = f"{_BASE}/OpportunityDetail?opportunityId={job_id}"

                    # Build location string; JobLocationType 2 = fully remote
                    loc_type = job.get("JobLocationType")
                    locations = job.get("Locations") or []
                    if loc_type == 2:
                        location = "Remote"
                    elif locations:
                        loc = locations[0]
                        addr = loc.get("Address") or {}
                        city = addr.get("City") or ""
                        state = (addr.get("State") or {}).get("Code") or ""
                        country = (addr.get("Country") or {}).get("Code") or ""
                        location = ", ".join(filter(None, [city, state, country]))
                    else:
                        location = "Unknown"

                    # Filter: only US or Remote postings
                    is_remote = loc_type == 2
                    has_usa = any(
                        (loc.get("Address") or {}).get("Country", {}).get("Code") == "USA"
                        for loc in locations
                    )
                    if not is_remote and not has_usa:
                        continue

                    date_posted = _parse_date(job.get("PostedDate"))
                    desc = (job.get("BriefDescription") or "")[:500]

                    all_jobs.append(
                        {
                            "title": title,
                            "location": location,
                            "job_url": job_url,
                            "date_posted": date_posted,
                            "description": desc,
                        }
                    )

                skip += page_size
                if skip >= (total or 0):
                    break
                time.sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            log.error(f"Milliman careers fetch failed: {e}")
        _STATIC_CACHE["milliman"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["milliman"]:
        if not _contains_keyword(f"{job['title']}\n{job['description']}", keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Milliman",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job["date_posted"],
                description=job["description"],
            )
        )
    return jobs


def _fetch_govcio(keyword: str) -> list[dict]:
    """Fetch jobs from GovCIO via their Jibe-powered careers portal.

    The Jibe API at careers.govcio.com/api/jobs returns all US jobs regardless
    of keyword (semantic matching returns near-full board), so all jobs are
    fetched once with pagination and cached; keyword filtering is done locally.
    """
    _BASE = "https://careers.govcio.com/api/jobs"
    _JOB_URL_TMPL = "https://careers-govcio.icims.com/jobs/{slug}/{title_slug}/job"

    if _STATIC_CACHE["govcio"] is None:
        all_jobs: list[dict] = []
        try:
            limit = 50
            start = 0
            total = None
            while True:
                resp = requests.get(
                    _BASE,
                    params={
                        "keyword": "",
                        "location": "United States",
                        "limit": limit,
                        "start": start,
                    },
                    headers={**HTML_HEADERS, "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                if total is None:
                    total = data.get("totalCount", 0)
                batch = data.get("jobs", [])
                if not batch:
                    break
                for item in batch:
                    j = item.get("data", {})
                    title = (j.get("title") or "").strip()
                    slug = str(j.get("slug") or j.get("req_id") or "")
                    if not title or not slug:
                        continue
                    title_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                    location = (j.get("full_location") or j.get("location_name") or "United States").strip()
                    all_jobs.append(
                        {
                            "title": title,
                            "location": location,
                            "job_url": _JOB_URL_TMPL.format(slug=slug, title_slug=title_slug),
                            "description": (j.get("description") or "")[:500],
                            "date_posted": j.get("posted_date"),
                        }
                    )
                start += limit
                if total is not None and start >= total:
                    break
        except Exception as e:
            log.error(f"GovCIO careers fetch failed: {e}")
        _STATIC_CACHE["govcio"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["govcio"]:
        if not _contains_keyword(job["title"], keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="GovCIO",
                location=job["location"],
                job_url=job["job_url"],
                description=job.get("description", ""),
                date_posted=job.get("date_posted"),
            )
        )
    return jobs


def _fetch_charles_schwab(keyword: str) -> list[dict]:
    """Fetch jobs from Charles Schwab's TalentBrew careers site (schwabjobs.com).

    GET /search-jobs/results with X-Requested-With header; returns JSON with HTML
    in 'results' key. Location filter (United States) is passed but server also
    returns non-US results, so _is_usa_or_remote() is applied client-side.
    """
    base_url = "https://www.schwabjobs.com/search-jobs/results"
    params = {
        "ActiveFacetID": "0",
        "CurrentPage": "1",
        "RecordsPerPage": "50",
        "TotalContentResults": "",
        "Distance": "50",
        "RadiusUnitType": "0",
        "Keywords": keyword,
        "Location": "United States",
        "ShowRadius": "False",
        "IsPagination": "False",
        "CustomFacetName": "",
        "FacetTerm": "",
        "FacetType": "0",
        "SearchResultsModuleName": "Search Results",
        "SearchFiltersModuleName": "Search Filters",
        "SortCriteria": "0",
        "SortDirection": "0",
        "SearchType": "5",
        "PostalCode": "",
        "ResultsType": "0",
        "fc": "",
        "fl": "",
        "fcf": "",
        "afc": "",
        "afl": "",
        "afcf": "",
        "TotalContentPages": "NaN",
    }
    req_headers = {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*",
        "Referer": "https://www.schwabjobs.com/search-jobs/",
    }
    job_pattern = re.compile(
        r'href="(/job/[^"]+)"[^>]*data-job-id="\d+"[^>]*>'
        r"\s*<h2>([^<]+)</h2>"
        r"\s*<span class=\"job-location\">([^<]+)</span>",
        re.DOTALL,
    )
    seen: set[str] = set()
    jobs: list[dict] = []
    page = 1

    while True:
        params["CurrentPage"] = str(page)
        params["IsPagination"] = "False" if page == 1 else "True"
        try:
            resp = requests.get(base_url, params=params, headers=req_headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"Charles Schwab careers fetch failed: {e}")
            break

        html = data.get("results", "")
        if not html:
            break

        total_pages_m = re.search(r'data-total-pages="(\d+)"', html)
        total_pages = int(total_pages_m.group(1)) if total_pages_m else 1

        for rel_url, title, location in job_pattern.findall(html):
            title = unescape(title.strip())
            location = location.strip()
            if not _is_usa_or_remote(location):
                continue
            job_url = f"https://www.schwabjobs.com{rel_url}"
            if job_url in seen:
                continue
            seen.add(job_url)
            jobs.append(
                _canonical_job(
                    title=title,
                    company="Charles Schwab",
                    location=location,
                    job_url=job_url,
                )
            )

        if page >= total_pages:
            break
        page += 1

    return jobs


def _fetch_ace(keyword: str) -> list[dict]:
    """Fetch jobs from ACE via Workable's POST API.

    Workable returns all jobs regardless of query; all are fetched once and
    cached, then filtered locally by keyword and US/Remote location.
    """
    _TOKEN = "ace-it-careers-1"
    _API = f"https://apply.workable.com/api/v3/accounts/{_TOKEN}/jobs"
    _URL_TMPL = f"https://apply.workable.com/{_TOKEN}/j/{{shortcode}}/"

    if _STATIC_CACHE["ace"] is None:
        all_jobs: list[dict] = []
        try:
            resp = requests.post(
                _API,
                json={"query": "", "location": [], "department": [], "worktype": [], "remote": [], "workplace": []},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            for job in data.get("results", []):
                title = (job.get("title") or "").strip()
                shortcode = job.get("shortcode", "")
                job_url = _URL_TMPL.format(shortcode=shortcode).lower()
                workplace = job.get("workplace", "")
                locations = job.get("locations") or []

                is_remote = workplace == "remote" or job.get("remote", False)
                has_usa = any(
                    (loc.get("countryCode") or loc.get("country", "")) in ("US", "United States")
                    for loc in locations
                )
                if not is_remote and not has_usa:
                    continue

                if is_remote:
                    location = "Remote"
                else:
                    primary = job.get("location") or {}
                    city = primary.get("city") or ""
                    region = primary.get("region") or ""
                    location = ", ".join(filter(None, [city, region])) or "United States"

                date_posted = _parse_date(job.get("published"))
                all_jobs.append(
                    {
                        "title": title,
                        "location": location,
                        "job_url": job_url,
                        "date_posted": date_posted,
                        "description": "",
                    }
                )
        except Exception as e:
            log.error(f"ACE careers fetch failed: {e}")
        _STATIC_CACHE["ace"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["ace"]:
        if not _contains_keyword(job["title"], keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="ACE",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job["date_posted"],
                description=job["description"],
            )
        )
    return jobs


def _fetch_brotherhood_mutual(keyword: str) -> list[dict]:
    """Fetch jobs from Brotherhood Mutual's UltiPro/UKG Pro careers portal.

    UltiPro requires a session CSRF token (X-RequestVerificationToken) obtained
    from a GET to the board page before the POST to LoadSearchResults will work.
    All jobs are fetched once and cached; keyword filtering is done in memory.
    """
    _ORG = "bro1008bmic"
    _BOARD = "67f5ad86-545d-42ef-bb07-cb84ada298ea"
    _BASE = f"https://recruiting.ultipro.com/{_ORG}/JobBoard/{_BOARD}"

    if _STATIC_CACHE["brotherhood_mutual"] is None:
        all_jobs: list[dict] = []
        try:
            session = requests.Session()
            session.headers.update(HTML_HEADERS)

            init_resp = session.get(f"{_BASE}/", timeout=REQUEST_TIMEOUT)
            init_resp.raise_for_status()
            csrf_match = re.search(
                r'name="__RequestVerificationToken"[^>]+value="([^"]+)"',
                init_resp.text,
            )
            if not csrf_match:
                log.error("Brotherhood Mutual careers fetch failed: CSRF token not found")
                _STATIC_CACHE["brotherhood_mutual"] = []
                return []
            csrf = csrf_match.group(1)

            page_size = 50
            skip = 0
            total = None
            while True:
                payload = {
                    "opportunitySearch": {
                        "Top": page_size,
                        "Skip": skip,
                        "QueryString": "",
                        "OrderBy": [
                            {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}
                        ],
                        "Filters": [
                            {"t": "TermsSearchFilterDto", "fieldName": 4, "extra": None, "values": []},
                            {"t": "TermsSearchFilterDto", "fieldName": 5, "extra": None, "values": []},
                            {"t": "TermsSearchFilterDto", "fieldName": 6, "extra": None, "values": []},
                            {"t": "TermsSearchFilterDto", "fieldName": 37, "extra": None, "values": []},
                        ],
                    },
                    "matchCriteria": {
                        "PreferredJobs": [],
                        "Educations": [],
                        "LicenseAndCertifications": [],
                        "Skills": [],
                        "hasNoLicenses": False,
                        "SkippedSkills": [],
                    },
                }
                resp = session.post(
                    f"{_BASE}/JobBoardView/LoadSearchResults",
                    json=payload,
                    headers={
                        "Content-Type": "application/json; charset=UTF-8",
                        "X-RequestVerificationToken": csrf,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Referer": f"{_BASE}/?q=&o=postedDateDesc",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                if total is None:
                    total = data.get("totalCount", 0)

                for job in data.get("opportunities", []):
                    title = (job.get("Title") or "").strip()
                    job_id = job.get("Id", "")
                    job_url = f"{_BASE}/OpportunityDetail?opportunityId={job_id}"

                    loc_type = job.get("JobLocationType")
                    locations = job.get("Locations") or []
                    if loc_type == 2:
                        location = "Remote"
                    elif locations:
                        loc = locations[0]
                        addr = loc.get("Address") or {}
                        city = addr.get("City") or ""
                        state = (addr.get("State") or {}).get("Code") or ""
                        country = (addr.get("Country") or {}).get("Code") or ""
                        location = ", ".join(filter(None, [city, state, country]))
                    else:
                        location = "Unknown"

                    is_remote = loc_type == 2
                    has_usa = any(
                        (loc.get("Address") or {}).get("Country", {}).get("Code") == "USA"
                        for loc in locations
                    )
                    if not is_remote and not has_usa:
                        continue

                    date_posted = _parse_date(job.get("PostedDate"))
                    desc = (job.get("BriefDescription") or "")[:500]

                    all_jobs.append(
                        {
                            "title": title,
                            "location": location,
                            "job_url": job_url,
                            "date_posted": date_posted,
                            "description": desc,
                        }
                    )

                skip += page_size
                if skip >= (total or 0):
                    break
                time.sleep(SLEEP_BETWEEN_REQUESTS)

        except Exception as e:
            log.error(f"Brotherhood Mutual careers fetch failed: {e}")
        _STATIC_CACHE["brotherhood_mutual"] = all_jobs

    jobs = []
    for job in _STATIC_CACHE["brotherhood_mutual"]:
        if not _contains_keyword(f"{job['title']}\n{job['description']}", keyword):
            continue
        jobs.append(
            _canonical_job(
                title=job["title"],
                company="Brotherhood Mutual",
                location=job["location"],
                job_url=job["job_url"],
                date_posted=job["date_posted"],
                description=job["description"],
            )
        )
    return jobs


# ---------------------------------------------------------------------------

BUILTIN_FETCHERS = {
    "Google": _fetch_google,
    "Meta": _fetch_meta,
    "Amazon": _fetch_amazon,
    "Apple": _fetch_apple,
    "Microsoft": _fetch_microsoft,
    "Netflix": _fetch_netflix,
    "Shopify": _fetch_shopify,
    "Nulogy": _fetch_nulogy,
    "RELEX Solutions": _fetch_relex,
    "Sidel": _fetch_sidel,
    "Wayfair": _fetch_wayfair,
    "John Deere": _fetch_john_deere,
    "Komatsu": _fetch_komatsu,
    "Eaton": _fetch_eaton,
    "Honeywell": _fetch_honeywell,
    "Siemens": _fetch_siemens,
    "Volvo Group": _fetch_volvo_group,
    "EquipmentShare": _fetch_equipmentshare,
    "Krones": _fetch_krones,
    "NYU Langone Health": _fetch_nyu_langone,
    "Potter Global Technologies": _fetch_potter_global,
    "Wacker Neuson": _fetch_wacker_neuson,
    "Fastenal": _fetch_fastenal,
    "Applied Materials": _fetch_applied_materials,
    "DSJ Global": _fetch_dsj_global,
    "ECS Federal": _fetch_ecs_federal,
    "Morgan Stanley": _fetch_morgan_stanley,
    "Chase": _fetch_chase,
    "NOV": _fetch_nov,
    "Oracle": _fetch_oracle,
    "Vertical Screen": _fetch_vertical_screen,
    "Milliman": _fetch_milliman,
    "GovCIO": _fetch_govcio,
    "Charles Schwab": _fetch_charles_schwab,
    "Brotherhood Mutual": _fetch_brotherhood_mutual,
    "ACE": _fetch_ace,
}


def _fetch_greenhouse_generic(company_config: dict, keyword: str) -> list[dict]:
    """Generic Greenhouse boards API handler for non-Meta companies."""
    board_token = company_config.get("greenhouse_token", "")
    if not board_token:
        log.warning(f"No greenhouse_token for {company_config['name']}")
        return []
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Greenhouse fetch failed for {company_config['name']}: {e}")
        return []

    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        content = job.get("content", "")
        if not _contains_keyword(title, keyword) and not _contains_keyword(content, keyword):
            continue
        location = job.get("location", {}).get("name", "United States")
        job_url = job.get("absolute_url", "")
        jobs.append(
            {
                "job_title": title.strip(),
                "company": company_config["name"],
                "location": location,
                "job_type": "Remote" if "remote" in location.lower() else "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": content[:500],
                "date_posted": _parse_date(job.get("updated_at")),
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


def _fetch_lever_generic(company_config: dict, keyword: str) -> list[dict]:
    """Generic Lever postings handler for non-Netflix companies."""
    lever_token = company_config.get("lever_token", "")
    if not lever_token:
        log.warning(f"No lever_token for {company_config['name']}")
        return []
    url = f"https://api.lever.co/v0/postings/{lever_token}?mode=json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Lever fetch failed for {company_config['name']}: {e}")
        return []

    if not isinstance(data, list):
        return []

    jobs = []
    for job in data:
        title = job.get("text", "")
        description = job.get("descriptionPlain") or job.get("description", "")
        if not _contains_keyword(title, keyword) and not _contains_keyword(description, keyword):
            continue
        job_url = job.get("hostedUrl") or ""
        categories = job.get("categories", {})
        location = categories.get("location", "United States")
        created_at = job.get("createdAt")
        date_posted = None
        if created_at:
            try:
                date_posted = datetime.fromtimestamp(int(created_at) / 1000).date().isoformat()
            except (ValueError, TypeError):
                pass
        jobs.append(
            {
                "job_title": title.strip(),
                "company": company_config["name"],
                "location": location,
                "job_type": "Remote" if "remote" in location.lower() else "Unknown",
                "job_url": job_url.lower(),
                "description_snippet": description[:500],
                "date_posted": date_posted,
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


def _fetch_custom_generic(company_config: dict, keyword: str) -> list[dict]:
    """
    Generic custom API handler. Requires the company config to specify
    a url template with {keyword} placeholder. Only works if the API
    returns a JSON list or a dict with a 'jobs' / 'results' key.
    """
    url_template = company_config.get("url", "")
    if not url_template:
        return []
    url = url_template.replace("{keyword}", quote_plus(keyword))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Custom fetch failed for {company_config['name']}: {e}")
        return []

    # Try to find a list of jobs
    if isinstance(data, list):
        raw_jobs = data
    elif isinstance(data, dict):
        for key in ("jobs", "results", "data", "items"):
            if isinstance(data.get(key), list):
                raw_jobs = data[key]
                break
        else:
            log.warning(f"Custom fetch for {company_config['name']}: can't find job list in response")
            return []
    else:
        return []

    # Best-effort extraction — field names vary
    jobs = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        title = job.get("title") or job.get("jobTitle") or job.get("name") or ""
        if not _contains_keyword(title, keyword):
            continue
        job_url = job.get("url") or job.get("applyUrl") or job.get("absolute_url") or ""
        location = job.get("location") or job.get("locationName") or "United States"
        if isinstance(location, dict):
            location = location.get("name") or location.get("address") or "United States"
        jobs.append(
            {
                "job_title": str(title).strip(),
                "company": company_config["name"],
                "location": str(location),
                "job_type": "Unknown",
                "job_url": str(job_url).lower(),
                "description_snippet": str(job.get("description") or "")[:500],
                "date_posted": _parse_date(job.get("updated_at") or job.get("postedDate") or job.get("datePosted")),
                "source": "company_careers",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
            }
        )
    return jobs


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(config: dict) -> list[dict]:
    """
    Fetch jobs from all configured company career pages.
    Returns deduplicated list of canonical job dicts.
    """
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")

    keywords = config["search"]["keywords"]
    companies = [c for c in config.get("companies", []) if c.get("enabled", True)]

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()

    for company in companies:
        name = company["name"]
        ats = company.get("ats", "custom")
        fetcher = BUILTIN_FETCHERS.get(name)
        company_raw_count = 0  # raw jobs from fetcher, before dedup
        fetch_ok = False  # set True when at least one keyword fetch doesn't raise

        for keyword in keywords:
            log.info(f"Company Careers: fetching {name} / '{keyword}'")
            try:
                if fetcher:
                    jobs = fetcher(keyword)
                elif ats == "greenhouse":
                    jobs = _fetch_greenhouse_generic(company, keyword)
                elif ats == "lever":
                    jobs = _fetch_lever_generic(company, keyword)
                else:
                    jobs = _fetch_custom_generic(company, keyword)
                fetch_ok = True
            except Exception as e:
                log.error(f"Company Careers: {name} / '{keyword}' FAILED — {e}")
                jobs = []

            company_raw_count += len(jobs)
            for job in jobs:
                job["_query"] = keyword
                url = job["job_url"]
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_jobs.append(job)

            time.sleep(SLEEP_BETWEEN_REQUESTS)

        # Skip health update if every keyword fetch raised an exception — we
        # have no signal about the board's actual state and should not push
        # days_empty upward on a transient network or parse error.
        if not fetch_ok:
            log.warning(f"Company Careers: all fetches for {name} failed — skipping health update")
            continue

        # For static-cache adapters, use the raw board size (all jobs fetched,
        # independent of keyword matching) so a healthy board with no QA
        # keyword hits doesn't drift into monitor/critical.
        cache_key = _COMPANY_CACHE_KEYS.get(name)
        cached = _STATIC_CACHE.get(cache_key) if cache_key else None
        health_jobs_found = len(cached) if cached else company_raw_count

        ats_token = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        # Prefer an explicit board_url (for companies whose url is an API endpoint);
        # fall back to stripping query-string / keyword placeholders from the fetch URL.
        job_board_url = company.get("board_url") or re.sub(r"\{[^}]+\}", "", company.get("url", "")).split("?")[
            0
        ].rstrip("/")
        upsert_company_health(
            company=name,
            ats_type="company_careers",
            ats_token=ats_token,
            job_board_url=job_board_url,
            jobs_found=health_jobs_found,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )

    log.info(f"Company Careers: {len(all_jobs)} unique jobs fetched")
    return all_jobs
