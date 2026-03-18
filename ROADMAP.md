# LarkScout Development Roadmap

> Each task is designed for a single `/implement` session in Claude Code.
> Claude Code should read this file to understand task scope and verify completion against the AC (Acceptance Criteria).

---

## How to Use

```
# In Claude Code, reference a task by its ID:
/implement TASK-001: Unified server entry point

# Claude Code will:
# 1. Read this file to find TASK-001's scope and AC
# 2. Read CLAUDE.md for project context
# 3. Read relevant SKILL files for API contracts
# 4. Implement, then self-verify against each AC item
```

**AC format:** Each `verify:` line is a command Claude Code should run. ✅ = expected pass, the command should exit 0 or produce the described output.

---

## Phase 0 — Project Skeleton

### TASK-001: Unified Server Entry Point

**Goal:** Create `larkscout_server.py` that mounts browser and docreader as sub-applications on a single FastAPI instance.

**Scope:**
- Create `larkscout_server.py` at repo root
- Import and mount `services/browser/larkscout_browser.py` under `/web`
- Import and mount `services/docreader/larkscout_docreader.py` under `/doc`
- Add a root `GET /health` that returns service version and sub-app status
- Default port: 9898

**AC:**
```bash
# AC-1: File exists and is valid Python
verify: python -c "import ast; ast.parse(open('larkscout_server.py').read())"

# AC-2: Server starts without error (start and kill after 3s)
verify: timeout 3 python larkscout_server.py || [ $? -eq 124 ]

# AC-3: Root health endpoint responds
verify: timeout 5 bash -c 'python larkscout_server.py &
  PID=$!; sleep 2;
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9898/health);
  kill $PID;
  [ "$STATUS" = "200" ]'

# AC-4: /web and /doc sub-paths are mounted
verify: timeout 5 bash -c 'python larkscout_server.py &
  PID=$!; sleep 2;
  S1=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9898/web/health);
  S2=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9898/doc/health);
  kill $PID;
  [ "$S1" != "404" ] && [ "$S2" != "404" ]'

# AC-5: Lint passes
verify: ruff check larkscout_server.py
```

**Depends on:** None (first task)

---

### TASK-002: Project Packaging

**Goal:** Add `requirements.txt` and `pyproject.toml` so the project is installable and dependencies are pinned.

**Scope:**
- Create `requirements.txt` with all current dependencies (fastapi, uvicorn, playwright, pymupdf, python-docx, httpx, etc.)
- Create `pyproject.toml` with project metadata, Python ≥3.11 requirement, ruff config, and pytest config
- Ensure `pip install -r requirements.txt` succeeds

**AC:**
```bash
# AC-1: requirements.txt exists and contains fastapi
verify: grep -qi "fastapi" requirements.txt

# AC-2: pyproject.toml is valid
verify: python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"

# AC-3: pyproject.toml has ruff config
verify: python -c "
import tomllib
cfg = tomllib.load(open('pyproject.toml','rb'))
assert 'ruff' in str(cfg.get('tool',{})), 'ruff config missing'
"

# AC-4: pip install succeeds in dry-run
verify: pip install -r requirements.txt --dry-run 2>&1 | head -5

# AC-5: Lint passes on all Python files
verify: ruff check .
```

**Depends on:** TASK-001

---

### TASK-003: Test Framework

**Goal:** Set up pytest with baseline tests for health endpoints.

**Scope:**
- Create `tests/conftest.py` with a shared FastAPI TestClient fixture
- Create `tests/test_health.py` — test `GET /health`, `GET /web/health`, `GET /doc/health`
- All tests should pass without starting external services (Playwright, Gemini)

**AC:**
```bash
# AC-1: Test files exist
verify: test -f tests/conftest.py && test -f tests/test_health.py

# AC-2: Tests are discovered
verify: pytest tests/ --collect-only 2>&1 | grep -c "test_" | xargs test 3 -le

# AC-3: Tests pass
verify: pytest tests/test_health.py -v

# AC-4: Lint passes
verify: ruff check tests/
```

**Depends on:** TASK-001, TASK-002

---

## Phase 1 — Core Features

### TASK-004: One-Shot Web Capture Endpoint

**Goal:** Add `POST /web/capture` that takes a URL, opens a browser session, distills content, persists to document library, and returns a doc_id — all in one call.

**Scope:**
- New endpoint `POST /web/capture` in browser service
- Request body: `{"url": "...", "tags": [...], "extract_tables": true}`
- Internally: session/new → goto → distill → persist to `docs/WEB-xxx/` → session/close
- Response: `{"doc_id": "WEB-001", "digest": "...", "section_count": N, "table_count": N}`
- Write to shared `doc-index.json` (v2 format, `source: "web_capture"`)
- Reference: `skills/larkscout-browser-SKILL.md` §6.9

**AC:**
```bash
# AC-1: Endpoint exists and returns 422 on empty body (not 404)
verify: timeout 5 bash -c 'python larkscout_server.py &
  PID=$!; sleep 2;
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:9898/web/capture -H "Content-Type: application/json" -d "{}");
  kill $PID;
  [ "$STATUS" = "422" ]'

# AC-2: Unit test exists and passes
verify: pytest tests/test_web_capture.py -v

# AC-3: Lint passes
verify: ruff check services/browser/

# AC-4: Type hints on all public functions in the new code
verify: python -c "
import ast, sys
tree = ast.parse(open('services/browser/larkscout_browser.py').read())
funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith('_')]
missing = [f.name for f in funcs if f.returns is None]
if missing: print(f'Missing return type hints: {missing[:5]}'); sys.exit(1)
"
```

**Depends on:** TASK-003

---

### TASK-005: XLSX/CSV File Support for DocReader

**Goal:** Extend `POST /doc/parse` to accept `.xlsx` and `.csv` files.

**Scope:**
- Add openpyxl (read_only mode) for XLSX parsing
- Add csv stdlib for CSV parsing
- Each sheet (XLSX) or file (CSV) becomes a section
- Tables extracted as Markdown
- Update `/doc/health` to include `xlsx` and `csv` in `supported_formats`
- Reference: `skills/larkscout-docreader-SKILL.md` §4.2

**AC:**
```bash
# AC-1: Health endpoint lists new formats
verify: timeout 5 bash -c 'python larkscout_server.py &
  PID=$!; sleep 2;
  RESP=$(curl -s http://127.0.0.1:9898/doc/health);
  kill $PID;
  echo "$RESP" | python -c "import sys,json; f=json.load(sys.stdin)[\"supported_formats\"]; assert \"xlsx\" in f and \"csv\" in f"'

# AC-2: openpyxl in requirements.txt
verify: grep -qi "openpyxl" requirements.txt

# AC-3: Unit test with a sample XLSX file
verify: pytest tests/test_xlsx_parse.py -v

# AC-4: Unit test with a sample CSV file
verify: pytest tests/test_csv_parse.py -v

# AC-5: Lint passes
verify: ruff check services/docreader/
```

**Depends on:** TASK-003

---

### TASK-006: Multi-LLM Provider Abstraction

**Goal:** Replace direct Gemini API calls with a provider abstraction layer so users can configure different LLM backends.

**Scope:**
- Create `larkscout/providers/base.py` — abstract `LLMProvider` class with `summarize()` and `ocr()` methods
- Create `larkscout/providers/gemini.py` — current Gemini implementation extracted
- Create `larkscout/providers/openai_compat.py` — OpenAI-compatible provider (works with OpenAI, DeepSeek, local Ollama, etc.)
- Config via environment variables: `LARKSCOUT_LLM_PROVIDER=gemini|openai_compat`, `LARKSCOUT_LLM_API_KEY`, `LARKSCOUT_LLM_BASE_URL`
- DocReader uses provider through the abstraction, not direct Gemini calls
- Default: Gemini (backward compatible)

**AC:**
```bash
# AC-1: Provider files exist
verify: test -f providers/base.py && test -f providers/gemini.py && test -f providers/openai_compat.py

# AC-2: Base class has required abstract methods
verify: python -c "
from providers.base import LLMProvider
import inspect
methods = [m for m in dir(LLMProvider) if not m.startswith('_')]
assert 'summarize' in methods and 'ocr' in methods, f'Missing methods: {methods}'
"

# AC-3: Gemini provider is used by default (no env change = backward compatible)
verify: python -c "
import os; os.environ.pop('LARKSCOUT_LLM_PROVIDER', None)
from providers import get_provider
p = get_provider()
assert 'gemini' in type(p).__name__.lower(), f'Default provider should be Gemini, got {type(p).__name__}'
"

# AC-4: Unit tests for provider selection
verify: pytest tests/test_providers.py -v

# AC-5: Lint passes
verify: ruff check providers/
```

**Depends on:** TASK-003

---

## Phase 2 — Distribution & Integration

### TASK-007: Docker Compose Setup

**Goal:** Provide a one-command way to run LarkScout with all dependencies.

**Scope:**
- Create `Dockerfile` — Python 3.11 slim, install deps, install Playwright browsers, expose 9898
- Create `docker-compose.yml` — single service, volume mount for `docs/` persistence
- Create `.dockerignore`
- Document in README

**AC:**
```bash
# AC-1: Dockerfile exists and is valid
verify: test -f Dockerfile && head -1 Dockerfile | grep -qi "FROM"

# AC-2: docker-compose.yml exists and is valid YAML
verify: python -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"

# AC-3: .dockerignore exists
verify: test -f .dockerignore

# AC-4: Port 9898 is exposed
verify: grep "9898" docker-compose.yml

# AC-5: docs/ volume is mounted for persistence
verify: grep -i "volume" docker-compose.yml | grep -qi "docs"
```

**Depends on:** TASK-002

---

### TASK-008: README and Contributing Guide

**Goal:** Create user-facing documentation for the open-source project.

**Scope:**
- Create `README.md` — project overview, features, quick start (pip + Docker), configuration, API overview, contributing link
- Create `CONTRIBUTING.md` — dev setup, code conventions (reference CLAUDE.md), PR process, issue templates
- Create `LICENSE` — MIT
- Add badges: CI status, license, Python version

**AC:**
```bash
# AC-1: Files exist
verify: test -f README.md && test -f CONTRIBUTING.md && test -f LICENSE

# AC-2: README has essential sections
verify: python -c "
content = open('README.md').read()
for section in ['Quick Start', 'Docker', 'API', 'Contributing', 'License']:
    assert section.lower() in content.lower(), f'README missing section: {section}'
"

# AC-3: LICENSE is MIT
verify: head -5 LICENSE | grep -qi "MIT"

# AC-4: CI badge in README points to correct repo
verify: grep "ReadyForAI/LarkScout" README.md

# AC-5: No Chinese text in README (English-first for open source)
verify: python -c "
import re
content = open('README.md').read()
cn = re.findall(r'[\u4e00-\u9fff]', content)
assert len(cn) == 0, f'Found {len(cn)} Chinese characters in README'
"
```

**Depends on:** TASK-007

---

## Phase 3 — SDK & Ecosystem

### TASK-009: Python SDK

**Goal:** Provide a lightweight Python client SDK for LarkScout API.

**Scope:**
- Create `sdk/python/larkscout_client.py` — sync + async client classes
- Methods: `capture(url)`, `parse(file)`, `search(query)`, `get_digest(doc_id)`, `get_section(doc_id, sid)`
- Publish-ready: `sdk/python/pyproject.toml` with package metadata
- Usage examples in `sdk/python/examples/`

**AC:**
```bash
# AC-1: SDK files exist
verify: test -f sdk/python/larkscout_client.py && test -f sdk/python/pyproject.toml

# AC-2: Client class is importable
verify: cd sdk/python && python -c "from larkscout_client import LarkScoutClient"

# AC-3: Has both sync and async interfaces
verify: python -c "
import ast
tree = ast.parse(open('sdk/python/larkscout_client.py').read())
classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
assert 'LarkScoutClient' in classes, 'Missing LarkScoutClient'
assert 'AsyncLarkScoutClient' in classes, 'Missing AsyncLarkScoutClient'
"

# AC-4: Examples exist
verify: ls sdk/python/examples/*.py | wc -l | xargs test 1 -le

# AC-5: Lint passes
verify: ruff check sdk/python/
```

**Depends on:** TASK-004, TASK-005

---

## Task Dependency Graph

```
TASK-001 (server entry)
├── TASK-002 (packaging)
│   └── TASK-007 (Docker)
│       └── TASK-008 (README)
└── TASK-003 (test framework)
    ├── TASK-004 (web capture) ──┐
    ├── TASK-005 (XLSX/CSV)  ───┼── TASK-009 (SDK)
    └── TASK-006 (multi-LLM)   ┘
```

---

## Status Tracking

| Task ID  | Title                  | Status      | PR   |
| -------- | ---------------------- | ----------- | ---- |
| TASK-001 | Unified server entry   | Not started |      |
| TASK-002 | Project packaging      | Not started |      |
| TASK-003 | Test framework         | Not started |      |
| TASK-004 | Web capture endpoint   | Not started |      |
| TASK-005 | XLSX/CSV support       | Not started |      |
| TASK-006 | Multi-LLM provider    | Not started |      |
| TASK-007 | Docker Compose         | Not started |      |
| TASK-008 | README & Contributing  | Not started |      |
| TASK-009 | Python SDK             | Not started |      |
