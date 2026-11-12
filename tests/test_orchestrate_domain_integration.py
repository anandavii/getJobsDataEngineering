import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import orchestrate


class OrchestrateDomainIntegrationTests(unittest.TestCase):
    def test_pushes_only_domain_jobs_and_writes_generic_filter_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            config = {
                "active_domain": "data_engineering",
                "search": {
                    "keywords": ["Data Engineer", "Analytics Engineer"],
                    "location": "United States",
                    "date_posted": "today",
                    "calendar_day_filter": {
                        "mode": "last_days",
                        "days": 8,
                        "timezone": "America/New_York",
                        "drop_if_missing_date": False,
                    },
                    "query_generation": {"enabled": False},
                    "role_taxonomy": {
                        "families": ["data_platform", "warehouse_analytics"],
                        "title_variants": {
                            "data_platform": ["Data Engineer"],
                            "warehouse_analytics": ["Analytics Engineer"],
                        },
                        "skill_variants": {
                            "primary_stack": ["python", "sql", "dbt", "airflow"],
                            "related_stack": [],
                            "genai_stack": [],
                        },
                        "seniority_tokens": {
                            "entry": ["junior"],
                            "mid": ["engineer"],
                            "senior": ["senior"],
                            "staff": ["staff"],
                        },
                    },
                    "seniority_policy": {"include_levels": ["entry", "mid", "senior", "staff"]},
                },
                "domain_filter": {
                    "enabled": True,
                    "mode": "title_or_description",
                    "require_software_context_for_description_only": True,
                    "include_patterns": ["data engineer", "analytics engineer"],
                    "exclude_patterns": ["data entry", "data scientist"],
                    "software_context_patterns": ["python", "sql", "dbt", "airflow"],
                    "rejected_sample_limit": 50,
                },
                "ranking": {
                    "enabled": True,
                    "weights": {
                        "title_family_match": 0.35,
                        "skill_stack_match": 0.35,
                        "seniority_alignment": 0.10,
                        "recency": 0.15,
                        "source_reliability": 0.05,
                    },
                    "min_fit_score": 0.45,
                },
                "sources": {"jsearch": True},
                "source_tiers": {"tier1": ["jsearch"], "tier2": [], "tier3": []},
                "health": {
                    "max_consecutive_failures": 3,
                    "alert_consecutive_failures_threshold": 2,
                    "tier_activation_threshold": 20,
                    "state_file": str(tmp_root / ".tmp" / "source_health_state.json"),
                },
                "supabase": {
                    "matched_table": "matched_jobs",
                    "rejected_table": "rejected_jobs",
                    "cleanup_older_than_days": 90,
                    "batch_size": 50,
                },
                "notification": {"max_jobs_in_message": 10, "include_sources": True, "format": "rich"},
            }
            fetched_jobs = [
                {
                    "job_title": "Data Engineer",
                    "company": "Acme",
                    "location": "United States",
                    "job_type": "Remote",
                    "job_url": "https://example.com/data-1",
                    "description_snippet": "Build Python SQL dbt pipelines orchestrated by Airflow.",
                    "date_posted": "2026-11-10",
                    "source": "jsearch",
                },
                {
                    "job_title": "Analytics Engineer",
                    "company": "Beta",
                    "location": "United States",
                    "job_type": "Hybrid",
                    "job_url": "https://example.com/analytics-1",
                    "description_snippet": "Develop SQL and dbt models.",
                    "date_posted": "2026-11-10",
                    "source": "jsearch",
                },
                {
                    "job_title": "Data Scientist",
                    "company": "Gamma",
                    "location": "United States",
                    "job_type": "Remote",
                    "job_url": "https://example.com/science-1",
                    "description_snippet": "Train machine learning models.",
                    "date_posted": "2026-11-10",
                    "source": "jsearch",
                },
                {
                    "job_title": "Data Entry Clerk",
                    "company": "Delta",
                    "location": "United States",
                    "job_type": "On-site",
                    "job_url": "https://example.com/entry-1",
                    "description_snippet": "Maintain spreadsheets.",
                    "date_posted": "2026-11-10",
                    "source": "jsearch",
                },
            ]
            pushed: dict = {}

            def _fake_push(jobs, _supabase_url, _supabase_key, _config, **kwargs):
                if kwargs.get("table") != "rejected_jobs":
                    pushed["jobs"] = jobs
                return {"inserted": len(jobs), "updated": 0, "skipped": 0, "cleaned": 0, "error": None, "inserted_urls": []}

            with (
                patch.object(orchestrate, "ROOT", tmp_root),
                patch.object(orchestrate, "TMP_DIR", tmp_root / ".tmp"),
                patch.object(orchestrate, "load_config", return_value=config),
                patch.object(orchestrate, "load_dotenv", return_value=None),
                patch.object(orchestrate, "load_source_health_state", return_value={}),
                patch.object(orchestrate, "save_source_health_state", return_value=None),
                patch.object(orchestrate.fetch_jsearch, "run", return_value=fetched_jobs),
                patch.object(orchestrate.push_to_supabase, "run", side_effect=_fake_push),
                patch.object(orchestrate.notify_webhook, "run", return_value=None),
            ):
                os.environ["JSEARCH_API_KEY"] = "x"
                os.environ["SUPABASE_URL"] = "https://example.supabase.co"
                os.environ["SUPABASE_SERVICE_KEY"] = "test_key"
                os.environ["DISCORD_WEBHOOK_URL"] = ""
                orchestrate.main()

            summary = json.loads((tmp_root / ".tmp" / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["pre_domain_filter_count"], 4)
            self.assertEqual(summary["post_domain_filter_count"] + summary["dropped_out_of_domain_count"], 4)
            self.assertEqual(summary["dropped_out_of_domain_count"], 2)
            self.assertTrue((tmp_root / ".tmp" / "domain_filtered_jobs.json").exists())
            self.assertTrue((tmp_root / ".tmp" / "rejected_out_of_domain_jobs.json").exists())
            self.assertTrue((tmp_root / ".tmp" / "all_jobs.json").exists())
            self.assertTrue((tmp_root / ".tmp" / "scored_jobs.json").exists())
            self.assertTrue((tmp_root / ".tmp" / "qualified_jobs.json").exists())
            self.assertEqual([job["job_title"] for job in pushed["jobs"]], ["Data Engineer", "Analytics Engineer"])


if __name__ == "__main__":
    unittest.main()
