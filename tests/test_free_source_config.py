import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fetch_jobs_jobicy
import fetch_jobs_remotive
import fetch_jobs_themuse


def test_free_board_categories_come_from_the_active_domain_config():
    config = {
        "search": {
            "free_sources": {
                "remotive_categories": ["software-dev"],
                "jobicy_tags": ["data-science", "engineering"],
                "themuse_categories": ["Data Science", "Engineering"],
            }
        }
    }

    assert fetch_jobs_remotive.categories_for(config) == ["software-dev"]
    assert fetch_jobs_jobicy.tags_for(config) == ["data-science", "engineering"]
    assert fetch_jobs_themuse.categories_for(config) == ["Data Science", "Engineering"]
