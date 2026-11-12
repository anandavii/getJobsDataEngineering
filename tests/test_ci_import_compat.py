"""
Verify tool modules import correctly under CI's sys.path.

CI runs `python tools/orchestrate.py` from the repo root, which puts `tools/`
on sys.path — NOT the repo root. Any `from tools.X import Y` style import
will fail in CI even if it works locally (where pytest adds the repo root).

This test replicates that environment by running the import in a subprocess
whose cwd is `tools/`, catching the exact class of error that broke CI.
"""

import pathlib
import subprocess
import sys

TOOLS_DIR = pathlib.Path(__file__).parent.parent / "tools"

# Modules that orchestrate.py imports at the top level — these must all be
# importable when `tools/` is the working directory (no repo root on sys.path).
_ENTRY_POINT_MODULES = [
    "fetch_jobs_catalog_adapters",
    "company_health",
    "health_state",
    "push_to_supabase",
    "domain_relevance",
    "role_horizon",
    "notify_webhook",
]


def _check_import(module: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=str(TOOLS_DIR),
    )


def test_catalog_adapters_importable_ci_style():
    """fetch_jobs_catalog_adapters must not use `from tools.X` style imports."""
    result = _check_import("fetch_jobs_catalog_adapters")
    assert result.returncode == 0, (
        "fetch_jobs_catalog_adapters failed CI-style import.\n"
        "Likely cause: `from tools.X import Y` — use `from X import Y` instead.\n"
        f"stderr:\n{result.stderr}"
    )


def test_all_tool_modules_importable_ci_style():
    """All top-level tool modules must be importable with only tools/ on sys.path."""
    failures = []
    for module in _ENTRY_POINT_MODULES:
        result = _check_import(module)
        if result.returncode != 0:
            failures.append(f"  {module}:\n{result.stderr.strip()}")

    assert not failures, (
        "The following modules failed CI-style import (use `from X` not `from tools.X`):\n" + "\n".join(failures)
    )
