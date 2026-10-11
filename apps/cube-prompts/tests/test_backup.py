"""Tests for JSON library export and import (backup and restore)."""

import json
from pathlib import Path
import tempfile
import unittest

from prompt_manager.core.models import Folder, Prompt, PromptTemplate, Tag
from prompt_manager.storage.backup import export_library_to_json, import_library_from_json
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository


class TestBackup(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.export_file = Path(self.temp_dir.name) / "backup.json"
        self.db = Database(self.db_path)
        self.repo = PromptRepository(self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_and_import_roundtrip(self):
        # Create folder
        folder = self.repo.save_folder(Folder(name="DevOps", icon="server"))
        # Create tag
        tag = self.repo.save_tag(Tag(name="kubernetes", color="#326ce5"))
        # Create template
        tmpl = self.repo.save_template(
            PromptTemplate(
                name="k8s-pod-template",
                description="Template for k8s pod specs",
                content="apiVersion: v1\nkind: Pod\nmetadata:\n  name: {{pod_name}}\nspec:\n  containers:\n  - name: {{container_name}}\n    image: {{image:nginx}}",
                system_instruction="You are a Kubernetes expert.",
                category="coding",
            )
        )
        # Create prompt linked to template and folder
        prompt = self.repo.save_prompt(
            Prompt(
                title="Generate NGINX Pod",
                description="Generates standard nginx pod",
                folder_id=folder.id,
                template_id=tmpl.id,
                template_content=tmpl.content,
                system_instruction=tmpl.system_instruction,
                target_model="GPT-4o",
                temperature=0.3,
                is_favorite=True,
                tags=["kubernetes", "devops"],
            )
        )

        # Export to JSON
        count = export_library_to_json(self.repo, self.export_file)
        self.assertGreaterEqual(count, 1)
        self.assertTrue(self.export_file.exists())

        # Verify JSON content structure
        data = json.loads(self.export_file.read_text(encoding="utf-8"))
        self.assertIn("version", data)
        self.assertIn("folders", data)
        self.assertIn("tags", data)
        self.assertIn("templates", data)
        self.assertIn("prompts", data)
        self.assertTrue(any(f["name"] == "DevOps" for f in data["folders"]))
        self.assertTrue(any(t["name"] == "k8s-pod-template" for t in data["templates"]))
        self.assertTrue(any(p["title"] == "Generate NGINX Pod" for p in data["prompts"]))

        # Create a fresh database and import
        db2_path = Path(self.temp_dir.name) / "test2.db"
        db2 = Database(db2_path)
        repo2 = PromptRepository(db2)

        # Wipe seed prompts in db2 to test clean import
        with db2.get_connection() as conn:
            conn.execute("DELETE FROM prompt_tags")
            conn.execute("DELETE FROM prompt_revisions")
            conn.execute("DELETE FROM prompts")
            conn.execute("DELETE FROM prompt_templates")
            conn.execute("DELETE FROM folders")
            conn.execute("DELETE FROM tags")

        imported_count = import_library_from_json(repo2, self.export_file)
        self.assertEqual(imported_count, count)

        # Verify prompt exists in repo2 with correct template_id and folder_id
        p2 = repo2.get_prompt_by_id(prompt.id)
        self.assertIsNotNone(p2)
        self.assertEqual(p2.title, "Generate NGINX Pod")
        self.assertEqual(p2.folder_id, folder.id)
        self.assertEqual(p2.template_id, tmpl.id)
        self.assertTrue(p2.is_favorite)
        self.assertEqual(sorted(p2.tags), ["devops", "kubernetes"])

        # Verify template exists in repo2
        t2 = repo2.get_template_by_id(tmpl.id)
        self.assertIsNotNone(t2)
        self.assertEqual(t2.name, "k8s-pod-template")
        self.assertEqual(t2.category, "coding")

    def test_import_legacy_v1_backup_without_templates(self):
        legacy_data = {
            "version": "1.0",
            "folders": [
                {
                    "id": "f-123",
                    "name": "Legacy Folder",
                    "parent_id": None,
                    "icon": "folder",
                    "sort_order": 0,
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
            "tags": [{"id": "t-123", "name": "legacy", "color": "#123456"}],
            "prompts": [
                {
                    "id": "p-123",
                    "title": "Legacy Prompt",
                    "description": "From old version",
                    "folder_id": "f-123",
                    "template_content": "Legacy {{var}}",
                    "system_instruction": "Old system instruction",
                    "target_model": "General",
                    "temperature": 0.7,
                    "is_favorite": False,
                    "use_count": 5,
                    "tags": ["legacy"],
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                }
            ],
        }
        legacy_file = Path(self.temp_dir.name) / "legacy.json"
        legacy_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        count = import_library_from_json(self.repo, legacy_file)
        self.assertEqual(count, 1)

        p = self.repo.get_prompt_by_id("p-123")
        self.assertIsNotNone(p)
        self.assertEqual(p.title, "Legacy Prompt")
        self.assertIsNone(p.template_id)
        self.assertEqual(p.tags, ["legacy"])

    def test_import_json_malformed_numeric_fields(self):
        corrupt_data = {
            "version": "1.2",
            "folders": [
                {
                    "id": "f-bad",
                    "name": "Bad Folder",
                    "sort_order": "not_an_int",
                }
            ],
            "prompts": [
                {
                    "id": "p-bad",
                    "title": "Bad Types",
                    "temperature": "not_a_float",
                    "use_count": None,
                }
            ],
        }
        fpath = Path(self.temp_dir.name) / "corrupt.json"
        fpath.write_text(json.dumps(corrupt_data), encoding="utf-8")

        count = import_library_from_json(self.repo, fpath)
        self.assertEqual(count, 1)

        p = self.repo.get_prompt_by_id("p-bad")
        self.assertIsNotNone(p)
        self.assertEqual(p.temperature, 0.7)
        self.assertEqual(p.use_count, 0)


if __name__ == "__main__":
    unittest.main()
