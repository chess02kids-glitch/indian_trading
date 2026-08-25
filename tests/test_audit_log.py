import pytest
from unittest.mock import patch, MagicMock
from scripts.verify_audit_log import verify_audit_log


@pytest.mark.operational
def test_verify_audit_chain_empty():
    with patch("psycopg2.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Simulate empty audit_log, and state 1 has genesis hash
        def fetchall_side_effect():
            return []

        def fetchone_side_effect():
            return {
                "last_hash": "0000000000000000000000000000000000000000000000000000000000000000"
            }

        mock_cursor.fetchall.side_effect = fetchall_side_effect
        mock_cursor.fetchone.side_effect = fetchone_side_effect

        with patch("os.getenv", return_value="postgresql://dummy"):
            verify_audit_log() # Should not raise SystemExit
