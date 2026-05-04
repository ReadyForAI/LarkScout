def _block(block_id, text, bbox, confidence=0.9):
    from larkscout_docreader import OCRTextBlock

    return OCRTextBlock(
        block_id=block_id,
        text=text,
        bbox=bbox,
        confidence=confidence,
    )


def test_detect_table_candidates_from_ocr_grid():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-001",
        pages=(
            OCRPageBlocks(
                page=2,
                width=1000,
                height=1000,
                blocks=(
                    _block("p2-b0001", "品名", (100, 100, 180, 120)),
                    _block("p2-b0002", "数量", (300, 100, 360, 120)),
                    _block("p2-b0003", "金额", (500, 100, 560, 120)),
                    _block("p2-b0004", "软件", (100, 140, 180, 160)),
                    _block("p2-b0005", "1", (300, 140, 330, 160)),
                    _block("p2-b0006", "100", (500, 140, 560, 160)),
                    _block("p2-b0007", "服务", (100, 180, 180, 200)),
                    _block("p2-b0008", "2", (300, 180, 330, 200)),
                    _block("p2-b0009", "200", (500, 180, 560, 200)),
                ),
            ),
        ),
    )

    candidates = _detect_table_candidates_from_ocr_blocks(sidecar)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["candidate_id"] == "p2-tc0001"
    assert candidate["page"] == 2
    assert candidate["bbox"] == [100, 100, 560, 200]
    assert candidate["row_count"] == 3
    assert candidate["column_count"] == 3
    assert candidate["source"] == "ocr_geometry"
    assert candidate["ocr_block_refs"] == [f"p2-b{i:04d}" for i in range(1, 10)]


def test_detect_table_candidates_ignores_paragraph_like_blocks():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-002",
        pages=(
            OCRPageBlocks(
                page=1,
                width=1000,
                height=1000,
                blocks=(
                    _block("p1-b0001", "第一段合同正文", (100, 100, 500, 120)),
                    _block("p1-b0002", "第二段合同正文", (100, 140, 500, 160)),
                    _block("p1-b0003", "第三段合同正文", (100, 180, 500, 200)),
                ),
            ),
        ),
    )

    candidates = _detect_table_candidates_from_ocr_blocks(sidecar)

    assert candidates == []


def test_detect_table_candidates_requires_multiple_rows():
    from larkscout_docreader import (
        OCRBlocksSidecar,
        OCRPageBlocks,
        _detect_table_candidates_from_ocr_blocks,
    )

    sidecar = OCRBlocksSidecar(
        doc_id="DOC-003",
        pages=(
            OCRPageBlocks(
                page=1,
                width=1000,
                height=1000,
                blocks=(
                    _block("p1-b0001", "品名", (100, 100, 180, 120)),
                    _block("p1-b0002", "数量", (300, 100, 360, 120)),
                    _block("p1-b0003", "金额", (500, 100, 560, 120)),
                ),
            ),
        ),
    )

    candidates = _detect_table_candidates_from_ocr_blocks(sidecar)

    assert candidates == []

