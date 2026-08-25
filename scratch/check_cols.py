import os

import psycopg2

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute(
    "SELECT column_name FROM information_schema.columns WHERE table_name='audit_log'"
)
print([row[0] for row in cur.fetchall()])
