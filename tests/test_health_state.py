import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import health_state as hs

SUPABASE_URL = "https://test.supabase.co"
SERVICE_KEY = "test-service-key"


class LoadFromSupabaseTests(unittest.TestCase):
    def _mock_resp(self, rows):
        resp = MagicMock()
        resp.json.return_value = rows
        resp.raise_for_status = MagicMock()
        return resp

    def test_returns_dict_keyed_by_source(self):
        rows = [
            {"source": "jsearch", "runs": 10, "successes": 9, "consecutive_failures": 0},
            {"source": "greenhouse", "runs": 8, "successes": 8, "consecutive_failures": 0},
        ]
        with patch("health_state.requests.get", return_value=self._mock_resp(rows)):
            result = hs.load_from_supabase(SUPABASE_URL, SERVICE_KEY)
        self.assertIn("jsearch", result)
        self.assertIn("greenhouse", result)
        self.assertEqual(result["jsearch"]["runs"], 10)

    def test_returns_empty_dict_on_http_error(self):
        with patch("health_state.requests.get", side_effect=Exception("connection refused")):
            result = hs.load_from_supabase(SUPABASE_URL, SERVICE_KEY)
        self.assertEqual(result, {})

    def test_returns_empty_dict_when_credentials_missing(self):
        result = hs.load_from_supabase("", "")
        self.assertEqual(result, {})

    def test_skips_rows_without_source_key(self):
        rows = [
            {"runs": 5},
            {"source": "dice", "runs": 3, "consecutive_failures": 1},
        ]
        with patch("health_state.requests.get", return_value=self._mock_resp(rows)):
            result = hs.load_from_supabase(SUPABASE_URL, SERVICE_KEY)
        self.assertIn("dice", result)
        self.assertEqual(len(result), 1)


class SaveToSupabaseTests(unittest.TestCase):
    def _mock_ok_resp(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    def _make_state(self):
        return {
            "jsearch": {
                "runs": 5,
                "successes": 5,
                "failures": 0,
                "empties": 0,
                "avg_duration_s": 12.3,
                "consecutive_failures": 0,
                "last_success_utc": "2026-03-27T10:00:00Z",
            }
        }

    def test_upserts_each_source_row(self):
        with patch("health_state.requests.post", return_value=self._mock_ok_resp()) as mock_post:
            result = hs.save_to_supabase(SUPABASE_URL, SERVICE_KEY, self._make_state())
        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["source"], "jsearch")
        self.assertEqual(payload["runs"], 5)

    def test_returns_false_on_partial_failure(self):
        state = {
            "jsearch": {
                "runs": 5,
                "successes": 5,
                "failures": 0,
                "empties": 0,
                "avg_duration_s": 12.3,
                "consecutive_failures": 0,
                "last_success_utc": None,
            },
            "dice": {
                "runs": 3,
                "successes": 1,
                "failures": 2,
                "empties": 0,
                "avg_duration_s": 3.0,
                "consecutive_failures": 2,
                "last_success_utc": None,
            },
        }
        ok_resp = self._mock_ok_resp()
        with patch("health_state.requests.post", side_effect=[ok_resp, Exception("timeout")]):
            result = hs.save_to_supabase(SUPABASE_URL, SERVICE_KEY, state)
        self.assertFalse(result)

    def test_returns_false_when_credentials_missing(self):
        result = hs.save_to_supabase("", "", self._make_state())
        self.assertFalse(result)

    def test_returns_false_for_empty_state(self):
        result = hs.save_to_supabase(SUPABASE_URL, SERVICE_KEY, {})
        self.assertFalse(result)

    def test_prefer_header_includes_merge_duplicates(self):
        with patch("health_state.requests.post", return_value=self._mock_ok_resp()) as mock_post:
            hs.save_to_supabase(SUPABASE_URL, SERVICE_KEY, self._make_state())
        headers = mock_post.call_args[1]["headers"]
        self.assertIn("merge-duplicates", headers.get("Prefer", ""))


if __name__ == "__main__":
    unittest.main()
