CREATE TABLE IF NOT EXISTS source_health_state (
    source                TEXT PRIMARY KEY,
    runs                  INTEGER DEFAULT 0,
    successes             INTEGER DEFAULT 0,
    failures              INTEGER DEFAULT 0,
    empties               INTEGER DEFAULT 0,
    avg_duration_s        FLOAT DEFAULT 0.0,
    last_success_utc      TIMESTAMPTZ,
    consecutive_failures  INTEGER DEFAULT 0,
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_source_health_updated_at
BEFORE UPDATE ON source_health_state
FOR EACH ROW EXECUTE FUNCTION update_updated_at();

ALTER TABLE source_health_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_select_health" ON source_health_state
  FOR SELECT TO anon USING (true);
