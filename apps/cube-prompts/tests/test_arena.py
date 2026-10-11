"""Tests for Multi-Model Arena runner and comparison logic."""

import unittest
from unittest.mock import MagicMock

from prompt_manager.core.arena import ArenaResult, run_arena_comparison
from prompt_manager.integrations.llm_providers import LLMClient, LLMResponse


class TestArena(unittest.TestCase):

    def test_arena_result_metrics(self):
        resp1 = LLMResponse(
            content="Fast model answer",
            elapsed_seconds=0.25,
            model_id="pollinations:openai-fast",
            total_tokens=50,
            estimated_cost_usd=0.0,
        )
        resp2 = LLMResponse(
            content="Smart model answer",
            elapsed_seconds=1.10,
            model_id="openai:gpt-4o",
            total_tokens=80,
            estimated_cost_usd=0.0015,
        )

        arena = ArenaResult(
            prompt="Write a poem",
            system_instruction="Be concise",
            responses=[resp1, resp2],
        )

        self.assertEqual(arena.fastest_response.model_id, "pollinations:openai-fast")
        self.assertEqual(arena.cheapest_response.model_id, "pollinations:openai-fast")

    def test_run_arena_comparison_mocked(self):
        mock_client = MagicMock(spec=LLMClient)
        mock_client.execute.side_effect = lambda req: LLMResponse(
            content=f"Response for {req.model_id}",
            elapsed_seconds=0.5,
            model_id=req.model_id,
            total_tokens=20,
        )

        result = run_arena_comparison(
            prompt="Test prompt",
            model_ids=["pollinations:openai-fast", "openai:gpt-4o"],
            client=mock_client,
        )

        self.assertEqual(len(result.responses), 2)
        self.assertEqual(result.responses[0].model_id, "pollinations:openai-fast")
        self.assertEqual(result.responses[1].model_id, "openai:gpt-4o")


if __name__ == "__main__":
    unittest.main()
