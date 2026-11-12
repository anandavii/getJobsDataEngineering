import sys
import unittest
from datetime import datetime as real_datetime
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import orchestrate


class OrchestrateHelpersTests(unittest.TestCase):
    def test_merge_and_deduplicate_preserves_first_source_priority(self) -> None:
        first = [{"job_title": "A", "job_url": "https://example.com/1", "source": "jsearch"}]
        second = [{"job_title": "B", "job_url": "https://example.com/1", "source": "dice"}]
        merged = orchestrate.merge_and_deduplicate([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["job_title"], "A")
        self.assertEqual(merged[0]["source"], "jsearch")

    def test_location_filter_behavior_is_unchanged(self) -> None:
        jobs = [
            {
                "job_title": "US city",
                "location": "New York, NY",
                "job_url": "https://e/1",
            },
            {
                "job_title": "US keyword",
                "location": "United States",
                "job_url": "https://e/2",
            },
            {
                "job_title": "Remote",
                "location": "Remote - Anywhere",
                "job_url": "https://e/3",
            },
            {"job_title": "Non-US", "location": "London, UK", "job_url": "https://e/4"},
        ]
        filtered = orchestrate.filter_by_location(jobs)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/1", "https://e/2", "https://e/3"})

    def test_location_filter_full_state_name(self) -> None:
        jobs = [
            {
                "job_title": "City State",
                "location": "Liberty Lake, Washington",
                "job_url": "https://e/1",
            },
            {
                "job_title": "State Only",
                "location": "Washington",
                "job_url": "https://e/2",
            },
            {
                "job_title": "Non-US",
                "location": "London, England",
                "job_url": "https://e/3",
            },
        ]
        filtered = orchestrate.filter_by_location(jobs)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/1", "https://e/2"})

    def test_location_filter_rejects_non_us_country_code_prefix(self) -> None:
        jobs = [
            {
                "job_title": "India role",
                "location": "IN, TN, Chennai",
                "job_url": "https://e/1",
            },
            {
                "job_title": "US role",
                "location": "US, TN, Nashville",
                "job_url": "https://e/2",
            },
        ]
        filtered = orchestrate.filter_by_location(jobs)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/2"})

    def test_calendar_day_filter_today(self) -> None:
        jobs = [
            {
                "job_title": "Today",
                "date_posted": "2026-02-27",
                "job_url": "https://e/1",
            },
            {
                "job_title": "Yesterday",
                "date_posted": "2026-02-26",
                "job_url": "https://e/2",
            },
        ]
        config = {
            "search": {
                "calendar_day_filter": {
                    "mode": "today",
                    "timezone": "America/New_York",
                    "drop_if_missing_date": True,
                }
            }
        }

        def _fixed_now(tz=None):
            return real_datetime(2026, 2, 27, 12, 0, tzinfo=tz or orchestrate.timezone.utc)

        with mock.patch("orchestrate.datetime") as mock_dt:
            mock_dt.now.side_effect = _fixed_now
            filtered = orchestrate.filter_by_calendar_day(jobs, config)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/1"})

    def test_calendar_day_filter_yesterday_includes_today(self) -> None:
        jobs = [
            {
                "job_title": "Today",
                "date_posted": "2026-02-27",
                "job_url": "https://e/1",
            },
            {
                "job_title": "Yesterday",
                "date_posted": "2026-02-26",
                "job_url": "https://e/2",
            },
            {"job_title": "Old", "date_posted": "2026-02-25", "job_url": "https://e/3"},
        ]
        config = {
            "search": {
                "calendar_day_filter": {
                    "mode": "yesterday",
                    "timezone": "America/New_York",
                    "drop_if_missing_date": True,
                }
            }
        }

        def _fixed_now(tz=None):
            return real_datetime(2026, 2, 27, 12, 0, tzinfo=tz or orchestrate.timezone.utc)

        with mock.patch("orchestrate.datetime") as mock_dt:
            mock_dt.now.side_effect = _fixed_now
            filtered = orchestrate.filter_by_calendar_day(jobs, config)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/1", "https://e/2"})

    def test_calendar_day_filter_last_days_includes_today(self) -> None:
        jobs = [
            {
                "job_title": "Today",
                "date_posted": "2026-02-27",
                "job_url": "https://e/1",
            },
            {
                "job_title": "Day-1",
                "date_posted": "2026-02-26",
                "job_url": "https://e/2",
            },
            {
                "job_title": "Day-2",
                "date_posted": "2026-02-25",
                "job_url": "https://e/3",
            },
            {"job_title": "Old", "date_posted": "2026-02-24", "job_url": "https://e/4"},
        ]
        config = {
            "search": {
                "calendar_day_filter": {
                    "mode": "last_days",
                    "days": 3,
                    "timezone": "America/New_York",
                    "drop_if_missing_date": True,
                }
            }
        }

        def _fixed_now(tz=None):
            return real_datetime(2026, 2, 27, 12, 0, tzinfo=tz or orchestrate.timezone.utc)

        with mock.patch("orchestrate.datetime") as mock_dt:
            mock_dt.now.side_effect = _fixed_now
            filtered = orchestrate.filter_by_calendar_day(jobs, config)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/1", "https://e/2", "https://e/3"})

    def test_calendar_day_filter_allows_missing_dates_when_configured(self) -> None:
        jobs = [
            {"job_title": "Missing", "date_posted": None, "job_url": "https://e/1"},
            {
                "job_title": "Today",
                "date_posted": "2026-02-27",
                "job_url": "https://e/2",
            },
        ]
        config = {
            "search": {
                "calendar_day_filter": {
                    "mode": "today",
                    "timezone": "America/New_York",
                    "drop_if_missing_date": False,
                }
            }
        }

        def _fixed_now(tz=None):
            return real_datetime(2026, 2, 27, 12, 0, tzinfo=tz or orchestrate.timezone.utc)

        with mock.patch("orchestrate.datetime") as mock_dt:
            mock_dt.now.side_effect = _fixed_now
            filtered = orchestrate.filter_by_calendar_day(jobs, config)
        kept_urls = {j["job_url"] for j in filtered}
        self.assertEqual(kept_urls, {"https://e/1", "https://e/2"})

    def test_fetch_plan_uses_broader_terms_for_non_jsearch_sources(self) -> None:
        config = {"search": {"keywords": ["old keyword"]}, "sources": {"jsearch": True}}
        effective_config = {
            "search": {
                "keywords": ["limited JSearch query"],
                "source_match_keywords": ["Data Engineer", "ETL", "Data Quality"],
            },
            "sources": {},
            "sources_catalog": {"workday": [{"name": "Acme", "enabled": True}]},
        }

        plan = orchestrate._fetch_plan(config, effective_config, jsearch_api_key="test-key")

        with (
            mock.patch.object(orchestrate.fetch_jsearch, "run", return_value=[]) as jsearch_run,
            mock.patch.object(orchestrate.fetch_company, "run", return_value=[]) as company_run,
            mock.patch.object(orchestrate.fetch_workday, "run", return_value=[]) as workday_run,
            mock.patch.object(orchestrate.fetch_remotive, "run", return_value=[]) as remotive_run,
            mock.patch.object(orchestrate.fetch_jobicy, "run", return_value=[]) as jobicy_run,
            mock.patch.object(orchestrate.fetch_themuse, "run", return_value=[]) as themuse_run,
        ):
            plan["jsearch"]()
            plan["company_careers"]()
            plan["workday"]()
            plan["remotive"]()
            plan["jobicy"]()
            plan["themuse"]()

        jsearch_run.assert_called_once_with(effective_config, "test-key")
        source_config = company_run.call_args.args[0]
        self.assertEqual(source_config["search"]["keywords"], ["Data Engineer", "ETL", "Data Quality"])
        workday_run.assert_called_once_with(source_config, ["Data Engineer", "ETL", "Data Quality"])
        remotive_run.assert_called_once_with(source_config)
        jobicy_run.assert_called_once_with(source_config)
        themuse_run.assert_called_once_with(source_config)


class TestIsUsaOrRemote(unittest.TestCase):
    def test_nationwide_is_usa(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertTrue(_is_usa_or_remote("Nationwide"))

    def test_state_name_alone_is_usa(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertTrue(_is_usa_or_remote("Washington"))
        self.assertTrue(_is_usa_or_remote("California"))

    def test_city_state_name_is_usa(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertTrue(_is_usa_or_remote("Liberty Lake, Washington"))
        self.assertTrue(_is_usa_or_remote("Austin, Texas"))

    def test_city_state_abbrev_is_usa(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertTrue(_is_usa_or_remote("Liberty Lake, WA"))

    def test_flexport_warehouse_aliases_are_usa(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertTrue(_is_usa_or_remote("Chicago Warehouse"))
        self.assertTrue(_is_usa_or_remote("Phillipsburg Warehouse"))
        self.assertTrue(_is_usa_or_remote("San Bernardino Warehouse"))

    def test_non_us_locations_rejected(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertFalse(_is_usa_or_remote("London, UK"))
        self.assertFalse(_is_usa_or_remote("Norway"))
        self.assertFalse(_is_usa_or_remote("Tokyo, Japan"))

    def test_empty_location_passes(self):
        from fetch_jobs_catalog_adapters import _is_usa_or_remote

        self.assertTrue(_is_usa_or_remote(""))
        self.assertTrue(_is_usa_or_remote(None))


if __name__ == "__main__":
    unittest.main()
