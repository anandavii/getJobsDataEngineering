import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import discover_companies as mod


DOMAIN_RULES = {
    "include_patterns": ["data engineer"],
    "exclude_patterns": ["data scientist"],
    "software_context_patterns": ["python", "sql"],
}


def test_resolve_discovery_keywords_uses_active_domain_config():
    config = {"search": {"keywords": ["Data Engineer", "Analytics Engineer"]}}

    assert mod.resolve_discovery_keywords(None, config) == ["Data Engineer", "Analytics Engineer"]
    assert mod.resolve_discovery_keywords(["Streaming Data Engineer"], config) == ["Streaming Data Engineer"]


def test_run_discovery_writes_generic_relevant_job_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_load_catalog", lambda: {"greenhouse": []})
    monkeypatch.setattr(mod, "_discover_tokens_for_ats", lambda *_: ({"acme"}, 1, 0, False))
    monkeypatch.setattr(
        mod,
        "_verify_fn_for_ats",
        lambda _ats, _rules: lambda token: mod.VerificationResult(
            ats="greenhouse",
            token=token,
            relevant_count=4,
            status="ok",
            http_status=200,
            error_kind=None,
            error_message=None,
        ),
    )

    output_path = tmp_path / "candidates.json"
    exit_code = mod.run_discovery(
        ats_list=["greenhouse"],
        min_relevant_jobs=3,
        keywords=["Data Engineer"],
        output_path=output_path,
        domain_rules=DOMAIN_RULES,
        display_name="Data Engineering",
    )

    data = json.loads(output_path.read_text())
    assert exit_code == 0
    assert data["summary"]["passed_domain_filter"] == 1
    assert data["candidates"]["greenhouse"] == [
        {"name": "acme", "board_token": "acme", "relevant_jobs": 4, "enabled": True}
    ]
    assert "passed_qa_filter" not in data["summary"]


def test_run_discovery_passes_active_domain_rules_to_verifier(monkeypatch, tmp_path):
    seen_rules = []
    monkeypatch.setattr(mod, "_load_catalog", lambda: {"greenhouse": []})
    monkeypatch.setattr(mod, "_discover_tokens_for_ats", lambda *_: ({"acme"}, 1, 0, False))

    def fake_verify_fn(_ats, rules):
        seen_rules.append(rules)
        return lambda token: mod.VerificationResult(
            ats="greenhouse",
            token=token,
            relevant_count=0,
            status="ok",
            http_status=200,
            error_kind=None,
            error_message=None,
        )

    monkeypatch.setattr(mod, "_verify_fn_for_ats", fake_verify_fn)
    mod.run_discovery(
        ats_list=["greenhouse"],
        min_relevant_jobs=3,
        keywords=["Data Engineer"],
        output_path=tmp_path / "candidates.json",
        domain_rules=DOMAIN_RULES,
        display_name="Data Engineering",
    )

    assert seen_rules == [DOMAIN_RULES]
