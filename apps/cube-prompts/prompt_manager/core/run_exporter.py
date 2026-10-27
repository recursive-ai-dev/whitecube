"""Portable comparison reports made only from saved inputs and responses."""

from dataclasses import asdict
import json
import re


def export_run_json(run) -> str:
    payload = asdict(run)
    payload['format'] = 'cube-prompts-comparison'
    payload['version'] = 1
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def _fenced(text: str) -> str:
    # Inputs can contain Markdown fences: make the report's fence longer.
    longest = max((len(match) for match in re.findall(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}\n{text}\n{fence}'


def export_run_markdown(run) -> str:
    title = run.title.replace('\n', ' ').replace('\r', ' ')
    lines = [f'# {title}', '', f'Created: {run.created_at}',
             f'Temperature: {run.temperature:g}',
             f'Maximum output tokens: {run.max_tokens}',
             f'Estimated total cost: ${run.total_cost_usd:.6f}', '', '## Inputs', '']
    if run.system_instruction:
        lines += ['### System instruction', '', _fenced(run.system_instruction), '']
    lines += ['### Prompt', '', _fenced(run.prompt), '']
    if run.variables:
        lines += ['### Variables', '', _fenced(json.dumps(run.variables, ensure_ascii=False, indent=2)), '']
    for response in run.responses:
        lines += [f'## {response.model_id}', '',
                  f'Status: {"success" if response.is_success else "failed"}',
                  f'Time: {response.elapsed_seconds:.2f} s',
                  f'Tokens: {response.prompt_tokens} input / {response.completion_tokens} output',
                  f'Estimated cost: ${response.estimated_cost_usd:.6f}', '',
                  _fenced(response.content if response.is_success else response.error or 'Empty answer.'), '']
    return '\n'.join(lines)
