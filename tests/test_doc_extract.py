#!/usr/bin/env python3
"""Tests for doc_extract.py: text extraction and context building."""

import pytest

import doc_extract as de


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------

def test_extract_text_from_txt():
    text = de.extract_text("notes.txt", "hello world".encode("utf-8"))
    assert text == "hello world"


def test_extract_text_from_md():
    text = de.extract_text("readme.md", "# Title\nBody".encode("utf-8"))
    assert "# Title" in text and "Body" in text


def test_extract_text_replaces_invalid_utf8():
    text = de.extract_text("data.txt", b"ok \xff\xfe bytes")
    assert "ok" in text


def test_extract_text_unsupported_type_raises():
    with pytest.raises(de.UnsupportedDocumentError):
        de.extract_text("image.png", b"\x89PNG")


def test_extract_text_unsupported_type_without_extension_raises():
    with pytest.raises(de.UnsupportedDocumentError):
        de.extract_text("noext", b"data")


def test_extract_text_from_xlsx():
    pytest.importorskip("openpyxl")
    import io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["name", "qty"])
    sheet.append(["widget", 5])
    buffer = io.BytesIO()
    workbook.save(buffer)

    text = de.extract_text("report.xlsx", buffer.getvalue())

    assert "[Sheet: Data]" in text
    assert "name | qty" in text
    assert "widget | 5" in text


def test_extract_text_xlsx_skips_blank_rows():
    pytest.importorskip("openpyxl")
    import io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["a", "b"])
    sheet.append([None, None])
    buffer = io.BytesIO()
    workbook.save(buffer)

    text = de.extract_text("blank.xlsx", buffer.getvalue())

    assert text.count("\n") == 1  # header row only


def test_extract_text_csv_formats_rows_with_pipes():
    csv_data = b"name,role\nAlice,admin\n"
    text = de.extract_text("people.csv", csv_data)
    assert "name | role" in text
    assert "Alice | admin" in text


def test_extract_text_from_pdf():
    pytest.importorskip("pypdf")
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    # A blank (scanned-like) page raises an actionable error instead of
    # silently embedding empty content the model cannot use.
    try:
        de.extract_text("doc.pdf", buffer.getvalue())
        raised = False
    except de.UnsupportedDocumentError as exc:
        raised = True
        assert "scanned" in str(exc) or "image-only" in str(exc)
    assert raised


def test_extract_text_docx_without_library_skips(monkeypatch):
    # python-docx may be absent; verify a graceful failure path exists.
    try:
        import docx  # noqa: F401
        has_docx = True
    except ImportError:
        has_docx = False
    if not has_docx:
        with pytest.raises(ImportError):
            de.extract_text("file.docx", b"bytes")


# ---------------------------------------------------------------------------
# clamping
# ---------------------------------------------------------------------------

def test_large_text_is_truncated():
    big = "a" * (de.MAX_CHARS + 1000)
    text = de.extract_text("big.txt", big.encode("utf-8"))
    assert len(text) < len(big)
    assert "truncated" in text


# ---------------------------------------------------------------------------
# build_document_context
# ---------------------------------------------------------------------------

def test_build_document_context_empty():
    assert de.build_document_context([]) == ""


def test_build_document_context_includes_names_and_text():
    docs = [
        {"name": "a.txt", "text": "content A"},
        {"name": "b.md", "text": "content B"},
    ]
    context = de.build_document_context(docs)
    assert "a.txt" in context and "content A" in context
    assert "b.md" in context and "content B" in context
    assert context.startswith("The user has attached")
