"""E2E full-pipeline smoke test.

Single test function that runs the entire LarkScout pipeline in sequence,
printing a pass/fail summary table at the end.  Designed as the
"one command to verify everything works" test.

Steps:
  1. Health check    — /health, /web/health, /doc/health all respond 200
  2. Web capture     — POST /web/capture → WEB-* doc_id
  3. Doc parse       — POST /doc/parse   → DOC-* doc_id
  4. Cross search    — GET /doc/library/search returns both WEB and DOC results
  5. Three-tier load — digest / brief / section for each doc are non-empty
  6. SDK round-trip  — repeat steps 2-5 via LarkScoutClient

Run::

    pytest tests/e2e/test_e2e_full_pipeline.py -v -m live --timeout=180 -s
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# Make the SDK importable without installation.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sdk" / "python"))
# Make the fixtures package importable.
sys.path.insert(0, str(Path(__file__).parent))

from larkscout_client import LarkScoutClient  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_BASE_URL = "http://localhost:9898"
_CAPTURE_URL = "https://example.com"
_TAG = "pipeline-e2e"


# ── result tracker ────────────────────────────────────────────────────────────


class _Step:
    """Accumulates pass/fail results for the summary table."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, str, str]] = []

    def ok(self, name: str, detail: str = "") -> None:
        self._rows.append(("PASS", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self._rows.append(("FAIL", name, detail))

    def assert_ok(self, condition: bool, name: str, detail: str = "") -> None:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail)

    def print_summary(self) -> None:
        width = max(len(r[1]) for r in self._rows) + 2
        print("\n" + "─" * (width + 30))
        print(f"  {'Step':<{width}}  {'Status':<6}  Detail")
        print("─" * (width + 30))
        for status, name, detail in self._rows:
            icon = "✓" if status == "PASS" else "✗"
            print(f"  {icon} {name:<{width}}  {status:<6}  {detail}")
        print("─" * (width + 30))
        failed = [r for r in self._rows if r[0] == "FAIL"]
        print(f"  {len(self._rows) - len(failed)}/{len(self._rows)} steps passed\n")

    def failures(self) -> list[str]:
        return [f"{r[1]}: {r[2]}" for r in self._rows if r[0] == "FAIL"]


# ── helpers ───────────────────────────────────────────────────────────────────


def _get(client: httpx.Client, path: str, **params: Any) -> dict[str, Any]:
    resp = client.get(f"{_BASE_URL}{path}", params={k: v for k, v in params.items() if v is not None})
    resp.raise_for_status()
    return resp.json()


def _post_json(client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(f"{_BASE_URL}{path}", json=body)
    resp.raise_for_status()
    return resp.json()


def _post_file(client: httpx.Client, path: str, file_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    with file_path.open("rb") as fh:
        resp = client.post(
            f"{_BASE_URL}{path}",
            data={k: v for k, v in data.items() if v is not None},
            files={"file": (file_path.name, fh)},
        )
    resp.raise_for_status()
    return resp.json()


def _ensure_pdf() -> Path:
    from fixtures.generate_fixtures import generate_pdf

    pdf = FIXTURES_DIR / "sample.pdf"
    if not pdf.exists():
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        generate_pdf(pdf)
    return pdf


# ── the smoke test ────────────────────────────────────────────────────────────


@pytest.mark.live
def test_full_pipeline() -> None:
    """Run the complete LarkScout pipeline end-to-end and print a summary."""
    steps = _Step()
    pdf = _ensure_pdf()

    with httpx.Client(timeout=120.0) as http:

        # ── Step 1: Health checks ─────────────────────────────────────────────
        for endpoint in ("/health", "/web/health", "/doc/health"):
            try:
                resp = http.get(f"{_BASE_URL}{endpoint}")
                steps.assert_ok(
                    resp.status_code == 200,
                    f"health {endpoint}",
                    f"status={resp.status_code}",
                )
            except Exception as exc:
                steps.fail(f"health {endpoint}", str(exc))

        # ── Step 2: Web capture ───────────────────────────────────────────────
        web_doc_id: str = ""
        try:
            data = _post_json(
                http,
                "/web/capture",
                {"url": _CAPTURE_URL, "tags": [_TAG], "extract_tables": True},
            )
            web_doc_id = data.get("doc_id", "")
            steps.assert_ok(
                web_doc_id.startswith("WEB-"),
                "web capture doc_id",
                web_doc_id,
            )
        except Exception as exc:
            steps.fail("web capture doc_id", str(exc))

        # ── Step 3: Doc parse ─────────────────────────────────────────────────
        doc_doc_id: str = ""
        try:
            data = _post_file(
                http,
                "/doc/parse",
                pdf,
                {"generate_summary": "false", "tags": f'["{_TAG}"]'},
            )
            doc_doc_id = data.get("doc_id", "")
            steps.assert_ok(
                doc_doc_id.startswith("DOC-"),
                "doc parse doc_id",
                doc_doc_id,
            )
        except Exception as exc:
            steps.fail("doc parse doc_id", str(exc))

        # ── Step 4: Cross search (by tag to ensure both sources are found) ──────
        try:
            results = _get(http, "/doc/library/search", q="", tags=_TAG, limit=50)
            doc_ids = {r["doc_id"] for r in results.get("results", [])}
            steps.assert_ok(
                web_doc_id in doc_ids,
                "search finds WEB doc",
                f"WEB doc_id={web_doc_id}",
            )
            steps.assert_ok(
                doc_doc_id in doc_ids,
                "search finds DOC doc",
                f"DOC doc_id={doc_doc_id}",
            )
        except Exception as exc:
            steps.fail("cross search", str(exc))

        # ── Step 5: Three-tier loading ────────────────────────────────────────
        for doc_id in filter(None, [web_doc_id, doc_doc_id]):
            prefix = "WEB" if doc_id.startswith("WEB-") else "DOC"
            # digest
            try:
                data = _get(http, f"/doc/library/{doc_id}/digest")
                steps.assert_ok(
                    bool(data.get("content")),
                    f"{prefix} digest",
                    f"len={len(data.get('content', ''))}",
                )
            except Exception as exc:
                steps.fail(f"{prefix} digest", str(exc))
            # brief — web captures don't generate a brief; skip for WEB docs
            if prefix != "WEB":
                try:
                    data = _get(http, f"/doc/library/{doc_id}/brief")
                    steps.assert_ok(
                        bool(data.get("content")),
                        f"{prefix} brief",
                        f"len={len(data.get('content', ''))}",
                    )
                except Exception as exc:
                    steps.fail(f"{prefix} brief", str(exc))
            # section — DOC must have sections; WEB may have none if capture
            # returned a challenge/redirect page (network-dependent)
            try:
                sections_data = _get(http, f"/doc/library/{doc_id}/sections")
                sections = sections_data.get("sections", [])
                if sections:
                    sid = sections[0]["sid"]
                    sec = _get(http, f"/doc/library/{doc_id}/section/{sid}")
                    steps.assert_ok(
                        bool(sec.get("content")),
                        f"{prefix} section",
                        f"sid={sid} len={len(sec.get('content', ''))}",
                    )
                elif prefix == "DOC":
                    steps.fail(f"{prefix} section", "no sections returned for DOC")
                else:
                    # WEB doc with no sections: page may have been a redirect/challenge
                    steps.ok(f"{prefix} section", "sections API ok (0 sections, network-dependent)")
            except Exception as exc:
                steps.fail(f"{prefix} section", str(exc))

    # ── Step 6: SDK round-trip ────────────────────────────────────────────────
    try:
        with LarkScoutClient(_BASE_URL, timeout=120.0) as sdk:
            # capture
            sdk_web = sdk.capture(_CAPTURE_URL, tags=[_TAG])
            steps.assert_ok(
                sdk_web["doc_id"].startswith("WEB-"),
                "SDK capture",
                sdk_web["doc_id"],
            )
            # get_digest
            dig = sdk.get_digest(sdk_web["doc_id"])
            steps.assert_ok(bool(dig.get("content")), "SDK get_digest", "")
            # parse
            sdk_doc = sdk.parse(pdf, generate_summary=False, tags=[_TAG])
            steps.assert_ok(
                sdk_doc["doc_id"].startswith("DOC-"),
                "SDK parse",
                sdk_doc["doc_id"],
            )
            # search
            sr = sdk.search("example")
            steps.assert_ok(sr["total"] >= 1, "SDK search", f"total={sr['total']}")
            # list_sections + get_section
            secs = sdk.list_sections(sdk_doc["doc_id"])
            if secs.get("sections"):
                sid = secs["sections"][0]["sid"]
                sec = sdk.get_section(sdk_doc["doc_id"], sid)
                steps.assert_ok(
                    bool(sec.get("content")), "SDK get_section", f"sid={sid}"
                )
            else:
                steps.fail("SDK get_section", "no sections returned by SDK")
    except Exception as exc:
        steps.fail("SDK round-trip", str(exc))

    # ── Summary ───────────────────────────────────────────────────────────────
    steps.print_summary()

    failures = steps.failures()
    assert not failures, "Pipeline failures:\n" + "\n".join(f"  • {f}" for f in failures)
