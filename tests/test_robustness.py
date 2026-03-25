"""Robustness tests for TASK-018: row limits, rate limiting, health masking, OCR retry, atomic writes."""

import csv
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient


class TestCSVRowLimit:
    """H5: CSV parsing must truncate at MAX_PARSE_ROWS."""

    def test_csv_truncated_at_limit(self):
        import larkscout_docreader

        old_limit = larkscout_docreader.MAX_PARSE_ROWS
        larkscout_docreader.MAX_PARSE_ROWS = 50
        try:
            with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["col_a", "col_b"])
                for i in range(200):
                    writer.writerow([f"val_{i}", str(i)])
                path = Path(f.name)

            result = larkscout_docreader.parse_csv(path)
            assert result.metadata.get("truncated") is True
            assert result.metadata.get("max_rows") == 50
            # The markdown table should have at most 50 data rows + header
            lines = result.sections[0].text.strip().split("\n")
            # header + separator + data rows
            assert len(lines) <= 50 + 2
        finally:
            larkscout_docreader.MAX_PARSE_ROWS = old_limit
            path.unlink(missing_ok=True)

    def test_csv_not_truncated_under_limit(self):
        import larkscout_docreader

        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["a", "b"])
            for i in range(5):
                writer.writerow([str(i), str(i)])
            path = Path(f.name)

        try:
            result = larkscout_docreader.parse_csv(path)
            assert result.metadata.get("truncated") is None
        finally:
            path.unlink(missing_ok=True)


class TestXLSXRowLimit:
    """H4: XLSX parsing must truncate at MAX_PARSE_ROWS."""

    def test_xlsx_truncated_at_limit(self):
        openpyxl = pytest.importorskip("openpyxl")
        import larkscout_docreader

        old_limit = larkscout_docreader.MAX_PARSE_ROWS
        larkscout_docreader.MAX_PARSE_ROWS = 50
        try:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
                path = Path(f.name)

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["col_a", "col_b"])
            for i in range(200):
                ws.append([f"val_{i}", i])
            wb.save(path)
            wb.close()

            result = larkscout_docreader.parse_xlsx(path)
            assert result.metadata.get("truncated") is True
            assert result.metadata.get("max_rows") == 50
        finally:
            larkscout_docreader.MAX_PARSE_ROWS = old_limit
            path.unlink(missing_ok=True)


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
