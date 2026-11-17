# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `SECURITY.md` — how to report vulnerabilities privately, plus the intended
  security model
- `CONTRIBUTING.md` — development setup, testing expectations, versioning notes

### Removed
- `CLAUDE.md` is no longer tracked (local AI-assistant notes, kept out of the
  public repo)

## [0.0.3] - 2026-07-26

### Added
- `DEMO_DISABLE_LLM` environment variable — when set, blocks the LLM-calling
  endpoints (`/api/sources/{id}/ingest`, `/api/query`, `/api/lint`,
  `/api/github/sync`) with a 403 while GET/browse endpoints keep working, for
  running a public, unauthenticated read-only demo instance
- README: link to a live public demo

## [0.0.2] - 2026-07-20

Open-source preparation: configuration moved out of code, personal deployment
details removed from the repository.

### Added
- MIT `LICENSE`; `pyproject.toml` metadata (`license`, `authors`, `[project.urls]`)
- `github-repos.json.sample` — example config for the GitHub connector
- `MCP_ALLOWED_ORIGINS` environment variable (CORS origins for `/mcp`)
- Generic self-host deployment guide in the README (venv, systemd unit, nginx reverse proxy)

### Changed
- **Breaking:** the GitHub connector's owner and repo lists moved from hardcoded
  constants to `<DATA_DIR>/github-repos.json`. `POST /api/github/sync` returns an
  explicit error until that file exists.
- **Breaking:** `MCP_ALLOWED_HOSTS` now defaults to localhost only. Deployments
  behind a reverse proxy must set it explicitly, or `/mcp` rejects requests with
  "Invalid Host header".
- `.env.sample`: generic defaults (`PORT=8000`, empty `BASE_PATH`), documented
  `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` and the GitHub connector config
- README and CLAUDE.md rewritten from a self-host perspective; personal deployment
  details are no longer in the repository

### Removed
- `.github/workflows/deploy.yml` — deployment is now a manual SSH step
  (git pull → pip install → systemctl restart)

## [0.0.1] - 2026-07-06

First versioned release. (No git tag exists for this version; it covers everything
up to the open-source preparation work.)

### Added
- Three-layer LLM-wiki engine: immutable sources (Layer 1), LLM-owned markdown wiki
  (Layer 2), `SCHEMA.md` spec injected into every prompt (Layer 3)
- Two-phase ingest (plan call, then write call), query with citations, whole-wiki lint
- One git commit per operation in the nested `data/` repository
- REST API plus a single-file frontend (dark/light themes, side-by-side wiki/source view)
- MCP server at `/mcp` (streamable-http, 6 tools, bearer-token gated)
- GitHub connector: sync repo docs into the wiki as sources, skipping unchanged repos
- Opt-in `API_TOKEN` auth for the REST API

### Fixed
- Ingest write phase switched from JSON to delimited plain-text blocks — long markdown
  in JSON strings broke escaping too often
- Repair pass for unescaped quotes/newlines and trailing commas in LLM JSON responses
- GitHub sync state saved after every repo instead of once at the end

[0.0.3]: https://github.com/Lewsiafat/llm-wiki-pro2/releases/tag/v0.0.3
[0.0.2]: https://github.com/Lewsiafat/llm-wiki-pro2/releases/tag/v0.0.2
