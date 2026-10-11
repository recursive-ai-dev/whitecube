"""Regression coverage for library fidelity and malformed input boundaries."""

import json
import sqlite3
from unittest.mock import patch

import pytest

from prompt_manager.core.arena import run_arena_comparison
from prompt_manager.core.exporter import _safe_float, _safe_int
from prompt_manager.core.models import Folder, Prompt, PromptRevision
from prompt_manager.integrations.llm_providers import LLMResponse
from prompt_manager.storage.backup import export_library_to_json, import_library_from_json
from prompt_manager.storage.database import Database
from prompt_manager.storage.repository import PromptRepository


@pytest.fixture
def repo(tmp_path):
    return PromptRepository(Database(tmp_path / "library.db"))


def test_import_restores_child_before_parent(repo, tmp_path):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"folders": [
        {"id": "child", "name": "A child", "parent_id": "parent"},
        {"id": "parent", "name": "Z parent"},
    ]}))
    import_library_from_json(repo, path)
    assert next(f for f in repo.list_folders() if f.id == "child").parent_id == "parent"


@pytest.mark.parametrize("value", ["false", "0", "off", False])
def test_import_preserves_false_favorite(repo, tmp_path, value):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"prompts": [{"id": "imported", "is_favorite": value}]}))
    import_library_from_json(repo, path)
    assert repo.get_prompt_by_id("imported").is_favorite is False


def test_export_propagates_template_read_failure(repo, tmp_path):
    path = tmp_path / "backup.json"
    path.write_text("previous backup")
    with patch.object(repo, "list_templates", side_effect=RuntimeError("read failed")):
        with pytest.raises(RuntimeError, match="read failed"):
            export_library_to_json(repo, path)
    assert path.read_text() == "previous backup"


def test_import_propagates_template_write_failure(repo, tmp_path):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"templates": [{"name": "example", "content": "hello"}]}))
    with patch.object(repo, "save_template", side_effect=RuntimeError("write failed")):
        with pytest.raises(RuntimeError, match="write failed"):
            import_library_from_json(repo, path)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "1e999", 10**400])
def test_numeric_overflow_uses_existing_model_defaults(value):
    assert Folder(sort_order=value).sort_order == 0
    assert PromptRevision(revision_number=value).revision_number == 1
    assert Prompt(use_count=value).use_count == 0
    assert Prompt(temperature=value).temperature == 0.7


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "1e999"])
def test_import_integer_overflow_uses_default(value):
    assert _safe_int(value) == 0


def test_import_huge_temperature_uses_default():
    assert _safe_float(10**400) == 0.7


@pytest.mark.parametrize("operation", ["read", "write", "failure"])
def test_repository_closes_connection_after_transaction(repo, operation):
    conn = repo.db.get_connection()
    with patch.object(repo.db, "get_connection", return_value=conn):
        if operation == "read":
            repo.list_prompts()
        elif operation == "write":
            repo.save_prompt(Prompt(id="saved"))
        else:
            prompt = Prompt(id="failed", tags=["ok"])
            prompt.tags = [None]  # Fail after inserting the prompt, forcing rollback.
            with pytest.raises(AttributeError):
                repo.save_prompt(prompt)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    if operation == "write":
        assert repo.get_prompt_by_id("saved") is not None
    elif operation == "failure":
        assert repo.get_prompt_by_id("failed") is None


def test_arena_preserves_duplicate_model_slots():
    # Force completion in reverse submission order without relying on timing.
    class Client:
        def execute(self, request):
            return LLMResponse(model_id=request.model_id, content=request.model_id)

    with patch("concurrent.futures.as_completed", side_effect=lambda futures: reversed(list(futures))):
        result = run_arena_comparison("hello", ["a", "b", "a"], client=Client())
    assert [r.model_id for r in result.responses] == ["a", "b", "a"]
