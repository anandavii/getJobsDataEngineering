import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import role_horizon


DATA_ENGINEERING_TAXONOMY = {
    "families": ["data_platform", "warehouse_analytics", "pipelines", "distributed_streaming"],
    "title_variants": {
        "data_platform": ["Data Engineer", "Data Platform Engineer"],
        "warehouse_analytics": ["Analytics Engineer", "Data Warehouse Engineer"],
        "pipelines": ["ETL Engineer", "ELT Engineer"],
        "distributed_streaming": ["Streaming Data Engineer", "Big Data Engineer"],
    },
    "skill_variants": {
        "primary_stack": ["python", "sql", "dbt", "airflow", "spark", "kafka"],
        "related_stack": ["snowflake", "bigquery", "databricks"],
        "genai_stack": [],
    },
    "seniority_tokens": {
        "entry": ["junior", "associate"],
        "mid": ["engineer", "developer"],
        "senior": ["senior", "lead"],
        "staff": ["staff", "principal"],
    },
}


def _config() -> dict:
    return {
        "search": {
            "keywords": ["data engineer", "analytics engineer"],
            "role_taxonomy": DATA_ENGINEERING_TAXONOMY,
            "seniority_policy": {"include_levels": ["entry", "mid", "senior", "staff"]},
        },
        "ranking": {
            "enabled": True,
            "min_fit_score": 0.45,
            "weights": {
                "title_family_match": 0.35,
                "skill_stack_match": 0.35,
                "seniority_alignment": 0.10,
                "recency": 0.15,
                "source_reliability": 0.05,
            },
        },
    }


class RoleHorizonTests(unittest.TestCase):
    def test_generate_queries_respects_cap_and_includes_base_keywords(self) -> None:
        cfg = _config()
        cfg["search"]["query_generation"] = {"enabled": True, "max_queries_per_run": 3, "dedup_queries": True}
        queries = role_horizon.generate_queries(cfg)
        self.assertLessEqual(len(queries), 3)
        self.assertIn("data engineer", queries)

    def test_classify_role_families_detects_data_engineering_and_skills(self) -> None:
        cfg = _config()
        job = {
            "job_title": "Senior Analytics Engineer",
            "description_snippet": "Build dbt models in Snowflake with Python and SQL for reliable analytics pipelines.",
        }
        families, matched_titles, matched_skills = role_horizon.classify_role_families(job, cfg)
        self.assertIn("warehouse_analytics", families)
        self.assertTrue(any("analytics engineer" in title.lower() for title in matched_titles))
        self.assertTrue(any(skill.lower() in {"python", "sql", "dbt", "snowflake"} for skill in matched_skills))

    def test_infer_seniority(self) -> None:
        cfg = _config()
        self.assertEqual(
            role_horizon.infer_seniority({"job_title": "Staff Data Engineer", "description_snippet": ""}, cfg),
            "staff",
        )
        self.assertEqual(
            role_horizon.infer_seniority({"job_title": "Junior Data Engineer", "description_snippet": ""}, cfg),
            "entry",
        )

    def test_filter_by_fit_score_keeps_high_scored_jobs(self) -> None:
        cfg = _config()
        scored = role_horizon.annotate_and_score_jobs(
            [
                {
                    "job_title": "Data Engineer",
                    "description_snippet": "Python SQL dbt Airflow data pipelines",
                    "source": "jsearch",
                    "date_posted": "2026-03-20",
                },
                {
                    "job_title": "Data Scientist",
                    "description_snippet": "Train statistical models and conduct experiments",
                    "source": "jsearch",
                    "date_posted": "2026-03-20",
                },
            ],
            cfg,
            {"state": {}},
        )
        kept, dropped = role_horizon.filter_by_fit_score(scored, cfg)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["job_title"], "Data Engineer")
        self.assertEqual(len(dropped), 1)

    def test_default_taxonomy_does_not_contain_retired_qa_families(self) -> None:
        self.assertEqual(role_horizon.get_role_taxonomy({})["families"], [])


if __name__ == "__main__":
    unittest.main()
