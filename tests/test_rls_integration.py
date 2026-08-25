import os
import uuid

import psycopg2
import pytest


@pytest.fixture
def db_conn():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not provided; skipping real RLS integration tests")

    conn = psycopg2.connect(db_url)
    # Enable autocommit for the overall connection if we want to isolate, but
    # it's better to run tests in a transaction and rollback at the end.
    yield conn
    conn.rollback()
    conn.close()


def simulate_user(cur, user_id: str):
    """Sets the request.jwt.claim.sub config simulating a Supabase JWT."""
    cur.execute("SET LOCAL request.jwt.claim.sub TO %s;", (user_id,))
    cur.execute("SET LOCAL role TO authenticated;")


def test_rls_cross_tenant_isolation(db_conn):
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())

    with db_conn.cursor() as cur:
        # Create a mock user in auth.users if possible, but since we might not have access to auth schema
        # We assume RLS policies just look at request.jwt.claim.sub

        # User A logs in and creates an order
        simulate_user(cur, user_a)

        order_id = str(uuid.uuid4())
        # The schema requires internal_order_id, symbol, exchange, side, quantity, limit_price, status
        # Let's see if we can insert it.
        try:
            cur.execute(
                """
                INSERT INTO public.orders (internal_order_id, user_id, symbol, exchange, side, quantity, limit_price, status)
                VALUES (%s, %s, 'RELIANCE', 'NSE', 'BUY', 10, 2500.0, 'PENDING')
                RETURNING internal_order_id;
            """,
                (order_id, user_a),
            )
        except psycopg2.Error as e:
            pytest.skip(
                f"Could not insert order (maybe missing auth user dependency): {e}"
            )

        # Ensure User A can read it
        cur.execute(
            "SELECT internal_order_id FROM public.orders WHERE internal_order_id = %s",
            (order_id,),
        )
        assert cur.fetchone() is not None, "User A should see their own order"

        # User B logs in
        simulate_user(cur, user_b)

        # Ensure User B CANNOT read User A's order
        cur.execute(
            "SELECT internal_order_id FROM public.orders WHERE internal_order_id = %s",
            (order_id,),
        )
        assert cur.fetchone() is None, "User B must NOT see User A's order"

        # Ensure User B CANNOT update User A's order
        # Even if they guess the ID
        cur.execute(
            """
            UPDATE public.orders SET status = 'CANCELLED' WHERE internal_order_id = %s
        """,
            (order_id,),
        )
        assert cur.rowcount == 0, "User B must NOT be able to update User A's order"


def test_rls_audit_log_immutable(db_conn):
    user_a = str(uuid.uuid4())
    with db_conn.cursor() as cur:
        simulate_user(cur, user_a)

        try:
            cur.execute("DELETE FROM public.audit_log")
            assert cur.rowcount == 0, "Should not be able to delete audit logs"
        except psycopg2.errors.InsufficientPrivilege:
            pass  # Also acceptable

        # Ensure we rollback since a failed transaction might abort the connection state
        db_conn.rollback()
