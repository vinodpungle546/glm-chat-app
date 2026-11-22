#!/usr/bin/env python3
"""Document text extraction for the GLM chat app.

Supports: .txt, .md, .csv, .log, .pdf (via pypdf), .docx (via python-docx),
.xlsx/.xlsm (via openpyxl). Extracted text is injected into the LLM context
to answer user queries.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm"}
MAX_CHARS = 50_000  # cap context contribution per document


class UnsupportedDocumentError(ValueError):
    """Raised when the uploaded file type is not supported."""


def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from an uploaded document's bytes."""
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = data.decode("utf-8", errors="replace")
        if suffix == ".csv":
            text = _format_csv(text)
    elif suffix == ".pdf":
        text = _extract_pdf(data)
    elif suffix == ".docx":
        text = _extract_docx(data)
    elif suffix in SPREADSHEET_SUFFIXES:
        text = _extract_xlsx(data)
    else:
        raise UnsupportedDocumentError(f"Unsupported file type: '{suffix or filename}'")
    return _clamp(text)


def _extract_pdf(data: bytes) -> str:
    """Text of every page of a PDF (pypdf). Raises UnsupportedDocumentError for scanned, image-only PDFs that
    contain no text.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not text.strip():
        raise UnsupportedDocumentError(
            "this PDF contains no extractable text (it is likely a scanned or "
            "image-only document). Please paste the relevant text into the chat, "
            "or upload a text-based PDF."
        )
    return text


def _extract_docx(data: bytes) -> str:
    """Text of a Word document: its paragraphs, one per line (python-docx)."""
    import docx  # python-docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _extract_xlsx(data: bytes) -> str:
    """Text of an Excel workbook: each sheet's name, then each non-empty row as 'cell | cell | cell' (openpyxl,
    formulas read as their saved values).
    """
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if cell is None else str(cell) for cell in row]
            if any(cells):
                parts.append(" | ".join(cells))
    workbook.close()
    return "\n".join(parts)


def _format_csv(text: str) -> str:
    """Render CSV rows with a consistent ' | ' separator for the LLM."""
    rows = list(csv.reader(io.StringIO(text)))
    return "\n".join(" | ".join(row) for row in rows if row)


def _clamp(text: str) -> str:
    """Trim very large documents so they fit comfortably in context."""
    text = text.strip()
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n...[truncated at {MAX_CHARS} characters]"
    return text


def build_document_context(documents: list[dict]) -> str:
    """Format all attached documents as one context block for the LLM."""
    if not documents:
        return ""
    parts = ["The user has attached the following documents. Use them as the primary source when answering:"]
    for doc in documents:
        parts.append(f"--- Document: {doc['name']} ---\n{doc['text']}")
    return "\n\n".join(parts)
