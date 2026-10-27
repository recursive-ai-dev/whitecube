"""Unified Multi-Provider LLM Inference Engine for Prompt Manager.

Supports:
- Pollinations.ai (Free, Zero setup)
- OpenAI (GPT-4o, GPT-4o-mini, o1-mini, o3-mini)
- Anthropic (Claude Opus 5.5, Claude Sonnet 5.5, Claude Haiku 5.5)
- Google Gemini (Gemini 2.5 Pro, Gemini 2.5 Flash, Gemini 2.0 Flash)
- Ollama (Local inference: Llama 3.2, DeepSeek-R1, Mistral, Qwen, etc.)
- OpenRouter (Unified routing aggregator)

Zero external pip dependencies: Built entirely with Python standard library urllib & json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from prompt_manager.core.keychain import get_key_vault
from prompt_manager.core.token_counter import count_bpe_tokens
from prompt_manager.integrations.pollinations_client import PollinationsClient


@dataclass
class ModelInfo:
    """Metadata, context limits, and token pricing for an LLM."""

    id: str
    display_name: str
    provider: str  # "pollinations", "openai", "anthropic", "gemini", "ollama", "openrouter"
    context_window: int = 128000
    cost_per_1k_input: float = 0.0  # USD
    cost_per_1k_output: float = 0.0  # USD
    is_free: bool = False
    description: str = ""


# ── Built-in Catalog of Models & Pricing ────────────────────────────────

MODEL_CATALOG: List[ModelInfo] = [
    # Free / Pollinations
    ModelInfo(
        id="pollinations:openai-fast",
        display_name="Pollinations: OpenAI Fast",
        provider="pollinations",
        is_free=True,
        description="Free fast OpenAI model via Pollinations.ai (No key required)",
    ),
    ModelInfo(
        id="pollinations:claude",
        display_name="Pollinations: Claude",
        provider="pollinations",
        is_free=True,
        description="Free Claude compatible model via Pollinations.ai",
    ),
    ModelInfo(
        id="pollinations:deepseek",
        display_name="Pollinations: DeepSeek R1",
        provider="pollinations",
        is_free=True,
        description="Free DeepSeek reasoning model via Pollinations.ai",
    ),
    ModelInfo(
        id="pollinations:mistral",
        display_name="Pollinations: Mistral",
        provider="pollinations",
        is_free=True,
        description="Free Mistral model via Pollinations.ai",
    ),
    # OpenAI
    ModelInfo(
        id="openai:gpt-4o",
        display_name="OpenAI: GPT-4o",
        provider="openai",
        context_window=128000,
        cost_per_1k_input=0.0025,
        cost_per_1k_output=0.0100,
        description="Flagship multimodal omni model with high intelligence",
    ),
    ModelInfo(
        id="openai:gpt-4o-mini",
        display_name="OpenAI: GPT-4o Mini",
        provider="openai",
        context_window=128000,
        cost_per_1k_input=0.00015,
        cost_per_1k_output=0.00060,
        description="Fast, cost-efficient model for focused lightweight tasks",
    ),
    ModelInfo(
        id="openai:o3-mini",
        display_name="OpenAI: o3-mini",
        provider="openai",
        context_window=200000,
        cost_per_1k_input=0.0011,
        cost_per_1k_output=0.0044,
        description="High-speed reasoning model specialized in STEM & code",
    ),
    # Anthropic
    ModelInfo(
        id="anthropic:claude-opus-5-5",
        display_name="Anthropic: Claude Opus 5.5",
        provider="anthropic",
        context_window=1000000,
        cost_per_1k_input=0.0040,
        cost_per_1k_output=0.0200,
        description="Most capable Opus model for complex reasoning and agentic coding",
    ),
    ModelInfo(
        id="anthropic:claude-sonnet-5-5",
        display_name="Anthropic: Claude Sonnet 5.5",
        provider="anthropic",
        context_window=1000000,
        cost_per_1k_input=0.0020,
        cost_per_1k_output=0.0100,
        description="Fast, capable everyday model for coding and writing",
    ),
    ModelInfo(
        id="anthropic:claude-haiku-5-5",
        display_name="Anthropic: Claude Haiku 5.5",
        provider="anthropic",
        context_window=1000000,
        cost_per_1k_input=0.0001,
        cost_per_1k_output=0.0005,
        description="Fastest, lowest-cost Claude model",
    ),
    # Google Gemini
    ModelInfo(
        id="gemini:gemini-2.5-pro",
        display_name="Gemini: 2.5 Pro",
        provider="gemini",
        context_window=1000000,
        cost_per_1k_input=0.00125,
        cost_per_1k_output=0.0050,
        description="Google's advanced frontier model with deep reasoning",
    ),
    ModelInfo(
        id="gemini:gemini-2.5-flash",
        display_name="Gemini: 2.5 Flash",
        provider="gemini",
        context_window=1000000,
        cost_per_1k_input=0.000075,
        cost_per_1k_output=0.00030,
        description="Ultra-fast, cost-effective multimodal model",
    ),
    # Ollama / Local
    ModelInfo(
        id="ollama:llama3.2",
        display_name="Ollama: Llama 3.2 (Local)",
        provider="ollama",
        is_free=True,
        description="Local inference via Ollama server (100% private, offline)",
    ),
    ModelInfo(
        id="ollama:deepseek-r1",
        display_name="Ollama: DeepSeek R1 (Local)",
        provider="ollama",
        is_free=True,
        description="Local DeepSeek reasoning model on Ollama",
    ),
    ModelInfo(
        id="ollama:mistral",
        display_name="Ollama: Mistral (Local)",
        provider="ollama",
        is_free=True,
        description="Local Mistral 7B on Ollama",
    ),
    # OpenRouter
    ModelInfo(
        id="openrouter:openrouter/auto",
        display_name="OpenRouter: Auto Router",
        provider="openrouter",
        cost_per_1k_input=0.002,
        cost_per_1k_output=0.008,
        description="Auto-routes to the most cost-effective top model",
    ),
]


SUPPORTED_PROVIDERS = {"pollinations", "openai", "anthropic", "gemini", "ollama", "openrouter"}


def get_model_info(model_id: str) -> ModelInfo:
    """Resolve a built-in model or a custom ``provider:model`` identifier.

    Custom IDs retain slashes and additional colons (for example Ollama tags).
    Their pricing is unknown; zero is an estimate, not a promise of free use.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("Choose a model or enter provider:model.")
    model_id = model_id.strip()
    if len(model_id) > 512 or any(char.isspace() or ord(char) < 32 for char in model_id):
        raise ValueError("Model IDs must be at most 512 characters without whitespace.")
    for model in MODEL_CATALOG:
        if model.id == model_id or model.id.split(":", 1)[-1] == model_id:
            return model
    provider, name = model_id.split(":", 1) if ":" in model_id else ("openai", model_id)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported provider. Use openai, anthropic, gemini, ollama, openrouter or pollinations.")
    if not name:
        raise ValueError("Enter a model name after the provider and colon.")
    return ModelInfo(id=f"{provider}:{name}", display_name=f"{provider.title()}: {name}",
                     provider=provider, is_free=provider == "ollama")


def validate_base_url(base_url: str) -> str:
    """Validate an explicitly configured HTTP endpoint without disclosing it.

    Keep path prefixes for reverse proxies, but reject credentials and queries:
    authentication belongs in the API key field rather than the URL.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Enter an HTTP or HTTPS endpoint.")
    base_url = base_url.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        _ = parsed.port  # Validate malformed ports as well as hostnames.
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or any(char.isspace() or ord(char) < 32 for char in base_url)):
            raise ValueError
    except ValueError:
        raise ValueError("Use an HTTP or HTTPS endpoint without credentials, query parameters or a fragment.") from None
    return base_url


def list_models_by_provider(provider: Optional[str] = None) -> List[ModelInfo]:
    """List available models, optionally filtered by provider."""
    if not provider or provider == "all":
        return list(MODEL_CATALOG)
    return [m for m in MODEL_CATALOG if m.provider == provider.lower()]


def discover_ollama_models(base_url: str = "http://localhost:11434") -> List[ModelInfo]:
    """Query local Ollama instance for installed models."""
    discovered = []
    try:
        url = f"{validate_base_url(base_url)}/api/tags"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("models", []):
                name = item.get("name", "")
                if name:
                    discovered.append(
                        ModelInfo(
                            id=f"ollama:{name}",
                            display_name=f"Ollama: {name} (Local)",
                            provider="ollama",
                            is_free=True,
                            description=f"Local Ollama model: {name}",
                        )
                    )
    except Exception:
        pass
    return discovered


# ── Request / Response Payloads ──────────────────────────────────────────


@dataclass
class LLMRequest:
    """Standardized LLM inference request."""

    prompt: str
    system_instruction: str = ""
    model_id: str = "pollinations:openai-fast"
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    timeout: int = 45


@dataclass
class LLMResponse:
    """Standardized LLM inference response with performance and cost telemetry."""

    content: str = ""
    elapsed_seconds: float = 0.0
    model_id: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None and bool(self.content)


# ── Provider Implementations ────────────────────────────────────────────


class LLMClient:
    """Unified client orchestrating requests to OpenAI, Anthropic, Gemini, Ollama, and Pollinations."""

    def __init__(self, key_vault=None):
        self.vault = key_vault or get_key_vault()

    def execute(self, request: LLMRequest) -> LLMResponse:
        """Execute request against target provider with automatic cost and token calculation."""
        start_time = time.monotonic()
        provider = ""
        prompt_tokens = 0
        try:
            model_info = get_model_info(request.model_id)
            provider = model_info.provider
            raw_model_name = model_info.id.split(":", 1)[-1]
            if not isinstance(request.prompt, str) or not request.prompt.strip():
                raise ValueError("Prompt cannot be empty.")
            if (not isinstance(request.timeout, (int, float)) or isinstance(request.timeout, bool)
                    or not math.isfinite(request.timeout) or not 0 < request.timeout <= 600):
                raise ValueError("Request timeout must be between 0 and 600 seconds.")
            if (not isinstance(request.temperature, (int, float)) or not math.isfinite(request.temperature)
                    or not 0 <= request.temperature <= 2):
                raise ValueError("Temperature must be between 0 and 2.")
            if request.max_tokens is not None and (type(request.max_tokens) is not int
                    or not 0 < request.max_tokens <= 1000000):
                raise ValueError("Maximum output tokens must be a positive integer up to 1000000.")
            input_text = f"{request.system_instruction}\n{request.prompt}"
            prompt_tokens = count_bpe_tokens(input_text)
            if provider == "pollinations":
                content = self._call_pollinations(request, raw_model_name)
            elif provider == "openai":
                content = self._call_openai(request, raw_model_name)
            elif provider == "anthropic":
                content = self._call_anthropic(request, raw_model_name)
            elif provider == "gemini":
                content = self._call_gemini(request, raw_model_name)
            elif provider == "ollama":
                content = self._call_ollama(request, raw_model_name)
            elif provider == "openrouter":
                content = self._call_openrouter(request, raw_model_name)
            else:
                raise ValueError(f"Unsupported provider '{provider}'")

            if not isinstance(content, str) or not content.strip():
                raise ValueError("The provider returned an empty response. Check its model and output limits.")
            elapsed = time.monotonic() - start_time
            completion_tokens = count_bpe_tokens(content)
            total_tokens = prompt_tokens + completion_tokens

            # Compute estimated cost
            cost = 0.0
            if model_info and not model_info.is_free:
                cost += (prompt_tokens / 1000.0) * model_info.cost_per_1k_input
                cost += (completion_tokens / 1000.0) * model_info.cost_per_1k_output

            return LLMResponse(
                content=content,
                elapsed_seconds=elapsed,
                model_id=request.model_id,
                provider=provider,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=cost,
                error=None,
            )
        except urllib.error.HTTPError as e:
            elapsed = time.monotonic() - start_time
            err_msg = f"HTTP {e.code}: {e.reason}"
            try:
                if e.fp:
                    raw_body = self._safe_error(e.read(8192).decode("utf-8", errors="replace"), provider)
                    try:
                        parsed = json.loads(raw_body)
                        if isinstance(parsed, dict):
                            err_obj = parsed.get("error")
                            if isinstance(err_obj, dict) and "message" in err_obj:
                                err_msg = f"HTTP {e.code}: {err_obj['message']}"
                            elif isinstance(err_obj, str):
                                err_msg = f"HTTP {e.code}: {err_obj}"
                            elif "message" in parsed:
                                err_msg = f"HTTP {e.code}: {parsed['message']}"
                            else:
                                err_msg = f"HTTP {e.code}: {raw_body[:200]}"
                    except Exception:
                        if raw_body.strip():
                            err_msg = f"HTTP {e.code}: {raw_body.strip()[:200]}"
            except Exception:
                pass
            return LLMResponse(
                content="",
                elapsed_seconds=elapsed,
                model_id=request.model_id,
                provider=provider,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                total_tokens=prompt_tokens,
                estimated_cost_usd=0.0,
                error=self._safe_error(err_msg, provider),
            )
        except Exception as e:
            elapsed = time.monotonic() - start_time
            return LLMResponse(
                content="",
                elapsed_seconds=elapsed,
                model_id=request.model_id,
                provider=provider,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                total_tokens=prompt_tokens,
                estimated_cost_usd=0.0,
                error=self._safe_error(str(e), provider),
            )

    def _safe_error(self, message: str, provider: str) -> str:
        """Keep useful provider diagnostics while removing credentials."""
        try:
            secret = self.vault.get_api_key(provider) if provider else ""
        except Exception:
            secret = ""
        if secret:
            for encoded in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
                message = message.replace(encoded, "[redacted]")
        message = re.sub(r"(?i)([?&](?:key|api_key|token|access_token)=)[^&\s]+", r"\1[redacted]", message)
        message = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[redacted]", message)
        return message[:600]

    # ── Provider-specific HTTP implementations ──────────────────────────

    def _call_pollinations(self, req: LLMRequest, model_name: str) -> str:
        api_key = self.vault.get_api_key("pollinations")
        client = PollinationsClient(api_key=api_key, timeout=req.timeout)
        return client.generate(
            prompt=req.prompt,
            system_instruction=req.system_instruction,
            model=model_name,
            temperature=req.temperature,
            timeout=req.timeout,
        )

    def _call_openai(self, req: LLMRequest, model_name: str) -> str:
        cfg = self.vault.get_provider_config("openai")
        base_url = validate_base_url(cfg.get("base_url") or "https://api.openai.com/v1")
        api_key = self.vault.get_api_key("openai")
        if not api_key and urllib.parse.urlsplit(base_url).hostname == "api.openai.com":
            raise ValueError("OpenAI API key not configured. Please set your API key in Settings (Ctrl+,).")

        messages = []
        if req.system_instruction.strip():
            messages.append({"role": "system", "content": req.system_instruction.strip()})
        messages.append({"role": "user", "content": req.prompt.strip()})

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": req.temperature,
        }
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens

        url = f"{base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "CubePrompts/1.0.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = json.dumps(payload).encode("utf-8")
        http_req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(http_req, timeout=req.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices") or []
            if not choices:
                raise ValueError("OpenAI returned no choices in response.")
            content = choices[0].get("message", {}).get("content")
            if content is None:
                content = ""
            return content.strip()

    def _call_anthropic(self, req: LLMRequest, model_name: str) -> str:
        api_key = self.vault.get_api_key("anthropic")
        if not api_key:
            raise ValueError("Anthropic API key not configured. Please set your API key in Settings (Ctrl+,).")

        messages = [{"role": "user", "content": req.prompt.strip()}]
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": req.max_tokens or 16000,
        }
        # Current Claude models reject sampling parameters with a 400; only
        # the legacy Claude 3 family still accepts temperature.
        if model_name.startswith("claude-3"):
            payload["temperature"] = req.temperature
        if req.system_instruction.strip():
            payload["system"] = req.system_instruction.strip()

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "CubePrompts/1.0.0",
        }
        data = json.dumps(payload).encode("utf-8")
        http_req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(http_req, timeout=req.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            blocks = body.get("content", [])
            text_blocks = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            return "\n".join(text_blocks).strip()

    def _call_gemini(self, req: LLMRequest, model_name: str) -> str:
        api_key = self.vault.get_api_key("gemini")
        if not api_key:
            raise ValueError("Google Gemini API key not configured. Please set your API key in Settings (Ctrl+,).")

        contents = [{"parts": [{"text": req.prompt.strip()}]}]
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": req.temperature,
            },
        }
        if req.system_instruction.strip():
            payload["systemInstruction"] = {
                "parts": [{"text": req.system_instruction.strip()}]
            }
        if req.max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = req.max_tokens

        encoded_model = urllib.parse.quote(model_name, safe="")
        encoded_key = urllib.parse.quote(api_key, safe="")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent?key={encoded_key}"
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8")
        http_req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(http_req, timeout=req.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            candidates = body.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini returned no response candidates.")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()

    def _call_ollama(self, req: LLMRequest, model_name: str) -> str:
        cfg = self.vault.get_provider_config("ollama")
        base_url = validate_base_url(cfg.get("base_url") or "http://localhost:11434")

        messages = []
        if req.system_instruction.strip():
            messages.append({"role": "system", "content": req.system_instruction.strip()})
        messages.append({"role": "user", "content": req.prompt.strip()})

        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": req.temperature,
            },
        }

        if req.max_tokens:
            payload["options"]["num_predict"] = req.max_tokens

        url = f"{base_url}/api/chat"
        headers = {"Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8")
        http_req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(http_req, timeout=req.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("message", {}).get("content", "").strip()
        except urllib.error.URLError as e:
            raise ConnectionError(
                "Cannot connect to Ollama. Check the endpoint in Settings and ensure Ollama is running (`ollama serve`)."
            ) from e

    def _call_openrouter(self, req: LLMRequest, model_name: str) -> str:
        api_key = self.vault.get_api_key("openrouter")
        if not api_key:
            raise ValueError("OpenRouter API key not configured. Please set your key in Settings (Ctrl+,).")

        messages = []
        if req.system_instruction.strip():
            messages.append({"role": "system", "content": req.system_instruction.strip()})
        messages.append({"role": "user", "content": req.prompt.strip()})

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": req.temperature,
        }
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/prompt-manager",
            "X-Title": "Cube Prompts",
        }
        data = json.dumps(payload).encode("utf-8")
        http_req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(http_req, timeout=req.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices") or []
            if not choices:
                raise ValueError("OpenRouter returned no choices in response.")
            content = choices[0].get("message", {}).get("content")
            if content is None:
                content = ""
            return content.strip()
