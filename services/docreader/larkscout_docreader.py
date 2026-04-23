#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markitdown[pdf,docx,pptx,xlsx]", "pymupdf", "google-genai", "Pillow", "fastapi", "uvicorn", "python-multipart"]
# ///

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from i18n import init_locale, prompt, t, tmpl

init_locale()

logger = logging.getLogger("larkscout_docreader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# ═══════════════════════════════════════════
# Config
# ═══════════════════════════════════════════
MAX_PARSE_ROWS = int(os.environ.get("LARKSCOUT_MAX_PARSE_ROWS", "100000"))
_MAX_CONCURRENT_PARSE = int(os.environ.get("LARKSCOUT_MAX_CONCURRENT_PARSE", "5"))
_parse_sem = asyncio.Semaphore(_MAX_CONCURRENT_PARSE)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv", ".html", ".htm"}

# Lazy-initialized MarkItDown converter
_md_converter = None
_md_converter_lock = threading.Lock()


def _get_converter():
    """Return a lazily-initialized MarkItDown converter (thread-safe)."""
    global _md_converter
    if _md_converter is None:
        with _md_converter_lock:
            if _md_converter is None:
                from markitdown import MarkItDown

                _md_converter = MarkItDown()
    return _md_converter


def _convert_to_markdown(filepath: Path) -> str:
    """Convert a document to Markdown text via MarkItDown."""
    try:
        result = _get_converter().convert(str(filepath))
        return result.text_content or ""
    except Exception as e:
        raise RuntimeError(t("file_open_failed", path=str(filepath))) from e


def _count_markdown_tables(text: str) -> int:
    """Count distinct Markdown tables by counting separator rows (| --- | --- |)."""
    return len(re.findall(r"^\|[\s\-:|]+\|$", text, re.MULTILINE))


# ═══════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════


@dataclass
class PageContent:
    """Single page content."""

    page_num: int
    text: str
    is_ocr: bool = False
    tables: list[str] = field(default_factory=list)


@dataclass
class Section:
    """Document section."""

    index: int
    title: str
    level: int  # heading level 1-3
    text: str
    page_range: str  # "p.5-12"
    summary: str = ""
    sid: str = ""  # stable ID


@dataclass
class ParsedDocument:
    """Parsed document result."""

    filename: str
    file_type: str  # "pdf" | "docx"
    total_pages: int
    pages: list[PageContent]
    sections: list[Section]
    ocr_page_count: int = 0
    table_count: int = 0
    metadata: dict = field(default_factory=dict)


# ═══════════════════════════════════════════
# LLM provider wrapper
# ═══════════════════════════════════════════


def gemini_ocr(image_bytes: bytes, page_num: int) -> str:
    """OCR a single page image via the active LLM provider."""
    from providers import get_provider

    try:
        return get_provider().ocr(image_bytes, page_num)
    except Exception as exc:
        logger.warning("OCR unavailable for page %d: %s", page_num, exc)
        return t("ocr_failed", page=page_num)


def gemini_summarize(text: str, summarize_prompt: str, max_retries: int = 2) -> str:
    """Generate summary via the active LLM provider."""
    from providers import get_provider

    return get_provider().summarize(text, summarize_prompt, max_retries=max_retries)


# ═══════════════════════════════════════════
# Token estimation
# ═══════════════════════════════════════════


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. CJK ~2.5 chars/tok, Latin ~4 chars/tok."""
    if not text:
        return 0
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ratio = cjk_count / max(len(text), 1)
    chars_per_token = 2.5 * ratio + 4.0 * (1 - ratio)
    return int(len(text) / chars_per_token)


# ═══════════════════════════════════════════
# Smart OCR detection
# ═══════════════════════════════════════════

OCR_THRESHOLD = 50


def _parse_page_range(spec: str, total_pages: int) -> set[int]:
    """Parse page range spec: "10-30" or "5,10-15,20"."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            start = max(1, int(a.strip()))
            end = min(total_pages, int(b.strip()))
            pages.update(range(start, end + 1))
        else:
            p = int(part.strip())
            if 1 <= p <= total_pages:
                pages.add(p)
    return pages


def _should_ocr(page, text: str, threshold: int) -> bool:
    """
    Multi-signal OCR detection:
      Signal 1: too little text
      Signal 2: page has images and text is sparse (scan indicator)
      Signal 3: low useful-character ratio (garbled or mostly whitespace)
    """
    if len(text) < threshold:
        return True
    try:
        images = page.get_images(full=False)
        if len(images) > 0 and len(text) < threshold * 3:
            return True
    except Exception:
        pass
    if len(text) > 0:
        useful = sum(1 for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fff")
        if useful / len(text) < 0.3 and len(text) < threshold * 5:
            return True
    return False


def _ocr_cache_path(doc_dir: Path, page_num: int) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"ocr_p{page_num:04d}.txt"


def _ocr_cache_key(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()[:16]


# ═══════════════════════════════════════════
# Section stable ID
# ═══════════════════════════════════════════


def _section_sid(title: str, text: str) -> str:
    raw = (title + text[:200]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ═══════════════════════════════════════════
# PDF parsing
# ═══════════════════════════════════════════


def parse_pdf(
    filepath: Path,
    force_ocr: bool = False,
    ocr_threshold: int = OCR_THRESHOLD,
    ocr_pages_spec: str | None = None,
    extract_tables: bool = True,
    max_tables_per_page: int = 3,
    concurrency: int = 3,
    cache_dir: Path | None = None,
) -> ParsedDocument:
    import fitz

    def _usable_page_text(raw_text: str, ocr_text: str | None) -> str:
        if not ocr_text:
            return raw_text
        if ocr_text.startswith("[OCR failed"):
            return raw_text or ocr_text
        return ocr_text

    logger.info(f"Parsing PDF: {filepath.name}")

    # Open with fitz for page count, TOC, and OCR rendering
    doc = fitz.open(str(filepath))
    total_pages = len(doc)
    logger.info(f"Total pages: {total_pages}")

    # PDF TOC (for section splitting)
    toc = doc.get_toc(simple=True)
    if toc:
        logger.info(f"PDF TOC detected: {len(toc)} entries")

    ocr_page_set: set[int] | None = None
    if ocr_pages_spec:
        ocr_page_set = _parse_page_range(ocr_pages_spec, total_pages)
        logger.info(f"OCR target pages: {sorted(ocr_page_set)}")

    # Determine which pages need OCR and collect per-page text for OCR decisions
    ocr_count = 0
    ocr_tasks: list[tuple[int, bytes]] = []
    ocr_results: dict[int, str] = {}
    page_texts: dict[int, str] = {}

    for i, page in enumerate(doc):
        page_num = i + 1
        text = page.get_text("text").strip()
        page_texts[page_num] = text

        # OCR decision
        if ocr_page_set is not None:
            need_ocr = page_num in ocr_page_set
        elif force_ocr:
            need_ocr = True
        else:
            need_ocr = _should_ocr(page, text, ocr_threshold)

        if need_ocr:
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")

            # OCR cache
            if cache_dir:
                cp = _ocr_cache_path(cache_dir, page_num)
                ck = _ocr_cache_key(img_bytes)
                ck_path = cp.with_suffix(f".{ck}.txt")
                if ck_path.exists():
                    ocr_results[page_num] = ck_path.read_text(encoding="utf-8")
                    logger.info(f"Page {page_num}/{total_pages}: OCR cache hit")
                    ocr_count += 1
                    continue

            ocr_tasks.append((page_num, img_bytes))
            ocr_count += 1

    doc.close()

    # Concurrent OCR
    if ocr_tasks:
        logger.info(f"Concurrent OCR: {len(ocr_tasks)} pages ({concurrency} workers)...")

        def _do_ocr(args):
            pn, img_b = args
            result = gemini_ocr(img_b, pn)
            return pn, img_b, result

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_do_ocr, task): task for task in ocr_tasks}
            for fut in as_completed(futures):
                pn, img_b, result = fut.result()
                ocr_results[pn] = result
                logger.info(f"Page {pn}/{total_pages}: OCR done")
                if cache_dir:
                    cp = _ocr_cache_path(cache_dir, pn)
                    ck = _ocr_cache_key(img_b)
                    ck_path = cp.with_suffix(f".{ck}.txt")
                    ck_path.write_text(result, encoding="utf-8")

    pages: list[PageContent] = []
    for page_num in range(1, total_pages + 1):
        raw_text = page_texts.get(page_num, "")
        page_text = raw_text
        if force_ocr or page_num in ocr_results:
            page_text = _usable_page_text(raw_text, ocr_results.get(page_num))
        pages.append(PageContent(page_num=page_num, text=page_text.strip()))

    if force_ocr and ocr_results:
        logger.info(f"Full OCR mode: {len(ocr_results)} pages")

    # Keep MarkItDown for PDF table counting, but derive page mapping from
    # per-page text extracted directly from the PDF.
    markdown_text = ""
    if extract_tables and not force_ocr:
        markdown_text = _convert_to_markdown(filepath)
        logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    # Section splitting: prefer TOC when available
    if toc:
        sections = _split_sections_from_toc(pages, toc)
    else:
        sections = _split_sections(pages)

    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    # Count tables in Markdown output
    table_count = _count_markdown_tables(markdown_text) if extract_tables and markdown_text else 0

    logger.info(
        f"Parse complete: {len(sections)} sections, {ocr_count} OCR pages, {table_count} tables"
    )

    return ParsedDocument(
        filename=filepath.name,
        file_type="pdf",
        total_pages=total_pages,
        pages=pages,
        sections=sections,
        ocr_page_count=ocr_count,
        table_count=table_count,
    )


# ═══════════════════════════════════════════
# Word parsing
# ═══════════════════════════════════════════


def parse_word(filepath: Path, extract_tables: bool = True) -> ParsedDocument:
    logger.info(f"Parsing Word: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    est_pages = max(1, len(markdown_text) // 3000)
    pages = [PageContent(page_num=1, text=markdown_text)]
    sections = _split_sections(pages)
    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    table_count = _count_markdown_tables(markdown_text) if extract_tables else 0

    logger.info(
        f"Parse complete: {len(sections)} sections, ~{est_pages} pages, {table_count} tables"
    )
    return ParsedDocument(
        filename=filepath.name,
        file_type="docx",
        total_pages=est_pages,
        pages=pages,
        sections=sections,
        table_count=table_count,
    )


# ═══════════════════════════════════════════
# XLSX parsing
# ═══════════════════════════════════════════


def parse_xlsx(filepath: Path) -> ParsedDocument:
    """Parse an XLSX workbook via MarkItDown."""
    logger.info(f"Parsing XLSX: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    # Split by sheet headers (MarkItDown uses "## Sheet: name" or similar)
    pages: list[PageContent] = []
    sections: list[Section] = []
    table_count = 0

    # Try to split by markdown headings for sheet-level sections
    sheet_blocks = re.split(r"^(##\s+.+)$", markdown_text, flags=re.MULTILINE)

    if len(sheet_blocks) > 1:
        idx = 0
        for i in range(1, len(sheet_blocks), 2):
            idx += 1
            title = sheet_blocks[i].lstrip("#").strip()
            text = sheet_blocks[i + 1].strip() if i + 1 < len(sheet_blocks) else ""
            if not text:
                continue
            page = PageContent(page_num=idx, text=text, tables=[text] if "| " in text else [])
            pages.append(page)
            if "| " in text:
                table_count += 1
            sid = _section_sid(title, text)
            sections.append(
                Section(
                    index=idx, title=title, level=1, text=text, page_range=f"sheet {idx}", sid=sid
                )
            )
    else:
        # Single block — treat as one section
        pages = [
            PageContent(
                page_num=1,
                text=markdown_text,
                tables=[markdown_text] if "| " in markdown_text else [],
            )
        ]
        if "| " in markdown_text:
            table_count = 1
        sid = _section_sid(filepath.stem, markdown_text)
        sections = (
            [
                Section(
                    index=1,
                    title=filepath.stem,
                    level=1,
                    text=markdown_text,
                    page_range="sheet 1",
                    sid=sid,
                )
            ]
            if markdown_text.strip()
            else []
        )

    # Size guard
    truncated = len(markdown_text) > MAX_PARSE_ROWS * 100  # rough char limit

    if truncated:
        logger.warning("XLSX output may be truncated (large file)")
    logger.info(f"XLSX parse complete: {len(sections)} sheets, {table_count} tables")
    result = ParsedDocument(
        filename=filepath.name,
        file_type="xlsx",
        total_pages=max(len(pages), 1),
        pages=pages,
        sections=sections,
        table_count=table_count,
    )
    if truncated:
        result.metadata["truncated"] = True
        result.metadata["max_rows"] = MAX_PARSE_ROWS
    return result


# ═══════════════════════════════════════════
# CSV parsing
# ═══════════════════════════════════════════


def parse_csv(filepath: Path) -> ParsedDocument:
    """Parse a CSV file via MarkItDown."""
    logger.info(f"Parsing CSV: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    stem = filepath.stem
    table_count = 1 if markdown_text.strip() else 0
    sid = _section_sid(stem, markdown_text)

    page = PageContent(
        page_num=1,
        text=markdown_text,
        tables=[markdown_text] if markdown_text.strip() else [],
    )
    section = Section(
        index=1,
        title=stem,
        level=1,
        text=markdown_text,
        page_range="sheet 1",
        sid=sid,
    )

    logger.info(f"CSV parse complete: {table_count} tables")
    return ParsedDocument(
        filename=filepath.name,
        file_type="csv",
        total_pages=1,
        pages=[page],
        sections=[section] if markdown_text.strip() else [],
        table_count=table_count,
    )


def parse_generic(filepath: Path) -> ParsedDocument:
    """Parse any MarkItDown-supported format (PPTX, HTML, etc.)."""
    ext = filepath.suffix.lower()
    file_type = ext.lstrip(".")
    logger.info(f"Parsing {file_type.upper()}: {filepath.name}")
    markdown_text = _convert_to_markdown(filepath)
    logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")

    est_pages = max(1, len(markdown_text) // 3000)
    pages = [PageContent(page_num=1, text=markdown_text)]
    sections = _split_sections(pages)
    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    table_count = _count_markdown_tables(markdown_text)

    logger.info(f"Parse complete: {len(sections)} sections, ~{est_pages} pages")
    return ParsedDocument(
        filename=filepath.name,
        file_type=file_type,
        total_pages=est_pages,
        pages=pages,
        sections=sections,
        table_count=table_count,
    )


# ═══════════════════════════════════════════
# Section splitting
# ═══════════════════════════════════════════

HEADING_PATTERNS = [
    re.compile(r"^第[一二三四五六七八九十\d]+[章节部分篇]\s*[、:：]?\s*.+"),
    re.compile(r"^[（(]?[一二三四五六七八九十]+[）)]?[、.．]\s*.+"),
    re.compile(r"^\d+(\.\d+)*[.、．)\s]\s*.{2,}"),
    re.compile(r"^(?=.{8,60}$)[A-Z][A-Za-z0-9/&()'-]*(?: [A-Z][A-Za-z0-9/&()'-]*){0,5}$"),
    re.compile(r"^[A-Z][A-Z\s]{5,}$"),
    re.compile(r"^(摘要|目录|引言|绪论|前言|导论|背景|概述|总结|结论|致谢|参考文献|附录|附件)$"),
]


def _is_heading(text: str) -> int:
    text = text.strip()
    if not text or len(text) > 100:
        return 0
    for i, pattern in enumerate(HEADING_PATTERNS):
        if pattern.match(text):
            return 1 if i < 2 else 2
    return 0


def _split_sections_from_toc(pages: list[PageContent], toc: list) -> list[Section]:
    """Split sections using PDF TOC."""
    if not toc or not pages:
        return _split_sections(pages)

    page_texts: dict[int, str] = {}
    for p in pages:
        t = p.text
        if p.tables:
            t += "\n\n" + "\n\n".join(p.tables)
        page_texts[p.page_num] = t

    max_page = max(p.page_num for p in pages)
    sections: list[Section] = []

    for i, (level, title, start_page) in enumerate(toc):
        end_page = toc[i + 1][2] - 1 if i + 1 < len(toc) else max_page
        end_page = max(end_page, start_page)
        text_parts = [page_texts[pn] for pn in range(start_page, end_page + 1) if pn in page_texts]
        text = "\n\n".join(text_parts).strip()
        if not text:
            continue
        sections.append(
            Section(
                index=len(sections) + 1,
                title=title.strip(),
                level=min(level, 3),
                text=text,
                page_range=f"p.{start_page}-{end_page}",
            )
        )

    if len(sections) < 2:
        logger.warning("PDF TOC produced too few sections, falling back to regex split")
        return _split_sections(pages)
    return sections


def _split_sections(pages: list[PageContent]) -> list[Section]:
    sections: list[Section] = []
    current_title = tmpl("default_section_title")
    current_level = 1
    current_lines: list[str] = []
    current_start_page = 1
    sec_index = 0

    for page in pages:
        page_has_body = False
        for line in page.text.split("\n"):
            line = line.strip()
            if not line:
                continue
            heading_level = _is_heading(line)
            if heading_level > 0 and current_lines:
                end_page = page.page_num if page_has_body else max(current_start_page, page.page_num - 1)
                sec_index += 1
                sections.append(
                    Section(
                        index=sec_index,
                        title=current_title,
                        level=current_level,
                        text="\n".join(current_lines),
                        page_range=f"p.{current_start_page}-{end_page}",
                    )
                )
                current_title = line
                current_level = heading_level
                current_lines = []
                current_start_page = page.page_num
            else:
                current_lines.append(line)
                page_has_body = True
        for table in page.tables:
            current_lines.append(f"\n{table}\n")
            page_has_body = True

    if current_lines:
        sec_index += 1
        last_page = pages[-1].page_num if pages else 1
        sections.append(
            Section(
                index=sec_index,
                title=current_title,
                level=current_level,
                text="\n".join(current_lines),
                page_range=f"p.{current_start_page}-{last_page}",
            )
        )

    if len(sections) == 1 and len(pages) > 1 and sections[0].page_range != "p.1-1":
        page_sections: list[Section] = []
        for page in pages:
            text_parts = [page.text.strip()] if page.text.strip() else []
            if page.tables:
                text_parts.extend(table.strip() for table in page.tables if table.strip())
            page_text = "\n\n".join(text_parts).strip()
            if not page_text:
                continue
            page_sections.append(
                Section(
                    index=len(page_sections) + 1,
                    title=f"Page {page.page_num}",
                    level=1,
                    text=page_text,
                    page_range=f"p.{page.page_num}-{page.page_num}",
                )
            )
        if page_sections:
            return page_sections

    if not sections:
        full_text = "\n\n".join(p.text for p in pages)
        sections.append(
            Section(
                index=1,
                title=tmpl("full_document_title"),
                level=1,
                text=full_text,
                page_range=f"p.1-{pages[-1].page_num if pages else 1}",
            )
        )
    return sections


# ═══════════════════════════════════════════
# Summary generation
# ═══════════════════════════════════════════

SUMMARY_MAX_CHARS = 500


def generate_summaries(
    parsed: ParsedDocument, concurrency: int = 3
) -> tuple[str, str, list[Section]]:
    logger.info("Generating summaries...")

    # Dynamic batching by token estimate
    BATCH_TOKEN_LIMIT = 10000
    batches: list[list[Section]] = []
    current_batch: list[Section] = []
    current_tokens = 0

    for sec in parsed.sections:
        sec_tokens = _estimate_tokens(sec.text) + _estimate_tokens(sec.title) + 20
        if current_tokens + sec_tokens > BATCH_TOKEN_LIMIT and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(sec)
        current_tokens += sec_tokens
    if current_batch:
        batches.append(current_batch)

    # Concurrent summarization
    if len(batches) > 1 and concurrency > 1:
        logger.info(f"{len(batches)} batches, {min(concurrency, len(batches))} workers")
        with ThreadPoolExecutor(max_workers=min(concurrency, len(batches))) as pool:
            futures = {pool.submit(_summarize_batch, batch): batch for batch in batches}
            for fut in as_completed(futures):
                fut.result()
    else:
        for batch in batches:
            _summarize_batch(batch)

    logger.info(f"{len(parsed.sections)} section summaries complete")

    # Brief input compression
    if len(parsed.sections) > 60:
        sections_overview = _compress_sections_for_brief(parsed.sections)
    else:
        sections_overview = "\n\n".join(
            f"## {sec.title} ({sec.page_range})\n{sec.summary[:SUMMARY_MAX_CHARS]}"
            for sec in parsed.sections
            if sec.summary
        )

    brief = gemini_summarize(
        f"Document: {parsed.filename}\nTotal pages: {parsed.total_pages}\n\n{sections_overview}",
        prompt("brief"),
    )
    logger.info("Brief generation complete")

    digest = gemini_summarize(
        f"Document: {parsed.filename}\n\nBriefing:\n{brief}",
        prompt("digest"),
    )
    logger.info("Digest generation complete")

    return digest, brief, parsed.sections


def _summarize_batch(sections: list[Section]):
    """Batch summarize with JSON output + single fallback."""
    n = len(sections)

    if n == 1:
        sec = sections[0]
        sec.summary = gemini_summarize(
            f"## {sec.title} ({sec.page_range})\n\n{sec.text}",
            prompt("section_summary"),
        )
        logger.info(f"Section {sec.index}: {sec.title[:30]}... done")
        return

    batch_text = ""
    for sec in sections:
        batch_text += f"\n\n## Section {sec.index}: {sec.title} ({sec.page_range})\n\n{sec.text}"

    result = gemini_summarize(batch_text, prompt("batch_summary", n=n))

    # JSON parse
    parsed_ok = False
    try:
        clean = result.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        items = json.loads(clean)
        if isinstance(items, list) and len(items) >= n:
            for sec in sections:
                match = next((it for it in items if it.get("index") == sec.index), None)
                if match and match.get("summary"):
                    sec.summary = match["summary"]
                else:
                    sec.summary = t("summary_missing")
            parsed_ok = True
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    if parsed_ok:
        for sec in sections:
            logger.info(f"Section {sec.index}: {sec.title[:30]}... done")
        return

    # Fallback
    logger.warning(f"Batch JSON parse failed, falling back to single ({n} items)")
    for sec in sections:
        sec.summary = gemini_summarize(
            f"## {sec.title} ({sec.page_range})\n\n{sec.text}",
            prompt("section_summary"),
        )
        logger.info(f"Section {sec.index}: {sec.title[:30]}... done (single)")


def _compress_sections_for_brief(sections: list[Section]) -> str:
    groups = []
    for i in range(0, len(sections), 10):
        group = sections[i : i + 10]
        group_text = "; ".join(f"{s.title}: {s.summary[:150]}" for s in group if s.summary)
        groups.append(f"**Sections {group[0].index}-{group[-1].index}**: {group_text}")
    return "\n\n".join(groups)


# ═══════════════════════════════════════════
# Output file writing
# ═══════════════════════════════════════════


def write_output(
    doc_id: str,
    parsed: ParsedDocument,
    digest: str,
    brief: str,
    output_dir: Path,
    tags: list[str] | None = None,
    source: str = "upload",
    original_path: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
):
    doc_dir = output_dir / doc_id
    sections_dir = doc_dir / "sections"
    tables_dir = doc_dir / "tables"
    doc_dir.mkdir(parents=True, exist_ok=True)
    sections_dir.mkdir(exist_ok=True)

    meta = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "ocr_page_count": parsed.ocr_page_count,
        "table_count": parsed.table_count,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "sections": [
            {
                "index": sec.index,
                "sid": sec.sid,
                "title": sec.title,
                "page_range": sec.page_range,
                "page_start": _page_bounds(sec.page_range)[0],
                "page_end": _page_bounds(sec.page_range)[1],
                "char_count": len(sec.text),
            }
            for sec in parsed.sections
        ],
    }
    _write_json(doc_dir / ".meta.json", meta)
    logger.info(".meta.json written")

    _write_text(doc_dir / "digest.md", f"# {doc_id}: {parsed.filename}\n\n{digest}\n")
    logger.info("digest.md written")

    brief_header = tmpl(
        "brief_header",
        doc_id=doc_id,
        filename=parsed.filename,
        pages=parsed.total_pages,
        sections=len(parsed.sections),
        ocr_pages=parsed.ocr_page_count,
    )
    _write_text(doc_dir / "brief.md", brief_header + brief + "\n")
    logger.info("brief.md written")

    full_parts = [
        f"{'#' * min(sec.level + 1, 4)} {sec.title}\n\n{sec.text}" for sec in parsed.sections
    ]
    _write_text(
        doc_dir / "full.md", f"# {parsed.filename}\n\n" + "\n\n---\n\n".join(full_parts) + "\n"
    )
    logger.info("full.md written")

    for sec in parsed.sections:
        sec_filename = f"{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md"
        sec_content = tmpl(
            "section_header",
            title=sec.title,
            index=sec.index,
            sid=sec.sid,
            page_range=sec.page_range,
        )
        if sec.summary:
            sec_content += tmpl("section_summary_line", summary=sec.summary)
        sec_content += sec.text + "\n"
        _write_text(sections_dir / sec_filename, sec_content)
    logger.info(f"sections/ ({len(parsed.sections)} files)")

    all_tables = [(p.page_num, t) for p in parsed.pages for t in p.tables]
    if all_tables:
        tables_dir.mkdir(exist_ok=True)
        for i, (page_num, table_md) in enumerate(all_tables, 1):
            _write_text(
                tables_dir / f"table-{i:02d}.md", f"# Table {i} (page {page_num})\n\n{table_md}\n"
            )
        logger.info(f"tables/ ({len(all_tables)} files)")

    # v3: content_hash
    full_text = "\n".join(sec.text for sec in parsed.sections)
    content_hash = (
        "sha256:" + hashlib.sha256(full_text.encode("utf-8", errors="ignore")).hexdigest()
    )

    # manifest.json + v3 provenance
    manifest = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "source": source,
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "paths": {
            "digest": "digest.md",
            "brief": "brief.md",
            "full": "full.md",
            "sections_dir": "sections/",
        },
        "sections": [
            _build_section_entry(
                sec,
                summary_preview=(sec.summary[:120] + "...") if len(sec.summary) > 120 else sec.summary,
            )
            for sec in parsed.sections
        ],
        "provenance": {
            "source": source,
            "source_url": original_path or str(parsed.filename),
            "created_at": meta["created_at"],
            "content_hash": content_hash,
            "source_kind": (source_record or {}).get("kind", ""),
            "source_filename": (source_record or {}).get("filename", ""),
            "source_ref": (source_record or {}).get("ref", ""),
            "source_sha256": (source_record or {}).get("sha256", ""),
            "source_size_bytes": (source_record or {}).get("size_bytes", 0),
        },
    }
    _write_json(doc_dir / "manifest.json", manifest)
    logger.info("manifest.json written")

    _update_doc_index(
        output_dir,
        meta,
        digest,
        tags=tags,
        source=source,
        source_url=original_path or str(parsed.filename),
        content_hash=content_hash,
        metadata=metadata,
        source_record=source_record,
    )


def write_output_extract_only(
    doc_id: str,
    parsed: ParsedDocument,
    output_dir: Path,
    tags: list[str] | None = None,
    source: str = "upload",
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
):
    doc_dir = output_dir / doc_id
    sections_dir = doc_dir / "sections"
    doc_dir.mkdir(parents=True, exist_ok=True)
    sections_dir.mkdir(exist_ok=True)

    meta = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "total_pages": parsed.total_pages,
        "section_count": len(parsed.sections),
        "ocr_page_count": parsed.ocr_page_count,
        "table_count": parsed.table_count,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "sections": [
            {
                "index": sec.index,
                "sid": sec.sid,
                "title": sec.title,
                "page_range": sec.page_range,
                "page_start": _page_bounds(sec.page_range)[0],
                "page_end": _page_bounds(sec.page_range)[1],
                "char_count": len(sec.text),
            }
            for sec in parsed.sections
        ],
    }
    _write_json(doc_dir / ".meta.json", meta)

    full_parts = [
        f"{'#' * min(sec.level + 1, 4)} {sec.title}\n\n{sec.text}" for sec in parsed.sections
    ]
    _write_text(
        doc_dir / "full.md", f"# {parsed.filename}\n\n" + "\n\n---\n\n".join(full_parts) + "\n"
    )

    for sec in parsed.sections:
        fn = f"{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md"
        _write_text(sections_dir / fn, f"# {sec.title}\n\n{sec.text}\n")

    _write_text(
        doc_dir / "digest.md",
        f"{tmpl('digest_title', doc_id=doc_id, filename=parsed.filename)}\n\n{t('summary_pending')}\n",
    )
    _write_text(
        doc_dir / "brief.md",
        f"{tmpl('digest_title', doc_id=doc_id, filename=parsed.filename)}\n\n{t('summary_pending')}\n",
    )

    full_text = "\n".join(sec.text for sec in parsed.sections)
    content_hash = (
        "sha256:" + hashlib.sha256(full_text.encode("utf-8", errors="ignore")).hexdigest()
    )

    manifest = {
        "doc_id": doc_id,
        "filename": parsed.filename,
        "file_type": parsed.file_type,
        "source": source,
        "metadata": metadata or {},
        "parse_metadata": parsed.metadata or {},
        "source_file": source_record or {},
        "paths": {
            "digest": "digest.md",
            "brief": "brief.md",
            "full": "full.md",
            "sections_dir": "sections/",
        },
        "sections": [
            _build_section_entry(sec, summary_preview="")
            for sec in parsed.sections
        ],
        "provenance": {
            "source": source,
            "source_url": str(parsed.filename),
            "created_at": meta["created_at"],
            "content_hash": content_hash,
            "source_kind": (source_record or {}).get("kind", ""),
            "source_filename": (source_record or {}).get("filename", ""),
            "source_ref": (source_record or {}).get("ref", ""),
            "source_sha256": (source_record or {}).get("sha256", ""),
            "source_size_bytes": (source_record or {}).get("size_bytes", 0),
        },
    }
    _write_json(doc_dir / "manifest.json", manifest)
    _update_doc_index(
        output_dir,
        meta,
        t("summary_pending"),
        tags=tags,
        source=source,
        source_url=str(parsed.filename),
        content_hash=content_hash,
        metadata=metadata,
        source_record=source_record,
    )
    logger.info(f"Text extraction complete (no summary): {doc_dir}")


# ═══════════════════════════════════════════
# Document index
# ═══════════════════════════════════════════


def _update_doc_index(
    docs_dir: Path,
    meta: dict,
    digest: str,
    tags: list[str] | None = None,
    source: str = "upload",
    source_url: str | None = None,
    content_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_record: dict[str, Any] | None = None,
):
    """Update doc-index.json with threading lock and atomic write."""
    with _doc_index_lock:
        index_path = docs_dir / "doc-index.json"
        if index_path.exists():
            try:
                with open(index_path, encoding="utf-8") as f:
                    index = json.load(f)
            except (json.JSONDecodeError, Exception):
                index = {"version": 2, "documents": []}
        else:
            index = {"version": 2, "documents": []}

        index["version"] = 2
        if not isinstance(index.get("documents"), list):
            index["documents"] = []
        index["documents"] = [d for d in index["documents"] if d.get("id") != meta["doc_id"]]

        entry: dict[str, Any] = {
            "id": meta["doc_id"],
            "filename": meta["filename"],
            "file_type": meta["file_type"],
            "source": source,
            "source_url": source_url or "",
            "pages": meta["total_pages"],
            "sections": meta["section_count"],
            "ocr_pages": meta.get("ocr_page_count", 0),
            "tables": meta.get("table_count", 0),
            "digest": digest[:200],
            "digest_path": f"docs/{meta['doc_id']}/digest.md",
            "tags": tags or [],
            "created_at": meta["created_at"],
            "content_hash": content_hash or "",
            "metadata": _indexable_metadata(metadata or meta.get("metadata") or {}),
            "source_ref": (source_record or meta.get("source_file") or {}).get("ref", ""),
            "source_filename": (source_record or meta.get("source_file") or {}).get("filename", ""),
            "source_sha256": (source_record or meta.get("source_file") or {}).get("sha256", ""),
            "source_available": bool((source_record or meta.get("source_file") or {}).get("ref")),
        }

        index["documents"].append(entry)
        index["last_updated"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_json(index_path, index)


# ═══════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════


def _write_text(path: Path, content: str):
    """Write text atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _write_json(path: Path, data: dict):
    """Write JSON atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_bytes(path: Path, content: bytes):
    """Write bytes atomically via temp file + os.replace."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)


def _safe_filename(title: str, max_len: int = 40) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    safe = safe.strip().replace(" ", "-")
    return (safe[:max_len] if len(safe) > max_len else safe) or "untitled"


_doc_counter_lock = threading.Lock()
_doc_index_lock = threading.Lock()


def _next_doc_id(docs_dir: Path) -> str:
    with _doc_counter_lock:
        counter_path = docs_dir / ".counter"
        if counter_path.exists():
            try:
                counter = int(counter_path.read_text(encoding="utf-8").strip())
            except ValueError:
                counter = 1
        else:
            counter = 1
        doc_id = f"DOC-{counter:03d}"
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = counter_path.with_suffix(".tmp")
        tmp.write_text(str(counter + 1), encoding="utf-8")
        os.replace(tmp, counter_path)
        return doc_id


# ═══════════════════════════════════════════
# HTTP API（FastAPI）
# ═══════════════════════════════════════════

DEFAULT_DOCS_DIR = Path(
    os.environ.get(
        "LARKSCOUT_DOCS_DIR",
        os.path.expanduser("~/.larkscout/docs"),
    )
)

MAX_UPLOAD_BYTES = int(os.environ.get("LARKSCOUT_MAX_UPLOAD_MB", "200")) * 1024 * 1024
STORE_SOURCE_FILES = os.environ.get("LARKSCOUT_STORE_SOURCE_FILES", "true").lower() not in {
    "0",
    "false",
    "no",
}

_DOC_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,78}[A-Za-z0-9])?$")
_TABLE_ID_RE = re.compile(r"^(table-)?\d+$")


def _validate_doc_id(doc_id: str) -> None:
    """Reject doc_id values that could cause path traversal."""
    if not _DOC_ID_RE.match(doc_id):
        raise HTTPException(400, f"invalid doc_id: {doc_id!r}")


def _validate_table_id(table_id: str) -> None:
    """Reject table_id values that could cause path traversal."""
    if not _TABLE_ID_RE.match(table_id):
        raise HTTPException(400, f"invalid table_id: {table_id!r}")


def _get_docs_dir() -> Path:
    d = DEFAULT_DOCS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_id_strategy() -> str:
    strategy = os.environ.get("LARKSCOUT_DOC_ID_STRATEGY", "counter").strip().lower()
    return strategy if strategy in {"counter", "source_filename"} else "counter"


def _sanitize_doc_id_candidate(value: str, max_len: int = 80) -> str:
    base = Path(value).name.strip()
    stem = Path(base).stem if Path(base).suffix else base
    normalized = re.sub(r"[\s._]+", "-", stem)
    sanitized = re.sub(r"[^A-Za-z0-9-]+", "", normalized)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return sanitized[:max_len]


def _next_filename_doc_id(docs_dir: Path, filename: str) -> str | None:
    base = _sanitize_doc_id_candidate(filename)
    if not base:
        return None
    candidate = base
    suffix = 2
    while (docs_dir / candidate).exists():
        numbered = f"{base}-{suffix}"
        candidate = numbered[:80].rstrip("-")
        suffix += 1
    return candidate if _DOC_ID_RE.match(candidate) else None


def _resolve_doc_id(docs_dir: Path, filename: str, requested_doc_id: str | None) -> str:
    if requested_doc_id:
        _validate_doc_id(requested_doc_id)
        return requested_doc_id

    if _doc_id_strategy() == "source_filename":
        filename_doc_id = _next_filename_doc_id(docs_dir, filename)
        if filename_doc_id:
            return filename_doc_id

    return _next_doc_id(docs_dir)


# ---- Pydantic Models ----


class ParseResponse(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    total_pages: int
    section_count: int
    table_count: int
    ocr_page_count: int
    digest: str
    manifest_path: str
    processing_time_sec: float
    source_ref: str | None = None


class SectionInfo(BaseModel):
    sid: str
    index: int
    title: str
    page_range: str
    char_count: int
    summary_preview: str = ""


class ManifestResponse(BaseModel):
    doc_id: str
    filename: str
    file_type: str | None = None
    source: str | None = None
    paths: dict[str, str]
    sections: list[dict[str, Any]]
    provenance: dict[str, Any] | None = None


class SearchResult(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    digest: str
    tags: list[str] = []
    source: str = "upload"
    created_at: str | None = None
    score: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_ref: str | None = None
    source_filename: str | None = None
    source_available: bool = False
    sid: str | None = None
    section_title: str | None = None
    page_range: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    snippet: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int


# ---- FastAPI app ----

app = FastAPI(title="Doc Reader API", version="3.0.0")


def _parse_metadata_form(metadata: str | None) -> dict[str, Any]:
    if not metadata:
        return {}
    try:
        value = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"metadata must be a JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "metadata must be a JSON object")
    return value


def _indexable_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only shallow scalar metadata in doc-index for cheap filtering."""
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            out[key] = raw
        elif isinstance(raw, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None for item in raw
        ):
            out[key] = raw[:20]
    return out


def _metadata_value_matches(actual: Any, expected: str) -> bool:
    expected_lower = expected.lower()
    if isinstance(actual, list):
        return any(_metadata_value_matches(item, expected) for item in actual)
    if actual is None:
        return expected_lower in {"", "null", "none"}
    return str(actual).lower() == expected_lower


def _metadata_filters_from_request(request: Request) -> dict[str, str]:
    filters: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("metadata."):
            meta_key = key.split(".", 1)[1].strip()
            if meta_key:
                filters[meta_key] = value
    return filters


def _matches_metadata_filters(metadata: dict[str, Any], filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        if not _metadata_value_matches(metadata.get(key), expected):
            return False
    return True


def _page_bounds(page_range: str | None) -> tuple[int | None, int | None]:
    if not page_range:
        return None, None
    cleaned = page_range.strip()
    m = re.fullmatch(r"(?:p\.)?(\d+)(?:-(\d+))?", cleaned)
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2) or m.group(1))
    return start, end


def _build_section_entry(sec: Section, summary_preview: str = "") -> dict[str, Any]:
    page_start, page_end = _page_bounds(sec.page_range)
    return {
        "sid": sec.sid,
        "index": sec.index,
        "title": sec.title,
        "page_range": sec.page_range,
        "page_start": page_start,
        "page_end": page_end,
        "char_count": len(sec.text),
        "type": "text",
        "summary_preview": summary_preview,
        "file": f"sections/{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md",
    }


def _safe_source_filename(filename: str) -> str:
    base = Path(filename).name or "source.bin"
    suffix = Path(base).suffix
    stem = base[: -len(suffix)] if suffix else base
    safe_stem = _safe_filename(stem, max_len=80)
    return f"{safe_stem}{suffix}" if suffix else safe_stem


def _persist_source_file(doc_dir: Path, filename: str, content: bytes) -> dict[str, Any]:
    source_dir = doc_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_source_filename(filename)
    target = source_dir / safe_name
    _write_bytes(target, content)
    return {
        "kind": "upload",
        "filename": filename,
        "stored_filename": safe_name,
        "ref": f"source/{safe_name}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _load_doc_index(docs_dir: Path) -> list[dict[str, Any]]:
    index_path = docs_dir / "doc-index.json"
    if not index_path.exists():
        return []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    documents = index.get("documents", [])
    return documents if isinstance(documents, list) else []


def _doc_entry_from_manifest(docs_dir: Path, doc_id: str) -> dict[str, Any] | None:
    doc_dir = docs_dir / doc_id
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(manifest, dict):
        return None

    meta: dict[str, Any] = {}
    meta_path = doc_dir / ".meta.json"
    if meta_path.exists():
        try:
            raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw_meta, dict):
                meta = raw_meta
        except Exception:
            meta = {}

    source_file = manifest.get("source_file") or meta.get("source_file") or {}
    provenance = manifest.get("provenance") or {}
    sections = manifest.get("sections") if isinstance(manifest.get("sections"), list) else []
    digest = ""
    digest_path = doc_dir / "digest.md"
    if digest_path.exists():
        try:
            digest = digest_path.read_text(encoding="utf-8")[:200]
        except Exception:
            digest = ""

    return {
        "id": doc_id,
        "filename": manifest.get("filename") or meta.get("filename") or "",
        "file_type": manifest.get("file_type") or meta.get("file_type") or "",
        "source": manifest.get("source") or provenance.get("source") or "upload",
        "source_url": provenance.get("source_url") or "",
        "pages": meta.get("total_pages", 0),
        "sections": len(sections),
        "ocr_pages": meta.get("ocr_page_count", 0),
        "tables": meta.get("table_count", 0),
        "digest": digest,
        "digest_path": f"docs/{doc_id}/digest.md",
        "tags": meta.get("tags", []),
        "created_at": provenance.get("created_at") or meta.get("created_at"),
        "content_hash": provenance.get("content_hash") or "",
        "metadata": _indexable_metadata(manifest.get("metadata") or meta.get("metadata") or {}),
        "source_ref": source_file.get("ref", ""),
        "source_filename": source_file.get("filename", ""),
        "source_sha256": source_file.get("sha256", ""),
        "source_available": bool(source_file.get("ref")),
    }


def _filter_documents(
    documents: list[dict[str, Any]],
    *,
    file_type: str | None = None,
    tags: str | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    filtered = documents
    if file_type:
        filtered = [d for d in filtered if d.get("file_type") == file_type]
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        filtered = [d for d in filtered if any(t in (d.get("tags") or []) for t in tag_list)]
    if metadata_filters:
        filtered = [
            d
            for d in filtered
            if _matches_metadata_filters(d.get("metadata") or {}, metadata_filters)
        ]
    return filtered


def _resolve_manifest_section_path(doc_dir: Path, rel_path: str) -> Path | None:
    if not isinstance(rel_path, str):
        return None
    raw_path = Path(rel_path)
    if raw_path.is_absolute() or raw_path.suffix != ".md":
        return None
    sections_dir = (doc_dir / "sections").resolve()
    section_path = (doc_dir / raw_path).resolve()
    try:
        section_path.relative_to(sections_dir)
    except ValueError:
        return None
    return section_path


def _make_snippet(text: str, query: str, radius: int = 90) -> str:
    haystack = text.strip()
    if not haystack:
        return ""
    idx = haystack.lower().find(query.lower())
    if idx == -1:
        return haystack[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(haystack), idx + len(query) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(haystack) else ""
    return prefix + haystack[start:end].strip() + suffix


def _search_score(*parts: tuple[bool, float]) -> float:
    return sum(weight for matched, weight in parts if matched)


def _mask_path(p: str | Path) -> str:
    """Replace home directory prefix with ~ to avoid exposing absolute paths."""
    s = str(p)
    home = os.path.expanduser("~")
    return s.replace(home, "~") if s.startswith(home) else s


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": "3.0.0",
        "docs_dir": _mask_path(_get_docs_dir()),
        "supported_formats": ["pdf", "docx", "pptx", "xlsx", "csv", "html"],
    }


@app.post("/parse", response_model=ParseResponse)
async def api_parse_doc(
    file: UploadFile = File(...),
    doc_id: str | None = Form(None),
    generate_summary: bool = Form(True),
    force_ocr: bool = Form(False),
    ocr_pages: str | None = Form(None),
    extract_tables: bool = Form(True),
    max_tables_per_page: int = Form(3),
    concurrency: int = Form(3),
    tags: str | None = Form(None),  # JSON array string: '["Q3","financial"]'
    metadata: str | None = Form(None),  # JSON object string
):
    """Parse uploaded document (PDF/DOCX), return structured result."""
    if _parse_sem.locked():
        raise HTTPException(429, "too many concurrent parse requests")
    async with _parse_sem:
        docs_dir = _get_docs_dir()
        t0 = time.time()
        parsed_metadata = _parse_metadata_form(metadata)

        # Check format
        filename = file.filename or "unknown"
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(422, t("unsupported_format", fmt=suffix))

        # Parse tags
        parsed_tags: list[str] = []
        if tags:
            try:
                parsed_tags = json.loads(tags)
            except json.JSONDecodeError:
                parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

        # Save temp file
        d_id = _resolve_doc_id(docs_dir, filename, doc_id)
        tmp_dir = docs_dir / d_id / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / filename
        content = b""
        try:
            content = await file.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    413, f"file too large: {len(content)} bytes (max {MAX_UPLOAD_BYTES})"
                )
            tmp_path.write_bytes(content)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, t("file_save_failed", err=str(e)))

        # Parse
        try:
            loop = asyncio.get_event_loop()
            if suffix == ".pdf":
                parsed = await loop.run_in_executor(
                    None,
                    lambda: parse_pdf(
                        tmp_path,
                        force_ocr=force_ocr,
                        ocr_threshold=OCR_THRESHOLD,
                        ocr_pages_spec=ocr_pages,
                        extract_tables=extract_tables,
                        max_tables_per_page=max_tables_per_page,
                        concurrency=concurrency,
                        cache_dir=docs_dir / d_id,
                    ),
                )
            elif suffix == ".docx":
                parsed = await loop.run_in_executor(
                    None,
                    lambda: parse_word(
                        tmp_path,
                        extract_tables=extract_tables,
                    ),
                )
            elif suffix in (".xlsx", ".xls"):
                parsed = await loop.run_in_executor(None, lambda: parse_xlsx(tmp_path))
            elif suffix == ".csv":
                parsed = await loop.run_in_executor(None, lambda: parse_csv(tmp_path))
            else:  # .pptx, .html, .htm, etc.
                parsed = await loop.run_in_executor(None, lambda: parse_generic(tmp_path))
        except Exception as e:
            raise HTTPException(500, t("parse_failed", err=str(e)))
        finally:
            # Cleanup temp file
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        # Summarize + write
        digest = t("summary_pending")
        source_record = (
            _persist_source_file(docs_dir / d_id, filename, content) if STORE_SOURCE_FILES else {}
        )
        try:
            if generate_summary:
                digest_text, brief_text, _ = await loop.run_in_executor(
                    None, lambda: generate_summaries(parsed, concurrency=concurrency)
                )
                digest = digest_text
                await loop.run_in_executor(
                    None,
                    lambda: write_output(
                        d_id,
                        parsed,
                        digest_text,
                        brief_text,
                        docs_dir,
                        tags=parsed_tags,
                        source="upload",
                        original_path=str(filename),
                        metadata=parsed_metadata,
                        source_record=source_record,
                    ),
                )
            else:
                await loop.run_in_executor(
                    None,
                    lambda: write_output_extract_only(
                        d_id,
                        parsed,
                        docs_dir,
                        tags=parsed_tags,
                        source="upload",
                        metadata=parsed_metadata,
                        source_record=source_record,
                    ),
                )
        except Exception as e:
            raise HTTPException(500, t("write_failed", err=str(e)))

        elapsed = round(time.time() - t0, 2)
        return ParseResponse(
            doc_id=d_id,
            filename=parsed.filename,
            file_type=parsed.file_type,
            total_pages=parsed.total_pages,
            section_count=len(parsed.sections),
            table_count=parsed.table_count,
            ocr_page_count=parsed.ocr_page_count,
            digest=digest[:300],
            manifest_path=f"docs/{d_id}/manifest.json",
            processing_time_sec=elapsed,
            source_ref=source_record.get("ref"),
        )


# ---- Library query endpoints ----


@app.get("/library/search", response_model=SearchResponse)
async def library_search(
    request: Request,
    q: str | None = None,
    tags: str | None = None,
    file_type: str | None = None,
    limit: int = 20,
):
    """Search document library."""
    docs_dir = _get_docs_dir()
    metadata_filters = _metadata_filters_from_request(request)
    documents = _filter_documents(
        _load_doc_index(docs_dir),
        file_type=file_type,
        tags=tags,
        metadata_filters=metadata_filters,
    )

    if q:
        q_lower = q.lower()
        scored = []
        for d in documents:
            score = 0.0
            if q_lower in (d.get("filename") or "").lower():
                score += 2.0
            if q_lower in (d.get("digest") or "").lower():
                score += 1.0
            if q_lower in (d.get("source_filename") or "").lower():
                score += 1.0
            for tag in d.get("tags") or []:
                if q_lower in tag.lower():
                    score += 1.5
            for val in (d.get("metadata") or {}).values():
                if isinstance(val, list):
                    if any(q_lower in str(item).lower() for item in val):
                        score += 1.0
                elif q_lower in str(val).lower():
                    score += 1.0
            if score > 0:
                scored.append((d, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        documents = [d for d, _ in scored[:limit]]
        scores = {d.get("id"): s for d, s in scored[:limit]}
    else:
        documents = documents[:limit]
        scores = {}

    results = [
        SearchResult(
            doc_id=d.get("id", ""),
            filename=d.get("filename", ""),
            file_type=d.get("file_type", ""),
            digest=d.get("digest", ""),
            tags=d.get("tags", []),
            source=d.get("source", "upload"),
            created_at=d.get("created_at"),
            score=scores.get(d.get("id"), 1.0),
            metadata=d.get("metadata") or {},
            source_ref=d.get("source_ref") or None,
            source_filename=d.get("source_filename") or None,
            source_available=bool(d.get("source_available")),
        )
        for d in documents
    ]
    return SearchResponse(results=results, total=len(results))


@app.get("/library/search_text", response_model=SearchResponse)
async def library_search_text(
    request: Request,
    q: str,
    tags: str | None = None,
    file_type: str | None = None,
    doc_id: str | None = None,
    limit: int = 20,
    scope: str = "all",
):
    """Search full text and/or section text with snippets and page hints."""
    query = q.strip()
    if not query:
        raise HTTPException(422, "q is required")
    if doc_id:
        _validate_doc_id(doc_id)
    if scope not in {"all", "full", "section"}:
        raise HTTPException(422, "scope must be one of: all, full, section")

    docs_dir = _get_docs_dir()
    metadata_filters = _metadata_filters_from_request(request)
    documents = _filter_documents(
        _load_doc_index(docs_dir),
        file_type=file_type,
        tags=tags,
        metadata_filters=metadata_filters,
    )
    if doc_id:
        documents = [d for d in documents if d.get("id") == doc_id]
        if not documents:
            fallback_doc = _doc_entry_from_manifest(docs_dir, doc_id)
            if fallback_doc:
                documents = _filter_documents(
                    [fallback_doc],
                    file_type=file_type,
                    tags=tags,
                    metadata_filters=metadata_filters,
                )

    results: list[SearchResult] = []
    for d in documents:
        current_doc_id = d.get("id", "")
        if not isinstance(current_doc_id, str) or not _DOC_ID_RE.match(current_doc_id):
            continue
        doc_dir = docs_dir / current_doc_id
        manifest_path = doc_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if scope in {"all", "full"}:
            full_path = doc_dir / "full.md"
            if full_path.exists():
                full_text = full_path.read_text(encoding="utf-8")
                if query.lower() in full_text.lower():
                    results.append(
                        SearchResult(
                            doc_id=current_doc_id,
                            filename=d.get("filename", ""),
                            file_type=d.get("file_type", ""),
                            digest=d.get("digest", ""),
                            tags=d.get("tags", []),
                            source=d.get("source", "upload"),
                            created_at=d.get("created_at"),
                            score=_search_score((True, 1.0)),
                            metadata=d.get("metadata") or {},
                            source_ref=d.get("source_ref") or None,
                            source_filename=d.get("source_filename") or None,
                            source_available=bool(d.get("source_available")),
                            snippet=_make_snippet(full_text, query),
                        )
                    )

        if scope in {"all", "section"}:
            for sec in manifest.get("sections", []):
                rel_path = sec.get("file")
                if not rel_path:
                    continue
                section_path = _resolve_manifest_section_path(doc_dir, rel_path)
                if not section_path:
                    continue
                if not section_path.exists():
                    continue
                section_text = section_path.read_text(encoding="utf-8")
                title = sec.get("title", "")
                title_hit = query.lower() in title.lower()
                text_hit = query.lower() in section_text.lower()
                if not (title_hit or text_hit):
                    continue
                page_start = sec.get("page_start")
                page_end = sec.get("page_end")
                if page_start is None and page_end is None:
                    page_start, page_end = _page_bounds(sec.get("page_range"))
                results.append(
                    SearchResult(
                        doc_id=current_doc_id,
                        filename=d.get("filename", ""),
                        file_type=d.get("file_type", ""),
                        digest=d.get("digest", ""),
                        tags=d.get("tags", []),
                        source=d.get("source", "upload"),
                        created_at=d.get("created_at"),
                        score=_search_score((title_hit, 2.0), (text_hit, 1.5)),
                        metadata=d.get("metadata") or {},
                        source_ref=d.get("source_ref") or None,
                        source_filename=d.get("source_filename") or None,
                        source_available=bool(d.get("source_available")),
                        sid=sec.get("sid"),
                        section_title=title,
                        page_range=sec.get("page_range"),
                        page_start=page_start,
                        page_end=page_end,
                        snippet=_make_snippet(section_text if text_hit else title, query),
                    )
                )

    results.sort(key=lambda item: item.score, reverse=True)
    total = len(results)
    return SearchResponse(results=results[:limit], total=total)


@app.get("/library/{doc_id}/manifest")
async def get_manifest(doc_id: str):
    """Get document manifest."""
    _validate_doc_id(doc_id)
    p = _get_docs_dir() / doc_id / "manifest.json"
    if not p.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/library/{doc_id}/digest")
async def get_digest(doc_id: str):
    """Get document digest (lowest token cost)."""
    _validate_doc_id(doc_id)
    p = _get_docs_dir() / doc_id / "digest.md"
    if not p.exists():
        raise HTTPException(404, t("digest_not_found", doc_id=doc_id))
    return {"doc_id": doc_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/brief")
async def get_brief(doc_id: str):
    """Get document brief (medium token cost)."""
    _validate_doc_id(doc_id)
    p = _get_docs_dir() / doc_id / "brief.md"
    if not p.exists():
        raise HTTPException(404, t("brief_not_found", doc_id=doc_id))
    return {"doc_id": doc_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/full")
async def get_full(doc_id: str):
    """Get full document text (high token cost, use sparingly)."""
    _validate_doc_id(doc_id)
    p = _get_docs_dir() / doc_id / "full.md"
    if not p.exists():
        raise HTTPException(404, t("full_not_found", doc_id=doc_id))
    return {"doc_id": doc_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/section/{sid}")
async def get_section(doc_id: str, sid: str):
    """Read a single section by sid."""
    _validate_doc_id(doc_id)
    sections_dir = _get_docs_dir() / doc_id / "sections"
    if not sections_dir.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))

    # sid is in filename: 01-{sid}-{title}.md
    for f in sections_dir.iterdir():
        if f.is_file() and sid in f.name:
            return {"doc_id": doc_id, "sid": sid, "content": f.read_text(encoding="utf-8")}

    raise HTTPException(404, t("section_not_found", sid=sid))


@app.get("/library/{doc_id}/table/{table_id}")
async def get_table(doc_id: str, table_id: str):
    """Read a single table."""
    _validate_doc_id(doc_id)
    _validate_table_id(table_id)
    tables_dir = _get_docs_dir() / doc_id / "tables"
    if not tables_dir.exists():
        raise HTTPException(404, t("tables_dir_not_found", doc_id=doc_id))

    # table_id: "table-01" or "01"
    tid = table_id if table_id.startswith("table-") else f"table-{table_id}"
    p = tables_dir / f"{tid}.md"
    if not p.exists():
        raise HTTPException(404, t("table_not_found", table_id=table_id))
    return {"doc_id": doc_id, "table_id": table_id, "content": p.read_text(encoding="utf-8")}


@app.get("/library/{doc_id}/sections")
async def list_sections(doc_id: str):
    """List all sections from manifest."""
    _validate_doc_id(doc_id)
    manifest_path = _get_docs_dir() / doc_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "doc_id": doc_id,
        "sections": manifest.get("sections", []),
    }


# ═══════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8090"))

    DEFAULT_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"LarkScout DocReader API v3.0 starting: {host}:{port}")
    logger.info(f"Docs directory: {DEFAULT_DOCS_DIR}")

    uvicorn.run(app, host=host, port=port)
