"""Tests for token counter and text metrics calculation."""

import unittest
from prompt_manager.core.token_counter import calculate_metrics, TextMetrics


class TestTokenCounter(unittest.TestCase):

    def test_empty_string(self):
        metrics = calculate_metrics("")
        self.assertEqual(metrics.characters, 0)
        self.assertEqual(metrics.words, 0)
        self.assertEqual(metrics.lines, 0)
        self.assertEqual(metrics.estimated_tokens, 0)

    def test_simple_text(self):
        text = "Hello world! This is a test prompt for token estimation."
        metrics = calculate_metrics(text)
        self.assertEqual(metrics.characters, len(text))
        self.assertEqual(metrics.words, 10)
        self.assertEqual(metrics.lines, 1)
        self.assertGreater(metrics.estimated_tokens, 0)

    def test_multiline_code_snippet(self):
        code = (
            "def calculate_fibonacci(n: int) -> int:\n"
            "    if n <= 1:\n"
            "        return n\n"
            "    return calculate_fibonacci(n - 1) + calculate_fibonacci(n - 2)\n"
        )
        metrics = calculate_metrics(code)
        self.assertEqual(metrics.characters, len(code))
        self.assertEqual(metrics.lines, 4)
        self.assertGreater(metrics.estimated_tokens, 15)


if __name__ == "__main__":
    unittest.main()
