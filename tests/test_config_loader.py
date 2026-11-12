import json
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data))


@pytest.fixture()
def cfg(tmp_path):
    """Minimal but complete config tree under tmp_path/config."""
    c = tmp_path / "config"
    (c).mkdir()
    (c / "domains").mkdir()
    (c / "sources").mkdir()

    _write(
        c / "job_search_config.json",
        {
            "active_domain": "test_domain",
            "search": {"query_generation": {"enabled": True}, "jsearch_num_pages": 1},
            "domain_filter": {"rejected_sample_limit": 50},
            "ranking": {"enabled": True, "weights": {"title_family_match": 0.4}, "min_fit_score": 0.4},
            "supabase": {"matched_table": "matched_jobs"},
            "notification": {"max_jobs_in_message": 10},
            "source_tiers": {"tier1": ["jsearch"], "tier2": [], "tier3": []},
            "health": {"max_consecutive_failures": 3},
            "sources": {"jsearch": True},
        },
    )

    _write(
        c / "domains" / "test_domain.json",
        {
            "search": {
                "keywords": ["QA automation engineer", "SDET"],
                "role_taxonomy": {"families": ["automation_qa"]},
                "seniority_policy": {"include_levels": ["mid", "senior"]},
            },
            "domain_filter": {
                "enabled": True,
                "mode": "title_or_description",
                "include_patterns": ["qa", "sdet"],
                "exclude_patterns": ["manufacturing"],
            },
        },
    )

    _write(
        c / "sources" / "sources_catalog.json",
        {
            "greenhouse": [{"name": "Flexport", "board_token": "flexport", "enabled": True}],
            "lever": [],
            "ashby": [],
            "workday": [],
        },
    )

    _write(
        c / "sources" / "companies.json",
        [{"name": "Amazon", "ats": "custom", "url": "https://example.com", "enabled": True}],
    )

    return c  # the config/ directory, pass as config_dir=cfg


# ---------------------------------------------------------------------------
# Happy path: shape and merge correctness
# ---------------------------------------------------------------------------


def test_all_expected_keys_present(cfg):
    from tools.config_loader import load_config

    config = load_config(config_dir=cfg)
    for key in [
        "search",
        "domain_filter",
        "ranking",
        "supabase",
        "notification",
        "source_tiers",
        "health",
        "sources",
        "sources_catalog",
        "companies",
    ]:
        assert key in config, f"Missing key: {key}"


def test_domain_keywords_merged_into_search(cfg):
    from tools.config_loader import load_config

    config = load_config(config_dir=cfg)
    assert "QA automation engineer" in config["search"]["keywords"]


def test_domain_filter_merged(cfg):
    from tools.config_loader import load_config

    config = load_config(config_dir=cfg)
    assert config["domain_filter"]["enabled"] is True
    assert "qa" in config["domain_filter"]["include_patterns"]


def test_runtime_knob_not_overwritten_by_domain(cfg):
    """domain_filter.rejected_sample_limit comes from runtime config; domain must not shadow it."""
    from tools.config_loader import load_config

    config = load_config(config_dir=cfg)
    assert config["domain_filter"]["rejected_sample_limit"] == 50


def test_sources_catalog_loaded(cfg):
    from tools.config_loader import load_config

    config = load_config(config_dir=cfg)
    assert config["sources_catalog"]["greenhouse"][0]["name"] == "Flexport"


def test_companies_loaded(cfg):
    from tools.config_loader import load_config

    config = load_config(config_dir=cfg)
    assert config["companies"][0]["name"] == "Amazon"


def test_active_data_engineering_config_merges_domain_rules():
    """The checked-in runtime config must load the active Data Engineering domain."""
    from tools.config_loader import load_config

    config = load_config()

    assert config["active_domain"] == "data_engineering"
    assert "Data Engineer" in config["search"]["keywords"]
    assert "data engineer" in config["domain_filter"]["include_patterns"]
    assert config["domain_filter"]["rejected_sample_limit"] == 200


def test_active_domain_defaults_to_data_engineering(cfg):
    """No active_domain key → loader uses data_engineering without error."""
    from tools.config_loader import load_config

    # Rename the domain file to data_engineering and remove the key.
    (cfg / "domains" / "test_domain.json").rename(cfg / "domains" / "data_engineering.json")
    data = json.loads((cfg / "job_search_config.json").read_text())
    del data["active_domain"]
    (cfg / "job_search_config.json").write_text(json.dumps(data))
    config = load_config(config_dir=cfg)
    assert "search" in config


# ---------------------------------------------------------------------------
# Error handling: ConfigLoadError (not sys.exit)
# ---------------------------------------------------------------------------


def test_missing_runtime_config_raises(cfg):
    from tools.config_loader import load_config, ConfigLoadError

    (cfg / "job_search_config.json").unlink()
    with pytest.raises(ConfigLoadError, match="not found"):
        load_config(config_dir=cfg)


def test_missing_domain_file_raises(cfg):
    from tools.config_loader import load_config, ConfigLoadError

    data = json.loads((cfg / "job_search_config.json").read_text())
    data["active_domain"] = "nonexistent"
    (cfg / "job_search_config.json").write_text(json.dumps(data))
    with pytest.raises(ConfigLoadError, match="not found"):
        load_config(config_dir=cfg)


def test_malformed_runtime_config_raises(cfg):
    from tools.config_loader import load_config, ConfigLoadError

    (cfg / "job_search_config.json").write_text("{ not valid json ,,, }")
    with pytest.raises(ConfigLoadError, match="parse error"):
        load_config(config_dir=cfg)


def test_malformed_domain_file_raises(cfg):
    from tools.config_loader import load_config, ConfigLoadError

    (cfg / "domains" / "test_domain.json").write_text("{ bad }")
    with pytest.raises(ConfigLoadError, match="parse error"):
        load_config(config_dir=cfg)


def test_malformed_sources_catalog_raises(cfg):
    from tools.config_loader import load_config, ConfigLoadError

    (cfg / "sources" / "sources_catalog.json").write_text("[ bad ]")
    with pytest.raises(ConfigLoadError, match="parse error"):
        load_config(config_dir=cfg)


def test_malformed_companies_raises(cfg):
    from tools.config_loader import load_config, ConfigLoadError

    (cfg / "sources" / "companies.json").write_text("{ bad }")
    with pytest.raises(ConfigLoadError, match="parse error"):
        load_config(config_dir=cfg)
