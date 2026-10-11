"""Multi-Model Arena evaluation and benchmarking orchestrator."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from prompt_manager.integrations.llm_providers import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    get_model_info,
)


@dataclass
class ArenaResult:
    """Consolidated benchmark metrics and responses from an Arena evaluation run."""

    prompt: str
    system_instruction: str
    responses: List[LLMResponse] = field(default_factory=list)

    @property
    def fastest_response(self) -> Optional[LLMResponse]:
        successful = [r for r in self.responses if r.is_success]
        if not successful:
            return None
        return min(successful, key=lambda r: r.elapsed_seconds)

    @property
    def cheapest_response(self) -> Optional[LLMResponse]:
        successful = [r for r in self.responses if r.is_success]
        if not successful:
            return None
        return min(successful, key=lambda r: r.estimated_cost_usd)


def run_arena_comparison(
    prompt: str,
    model_ids: List[str],
    system_instruction: str = "",
    temperature: float = 0.7,
    timeout: int = 45,
    client: Optional[LLMClient] = None,
) -> ArenaResult:
    """Execute the prompt concurrently across all selected models and gather telemetry."""
    if not model_ids:
        return ArenaResult(prompt=prompt, system_instruction=system_instruction)

    llm_client = client or LLMClient()
    result = ArenaResult(prompt=prompt, system_instruction=system_instruction)

    def _execute_single(mid: str) -> LLMResponse:
        req = LLMRequest(
            prompt=prompt,
            system_instruction=system_instruction,
            model_id=mid,
            temperature=temperature,
            timeout=timeout,
        )
        return llm_client.execute(req)

    # Concurrently execute models across provider boundaries
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(model_ids)) as executor:
        future_map = {
            executor.submit(_execute_single, mid): i for i, mid in enumerate(model_ids)
        }
        responses = {}
        for future in concurrent.futures.as_completed(future_map):
            index = future_map[future]
            try:
                responses[index] = future.result()
            except Exception as e:
                responses[index] = LLMResponse(
                    model_id=model_ids[index],
                    error=f"Execution exception: {e}",
                )

    # Sort responses to preserve original model selection order
    result.responses = [responses[i] for i in range(len(model_ids))]
    return result
