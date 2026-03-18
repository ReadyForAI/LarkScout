# LarkScout

Open-source data collection and document parsing platform by ReadyForAI.
MIT License. GitHub: ReadyForAI/LarkScout. Default port: 9898.

## Architecture

Single-process FastAPI application with two mounted sub-apps:

- `/web/*` — Browser service (Playwright-based web scraping, semantic distillation, WebMCP)
- `/doc/*` — Document reader (PDF/DOCX parsing, OCR, three-tier summaries)

Unified entry point: `larkscout_server.py` (planned)

Key source files:
- `larkscout_browser.py` — Web browser service (~2100 lines)
- `larkscout_docreader.py` — Document parsing service (~1300 lines)
- `i18n.py` — Internationalization (zh/en, default en, LANG env switch)

## Tech Stack

- Python 3.11+
- FastAPI + uvicorn
- Playwright (browser automation)
- PyMuPDF / python-docx (document parsing)
- Gemini API (OCR + summarization)

## Code Conventions

- All code, comments, logs, error messages in English (or i18n calls)
- Type hints required on all public functions
- Docstrings in English for all public APIs
- Use `ruff` for linting and formatting
- Commit messages: `{type}({scope}): {description}` in English
  - Types: feat, fix, refactor, docs, ci, test, chore

## Project Structure

```
larkscout/
├── larkscout_server.py          # Unified entry (mounts /web and /doc)
├── larkscout_browser.py         # Browser service
├── larkscout_docreader.py       # Document reader service
├── i18n.py                      # Internationalization
├── requirements.txt
├── tests/
│   ├── test_browser.py
│   └── test_docreader.py
├── docs/                        # Design documents
│   └── larkscout_opensource_design.md
└── skills/                      # Agent SKILL files
    ├── larkscout-browser-SKILL.md
    └── larkscout-docreader-SKILL.md
```

## Commands

```bash
# Run the service
python larkscout_server.py                    # default port 9898

# Lint
ruff check .

# Format
ruff format .

# Test
pytest tests/ -v

# Type check (optional)
pyright larkscout_*.py
```

## Key Design Decisions

- doc-index v2 format: shared unified index between /web (web captures) and /doc (uploaded documents)
- Three-tier loading: digest (~200 tokens) → brief (~1500 tokens) → section (on-demand) → full (almost never)
- Table extraction: automatic HTML <table> → Markdown with numeric column statistics
- WebMCP support: Chrome 146+ structured tool discovery and invocation
- Provenance tracking: every document/capture has manifest.json with source, timestamp, content_hash

## Before Making Changes

1. Read relevant SKILL files in `skills/` to understand API contracts
2. Check `docs/larkscout_opensource_design.md` for feature boundaries (open-source vs commercial)
3. Run `ruff check .` and `pytest tests/ -v` before committing
