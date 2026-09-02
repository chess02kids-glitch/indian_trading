"""Pytest root conftest: make the repository importable without installation.

CI installs the package; this keeps ``pytest`` runnable from a bare checkout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def isolated_system_state(tmp_path, monkeypatch):
    """Give every test its own persisted system-state file.

    AUDIT-027 / AUDIT-021.  ``datahub.state`` persists the operator kill switch
    and (since AUDIT-021) the last automatic protective risk state into
    ``var/system_state.json``.  That file lives *outside* any test's ``tmp_path``,
    so before this fixture existed:

    * a test that armed the switch (or tripped a protective state) leaked it into
      every test that ran afterwards, in the same session and — because the file
      is committed-adjacent state — in the next session too;
    * a full ``pytest`` run dirtied the working tree.

    Pointing ``QUANT_STATE_FILE`` at a per-test temporary file makes the
    kill-switch and risk-latch behaviour deterministic and isolated.  A test
    that needs to exercise the real file can always override the variable
    itself.
    """
    state_file = tmp_path / "system_state.json"
    monkeypatch.setenv("QUANT_STATE_FILE", str(state_file))
    yield state_file


@pytest.fixture(autouse=True)
def isolated_derived_data(tmp_path, monkeypatch):
    """Keep tests from writing into the repository's committed data tree.

    AUDIT-027.  A plain ``pytest tests/`` used to leave
    ``data/quant.duckdb`` and ``data/snapshots/test_snap.parquet`` modified in
    the working tree, and — because the suite *generates cases from the
    contents of that database* — the collected test count differed between a
    clean checkout (1293) and a dirty one (1411).  A test run was therefore not
    reproducible.

    Every test gets a per-test temporary data directory, applied **twice**:

    * ``QUANT_DATA_DIR`` in the environment, for code that builds its own
      :class:`~config.settings.StorageConfig`;
    * ``settings.storage.data_dir`` itself, because ``settings`` is a
      module-level singleton whose field default was already evaluated at
      import time — setting only the environment is not enough (this is the
      bug the fixture was written for and did not actually fix).

    Paths that hard-code ``data/`` keep doing so, so this is a safety net
    rather than a claim that the problem is fully solved.
    """
    from config.settings import settings

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QUANT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings.storage, "data_dir", data_dir)
    previous = os.getcwd()
    yield data_dir
    os.chdir(previous)
