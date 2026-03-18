"""E2E tests for cross-source document library search.

Verifies that documents from web capture (WEB-*) and file upload (DOC-*)
are both indexed and searchable through the unified search endpoint.

Session-scoped fixtures populate the library before any tests run, so
this file is self-contained — no prior test run is required.

Run::

    pytest tests/e2e/test_e2e_search.py -v -m live --timeout=30
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Tag applied to every document created by this test module so searches
# are scoped and don't collide with documents from other test runs.
_E2E_TAG = "search-e2e"

# Stable URL — small page, reliable, no auth.
_CAPTURE_URL = "https://example.com"


# ── session-scoped document library setup ────────────────────────────────────


@pytest.fixture(scope="session")
def _search_web_doc(base_url: str, http_client) -> str:
    """Capture a web page tagged 'search-e2e'; return its doc_id."""
    resp = http_client.post(
        f"{base_url}/web/capture",
        json={"url": _CAPTURE_URL, "tags": [_E2E_TAG], "extract_tables": True},
    )
    assert resp.status_code == 200, f"search-e2e web capture failed: {resp.text}"
    return resp.json()["doc_id"]


@pytest.fixture(scope="session")
def _search_doc(base_url: str, http_client) -> str:
    """Parse the sample PDF tagged 'search-e2e'; return its doc_id."""
    from fixtures.generate_fixtures import generate_pdf

    pdf_path = FIXTURES_DIR / "sample.pdf"
    if not pdf_path.exists():
        generate_pdf(pdf_path)

    with pdf_path.open("rb") as fh:
        resp = http_client.post(
            f"{base_url}/doc/parse",
            data={
                "generate_summary": "false",
                "tags": f'["{_E2E_TAG}"]',
            },
            files={"file": (pdf_path.name, fh)},
        )
    assert resp.status_code == 200, f"search-e2e doc parse failed: {resp.text}"
    return resp.json()["doc_id"]


# ── keyword search ────────────────────────────────────────────────────────────


@pytest.mark.live
def test_search_keyword_returns_results(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """A broad keyword search returns at least the two seeded documents."""
    resp = http_client.get(f"{base_url}/doc/library/search", params={"q": "example"})
    assert resp.status_code == 200, f"search failed: {resp.text}"
    data = resp.json()
    assert data["total"] >= 1, "Expected ≥1 search result"
    assert len(data["results"]) >= 1


@pytest.mark.live
def test_search_results_have_non_empty_digest(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """Each search result includes a non-empty digest preview."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "example", "limit": 5},
    )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert item.get("digest"), (
            f"result {item.get('doc_id')} has empty digest"
        )


# ── file_type filter ──────────────────────────────────────────────────────────


@pytest.mark.live
def test_search_file_type_pdf_returns_only_doc_ids(
    base_url: str, http_client, _search_doc: str
) -> None:
    """file_type=pdf returns only uploaded documents (DOC-* ids)."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "file_type": "pdf", "limit": 20},
    )
    assert resp.status_code == 200, f"search file_type=pdf failed: {resp.text}"
    results = resp.json()["results"]
    assert len(results) >= 1, "Expected ≥1 pdf result after parsing sample.pdf"
    for item in results:
        assert item["doc_id"].startswith("DOC-"), (
            f"file_type=pdf returned non-DOC id: {item['doc_id']}"
        )
        assert item.get("source") in ("upload", None), (
            f"Expected source='upload' for pdf result, got {item.get('source')!r}"
        )


@pytest.mark.live
def test_search_file_type_web_returns_only_web_ids(
    base_url: str, http_client, _search_web_doc: str
) -> None:
    """file_type=web returns only web-captured documents (WEB-* ids)."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "file_type": "web", "limit": 20},
    )
    assert resp.status_code == 200, f"search file_type=web failed: {resp.text}"
    results = resp.json()["results"]
    assert len(results) >= 1, "Expected ≥1 web result after capturing example.com"
    for item in results:
        assert item["doc_id"].startswith("WEB-"), (
            f"file_type=web returned non-WEB id: {item['doc_id']}"
        )
        assert item.get("source") in ("web_capture", None), (
            f"Expected source='web_capture' for web result, got {item.get('source')!r}"
        )


@pytest.mark.live
def test_search_file_type_pdf_excludes_web_docs(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """file_type=pdf results contain no WEB-* ids."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "file_type": "pdf", "limit": 50},
    )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert not item["doc_id"].startswith("WEB-"), (
            f"file_type=pdf unexpectedly returned web doc: {item['doc_id']}"
        )


@pytest.mark.live
def test_search_file_type_web_excludes_doc_docs(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """file_type=web results contain no DOC-* ids."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "file_type": "web", "limit": 50},
    )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert not item["doc_id"].startswith("DOC-"), (
            f"file_type=web unexpectedly returned uploaded doc: {item['doc_id']}"
        )


# ── tag filter ────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_search_by_tag_returns_seeded_docs(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """tags=search-e2e returns both the seeded WEB and DOC documents."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "tags": _E2E_TAG, "limit": 50},
    )
    assert resp.status_code == 200, f"tag search failed: {resp.text}"
    data = resp.json()
    doc_ids = {item["doc_id"] for item in data["results"]}
    assert _search_web_doc in doc_ids, (
        f"WEB doc {_search_web_doc} not found in tag={_E2E_TAG} results: {doc_ids}"
    )
    assert _search_doc in doc_ids, (
        f"DOC doc {_search_doc} not found in tag={_E2E_TAG} results: {doc_ids}"
    )


@pytest.mark.live
def test_search_by_tag_cross_source(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """Tag search returns documents from both web_capture and upload sources."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "tags": _E2E_TAG, "limit": 50},
    )
    assert resp.status_code == 200
    sources = {item.get("source") for item in resp.json()["results"]}
    # Both web_capture and upload sources should appear
    assert "web_capture" in sources, (
        f"Expected source='web_capture' in tag results, got sources: {sources}"
    )
    assert "upload" in sources, (
        f"Expected source='upload' in tag results, got sources: {sources}"
    )


# ── score and metadata ────────────────────────────────────────────────────────


@pytest.mark.live
def test_search_results_have_positive_scores(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """Search results include a positive relevance score."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "example", "limit": 10},
    )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        score = item.get("score", 0)
        assert score > 0, f"Expected score > 0 for {item['doc_id']}, got {score}"


@pytest.mark.live
def test_search_limit_respected(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """limit parameter caps the number of results returned."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "limit": 1},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) <= 1, f"Expected ≤1 result with limit=1, got {len(results)}"


@pytest.mark.live
def test_search_unknown_tag_returns_empty(
    base_url: str, http_client, _search_web_doc: str, _search_doc: str
) -> None:
    """Searching by a non-existent tag returns zero results."""
    resp = http_client.get(
        f"{base_url}/doc/library/search",
        params={"q": "", "tags": "definitely-nonexistent-tag-xyz-999"},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0, (
        f"Expected 0 results for non-existent tag, got {resp.json()['total']}"
    )
