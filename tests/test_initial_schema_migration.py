import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260101000000_initial_job_tracker_schema.sql"


def test_initial_schema_migration_provisions_pipeline_and_dashboard_tables() -> None:
    sql = MIGRATION.read_text().lower()

    assert "create table if not exists public.matched_jobs" in sql
    assert "create table if not exists public.rejected_jobs" in sql
    assert re.search(r"url\s+text\s+not\s+null\s+unique", sql)
    assert "alter table public.matched_jobs enable row level security" in sql
    assert "alter table public.rejected_jobs enable row level security" in sql
    assert "anon_select_matched_jobs" in sql
    assert "anon_select_rejected_jobs" in sql
