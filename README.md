# LarkScout

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/ReadyForAI/LarkScout/actions/workflows/ci.yml/badge.svg)](https://github.com/ReadyForAI/LarkScout/actions)

Open-source data collection and document parsing platform by [ReadyForAI](https://github.com/ReadyForAI).

## Features

- **Web capture** — one-shot URL → structured document (Playwright-powered)
- **Document parsing** — PDF, DOCX, XLSX, CSV with OCR fallback
- **Three-tier summaries** — digest (~200 tokens) → brief (~1500 tokens) → section (on-demand)
- **Multi-LLM support** — Gemini (default), OpenAI, DeepSeek, Ollama, Groq, or any OpenAI-compatible API
- **Table extraction** — automatic HTML/sheet tables → Markdown with statistics
- **WebMCP** — Chrome 146+ structured tool discovery (MCP-over-HTTP)
- **i18n** — English and Chinese (set `LANG=zh`)

## Quick Start

### Docker (recommended)

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

### Python (local)

```bash
git clone https://github.com/ReadyForAI/LarkScout.git
cd LarkScout
pip install -r requirements.txt
playwright install chromium

export GEMINI_API_KEY=your_key_here
python larkscout_server.py     # listens on port 9898
```

## Docker

The `docker-compose.yml` provides a single-service setup with a persistent `docs/` volume.

```yaml
# docker-compose.yml (excerpt)
services:
  larkscout:
    build: .
    ports:
      - "9898:9898"
    volumes:
      - ./docs:/app/docs   # document library persists across restarts
```

**Environment variables (pass via `.env` or `docker compose` `environment` block):**

| Variable | Default | Description |
|---|---|---|
| `LARKSCOUT_LLM_PROVIDER` | `gemini` | LLM backend: `gemini` or `openai` |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `LARKSCOUT_LLM_API_KEY` | — | API key for OpenAI-compatible provider |
| `LARKSCOUT_LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compat provider |
| `LARKSCOUT_LLM_MODEL` | provider default | Model name override |

### Using a local Ollama model

```bash
LARKSCOUT_LLM_PROVIDER=openai \
LARKSCOUT_LLM_API_KEY=ollama \
LARKSCOUT_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
LARKSCOUT_LLM_MODEL=llama3 \
docker compose up
```

## API

All endpoints are served on port **9898**.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health + version |
| `POST` | `/web/capture` | Capture a URL → doc_id |
| `GET` | `/web/health` | Browser sub-app health |
| `POST` | `/doc/parse` | Upload and parse a document |
| `GET` | `/doc/health` | DocReader sub-app health |

Full API reference: see `skills/larkscout-browser-SKILL.md` and `skills/larkscout-docreader-SKILL.md`.

## Configuration

LarkScout is configured entirely through environment variables. See the table in the **Docker** section above for LLM settings. Additional variables:

| Variable | Default | Description |
|---|---|---|
| `PORT` | `9898` | HTTP listening port |
| `LANG` | `en` | UI language (`en` or `zh`) |

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code conventions, and the PR process.

## License

MIT — see [LICENSE](LICENSE).
