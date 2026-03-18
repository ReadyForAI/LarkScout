"""Tests for XLSX parsing support in the DocReader service."""

import sys
import tempfile
from pathlib import Path

import pytest

# Ensure docreader module is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "docreader"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from larkscout_docreader import parse_xlsx


@pytest.fixture()
def sample_xlsx(tmp_path: Path) -> Path:
    """Create a minimal XLSX workbook with two sheets."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "Sales"
    ws1.append(["Region", "Q1", "Q2"])
    ws1.append(["North", 100, 200])
    ws1.append(["South", 150, 250])

    ws2 = wb.create_sheet("Expenses")
    ws2.append(["Category", "Amount"])
    ws2.append(["Rent", 5000])
    ws2.append(["Utilities", 800])

    path = tmp_path / "report.xlsx"
    wb.save(path)
    return path


def test_xlsx_parse_returns_parsed_document(sample_xlsx: Path) -> None:
    """parse_xlsx returns a ParsedDocument with correct file metadata."""
    result = parse_xlsx(sample_xlsx)

    assert result.file_type == "xlsx"
    assert result.filename == "report.xlsx"
    assert result.total_pages >= 1


def test_xlsx_each_sheet_is_a_section(sample_xlsx: Path) -> None:
    """Each non-empty worksheet becomes a separate section."""
    result = parse_xlsx(sample_xlsx)

    assert len(result.sections) == 2
    titles = [s.title for s in result.sections]
    assert "Sales" in titles
    assert "Expenses" in titles


def test_xlsx_section_text_is_markdown_table(sample_xlsx: Path) -> None:
    """Section text for each sheet is rendered as a Markdown table."""
    result = parse_xlsx(sample_xlsx)

    sales_section = next(s for s in result.sections if s.title == "Sales")
    assert "| Region |" in sales_section.text
    assert "| North |" in sales_section.text
    assert "---" in sales_section.text


def test_xlsx_table_count(sample_xlsx: Path) -> None:
    """table_count matches the number of non-empty sheets."""
    result = parse_xlsx(sample_xlsx)

    assert result.table_count == 2


def test_xlsx_sections_have_stable_sids(sample_xlsx: Path) -> None:
    """Each section has a non-empty stable ID."""
    result = parse_xlsx(sample_xlsx)

    for sec in result.sections:
        assert sec.sid, f"Section '{sec.title}' has empty sid"


def test_xlsx_empty_sheet_skipped(tmp_path: Path) -> None:
    """Completely empty sheets are skipped and not added as sections."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Name", "Value"])
    ws.append(["A", 1])
    wb.create_sheet("Empty")  # no rows added

    path = tmp_path / "partial.xlsx"
    wb.save(path)

    result = parse_xlsx(path)
    assert len(result.sections) == 1
    assert result.sections[0].title == "Data"
