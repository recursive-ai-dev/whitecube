"""GitHub API client — PAT + OAuth Device Flow, no extra dependencies.

Uses stdlib urllib so the app works without `requests`. Handles:

- PAT authentication & verification
- Listing / creating repositories
- Reading / creating / updating file contents via the Contents API
- OAuth Device Flow (github.com/login/device/code) with polling
- Friendly GithubError with status + message

All methods raise GithubError on failure.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


GITHUB_API_BASE = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
# Default OAuth app — users should create their own for production.
# This placeholder will require users to set their own client_id via settings or env.
DEFAULT_OAUTH_CLIENT_ID = ""  # must be configured
DEFAULT_OAUTH_SCOPE = "repo user"


class GithubError(Exception):
    """Raised when GitHub API returns an error."""

    def __init__(self, message: str, status: Optional[int] = None, payload: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[dict] = None,
    raw_data: Optional[bytes] = None,
    accept_json: bool = True,
) -> Any:
    """Low-level HTTP helper using urllib. Returns parsed JSON or raw bytes."""
    hdrs = dict(headers or {})
    body: Optional[bytes] = None

    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif raw_data is not None:
        body = raw_data

    if accept_json:
        hdrs.setdefault("Accept", "application/vnd.github+json")
        hdrs.setdefault("X-GitHub-Api-Version", "2022-11-28")

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype or accept_json:
                if raw:
                    try:
                        return json.loads(raw.decode("utf-8"))
                    except Exception:
                        return raw.decode("utf-8")
                return {}
            return raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"message": raw}
        msg = payload.get("message") or payload.get("error_description") or payload.get("error") or raw or f"HTTP {e.code}"
        raise GithubError(msg, status=e.code, payload=payload) from e
    except urllib.error.URLError as e:
        raise GithubError(str(e.reason) if hasattr(e, "reason") else str(e)) from e


@dataclass
class DeviceCodeResponse:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class GithubClient:
    """Authenticated GitHub API client using a personal access token or OAuth token."""

    def __init__(self, token: str, user_agent: str = "prompt-manager/0.2.0"):
        if not token or not token.strip():
            raise ValueError("GitHub token must not be empty")
        self.token = token.strip()
        self.user_agent = user_agent

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,
        }
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path_or_url: str, data: Optional[dict] = None, params: Optional[dict] = None) -> Any:
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"{GITHUB_API_BASE}{path_or_url}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _http_request(method, url, headers=self._headers(), data=data)

    # ── User / Auth ─────────────────────────────────────────────────

    def get_user(self) -> Dict[str, Any]:
        """Return authenticated user info. Raises GithubError on invalid token."""
        return self._request("GET", "/user")

    def get_user_repos(self, per_page: int = 100, sort: str = "updated") -> List[Dict[str, Any]]:
        """List repositories for authenticated user (first page)."""
        repos = self._request("GET", "/user/repos", params={"per_page": per_page, "sort": sort, "affiliation": "owner"})
        # API returns list
        if isinstance(repos, list):
            return repos
        return []

    def get_repo(self, full_name: str) -> Dict[str, Any]:
        """Fetch a single repo by full_name (owner/repo)."""
        owner, repo = _split_full_name(full_name)
        return self._request("GET", f"/repos/{owner}/{repo}")

    def create_repo(
        self,
        name: str,
        private: bool = False,
        description: str = "",
        auto_init: bool = True,
    ) -> Dict[str, Any]:
        """Create a new repository under the authenticated user."""
        if not name or not name.strip():
            raise ValueError("Repository name required")
        payload: Dict[str, Any] = {
            "name": name.strip(),
            "private": private,
            "description": description,
            "auto_init": auto_init,
        }
        return self._request("POST", "/user/repos", data=payload)

    # ── Contents API (file create / update) ─────────────────────────

    def get_file(self, full_name: str, path: str, ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch file metadata including `sha` and `content` (base64). Returns None on 404."""
        owner, repo = _split_full_name(full_name)
        # Path must be URL-encoded but slashes preserved
        encoded_path = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
        params = {}
        if ref:
            params["ref"] = ref
        try:
            return self._request("GET", f"/repos/{owner}/{repo}/contents/{encoded_path}", params=params or None)
        except GithubError as e:
            if e.status == 404:
                return None
            raise

    def create_or_update_file(
        self,
        full_name: str,
        path: str,
        content_str: str,
        message: str,
        branch: str = "main",
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update a file. If `sha` is None and file exists, it will be fetched automatically."""
        owner, repo = _split_full_name(full_name)
        encoded_path = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))

        # If sha not provided, try to fetch existing file to get sha
        if sha is None:
            existing = self.get_file(full_name, path, ref=branch)
            if existing and isinstance(existing, dict) and "sha" in existing:
                sha = existing["sha"]
            # If 404, sha stays None (create)
            # If file is directory or other, let API error

        b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")
        payload: Dict[str, Any] = {
            "message": message,
            "content": b64,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        return self._request("PUT", f"/repos/{owner}/{repo}/contents/{encoded_path}", data=payload)

    def delete_file(
        self,
        full_name: str,
        path: str,
        message: str,
        branch: str = "main",
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        owner, repo = _split_full_name(full_name)
        encoded_path = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
        if sha is None:
            existing = self.get_file(full_name, path, ref=branch)
            if not existing or "sha" not in existing:
                raise GithubError(f"File not found: {path}")
            sha = existing["sha"]
        payload = {"message": message, "sha": sha, "branch": branch}
        # Github delete uses DELETE with body; urllib supports it
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{encoded_path}"
        return _http_request("DELETE", url, headers=self._headers(), data=payload)

    def get_branch(self, full_name: str, branch: str) -> Dict[str, Any]:
        owner, repo = _split_full_name(full_name)
        return self._request("GET", f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(branch, safe='')}")


def _split_full_name(full_name: str) -> tuple[str, str]:
    if "/" not in full_name:
        raise ValueError(f"Invalid repo full_name '{full_name}' — expected 'owner/repo'")
    owner, repo = full_name.split("/", 1)
    if not owner or not repo:
        raise ValueError(f"Invalid repo full_name '{full_name}'")
    return owner.strip(), repo.strip()


# ── OAuth Device Flow helpers ───────────────────────────────────────

def request_device_code(client_id: str, scope: str = DEFAULT_OAUTH_SCOPE) -> DeviceCodeResponse:
    """Initiate OAuth Device Flow. Returns device_code + user_code to show to user."""
    if not client_id or not client_id.strip():
        raise ValueError("OAuth client_id is required — create an OAuth App at https://github.com/settings/developers")
    data = urllib.parse.urlencode({"client_id": client_id.strip(), "scope": scope}).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    resp = _http_request("POST", DEVICE_CODE_URL, headers=headers, raw_data=data, accept_json=True)
    # resp is dict
    if not isinstance(resp, dict) or "device_code" not in resp:
        raise GithubError(f"Unexpected device code response: {resp}")
    return DeviceCodeResponse(
        device_code=resp["device_code"],
        user_code=resp["user_code"],
        verification_uri=resp.get("verification_uri") or "https://github.com/login/device",
        verification_uri_complete=resp.get("verification_uri_complete") or resp.get("verification_uri") or "https://github.com/login/device",
        expires_in=int(resp.get("expires_in", 900)),
        interval=int(resp.get("interval", 5)),
    )


def poll_for_token(client_id: str, device_code: str, interval: int = 5, expires_in: int = 900) -> str:
    """Poll for OAuth token after user authorizes. Blocks until success or timeout.

    Raises GithubError on denied/expired. Returns access_token string on success.
    Handles `slow_down` and `interval` per spec.
    """
    if not client_id or not client_id.strip():
        raise ValueError("OAuth client_id required")
    deadline = time.time() + expires_in
    current_interval = max(interval, 5)

    while time.time() < deadline:
        time.sleep(current_interval)
        data = urllib.parse.urlencode(
            {
                "client_id": client_id.strip(),
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
        ).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        try:
            resp = _http_request("POST", ACCESS_TOKEN_URL, headers=headers, raw_data=data, accept_json=True)
        except GithubError as e:
            # Device flow errors are returned as 200 with error field, not HTTP error in some cases
            # But handle HTTP errors too
            raise

        if not isinstance(resp, dict):
            raise GithubError(f"Unexpected token response: {resp}")

        if "access_token" in resp and resp["access_token"]:
            return resp["access_token"]

        error = resp.get("error")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            current_interval += 5
            continue
        elif error == "expired_token":
            raise GithubError("Device code expired — please restart authorization", payload=resp)
        elif error == "unsupported_grant_type":
            raise GithubError("Unsupported grant type — check client_id and flow", payload=resp)
        elif error == "incorrect_client_credentials":
            raise GithubError("Incorrect client credentials — check OAuth App client_id", payload=resp)
        elif error == "incorrect_device_code":
            raise GithubError("Incorrect device code — restart flow", payload=resp)
        elif error == "access_denied":
            raise GithubError("User denied access", payload=resp)
        elif error:
            raise GithubError(resp.get("error_description") or error, payload=resp)
        else:
            # No token and no error — continue polling
            continue

    raise GithubError("OAuth polling timed out — device code expired")


# ── Helper for local git operations (optional) ──────────────────────

def check_github_token_scopes(token: str) -> List[str]:
    """Return list of scopes for token by inspecting response headers via HEAD to /user."""
    # We inspect via a request and parse the `X-OAuth-Scopes` or `X-Accepted-OAuth-Scopes` header
    # Fallback: try to fetch user and check header — urllib doesn't expose headers easily via _http_request helper
    # So do manual request
    req = urllib.request.Request(f"{GITHUB_API_BASE}/user", headers={"Authorization": f"Bearer {token}", "User-Agent": "prompt-manager/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            scopes_header = resp.headers.get("X-OAuth-Scopes", "") or resp.headers.get("X-Accepted-OAuth-Scopes", "")
            if scopes_header:
                return [s.strip() for s in scopes_header.split(",") if s.strip()]
            return []
    except Exception:
        return []
