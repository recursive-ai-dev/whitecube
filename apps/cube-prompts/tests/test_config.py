"""Tests for configuration and settings persistence."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prompt_manager import config


class TestConfig(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_file = Path(self.temp_dir.name) / "settings.json"
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.config_dir = Path(self.temp_dir.name) / "config"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_theme_get_and_set(self):
        with patch.object(config, "CONFIG_FILE_PATH", self.settings_file), \
             patch.object(config, "APP_CONFIG_DIR", self.config_dir):
            self.assertEqual(config.get_theme_id(), config.DEFAULT_THEME_ID)
            config.set_theme_id("dracula")
            self.assertEqual(config.get_theme_id(), "dracula")

    def test_github_config_lifecycle(self):
        with patch.object(config, "CONFIG_FILE_PATH", self.settings_file), \
             patch.object(config, "APP_CONFIG_DIR", self.config_dir):
            gh = config.get_github_config()
            self.assertEqual(gh["token"], "")
            self.assertFalse(config.is_github_connected())

            config.set_github_config({
                "token": "ghp_secrettoken123",
                "token_type": "pat",
                "username": "octocat",
                "repo": "octocat/my-prompts",
                "connected": True,
            })
            self.assertTrue(config.is_github_connected())
            gh_updated = config.get_github_config()
            self.assertEqual(gh_updated["username"], "octocat")
            self.assertEqual(gh_updated["repo"], "octocat/my-prompts")

            config.clear_github_config()
            self.assertFalse(config.is_github_connected())
            gh_cleared = config.get_github_config()
            self.assertEqual(gh_cleared["token"], "")
            self.assertFalse(gh_cleared["connected"])

    def test_generic_setting(self):
        with patch.object(config, "CONFIG_FILE_PATH", self.settings_file), \
             patch.object(config, "APP_CONFIG_DIR", self.config_dir):
            self.assertIsNone(config.get_setting("custom_key"))
            self.assertEqual(config.get_setting("custom_key", "default_val"), "default_val")
            config.set_setting("custom_key", 42)
            self.assertEqual(config.get_setting("custom_key"), 42)


if __name__ == "__main__":
    unittest.main()


def test_legacy_credentials_migrate_out_of_settings():
    import json
    from prompt_manager.core.keychain import get_key_vault

    config.ensure_directories()
    config.CONFIG_FILE_PATH.write_text(json.dumps({
        "theme": "dracula", "github": {"token": "ghp_legacy", "repo": "me/prompts"},
        "pollinations": {"api_key": "pk_legacy", "model": "mistral"},
    }))
    assert config.get_github_config()["token"] == "ghp_legacy"
    assert config.get_pollinations_config()["api_key"] == "pk_legacy"
    assert get_key_vault().get_api_key("github") == "ghp_legacy"
    stored = json.loads(config.CONFIG_FILE_PATH.read_text())
    assert "token" not in stored["github"]
    assert "api_key" not in stored["pollinations"]
    assert stored["theme"] == "dracula"
    assert stored["github"]["repo"] == "me/prompts"


def test_failed_credential_migration_preserves_settings():
    from prompt_manager.core.keychain import CredentialStoreError, get_key_vault
    import pytest

    config.ensure_directories()
    original = '{"github": {"token": "ghp_legacy"}}'
    config.CONFIG_FILE_PATH.write_text(original)
    with patch.object(get_key_vault(), "set_api_key", side_effect=CredentialStoreError("locked")):
        with pytest.raises(CredentialStoreError):
            config.get_github_config()
    assert config.CONFIG_FILE_PATH.read_text() == original


def test_new_settings_and_status_never_persist_tokens():
    import json

    patch_data = {"token": "ghp_secret", "connected": True}
    config.set_github_config(patch_data)
    config.set_pollinations_config({"api_key": "pk_secret", "model": "mistral"})
    config.set_theme_id("nord")
    stored = config.CONFIG_FILE_PATH.read_text()
    assert "ghp_secret" not in stored
    assert "pk_secret" not in stored
    assert "token" not in json.loads(stored)["github"]
    assert patch_data["token"] == "ghp_secret"
    config.set_github_config({"repo": "me/prompts"})
    assert config.get_github_config()["token"] == "ghp_secret"
    config.clear_github_config()
    assert config.get_github_config()["token"] == ""


def test_failed_disconnect_keeps_connection_metadata():
    from prompt_manager.core.keychain import CredentialStoreError, get_key_vault
    import pytest

    config.set_github_config({"token": "ghp_secret", "repo": "me/prompts", "connected": True})
    original = config.CONFIG_FILE_PATH.read_bytes()
    with patch.object(get_key_vault(), "delete_api_key", side_effect=CredentialStoreError("locked")):
        with pytest.raises(CredentialStoreError):
            config.clear_github_config()
    assert config.CONFIG_FILE_PATH.read_bytes() == original
    assert config.get_github_config()["token"] == "ghp_secret"


def test_settings_file_has_private_permissions():
    import os
    import stat
    import pytest

    if os.name == "nt":
        pytest.skip("POSIX permissions")
    config.set_theme_id("nord")
    assert stat.S_IMODE(config.CONFIG_FILE_PATH.stat().st_mode) == 0o600
