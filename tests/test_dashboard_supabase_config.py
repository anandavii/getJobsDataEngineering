from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGES = ("index.html", "login.html", "rejected.html", "health.html")


def test_dashboard_pages_load_deployment_supabase_config() -> None:
    for page in PAGES:
        html = (ROOT / "docs" / page).read_text()

        assert 'src="supabase-config.js"' in html
        assert "window.SUPABASE_CONFIG" in html
        assert "supabase.createClient = () => createConfiguredClient" in html
