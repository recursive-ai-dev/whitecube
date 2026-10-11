"""Tests for hardened core data-model invariants."""

import json
from pathlib import Path
import tempfile
import unittest

from prompt_manager.core.exporter import from_csv_string, to_csv_string
from prompt_manager.core.models import (
    Folder, Prompt, PromptRevision, PromptTemplate, Tag, VariableSpec,
    normalize_tag_name,
)
from prompt_manager.core.template_engine import extract_variables
from prompt_manager.storage.backup import export_library_to_json, import_library_from_json
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository


class TestModelCoercion(unittest.TestCase):
    def test_ids_regenerated_when_blank(self):
        self.assertTrue(Folder(id="").id)
        self.assertTrue(Tag(id="  ").id)
        self.assertTrue(Prompt(id=None).id)
        self.assertTrue(PromptTemplate(id="").id)
        self.assertTrue(PromptRevision(id="").id)
        self.assertEqual(Prompt(id=" keep ").id, "keep")

    def test_timestamps_normalized(self):
        f = Folder(created_at="")
        self.assertIn("T", f.created_at)
        p = Prompt(created_at="not-a-date", updated_at="")
        self.assertIn("T", p.created_at)
        self.assertIn("T", p.updated_at)
        t = PromptTemplate(created_at="2026-01-01 00:00:00", updated_at="bad")
        self.assertTrue(t.created_at.startswith("2026-01-01T00:00:00"))
        self.assertIn("T", t.updated_at)

    def test_temperature_bounds_and_coercion(self):
        self.assertEqual(Prompt(temperature="not_a_float").temperature, 0.7)
        self.assertEqual(Prompt(temperature=None).temperature, 0.7)
        self.assertEqual(Prompt(temperature="").temperature, 0.7)
        self.assertEqual(Prompt(temperature=-1).temperature, 0.0)
        self.assertEqual(Prompt(temperature=99).temperature, 2.0)
        self.assertEqual(Prompt(temperature="0.3").temperature, 0.3)
        self.assertEqual(Prompt(temperature=float("nan")).temperature, 0.7)

    def test_use_count_floor_and_coercion(self):
        self.assertEqual(Prompt(use_count=-5).use_count, 0)
        self.assertEqual(Prompt(use_count=None).use_count, 0)
        self.assertEqual(Prompt(use_count="not_an_int").use_count, 0)
        self.assertEqual(Prompt(use_count="7.9").use_count, 7)
        self.assertEqual(Prompt(use_count=3).use_count, 3)

    def test_is_favorite_explicit_strings(self):
        self.assertFalse(Prompt(is_favorite="false").is_favorite)
        self.assertFalse(Prompt(is_favorite="0").is_favorite)
        self.assertFalse(Prompt(is_favorite="").is_favorite)
        self.assertTrue(Prompt(is_favorite="true").is_favorite)
        self.assertTrue(Prompt(is_favorite=1).is_favorite)

    def test_tags_canonical_names(self):
        p = Prompt(tags=[" #Hello ", "hello", "WORLD", "", None, "world"])
        self.assertEqual(p.tags, ["hello", "world"])
        self.assertEqual(normalize_tag_name(" #Mixed "), "mixed")
        self.assertEqual(Tag.normalize_name("#ABC"), "abc")
        t = Tag(name=" #Blue ", color="red")
        self.assertEqual(t.name, "blue")
        self.assertEqual(t.color, "#3b82f6")
        self.assertEqual(Tag(name="x", color="#ABCDEF").color, "#abcdef")

    def test_folder_parent_and_misc_coercion(self):
        f = Folder(name=None, parent_id="", icon="", sort_order="not_an_int")
        self.assertEqual(f.name, "")
        self.assertIsNone(f.parent_id)
        self.assertEqual(f.icon, "folder")
        self.assertEqual(f.sort_order, 0)
        self.assertEqual(Folder(sort_order="3").sort_order, 3)
        tmpl = PromptTemplate(category="  CODING ", description=None, content=None)
        self.assertEqual(tmpl.category, "coding")
        self.assertEqual(tmpl.description, "")
        rev = PromptRevision(revision_number=0, prompt_id=" x ")
        self.assertEqual(rev.revision_number, 1)
        self.assertEqual(rev.prompt_id, "x")

    def test_variable_spec_coercion(self):
        v = VariableSpec(name=" x ", default_value=None, is_multiline="false", options=[" a ", "", "a"])
        self.assertEqual(v.name, "x")
        self.assertEqual(v.default_value, "")
        self.assertFalse(v.is_multiline)
        self.assertEqual(v.options, ["a"])
        self.assertTrue(VariableSpec(name="a", options=["x"]).has_options)

    def test_variable_specs_stay_in_sync(self):
        content = "Hello {{name}} v={{v:1}}"
        system = "Sys {{tone:friendly|options:a,b}}"
        prompt = Prompt(template_content=content, system_instruction=system)
        tmpl = PromptTemplate(content=content, system_instruction=system)
        expected = [(s.name, s.default_value, s.options) for s in extract_variables(content + "\n" + system)]
        got_p = [(s.name, s.default_value, s.options) for s in prompt.variable_specs()]
        got_t = [(s.name, s.default_value, s.options) for s in tmpl.variable_specs()]
        self.assertEqual(got_p, expected)
        self.assertEqual(got_t, expected)


class TestRepositoryInvariants(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "models.db")
        self.repo = PromptRepository(self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_orphan_folder_and_template_nullified_not_crash(self):
        p = self.repo.save_prompt(Prompt(title="orphan", template_content="hi", folder_id="missing", template_id="missing"))
        self.assertIsNone(p.folder_id)
        self.assertIsNone(p.template_id)
        fetched = self.repo.get_prompt_by_id(p.id)
        self.assertIsNotNone(fetched)
        self.assertIsNone(fetched.folder_id)
        self.assertIsNone(fetched.template_id)

    def test_orphan_parent_and_self_parent_nullified(self):
        f = self.repo.save_folder(Folder(name="child", parent_id="missing"))
        self.assertIsNone(f.parent_id)
        f.parent_id = f.id
        self.repo.save_folder(f)
        fetched = next(x for x in self.repo.list_folders() if x.id == f.id)
        self.assertIsNone(fetched.parent_id)

    def test_folder_delete_sets_prompt_null(self):
        folder = self.repo.save_folder(Folder(name="doomed"))
        p = self.repo.save_prompt(Prompt(title="p", template_content="hi", folder_id=folder.id))
        self.assertEqual(p.folder_id, folder.id)
        self.repo.delete_folder(folder.id)
        self.assertIsNone(self.repo.get_prompt_by_id(p.id).folder_id)

    def test_tags_resolved_names_not_ids(self):
        p = self.repo.save_prompt(Prompt(title="t", template_content="hi", tags=[" #Beta ", "alpha", "beta"]))
        fetched = self.repo.get_prompt_by_id(p.id)
        self.assertEqual(fetched.tags, ["alpha", "beta"])

    def test_temperature_clamped_on_persist(self):
        p = self.repo.save_prompt(Prompt(title="t", template_content="hi", temperature=99))
        self.assertEqual(self.repo.get_prompt_by_id(p.id).temperature, 2.0)

    def test_csv_roundtrip_preserves_refs_and_counts(self):
        folder = self.repo.save_folder(Folder(name="csv-folder"))
        tmpl = self.repo.save_template(PromptTemplate(name="csv-tmpl", content="Hello {{n}}"))
        original = self.repo.save_prompt(Prompt(title="csv", template_content="hi", folder_id=folder.id, template_id=tmpl.id, tags=["a"]))
        self.repo.increment_use_count(original.id)
        stored = self.repo.get_prompt_by_id(original.id)
        csv_text = to_csv_string([stored])
        self.assertIn("folder_id", csv_text)
        self.assertIn("template_id", csv_text)
        self.assertIn("use_count", csv_text)
        parsed = from_csv_string(csv_text)[0]
        self.assertEqual(parsed.folder_id, folder.id)
        self.assertEqual(parsed.template_id, tmpl.id)
        self.assertEqual(parsed.use_count, 1)

    def test_legacy_csv_without_new_columns_still_parses(self):
        legacy = ("id,title,description,template_content,system_instruction,target_model,temperature,tags,is_favorite,created_at,updated_at\n" "id1,Test Title,Desc,Hello {{var}},Sys,General,,,0,,\n")
        parsed = from_csv_string(legacy)
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0].folder_id)
        self.assertIsNone(parsed[0].template_id)
        self.assertEqual(parsed[0].use_count, 0)

    def test_json_roundtrip_no_silent_loss(self):
        folder = self.repo.save_folder(Folder(name="j-folder"))
        prompt = self.repo.save_prompt(Prompt(title="j", template_content="Hello {{v}}", folder_id=folder.id, temperature=0.3, tags=["k"]))
        export_path = Path(self.temp_dir.name) / "lib.json"
        export_library_to_json(self.repo, export_path)
        data = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertTrue(any(pp["id"] == prompt.id and pp["folder_id"] == folder.id for pp in data["prompts"]))
        db2 = Database(Path(self.temp_dir.name) / "lib2.db")
        repo2 = PromptRepository(db2)
        with db2.get_connection() as conn:
            conn.execute("DELETE FROM prompt_tags")
            conn.execute("DELETE FROM prompt_revisions")
            conn.execute("DELETE FROM prompts")
            conn.execute("DELETE FROM folders")
            conn.execute("DELETE FROM tags")
        import_library_from_json(repo2, export_path)
        fetched = repo2.get_prompt_by_id(prompt.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.folder_id, folder.id)
        self.assertEqual(fetched.temperature, 0.3)


if __name__ == "__main__":
    unittest.main()
