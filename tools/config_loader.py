"""
Shared config loader for the job pipeline.

Usage
-----
Inside tools/ (e.g. orchestrate.py — sibling module):

    from config_loader import load_config, ConfigLoadError

From tests or repo root (via conftest.py which adds repo root to sys.path):

    from tools.config_loader import load_config, ConfigLoadError

In either case:

    try:
        config = load_config()
    except ConfigLoadError as e:
        log.error(str(e))
        sys.exit(1)

The returned dict has the same shape regardless of which files the values
came from, so all downstream code is unaffected.

Deep-merge semantics (see _deep_merge)
---------------------------------------
- Dicts are merged recursively; the domain file's keys win on conflict.
- Lists are REPLACED, not extended — the domain file's list wins entirely.
  This is intentional: qa.json's include_patterns fully replace
  any stub in the runtime config; there is no accidental concatenation.
- Scalar values (str, int, bool, None) are replaced; domain file wins.
- The runtime config's knobs (rejected_sample_limit, query_generation, etc.)
  are preserved as long as the domain file does NOT define the same leaf key.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class ConfigLoadError(Exception):
    """Raised when any config file is missing or contains invalid JSON."""


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge *override* into *base* and return a new dict.

    Rules:
    - If both values are dicts → recurse.
    - Otherwise (list, scalar, None) → override value replaces base value.

    Why lists are replaced instead of extended:
        A domain file owns its lists completely. Merging lists would
        silently combine supply_chain patterns with any leftover stubs in
        the runtime config, producing duplicates and hard-to-debug behavior.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_json(path: Path) -> object:
    """Load JSON from *path*, raising ConfigLoadError on any failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise ConfigLoadError(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(f"Config parse error in {path}: {exc}") from exc


def load_config(config_dir: Path = None) -> dict:
    """
    Load and merge all config files into a single dict.

    Parameters
    ----------
    config_dir : Path, optional
        Override the config directory. Defaults to <repo_root>/config.
        Pass a tmp_path fixture value in tests to avoid touching real files.

    Returns
    -------
    dict with keys: search, domain_filter, ranking, supabase, notification,
                    source_tiers, health, sources, sources_catalog, companies
    """
    base_dir = config_dir or CONFIG_DIR

    # 1. Load slim runtime config (knobs, weights, infrastructure settings)
    config = _load_json(base_dir / "job_search_config.json")

    # 2. Deep-merge the active domain config (keywords, taxonomy, filter patterns)
    #    Domain values win on conflict; runtime knobs without domain equivalents
    #    are preserved untouched.
    domain = config.get("active_domain", "data_engineering")
    domain_cfg = _load_json(base_dir / "domains" / f"{domain}.json")
    config = _deep_merge(config, domain_cfg)

    # 3. Load sources_catalog (full replacement — no partial version in runtime config)
    config["sources_catalog"] = _load_json(base_dir / "sources" / "sources_catalog.json")

    # 4. Load companies list (full replacement)
    config["companies"] = _load_json(base_dir / "sources" / "companies.json")

    return config
