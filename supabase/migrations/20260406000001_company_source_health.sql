-- Create company_source_health table
CREATE TABLE IF NOT EXISTS public.company_source_health (
    company TEXT NOT NULL,
    ats_type TEXT NOT NULL,
    ats_token TEXT NOT NULL,
    job_board_url TEXT NOT NULL,
    last_nonempty_at TIMESTAMPTZ,
    days_empty INT NOT NULL DEFAULT 0,
    daily_counts JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'healthy' CHECK (status IN ('healthy', 'monitor', 'critical')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company, ats_type)
);

-- Create index on status for filtering
CREATE INDEX IF NOT EXISTS idx_company_source_health_status
    ON public.company_source_health(status);

-- Create index on days_empty for sorting
CREATE INDEX IF NOT EXISTS idx_company_source_health_days_empty
    ON public.company_source_health(days_empty DESC);

-- Enable RLS
ALTER TABLE public.company_source_health ENABLE ROW LEVEL SECURITY;

-- Allow anonymous SELECT (read-only dashboard access)
CREATE POLICY "Allow anonymous read access"
    ON public.company_source_health
    FOR SELECT
    TO anon
    USING (true);

-- Allow authenticated SELECT (read-only dashboard access)
CREATE POLICY "Allow authenticated read access"
    ON public.company_source_health
    FOR SELECT
    TO authenticated
    USING (true);

-- Allow service role full access (pipeline writes)
CREATE POLICY "Allow service role full access"
    ON public.company_source_health
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Auto-update updated_at on row changes (mirrors source_health_state pattern)
CREATE TRIGGER trg_company_source_health_updated_at
    BEFORE UPDATE ON public.company_source_health
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
