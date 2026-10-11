"""Import and export entire prompt library to JSON, CSV, and portable packages."""

from __future__ import annotations

import json
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


def export_library_to_json(repo: PromptRepository, filepath: Path) -> int:
    """Export all prompts, folders, tags, and prompt_templates to a portable JSON file."""
    folders = repo.list_folders()
    tags = repo.list_tags()
    prompts = repo.list_prompts()
    templates = repo.list_templates()

    data: Dict[str, Any] = {
        "version": "1.2",
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

    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(filepath)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return len(prompts)


def import_library_from_json(repo: PromptRepository, filepath: Path) -> int:
    """Import prompts, folders, tags, and prompt_templates from JSON backup.

    Missing ids fall back to fresh UUIDs via model coercion (never KeyError);
    malformed numerics fall back (temperature->0.7, use_count/sort_order->0);
    orphan folder_id/template_id/parent_id are nullified by the repository
    (never crash). Revisions are intentionally not part of the format.
    """
    content = filepath.read_text(encoding="utf-8")
    data = json.loads(content)

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

    for t_data in data.get("tags", []):
        repo.save_tag(
            Tag(
                id=t_data.get("id") or None,
                name=t_data.get("name", ""),
                color=t_data.get("color", "#3b82f6"),
            )
        )

    # Import templates first (prompts may reference template_id)
    for tmpl_data in data.get("templates", []):
        repo.save_template(
            PromptTemplate(
                id=tmpl_data.get("id") or None,
                name=tmpl_data.get("name", "Untitled Template"),
                description=tmpl_data.get("description", ""),
                content=tmpl_data.get("content", ""),
                system_instruction=tmpl_data.get("system_instruction", ""),
                category=tmpl_data.get("category", "general"),
                created_at=tmpl_data.get("created_at", ""),
                updated_at=tmpl_data.get("updated_at", ""),
            )
        )

    imported_count = 0
    for p_data in data.get("prompts", []):
        p = Prompt(
            id=p_data.get("id") or None,
            title=p_data.get("title", "Imported Prompt"),
            description=p_data.get("description", ""),
            folder_id=p_data.get("folder_id"),
            template_id=p_data.get("template_id"),
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
        repo.save_prompt(p)
        imported_count += 1

    return imported_count


def export_library_to_csv(repo: PromptRepository, filepath: Path) -> int:
    """Export all prompts to a CSV file."""
    prompts = repo.list_prompts()
    csv_content = to_csv_string(prompts)
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
        tmp_path.write_text(csv_content, encoding="utf-8")
        tmp_path.replace(filepath)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return len(prompts)


def import_library_from_csv(repo: PromptRepository, filepath: Path) -> int:
    """Import prompts from a CSV file."""
    content = filepath.read_text(encoding="utf-8")
    prompts = from_csv_string(content)
    for p in prompts:
        repo.save_prompt(p)
    return len(prompts)


def export_library_to_markdown_zip(repo: PromptRepository, filepath: Path) -> int:
    """Export all prompts as individual Markdown files inside a ZIP archive."""
    prompts = repo.list_prompts()
    used_names: set[str] = set()
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    try:
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
        tmp_path.replace(filepath)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return len(prompts)
