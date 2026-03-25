"""Tests for TASK-022: document library endpoints, rate limiting, input validation."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_doc(docs_dir: Path, doc_id: str = "DOC-001") -> Path:
    """Create a minimal document directory with all tier files."""
    doc_dir = docs_dir / doc_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "digest.md").write_text(f"# {doc_id} digest\n\nShort summary.", encoding="utf-8")
    (doc_dir / "brief.md").write_text(f"# {doc_id} brief\n\nDetailed brief.", encoding="utf-8")
    (doc_dir / "full.md").write_text(f"# {doc_id} full\n\nFull text content.", encoding="utf-8")

    sections_dir = doc_dir / "sections"
    sections_dir.mkdir()
    (sections_dir / "01-abc123-Introduction.md").write_text(
        "# Introduction\n\nHello.", encoding="utf-8"
    )
    (sections_dir / "02-def456-Methods.md").write_text("# Methods\n\nWorld.", encoding="utf-8")

    tables_dir = doc_dir / "tables"
    tables_dir.mkdir()
    (tables_dir / "table-01.md").write_text(
        "# Table 1\n\n| A | B |\n|---|---|\n| 1 | 2 |", encoding="utf-8"
    )

    manifest = {
        "doc_id": doc_id,
        "filename": "test.pdf",
        "file_type": "pdf",
        "paths": {
            "digest": "digest.md",
            "brief": "brief.md",
            "full": "full.md",
            "sections_dir": "sections/",
        },
        "sections": [
            {
                "sid": "abc123",
                "index": 1,
                "title": "Introduction",
                "page_range": "p.1",
                "char_count": 6,
                "type": "text",
                "summary_preview": "Hello.",
                "file": "sections/01-abc123-Introduction.md",
            },
            {
                "sid": "def456",
                "index": 2,
                "title": "Methods",
                "page_range": "p.2",
                "char_count": 6,
                "type": "text",
                "summary_preview": "World.",
                "file": "sections/02-def456-Methods.md",
            },
        ],
        "provenance": {
            "source": "upload",
            "source_url": "test.pdf",
            "created_at": "2026-01-01T00:00:00Z",
            "content_hash": "sha256:abc",
        },
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Write doc-index.json
    index = {
        "version": 2,
        "documents": [
            {
                "id": doc_id,
                "filename": "test.pdf",
                "file_type": "pdf",
                "source": "upload",
                "pages": 2,
                "sections": 2,
                "ocr_pages": 0,
                "tables": 1,
                "digest": "Short summary.",
                "digest_path": f"docs/{doc_id}/digest.md",
                "tags": ["test", "Q3"],
                "created_at": "2026-01-01T00:00:00Z",
                "content_hash": "sha256:abc",
            }
        ],
    }
    (docs_dir / "doc-index.json").write_text(json.dumps(index), encoding="utf-8")
    return doc_dir


# ---------------------------------------------------------------------------
# Library tier endpoints: digest, brief, full
# ---------------------------------------------------------------------------


class TestLibraryDigest:
    def test_digest_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/digest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "digest" in data["content"].lower()

    def test_digest_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/digest")
        assert resp.status_code == 404


class TestLibraryBrief:
    def test_brief_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/brief")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "brief" in data["content"].lower()

    def test_brief_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/brief")
        assert resp.status_code == 404


class TestLibraryFull:
    def test_full_returns_content(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/full")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "full" in data["content"].lower()

    def test_full_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/full")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section & table endpoints
# ---------------------------------------------------------------------------


class TestLibrarySections:
    def test_list_sections(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/sections")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sections"]) == 2
        sids = [s["sid"] for s in data["sections"]]
        assert "abc123" in sids
        assert "def456" in sids

    def test_read_section_by_sid(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/section/abc123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sid"] == "abc123"
        assert "Introduction" in data["content"]

    def test_section_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/section/nonexistent")
        assert resp.status_code == 404

    def test_read_table(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/01")
        assert resp.status_code == 200
        assert "Table 1" in resp.json()["content"]

    def test_table_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/99")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


class TestLibrarySearch:
    def test_search_by_keyword(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["results"][0]["doc_id"] == "DOC-001"

    def test_search_by_tag(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?tags=Q3")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_by_file_type(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?file_type=pdf")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_no_match(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=zzz_no_match")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_search_empty_index(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/search?q=anything")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


class TestLibraryManifest:
    def test_manifest_returns_json(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "DOC-001"
        assert "provenance" in data
        assert "sections" in data

    def test_manifest_not_found(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-999/manifest")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Rate limiting (429)
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_parse_sem_locked_returns_429(self, client: TestClient):
        """When _parse_sem is fully acquired, parse should return 429."""
        import larkscout_docreader

        original_sem = larkscout_docreader._parse_sem
        # Replace with a semaphore of size 0 (always locked)
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        larkscout_docreader._parse_sem = locked_sem
        try:
            resp = client.post(
                "/doc/parse",
                files={"file": ("test.pdf", b"%PDF-1.4 minimal", "application/pdf")},
            )
            assert resp.status_code == 429
        finally:
            larkscout_docreader._parse_sem = original_sem

    def test_capture_sem_locked_returns_429(self, client: TestClient):
        """When _capture_sem is fully acquired, capture should return 429."""
        import larkscout_browser

        original_sem = larkscout_browser._capture_sem
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        larkscout_browser._capture_sem = locked_sem
        try:
            resp = client.post(
                "/web/capture",
                json={"url": "https://example.com"},
            )
            assert resp.status_code == 429
        finally:
            larkscout_browser._capture_sem = original_sem

    def test_session_sem_locked_returns_429(self, client: TestClient):
        """When _session_sem is fully acquired, new session should return 429."""
        import larkscout_browser

        original_sem = larkscout_browser._session_sem
        import asyncio

        locked_sem = asyncio.Semaphore(0)
        larkscout_browser._session_sem = locked_sem
        try:
            resp = client.post("/web/session/new", json={})
            assert resp.status_code == 429
        finally:
            larkscout_browser._session_sem = original_sem


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_parse_unsupported_format(self, client: TestClient):
        resp = client.post(
            "/doc/parse",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )
        assert resp.status_code == 422

    def test_parse_no_file(self, client: TestClient):
        resp = client.post("/doc/parse")
        assert resp.status_code == 422

    def test_doc_id_traversal_blocked(self, client: TestClient):
        resp = client.get("/doc/library/../etc/passwd/digest")
        assert resp.status_code in (400, 404, 422)

    def test_table_id_traversal_blocked(self, client: TestClient):
        with tempfile.TemporaryDirectory() as tmp:
            _setup_doc(Path(tmp))
            with patch("larkscout_docreader._get_docs_dir", return_value=Path(tmp)):
                resp = client.get("/doc/library/DOC-001/table/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    def test_capture_invalid_url_blocked(self, client: TestClient):
        resp = client.post("/web/capture", json={"url": "file:///etc/passwd"})
        assert resp.status_code in (400, 422)

    def test_capture_private_ip_blocked(self, client: TestClient):
        resp = client.post("/web/capture", json={"url": "http://169.254.169.254/latest"})
        assert resp.status_code in (400, 422)
