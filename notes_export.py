#!/usr/bin/env python3
"""Exports for meeting notes. Everything is derived from the notes' Markdown, so inline edits
the user made are always included: Markdown, email-ready HTML (inline styles: pastes cleanly
into Outlook/Gmail, Teams and Confluence), DOCX, PDF, action items as CSV, and a follow-up
email draft."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import markdown as md_lib

FONT_CANDIDATES = ["/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
STYLE = {
    "h1": "font-family:Arial,Helvetica,sans-serif;font-size:22px;margin:0 0 8px;color:#111",
    "h2": "font-family:Arial,Helvetica,sans-serif;font-size:17px;margin:20px 0 8px;color:#111;border-bottom:1px solid #e5e5e5;padding-bottom:4px",
    "h3": "font-family:Arial,Helvetica,sans-serif;font-size:15px;margin:14px 0 6px;color:#111",
    "p": "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;margin:6px 0;color:#222",
    "li": "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222",
    "table": "border-collapse:collapse;margin:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px",
    "th": "border:1px solid #d0d0d0;background:#f3f4f6;padding:6px 8px;text-align:left;font-weight:bold",
    "td": "border:1px solid #d0d0d0;padding:6px 8px;vertical-align:top",
    "blockquote": "border-left:3px solid #d0d0d0;margin:8px 0;padding:2px 10px;color:#444",
    "a": "color:#2563eb",
}


def to_html(markdown_text: str) -> str:
    """Meeting notes Markdown as HTML (tables and lists supported), the base of the email and PDF exports."""
    return md_lib.markdown(markdown_text, extensions=["tables", "sane_lists", "nl2br"])


def email_html(markdown_text: str, title: str = "Meeting notes") -> str:
    """Self-contained HTML with inline styles (what email clients and Teams keep on paste)."""
    body = to_html(markdown_text)
    for tag, style in STYLE.items():
        body = re.sub(rf"<{tag}(\s[^>]*)?>", lambda m, t=tag, s=style: f'<{t}{m.group(1) or ""} style="{s}">', body)
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + _esc(title) + "</title></head>"
            '<body style="margin:0;padding:16px;background:#ffffff">' + body + "</body></html>")


def _esc(text: str) -> str:
    """Escapes &, < and > so text is shown literally in HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plain_text(markdown_text: str) -> str:
    """Readable text for chat apps: links become their label, table rows become "a — b — c"."""
    out = []
    for line in markdown_text.splitlines():
        if re.fullmatch(r"\s*\|?\s*:?-{3,}.*", line):
            continue
        if line.strip().startswith("|"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            line = " — ".join(c for c in cells if c and c != "—")
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = line.replace("\\|", "|")
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


# ---------------------------------------------------------------------------
# Tables in the Markdown
# ---------------------------------------------------------------------------

def _cells(row: str) -> list[str]:
    """The cells of one Markdown table row ('| a | b |' -> ['a', 'b']); a pipe escaped with a backslash stays inside a cell."""
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]


def section_table(markdown_text: str, heading: str) -> list[dict]:
    """Rows of the first table under the `## heading` section, keyed by column name."""
    lines = markdown_text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(rf"#+\s*{re.escape(heading)}\b", l, re.I)), None)
    if start is None:
        return []
    rows, header = [], None
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if not line.strip().startswith("|"):
            if header:
                break
            continue
        if re.fullmatch(r"\s*\|?\s*:?-{3,}.*", line):
            continue
        cells = _cells(line)
        if header is None:
            header = cells
        else:
            rows.append(dict(zip(header, cells)))
    return rows


def _unlink(text: str) -> str:
    """Replaces Markdown links '[text](url)' with just their text."""
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text or "").strip()


def actions_csv(markdown_text: str) -> str:
    """The 'Action items' table of the notes as CSV (number, action, owner, due date, priority, timestamp, link)
    for spreadsheets or task trackers.
    """
    rows = section_table(markdown_text, "Action items")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["#", "Action", "Owner", "Due date", "Priority", "Timestamp", "Link"])
    for i, row in enumerate(rows, start=1):
        ts = row.get("Timestamp", "")
        link = re.search(r"\]\(([^)]+)\)", ts)
        writer.writerow([row.get("#", i), _unlink(row.get("Action", "")), _unlink(row.get("Owner", "")),
                         row.get("Due date", ""), "" if row.get("Priority") == "—" else row.get("Priority", ""),
                         _unlink(ts), link.group(1) if link else ""])
    return buf.getvalue()


def _bullets(markdown_text: str, heading: str) -> list[str]:
    """The bullet points under a heading of the notes, as plain text lines."""
    lines = markdown_text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(rf"##+\s*{re.escape(heading)}\b", l, re.I)), None)
    if start is None:
        return []
    out = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        m = re.match(r"\s*[-*]\s+(.*)", line)
        if m:
            out.append(_unlink(m.group(1)).replace("**", ""))
    return out


def follow_up_email(markdown_text: str) -> dict:
    """A neutral follow-up email from the notes (no model involved, so nothing is invented)."""
    title = next((l[2:].strip() for l in markdown_text.splitlines() if l.startswith("# ")), "Meeting")
    date = re.search(r"\*\*Date:\*\*\s*([^·\n]+)", markdown_text)
    subject = f"Follow-up: {title}" + (f" ({date.group(1).strip()})" if date else "")
    tldr = _bullets(markdown_text, "Summary") or _bullets(markdown_text, "TL;DR")  # notes saved before the rename say TL;DR
    decisions = [r.get("Decision", "") for r in section_table(markdown_text, "Decisions")]
    actions = section_table(markdown_text, "Action items")
    next_lines = _bullets(markdown_text, "Next steps")
    lines = ["Hi all,", "", f"Thanks for joining {title}. Here's a short recap.", ""]
    if tldr:
        lines += ["Summary:"] + [f"• {t}" for t in tldr] + [""]
    if decisions:
        lines += ["Decisions:"] + [f"• {_unlink(d)}" for d in decisions if d] + [""]
    if actions:
        lines += ["Action items:"]
        for a in actions:
            owner, due = _unlink(a.get("Owner", "Unassigned")), a.get("Due date", "TBD")
            lines.append(f"• {_unlink(a.get('Action', ''))} — {owner} — {due}")
        lines.append("")
    if next_lines:
        lines += ["Next steps:"] + [f"• {n}" for n in next_lines] + [""]
    lines += ["Please reply if I've missed or misstated anything.", "", "Thanks,"]
    text = "\n".join(lines)
    html = "".join(f"<p style=\"{STYLE['p']}\">{_esc(l) or '&nbsp;'}</p>" for l in lines)
    return {"subject": subject, "text": text, "html": html}


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def _runs(paragraph, text: str) -> None:
    """**bold** and [label](url) → runs (links become underlined blue text)."""
    from docx.shared import RGBColor
    pos = 0
    for m in re.finditer(r"\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)|_([^_]+)_", text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()].replace("\\|", "|"))
        if m.group(1):
            paragraph.add_run(m.group(1)).bold = True
        elif m.group(2):
            run = paragraph.add_run(m.group(2))
            run.underline = True
            run.font.color.rgb = RGBColor(0x25, 0x63, 0xEB)
        else:
            paragraph.add_run(m.group(4)).italic = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:].replace("\\|", "|"))


def to_docx(markdown_text: str) -> bytes:
    """The notes as a Word document: headings, paragraphs, bullet lists and tables."""
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    doc.styles["Normal"].font.name = "Arial"
    doc.styles["Normal"].font.size = Pt(10.5)
    lines = markdown_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.fullmatch(r"\s*\|?\s*:?-{3,}.*", lines[i]):
                    block.append(_cells(lines[i]))
                i += 1
            if block:
                table = doc.add_table(rows=len(block), cols=len(block[0]))
                table.style = "Table Grid"
                for r, row in enumerate(block):
                    for c, cell in enumerate(row[: len(block[0])]):
                        para = table.cell(r, c).paragraphs[0]
                        _runs(para, cell)
                        if r == 0:
                            for run in para.runs:
                                run.bold = True
            continue
        heading = re.match(r"(#{1,3})\s+(.*)", line)
        bullet = re.match(r"\s*[-*]\s+(.*)", line)
        quote = re.match(r">\s?(.*)", line)
        if heading:
            doc.add_heading(heading.group(2).strip(), level=len(heading.group(1)))
        elif bullet:
            _runs(doc.add_paragraph(style="List Bullet"), bullet.group(1))
        elif quote:
            para = doc.add_paragraph(style="Intense Quote" if "Intense Quote" in [s.name for s in doc.styles] else None)
            _runs(para, quote.group(1))
        elif line.strip():
            _runs(doc.add_paragraph(), line.strip().rstrip("\\").rstrip())
        i += 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

WIDTHS = {  # column widths (%) for the notes' tables
    ("#", "Action", "Owner", "Due date", "Priority", "Timestamp"): [5, 38, 16, 15, 11, 15],
    ("Decision", "Made by", "Rationale", "Timestamp"): [38, 16, 31, 15],
    ("Item", "Type", "Impact", "Owner", "Mitigation"): [28, 12, 22, 14, 24],
}
FONT_DIR = Path("/System/Library/Fonts/Supplemental")


def _pdf_fonts(pdf) -> str:
    """Registers fonts for the PDF: Arial when available plus a Unicode fallback (so names with accents print).
    Returns the font family to use, or '' for the built-in fonts.
    """
    regular = {"": "Arial.ttf", "B": "Arial Bold.ttf", "I": "Arial Italic.ttf", "BI": "Arial Bold Italic.ttf"}
    if all((FONT_DIR / f).exists() for f in regular.values()):
        for style, name in regular.items():
            pdf.add_font("Body", style, str(FONT_DIR / name))
        unicode_font = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
        if unicode_font:
            pdf.add_font("Fallback", "", unicode_font)
            pdf.set_fallback_fonts(["Fallback"], exact_match=False)
        return "Body"
    unicode_font = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    if unicode_font:
        for style in ("", "B", "I", "BI"):
            pdf.add_font("Body", style, unicode_font)
        return "Body"
    return ""


def _sized_tables(html: str) -> str:
    """Gives the notes' HTML tables column widths and borders that the PDF renderer understands."""
    def size(match):
        """Rewrites one <table>: known column sets get tuned widths, others equal widths."""
        table = match.group(0)
        headers = tuple(re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S))
        widths = WIDTHS.get(headers) or [round(100 / max(1, len(headers)))] * len(headers)
        cells = iter(widths)
        table = re.sub(r"<th(\s[^>]*)?>", lambda m: f'<th width="{next(cells, 10)}%" align="left">', table)
        table = re.sub(r"<td(\s[^>]*)?>", '<td align="left">', table)
        return table.replace("<table>", '<table width="100%" border="1">')
    return re.sub(r"<table>.*?</table>", size, html, flags=re.S)


def to_pdf(markdown_text: str, title: str = "Meeting notes") -> bytes:
    """The notes as an A4 PDF (rendered from their HTML)."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace
    pdf = FPDF(format="A4")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, 16)
    pdf.set_title(title)
    pdf.add_page()
    family = _pdf_fonts(pdf) or "Helvetica"
    html = _sized_tables(to_html(markdown_text))
    if family == "Helvetica":  # core fonts are Latin-1 only
        html = html.encode("latin-1", "replace").decode("latin-1")
    dark = (17, 17, 17)
    styles = {"h1": FontFace(color=dark, size_pt=19, emphasis="BOLD"), "h2": FontFace(color=dark, size_pt=14, emphasis="BOLD"),
              "h3": FontFace(color=dark, size_pt=12, emphasis="BOLD"), "a": FontFace(color=(37, 99, 235))}
    pdf.set_font(family, size=10)
    try:
        pdf.write_html(html, font_family=family, tag_styles=styles, table_line_separators=True)
    except TypeError:  # older fpdf2 without tag_styles
        pdf.write_html(html, font_family=family, table_line_separators=True)
    return bytes(pdf.output())
