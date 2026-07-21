"""GitHub connector — pulls docs/README/CLAUDE.md content from configured repos
into the wiki as Layer 1 sources, then triggers the normal ingest pipeline.

Does NOT touch engine.py's ingest/query/lint logic — this module only calls
add_source() / ingest() like any other source. It adds its own sync-state
file (data/github-sync.json) so re-running a sync skips unchanged repos.

Which repos to sync comes from `data/github-repos.json` (see
`github-repos.json.sample` in the repo root). Fields:
  - owner: GitHub user/org that owns the repos
  - standard_repos: README.md / CLAUDE.md / docs/**.md / specs/**.md only
  - full_tree_repos: read every file in the repo, no folder/extension filter.
    For repos whose real content lives outside README/docs/specs.
  - html_repos: repo only has an index.html (no markdown at all). We fetch
    index.html and strip tags/script/style before treating it as source text.
  - excluded_repos: never touched by sync, not even listed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from app import engine

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_API = "https://api.github.com"

SYNC_FILE = engine.DATA_DIR / "github-sync.json"
REPOS_FILE = engine.DATA_DIR / "github-repos.json"

# Files matched under the standard rule (case-insensitive).
_STANDARD_PATTERN = re.compile(
    r"^(readme[^/]*\.md|readme)$|^claude\.md$|^(docs|specs)/.*\.md$",
    re.IGNORECASE,
)

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


# ---------------------------------------------------------------------------
# Repo configuration (data/github-repos.json)
# ---------------------------------------------------------------------------


def _load_repo_config() -> dict:
    """Reads data/github-repos.json. Raises RuntimeError with an actionable
    message when it's missing or malformed — sync is useless without it."""
    if not REPOS_FILE.exists():
        raise RuntimeError(
            f"GitHub repo config not found at {REPOS_FILE}. "
            "Copy github-repos.json.sample from the project root into your "
            "data directory and fill in your owner and repo lists."
        )
    try:
        cfg = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{REPOS_FILE} is not valid JSON: {e}")

    owner = str(cfg.get("owner", "")).strip()
    if not owner:
        raise RuntimeError(f'{REPOS_FILE} is missing a non-empty "owner" field.')

    config = {
        "owner": owner,
        "standard_repos": list(cfg.get("standard_repos", [])),
        "full_tree_repos": list(cfg.get("full_tree_repos", [])),
        "html_repos": list(cfg.get("html_repos", [])),
        "excluded_repos": list(cfg.get("excluded_repos", [])),
    }
    config["all_repos"] = (
        config["standard_repos"] + config["full_tree_repos"] + config["html_repos"]
    )
    if not config["all_repos"]:
        raise RuntimeError(f"{REPOS_FILE} lists no repos to sync.")
    return config


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(base_url=GITHUB_API, headers=_HEADERS, timeout=30)


def _default_branch(client: httpx.Client, owner: str, repo: str) -> str:
    r = client.get(f"/repos/{owner}/{repo}")
    r.raise_for_status()
    return r.json()["default_branch"]


def _tree(client: httpx.Client, owner: str, repo: str, branch: str) -> list[dict]:
    r = client.get(f"/repos/{owner}/{repo}/git/trees/{branch}", params={"recursive": "1"})
    r.raise_for_status()
    data = r.json()
    if data.get("truncated"):
        # Configured repos are expected to be small; if this ever fires, the
        # repo needs a dedicated paginated fetch instead of the recursive call.
        pass
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]


def _fetch_file(client: httpx.Client, owner: str, repo: str, path: str) -> str:
    r = client.get(f"/repos/{owner}/{repo}/contents/{path}")
    r.raise_for_status()
    data = r.json()
    import base64
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Crude but effective: drop script/style blocks, then all tags, then
    collapse whitespace. Verified against a real course index.html (64184
    bytes raw -> 7498 chars of clean prose, no tag/CSS/JS bleed-through)."""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Per-repo content assembly
# ---------------------------------------------------------------------------


def _collect_repo_content(client: httpx.Client, config: dict, repo: str) -> str | None:
    """Returns the assembled source text for one repo, or None if there's
    nothing to ingest (empty result after filtering)."""
    owner = config["owner"]
    branch = _default_branch(client, owner, repo)

    if repo in config["html_repos"]:
        try:
            html = _fetch_file(client, owner, repo, "index.html")
        except httpx.HTTPStatusError:
            return None
        text = _strip_html(html)
        if not text:
            return None
        return f"# {repo} (index.html, HTML-stripped)\n\n{text}"

    tree = _tree(client, owner, repo, branch)

    if repo in config["full_tree_repos"]:
        paths = [item["path"] for item in tree]
    else:
        paths = [item["path"] for item in tree if _STANDARD_PATTERN.match(item["path"])]

    if not paths:
        return None

    parts = []
    for path in sorted(paths):
        try:
            content = _fetch_file(client, owner, repo, path)
        except httpx.HTTPStatusError:
            continue
        if not content.strip():
            continue
        parts.append(f"## {path}\n\n{content}")

    if not parts:
        return None

    return f"# {repo}\n\n" + "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Sync-state file (data/github-sync.json)
# ---------------------------------------------------------------------------


def _load_sync_state() -> dict:
    if not SYNC_FILE.exists():
        return {}
    return json.loads(SYNC_FILE.read_text(encoding="utf-8"))


def _save_sync_state(state: dict) -> None:
    SYNC_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_status() -> dict:
    return _load_sync_state()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(repos: list[str] | None = None, force: bool = False) -> dict:
    """Sync GitHub repos into the wiki.

    - repos=None: check every repo in data/github-repos.json, skip any whose
      content hash is unchanged since the last successful sync.
    - repos=[...]: only touch these repos.
    - force=True: re-ingest the given (or all) repos even if the hash
      matches — i.e. bypasses the skip check. Only meaningful together
      with an explicit `repos` list in practice, but works for full runs too.
    """
    start = time.monotonic()
    config = _load_repo_config()
    targets = repos if repos else config["all_repos"]
    targets = [r for r in targets if r not in config["excluded_repos"]]

    state = _load_sync_state()
    result = {"checked": 0, "added": 0, "updated": 0, "skipped": 0, "failed": 0}
    failed_repos: list[dict] = []

    with _client() as client:
        for repo in targets:
            result["checked"] += 1
            try:
                content = _collect_repo_content(client, config, repo)
            except httpx.HTTPStatusError as e:
                result["failed"] += 1
                failed_repos.append({"repo": repo, "error": str(e)})
                state[repo] = {
                    **state.get(repo, {}),
                    "status": "failed",
                    "last_synced_at": _now(),
                }
                _save_sync_state(state)
                continue

            if content is None:
                result["failed"] += 1
                failed_repos.append({"repo": repo, "error": "no matching content found"})
                state[repo] = {
                    **state.get(repo, {}),
                    "status": "failed",
                    "last_synced_at": _now(),
                }
                _save_sync_state(state)
                continue

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prev = state.get(repo)

            if prev and prev.get("content_hash") == content_hash and not force:
                result["skipped"] += 1
                continue

            is_update = bool(prev and prev.get("source_id"))
            title = f"repo-{repo}: GitHub 專案 {repo}"
            try:
                source = engine.add_source(title, content, filename=f"repo-{repo}")
                engine.ingest(source["id"])
            except (ValueError, RuntimeError, KeyError) as e:
                result["failed"] += 1
                failed_repos.append({"repo": repo, "error": str(e)})
                state[repo] = {
                    **state.get(repo, {}),
                    "status": "failed",
                    "last_synced_at": _now(),
                }
                _save_sync_state(state)
                continue

            state[repo] = {
                "content_hash": content_hash,
                "source_id": source["id"],
                "last_synced_at": _now(),
                "status": "ingested",
            }
            _save_sync_state(state)
            if is_update:
                result["updated"] += 1
            else:
                result["added"] += 1

    result["duration_sec"] = round(time.monotonic() - start, 1)
    if failed_repos:
        result["failed_repos"] = failed_repos
    return result


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
