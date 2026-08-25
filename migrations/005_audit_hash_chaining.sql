CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE public.audit_log ADD COLUMN IF NOT EXISTS seq_id BIGSERIAL UNIQUE;
ALTER TABLE public.audit_log ADD COLUMN IF NOT EXISTS previous_hash VARCHAR(64);
ALTER TABLE public.audit_log ADD COLUMN IF NOT EXISTS record_hash VARCHAR(64);

CREATE TABLE IF NOT EXISTS public.audit_log_state (
    id INT PRIMARY KEY DEFAULT 1,
    last_hash VARCHAR(64)
);

INSERT INTO public.audit_log_state (id, last_hash) 
VALUES (1, '0000000000000000000000000000000000000000000000000000000000000000') 
ON CONFLICT DO NOTHING;

-- Replace the Trigger Function
CREATE OR REPLACE FUNCTION audit_trigger_func()
RETURNS TRIGGER AS $$
DECLARE
    current_uid UUID;
    prev_hash VARCHAR(64);
    new_hash VARCHAR(64);
    payload_text TEXT;
BEGIN
    BEGIN
        current_uid := auth.uid();
    EXCEPTION WHEN OTHERS THEN
        current_uid := NULL;
    END;

    -- Lock the state to serialize audit log entries and ensure strict cryptographic chain
    SELECT last_hash INTO prev_hash FROM public.audit_log_state WHERE id = 1 FOR UPDATE;

    IF (TG_OP = 'INSERT') THEN
        payload_text := prev_hash || TG_TABLE_NAME || TG_OP || COALESCE(current_uid::text, '') || NEW.id::text || COALESCE(NEW::text, '');
        new_hash := encode(digest(payload_text, 'sha256'), 'hex');

        INSERT INTO public.audit_log (table_name, record_id, action, actor, new_data, previous_hash, record_hash)
        VALUES (TG_TABLE_NAME, NEW.id, 'INSERT', current_uid, to_jsonb(NEW), prev_hash, new_hash);

    ELSIF (TG_OP = 'UPDATE') THEN
        payload_text := prev_hash || TG_TABLE_NAME || TG_OP || COALESCE(current_uid::text, '') || NEW.id::text || COALESCE(OLD::text, '') || COALESCE(NEW::text, '');
        new_hash := encode(digest(payload_text, 'sha256'), 'hex');

        INSERT INTO public.audit_log (table_name, record_id, action, actor, old_data, new_data, previous_hash, record_hash)
        VALUES (TG_TABLE_NAME, NEW.id, 'UPDATE', current_uid, to_jsonb(OLD), to_jsonb(NEW), prev_hash, new_hash);

    ELSIF (TG_OP = 'DELETE') THEN
        payload_text := prev_hash || TG_TABLE_NAME || TG_OP || COALESCE(current_uid::text, '') || OLD.id::text || COALESCE(OLD::text, '');
        new_hash := encode(digest(payload_text, 'sha256'), 'hex');

        INSERT INTO public.audit_log (table_name, record_id, action, actor, old_data, previous_hash, record_hash)
        VALUES (TG_TABLE_NAME, OLD.id, 'DELETE', current_uid, to_jsonb(OLD), prev_hash, new_hash);
    END IF;

    UPDATE public.audit_log_state SET last_hash = new_hash WHERE id = 1;

    IF (TG_OP = 'DELETE') THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
