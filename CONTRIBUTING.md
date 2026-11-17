# Contributing

Thanks for your interest! This is a small personal project, so the process is
lightweight. Bug reports, small fixes, and documentation improvements are very
welcome. For larger features, please open an issue first to discuss the idea
before writing code.

## Development setup

Follow [Running locally](README.md#running-locally) in the README. In short:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.sample .env    # set OPENROUTER_API_KEY
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8777 --reload
```

Runtime data goes to `data/` (a separate, nested git repo that is ignored by this
repo). Point `DATA_DIR` at a scratch directory if you don't want to touch your
real wiki while testing.

## Project layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI routes (REST API, static frontend, `/mcp` mount) |
| `app/engine.py` | Core logic: ingest / query / lint, prompts, git commits |
| `app/llm.py` | OpenRouter client |
| `app/mcp_server.py` | MCP tools (thin wrappers over the engine) |
| `app/github.py` | GitHub sync connector |
| `static/index.html` | Single-file frontend (vanilla JS, no build step) |

## Testing

There is **no automated test suite yet**. Before opening a PR, please smoke-test
the flows you touched by hand (add a source, ingest it, run a query, run lint)
and describe what you checked in the PR.

Tests are a welcome contribution. Good first targets are pieces with clear inputs
and outputs that don't need an LLM call:

- the write-path whitelist and path-traversal protection in `engine.py`
- `extract_json`'s tolerance for malformed LLM output (`app/llm.py`)
- the error paths in `github._load_repo_config()`

## Guidelines

- Keep dependencies minimal and the frontend a single vanilla-JS file with no
  build step.
- Don't commit secrets, `.env`, or anything from `data/`.
- Update `README.md` and `.env.sample` when you add or change an environment
  variable or endpoint.
- Add an entry under `## [Unreleased]` in `CHANGELOG.md` for user-visible changes.

### Versioning (maintainers)

The version lives in three places that must be bumped together:
`pyproject.toml` (`version`), `app/main.py` (`APP_VERSION`), and the `v0.0.x`
label in `static/index.html` — plus a new section in `CHANGELOG.md`.

## Security issues

Please don't report vulnerabilities in public issues — see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
