import os

import psycopg2


def run_rls_audit():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set")
        return

    conn = psycopg2.connect(db_url)
    with conn.cursor() as cur:
        # Check which tables have RLS enabled
        cur.execute("""
            SELECT relname, relrowsecurity 
            FROM pg_class 
            WHERE relnamespace = 'public'::regnamespace 
              AND relkind = 'r';
        """)
        tables = cur.fetchall()

        print("--- RLS Audit ---")
        failed = False
        for table, rls_enabled in tables:
            # We assume schema_migrations doesn't strictly need RLS, but standard is to check all
            if table == "schema_migrations":
                continue

            print(f"Table: {table}")
            if not rls_enabled:
                print(f"  [!] WARNING: RLS is NOT enabled for {table}")
                failed = True
            else:
                print("  [x] RLS is enabled")

                # Check policies for this table
                cur.execute(
                    """
                    SELECT polname, polcmd, polroles, polqual, polwithcheck 
                    FROM pg_policy 
                    WHERE polrelid = %s::regclass;
                """,
                    (table,),
                )
                policies = cur.fetchall()
                if not policies:
                    print(
                        f"  [!] WARNING: No policies defined for {table} despite RLS being enabled (Deny All by default)"
                    )
                for pol in policies:
                    print(f"      Policy: {pol[0]} | Cmd: {pol[1]}")
                    # In a real environment we would regex the polqual for auth.uid() usage

        if failed:
            print("\nAudit FAILED: Some tables lack RLS.")
            exit(1)
        else:
            print("\nAudit PASSED: All primary tables enforce RLS.")


if __name__ == "__main__":
    run_rls_audit()
