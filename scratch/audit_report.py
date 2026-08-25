import os
from datetime import datetime

import psycopg2


def generate_report():
    db_url = os.getenv("DATABASE_URL")
    conn = psycopg2.connect(db_url)

    with conn.cursor() as cur:
        # Check migrations
        cur.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        )
        migrations = cur.fetchall()

        # Check RLS
        cur.execute("""
            SELECT tablename, rowsecurity 
            FROM pg_tables 
            WHERE schemaname = 'public'
            ORDER BY tablename
        """)
        tables = cur.fetchall()

        cur.execute("""
            SELECT tablename, policyname, cmd, roles, qual, with_check 
            FROM pg_policies 
            WHERE schemaname = 'public'
            ORDER BY tablename, policyname
        """)
        policies = cur.fetchall()

    with open("AUDIT_REPORT.md", "w") as f:
        f.write("# Infrastructure Security & Migration Audit\n")
        f.write(f"Generated at: {datetime.now().isoformat()}\n\n")

        f.write("## Migrations\n")
        for m in migrations:
            f.write(f"- {m[0]} (Applied: {m[1]})\n")

        f.write("\n## Row Level Security\n")
        for t in tables:
            f.write(f"### Table: {t[0]}\n")
            f.write(f"- RLS Enabled: {'Yes' if t[1] else 'No'}\n")

            table_policies = [p for p in policies if p[0] == t[0]]
            for p in table_policies:
                f.write(f"  - Policy: {p[1]} (Cmd: {p[2]}, Roles: {p[3]})\n")

    conn.close()
    print("Generated AUDIT_REPORT.md")


if __name__ == "__main__":
    generate_report()
