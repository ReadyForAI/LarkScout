"""E2E tests for the Python SDK round-trip.

Verifies that both LarkScoutClient (sync) and AsyncLarkScoutClient (async)
can complete the full capture → digest → parse → search → section workflow
against a live server.

Session-scoped sync fixtures perform the heavy operations once so the
individual test functions are fast assertions.  The async test performs the
full flow in a single coroutine to avoid session-scoped async fixture
complexity.

Run::

    pytest tests/e2e/test_e2e_sdk.py -v -m live --timeout=60
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the SDK importable without installation.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sdk" / "python"))
# Make the fixtures package importable.
sys.path.insert(0, str(Path(__file__).parent))

from larkscout_client import AsyncLarkScoutClient, LarkScoutClient  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Tag applied to every document created by this module.
_SDK_TAG = "sdk-e2e"

# Stable URL used for web capture.
_CAPTURE_URL = "https://example.com"


# ── shared fixture: generate test files ──────────────────────────────────────


@pytest.fixture(scope="session")
def fixtures() -> dict[str, Path]:
    """Generate fixture files (idempotent); return format → path mapping."""
    from fixtures.generate_fixtures import generate_all

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    return generate_all(FIXTURES_DIR)


# ── sync fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def _sync_client(base_url: str) -> LarkScoutClient:
    """Session-scoped sync SDK client; closed after all tests complete."""
    client = LarkScoutClient(base_url, timeout=60.0)
    yield client
    client.close()


@pytest.fixture(scope="session")
def _sync_web_doc(_sync_client: LarkScoutClient) -> str:
    """Capture example.com via the sync SDK; return its doc_id."""
    result = _sync_client.capture(_CAPTURE_URL, tags=[_SDK_TAG])
    assert result["doc_id"].startswith("WEB-"), (
        f"sync capture returned non-WEB id: {result['doc_id']}"
    )
    return result["doc_id"]


@pytest.fixture(scope="session")
def _sync_doc_data(
    _sync_client: LarkScoutClient, fixtures: dict
) -> tuple[str, str]:
    """Parse sample.pdf via the sync SDK; return (doc_id, first_sid)."""
    result = _sync_client.parse(
        fixtures["pdf"], generate_summary=False, tags=[_SDK_TAG]
    )
    doc_id: str = result["doc_id"]
    assert doc_id.startswith("DOC-"), (
        f"sync parse returned non-DOC id: {doc_id}"
    )
    sections = _sync_client.list_sections(doc_id)
    first_sid: str = sections["sections"][0]["sid"]
    return doc_id, first_sid


# ── sync tests ────────────────────────────────────────────────────────────────


@pytest.mark.live
def test_sync_capture_returns_web_doc_id(_sync_web_doc: str) -> None:
    """SDK capture() returns a WEB-* doc_id."""
    assert _sync_web_doc.startswith("WEB-")


@pytest.mark.live
def test_sync_digest_non_empty(
    _sync_client: LarkScoutClient, _sync_web_doc: str
) -> None:
    """get_digest() returns non-empty content for the captured web page."""
    result = _sync_client.get_digest(_sync_web_doc)
    assert result.get("content"), f"sync digest empty for {_sync_web_doc}"


@pytest.mark.live
def test_sync_parse_returns_doc_id(_sync_doc_data: tuple) -> None:
    """SDK parse() returns a DOC-* doc_id."""
    doc_id, _ = _sync_doc_data
    assert doc_id.startswith("DOC-")


@pytest.mark.live
def test_sync_search_returns_results(
    _sync_client: LarkScoutClient, _sync_web_doc: str
) -> None:
    """search() returns at least one result after seeding the library."""
    result = _sync_client.search("example")
    assert result["total"] >= 1, (
        f"Expected ≥1 search result, got total={result['total']}"
    )
    assert len(result["results"]) >= 1


@pytest.mark.live
def test_sync_section_content_non_empty(
    _sync_client: LarkScoutClient, _sync_doc_data: tuple
) -> None:
    """get_section() returns non-empty content for the first parsed section."""
    doc_id, sid = _sync_doc_data
    result = _sync_client.get_section(doc_id, sid)
    assert result.get("content"), (
        f"sync section content empty for {doc_id}/{sid}"
    )


# ── async tests ────────────────────────────────────────────────────────────────


@pytest.mark.live
@pytest.mark.asyncio
async def test_async_sdk_round_trip(base_url: str, fixtures: dict) -> None:
    """Full async SDK round-trip: capture → digest → parse → search → section."""
    async with AsyncLarkScoutClient(base_url, timeout=60.0) as client:
        # 1. Capture a web page.
        result = await client.capture(_CAPTURE_URL, tags=[_SDK_TAG])
        web_doc_id: str = result["doc_id"]
        assert web_doc_id.startswith("WEB-"), (
            f"async capture returned non-WEB id: {web_doc_id}"
        )

        # 2. Retrieve digest.
        digest = await client.get_digest(web_doc_id)
        assert digest.get("content"), f"async digest empty for {web_doc_id}"

        # 3. Parse a document.
        result = await client.parse(
            fixtures["pdf"], generate_summary=False, tags=[_SDK_TAG]
        )
        doc_id: str = result["doc_id"]
        assert doc_id.startswith("DOC-"), (
            f"async parse returned non-DOC id: {doc_id}"
        )

        # 4. Search the library.
        search_result = await client.search("example")
        assert search_result["total"] >= 1, (
            f"async search returned total={search_result['total']}, expected ≥1"
        )

        # 5. List sections and retrieve first section content.
        sections = await client.list_sections(doc_id)
        assert sections.get("sections"), f"async list_sections empty for {doc_id}"
        first_sid: str = sections["sections"][0]["sid"]
        section = await client.get_section(doc_id, first_sid)
        assert section.get("content"), (
            f"async section content empty for {doc_id}/{first_sid}"
        )
