---
name: larkscout-docreader
description: Long document parsing and reading HTTP API. Use when you need to read, analyze, or summarize Word (.docx) or PDF files. Supports file upload parsing, three-tier summaries (digest/brief/full), on-demand section loading, table extraction, and document library search via HTTP API. Outputs doc-index v2 format, sharing a unified index with larkscout-browser web capture results. Serves as the document parsing engine for the LarkScout open-source data collection platform.
triggers:
  - "read document"
  - "parse document"
  - "analyze this PDF"
  - "this Word file"
  - "document summary"
  - "extract content"
  - "cross-document"
  - "consolidate documents"
  - "upload document"
  - "document library search"
  - ".pdf"
  - ".docx"
  - ".xlsx"
  - ".csv"
  - ".pptx"
  - ".html"
---

# SKILL: LarkScout DocReader (Document Parsing HTTP API)

## 1. Purpose

Use for: document analysis, cross-document consolidation, research report extraction, financial data collection, contract review, meeting minutes processing.

---

## 2. Service Dependency

- Base URL: `http://127.0.0.1:9898/doc/`

---

## 3. Agent Execution Strategy (Low-Token Rules — Must Follow)

### 3.1 Three-Tier Loading Rules

| Tier | Endpoint                                     | Token Cost | When to Use                              |
| ---- | -------------------------------------------- | ---------- | ---------------------------------------- |
| L1   | `GET /doc/library/{doc_id}/digest`           | ~200       | When a document is mentioned; quick topic overview |
| L2   | `GET /doc/library/{doc_id}/brief`            | ~1500      | When you need key points per section     |
| L3   | `GET /doc/library/{doc_id}/section/{sid}`    | On-demand  | When you need the original text of a specific section |
| L4   | `GET /doc/library/{doc_id}/full`             | Full       | **Almost never used** — only in extreme cases |

**Never inject full text into context. Use section/{sid} to load specific sections on demand.**

### 3.2 Golden Workflow

```
POST /doc/parse (upload file)
↓
Returns doc_id + digest (summary already included — no extra call needed)
↓
Need more detail → GET /doc/library/{doc_id}/brief
↓
Need a section's original text → GET /doc/library/{doc_id}/sections (get section list)
                               → GET /doc/library/{doc_id}/section/{sid}
↓
Need table data → GET /doc/library/{doc_id}/table/{table_id}
```

### 3.3 Cross-Document Consolidation

When consolidating multiple documents:

1. Read the digest for all relevant documents (~200 tokens each)
2. Identify the dimensions needing cross-comparison
3. Load relevant sections from each document on demand
4. Synthesize analysis and produce a consolidated report

```
Total context cost:
  3 × digest         = ~600 tokens
  + 4 sections on-demand = ~4000 tokens
  ────────────────────────────────
  Total                ≈ 4600 tokens

vs. injecting 3 full documents:  ≈ 180,000 tokens
Savings: 97%
```

### 3.4 Document Library Search

```
GET /doc/library/search?q=revenue&tags=financial&file_type=pdf
↓
Returns matching doc_id list + digest previews
↓
Load specific documents' brief or section on demand
```

**Prohibited behaviors:**

- Requesting full directly (wastes tokens)
- Reading brief without checking digest first (assess need first)
- Iterating all documents without using search (use search)

---

## 4. API Reference

> All requests use `Content-Type: application/json` (query endpoints) or `multipart/form-data` (upload endpoints)

### 4.1 Health Check

- `GET /doc/health`

Response example:

```json
{
  "ok": true,
  "version": "3.0.0",
  "docs_dir": "~/.larkscout/docs",
  "supported_formats": ["pdf", "docx", "pptx", "xlsx", "csv", "html"]
}
```

Notes:
- `docs_dir` shows a masked path (`~` replaces the home directory) — this is intentional for security
- `supported_formats` includes `pptx`, `xlsx`, `csv`, and `html` in addition to `pdf` and `docx`
- Document parsing powered by [MarkItDown](https://github.com/microsoft/markitdown) (Microsoft)

### 4.2 Upload and Parse Document (Core)

- `POST /doc/parse`
- Content-Type: `multipart/form-data`

Request parameters:

| Parameter             | Type   | Default    | Description                                                                             |
| --------------------- | ------ | ---------- | --------------------------------------------------------------------------------------- |
| `file`                | File   | (required) | File to upload (.pdf, .docx, .pptx, .xlsx, .csv, .html)                                 |
| `doc_id`              | string | Auto-increment | Manually specify DOC-ID                                                             |
| `generate_summary`    | bool   | `true`     | Whether to generate summaries (false = extract text only)                               |
| `force_ocr`           | bool   | `false`    | Force OCR on all pages                                                                  |
| `ocr_pages`           | string | null       | OCR only specified page ranges, e.g. `"10-30"`                                          |
| `extract_tables`      | bool   | `true`     | Whether to extract tables                                                               |
| `max_tables_per_page` | int    | `3`        | Maximum tables to extract per page                                                      |
| `concurrency`         | int    | `3`        | OCR/summary concurrency                                                                 |
| `tags`                | string | null       | Tags — JSON array (`'["Q3","financial"]'`) or comma-separated (`"Q3,financial"`)        |
| `metadata`            | string | null       | Custom metadata (JSON object)                                                           |

Call example:

```bash
curl -X POST http://localhost:9898/doc/parse \
  -F "file=@report.pdf" \
  -F "generate_summary=true" \
  -F "extract_tables=true" \
  -F 'tags=["Q3","financial"]'
```

Response example:

```json
{
  "doc_id": "DOC-010",
  "filename": "report.pdf",
  "file_type": "pdf",
  "total_pages": 45,
  "section_count": 12,
  "table_count": 8,
  "ocr_page_count": 3,
  "digest": "Q3 revenue grew 15%, net profit up 23% YoY...",
  "manifest_path": "docs/DOC-010/manifest.json",
  "processing_time_sec": 23.5
}
```

**Key notes:**

- The returned `digest` field already contains the first 300 characters of the summary — Agent usually doesn't need an extra call to `/doc/library/{doc_id}/digest`
- `generate_summary=false` extracts text and tables only without calling LLM — faster but no summary
- Large files (100+ page PDFs) may take 30–60 seconds to parse — Agents should set a longer timeout

### 4.3 Search Document Library

- `GET /doc/library/search`

| Parameter   | Description                                         |
| ----------- | --------------------------------------------------- |
| `q`         | Keyword (searches filename, digest, tags)           |
| `tags`      | Tag filter, comma-separated                         |
| `file_type` | File type filter (`pdf` / `docx` / `web`)           |
| `limit`     | Maximum results (default 20)                        |

Response example:

```json
{
  "results": [
    {
      "doc_id": "DOC-010",
      "filename": "Q3-report.pdf",
      "file_type": "pdf",
      "digest": "Q3 revenue grew 15%...",
      "tags": ["Q3", "financial"],
      "source": "upload",
      "score": 3.5
    }
  ],
  "total": 1
}
```

**Search matches both documents uploaded via DocReader and web pages captured via LarkScout Browser.** The `source` field distinguishes origin: `"upload"` = file upload, `"web_capture"` = web capture.

### 4.4 Get Document Digest (Lowest Token Cost)

- `GET /doc/library/{doc_id}/digest`

Response: `{"doc_id": "DOC-010", "content": "# DOC-010: report.pdf\n\nQ3 revenue grew 15%..."}`

### 4.5 Get Document Brief (Medium Token Cost)

- `GET /doc/library/{doc_id}/brief`

Response: `{"doc_id": "DOC-010", "content": "# DOC-010: report.pdf · Brief\n\n..."}`

### 4.6 Get Document Full Text (High Token Cost — Use Sparingly)

- `GET /doc/library/{doc_id}/full`

Response: `{"doc_id": "DOC-010", "content": "# report.pdf\n\n..."}`

### 4.7 List Document Sections

- `GET /doc/library/{doc_id}/sections`

Response example:

```json
{
  "doc_id": "DOC-010",
  "sections": [
    {
      "sid": "a3f8e1b902cd",
      "index": 1,
      "title": "Executive Summary",
      "page_range": "p.1-3",
      "char_count": 2500,
      "summary_preview": "Q3 revenue grew 15%, net profit up 23% YoY..."
    },
    {
      "sid": "b7c2d4e5f612",
      "index": 2,
      "title": "Financial Analysis",
      "page_range": "p.4-15",
      "char_count": 12000,
      "summary_preview": "Revenue mix shifted, service revenue share rose to 42%..."
    }
  ]
}
```

**Agent should call this endpoint first to get the section list, then read specific sections by sid.**

### 4.8 Read Single Section

- `GET /doc/library/{doc_id}/section/{sid}`

Response: `{"doc_id": "DOC-010", "sid": "a3f8e1b902cd", "content": "# Executive Summary\n\n..."}`

### 4.9 Read Single Table

- `GET /doc/library/{doc_id}/table/{table_id}`

table_id format: `"01"` or `"table-01"`.

Response: `{"doc_id": "DOC-010", "table_id": "01", "content": "# Table 1 (Page 5)\n\n| ... |"}`

### 4.10 Get Manifest

- `GET /doc/library/{doc_id}/manifest`

Returns the full manifest.json contents, including document structure, section list, path information, and provenance.

---

## 5. Document Library Structure

All parsed results are stored under `DOCS_DIR`:

```text
docs/
  ├─ doc-index.json              ← Global index (v2 format, shared with LarkScout Browser)
  │
  ├─ DOC-001/                    ← PDF parsed results
  │   ├─ .meta.json
  │   ├─ manifest.json           ← Contains provenance tracking
  │   ├─ digest.md               ← ~200 tokens
  │   ├─ brief.md                ← ~1500 tokens
  │   ├─ full.md                 ← Full text
  │   ├─ sections/               ← Section slices
  │   │   ├─ 01-{sid}-{title}.md
  │   │   └─ 02-{sid}-{title}.md
  │   └─ tables/                 ← Extracted tables
  │       ├─ table-01.md
  │       └─ table-02.md
  │
  └─ WEB-001/                    ← Web capture results (written by LarkScout Browser, shared index)
      ├─ manifest.json
      ├─ digest.md
      ├─ sections/
      └─ tables/
```

**doc-index.json v2 Key Fields:**

| Field          | Description                                     |
| -------------- | ----------------------------------------------- |
| `id`           | DOC-001 / WEB-001                               |
| `source`       | `"upload"` or `"web_capture"`                   |
| `tags`         | Tag array                                       |
| `content_hash` | SHA256 of content, used for deduplication and change detection |
| `digest`       | First 200 characters of the summary             |

---

## 6. Agent Call Templates

### 6.1 Single Document Analysis

```
POST /doc/parse (upload file)
↓
Returns doc_id + digest → determine if document is relevant
↓
GET /doc/library/{doc_id}/brief → understand key points per section
↓
GET /doc/library/{doc_id}/section/{target_sid} → deep read key sections
```

### 6.2 Cross-Document Comparison

```
POST /doc/parse (Document A) → doc_id_a
POST /doc/parse (Document B) → doc_id_b
↓
GET /doc/library/{doc_id_a}/digest + GET /doc/library/{doc_id_b}/digest
↓
Compare digests, identify dimensions needing cross-comparison
↓
GET /doc/library/{doc_id_a}/section/{relevant_sid}
GET /doc/library/{doc_id_b}/section/{relevant_sid}
↓
Synthesize analysis and produce comparison report
```

### 6.3 Document Library Search

```
GET /doc/library/search?q=Q3+revenue&tags=financial
↓
Returns matching document list + digest previews
↓
Select target document → GET /doc/library/{doc_id}/brief
↓
Drill down as needed → GET /doc/library/{doc_id}/section/{sid}
```

### 6.4 Text-Only Extraction (No Summary Generation)

```
POST /doc/parse (generate_summary=false)
↓
Returns doc_id → text extracted, sections readable
↓
GET /doc/library/{doc_id}/sections → section list
GET /doc/library/{doc_id}/section/{sid} → read content
```

Use for: scenarios where the Agent performs its own analysis without needing LLM summaries, or to conserve Gemini API calls.

---

## 7. Common Errors and Solutions

| Error                                              | Cause                          | Solution                                                                   |
| -------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------- |
| `422 unsupported format`                           | Uploaded non-supported file    | Check file format (pdf, docx, pptx, xlsx, csv, html supported)            |
| `429 too many concurrent requests`                 | Rate limit exceeded            | Wait and retry — server limits concurrent parse operations                 |
| `404 document not found`                           | Invalid doc_id or unparsed doc | Use search to confirm doc_id first                                         |
| `404 section not found`                            | Invalid sid                    | Call `/doc/library/{doc_id}/sections` first to get valid sid list           |
| `500 parse failed`                                 | Corrupted or encrypted PDF     | Prompt user to check the file                                              |
| `500 RuntimeError: Please set GEMINI_API_KEY`      | API key not configured         | Set environment variable and restart service                               |
| Parsing takes too long                             | Large file + OCR               | Use `generate_summary=false` for fast extraction first, generate summary later |
| Table is empty                                     | Tables in PDF are images       | Use `force_ocr=true` — OCR will attempt to recognize tables in images      |
| XLSX/CSV truncated warning in metadata             | File exceeds MAX_PARSE_ROWS    | Normal — large spreadsheets are truncated for safety; check `metadata.truncated` |

---

## 8. Recommended Default Parameters

**Parsing:**

- `generate_summary=true` (when summaries are needed)
- `extract_tables=true`
- `max_tables_per_page=3`
- `concurrency=3` (adjust based on Gemini API quota)

**OCR:**

- Normal documents: Don't pass `force_ocr` — service auto-detects pages needing OCR
- Scanned documents: `force_ocr=true`
- Mixed documents: `ocr_pages="10-30"` (OCR only specified page ranges)

---

## 9. Security and Compliance

- Temporary copies of uploaded files are automatically cleaned up after parsing
- Document library is physically isolated by `DOCS_DIR` directory
- Provenance tracking: Each document's manifest contains provenance (upload time, content_hash, original path)
- Raw file content is not cached — only structured parsed text is retained
