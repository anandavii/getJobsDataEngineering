"""
Role taxonomy, query generation, and stack-weighted scoring helpers.
"""

from __future__ import annotations

import re
from datetime import date, datetime, UTC
from typing import Any


DEFAULT_ROLE_TAXONOMY: dict[str, Any] = {
    "families": [],
    "title_variants": {},
    "skill_variants": {},
    "seniority_tokens": {},
}

DEFAULT_RANKING: dict[str, Any] = {
    "enabled": True,
    "weights": {
        "title_family_match": 0.35,
        "skill_stack_match": 0.35,
        "seniority_alignment": 0.10,
        "recency": 0.15,
        "source_reliability": 0.05,
    },
    "min_fit_score": 0.45,
}


def _normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def _contains_phrase(text: str, phrase: str) -> bool:
    text_n = _normalize_text(text)
    phrase_n = _normalize_text(phrase)
    if not text_n or not phrase_n:
        return False
    tokens = re.findall(r"[a-z0-9]+", phrase_n)
    if not tokens:
        return False
    pattern = r"\b" + r"[\W_]+".join(re.escape(t) for t in tokens) + r"\b"
    return re.search(pattern, text_n) is not None


def _unique(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def get_role_taxonomy(config: dict) -> dict:
    search = config.get("search", {}) if isinstance(config, dict) else {}
    user_tax = search.get("role_taxonomy") if isinstance(search, dict) else None
    merged = {
        "families": list(DEFAULT_ROLE_TAXONOMY["families"]),
        "title_variants": {k: list(v) for k, v in DEFAULT_ROLE_TAXONOMY["title_variants"].items()},
        "skill_variants": {k: list(v) for k, v in DEFAULT_ROLE_TAXONOMY["skill_variants"].items()},
        "seniority_tokens": {k: list(v) for k, v in DEFAULT_ROLE_TAXONOMY["seniority_tokens"].items()},
    }
    if not isinstance(user_tax, dict):
        return merged

    for k in ("families",):
        if isinstance(user_tax.get(k), list):
            merged[k] = list(user_tax[k])

    for key in ("title_variants", "skill_variants", "seniority_tokens"):
        val = user_tax.get(key)
        if not isinstance(val, dict):
            continue
        for sub_key, sub_val in val.items():
            if isinstance(sub_val, list):
                merged[key][sub_key] = [str(x) for x in sub_val]

    return merged


def _ranking_config(config: dict) -> dict:
    ranking = config.get("ranking") if isinstance(config, dict) else None
    merged = {
        "enabled": DEFAULT_RANKING["enabled"],
        "weights": dict(DEFAULT_RANKING["weights"]),
        "min_fit_score": DEFAULT_RANKING["min_fit_score"],
    }
    if not isinstance(ranking, dict):
        return merged

    merged["enabled"] = bool(ranking.get("enabled", merged["enabled"]))
    if isinstance(ranking.get("weights"), dict):
        for k, v in ranking["weights"].items():
            try:
                merged["weights"][k] = float(v)
            except (TypeError, ValueError):
                pass
    try:
        merged["min_fit_score"] = float(ranking.get("min_fit_score", merged["min_fit_score"]))
    except (TypeError, ValueError):
        pass
    return merged


def generate_queries(config: dict) -> list[str]:
    search = config.get("search", {}) if isinstance(config, dict) else {}
    base_keywords = [str(k).strip() for k in search.get("keywords", []) if str(k).strip()]

    query_cfg = search.get("query_generation", {}) if isinstance(search.get("query_generation"), dict) else {}
    if not bool(query_cfg.get("enabled", False)):
        return _unique(base_keywords)

    max_queries = query_cfg.get("max_queries_per_run", 60)
    try:
        max_queries = int(max_queries)
    except (TypeError, ValueError):
        max_queries = 60
    if max_queries < 1:
        max_queries = 1

    taxonomy = get_role_taxonomy(config)
    title_variants = taxonomy.get("title_variants", {})
    skill_variants = taxonomy.get("skill_variants", {})

    titles: list[str] = []
    for family in taxonomy.get("families", []):
        titles.extend([str(t) for t in title_variants.get(family, [])])
    titles = _unique(base_keywords + titles)

    primary = _unique([str(s) for s in skill_variants.get("primary_stack", [])])
    related = _unique([str(s) for s in skill_variants.get("related_stack", [])])
    genai = _unique([str(s) for s in skill_variants.get("genai_stack", [])])

    out: list[str] = []

    def add(q: str) -> None:
        if len(out) >= max_queries:
            return
        if q and q.strip():
            out.append(q.strip())

    for q in base_keywords:
        add(q)

    for title in titles:
        add(title)

    for title in titles:
        for skill in primary[:4]:
            add(f"{title} {skill}")
            if len(out) >= max_queries:
                break
        if len(out) >= max_queries:
            break

    for title in titles:
        for skill in related[:3]:
            add(f"{title} {skill}")
            if len(out) >= max_queries:
                break
        if len(out) >= max_queries:
            break

    genai_titles = [t for t in titles if _contains_phrase(t, "ai") or _contains_phrase(t, "genai") or _contains_phrase(t, "llm")]
    for title in genai_titles[:8]:
        for skill in genai[:3]:
            add(f"{title} {skill}")
            if len(out) >= max_queries:
                break
        if len(out) >= max_queries:
            break

    if bool(query_cfg.get("dedup_queries", True)):
        out = _unique(out)

    return out[:max_queries]


def with_generated_queries(config: dict, queries: list[str]) -> dict:
    cloned = dict(config)
    search = dict(cloned.get("search", {}))
    search["keywords"] = queries
    cloned["search"] = search
    return cloned


def infer_seniority(job: dict, config: dict) -> str:
    taxonomy = get_role_taxonomy(config)
    tokens = taxonomy.get("seniority_tokens", {})
    text = f"{job.get('job_title', '')} {job.get('description_snippet', '')}".lower()
    for level in ("staff", "senior", "entry", "mid"):
        for tok in tokens.get(level, []):
            if _contains_phrase(text, str(tok)):
                return level
    return "mid"


def classify_role_families(job: dict, config: dict) -> tuple[list[str], list[str], list[str]]:
    taxonomy = get_role_taxonomy(config)
    families = taxonomy.get("families", [])
    title_variants = taxonomy.get("title_variants", {})
    skill_variants = taxonomy.get("skill_variants", {})

    title = str(job.get("job_title") or "")
    desc = str(job.get("description_snippet") or "")
    combined = f"{title}\n{desc}"

    matched_families: list[str] = []
    matched_titles: list[str] = []
    for fam in families:
        fam_titles = [str(t) for t in title_variants.get(fam, [])]
        fam_hit = False
        for phrase in fam_titles:
            if _contains_phrase(combined, phrase):
                fam_hit = True
                matched_titles.append(phrase)
        if fam_hit:
            matched_families.append(fam)

    matched_skills: list[str] = []
    for skill_list in skill_variants.values():
        for skill in skill_list:
            if _contains_phrase(combined, str(skill)):
                matched_skills.append(str(skill))

    return _unique(matched_families), _unique(matched_titles), _unique(matched_skills)


def _recency_score(date_posted: str | None) -> float:
    if not date_posted:
        return 0.2
    try:
        posted = date.fromisoformat(str(date_posted)[:10])
    except ValueError:
        return 0.2
    today = datetime.now(tz=UTC).date()
    delta = (today - posted).days
    if delta <= 0:
        return 1.0
    if delta == 1:
        return 0.8
    if delta <= 7:
        return 0.6
    if delta <= 30:
        return 0.4
    return 0.2


def _source_reliability(source: str, source_health: dict) -> float:
    state = source_health.get("state", {}) if isinstance(source_health, dict) else {}
    row = state.get(source, {}) if isinstance(state, dict) else {}
    runs = int(row.get("runs", 0) or 0)
    successes = int(row.get("successes", 0) or 0)
    empties = int(row.get("empties", 0) or 0)
    if runs <= 0:
        return 0.5
    success_rate = successes / runs
    empty_penalty = 0.5 * (empties / runs)
    return max(0.05, min(1.0, success_rate - empty_penalty))


def annotate_and_score_jobs(jobs: list[dict], config: dict, source_health: dict | None = None) -> list[dict]:
    source_health = source_health or {}
    ranking = _ranking_config(config)
    weights = ranking.get("weights", {})
    weight_sum = sum(float(v) for v in weights.values()) or 1.0

    seniority_policy = (config.get("search", {}) or {}).get("seniority_policy", {})
    include_levels = seniority_policy.get("include_levels", ["entry", "mid", "senior", "staff"])
    include_levels = {str(x).strip().lower() for x in include_levels}

    out: list[dict] = []
    for job in jobs:
        row = dict(job)

        families, matched_titles, matched_skills = classify_role_families(row, config)
        seniority = infer_seniority(row, config)

        title_family_score = 1.0 if families else 0.0
        # Cap at 1.0; 4+ stack hits means full skill score.
        skill_score = min(1.0, len(matched_skills) / 4.0)
        seniority_score = 1.0 if seniority in include_levels else 0.0
        recency = _recency_score(row.get("date_posted"))
        source_rel = _source_reliability(str(row.get("source") or "unknown"), source_health)

        raw_score = (
            float(weights.get("title_family_match", 0.35)) * title_family_score
            + float(weights.get("skill_stack_match", 0.35)) * skill_score
            + float(weights.get("seniority_alignment", 0.10)) * seniority_score
            + float(weights.get("recency", 0.15)) * recency
            + float(weights.get("source_reliability", 0.05)) * source_rel
        ) / weight_sum

        row["role_family"] = families[0] if families else "unknown"
        row["seniority_guess"] = seniority
        row["matched_titles"] = matched_titles
        row["matched_skills"] = matched_skills
        row["fit_score"] = round(raw_score, 4)
        row["fetched_at_utc"] = datetime.now(tz=UTC).isoformat()
        out.append(row)

    return out


def filter_by_fit_score(scored_jobs: list[dict], config: dict) -> tuple[list[dict], list[dict]]:
    ranking = _ranking_config(config)
    if not ranking.get("enabled", True):
        return scored_jobs, []

    min_fit = float(ranking.get("min_fit_score", 0.45))
    kept: list[dict] = []
    dropped: list[dict] = []
    for job in scored_jobs:
        if float(job.get("fit_score", 0.0) or 0.0) >= min_fit:
            kept.append(job)
        else:
            row = dict(job)
            row["_fit_filter_reason"] = f"fit_score<{min_fit}"
            dropped.append(row)
    return kept, dropped


def build_query_performance(all_jobs: list[dict], qualified_jobs: list[dict]) -> list[dict]:
    fetched_counts: dict[str, int] = {}
    qualified_counts: dict[str, int] = {}

    for job in all_jobs:
        q = str(job.get("_query") or "unknown")
        fetched_counts[q] = fetched_counts.get(q, 0) + 1

    for job in qualified_jobs:
        q = str(job.get("_query") or "unknown")
        qualified_counts[q] = qualified_counts.get(q, 0) + 1

    rows: list[dict] = []
    for query, fetched in sorted(fetched_counts.items(), key=lambda item: (-item[1], item[0])):
        qualified = qualified_counts.get(query, 0)
        ratio = (qualified / fetched) if fetched else 0.0
        rows.append(
            {
                "query": query,
                "fetched": fetched,
                "qualified": qualified,
                "qualified_ratio": round(ratio, 4),
            }
        )

    return rows


def should_enable_next_tier(current_qualified_count: int, config: dict) -> bool:
    health = config.get("health", {}) if isinstance(config, dict) else {}
    threshold = health.get("tier_activation_threshold", 20)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = 20
    return current_qualified_count < threshold
