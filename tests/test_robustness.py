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

    def test_load_document_profile_contract_cn(self):
        from larkscout_docreader import _load_document_profile

        profile = _load_document_profile("contract_cn", None)

        assert profile is not None
        assert profile.name == "contract_cn"
        assert profile.upgrade_policy.local_ocr_backend == "paddleocr"
        assert profile.processing_policy.large_file_threshold_mb == 50
        assert profile.processing_policy.max_local_ocr_pixels == 4_000_000
        assert profile.summary_policy.async_modes == ("fast", "accurate")
        assert profile.classification.required_terms

    def test_resolve_ocr_render_scale_caps_large_pages(self):
        from larkscout_docreader import _resolve_ocr_render_scale

        class Rect:
            width = 1500
            height = 1500

        class Page:
            rect = Rect()

        scale, pixels, capped = _resolve_ocr_render_scale(
            Page(),
            requested_scale=2.0,
            max_pixels=4_000_000,
            min_scale=1.25,
        )

        assert capped is True
        assert scale < 2.0
        assert pixels <= 4_000_000

    def test_assess_contract_quality_detects_scan_only_pdf(self):
        from larkscout_docreader import _assess_contract_quality, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        assessment = _assess_contract_quality(
            "合同\n甲方\n乙方",
            [
                {"page_num": 1, "text_len": 0, "image_count": 1, "scan_like": True},
                {"page_num": 2, "text_len": 12, "image_count": 1, "scan_like": True},
                {"page_num": 3, "text_len": 0, "image_count": 1, "scan_like": True},
            ],
            profile,
        )

        assert assessment["document_quality"] == "scan_only"
        assert assessment["is_contract"] is True

    def test_classify_contract_text_matches_required_terms(self):
        from larkscout_docreader import _classify_contract_text, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        is_contract, matched_terms = _classify_contract_text(
            "采购合同\n甲方：测试公司\n乙方：示例公司",
            profile,
        )

        assert is_contract is True
        assert matched_terms == ["合同", "甲方", "乙方"]

    def test_plan_pdf_ocr_uses_local_backend_for_scan_only_accurate_mode(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=False,
            explicit_ocr_pages=None,
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2, 3],
                "sparse_pages": [1, 2, 3],
                "image_pages": [1, 2, 3],
                "page_signals": [
                    {"page_num": 1},
                    {"page_num": 2},
                    {"page_num": 3},
                ],
            },
        )

        assert plan["local_backend"] == "paddleocr"
        assert plan["local_ocr_pages"] == [1, 2, 3]
        assert plan["llm_ocr_pages"] == []
        assert plan["region_llm"] is True

    def test_plan_pdf_ocr_force_ocr_uses_llm_full_path(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=True,
            explicit_ocr_pages=None,
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2],
                "sparse_pages": [1, 2],
                "image_pages": [1, 2],
                "page_signals": [{"page_num": 1}, {"page_num": 2}],
            },
        )

        assert plan["llm_ocr_pages"] == [1, 2]
        assert plan["proofread"] is True

    def test_plan_pdf_ocr_explicit_pages_upgrade_only_selected_pages(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=False,
            explicit_ocr_pages={2},
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2, 3],
                "sparse_pages": [1, 2, 3],
                "image_pages": [1, 2, 3],
                "page_signals": [{"page_num": 1}, {"page_num": 2}, {"page_num": 3}],
            },
        )

        assert plan["llm_ocr_pages"] == [2]
        assert plan["local_ocr_pages"] == [1, 3]
        assert plan["region_llm"] is True

    def test_plan_pdf_ocr_skips_detected_blank_scan_pages(self):
        from larkscout_docreader import _load_document_profile, _plan_pdf_ocr

        profile = _load_document_profile("contract_cn", None)
        plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode="accurate",
            force_ocr=False,
            explicit_ocr_pages=None,
            assessment={
                "document_quality": "scan_only",
                "scan_like_pages": [1, 2, 3],
                "sparse_pages": [1, 2, 3],
                "image_pages": [1, 2, 3],
                "blank_pages": [2],
                "page_signals": [{"page_num": 1}, {"page_num": 2}, {"page_num": 3}],
            },
        )

        assert plan["local_ocr_pages"] == [1, 3]
        assert plan["llm_ocr_pages"] == []

    def test_metadata_page_range_spec_accepts_list_values(self):
        from larkscout_docreader import _metadata_page_range_spec

        assert _metadata_page_range_spec([20, 28, "32-34"]) == "20,28,32-34"

    def test_resolve_summary_mode_uses_contract_profile_async_for_accurate(self):
        from larkscout_docreader import _load_document_profile, _resolve_summary_mode

        profile = _load_document_profile("contract_cn", None)
        mode = _resolve_summary_mode(
            profile=profile,
            parse_mode="accurate",
            generate_summary=True,
            requested_mode=None,
        )

        assert mode == "defer"

    def test_classify_summary_error_maps_rate_limit(self):
        from larkscout_docreader import _classify_summary_error

        code, message = _classify_summary_error(RuntimeError("Error code: 429 - 速率限制"))

        assert code == "rate_limit"
        assert message == "upstream rate limit"

    def test_strip_section_storage_wrapper_removes_summary_prefix(self):
        from larkscout_docreader import _strip_section_storage_wrapper

        raw = (
            "# 合同条款\n\n"
            "**章节 1** | **SID**: abc | **页码**: p.1-1\n\n"
            "**摘要**: 示例摘要\n\n---\n\n"
            "正文内容"
        )

        assert _strip_section_storage_wrapper(raw) == "正文内容"

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

    def test_extract_tables_from_ocr_text_strips_footer_page_number(self):
        from larkscout_docreader import _extract_tables_from_ocr_text

        text, tables = _extract_tables_from_ocr_text(
            "合同正文\n甲方：测试公司\n2",
            page_num=3,
            total_pages=15,
        )

        assert text == "合同正文\n甲方：测试公司"
        assert tables == []

    def test_cleanup_ocr_text_removes_watermark_noise_and_footer(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "\n".join(
                [
                    "[Tp]",
                    "次",
                    "[24Yeeeeai_a_a入的场所处tblaeta告i可ztg",
                    "括其雇员、工作员或代理，不得进入甲方的任何场所。",
                    "4.2乙方应在本合同附件《APM应用性能监测软件采贝合同补充条款》",
                    "eeaeee]",
                    "5.1.1许可软件安装元成后应符合软件说明书的标准。",
                    "[T1tb_e可e_i_eieteeobleset]",
                    "第 4 页 / 共 25 页",
                ]
            )
        )

        assert "[Tp]" not in cleaned
        assert "eeaeee" not in cleaned
        assert "tblaeta" not in cleaned
        assert "第 4 页" not in cleaned
        assert "软件采购合同补充条款" in cleaned
        assert "许可软件安装完成" in cleaned
        assert "括其雇员、工作员或代理" in cleaned

    def test_cleanup_ocr_text_removes_stray_dingzuo_after_sign_place(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "\n".join(
                [
                    "[Tbabla_e_ea_l_e_e_e_a_T_e_eantrp]",
                    "合同签订地点：",
                    "上海市浦东新区",
                    "定作",
                    "-第1页共19页-",
                ]
            )
        )

        assert "Tbabla" not in cleaned
        assert "定作" not in cleaned
        assert "第1页" not in cleaned
        assert "上海市浦东新区" in cleaned

    def test_cleanup_ocr_text_uses_source_filename_for_leading_doc_id(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "NBS220752\n甲方（委托方）：华夏基金管理有限公司\n第1页 / 共25页",
            source_filename="NBS220952.pdf",
        )

        assert cleaned.splitlines()[0] == "NBS220952"
        assert "第1页" not in cleaned

    def test_cleanup_ocr_text_removes_generic_llm_preface(self):
        from larkscout_docreader import _cleanup_ocr_text

        cleaned = _cleanup_ocr_text(
            "Preface\n兴业数字金融服务（上海）股份有限公司\n合同编号： CFT-JT-FZ-202205-0018"
        )

        assert cleaned.splitlines()[0] == "兴业数字金融服务（上海）股份有限公司"

    def test_extract_profile_fields_rejects_bad_cover_values_and_uses_filename(self):
        from larkscout_docreader import PageContent, _extract_profile_fields, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        fields = _extract_profile_fields(
            [
                PageContent(
                    page_num=1,
                    text="\n".join(
                        [
                            "合同编号：",
                            "甲",
                            "乙方：",
                            "てさ",
                            "合同签订地点：",
                            "上海市浦东新区",
                        ]
                    ),
                )
            ],
            profile,
            source_filename="NBS220952.pdf",
        )

        assert fields["contract_no"]["value"] == "NBS220952"
        assert fields["contract_no"]["source"] == "source_filename"
        assert "party_b_name" not in fields
        assert fields["sign_place"]["value"] == "上海市浦东新区"

    def test_extract_profile_fields_supports_cover_party_labels(self):
        from larkscout_docreader import PageContent, _extract_profile_fields, _load_document_profile

        profile = _load_document_profile("contract_cn", None)
        fields = _extract_profile_fields(
            [
                PageContent(
                    page_num=1,
                    text="\n".join(
                        [
                            "甲方（委托方）：华夏基金管理有限公司",
                            "乙方（受托方）：北京基调网络股份有限公司",
                            "签订地点：北京市顺义区后沙峪镇空港B区安庆大街甲3号",
                        ]
                    ),
                )
            ],
            profile,
            source_filename="NBS220952.pdf",
        )

        assert fields["party_a_name"]["value"] == "华夏基金管理有限公司"
        assert fields["party_b_name"]["value"] == "北京基调网络股份有限公司"
        assert fields["sign_place"]["value"] == "北京市顺义区后沙峪镇空港B区安庆大街甲3号"

    def test_extract_tables_from_ocr_text_keeps_table_complete(self):
        from larkscout_docreader import _extract_tables_from_ocr_text

        text, tables = _extract_tables_from_ocr_text(
            "\n".join(
                [
                    "1. 软件产品",
                    "序号 名称 数量 税率 含税金额",
                    "1 平台A 1 13% ¥29,800.00",
                    "2 平台B 1 13% ¥562,520.00",
                    "服务小计 ¥592,320.00",
                    "2. 合同价款的支付方式",
                ]
            ),
            page_num=3,
            total_pages=15,
        )

        assert text == "1. 软件产品\n2. 合同价款的支付方式"
        assert tables == [
            "\n".join(
                [
                    "序号 名称 数量 税率 含税金额",
                    "1 平台A 1 13% ¥29,800.00",
                    "2 平台B 1 13% ¥562,520.00",
                    "服务小计 ¥592,320.00",
                ]
            )
        ]

    def test_split_sections_does_not_treat_table_rows_as_headings(self):
        from larkscout_docreader import PageContent, _split_sections

        pages = [
            PageContent(
                page_num=1,
                text="\n".join(
                    [
                        "1. 软件产品",
                        "产品说明",
                        "2. 合同价款的支付方式",
                        "付款安排",
                    ]
                ),
                tables=[
                    "\n".join(
                        [
                            "序号 名称 数量 税率 含税金额",
                            "1 平台A 1 13% ¥29,800.00",
                            "2 平台B 1 13% ¥562,520.00",
                        ]
                    )
                ],
            )
        ]

        sections = _split_sections(pages)

        assert [sec.title for sec in sections] == ["1. 软件产品", "2. 合同价款的支付方式"]
        assert "1 平台A 1 13% ¥29,800.00" in sections[0].text


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
