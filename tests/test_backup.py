from unittest.mock import MagicMock, patch

from scripts.backup_db import run_backup


def test_backup_no_url(caplog):
    with patch("os.getenv", return_value=None):
        run_backup()
        assert "DATABASE_URL is not set" in caplog.text


@patch("subprocess.run")
def test_backup_success(mock_run, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO)
    mock_run.return_value = MagicMock(returncode=0)
    with patch("os.getenv", return_value="postgres://dummy"):
        run_backup(output_dir=tmp_path)

    assert "Backup completed successfully" in caplog.text
    mock_run.assert_called_once()

    # Check that pg_dump was called with the dummy URL
    cmd_args = mock_run.call_args[0][0]
    assert cmd_args[0] == "pg_dump"
    assert cmd_args[1] == "postgres://dummy"
