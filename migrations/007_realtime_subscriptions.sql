-- 007_realtime_subscriptions.sql

-- 1. Create health_state table for realtime health synchronization
CREATE TABLE IF NOT EXISTS public.health_state (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_name VARCHAR(255) NOT NULL UNIQUE,
    status VARCHAR(50) NOT NULL,
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.health_state ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'health_state' AND policyname = 'Allow auth access to health_state'
    ) THEN
        CREATE POLICY "Allow auth access to health_state"
            ON public.health_state
            FOR ALL
            TO authenticated
            USING (true)
            WITH CHECK (true);
    END IF;
END
$$;

DROP TRIGGER IF EXISTS update_health_state_modtime ON public.health_state;
CREATE TRIGGER update_health_state_modtime BEFORE UPDATE ON public.health_state FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 2. Add Tables to the Supabase Realtime Publication
-- Note: 'supabase_realtime' publication is created automatically by Supabase.
-- If it doesn't exist (e.g. in local testing), we create it safely.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        CREATE PUBLICATION supabase_realtime;
    END IF;
END
$$;

-- Add all required tables for Realtime Phase B
ALTER PUBLICATION supabase_realtime ADD TABLE public.health_state;
ALTER PUBLICATION supabase_realtime ADD TABLE public.reconciliation_log;
ALTER PUBLICATION supabase_realtime ADD TABLE public.experiments;
ALTER PUBLICATION supabase_realtime ADD TABLE public.positions;
ALTER PUBLICATION supabase_realtime ADD TABLE public.orders;
