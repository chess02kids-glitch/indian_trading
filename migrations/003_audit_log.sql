-- Create Audit Log Table
CREATE TABLE IF NOT EXISTS public.audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    table_name VARCHAR(255) NOT NULL,
    record_id UUID NOT NULL,
    action VARCHAR(50) NOT NULL,
    actor UUID, -- authenticated user performing action (if available via auth.uid())
    old_data JSONB,
    new_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Make Audit Log Immutable using RLS
ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Deny updates on audit_log" ON public.audit_log FOR UPDATE TO authenticated USING (false);
CREATE POLICY "Deny deletes on audit_log" ON public.audit_log FOR DELETE TO authenticated USING (false);

-- Audit Trigger Function
CREATE OR REPLACE FUNCTION audit_trigger_func()
RETURNS TRIGGER AS $$
DECLARE
    current_uid UUID;
BEGIN
    -- Attempt to get the Supabase auth user id, otherwise it might be a service_role or system process
    BEGIN
        current_uid := auth.uid();
    EXCEPTION WHEN OTHERS THEN
        current_uid := NULL;
    END;

    IF (TG_OP = 'INSERT') THEN
        INSERT INTO public.audit_log (table_name, record_id, action, actor, new_data)
        VALUES (TG_TABLE_NAME, NEW.id, 'INSERT', current_uid, to_jsonb(NEW));
        RETURN NEW;
    ELSIF (TG_OP = 'UPDATE') THEN
        INSERT INTO public.audit_log (table_name, record_id, action, actor, old_data, new_data)
        VALUES (TG_TABLE_NAME, NEW.id, 'UPDATE', current_uid, to_jsonb(OLD), to_jsonb(NEW));
        RETURN NEW;
    ELSIF (TG_OP = 'DELETE') THEN
        INSERT INTO public.audit_log (table_name, record_id, action, actor, old_data)
        VALUES (TG_TABLE_NAME, OLD.id, 'DELETE', current_uid, to_jsonb(OLD));
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Attach Audit Triggers to Critical Tables
DROP TRIGGER IF EXISTS audit_orders_trigger ON public.orders;
CREATE TRIGGER audit_orders_trigger 
AFTER INSERT OR UPDATE OR DELETE ON public.orders 
FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

DROP TRIGGER IF EXISTS audit_executions_trigger ON public.executions;
CREATE TRIGGER audit_executions_trigger 
AFTER INSERT OR UPDATE OR DELETE ON public.executions 
FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

DROP TRIGGER IF EXISTS audit_positions_trigger ON public.positions;
CREATE TRIGGER audit_positions_trigger 
AFTER INSERT OR UPDATE OR DELETE ON public.positions 
FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();

DROP TRIGGER IF EXISTS audit_api_sessions_trigger ON public.api_sessions;
CREATE TRIGGER audit_api_sessions_trigger 
AFTER INSERT OR UPDATE OR DELETE ON public.api_sessions 
FOR EACH ROW EXECUTE FUNCTION audit_trigger_func();
