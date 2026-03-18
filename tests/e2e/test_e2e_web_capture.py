"""E2E tests for the web capture pipeline.

Flow under test:
  POST /web/capture → doc_id (WEB-xxx)
  GET  /doc/library/{doc_id}/digest
  GET  /doc/library/{doc_id}/sections
  GET  /doc/library/{doc_id}/section/{first_sid}

Run with::

    pytest tests/e2e/test_e2e_web_capture.py -v -m live --timeout=60
"""

import pytest

# A stable, lightweight public page with deterministic content.
CAPTURE_URL = "https://example.com"


@pytest.mark.live
def test_web_capture_returns_web_doc_id(base_url: str, http_client) -> None:
    """POST /web/capture returns a WEB-prefixed doc_id."""
    resp = http_client.post(
        f"{base_url}/web/capture",
        json={"url": CAPTURE_URL, "tags": ["e2e", "test"], "extract_tables": True},
    )
    assert resp.status_code == 200, f"capture failed ({resp.status_code}): {resp.text}"
    data = resp.json()
    assert data["doc_id"].startswith("WEB-"), (
        f"Expected doc_id to start with 'WEB-', got: {data['doc_id']}"
    )


@pytest.mark.live
def test_web_capture_full_pipeline(base_url: str, http_client) -> None:
    """Full flow: capture → digest → sections list → section content."""
    # ── Step 1: capture ───────────────────────────────────────────────────────
    resp = http_client.post(
        f"{base_url}/web/capture",
        json={"url": CAPTURE_URL, "tags": ["e2e", "pipeline"], "extract_tables": True},
    )
    assert resp.status_code == 200, f"capture failed: {resp.text}"
    capture = resp.json()

    doc_id: str = capture["doc_id"]
    assert doc_id.startswith("WEB-"), f"Expected WEB- prefix, got {doc_id!r}"
    assert capture.get("digest"), "capture.digest should be non-empty"
    assert capture.get("section_count", 0) >= 1, "Expected at least 1 section"

    # ── Step 2: digest ────────────────────────────────────────────────────────
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/digest")
    assert resp.status_code == 200, f"GET digest failed: {resp.text}"
    digest = resp.json()
    assert digest.get("content"), f"digest.content should be non-empty for {doc_id}"

    # ── Step 3: sections list ─────────────────────────────────────────────────
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/sections")
    assert resp.status_code == 200, f"GET sections failed: {resp.text}"
    sections_resp = resp.json()
    sections = sections_resp.get("sections", [])
    assert len(sections) >= 1, f"Expected ≥1 section for {doc_id}, got {len(sections)}"

    # ── Step 4: first section content ────────────────────────────────────────
    first_sid: str = sections[0]["sid"]
    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/section/{first_sid}")
    assert resp.status_code == 200, f"GET section/{first_sid} failed: {resp.text}"
    section = resp.json()
    assert section.get("content"), (
        f"section content should be non-empty for {doc_id}/{first_sid}"
    )


@pytest.mark.live
def test_web_capture_digest_contains_doc_id(base_url: str, http_client) -> None:
    """The digest content should reference the doc_id."""
    resp = http_client.post(
        f"{base_url}/web/capture",
        json={"url": CAPTURE_URL, "tags": ["e2e"]},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["doc_id"]

    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/digest")
    assert resp.status_code == 200
    content: str = resp.json().get("content", "")
    assert doc_id in content, (
        f"Expected digest content to contain doc_id {doc_id!r}"
    )


@pytest.mark.live
def test_web_capture_section_count_matches_sections_list(
    base_url: str, http_client
) -> None:
    """section_count in capture response matches the length of the sections list."""
    resp = http_client.post(
        f"{base_url}/web/capture",
        json={"url": CAPTURE_URL, "tags": ["e2e"]},
    )
    assert resp.status_code == 200
    capture = resp.json()
    doc_id = capture["doc_id"]
    reported_count: int = capture["section_count"]

    resp = http_client.get(f"{base_url}/doc/library/{doc_id}/sections")
    assert resp.status_code == 200
    actual_sections = resp.json().get("sections", [])
    assert len(actual_sections) == reported_count, (
        f"capture.section_count={reported_count} but "
        f"sections list has {len(actual_sections)} entries"
    )
