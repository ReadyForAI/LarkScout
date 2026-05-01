import io
import json
import zipfile
from pathlib import Path

from PIL import Image


def _make_png_bytes() -> bytes:
    image = Image.new("RGB", (80, 32), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _make_docx_with_image(path: Path, image_path: Path) -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>学历证明</w:t></w:r></w:p>
    <w:p><w:r><w:t>以下图片为证明材料。</w:t></w:r></w:p>
    <w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
  </w:body>
</w:document>
"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/image1.png"/>
</Relationships>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        zf.writestr("word/media/image1.png", image_path.read_bytes())


def test_extract_word_embedded_images_keeps_generic_anchor(tmp_path, monkeypatch):
    import larkscout_docreader as docreader
    from larkscout_docreader import Section

    image_path = tmp_path / "proof.png"
    image_path.write_bytes(_make_png_bytes())
    docx_path = tmp_path / "bid.docx"
    _make_docx_with_image(docx_path, image_path)

    sections = [
        Section(
            index=1,
            title="学历证明",
            level=1,
            text="学历证明\n以下图片为证明材料。",
            page_range="p.1-1",
            sid="s-proof",
        )
    ]
    monkeypatch.setattr(
        docreader,
        "_ocr_embedded_image",
        lambda image, backend: ("普通高等学校毕业证书", "local-paddleocr", "ok", ""),
    )

    images = docreader._extract_word_embedded_images(
        docx_path,
        sections=sections,
        ocr_images=True,
        image_ocr_backend="local",
        max_images=10,
    )

    assert len(images) == 1
    image = images[0]
    assert image.image_id == "IMG-001"
    assert image.near_heading == "学历证明"
    assert image.anchor_sid == "s-proof"
    assert image.section_title == "学历证明"
    assert image.original_type == "image/png"
    assert image.render_status == "ok"
    assert image.ocr_status == "ok"
    assert image.ocr_text == "普通高等学校毕业证书"
    assert sections[0].image_refs == ["IMG-001"]


def test_extract_word_embedded_images_honors_zero_limit(tmp_path):
    import larkscout_docreader as docreader
    from larkscout_docreader import Section

    image_path = tmp_path / "proof.png"
    image_path.write_bytes(_make_png_bytes())
    docx_path = tmp_path / "bid.docx"
    _make_docx_with_image(docx_path, image_path)

    sections = [
        Section(
            index=1,
            title="学历证明",
            level=1,
            text="学历证明\n以下图片为证明材料。",
            page_range="p.1-1",
            sid="s-proof",
        )
    ]

    images = docreader._extract_word_embedded_images(
        docx_path,
        sections=sections,
        max_images=0,
    )

    assert images == []
    assert sections[0].image_refs == []


def test_write_output_extract_only_writes_image_artifacts(tmp_path):
    from larkscout_docreader import (
        EmbeddedImage,
        PageContent,
        ParsedDocument,
        Section,
        write_output_extract_only,
    )

    png_bytes = _make_png_bytes()
    section = Section(
        index=1,
        title="证明材料",
        level=1,
        text="证明材料正文",
        page_range="p.1-1",
        sid="s-proof",
        image_refs=["IMG-001"],
    )
    parsed = ParsedDocument(
        filename="bid.docx",
        file_type="docx",
        total_pages=1,
        pages=[PageContent(page_num=1, text="证明材料正文")],
        sections=[section],
        images=[
            EmbeddedImage(
                image_id="IMG-001",
                order=1,
                media_path="word/media/image1.png",
                relationship_id="rId9",
                paragraph_index=3,
                near_heading="证明材料",
                anchor_sid="s-proof",
                section_title="证明材料",
                original_ext=".png",
                original_type="image/png",
                original_bytes=png_bytes,
                rendered_ext=".png",
                rendered_type="image/png",
                rendered_bytes=png_bytes,
                render_status="ok",
                ocr_enabled=True,
                ocr_backend="local-paddleocr",
                ocr_status="ok",
                ocr_text="证书 OCR 文本",
            )
        ],
    )

    write_output_extract_only(
        "DOC-101",
        parsed,
        tmp_path,
        summary_placeholder="pending",
    )

    doc_dir = tmp_path / "DOC-101"
    manifest = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    images = json.loads((doc_dir / "images.json").read_text(encoding="utf-8"))

    assert manifest["paths"]["images"] == "images.json"
    assert manifest["sections"][0]["image_refs"] == ["IMG-001"]
    assert manifest["images"][0]["image_id"] == "IMG-001"
    assert images[0]["ocr"]["text"] == "证书 OCR 文本"
    assert (doc_dir / "images" / "IMG-001.original.png").exists()
    assert (doc_dir / "images" / "IMG-001.png").exists()
    assert (doc_dir / "images" / "IMG-001.ocr.txt").read_text(encoding="utf-8").strip() == "证书 OCR 文本"
