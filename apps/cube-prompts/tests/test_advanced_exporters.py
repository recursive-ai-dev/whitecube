"""Tests for advanced exporters (CSV, Markdown Zip bundle, LangChain, LlamaIndex)."""

from pathlib import Path
import tempfile
import unittest

from prompt_manager.core.exporter import (
    from_csv_string,
    to_csv_string,
    to_langchain_template,
    to_llamaindex_template,
)
from prompt_manager.core.models import Prompt
from prompt_manager.storage.backup import (
    export_library_to_csv,
    export_library_to_markdown_zip,
    import_library_from_csv,
)
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository


class TestAdvancedExporters(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_exporters.db"
        self.db = Database(self.db_path)
        self.repo = PromptRepository(self.db)
        self.prompt1 = Prompt(
            id="p1",
            title="Refactor Master",
            description="Refactoring tool",
            template_content="Refactor this {{lang:python}} code:\n{{snippet|multiline}}",
            system_instruction="Be succinct.",
            target_model="Claude 3.7 Sonnet",
            tags=["refactor", "code"],
        )
        self.prompt2 = Prompt(
            id="p2",
            title="SQL Optimizer",
            description="Optimize SQL",
            template_content="Optimize this query: {{query}}",
            system_instruction="",
            target_model="GPT-4o",
            tags=["database", "sql"],
        )
        self.repo.save_prompt(self.prompt1)
        self.repo.save_prompt(self.prompt2)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_to_langchain_template(self):
        code = to_langchain_template(self.prompt1)
        self.assertIn("ChatPromptTemplate", code)
        self.assertIn("SystemMessagePromptTemplate", code)
        self.assertIn("{lang}", code)
        self.assertIn("{snippet}", code)

    def test_to_llamaindex_template(self):
        code = to_llamaindex_template(self.prompt2)
        self.assertIn("PromptTemplate", code)
        self.assertIn("{query}", code)

    def test_csv_export_and_import(self):
        prompts = [self.prompt1, self.prompt2]
        csv_text = to_csv_string(prompts)
        self.assertIn("Refactor Master", csv_text)
        self.assertIn("SQL Optimizer", csv_text)

        parsed = from_csv_string(csv_text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].title, "Refactor Master")
        self.assertEqual(parsed[0].tags, ["refactor", "code"])
        self.assertEqual(parsed[1].title, "SQL Optimizer")

    def test_backup_csv_and_zip_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "prompts.csv"
            zip_path = Path(tmpdir) / "prompts_md.zip"

            total_initial = len(self.repo.list_prompts())
            count_csv = export_library_to_csv(self.repo, csv_path)
            self.assertEqual(count_csv, total_initial)
            self.assertTrue(csv_path.exists())

            count_zip = export_library_to_markdown_zip(self.repo, zip_path)
            self.assertEqual(count_zip, total_initial)
            self.assertTrue(zip_path.exists())

            # Test importing CSV into new empty database
            new_db_path = Path(tmpdir) / "new_test.db"
            new_db = Database(new_db_path)
            new_repo = PromptRepository(new_db)
            # Clear seeded starter prompts from new repo to test import count precisely
            for p in new_repo.list_prompts():
                new_repo.delete_prompt(p.id)

            imported_count = import_library_from_csv(new_repo, csv_path)
            self.assertEqual(imported_count, total_initial)
            self.assertEqual(len(new_repo.list_prompts()), total_initial)

    def test_python_code_export_escaping_triple_quotes(self):
        prompt_with_quotes = Prompt(
            id="p_quotes",
            title="Code With Quotes",
            description="Docstring tester",
            template_content='Run: """print("evil")""" and end with quote"',
            system_instruction='System: """instructions""" and backslash \\',
        )
        lc_code = to_langchain_template(prompt_with_quotes)
        li_code = to_llamaindex_template(prompt_with_quotes)

        # Must compile without SyntaxError
        compile(lc_code, "<string>", "exec")
        compile(li_code, "<string>", "exec")

    def test_from_csv_empty_numeric_fields(self):
        csv_text = (
            "id,title,description,template_content,system_instruction,target_model,temperature,tags,is_favorite,created_at,updated_at\n"
            "id1,Test Title,Desc,Hello {{var}},Sys,General,,,0,,\n"
        )
        prompts = from_csv_string(csv_text)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].temperature, 0.7)
        self.assertFalse(prompts[0].is_favorite)

    def test_export_markdown_zip_duplicate_titles(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "duplicates.zip"
            db_path = Path(tmpdir) / "dup.db"
            db = Database(db_path)
            repo = PromptRepository(db)
            for p in repo.list_prompts():
                repo.delete_prompt(p.id)

            repo.save_prompt(Prompt(id="id11111111", title="Same Title", template_content="content 1"))
            repo.save_prompt(Prompt(id="id22222222", title="Same Title", template_content="content 2"))

            count = export_library_to_markdown_zip(repo, zip_path)
            self.assertEqual(count, 2)
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                self.assertEqual(len(names), 2)
                self.assertIn("Same Title.md", names)
                self.assertTrue(any(n.startswith("Same Title_") and n.endswith(".md") for n in names))


if __name__ == "__main__":
    unittest.main()
