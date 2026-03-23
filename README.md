# LarkScout

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/ReadyForAI/LarkScout/actions/workflows/ci.yml/badge.svg)](https://github.com/ReadyForAI/LarkScout/actions)

[English](#english) | [中文](#中文)

---

## English

Open-source data collection and document parsing platform by [ReadyForAI](https://github.com/ReadyForAI).

### Features

- **Web capture** — one-shot URL → structured document (Playwright-powered)
- **Document parsing** — PDF, DOCX, XLSX, CSV with OCR fallback
- **Three-tier summaries** — digest (~200 tokens) → brief (~1500 tokens) → section (on-demand)
- **Multi-LLM support** — Gemini (default), OpenAI, DeepSeek, Ollama, Groq, or any OpenAI-compatible API
- **Table extraction** — automatic HTML/sheet tables → Markdown with statistics
- **WebMCP** — Chrome 146+ structured tool discovery (MCP-over-HTTP)
- **i18n** — English and Chinese (set `LANG=zh`)

### Quick Start

#### Docker (recommended)

```bash
# Clone and configure
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
cp .env.example .env          # add your GEMINI_API_KEY

# Start the service
docker compose up -d

# Check health
curl http://localhost:9898/health
```

#### Python (local)

```bash
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
pip install -r requirements.txt
playwright install chromium

export GEMINI_API_KEY=your_key_here
python larkscout_server.py     # listens on port 9898
```

### Docker

The `docker-compose.yml` provides a single-service setup with a persistent named volume for the document library.

```yaml
# docker-compose.yml (excerpt)
services:
  larkscout:
    build: .
    ports:
      - "9898:9898"
    volumes:
      - larkscout-docs:/root/.larkscout/docs   # document library persists across restarts
```

**Environment variables (pass via `.env` or `docker compose` `environment` block):**

| Variable | Default | Description |
|---|---|---|
| `LARKSCOUT_LLM_PROVIDER` | `gemini` | LLM backend: `gemini` or `openai` |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `LARKSCOUT_LLM_API_KEY` | — | API key for OpenAI-compatible provider |
| `LARKSCOUT_LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compat provider |
| `LARKSCOUT_LLM_MODEL` | provider default | Model name override |
| `LARKSCOUT_DOCS_DIR` | `~/.larkscout/docs` | Document library directory |

#### Using a local Ollama model

```bash
LARKSCOUT_LLM_PROVIDER=openai \
LARKSCOUT_LLM_API_KEY=ollama \
LARKSCOUT_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
LARKSCOUT_LLM_MODEL=llama3 \
docker compose up
```

### API

All endpoints are served on port **9898**.

#### Core

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Aggregated health check for all services |
| `GET` | `/web/health` | Browser sub-app health |
| `GET` | `/doc/health` | DocReader sub-app health (includes `docs_dir`) |
| `POST` | `/web/capture` | Capture a URL and persist it to the document library |
| `POST` | `/doc/parse` | Upload and parse a document (PDF, DOCX, XLSX, CSV) |

#### Browser Session API

Stateful browser sessions for multi-step web automation.

| Method | Path | Description |
|---|---|---|
| `POST` | `/web/session/new` | Open a new Playwright browser session |
| `POST` | `/web/session/goto` | Navigate the session to a URL |
| `POST` | `/web/session/distill` | Extract structured content from the current page |
| `POST` | `/web/session/read_sections` | Retrieve specific sections by ID from the last distill |
| `POST` | `/web/session/act` | Click, type, select, or scroll an interactive element |
| `POST` | `/web/session/scroll` | Scroll the page up or down by a pixel amount |
| `POST` | `/web/session/navigate` | Go back or forward in the browser history |
| `POST` | `/web/session/webmcp_discover` | Discover WebMCP tools exposed by the current page |
| `POST` | `/web/session/webmcp_invoke` | Invoke a WebMCP tool by name |
| `POST` | `/web/session/export_storage_state` | Export cookies and local storage for session reuse |
| `POST` | `/web/session/close` | Close the session and release browser resources |

#### Document Library API

Access documents stored by `/web/capture` and `/doc/parse`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/doc/library/search` | Search by keyword, tag, and/or file type |
| `GET` | `/doc/library/{doc_id}/digest` | Short summary (~200 tokens) |
| `GET` | `/doc/library/{doc_id}/brief` | Extended summary (~1500 tokens) |
| `GET` | `/doc/library/{doc_id}/full` | Full document text |
| `GET` | `/doc/library/{doc_id}/sections` | List all sections with metadata |
| `GET` | `/doc/library/{doc_id}/section/{sid}` | Full text of a single section |
| `GET` | `/doc/library/{doc_id}/table/{table_id}` | Markdown table with column statistics |
| `GET` | `/doc/library/{doc_id}/manifest` | Provenance metadata (source, timestamps, content hash) |

Full API reference: see [`skills/larkscout-browser-SKILL.md`](skills/larkscout-browser-SKILL.md) and [`skills/larkscout-docreader-SKILL.md`](skills/larkscout-docreader-SKILL.md).

### Configuration

LarkScout is configured entirely through environment variables. See the table in the **Docker** section above for LLM settings. Additional variables:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `9898` | HTTP listening port |
| `LANG` | `en` | UI language (`en` or `zh`) |

### Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code conventions, and the PR process.

### License

MIT — see [LICENSE](LICENSE).

---

## 中文

由 [ReadyForAI](https://github.com/ReadyForAI) 开源的数据采集与文档解析平台。

### 功能特性

- **网页抓取** — 一次调用 URL → 结构化文档（基于 Playwright）
- **文档解析** — 支持 PDF、DOCX、XLSX、CSV，可自动 OCR 兜底
- **三层摘要** — digest（约 200 token）→ brief（约 1500 token）→ section（按需加载）
- **多 LLM 支持** — Gemini（默认）、OpenAI、DeepSeek、Ollama、Groq，以及任意 OpenAI 兼容接口
- **表格提取** — 自动将 HTML/表格文件转为带统计信息的 Markdown
- **WebMCP** — Chrome 146+ 结构化工具发现（MCP-over-HTTP）
- **多语言** — 支持中英文（设置 `LANG=zh` 切换为中文）

### 快速上手

#### Docker（推荐）

```bash
# 克隆并配置
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
cp .env.example .env          # 填入你的 GEMINI_API_KEY

# 启动服务
docker compose up -d

# 检查服务状态
curl http://localhost:9898/health
```

#### Python（本地运行）

```bash
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
pip install -r requirements.txt
playwright install chromium

export GEMINI_API_KEY=your_key_here
python larkscout_server.py     # 监听 9898 端口
```

### Docker 配置

`docker-compose.yml` 提供单服务部署方案，文档库数据通过 named volume 持久化。

```yaml
# docker-compose.yml（节选）
services:
  larkscout:
    build: .
    ports:
      - "9898:9898"
    volumes:
      - larkscout-docs:/root/.larkscout/docs   # 重启后文档库数据不丢失
```

**环境变量（通过 `.env` 文件或 `docker compose` 的 `environment` 块传入）：**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LARKSCOUT_LLM_PROVIDER` | `gemini` | LLM 后端：`gemini` 或 `openai` |
| `GEMINI_API_KEY` | — | Google Gemini API Key |
| `LARKSCOUT_LLM_API_KEY` | — | OpenAI 兼容接口的 API Key |
| `LARKSCOUT_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口的 Base URL |
| `LARKSCOUT_LLM_MODEL` | 各 provider 默认值 | 指定模型名称 |
| `LARKSCOUT_DOCS_DIR` | `~/.larkscout/docs` | 文档库存储目录 |

#### 使用本地 Ollama 模型

```bash
LARKSCOUT_LLM_PROVIDER=openai \
LARKSCOUT_LLM_API_KEY=ollama \
LARKSCOUT_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
LARKSCOUT_LLM_MODEL=llama3 \
docker compose up
```

### API 接口

所有接口均运行在 **9898** 端口。

#### 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 全服务聚合健康检查 |
| `GET` | `/web/health` | Browser 子服务健康检查 |
| `GET` | `/doc/health` | DocReader 子服务健康检查（含 `docs_dir`） |
| `POST` | `/web/capture` | 抓取 URL 并保存到文档库 |
| `POST` | `/doc/parse` | 上传并解析文档（PDF、DOCX、XLSX、CSV） |

#### Browser Session API

有状态浏览器会话，支持多步骤网页自动化。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/web/session/new` | 打开新的 Playwright 浏览器会话 |
| `POST` | `/web/session/goto` | 在当前会话中导航到指定 URL |
| `POST` | `/web/session/distill` | 从当前页面提取结构化内容 |
| `POST` | `/web/session/read_sections` | 按 ID 获取上次 distill 的指定章节 |
| `POST` | `/web/session/act` | 对交互元素执行点击、输入、选择或滚动 |
| `POST` | `/web/session/scroll` | 按像素上下滚动页面 |
| `POST` | `/web/session/navigate` | 浏览器前进或后退 |
| `POST` | `/web/session/webmcp_discover` | 发现当前页面暴露的 WebMCP 工具 |
| `POST` | `/web/session/webmcp_invoke` | 按名称调用 WebMCP 工具 |
| `POST` | `/web/session/export_storage_state` | 导出 Cookie 和 LocalStorage 以复用会话 |
| `POST` | `/web/session/close` | 关闭会话并释放浏览器资源 |

#### Document Library API

访问由 `/web/capture` 和 `/doc/parse` 存入文档库的文档。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/doc/library/search` | 按关键词、标签和/或文件类型搜索 |
| `GET` | `/doc/library/{doc_id}/digest` | 简短摘要（约 200 token） |
| `GET` | `/doc/library/{doc_id}/brief` | 详细摘要（约 1500 token） |
| `GET` | `/doc/library/{doc_id}/full` | 完整文档正文 |
| `GET` | `/doc/library/{doc_id}/sections` | 列出所有章节及元数据 |
| `GET` | `/doc/library/{doc_id}/section/{sid}` | 单个章节的完整文本 |
| `GET` | `/doc/library/{doc_id}/table/{table_id}` | 带列统计的 Markdown 表格 |
| `GET` | `/doc/library/{doc_id}/manifest` | 来源元数据（来源地址、时间戳、内容哈希） |

完整 API 说明见 [`skills/larkscout-browser-SKILL.md`](skills/larkscout-browser-SKILL.md) 和 [`skills/larkscout-docreader-SKILL.md`](skills/larkscout-docreader-SKILL.md)。

### 配置项

LarkScout 所有配置均通过环境变量管理。LLM 相关配置见上方 **Docker 配置** 一节，其他变量如下：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `9898` | HTTP 监听端口 |
| `LANG` | `en` | 界面语言（`en` 英文 / `zh` 中文） |

### 参与贡献

欢迎提交 PR！开发环境搭建、代码规范和 PR 流程请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 许可证

MIT 协议，详见 [LICENSE](LICENSE)。
