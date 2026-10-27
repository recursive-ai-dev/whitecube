"""Import and export entire prompt library to JSON, CSV, and portable packages."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List
import zipfile

from prompt_manager.core.exporter import (
    _safe_float,
    _safe_int,
    from_csv_string,
    to_csv_string,
    to_markdown_frontmatter,
)
from prompt_manager.core.models import Folder, Prompt, PromptTemplate, Tag
from prompt_manager.storage.repository import PromptRepository


@contextmanager
def _atomic_path(filepath: Path):
    """Publish a private export only when the complete file is ready."""
    fd, name = tempfile.mkstemp(prefix=f".{filepath.name}.", suffix=".tmp", dir=filepath.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(filepath)
    finally:
        temporary.unlink(missing_ok=True)


def export_library_to_json(repo: PromptRepository, filepath: Path) -> int:
    """Export the library, presets, revisions and arena history consistently."""
    with repo.db.transaction():
        folders = repo.list_folders()
        tags = repo.list_tags()
        prompts = repo.list_prompts()
        templates = repo.list_templates()

        data: Dict[str, Any] = {
            "version": "1.3",
            "folders": [
                {
                    "id": f.id,
                    "name": f.name,
                    "parent_id": f.parent_id,
                    "icon": f.icon,
                    "sort_order": f.sort_order,
                    "created_at": f.created_at,
                }
                for f in folders
            ],
            "tags": [{"id": t.id, "name": t.name, "color": t.color} for t in tags],
            "templates": [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "content": t.content,
                    "system_instruction": t.system_instruction,
                    "category": t.category,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
                for t in templates
            ],
            "prompts": [
                {
                    "id": p.id,
                    "title": p.title,
                    "description": p.description,
                    "folder_id": p.folder_id,
                    "template_id": p.template_id,
                    "template_content": p.template_content,
                    "system_instruction": p.system_instruction,
                    "target_model": p.target_model,
                    "temperature": p.temperature,
                    "is_favorite": p.is_favorite,
                    "use_count": p.use_count,
                    "tags": p.tags,
                    "created_at": p.created_at,
                    "updated_at": p.updated_at,
                }
                for p in prompts
            ],
        }

        # These sections retain all local experiment data in a portable backup.
        with closing(repo.db.get_connection()) as conn:
            data["revisions"] = [dict(row) for row in conn.execute("SELECT * FROM prompt_revisions ORDER BY prompt_id, revision_number")]
            data["presets"] = [dict(row) for row in conn.execute("SELECT * FROM variable_presets ORDER BY prompt_id, name")]
            data["runs"] = [dict(row) for row in conn.execute("SELECT * FROM arena_runs ORDER BY created_at")]
    with _atomic_path(filepath) as tmp_path:
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(prompts)


def import_library_from_json(repo: PromptRepository, filepath: Path) -> int:
    """Import prompts, folders, tags, and prompt_templates from JSON backup.

    Missing ids fall back to fresh UUIDs via model coercion (never KeyError);
    malformed numerics fall back (temperature->0.7, use_count/sort_order->0);
    orphan folder_id/template_id/parent_id are nullified by the repository
    (never crash). Version 1.3 also restores revisions, variable presets and
    arena runs; older backups without these sections remain supported.
    """
    content = filepath.read_text(encoding="utf-8")
    data = json.loads(content)

    if not isinstance(data, dict):
        raise ValueError("Backup must contain a JSON object")
    for section in ("folders", "tags", "templates", "prompts", "revisions", "presets", "runs"):
        rows = data.get(section, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Backup '{section}' must be a list of objects")
        ids = [str(row["id"]).strip() for row in rows if row.get("id")]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Backup '{section}' contains duplicate ids")

    # A late validation or write failure must never leave half a library.
    with repo.db.transaction():
        folders = [
            Folder(
                id=f_data.get("id") or None,
                name=f_data.get("name", ""),
                parent_id=f_data.get("parent_id"),
                icon=f_data.get("icon", "folder"),
                sort_order=_safe_int(f_data.get("sort_order"), default=0),
                created_at=f_data.get("created_at", ""),
            )
            for f_data in data.get("folders", [])
        ]
        # Backups are sorted by name, so a child's parent may appear later.
        # Keep the original links while the repository nullifies missing parents.
        parent_ids = [folder.parent_id for folder in folders]
        for folder in folders:
            repo.save_folder(folder)
        for folder, parent_id in zip(folders, parent_ids):
            if parent_id != folder.parent_id:
                folder.parent_id = parent_id
                repo.save_folder(folder)

        known_tags = {tag.name: tag for tag in repo.list_tags()}
        for t_data in data.get("tags", []):
            tag = Tag(id=t_data.get("id") or None, name=t_data.get("name", ""), color=t_data.get("color", "#3b82f6"))
            existing = known_tags.get(tag.name)
            if existing and existing.id != tag.id:
                tag.id = existing.id
            repo.save_tag(tag)
            known_tags[tag.name] = tag

        # Merge templates by unique name and remap imported prompt links.
        template_ids = {}
        for tmpl_data in data.get("templates", []):
            template = PromptTemplate(
                id=tmpl_data.get("id") or None,
                name=tmpl_data.get("name", "Untitled Template"),
                description=tmpl_data.get("description", ""),
                content=tmpl_data.get("content", ""),
                system_instruction=tmpl_data.get("system_instruction", ""),
                category=tmpl_data.get("category", "general"),
                created_at=tmpl_data.get("created_at", ""),
                updated_at=tmpl_data.get("updated_at", ""),
            )
            original_id = template.id
            existing = repo.get_template_by_name(template.name)
            if existing and existing.id != template.id and not repo.get_template_by_id(template.id):
                template.id = existing.id
            repo.save_template(template)
            template_ids[original_id] = template.id

        imported_count = 0
        for p_data in data.get("prompts", []):
            p = Prompt(
                id=p_data.get("id") or None,
                title=p_data.get("title", "Imported Prompt"),
                description=p_data.get("description", ""),
                folder_id=p_data.get("folder_id"),
                template_id=template_ids.get(p_data.get("template_id"), p_data.get("template_id")),
                template_content=p_data.get("template_content", ""),
                system_instruction=p_data.get("system_instruction", ""),
                target_model=p_data.get("target_model", "General"),
                temperature=_safe_float(p_data.get("temperature"), default=0.7),
                is_favorite=p_data.get("is_favorite", False),
                use_count=_safe_int(p_data.get("use_count"), default=0),
                tags=p_data.get("tags", []),
                created_at=p_data.get("created_at", ""),
                updated_at=p_data.get("updated_at", ""),
            )
            previous = repo.get_prompt_by_id(p.id)
            changed = previous is not None and any(
                getattr(previous, field) != getattr(p, field)
                for field in ("title", "template_content", "system_instruction")
            )
            repo.save_prompt(p, create_revision=changed)
            imported_count += 1

        _restore_experiments(repo, data)
        return imported_count


def export_library_to_csv(repo: PromptRepository, filepath: Path) -> int:
    """Export all prompts to a CSV file."""
    prompts = repo.list_prompts()
    csv_content = to_csv_string(prompts)
    with _atomic_path(filepath) as tmp_path:
        tmp_path.write_text(csv_content, encoding="utf-8")
    return len(prompts)


def import_library_from_csv(repo: PromptRepository, filepath: Path) -> int:
    """Import prompts from a CSV file."""
    content = filepath.read_text(encoding="utf-8")
    prompts = from_csv_string(content)
    with repo.db.transaction():
        for p in prompts:
            repo.save_prompt(p, create_revision=True)
    return len(prompts)


def export_library_to_markdown_zip(repo: PromptRepository, filepath: Path) -> int:
    """Export all prompts as individual Markdown files inside a ZIP archive."""
    prompts = repo.list_prompts()
    used_names: set[str] = set()
    with _atomic_path(filepath) as tmp_path:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in prompts:
                clean_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in p.title).strip() or "prompt"
                filename = f"{clean_name}.md"
                if filename in used_names:
                    filename = f"{clean_name}_{p.id[:8]}.md"
                counter = 1
                while filename in used_names:
                    filename = f"{clean_name}_{p.id[:8]}_{counter}.md"
                    counter += 1
                used_names.add(filename)
                md_content = to_markdown_frontmatter(p)
                zf.writestr(filename, md_content)
    return len(prompts)


def _restore_experiments(repo: PromptRepository, data: dict) -> None:
    """Restore optional v1.3 sections within the caller's import transaction."""
    from prompt_manager.core.models import PromptRevision
    from prompt_manager.storage.run_history import validate_responses
    from prompt_manager.storage.presets import PresetRepository

    with closing(repo.db.get_connection()) as conn:
        for item in data.get('revisions', []):
            revision = PromptRevision(**{key: value for key, value in item.items() if key in PromptRevision.__dataclass_fields__})
            if conn.execute('SELECT 1 FROM prompts WHERE id = ?', (revision.prompt_id,)).fetchone():
                conn.execute('INSERT OR IGNORE INTO prompt_revisions (id, prompt_id, revision_number, title, template_content, system_instruction, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (revision.id, revision.prompt_id, revision.revision_number, revision.title, revision.template_content, revision.system_instruction, revision.created_at))
        for item in data.get('presets', []):
            PresetRepository(repo.db).save_preset(item.get('prompt_id'), item.get('name', ''), json.loads(item.get('values_json', '{}')))
        for item in data.get('runs', []):
            responses = json.loads(item.get('responses_json', '[]'))
            validate_responses(responses)
            variables = json.loads(item.get('variables_json', '{}'))
            if not isinstance(variables, dict):
                raise ValueError('Backup run variables must be an object')
            prompt_id = item.get('prompt_id')
            if not conn.execute('SELECT 1 FROM prompts WHERE id = ?', (prompt_id,)).fetchone():
                prompt_id = None
            conn.execute('INSERT OR IGNORE INTO arena_runs (id, prompt_id, title, created_at, prompt, system_instruction, variables_json, temperature, max_tokens, responses_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (item.get('id') or str(uuid.uuid4()), prompt_id, str(item.get('title', 'Imported run')), str(item.get('created_at', '')), str(item.get('prompt', '')), str(item.get('system_instruction', '')), json.dumps(variables), _safe_float(item.get('temperature'), .7), _safe_int(item.get('max_tokens'), 2048), json.dumps(responses)))
