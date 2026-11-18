#!/usr/bin/env python3
"""Scanned/blank PDFs must raise a clear, actionable error."""

from pypdf import PdfWriter

from doc_extract import UnsupportedDocumentError, extract_text


def test_blank_pdf_raises_clear_error(tmp_path):
    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(str(pdf_path))

    try:
        extract_text("blank.pdf", pdf_path.read_bytes())
        raised = False
    except UnsupportedDocumentError as exc:
        raised = True
        assert "scanned" in str(exc) or "image-only" in str(exc)
    assert raised
