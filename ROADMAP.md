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

## Phase 4 — End-to-End Validation

> Goal: verify the full data pipeline works on a real local environment.
> Each task produces a runnable test script in `tests/e2e/` that can be re-run anytime.
> **Prerequisite:** `source ~/.venv/bin/activate && python larkscout_server.py &` running on port 9898.

### TASK-010: E2E — Web Capture Pipeline

**Goal:** Verify the full web capture flow: capture a public webpage → persist to doc library → retrieve digest/sections.

**Scope:**

- Create `tests/e2e/conftest.py` with shared fixtures (base_url, httpx client, live marker config)
- Create `tests/e2e/test_e2e_web_capture.py`
- Use `httpx` as the HTTP client (no SDK dependency yet)
- Target a stable public page (e.g., `https://example.com`)
- Flow: `POST /web/capture` → get doc_id → `GET /doc/library/{doc_id}/digest` → `GET /doc/library/{doc_id}/sections` → `GET /doc/library/{doc_id}/section/{first_sid}`
- Assert: doc_id starts with "WEB-", digest is non-empty, at least 1 section exists, section content is non-empty
- Add a `live` pytest marker so it only runs when explicitly requested (not in CI)

**AC:**

```bash
# AC-1: Test file exists
verify: test -f tests/e2e/test_e2e_web_capture.py

# AC-2: Test is collected with correct marker
verify: pytest tests/e2e/test_e2e_web_capture.py --collect-only 2>&1 | grep "test_"

# AC-3: Live test passes (requires running server + network)
verify: pytest tests/e2e/test_e2e_web_capture.py -v -m live --timeout=60

# AC-4: Lint passes
verify: ruff check tests/e2e/
```

**Depends on:** TASK-004

---

### TASK-011: E2E — Document Parse Pipeline

**Goal:** Verify the full document parsing flow: upload a file → persist → retrieve digest/brief/sections/tables.

**Scope:**

- Create `tests/e2e/test_e2e_doc_parse.py`
- Create `tests/e2e/fixtures/` with:
  - A small test PDF (1-2 pages, include a table)
  - A small test DOCX (a few paragraphs)
  - A small test CSV (5-10 rows)
  - A small test XLSX (5-10 rows)
  - Generate these programmatically in a `tests/e2e/fixtures/generate_fixtures.py` script
- Flow for each file type:
  - `POST /doc/parse` (multipart upload, `generate_summary=false`) → get doc_id
  - `GET /doc/library/{doc_id}/digest` → non-empty
  - `GET /doc/library/{doc_id}/sections` → list with ≥1 section
  - `GET /doc/library/{doc_id}/section/{first_sid}` → non-empty content
- Separate test for `generate_summary=true` (requires GEMINI_API_KEY, mark as `live_llm`)
- Use `live` marker for all

**AC:**

```bash
# AC-1: Test file and fixture generator exist
verify: test -f tests/e2e/test_e2e_doc_parse.py && test -f tests/e2e/fixtures/generate_fixtures.py

# AC-2: Fixtures can be generated
verify: python tests/e2e/fixtures/generate_fixtures.py && ls tests/e2e/fixtures/ | grep -E "\.(pdf|docx|csv|xlsx)"

# AC-3: Tests are collected
verify: pytest tests/e2e/test_e2e_doc_parse.py --collect-only 2>&1 | grep "test_"

# AC-4: No-summary parse tests pass (no LLM needed)
verify: pytest tests/e2e/test_e2e_doc_parse.py -v -m "live and not live_llm" --timeout=60

# AC-5: Lint passes
verify: ruff check tests/e2e/
```

**Depends on:** TASK-005

---

### TASK-012: E2E — Cross-Source Search

**Goal:** Verify that documents from both web capture and file upload are searchable through the unified library.

**Scope:**

- Create `tests/e2e/test_e2e_search.py`
- Prerequisite: TASK-010 and TASK-011 tests have run (documents exist in library)
- Flow:
  - `GET /doc/library/search?q=<keyword>` → returns results
  - `GET /doc/library/search?file_type=pdf` → only DOC-\* results
  - `GET /doc/library/search?file_type=web` → only WEB-\* results
  - `GET /doc/library/search?tags=test` → tag filtering works
- Assert: search returns correct source types, scores > 0, digest previews non-empty

**AC:**

```bash
# AC-1: Test file exists
verify: test -f tests/e2e/test_e2e_search.py

# AC-2: Tests are collected
verify: pytest tests/e2e/test_e2e_search.py --collect-only 2>&1 | grep "test_"

# AC-3: Search test passes (requires prior capture/parse to populate library)
verify: pytest tests/e2e/test_e2e_search.py -v -m live --timeout=30

# AC-4: Lint passes
verify: ruff check tests/e2e/
```

**Depends on:** TASK-010, TASK-011

---

### TASK-013: E2E — SDK Round-Trip

**Goal:** Verify the Python SDK works against a live server for the complete workflow.

**Scope:**

- Create `tests/e2e/test_e2e_sdk.py`
- Use `LarkScoutClient` (sync) and `AsyncLarkScoutClient` from `sdk/python/larkscout_client.py`
- Flow (sync client):
  1. `client.capture("https://example.com")` → returns doc_id starting with WEB-
  2. `client.get_digest(doc_id)` → non-empty
  3. `client.parse(open("tests/e2e/fixtures/sample.pdf", "rb"))` → returns doc_id starting with DOC-
  4. `client.search("example")` → returns results
  5. `client.get_section(doc_id, sid)` → non-empty content
- Flow (async client): same operations with `await`
- Assert: all operations complete without error, return types match SDK signatures

**AC:**

```bash
# AC-1: Test file exists
verify: test -f tests/e2e/test_e2e_sdk.py

# AC-2: Tests are collected
verify: pytest tests/e2e/test_e2e_sdk.py --collect-only 2>&1 | grep "test_"

# AC-3: Sync SDK test passes
verify: pytest tests/e2e/test_e2e_sdk.py -v -m live -k "sync" --timeout=60

# AC-4: Async SDK test passes
verify: pytest tests/e2e/test_e2e_sdk.py -v -m live -k "async" --timeout=60

# AC-5: Lint passes
verify: ruff check tests/e2e/
```

**Depends on:** TASK-009, TASK-010, TASK-011

---

### TASK-014: E2E — Full Pipeline Smoke Test

**Goal:** One single test that runs the entire pipeline end-to-end in sequence, verifying data flows correctly between all components.

**Scope:**

- Create `tests/e2e/test_e2e_full_pipeline.py`
- Single test function `test_full_pipeline()` that runs the complete sequence:
  1. Health check: all 3 endpoints respond 200
  2. Web capture: capture a page → get WEB doc_id
  3. Doc parse: upload PDF → get DOC doc_id
  4. Cross search: search returns both WEB and DOC results
  5. Three-tier loading: digest → brief → section for each doc
  6. SDK: repeat steps 2-5 using SDK client
- Print a summary table at the end showing each step's pass/fail status
- This is the "one command to verify everything works" test

**AC:**

```bash
# AC-1: Test file exists
verify: test -f tests/e2e/test_e2e_full_pipeline.py

# AC-2: Single test function exists
verify: pytest tests/e2e/test_e2e_full_pipeline.py --collect-only 2>&1 | grep "test_full_pipeline"

# AC-3: Full pipeline passes
verify: pytest tests/e2e/test_e2e_full_pipeline.py -v -m live --timeout=180 -s

# AC-4: Lint passes
verify: ruff check tests/e2e/
```

**Depends on:** TASK-010, TASK-011, TASK-012, TASK-013

### TASK-015: Default Docs Directory Migration

**Goal:** Replace OpenClaw legacy path with LarkScout's own default, configurable via environment variable.

**Scope:**

- Default docs directory: `~/.larkscout/docs` (was `~/.openclaw/subworkspace/shared/docs`)
- Environment variable `LARKSCOUT_DOCS_DIR` overrides the default
- Update `larkscout_docreader.py` — replace hardcoded/config path
- Update `larkscout_browser.py` — web capture output path should use the same directory
- Update `larkscout_server.py` — pass docs_dir to both sub-apps if needed
- Auto-create directory if it doesn't exist on startup
- Update CLAUDE.md and SKILL files if they reference the old path
- Do NOT migrate existing data — just change the default going forward

**AC:**

```bash
# AC-1: Old path not referenced in any Python file
verify: ! grep -r "openclaw" services/ larkscout_server.py --include="*.py"

# AC-2: Default path is ~/.larkscout/docs
verify: python -c "
import os, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'services/docreader')
# Should not need LARKSCOUT_DOCS_DIR to get a valid default
os.environ.pop('LARKSCOUT_DOCS_DIR', None)
from larkscout_docreader import DOCS_DIR
assert '.larkscout/docs' in str(DOCS_DIR), f'Expected .larkscout/docs, got {DOCS_DIR}'
"

# AC-3: Env var override works
verify: LARKSCOUT_DOCS_DIR=/tmp/test-docs python -c "
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'services/docreader')
from larkscout_docreader import DOCS_DIR
assert str(DOCS_DIR) == '/tmp/test-docs', f'Expected /tmp/test-docs, got {DOCS_DIR}'
"

# AC-4: Health endpoint shows new path
verify: timeout 5 bash -c 'python larkscout_server.py &
  PID=$!; sleep 2;
  RESP=$(curl -s http://127.0.0.1:9898/doc/health);
  kill $PID;
  echo "$RESP" | python -c "import sys,json; d=json.load(sys.stdin)[\"docs_dir\"]; assert \"openclaw\" not in d and \"larkscout\" in d, d"'

# AC-5: Lint passes
verify: ruff check services/ larkscout_server.py
```

**Depends on:** TASK-001

```


---

## Task Dependency Graph

```

Phase 0-3 (completed)
├── TASK-001 → TASK-002 → TASK-003
│ ├── TASK-004 ─── TASK-010 (web capture e2e) ──┐
│ ├── TASK-005 ─── TASK-011 (doc parse e2e) ────┤
│ ├── TASK-006 ├── TASK-014 (full pipeline)
│ ├── TASK-007 → TASK-008 │
│ └── TASK-009 ─── TASK-013 (SDK e2e) ──────────┤
│ TASK-012 (search e2e) ─────────┘

```

---

## Status Tracking

| Task ID  | Title                 | Status      | PR  |
| -------- | --------------------- | ----------- | --- |
| TASK-001 | Unified server entry  | ✅ Done     |     |
| TASK-002 | Project packaging     | ✅ Done     |     |
| TASK-003 | Test framework        | ✅ Done     |     |
| TASK-004 | Web capture endpoint  | ✅ Done     |     |
| TASK-005 | XLSX/CSV support      | ✅ Done     |     |
| TASK-006 | Multi-LLM provider    | ✅ Done     |     |
| TASK-007 | Docker Compose        | ✅ Done     |     |
| TASK-008 | README & Contributing | ✅ Done     |     |
| TASK-009 | Python SDK            | ✅ Done     |     |
| TASK-010 | E2E: Web capture      | Not started |     |
| TASK-011 | E2E: Doc parse        | Not started |     |
| TASK-012 | E2E: Cross search     | Not started |     |
| TASK-013 | E2E: SDK round-trip   | Not started |     |
| TASK-014 | E2E: Full pipeline    | Not started |     |
| TASK-015 | Docs directory migration | Not started |      |
```
