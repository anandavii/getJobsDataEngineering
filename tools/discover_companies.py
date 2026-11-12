"""
discover_companies.py — ATS job board discovery tool.

Discovers companies hiring for the active job domain on Greenhouse, Lever, Ashby, and Breezy HR
by querying DuckDuckGo Lite HTML search, extracting company tokens/slugs,
verifying active-domain job counts, and outputting a ready-to-merge candidates JSON.

Usage:
    uv run python tools/discover_companies.py [--ats greenhouse lever ashby breezyhr]
        [--min-relevant-jobs 3] [--output path] [--keywords kw1 kw2 ...]
    uv run python tools/discover_companies.py --apply config/discovery/candidates_2026-03-30.json
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
import tempfile
import time
from datetime import date
from html import unescape
from pathlib import Path
from typing import Any, Literal
from collections.abc import Callable

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCES_CATALOG = _REPO_ROOT / "config" / "sources" / "sources_catalog.json"
_DISCOVERY_DIR = _REPO_ROOT / "config" / "discovery"
# Ensure tools/ is on sys.path so local modules can be imported at module level.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from config_loader import ConfigLoadError, load_config
from domain_relevance import is_domain_relevant

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT = 20
DDG_DELAY = 1.5  # seconds between DuckDuckGo requests
MAX_PER_ATS = 50  # cap discovered companies before verification

HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (compatible; job-search-bot/1.0)",
    "Accept": "application/json",
}
HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Path segments that appear in ATS URLs but are not company tokens
_SKIP_TOKENS: frozenset[str] = frozenset(
    {"embed", "api", "jobs", "v1", "v0", "apply", "job_board", "postings", "boards"}
)

def resolve_discovery_keywords(cli_keywords: list[str] | None, config: dict) -> list[str]:
    if cli_keywords is not None:
        return list(cli_keywords)
    return [str(keyword) for keyword in config.get("search", {}).get("keywords", []) if str(keyword).strip()]


@dataclass(frozen=True)
class DiscoveryResult:
    provider: str
    ats: str
    keyword: str
    tokens: list[str]
    status: Literal["ok", "ok_empty", "failed"]
    http_status: int | None
    error_kind: (
        Literal[
            "timeout",
            "http_403",
            "http_4xx",
            "http_5xx",
            "network_error",
            "invalid_response",
        ]
        | None
    )
    error_message: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class VerificationResult:
    ats: str
    token: str
    relevant_count: int | None
    status: Literal["ok", "failed"]
    http_status: int | None
    error_kind: (
        Literal[
            "timeout",
            "http_403",
            "http_4xx",
            "http_5xx",
            "network_error",
            "invalid_response",
        ]
        | None
    )
    error_message: str | None


# ATS URL patterns used for extraction
ATS_PATTERNS: dict[str, dict] = {
    "greenhouse": {
        "domain": "boards.greenhouse.io",
        "search_domain": "site:boards.greenhouse.io",
        "url_regex": re.compile(r"boards\.greenhouse\.io/([A-Za-z0-9_\-]+)", re.IGNORECASE),
    },
    "lever": {
        "domain": "jobs.lever.co",
        "search_domain": "site:jobs.lever.co",
        "url_regex": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_\-]+)", re.IGNORECASE),
    },
    "ashby": {
        "domain": "jobs.ashbyhq.com",
        "search_domain": "site:jobs.ashbyhq.com",
        "url_regex": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_\-]+)", re.IGNORECASE),
    },
    "breezyhr": {
        "domain": "breezy.hr",
        "search_domain": "site:breezy.hr",
        "url_regex": re.compile(r"([A-Za-z0-9][A-Za-z0-9\-]*)\.breezy\.hr", re.IGNORECASE),
    },
}

# ---------------------------------------------------------------------------
# Helper functions (self-contained copies from fetch_jobs_catalog_adapters.py)
# ---------------------------------------------------------------------------


# Copied from fetch_jobs_catalog_adapters.py to keep this script self-contained.
def _strip_html(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _request_json(url: str, method: str = "GET", payload: Any = None) -> Any:
    if method == "POST":
        resp = requests.post(url, headers=HEADERS_JSON, json=payload, timeout=REQUEST_TIMEOUT)
    else:
        resp = requests.get(url, headers=HEADERS_JSON, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# Copied from fetch_jobs_catalog_adapters.py to keep this script self-contained.
def _canonical(
    *,
    title: str,
    company: str,
    location: str,
    job_url: str,
    description: str,
    source: str,
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
        "date_posted": None,
        "source": source,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "_query": None,
    }


# ---------------------------------------------------------------------------
# DuckDuckGo Lite search
# ---------------------------------------------------------------------------


def _extract_tokens_from_ddg_html(html: str, ats: str) -> set[str]:
    """Extract ATS tokens from DDG HTML response."""
    pattern_info = ATS_PATTERNS[ats]
    tokens: set[str] = set()

    url_regex = pattern_info["url_regex"]
    for match in url_regex.finditer(html):
        token = match.group(1).lower().rstrip("/")
        # Skip tokens that look like job IDs (all digits), are too short, or are known path segments
        if token and not token.isdigit() and len(token) >= 2 and token not in _SKIP_TOKENS:
            tokens.add(token)

    return tokens


def _classify_http_error(
    status_code: int | None,
) -> Literal["http_403", "http_4xx", "http_5xx", "network_error"]:
    if status_code == 403:
        return "http_403"
    if status_code is not None and 400 <= status_code < 500:
        return "http_4xx"
    if status_code is not None and 500 <= status_code < 600:
        return "http_5xx"
    return "network_error"


def _verification_failure_result(
    *,
    ats: str,
    token: str,
    http_status: int | None,
    error_kind: Literal["timeout", "http_403", "http_4xx", "http_5xx", "network_error", "invalid_response"],
    error_message: str | None,
) -> VerificationResult:
    return VerificationResult(
        ats=ats,
        token=token,
        relevant_count=None,
        status="failed",
        http_status=http_status,
        error_kind=error_kind,
        error_message=error_message,
    )


def _verification_success_result(*, ats: str, token: str, relevant_count: int) -> VerificationResult:
    return VerificationResult(
        ats=ats,
        token=token,
        relevant_count=relevant_count,
        status="ok",
        http_status=200,
        error_kind=None,
        error_message=None,
    )


def _normalize_discovered_token(token: Any) -> str:
    return str(token or "").strip().lower()


def _normalize_discovered_tokens(tokens: Any) -> list[str]:
    normalized = {cleaned for token in tokens or [] if (cleaned := _normalize_discovered_token(token))}
    return sorted(normalized)


def search_ddg_lite(ats: str, keyword: str) -> DiscoveryResult:
    """Query DDG Lite for a keyword + ATS domain and return a structured result."""
    pattern_info = ATS_PATTERNS[ats]
    query = f'{pattern_info["search_domain"]} "{keyword}"'
    started = time.monotonic()

    try:
        resp = requests.post(
            "https://lite.duckduckgo.com/lite/",
            headers=HEADERS_BROWSER,
            data={"q": query},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        try:
            tokens = _extract_tokens_from_ddg_html(resp.text, ats)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return DiscoveryResult(
                provider="ddg_lite",
                ats=ats,
                keyword=keyword,
                tokens=[],
                status="failed",
                http_status=getattr(resp, "status_code", None),
                error_kind="invalid_response",
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return DiscoveryResult(
            provider="ddg_lite",
            ats=ats,
            keyword=keyword,
            tokens=sorted(tokens),
            status="ok" if tokens else "ok_empty",
            http_status=getattr(resp, "status_code", None),
            error_kind=None,
            error_message=None,
            elapsed_ms=elapsed_ms,
        )
    except requests.Timeout as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return DiscoveryResult(
            provider="ddg_lite",
            ats=ats,
            keyword=keyword,
            tokens=[],
            status="failed",
            http_status=None,
            error_kind="timeout",
            error_message=str(exc),
            elapsed_ms=elapsed_ms,
        )
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return DiscoveryResult(
            provider="ddg_lite",
            ats=ats,
            keyword=keyword,
            tokens=[],
            status="failed",
            http_status=status_code,
            error_kind=_classify_http_error(status_code),
            error_message=str(exc),
            elapsed_ms=elapsed_ms,
        )
    except requests.RequestException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return DiscoveryResult(
            provider="ddg_lite",
            ats=ats,
            keyword=keyword,
            tokens=[],
            status="failed",
            http_status=None,
            error_kind="network_error",
            error_message=str(exc),
            elapsed_ms=elapsed_ms,
        )


def _discover_tokens_for_ats(
    ats: str,
    keywords: list[str],
) -> tuple[list[str], int, int, bool]:
    """Return normalized tokens plus run-level discovery state for one ATS.

    Searches DDG Lite for each keyword in sequence. The circuit breaker opens
    after 3 consecutive failures, stopping further searches for this ATS.
    Tokens discovered before the breaker opens are always returned — a run can
    be partially successful even when the breaker fires.
    """
    all_tokens: set[str] = set()
    success_count = 0
    failure_count = 0
    consecutive_failures = 0
    outage = False

    print(f"  Searching DDG Lite for {ats} ({len(keywords)} keywords)...", flush=True)

    for i, kw in enumerate(keywords):
        if i > 0:
            time.sleep(DDG_DELAY)

        result = search_ddg_lite(ats, kw)
        if result.status == "failed":
            failure_count += 1
            consecutive_failures += 1
            print(
                f"  stage=discovery ats={result.ats} keyword={result.keyword!r} "
                f"provider={result.provider} status={result.status} "
                f"error_kind={result.error_kind} elapsed_ms={result.elapsed_ms}",
                flush=True,
            )
            print(
                f"  [warn] DDG search failed for {ats}/{kw!r}: {result.error_kind}: {result.error_message}",
                flush=True,
            )
            if consecutive_failures >= 3:
                print(
                    f"  [warn] DDG circuit breaker opened for {ats} after {consecutive_failures} consecutive failures",
                    flush=True,
                )
                break
            continue

        consecutive_failures = 0
        success_count += 1
        new_tokens = set(result.tokens)
        added = new_tokens - all_tokens
        all_tokens |= new_tokens
        if added:
            print(f"    [{ats}] '{kw}' → +{len(added)} tokens", flush=True)

    if failure_count > 0 and success_count == 0:
        outage = True
    if failure_count > 0 and success_count > 0:
        print(
            f"  [warn] DDG discovery degraded for {ats}: {success_count} successful searches, {failure_count} failures",
            flush=True,
        )
    elif outage:
        print(
            f"  [warn] DDG discovery outage for {ats}: no successful search results",
            flush=True,
        )

    return _normalize_discovered_tokens(all_tokens), success_count, failure_count, outage


# ---------------------------------------------------------------------------
# Existing catalog loading
# ---------------------------------------------------------------------------


def _load_catalog() -> dict:
    if not _SOURCES_CATALOG.exists():
        return {"greenhouse": [], "lever": [], "ashby": []}
    with open(_SOURCES_CATALOG) as f:
        return json.load(f)


def _existing_tokens(catalog: dict, ats: str) -> set[str]:
    """Return lowercased set of board_token / organization values already in catalog."""
    entries = catalog.get(ats, [])
    key = "organization" if ats == "ashby" else "board_token"
    return {str(e.get(key, "")).lower() for e in entries if e.get(key)}


# ---------------------------------------------------------------------------
# Verification: fetch jobs per ATS and count SC-relevant ones
# ---------------------------------------------------------------------------


def _count_relevant_jobs_greenhouse(token: str, domain_rules: dict) -> VerificationResult:
    """Fetch all jobs for a Greenhouse board, return structured verification data."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    try:
        data = _request_json(url)
    except requests.Timeout as exc:
        return _verification_failure_result(
            ats="greenhouse",
            token=token,
            http_status=None,
            error_kind="timeout",
            error_message=str(exc),
        )
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return _verification_failure_result(
            ats="greenhouse",
            token=token,
            http_status=status_code,
            error_kind=_classify_http_error(status_code),
            error_message=str(exc),
        )
    except requests.RequestException as exc:
        return _verification_failure_result(
            ats="greenhouse",
            token=token,
            http_status=None,
            error_kind="network_error",
            error_message=str(exc),
        )

    if not isinstance(data, dict):
        return _verification_failure_result(
            ats="greenhouse",
            token=token,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected object response",
        )

    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        return _verification_failure_result(
            ats="greenhouse",
            token=token,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected jobs list",
        )

    count = 0
    for j in jobs:
        if not isinstance(j, dict):
            return _verification_failure_result(
                ats="greenhouse",
                token=token,
                http_status=200,
                error_kind="invalid_response",
                error_message="expected job object",
            )
        job = _canonical(
            title=j.get("title", ""),
            company=token,
            location=(j.get("location") or {}).get("name", ""),
            job_url=j.get("absolute_url", ""),
            description=_strip_html(j.get("content", "")),
            source="greenhouse",
        )
        if job is None:
            continue
        keep, _ = is_domain_relevant(job, domain_rules)
        if keep:
            count += 1
    return _verification_success_result(ats="greenhouse", token=token, relevant_count=count)


def _verification_context_greenhouse(token: str) -> dict[str, str]:
    return {
        "endpoint": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
        "provider": "greenhouse",
    }


def _count_relevant_jobs_lever(slug: str, domain_rules: dict) -> VerificationResult:
    """Fetch all postings for a Lever board, return structured verification data."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        data = _request_json(url)
    except requests.Timeout as exc:
        return _verification_failure_result(
            ats="lever",
            token=slug,
            http_status=None,
            error_kind="timeout",
            error_message=str(exc),
        )
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return _verification_failure_result(
            ats="lever",
            token=slug,
            http_status=status_code,
            error_kind=_classify_http_error(status_code),
            error_message=str(exc),
        )
    except requests.RequestException as exc:
        return _verification_failure_result(
            ats="lever",
            token=slug,
            http_status=None,
            error_kind="network_error",
            error_message=str(exc),
        )

    if not isinstance(data, list):
        return _verification_failure_result(
            ats="lever",
            token=slug,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected list response",
        )

    count = 0
    for j in data:
        if not isinstance(j, dict):
            return _verification_failure_result(
                ats="lever",
                token=slug,
                http_status=200,
                error_kind="invalid_response",
                error_message="expected posting object",
            )
        categories = j.get("categories") or {}
        location = categories.get("location", "")
        job = _canonical(
            title=j.get("text", ""),
            company=slug,
            location=location,
            job_url=j.get("hostedUrl", ""),
            description=_strip_html(j.get("descriptionPlain", "")),
            source="lever",
        )
        if job is None:
            continue
        keep, _ = is_domain_relevant(job, domain_rules)
        if keep:
            count += 1
    return _verification_success_result(ats="lever", token=slug, relevant_count=count)


def _verification_context_lever(token: str) -> dict[str, str]:
    return {
        "endpoint": f"https://api.lever.co/v0/postings/{token}?mode=json",
        "provider": "lever",
    }


def _count_relevant_jobs_ashby(org: str, domain_rules: dict) -> VerificationResult:
    """Fetch all job postings for an Ashby org, return structured verification data."""
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=apiJobBoardWithTeams"
    payload = {
        "operationName": "apiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": org},
        "query": (
            "query apiJobBoardWithTeams($organizationHostedJobsPageName: String!) { "
            "jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { "
            "jobPostings { id title locationName workplaceType } } }"
        ),
    }
    try:
        data = _request_json(url, method="POST", payload=payload)
    except requests.Timeout as exc:
        return _verification_failure_result(
            ats="ashby",
            token=org,
            http_status=None,
            error_kind="timeout",
            error_message=str(exc),
        )
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return _verification_failure_result(
            ats="ashby",
            token=org,
            http_status=status_code,
            error_kind=_classify_http_error(status_code),
            error_message=str(exc),
        )
    except requests.RequestException as exc:
        return _verification_failure_result(
            ats="ashby",
            token=org,
            http_status=None,
            error_kind="network_error",
            error_message=str(exc),
        )

    if not isinstance(data, dict):
        return _verification_failure_result(
            ats="ashby",
            token=org,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected object response",
        )

    board = (data.get("data") or {}).get("jobBoardWithTeams") or {}
    if not isinstance(board, dict):
        return _verification_failure_result(
            ats="ashby",
            token=org,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected jobBoardWithTeams object",
        )
    postings = board.get("jobPostings") or []
    if not isinstance(postings, list):
        return _verification_failure_result(
            ats="ashby",
            token=org,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected jobPostings list",
        )

    count = 0
    for j in postings:
        if not isinstance(j, dict):
            return _verification_failure_result(
                ats="ashby",
                token=org,
                http_status=200,
                error_kind="invalid_response",
                error_message="expected job posting object",
            )
        job_id = j.get("id", "")
        job = _canonical(
            title=j.get("title", ""),
            company=org,
            location=j.get("locationName", ""),
            job_url=f"https://jobs.ashbyhq.com/{org}/{job_id}",
            description="",
            source="ashby",
        )
        if job is None:
            continue
        keep, _ = is_domain_relevant(job, domain_rules)
        if keep:
            count += 1
    return _verification_success_result(ats="ashby", token=org, relevant_count=count)


def _verification_context_ashby(token: str) -> dict[str, str]:
    return {
        "endpoint": "https://jobs.ashbyhq.com/api/non-user-graphql?op=apiJobBoardWithTeams",
        "provider": "ashby",
    }


def _count_relevant_jobs_breezyhr(subdomain: str, domain_rules: dict) -> VerificationResult:
    """Fetch all positions for a Breezy HR board, return structured verification data."""
    url = f"https://{subdomain}.breezy.hr/positions"
    try:
        data = _request_json(url)
    except requests.Timeout as exc:
        return _verification_failure_result(
            ats="breezyhr",
            token=subdomain,
            http_status=None,
            error_kind="timeout",
            error_message=str(exc),
        )
    except requests.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return _verification_failure_result(
            ats="breezyhr",
            token=subdomain,
            http_status=status_code,
            error_kind=_classify_http_error(status_code),
            error_message=str(exc),
        )
    except requests.RequestException as exc:
        return _verification_failure_result(
            ats="breezyhr",
            token=subdomain,
            http_status=None,
            error_kind="network_error",
            error_message=str(exc),
        )

    if not isinstance(data, list):
        return _verification_failure_result(
            ats="breezyhr",
            token=subdomain,
            http_status=200,
            error_kind="invalid_response",
            error_message="expected list response",
        )

    count = 0
    for j in data:
        if not isinstance(j, dict):
            return _verification_failure_result(
                ats="breezyhr",
                token=subdomain,
                http_status=200,
                error_kind="invalid_response",
                error_message="expected position object",
            )
        job = _canonical(
            title=j.get("name", ""),
            company=subdomain,
            location=(j.get("location") or {}).get("name", ""),
            job_url=j.get("url", ""),
            description=_strip_html(j.get("description", "")),
            source="breezyhr",
        )
        if job is None:
            continue
        keep, _ = is_domain_relevant(job, domain_rules)
        if keep:
            count += 1
    return _verification_success_result(ats="breezyhr", token=subdomain, relevant_count=count)


def _verification_context_breezyhr(token: str) -> dict[str, str]:
    return {
        "endpoint": f"https://{token}.breezy.hr/positions",
        "provider": "breezyhr",
    }


def _verification_context_for_ats(ats: str, token: str) -> dict[str, str]:
    context_fns: dict[str, Callable[[str], dict[str, str]]] = {
        "greenhouse": _verification_context_greenhouse,
        "lever": _verification_context_lever,
        "ashby": _verification_context_ashby,
        "breezyhr": _verification_context_breezyhr,
    }
    return context_fns[ats](token)


def _verify_fn_for_ats(ats: str, domain_rules: dict) -> Callable[[str], VerificationResult]:
    verify_fns: dict[str, Callable[[str], VerificationResult]] = {
        "greenhouse": lambda token: _count_relevant_jobs_greenhouse(token, domain_rules),
        "lever": lambda token: _count_relevant_jobs_lever(token, domain_rules),
        "ashby": lambda token: _count_relevant_jobs_ashby(token, domain_rules),
        "breezyhr": lambda token: _count_relevant_jobs_breezyhr(token, domain_rules),
    }
    return verify_fns[ats]


def _coerce_verification_result(ats: str, token: str, result: Any) -> VerificationResult:
    if isinstance(result, VerificationResult):
        return result
    return VerificationResult(
        ats=ats,
        token=token,
        relevant_count=None,
        status="failed",
        http_status=None,
        error_kind="invalid_response",
        error_message="unexpected verification result type",
    )


# ---------------------------------------------------------------------------
# Discovery pipeline
# ---------------------------------------------------------------------------


def run_discovery(
    ats_list: list[str],
    min_relevant_jobs: int,
    keywords: list[str],
    output_path: Path,
    domain_rules: dict,
    display_name: str,
) -> int:
    catalog = _load_catalog()

    summary = {
        "discovered": 0,
        "passed_domain_filter": 0,
        "already_tracked": 0,
        "discovery_failures": 0,
        "verification_failures": 0,
        "failed_verification": 0,
    }
    # Always initialize all ATS keys so output always contains every platform
    candidates: dict[str, list[dict]] = {"greenhouse": [], "lever": [], "ashby": [], "breezyhr": []}

    any_discovery_outage = False

    for ats in ats_list:
        print(f"\n[{ats}] Starting discovery...", flush=True)
        existing = _existing_tokens(catalog, ats)

        # Step 1: DDG search
        raw_tokens, success_count, failure_count, outage = _discover_tokens_for_ats(ats, keywords)
        any_discovery_outage = any_discovery_outage or outage
        summary["discovery_failures"] += failure_count
        print(f"  [{ats}] Found {len(raw_tokens)} unique tokens from DDG", flush=True)

        # Step 2: Filter out already-tracked
        new_tokens = [t for t in sorted(raw_tokens) if t not in existing]
        already_tracked = len(raw_tokens) - len(new_tokens)
        summary["already_tracked"] += already_tracked
        summary["discovered"] += len(new_tokens)
        print(
            f"  [{ats}] {len(new_tokens)} new (skipped {already_tracked} already tracked)",
            flush=True,
        )
        if success_count == 0 and failure_count == 0:
            print(
                f"  [warn] DDG discovery returned no results or failures for {ats}",
                flush=True,
            )

        # Step 3: Cap at MAX_PER_ATS
        if len(new_tokens) > MAX_PER_ATS:
            print(
                f"  [{ats}] Capping at {MAX_PER_ATS} tokens for verification",
                flush=True,
            )
            new_tokens = new_tokens[:MAX_PER_ATS]

        # Step 4: Verify each token
        verify_fn = _verify_fn_for_ats(ats, domain_rules)
        org_key = "organization" if ats == "ashby" else "board_token"

        for token in new_tokens:
            verify_context = _verification_context_for_ats(ats, token)
            verify_started = time.monotonic()
            print(f"    [{ats}] Verifying {token!r}...", flush=True, end=" ")
            verification = _coerce_verification_result(ats, token, verify_fn(token))
            verify_elapsed_ms = int((time.monotonic() - verify_started) * 1000)
            print(
                f"stage=verification ats={verification.ats} token={verification.token} "
                f"provider={verify_context['provider']} endpoint={verify_context['endpoint']} "
                f"status={verification.status} error_kind={verification.error_kind} "
                f"elapsed_ms={verify_elapsed_ms}",
                flush=True,
            )

            if verification.status == "failed":
                summary["verification_failures"] += 1
                summary["failed_verification"] += 1
                print("FAIL (request error)", flush=True)
                continue

            relevant_count = verification.relevant_count or 0
            print(f"{relevant_count} {display_name} jobs", flush=True)

            if relevant_count >= min_relevant_jobs:
                entry: dict = {
                    "name": token,
                    org_key: token,
                    "relevant_jobs": relevant_count,
                    "enabled": True,
                }
                candidates[ats].append(entry)
                summary["passed_domain_filter"] += 1
            # else: silently drop (doesn't meet threshold)

        # Step 5: Sort by relevant job count descending
        candidates[ats].sort(key=lambda x: x["relevant_jobs"], reverse=True)
        print(
            f"  [{ats}] {len(candidates[ats])} companies passed (≥{min_relevant_jobs} relevant jobs)",
            flush=True,
        )

    # Build output
    today = date.today().isoformat()
    output = {
        "generated": today,
        "summary": summary,
        "candidates": candidates,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {output_path}", flush=True)
    print(
        f"Summary: {summary['discovered']} discovered, "
        f"{summary['passed_domain_filter']} passed {display_name} filter, "
        f"{summary['already_tracked']} already tracked, "
        f"{summary['failed_verification']} failed verification",
        flush=True,
    )
    return 1 if any_discovery_outage else 0


# ---------------------------------------------------------------------------
# --apply mode
# ---------------------------------------------------------------------------


def _validate_candidates_file(data: Any, path: Path) -> None:
    """Exit with an error message if the candidates file has an invalid schema."""
    if not isinstance(data, dict):
        print(f"ERROR: {path}: expected a JSON object at top level", flush=True)
        sys.exit(1)
    if "candidates" not in data:
        print(f"ERROR: {path}: missing required key 'candidates'", flush=True)
        sys.exit(1)
    candidates = data["candidates"]
    if not isinstance(candidates, dict):
        print(f"ERROR: {path}: 'candidates' must be a JSON object", flush=True)
        sys.exit(1)
    for ats, entries in candidates.items():
        if not isinstance(entries, list):
            print(f"ERROR: {path}: candidates[{ats!r}] must be a list", flush=True)
            sys.exit(1)
        org_key = "organization" if ats == "ashby" else "board_token"
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                print(
                    f"ERROR: {path}: candidates[{ats!r}][{i}] must be an object",
                    flush=True,
                )
                sys.exit(1)
            if not entry.get(org_key):
                print(
                    f"ERROR: {path}: candidates[{ats!r}][{i}] missing required field {org_key!r}",
                    flush=True,
                )
                sys.exit(1)


def run_apply(candidates_path: Path) -> None:
    if not candidates_path.exists():
        print(f"ERROR: File not found: {candidates_path}", flush=True)
        sys.exit(1)

    with open(candidates_path) as f:
        candidates_data = json.load(f)

    _validate_candidates_file(candidates_data, candidates_path)
    candidates = candidates_data.get("candidates", {})
    catalog = _load_catalog()

    added_counts: dict[str, int] = {}

    for ats, entries in candidates.items():
        if not entries:
            added_counts[ats] = 0
            continue

        existing = _existing_tokens(catalog, ats)
        org_key = "organization" if ats == "ashby" else "board_token"

        new_entries = []
        for entry in entries:
            token = str(entry.get(org_key, "")).lower()
            if not token or token in existing:
                continue
            # Build clean catalog entry (strip relevant_jobs — not part of catalog schema)
            catalog_entry: dict = {
                "name": entry.get("name", token),
                org_key: entry.get(org_key, token),
                "enabled": entry.get("enabled", True),
            }
            new_entries.append(catalog_entry)

        if ats not in catalog:
            catalog[ats] = []
        catalog[ats].extend(new_entries)
        added_counts[ats] = len(new_entries)

    fd, tmp_str = tempfile.mkstemp(dir=_SOURCES_CATALOG.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(catalog, f, indent=2)
        os.replace(tmp_str, _SOURCES_CATALOG)
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise

    parts = [f"{count} to {ats}" for ats, count in added_counts.items()]
    print(f"Applied: added {', '.join(parts)}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discover_companies.py",
        description=(
            "Discover companies hiring for the active domain on Greenhouse, Lever, Ashby, and Breezy HR "
            "via DuckDuckGo Lite search, verify relevant job counts, and output candidates JSON."
        ),
    )

    parser.add_argument(
        "--apply",
        metavar="CANDIDATES_JSON",
        help="Path to candidates JSON file to merge into sources_catalog.json.",
    )

    parser.add_argument(
        "--ats",
        nargs="+",
        choices=["greenhouse", "lever", "ashby", "breezyhr"],
        default=["greenhouse", "lever", "ashby", "breezyhr"],
        help="ATS platforms to search (default: all four).",
    )
    parser.add_argument(
        "--min-relevant-jobs",
        type=int,
        default=3,
        metavar="N",
        help="Minimum active-domain-relevant jobs to include a company (default: 3).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help=("Output file path (default: config/discovery/candidates_YYYY-MM-DD.json)."),
    )
    parser.add_argument(
        "--keywords",
        nargs="+",
        metavar="KW",
        help="Override discovery keywords (default: curated internal list).",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.apply:
        # Fix C: warn if discovery-mode args are also provided alongside --apply
        discovery_args_set = (
            args.keywords is not None
            or set(args.ats) != {"greenhouse", "lever", "ashby", "breezyhr"}
            or args.min_relevant_jobs != 3
            or args.output is not None
        )
        if discovery_args_set:
            print(
                "ERROR: --apply cannot be combined with --ats, --keywords, --min-relevant-jobs, or --output.",
                flush=True,
            )
            sys.exit(1)
        run_apply(Path(args.apply))
        return

    # Discovery mode
    try:
        config = load_config()
    except ConfigLoadError as exc:
        print(f"ERROR: {exc}", flush=True)
        sys.exit(1)
    keywords = resolve_discovery_keywords(args.keywords, config)
    domain_rules = config.get("domain_filter", {})
    display_name = str(config.get("identity", {}).get("display_name", "Job Search"))
    ats_list = list(dict.fromkeys(args.ats))  # deduplicate, preserve order

    if args.output:
        output_path = Path(args.output)
    else:
        today = date.today().isoformat()
        output_path = _DISCOVERY_DIR / f"candidates_{today}.json"

    print(
        f"Starting discovery: ats={ats_list}, min_relevant_jobs={args.min_relevant_jobs}, keywords={len(keywords)}",
        flush=True,
    )

    exit_code = run_discovery(
        ats_list=ats_list,
        min_relevant_jobs=args.min_relevant_jobs,
        keywords=keywords,
        output_path=output_path,
        domain_rules=domain_rules,
        display_name=display_name,
    )
    if exit_code is not None:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
