-- 006_research_infrastructure.sql

-- 1. DATASETS TABLE
CREATE TABLE IF NOT EXISTS public.datasets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dataset_name VARCHAR(255) NOT NULL,
    fingerprint VARCHAR(255) NOT NULL UNIQUE,
    ingestion_metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. UNIVERSE HISTORY TABLE
CREATE TABLE IF NOT EXISTS public.universe_history (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol VARCHAR(50) NOT NULL,
    index_name VARCHAR(100) NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_universe_history_symbol ON public.universe_history(symbol);
CREATE INDEX IF NOT EXISTS idx_universe_history_index ON public.universe_history(index_name);

-- 3. ENHANCE EXPERIMENTS WITH FINGERPRINTS
ALTER TABLE public.experiments 
ADD COLUMN IF NOT EXISTS dataset_fingerprint VARCHAR(255),
ADD COLUMN IF NOT EXISTS config_fingerprint VARCHAR(255),
ADD COLUMN IF NOT EXISTS code_fingerprint VARCHAR(255);

-- 4. RLS POLICIES FOR NEW TABLES
-- Ensure Row Level Security is active
ALTER TABLE public.datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.universe_history ENABLE ROW LEVEL SECURITY;

-- Provide SELECT/INSERT to authenticated users
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'datasets' AND policyname = 'Allow auth access to datasets'
    ) THEN
        CREATE POLICY "Allow auth access to datasets"
            ON public.datasets
            FOR ALL
            TO authenticated
            USING (true)
            WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'universe_history' AND policyname = 'Allow auth access to universe_history'
    ) THEN
        CREATE POLICY "Allow auth access to universe_history"
            ON public.universe_history
            FOR ALL
            TO authenticated
            USING (true)
            WITH CHECK (true);
    END IF;
END
$$;

-- 5. ATTACH UPDATED_AT TRIGGERS
DROP TRIGGER IF EXISTS update_datasets_modtime ON public.datasets;
CREATE TRIGGER update_datasets_modtime BEFORE UPDATE ON public.datasets FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_universe_history_modtime ON public.universe_history;
CREATE TRIGGER update_universe_history_modtime BEFORE UPDATE ON public.universe_history FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Add audit log triggers for new tables
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'audit_datasets_changes') THEN
        CREATE TRIGGER audit_datasets_changes
            AFTER INSERT OR UPDATE OR DELETE ON public.datasets
            FOR EACH ROW EXECUTE FUNCTION public.audit_trigger_func();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'audit_universe_history_changes') THEN
        CREATE TRIGGER audit_universe_history_changes
            AFTER INSERT OR UPDATE OR DELETE ON public.universe_history
            FOR EACH ROW EXECUTE FUNCTION public.audit_trigger_func();
    END IF;
END
$$;
