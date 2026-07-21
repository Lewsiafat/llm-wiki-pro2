"""MCP server — exposes the wiki engine as Model Context Protocol tools.

Mounted by app.main at {BASE_PATH}/mcp using the streamable-http transport,
so any MCP client (Claude Desktop, other agents) can connect to
https://your-domain.example/llm-wiki-pro2/mcp directly.

Security: /mcp has its own shared-secret bearer token, independent from the
REST API's opt-in API_TOKEN (see app.main). Requests must send
`Authorization: Bearer <MCP_API_KEY>`; the key lives in .env. If MCP_API_KEY
is unset the MCP endpoint fails closed (503) rather than running open.
"""
from __future__ import annotations

import json
import os
import secrets

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from app import engine

# The SDK's DNS-rebinding protection only allows localhost, which rejects
# proxied requests (nginx passes the public Host header) with "Invalid Host
# header". Deployments behind a reverse proxy MUST list their public host in
# MCP_ALLOWED_HOSTS (comma-separated, wildcards allowed) — e.g.
# "wiki.example.com,wiki.example.com:*,127.0.0.1:*,localhost:*".
# MCP_ALLOWED_ORIGINS does the same for the CORS/Origin check.
def _csv_env(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


_ALLOWED_HOSTS = _csv_env("MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*")
_ALLOWED_ORIGINS = _csv_env(
    "MCP_ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*"
)

mcp = FastMCP(
    "llm-wiki-pro2",
    instructions=(
        "Personal knowledge base built on the LLM-wiki pattern: sources are "
        "ingested into a markdown wiki that compounds over time. Typical flow: "
        "add_source -> ingest_source -> query_wiki. Browse with list_wiki_tree "
        "and get_wiki_page; run lint_wiki for a health check."
    ),
    stateless_http=True,   # each request is independent — simplest for remote clients
    json_response=True,    # plain JSON responses (no SSE) — avoids nginx buffering issues
    streamable_http_path="/",  # endpoint lives exactly at the mount point ({BASE_PATH}/mcp)
    transport_security=TransportSecuritySettings(
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_ALLOWED_ORIGINS,
    ),
)


# ---- tools: thin wrappers over app.engine (same logic as the REST API) ----
# engine calls are sync + blocking (LLM calls can take 30-90 s), so run them
# in a worker thread to keep the event loop (and the REST API) responsive.


@mcp.tool()
async def add_source(title: str, content: str, filename: str | None = None) -> dict:
    """Add a new raw source document (Layer 1) to the knowledge base.

    Returns the created source metadata including its id (e.g. "s003").
    This does NOT process the source into the wiki — call ingest_source next.
    """
    if not title.strip() or not content.strip():
        raise ValueError("title and content are required")
    return await anyio.to_thread.run_sync(engine.add_source, title.strip(), content, filename)


@mcp.tool()
async def ingest_source(source_id: str) -> dict:
    """Run the two-phase LLM ingest for a source: update/create wiki summary and topic pages plus the index.

    SLOW — two LLM calls, typically 30-90 seconds. Returns the ingest result
    (updated pages, log line). The source must exist (see add_source).
    """
    try:
        return await anyio.to_thread.run_sync(engine.ingest, source_id)
    except KeyError:
        raise ValueError(f"source {source_id} not found")


@mcp.tool()
async def query_wiki(question: str, save: bool = False) -> dict:
    """Ask a question answered strictly from the wiki's pages, with page citations.

    Set save=true to also archive the answer as a wiki page under notes/.
    """
    if not question.strip():
        raise ValueError("question is required")
    return await anyio.to_thread.run_sync(engine.query, question.strip(), save)


@mcp.tool()
async def lint_wiki() -> dict:
    """Run a whole-wiki health check (contradictions, stale claims, orphan pages, missing cross-links).

    SLOW — LLM call over the entire wiki. Writes a report page under
    maintenance/ and returns it; never auto-modifies other pages.
    """
    return await anyio.to_thread.run_sync(engine.lint)


@mcp.tool()
async def get_wiki_page(path: str) -> dict:
    """Read a single wiki page by its relative path (e.g. "index.md", "topics/agents.md", "sources/s001-foo.md")."""
    try:
        return await anyio.to_thread.run_sync(engine.read_page, path)
    except (KeyError, ValueError):
        raise ValueError(f"page not found: {path}")


@mcp.tool()
async def list_wiki_tree() -> dict:
    """Get the wiki index: full file list plus the current index.md contents. Start here to see what the wiki knows."""
    return await anyio.to_thread.run_sync(engine.wiki_tree)


# ---- bearer-token gate (MCP surface only; REST API stays open) ----


class BearerAuth:
    """Minimal ASGI wrapper requiring `Authorization: Bearer <MCP_API_KEY>`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = os.getenv("MCP_API_KEY", "")
        if not expected:
            await self._deny(send, 503, "MCP endpoint disabled: MCP_API_KEY not configured")
            return
        auth = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                auth = value.decode("latin-1")
                break
        if not secrets.compare_digest(auth, f"Bearer {expected}"):
            await self._deny(send, 401, "unauthorized: missing or invalid bearer token")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_asgi_app():
    """The mountable ASGI app: streamable-http MCP handler behind the bearer gate.

    Uses StreamableHTTPASGIApp directly (path-agnostic) instead of
    mcp.streamable_http_app(), whose internal exact-path Route would 404 when
    registered under an outer path like {BASE_PATH}/mcp without path rewriting.
    streamable_http_app() is still called once for its side effect: it lazily
    creates mcp.session_manager, which app.main's lifespan then runs.
    """
    mcp.streamable_http_app()
    return BearerAuth(StreamableHTTPASGIApp(mcp.session_manager))
