# Security Policy

## Supported versions

This is a small self-hosted project without long-term support branches. Only the
latest commit on `main` receives security fixes — please update before reporting.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub instead: go to the repository's **Security** tab
and click **Report a vulnerability**. Include:

- what the issue is and which endpoint / file it affects
- steps to reproduce (a minimal request or config is ideal)
- the impact you think it has

You should get an initial response within a week. Once a fix is available, the
report can be disclosed publicly with credit to you, if you want it.

## Security model (what to expect)

Knowing the intended design helps tell bugs apart from configuration choices:

- **Single trust domain.** Everyone holding a token has full read/write access.
  There are no per-user accounts or permission scopes.
- **`/mcp` fails closed.** It requires `Authorization: Bearer <MCP_API_KEY>`; if
  `MCP_API_KEY` is unset, the endpoint returns 503. Host/Origin checks
  (`MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`) guard against DNS rebinding.
- **`/api/*` is open unless you set `API_TOKEN`.** This is intentional for local
  use. If you expose the app to the internet, set `API_TOKEN` — otherwise anyone
  can add sources and trigger LLM calls that spend your OpenRouter credit.
- **`DEMO_DISABLE_LLM`** blocks the credit-spending endpoints for public
  read-only demo instances.
- **LLM output is untrusted.** Files written by the LLM go through a path
  whitelist (`sources/`, `topics/`, `notes/`, `maintenance/`, `overview.md`) that
  rejects path traversal and protects `index.md`, `log.md`, and `SCHEMA.md`.

## In scope

- Authentication bypass on `/mcp` or `/api/*` (when the token is configured)
- Path traversal or writes outside the whitelisted wiki paths
- Prompt-injection paths that let source content write files or run commands
- Leaking secrets from `.env` through API responses, logs, or the data repo

## Out of scope

- Abuse of an instance where `API_TOKEN` was deliberately left unset
- LLM answers that are wrong or low quality (that is a bug, not a vulnerability)
- Issues in third-party services (OpenRouter, GitHub) or in your reverse proxy setup
