import os

import psycopg2


def force_apply(filepath):
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    conn.autocommit = True
    with conn.cursor() as cur:
        with open(filepath, "r") as f:
            sql = f.read()
        cur.execute(sql)
        print(f"Successfully force-applied {filepath}")
    conn.close()


force_apply("migrations/005_audit_hash_chaining.sql")
