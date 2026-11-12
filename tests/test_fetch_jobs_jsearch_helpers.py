import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fetch_jobs_jsearch


class FetchJobsJSearchHelpersTests(unittest.TestCase):
    def test_to_canonical_location_uses_city_state_country(self) -> None:
        raw = {
            "job_title": "Senior SDET",
            "employer_name": "Example",
            "job_city": "Arlington",
            "job_state": "VA",
            "job_country": "US",
            "job_url": "https://example.com/job/1",
            "job_description": "desc",
        }
        out = fetch_jobs_jsearch._to_canonical(raw)
        self.assertEqual(out["location"], "Arlington, VA, US")

    def test_to_canonical_location_marks_remote(self) -> None:
        raw = {
            "job_title": "Senior SDET",
            "employer_name": "Example",
            "job_is_remote": True,
            "job_city": "Arlington",
            "job_state": "VA",
            "job_country": "US",
            "job_url": "https://example.com/job/1",
            "job_description": "desc",
        }
        out = fetch_jobs_jsearch._to_canonical(raw)
        self.assertEqual(out["location"], "Remote")


_MINIMAL_CONFIG = {
    "search": {
        "keywords": ["QA automation engineer", "SDET"],
        "date_posted": "today",
        "jsearch_num_pages": 1,
    }
}

_429_HEADERS = {
    "X-RateLimit-Requests-Remaining": "0",
    "X-RateLimit-Requests-Limit": "200",
    "X-RateLimit-Requests-Reset": "1234567",
}


def _make_429_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = _429_HEADERS
    resp.json.return_value = {"message": "quota exceeded"}
    return resp


class FetchJobsJSearchQuotaTests(unittest.TestCase):
    def test_fetch_keyword_raises_quota_exhausted_on_429(self) -> None:
        with patch("fetch_jobs_jsearch.requests.get", return_value=_make_429_response()):
            with self.assertRaises(fetch_jobs_jsearch._QuotaExhausted):
                fetch_jobs_jsearch._fetch_keyword("QA Engineer", "fake-key", _MINIMAL_CONFIG)

    def test_run_stops_after_first_429_and_makes_only_one_request(self) -> None:
        with patch("fetch_jobs_jsearch.requests.get", return_value=_make_429_response()) as mock_get:
            result = fetch_jobs_jsearch.run(_MINIMAL_CONFIG, "fake-key")

        self.assertEqual(result, [])
        mock_get.assert_called_once()  # must NOT retry remaining keywords


class FetchJobsJSearchRequestTests(unittest.TestCase):
    def test_run_maps_legacy_yesterday_to_supported_three_days(self) -> None:
        config = {"search": {"keywords": ["Data Engineer"], "date_posted": "yesterday", "jsearch_num_pages": 1}}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": []}

        with patch("fetch_jobs_jsearch.requests.get", return_value=response) as mock_get:
            fetch_jobs_jsearch.run(config, "fake-key")

        self.assertEqual(mock_get.call_args.kwargs["params"]["date_posted"], "3days")

    def test_run_raises_when_every_keyword_request_fails(self) -> None:
        config = {"search": {"keywords": ["Data Engineer"], "date_posted": "today", "jsearch_num_pages": 1}}

        with patch("fetch_jobs_jsearch.requests.get", side_effect=requests.RequestException("bad request")):
            with self.assertRaises(RuntimeError):
                fetch_jobs_jsearch.run(config, "fake-key")


if __name__ == "__main__":
    unittest.main()
