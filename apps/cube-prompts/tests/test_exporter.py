import unittest
from prompt_manager.core.models import Prompt
from prompt_manager.core.exporter import (
    to_plain_text,
    to_openai_payload,
    to_anthropic_payload,
    to_markdown_frontmatter,
)


class TestExporter(unittest.TestCase):

    def setUp(self):
        self.prompt = Prompt(
            title="Code Auditor",
            description="Audits security",
            template_content="Audit this: {{target}}",
            system_instruction="Be vigilant and precise.",
            target_model="Claude 3.7 Sonnet",
            temperature=0.2,
            tags=["security", "audit"],
        )

    def test_to_plain_text(self):
        plain = to_plain_text(self.prompt, hydrated_content="Audit this: AuthController.java")
        self.assertIn("[SYSTEM INSTRUCTION]", plain)
        self.assertIn("Be vigilant and precise.", plain)
        self.assertIn("Audit this: AuthController.java", plain)

    def test_to_openai_payload(self):
        payload = to_openai_payload(self.prompt, hydrated_content="Audit this: user_service.py")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertEqual(payload["messages"][1]["content"], "Audit this: user_service.py")

    def test_to_anthropic_payload(self):
        payload = to_anthropic_payload(self.prompt, hydrated_content="Audit this: main.rs")
        self.assertEqual(payload["system"], "Be vigilant and precise.")
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][0]["content"], "Audit this: main.rs")

    def test_to_markdown_frontmatter(self):
        md = to_markdown_frontmatter(self.prompt, hydrated_content="Audit this: config.json")
        self.assertTrue(md.startswith("---"))
        self.assertIn("## System Instruction", md)
        self.assertIn("## Prompt", md)
        self.assertIn("Audit this: config.json", md)


if __name__ == "__main__":
    unittest.main()
