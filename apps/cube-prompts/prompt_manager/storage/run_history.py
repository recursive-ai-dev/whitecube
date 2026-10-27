"""Local arena snapshots, retained even after their source prompt is deleted."""

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from typing import Optional
import uuid

from prompt_manager.core.arena import ArenaResult
from prompt_manager.integrations.llm_providers import LLMResponse
from prompt_manager.storage.database import Database


def validate_responses(responses: list[dict]) -> None:
    """Reject snapshots that would break history rendering or cost totals."""
    if not isinstance(responses, list) or any(not isinstance(item, dict) for item in responses):
        raise ValueError("Backup run responses must be a list of objects")
    for item in responses:
        try:
            response = LLMResponse(**item)
        except TypeError as error:
            raise ValueError("Backup run response has invalid fields") from error
        for name in ("content", "model_id", "provider"):
            if not isinstance(getattr(response, name), str):
                raise ValueError(f"Run response {name} must be text")
        if response.error is not None and not isinstance(response.error, str):
            raise ValueError("Run response error must be text")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(response, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Run response {name} must be a nonnegative integer")
        for name in ("elapsed_seconds", "estimated_cost_usd"):
            value = getattr(response, name)
            try:
                valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"Run response {name} must be a nonnegative finite number")


@dataclass
class SavedRun:
    id: str
    prompt_id: Optional[str]
    title: str
    created_at: str
    prompt: str
    system_instruction: str
    variables: dict[str, str]
    temperature: float
    responses: list[LLMResponse]
    max_tokens: int = 2048

    @property
    def total_cost_usd(self) -> float:
        return sum(response.estimated_cost_usd for response in self.responses)


class RunHistoryRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, prompt_id, title, result: ArenaResult, variables=None, temperature=0.7, max_tokens=2048) -> str:
        """Store one complete comparison with its original input and errors."""
        run_id = str(uuid.uuid4())
        responses = [asdict(response) for response in result.responses]
        validate_responses(responses)
        responses_json = json.dumps(responses, ensure_ascii=False, allow_nan=False)
        variables_json = json.dumps(dict(variables or {}), ensure_ascii=False, allow_nan=False)
        with closing(self.db.get_connection()) as conn, conn:
            if prompt_id and not conn.execute("SELECT 1 FROM prompts WHERE id = ?", (prompt_id,)).fetchone():
                prompt_id = None
            conn.execute(
                "INSERT INTO arena_runs (id, prompt_id, title, created_at, prompt, system_instruction, variables_json, temperature, max_tokens, responses_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, prompt_id, title, datetime.now(timezone.utc).isoformat(), result.prompt, result.system_instruction, variables_json, temperature, max_tokens, responses_json),
            )
        return run_id

    @staticmethod
    def _from_row(row) -> SavedRun:
        return SavedRun(
            id=row['id'], prompt_id=row['prompt_id'], title=row['title'], created_at=row['created_at'],
            prompt=row['prompt'], system_instruction=row['system_instruction'],
            variables=json.loads(row['variables_json']), temperature=row['temperature'], max_tokens=row['max_tokens'],
            responses=[LLMResponse(**response) for response in json.loads(row['responses_json'])],
        )

    def list_runs(self, prompt_id=None, limit=100) -> list[SavedRun]:
        limit = max(1, min(int(limit), 1000))
        where, args = ("WHERE prompt_id = ?", [prompt_id]) if prompt_id else ("", [])
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(f"SELECT * FROM arena_runs {where} ORDER BY created_at DESC, id DESC LIMIT ?", args + [limit]).fetchall()
        return [self._from_row(row) for row in rows]

    def get_run(self, run_id) -> Optional[SavedRun]:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT * FROM arena_runs WHERE id = ?", (run_id,)).fetchone()
        return self._from_row(row) if row else None

    def delete_run(self, run_id) -> bool:
        with closing(self.db.get_connection()) as conn, conn:
            return conn.execute("DELETE FROM arena_runs WHERE id = ?", (run_id,)).rowcount > 0
