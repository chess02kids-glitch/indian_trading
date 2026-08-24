-- Enable Row Level Security (RLS) on all tables
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.api_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reconciliation_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.experiments ENABLE ROW LEVEL SECURITY;

-- Note: The `service_role` automatically bypasses RLS in Supabase. 
-- The policies below restrict access for `authenticated` and `anon` roles.

-------------------------------------------------------------------------------
-- USERS
-------------------------------------------------------------------------------
CREATE POLICY "Users can view their own profile" 
ON public.users FOR SELECT 
TO authenticated 
USING (auth.uid() = id);

CREATE POLICY "Users can update their own profile" 
ON public.users FOR UPDATE 
TO authenticated 
USING (auth.uid() = id);

-------------------------------------------------------------------------------
-- API SESSIONS
-------------------------------------------------------------------------------
CREATE POLICY "Users can view their own API sessions" 
ON public.api_sessions FOR SELECT 
TO authenticated 
USING (auth.uid() = user_id);

CREATE POLICY "Users can insert their own API sessions" 
ON public.api_sessions FOR INSERT 
TO authenticated 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete their own API sessions" 
ON public.api_sessions FOR DELETE 
TO authenticated 
USING (auth.uid() = user_id);

-------------------------------------------------------------------------------
-- ORDERS
-------------------------------------------------------------------------------
CREATE POLICY "Users can view their own orders" 
ON public.orders FOR SELECT 
TO authenticated 
USING (auth.uid() = user_id);

CREATE POLICY "Users can insert their own orders" 
ON public.orders FOR INSERT 
TO authenticated 
WITH CHECK (auth.uid() = user_id);

-- System handles most updates, but users can cancel their pending orders
CREATE POLICY "Users can update their own orders" 
ON public.orders FOR UPDATE 
TO authenticated 
USING (auth.uid() = user_id);

-------------------------------------------------------------------------------
-- EXECUTIONS
-------------------------------------------------------------------------------
-- Executions are inserted by the system, users can only view their own
CREATE POLICY "Users can view their own executions" 
ON public.executions FOR SELECT 
TO authenticated 
USING (
    order_id IN (
        SELECT id FROM public.orders WHERE user_id = auth.uid()
    )
);

-------------------------------------------------------------------------------
-- POSITIONS
-------------------------------------------------------------------------------
CREATE POLICY "Users can view their own positions" 
ON public.positions FOR SELECT 
TO authenticated 
USING (auth.uid() = user_id);

-------------------------------------------------------------------------------
-- RECONCILIATION LOG
-------------------------------------------------------------------------------
-- Only system/service_role can insert and view logs. 
-- Authenticated and anon users have no access (no policies defined for them).

-------------------------------------------------------------------------------
-- EXPERIMENTS
-------------------------------------------------------------------------------
-- All authenticated researchers can view experiments
CREATE POLICY "Authenticated users can view experiments" 
ON public.experiments FOR SELECT 
TO authenticated 
USING (true);
