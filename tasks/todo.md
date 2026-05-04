# LarkScout Scanned Table / OCR Geometry Execution Checklist

Date: 2026-05-04

## Scope

Implement the next LarkScout pass for scanned contracts, invoices, quotations, and similar enterprise PDFs. The goal is to improve low-level document structure fidelity, especially OCR geometry and tables, without adding business semantics to core.

Reference project to study: https://github.com/opendataloader-project/opendataloader-pdf

Use OpenDataLoader as a design reference for element-level bounding boxes, Markdown+JSON dual output, reading order, table structure, and visual debug output. Do not make it the immediate production backend.

## Execution Checklist

### P0.1 Define Layout Sidecar Contract

- [x] Task: Define the stable `ocr_blocks.json` schema and manifest metadata shape.

说明:
Create a low-level OCR geometry sidecar contract that can support table reconstruction, evidence lookup, and region re-recognition. Keep it generic: text, bbox, confidence, page size, source, and stable block IDs. Do not include business labels such as invoice number, contract amount, buyer, seller, or payment terms.

AC:
- [x] `ocr_blocks.json` has `version`, `doc_id`, and `pages`.
- [x] Each page has `page`, `width`, `height`, and `blocks`.
- [x] Each block has stable `block_id`, `text`, `bbox`, `confidence`, `source`, and ordering metadata.
- [x] Coordinate system is explicitly documented as image pixels or PDF points.
- [x] Manifest stores only path, availability, version, and coordinate system.
- [x] Default digest/brief/section APIs do not inline OCR blocks.

### P0.2 Capture PaddleOCR Geometry

- [x] Task: Preserve PaddleOCR line/block coordinates and confidence during local OCR.

说明:
Current scan OCR mostly produces text for downstream sectioning. Extend the OCR path so raw OCR geometry is normalized and written to the sidecar. This is the foundation for table reconstruction and page/region evidence.

AC:
- [x] Local scan OCR writes `ocr_blocks.json` for pages that run OCR.
- [x] Text content in generated sections remains compatible with current behavior.
- [x] Raw OCR cache files remain usable and are not treated as the structured source of truth.
- [x] Blank or skipped OCR pages are represented clearly or omitted with manifest metadata.
- [x] Existing scanned contract tests still pass.

### P0.3 Add Manifest Layout Metadata

- [ ] Task: Add manifest `layout` metadata for OCR block sidecars.

说明:
Expose sidecar availability without increasing token usage in normal APIs. Consumers should be able to discover whether geometry exists and where it is stored.

AC:
- [ ] Manifest includes layout availability for scan OCR outputs.
- [ ] Manifest remains backward compatible for old documents without layout metadata.
- [ ] API responses that expose manifest do not include the full block payload.
- [ ] Unit tests cover documents with and without `ocr_blocks.json`.

### P0.4 Table Metadata Compatibility Layer

- [ ] Task: Add generic table metadata while preserving current Markdown table behavior.

说明:
LarkScout should continue serving existing table Markdown, but each table should gain stable metadata such as page range, row count, column count, source, and continuation links when detectable.

AC:
- [ ] Existing `/library/{doc_id}/table/{table_id}` behavior is preserved.
- [ ] Table records include `table_id`, `page_start`, `page_end`, `row_count`, `column_count`, and `source`.
- [ ] Metadata supports `continued_from` and `continued_to`.
- [ ] Table body is not duplicated back into normal section text.
- [ ] Existing table-related tests still pass.

### P0.5 Markdown Table Row/Column Counting

- [ ] Task: Implement reliable row and column counting for existing Markdown tables.

说明:
Before reconstructing scanned tables, strengthen metadata for tables already represented as Markdown. This gives a low-risk compatibility layer and better baseline metrics.

AC:
- [ ] Markdown tables with separator rows are counted correctly.
- [ ] Empty cells and uneven rows are handled conservatively.
- [ ] Header row detection is captured where obvious.
- [ ] Unit tests include normal, empty-cell, and uneven-row tables.

### P1.1 Scanned Table Candidate Detection

- [ ] Task: Detect table-like regions from OCR block geometry.

说明:
Use generic layout signals such as repeated x positions, aligned y bands, dense numeric/text grids, and optional line/border evidence. Keep the detector document-agnostic.

AC:
- [ ] Detector returns candidate table regions with page, bbox, confidence, and block refs.
- [ ] Candidate detection does not create business-specific table labels.
- [ ] Non-table paragraphs and section headings are not over-classified in basic samples.
- [ ] Detection output can be disabled or kept sidecar-only.

### P1.2 Reconstruct Rows and Columns

- [ ] Task: Build first-pass scanned table reconstruction from OCR blocks.

说明:
Cluster OCR blocks into rows by y overlap and infer columns from x alignment. Preserve source block references for every cell so downstream Skills can inspect evidence.

AC:
- [ ] Structured table JSON is written under `tables/table-xx.json`.
- [ ] Each cell has row, column, text, bbox, confidence, and OCR block refs.
- [ ] Generated Markdown is available for low-token LLM usage.
- [ ] Multi-line cells are merged when geometry strongly supports it.
- [ ] Unit tests cover row clustering and column inference.

### P1.3 Cross-Page Table Continuation

- [ ] Task: Add heuristic continuation links for tables split across pages.

说明:
Enterprise contracts and quotations often split line-item tables across pages. Detect likely continuation using adjacent page positions, compatible column structure, repeated headers, and missing/continued titles.

AC:
- [ ] Table metadata can link `continued_from` and `continued_to`.
- [ ] Continuation logic is conservative and avoids merging unrelated tables.
- [ ] Tests include a positive multi-page sample and a negative adjacent-table sample.

### P1.4 Region Crop Export

- [ ] Task: Implement page and bbox crop export for inspection and downstream Skills.

说明:
Skills need a low-level way to inspect or reprocess a document area without core deciding what the region means. Crops should be traceable to source document, page, bbox, and render settings.

AC:
- [ ] API or internal helper can export a crop by `doc_id`, `page`, and `bbox`.
- [ ] Crop metadata records source page, bbox, coordinate system, DPI, and output path.
- [ ] Invalid bbox/page inputs return clear errors.
- [ ] Crop files are stored separately from canonical parse outputs or clearly marked as derived artifacts.

### P1.5 Region Re-OCR

- [ ] Task: Add targeted re-OCR by page and bbox.

说明:
When a field, table cell, or blurred area is weak, Skills should be able to request re-recognition of a specific region. LarkScout should provide the generic operation and traceable output only.

AC:
- [ ] Re-OCR accepts page+bbox and OCR backend parameters.
- [ ] Output is stored as a separate rerun artifact with source refs.
- [ ] Existing document outputs are not silently overwritten.
- [ ] Re-OCR result includes text, bbox, confidence where available, and backend metadata.
- [ ] Tests cover successful rerun and invalid region handling.

### P1.6 Visual Debug Artifact

- [ ] Task: Add optional annotated visual debug output for OCR blocks and tables.

说明:
Borrow the OpenDataLoader idea of visual structure verification. A developer should be able to see detected blocks, table regions, and cell boxes overlaid on page images.

AC:
- [ ] Debug output is opt-in.
- [ ] Annotated output marks OCR blocks and table regions distinctly.
- [ ] Debug artifact location is recorded in metadata when generated.
- [ ] Debug output is not returned by default APIs.

### P1.7 API Discovery Endpoints

- [ ] Task: Expose low-token discovery for layout and table sidecars.

说明:
Consumers need to discover available sidecars and request specific artifacts without pulling large payloads by default.

AC:
- [ ] Existing digest, brief, full, sections, section, table, and manifest APIs remain compatible.
- [ ] New layout/table sidecar access is explicit and bounded.
- [ ] Large geometry payloads require targeted calls or local file access.
- [ ] Response sizes remain stable for default endpoints.

### P2.1 OpenDataLoader Comparative Spike

- [ ] Task: Run a limited comparison against OpenDataLoader on selected PDFs.

说明:
Use this as a learning spike, not a backend migration. Compare element JSON, bbox conventions, table output, Markdown fidelity, and debug artifacts against LarkScout outputs.

AC:
- [ ] Comparison uses at least one scanned contract, one invoice-like table, and one text-rich PDF.
- [ ] Notes identify reusable data-model ideas and non-reusable dependency/runtime choices.
- [ ] Findings are documented in `docs/` without changing production defaults.
- [ ] No new required Java/OpenDataLoader runtime dependency is introduced by this spike.

### P2.2 Performance Guardrails

- [ ] Task: Add performance and payload-size checks for sidecar generation.

说明:
OCR geometry can become large. The implementation must preserve LarkScout's low-token default behavior and avoid large response payload regressions.

AC:
- [ ] Default API payload sizes do not materially increase.
- [ ] Sidecar generation cost is measured on representative scanned PDFs.
- [ ] Large documents avoid inlining geometry in manifest or section outputs.
- [ ] Tests or scripts capture basic size/performance metrics.

### P2.3 Real Document Validation Batch

- [ ] Task: Validate on known real scanned contract samples.

说明:
Use existing local samples such as NBS220667, NBS220952, NBS230310, and NBS250523 to verify that geometry and table output improves practical downstream extraction.

AC:
- [ ] Reparse selected samples and confirm OCR/page counts remain sane.
- [ ] Confirm blank-page behavior still works.
- [ ] Confirm table sidecars exist where table-like regions are present.
- [ ] Confirm section text quality does not regress on known corrected OCR noise cases.
- [ ] Save sample findings and remaining gaps in `docs/`.

## Review Checklist

- [ ] Root cause addressed: table failures are tackled via geometry and structure, not business-field hacks.
- [ ] Simplicity checked: sidecars are explicit and low-token APIs remain stable.
- [ ] Compatibility checked: existing APIs and tests continue to pass.
- [ ] Elegance checked: data model is generic enough for contracts, invoices, quotations, and statements.
- [ ] Verification complete: unit, regression, sample-document, and payload-size checks are run before marking done.

## Review Notes

### P0.1 Layout Sidecar Contract

- Branch: `task/p0-1-layout-sidecar-contract`
- Implementation:
  - Added normalized OCR geometry dataclasses and manifest discovery helper.
  - Added `docs/layout-sidecar-contract.md`.
  - Added `tests/test_layout_sidecar_contract.py`.
- Verification:
  - `.venv/bin/pytest tests/test_layout_sidecar_contract.py -q`: 5 passed.
  - `.venv/bin/pytest tests/test_schema_consistency.py tests/test_library_endpoints.py -q`: 48 passed.
  - `.venv/bin/pytest`: 216 passed, 15 skipped.

### P0.2 Capture PaddleOCR Geometry

- Branch: `task/p0-2-capture-paddleocr-geometry`
- Implementation:
  - Extended the isolated PaddleOCR worker to return text blocks with bbox/confidence.
  - Added `local_ocr_with_layout` while keeping existing `local_ocr` text behavior compatible.
  - Threaded local OCR page blocks through PDF parsing into `ParsedDocument.ocr_blocks`.
  - Wrote `ocr_blocks.json` and low-token manifest `layout` metadata during output persistence.
  - Reset stale `ocr_blocks.json` on document rewrite.
- Verification:
  - `.venv/bin/pytest tests/test_layout_sidecar_contract.py tests/test_robustness.py::TestPDFParse::test_local_ocr_uses_isolated_worker tests/test_robustness.py::TestPDFParse::test_local_ocr_worker_crash_does_not_crash_parent -q`: 12 passed.
  - `.venv/bin/pytest tests/test_schema_consistency.py tests/test_library_endpoints.py -q`: 48 passed.
  - `.venv/bin/pytest tests/test_word_embedded_images.py -q`: 6 passed.
  - `.venv/bin/pytest`: 221 passed, 15 skipped.
