"""Pollinations.ai Free Text Generation API Client.

Provides free, no-API-key-required LLM inference for live testing prompt templates.
Uses standard library urllib (zero external dependencies).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


POLLINATIONS_BASE_URL = "https://text.pollinations.ai"
USER_AGENT = "Mozilla/5.0 CubePrompts/1.0.0 (Linux; Whitecube)"

FALLBACK_MODELS = [
    {"name": "openai-fast", "description": "Fast reasoning & general assistant (Default)"},
    {"name": "openai", "description": "OpenAI general text model"},
    {"name": "mistral", "description": "Mistral text model"},
    {"name": "qwen", "description": "Qwen model"},
    {"name": "llama", "description": "Meta Llama model"},
    {"name": "deepseek", "description": "DeepSeek reasoning model"},
    {"name": "claude", "description": "Claude compatible model"},
]


class PollinationsError(Exception):
    """Raised when Pollinations API returns an error or fails."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class PollinationsClient:
    """Client for Pollinations free text AI generation."""

    def __init__(self, api_key: Optional[str] = None, timeout: int = 45):
        self.api_key = api_key.strip() if api_key else ""
        self.timeout = timeout

    def _headers(self, content_type: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/plain, application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        model: str = "openai-fast",
        temperature: float = 0.7,
        seed: Optional[int] = None,
        json_mode: bool = False,
        timeout: Optional[int] = None,
    ) -> str:
        """Generate text from a prompt using Pollinations free endpoint.

        Args:
            prompt: User prompt content.
            system_instruction: Optional system persona or constraints.
            model: Target model (e.g. 'openai-fast', 'openai', 'mistral').
            temperature: Sampling temperature (0.0 - 2.0).
            seed: Optional integer seed for deterministic output.
            json_mode: If true, requests JSON response formatting.
            timeout: Request timeout in seconds.

        Returns:
            The generated text string.
        """
        if not prompt or not prompt.strip():
            raise ValueError("Prompt cannot be empty")

        clean_prompt = prompt.strip()
        clean_model = model.strip() if model and model.strip() else "openai-fast"
        to = timeout or self.timeout

        # For long prompts (> 1500 chars), use POST request with JSON body
        # For shorter prompts, GET request is the primary official format
        if len(clean_prompt) > 1500 or (system_instruction and len(system_instruction) > 500):
            return self._generate_post(
                prompt=clean_prompt,
                system_instruction=system_instruction,
                model=clean_model,
                temperature=temperature,
                seed=seed,
                json_mode=json_mode,
                timeout=to,
            )

        # GET request approach
        params: Dict[str, str] = {
            "model": clean_model,
            "temperature": f"{temperature:.2f}",
        }
        if system_instruction and system_instruction.strip():
            params["system"] = system_instruction.strip()
        if seed is not None:
            params["seed"] = str(seed)
        if json_mode:
            params["jsonMode"] = "true"

        encoded_prompt = urllib.parse.quote(clean_prompt, safe="")
        query_string = urllib.parse.urlencode(params)
        url = f"{POLLINATIONS_BASE_URL}/{encoded_prompt}?{query_string}"

        req = urllib.request.Request(url, headers=self._headers(), method="GET")

        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return raw.strip()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
            if e.code == 429:
                raise PollinationsError(
                    "Rate limit exceeded (HTTP 429). Please wait a few seconds before trying again.",
                    status=429,
                ) from e
            elif e.code == 402:
                # Try fallback model
                raise PollinationsError(
                    f"Selected model '{clean_model}' requires an API key. Try 'openai-fast' for free tier.",
                    status=402,
                ) from e
            msg = err_body or f"HTTP {e.code}: {e.reason}"
            raise PollinationsError(f"Pollinations error: {msg}", status=e.code) from e
        except urllib.error.URLError as e:
            reason = str(e.reason) if hasattr(e, "reason") else str(e)
            raise PollinationsError(f"Network connection error: {reason}") from e
        except TimeoutError as e:
            raise PollinationsError(f"Request timed out after {to}s.") from e

    def _generate_post(
        self,
        prompt: str,
        system_instruction: str = "",
        model: str = "openai-fast",
        temperature: float = 0.7,
        seed: Optional[int] = None,
        json_mode: bool = False,
        timeout: int = 45,
    ) -> str:
        """Fallback to POST payload for larger prompt bodies."""
        messages: List[Dict[str, str]] = []
        if system_instruction and system_instruction.strip():
            messages.append({"role": "system", "content": system_instruction.strip()})
        messages.append({"role": "user", "content": prompt.strip()})

        payload: Dict[str, Any] = {
            "messages": messages,
            "model": model,
            "temperature": temperature,
        }
        if seed is not None:
            payload["seed"] = seed
        if json_mode:
            payload["jsonMode"] = True

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{POLLINATIONS_BASE_URL}/",
            data=data,
            headers=self._headers("application/json"),
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                # May return text directly or JSON choices
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "choices" in parsed:
                        choices = parsed.get("choices") or []
                        if choices:
                            choice = choices[0]
                            content = choice.get("message", {}).get("content", raw)
                            if content is not None:
                                return str(content).strip()
                    elif isinstance(parsed, dict) and "content" in parsed:
                        content = parsed.get("content")
                        if content is not None:
                            return str(content).strip()
                except Exception:
                    pass
                return raw.strip()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
            if e.code == 429:
                raise PollinationsError(
                    "Rate limit exceeded (HTTP 429). Please wait a few seconds before trying again.",
                    status=429,
                ) from e
            elif e.code == 402:
                raise PollinationsError(
                    f"Selected model '{model}' requires payment or API key. Use 'openai-fast' for free tier.",
                    status=402,
                ) from e
            msg = err_body or f"HTTP {e.code}: {e.reason}"
            raise PollinationsError(f"Pollinations error: {msg}", status=e.code) from e
        except urllib.error.URLError as e:
            reason = str(e.reason) if hasattr(e, "reason") else str(e)
            raise PollinationsError(f"Network connection error: {reason}") from e

    def list_models(self) -> List[Dict[str, Any]]:
        """Fetch available models from Pollinations endpoint with local fallback."""
        url = f"{POLLINATIONS_BASE_URL}/models"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                if isinstance(data, list) and len(data) > 0:
                    return data
        except Exception:
            pass
        return list(FALLBACK_MODELS)
