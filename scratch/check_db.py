import os

import psycopg2


def run():
    db_url = os.getenv("DATABASE_URL")
    conn = psycopg2.connect(db_url)
    with conn.cursor() as cur:
        # Check tables
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public';"
        )
        tables = [r[0] for r in cur.fetchall()]
        print(f"Tables in public schema: {tables}")

        if "schema_migrations" in tables:
            cur.execute("SELECT version FROM public.schema_migrations;")
            migrations = [r[0] for r in cur.fetchall()]
            print(f"Applied migrations: {migrations}")
        else:
            print("schema_migrations table does not exist!")

    conn.close()


if __name__ == "__main__":
    run()
