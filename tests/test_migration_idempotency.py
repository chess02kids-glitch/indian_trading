import os
import subprocess

import pytest


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Requires Supabase DATABASE_URL"
)
def test_migrations_are_idempotent():
    # Run the migrations script twice. It should succeed both times.
    env = os.environ.copy()

    # First run
    res1 = subprocess.run(
        ["python", "migrations/run_migrations.py"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, f"First run failed: {res1.stderr}"

    # Second run
    res2 = subprocess.run(
        ["python", "migrations/run_migrations.py"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"Second run failed: {res2.stderr}"
    assert "No new migrations" in res2.stdout or "applied" in res2.stdout
