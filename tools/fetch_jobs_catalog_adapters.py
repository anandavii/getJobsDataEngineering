"""
Catalog-driven ATS/board adapters used by role-horizon expansion.
All adapters are fault-tolerant and return canonical job dict lists.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, UTC
from html import unescape
from typing import Any

import requests

from company_health import upsert_company_health

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 25
HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (compatible; job-search-bot/1.0)",
    "Accept": "application/json",
}

_SUPABASE_URL = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def _parse_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    date_str = str(date_str)

    # epoch ms / sec support
    if date_str.isdigit():
        try:
            ts = int(date_str)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts).date().isoformat()
        except Exception:
            pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(date_str[:32], fmt).date().isoformat()
        except ValueError:
            pass

    # Workday relative strings: "Posted Today", "Posted Yesterday",
    # "Posted N Days ago", "Posted 30+ Days ago"
    lower = date_str.lower().strip()
    if lower.startswith("posted"):
        today = datetime.now(tz=UTC).date()
        if "today" in lower or lower == "posted yes":
            return today.isoformat()
        if "yesterday" in lower:
            return (today - timedelta(days=1)).isoformat()
        m = re.search(r"(\d+)\+?\s*day", lower)
        if m:
            return (today - timedelta(days=int(m.group(1)))).isoformat()
        # "Posted Yes" already handled above; anything else unparseable → None
        return None

    return None


def _strip_html(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    text = (text or "").lower().strip()
    phrase = (phrase or "").lower().strip()
    if not text or not phrase:
        return False
    tokens = re.findall(r"[a-z0-9]+", phrase)
    if not tokens:
        return False
    pat = r"\b" + r"[\W_]+".join(re.escape(t) for t in tokens) + r"\b"
    return re.search(pat, text) is not None


def _matching_keyword(text: str, keywords: list[str]) -> str | None:
    for kw in keywords:
        if _contains_phrase(text, kw):
            return kw
    return None


_STATE_ABBREVS: frozenset[str] = frozenset(
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
_STATE_NAMES: frozenset[str] = frozenset(
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

_KNOWN_US_LOCATION_ALIASES: frozenset[str] = frozenset(
    {
        "chicago warehouse",
        "phillipsburg warehouse",
        "san bernardino warehouse",
        "los angeles warehouse",
    }
)


def _is_usa_or_remote(location: str) -> bool:
    if not location:
        return True
    loc = location.lower().strip()
    if "remote" in loc:
        return True
    if loc in _KNOWN_US_LOCATION_ALIASES:
        return True
    if any(token in loc for token in ("united states", " usa", " u.s.", " u.s.a", " us", "nationwide")):
        return True

    parts = [p.strip() for p in location.split(",") if p.strip()]
    if parts:
        first = parts[0]
        if first.upper() in {"US", "USA"}:
            return True
        last = parts[-1].upper()
        if len(last) == 2 and last in _STATE_ABBREVS:
            return True
        if last.lower() in _STATE_NAMES:
            return True
    return False


def _entry_enabled(entry: dict) -> bool:
    return bool(entry.get("enabled", True))


def _canonical(
    *,
    title: str,
    company: str,
    location: str,
    job_url: str,
    description: str,
    date_posted: str | None,
    source: str,
    query: str | None,
) -> dict | None:
    url = (job_url or "").strip().lower()
    if not title or not url:
        return None
    return {
        "job_title": title.strip(),
        "company": (company or "").strip(),
        "location": (location or "United States").strip(),
        "job_type": "Remote" if "remote" in (location or "").lower() else "Unknown",
        "job_url": url,
        "description_snippet": (description or "")[:500],
        "date_posted": _parse_date(date_posted),
        "source": source,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "_query": query,
    }


def _dedup(jobs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for job in jobs:
        url = (job.get("job_url") or "").lower().strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(job)
    return out


def _request_json(url: str, method: str = "GET", payload: Any = None) -> Any:
    if method == "POST":
        resp = requests.post(url, headers=HEADERS_JSON, json=payload, timeout=REQUEST_TIMEOUT)
    else:
        resp = requests.get(url, headers=HEADERS_JSON, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


_ASHBY_DETAIL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=jobPosting"
_ASHBY_DETAIL_QUERY = (
    "query jobPosting($jobPostingId: String!) { jobPosting(jobPostingId: $jobPostingId) { publishedDate } }"
)


def _fetch_ashby_published_date(job_id: str) -> str | None:
    """Return publishedDate for one Ashby job via the detail API, or None on any failure."""
    if not job_id:
        return None
    try:
        data = _request_json(
            _ASHBY_DETAIL_URL,
            method="POST",
            payload={
                "operationName": "jobPosting",
                "variables": {"jobPostingId": job_id},
                "query": _ASHBY_DETAIL_QUERY,
            },
        )
        return (data.get("data", {}).get("jobPosting") or {}).get("publishedDate")
    except Exception as e:
        log.warning(f"Ashby detail fetch failed for job {job_id}: {e}")
        return None


def run_greenhouse(config: dict, keywords: list[str] | None = None) -> list[dict]:
    keywords = keywords or config.get("search", {}).get("keywords", [])
    entries = config.get("sources_catalog", {}).get("greenhouse", [])
    jobs: list[dict] = []

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        token = entry.get("board_token") or entry.get("token")
        if not token and not entry.get("url"):
            continue
        url = entry.get("url") or f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        company_name = entry.get("name") or str(token or "greenhouse")

        try:
            data = _request_json(url)
        except Exception as e:
            log.error(f"Greenhouse fetch failed for '{company_name}': {e}")
            continue

        raw_jobs = data.get("jobs", [])
        job_board_url = entry.get("job_board_url", "")
        if not job_board_url:
            log.warning(
                "Missing job_board_url for %s (%s) — health record will have empty board link",
                entry.get("name"),
                "greenhouse",
            )
        upsert_company_health(
            company=company_name,
            ats_type="greenhouse",
            ats_token=token or "",
            job_board_url=job_board_url,
            jobs_found=len(raw_jobs),
            supabase_url=_SUPABASE_URL,
            supabase_key=_SUPABASE_KEY,
        )

        for job in raw_jobs:
            title = str(job.get("title") or "")
            desc = _strip_html(str(job.get("content") or ""))
            match_kw = _matching_keyword(f"{title}\n{desc}", keywords)
            if not match_kw:
                continue
            location = str(job.get("location", {}).get("name") or "United States")
            if not _is_usa_or_remote(location):
                continue
            row = _canonical(
                title=title,
                company=company_name,
                location=location,
                job_url=str(job.get("absolute_url") or ""),
                description=desc,
                date_posted=job.get("updated_at") or job.get("created_at"),
                source="greenhouse",
                query=match_kw,
            )
            if row:
                jobs.append(row)

    return _dedup(jobs)


def run_lever(config: dict, keywords: list[str] | None = None) -> list[dict]:
    keywords = keywords or config.get("search", {}).get("keywords", [])
    entries = config.get("sources_catalog", {}).get("lever", [])
    jobs: list[dict] = []

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        token = entry.get("board_token") or entry.get("token")
        if not token and not entry.get("url"):
            continue
        url = entry.get("url") or f"https://api.lever.co/v0/postings/{token}?mode=json"
        company_name = entry.get("name") or str(token or "lever")

        try:
            data = _request_json(url)
        except Exception as e:
            log.error(f"Lever fetch failed for '{company_name}': {e}")
            continue

        raw_jobs = data if isinstance(data, list) else []
        job_board_url = entry.get("job_board_url", "")
        if not job_board_url:
            log.warning(
                "Missing job_board_url for %s (%s) — health record will have empty board link",
                entry.get("name"),
                "lever",
            )
        upsert_company_health(
            company=company_name,
            ats_type="lever",
            ats_token=token or "",
            job_board_url=job_board_url,
            jobs_found=len(raw_jobs),
            supabase_url=_SUPABASE_URL,
            supabase_key=_SUPABASE_KEY,
        )

        for job in raw_jobs:
            title = str(job.get("text") or "")
            desc = _strip_html(str(job.get("descriptionPlain") or job.get("description") or ""))
            match_kw = _matching_keyword(f"{title}\n{desc}", keywords)
            if not match_kw:
                continue
            categories = job.get("categories") or {}
            location = str(categories.get("location") or "United States")
            if not _is_usa_or_remote(location):
                continue
            row = _canonical(
                title=title,
                company=company_name,
                location=location,
                job_url=str(job.get("hostedUrl") or job.get("applyUrl") or ""),
                description=desc,
                date_posted=job.get("createdAt"),
                source="lever",
                query=match_kw,
            )
            if row:
                jobs.append(row)

    return _dedup(jobs)


def run_ashby(config: dict, keywords: list[str] | None = None) -> list[dict]:
    keywords = keywords or config.get("search", {}).get("keywords", [])
    entries = config.get("sources_catalog", {}).get("ashby", [])
    jobs: list[dict] = []

    # NOTE: Ashby's GraphQL schema changed — descriptionHtml, publishedDate, applyUrl,
    # isRemote, and teamName are no longer available in the list response.
    # Valid fields as of 2026-03: id, title, locationName, workplaceType, employmentType, teamId.
    # Job URL is constructed from org slug + job id.
    graphql_query = (
        "query apiJobBoardWithTeams($organizationHostedJobsPageName: String!) "
        "{ jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) "
        "{ jobPostings { id title locationName workplaceType } } }"
    )

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        org = entry.get("organization") or entry.get("org")
        company_name = entry.get("name") or str(org or "ashby")
        url = entry.get("url") or "https://jobs.ashbyhq.com/api/non-user-graphql?op=apiJobBoardWithTeams"
        if not org and not entry.get("url"):
            continue

        payload = entry.get("payload")
        if not payload:
            payload = {
                "operationName": "apiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": org},
                "query": graphql_query,
            }

        try:
            data = _request_json(url, method="POST", payload=payload)
        except Exception as e:
            log.error(f"Ashby fetch failed for '{company_name}': {e}")
            continue

        # Check for API errors (GraphQL may return 200 with errors in body)
        if not isinstance(data, dict):
            log.error(f"Ashby fetch failed for '{company_name}': invalid response type")
            continue
        if "errors" in data:
            log.error(f"Ashby fetch failed for '{company_name}': {data['errors'][0].get('message', 'unknown error')}")
            continue

        data_obj = data.get("data", {})
        if not data_obj:
            log.error(f"Ashby fetch failed for '{company_name}': no data in response")
            continue

        job_board = data_obj.get("jobBoardWithTeams")
        if job_board is None:
            log.warning(f"Ashby: '{company_name}' board returned null (may not have published jobs)")
            continue

        postings = job_board.get("jobPostings", [])
        job_board_url = entry.get("job_board_url", "")
        if not job_board_url:
            log.warning(
                "Missing job_board_url for %s (%s) — health record will have empty board link",
                entry.get("name"),
                "ashby",
            )
        upsert_company_health(
            company=company_name,
            ats_type="ashby",
            ats_token=org or "",
            job_board_url=job_board_url,
            jobs_found=len(postings),
            supabase_url=_SUPABASE_URL,
            supabase_key=_SUPABASE_KEY,
        )

        for job in postings:
            title = str(job.get("title") or "")
            match_kw = _matching_keyword(title, keywords)
            if not match_kw:
                continue
            location = str(job.get("locationName") or "United States")
            workplace = str(job.get("workplaceType") or "")
            # Treat Remote workplaceType as a USA-eligible remote role
            if workplace.lower() == "remote":
                pass  # always include remote roles
            elif not _is_usa_or_remote(location):
                continue
            job_id = str(job.get("id") or "")
            job_url = f"https://jobs.ashbyhq.com/{org}/{job_id}" if org and job_id else ""
            row = _canonical(
                title=title,
                company=company_name,
                location=location,
                job_url=job_url,
                description="",
                date_posted=_fetch_ashby_published_date(job_id),
                source="ashby",
                query=match_kw,
            )
            if row:
                jobs.append(row)
            time.sleep(0.3)  # throttle: avoid rapid-fire POSTs to Ashby detail API

    return _dedup(jobs)


def run_breezyhr(config: dict, keywords: list[str] | None = None) -> list[dict]:
    keywords = keywords or config.get("search", {}).get("keywords", [])
    entries = config.get("sources_catalog", {}).get("breezyhr", [])
    jobs: list[dict] = []

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        subdomain = entry.get("subdomain")
        if not subdomain and not entry.get("url"):
            continue
        url = entry.get("url") or f"https://{subdomain}.breezy.hr/json"
        company_name = entry.get("name") or str(subdomain or "breezyhr")

        try:
            data = _request_json(url)
        except Exception as e:
            log.error(f"Breezy HR fetch failed for '{company_name}': {e}")
            continue

        if not isinstance(data, list):
            log.warning(f"Breezy HR: unexpected response shape for '{company_name}'")
            continue

        job_board_url = entry.get("job_board_url", "")
        if not job_board_url:
            log.warning(
                "Missing job_board_url for %s (%s) — health record will have empty board link",
                entry.get("name"),
                "breezyhr",
            )
        upsert_company_health(
            company=company_name,
            ats_type="breezyhr",
            ats_token=subdomain or "",
            job_board_url=job_board_url,
            jobs_found=len(data),
            supabase_url=_SUPABASE_URL,
            supabase_key=_SUPABASE_KEY,
        )

        for job in data:
            if not isinstance(job, dict):
                continue
            title = str(job.get("name") or "")
            desc = _strip_html(str(job.get("description") or ""))
            match_kw = _matching_keyword(f"{title}\n{desc}", keywords)
            if not match_kw:
                continue
            location = str((job.get("location") or {}).get("name") or "United States")
            if not _is_usa_or_remote(location):
                continue
            row = _canonical(
                title=title,
                company=company_name,
                location=location,
                job_url=str(job.get("url") or ""),
                description=desc,
                date_posted=job.get("published_date"),
                source="breezyhr",
                query=match_kw,
            )
            if row:
                jobs.append(row)

    return _dedup(jobs)


def run_smartrecruiters(config: dict, keywords: list[str] | None = None) -> list[dict]:
    keywords = keywords or config.get("search", {}).get("keywords", [])
    entries = config.get("sources_catalog", {}).get("smartrecruiters", [])
    jobs: list[dict] = []

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        company = entry.get("company_identifier") or entry.get("token")
        if not company and not entry.get("url"):
            continue
        base_url = entry.get("url") or f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
        company_name = entry.get("name") or str(company or "smartrecruiters")

        for keyword in keywords:
            from urllib.parse import urlencode

            params = urlencode({"limit": 100, "q": keyword})
            url = f"{base_url}?{params}"
            try:
                data = _request_json(url)
            except Exception as e:
                log.error(f"SmartRecruiters fetch failed for '{company_name}' / '{keyword}': {e}")
                continue

            for job in data.get("content", []) if isinstance(data, dict) else []:
                title = str(job.get("name") or "")
                desc = _strip_html(str(job.get("jobAd", {}).get("sections", "") or ""))
                loc_obj = job.get("location") or {}
                location = str(
                    loc_obj.get("fullLocation")
                    or loc_obj.get("city")
                    or loc_obj.get("region")
                    or loc_obj.get("country")
                    or "United States"
                )
                if not _is_usa_or_remote(location):
                    continue
                posting_id = job.get("id")
                job_url = f"https://jobs.smartrecruiters.com/{company}/{posting_id}" if posting_id else ""
                row = _canonical(
                    title=title,
                    company=company_name,
                    location=location,
                    job_url=str(job_url),
                    description=desc,
                    date_posted=job.get("releasedDate"),
                    source="smartrecruiters",
                    query=keyword,
                )
                if row:
                    jobs.append(row)

    return _dedup(jobs)


def _run_generic_json_entries(config: dict, source_key: str, keywords: list[str]) -> list[dict]:
    entries = config.get("sources_catalog", {}).get(source_key, [])
    jobs: list[dict] = []

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        url = entry.get("url")
        if not url:
            continue
        company_name = entry.get("name") or source_key

        try:
            data = _request_json(url)
        except Exception as e:
            log.error(f"{source_key} fetch failed for '{company_name}': {e}")
            continue

        # Accept common envelope shapes; fallback to list root.
        records = []
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            for key in ("results", "jobs", "jobPostings", "content", "data"):
                if isinstance(data.get(key), list):
                    records = data.get(key)
                    break

        for job in records:
            title = str(job.get("title") or job.get("name") or job.get("text") or "")
            desc = _strip_html(str(job.get("description") or job.get("descriptionPlain") or ""))
            match_kw = _matching_keyword(f"{title}\n{desc}", keywords)
            if not match_kw:
                continue
            location = str(job.get("location") or job.get("locationName") or "United States")
            if isinstance(job.get("location"), dict):
                loc_obj = job.get("location")
                location = str(loc_obj.get("name") or loc_obj.get("city") or loc_obj.get("country") or "United States")
            if not _is_usa_or_remote(location):
                continue
            job_url = str(job.get("url") or job.get("jobUrl") or job.get("applyUrl") or job.get("hostedUrl") or "")
            row = _canonical(
                title=title,
                company=company_name,
                location=location,
                job_url=job_url,
                description=desc,
                date_posted=job.get("updated_at")
                or job.get("updatedAt")
                or job.get("createdAt")
                or job.get("datePosted"),
                source=source_key,
                query=match_kw,
            )
            if row:
                jobs.append(row)

    return _dedup(jobs)


# Workday returns city-only names in locationsText (e.g. "San Jose", "Bangalore").
# _is_usa_or_remote fails on bare city names, so we use a non-US blocklist instead.
_WORKDAY_NON_US_CITIES: frozenset[str] = frozenset(
    {
        # India
        "bangalore",
        "bengaluru",
        "noida",
        "hyderabad",
        "pune",
        "mumbai",
        "delhi",
        "new delhi",
        "gurgaon",
        "gurugram",
        "chennai",
        "kolkata",
        # Romania / Eastern Europe
        "bucharest",
        "cluj",
        "iasi",
        "warsaw",
        "krakow",
        "prague",
        # Western Europe
        "london",
        "paris",
        "berlin",
        "amsterdam",
        "dublin",
        "stockholm",
        "oslo",
        "copenhagen",
        "zurich",
        "munich",
        "hamburg",
        "madrid",
        "milan",
        # APAC
        "tokyo",
        "osaka",
        "seoul",
        "beijing",
        "shanghai",
        "shenzhen",
        "singapore",
        "sydney",
        "melbourne",
        # Canada (separate labor market)
        "toronto",
        "ottawa",
        "montreal",
        "vancouver",
        "calgary",
        "winnipeg",
        "edmonton",
        # India (additional cities)
        "ahmedabad",
        "jaipur",
        "kochi",
        "chandigarh",
        # Europe (additional cities)
        "lisbon",
        "barcelona",
        "rome",
        "vienna",
        "brussels",
        "helsinki",
        "kyiv",
        # APAC (additional cities)
        "hong kong",
        "taipei",
        "jakarta",
        "kuala lumpur",
        "manila",
        "bangkok",
        # Middle East
        "dubai",
        "abu dhabi",
        "riyadh",
        # Latin America
        "sao paulo",
        "mexico city",
        "bogota",
        "buenos aires",
        # Other
        "yerevan",
        "tbilisi",
        "tel aviv",
        "cairo",
        "nairobi",
    }
)


def _workday_is_usa_or_remote(locations_text: str, external_path: str) -> bool:
    """
    Workday locationsText is a bare city name with no country/state.
    Pass-through unless the city is in a known non-US blocklist.
    Multi-location jobs ("3 Locations") are always passed through.
    """
    loc = (locations_text or "").lower().strip()
    if not loc:
        return True
    if "remote" in loc:
        return True
    # "N Locations" → multi-location, may include US offices — pass through
    if "location" in loc:
        return True
    # Derive city from externalPath as fallback (e.g. /job/San-Jose/Title_R123)
    path_parts = (external_path or "").split("/")
    path_city = path_parts[2].lower().replace("-", " ") if len(path_parts) > 2 else ""
    text = loc or path_city
    # Substring match so "Bengaluru, India" matches blocked city "bengaluru"
    return not any(blocked in text for blocked in _WORKDAY_NON_US_CITIES)


def run_workday(config: dict, keywords: list[str] | None = None) -> list[dict]:
    keywords = keywords or config.get("search", {}).get("keywords", [])
    entries = config.get("sources_catalog", {}).get("workday", [])
    jobs: list[dict] = []

    for entry in entries:
        if not _entry_enabled(entry):
            continue
        tenant = entry.get("tenant", "")
        site = entry.get("site", "")
        company_name = entry.get("name") or tenant or "workday"

        if entry.get("url"):
            base_url = entry["url"].rstrip("/")
        elif tenant and site:
            subdomain = entry.get("subdomain", "wd5")
            base_url = f"https://{tenant}.{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        else:
            log.warning(f"Workday: skipping '{company_name}' — missing tenant/site and no url override")
            continue

        company_raw_count = 0
        successful_fetches = 0
        for keyword in keywords:
            payload = {
                "searchText": keyword,
                "limit": 20,
                "offset": 0,
                "appliedFacets": {},
            }
            try:
                data = _request_json(base_url, method="POST", payload=payload)
            except Exception as e:
                log.error(f"Workday fetch failed for '{company_name}' / '{keyword}': {e}")
                continue

            successful_fetches += 1
            raw_postings = data.get("jobPostings", [])
            company_raw_count += len(raw_postings)
            for job in raw_postings:
                title = str(job.get("title") or "")
                location = str(job.get("locationsText") or "United States")
                external_path = str(job.get("externalPath") or "")
                if not _workday_is_usa_or_remote(location, external_path):
                    continue
                if entry.get("url"):
                    job_url = external_path
                else:
                    job_url = (
                        f"https://{tenant}.{subdomain}.myworkdayjobs.com/en-US/{site}{external_path}"
                        if external_path
                        else ""
                    )
                desc = _strip_html(str(job.get("jobDescription", {}).get("jobDescription") or ""))
                row = _canonical(
                    title=title,
                    company=company_name,
                    location=location,
                    job_url=job_url,
                    description=desc,
                    date_posted=job.get("postedOn"),
                    source="workday",
                    query=keyword,
                )
                if row:
                    jobs.append(row)

        if successful_fetches > 0:
            job_board_url = entry.get("job_board_url", "")
            if not job_board_url:
                log.warning(
                    "Missing job_board_url for %s (%s) — health record will have empty board link",
                    entry.get("name"),
                    "workday",
                )
            upsert_company_health(
                company=company_name,
                ats_type="workday",
                ats_token=tenant or "",
                job_board_url=job_board_url,
                jobs_found=company_raw_count,
                supabase_url=_SUPABASE_URL,
                supabase_key=_SUPABASE_KEY,
            )

    return _dedup(jobs)


def run_workable(config: dict, keywords: list[str] | None = None) -> list[dict]:
    return _run_generic_json_entries(config, "workable", keywords or config.get("search", {}).get("keywords", []))


def run_icims(config: dict, keywords: list[str] | None = None) -> list[dict]:
    return _run_generic_json_entries(config, "icims", keywords or config.get("search", {}).get("keywords", []))


def run_jobvite(config: dict, keywords: list[str] | None = None) -> list[dict]:
    return _run_generic_json_entries(config, "jobvite", keywords or config.get("search", {}).get("keywords", []))
