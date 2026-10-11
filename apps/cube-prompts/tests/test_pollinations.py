"""Tests for Pollinations.ai free text generation client and configuration."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from prompt_manager import config
from prompt_manager.integrations.pollinations_client import (
    FALLBACK_MODELS,
    PollinationsClient,
    PollinationsError,
)


class TestPollinations(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_file = Path(self.temp_dir.name) / "settings.json"
        self.config_dir = Path(self.temp_dir.name) / "config"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_client_init_and_defaults(self):
        client = PollinationsClient()
        self.assertEqual(client.api_key, "")
        self.assertEqual(client.timeout, 45)

        client_custom = PollinationsClient(api_key="pk_test123", timeout=60)
        self.assertEqual(client_custom.api_key, "pk_test123")
        self.assertEqual(client_custom.timeout, 60)

    def test_generate_empty_prompt_raises(self):
        client = PollinationsClient()
        with self.assertRaises(ValueError):
            client.generate("")
        with self.assertRaises(ValueError):
            client.generate("   \n\t  ")

    @patch("urllib.request.urlopen")
    def test_generate_get_mocked(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"Hello from Pollinations AI!"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = PollinationsClient()
        result = client.generate(
            prompt="Hello there",
            system_instruction="Be helpful",
            model="openai-fast",
            temperature=0.8,
        )
        self.assertEqual(result, "Hello from Pollinations AI!")
        mock_urlopen.assert_called_once()

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "GET")
        self.assertIn("openai-fast", req.full_url)
        self.assertIn("Be+helpful", req.full_url)

    @patch("urllib.request.urlopen")
    def test_generate_post_long_prompt_mocked(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "Processed long document successfully."}}]
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = PollinationsClient()
        long_prompt = "Analyze this: " + ("x" * 2000)
        result = client.generate(
            prompt=long_prompt,
            system_instruction="Be a summary expert",
            model="openai-fast",
        )
        self.assertEqual(result, "Processed long document successfully.")
        mock_urlopen.assert_called_once()

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")

    @patch("urllib.request.urlopen")
    def test_generate_rate_limit_429_raises_pollinations_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://text.pollinations.ai/test",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=io.BytesIO(b"Rate limit exceeded"),
        )
        client = PollinationsClient()
        with self.assertRaises(PollinationsError) as ctx:
            client.generate(prompt="Test prompt")
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("Rate limit", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_list_models_mocked(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([
            {"name": "openai-fast", "description": "Fast model"}
        ]).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = PollinationsClient()
        models = client.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["name"], "openai-fast")

    @patch("urllib.request.urlopen")
    def test_list_models_fallback_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        client = PollinationsClient()
        models = client.list_models()
        self.assertEqual(models, FALLBACK_MODELS)

    def test_pollinations_config_lifecycle(self):
        with patch.object(config, "CONFIG_FILE_PATH", self.settings_file), \
             patch.object(config, "APP_CONFIG_DIR", self.config_dir):
            cfg = config.get_pollinations_config()
            self.assertEqual(cfg["model"], config.DEFAULT_POLLINATIONS_MODEL)
            self.assertEqual(cfg["api_key"], "")

            config.set_pollinations_config({
                "model": "mistral",
                "api_key": "pk_sample_key",
                "temperature": 0.5,
            })
            updated = config.get_pollinations_config()
            self.assertEqual(updated["model"], "mistral")
            self.assertEqual(updated["api_key"], "pk_sample_key")
            self.assertEqual(updated["temperature"], 0.5)


if __name__ == "__main__":
    unittest.main()
