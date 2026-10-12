"""PDF loader (manuals, bulletins, decks) built on PyMuPDF.

- Headings: bold lines that are larger than body text, numbered ("9.1 Controller fault codes"), or
  ALL CAPS. A bold section number on its own line is merged with the bold title that follows it.
  Bold step numbers followed by regular text ("1", "c)") are not headings.
- Running headers/footers are dropped by position (top/bottom page margin).
- Tables come from page.find_tables(); each row becomes a block that repeats the header labels, so a
  single row ("P-07 | Cavitation | ... 0.30 bar") is retrievable on its own.
- Damaged files are opened in PyMuPDF's tolerant mode and marked 'partial'.
- Image-only pages (no text layer) are sent to OCR (loaders/pdf_ocr.py) when an API key is configured,
  otherwise the document is marked 'ocr_pending'.
"""

import re
from collections import Counter
from pathlib import Path

import pymupdf

from app.ingestion.entities import FAULT_RE
from app.ingestion.types import Block, ParsedDocument, Section

pymupdf.TOOLS.mupdf_display_errors(False)

HEADER_MARGIN = 0.06
FOOTER_MARGIN = 0.955
SIGNAL_WORDS = {"DANGER", "WARNING", "CAUTION", "NOTE", "STOP", "IMPORTANT", "LET OP / NOTE"}
MIN_TEXT_CHARS_PER_PAGE = 50

NUMBERED_HEADING_RE = re.compile(r"^(?:SECTION\s+)?(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.{0,80})$")
SECTION_NO_ONLY_RE = re.compile(r"^(?:SECTION\s+)?(\d{1,2}(?:\.\d{1,2}){0,3})\.?$")
STEP_TABLE_HEADERS = {"step", "item", "id"}


def _line_text(line: dict) -> str:
    return "".join(s["text"] for s in line["spans"]).strip()


def _is_bold(line: dict) -> bool:
    spans = [s for s in line["spans"] if s["text"].strip()]
    return bool(spans) and all(s["flags"] & 16 or "Bold" in s["font"] for s in spans)


def _line_size(line: dict) -> float:
    spans = [s for s in line["spans"] if s["text"].strip()]
    return max((s["size"] for s in spans), default=0.0)


def _body_size(doc: pymupdf.Document) -> float:
    sizes: Counter = Counter()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for s in line["spans"]:
                    if s["text"].strip():
                        sizes[round(s["size"], 1)] += len(s["text"])
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _is_all_caps_heading(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 4 and all(c.isupper() for c in letters) and len(text) <= 70


def _cell(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _table_blocks(table, page_no: int, section_no: str | None, idx: int) -> list[Block]:
    rows = [[_cell(c) for c in row] for row in table.extract()]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    # One-cell boxes (WARNING / NOTE panels) are prose.
    if ncols <= 2 and len(rows) == 1:
        text = " ".join(c for c in rows[0] if c)
        kind = "warning" if re.match(r"^(DANGER|WARNING|CAUTION|STOP|NOTE|IMPORTANT)", text) else "prose"
        return [Block(kind=kind, text=text, locator={"page": page_no, "section": section_no})]
    header = rows[0] if len(rows) >= 2 and all(rows[0]) else None
    body = rows[1:] if header else rows
    blocks = []
    for r_i, row in enumerate(body, start=1):
        if header:
            parts = [f"{h}: {v}" for h, v in zip(header, row) if v]
        else:
            parts = [v for v in row if v]
        text = " | ".join(parts)
        first = row[0] if row else ""
        standalone = bool(FAULT_RE.fullmatch(first)) or ncols == 2 or (
            header is not None and header[0].lower() in STEP_TABLE_HEADERS
        )
        label = first if standalone else None
        blocks.append(
            Block(
                kind="table_row",
                text=text,
                locator={"page": page_no, "section": section_no, "table": idx, "row": r_i, "label": label},
                merge_key=None if standalone else f"table:{page_no}:{idx}",
            )
        )
    return blocks


def load_pdf(path: Path, ocr=None) -> ParsedDocument:
    """Parse a text PDF. `ocr` is an optional callable(path, page_numbers) -> {page_no: markdown}."""
    out = ParsedDocument()
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - any open failure is reported, not raised
        out.parse_status = "failed"
        out.parse_notes.append(f"cannot open PDF: {exc}")
        return out

    out.page_count = doc.page_count
    out.metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
    if doc.is_repaired:
        out.parse_status = "partial"
        out.parse_notes.append("damaged PDF repaired by PyMuPDF; content may be incomplete")

    text_pages = [len(p.get_text().strip()) for p in doc]
    image_only = [i + 1 for i, n in enumerate(text_pages) if n < MIN_TEXT_CHARS_PER_PAGE]
    if doc.is_repaired:
        lost = [i + 1 for i, n in enumerate(text_pages) if n == 0]
        if lost:
            readable = [i + 1 for i, n in enumerate(text_pages) if n > 0]
            out.parse_notes.append(f"only page(s) {', '.join(map(str, readable))} readable; page(s) "
                                   f"{', '.join(map(str, lost))} could not be recovered")
        image_only = []
    elif image_only and len(image_only) == doc.page_count:
        if ocr is None:
            out.parse_status = "ocr_pending"
            out.parse_notes.append("image-only scan; OCR needs OPENAI_API_KEY (run ingest again with a key)")
            return out
        from app.ingestion.loaders.pdf_ocr import markdown_to_sections

        pages_md = ocr(path, list(range(1, doc.page_count + 1)))
        out.sections = markdown_to_sections(pages_md)
        out.parse_status = "ocr"
        out.parse_notes.append(f"OCR on {len(pages_md)} pages")
        return out

    body = _body_size(doc)
    sections: list[Section] = [Section(heading_path=["(start)"], page_from=1, page_to=1)]
    stack: list[tuple[int, str]] = []  # (depth, heading)
    skipping_toc = False

    def open_section(title: str, number: str | None, depth: int, page_no: int) -> None:
        nonlocal stack
        stack = [s for s in stack if s[0] < depth] + [(depth, title)]
        sections.append(
            Section(heading_path=[t for _, t in stack], section_no=number, page_from=page_no, page_to=page_no)
        )

    for page_index, page in enumerate(doc):
        page_no = page_index + 1
        height, width = page.rect.height, page.rect.width
        skipping_toc = False if skipping_toc == "page" else skipping_toc
        tables = page.find_tables().tables
        table_rects = [pymupdf.Rect(t.bbox) for t in tables]
        items: list[tuple[float, str, object]] = []
        for t_i, t in enumerate(tables):
            items.append((t.bbox[1], "table", (t_i, t)))
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                text = _line_text(line)
                if not text:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                if y0 < height * HEADER_MARGIN or y1 > height * FOOTER_MARGIN:
                    continue
                if re.fullmatch(r"\d{1,2}", text) and x0 > width * 0.85:
                    continue  # chapter tab at the page edge
                centre = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
                if any(r.contains(centre) for r in table_rects):
                    continue
                items.append((y0, "line", (b["number"], line, text)))
        items.sort(key=lambda it: it[0])

        paragraph: list[str] = []
        last_block_no = None

        def flush() -> None:
            nonlocal paragraph
            if paragraph:
                text = re.sub(r"\s+", " ", " ".join(paragraph)).strip()
                text = re.sub(r"(\w)- (\w)", r"\1\2", text) if text.count("- ") > 3 else text
                cur = sections[-1]
                cur.blocks.append(
                    Block(
                        kind="warning" if re.match(r"^(DANGER|WARNING|CAUTION|STOP|IMPORTANT)\b", text)
                        else "prose",
                        text=text,
                        locator={"page": page_no, "section": cur.section_no},
                        merge_key=f"sec:{len(sections)}",
                    )
                )
                cur.page_to = page_no
            paragraph = []

        pending_number: str | None = None
        i = 0
        while i < len(items):
            _, kind, payload = items[i]
            i += 1
            if kind == "table":
                flush()
                if skipping_toc:
                    continue
                t_i, table = payload
                sections[-1].blocks.extend(_table_blocks(table, page_no, sections[-1].section_no, t_i))
                sections[-1].page_to = page_no
                continue

            block_no, line, text = payload
            bold, size = _is_bold(line), _line_size(line)

            if text.strip().lower() in {"contents", "inhalt / contents", "inhalt"}:
                flush()
                skipping_toc = "page"
                continue
            if skipping_toc:
                if bold and size >= body + 2 and not NUMBERED_HEADING_RE.match(text):
                    skipping_toc = False  # a large unnumbered heading ends the table of contents
                else:
                    continue

            # Bold section number alone on its line ("1.4" / "SECTION 7"): merge with the next bold line.
            m_no = SECTION_NO_ONLY_RE.match(text)
            if bold and m_no and size >= body - 3:
                nxt = items[i] if i < len(items) else None
                if nxt and nxt[1] == "line" and _is_bold(nxt[2][1]):
                    pending_number = m_no.group(1)
                    continue
            if pending_number and bold:
                flush()
                depth = pending_number.count(".") + 1
                open_section(f"{pending_number} {text}", pending_number, depth, page_no)
                pending_number = None
                continue
            pending_number = None

            if text.strip().upper() in SIGNAL_WORDS:
                flush()  # a signal word starts a warning paragraph; it is not a heading
                paragraph.append(text.strip().upper() + ":")
                last_block_no = block_no
                continue
            m = NUMBERED_HEADING_RE.match(text)
            is_heading = bold and sum(c.isalpha() for c in text) >= 3 and len(text) <= 90 and not text.endswith(".") and (
                size >= body + 0.4
                or (m is not None and size >= body - 0.6)
                or _is_all_caps_heading(text)
            )
            if is_heading and m and len(m.group(2).split()) > 12:
                is_heading = False
            if is_heading:
                flush()
                if m:
                    number = m.group(1)
                    depth = number.count(".") + 1
                else:
                    number = None
                    depth = (stack[-1][0] + 1) if stack else 1
                    if size >= body + 3:
                        depth = 1
                open_section(text, number, depth, page_no)
                continue

            if last_block_no is not None and block_no != last_block_no and not bold:
                # A new PyMuPDF text block usually starts a new paragraph, but keep bullet markers attached.
                if paragraph and not paragraph[-1].endswith(":") and paragraph[-1] not in {"-", "•", "·"} and not re.fullmatch(
                    r"\d{1,2}\.?|[a-z]\)", paragraph[-1]
                ):
                    flush()
            paragraph.append(text)
            last_block_no = block_no
        flush()

    out.sections = [s for s in sections if s.blocks]
    if not out.sections:
        out.parse_status = "failed" if out.parse_status == "ok" else out.parse_status
        out.parse_notes.append("no text extracted")
    return out
