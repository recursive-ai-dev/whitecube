"""Regression tests for atomic imports and persistent experiment data."""

from contextlib import closing
import json
from unittest.mock import patch

import pytest

from prompt_manager.core.arena import ArenaResult
from prompt_manager.core.models import Folder, Prompt, PromptTemplate
from prompt_manager.integrations.llm_providers import LLMResponse
from prompt_manager.storage import backup
from prompt_manager.storage.database import Database
from prompt_manager.storage.presets import PresetRepository
from prompt_manager.storage.repository import PromptRepository
from prompt_manager.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path):
    return PromptRepository(Database(tmp_path / 'library.db'))


def test_empty_library_stays_empty_after_restart(repo):
    for prompt in repo.list_prompts():
        repo.delete_prompt(prompt.id)
    reopened = PromptRepository(Database(repo.db.db_path))
    assert reopened.list_prompts() == []
    assert len(reopened.list_folders()) == 2


def test_import_failure_rolls_back_folders_and_content(repo, tmp_path):
    original = repo.save_prompt(Prompt(id='original', title='Before', template_content='before'))
    path = tmp_path / 'broken.json'
    path.write_text(json.dumps({'folders': [{'id': 'new-folder', 'name': 'New'}], 'prompts': [
        {'id': original.id, 'title': 'After'}, {'id': 'broken', 'title': 'Broken'}]}))
    save = repo.save_prompt

    def fail_after_update(prompt, *args, **kwargs):
        if prompt.id == 'broken':
            raise RuntimeError('late write error')
        return save(prompt, *args, **kwargs)

    with patch.object(repo, 'save_prompt', side_effect=fail_after_update):
        with pytest.raises(RuntimeError, match='late write error'):
            backup.import_library_from_json(repo, path)
    assert repo.get_prompt_by_id(original.id).title == 'Before'
    assert not any(folder.id == 'new-folder' for folder in repo.list_folders())
    assert repo.get_prompt_by_id('broken') is None
    # The connection state must reset after rollback.
    repo.save_prompt(Prompt(id='subsequent'))
    assert repo.get_prompt_by_id('subsequent') is not None


@pytest.mark.parametrize('data', [[], {'prompts': {}}, {'prompts': [None]}, {'folders': [{'id': 'same'}, {'id': 'same'}]}])
def test_malformed_import_has_clear_error_and_no_mutations(repo, tmp_path, data):
    before = [p.id for p in repo.list_prompts()]
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Backup'):
        backup.import_library_from_json(repo, path)
    assert [p.id for p in repo.list_prompts()] == before


def test_folder_reparenting_cannot_create_descendant_cycle(repo):
    ancestor = repo.save_folder(Folder(name='Ancestor'))
    child = repo.save_folder(Folder(name='Child', parent_id=ancestor.id))
    grandchild = repo.save_folder(Folder(name='Grandchild', parent_id=child.id))
    ancestor.parent_id = grandchild.id
    repo.save_folder(ancestor)
    assert ancestor.parent_id is None
    assert next(f for f in repo.list_folders() if f.id == child.id).parent_id == ancestor.id


def test_fts_backfills_preexisting_content_when_enabled(repo):
    if not repo.db.fts_enabled:
        pytest.skip('SQLite without FTS5')
    prompt = repo.save_prompt(Prompt(title='quokka searchable'))
    template = repo.save_template(PromptTemplate(name='quokka template', content='hello'))
    with closing(repo.db.get_connection()) as conn, conn:
        for name in ('prompts_ai', 'prompts_ad', 'prompts_au', 'templates_ai', 'templates_ad', 'templates_au'):
            conn.execute(f'DROP TRIGGER {name}')
        conn.execute('DROP TABLE prompts_fts')
        conn.execute('DROP TABLE templates_fts')
    reopened = PromptRepository(Database(repo.db.db_path))
    assert prompt.id in [p.id for p in reopened.list_prompts(search_query='quokka')]
    assert template.id in [p.id for p in reopened.list_templates(search_query='quokka')]


def test_run_history_roundtrip_keeps_failures_and_original_snapshot(repo):
    prompt = repo.save_prompt(Prompt(title='Source', template_content='old {{name}}'))
    history = RunHistoryRepository(repo.db)
    result = ArenaResult(prompt='old Alice', system_instruction='Be precise', responses=[
        LLMResponse(model_id='provider:model', content='Answer', elapsed_seconds=1.2, prompt_tokens=10,
                    completion_tokens=20, total_tokens=30, estimated_cost_usd=.002),
        LLMResponse(model_id='provider:failed', error='Rate limit')])
    run_id = history.save(prompt.id, prompt.title, result, variables={'name': 'Alice'}, temperature=.3)
    prompt.template_content = 'new content'
    repo.save_prompt(prompt)
    saved = RunHistoryRepository(Database(repo.db.db_path)).get_run(run_id)
    assert saved.prompt == 'old Alice'
    assert saved.system_instruction == 'Be precise'
    assert saved.variables == {'name': 'Alice'}
    assert saved.responses[0].total_tokens == 30
    assert saved.responses[1].error == 'Rate limit'
    assert saved.total_cost_usd == .002
    assert saved.temperature == .3
    assert history.list_runs(prompt.id)[0].id == run_id
    repo.delete_prompt(prompt.id)
    assert history.get_run(run_id).prompt_id is None
    assert history.get_run(run_id).title == 'Source'
    assert history.delete_run(run_id) is True
    assert history.get_run(run_id) is None


def test_presets_persist_update_and_follow_prompt_lifetime(repo):
    prompt = repo.save_prompt(Prompt())
    presets = PresetRepository(repo.db)
    first = presets.save_preset(prompt.id, 'Sample', {'name': 'Alice', 'body': 'one\ntwo'})
    updated = presets.save_preset(prompt.id, 'Sample', {'name': 'Bob'})
    assert first.id == updated.id
    assert PresetRepository(Database(repo.db.db_path)).list_presets(prompt.id)[0].values == {'name': 'Bob'}
    with pytest.raises(ValueError, match='name'):
        presets.save_preset(prompt.id, '', {})
    with pytest.raises(ValueError, match='text'):
        presets.save_preset(prompt.id, 'Invalid', {'name': 12})
    repo.delete_prompt(prompt.id)
    assert presets.list_presets(prompt.id) == []


def test_complete_backup_restores_revisions_presets_and_run_history(repo, tmp_path):
    prompt = repo.save_prompt(Prompt(title='Portable experiment', template_content='before'))
    prompt.template_content = 'after'
    repo.save_prompt(prompt, create_revision=True)
    PresetRepository(repo.db).save_preset(prompt.id, 'Test inputs', {'name': 'Alice'})
    history = RunHistoryRepository(repo.db)
    run_id = history.save(prompt.id, prompt.title, ArenaResult(prompt='after', system_instruction='', responses=[
        LLMResponse(model_id='local:model', content='answer')]), max_tokens=4096)
    path = tmp_path / 'complete.json'
    count = backup.export_library_to_json(repo, path)
    # Target seeds have different ids but matching tag names; these merge.
    target = PromptRepository(Database(tmp_path / 'target.db'))
    assert backup.import_library_from_json(target, path) == count
    assert target.get_revisions(prompt.id)[0].template_content == 'before'
    assert PresetRepository(target.db).list_presets(prompt.id)[0].values == {'name': 'Alice'}
    saved = RunHistoryRepository(target.db).get_run(run_id)
    assert saved.responses[0].content == 'answer'
    assert saved.max_tokens == 4096
    # Reimporting the same backup doesn't duplicate experiments.
    backup.import_library_from_json(target, path)
    assert len(RunHistoryRepository(target.db).list_runs(prompt.id)) == 1
    assert len(PresetRepository(target.db).list_presets(prompt.id)) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_import_merges_named_templates_and_preserves_prompt_link(repo, tmp_path):
    existing = repo.save_template(PromptTemplate(name='Shared template', content='old'))
    path = tmp_path / 'merge.json'
    path.write_text(json.dumps({'templates': [{'id': 'foreign-template', 'name': 'Shared template', 'content': 'new'}],
                                'prompts': [{'id': 'foreign-prompt', 'template_id': 'foreign-template'}]}))
    backup.import_library_from_json(repo, path)
    assert repo.get_template_by_id(existing.id).content == 'new'
    assert repo.get_prompt_by_id('foreign-prompt').template_id == existing.id


def test_invalid_run_backup_rolls_back_library_content(repo, tmp_path):
    path = tmp_path / 'bad-history.json'
    path.write_text(json.dumps({'prompts': [{'id': 'should-rollback'}],
                               'runs': [{'id': 'bad-run', 'responses_json': '{}'}]}))
    with pytest.raises(ValueError, match='responses'):
        backup.import_library_from_json(repo, path)
    assert repo.get_prompt_by_id('should-rollback') is None


@pytest.mark.parametrize('metric', ['estimated_cost_usd', 'elapsed_seconds', 'total_tokens'])
def test_import_rejects_malformed_run_metrics(repo, tmp_path, metric):
    path = tmp_path / 'bad-metric.json'
    path.write_text(json.dumps({'prompts': [{'id': 'rollback-metric'}], 'runs': [{
        'id': 'bad-metric', 'responses_json': json.dumps([{metric: 'broken'}])}]}))
    with pytest.raises(ValueError, match=metric):
        backup.import_library_from_json(repo, path)
    assert repo.get_prompt_by_id('rollback-metric') is None
