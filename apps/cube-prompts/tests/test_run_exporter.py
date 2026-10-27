import json

from prompt_manager.core.arena import ArenaResult
from prompt_manager.core.run_exporter import export_run_json, export_run_markdown
from prompt_manager.integrations.llm_providers import LLMResponse
from prompt_manager.storage.database import Database
from prompt_manager.storage.run_history import RunHistoryRepository


def saved_run(tmp_path):
    history = RunHistoryRepository(Database(tmp_path / 'library.db'))
    result = ArenaResult('Review ```python\nprint("héllo")\n```', 'Be precise.', [
        LLMResponse(content='Good code.', model_id='ollama:local', elapsed_seconds=0.25),
        LLMResponse(error='No API key configured.', model_id='openai:custom'),
    ])
    return history.get_run(history.save(None, 'Code review', result, variables={'language': 'Python'}))


def test_json_report_contains_inputs_responses_and_schema(tmp_path):
    run = saved_run(tmp_path)
    report = json.loads(export_run_json(run))
    assert report['format'] == 'cube-prompts-comparison'
    assert report['prompt'] == run.prompt
    assert report['variables'] == {'language': 'Python'}
    assert report['responses'][1]['error'] == 'No API key configured.'
    assert 'api_key' not in report


def test_markdown_uses_safe_fences_and_keeps_failures(tmp_path):
    report = export_run_markdown(saved_run(tmp_path))
    assert '````\nReview ```python' in report
    assert 'Status: success' in report
    assert 'Status: failed' in report
    assert 'No API key configured.' in report
    assert 'héllo' in report
