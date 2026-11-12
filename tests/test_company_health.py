"""
Unit tests for tools/company_health.py

TDD — written before implementation. All tests must FAIL initially
(ModuleNotFoundError or assertion errors) and PASS after implementation.

Mocking strategy: patch `company_health.requests.get` and
`company_health.requests.post` since the module uses direct REST calls
via the requests library (matching the health_state.py pattern in this project).
"""

import sys
import unittest
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import company_health as ch

SUPABASE_URL = "https://test.supabase.co"
SUPABASE_KEY = "test-service-key"

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_get_resp(rows: list):
    """Return a mock requests.Response for GET (fetch existing row)."""
    resp = MagicMock()
    resp.json.return_value = rows
    resp.raise_for_status = MagicMock()
    return resp


def _make_post_resp():
    """Return a mock requests.Response for POST (upsert)."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    return resp


def _make_load_resp(rows: list):
    """Return a mock requests.Response for load (GET all rows)."""
    resp = MagicMock()
    resp.json.return_value = rows
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Tests for _compute_days_empty
# ---------------------------------------------------------------------------


class ComputeDaysEmptyTests(unittest.TestCase):
    def test_returns_zero_when_none(self):
        self.assertEqual(ch._compute_days_empty(None), 0)

    def test_returns_zero_for_today(self):
        today_iso = datetime.now(UTC).isoformat()
        self.assertEqual(ch._compute_days_empty(today_iso), 0)

    def test_returns_correct_days_for_past_date(self):
        # Use a date that is clearly in the past
        past_iso = "2020-01-01T00:00:00+00:00"
        result = ch._compute_days_empty(past_iso)
        self.assertGreater(result, 365)

    def test_returns_one_for_yesterday(self):
        from datetime import timedelta

        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        result = ch._compute_days_empty(yesterday)
        self.assertEqual(result, 1)


# ---------------------------------------------------------------------------
# Tests for _derive_status
# ---------------------------------------------------------------------------


class DeriveStatusTests(unittest.TestCase):
    def test_healthy_when_days_empty_zero(self):
        self.assertEqual(ch._derive_status(0), "healthy")

    def test_healthy_when_days_empty_two(self):
        self.assertEqual(ch._derive_status(2), "healthy")

    def test_monitor_when_days_empty_three(self):
        self.assertEqual(ch._derive_status(3), "monitor")

    def test_monitor_when_days_empty_six(self):
        self.assertEqual(ch._derive_status(6), "monitor")

    def test_critical_when_days_empty_seven(self):
        self.assertEqual(ch._derive_status(7), "critical")

    def test_critical_when_days_empty_ten(self):
        self.assertEqual(ch._derive_status(10), "critical")


# ---------------------------------------------------------------------------
# Tests for _update_daily_counts
# ---------------------------------------------------------------------------


class UpdateDailyCountsTests(unittest.TestCase):
    def test_new_company_with_jobs_returns_zero_padded_list(self):
        """New company (empty existing) → [0,0,0,0,0,0,N]"""
        result = ch._update_daily_counts([], 5, None)
        self.assertEqual(result, [0, 0, 0, 0, 0, 0, 5])

    def test_new_company_with_zero_jobs(self):
        """New company, zero jobs → [0,0,0,0,0,0,0]"""
        result = ch._update_daily_counts([], 0, None)
        self.assertEqual(result, [0, 0, 0, 0, 0, 0, 0])

    def test_window_shifts_left_on_new_day(self):
        """existing [10,12,8,0,0,5,7] + new fetch today → [12,8,0,0,5,7,N]"""
        yesterday = "2020-01-01T00:00:00+00:00"  # clearly not today
        result = ch._update_daily_counts([10, 12, 8, 0, 0, 5, 7], 3, yesterday)
        self.assertEqual(result, [12, 8, 0, 0, 5, 7, 3])

    def test_same_day_overwrites_last_element(self):
        """If updated_at is today (UTC), overwrite last element instead of shifting."""
        today_iso = datetime.now(UTC).isoformat()
        result = ch._update_daily_counts([10, 12, 8, 0, 0, 5, 7], 99, today_iso)
        self.assertEqual(result, [10, 12, 8, 0, 0, 5, 99])

    def test_always_returns_seven_elements(self):
        yesterday = "2020-01-01T00:00:00+00:00"
        result = ch._update_daily_counts([1, 2, 3], 4, yesterday)
        self.assertEqual(len(result), 7)


# ---------------------------------------------------------------------------
# Tests for upsert_company_health
# ---------------------------------------------------------------------------


class UpsertCompanyHealthTests(unittest.TestCase):
    def _call_capture_payload(self, jobs_found, existing_rows=None):
        """Return (result, captured_payload) from the upsert POST."""
        if existing_rows is None:
            existing_rows = []

        get_resp = _make_get_resp(existing_rows)
        post_resp = _make_post_resp()
        captured = {}

        def capture_post(url, **kwargs):
            captured["payload"] = kwargs.get("json", {})
            return post_resp

        with (
            patch("company_health.requests.get", return_value=get_resp),
            patch("company_health.requests.post", side_effect=capture_post),
        ):
            result = ch.upsert_company_health(
                company="Acme",
                ats_type="greenhouse",
                ats_token="acme",
                job_board_url="https://acme.com/jobs",
                jobs_found=jobs_found,
                supabase_url=SUPABASE_URL,
                supabase_key=SUPABASE_KEY,
            )
        return result, captured.get("payload", {})

    # --- Test 1: New company with jobs found ---
    def test_new_company_with_jobs_found(self):
        result, payload = self._call_capture_payload(jobs_found=5, existing_rows=[])
        self.assertTrue(result)
        self.assertEqual(payload["days_empty"], 0)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["daily_counts"], [0, 0, 0, 0, 0, 0, 5])
        self.assertIsNotNone(payload["last_nonempty_at"])

    # --- Test 2: New company with zero jobs ---
    def test_new_company_with_zero_jobs(self):
        result, payload = self._call_capture_payload(jobs_found=0, existing_rows=[])
        self.assertTrue(result)
        self.assertEqual(payload["days_empty"], 0)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["daily_counts"], [0, 0, 0, 0, 0, 0, 0])
        self.assertIsNone(payload["last_nonempty_at"])

    # --- Test 3: Daily counts window shifts correctly ---
    def test_daily_counts_shift_on_new_day(self):
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [10, 12, 8, 0, 0, 5, 7],
                "updated_at": "2020-01-01T00:00:00+00:00",
                "last_nonempty_at": "2020-01-01T00:00:00+00:00",
                "days_empty": 0,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=3, existing_rows=existing)
        self.assertTrue(result)
        self.assertEqual(payload["daily_counts"], [12, 8, 0, 0, 5, 7, 3])

    # --- Test 4: Same-day second run overwrites ---
    def test_same_day_second_run_overwrites_last_element(self):
        today_iso = datetime.now(UTC).isoformat()
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [10, 12, 8, 0, 0, 5, 7],
                "updated_at": today_iso,
                "last_nonempty_at": today_iso,
                "days_empty": 0,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=99, existing_rows=existing)
        self.assertTrue(result)
        self.assertEqual(payload["daily_counts"], [10, 12, 8, 0, 0, 5, 99])

    # --- Test 5: Status healthy ---
    def test_status_healthy_when_days_empty_less_than_3(self):
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [0, 0, 0, 0, 0, 0, 0],
                "updated_at": "2020-01-01T00:00:00+00:00",
                "last_nonempty_at": datetime.now(UTC).isoformat(),
                "days_empty": 2,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=0, existing_rows=existing)
        self.assertEqual(payload["status"], "healthy")

    # --- Test 6: Status monitor ---
    def test_status_monitor_when_days_empty_3_to_6(self):
        from datetime import timedelta

        three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [0, 0, 0, 0, 0, 0, 0],
                "updated_at": "2020-01-01T00:00:00+00:00",
                "last_nonempty_at": three_days_ago,
                "days_empty": 3,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=0, existing_rows=existing)
        self.assertEqual(payload["status"], "monitor")

    # --- Test 7: Status critical ---
    def test_status_critical_when_days_empty_7_or_more(self):
        from datetime import timedelta

        seven_days_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [0, 0, 0, 0, 0, 0, 0],
                "updated_at": "2020-01-01T00:00:00+00:00",
                "last_nonempty_at": seven_days_ago,
                "days_empty": 7,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=0, existing_rows=existing)
        self.assertEqual(payload["status"], "critical")

    # --- Test 8: Status resets after critical ---
    def test_status_resets_to_healthy_when_jobs_found_after_critical(self):
        from datetime import timedelta

        nine_days_ago = (datetime.now(UTC) - timedelta(days=9)).isoformat()
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [0, 0, 0, 0, 0, 0, 0],
                "updated_at": "2020-01-01T00:00:00+00:00",
                "last_nonempty_at": nine_days_ago,
                "days_empty": 9,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=5, existing_rows=existing)
        self.assertTrue(result)
        self.assertEqual(payload["days_empty"], 0)
        self.assertEqual(payload["status"], "healthy")
        self.assertIsNotNone(payload["last_nonempty_at"])

    # --- Test 9: last_nonempty_at updates when jobs found ---
    def test_last_nonempty_at_updated_when_jobs_found(self):
        old_ts = "2020-01-01T00:00:00+00:00"
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [0, 0, 0, 0, 0, 0, 0],
                "updated_at": old_ts,
                "last_nonempty_at": old_ts,
                "days_empty": 5,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=3, existing_rows=existing)
        self.assertNotEqual(payload["last_nonempty_at"], old_ts)
        self.assertIsNotNone(payload["last_nonempty_at"])

    # --- Test 10: last_nonempty_at unchanged when zero jobs ---
    def test_last_nonempty_at_unchanged_when_zero_jobs(self):
        old_ts = "2022-06-15T10:00:00+00:00"
        existing = [
            {
                "company": "Acme",
                "ats_type": "greenhouse",
                "daily_counts": [0, 0, 0, 0, 0, 0, 0],
                "updated_at": "2020-01-01T00:00:00+00:00",
                "last_nonempty_at": old_ts,
                "days_empty": 1,
            }
        ]
        result, payload = self._call_capture_payload(jobs_found=0, existing_rows=existing)
        self.assertEqual(payload["last_nonempty_at"], old_ts)

    # --- Test 11: upsert returns False on Supabase error ---
    def test_returns_false_on_supabase_error(self):
        get_resp = _make_get_resp([])
        with (
            patch("company_health.requests.get", return_value=get_resp),
            patch("company_health.requests.post", side_effect=Exception("connection refused")),
        ):
            result = ch.upsert_company_health(
                company="Acme",
                ats_type="greenhouse",
                ats_token="acme",
                job_board_url="https://acme.com/jobs",
                jobs_found=5,
                supabase_url=SUPABASE_URL,
                supabase_key=SUPABASE_KEY,
            )
        self.assertFalse(result)

    def test_returns_false_on_fetch_error(self):
        with patch("company_health.requests.get", side_effect=Exception("timeout")):
            result = ch.upsert_company_health(
                company="Acme",
                ats_type="greenhouse",
                ats_token="acme",
                job_board_url="https://acme.com/jobs",
                jobs_found=5,
                supabase_url=SUPABASE_URL,
                supabase_key=SUPABASE_KEY,
            )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Tests for load_company_health
# ---------------------------------------------------------------------------


class LoadCompanyHealthTests(unittest.TestCase):
    # --- Test 12: returns [] on error ---
    def test_returns_empty_list_on_error(self):
        with patch("company_health.requests.get", side_effect=Exception("network error")):
            result = ch.load_company_health(SUPABASE_URL, SUPABASE_KEY)
        self.assertEqual(result, [])

    # --- Test 13: returns sorted data ---
    def test_returns_rows_sorted_by_days_empty_desc(self):
        rows = [
            {"company": "Acme", "ats_type": "greenhouse", "days_empty": 5, "status": "monitor"},
            {"company": "Beta", "ats_type": "workday", "days_empty": 0, "status": "healthy"},
            {"company": "Zeta", "ats_type": "lever", "days_empty": 9, "status": "critical"},
        ]
        resp = _make_load_resp(rows)
        with patch("company_health.requests.get", return_value=resp):
            result = ch.load_company_health(SUPABASE_URL, SUPABASE_KEY)
        self.assertEqual(len(result), 3)
        # Verify sorted descending by days_empty
        self.assertEqual(result[0]["days_empty"], 9)
        self.assertEqual(result[1]["days_empty"], 5)
        self.assertEqual(result[2]["days_empty"], 0)

    def test_load_uses_order_by_days_empty_desc_in_url(self):
        """Verify that the GET request includes order=days_empty.desc query param."""
        resp = _make_load_resp([])
        with patch("company_health.requests.get", return_value=resp) as mock_get:
            ch.load_company_health(SUPABASE_URL, SUPABASE_KEY)
        call_url = mock_get.call_args[0][0]
        self.assertIn("days_empty", call_url)

    def test_returns_empty_list_when_credentials_missing(self):
        result = ch.load_company_health("", "")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
