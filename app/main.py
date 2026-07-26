"""llm-wiki-pro2 — FastAPI server: REST API + static frontend."""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before engine reads DATA_DIR

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app import engine, github, mcp_server  # noqa: E402

BASE_PATH = os.getenv("BASE_PATH", "")  # e.g. "/llm-wiki-pro2" behind nginx, "" locally
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_VERSION = "0.0.3"

# Opt-in bearer-token gate for the REST API (the frontend page itself and its
# static assets stay reachable so the login prompt can render). Unset =
# fully open, same as before. Independent from MCP's own MCP_API_KEY gate.
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# Demo-only kill switch: blocks the LLM-calling endpoints (ingest/query/lint/
# github sync) so a public, unauthenticated demo instance can't burn
# OpenRouter credit. Browsing the already-ingested wiki (GET endpoints)
# still works. Off unless DEMO_DISABLE_LLM is explicitly set.
DEMO_DISABLE_LLM = os.getenv("DEMO_DISABLE_LLM", "").strip().lower() in ("1", "true", "yes")


def require_token(authorization: str | None = Header(default=None)):
    if not API_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer ") or not secrets.compare_digest(
        authorization.removeprefix("Bearer "), API_TOKEN
    ):
        raise HTTPException(401, "unauthorized")


def require_llm_enabled():
    if DEMO_DISABLE_LLM:
        raise HTTPException(403, "demo instance: this feature is disabled to avoid burning API credit")

engine.init_data()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # the MCP streamable-http session manager must run for the /mcp mount to work
    async with mcp_server.mcp.session_manager.run():
        yield


app = FastAPI(
    title="llm-wiki-pro2",
    description="Personal knowledge base built on the LLM-wiki pattern.",
    version=APP_VERSION,
    docs_url=f"{BASE_PATH}/api/docs",
    openapi_url=f"{BASE_PATH}/api/openapi.json",
    lifespan=lifespan,
)
router = APIRouter(prefix=BASE_PATH)
api_router = APIRouter(prefix=BASE_PATH, dependencies=[Depends(require_token)])


class SourceIn(BaseModel):
    title: str
    content: str
    filename: str | None = None


class QueryIn(BaseModel):
    question: str
    save: bool = False


class GithubSyncIn(BaseModel):
    repos: list[str] | None = None
    force: bool = False


# ---- sources (Layer 1) ----

@api_router.get("/api/sources")
def api_list_sources():
    return engine.list_sources()


@api_router.post("/api/sources", status_code=201)
def api_add_source(body: SourceIn):
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(400, "title and content are required")
    return engine.add_source(body.title.strip(), body.content, body.filename)


@api_router.get("/api/sources/{sid}")
def api_get_source(sid: str):
    try:
        return engine.get_source(sid)
    except KeyError:
        raise HTTPException(404, f"source {sid} not found")


@api_router.delete("/api/sources/{sid}")
def api_delete_source(sid: str):
    try:
        return engine.delete_source(sid)
    except KeyError:
        raise HTTPException(404, f"source {sid} not found")


@api_router.post("/api/sources/{sid}/ingest", dependencies=[Depends(require_llm_enabled)])
def api_ingest(sid: str):
    try:
        return engine.ingest(sid)
    except KeyError:
        raise HTTPException(404, f"source {sid} not found")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


# ---- wiki (Layer 2) ----

@api_router.get("/api/wiki/tree")
def api_wiki_tree():
    return engine.wiki_tree()


@api_router.get("/api/wiki/page")
def api_wiki_page(path: str):
    try:
        return engine.read_page(path)
    except (KeyError, ValueError):
        raise HTTPException(404, f"page not found: {path}")


@api_router.get("/api/wiki/history")
def api_wiki_history(limit: int = 50):
    return engine.git_log(limit)


# ---- operations ----

@api_router.post("/api/query", dependencies=[Depends(require_llm_enabled)])
def api_query(body: QueryIn):
    if not body.question.strip():
        raise HTTPException(400, "question is required")
    try:
        return engine.query(body.question.strip(), body.save)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@api_router.post("/api/lint", dependencies=[Depends(require_llm_enabled)])
def api_lint():
    try:
        return engine.lint()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@api_router.post("/api/github/sync", dependencies=[Depends(require_llm_enabled)])
def api_github_sync(body: GithubSyncIn):
    try:
        return github.sync(repos=body.repos, force=body.force)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(502, str(e))


@api_router.get("/api/github/status")
def api_github_status():
    return github.get_status()


@api_router.get("/api/health")
def api_health():
    return {"ok": True, "data_dir": str(engine.DATA_DIR), "version": APP_VERSION}


# ---- frontend ----

@router.get("/")
def frontend_index():
    return FileResponse(STATIC_DIR / "index.html")


app.include_router(router)
app.include_router(api_router)
if BASE_PATH:  # allow hitting the bare root locally too
    @app.get("/")
    def root_redirect():
        return FileResponse(STATIC_DIR / "index.html")

app.mount(f"{BASE_PATH}/static", StaticFiles(directory=STATIC_DIR), name="static")

# MCP server (streamable-http) — bearer-token gated, see app/mcp_server.py.
# Registered as exact Routes (not a Mount) so bare "/mcp" works without a
# 307 trailing-slash redirect, which some MCP clients refuse to follow.
_mcp_asgi = mcp_server.build_asgi_app()
app.router.routes.append(Route(f"{BASE_PATH}/mcp", _mcp_asgi, methods=["GET", "POST", "DELETE"]))
app.router.routes.append(Route(f"{BASE_PATH}/mcp/", _mcp_asgi, methods=["GET", "POST", "DELETE"]))
