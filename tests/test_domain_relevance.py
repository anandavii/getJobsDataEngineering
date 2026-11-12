import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import domain_relevance
import config_loader


RULES = {
    "enabled": True,
    "mode": "title_or_description",
    "require_software_context_for_description_only": True,
    "include_patterns": [
        "data engineer",
        "data platform engineer",
        "analytics engineer",
        "streaming data engineer",
    ],
    "exclude_patterns": [
        "data entry",
        "data scientist",
        "business intelligence analyst",
        "database administrator",
    ],
    "software_context_patterns": ["python", "sql", "dbt", "airflow", "spark", "kafka"],
}


class DomainRelevanceTests(unittest.TestCase):
    def test_active_domain_accepts_all_approved_data_role_titles(self) -> None:
        config = config_loader.load_config()
        approved_titles = (
            "Data Engineer",
            "Azure Data Engineer",
            "Cloud Data Engineer",
            "Junior Data Engineer",
            "Associate Data Engineer",
            "Entry-Level Data Engineer",
            "Data Engineer I",
            "Data Engineer II",
            "Graduate Data Engineer",
            "Data Engineering Associate",
            "Data Engineering Analyst",
            "Data Pipeline Engineer",
            "Data Pipeline Developer",
            "Data Platform Engineer",
            "Big Data Engineer",
            "ETL Developer",
            "ETL Engineer",
            "ETL Programmer",
            "ELT Developer",
            "Data Integration Engineer",
            "Data Integration Developer",
            "Azure Data Factory Developer",
            "ADF Developer",
            "Databricks Engineer",
            "Databricks Developer",
            "PySpark Developer",
            "Spark Developer",
            "Data Processing Engineer",
            "Data Warehouse Developer",
            "Data Warehouse Engineer",
            "SQL Developer",
            "Data Quality Engineer",
            "Data Quality Analyst",
            "Data Validation Engineer",
            "Data Validation Analyst",
            "Data Quality Automation Engineer",
            "Data Test Engineer",
            "ETL Test Engineer",
            "ETL Tester",
            "ETL QA Engineer",
            "ETL Automation Engineer",
            "Data Warehouse Tester",
            "Database QA Engineer",
            "Data Reliability Engineer",
            "Data Reconciliation Analyst",
            "Data Integrity Analyst",
            "Data Analytics Engineer",
            "Analytics Engineer",
            "Junior Analytics Engineer",
            "BI Developer",
            "BI Engineer",
            "Data Analyst",
            "Junior Data Analyst",
            "Associate Data Analyst",
            "SQL Data Analyst",
            "Streaming Data Engineer",
            "Real-Time Data Engineer",
            "Lakehouse Engineer",
            "Data Infrastructure Engineer",
            "DataOps Engineer",
            "Cloud Data Platform Engineer",
            "Data Observability Engineer",
            "Data Transformation Engineer",
            "Data Migration Engineer",
            "Data Migration Developer",
            "Data Modeling Engineer",
            "Database Developer",
        )

        for title in approved_titles:
            keep, reason = domain_relevance.is_domain_relevant(
                {"job_title": title, "description_snippet": ""}, config["domain_filter"]
            )
            self.assertTrue(keep, title)
            self.assertEqual(reason, "title_match", title)

    def test_active_domain_keeps_data_analyst_titles(self) -> None:
        config = config_loader.load_config()
        for title in ("Data Analyst", "Junior Data Analyst", "Associate Data Analyst", "Data Analyst I", "SQL Data Analyst"):
            keep, _ = domain_relevance.is_domain_relevant(
                {"job_title": title, "description_snippet": ""}, config["domain_filter"]
            )
            self.assertTrue(keep, title)

        for title in ("Business Intelligence Analyst", "Reporting Analyst", "Business Data Analyst", "Operations Data Analyst"):
            keep, reason = domain_relevance.is_domain_relevant(
                {"job_title": title, "description_snippet": ""}, config["domain_filter"]
            )
            self.assertFalse(keep, title)
            self.assertEqual(reason, "excluded_out_of_domain", title)

    def test_keeps_data_engineering_titles(self) -> None:
        for title in ("Data Platform Engineer", "Analytics Engineer", "Streaming Data Engineer"):
            keep, reason = domain_relevance.is_domain_relevant({"job_title": title, "description_snippet": ""}, RULES)
            self.assertTrue(keep, title)
            self.assertEqual(reason, "title_match")

    def test_drops_adjacent_data_roles(self) -> None:
        for title in ("Data Entry Clerk", "Senior Data Scientist", "Business Intelligence Analyst", "Database Administrator"):
            keep, reason = domain_relevance.is_domain_relevant({"job_title": title, "description_snippet": ""}, RULES)
            self.assertFalse(keep, title)
            self.assertEqual(reason, "excluded_out_of_domain")

    def test_active_domain_rejects_generic_integration_engineering(self) -> None:
        config = config_loader.load_config()
        keep, reason = domain_relevance.is_domain_relevant(
            {
                "job_title": "IC Packaging Integration Engineer",
                "description_snippet": "Own package integration for silicon and hardware validation.",
            },
            config["domain_filter"],
        )

        self.assertFalse(keep)
        self.assertEqual(reason, "no_domain_match")

    def test_active_domain_keeps_data_integration_engineering(self) -> None:
        config = config_loader.load_config()
        keep, reason = domain_relevance.is_domain_relevant(
            {
                "job_title": "Data Integration Engineer",
                "description_snippet": "Build ETL pipelines with Python and SQL.",
            },
            config["domain_filter"],
        )

        self.assertTrue(keep)
        self.assertEqual(reason, "title_match")

    def test_description_only_match_requires_data_engineering_context(self) -> None:
        keep, reason = domain_relevance.is_domain_relevant(
            {
                "job_title": "Platform Engineer",
                "description_snippet": "The Data Engineer will build Python dbt pipelines orchestrated by Airflow.",
            },
            RULES,
        )
        self.assertTrue(keep)
        self.assertEqual(reason, "description_match_with_context")

    def test_description_only_incidental_data_engineering_is_dropped(self) -> None:
        keep, reason = domain_relevance.is_domain_relevant(
            {
                "job_title": "Operations Specialist",
                "description_snippet": "Partner with a Data Engineer to document monthly reporting.",
            },
            RULES,
        )
        self.assertFalse(keep)
        self.assertEqual(reason, "description_match_missing_context")

    def test_filter_records_generic_rejection_metadata(self) -> None:
        kept, rejected, metrics = domain_relevance.filter_domain_jobs(
            [
                {"job_title": "Data Engineer", "description_snippet": "", "source": "greenhouse"},
                {"job_title": "Data Entry Clerk", "description_snippet": "", "source": "lever"},
            ],
            RULES,
        )

        self.assertEqual([job["job_title"] for job in kept], ["Data Engineer"])
        self.assertEqual(rejected[0]["_domain_filter_reason"], "excluded_out_of_domain")
        self.assertEqual(metrics["dropped_out_of_domain_by_source"], {"lever": 1})


if __name__ == "__main__":
    unittest.main()
