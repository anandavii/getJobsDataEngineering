#!/usr/bin/env python
"""
Test a specific job board and see what jobs it returns + active-domain filter results.

Usage:
    uv run python tools/test_job_board.py --source greenhouse --token oura
    uv run python tools/test_job_board.py --source ashby --org junipersquare
    uv run python tools/test_job_board.py --source lever --token netflix
    uv run python tools/test_job_board.py --source smartrecruiters --token SikaAG
    uv run python tools/test_job_board.py --source custom --company Sidel
    uv run python tools/test_job_board.py --source custom --company Wayfair
"""

import argparse
import logging
import sys

from fetch_jobs_catalog_adapters import (
    run_greenhouse,
    run_ashby,
    run_lever,
    run_workday,
    run_smartrecruiters,
)
import fetch_jobs_company_careers as fetch_custom
from domain_relevance import filter_domain_jobs
from config_loader import load_config as _load_config, ConfigLoadError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def load_config() -> dict:
    try:
        return _load_config()
    except ConfigLoadError as e:
        log.error(str(e))
        sys.exit(1)


def test_source(source: str, tokens: list[str] = None, orgs: list[str] = None, companies: list[str] = None):
    """Fetch jobs from one or more companies and show active-domain filter results."""
    config = load_config()
    keywords = config.get("search", {}).get("keywords", [])
    domain_rules = config.get("domain_filter", {})
    display_name = config.get("identity", {}).get("display_name", "Job Search")

    tokens = tokens or []
    orgs = orgs or []
    companies = companies or []

    # Fetch based on source type
    if source == "greenhouse":
        if not tokens:
            log.error("--token required for greenhouse (can specify multiple)")
            sys.exit(1)
        # Temporarily set only these companies in config
        config["sources_catalog"]["greenhouse"] = [
            {"name": t.title(), "board_token": t, "enabled": True} for t in tokens
        ]
        jobs = run_greenhouse(config, keywords=keywords)
        source_label = f"Greenhouse ({', '.join(tokens)})"

    elif source == "ashby":
        if not orgs:
            log.error("--org required for ashby (can specify multiple)")
            sys.exit(1)
        config["sources_catalog"]["ashby"] = [{"name": o.title(), "organization": o, "enabled": True} for o in orgs]
        jobs = run_ashby(config, keywords=keywords)
        source_label = f"Ashby ({', '.join(orgs)})"

    elif source == "lever":
        if not tokens:
            log.error("--token required for lever (can specify multiple)")
            sys.exit(1)
        config["sources_catalog"]["lever"] = [{"name": t.title(), "board_token": t, "enabled": True} for t in tokens]
        jobs = run_lever(config, keywords=keywords)
        source_label = f"Lever ({', '.join(tokens)})"

    elif source == "smartrecruiters":
        if not tokens:
            log.error("--token required for smartrecruiters (can specify multiple)")
            sys.exit(1)
        config.setdefault("sources_catalog", {})["smartrecruiters"] = [
            {"name": t, "company_identifier": t, "enabled": True} for t in tokens
        ]
        jobs = run_smartrecruiters(config, keywords=keywords)
        source_label = f"SmartRecruiters ({', '.join(tokens)})"

    elif source == "workday":
        if not tokens:
            log.error("--tenant required for workday (can specify multiple)")
            sys.exit(1)
        all_entries = config.get("sources_catalog", {}).get("workday", [])
        matched = [e for e in all_entries if e.get("tenant") in tokens]
        if not matched:
            log.error(f"No workday entries found in sources_catalog for tenant(s): {', '.join(tokens)}")
            sys.exit(1)
        config["sources_catalog"]["workday"] = [{**e, "enabled": True} for e in matched]
        jobs = run_workday(config, keywords=keywords)
        source_label = f"Workday ({', '.join(tokens)})"

    elif source == "custom":
        if not companies:
            log.error("--company required for custom (can specify multiple)")
            sys.exit(1)
        names = {c.lower() for c in companies}
        config["companies"] = [
            {**c, "enabled": True} for c in config.get("companies", []) if c["name"].lower() in names
        ]
        if not config["companies"]:
            log.error(f"No matching companies found in config for: {', '.join(companies)}")
            sys.exit(1)
        jobs = fetch_custom.run(config)
        source_label = f"Custom ({', '.join(companies)})"

    else:
        log.error(f"Unknown source: {source}. Supported: greenhouse, ashby, lever, smartrecruiters, workday, custom")
        sys.exit(1)

    # Apply active-domain filter
    domain_passed, domain_failed, metrics = filter_domain_jobs(jobs, domain_rules)

    # Display results
    print(f"\n{'=' * 70}")
    print(f"  {source_label} — Test Results")
    print(f"{'=' * 70}\n")

    print(f"Total jobs returned by source: {len(jobs)}")
    print(f"Passed {display_name} filter: {len(domain_passed)}")
    print(f"Failed {display_name} filter: {len(domain_failed)}\n")

    if domain_passed:
        print(f"✓ PASSED DOMAIN FILTER ({len(domain_passed)}):\n")
        for job in domain_passed:
            reason = job.get("_domain_filter_reason", "matched domain criteria")
            print(f"  [{job.get('source', '?')}] {job['job_title']}")
            print(f"    Company: {job.get('company', '?')}")
            print(f"    Location: {job.get('location', '?')}")
            print(f"    URL: {job['job_url']}")
            print(f"    Reason: {reason}\n")
    else:
        print("✓ PASSED DOMAIN FILTER: (none)\n")

    if domain_failed:
        print(f"✗ FAILED DOMAIN FILTER ({len(domain_failed)}):\n")
        for job in domain_failed:
            reason = job.get("_domain_filter_reason", "unknown")
            print(f"  [{job.get('source', '?')}] {job['job_title']}")
            print(f"    Company: {job.get('company', '?')}")
            print(f"    Reason: {reason}\n")

    print(f"{'=' * 70}\n")


test_source.__test__ = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test one or more job boards and see what jobs pass the active-domain filter")
    parser.add_argument(
        "--source",
        choices=["greenhouse", "ashby", "lever", "smartrecruiters", "workday", "custom"],
        required=True,
        help="Job board source",
    )
    parser.add_argument(
        "--token",
        action="append",
        help="Board token (for greenhouse, lever) — can specify multiple times",
    )
    parser.add_argument(
        "--org",
        action="append",
        help="Organization slug (for ashby) — can specify multiple times",
    )
    parser.add_argument(
        "--company",
        action="append",
        help="Company name as in config (for custom) — can specify multiple times",
    )
    parser.add_argument(
        "--tenant",
        action="append",
        help="Workday tenant slug as in sources_catalog (for workday) — can specify multiple times",
    )

    args = parser.parse_args()

    # --tenant feeds into tokens for workday
    tokens = (args.token or []) + (args.tenant or [] if args.source == "workday" else [])
    test_source(args.source, tokens=tokens, orgs=args.org or [], companies=args.company or [])
