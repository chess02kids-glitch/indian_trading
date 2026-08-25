import os
import psycopg2

def run():
    db_url = os.getenv("DATABASE_URL")
    conn = psycopg2.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                version VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            INSERT INTO public.schema_migrations (version) VALUES 
            ('001_initial_schema.sql'), 
            ('002_rls_policies.sql'), 
            ('003_audit_log.sql'), 
            ('004_transactional_boundaries.sql'), 
            ('005_audit_hash_chaining.sql')
            ON CONFLICT DO NOTHING;
            """
        )
    conn.commit()
    conn.close()

if __name__ == "__main__":
    run()
