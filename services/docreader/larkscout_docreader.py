#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["markitdown[pdf,docx,pptx,xlsx]", "pymupdf", "google-genai", "Pillow", "fastapi", "uvicorn", "python-multipart", "paddleocr", "paddlepaddle"]
# ///

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
_MAX_CONCURRENT_PARSE = int(os.environ.get("LARKSCOUT_MAX_CONCURRENT_PARSE", "2"))
_parse_sem = asyncio.Semaphore(_MAX_CONCURRENT_PARSE)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".csv", ".html", ".htm"}
DOCUMENT_PROFILE_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "document_profiles"
FIELD_OCR_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "field_profiles"

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
    tables_in_text: bool = False


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


@dataclass(frozen=True)
class FieldCrop:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class FieldGroup:
    id: str
    aliases: tuple[str, ...] = ()
    page_scope: tuple[int, ...] = ()
    crop: FieldCrop | None = None
    start_alias: str | None = None
    end_alias: str | None = None
    replace_mode: str = "block_between_aliases"


@dataclass(frozen=True)
class FieldRule:
    id: str
    aliases: tuple[str, ...] = ()
    pattern: str | None = None
    page_scope: tuple[int, ...] = ()


@dataclass(frozen=True)
class ClassificationPolicy:
    required_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityPolicy:
    sparse_text_chars: int = 40
    usable_text_chars: int = 120
    scan_page_ratio: float = 0.85
    mixed_page_ratio: float = 0.2


@dataclass(frozen=True)
class UpgradePolicy:
    default_mode: str = "accurate"
    local_ocr_backend: str = "paddleocr"
    region_llm_modes: tuple[str, ...] = ("accurate", "full")
    full_llm_modes: tuple[str, ...] = ("full",)
    proofread_modes: tuple[str, ...] = ("full",)


@dataclass(frozen=True)
class TablePolicy:
    prefer_markitdown: bool = True


@dataclass(frozen=True)
class CachePolicy:
    page_ocr: bool = True
    region_ocr: bool = True


@dataclass(frozen=True)
class ProcessingPolicy:
    large_file_threshold_mb: int = 50
    local_ocr_render_scale: float = 2.0
    llm_ocr_render_scale: float = 3.0
    max_local_ocr_pixels: int = 4_000_000
    max_llm_ocr_pixels: int = 8_000_000
    min_ocr_render_scale: float = 1.25


@dataclass(frozen=True)
class SummaryPolicy:
    default_mode: str = "sync"
    async_modes: tuple[str, ...] = ()
    sync_modes: tuple[str, ...] = ("full",)


@dataclass(frozen=True)
class DocumentProfile:
    name: str
    classification: ClassificationPolicy = ClassificationPolicy()
    quality_policy: QualityPolicy = QualityPolicy()
    upgrade_policy: UpgradePolicy = UpgradePolicy()
    table_policy: TablePolicy = TablePolicy()
    cache_policy: CachePolicy = CachePolicy()
    processing_policy: ProcessingPolicy = ProcessingPolicy()
    summary_policy: SummaryPolicy = SummaryPolicy()
    groups: tuple[FieldGroup, ...] = ()
    fields: tuple[FieldRule, ...] = ()


# ═══════════════════════════════════════════
# LLM provider wrapper
# ═══════════════════════════════════════════


def gemini_ocr(image_bytes: bytes, page_num: int, *, proofread: bool | None = None) -> str:
    """OCR a single page image via the active LLM provider."""
    from providers import get_provider

    try:
        return get_provider().ocr(image_bytes, page_num, proofread=proofread)
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
OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_OCR_RENDER_SCALE", "3.0"))
FIELD_OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_FIELD_OCR_RENDER_SCALE", "4.0"))
LOCAL_OCR_RENDER_SCALE = float(os.environ.get("LARKSCOUT_LOCAL_OCR_RENDER_SCALE", "2.0"))
LOCAL_OCR_CONCURRENCY = max(1, int(os.environ.get("LARKSCOUT_LOCAL_OCR_CONCURRENCY", "1")))
DEFERRED_SUMMARY_MAX_CONCURRENT = max(
    1,
    int(os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_MAX_CONCURRENT", "1")),
)
DEFERRED_SUMMARY_TIMEOUT_SEC = max(
    10.0,
    float(os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_TIMEOUT_SEC", "180")),
)
DEFERRED_SUMMARY_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_MAX_ATTEMPTS", "3")),
)
_TABLE_HEADER_TERMS = {
    "序号",
    "名称",
    "售卖模式",
    "内容描述",
    "计价单位",
    "数量",
    "税率",
    "含税单价",
    "含税金额",
    "服务类型/服务项",
    "服务描述",
}
_TABLE_FOOTER_TERMS = ("小计", "合计", "大写人民币")
_COMPANY_NAME_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff()（）·]+(?:股份有限公司|有限责任公司|有限公司)")
_UPPER_AMOUNT_RE = re.compile(
    r"(¥\s*[\d,]+(?:\.\d+)?)\s*[（(]\s*大写[：:]\s*人民币\s*([零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億元角分整正]+)\s*[)）]"
)


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


def _metadata_page_range_spec(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ",".join(parts) or None
    return str(value).strip() or None


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


def _page_render_pixels(page: Any, scale: float) -> int:
    rect = page.rect
    return max(1, int(rect.width * scale)) * max(1, int(rect.height * scale))


def _resolve_ocr_render_scale(
    page: Any,
    requested_scale: float,
    max_pixels: int,
    min_scale: float,
) -> tuple[float, int, bool]:
    requested_scale = max(0.5, float(requested_scale))
    min_scale = min(requested_scale, max(0.5, float(min_scale)))
    max_pixels = max(1, int(max_pixels))
    requested_pixels = _page_render_pixels(page, requested_scale)
    if requested_pixels <= max_pixels:
        return requested_scale, requested_pixels, False

    rect = page.rect
    base_area = max(1.0, float(rect.width) * float(rect.height))
    capped_scale = (max_pixels / base_area) ** 0.5
    scale = max(min_scale, min(requested_scale, capped_scale))
    return scale, _page_render_pixels(page, scale), scale < requested_scale


def _page_blank_signal(page: Any, *, scale: float = 0.5) -> dict[str, Any]:
    import fitz
    from PIL import Image, ImageOps

    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    gray = ImageOps.grayscale(img)
    hist = gray.histogram()
    total = max(1, gray.width * gray.height)
    nonwhite_ratio = sum(hist[:245]) / total
    dark_ratio = sum(hist[:180]) / total
    return {
        "blank_like": dark_ratio < 0.00002 and nonwhite_ratio < 0.001,
        "nonwhite_ratio": nonwhite_ratio,
        "dark_ratio": dark_ratio,
    }


def _ocr_cache_path(doc_dir: Path, page_num: int) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"ocr_p{page_num:04d}.txt"


def _ocr_cache_variant_path(doc_dir: Path, key: str) -> Path:
    cache_dir = doc_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-") or "cache"
    return cache_dir / safe


def _ocr_cache_key(image_bytes: bytes) -> str:
    return hashlib.sha1(image_bytes).hexdigest()[:16]


_paddle_ocr = None
_paddle_ocr_lock = threading.Lock()
_paddle_ocr_ready = threading.Event()
_paddle_ocr_initializing = threading.Event()
_deferred_summary_sem = threading.BoundedSemaphore(DEFERRED_SUMMARY_MAX_CONCURRENT)
DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC = float(
    os.environ.get("LARKSCOUT_DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC", "30")
)


def _get_paddle_ocr():
    global _paddle_ocr
    if _paddle_ocr is None:
        with _paddle_ocr_lock:
            if _paddle_ocr is None:
                os.environ.setdefault("FLAGS_enable_pir_api", "0")
                os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
                _paddle_ocr_initializing.set()
                try:
                    from paddleocr import PaddleOCR
                except ImportError as exc:
                    _paddle_ocr_initializing.clear()
                    raise RuntimeError(
                        f"PaddleOCR backend import failed: {exc}"
                    ) from exc
                try:
                    _paddle_ocr = PaddleOCR(
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                        use_textline_orientation=False,
                        text_detection_model_name=os.environ.get(
                            "LARKSCOUT_LOCAL_OCR_DET_MODEL", "PP-OCRv5_mobile_det"
                        ),
                        text_recognition_model_name=os.environ.get(
                            "LARKSCOUT_LOCAL_OCR_REC_MODEL", "PP-OCRv5_mobile_rec"
                        ),
                    )
                    _paddle_ocr_ready.set()
                finally:
                    _paddle_ocr_initializing.clear()
    return _paddle_ocr


def _flatten_paddle_ocr_result(result: Any) -> str:
    lines: list[str] = []
    blocks = result if isinstance(result, list) else [result]
    for block in blocks:
        if isinstance(block, dict):
            texts = block.get("rec_texts") or []
            for text in texts:
                value = str(text).strip()
                if value:
                    lines.append(value)
            continue
        if isinstance(block, list):
            for item in block:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                payload = item[1]
                if isinstance(payload, (list, tuple)) and payload:
                    text = str(payload[0]).strip()
                else:
                    text = str(payload).strip()
                if text:
                    lines.append(text)
    return "\n".join(lines).strip()


def local_ocr(image_bytes: bytes, page_num: int, backend: str) -> str:
    name = (backend or "").strip().lower()
    if name in {"", "none"}:
        return ""
    if name != "paddleocr":
        raise RuntimeError(f"unsupported local OCR backend: {backend}")
    try:
        import numpy as np
        from PIL import Image

        engine = _get_paddle_ocr()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        result = engine.predict(
            np.asarray(image),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        text = _flatten_paddle_ocr_result(result)
        return text or t("ocr_failed", page=page_num)
    except Exception as exc:
        logger.warning("Local OCR unavailable for page %d via %s: %s", page_num, backend, exc)
        return t("ocr_failed", page=page_num)


def _remove_footer_page_number(lines: list[str], page_num: int, total_pages: int) -> list[str]:
    cleaned = list(lines)
    if not cleaned:
        return cleaned
    candidate_numbers = {n for n in (page_num - 1, page_num, page_num + 1) if 0 < n <= total_pages}
    while cleaned:
        tail = cleaned[-1].strip()
        if tail.isdigit() and int(tail) in candidate_numbers and len(cleaned) >= 3:
            cleaned.pop()
            continue
        break
    return cleaned


def _looks_like_page_footer(line: str) -> bool:
    return bool(
        re.fullmatch(
            r"[-—_]*\s*第\s*\d+\s*[页頁]\s*(?:(?:[/／]\s*)?共\s*\d+\s*[页頁]?)?\s*[-—_]*",
            line.strip(),
        )
    )


def _looks_like_bracket_noise(line: str) -> bool:
    compact = re.sub(r"\s+", "", line.strip())
    if "[" not in compact and "]" not in compact:
        return False
    if len(compact) <= 5 and re.fullmatch(r"\[[A-Za-z0-9_]+\]?", compact):
        return True
    ascii_count = sum(1 for ch in compact if ch.isascii() and (ch.isalnum() or ch in "_-[]"))
    cjk_count = sum(1 for ch in compact if "\u4e00" <= ch <= "\u9fff")
    if ascii_count >= 6 and ascii_count >= cjk_count * 2:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-\[\]]{5,}", compact))


def _cleanup_ocr_text(text: str, *, source_filename: str | None = None) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned: list[str] = []
    for idx, line in enumerate(lines):
        if not line:
            continue
        if _looks_like_bracket_noise(line):
            continue
        if _looks_like_page_footer(line):
            continue
        if line == "定作":
            prev_context = "\n".join(cleaned[-4:])
            next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if "合同签订地点" in prev_context or _looks_like_page_footer(next_line):
                continue
        cleaned.append(line)

    if len(cleaned) > 1 and cleaned[0].strip().lower() in {"preface"}:
        cleaned.pop(0)

    cleaned_text = "\n".join(cleaned)
    replacements = {
        "安装元成": "安装完成",
        "软件采贝": "软件采购",
        "合同采贝": "合同采购",
        "软件东统": "软件系统",
        "则特殊开发部分应符\n合需求说明书": "则特殊开发部分应符合需求说明书",
    }
    for src, dst in replacements.items():
        cleaned_text = cleaned_text.replace(src, dst)

    source_contract_no = _source_filename_contract_no(source_filename)
    if source_contract_no:
        cleaned_lines = cleaned_text.splitlines()
        if cleaned_lines:
            leading = re.sub(r"\s+", "", cleaned_lines[0].strip())
            if re.fullmatch(r"[A-Za-z]{2,10}\d{4,20}", leading) and leading != source_contract_no:
                cleaned_lines[0] = source_contract_no
                cleaned_text = "\n".join(cleaned_lines)
    return cleaned_text.strip()


def _is_markdown_table_delimiter(line: str) -> bool:
    return bool(re.match(r"^\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?$", line.strip()))


def _looks_like_markdown_table_row(line: str) -> bool:
    line = line.strip()
    return line.count("|") >= 2 and len(line.replace("|", "").strip()) > 0


def _looks_like_plain_table_header(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    matches = sum(1 for term in _TABLE_HEADER_TERMS if term in line)
    return matches >= 3 or line.startswith("序号 ")


def _looks_like_plain_table_footer(line: str) -> bool:
    return any(term in line for term in _TABLE_FOOTER_TERMS)


def _looks_like_plain_table_row(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    if _looks_like_plain_table_header(line) or _looks_like_plain_table_footer(line):
        return True
    if re.match(r"^\d+\s+", line) and len(line) >= 20:
        if any(token in line for token in ("¥", "%", "套", "次", "年", "项", "个", "台", "PV")):
            return True
    if line in {"软件产品", "服务中心"}:
        return True
    return False


def _extract_tables_from_ocr_text(text: str, page_num: int, total_pages: int) -> tuple[str, list[str]]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line.strip() for line in lines if line.strip()]
    lines = _remove_footer_page_number(lines, page_num, total_pages)

    body_parts: list[str] = []
    tables: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if (
            i + 1 < len(lines)
            and _looks_like_markdown_table_row(line)
            and _is_markdown_table_delimiter(lines[i + 1])
        ):
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and _looks_like_markdown_table_row(lines[i]):
                table_lines.append(lines[i])
                i += 1
            table_text = "\n".join(table_lines).strip()
            tables.append(table_text)
            continue

        if _looks_like_plain_table_header(line):
            table_lines = [line]
            i += 1
            while i < len(lines):
                current = lines[i]
                if _is_heading(current) > 0 and not _looks_like_plain_table_row(current):
                    break
                if _looks_like_plain_table_row(current):
                    table_lines.append(current)
                    i += 1
                    continue
                break
            table_text = "\n".join(table_lines).strip()
            tables.append(table_text)
            continue

        body_parts.append(line)
        i += 1

    return "\n".join(part for part in body_parts if part).strip(), tables


def _amount_to_uppercase_rmb(amount_text: str) -> str | None:
    digits = amount_text.replace("¥", "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", digits):
        return None

    value = round(float(digits) + 1e-9, 2)
    integer = int(value)
    jiao = int((value * 10) % 10)
    fen = int(round(value * 100)) % 10

    digits_map = "零壹贰叁肆伍陆柒捌玖"
    small_units = ["", "拾", "佰", "仟"]
    large_units = ["", "万", "亿", "兆"]

    if integer == 0:
        integer_text = "零元"
    else:
        groups: list[int] = []
        while integer > 0:
            groups.append(integer % 10000)
            integer //= 10000

        parts: list[str] = []
        zero_between = False
        for idx in range(len(groups) - 1, -1, -1):
            group = groups[idx]
            if group == 0:
                zero_between = bool(parts)
                continue

            if zero_between or (parts and group < 1000):
                parts.append("零")
                zero_between = False

            group_digits: list[str] = []
            zero_inside = False
            for pos in range(3, -1, -1):
                divisor = 10**pos
                digit = group // divisor
                group %= divisor
                if digit == 0:
                    if group_digits:
                        zero_inside = True
                    continue
                if zero_inside:
                    group_digits.append("零")
                    zero_inside = False
                group_digits.append(digits_map[digit] + small_units[pos])

            parts.append("".join(group_digits) + large_units[idx])

        integer_text = "".join(parts) + "元"

    if jiao == 0 and fen == 0:
        return integer_text + "整"

    tail = ""
    if jiao > 0:
        tail += digits_map[jiao] + "角"
    elif fen > 0:
        tail += "零"
    if fen > 0:
        tail += digits_map[fen] + "分"
    return integer_text + tail


def _normalize_amount_phrases(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        amount_text = match.group(1)
        normalized_upper = _amount_to_uppercase_rmb(amount_text)
        if not normalized_upper:
            return match.group(0)
        return f"{amount_text}（大写：人民币{normalized_upper}）"

    return _UPPER_AMOUNT_RE.sub(repl, text)


def _collect_company_names(blocks: list[str]) -> list[str]:
    names: set[str] = set()
    for block in blocks:
        for match in _COMPANY_NAME_RE.findall(block):
            names.add(match.strip())
    return sorted(names)


def _split_company_name(name: str) -> tuple[str, str]:
    for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return name, ""


def _build_company_name_replacements(blocks: list[str]) -> dict[str, str]:
    names = _collect_company_names(blocks)
    replacements: dict[str, str] = {}
    for name in names:
        stem, suffix = _split_company_name(name)
        best = name
        best_score = 1
        for other in names:
            if other == name:
                continue
            other_stem, other_suffix = _split_company_name(other)
            if not suffix or suffix != other_suffix:
                continue
            if len(stem) < 2 or len(other_stem) < 2:
                continue
            if stem[-2:] != other_stem[-2:]:
                continue
            score = SequenceMatcher(None, stem, other_stem).ratio()
            if score < 0.7:
                continue
            if len(other) > len(best):
                best = other
                best_score = score
        if best != name and best_score >= 0.7:
            replacements[name] = best
    return replacements


def _apply_company_name_replacements(text: str, replacements: dict[str, str]) -> str:
    for src, dst in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9\u4e00-\u9fff]){re.escape(src)}(?![A-Za-z0-9\u4e00-\u9fff])"
        )
        text = pattern.sub(dst, text)
    return text


def _normalize_document_text(pages: list[PageContent]) -> None:
    for page in pages:
        page.text = _normalize_amount_phrases(page.text)
        page.tables = [_normalize_amount_phrases(table) for table in page.tables]


def _load_document_profile(profile_name: str | None, config_path: str | None) -> DocumentProfile | None:
    selected = (profile_name or "").strip()
    custom = (config_path or "").strip()
    if not selected and not custom:
        return None

    if custom:
        path = Path(custom).expanduser()
    else:
        path = DOCUMENT_PROFILE_CONFIG_DIR / f"{selected}.json"
        if not path.exists():
            path = FIELD_OCR_CONFIG_DIR / f"{selected}.json"

    if not path.exists():
        raise RuntimeError(f"field OCR config not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid field OCR config JSON: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"field OCR config must be a JSON object: {path}")

    classification_raw = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    quality_raw = raw.get("quality_policy") if isinstance(raw.get("quality_policy"), dict) else {}
    upgrade_raw = raw.get("upgrade_policy") if isinstance(raw.get("upgrade_policy"), dict) else {}
    table_raw = raw.get("table_policy") if isinstance(raw.get("table_policy"), dict) else {}
    cache_raw = raw.get("cache_policy") if isinstance(raw.get("cache_policy"), dict) else {}
    processing_raw = raw.get("processing_policy") if isinstance(raw.get("processing_policy"), dict) else {}
    summary_raw = raw.get("summary_policy") if isinstance(raw.get("summary_policy"), dict) else {}

    groups: list[FieldGroup] = []
    for item in raw.get("groups", []):
        if not isinstance(item, dict):
            continue
        crop_raw = item.get("crop") or {}
        crop = None
        if isinstance(crop_raw, dict):
            try:
                crop = FieldCrop(
                    x0=float(crop_raw["x0"]),
                    y0=float(crop_raw["y0"]),
                    x1=float(crop_raw["x1"]),
                    y1=float(crop_raw["y1"]),
                )
            except (KeyError, TypeError, ValueError):
                crop = None
        groups.append(
            FieldGroup(
                id=str(item.get("id") or f"group_{len(groups)+1}"),
                aliases=tuple(str(v) for v in item.get("aliases", []) if str(v).strip()),
                page_scope=tuple(int(v) for v in item.get("page_scope", []) if isinstance(v, int)),
                crop=crop,
                start_alias=str(item.get("start_alias")).strip() if item.get("start_alias") else None,
                end_alias=str(item.get("end_alias")).strip() if item.get("end_alias") else None,
                replace_mode=str(item.get("replace_mode") or "block_between_aliases"),
            )
        )

    fields: list[FieldRule] = []
    for item in raw.get("fields", []):
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        fields.append(
            FieldRule(
                id=str(item.get("id") or f"field_{len(fields)+1}"),
                aliases=tuple(str(v) for v in item.get("aliases", []) if str(v).strip()),
                pattern=str(pattern) if pattern else None,
                page_scope=tuple(int(v) for v in item.get("page_scope", []) if isinstance(v, int)),
            )
        )

    return DocumentProfile(
        name=str(raw.get("profile") or selected or path.stem),
        classification=ClassificationPolicy(
            required_terms=tuple(
                str(v) for v in classification_raw.get("required_terms", []) if str(v).strip()
            )
        ),
        quality_policy=QualityPolicy(
            sparse_text_chars=max(0, int(quality_raw.get("sparse_text_chars", 40))),
            usable_text_chars=max(1, int(quality_raw.get("usable_text_chars", 120))),
            scan_page_ratio=float(quality_raw.get("scan_page_ratio", 0.85)),
            mixed_page_ratio=float(quality_raw.get("mixed_page_ratio", 0.2)),
        ),
        upgrade_policy=UpgradePolicy(
            default_mode=str(upgrade_raw.get("default_mode") or "accurate").strip().lower(),
            local_ocr_backend=str(upgrade_raw.get("local_ocr_backend") or "paddleocr").strip().lower(),
            region_llm_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("region_llm_modes", ["accurate", "full"])
                if str(v).strip()
            ),
            full_llm_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("full_llm_modes", ["full"])
                if str(v).strip()
            ),
            proofread_modes=tuple(
                str(v).strip().lower()
                for v in upgrade_raw.get("proofread_modes", ["full"])
                if str(v).strip()
            ),
        ),
        table_policy=TablePolicy(
            prefer_markitdown=bool(table_raw.get("prefer_markitdown", True))
        ),
        cache_policy=CachePolicy(
            page_ocr=bool(cache_raw.get("page_ocr", True)),
            region_ocr=bool(cache_raw.get("region_ocr", True)),
        ),
        processing_policy=ProcessingPolicy(
            large_file_threshold_mb=max(1, int(processing_raw.get("large_file_threshold_mb", 50))),
            local_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("local_ocr_render_scale", LOCAL_OCR_RENDER_SCALE)),
            ),
            llm_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("llm_ocr_render_scale", OCR_RENDER_SCALE)),
            ),
            max_local_ocr_pixels=max(
                500_000,
                int(processing_raw.get("max_local_ocr_pixels", 4_000_000)),
            ),
            max_llm_ocr_pixels=max(
                500_000,
                int(processing_raw.get("max_llm_ocr_pixels", 8_000_000)),
            ),
            min_ocr_render_scale=max(
                0.5,
                float(processing_raw.get("min_ocr_render_scale", 1.25)),
            ),
        ),
        summary_policy=SummaryPolicy(
            default_mode=str(summary_raw.get("default_mode") or "sync").strip().lower(),
            async_modes=tuple(
                str(v).strip().lower()
                for v in summary_raw.get("async_modes", [])
                if str(v).strip()
            ),
            sync_modes=tuple(
                str(v).strip().lower()
                for v in summary_raw.get("sync_modes", ["full"])
                if str(v).strip()
            ),
        ),
        groups=tuple(groups),
        fields=tuple(fields),
    )


def _page_blob(page: PageContent) -> str:
    if page.tables_in_text:
        return page.text.strip()

    parts = [page.text.strip()] if page.text.strip() else []
    parts.extend(table.strip() for table in page.tables if table.strip())
    return "\n\n".join(parts).strip()


def _set_page_blob(page: PageContent, text: str) -> None:
    body, tables = _extract_tables_from_ocr_text(text, page.page_num, page.page_num)
    page.text = body
    page.tables = tables
    page.tables_in_text = bool(tables)


def _blob_has_alias(text: str, aliases: tuple[str, ...]) -> bool:
    return any(alias and alias in text for alias in aliases)


def _field_value_quality(field_id: str, value: str) -> tuple[bool, str]:
    value = value.strip()
    if not value:
        return False, "empty"
    if re.search(r"[\u3040-\u30ff]", value):
        return False, "kana_noise"
    if _looks_like_bracket_noise(value):
        return False, "bracket_noise"

    normalized = re.sub(r"\s+", "", value)
    if field_id == "contract_no":
        if normalized in {"甲", "乙", "合同", "合同编号", "方"}:
            return False, "label_only"
        if len(normalized) < 4 or not re.search(r"\d", normalized):
            return False, "too_short_or_no_digit"
    elif field_id in {"party_a_name", "party_b_name", "customer_name"}:
        if len(normalized) < 4:
            return False, "too_short"
        if not re.search(r"(公司|中心|银行|基金|学校|医院|政府|委员会|研究院|事务所|集团)", normalized):
            return False, "not_org_like"
    elif field_id.endswith("_phone"):
        if len(re.sub(r"\D", "", normalized)) < 7:
            return False, "not_phone_like"
    elif field_id.endswith("_account"):
        if len(re.sub(r"\D", "", normalized)) < 6:
            return False, "not_account_like"
    return True, ""


def _source_filename_contract_no(source_filename: str | None) -> str | None:
    stem = Path(source_filename or "").stem.strip()
    if re.fullmatch(r"[A-Za-z]{2,10}\d{4,20}", stem):
        return stem
    return None


def _normalize_cover_label_lines(blob: str) -> str:
    text = blob.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?m)^甲\s*\n\s*方\s*[：:]", "甲方：", text)
    text = re.sub(r"(?m)^乙\s*\n\s*方\s*[：:]", "乙方：", text)
    return text


def _replace_blob_segment(text: str, group: FieldGroup, replacement: str) -> str:
    if group.replace_mode == "replace_entire_page":
        return replacement.strip()

    if not group.start_alias:
        return replacement.strip()

    start = text.find(group.start_alias)
    if start < 0:
        return text

    end = len(text)
    if group.end_alias:
        found = text.find(group.end_alias, start + len(group.start_alias))
        if found >= 0:
            end = found
    return (text[:start].rstrip() + "\n\n" + replacement.strip() + "\n\n" + text[end:].lstrip()).strip()


def _extract_profile_fields(
    pages: list[PageContent],
    profile: DocumentProfile,
    *,
    source_filename: str | None = None,
) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    for field_rule in profile.fields:
        for page in pages:
            if field_rule.page_scope and page.page_num not in field_rule.page_scope:
                continue
            blob = _normalize_cover_label_lines(_page_blob(page))
            if field_rule.aliases and not _blob_has_alias(blob, field_rule.aliases):
                continue
            if field_rule.pattern:
                match = re.search(field_rule.pattern, blob, flags=re.MULTILINE)
                if not match:
                    continue
                value = (match.group(1) if match.groups() else match.group(0)).strip()
            else:
                value = next((alias for alias in field_rule.aliases if alias in blob), "").strip()
            if value:
                valid, reason = _field_value_quality(field_rule.id, value)
                if not valid:
                    logger.info(
                        "Discarded low-confidence field %s on page %d: %r (%s)",
                        field_rule.id,
                        page.page_num,
                        value,
                        reason,
                    )
                    continue
                extracted[field_rule.id] = {
                    "value": value,
                    "page": page.page_num,
                    "source": "profile_regex",
                }
                break
    if "contract_no" not in extracted:
        fallback_contract_no = _source_filename_contract_no(source_filename)
        if fallback_contract_no:
            extracted["contract_no"] = {
                "value": fallback_contract_no,
                "page": 1,
                "source": "source_filename",
            }
    return extracted


def _apply_field_focused_ocr(
    filepath: Path,
    pages: list[PageContent],
    profile: DocumentProfile,
    cache_dir: Path | None = None,
    proofread: bool = True,
) -> dict[str, Any]:
    import fitz

    applied_groups: list[dict[str, Any]] = []
    doc = fitz.open(str(filepath))
    try:
        for group in profile.groups:
            if not group.crop:
                continue
            for page in pages:
                if group.page_scope and page.page_num not in group.page_scope:
                    continue
                blob = _page_blob(page)
                if group.aliases and not _blob_has_alias(blob, group.aliases):
                    continue

                fitz_page = doc[page.page_num - 1]
                rect = fitz_page.rect
                clip = fitz.Rect(
                    rect.x0 + rect.width * group.crop.x0,
                    rect.y0 + rect.height * group.crop.y0,
                    rect.x0 + rect.width * group.crop.x1,
                    rect.y0 + rect.height * group.crop.y1,
                )
                pix = fitz_page.get_pixmap(matrix=fitz.Matrix(FIELD_OCR_RENDER_SCALE, FIELD_OCR_RENDER_SCALE), clip=clip)
                img_bytes = pix.tobytes("png")
                region_text = ""
                if cache_dir and profile.cache_policy.region_ocr:
                    ck = _ocr_cache_key(img_bytes)
                    cache_path = _ocr_cache_variant_path(
                        cache_dir,
                        f"ocr_region_p{page.page_num:04d}_{group.id}.{ck}.txt",
                    )
                    if cache_path.exists():
                        region_text = cache_path.read_text(encoding="utf-8").strip()
                if not region_text:
                    region_text = gemini_ocr(img_bytes, page.page_num, proofread=proofread).strip()
                    if cache_dir and profile.cache_policy.region_ocr and region_text:
                        cache_path = _ocr_cache_variant_path(
                            cache_dir,
                            f"ocr_region_p{page.page_num:04d}_{group.id}.{_ocr_cache_key(img_bytes)}.txt",
                        )
                        cache_path.write_text(region_text, encoding="utf-8")
                if not region_text or region_text.startswith("["):
                    continue
                region_text = _cleanup_ocr_text(region_text, source_filename=filepath.name)

                replace_source = page.text.strip()
                replaced = _replace_blob_segment(replace_source, group, region_text)
                if replaced != replace_source:
                    page.tables = []
                    _set_page_blob(page, replaced)
                    applied_groups.append({"group_id": group.id, "page": page.page_num})
    finally:
        doc.close()

    _normalize_document_text(pages)
    return {
        "profile": profile.name,
        "applied_groups": applied_groups,
        "extracted_fields": _extract_profile_fields(pages, profile, source_filename=filepath.name),
    }


# ═══════════════════════════════════════════
# Section stable ID
# ═══════════════════════════════════════════


def _section_sid(title: str, text: str) -> str:
    raw = (title + text[:200]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _resolve_pdf_parse_mode(profile: DocumentProfile | None, requested_mode: str | None) -> str:
    mode = (requested_mode or "").strip().lower()
    if not mode and profile:
        mode = profile.upgrade_policy.default_mode
    if not mode:
        mode = os.environ.get("LARKSCOUT_PDF_PARSE_MODE", "accurate").strip().lower()
    allowed = {"fast", "accurate", "full"}
    if mode not in allowed:
        raise RuntimeError("PDF parse mode must be one of: fast, accurate, full.")
    return mode


def _resolve_summary_mode(
    *,
    profile: DocumentProfile | None,
    parse_mode: str | None,
    generate_summary: bool,
    requested_mode: str | None,
) -> str:
    if not generate_summary:
        return "off"

    mode = (requested_mode or "").strip().lower()
    if not mode:
        mode = os.environ.get("LARKSCOUT_SUMMARY_MODE", "").strip().lower()

    if mode in {"off", "sync", "defer"}:
        return mode

    selected_parse_mode = (parse_mode or "").strip().lower()
    if profile:
        if selected_parse_mode and selected_parse_mode in profile.summary_policy.async_modes:
            return "defer"
        if selected_parse_mode and selected_parse_mode in profile.summary_policy.sync_modes:
            return "sync"
        if profile.summary_policy.default_mode in {"off", "sync", "defer"}:
            return profile.summary_policy.default_mode

    return "sync"


def _set_summary_metadata(
    parsed: ParsedDocument,
    *,
    mode: str,
    status: str,
    error: str | None = None,
    error_code: str | None = None,
    attempts: int | None = None,
) -> None:
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    existing = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    metadata["summary"] = {
        "mode": mode,
        "status": status,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempts": int(attempts if attempts is not None else existing.get("attempts", 0)),
    }
    if status == "running":
        metadata["summary"]["started_at"] = metadata["summary"]["updated_at"]
    elif existing.get("started_at"):
        metadata["summary"]["started_at"] = existing.get("started_at")
    if status in {"completed", "failed"}:
        metadata["summary"]["finished_at"] = metadata["summary"]["updated_at"]
    if error:
        metadata["summary"]["error"] = error
    if error_code:
        metadata["summary"]["error_code"] = error_code
    parsed.metadata = metadata


def _summary_placeholder_text(status: str, error: str | None = None) -> str:
    if status == "running":
        return "(Summary running)"
    if status == "failed":
        if error:
            return f"(Summary failed: {error})"
        return "(Summary failed)"
    return t("summary_pending")


def _current_summary_attempts(parsed: ParsedDocument) -> int:
    metadata = parsed.metadata if isinstance(parsed.metadata, dict) else {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    try:
        return int(summary.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _classify_summary_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, FuturesTimeoutError):
        return "timeout", f"summary timed out after {int(DEFERRED_SUMMARY_TIMEOUT_SEC)}s"

    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "attempt limit" in lower:
        return "attempt_limit", text
    if "429" in text or "rate limit" in lower or "速率限制" in text:
        return "rate_limit", "upstream rate limit"
    if "timeout" in lower or "timed out" in lower:
        return "timeout", text
    return "provider_error", text


def _classify_contract_text(
    text: str,
    profile: DocumentProfile | None,
) -> tuple[bool, list[str]]:
    required_terms = profile.classification.required_terms if profile else ()
    if not required_terms:
        return True, []
    matched_terms = [term for term in required_terms if term and term in text]
    return bool(matched_terms), matched_terms


def _assess_contract_quality(
    markdown_text: str,
    page_signals: list[dict[str, Any]],
    profile: DocumentProfile | None,
) -> dict[str, Any]:
    quality_policy = profile.quality_policy if profile else QualityPolicy()
    total_pages = len(page_signals)
    sparse_pages = [s["page_num"] for s in page_signals if s["text_len"] < quality_policy.sparse_text_chars]
    usable_pages = [s["page_num"] for s in page_signals if s["text_len"] >= quality_policy.usable_text_chars]
    image_pages = [s["page_num"] for s in page_signals if s["image_count"] > 0]
    scan_like_pages = [s["page_num"] for s in page_signals if s["scan_like"]]
    blank_pages = [s["page_num"] for s in page_signals if s.get("blank_like")]
    manual_blank_pages = [s["page_num"] for s in page_signals if s.get("blank_override")]

    scan_ratio = len(scan_like_pages) / max(total_pages, 1)
    mixed_ratio = len(sparse_pages) / max(total_pages, 1)
    if scan_ratio >= quality_policy.scan_page_ratio:
        document_quality = "scan_only"
    elif mixed_ratio >= quality_policy.mixed_page_ratio:
        document_quality = "mixed"
    else:
        document_quality = "text"

    is_contract, matched_terms = _classify_contract_text(markdown_text, profile)

    return {
        "profile": profile.name if profile else None,
        "is_contract": is_contract,
        "matched_terms": matched_terms,
        "document_quality": document_quality,
        "scan_ratio": scan_ratio,
        "sparse_pages": sparse_pages,
        "usable_pages": usable_pages,
        "image_pages": image_pages,
        "scan_like_pages": scan_like_pages,
        "blank_pages": blank_pages,
        "near_blank_pages": blank_pages,
        "manual_blank_pages": manual_blank_pages,
        "page_signals": page_signals,
    }


def _plan_pdf_ocr(
    *,
    profile: DocumentProfile | None,
    parse_mode: str,
    force_ocr: bool,
    explicit_ocr_pages: set[int] | None,
    assessment: dict[str, Any],
) -> dict[str, Any]:
    quality = assessment.get("document_quality") or "text"
    scan_like_pages = set(assessment.get("scan_like_pages") or [])
    sparse_pages = set(assessment.get("sparse_pages") or [])
    blank_pages = set(assessment.get("blank_pages") or assessment.get("near_blank_pages") or [])
    problem_pages = (scan_like_pages | sparse_pages) - blank_pages

    local_backend = profile.upgrade_policy.local_ocr_backend if profile else "paddleocr"
    local_ocr_pages: set[int] = set()
    llm_ocr_pages: set[int] = set()
    region_llm = False
    proofread = False

    if explicit_ocr_pages:
        llm_ocr_pages |= set(explicit_ocr_pages)
        if parse_mode in {"fast", "accurate"} and quality in {"scan_only", "mixed"}:
            local_ocr_pages |= problem_pages
            region_llm = bool(
                parse_mode == "accurate"
                and profile
                and parse_mode in profile.upgrade_policy.region_llm_modes
            )
    elif force_ocr:
        llm_ocr_pages = set(scan_like_pages or sparse_pages or assessment.get("image_pages") or []) - blank_pages
        if not llm_ocr_pages:
            llm_ocr_pages = {
                signal["page_num"]
                for signal in assessment.get("page_signals", [])
                if signal["page_num"] not in blank_pages
            }
    elif parse_mode == "fast":
        if quality in {"scan_only", "mixed"}:
            local_ocr_pages |= problem_pages
    elif parse_mode == "accurate":
        if quality in {"scan_only", "mixed"}:
            local_ocr_pages |= problem_pages
            region_llm = bool(profile and parse_mode in profile.upgrade_policy.region_llm_modes)
    elif parse_mode == "full":
        llm_ocr_pages = {
            signal["page_num"]
            for signal in assessment.get("page_signals", [])
            if signal["page_num"] not in blank_pages
        }
        region_llm = bool(profile and parse_mode in profile.upgrade_policy.region_llm_modes)

    if profile and parse_mode in profile.upgrade_policy.proofread_modes:
        proofread = True
    if explicit_ocr_pages or force_ocr:
        proofread = True

    return {
        "parse_mode": parse_mode,
        "local_backend": local_backend,
        "local_ocr_pages": sorted(local_ocr_pages - llm_ocr_pages),
        "llm_ocr_pages": sorted(llm_ocr_pages),
        "region_llm": region_llm,
        "proofread": proofread,
    }


def _should_prewarm_local_ocr_for_pdf(
    filepath: Path,
    *,
    profile: DocumentProfile | None,
    parse_mode: str | None,
    force_ocr: bool,
    ocr_pages_spec: str | None,
    manual_blank_pages_spec: str | None,
    ocr_threshold: int,
) -> bool:
    if force_ocr or ocr_pages_spec:
        return False

    selected_mode = _resolve_pdf_parse_mode(profile, parse_mode)
    if selected_mode == "full":
        return False

    import fitz

    doc = fitz.open(str(filepath))
    try:
        manual_blank_pages = (
            _parse_page_range(manual_blank_pages_spec, len(doc))
            if manual_blank_pages_spec
            else set()
        )
        page_signals: list[dict[str, Any]] = []
        for i, page in enumerate(doc):
            page_num = i + 1
            text = page.get_text("text").strip()
            try:
                image_count = len(page.get_images(full=False))
            except Exception:
                image_count = 0
            manual_blank = page_num in manual_blank_pages
            scan_like = _should_ocr(page, text, ocr_threshold)
            blank_info: dict[str, Any] = {
                "blank_like": False,
                "blank_override": False,
                "nonwhite_ratio": None,
                "dark_ratio": None,
            }
            if manual_blank:
                blank_info["blank_like"] = True
                blank_info["blank_override"] = True
            elif scan_like and not text and image_count:
                blank_info = _page_blank_signal(page)
                blank_info["blank_override"] = False
            page_signals.append(
                {
                    "page_num": page_num,
                    "text_len": len(text),
                    "image_count": image_count,
                    "scan_like": scan_like,
                    **blank_info,
                }
            )
        assessment = _assess_contract_quality("", page_signals, profile)
        ocr_plan = _plan_pdf_ocr(
            profile=profile,
            parse_mode=selected_mode,
            force_ocr=False,
            explicit_ocr_pages=None,
            assessment=assessment,
        )
        return bool(ocr_plan["local_ocr_pages"])
    finally:
        doc.close()


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
    field_ocr_profile: str | None = None,
    field_ocr_config: str | None = None,
    parse_mode: str | None = None,
    manual_blank_pages_spec: str | None = None,
) -> ParsedDocument:
    import fitz

    def _usable_page_text(raw_text: str, enhanced_text: str | None) -> str:
        if not enhanced_text:
            return raw_text
        if enhanced_text.startswith("[OCR failed"):
            return raw_text or enhanced_text
        return enhanced_text

    logger.info(f"Parsing PDF: {filepath.name}")
    profile = _load_document_profile(field_ocr_profile, field_ocr_config)
    selected_mode = _resolve_pdf_parse_mode(profile, parse_mode)
    processing_policy = (
        profile.processing_policy
        if profile
        else ProcessingPolicy(
            local_ocr_render_scale=LOCAL_OCR_RENDER_SCALE,
            llm_ocr_render_scale=OCR_RENDER_SCALE,
        )
    )
    source_size_bytes = filepath.stat().st_size
    large_file_threshold_bytes = processing_policy.large_file_threshold_mb * 1024 * 1024
    source_file_meta = {
        "size_bytes": source_size_bytes,
        "large_file_threshold_mb": processing_policy.large_file_threshold_mb,
        "large_file": source_size_bytes > large_file_threshold_bytes,
    }
    markdown_text = ""
    try:
        markdown_text = _convert_to_markdown(filepath)
        logger.info(f"MarkItDown extraction complete: {len(markdown_text)} chars")
    except RuntimeError as exc:
        logger.warning("MarkItDown extraction failed for %s: %s", filepath.name, exc)

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
    manual_blank_pages = (
        _parse_page_range(manual_blank_pages_spec, total_pages)
        if manual_blank_pages_spec
        else set()
    )
    if manual_blank_pages:
        logger.info("Manual blank/skip OCR pages: %s", sorted(manual_blank_pages))

    # Build page-level baseline signals for selective enhancement.
    page_texts: dict[int, str] = {}
    page_signals: list[dict[str, Any]] = []

    for i, page in enumerate(doc):
        page_num = i + 1
        text = page.get_text("text").strip()
        page_texts[page_num] = text
        image_count = 0
        try:
            image_count = len(page.get_images(full=False))
        except Exception:
            image_count = 0
        manual_blank = page_num in manual_blank_pages
        scan_like = _should_ocr(page, text, ocr_threshold)
        blank_info: dict[str, Any] = {
            "blank_like": False,
            "blank_override": False,
            "nonwhite_ratio": None,
            "dark_ratio": None,
        }
        if manual_blank:
            blank_info["blank_like"] = True
            blank_info["blank_override"] = True
        elif scan_like and not text and image_count:
            blank_info = _page_blank_signal(page)
            blank_info["blank_override"] = False
        page_signals.append(
            {
                "page_num": page_num,
                "text_len": len(text),
                "image_count": image_count,
                "scan_like": scan_like,
                **blank_info,
            }
        )

    assessment = _assess_contract_quality(markdown_text, page_signals, profile)
    ocr_plan = _plan_pdf_ocr(
        profile=profile,
        parse_mode=selected_mode,
        force_ocr=force_ocr,
        explicit_ocr_pages=ocr_page_set,
        assessment=assessment,
    )
    logger.info(
        "PDF parse plan: mode=%s quality=%s local_pages=%s llm_pages=%s region_llm=%s",
        ocr_plan["parse_mode"],
        assessment["document_quality"],
        ocr_plan["local_ocr_pages"],
        ocr_plan["llm_ocr_pages"],
        ocr_plan["region_llm"],
    )

    local_ocr_set = set(ocr_plan["local_ocr_pages"])
    llm_ocr_set = set(ocr_plan["llm_ocr_pages"])
    local_ocr_results: dict[int, str] = {}
    llm_ocr_results: dict[int, str] = {}
    local_tasks: list[tuple[int, bytes]] = []
    llm_tasks: list[tuple[int, bytes]] = []
    render_meta: dict[str, Any] = {
        "local_ocr_render_scale": processing_policy.local_ocr_render_scale,
        "llm_ocr_render_scale": processing_policy.llm_ocr_render_scale,
        "max_local_ocr_pixels": processing_policy.max_local_ocr_pixels,
        "max_llm_ocr_pixels": processing_policy.max_llm_ocr_pixels,
        "min_ocr_render_scale": processing_policy.min_ocr_render_scale,
        "pages_capped": [],
    }

    for page in doc:
        page_num = page.number + 1
        if page_num not in local_ocr_set and page_num not in llm_ocr_set:
            continue
        if page_num in llm_ocr_set:
            requested_scale = processing_policy.llm_ocr_render_scale
            max_pixels = processing_policy.max_llm_ocr_pixels
            cache_key = "llm"
        else:
            requested_scale = processing_policy.local_ocr_render_scale
            max_pixels = processing_policy.max_local_ocr_pixels
            cache_key = f"local-{ocr_plan['local_backend']}"
        scale, render_pixels, capped = _resolve_ocr_render_scale(
            page,
            requested_scale=requested_scale,
            max_pixels=max_pixels,
            min_scale=processing_policy.min_ocr_render_scale,
        )
        if capped:
            logger.info(
                "Page %d/%d: capped %s OCR render scale %.2f -> %.2f (%d px)",
                page_num,
                total_pages,
                cache_key,
                requested_scale,
                scale,
                render_pixels,
            )
            render_meta["pages_capped"].append(
                {
                    "page_num": page_num,
                    "backend": cache_key,
                    "requested_scale": requested_scale,
                    "actual_scale": scale,
                    "render_pixels": render_pixels,
                    "max_pixels": max_pixels,
                }
            )
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img_bytes = pix.tobytes("png")

        if cache_dir:
            ck = _ocr_cache_key(img_bytes)
            if page_num in llm_ocr_set:
                cp = _ocr_cache_path(cache_dir, page_num)
                ck_path = cp.with_suffix(f".{ck}.txt")
            else:
                ck_path = _ocr_cache_variant_path(
                    cache_dir,
                    f"ocr_p{page_num:04d}.{cache_key}.{ck}.txt",
                )
            if ck_path.exists():
                cached = ck_path.read_text(encoding="utf-8")
                if page_num in llm_ocr_set:
                    llm_ocr_results[page_num] = cached
                else:
                    local_ocr_results[page_num] = cached
                logger.info("Page %d/%d: %s OCR cache hit", page_num, total_pages, cache_key)
                continue
        if page_num in llm_ocr_set:
            llm_tasks.append((page_num, img_bytes))
        else:
            local_tasks.append((page_num, img_bytes))

    doc.close()

    if local_tasks:
        _get_paddle_ocr()
        logger.info(
            "Concurrent local OCR: %d pages (%d workers, backend=%s)...",
            len(local_tasks),
            LOCAL_OCR_CONCURRENCY,
            ocr_plan["local_backend"],
        )

        def _do_local_ocr(args):
            pn, img_b = args
            return pn, img_b, local_ocr(img_b, pn, ocr_plan["local_backend"])

        with ThreadPoolExecutor(max_workers=LOCAL_OCR_CONCURRENCY) as pool:
            futures = {pool.submit(_do_local_ocr, task): task for task in local_tasks}
            for fut in as_completed(futures):
                pn, img_b, result = fut.result()
                local_ocr_results[pn] = result
                logger.info(f"Page {pn}/{total_pages}: local OCR done")
                if cache_dir and profile and profile.cache_policy.page_ocr:
                    cache_path = _ocr_cache_variant_path(
                        cache_dir,
                        f"ocr_p{pn:04d}.local-{ocr_plan['local_backend']}.{_ocr_cache_key(img_b)}.txt",
                    )
                    cache_path.write_text(result, encoding="utf-8")

    # Concurrent LLM OCR
    if llm_tasks:
        logger.info(f"Concurrent LLM OCR: {len(llm_tasks)} pages ({concurrency} workers)...")

        def _do_ocr(args):
            pn, img_b = args
            result = gemini_ocr(img_b, pn, proofread=ocr_plan["proofread"])
            return pn, img_b, result

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_do_ocr, task): task for task in llm_tasks}
            for fut in as_completed(futures):
                pn, img_b, result = fut.result()
                llm_ocr_results[pn] = result
                logger.info(f"Page {pn}/{total_pages}: LLM OCR done")
                if cache_dir:
                    cp = _ocr_cache_path(cache_dir, pn)
                    ck = _ocr_cache_key(img_b)
                    ck_path = cp.with_suffix(f".{ck}.txt")
                    ck_path.write_text(result, encoding="utf-8")

    pages: list[PageContent] = []
    ocr_table_count = 0
    ocr_count = len(local_ocr_set | llm_ocr_set)
    for page_num in range(1, total_pages + 1):
        raw_text = page_texts.get(page_num, "")
        page_text = raw_text
        page_tables: list[str] = []
        enhanced = llm_ocr_results.get(page_num) or local_ocr_results.get(page_num)
        if enhanced:
            page_text = _cleanup_ocr_text(_usable_page_text(raw_text, enhanced))
            page_text, page_tables = _extract_tables_from_ocr_text(page_text, page_num, total_pages)
            ocr_table_count += len(page_tables)
        pages.append(
            PageContent(
                page_num=page_num,
                text=page_text.strip(),
                is_ocr=page_num in (local_ocr_set | llm_ocr_set),
                tables=page_tables,
                tables_in_text=bool(page_tables),
            )
        )

    if llm_ocr_results:
        logger.info(f"LLM OCR pages: {sorted(llm_ocr_results)}")
    if local_ocr_results:
        logger.info(f"Local OCR pages: {sorted(local_ocr_results)}")

    if profile and not assessment.get("is_contract"):
        combined_text = "\n".join(page.text for page in pages if page.text)
        is_contract, matched_terms = _classify_contract_text(combined_text, profile)
        if is_contract:
            assessment["is_contract"] = True
            assessment["matched_terms"] = matched_terms
            assessment["classification_source"] = "enhanced_text"

    _normalize_document_text(pages)
    field_ocr_meta: dict[str, Any] = {}
    if profile and ocr_plan["region_llm"]:
        field_ocr_meta = _apply_field_focused_ocr(
            filepath,
            pages,
            profile,
            cache_dir=cache_dir,
            proofread=ocr_plan["proofread"],
        )
        _normalize_document_text(pages)

    # Section splitting: prefer TOC when available
    if toc:
        sections = _split_sections_from_toc(pages, toc)
    else:
        sections = _split_sections(pages)

    for sec in sections:
        sec.sid = _section_sid(sec.title, sec.text)

    # Count tables in Markdown output
    if extract_tables:
        table_count = _count_markdown_tables(markdown_text) if (markdown_text and (not profile or profile.table_policy.prefer_markitdown)) else ocr_table_count
    else:
        table_count = 0

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
        metadata={
            "document_profile": profile.name if profile else None,
            "pdf_parse_mode": selected_mode,
            "source_file": source_file_meta,
            "quality_assessment": assessment,
            "ocr_plan": ocr_plan,
            "ocr_rendering": render_meta,
            "field_ocr": field_ocr_meta,
        },
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
    if _looks_like_plain_table_row(text) or _looks_like_markdown_table_row(text):
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
        if p.tables and not p.tables_in_text:
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
        page_tables_attached = False
        for line in page.text.split("\n"):
            line = line.strip()
            if not line:
                continue
            heading_level = _is_heading(line)
            if heading_level > 0 and not current_lines and current_title == tmpl("default_section_title"):
                current_title = line
                current_level = heading_level
                current_start_page = page.page_num
                continue
            if heading_level > 0 and current_lines:
                if page.tables and not page.tables_in_text and not page_tables_attached:
                    current_lines.extend(table.strip() for table in page.tables if table.strip())
                    page_tables_attached = True
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
        if page.tables_in_text:
            continue
        if not page_tables_attached:
            for table in page.tables:
                value = table.strip()
                if value:
                    current_lines.append(value)
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
            if page.tables and not page.tables_in_text:
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
    parsed: ParsedDocument, concurrency: int = 3, allow_single_fallback: bool = True
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
            futures = {
                pool.submit(_summarize_batch, batch, allow_single_fallback): batch
                for batch in batches
            }
            for fut in as_completed(futures):
                fut.result()
    else:
        for batch in batches:
            _summarize_batch(batch, allow_single_fallback)

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


def _summarize_batch(sections: list[Section], allow_single_fallback: bool = True):
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
    if not allow_single_fallback:
        raise RuntimeError(f"batch summary parse failed for {n} sections")

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


def _reset_generated_output_dirs(doc_dir: Path) -> None:
    for child in ("sections", "tables"):
        path = doc_dir / child
        if path.exists():
            shutil.rmtree(path)
    for child in ("sections.json", "tables.json"):
        path = doc_dir / child
        if path.exists():
            path.unlink()


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
    doc_dir.mkdir(parents=True, exist_ok=True)
    _reset_generated_output_dirs(doc_dir)
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

    table_entries = _write_tables(doc_dir, parsed)
    if table_entries:
        _write_json(doc_dir / "tables.json", table_entries)
        logger.info(f"tables/ ({len(table_entries)} files)")

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
            "sections": "sections.json",
            "tables_dir": "tables/",
            "tables": "tables.json",
        },
        "sections": [
            _build_section_entry(
                sec,
                summary_preview=(sec.summary[:120] + "...") if len(sec.summary) > 120 else sec.summary,
            )
            for sec in parsed.sections
        ],
        "tables": table_entries,
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
    _write_json(doc_dir / "sections.json", manifest["sections"])
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
    summary_placeholder: str | None = None,
):
    doc_dir = output_dir / doc_id
    sections_dir = doc_dir / "sections"
    doc_dir.mkdir(parents=True, exist_ok=True)
    _reset_generated_output_dirs(doc_dir)
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

    table_entries = _write_tables(doc_dir, parsed)
    if table_entries:
        _write_json(doc_dir / "tables.json", table_entries)

    placeholder = summary_placeholder or t("summary_pending")
    _write_text(
        doc_dir / "digest.md",
        f"{tmpl('digest_title', doc_id=doc_id, filename=parsed.filename)}\n\n{placeholder}\n",
    )
    _write_text(
        doc_dir / "brief.md",
        f"{tmpl('digest_title', doc_id=doc_id, filename=parsed.filename)}\n\n{placeholder}\n",
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
            "sections": "sections.json",
            "tables_dir": "tables/",
            "tables": "tables.json",
        },
        "sections": [
            _build_section_entry(sec, summary_preview="")
            for sec in parsed.sections
        ],
        "tables": table_entries,
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
    _write_json(doc_dir / "sections.json", manifest["sections"])
    _write_json(doc_dir / "manifest.json", manifest)
    _update_doc_index(
        output_dir,
        meta,
        placeholder,
        tags=tags,
        source=source,
        source_url=str(parsed.filename),
        content_hash=content_hash,
        metadata=metadata,
        source_record=source_record,
    )
    logger.info(f"Text extraction complete (no summary): {doc_dir}")


def _generate_deferred_summary(
    doc_id: str,
    parsed: ParsedDocument,
    output_dir: Path,
    concurrency: int,
    tags: list[str] | None,
    metadata: dict[str, Any] | None,
    source_record: dict[str, Any] | None,
) -> None:
    logger.info("Deferred summary thread started: %s", doc_id)
    attempts = _current_summary_attempts(parsed) + 1
    acquired = False
    try:
        if attempts > DEFERRED_SUMMARY_MAX_ATTEMPTS:
            raise RuntimeError(
                f"summary attempt limit reached ({DEFERRED_SUMMARY_MAX_ATTEMPTS})"
            )
        if _paddle_ocr_initializing.is_set() and DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC > 0:
            logger.info(
                "Deferred summary waiting for local OCR init: %s (timeout=%ss)",
                doc_id,
                DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC,
            )
            _paddle_ocr_ready.wait(timeout=DEFERRED_SUMMARY_LOCAL_OCR_WAIT_SEC)
        _deferred_summary_sem.acquire()
        acquired = True
        _set_summary_metadata(parsed, mode="defer", status="running", attempts=attempts)
        write_output_extract_only(
            doc_id,
            parsed,
            output_dir,
            tags=tags,
            source="upload",
            metadata=metadata,
            source_record=source_record,
            summary_placeholder=_summary_placeholder_text("running"),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                generate_summaries,
                parsed,
                concurrency,
                False,
            )
            digest_text, brief_text, _ = future.result(timeout=DEFERRED_SUMMARY_TIMEOUT_SEC)
        _set_summary_metadata(parsed, mode="defer", status="completed", attempts=attempts)
        write_output(
            doc_id,
            parsed,
            digest_text,
            brief_text,
            output_dir,
            tags=tags,
            source="upload",
            original_path=str(parsed.filename),
            metadata=metadata,
            source_record=source_record,
        )
        logger.info("Deferred summary complete: %s", doc_id)
    except Exception as exc:
        error_code, error_message = _classify_summary_error(exc)
        logger.exception("Deferred summary failed for %s [%s]: %s", doc_id, error_code, exc)
        _set_summary_metadata(
            parsed,
            mode="defer",
            status="failed",
            error=error_message,
            error_code=error_code,
            attempts=attempts,
        )
        write_output_extract_only(
            doc_id,
            parsed,
            output_dir,
            tags=tags,
            source="upload",
            metadata=metadata,
            source_record=source_record,
            summary_placeholder=_summary_placeholder_text("failed", error_message),
        )
    finally:
        if acquired:
            try:
                _deferred_summary_sem.release()
            except ValueError:
                pass


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
        summary_meta = (
            meta.get("parse_metadata", {}).get("summary")
            if isinstance(meta.get("parse_metadata"), dict)
            else {}
        )
        if isinstance(summary_meta, dict):
            entry["summary_mode"] = summary_meta.get("mode")
            entry["summary_status"] = summary_meta.get("status")
            entry["summary_error_code"] = summary_meta.get("error_code")

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

_DOC_ID_RE = re.compile(r"^(?=.{1,80}$)(?=.*\d)[A-Za-z0-9](?:[A-Za-z0-9-]{0,78}[A-Za-z0-9])?$")
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


def _doc_id_strategy(requested_strategy: str | None = None) -> str:
    strategy = (requested_strategy or os.environ.get("LARKSCOUT_DOC_ID_STRATEGY", "counter")).strip().lower()
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


def _resolve_doc_id(
    docs_dir: Path,
    filename: str,
    requested_doc_id: str | None,
    requested_strategy: str | None = None,
) -> str:
    if requested_doc_id:
        _validate_doc_id(requested_doc_id)
        return requested_doc_id

    if _doc_id_strategy(requested_strategy) == "source_filename":
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
    summary_mode: str | None = None
    summary_status: str | None = None
    summary_error_code: str | None = None
    sid: str | None = None
    section_title: str | None = None
    page_range: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    snippet: str | None = None
    content: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int


class SectionSearchRequest(BaseModel):
    q: str
    limit: int = Field(default=20, ge=1, le=200)
    include_content: bool = False
    case_sensitive: bool = False


class ChunkRequest(BaseModel):
    max_tokens_per_chunk: int = Field(default=4000, ge=200, le=50000)
    overlap_tokens: int = Field(default=200, ge=0, le=5000)
    merge_short_sections: bool = True
    merge_threshold_tokens: int = Field(default=500, ge=0, le=10000)
    include_text: bool = True


# ---- FastAPI app ----

app = FastAPI(title="Doc Reader API", version="3.0.0")
PREWARM_LOCAL_OCR = os.environ.get("LARKSCOUT_PREWARM_LOCAL_OCR", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


@app.on_event("startup")
async def _startup_prewarm_local_ocr() -> None:
    if not PREWARM_LOCAL_OCR:
        return
    try:
        _get_paddle_ocr()
        logger.info("Local OCR backend prewarmed")
    except Exception as exc:
        logger.warning("Local OCR prewarm skipped: %s", exc)


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
    text_hash = hashlib.sha256(sec.text.encode("utf-8", errors="ignore")).hexdigest()
    return {
        "sid": sec.sid,
        "index": sec.index,
        "order": sec.index,
        "title": sec.title,
        "page_range": sec.page_range,
        "page_start": page_start,
        "page_end": page_end,
        "char_count": len(sec.text),
        "token_estimate": _estimate_tokens(sec.text),
        "text_hash": f"sha256:{text_hash}",
        "table_refs": [],
        "ocr_quality": None,
        "type": "text",
        "summary_preview": summary_preview,
        "file": f"sections/{sec.index:02d}-{sec.sid}-{_safe_filename(sec.title)}.md",
    }


def _build_table_entries(parsed: ParsedDocument) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for i, (page_num, table_md) in enumerate(
        ((p.page_num, table) for p in parsed.pages for table in p.tables),
        1,
    ):
        text_hash = hashlib.sha256(table_md.encode("utf-8", errors="ignore")).hexdigest()
        entries.append(
            {
                "table_id": f"table-{i:02d}",
                "index": i,
                "page": page_num,
                "page_start": page_num,
                "page_end": page_num,
                "char_count": len(table_md),
                "token_estimate": _estimate_tokens(table_md),
                "text_hash": f"sha256:{text_hash}",
                "type": "markdown",
                "file": f"tables/table-{i:02d}.md",
            }
        )
    return entries


def _write_tables(doc_dir: Path, parsed: ParsedDocument) -> list[dict[str, Any]]:
    table_entries = _build_table_entries(parsed)
    if not table_entries:
        return []
    tables_dir = doc_dir / "tables"
    tables_dir.mkdir(exist_ok=True)
    for entry in table_entries:
        page_num = entry["page"]
        table_index = entry["index"]
        table_md = next(
            t
            for idx, (_page, t) in enumerate(
                ((p.page_num, table) for p in parsed.pages for table in p.tables),
                1,
            )
            if idx == table_index
        )
        _write_text(
            tables_dir / f"{entry['table_id']}.md",
            f"# Table {table_index} (page {page_num})\n\n{table_md}\n",
        )
    return table_entries


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
    parse_metadata = manifest.get("parse_metadata") if isinstance(manifest.get("parse_metadata"), dict) else {}
    summary_meta = parse_metadata.get("summary") if isinstance(parse_metadata.get("summary"), dict) else {}
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
        "summary_mode": summary_meta.get("mode"),
        "summary_status": summary_meta.get("status"),
        "summary_error_code": summary_meta.get("error_code"),
    }


def _strip_section_storage_wrapper(raw: str) -> str:
    body = raw
    body = re.sub(
        r"^# .*\n\n\*\*(?:章节|Section) .*?\n\n",
        "",
        body,
        count=1,
        flags=re.S,
    )
    body = re.sub(
        r"^\*\*(?:摘要|Summary)\*\*: .*?\n\n---\n\n",
        "",
        body,
        count=1,
        flags=re.S,
    )
    return body.strip()


def _load_doc_tags(docs_dir: Path, doc_id: str) -> list[str]:
    for entry in _load_doc_index(docs_dir):
        if entry.get("id") == doc_id:
            tags = entry.get("tags")
            if isinstance(tags, list):
                return [str(tag) for tag in tags]
            return []
    return []


def _load_parsed_document_from_storage(docs_dir: Path, doc_id: str) -> tuple[ParsedDocument, dict[str, Any], dict[str, Any]]:
    doc_dir = docs_dir / doc_id
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sections_meta = manifest.get("sections")
    if not isinstance(sections_meta, list):
        raise HTTPException(500, f"manifest missing sections for {doc_id}")

    sections: list[Section] = []
    for sec in sorted(
        (item for item in sections_meta if isinstance(item, dict)),
        key=lambda item: int(item.get("index", 0)),
    ):
        rel_path = sec.get("file")
        section_path = _resolve_manifest_section_path(doc_dir, rel_path)
        if not section_path or not section_path.exists():
            raise HTTPException(500, f"section file missing for {doc_id}: {rel_path}")

        raw = section_path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        title = str(sec.get("title") or "")
        text = _strip_section_storage_wrapper(raw)
        if lines and lines[0].startswith("#"):
            title = lines[0].lstrip("#").strip() or title

        sections.append(
            Section(
                index=int(sec.get("index", len(sections) + 1)),
                title=title or f"Section {len(sections) + 1}",
                level=1,
                text=text,
                page_range=str(sec.get("page_range") or ""),
                sid=str(sec.get("sid") or ""),
            )
        )

    parsed = ParsedDocument(
        filename=str(manifest.get("filename") or doc_id),
        file_type=str(manifest.get("file_type") or "pdf"),
        total_pages=int((manifest.get("parse_metadata") or {}).get("total_pages") or 0),
        pages=[],
        sections=sections,
        ocr_page_count=int((manifest.get("parse_metadata") or {}).get("ocr_page_count") or 0),
        table_count=0,
        metadata=dict(manifest.get("parse_metadata") or {}),
    )

    if not parsed.total_pages:
        meta_path = doc_dir / ".meta.json"
        if meta_path.exists():
            raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            parsed.total_pages = int(raw_meta.get("total_pages") or 0)
            parsed.ocr_page_count = int(raw_meta.get("ocr_page_count") or parsed.ocr_page_count)
            parsed.table_count = int(raw_meta.get("table_count") or 0)

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    source_record = manifest.get("source_file") if isinstance(manifest.get("source_file"), dict) else {}
    return parsed, metadata, source_record


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


def _load_section_records(docs_dir: Path, doc_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _validate_doc_id(doc_id)
    doc_dir = docs_dir / doc_id
    manifest_path = doc_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, f"manifest unreadable for {doc_id}: {exc}") from exc
    sections_meta = manifest.get("sections")
    if not isinstance(sections_meta, list):
        raise HTTPException(500, f"manifest missing sections for {doc_id}")

    records: list[dict[str, Any]] = []
    for sec in sorted(
        (item for item in sections_meta if isinstance(item, dict)),
        key=lambda item: int(item.get("index", item.get("order", 0)) or 0),
    ):
        section_path = _resolve_manifest_section_path(doc_dir, sec.get("file", ""))
        if not section_path or not section_path.exists():
            continue
        raw = section_path.read_text(encoding="utf-8")
        text = _strip_section_storage_wrapper(raw)
        page_start = sec.get("page_start")
        page_end = sec.get("page_end")
        if page_start is None and page_end is None:
            page_start, page_end = _page_bounds(sec.get("page_range"))
        token_estimate = int(sec.get("token_estimate") or _estimate_tokens(text))
        record = {
            **sec,
            "doc_id": doc_id,
            "text": text,
            "page_start": page_start,
            "page_end": page_end,
            "char_count": len(text),
            "token_estimate": token_estimate,
        }
        records.append(record)
    return manifest, records


def _make_chunk(
    doc_id: str,
    index: int,
    records: list[dict[str, Any]],
    text: str,
    *,
    include_text: bool,
) -> dict[str, Any]:
    section_ids = [str(r.get("sid") or "") for r in records if r.get("sid")]
    page_starts = [r.get("page_start") for r in records if isinstance(r.get("page_start"), int)]
    page_ends = [r.get("page_end") for r in records if isinstance(r.get("page_end"), int)]
    chunk = {
        "chunk_id": f"chunk-{index:04d}",
        "doc_id": doc_id,
        "index": index,
        "section_ids": section_ids,
        "title": " / ".join(str(r.get("title") or "") for r in records[:3]).strip(" / "),
        "page_start": min(page_starts) if page_starts else None,
        "page_end": max(page_ends) if page_ends else None,
        "char_count": len(text),
        "token_estimate": _estimate_tokens(text),
        "provenance": [
            {
                "doc_id": doc_id,
                "sid": r.get("sid"),
                "title": r.get("title"),
                "page_start": r.get("page_start"),
                "page_end": r.get("page_end"),
                "token_estimate": r.get("token_estimate"),
            }
            for r in records
        ],
    }
    if include_text:
        chunk["text"] = text
    return chunk


def _split_text_by_token_estimate(
    record: dict[str, Any],
    *,
    max_tokens: int,
    overlap_tokens: int,
    include_text: bool,
    start_index: int,
) -> list[dict[str, Any]]:
    text = str(record.get("text") or "")
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[dict[str, Any]] = []
    current_parts: list[str] = []
    current_tokens = 0
    chunk_index = start_index
    for para in paragraphs:
        para_tokens = _estimate_tokens(para)
        if current_parts and current_tokens + para_tokens > max_tokens:
            chunk_text = "\n\n".join(current_parts).strip()
            chunks.append(
                _make_chunk(
                    str(record["doc_id"]),
                    chunk_index,
                    [record],
                    chunk_text,
                    include_text=include_text,
                )
            )
            chunk_index += 1
            if overlap_tokens:
                overlap_chars = max(0, int(overlap_tokens * 4))
                current_parts = [chunk_text[-overlap_chars:]] if overlap_chars else []
                current_tokens = _estimate_tokens(current_parts[0]) if current_parts else 0
            else:
                current_parts = []
                current_tokens = 0
        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        chunks.append(
            _make_chunk(
                str(record["doc_id"]),
                chunk_index,
                [record],
                "\n\n".join(current_parts).strip(),
                include_text=include_text,
            )
        )
    return chunks


def _chunk_sections(
    doc_id: str,
    records: list[dict[str, Any]],
    request: ChunkRequest,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_records: list[dict[str, Any]] = []
    current_parts: list[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current_records, current_parts, current_tokens
        if not current_records:
            return
        chunks.append(
            _make_chunk(
                doc_id,
                len(chunks) + 1,
                current_records,
                "\n\n".join(current_parts).strip(),
                include_text=request.include_text,
            )
        )
        current_records = []
        current_parts = []
        current_tokens = 0

    for record in records:
        text = str(record.get("text") or "")
        tokens = int(record.get("token_estimate") or _estimate_tokens(text))
        if tokens > request.max_tokens_per_chunk:
            flush()
            split_chunks = _split_text_by_token_estimate(
                record,
                max_tokens=request.max_tokens_per_chunk,
                overlap_tokens=request.overlap_tokens,
                include_text=request.include_text,
                start_index=len(chunks) + 1,
            )
            chunks.extend(split_chunks)
            continue

        can_merge = (
            request.merge_short_sections
            and current_records
            and current_tokens + tokens <= request.max_tokens_per_chunk
            and (current_tokens < request.merge_threshold_tokens or tokens < request.merge_threshold_tokens)
        )
        if not current_records or can_merge:
            current_records.append(record)
            current_parts.append(text)
            current_tokens += tokens
            continue

        flush()
        current_records.append(record)
        current_parts.append(text)
        current_tokens = tokens

    flush()
    return chunks


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
    summary_mode: str | None = Form(None),
    document_profile: str | None = Form(None),
    field_ocr_config: str | None = Form(None),
    parse_mode: str | None = Form(None),
    id_strategy: str | None = Form(None),
    skip_ocr_pages: str | None = Form(None),
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
        requested_parse_mode = (
            str(parse_mode or parsed_metadata.get("parse_mode") or "").strip()
            or os.environ.get("LARKSCOUT_PDF_PARSE_MODE", "").strip()
            or None
        )
        field_ocr_profile = (
            str(document_profile or parsed_metadata.get("document_profile") or "").strip()
            or str(parsed_metadata.get("field_ocr_profile") or "").strip()
            or os.environ.get("LARKSCOUT_FIELD_OCR_PROFILE", "").strip()
            or None
        )
        requested_field_ocr_config = (
            str(field_ocr_config or parsed_metadata.get("field_ocr_config") or "").strip()
            or os.environ.get("LARKSCOUT_FIELD_OCR_CONFIG", "").strip()
            or None
        )
        requested_summary_mode = (
            str(summary_mode or parsed_metadata.get("summary_mode") or "").strip()
            or None
        )
        for key, value in {
            "summary_mode": requested_summary_mode,
            "document_profile": field_ocr_profile,
            "field_ocr_config": requested_field_ocr_config,
            "parse_mode": requested_parse_mode,
            "id_strategy": id_strategy,
            "skip_ocr_pages": skip_ocr_pages,
        }.items():
            if value:
                parsed_metadata.setdefault(key, value)
        manual_blank_pages_spec = (
            _metadata_page_range_spec(skip_ocr_pages)
            or _metadata_page_range_spec(parsed_metadata.get("skip_ocr_pages"))
            or _metadata_page_range_spec(parsed_metadata.get("blank_pages"))
            or _metadata_page_range_spec(parsed_metadata.get("near_blank_pages"))
            or _metadata_page_range_spec(parsed_metadata.get("manual_blank_pages"))
        )

        # Check format
        filename = file.filename or "unknown"
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(422, t("unsupported_format", fmt=suffix))

        profile = None
        if suffix == ".pdf":
            profile = _load_document_profile(field_ocr_profile, requested_field_ocr_config)
        summary_mode = _resolve_summary_mode(
            profile=profile,
            parse_mode=requested_parse_mode,
            generate_summary=generate_summary,
            requested_mode=requested_summary_mode,
        )

        # Parse tags
        parsed_tags: list[str] = []
        if tags:
            try:
                parsed_tags = json.loads(tags)
            except json.JSONDecodeError:
                parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

        content = b""
        try:
            content = await file.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    413, f"file too large: {len(content)} bytes (max {MAX_UPLOAD_BYTES})"
                )
            # Avoid touching counters or storage for requests rejected by size validation.
            d_id = _resolve_doc_id(docs_dir, filename, doc_id, id_strategy)
            tmp_dir = docs_dir / d_id / ".tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / filename
            tmp_path.write_bytes(content)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, t("file_save_failed", err=str(e)))

        # Parse
        try:
            loop = asyncio.get_event_loop()
            if suffix == ".pdf":
                should_prewarm_local_ocr = False
                if PREWARM_LOCAL_OCR:
                    try:
                        should_prewarm_local_ocr = _should_prewarm_local_ocr_for_pdf(
                            tmp_path,
                            profile=profile,
                            parse_mode=requested_parse_mode,
                            force_ocr=force_ocr,
                            ocr_pages_spec=ocr_pages,
                            manual_blank_pages_spec=manual_blank_pages_spec,
                            ocr_threshold=OCR_THRESHOLD,
                        )
                    except Exception as exc:
                        logger.warning("Local OCR prewarm planning skipped before parse: %s", exc)
                if should_prewarm_local_ocr:
                    try:
                        _get_paddle_ocr()
                        logger.info("Local OCR backend prewarmed before PDF parse")
                    except Exception as exc:
                        logger.warning("Local OCR prewarm skipped before parse: %s", exc)
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
                        field_ocr_profile=field_ocr_profile,
                        field_ocr_config=requested_field_ocr_config,
                        parse_mode=requested_parse_mode,
                        manual_blank_pages_spec=manual_blank_pages_spec,
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
            if summary_mode == "sync":
                _set_summary_metadata(parsed, mode="sync", status="running")
                digest_text, brief_text, _ = await loop.run_in_executor(
                    None, lambda: generate_summaries(parsed, concurrency=concurrency)
                )
                digest = digest_text
                _set_summary_metadata(parsed, mode="sync", status="completed")
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
                status = "disabled" if summary_mode == "off" else "pending"
                _set_summary_metadata(parsed, mode=summary_mode, status=status)
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
                if summary_mode == "defer":
                    worker = threading.Thread(
                        target=_generate_deferred_summary,
                        args=(
                            d_id,
                            parsed,
                            docs_dir,
                            concurrency,
                            parsed_tags,
                            parsed_metadata,
                            source_record,
                        ),
                        daemon=True,
                    )
                    worker.start()
                    logger.info("Deferred summary scheduled: %s", d_id)
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
            summary_mode=d.get("summary_mode") or None,
            summary_status=d.get("summary_status") or None,
            summary_error_code=d.get("summary_error_code") or None,
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
                            summary_mode=d.get("summary_mode") or None,
                            summary_status=d.get("summary_status") or None,
                            summary_error_code=d.get("summary_error_code") or None,
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
                        summary_mode=d.get("summary_mode") or None,
                        summary_status=d.get("summary_status") or None,
                        summary_error_code=d.get("summary_error_code") or None,
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


@app.post("/library/{doc_id}/search_sections", response_model=SearchResponse)
async def search_sections(doc_id: str, request: SectionSearchRequest):
    """Search within one document's section files and return sid/page provenance."""
    query = request.q.strip()
    if not query:
        raise HTTPException(422, "q is required")

    docs_dir = _get_docs_dir()
    manifest, records = _load_section_records(docs_dir, doc_id)
    needle = query if request.case_sensitive else query.lower()
    results: list[SearchResult] = []
    for record in records:
        title = str(record.get("title") or "")
        text = str(record.get("text") or "")
        title_haystack = title if request.case_sensitive else title.lower()
        text_haystack = text if request.case_sensitive else text.lower()
        title_hit = needle in title_haystack
        text_hit = needle in text_haystack
        if not (title_hit or text_hit):
            continue
        results.append(
            SearchResult(
                doc_id=doc_id,
                filename=str(manifest.get("filename") or ""),
                file_type=str(manifest.get("file_type") or ""),
                digest="",
                tags=[],
                source=str(manifest.get("source") or "upload"),
                score=_search_score((title_hit, 2.0), (text_hit, 1.5)),
                metadata=manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {},
                source_ref=(manifest.get("source_file") or {}).get("ref") if isinstance(manifest.get("source_file"), dict) else None,
                source_filename=(manifest.get("source_file") or {}).get("filename") if isinstance(manifest.get("source_file"), dict) else None,
                source_available=bool((manifest.get("source_file") or {}).get("ref")) if isinstance(manifest.get("source_file"), dict) else False,
                sid=record.get("sid"),
                section_title=title,
                page_range=record.get("page_range"),
                page_start=record.get("page_start"),
                page_end=record.get("page_end"),
                snippet=_make_snippet(text if text_hit else title, query),
                content=text if request.include_content else None,
            )
        )
    results.sort(key=lambda item: item.score, reverse=True)
    total = len(results)
    return SearchResponse(results=results[: request.limit], total=total)


@app.post("/library/{doc_id}/chunks")
async def chunk_document(doc_id: str, request: ChunkRequest):
    """Build generic section-boundary chunks for downstream skills."""
    docs_dir = _get_docs_dir()
    _, records = _load_section_records(docs_dir, doc_id)
    chunks = _chunk_sections(doc_id, records, request)
    return {
        "doc_id": doc_id,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "config": request.model_dump() if hasattr(request, "model_dump") else request.dict(),
    }


@app.get("/library/{doc_id}/summary")
async def get_summary_status(doc_id: str):
    _validate_doc_id(doc_id)
    manifest_path = _get_docs_dir() / doc_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, t("doc_not_found", doc_id=doc_id))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parse_metadata = manifest.get("parse_metadata") if isinstance(manifest.get("parse_metadata"), dict) else {}
    summary = parse_metadata.get("summary") if isinstance(parse_metadata.get("summary"), dict) else {}
    return {
        "doc_id": doc_id,
        "summary": summary,
        "paths": manifest.get("paths") or {},
    }


@app.post("/library/{doc_id}/summary")
async def retry_summary(doc_id: str, concurrency: int = 3, force: bool = False):
    _validate_doc_id(doc_id)
    docs_dir = _get_docs_dir()
    parsed, metadata, source_record = _load_parsed_document_from_storage(docs_dir, doc_id)
    tags = _load_doc_tags(docs_dir, doc_id)

    summary_meta = parsed.metadata.get("summary") if isinstance(parsed.metadata, dict) else {}
    current_status = summary_meta.get("status") if isinstance(summary_meta, dict) else None
    attempts = _current_summary_attempts(parsed)
    if current_status == "running" and not force:
        raise HTTPException(409, f"summary already running for {doc_id}")
    if attempts >= DEFERRED_SUMMARY_MAX_ATTEMPTS and not force:
        raise HTTPException(409, f"summary attempt limit reached for {doc_id}")

    _set_summary_metadata(parsed, mode="defer", status="pending", attempts=attempts)
    write_output_extract_only(
        doc_id,
        parsed,
        docs_dir,
        tags=tags,
        source="upload",
        metadata=metadata,
        source_record=source_record,
        summary_placeholder=_summary_placeholder_text("pending"),
    )
    worker = threading.Thread(
        target=_generate_deferred_summary,
        args=(
            doc_id,
            parsed,
            docs_dir,
            concurrency,
            tags,
            metadata,
            source_record,
        ),
        daemon=True,
    )
    worker.start()
    logger.info("Deferred summary retry scheduled: %s", doc_id)
    return {
        "doc_id": doc_id,
        "scheduled": True,
        "summary": parsed.metadata.get("summary"),
        "limits": {
            "max_attempts": DEFERRED_SUMMARY_MAX_ATTEMPTS,
            "timeout_sec": DEFERRED_SUMMARY_TIMEOUT_SEC,
            "max_concurrent": DEFERRED_SUMMARY_MAX_CONCURRENT,
        },
    }


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
