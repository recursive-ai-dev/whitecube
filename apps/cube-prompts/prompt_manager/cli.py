"""Headless CLI for Cube Prompts (database diagnostics, library inspection, templates, and backups)."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from prompt_manager import __version__
from prompt_manager import config
from prompt_manager.core import exporter
from prompt_manager.core.template_engine import get_unfilled_variables, hydrate_template
from prompt_manager.storage import backup
from prompt_manager.storage.database import Database, fts5_available
from prompt_manager.storage.repository import PromptRepository


def _prompt_to_dict(prompt) -> dict:
    return {
        "id": prompt.id,
        "title": prompt.title,
        "description": prompt.description,
        "folder_id": prompt.folder_id,
        "template_id": prompt.template_id,
        "template_content": prompt.template_content,
        "system_instruction": prompt.system_instruction,
        "target_model": prompt.target_model,
        "temperature": prompt.temperature,
        "is_favorite": bool(prompt.is_favorite),
        "use_count": prompt.use_count,
        "tags": list(prompt.tags),
        "created_at": prompt.created_at,
        "updated_at": prompt.updated_at,
    }


def _resolve_prompt(repo: PromptRepository, target: str):
    prompt = repo.get_prompt_by_id(target)
    if prompt is not None:
        return prompt
    matches = [item for item in repo.list_prompts() if item.title.lower() == target.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Multiple prompts match title {target!r}; specify its prompt ID instead.")
    raise LookupError(f"Prompt {target!r} not found.")


def _resolve_folder_id(repo: PromptRepository, folder: str | None) -> str | None:
    if not folder:
        return None
    for item in repo.list_folders():
        if item.id == folder or item.name.lower() == folder.lower():
            return item.id
    raise LookupError(f"Folder {folder!r} not found.")


def _resolve_tag_id(repo: PromptRepository, tag: str | None) -> str | None:
    if not tag:
        return None
    for item in repo.list_tags():
        if item.id == tag or item.name.lower() == tag.lower():
            return item.id
    raise LookupError(f"Tag {tag!r} not found.")


def _parse_vars(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid --var specification {pair!r}; expected KEY=VALUE.")
        key, val = pair.split("=", 1)
        if not key:
            raise ValueError("Variable name in --var cannot be empty.")
        values[key] = val
    return values


def cmd_doctor(args) -> int:
    """Inspect SQLite/FTS5 readiness and local library state without creating files."""
    db_path = Path(args.db) if args.db else config.DATABASE_PATH
    checks = []
    fts_ok = fts5_available()
    checks.append({
        "id": "sqlite",
        "status": "ok",
        "message": f"SQLite {sqlite3.sqlite_version} available"
                   + (" with FTS5" if fts_ok else " (FTS5 missing; using LIKE fallback)"),
    })
    if not fts_ok:
        checks.append({
            "id": "fts5",
            "status": "warning",
            "message": "SQLite FTS5 extension unavailable; search degrades to LIKE queries.",
        })

    prompt_count = None
    if db_path.is_symlink():
        checks.append({"id": "database", "status": "error", "message": f"Database path {db_path} must not be a symlink."})
    elif db_path.exists():
        if not db_path.is_file():
            checks.append({"id": "database", "status": "error", "message": f"Database path {db_path} is not a regular file."})
        else:
            try:
                uri = db_path.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()
                    prompt_count = int(row[0]) if row else 0
                checks.append({"id": "database", "status": "ok", "message": f"{db_path} readable ({prompt_count} prompts)"})
            except sqlite3.Error as error:
                checks.append({"id": "database", "status": "error", "message": f"Database check failed for {db_path}: {error}"})
    else:
        checks.append({"id": "database", "status": "info", "message": f"Database not created yet ({db_path})"})

    ready = all(c["status"] != "error" for c in checks)
    payload = {
        "format": "cube-prompts-diagnostics",
        "version": __version__,
        "ready": ready,
        "fts5": fts_ok,
        "sqlite_version": sqlite3.sqlite_version,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "prompt_count": prompt_count,
        "config_dir": str(config.APP_CONFIG_DIR),
        "checks": checks,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"cube-prompts {__version__} (SQLite {sqlite3.sqlite_version}, FTS5={'yes' if fts_ok else 'no'})")
        for item in checks:
            print(f"[{item['status'].upper()}] {item['id']}: {item['message']}")
    return 0 if ready else 1


def cmd_list(args) -> int:
    repo = PromptRepository(Database(args.db))
    folder_id = _resolve_folder_id(repo, args.folder)
    tag_id = _resolve_tag_id(repo, args.tag)
    prompts = repo.list_prompts(
        folder_id=folder_id,
        tag_id=tag_id,
        favorite_only=args.favorite,
        search_query=args.query,
    )
    if args.json:
        print(json.dumps([_prompt_to_dict(p) for p in prompts], indent=2))
        return 0
    if not prompts:
        print("No matching prompts.")
        return 0
    for item in prompts:
        star = "*" if item.is_favorite else " "
        tags = f" [{', '.join(item.tags)}]" if item.tags else ""
        print(f"{star} {item.id}  {item.title}  ({item.target_model}){tags}")
    return 0


def cmd_show(args) -> int:
    repo = PromptRepository(Database(args.db))
    prompt = _resolve_prompt(repo, args.target)
    if args.json:
        print(json.dumps(_prompt_to_dict(prompt), indent=2))
        return 0
    print(f"ID:     {prompt.id}")
    print(f"Title:  {prompt.title}")
    print(f"Model:  {prompt.target_model} (temperature={prompt.temperature})")
    if prompt.tags:
        print(f"Tags:   {', '.join(prompt.tags)}")
    if prompt.description:
        print(f"Desc:   {prompt.description}")
    if prompt.system_instruction:
        print(f"\n[System]\n{prompt.system_instruction}")
    print(f"\n[Template]\n{prompt.template_content}")
    return 0


def cmd_render(args) -> int:
    repo = PromptRepository(Database(args.db))
    prompt = _resolve_prompt(repo, args.target)
    values = _parse_vars(args.var or [])
    unfilled = get_unfilled_variables(prompt.template_content, values)
    if args.strict and unfilled:
        raise ValueError("Missing required template variables: " + ", ".join(unfilled))
    hydrated = hydrate_template(prompt.template_content, values, fallback_to_defaults=True)
    fmt = args.format
    if fmt == "plain":
        rendered = exporter.to_plain_text(prompt, hydrated)
    elif fmt == "openai":
        rendered = exporter.to_openai_payload(prompt, hydrated)
    elif fmt == "anthropic":
        rendered = exporter.to_anthropic_payload(prompt, hydrated)
    elif fmt == "markdown":
        rendered = exporter.to_markdown_frontmatter(prompt, hydrated)
    elif fmt == "langchain":
        rendered = exporter.to_langchain_template(prompt)
    else:
        rendered = exporter.to_llamaindex_template(prompt)

    if args.json:
        print(json.dumps({
            "id": prompt.id,
            "title": prompt.title,
            "format": fmt,
            "unfilled_variables": unfilled,
            "output": rendered,
        }, indent=2))
    elif isinstance(rendered, dict):
        print(json.dumps(rendered, indent=2))
    else:
        print(rendered)
    return 0


def cmd_export(args) -> int:
    repo = PromptRepository(Database(args.db))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fmt = args.format
    if fmt == "auto":
        suffix = output.suffix.lower()
        fmt = "csv" if suffix == ".csv" else ("markdown-zip" if suffix == ".zip" else "json")
    if fmt == "json":
        count = backup.export_library_to_json(repo, output)
    elif fmt == "csv":
        count = backup.export_library_to_csv(repo, output)
    else:
        count = backup.export_library_to_markdown_zip(repo, output)
    if args.json:
        print(json.dumps({"format": fmt, "output": str(output), "count": count}, indent=2))
    else:
        print(f"Exported {count} prompt(s) to {output} ({fmt})")
    return 0


def cmd_import(args) -> int:
    repo = PromptRepository(Database(args.db))
    source = Path(args.input)
    fmt = args.format
    if fmt == "auto":
        fmt = "csv" if source.suffix.lower() == ".csv" else "json"
    if fmt == "csv":
        count = backup.import_library_from_csv(repo, source)
    else:
        count = backup.import_library_from_json(repo, source)
    if args.json:
        print(json.dumps({"format": fmt, "input": str(source), "imported": count}, indent=2))
    else:
        print(f"Imported {count} prompt(s) from {source} ({fmt})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cube-prompts --cli",
        description="Headless Cube Prompts CLI (doctor, library listing, template rendering, and backups).",
    )
    parser.add_argument("-v", "--version", action="version", version=f"cube-prompts {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="Check SQLite, FTS5, and prompt database health without mutating state")
    p_doc.add_argument("--db", type=Path, help="Override prompts SQLite database path")
    p_doc.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p_doc.set_defaults(func=cmd_doctor)

    p_list = sub.add_parser("list", help="List stored prompts")
    p_list.add_argument("--db", type=Path, help="Override prompts SQLite database path")
    p_list.add_argument("--folder", help="Filter by folder ID or name")
    p_list.add_argument("--tag", help="Filter by tag ID or name")
    p_list.add_argument("--favorite", action="store_true", help="Only list favorite prompts")
    p_list.add_argument("--query", help="Full-text or substring search query")
    p_list.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show details of a prompt by ID or exact title")
    p_show.add_argument("target", help="Prompt ID or title")
    p_show.add_argument("--db", type=Path, help="Override prompts SQLite database path")
    p_show.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p_show.set_defaults(func=cmd_show)

    p_render = sub.add_parser("render", help="Hydrate and format a prompt for an LLM target")
    p_render.add_argument("target", help="Prompt ID or title")
    p_render.add_argument("--db", type=Path, help="Override prompts SQLite database path")
    p_render.add_argument("--var", action="append", help="Template variable assignment KEY=VALUE (repeatable)")
    p_render.add_argument(
        "--format",
        choices=("plain", "openai", "anthropic", "markdown", "langchain", "llamaindex"),
        default="plain",
        help="Output format (default: plain)",
    )
    p_render.add_argument("--strict", action="store_true", help="Fail if any required variable has no value or default")
    p_render.add_argument("--json", action="store_true", help="Wrap output and metadata as JSON")
    p_render.set_defaults(func=cmd_render)

    p_export = sub.add_parser("export", help="Export the prompt library to JSON, CSV, or Markdown ZIP")
    p_export.add_argument("output", help="Destination archive path (.json, .csv, or .zip)")
    p_export.add_argument("--db", type=Path, help="Override prompts SQLite database path")
    p_export.add_argument("--format", choices=("auto", "json", "csv", "markdown-zip"), default="auto")
    p_export.add_argument("--json", action="store_true", help="Print machine-readable JSON summary")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="Import prompts from a regular JSON backup or CSV file")
    p_import.add_argument("input", help="Source .json or .csv file")
    p_import.add_argument("--db", type=Path, help="Override prompts SQLite database path")
    p_import.add_argument("--format", choices=("auto", "json", "csv"), default="auto")
    p_import.add_argument("--json", action="store_true", help="Print machine-readable JSON summary")
    p_import.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, LookupError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"cube-prompts: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
