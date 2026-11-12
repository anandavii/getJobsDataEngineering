"""
Fetch QA/automation jobs from Dice.com by parsing React Server Components (RSC)
streaming format embedded in the page HTML.

Dice.com migrated from Next.js __NEXT_DATA__ to RSC. Job data is now embedded
in self.__next_f.push([id, json_string]) script tags, with job objects living
inside jobList.data within one of the RSC payloads.

Note: Dice.com may return 403 on GitHub Actions IP ranges.
This is handled gracefully — the source is non-fatal.

Usage:
    results = run(config)
    # returns list of canonical job dicts
"""

import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DICE_BASE_URL = "https://www.dice.com/jobs"
REQUEST_TIMEOUT = 25
SLEEP_BETWEEN_KEYWORDS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_dice_date(date_str: str | None) -> str | None:
    if not date_str:
        return None
    # Dice often uses ISO 8601 with timezone
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:26], fmt).date().isoformat()
        except ValueError:
            pass
    return date_str[:10] if len(date_str) >= 10 else None


def _normalize_job_type(employment_type: str | None) -> str:
    if not employment_type:
        return "Unknown"
    et = employment_type.lower()
    if "remote" in et:
        return "Remote"
    if "hybrid" in et:
        return "Hybrid"
    if "on-site" in et or "onsite" in et or "in-person" in et:
        return "On-site"
    return "Unknown"


def _find_joblist_data(obj, max_depth: int = 50) -> list:
    """
    Recursively search a parsed RSC payload object for jobList.data.
    Returns the job list if found, empty list otherwise.
    """
    if max_depth <= 0:
        return []
    if isinstance(obj, dict):
        if "jobList" in obj:
            job_list = obj["jobList"]
            if isinstance(job_list, dict) and isinstance(job_list.get("data"), list):
                return job_list["data"]
        for v in obj.values():
            result = _find_joblist_data(v, max_depth - 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_joblist_data(item, max_depth - 1)
            if result:
                return result
    return []


def _parse_rsc_jobs(html_text: str) -> list[dict]:
    """
    Parse job listings from Dice.com RSC (React Server Components) streaming format.
    Job data is embedded in self.__next_f.push([id, json_string]) script tags,
    with job objects living inside jobList.data in one of the payloads.
    """
    soup = BeautifulSoup(html_text, "lxml")

    for script in soup.find_all("script"):
        text = script.string or ""
        if "self.__next_f.push" not in text or '"jobList"' not in text:
            continue

        for m in re.finditer(r"self\.__next_f\.push\(", text):
            start = m.end()  # position right after the opening '('
            try:
                arr, _ = json.JSONDecoder().raw_decode(text, start)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(arr, list) or len(arr) < 2:
                continue

            payload_str = arr[1]
            if not isinstance(payload_str, str) or '"jobList"' not in payload_str:
                continue

            # RSC payloads sometimes carry a leading "row_id:" prefix — strip it
            stripped = re.sub(r"^\d+:", "", payload_str, count=1)
            for candidate in (stripped, payload_str):
                try:
                    data = json.loads(candidate)
                    jobs = _find_joblist_data(data)
                    if jobs:
                        log.debug(f"Dice: RSC parser found {len(jobs)} raw jobs")
                        return jobs
                except (json.JSONDecodeError, ValueError):
                    continue

    return []


def _to_canonical(job: dict) -> dict | None:
    # RSC field names: detailsPageUrl, title, companyName, summary,
    # jobLocation (dict with city/state/country/displayName), postedDate,
    # employmentType, workplaceTypes (list), isRemote.
    # Legacy field names kept as fallbacks.
    job_url = job.get("detailsPageUrl") or job.get("applyUrl") or job.get("applyLink") or ""
    if job_url and not job_url.startswith("http"):
        job_url = f"https://www.dice.com{job_url}"

    # Fallback: build URL from id
    if not job_url:
        job_id = job.get("id") or job.get("jobId", "")
        if job_id:
            job_url = f"https://www.dice.com/jobs/detail/{job_id}"
    if not job_url:
        return None

    # Location — RSC: jobLocation dict; legacy: location dict
    location_data = job.get("jobLocation") or job.get("location") or {}
    if isinstance(location_data, dict):
        display = location_data.get("displayName", "")
        if not display:
            city = location_data.get("city", "")
            state = location_data.get("state", "")
            display = f"{city}, {state}".strip(", ")
        location_str = display or "United States"
    else:
        location_str = str(location_data) or "United States"

    is_remote = bool(job.get("isRemote")) or "remote" in location_str.lower()

    # Job type — RSC: workplaceTypes list or employmentType string
    workplace_types = job.get("workplaceTypes") or []
    workplace_str = " ".join(workplace_types).lower() if isinstance(workplace_types, list) else ""
    if is_remote or "remote" in workplace_str:
        job_type = "Remote"
    elif "hybrid" in workplace_str:
        job_type = "Hybrid"
    else:
        job_type_raw = job.get("employmentType") or job.get("jobType", "")
        job_type = _normalize_job_type(job_type_raw)

    description = (job.get("summary") or job.get("descriptionTeaser") or job.get("description") or "")[:500]

    # Company — RSC: companyName string; legacy: company dict
    company_raw = job.get("companyName") or job.get("company") or ""
    if isinstance(company_raw, dict):
        company = company_raw.get("name", "")
    else:
        company = str(company_raw)

    # Date — RSC: postedDate; legacy: datePosted
    date_str = job.get("postedDate") or job.get("datePosted")

    # Salary — RSC: salary dict or string
    salary_raw = job.get("salary") or {}
    salary_min = salary_max = salary_currency = None
    if isinstance(salary_raw, dict):
        salary_min = salary_raw.get("min") or salary_raw.get("minimum")
        salary_max = salary_raw.get("max") or salary_raw.get("maximum")
        salary_currency = salary_raw.get("currency", "USD") if (salary_min or salary_max) else None

    return {
        "job_title": (job.get("title") or job.get("jobTitle") or "").strip(),
        "company": company,
        "location": location_str,
        "job_type": job_type,
        "job_url": job_url.lower(),
        "description_snippet": description,
        "date_posted": _parse_dice_date(date_str),
        "source": "dice",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
    }


def _fetch_keyword(keyword: str) -> list[dict]:
    params = {
        "q": keyword,
        "location": "United States",
        "datePosted": "ONE",  # Last 24 hours
        "countryCode": "US",
        "language": "en",
    }
    query_str = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    url = f"{DICE_BASE_URL}?{query_str}"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 403:
            log.warning(f"Dice.com returned 403 for keyword '{keyword}' (IP may be blocked on CI)")
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Dice request failed for keyword '{keyword}': {e}")
        return []

    jobs_raw = _parse_rsc_jobs(resp.text)
    if not jobs_raw:
        log.warning(f"Dice: no job data found in RSC payload for keyword '{keyword}'")
        return []

    jobs = []
    for j in jobs_raw:
        canonical = _to_canonical(j)
        if canonical and canonical["job_title"]:
            jobs.append(canonical)

    return jobs


def run(config: dict) -> list[dict]:
    """
    Fetch jobs for all configured keywords from Dice.com.
    Returns deduplicated list of canonical job dicts.
    """
    keywords = config["search"]["keywords"]
    all_jobs: list[dict] = []
    seen_urls: set[str] = set()

    for i, keyword in enumerate(keywords):
        log.info(f"Dice: fetching keyword {i + 1}/{len(keywords)}: '{keyword}'")
        jobs = _fetch_keyword(keyword)

        for job in jobs:
            job["_query"] = keyword
            url = job["job_url"]
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_jobs.append(job)

        if i < len(keywords) - 1:
            time.sleep(SLEEP_BETWEEN_KEYWORDS)

    log.info(f"Dice: {len(all_jobs)} unique jobs fetched across {len(keywords)} keywords")
    return all_jobs
