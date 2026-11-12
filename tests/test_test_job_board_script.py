import sys
import unittest
import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_job_board


class TestJobBoardScriptTests(unittest.TestCase):
    def test_cli_helper_is_not_collected_by_pytest(self) -> None:
        self.assertFalse(getattr(test_job_board.test_source, "__test__", True))

    def test_uses_the_active_domain_filter(self) -> None:
        source = inspect.getsource(test_job_board)
        self.assertIn("filter_domain_jobs", source)
        self.assertNotIn("filter_qa_jobs", source)


if __name__ == "__main__":
    unittest.main()
