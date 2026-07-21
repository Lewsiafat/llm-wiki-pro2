"""Wiki engine — the three-layer LLM-wiki core.

Layer 1: data/sources/   raw source documents (immutable, never touched by the LLM)
Layer 2: data/wiki/      LLM-generated markdown (index.md, log.md, overview.md,
                         sources/, topics/, notes/, maintenance/)
Layer 3: data/wiki/SCHEMA.md  the config document injected into every LLM prompt

data/ is its own git repository; every wiki mutation produces one commit.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from app.llm import chat, extract_json

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
SOURCES_DIR = DATA_DIR / "sources"
WIKI_DIR = DATA_DIR / "wiki"
META_FILE = DATA_DIR / "sources.json"
SCHEMA_FILE = WIKI_DIR / "SCHEMA.md"
INDEX_FILE = WIKI_DIR / "index.md"
LOG_FILE = WIKI_DIR / "log.md"

LOCK = threading.Lock()  # serialize all wiki-mutating operations (LLM + git)

# LLM-writable locations inside data/wiki/
ALLOWED_PREFIXES = ("sources/", "topics/", "notes/", "maintenance/")
ALLOWED_FILES = ("overview.md",)

SOURCE_CHAR_LIMIT = 24000
PAGE_CHAR_LIMIT = 8000
LINT_TOTAL_CHAR_LIMIT = 60000

# ---------------------------------------------------------------------------
# Default files (Layer 3 schema + navigation skeleton)
# ---------------------------------------------------------------------------

DEFAULT_SCHEMA = """# Wiki Schema（LLM 維護規範）

本 Wiki 是一座個人知識庫，由 LLM 依照本文件的規範建立與維護。

## 結構

- `index.md` — 內容目錄，依分類列出所有頁面，每頁一行摘要 + 連結。主要導覽入口。
- `log.md` — 只增不改的時間軸紀錄（由程式維護，LLM 不寫入）。
- `overview.md` — 全庫綜覽：目前知識庫涵蓋哪些領域、重點主題之間的關聯。
- `sources/` — 來源摘要頁，每個原始來源一頁，檔名 `<source-id>-<slug>.md`。
- `topics/` — 主題／概念／實體頁，跨來源整合，檔名 kebab-case 英文 slug。
- `notes/` — 由查詢（Query）存檔下來的分析筆記。
- `maintenance/` — Lint 健檢報告。

## 撰寫慣例

- 所有內容以繁體中文撰寫；程式碼、指令、專有名詞、檔名保留英文原文。
- 每頁開頭為 `# 標題`，接著一行斜體摘要。
- 來源摘要頁需含「重點整理」與「與其他主題的關聯」段落，並在文末列出原始來源檔名。
- 主題頁整合多個來源的觀點；引用來源時以 `（來源：sources/xxx.md）` 標註。
- 內部連結一律使用相對路徑，例如 `[Agent 設計](topics/agent-design.md)`。
- 發現互相矛盾的說法時，不要靜默擇一，明確寫出「矛盾：A 說 …，B 說 …」。

## 工作流程

- **Ingest**：閱讀新來源 → 撰寫來源摘要頁 → 建立或更新相關主題頁 → 視需要更新 overview.md → 重寫 index.md。
- **Query**：從 index.md 挑出相關頁面 → 綜合回答並附引用 → 有價值的答案可存為 notes/ 頁面。
- **Lint**：檢查矛盾、過時敘述、孤兒頁（index 未列入）、缺漏的交叉連結，輸出報告到 maintenance/。
"""

DEFAULT_INDEX = """# Wiki 索引

> 本檔案為整個 Wiki 的內容目錄，由 LLM 維護。

## 總覽

- [overview.md](overview.md) — 全庫綜覽（尚未建立內容）

## 來源摘要

（尚無內容）

## 主題

（尚無內容）

## 查詢筆記

（尚無內容）

## 維護紀錄

（尚無內容）
"""

DEFAULT_LOG = """# Wiki Log

> 只增不改的時間軸紀錄，每筆操作一個區塊：📥 匯入（INGEST）／❓ 查詢（QUERY）／🔍 健檢（LINT）／⚙️ 系統（SYSTEM）。

### ⚙️ SYSTEM · {now}

wiki 初始化

---
"""

DEFAULT_OVERVIEW = """# 全庫綜覽

*尚無內容 — 匯入第一個來源後由 LLM 建立。*
"""

# ---------------------------------------------------------------------------
# Git helpers (data/ is its own repo)
# ---------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=DATA_DIR, capture_output=True, text=True, timeout=60
    )


def _commit(message: str) -> str | None:
    """Stage everything in data/ and commit. Returns short hash or None if no changes."""
    _git("add", "-A")
    r = _git(
        "-c", "user.name=llm-wiki", "-c", "user.email=llm-wiki@local",
        "commit", "-m", message,
    )
    if r.returncode != 0:
        if "nothing to commit" in (r.stdout + r.stderr):
            return None
        raise RuntimeError(f"git commit failed: {r.stderr[:300]}")
    return _git("rev-parse", "--short", "HEAD").stdout.strip()


def init_data() -> None:
    """Create the data layout, seed default files, init git repo. Idempotent."""
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("sources", "topics", "notes", "maintenance"):
        (WIKI_DIR / sub).mkdir(parents=True, exist_ok=True)
    if not META_FILE.exists():
        META_FILE.write_text("[]", encoding="utf-8")
    if not SCHEMA_FILE.exists():
        SCHEMA_FILE.write_text(DEFAULT_SCHEMA, encoding="utf-8")
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text(DEFAULT_INDEX, encoding="utf-8")
    if not LOG_FILE.exists():
        LOG_FILE.write_text(DEFAULT_LOG.format(now=_now()), encoding="utf-8")
    if not (WIKI_DIR / "overview.md").exists():
        (WIKI_DIR / "overview.md").write_text(DEFAULT_OVERVIEW, encoding="utf-8")
    if not (DATA_DIR / ".git").exists():
        _git("init")
        _commit("wiki: initialize data repository")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "untitled"


LOG_ICONS = {"INGEST": "📥", "QUERY": "❓", "LINT": "🔍", "SYSTEM": "⚙️"}


def _append_log(kind: str, title: str, detail: str = "", items: list[str] | None = None) -> None:
    """Append one readable log block: a heading, an optional detail line, and
    an optional bulleted item list — instead of one long run-on line."""
    icon = LOG_ICONS.get(kind, "•")
    block = f"### {icon} {kind} · {_now()}\n\n{title}\n"
    if detail:
        block += f"\n{detail}\n"
    if items:
        block += "\n" + "\n".join(f"- `{i}`" for i in items) + "\n"
    block += "\n---\n\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(block)


def _load_meta() -> list[dict]:
    return json.loads(META_FILE.read_text(encoding="utf-8"))


def _save_meta(meta: list[dict]) -> None:
    META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _safe_wiki_path(rel: str) -> Path:
    """Validate an LLM-proposed wiki path. Only .md files in allowed locations."""
    rel = rel.strip().lstrip("/")
    if not rel.endswith(".md") or ".." in rel or rel.startswith("."):
        raise ValueError(f"illegal wiki path: {rel}")
    if not (rel in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES)):
        raise ValueError(f"wiki path outside allowed locations: {rel}")
    p = (WIKI_DIR / rel).resolve()
    if not str(p).startswith(str(WIKI_DIR.resolve())):
        raise ValueError(f"path escapes wiki dir: {rel}")
    return p


def _wiki_file_list() -> list[str]:
    return sorted(
        str(p.relative_to(WIKI_DIR))
        for p in WIKI_DIR.rglob("*.md")
        if p.name not in ("SCHEMA.md",)
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（內容過長，已截斷，原文共 {len(text)} 字元）"


_FILE_BLOCK_RE = re.compile(
    r"===FILE:\s*(?P<path>[^\n=]+?)\s*===\n(?P<content>.*?)\n?===END_FILE===",
    re.DOTALL,
)
_INDEX_BLOCK_RE = re.compile(
    r"===INDEX_MD===\n(?P<content>.*?)\n?===END_INDEX_MD===", re.DOTALL
)
_LOG_BLOCK_RE = re.compile(
    r"===LOG_LINE===\n(?P<content>.*?)\n?===END_LOG_LINE===", re.DOTALL
)


def _parse_write_response(text: str) -> dict:
    """Parse the Phase-2 write response's plain-text block format (see
    INGEST_WRITE_PROMPT). Deliberately avoids JSON: at 16k tokens of freeform
    markdown, the LLM reliably fails to escape every quote/backslash/newline
    inside long string values, breaking json.loads unpredictably. Delimited
    plain-text blocks need no escaping at all."""
    files = [
        {"path": m.group("path").strip(), "content": m.group("content")}
        for m in _FILE_BLOCK_RE.finditer(text)
    ]
    if not files:
        raise ValueError(f"No ===FILE: ...=== blocks found in LLM response: {text[:300]}")
    index_m = _INDEX_BLOCK_RE.search(text)
    log_m = _LOG_BLOCK_RE.search(text)
    return {
        "files": files,
        "index_md": index_m.group("content") if index_m else "",
        "log_line": log_m.group("content").strip() if log_m else "",
    }


# ---------------------------------------------------------------------------
# Sources (Layer 1) CRUD
# ---------------------------------------------------------------------------


def list_sources() -> list[dict]:
    return _load_meta()


def get_source(sid: str) -> dict:
    for s in _load_meta():
        if s["id"] == sid:
            content = (SOURCES_DIR / s["filename"]).read_text(encoding="utf-8")
            return {**s, "content": content}
    raise KeyError(sid)


def add_source(title: str, content: str, filename: str | None = None) -> dict:
    with LOCK:
        meta = _load_meta()
        n = 1 + max((int(s["id"][1:]) for s in meta), default=0)
        sid = f"s{n:03d}"
        fname = f"{sid}-{_slugify(filename or title)}.md"
        (SOURCES_DIR / fname).write_text(content, encoding="utf-8")
        entry = {
            "id": sid,
            "title": title,
            "filename": fname,
            "added_at": _now(),
            "status": "pending",  # pending | ingested
            "ingested_at": None,
            "wiki_pages": [],
        }
        meta.append(entry)
        _save_meta(meta)
        _commit(f'source: add "{title}" ({sid})')
        return entry


def delete_source(sid: str) -> dict:
    with LOCK:
        meta = _load_meta()
        for s in meta:
            if s["id"] == sid:
                (SOURCES_DIR / s["filename"]).unlink(missing_ok=True)
                meta.remove(s)
                _save_meta(meta)
                _append_log("SYSTEM", f'來源刪除：{s["title"]}（{sid}）', "wiki 頁面保留，未一併移除")
                _commit(f'source: delete "{s["title"]}" ({sid})')
                return s
        raise KeyError(sid)


# ---------------------------------------------------------------------------
# Wiki (Layer 2) read access
# ---------------------------------------------------------------------------


def wiki_tree() -> dict:
    files = []
    for rel in _wiki_file_list():
        p = WIKI_DIR / rel
        head = ""
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                head = line[2:].strip()
                break
        files.append({
            "path": rel,
            "title": head or rel,
            "dir": rel.rsplit("/", 1)[0] if "/" in rel else "",
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return {"files": files, "index": INDEX_FILE.read_text(encoding="utf-8")}


def read_page(rel: str) -> dict:
    rel = rel.strip().lstrip("/")
    if ".." in rel or not rel.endswith(".md"):
        raise ValueError(f"illegal path: {rel}")
    p = (WIKI_DIR / rel).resolve()
    if not str(p).startswith(str(WIKI_DIR.resolve())) or not p.is_file():
        raise KeyError(rel)
    return {"path": rel, "content": p.read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the disciplined maintainer of a personal knowledge wiki. \
You strictly follow the wiki schema document provided below. \
All wiki content you write must be in Traditional Chinese (繁體中文); keep code, \
commands, proper nouns and file names in their original language. \
When asked for JSON, respond with ONLY one valid JSON object — no code fences, no prose.

=== WIKI SCHEMA ===
{schema}
"""

INGEST_PLAN_PROMPT = """A new source document is being ingested into the wiki.

=== CURRENT index.md ===
{index}

=== EXISTING WIKI FILES ===
{files}

=== NEW SOURCE (id: {sid}, title: {title}) ===
{content}

Plan the ingest. Decide:
1. the source summary page path: must be "sources/{sid}-<kebab-slug>.md"
2. which existing topic pages should be updated with knowledge from this source
3. which new topic pages should be created (only for substantial recurring concepts/entities; prefer updating existing pages over creating near-duplicates)
4. whether overview.md should be updated
5. the category name (in 繁體中文) under which this source belongs in index.md

Respond with ONLY this JSON:
{{
  "summary_line": "<one-line 繁體中文 summary of the source>",
  "category": "<分類名稱>",
  "source_page": "sources/{sid}-<slug>.md",
  "update_pages": ["topics/existing-page.md", ...],
  "create_pages": [{{"path": "topics/new-page.md", "purpose": "<why>"}}, ...],
  "update_overview": true/false
}}"""

INGEST_WRITE_PROMPT = """Execute the ingest plan: write full markdown content for every page.

=== NEW SOURCE (id: {sid}, title: {title}, file: sources_raw/{filename}) ===
{content}

=== PLAN ===
{plan}

=== CURRENT CONTENT OF PAGES TO UPDATE ===
{current_pages}

=== CURRENT index.md ===
{index}

Rules:
- Provide the COMPLETE new content for every file (source page, each updated/created topic page, overview.md if planned). Updated pages must retain their existing valuable content and integrate the new knowledge — never blindly replace.
- Rewrite index.md completely: every wiki page listed exactly once under its category, each with a one-line 繁體中文 summary. Keep the section structure (總覽 / 來源摘要 / 主題 / 查詢筆記 / 維護紀錄).
- Follow the schema conventions (citations like （來源：sources/xxx.md）, relative links, contradiction flagging).
- Do NOT use JSON. Respond using the plain-text block format below, verbatim markers, one block per file. Write raw markdown content between the markers — do not escape quotes, backslashes, or newlines; write them exactly as they should appear in the file.

Respond with ONLY blocks in this exact format (repeat the FILE block for every file to write; include the INDEX_MD and LOG_LINE blocks exactly once each):

===INDEX_MD===
<full new index.md content>
===END_INDEX_MD===

===LOG_LINE===
<short 繁體中文 description of what changed, one line>
===END_LOG_LINE===

===FILE: sources/....md===
<full markdown content for this file>
===END_FILE===

===FILE: topics/....md===
<full markdown content for this file>
===END_FILE==="""

INGEST_WRITE_RETRY_SUFFIX = """

Your previous reply could not be parsed ({error}). Re-send the COMPLETE response again using EXACTLY the block format requested: ===INDEX_MD===, ===END_INDEX_MD===, ===LOG_LINE===, ===END_LOG_LINE===, and one ===FILE: <path>=== / ===END_FILE=== pair per file, each marker alone on its own line. No JSON, no code fences, no commentary — just the blocks."""

QUERY_SELECT_PROMPT = """The user asked a question. Select the wiki pages most likely to contain the answer.

=== index.md ===
{index}

=== ALL WIKI FILES ===
{files}

=== QUESTION ===
{question}

Respond with ONLY this JSON (max 8 pages, most relevant first; empty list if nothing fits):
{{"pages": ["topics/xxx.md", ...]}}"""

QUERY_ANSWER_PROMPT = """Answer the user's question using ONLY the wiki pages below. \
Answer in 繁體中文, in well-structured markdown. Cite pages inline like （來源：topics/xxx.md）. \
If the wiki does not contain enough information, say so explicitly and answer with what is available.

=== WIKI PAGES ===
{pages}

=== QUESTION ===
{question}"""

LINT_PROMPT = """Perform a health check (lint) of the entire wiki. Look for:
1. 矛盾 — pages making contradictory claims
2. 過時 — stale statements superseded by newer sources
3. 孤兒頁 — pages missing from index.md
4. 缺漏連結 — related pages that should cross-reference each other but don't
5. 索引錯誤 — index.md entries pointing to non-existent pages or with wrong summaries

=== index.md ===
{index}

=== ALL WIKI PAGES ===
{pages}

Respond with ONLY this JSON:
{{
  "issues_count": <int>,
  "report_md": "<full 繁體中文 markdown report: # 標題, 逐項列出問題（含頁面路徑與建議修法）; 若無問題則說明檢查了哪些面向>"
}}"""


def _system_msg() -> dict:
    return {
        "role": "system",
        "content": SYSTEM_PROMPT.format(schema=SCHEMA_FILE.read_text(encoding="utf-8")),
    }


# ---------------------------------------------------------------------------
# Core operations: ingest / query / lint
# ---------------------------------------------------------------------------


def ingest(sid: str) -> dict:
    with LOCK:
        meta = _load_meta()
        src = next((s for s in meta if s["id"] == sid), None)
        if src is None:
            raise KeyError(sid)
        content = _truncate(
            (SOURCES_DIR / src["filename"]).read_text(encoding="utf-8"),
            SOURCE_CHAR_LIMIT,
        )
        index = INDEX_FILE.read_text(encoding="utf-8")
        files = "\n".join(_wiki_file_list())

        # Phase 1 — plan
        plan_raw = chat([
            _system_msg(),
            {"role": "user", "content": INGEST_PLAN_PROMPT.format(
                index=index, files=files, sid=sid, title=src["title"], content=content)},
        ])
        plan = extract_json(plan_raw)

        update_pages = [p for p in plan.get("update_pages", []) if (WIKI_DIR / p).is_file()]
        current_pages = "\n\n".join(
            f"--- {p} ---\n{_truncate((WIKI_DIR / p).read_text(encoding='utf-8'), PAGE_CHAR_LIMIT)}"
            for p in update_pages
        ) or "（無）"
        if plan.get("update_overview"):
            ov = _truncate((WIKI_DIR / "overview.md").read_text(encoding="utf-8"), PAGE_CHAR_LIMIT)
            current_pages += f"\n\n--- overview.md ---\n{ov}"

        # Phase 2 — write (retry a couple of times if the model returns
        # malformed JSON — more likely on large sources with long, code-heavy
        # content where the model forgets to escape a quote/newline)
        write_messages = [
            _system_msg(),
            {"role": "user", "content": INGEST_WRITE_PROMPT.format(
                sid=sid, title=src["title"], filename=src["filename"],
                content=content, plan=json.dumps(plan, ensure_ascii=False, indent=2),
                current_pages=current_pages, index=index)},
        ]
        result = None
        last_error: Exception | None = None
        for attempt in range(3):
            write_raw = chat(write_messages, max_tokens=16000)
            try:
                result = _parse_write_response(write_raw)
                break
            except ValueError as e:
                last_error = e
                write_messages = write_messages + [
                    {"role": "assistant", "content": write_raw},
                    {"role": "user", "content": INGEST_WRITE_RETRY_SUFFIX.format(error=e)},
                ]
        if result is None:
            raise last_error

        touched: list[str] = []
        for f in result.get("files", []):
            path = _safe_wiki_path(f["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f["content"], encoding="utf-8")
            touched.append(str(path.relative_to(WIKI_DIR)))
        if result.get("index_md"):
            INDEX_FILE.write_text(result["index_md"], encoding="utf-8")
            touched.append("index.md")
        if not touched:
            raise RuntimeError("LLM returned no files to write")

        _append_log("INGEST", f'{src["title"]}（{sid}）', result.get("log_line", ""), touched)
        commit = _commit(f'wiki: ingest "{src["title"]}" — updated {len(touched)} pages')

        src["status"] = "ingested"
        src["ingested_at"] = _now()
        src["wiki_pages"] = sorted(set(src["wiki_pages"] + touched))
        _save_meta(meta)
        _commit(f"meta: mark {sid} as ingested")

        return {"source": sid, "pages": touched, "commit": commit,
                "summary": plan.get("summary_line", "")}


def query(question: str, save: bool = False) -> dict:
    index = INDEX_FILE.read_text(encoding="utf-8")
    files = "\n".join(_wiki_file_list())

    select_raw = chat([
        _system_msg(),
        {"role": "user", "content": QUERY_SELECT_PROMPT.format(
            index=index, files=files, question=question)},
    ], max_tokens=1000)
    pages = [p for p in extract_json(select_raw).get("pages", [])
             if (WIKI_DIR / p).is_file()][:8]

    pages_text = "\n\n".join(
        f"--- {p} ---\n{_truncate((WIKI_DIR / p).read_text(encoding='utf-8'), PAGE_CHAR_LIMIT)}"
        for p in pages
    ) or "（Wiki 目前沒有可參考的頁面）"

    answer = chat([
        _system_msg(),
        {"role": "user", "content": QUERY_ANSWER_PROMPT.format(
            pages=pages_text, question=question)},
    ])

    saved_page = None
    if save:
        with LOCK:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            rel = f"notes/q-{ts}.md"
            note = (f"# 查詢筆記：{question}\n\n*建立於 {_now()}，參考頁面："
                    f"{', '.join(pages) or '無'}*\n\n---\n\n{answer}\n")
            (WIKI_DIR / rel).write_text(note, encoding="utf-8")
            # register in index.md under 查詢筆記 section
            idx = INDEX_FILE.read_text(encoding="utf-8")
            line = f"- [{rel}]({rel}) — {question[:80]}"
            if "## 查詢筆記" in idx:
                idx = idx.replace("## 查詢筆記\n\n（尚無內容）", "## 查詢筆記\n")
                idx = idx.replace("## 查詢筆記", f"## 查詢筆記\n\n{line}", 1) \
                    if line not in idx else idx
                # normalize accidental double blank lines
                idx = re.sub(r"\n{4,}", "\n\n\n", idx)
            else:
                idx += f"\n## 查詢筆記\n\n{line}\n"
            INDEX_FILE.write_text(idx, encoding="utf-8")
            _append_log("QUERY", question[:100], f"已存為 {rel}")
            _commit(f'wiki: query "{question[:60]}" — saved answer as {rel}')
            saved_page = rel
    else:
        _append_log("QUERY", question[:100])
        with LOCK:
            _commit(f'wiki: query "{question[:60]}" — log only')

    return {"answer": answer, "pages_used": pages, "saved_page": saved_page}


def lint() -> dict:
    with LOCK:
        index = INDEX_FILE.read_text(encoding="utf-8")
        parts, total = [], 0
        for rel in _wiki_file_list():
            if rel.startswith("maintenance/"):
                continue
            text = _truncate((WIKI_DIR / rel).read_text(encoding="utf-8"), 4000)
            total += len(text)
            if total > LINT_TOTAL_CHAR_LIMIT:
                parts.append(f"--- {rel} ---\n（略過：已達內容上限）")
                continue
            parts.append(f"--- {rel} ---\n{text}")

        result = extract_json(chat([
            _system_msg(),
            {"role": "user", "content": LINT_PROMPT.format(
                index=index, pages="\n\n".join(parts))},
        ], max_tokens=8000))

        ts = datetime.now().strftime("%Y%m%d-%H%M")
        rel = f"maintenance/lint-{ts}.md"
        (WIKI_DIR / rel).write_text(result.get("report_md", ""), encoding="utf-8")
        n = result.get("issues_count", 0)
        _append_log("LINT", f"發現 {n} 個問題", f"報告：{rel}")
        commit = _commit(f"wiki: lint — {n} issues found, report at {rel}")
        return {"issues_count": n, "report_page": rel, "commit": commit}


def git_log(limit: int = 50) -> list[dict]:
    r = _git("log", f"-{limit}", "--pretty=format:%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M")
    if r.returncode != 0:
        return []
    return [
        {"hash": h, "date": d, "message": m}
        for h, d, m in (line.split("|", 2) for line in r.stdout.splitlines() if line)
    ]
