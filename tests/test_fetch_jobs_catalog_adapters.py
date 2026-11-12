import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fetch_jobs_catalog_adapters


_LIST_RESPONSE = {
    "data": {
        "jobBoardWithTeams": {
            "jobPostings": [
                {
                    "id": "abc-123",
                    "title": "Supply Chain Manager",
                    "locationName": "Austin, TX",
                    "workplaceType": "OnSite",
                }
            ]
        }
    }
}

_DETAIL_RESPONSE = {
    "data": {
        "jobPosting": {
            "publishedDate": "2026-02-04",
        }
    }
}


class AshbyPublishedDateTests(unittest.TestCase):
    def _make_config(self, org: str = "testorg") -> dict:
        return {
            "search": {"keywords": ["supply chain"]},
            "sources_catalog": {"ashby": [{"name": "Test Co", "organization": org, "enabled": True}]},
        }

    def test_run_ashby_fetches_published_date_for_matching_job(self) -> None:
        with patch(
            "fetch_jobs_catalog_adapters._request_json",
            side_effect=[_LIST_RESPONSE, _DETAIL_RESPONSE],
        ):
            with patch("fetch_jobs_catalog_adapters.upsert_company_health"):
                jobs = fetch_jobs_catalog_adapters.run_ashby(self._make_config())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["date_posted"], "2026-02-04")

    def test_run_ashby_date_posted_none_when_detail_fetch_fails(self) -> None:
        with patch(
            "fetch_jobs_catalog_adapters._request_json",
            side_effect=[_LIST_RESPONSE, Exception("timeout")],
        ):
            with patch("fetch_jobs_catalog_adapters.upsert_company_health"):
                jobs = fetch_jobs_catalog_adapters.run_ashby(self._make_config())

        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["date_posted"])


if __name__ == "__main__":
    unittest.main()
