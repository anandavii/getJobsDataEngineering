import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import push_to_supabase


class PushToSupabaseTests(unittest.TestCase):
    @patch("push_to_supabase.requests.post")
    @patch("push_to_supabase.requests.get")
    @patch("push_to_supabase.requests.delete")
    def test_run_reports_only_truly_new_urls(self, mock_delete: Mock, mock_get: Mock, mock_post: Mock) -> None:
        mock_delete.return_value = Mock(text="[]", json=Mock(return_value=[]), raise_for_status=Mock())
        mock_get.return_value = Mock(
            text='[{"url":"https://example.com/existing"}]',
            json=Mock(return_value=[{"url": "https://example.com/existing"}]),
            raise_for_status=Mock(),
        )
        # Two upsert calls: first for new rows, second for existing rows.
        mock_post.side_effect = [
            Mock(
                text='[{"url":"https://example.com/new"}]',
                json=Mock(return_value=[{"url": "https://example.com/new"}]),
                raise_for_status=Mock(),
            ),
            Mock(
                text='[{"url":"https://example.com/existing"}]',
                json=Mock(return_value=[{"url": "https://example.com/existing"}]),
                raise_for_status=Mock(),
            ),
        ]

        result = push_to_supabase.run(
            jobs=[
                {
                    "job_url": "https://example.com/existing",
                    "job_title": "Existing role",
                },
                {"job_url": "https://example.com/new", "job_title": "New role"},
            ],
            supabase_url="https://example.supabase.co",
            supabase_key="key",
            config={"supabase": {"matched_table": "matched_jobs", "batch_size": 50}},
        )

        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["inserted_urls"], ["https://example.com/new"])

    @patch("push_to_supabase.requests.post")
    @patch("push_to_supabase.requests.get")
    @patch("push_to_supabase.requests.delete")
    def test_run_cleanup_for_matched_jobs_does_not_require_status_column(
        self, mock_delete: Mock, mock_get: Mock, mock_post: Mock
    ) -> None:
        mock_delete.return_value = Mock(text="[]", json=Mock(return_value=[]), raise_for_status=Mock())
        mock_get.return_value = Mock(text="[]", json=Mock(return_value=[]), raise_for_status=Mock())
        mock_post.return_value = Mock(
            text='[{"url":"https://example.com/new"}]',
            json=Mock(return_value=[{"url": "https://example.com/new"}]),
            raise_for_status=Mock(),
        )

        push_to_supabase.run(
            jobs=[{"job_url": "https://example.com/new", "job_title": "New role"}],
            supabase_url="https://example.supabase.co",
            supabase_key="key",
            config={"supabase": {"matched_table": "matched_jobs", "batch_size": 50}},
        )

        delete_params = mock_delete.call_args.kwargs["params"]
        self.assertIn("fetched_at", delete_params)
        self.assertNotIn("status", delete_params)

    @patch("push_to_supabase.requests.post")
    @patch("push_to_supabase.requests.get")
    @patch("push_to_supabase.requests.delete")
    def test_upsert_includes_on_conflict_url_param(self, mock_delete: Mock, mock_get: Mock, mock_post: Mock) -> None:
        """PostgREST requires ?on_conflict=url for merge-duplicates to work."""
        mock_delete.return_value = Mock(text="[]", json=Mock(return_value=[]), raise_for_status=Mock())
        mock_get.return_value = Mock(text="[]", json=Mock(return_value=[]), raise_for_status=Mock())
        mock_post.return_value = Mock(
            text='[{"url":"https://example.com/job1"}]',
            json=Mock(return_value=[{"url": "https://example.com/job1"}]),
            raise_for_status=Mock(),
        )

        push_to_supabase.run(
            jobs=[{"job_url": "https://example.com/job1", "job_title": "Supply Chain Manager"}],
            supabase_url="https://example.supabase.co",
            supabase_key="key",
            config={"supabase": {"matched_table": "matched_jobs", "batch_size": 50}},
        )

        post_url = mock_post.call_args.args[0]
        self.assertIn("on_conflict=url", post_url)


if __name__ == "__main__":
    unittest.main()
