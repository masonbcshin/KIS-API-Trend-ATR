"""Unit tests for DB namespace mode resolution."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root for local imports.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from env import get_db_namespace_mode


def test_db_namespace_prefers_execution_mode_dry_run():
    with patch.dict(os.environ, {"EXECUTION_MODE": "DRY_RUN", "TRADING_MODE": "REAL"}, clear=True):
        assert get_db_namespace_mode() == "DRY_RUN"


def test_db_namespace_uses_trading_mode_cbt_when_execution_mode_missing():
    with patch.dict(os.environ, {"TRADING_MODE": "CBT"}, clear=True):
        assert get_db_namespace_mode() == "DRY_RUN"


def test_db_namespace_real_mode():
    with patch.dict(os.environ, {"EXECUTION_MODE": "REAL"}, clear=True):
        assert get_db_namespace_mode() == "REAL"


def test_db_namespace_paper_fallback():
    with patch.dict(os.environ, {}, clear=True):
        assert get_db_namespace_mode() == "PAPER"
