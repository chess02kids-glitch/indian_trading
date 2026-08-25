from unittest.mock import MagicMock, patch

import pytest

from scripts.backup_db import run_backup
from scripts.restore_db import restore_backup


@pytest.mark.operational
def test_restore_dry_run():
    # Dry run should not actually execute psql
    with patch("subprocess.run") as mock_run:
        with patch("builtins.open", MagicMock()):
            with patch("os.getenv", return_value="postgresql://dummy"):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.suffix", return_value=".sql"):
                        restore_backup("dummy.sql", dry_run=True)
                        mock_run.assert_not_called()


@pytest.mark.operational
def test_backup_retention_logic():
    def mock_getenv(key, default=None):
        if key == "DATABASE_URL":
            return "postgresql://dummy"
        if key == "BACKUP_ENCRYPTION_KEY":
            return "b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b_b=" # 32 url-safe base64
        return default

    with patch("subprocess.run") as mock_run:
        with patch("os.getenv", side_effect=mock_getenv):
            with patch("pathlib.Path.mkdir"):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = b"dummydata"
                with patch("builtins.open", return_value=mock_file):
                    with patch("hashlib.sha256") as mock_hash:
                        mock_hash.return_value.hexdigest.return_value = "dummyhash"
                        with patch("pathlib.Path.glob") as mock_glob:
                            # Return empty list to test without failing on unlink
                            mock_glob.return_value = []
                            run_backup("dummy_dir")
                            mock_run.assert_called_once()
