from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGES = ("index.html", "login.html", "rejected.html", "health.html")
CONFIG = ROOT / "docs" / "supabase-config.js"


def test_dashboard_pages_load_deployment_supabase_config() -> None:
    for page in PAGES:
        html = (ROOT / "docs" / page).read_text()

        assert 'src="supabase-config.js"' in html
        assert "window.SUPABASE_CONFIG" in html
        assert "supabase.createClient = () => createConfiguredClient" in html


def test_public_pages_config_uses_a_publishable_key() -> None:
    config = CONFIG.read_text()

    assert "window.SUPABASE_CONFIG" in config
    assert "publishableKey" in config
    assert "service_role" not in config
