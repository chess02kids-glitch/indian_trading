-- Migration 009: Broker Operational Schema Integration

-- 1. Broker Accounts
CREATE TABLE IF NOT EXISTS public.broker_accounts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    broker VARCHAR(50) NOT NULL,
    environment VARCHAR(50) NOT NULL DEFAULT 'SANDBOX',
    status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    account_identifier VARCHAR(100),
    last_health_check_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, broker, environment)
);

CREATE TRIGGER update_broker_accounts_modtime 
BEFORE UPDATE ON public.broker_accounts 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 2. Broker Sessions
CREATE TABLE IF NOT EXISTS public.broker_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    broker_account_id UUID NOT NULL REFERENCES public.broker_accounts(id) ON DELETE CASCADE,
    token_status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    issued_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    last_refresh_at TIMESTAMPTZ,
    last_authenticated_at TIMESTAMPTZ,
    reauth_required BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER update_broker_sessions_modtime 
BEFORE UPDATE ON public.broker_sessions 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 3. Broker Health Events
CREATE TABLE IF NOT EXISTS public.broker_health_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    broker_account_id UUID NOT NULL REFERENCES public.broker_accounts(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. Extend Orders for broker integration
ALTER TABLE public.orders 
ADD COLUMN IF NOT EXISTS environment VARCHAR(50) DEFAULT 'SANDBOX',
ADD COLUMN IF NOT EXISTS strategy_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS hypothesis_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS portfolio_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS risk_decision_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

-- 5. Extend Executions for broker fills
ALTER TABLE public.executions
ADD COLUMN IF NOT EXISTS fees DECIMAL DEFAULT 0.0;

-- 6. Extend Reconciliation for detailed broker state
ALTER TABLE public.reconciliation_log
ADD COLUMN IF NOT EXISTS expected_state_hash VARCHAR(255),
ADD COLUMN IF NOT EXISTS broker_state_hash VARCHAR(255),
ADD COLUMN IF NOT EXISTS mismatch_count INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS run_type VARCHAR(50) DEFAULT 'EOD';

-- 7. RLS Policies
ALTER TABLE public.broker_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.broker_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.broker_health_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    -- Broker Accounts RLS
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Users can view their own broker accounts' AND tablename = 'broker_accounts') THEN
        CREATE POLICY "Users can view their own broker accounts" ON public.broker_accounts FOR SELECT USING (user_id = auth.uid());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Service role can manage broker accounts' AND tablename = 'broker_accounts') THEN
        CREATE POLICY "Service role can manage broker accounts" ON public.broker_accounts USING (true) WITH CHECK (true);
    END IF;

    -- Broker Sessions RLS
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Users can view their own broker sessions' AND tablename = 'broker_sessions') THEN
        CREATE POLICY "Users can view their own broker sessions" ON public.broker_sessions FOR SELECT USING (
            EXISTS (SELECT 1 FROM public.broker_accounts a WHERE a.id = broker_sessions.broker_account_id AND a.user_id = auth.uid())
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Service role can manage broker sessions' AND tablename = 'broker_sessions') THEN
        CREATE POLICY "Service role can manage broker sessions" ON public.broker_sessions USING (true) WITH CHECK (true);
    END IF;

    -- Broker Health Events RLS
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Users can view their own health events' AND tablename = 'broker_health_events') THEN
        CREATE POLICY "Users can view their own health events" ON public.broker_health_events FOR SELECT USING (
            EXISTS (SELECT 1 FROM public.broker_accounts a WHERE a.id = broker_health_events.broker_account_id AND a.user_id = auth.uid())
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Service role can insert health events' AND tablename = 'broker_health_events') THEN
        CREATE POLICY "Service role can insert health events" ON public.broker_health_events FOR INSERT WITH CHECK (true);
    END IF;
END $$;

-- 8. Realtime (Add new tables to logical replication)
ALTER PUBLICATION supabase_realtime ADD TABLE public.broker_health_events;
