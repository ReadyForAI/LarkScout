import json
from pathlib import Path

import pytest


def test_ocr_blocks_sidecar_contract_shape(tmp_path: Path):
    from larkscout_docreader import OCRBlocksSidecar, OCRPageBlocks, OCRTextBlock

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-001",
        pages=(
            OCRPageBlocks(
                page=1,
                width=2480,
                height=3508,
                blocks=(
                    OCRTextBlock(
                        block_id="p1-b0001",
                        text="示例文本",
                        bbox=(100, 220, 680, 260),
                        confidence=0.94,
                        source="local_ocr",
                        line_index=12,
                        order=12,
                    ),
                ),
            ),
        ),
    )

    data = sidecar.to_dict()

    assert data["version"] == 1
    assert data["doc_id"] == "DOC-001"
    assert data["coordinate_system"] == "image_pixels"
    assert len(data["pages"]) == 1
    page = data["pages"][0]
    assert page["page"] == 1
    assert page["width"] == 2480
    assert page["height"] == 3508
    block = page["blocks"][0]
    assert block == {
        "block_id": "p1-b0001",
        "text": "示例文本",
        "bbox": [100.0, 220.0, 680.0, 260.0],
        "confidence": 0.94,
        "source": "local_ocr",
        "line_index": 12,
        "order": 12,
    }


def test_layout_manifest_entry_is_low_token_metadata_only():
    from larkscout_docreader import _build_layout_manifest_entry

    layout = _build_layout_manifest_entry(available=True)

    assert layout == {
        "available": True,
        "ocr_blocks_path": "ocr_blocks.json",
        "version": 1,
        "coordinate_system": "image_pixels",
    }
    assert "pages" not in layout
    assert "blocks" not in layout


def test_unavailable_layout_manifest_entry_has_no_sidecar_path():
    from larkscout_docreader import _build_layout_manifest_entry

    layout = _build_layout_manifest_entry(available=False)

    assert layout["available"] is False
    assert layout["ocr_blocks_path"] == ""
    assert layout["version"] == 1
    assert layout["coordinate_system"] == "image_pixels"


def test_write_ocr_blocks_sidecar_returns_manifest_metadata(tmp_path: Path):
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        OCRTextBlock,
        _write_ocr_blocks_sidecar,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-002",
        pages=(
            OCRPageBlocks(
                page=1,
                width=100,
                height=200,
                blocks=(OCRTextBlock(block_id="p1-b0001", text="A", bbox=(1, 2, 3, 4)),),
            ),
        ),
    )

    layout = _write_ocr_blocks_sidecar(tmp_path, sidecar)
    written = json.loads((tmp_path / "ocr_blocks.json").read_text(encoding="utf-8"))

    assert layout["available"] is True
    assert layout["ocr_blocks_path"] == "ocr_blocks.json"
    assert "pages" not in layout
    assert written["doc_id"] == "DOC-002"
    assert written["pages"][0]["blocks"][0]["block_id"] == "p1-b0001"


def test_ocr_block_rejects_malformed_bbox():
    from larkscout_docreader import OCRTextBlock

    with pytest.raises(ValueError, match="exactly four"):
        OCRTextBlock(block_id="bad", text="A", bbox=(1, 2, 3)).to_dict()  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ordered"):
        OCRTextBlock(block_id="bad", text="A", bbox=(5, 2, 3, 4)).to_dict()

