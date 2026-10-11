"""Orchestrator for GitHub sync — bridges PromptRepository <-> GitHub.

Supports:
- Exporting/entire library as JSON (via storage.backup)
- Pushing via Contents API (primary) or local git (optional, if git installed)
- Pulling / importing from GitHub
- Per-prompt markdown export (individual files)

Uses GithubClient for API and git_helper for local git.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from prompt_manager.core.models import Prompt
from prompt_manager.integrations.github_client import GithubClient, GithubError
from prompt_manager.storage.repository import PromptRepository
from prompt_manager.storage.backup import export_library_to_json, import_library_from_json


def _get_library_json_string(repo: PromptRepository) -> str:
    """Return library JSON as string without writing to disk."""
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        export_library_to_json(repo, tmp_path)
        return tmp_path.read_text(encoding="utf-8")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def push_library_via_api(
    repo: PromptRepository,
    client: GithubClient,
    full_name: str,
    branch: str = "main",
    file_path: str = "prompt-library.json",
    commit_message: Optional[str] = None,
) -> dict:
    """Push entire library JSON to GitHub via Contents API.

    Returns GitHub API response (content + commit info). Raises GithubError on failure.
    """
    content = _get_library_json_string(repo)
    # Build commit message
    if not commit_message:
        count = len(repo.list_prompts())
        commit_message = f"Sync prompt library ({count} prompts) via Prompt Manager"

    # Try to handle branch existence: get_file will auto-fetch sha
    try:
        result = client.create_or_update_file(
            full_name=full_name,
            path=file_path.lstrip("/"),
            content_str=content,
            message=commit_message,
            branch=branch,
        )
        return result
    except GithubError as e:
        # Provide clearer message for common cases
        if e.status == 404 and "branch" in str(e).lower():
            raise GithubError(f"Branch '{branch}' not found in {full_name}. Create the branch first or use 'main'.", status=e.status, payload=e.payload) from e
        raise


def pull_library_via_api(
    repo: PromptRepository,
    client: GithubClient,
    full_name: str,
    branch: str = "main",
    file_path: str = "prompt-library.json",
) -> Tuple[int, dict]:
    """Pull library JSON from GitHub and import into repo.

    Returns (imported_count, file_metadata). Raises GithubError if file not found.
    """
    file_info = client.get_file(full_name, file_path.lstrip("/"), ref=branch)
    if not file_info or "content" not in file_info:
        raise GithubError(f"File not found on GitHub: {full_name}/{file_path} @ {branch}", status=404)

    # Content is base64-encoded
    b64 = file_info["content"]
    # GitHub API splits content with newlines every 60 chars; strip whitespace
    b64_clean = "".join(b64.split())
    try:
        decoded = base64.b64decode(b64_clean).decode("utf-8")
    except Exception as e:
        raise GithubError(f"Failed to decode file content: {e}") from e

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tf:
        tmp_path = Path(tf.name)
        tf.write(decoded)
    try:
        count = import_library_from_json(repo, tmp_path)
        return count, file_info
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def push_library_via_git(
    repo: PromptRepository,
    full_name: str,
    token: str,
    branch: str = "main",
    file_path: str = "prompt-library.json",
    local_dir: Optional[Path] = None,
    commit_message: Optional[str] = None,
) -> str:
    """Push library using local git binary (requires `git` installed).

    - local_dir defaults to ~/.local/share/prompt-manager/github_sync/<owner>__<repo>
    - Returns commit hash or "no-changes"

    Raises GitError or GithubError on failure.
    """
    from prompt_manager.config import APP_DATA_DIR
    from prompt_manager.integrations.git_helper import commit_and_push

    content = _get_library_json_string(repo)
    if not commit_message:
        count = len(repo.list_prompts())
        commit_message = f"Sync prompt library ({count} prompts) via Prompt Manager"

    if local_dir is None:
        safe_name = full_name.replace("/", "__")
        local_dir = APP_DATA_DIR / "github_sync" / safe_name

    return commit_and_push(
        local_path=local_dir,
        repo_full_name=full_name,
        token=token,
        file_name=file_path.lstrip("/"),
        content=content,
        commit_message=commit_message,
        branch=branch,
    )


def push_individual_prompts_via_api(
    repo: PromptRepository,
    client: GithubClient,
    full_name: str,
    branch: str = "main",
    folder: str = "prompts",
) -> int:
    """Push each prompt as an individual markdown file under `folder/`.

    Returns number of files pushed.
    """
    from prompt_manager.core.exporter import to_markdown_frontmatter
    from prompt_manager.core.template_engine import hydrate_template

    prompts = repo.list_prompts()
    count = 0
    for p in prompts:
        hydrated = hydrate_template(p.template_content, {}, fallback_to_defaults=True)
        md = to_markdown_frontmatter(p, hydrated)
        # Filename: slugified title + id prefix to avoid collisions
        safe_title = "".join(c if c.isalnum() or c in "-_" else "-" for c in p.title.lower().replace(" ", "-"))
        filename = f"{folder}/{safe_title[:40]}-{p.id[:8]}.md"
        client.create_or_update_file(
            full_name=full_name,
            path=filename,
            content_str=md,
            message=f"Update prompt: {p.title}",
            branch=branch,
        )
        count += 1
    return count


def validate_github_config(full_name: str, branch: str, file_path: str) -> Optional[str]:
    """Validate repo config; returns error message or None if ok."""
    if not full_name or "/" not in full_name:
        return "Repository must be in 'owner/repo' format"
    if not branch or not branch.strip():
        return "Branch name required"
    if not file_path or not file_path.strip():
        return "File path required (e.g. prompt-library.json)"
    if ".." in file_path or file_path.startswith("/"):
        return "File path must be relative and not contain '..'"
    return None
