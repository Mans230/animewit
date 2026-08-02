import importlib
import os
import sys

import pytest

# Make the repo root importable (store.py, scraper.py, resolvers.py live there).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    """store module reloaded against a temporary DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import store

    importlib.reload(store)
    yield store
    importlib.reload(store)
