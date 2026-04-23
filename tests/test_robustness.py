"""Robustness tests for TASK-018: row limits, rate limiting, health masking, OCR retry, atomic writes."""

import csv
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


class TestCSVParse:
    """CSV parsing via MarkItDown produces valid results."""

    def test_csv_small_file(self):
        import larkscout_docreader

        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["a", "b"])
            for i in range(5):
                writer.writerow([str(i), str(i)])
            path = Path(f.name)

        try:
            result = larkscout_docreader.parse_csv(path)
            assert result.file_type == "csv"
            assert result.total_pages == 1
        finally:
            path.unlink(missing_ok=True)


class TestXLSXParse:
    """XLSX parsing via MarkItDown produces valid results."""

    def test_xlsx_basic_parse(self):
        openpyxl = pytest.importorskip("openpyxl")
        import larkscout_docreader

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            path = Path(f.name)

        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["col_a", "col_b"])
            for i in range(10):
                ws.append([f"val_{i}", i])
            wb.save(path)
            wb.close()

            result = larkscout_docreader.parse_xlsx(path)
            assert result.file_type == "xlsx"
            assert result.total_pages >= 1
        finally:
            path.unlink(missing_ok=True)


class TestPDFParse:
    """PDF parsing should preserve page-level location hints."""

    def test_pdf_page_ranges_are_not_collapsed_to_page_one(self):
        from fixtures.generate_fixtures import generate_pdf
        from larkscout_docreader import _page_bounds, parse_pdf

        with tempfile.TemporaryDirectory() as tmp:
            path = generate_pdf(Path(tmp) / "sample.pdf")
            result = parse_pdf(path, extract_tables=False)

        assert result.total_pages == 2
        assert result.sections
        page_ranges = [_page_bounds(sec.page_range) for sec in result.sections]
        assert any((start == 2 or end == 2) for start, end in page_ranges), page_ranges


class TestDocIdStrategy:
    """doc_id generation can derive a safe directory name from the source filename."""

    def test_source_filename_strategy_uses_stem(self, monkeypatch):
        import larkscout_docreader

        monkeypatch.setenv("LARKSCOUT_DOC_ID_STRATEGY", "source_filename")

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            assert larkscout_docreader._resolve_doc_id(docs_dir, "NBS250321.pdf", None) == "NBS250321"

    def test_source_filename_strategy_filters_unsupported_chars(self, monkeypatch):
        import larkscout_docreader

        monkeypatch.setenv("LARKSCOUT_DOC_ID_STRATEGY", "source_filename")

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            result = larkscout_docreader._resolve_doc_id(
                docs_dir,
                "合同/NBS_250321（终版）.pdf",
                None,
            )
            assert result == "NBS-250321"

    def test_source_filename_strategy_falls_back_when_nothing_usable_remains(self, monkeypatch):
        import larkscout_docreader

        monkeypatch.setenv("LARKSCOUT_DOC_ID_STRATEGY", "source_filename")

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp)
            result = larkscout_docreader._resolve_doc_id(docs_dir, "合同终版.pdf", None)
            assert result == "DOC-001"


class TestHealthPathMasking:
    """M10: Health endpoints must not expose absolute filesystem paths."""

    def test_doc_health_masks_path(self, client: TestClient) -> None:
        resp = client.get("/doc/health")
        assert resp.status_code == 200
        data = resp.json()
        docs_dir = data.get("docs_dir", "")
        home = os.path.expanduser("~")
        assert not docs_dir.startswith(home), f"Absolute path exposed: {docs_dir}"

    def test_web_health_masks_paths(self, client: TestClient) -> None:
        resp = client.get("/web/health")
        assert resp.status_code == 200
        data = resp.json()
        home = os.path.expanduser("~")
        for key in ("readability_js_path", "yolo_onnx_path"):
            val = data.get(key)
            if val:
                assert not val.startswith(home), f"{key} exposes absolute path: {val}"


class TestAtomicWriteText:
    """M9: _write_text must use atomic write pattern."""

    def test_write_text_is_atomic(self):
        from larkscout_docreader import _write_text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.md"
            _write_text(path, "hello world")
            assert path.read_text() == "hello world"
            # No leftover .tmp file
            assert not path.with_suffix(".tmp").exists()


class TestGeminiOCRRetry:
    """M6: OCR must retry on failure like summarize()."""

    def test_docreader_ocr_wrapper_handles_provider_init_failure(self, monkeypatch):
        import larkscout_docreader

        import providers

        monkeypatch.setattr(providers, "get_provider", lambda: (_ for _ in ()).throw(RuntimeError("missing key")))

        result = larkscout_docreader.gemini_ocr(b"png-bytes", page_num=2)

        assert result == "[OCR failed: page 2]"

    def test_ocr_retries_on_failure(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        # First call raises, second succeeds
        mock_response = MagicMock()
        mock_response.text = "extracted text"
        mock_client.models.generate_content.side_effect = [
            RuntimeError("transient"),
            mock_response,
        ]

        # Minimal 1x1 white PNG
        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

        with patch("time.sleep"):
            result = provider.ocr(img_bytes, page_num=1, max_retries=2)

        assert result == "extracted text"
        assert mock_client.models.generate_content.call_count == 2

    def test_ocr_exhausts_retries(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client
        mock_client.models.generate_content.side_effect = RuntimeError("persistent")

        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

        with patch("time.sleep"):
            result = provider.ocr(img_bytes, page_num=3, max_retries=2)

        assert "[OCR failed for page 3]" in result
        assert mock_client.models.generate_content.call_count == 3


class TestGeminiTimeout:
    """M7: Gemini API calls must include timeout config."""

    def test_summarize_passes_timeout(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        mock_response = MagicMock()
        mock_response.text = "summary"
        mock_client.models.generate_content.return_value = mock_response

        provider.summarize("text", "prompt")

        call_kwargs = mock_client.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert config["http_options"]["timeout"] == 60_000

    def test_ocr_passes_timeout(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        provider._client = mock_client

        mock_response = MagicMock()
        mock_response.text = "ocr text"
        mock_client.models.generate_content.return_value = mock_response

        import io

        from PIL import Image as PILImage

        buf = io.BytesIO()
        PILImage.new("RGB", (1, 1)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

        provider.ocr(img_bytes, page_num=1)

        call_kwargs = mock_client.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert config is not None
        assert config["http_options"]["timeout"] == 60_000
