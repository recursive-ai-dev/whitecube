"""Malformed optional settings must not prevent the desktop app from opening."""

import json

import pytest

from prompt_manager import config
from prompt_manager.core.keychain import get_key_vault


@pytest.mark.parametrize("value", [None, False, 42, "section", ["token"]])
def test_malformed_provider_section_shapes_have_defaults(value):
    config.ensure_directories()
    original = json.dumps({"github": value, "pollinations": value, "theme": "nord"})
    config.CONFIG_FILE_PATH.write_text(original)
    assert config.get_setting("custom_models", []) == []
    assert config.get_github_config()["token"] == ""
    assert config.get_pollinations_config()["api_key"] == ""
    assert not config.is_github_connected()
    assert config.CONFIG_FILE_PATH.read_text() == original
    config.set_github_config({"repo": "me/prompts"})
    config.set_pollinations_config({"model": "custom"})
    assert config.get_github_config()["repo"] == "me/prompts"
    assert config.get_pollinations_config()["model"] == "custom"
    assert config.get_theme_id() == "nord"


@pytest.mark.parametrize("value,default", [
    (None, []), ("ollama:model", []), ({}, []), ("0.7", 0.7),
    (True, 2048), (2.5, 2048), (float("inf"), 0.7), (float("nan"), 0.7),
    (10 ** 400, 0.7), (None, "default"), (42, "default"), ("false", False),
])
def test_generic_setting_invalid_types_use_default(value, default):
    config.ensure_directories()
    config.CONFIG_FILE_PATH.write_text(json.dumps({"example": value}))
    assert config.get_setting("example", default) == default


def test_generic_setting_preserves_valid_numeric_zero_and_empty_values():
    config.set_setting("integer", 0)
    config.set_setting("number", 0)
    config.set_setting("string", "")
    config.set_setting("boolean", False)
    assert config.get_setting("integer", 10) == 0
    assert config.get_setting("number", 0.7) == 0
    assert config.get_setting("string", "fallback") == ""
    assert config.get_setting("boolean", True) is False


def test_invalid_legacy_secret_type_does_not_break_migration():
    config.ensure_directories()
    config.CONFIG_FILE_PATH.write_text(json.dumps({"github": {"token": 123, "repo": "me/prompts"}}))
    assert config.get_github_config()["token"] == ""
    assert config.get_github_config()["repo"] == "me/prompts"
    assert "token" not in json.loads(config.CONFIG_FILE_PATH.read_text())["github"]


def test_github_connected_requires_boolean_true():
    config.set_github_config({"token": "secret", "connected": "false"})
    assert not config.is_github_connected()
    assert get_key_vault().get_api_key("github") == "secret"


def test_nonfinite_write_retains_prior_settings():
    config.set_setting("saved", "prior value")
    original = config.CONFIG_FILE_PATH.read_text()
    with pytest.raises(ValueError):
        config.set_setting("invalid", float("nan"))
    assert config.CONFIG_FILE_PATH.read_text() == original
