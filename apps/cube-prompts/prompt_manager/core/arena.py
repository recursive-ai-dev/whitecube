"""Multi-model comparison with bounded workers, progress and cancellation."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from threading import Event
from typing import Callable, List, Optional

from prompt_manager.integrations.llm_providers import LLMClient, LLMRequest, LLMResponse


@dataclass
class ArenaResult:
    """Consolidated benchmark metrics and responses from an evaluation run."""

    prompt: str
    system_instruction: str
    responses: List[LLMResponse] = field(default_factory=list)
    cancelled: bool = False

    @property
    def fastest_response(self) -> Optional[LLMResponse]:
        successful = [r for r in self.responses if r.is_success]
        return min(successful, key=lambda r: r.elapsed_seconds) if successful else None

    @property
    def cheapest_response(self) -> Optional[LLMResponse]:
        successful = [r for r in self.responses if r.is_success]
        return min(successful, key=lambda r: r.estimated_cost_usd) if successful else None


def run_arena_comparison(
    prompt: str,
    model_ids: List[str],
    system_instruction: str = "",
    temperature: float = 0.7,
    timeout: int = 45,
    client: Optional[LLMClient] = None,
    max_tokens: Optional[int] = None,
    cancel_event: Optional[Event] = None,
    on_response: Optional[Callable[[int, LLMResponse], None]] = None,
) -> ArenaResult:
    """Run models concurrently, preserving slots including duplicate model IDs.

    ``on_response(index, response)`` runs on the calling worker thread; GUI
    callers must marshal updates to their main loop. Cancellation skips queued
    work and returns promptly. Requests already sent may complete at the
    provider and incur usage, but their late answers are discarded.
    """
    result = ArenaResult(prompt=prompt, system_instruction=system_instruction)
    if not model_ids:
        return result
    if len(model_ids) > 32:
        raise ValueError("Compare at most 32 models in a single run.")
    event = cancel_event or Event()
    llm_client = client or LLMClient()
    responses = {}

    def _cancelled(mid: str) -> LLMResponse:
        return LLMResponse(model_id=mid, error="Cancelled. Requests already sent may still incur provider usage.")

    def _execute_single(mid: str) -> LLMResponse:
        if event.is_set():
            return _cancelled(mid)
        return llm_client.execute(LLMRequest(
            prompt=prompt, system_instruction=system_instruction, model_id=mid,
            temperature=temperature, timeout=timeout, max_tokens=max_tokens,
        ))

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(model_ids), 6))
    pending = {}
    try:
        if not event.is_set():
            pending = {executor.submit(_execute_single, mid): index for index, mid in enumerate(model_ids)}
        while pending and not event.is_set():
            done, _ = concurrent.futures.wait(
                pending, timeout=0.05, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                index = pending.pop(future)
                try:
                    response = future.result()
                except Exception:
                    # A third-party client exception can include request headers.
                    response = LLMResponse(model_id=model_ids[index], error="The model request failed unexpectedly.")
                responses[index] = response
                if on_response:
                    on_response(index, response)
        result.cancelled = event.is_set()
        for future in pending:
            future.cancel()
        result.responses = [responses.get(index) or _cancelled(mid) for index, mid in enumerate(model_ids)]
        return result
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
