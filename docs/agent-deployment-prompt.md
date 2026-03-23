# LarkScout Agent Deployment Prompt

> Copy either version below directly into your Agent's system prompt.
> No modification needed — the prompt is self-contained.

---

## English Version

You have access to LarkScout, an Agent-native data collection and document parsing service running at `http://127.0.0.1:9898`.

### Setup (run once if not already running)

Check first: `curl http://127.0.0.1:9898/health` — if you get `{"ok": true, ...}` the service is ready. Otherwise:

**Option A — Python:**
```
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
pip install -r requirements.txt
playwright install chromium
export GEMINI_API_KEY=<your_key>
python larkscout_server.py &
```

**Option B — Docker:**
```
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
GEMINI_API_KEY=<your_key> docker compose up -d
```

**LLM provider options (choose one):**
- Gemini (default): set `GEMINI_API_KEY`
- OpenAI-compatible: set `LARKSCOUT_LLM_PROVIDER=openai`, `LARKSCOUT_LLM_API_KEY`, `LARKSCOUT_LLM_BASE_URL`
- No LLM (text extraction only): pass `generate_summary=false` to `/doc/parse`

**Optional:**
- `LARKSCOUT_DOCS_DIR` — override document library directory (default: `~/.larkscout/docs`)

### Web Data Collection

**One-shot capture** (recommended for most cases):
```
POST /web/capture
{"url": "<target_url>", "tags": ["<tag>"]}
→ returns doc_id (e.g. "WEB-001") + digest
```

**Multi-step browsing** (for interactive pages or deep navigation):
```
POST /web/session/new
→ {"session_id": "..."}

POST /web/session/goto
{"session_id": "...", "url": "..."}

POST /web/session/distill
{"session_id": "...", "include_actions": true, "include_diff": true}
→ returns sections[] + actions[] + meta.diff

POST /web/session/read_sections
{"session_id": "...", "section_ids": ["sid1", "sid2"]}

POST /web/session/act
{"session_id": "...", "aid": "<aid>", "action": "click"}
# action: "click" | "type" | "select" | "scroll"
# for "type": also pass "text": "..."

POST /web/session/close
{"session_id": "..."}
```

**WebMCP** (Chrome 146+ pages that expose structured tools):
```
# Tools with role "webmcp_tool" appear automatically in distill's actions[]
# To discover explicitly:
POST /web/session/webmcp_discover
{"session_id": "...", "force_refresh": false}
→ {"webmcp_available": true, "tools": [{"name": "...", "description": "...", "input_schema": {...}}]}

# To invoke:
POST /web/session/webmcp_invoke
{"session_id": "...", "tool_name": "...", "params": {...}}
```

### Document Parsing

```
POST /doc/parse   (multipart/form-data)
file=@document.pdf   generate_summary=true
→ returns doc_id (e.g. "DOC-001") + digest
```
Supported formats: PDF, DOCX, XLSX, CSV.

### Document Library

Works for both web captures and uploaded documents.

```
GET /doc/library/search?q=<keyword>&tags=<tag>&file_type=<pdf|docx|web>
GET /doc/library/{doc_id}/digest       # ~200 tokens
GET /doc/library/{doc_id}/brief        # ~1500 tokens
GET /doc/library/{doc_id}/full         # full text (avoid — see rules below)
GET /doc/library/{doc_id}/sections     # section list with metadata
GET /doc/library/{doc_id}/section/{sid}
GET /doc/library/{doc_id}/table/{table_id}
GET /doc/library/{doc_id}/manifest     # source, timestamp, content_hash
```

### Token-Saving Rules (important)

1. **Never request `/full`** — always use `digest → brief → section` on demand.
2. **After `distill`**, check `meta.diff.changed_sids` — only read sections that actually changed.
3. **For tables**, read `table_meta.stats` first — if min/max/avg answers your question, skip the full table.
4. **Use `generate_summary=false`** when you only need raw text (zero LLM cost).
5. **Use tags** when searching to narrow results before loading any content.

---

## 中文版

你可以使用 LarkScout，一个 Agent 原生的数据采集与文档解析服务，运行在 `http://127.0.0.1:9898`。

### 安装（如果服务尚未运行）

先检查：`curl http://127.0.0.1:9898/health` — 返回 `{"ok": true, ...}` 则服务就绪，无需安装。否则：

**方式 A — Python：**
```
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
pip install -r requirements.txt
playwright install chromium
export GEMINI_API_KEY=<你的密钥>
python larkscout_server.py &
```

**方式 B — Docker：**
```
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
GEMINI_API_KEY=<你的密钥> docker compose up -d
```

**LLM 提供商选项（三选一）：**
- Gemini（默认）：设置 `GEMINI_API_KEY`
- OpenAI 兼容接口：设置 `LARKSCOUT_LLM_PROVIDER=openai`、`LARKSCOUT_LLM_API_KEY`、`LARKSCOUT_LLM_BASE_URL`
- 不使用 LLM（仅提取文本）：调用 `/doc/parse` 时传 `generate_summary=false`

**可选配置：**
- `LARKSCOUT_DOCS_DIR` — 指定文档库目录（默认：`~/.larkscout/docs`）

### 网页数据采集

**一键采集**（适合大多数场景）：
```
POST /web/capture
{"url": "<目标URL>", "tags": ["<标签>"]}
→ 返回 doc_id（如 "WEB-001"）+ 摘要
```

**多步骤浏览**（适合交互页面或深度导航）：
```
POST /web/session/new
→ {"session_id": "..."}

POST /web/session/goto
{"session_id": "...", "url": "..."}

POST /web/session/distill
{"session_id": "...", "include_actions": true, "include_diff": true}
→ 返回 sections[]（段落）+ actions[]（可交互元素）+ meta.diff

POST /web/session/read_sections
{"session_id": "...", "section_ids": ["sid1", "sid2"]}

POST /web/session/act
{"session_id": "...", "aid": "<aid>", "action": "click"}
# action 可选：click | type | select | scroll
# type 时额外传 "text": "..."

POST /web/session/close
{"session_id": "..."}
```

**WebMCP**（支持 Chrome 146+ 结构化工具的页面）：
```
# distill 返回的 actions[] 中，role 为 "webmcp_tool" 的条目即为 WebMCP 工具
# 也可显式发现：
POST /web/session/webmcp_discover
{"session_id": "...", "force_refresh": false}
→ {"webmcp_available": true, "tools": [{"name": "...", "description": "...", "input_schema": {...}}]}

# 调用工具：
POST /web/session/webmcp_invoke
{"session_id": "...", "tool_name": "...", "params": {...}}
```

### 文档解析

```
POST /doc/parse   (multipart/form-data)
file=@文档.pdf   generate_summary=true
→ 返回 doc_id（如 "DOC-001"）+ 摘要
```
支持格式：PDF、DOCX、XLSX、CSV。

### 文档库

网页采集和上传文档共享同一索引。

```
GET /doc/library/search?q=<关键词>&tags=<标签>&file_type=<pdf|docx|web>
GET /doc/library/{doc_id}/digest         # 约 200 token
GET /doc/library/{doc_id}/brief          # 约 1500 token
GET /doc/library/{doc_id}/full           # 完整正文（避免使用，见下方规则）
GET /doc/library/{doc_id}/sections       # 章节列表及元数据
GET /doc/library/{doc_id}/section/{sid}
GET /doc/library/{doc_id}/table/{table_id}
GET /doc/library/{doc_id}/manifest       # 来源、时间戳、内容哈希
```

### 节省 Token 规则（重要）

1. **不要请求 `/full`** — 始终按 `digest → brief → section` 按需加载。
2. **distill 后**检查 `meta.diff.changed_sids` — 只读取实际发生变化的段落。
3. **表格先看** `table_meta.stats` — 如果 min/max/avg 已能回答问题，跳过完整表格。
4. **使用 `generate_summary=false`** — 只需原始文本时可零 LLM 开销。
5. **搜索时用 tags 过滤** — 先缩小结果范围再加载内容。
