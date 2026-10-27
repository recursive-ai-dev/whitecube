"""Regression tests for cancellable comparisons and configurable model safety."""

import concurrent.futures
import io
import json
from threading import Event
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from prompt_manager.core.arena import run_arena_comparison
from prompt_manager.core.keychain import CredentialStoreError, KeyVault
from prompt_manager.integrations.llm_providers import (
    LLMClient, LLMRequest, LLMResponse, get_model_info, validate_base_url,
)


def json_response(data):
    response = MagicMock()
    response.read.return_value = json.dumps(data).encode()
    response.__enter__.return_value = response
    return response


@pytest.mark.parametrize("endpoint", [
    "file:///etc/passwd", "ftp://host", "https://user:password@host/v1",
    "https://host/v1?key=secret", "https://host/v1#fragment", "http://host:bad",
    "https://ho st/v1", "https:///v1", "", None,
])
def test_invalid_endpoint_rejected_without_echoing_input(endpoint):
    with pytest.raises(ValueError) as error:
        validate_base_url(endpoint)
    assert "password" not in str(error.value)
    assert "secret" not in str(error.value)


def test_reverse_proxy_endpoint_and_tagged_custom_models():
    assert validate_base_url(" http://127.0.0.1:8080/proxy/v1/ ") == "http://127.0.0.1:8080/proxy/v1"
    model = get_model_info("ollama:team/custom:latest")
    assert model.id == "ollama:team/custom:latest"
    assert model.provider == "ollama"
    assert model.is_free
    assert get_model_info("openai:my-company/model").is_free is False


@pytest.mark.parametrize("model_id", ["unknown:abc", "openai:", "openai:with space", "", None])
def test_invalid_model_id_becomes_actionable_response(model_id):
    response = LLMClient(KeyVault(use_keyring=False)).execute(LLMRequest(prompt="hello", model_id=model_id))
    assert not response.is_success
    assert response.error


def test_keyless_openai_compatible_endpoint_preserves_path_and_output_limit():
    vault = KeyVault(use_keyring=False)
    vault.set_provider_config("openai", {"base_url": "http://127.0.0.1:8080/custom/v1/"})
    with patch("urllib.request.urlopen", return_value=json_response({"choices": [{"message": {"content": "Local answer"}}]})) as open_url:
        response = LLMClient(vault).execute(LLMRequest(prompt="Hello", model_id="openai:local-model", max_tokens=321))
    assert response.is_success
    request = open_url.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8080/custom/v1/chat/completions"
    assert request.get_header("Authorization") is None
    assert json.loads(request.data)["max_tokens"] == 321


def test_ollama_output_limit_is_sent():
    vault = KeyVault(use_keyring=False)
    with patch("urllib.request.urlopen", return_value=json_response({"message": {"content": "Answer"}})) as open_url:
        response = LLMClient(vault).execute(LLMRequest(prompt="Hello", model_id="ollama:custom:tag", max_tokens=64))
    assert response.is_success
    assert json.loads(open_url.call_args.args[0].data)["options"]["num_predict"] == 64


@pytest.mark.parametrize("field,value", [("timeout", 0), ("timeout", float("nan")), ("temperature", 3), ("max_tokens", -1), ("max_tokens", 1.5)])
def test_invalid_request_stops_before_network(field, value):
    with patch("urllib.request.urlopen") as open_url:
        response = LLMClient(KeyVault(use_keyring=False)).execute(LLMRequest(prompt="Hello", **{field: value}))
    assert response.error
    open_url.assert_not_called()


@pytest.mark.parametrize("structured", [True, False])
def test_http_diagnostics_redact_configured_api_key(structured):
    vault = KeyVault(use_keyring=False)
    secret = "sk-my-key+with/special?characters"
    vault.set_api_key("openai", secret)
    diagnostic = f"Rejected key {secret} and {urllib.parse.quote(secret, safe='')}"
    body = json.dumps({"error": {"message": diagnostic}}) if structured else diagnostic
    error = urllib.error.HTTPError("https://api.openai.com/v1/chat/completions", 401, "Unauthorized", {}, io.BytesIO(body.encode()))
    with patch("urllib.request.urlopen", side_effect=error):
        response = LLMClient(vault).execute(LLMRequest(prompt="Hello", model_id="openai:gpt-4o"))
    assert "HTTP 401" in response.error
    assert secret not in response.error
    assert urllib.parse.quote(secret, safe="") not in response.error
    assert "[redacted]" in response.error


def test_empty_content_is_an_explicit_provider_error():
    with patch("prompt_manager.integrations.llm_providers.PollinationsClient.generate", return_value=""):
        response = LLMClient(KeyVault(use_keyring=False)).execute(LLMRequest(prompt="Hello"))
    assert "empty response" in response.error


def test_cancelled_comparison_returns_while_inflight_request_finishes():
    started = Event()
    release = Event()
    cancel = Event()

    class Client:
        def execute(self, request):
            started.set()
            release.wait(3)
            return LLMResponse(model_id=request.model_id, content="Late answer")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_arena_comparison, "hello", ["one"], client=Client(), cancel_event=cancel)
        try:
            assert started.wait(1)
            cancel.set()
            result = future.result(timeout=1)
            assert result.cancelled
            assert len(result.responses) == 1
            assert "Cancelled" in result.responses[0].error
            assert not release.is_set()
        finally:
            release.set()


def test_pre_cancelled_comparison_sends_no_requests():
    cancel = Event()
    cancel.set()
    client = MagicMock()
    result = run_arena_comparison("hello", ["one", "two"], client=client, cancel_event=cancel)
    client.execute.assert_not_called()
    assert result.cancelled
    assert [response.model_id for response in result.responses] == ["one", "two"]


def test_progress_preserves_duplicate_slots_and_max_tokens():
    received = []
    client = MagicMock()
    client.execute.side_effect = lambda request: LLMResponse(content="answer", model_id=request.model_id)
    result = run_arena_comparison("hello", ["one", "two", "one"], client=client,
                                  max_tokens=256, on_response=lambda index, response: received.append((index, response.model_id)))
    assert sorted(received) == [(0, "one"), (1, "two"), (2, "one")]
    assert [response.model_id for response in result.responses] == ["one", "two", "one"]
    assert all(call.args[0].max_tokens == 256 for call in client.execute.call_args_list)


def test_unexpected_custom_client_error_does_not_expose_credentials():
    client = MagicMock()
    client.execute.side_effect = RuntimeError("Authorization: secret-key")
    response = run_arena_comparison("hello", ["one"], client=client).responses[0]
    assert response.error
    assert "secret-key" not in response.error


def test_corrupt_private_credentials_are_retained(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('["not", "a", "vault"]')
    vault = KeyVault(private_file=path)
    with patch.object(vault, "_store", return_value=None):
        with pytest.raises(CredentialStoreError):
            vault.set_api_key("openai", "replacement")
    assert path.read_text() == '["not", "a", "vault"]'


def test_private_credentials_keep_all_concurrent_writes_with_private_modes(tmp_path):
    path = tmp_path / "config" / "credentials.json"
    vault = KeyVault(private_file=path)
    with patch.object(vault, "_store", return_value=None):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda index: vault.set_api_key(f"provider{index}", f"key{index}"), range(20)))
    data = json.loads(path.read_text())
    assert len(data) == 20
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_private_credential_directory_errors_are_actionable(tmp_path):
    parent = tmp_path / "file"
    parent.write_text("occupied")
    vault = KeyVault(private_file=parent / "credentials.json")
    with patch.object(vault, "_store", return_value=None):
        with pytest.raises(CredentialStoreError):
            vault.set_api_key("openai", "secret")
