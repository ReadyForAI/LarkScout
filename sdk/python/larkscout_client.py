"""LarkScout Python SDK.

Lightweight sync and async clients for the LarkScout API.

Basic usage (sync)::

    from larkscout_client import LarkScoutClient

    client = LarkScoutClient("http://localhost:9898")
    result = client.capture("https://example.com")
    print(result["doc_id"])

Basic usage (async)::

    from larkscout_client import AsyncLarkScoutClient

    async with AsyncLarkScoutClient("http://localhost:9898") as client:
        result = await client.capture("https://example.com")
        print(result["doc_id"])
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

_DEFAULT_BASE_URL = "http://localhost:9898"
_DEFAULT_TIMEOUT = 120.0  # seconds; large uploads / OCR can be slow


# ── helpers ───────────────────────────────────────────────────────────────────


def _base_url(url: str) -> str:
    return url.rstrip("/")


def _tags_param(tags: list[str] | None) -> str | None:
    """Encode a tag list as a JSON-array string for form fields."""
    import json

    return json.dumps(tags) if tags else None


# ── sync client ───────────────────────────────────────────────────────────────


class LarkScoutClient:
    """Synchronous LarkScout API client.

    Args:
        base_url: Base URL of the LarkScout service (default: http://localhost:9898).
        timeout:  HTTP timeout in seconds (default: 120).
        api_key:  Optional API key passed as ``Authorization: Bearer <key>`` header.
                  Also read from the ``LARKSCOUT_API_KEY`` environment variable.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        api_key: str | None = None,
    ) -> None:
        import httpx

        self._base = _base_url(base_url)
        key = api_key or os.environ.get("LARKSCOUT_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._http = httpx.Client(timeout=timeout, headers=headers)

    # ── context-manager support ──────────────────────────────────────────────

    def __enter__(self) -> LarkScoutClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    # ── internal ─────────────────────────────────────────────────────────────

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}{path}", params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(f"{self._base}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _post_multipart(self, path: str, data: dict[str, Any], file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as fh:
            resp = self._http.post(
                f"{self._base}{path}",
                data={k: v for k, v in data.items() if v is not None},
                files={"file": (file_path.name, fh)},
            )
        resp.raise_for_status()
        return resp.json()

    # ── public API ────────────────────────────────────────────────────────────

    def capture(
        self,
        url: str,
        *,
        tags: list[str] | None = None,
        extract_tables: bool = True,
    ) -> dict[str, Any]:
        """Capture a web page and persist it to the document library.

        Args:
            url:            The URL to capture.
            tags:           Optional list of tags to attach to the document.
            extract_tables: Whether to extract HTML tables (default: True).

        Returns:
            dict with ``doc_id``, ``digest``, ``section_count``, ``table_count``.
        """
        return self._post_json(
            "/web/capture",
            {"url": url, "tags": tags or [], "extract_tables": extract_tables},
        )

    def parse(
        self,
        file_path: str | Path,
        *,
        generate_summary: bool = True,
        extract_tables: bool = True,
        tags: list[str] | None = None,
        force_ocr: bool = False,
    ) -> dict[str, Any]:
        """Upload and parse a document (PDF, DOCX, XLSX, or CSV).

        Args:
            file_path:        Local path to the document.
            generate_summary: Generate LLM summaries (default: True).
            extract_tables:   Extract tables as Markdown (default: True).
            tags:             Optional list of tags.
            force_ocr:        Force OCR on all pages (default: False).

        Returns:
            dict with ``doc_id``, ``digest``, ``section_count``, ``table_count``, etc.
        """
        path = Path(file_path)
        return self._post_multipart(
            "/doc/parse",
            {
                "generate_summary": str(generate_summary).lower(),
                "extract_tables": str(extract_tables).lower(),
                "force_ocr": str(force_ocr).lower(),
                "tags": _tags_param(tags),
            },
            path,
        )

    def search(
        self,
        query: str,
        *,
        tags: str | None = None,
        file_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the document library.

        Args:
            query:     Full-text keyword query (searches filename, digest, tags).
            tags:      Comma-separated tag filter, e.g. ``"Q3,financial"``.
            file_type: Filter by file type: ``"pdf"``, ``"docx"``, or ``"web"``.
            limit:     Maximum number of results (default: 20).

        Returns:
            dict with ``results`` list and ``total`` count.
        """
        return self._get(
            "/doc/library/search",
            q=query,
            tags=tags,
            file_type=file_type,
            limit=limit,
        )

    def get_digest(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the digest (~200 tokens) for a document.

        Args:
            doc_id: Document ID, e.g. ``"DOC-001"`` or ``"WEB-001"``.

        Returns:
            dict with ``doc_id`` and ``content`` (Markdown string).
        """
        return self._get(f"/doc/library/{doc_id}/digest")

    def get_section(self, doc_id: str, sid: str) -> dict[str, Any]:
        """Retrieve the full text of a specific document section.

        Args:
            doc_id: Document ID.
            sid:    Section ID obtained from ``GET /doc/library/{doc_id}/sections``.

        Returns:
            dict with ``doc_id``, ``sid``, and ``content`` (Markdown string).
        """
        return self._get(f"/doc/library/{doc_id}/section/{sid}")

    def get_brief(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the brief (~1500 tokens) for a document.

        Args:
            doc_id: Document ID.

        Returns:
            dict with ``doc_id`` and ``content`` (Markdown string).
        """
        return self._get(f"/doc/library/{doc_id}/brief")

    def list_sections(self, doc_id: str) -> dict[str, Any]:
        """List all sections of a document with their sids and metadata.

        Args:
            doc_id: Document ID.

        Returns:
            dict with ``doc_id`` and ``sections`` list.
        """
        return self._get(f"/doc/library/{doc_id}/sections")

    def health(self) -> dict[str, Any]:
        """Return the service health status.

        Returns:
            dict with ``ok``, ``version``, and sub-app statuses.
        """
        return self._get("/health")


# ── async client ──────────────────────────────────────────────────────────────


class AsyncLarkScoutClient:
    """Asynchronous LarkScout API client.

    Identical API surface to :class:`LarkScoutClient` but all methods are
    coroutines.  Use as an async context manager or call :meth:`aclose`
    explicitly when done.

    Args:
        base_url: Base URL of the LarkScout service (default: http://localhost:9898).
        timeout:  HTTP timeout in seconds (default: 120).
        api_key:  Optional API key.  Also read from ``LARKSCOUT_API_KEY``.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        api_key: str | None = None,
    ) -> None:
        import httpx

        self._base = _base_url(base_url)
        key = api_key or os.environ.get("LARKSCOUT_API_KEY", "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._http = httpx.AsyncClient(timeout=timeout, headers=headers)

    # ── context-manager support ──────────────────────────────────────────────

    async def __aenter__(self) -> AsyncLarkScoutClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying async HTTP connection pool."""
        await self._http.aclose()

    # ── internal ─────────────────────────────────────────────────────────────

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self._http.get(
            f"{self._base}{path}",
            params={k: v for k, v in params.items() if v is not None},
        )
        resp.raise_for_status()
        return resp.json()

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(f"{self._base}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    async def _post_multipart(self, path: str, data: dict[str, Any], file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as fh:
            resp = await self._http.post(
                f"{self._base}{path}",
                data={k: v for k, v in data.items() if v is not None},
                files={"file": (file_path.name, fh)},
            )
        resp.raise_for_status()
        return resp.json()

    # ── public API ────────────────────────────────────────────────────────────

    async def capture(
        self,
        url: str,
        *,
        tags: list[str] | None = None,
        extract_tables: bool = True,
    ) -> dict[str, Any]:
        """Capture a web page and persist it to the document library."""
        return await self._post_json(
            "/web/capture",
            {"url": url, "tags": tags or [], "extract_tables": extract_tables},
        )

    async def parse(
        self,
        file_path: str | Path,
        *,
        generate_summary: bool = True,
        extract_tables: bool = True,
        tags: list[str] | None = None,
        force_ocr: bool = False,
    ) -> dict[str, Any]:
        """Upload and parse a document (PDF, DOCX, XLSX, or CSV)."""
        path = Path(file_path)
        return await self._post_multipart(
            "/doc/parse",
            {
                "generate_summary": str(generate_summary).lower(),
                "extract_tables": str(extract_tables).lower(),
                "force_ocr": str(force_ocr).lower(),
                "tags": _tags_param(tags),
            },
            path,
        )

    async def search(
        self,
        query: str,
        *,
        tags: str | None = None,
        file_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the document library."""
        return await self._get(
            "/doc/library/search",
            q=query,
            tags=tags,
            file_type=file_type,
            limit=limit,
        )

    async def get_digest(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the digest (~200 tokens) for a document."""
        return await self._get(f"/doc/library/{doc_id}/digest")

    async def get_section(self, doc_id: str, sid: str) -> dict[str, Any]:
        """Retrieve the full text of a specific document section."""
        return await self._get(f"/doc/library/{doc_id}/section/{sid}")

    async def get_brief(self, doc_id: str) -> dict[str, Any]:
        """Retrieve the brief (~1500 tokens) for a document."""
        return await self._get(f"/doc/library/{doc_id}/brief")

    async def list_sections(self, doc_id: str) -> dict[str, Any]:
        """List all sections of a document with their sids and metadata."""
        return await self._get(f"/doc/library/{doc_id}/sections")

    async def health(self) -> dict[str, Any]:
        """Return the service health status."""
        return await self._get("/health")
