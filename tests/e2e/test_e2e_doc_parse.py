"""E2E tests for the document parse pipeline.

Flow under test for each file type:
  POST /doc/parse (multipart, generate_summary=false) → doc_id (DOC-xxx)
  GET  /doc/library/{doc_id}/digest                   → non-empty
  GET  /doc/library/{doc_id}/sections                 → ≥1 section
  GET  /doc/library/{doc_id}/section/{first_sid}      → non-empty content

Run without an LLM key (no summary generation)::

    pytest tests/e2e/test_e2e_doc_parse.py -v -m "live and not live_llm" --timeout=60

Run with summary generation (requires GEMINI_API_KEY or equivalent)::

    pytest tests/e2e/test_e2e_doc_parse.py -v -m live_llm --timeout=120
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

# Ensure fixtures package is importable when running from repo root.
sys.path.insert(0, str(Path(__file__).parent))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── session fixture: generate test files once ─────────────────────────────────


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    """Generate fixture files into a temp directory (idempotent per session)."""
    from fixtures.generate_fixtures import generate_all

    dest = FIXTURES_DIR  # keep fixtures next to generator for easy inspection
    dest.mkdir(parents=True, exist_ok=True)
    return generate_all(dest)


# ── helpers ───────────────────────────────────────────────────────────────────


def _parse_and_verify(
    base_url: str,
    http_client,
    file_path: Path,
    *,
    generate_summary: bool = False,
    expected_prefix: str = "DOC-",
) -> str:
    """Upload a file, retrieve its digest/sections/section, return doc_id."""
    # Step 1: parse
    with file_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={"generate_summary": str(generate_summary).lower(), "tags": '["e2e","test"]'},
            files={"file": (file_path.name, fh)},
        )
    assert resp.status_code == 200, (
        f"parse {file_path.name} failed ({resp.status_code}): {resp.text[:300]}"
    )
    parse_data = resp.json()
    doc_id: str = parse_data["doc_id"]
    assert doc_id.startswith(expected_prefix), (
        f"Expected doc_id prefix {expected_prefix!r}, got {doc_id!r}"
    )
    assert parse_data.get("section_count", 0) >= 1, (
        f"Expected ≥1 section for {file_path.name}, got {parse_data.get('section_count')}"
    )

    # Step 2: digest
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/digest")
    assert resp.status_code == 200, f"GET digest failed: {resp.text}"
    assert resp.json().get("content"), f"digest.content empty for {doc_id}"

    # Step 3: sections list
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/sections")
    assert resp.status_code == 200, f"GET sections failed: {resp.text}"
    sections = resp.json().get("sections", [])
    assert len(sections) >= 1, f"Expected ≥1 section for {doc_id}, got {len(sections)}"

    # Step 4: first section content
    first_sid = sections[0]["sid"]
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/section/{first_sid}")
    assert resp.status_code == 200, f"GET section/{first_sid} failed: {resp.text}"
    assert resp.json().get("content"), (
        f"section content empty for {doc_id}/{first_sid}"
    )

    return doc_id


# ── PDF tests ─────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_parse_pdf_no_summary(base_url: str, http_client, fixtures: dict) -> None:
    """Parse a PDF (no LLM summary) — full digest/sections/section flow."""
    _parse_and_verify(base_url, http_client, fixtures["pdf"])


@pytest.mark.live
def test_parse_pdf_section_count_matches(base_url: str, http_client, fixtures: dict) -> None:
    """section_count in parse response matches the sections list length."""
    file_path = fixtures["pdf"]
    with file_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={"generate_summary": "false"},
            files={"file": (file_path.name, fh)},
        )
    assert resp.status_code == 200
    parse_data = resp.json()
    doc_id = parse_data["doc_id"]
    reported = parse_data["section_count"]

    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/sections")
    assert resp.status_code == 200
    actual = len(resp.json().get("sections", []))
    assert actual == reported, (
        f"parse.section_count={reported} but sections list has {actual} entries"
    )


@pytest.mark.live
def test_parse_pdf_metadata_source_and_text_search(
    base_url: str,
    http_client,
    fixtures: dict,
) -> None:
    """Parse with metadata/source retention, then verify manifest and library search."""
    file_path = fixtures["pdf"]
    customer = f"ACME-E2E-{uuid.uuid4().hex[:8]}"
    metadata = {
        "customer": customer,
        "contract_type": "MSA",
        "status": "draft",
    }

    with file_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={
                "generate_summary": "false",
                "metadata": json.dumps(metadata),
                "tags": '["e2e","metadata"]',
            },
            files={"file": (file_path.name, fh)},
        )
    assert resp.status_code == 200, f"parse with metadata failed: {resp.text}"
    parse_data = resp.json()
    doc_id = parse_data["doc_id"]
    source_ref = parse_data.get("source_ref")
    assert doc_id.startswith("DOC-"), f"Expected DOC- doc_id, got {doc_id!r}"
    assert source_ref, "Expected source_ref when source file storage is enabled"

    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/manifest")
    assert resp.status_code == 200, f"GET manifest failed: {resp.text}"
    manifest = resp.json()
    assert manifest.get("metadata", {}).get("customer") == customer
    assert manifest.get("source_file", {}).get("ref") == source_ref
    sections = manifest.get("sections", [])
    assert sections, f"Expected manifest sections for {doc_id}"
    assert sections[0].get("page_start") is not None
    assert sections[0].get("page_end") is not None

    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"metadata.customer": customer},
    )
    assert resp.status_code == 200, f"metadata search failed: {resp.text}"
    search_results = resp.json().get("results", [])
    assert any(item.get("doc_id") == doc_id for item in search_results), (
        f"metadata search did not return {doc_id}: {search_results}"
    )

    resp = http_client.get(
        f"{base_url}/doc/library/search_text",
        params={"q": "provider", "scope": "section", "doc_id": doc_id},
    )
    assert resp.status_code == 200, f"search_text failed: {resp.text}"
    text_results = resp.json().get("results", [])
    assert text_results, f"search_text returned no section results for {doc_id}"
    first = text_results[0]
    assert first.get("sid"), f"search_text result missing sid: {first}"
    assert first.get("snippet"), f"search_text result missing snippet: {first}"
    assert first.get("page_start") is not None, f"search_text missing page_start: {first}"


@pytest.mark.live
@pytest.mark.live_llm
def test_parse_pdf_with_summary(base_url: str, http_client, fixtures: dict) -> None:
    """Parse a PDF with LLM summary generation (requires API key)."""
    doc_id = _parse_and_verify(
        base_url, http_client, fixtures["pdf"], generate_summary=True
    )
    # Digest should be richer than a plain text extract
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/digest")
    assert resp.status_code == 200
    content = resp.json().get("content", "")
    assert len(content) > 50, f"Expected a non-trivial digest, got: {content!r}"


# ── DOCX tests ────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_parse_docx_no_summary(base_url: str, http_client, fixtures: dict) -> None:
    """Parse a DOCX (no LLM summary) — full digest/sections/section flow."""
    _parse_and_verify(base_url, http_client, fixtures["docx"])


@pytest.mark.live
def test_parse_docx_has_table_content(base_url: str, http_client, fixtures: dict) -> None:
    """DOCX parse captures the embedded table."""
    file_path = fixtures["docx"]
    with file_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={"generate_summary": "false", "extract_tables": "true"},
            files={"file": (file_path.name, fh)},
        )
    assert resp.status_code == 200
    parse_data = resp.json()
    assert parse_data.get("table_count", 0) >= 1, (
        f"Expected ≥1 table in DOCX parse, got {parse_data.get('table_count')}"
    )


# ── CSV tests ─────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_parse_csv_no_summary(base_url: str, http_client, fixtures: dict) -> None:
    """Parse a CSV file — full digest/sections/section flow."""
    _parse_and_verify(base_url, http_client, fixtures["csv"])


@pytest.mark.live
def test_parse_csv_single_section(base_url: str, http_client, fixtures: dict) -> None:
    """CSV parses to exactly one section (the whole file is one table)."""
    file_path = fixtures["csv"]
    with file_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={"generate_summary": "false"},
            files={"file": (file_path.name, fh)},
        )
    assert resp.status_code == 200
    parse_data = resp.json()
    doc_id = parse_data["doc_id"]

    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/sections")
    assert resp.status_code == 200
    sections = resp.json().get("sections", [])
    assert len(sections) == 1, f"Expected exactly 1 section for CSV, got {len(sections)}"


# ── XLSX tests ────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_parse_xlsx_no_summary(base_url: str, http_client, fixtures: dict) -> None:
    """Parse an XLSX file — full digest/sections/section flow."""
    _parse_and_verify(base_url, http_client, fixtures["xlsx"])


@pytest.mark.live
def test_parse_xlsx_section_is_markdown_table(
    base_url: str, http_client, fixtures: dict
) -> None:
    """XLSX section content contains a Markdown table."""
    file_path = fixtures["xlsx"]
    with file_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={"generate_summary": "false"},
            files={"file": (file_path.name, fh)},
        )
    assert resp.status_code == 200
    doc_id = resp.json()["doc_id"]

    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/sections")
    assert resp.status_code == 200
    sections = resp.json().get("sections", [])
    assert sections, f"No sections for {doc_id}"

    first_sid = sections[0]["sid"]
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/section/{first_sid}")
    assert resp.status_code == 200
    content: str = resp.json().get("content", "")
    assert "|" in content, (
        f"Expected Markdown table (pipe chars) in XLSX section, got: {content[:200]!r}"
    )


# ── unsupported format ────────────────────────────────────────────────────────


@pytest.mark.live
def test_parse_unsupported_format_returns_422(
    base_url: str, http_client, fixtures: dict
) -> None:
    """Uploading an unsupported file type returns 422."""
    # Create a tiny .txt file in memory
    import io

    fake_txt = io.BytesIO(b"Hello world")
    resp = http_client.post(
        f"{base_url}/doc/parse",
        data={"generate_summary": "false"},
        files={"file": ("test.txt", fake_txt)},
    )
    assert resp.status_code == 422, (
        f"Expected 422 for unsupported format, got {resp.status_code}"
    )
