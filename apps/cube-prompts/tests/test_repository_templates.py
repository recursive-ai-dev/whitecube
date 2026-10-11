"""Tests for PromptTemplate CRUD, validation, FTS search, and instantiation."""

from pathlib import Path
import tempfile
import unittest

from prompt_manager.core.models import PromptTemplate
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository


class TestRepositoryTemplates(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.repo = PromptRepository(self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_starts_empty_templates(self):
        # Database seeds default prompts and folders, but 0 prompt_templates (infrastructure only)
        templates = self.repo.list_templates()
        self.assertEqual(len(templates), 0)

    def test_create_and_read_template(self):
        tmpl = PromptTemplate(
            name="code-review",
            description="Reviews code for security anti-patterns",
            content="Please review this {{lang:Python}} code:\n```\n{{code|multiline}}\n```",
            system_instruction="You are a Principal Security Engineer.",
            category="coding",
        )
        saved = self.repo.save_template(tmpl)
        self.assertEqual(saved.id, tmpl.id)

        fetched = self.repo.get_template_by_id(tmpl.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.name, "code-review")
        self.assertEqual(fetched.category, "coding")
        self.assertEqual(fetched.system_instruction, "You are a Principal Security Engineer.")

        fetched_by_name = self.repo.get_template_by_name("code-review")
        self.assertIsNotNone(fetched_by_name)
        self.assertEqual(fetched_by_name.id, tmpl.id)

    def test_update_template(self):
        tmpl = self.repo.save_template(
            PromptTemplate(
                name="initial-template",
                content="Hello {{name}}",
                category="general",
            )
        )
        tmpl.name = "updated-template"
        tmpl.content = "Hello {{name}}, welcome to {{city}}!"
        tmpl.category = "writing"
        self.repo.save_template(tmpl)

        fetched = self.repo.get_template_by_id(tmpl.id)
        self.assertEqual(fetched.name, "updated-template")
        self.assertEqual(fetched.content, "Hello {{name}}, welcome to {{city}}!")
        self.assertEqual(fetched.category, "writing")

    def test_validation_empty_name(self):
        tmpl = PromptTemplate(name="", content="Valid content")
        with self.assertRaises(ValueError):
            self.repo.save_template(tmpl)

    def test_validation_empty_content(self):
        tmpl = PromptTemplate(name="Valid Name", content="")
        with self.assertRaises(ValueError):
            self.repo.save_template(tmpl)

    def test_unique_name_constraint(self):
        self.repo.save_template(PromptTemplate(name="duplicate-test", content="Content 1"))
        duplicate = PromptTemplate(name="duplicate-test", content="Content 2")
        with self.assertRaises(ValueError):
            self.repo.save_template(duplicate)

    def test_fts_search_and_category_filter(self):
        self.repo.save_template(
            PromptTemplate(
                name="sql-optimizer",
                description="Optimizes slow PostgreSQL queries",
                content="EXPLAIN ANALYZE {{query|multiline}}",
                category="coding",
            )
        )
        self.repo.save_template(
            PromptTemplate(
                name="blog-outline",
                description="Generates outline for technical blog",
                content="Outline a blog about {{topic}}",
                category="writing",
            )
        )

        # Category filtering
        coding_tmpls = self.repo.list_templates(category="coding")
        self.assertEqual(len(coding_tmpls), 1)
        self.assertEqual(coding_tmpls[0].name, "sql-optimizer")

        writing_tmpls = self.repo.list_templates(category="writing")
        self.assertEqual(len(writing_tmpls), 1)
        self.assertEqual(writing_tmpls[0].name, "blog-outline")

        # FTS search
        sql_search = self.repo.list_templates(search_query="PostgreSQL")
        self.assertEqual(len(sql_search), 1)
        self.assertEqual(sql_search[0].name, "sql-optimizer")

    def test_create_prompt_from_template(self):
        tmpl = self.repo.save_template(
            PromptTemplate(
                name="summarizer",
                description="Summarizes meeting notes",
                content="Summarize meeting: {{notes|multiline}}",
                system_instruction="Be concise.",
                category="writing",
            )
        )
        prompt = self.repo.create_prompt_from_template(
            template_id=tmpl.id,
            title="Q3 Strategy Meeting Summary",
            extra_tags=["meeting", "q3"],
        )
        self.assertIsNotNone(prompt.id)
        self.assertEqual(prompt.title, "Q3 Strategy Meeting Summary")
        self.assertEqual(prompt.template_id, tmpl.id)
        self.assertEqual(prompt.template_content, tmpl.content)
        self.assertEqual(prompt.system_instruction, tmpl.system_instruction)
        self.assertEqual(sorted(prompt.tags), ["meeting", "q3"])

    def test_delete_template_nullifies_prompt_link(self):
        tmpl = self.repo.save_template(
            PromptTemplate(name="to-delete", content="Content {{var}}")
        )
        prompt = self.repo.create_prompt_from_template(template_id=tmpl.id)
        self.assertEqual(prompt.template_id, tmpl.id)

        self.repo.delete_template(tmpl.id)
        self.assertIsNone(self.repo.get_template_by_id(tmpl.id))

        # Check prompt still exists with template_id set to None
        p = self.repo.get_prompt_by_id(prompt.id)
        self.assertIsNotNone(p)
        self.assertIsNone(p.template_id)
        self.assertEqual(p.template_content, "Content {{var}}")


if __name__ == "__main__":
    unittest.main()
