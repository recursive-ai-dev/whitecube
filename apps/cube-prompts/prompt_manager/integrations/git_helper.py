"""Local git helper — optional push/pull via system `git` binary.

This complements the GitHub Contents API approach. If `git` is installed,
users can clone a repo locally, commit the prompt library, and push using
the stored OAuth/PAT token.

All functions are thin wrappers around `subprocess` and raise GitError on failure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple


class GitError(Exception):
    pass


def is_git_available() -> bool:
    return shutil.which("git") is not None


def _scrub_token(text: str) -> str:
    """Scrub OAuth/PAT tokens from command strings and error messages."""
    if not text:
        return ""
    scrubbed = re.sub(r"://([^:@]+:)?([^@]+)@", r"://\1***@", text)
    scrubbed = re.sub(r"(ghp_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{30,})", "***", scrubbed)
    return scrubbed


def _run_git(args: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None) -> Tuple[str, str]:
    if not is_git_available():
        raise GitError("`git` binary not found — install git to use local git sync")
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        safe_cmd = [_scrub_token(c) for c in cmd]
        raise GitError(f"git command timed out: {' '.join(safe_cmd)}") from e
    if result.returncode != 0:
        safe_args = [_scrub_token(a) for a in args]
        stderr = _scrub_token(result.stderr.strip())
        stdout = _scrub_token(result.stdout.strip())
        raise GitError(f"git {' '.join(safe_args)} failed:\n{stderr or stdout}")
    return result.stdout.strip(), result.stderr.strip()


def build_authenticated_remote_url(repo_full_name: str, token: str, host: str = "github.com") -> str:
    """Build https URL with token embedded for push (token is oauth token or PAT).

    Example: https://oauth2:TOKEN@github.com/owner/repo.git
    For PAT, GitHub accepts https://TOKEN@github.com/...
    We use the oauth2 style for compatibility.
    """
    # Use x-access-token for OAuth, or just token for PAT — both work with the oauth2: prefix
    # Encode token for URL safety
    encoded_token = urllib.parse.quote(token, safe="")
    return f"https://oauth2:{encoded_token}@{host}/{repo_full_name}.git"


def init_repo(local_path: Path, repo_full_name: str, token: str, branch: str = "main") -> None:
    """Initialize a local git repo, set remote, and fetch."""
    local_path.mkdir(parents=True, exist_ok=True)
    if not (local_path / ".git").exists():
        _run_git(["init", "-b", branch], cwd=local_path)
        # Set default user if not configured
        try:
            _run_git(["config", "user.name"], cwd=local_path)
        except GitError:
            _run_git(["config", "user.name", "Prompt Manager"], cwd=local_path)
        try:
            _run_git(["config", "user.email"], cwd=local_path)
        except GitError:
            _run_git(["config", "user.email", "prompt-manager@local"], cwd=local_path)

    remote_url = build_authenticated_remote_url(repo_full_name, token)
    # Add or set remote
    try:
        existing, _ = _run_git(["remote", "get-url", "origin"], cwd=local_path)
        if existing != remote_url:
            _run_git(["remote", "set-url", "origin", remote_url], cwd=local_path)
    except GitError:
        _run_git(["remote", "add", "origin", remote_url], cwd=local_path)

    # Fetch (may fail if repo empty)
    try:
        _run_git(["fetch", "origin", branch], cwd=local_path)
        # Try to set upstream if remote branch exists
        try:
            _run_git(["rev-parse", "--verify", f"origin/{branch}"], cwd=local_path)
            # Checkout or create local branch tracking remote
            try:
                _run_git(["checkout", branch], cwd=local_path)
            except GitError:
                _run_git(["checkout", "-b", branch, f"origin/{branch}"], cwd=local_path)
        except GitError:
            # Remote branch doesn't exist yet — stay on local branch
            pass
    except GitError:
        # Repo may be empty (no commits) — that's ok
        pass


def commit_and_push(
    local_path: Path,
    repo_full_name: str,
    token: str,
    file_name: str,
    content: str,
    commit_message: str,
    branch: str = "main",
) -> str:
    """Write content to file in repo, commit, and push. Returns commit hash."""
    if not local_path.exists():
        raise GitError(f"Local path does not exist: {local_path}")
    init_repo(local_path, repo_full_name, token, branch=branch)

    # Write file safely preventing path traversal
    if not file_name or ".." in file_name or Path(file_name).is_absolute():
        raise GitError(f"Invalid or unsafe file path: '{file_name}'")
    target = (local_path / file_name).resolve()
    if not target.is_relative_to(local_path.resolve()):
        raise GitError(f"Path traversal detected: '{file_name}' resolves outside repository")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    _run_git(["add", file_name], cwd=local_path)

    # Check if anything to commit
    status_out, _ = _run_git(["status", "--porcelain"], cwd=local_path)
    if not status_out:
        return "no-changes"

    _run_git(["commit", "-m", commit_message], cwd=local_path)
    remote_url = build_authenticated_remote_url(repo_full_name, token)
    # Ensure remote url updated (token may have changed)
    _run_git(["remote", "set-url", "origin", remote_url], cwd=local_path)
    # Push
    _run_git(["push", "-u", "origin", branch], cwd=local_path)
    commit_hash, _ = _run_git(["rev-parse", "HEAD"], cwd=local_path)
    return commit_hash


def clone_repo(local_path: Path, repo_full_name: str, token: str, branch: str = "main") -> None:
    """Clone repo to local_path (must not exist or be empty)."""
    if local_path.exists() and any(local_path.iterdir()):
        raise GitError(f"Local path is not empty: {local_path}")
    local_path.mkdir(parents=True, exist_ok=True)
    remote_url = build_authenticated_remote_url(repo_full_name, token)
    # Clone without branch first, then checkout
    parent = local_path.parent
    tmp_name = local_path.name
    # Use clone to temp then move? Simpler: git clone url local_path (git handles empty dir as target)
    # git clone expects target not to exist or empty — we made it exist empty, so remove and clone
    # To avoid complexity, clone to parent with tmp
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_clone = Path(tmpdir) / "clone"
        _run_git(["clone", "-b", branch, remote_url, str(tmp_clone)])
        # Move contents
        for item in tmp_clone.iterdir():
            if item.name == ".git":
                # Move .git
                if (local_path / ".git").exists():
                    shutil.rmtree(local_path / ".git")
                shutil.move(str(item), str(local_path / ".git"))
            else:
                dest = local_path / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), str(dest))
        # Set remote url with token for future pushes
        _run_git(["remote", "set-url", "origin", remote_url], cwd=local_path)


def get_status(local_path: Path) -> str:
    out, _ = _run_git(["status", "--porcelain"], cwd=local_path)
    return out


def get_remote_url(local_path: Path) -> str:
    out, _ = _run_git(["remote", "get-url", "origin"], cwd=local_path)
    return out
