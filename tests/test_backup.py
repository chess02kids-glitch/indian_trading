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

    # Fake the output file that subprocess would create
    import datetime

    with patch("os.getenv") as mock_getenv:

        def mock_env(key):
            if key == "DATABASE_URL":
                return "postgres://dummy"
            if key == "BACKUP_ENCRYPTION_KEY":
                return "f1K1fP3nF9mPzY2aD1qQ9wR7sT5vB2cU8xH4jG6kM3o="
            return None

        mock_getenv.side_effect = mock_env

        # We need to create a dummy file so the hashing/encryption logic doesn't fail
        # But we don't know the exact timestamp in the file.
        # We'll mock datetime.now() or just patch open

        with patch("scripts.backup_db.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.datetime(2024, 1, 1, 12, 0, 0)
            mock_dt.strftime = datetime.datetime.strftime

            dummy_file = tmp_path / "supabase_backup_20240101_120000.sql"
            dummy_file.write_text("SELECT 1;")

            run_backup(output_dir=tmp_path)

    assert "Backup completed successfully" in caplog.text
    assert "SHA256:" in caplog.text

    enc_file = dummy_file.with_suffix(".sql.enc")
    assert enc_file.exists(), "Encrypted backup file was not created"
    assert not dummy_file.exists(), "Unencrypted backup file was not deleted"
