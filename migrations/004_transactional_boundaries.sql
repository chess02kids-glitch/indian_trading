-- 1. Orders: add internal_order_id and idempotency_key
ALTER TABLE public.orders 
ADD COLUMN IF NOT EXISTS internal_order_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_orders_user_idempotency'
    ) THEN
        ALTER TABLE public.orders ADD CONSTRAINT uq_orders_user_idempotency UNIQUE (user_id, idempotency_key);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_orders_internal_id'
    ) THEN
        ALTER TABLE public.orders ADD CONSTRAINT uq_orders_internal_id UNIQUE (internal_order_id);
    END IF;
END $$;

-- 2. Order Attempts Table
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'attempt_status') THEN
        CREATE TYPE attempt_status AS ENUM ('PENDING', 'SUCCESS', 'FAILED');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS public.order_attempts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id UUID NOT NULL REFERENCES public.orders(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(255) NOT NULL,
    request_payload JSONB,
    status attempt_status NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(order_id, idempotency_key)
);

DROP TRIGGER IF EXISTS update_order_attempts_modtime ON public.order_attempts;
CREATE TRIGGER update_order_attempts_modtime BEFORE UPDATE ON public.order_attempts FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

ALTER TABLE public.order_attempts ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'Users can view their own order attempts' AND tablename = 'order_attempts'
    ) THEN
        CREATE POLICY "Users can view their own order attempts"
            ON public.order_attempts FOR SELECT
            USING (
                EXISTS (
                    SELECT 1 FROM public.orders o
                    WHERE o.id = order_attempts.order_id
                    AND o.user_id = auth.uid()
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'Users can insert their own order attempts' AND tablename = 'order_attempts'
    ) THEN
        CREATE POLICY "Users can insert their own order attempts"
            ON public.order_attempts FOR INSERT
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM public.orders o
                    WHERE o.id = order_attempts.order_id
                    AND o.user_id = auth.uid()
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE policyname = 'Users can update their own order attempts' AND tablename = 'order_attempts'
    ) THEN
        CREATE POLICY "Users can update their own order attempts"
            ON public.order_attempts FOR UPDATE
            USING (
                EXISTS (
                    SELECT 1 FROM public.orders o
                    WHERE o.id = order_attempts.order_id
                    AND o.user_id = auth.uid()
                )
            );
    END IF;
END $$;


-- 3. Fills / Executions unique constraint
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_executions_broker_id'
    ) THEN
        ALTER TABLE public.executions ADD CONSTRAINT uq_executions_broker_id UNIQUE (broker_execution_id);
    END IF;
END $$;

-- 4. Reconciliation
ALTER TABLE public.reconciliation_log
ADD COLUMN IF NOT EXISTS run_id VARCHAR(255),
ADD COLUMN IF NOT EXISTS matched BOOLEAN,
ADD COLUMN IF NOT EXISTS locked BOOLEAN DEFAULT false,
ADD COLUMN IF NOT EXISTS lock_reason TEXT,
ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(255),
ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_reconciliation_run_id'
    ) THEN
        ALTER TABLE public.reconciliation_log ADD CONSTRAINT uq_reconciliation_run_id UNIQUE (run_id);
    END IF;
END $$;
