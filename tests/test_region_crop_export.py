import json

import pytest
from fastapi import HTTPException


def _write_pdf(path):
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.draw_rect(fitz.Rect(0, 0, 200, 100), color=(1, 1, 1), fill=(1, 1, 1))
    page.draw_rect(fitz.Rect(40, 20, 120, 70), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(path)
    doc.close()


def _write_doc_fixture(tmp_path, *, with_ocr_blocks=False):
    docs_dir = tmp_path / "docs"
    doc_dir = docs_dir / "DOC-001"
    source_dir = doc_dir / "source"
    source_dir.mkdir(parents=True)
    pdf_path = source_dir / "sample.pdf"
    _write_pdf(pdf_path)
    manifest = {
        "doc_id": "DOC-001",
        "filename": "sample.pdf",
        "file_type": "pdf",
        "source_file": {
            "kind": "upload",
            "filename": "sample.pdf",
            "stored_filename": "sample.pdf",
            "ref": "source/sample.pdf",
        },
    }
    (doc_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_ocr_blocks:
        sidecar = {
            "version": 1,
            "doc_id": "DOC-001",
            "coordinate_system": "image_pixels",
            "pages": [{"page": 1, "width": 400, "height": 200, "blocks": []}],
        }
        (doc_dir / "ocr_blocks.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return docs_dir, doc_dir


def test_export_pdf_region_crop_writes_derived_artifacts(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, doc_dir = _write_doc_fixture(tmp_path)

    metadata = export_pdf_region_crop(
        docs_dir,
        "DOC-001",
        1,
        [40, 20, 120, 70],
        dpi=144,
        coordinate_system="page_points",
    )

    output_path = doc_dir / metadata["output_path"]
    metadata_path = doc_dir / metadata["metadata_path"]
    assert metadata["derived"] is True
    assert metadata["coordinate_system"] == "page_points"
    assert metadata["dpi"] == 144
    assert metadata["source_ref"] == "source/sample.pdf"
    assert metadata["source_bounds"] == {"width": 200.0, "height": 100.0, "unit": "points"}
    assert output_path.read_bytes().startswith(b"\x89PNG")
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["output_path"] == metadata["output_path"]
    assert metadata["output_path"].startswith("derived/crops/")


def test_export_pdf_region_crop_converts_image_pixel_bbox(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path, with_ocr_blocks=True)

    metadata = export_pdf_region_crop(
        docs_dir,
        "DOC-001",
        1,
        [80, 40, 240, 140],
        dpi=72,
        coordinate_system="image_pixels",
    )

    assert metadata["source_bounds"] == {"width": 400.0, "height": 200.0, "unit": "pixels"}
    assert metadata["clip_rect"] == [40.0, 20.0, 120.0, 70.0]


def test_export_pdf_region_crop_rejects_invalid_page(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc:
        export_pdf_region_crop(
            docs_dir,
            "DOC-001",
            2,
            [40, 20, 120, 70],
            coordinate_system="page_points",
        )

    assert exc.value.status_code == 422
    assert "page out of range" in str(exc.value.detail)


def test_export_pdf_region_crop_rejects_invalid_bbox(tmp_path):
    from larkscout_docreader import export_pdf_region_crop

    docs_dir, _doc_dir = _write_doc_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc:
        export_pdf_region_crop(
            docs_dir,
            "DOC-001",
            1,
            [120, 20, 40, 70],
            coordinate_system="page_points",
        )

    assert exc.value.status_code == 422
    assert "positive area" in str(exc.value.detail)
