import sys
from pathlib import Path
from unittest.mock import patch

import requests
import pytest


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


def test_remotive_retries_then_raises_when_every_category_fails():
    config = {"search": {"keywords": ["Data Engineer"]}}

    with (
        patch("fetch_jobs_remotive.requests.get", side_effect=requests.RequestException("gateway")) as mock_get,
        patch("fetch_jobs_remotive.time.sleep"),
    ):
        with pytest.raises(RuntimeError):
            fetch_jobs_remotive.run(config)

    assert mock_get.call_count == 3
