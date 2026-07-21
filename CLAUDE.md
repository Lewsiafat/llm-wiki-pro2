# llm-wiki-pro2

個人知識庫，實作 Andrej Karpathy 的「LLM Wiki」模式：LLM 不做即時 RAG，而是持續建立並維護一座 markdown wiki，知識隨來源累積而複利成長。

## 開發指令

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.sample .env   # 填入 OPENROUTER_API_KEY，本機開發 BASE_PATH 留空
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8777 --reload
```

打開 `http://127.0.0.1:8777/`。API 文件在 `/api/docs`。

## 架構

| 檔案 | 用途 |
|------|------|
| `app/main.py` | FastAPI 路由層（REST API + 靜態前端 + `/mcp` 掛載），API 為通用設計，不綁定前端 |
| `app/engine.py` | 核心引擎：三層架構、ingest/query/lint、git commit、prompt 模板 |
| `app/llm.py` | OpenRouter client（`deepseek/deepseek-v4-flash`，`AI_MODEL` 可覆寫） |
| `app/mcp_server.py` | MCP server（官方 `mcp` SDK，streamable-http）：6 個 tools 薄包 engine，bearer token 閘門 |
| `app/github.py` | GitHub connector：依 `data/github-repos.json`（owner + repo 清單，範例見 `github-repos.json.sample`）拉各 repo 的 README/CLAUDE.md/docs/specs 進 wiki 當 Layer 1 來源再走一般 ingest；設定檔不存在時 sync 回明確錯誤。同步狀態存 `data/github-sync.json`（content hash，未變更即 skip）；需 `GITHUB_TOKEN` 環境變數 |
| `static/index.html` | 單檔前端（vanilla JS + marked.js CDN，深色／淺色主題可切換，Wiki／來源並排對照） |
| `data/` | **執行期資料，獨立 git repo**（不在專案 repo 內，`.gitignore` 排除；位置可用 `DATA_DIR` 環境變數覆寫，預設 `<project>/data`） |

### 三層資料佈局（`data/`）

```
data/
├── sources/          # Layer 1：原始來源，永不被 LLM 修改（s001-slug.md）
├── sources.json      # 來源 metadata（id/title/status/wiki_pages）
└── wiki/             # Layer 2：LLM 擁有的 wiki
    ├── SCHEMA.md     # Layer 3：規範文件，注入每個 LLM prompt 的 system message
    ├── index.md      # 內容目錄（LLM 每次 ingest 完整重寫）
    ├── log.md        # 只增不改的時間軸（程式維護，結構化區塊：### <icon> TYPE · 時間 + 說明 + 條列，TYPE ∈ INGEST/QUERY/LINT/SYSTEM）
    ├── overview.md   # 全庫綜覽
    ├── sources/      # 來源摘要頁
    ├── topics/       # 跨來源主題頁
    ├── notes/        # 查詢存檔（q-<timestamp>.md）
    └── maintenance/  # lint 報告
```

### 設計決策（原始 gist 未指定、由實作自行決定的部分）

- **Ingest 採兩階段 LLM 呼叫**：Phase 1「規劃」（給 index.md + 檔案清單 + 來源 → 回 JSON：摘要頁路徑、要更新／新建的 topic 頁）；Phase 2「撰寫」（給計畫 + 待更新頁面現有內容 → 回分隔純文字區塊：完整檔案內容 + 全新 index.md）。避免一次呼叫塞進整座 wiki。
- **LLM 輸出格式**：Phase 1 規劃、query 選頁、lint 回純 JSON（`extract_json` 容忍 code fence / 前後雜訊）；**Phase 2 撰寫改用分隔純文字區塊**（`===FILE: path=== … ===END_FILE===`、`===INDEX_MD===`、`===LOG_LINE===`），因為 LLM 產長 markdown 塞進 JSON 字串時跳脫常壞掉。寫入路徑經白名單驗證（僅 `sources/ topics/ notes/ maintenance/ overview.md`，防 path traversal，`index.md`/`log.md`/`SCHEMA.md` 不可被 files 陣列覆寫）。
- **Git 策略**：`data/` 是巢狀獨立 git repo（由 engine 自動 `git init`），與部署用的專案 repo 完全分離——runtime commit 不會與部署時的 `git pull` 衝突。每個操作一個 commit：`wiki: ingest "…" — updated N pages`、`wiki: query "…" — saved answer as …`（未存檔的 query 也會留 `— log only` commit）、`wiki: lint — N issues found`、`source: add "…"`。
- **log.md 由程式寫入**（非 LLM），保證格式可解析；LLM 只提供 `log_line` 描述文字。每筆操作寫成一個結構化區塊（標題＋可選細節行＋可選條列），取代舊版單行長字串，可讀性較好；**既有部署上的舊格式紀錄不會回填，只有新寫入的條目套用新格式**。
- **Lint 只產報告不自動改頁**（報告存 `maintenance/`），避免 LLM 健檢時誤刪內容；修復由下次 ingest 或人工觸發。
- **儲存**：無資料庫。markdown 檔即資料，`sources.json` 是唯一的 metadata 索引。
- **主題切換**：CSS variables 定義在 `:root`（深色，預設）與 `:root[data-theme="light"]`（淺色 override），前端右上角按鈕切換，狀態存 `localStorage['lw_theme']`，預設深色。
- **併發**：`threading.Lock` 序列化所有寫入操作（LLM + git），單人使用足夠。

## REST API（通用，不綁前端）

```
GET/POST   /api/sources            列出／新增來源
GET/DELETE /api/sources/{id}       讀取／刪除來源
POST       /api/sources/{id}/ingest  觸發 LLM 匯入（30–90 秒）
GET        /api/wiki/tree          wiki 檔案清單 + index.md
GET        /api/wiki/page?path=    讀取單頁
GET        /api/wiki/history       data repo 的 git log
POST       /api/query              {question, save} 問答（可存檔）
POST       /api/lint               觸發健檢
POST       /api/github/sync        {repos?, force?} 同步 GitHub repos（每 repo 各跑一次 ingest，可能數分鐘）
GET        /api/github/status      github-sync.json 內容
GET        /api/health
```

## MCP server（`/mcp`）

同一個 FastAPI app 直接掛載 MCP server（streamable-http），**不另開 port/service**，端點在 `{BASE_PATH}/mcp`。

- **6 個 tools**（薄包 engine，與 REST API 共用同一套邏輯）：`add_source`、`ingest_source`、`query_wiki`、`lint_wiki`、`get_wiki_page`、`list_wiki_tree`
- **實作**：官方 `mcp` SDK 的 `FastMCP`（`stateless_http=True` + `json_response=True`，回應是純 JSON 不走 SSE，避開 nginx buffering）。沒用 `fastapi-mcp`，因為只要 6 個精選 tools、不想從路由自動生成。
- **掛載方式**：用兩條 exact `Route`（`/mcp` 與 `/mcp/`）而不是 `Mount`——`Mount` 對裸 `/mcp` 會回 307 trailing-slash redirect，部分 MCP client 不跟。`StreamableHTTPASGIApp` 直接使用（path-agnostic），因為 `streamable_http_app()` 內部的 exact Route 在外層路徑下會 404。
- **認證（安全決策）**：`/mcp` 有寫入能力（ingest 燒 OpenRouter credits）且公網可達，加 shared-secret bearer token：`.env` 的 `MCP_API_KEY`，請求需帶 `Authorization: Bearer <MCP_API_KEY>`。沒帶 → 401；`MCP_API_KEY` 未設 → fail closed 503。反代情境另需 `MCP_ALLOWED_HOSTS`／`MCP_ALLOWED_ORIGINS` 含公開域名（預設只允許 localhost），否則會被 DNS-rebinding 保護擋掉。
- **REST API 認證（opt-in，新增）**：`/api/*` 現在支援獨立的 `API_TOKEN` 環境變數（與 `MCP_API_KEY` 完全獨立、不同機制）。設定後所有 `/api/*` 端點要求 `Authorization: Bearer <API_TOKEN>`，未設定則維持全開放（向後相容）。前端首頁 `/` 與靜態資源永遠不擋，才能顯示登入視窗；前端會把 token 存在 `localStorage['lw_token']`，收到 401 自動彈出登入 modal。
- ingest/lint 經 MCP 呼叫一樣是 30–90 秒長請求；engine 是同步阻塞，tools 用 `anyio.to_thread` 跑，不會卡住 event loop。

## 部署

無 CI／自動部署：SSH 到伺服器手動 `git pull` → `pip install -r requirements.txt` → 重啟 systemd service。通用 self-host 步驟（venv、systemd unit、nginx 反代範例）見 README 的 Deployment 章節；反代情境下 `BASE_PATH` 要與 nginx 的 subpath 一致、`MCP_ALLOWED_HOSTS` 要含公開域名。

## 已知眉角

- 部署用 `python3 -m venv` + `requirements.txt`（伺服器上沒有 `uv`）；`pyproject.toml` 保留給未來改用 uv。
- ingest/lint 是長請求（兩次 LLM 呼叫），nginx `proxy_read_timeout` 已調到 300s，前端按鈕有 spinner；不要在中途重整。
- `data/` 被專案 `.gitignore` 排除——**wiki 內容只存在伺服器上的巢狀 git repo，記得另行備份**（見「待辦」）。
- deepseek-v4-flash 偶爾在 JSON 尾端加逗號或留下未跳脫的引號/換行，`extract_json` 已做輕量修復（只影響仍走 JSON 的規劃/query/lint 呼叫）；若 ingest 回 502 通常重按一次即可。
- **Prompt 截斷上限**（`engine.py` 頂部常數）：來源 24000 字元、單頁 8000、lint 全庫 60000——超長來源後段不會進 LLM prompt，必要時先手動拆分來源再匯入。
- **版本號**：目前 `0.0.2`（`0.0.1` 起算），三處要同步手動更新——`pyproject.toml` 的 `version`、`app/main.py` 的 `APP_VERSION` 常數（回傳於 `GET /api/health`，也是 FastAPI OpenAPI 的 version）、`static/index.html` 標頭的 `v0.0.x` 文字，外加 `CHANGELOG.md` 新增版本區塊。無自動化 bump 流程。

## 待辦

- **無任何測試**（尚未決定測試框架，目前只靠手動煙霧測試）。優先補的是有明確輸入輸出、不需打 LLM 的部分：`github._load_repo_config()` 的三條錯誤路徑、`engine` 的寫入路徑白名單／path traversal 防護、`extract_json` 的容錯修復。
- **`data/` 無異地備份**：內容只存在伺服器上的巢狀 git repo。`data/` 本身已是 git repo，加 private remote + 定期 push 的排程即可。
