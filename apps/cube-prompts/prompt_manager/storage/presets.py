"""Named variable values for repeatable prompt experiments."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

from prompt_manager.storage.database import Database


@dataclass
class VariablePreset:
    id: str
    prompt_id: str
    name: str
    values: dict[str, str]
    updated_at: str


class PresetRepository:
    def __init__(self, db: Database):
        self.db = db

    def save_preset(self, prompt_id, name, values) -> VariablePreset:
        name = str(name).strip()
        if not name:
            raise ValueError("Preset name must not be empty")
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise ValueError("Preset values must map variable names to text")
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self.db.get_connection()) as conn, conn:
            if not conn.execute("SELECT 1 FROM prompts WHERE id = ?", (prompt_id,)).fetchone():
                raise ValueError("Save this prompt before creating a preset")
            row = conn.execute("SELECT id FROM variable_presets WHERE prompt_id = ? AND name = ?", (prompt_id, name)).fetchone()
            preset_id = row['id'] if row else str(uuid.uuid4())
            conn.execute("INSERT INTO variable_presets (id, prompt_id, name, values_json, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(prompt_id, name) DO UPDATE SET values_json=excluded.values_json, updated_at=excluded.updated_at", (preset_id, prompt_id, name, json.dumps(values, ensure_ascii=False), updated_at))
        return VariablePreset(preset_id, prompt_id, name, dict(values), updated_at)

    def list_presets(self, prompt_id) -> list[VariablePreset]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute("SELECT * FROM variable_presets WHERE prompt_id = ? ORDER BY name COLLATE NOCASE", (prompt_id,)).fetchall()
        return [VariablePreset(row['id'], row['prompt_id'], row['name'], json.loads(row['values_json']), row['updated_at']) for row in rows]

    def delete_preset(self, preset_id) -> bool:
        with closing(self.db.get_connection()) as conn, conn:
            return conn.execute("DELETE FROM variable_presets WHERE id = ?", (preset_id,)).rowcount > 0
