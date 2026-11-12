"""Deterministic, configuration-driven relevance filtering for the active job domain."""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import Any


DEFAULT_DOMAIN_FILTER_RULES: dict[str, Any] = {
    "enabled": True,
    "mode": "title_or_description",
    "require_software_context_for_description_only": True,
    "include_patterns": [],
    "exclude_patterns": [],
    "software_context_patterns": [],
    "rejected_sample_limit": 200,
}


def _extract_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


@lru_cache(maxsize=512)
def _pattern_to_regex(pattern: str) -> re.Pattern[str] | None:
    if not pattern:
        return None
    normalized = pattern.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("re:"):
        try:
            return re.compile(normalized[3:], re.IGNORECASE)
        except re.error:
            return None

    tokens = _extract_tokens(normalized)
    if not tokens:
        return None
    joined = r"[\W_]+".join(re.escape(token) for token in tokens)
    return re.compile(rf"\b{joined}\b", re.IGNORECASE)


def _matches_any(text: str, patterns: list[str]) -> bool:
    return bool(text) and any(regex and regex.search(text) for pattern in patterns if (regex := _pattern_to_regex(pattern)))


def _merge_rules(rules: dict | None) -> dict[str, Any]:
    merged = dict(DEFAULT_DOMAIN_FILTER_RULES)
    if isinstance(rules, dict):
        merged.update(rules)

    for key in ("include_patterns", "exclude_patterns", "software_context_patterns"):
        if not isinstance(merged.get(key), list):
            merged[key] = list(DEFAULT_DOMAIN_FILTER_RULES[key])

    mode = str(merged.get("mode", "title_or_description")).strip().lower()
    merged["mode"] = mode if mode in {"title_only", "title_or_description"} else "title_or_description"
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["require_software_context_for_description_only"] = bool(
        merged.get("require_software_context_for_description_only", True)
    )
    return merged


def is_domain_relevant(job: dict, rules: dict | None = None) -> tuple[bool, str]:
    """Return whether a canonical job matches the configured domain and why."""
    effective = _merge_rules(rules)
    if not effective["enabled"]:
        return True, "domain_filter_disabled"

    title = str(job.get("job_title") or "")
    description = str(job.get("description_snippet") or "")
    combined = f"{title}\n{description}".strip()

    title_match = _matches_any(title, effective["include_patterns"])
    title_excluded = _matches_any(title, effective["exclude_patterns"])
    if title_match and not title_excluded:
        return True, "title_match"

    if _matches_any(combined, effective["exclude_patterns"]):
        return False, "excluded_out_of_domain"

    if title_match:
        return True, "title_match"
    if effective["mode"] == "title_only":
        return False, "no_title_match"

    if not _matches_any(description, effective["include_patterns"]):
        return False, "no_domain_match"
    if not effective["require_software_context_for_description_only"]:
        return True, "description_match"
    if _matches_any(combined, effective["software_context_patterns"]):
        return True, "description_match_with_context"
    return False, "description_match_missing_context"


def filter_domain_jobs(jobs: list[dict], rules: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """Split canonical jobs into domain-relevant and rejected groups with metrics."""
    kept: list[dict] = []
    rejected: list[dict] = []
    drop_reasons: Counter[str] = Counter()
    dropped_by_source: Counter[str] = Counter()

    for job in jobs:
        keep, reason = is_domain_relevant(job, rules)
        if keep:
            kept.append(job)
            continue
        rejected_job = dict(job)
        rejected_job["_domain_filter_reason"] = reason
        rejected.append(rejected_job)
        drop_reasons[reason] += 1
        dropped_by_source[str(job.get("source") or "unknown")] += 1

    return kept, rejected, {
        "total": len(jobs),
        "kept": len(kept),
        "dropped": len(rejected),
        "drop_reasons": dict(drop_reasons),
        "dropped_out_of_domain_by_source": dict(dropped_by_source),
    }
