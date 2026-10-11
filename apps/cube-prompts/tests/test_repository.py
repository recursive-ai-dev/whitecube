import tempfile
from pathlib import Path
import unittest

from prompt_manager.core.models import Folder, Prompt, Tag
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository


class TestRepository(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        self.repo = PromptRepository(self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_starter_data_seeded(self):
        prompts = self.repo.list_prompts()
        self.assertGreater(len(prompts), 0)
        folders = self.repo.list_folders()
        self.assertGreater(len(folders), 0)

    def test_create_and_read_prompt(self):
        prompt = Prompt(
            title="Custom Test Prompt",
            description="A test description",
            template_content="Hello {{world}}",
            system_instruction="You are a test helper",
            tags=["test", "custom"],
        )
        saved = self.repo.save_prompt(prompt)
        self.assertEqual(saved.id, prompt.id)

        fetched = self.repo.get_prompt_by_id(prompt.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.title, "Custom Test Prompt")
        self.assertEqual(fetched.tags, ["custom", "test"])

    def test_full_text_search(self):
        prompt = Prompt(
            title="Kubernetes Helm Deployment Helper",
            description="Generates helm chart templates",
            template_content="Deploy {{service_name}} to cluster",
        )
        self.repo.save_prompt(prompt)

        results = self.repo.list_prompts(search_query="Kubernetes")
        self.assertTrue(any(p.title == "Kubernetes Helm Deployment Helper" for p in results))

        results_by_body = self.repo.list_prompts(search_query="cluster")
        self.assertTrue(any(p.title == "Kubernetes Helm Deployment Helper" for p in results_by_body))

    def test_revisions(self):
        prompt = Prompt(
            title="Versioned Prompt",
            template_content="Initial Version",
        )
        self.repo.save_prompt(prompt)

        prompt.template_content = "Updated Version 2"
        self.repo.save_prompt(prompt, create_revision=True)

        revisions = self.repo.get_revisions(prompt.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].template_content, "Initial Version")
        self.assertEqual(self.repo.get_prompt_by_id(prompt.id).template_content, "Updated Version 2")

        prompt.template_content = revisions[0].template_content
        self.repo.save_prompt(prompt, create_revision=True)
        self.assertEqual(self.repo.get_prompt_by_id(prompt.id).template_content, "Initial Version")
        self.assertEqual(
            [rev.template_content for rev in self.repo.get_revisions(prompt.id)],
            ["Updated Version 2", "Initial Version"],
        )


if __name__ == "__main__":
    unittest.main()


def test_revision_keeps_all_restorable_fields_and_failed_save_rolls_back(tmp_path):
    from unittest.mock import patch
    import pytest

    repo = PromptRepository(Database(tmp_path / "revisions.db"))
    prompt = Prompt(title="Original title", template_content="Original body", system_instruction="Original system")
    repo.save_prompt(prompt, create_revision=True)
    assert repo.get_revisions(prompt.id) == []
    prompt.title = "New title"
    prompt.template_content = "New body"
    prompt.system_instruction = "New system"
    record = repo._record_revision

    def fail_after_snapshot(conn, previous):
        record(conn, previous)
        raise RuntimeError("simulated interrupted save")

    with patch.object(repo, "_record_revision", side_effect=fail_after_snapshot):
        with pytest.raises(RuntimeError):
            repo.save_prompt(prompt, create_revision=True)
    assert repo.get_revisions(prompt.id) == []
    assert repo.get_prompt_by_id(prompt.id).template_content == "Original body"

    repo.save_prompt(prompt, create_revision=True)
    revision = repo.get_revisions(prompt.id)[0]
    assert (revision.title, revision.template_content, revision.system_instruction) == (
        "Original title", "Original body", "Original system",
    )
    assert revision.revision_number == 1
