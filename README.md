# llm-wiki-pro2

A personal knowledge base implementing Andrej Karpathy's **"LLM Wiki"** pattern: instead of retrieval-at-query-time (RAG), the LLM continuously builds and maintains a markdown wiki. Every new source compounds — summaries, cross-source topic pages, and an index the LLM keeps coherent over time.

Self-hosted: run it locally or behind your own reverse proxy (see [Deployment](#deployment)).

**Live demo:** [https://lewsi.ddns.net/llm-wiki-pro2-demo/](https://lewsi.ddns.net/llm-wiki-pro2-demo/) — browse an already-populated wiki. It's public and unauthenticated, so the LLM-calling endpoints (ingest / query / lint / GitHub sync) are disabled there via `DEMO_DISABLE_LLM` (see the env var table below) to avoid burning API credit; everything read-only still works.

## The three-layer architecture

```
data/                      # its own git repo, separate from this one
├── sources/               # Layer 1 — raw source documents, never touched by the LLM
├── sources.json           # source metadata index
└── wiki/                  # Layer 2 — the LLM-owned wiki
    ├── SCHEMA.md          # Layer 3 — the spec injected into every LLM prompt
    ├── index.md           # table of contents (fully rewritten on each ingest)
    ├── log.md             # append-only timeline (written by code, not the LLM)
    ├── overview.md        # whole-library overview
    ├── sources/           # per-source summary pages
    ├── topics/            # cross-source topic pages
    ├── notes/             # saved query answers
    └── maintenance/       # lint reports
```

- **Layer 1 (sources)** is immutable input.
- **Layer 2 (wiki)** is entirely LLM-generated markdown, constrained by a path whitelist (no touching `index.md`/`log.md`/`SCHEMA.md` via the file-write channel, no path traversal).
- **Layer 3 (SCHEMA.md)** is the contract: structure, writing conventions, and workflows the LLM must follow — editable to steer the whole wiki's behavior.

## Features

- **Git commit per operation** — `data/` is a nested, standalone git repo; every ingest / query / lint / source change produces one commit, giving full history and diffability of the wiki's evolution (browsable via `/api/wiki/history`).
- **Two-phase ingest** — a *plan* call (which pages to create/update) followed by a *write* call (full page contents + rewritten index), so a single prompt never has to hold the entire wiki.
- **Query with citations** — page selection from the index, then an answer grounded only in selected pages, optionally saved as a `notes/` page.
- **Lint** — a whole-wiki health check (contradictions, stale claims, orphan pages, missing cross-links) that writes a report to `maintenance/` without auto-modifying pages.
- **No database** — markdown files are the data; `sources.json` is the only metadata index.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, uvicorn, httpx
- **LLM:** OpenRouter (`deepseek/deepseek-v4-flash` by default, override with `AI_MODEL`)
- **Frontend:** single-file vanilla JS + marked.js (CDN), dark theme, side-by-side wiki/source view
- **Storage:** markdown + JSON + git — no database

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.sample .env    # set OPENROUTER_API_KEY; leave BASE_PATH empty for local dev
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8777 --reload
```

Open `http://127.0.0.1:8777/`. (A `pyproject.toml` is included if you prefer `uv sync` / `uv run uvicorn ...`.)

`.env` variables (see `.env.sample`):

| Var | Purpose |
|-----|---------|
| `OPENROUTER_API_KEY` | required — all LLM calls go through OpenRouter |
| `AI_MODEL` | optional model override (default `deepseek/deepseek-v4-flash`) |
| `BASE_PATH` | subpath when behind a reverse proxy (e.g. `/llm-wiki-pro2`); empty for local |
| `PORT` | server port (used by the systemd unit; pass `--port` yourself in dev) |
| `DATA_DIR` | data location (defaults to `<project>/data`) |
| `MCP_API_KEY` | shared secret for the MCP endpoint (`openssl rand -hex 32`); if unset, `/mcp` returns 503 |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | comma-separated hosts/origins `/mcp` accepts (default: localhost only — set your public host when behind a proxy) |
| `API_TOKEN` | optional bearer token for `/api/*`; if unset the REST API stays open |
| `GITHUB_TOKEN` | optional — only for the GitHub sync connector (see below) |
| `DEMO_DISABLE_LLM` | optional — set `1`/`true`/`yes` to 403 the LLM-calling endpoints (ingest/query/lint/github-sync) while leaving GET/browse endpoints working; for public read-only demo instances |

## API

Generic REST API, not coupled to the bundled frontend. Full OpenAPI docs at `/api/docs`.

```
GET/POST   /api/sources               list / add sources
GET/DELETE /api/sources/{id}          read / delete a source
POST       /api/sources/{id}/ingest   LLM ingest (two LLM calls, 30–90 s)
GET        /api/wiki/tree             wiki file list + index.md
GET        /api/wiki/page?path=       read a single page
GET        /api/wiki/history          git log of the data repo
POST       /api/query                 {question, save} — answer from the wiki
POST       /api/lint                  run a wiki health check
POST       /api/github/sync           {repos?, force?} — ingest GitHub repo docs
GET        /api/github/status         last sync state per repo
GET        /api/health
```

### GitHub sync connector

`POST /api/github/sync` pulls `README.md` / `CLAUDE.md` / `docs/**` / `specs/**` from your own GitHub repos and ingests each repo as a Layer 1 source. To enable it:

1. Set `GITHUB_TOKEN` in `.env` (needs `repo` scope for private repos).
2. Copy `github-repos.json.sample` to `<DATA_DIR>/github-repos.json` and fill in your `owner` plus the repo lists (`standard_repos`, `full_tree_repos` for repos whose content sits outside the standard paths, `html_repos` for HTML-only repos, `excluded_repos` to skip).

Sync state (content hashes) is kept in `<DATA_DIR>/github-sync.json`, so unchanged repos are skipped on re-runs unless you pass `force: true`.

## MCP server

The same FastAPI app also exposes an **MCP server** (streamable-http transport) at `/mcp` under the base path — e.g. `https://your-domain.example/llm-wiki-pro2/mcp` — so any MCP client (Claude Desktop, agents) can drive the wiki directly. Six tools, each a thin wrapper over the same engine the REST API uses:

| Tool | Wraps |
|------|-------|
| `add_source` | `POST /api/sources` |
| `ingest_source` | `POST /api/sources/{id}/ingest` |
| `query_wiki` | `POST /api/query` |
| `lint_wiki` | `POST /api/lint` |
| `get_wiki_page` | `GET /api/wiki/page` |
| `list_wiki_tree` | `GET /api/wiki/tree` |

**Auth:** `/mcp` is write-capable and, once exposed, publicly reachable (ingest burns OpenRouter credits), so it requires a shared-secret bearer token: set `MCP_API_KEY` in `.env` and send `Authorization: Bearer <MCP_API_KEY>` on every MCP request. Without the header you get `401`; with `MCP_API_KEY` unset the endpoint fails closed (`503`). The REST API has its own independent, opt-in `API_TOKEN` (open when unset).

**Behind a reverse proxy** you must also set `MCP_ALLOWED_HOSTS` (and `MCP_ALLOWED_ORIGINS`) to include your public host — the MCP SDK's DNS-rebinding protection allows only localhost by default and otherwise rejects proxied requests with "Invalid Host header".

Example client config (Claude Desktop / any streamable-http MCP client):

```json
{
  "mcpServers": {
    "llm-wiki": {
      "type": "http",
      "url": "https://your-domain.example/llm-wiki-pro2/mcp",
      "headers": { "Authorization": "Bearer <MCP_API_KEY>" }
    }
  }
}
```

## Deployment

Self-hosting is a plain Python service behind a reverse proxy — no container or database required. The wiki data lives on the server in its own nested git repo (`DATA_DIR`) and is deliberately **not** part of this repository, so back it up separately.

**1. Install on the server**

```bash
git clone https://github.com/<you>/llm-wiki-pro2.git /opt/llm-wiki-pro2
cd /opt/llm-wiki-pro2
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.sample .env    # fill in OPENROUTER_API_KEY, MCP_API_KEY, BASE_PATH, PORT
```

**2. systemd unit** (`/etc/systemd/system/llm-wiki-pro2.service`)

```ini
[Unit]
Description=llm-wiki-pro2
After=network.target

[Service]
User=youruser
WorkingDirectory=/opt/llm-wiki-pro2
EnvironmentFile=/opt/llm-wiki-pro2/.env
ExecStart=/opt/llm-wiki-pro2/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

**3. nginx reverse proxy**

```nginx
location /llm-wiki-pro2/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;   # ingest/lint make two LLM calls (30–90 s)
}
```

Set `BASE_PATH=/llm-wiki-pro2` to match the subpath (leave empty when serving at the domain root), and remember `MCP_ALLOWED_HOSTS` if you use `/mcp`.

**4. Updating:** `git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart llm-wiki-pro2`.
