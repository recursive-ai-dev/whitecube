"""Application configuration and XDG path management for Prompt Manager."""

import json
import os
import tempfile
from pathlib import Path

from prompt_manager import __version__

APP_NAME = "cube-prompts"
APP_DISPLAY_NAME = "Cube Prompts"

APP_VERSION = __version__

# XDG Base Directory specification compliance
XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))

APP_DATA_DIR = XDG_DATA_HOME / APP_NAME
APP_CONFIG_DIR = XDG_CONFIG_HOME / APP_NAME

DATABASE_PATH = APP_DATA_DIR / "prompts.db"
CONFIG_FILE_PATH = APP_CONFIG_DIR / "settings.json"

DEFAULT_TARGET_MODELS = [
    "General",
    "Claude Opus 5.5",
    "Claude Sonnet 5.5",
    "Claude Haiku 5.5",
    "GPT-4o",
    "GPT-4o mini",
    "Gemini 2.5 Pro",
    "Gemini 2.5 Flash",
    "DeepSeek R1",
    "DeepSeek V3",
    "Ollama / Local LLM",
]

DEFAULT_THEME_ID = "midnight_dark"


def ensure_directories() -> None:
    """Ensure data and configuration directories exist."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# ── Settings persistence (settings.json) ─────────────────────────────────

def _migrate_settings_credentials(data: dict) -> bool:
    """Move legacy secrets before rewriting settings; failed saves retain the file."""
    from prompt_manager.core.keychain import get_key_vault

    changed = False
    for section, field, provider in (
        ("github", "token", "github"),
        ("pollinations", "api_key", "pollinations"),
    ):
        block = data.get(section, {})
        if field in block:
            secret = block[field]
            if secret and not get_key_vault().get_api_key(provider):
                get_key_vault().set_api_key(provider, secret)
            block.pop(field)
            changed = True
    return changed


def _read_settings() -> dict:
    """Read settings and migrate legacy credentials into the OS keyring."""
    try:
        data = json.loads(CONFIG_FILE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if _migrate_settings_credentials(data):
        _write_settings(data)
    return data


def _write_settings(data: dict) -> None:
    """Write settings atomically with private permissions, excluding credentials."""
    _migrate_settings_credentials(data)
    ensure_directories()
    CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=CONFIG_FILE_PATH.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        temp_path.replace(CONFIG_FILE_PATH)
    finally:
        temp_path.unlink(missing_ok=True)


def get_theme_id() -> str:
    """Return persisted theme id, or DEFAULT_THEME_ID."""
    return _read_settings().get("theme", DEFAULT_THEME_ID)


def set_theme_id(theme_id: str) -> None:
    """Persist theme choice to settings.json."""
    data = _read_settings()
    data["theme"] = theme_id
    _write_settings(data)


def get_setting(key: str, default=None):
    return _read_settings().get(key, default)


def set_setting(key: str, value) -> None:
    data = _read_settings()
    data[key] = value
    _write_settings(data)


# ── GitHub integration persistence ────────────────────────────────────

def get_github_config() -> dict:
    """Return persisted GitHub config block (may be empty)."""
    data = _read_settings()
    gh = data.get("github", {})
    # Ensure defaults
    from prompt_manager.core.keychain import get_key_vault
    gh["token"] = get_key_vault().get_api_key("github")
    gh.setdefault("token_type", "")  # "pat" or "oauth"
    gh.setdefault("username", "")
    gh.setdefault("avatar_url", "")
    gh.setdefault("repo", "")  # full_name e.g. "owner/repo"
    gh.setdefault("branch", "main")
    gh.setdefault("file_path", "prompt-library.json")
    gh.setdefault("connected", False)
    gh.setdefault("oauth_client_id", "")
    gh.setdefault("last_sync", "")
    gh.setdefault("last_sync_sha", "")
    return gh


def set_github_config(patch: dict) -> None:
    """Merge patch into github config block and persist."""
    data = _read_settings()
    gh = data.get("github", {})
    from prompt_manager.core.keychain import get_key_vault
    patch = dict(patch)
    if "token" in patch:
        get_key_vault().set_api_key("github", patch.pop("token"))
    gh.update(patch)
    data["github"] = gh
    _write_settings(data)


def clear_github_config() -> None:
    """Remove GitHub connection (token + repo) but keep branch/path defaults."""
    data = _read_settings()
    from prompt_manager.core.keychain import get_key_vault
    get_key_vault().delete_api_key("github")
    gh = data.get("github", {})
    for key in ["token", "token_type", "username", "avatar_url", "repo", "connected", "last_sync", "last_sync_sha"]:
        gh.pop(key, None)
    gh["connected"] = False
    data["github"] = gh
    _write_settings(data)


def is_github_connected() -> bool:
    gh = get_github_config()
    return bool(gh.get("token") and gh.get("connected"))


# ── Pollinations AI configuration ─────────────────────────────────────

DEFAULT_POLLINATIONS_MODEL = "openai-fast"
POLLINATIONS_MODELS = [
    "openai-fast",
    "openai",
    "mistral",
    "qwen",
    "llama",
    "deepseek",
    "claude",
]


def get_pollinations_config() -> dict:
    """Return persisted Pollinations configuration."""
    data = _read_settings()
    pol = data.get("pollinations", {})
    pol.setdefault("model", DEFAULT_POLLINATIONS_MODEL)
    from prompt_manager.core.keychain import get_key_vault
    pol["api_key"] = get_key_vault().get_api_key("pollinations")
    pol.setdefault("temperature", 0.7)
    pol.setdefault("timeout", 45)
    return pol


def set_pollinations_config(patch: dict) -> None:
    """Merge patch into pollinations configuration and persist."""
    data = _read_settings()
    pol = data.get("pollinations", {})
    from prompt_manager.core.keychain import get_key_vault
    patch = dict(patch)
    if "api_key" in patch:
        get_key_vault().set_api_key("pollinations", patch.pop("api_key"))
    pol.update(patch)
    data["pollinations"] = pol
    _write_settings(data)

