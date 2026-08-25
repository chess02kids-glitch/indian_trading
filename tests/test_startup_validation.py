import pytest
from unittest.mock import patch
from config.env_validator import validate_database_health

def test_startup_validation_no_db():
    with patch("os.getenv", return_value=""):
        # Should return silently if DATABASE_URL is absent
        validate_database_health()

@patch("psycopg2.connect")
def test_startup_validation_mocked(mock_connect):
    mock_conn = mock_connect.return_value
    mock_conn.info.ssl_in_use = True
    
    mock_cursor = mock_conn.cursor.return_value.__enter__.return_value
    # 1st fetchone: Schema tracking table exists -> True
    # 2nd fetchone: Count of migrations -> 5
    # 3rd-nth fetchones: RLS checks -> True
    mock_cursor.fetchone.side_effect = [
        [True],
        [5],
        [True], [True], [True], [True]
    ]
    
    with patch("os.getenv", return_value="postgresql://dummy"):
        validate_database_health()
        
    assert mock_cursor.execute.call_count >= 5
