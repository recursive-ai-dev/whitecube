"""Keep every test away from the user's settings and OS keyring."""

import pytest

from prompt_manager import config
from prompt_manager.core import keychain


@pytest.fixture(scope="session")
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PyQt6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "APP_CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "CONFIG_FILE_PATH", tmp_path / "config" / "settings.json")
    monkeypatch.setattr(keychain, "VAULT_FILE_PATH", tmp_path / "config" / "vault.dat")
    monkeypatch.setattr(keychain, "_global_vault", keychain.KeyVault(use_keyring=False))
