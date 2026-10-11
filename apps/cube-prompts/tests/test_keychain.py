"""Credential persistence and fail-closed migration regression tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prompt_manager.core.keychain import (
    CredentialStoreError, KEYRING_SERVICE_NAME, KeyVault,
    _get_machine_seed, _xor_cipher,
)


class FakeNativeBackend:
    __module__ = "keyring.backends.SecretService"
    priority = 5

    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[service, name] = value

    def delete_password(self, service, name):
        del self.data[service, name]


class TestKeyVault(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "vault.dat"
        self.backend = FakeNativeBackend()
        patcher = patch("keyring.get_keyring", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vault = KeyVault(vault_path=self.path)

    def legacy(self, data):
        self.path.write_bytes(_xor_cipher(json.dumps(data).encode(), _get_machine_seed()))

    def test_credentials_persist_only_in_keyring(self):
        self.vault.set_api_key(" OpenAI ", " sk-test ")
        self.assertEqual(KeyVault(vault_path=self.path).get_api_key("openai"), "sk-test")
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])
        self.vault.delete_api_key("openai")
        self.vault.delete_api_key("openai")
        self.assertEqual(self.vault.get_api_key("openai"), "")

    def test_explicit_session_store_never_writes_disk(self):
        vault = KeyVault(use_keyring=False, vault_path=self.path)
        vault.set_api_key("openai", "temporary")
        self.assertEqual(vault.get_api_key("openai"), "temporary")
        self.assertEqual(KeyVault(use_keyring=False, vault_path=self.path).get_api_key("openai"), "")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.backend.data)

    def test_provider_config(self):
        self.vault.set_provider_config("ollama", {"base_url": "http://127.0.0.1:11434", "api_key": "secret"})
        cfg = self.vault.get_provider_config("ollama")
        self.assertEqual(cfg["base_url"], "http://127.0.0.1:11434")
        self.assertEqual(cfg["api_key"], "secret")
        self.assertFalse(self.path.exists())

    def test_backend_failure_does_not_write_fallback_or_expose_secret(self):
        with patch.object(self.backend, "set_password", side_effect=RuntimeError("secret-value")):
            with self.assertRaises(CredentialStoreError) as caught:
                self.vault.set_api_key("openai", "secret-value")
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_null_and_plaintext_backends_are_rejected(self):
        from keyring.backends.null import Keyring
        for backend in (Keyring(), object()):
            with self.subTest(backend=backend), patch("keyring.get_keyring", return_value=backend):
                with self.assertRaises(CredentialStoreError):
                    self.vault.set_api_key("openai", "secret")
        self.assertFalse(self.path.exists())

    def test_migrate_all_legacy_values_without_overwriting_keyring(self):
        self.backend.set_password(KEYRING_SERVICE_NAME, "api_key_openai", "new-key")
        self.legacy({"api_key_openai": "old-key", "api_key_gemini": "gem-key", "config_ollama": {"base_url": "http://local"}})
        self.assertEqual(self.vault.get_api_key("openai"), "new-key")
        self.assertEqual(self.vault.get_api_key("gemini"), "gem-key")
        self.assertEqual(self.vault.get_provider_config("ollama")["base_url"], "http://local")
        self.assertFalse(self.path.exists())

    def test_failed_migration_retains_file_and_can_be_retried(self):
        self.legacy({"api_key_openai": "first", "api_key_gemini": "second"})
        original = self.path.read_bytes()
        real_set = self.backend.set_password

        def fail_second(service, name, value):
            if name == "api_key_gemini":
                raise RuntimeError("locked")
            real_set(service, name, value)

        with patch.object(self.backend, "set_password", side_effect=fail_second):
            with self.assertRaises(CredentialStoreError):
                self.vault.get_api_key("openai")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.vault.get_api_key("gemini"), "second")
        self.assertFalse(self.path.exists())

    def test_silent_backend_write_failure_preserves_legacy_file(self):
        self.legacy({"api_key_openai": "secret"})
        with patch.object(self.backend, "set_password", return_value=None):
            with self.assertRaises(CredentialStoreError):
                self.vault.get_api_key("openai")
        self.assertTrue(self.path.exists())

    def test_corrupt_legacy_vault_is_not_destroyed(self):
        self.path.write_bytes(b"invalid")
        with self.assertRaises(CredentialStoreError):
            self.vault.set_api_key("openai", "replacement")
        self.assertEqual(self.path.read_bytes(), b"invalid")
