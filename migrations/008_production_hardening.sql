-- 008_production_hardening.sql

-- 1. Enforce dataset fingerprint referential integrity
ALTER TABLE public.experiments 
ADD CONSTRAINT fk_experiments_dataset_fingerprint 
FOREIGN KEY (dataset_fingerprint) 
REFERENCES public.datasets(fingerprint) 
ON DELETE SET NULL;

-- 2. Enforce timestamp integrity for universe history
ALTER TABLE public.universe_history
ADD CONSTRAINT chk_universe_valid_dates 
CHECK (valid_to IS NULL OR valid_to >= valid_from);

-- 3. Ensure datasets.fingerprint is strictly immutable
CREATE OR REPLACE FUNCTION public.enforce_immutable_fingerprint()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.fingerprint IS DISTINCT FROM OLD.fingerprint THEN
        RAISE EXCEPTION 'Cannot modify immutable dataset fingerprint.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_immutable_fingerprint ON public.datasets;
CREATE TRIGGER trigger_immutable_fingerprint
    BEFORE UPDATE ON public.datasets
    FOR EACH ROW EXECUTE FUNCTION public.enforce_immutable_fingerprint();
