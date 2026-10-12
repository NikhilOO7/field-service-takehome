"""OCR for image-only PDF pages with an OpenAI vision model (spec M4, §5.1).

Each page is rendered at 200 dpi and transcribed to Markdown (headings with section numbers, tables as
Markdown tables, both languages kept). Results are cached in var/ocr_cache by file hash + page, so OCR
runs once per page.
"""

import base64
import hashlib
import re
from pathlib import Path

import pymupdf

from app.config import get_settings
from app.ingestion.entities import FAULT_RE
from app.ingestion.types import Block, Section

OCR_PROMPT = """Transcribe this scanned manual page exactly. Rules:
- Keep every word in the original languages (German and English); do not translate or summarise.
- Headings: start the line with '#' (chapter) or '##' (sub-section) and keep the section number, e.g. '# 3 Technische Daten / Technical data' or '## 3.2 Weitere Anzugsmomente / Other tightening torques'.
- Tables: output Markdown tables with a header row; keep every number and unit exactly as printed (e.g. '0.38 - 0.50 mm').
- Handwritten notes: write them as [handwritten: ...].
- Skip the running page header and footer lines.
Output only the Markdown."""


def _cache_path(pdf_path: Path, page_no: int) -> Path:
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
    cache_dir = get_settings().var_dir / "ocr_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest}_p{page_no}.md"


def ocr_pages(pdf_path: Path, page_numbers: list[int]) -> dict[int, str]:
    """Return {page_no: markdown}. Calls the vision model only for pages not in the cache."""
    from app.llm.openai_client import get_llm

    doc = pymupdf.open(pdf_path)
    results: dict[int, str] = {}
    for page_no in page_numbers:
        cache = _cache_path(pdf_path, page_no)
        if cache.exists():
            results[page_no] = cache.read_text(encoding="utf-8")
            continue
        pix = doc[page_no - 1].get_pixmap(dpi=200)
        image_b64 = base64.b64encode(pix.tobytes("png")).decode()
        markdown = get_llm().vision_transcribe(OCR_PROMPT, image_b64)
        cache.write_text(markdown, encoding="utf-8")
        results[page_no] = markdown
    return results


HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
SECTION_NO_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s")


def _split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [re.sub(r"\s*<br\s*/?>\s*", " / ", c).strip() for c in cells]


def markdown_to_sections(pages_md: dict[int, str]) -> list[Section]:
    sections: list[Section] = [Section(heading_path=["(start)"], page_from=1, page_to=1)]
    stack: list[tuple[int, str]] = []
    for page_no in sorted(pages_md):
        lines = pages_md[page_no].splitlines()
        paragraph: list[str] = []
        table: list[list[str]] = []
        table_idx = 0

        def flush_paragraph() -> None:
            nonlocal paragraph
            if paragraph:
                cur = sections[-1]
                cur.blocks.append(
                    Block(
                        kind="prose",
                        text=" ".join(paragraph),
                        locator={"page": page_no, "section": cur.section_no, "ocr": True},
                        merge_key=f"ocr:{len(sections)}",
                        flags=["ocr"],
                    )
                )
                cur.page_to = page_no
            paragraph = []

        def flush_table() -> None:
            nonlocal table, table_idx
            if table:
                rows = [r for r in table if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in r)]
                header, body = (rows[0], rows[1:]) if len(rows) > 1 else (None, rows)
                cur = sections[-1]
                for r_i, row in enumerate(body, start=1):
                    text = " | ".join(f"{h}: {v}" for h, v in zip(header, row) if v) if header else " | ".join(row)
                    standalone = bool(row) and (bool(FAULT_RE.fullmatch(row[0])) or len(row) == 2)
                    cur.blocks.append(
                        Block(
                            kind="table_row",
                            text=text,
                            locator={"page": page_no, "section": cur.section_no, "table": table_idx,
                                     "row": r_i, "label": row[0] if standalone else None, "ocr": True},
                            merge_key=None if standalone else f"ocrtable:{page_no}:{table_idx}",
                            flags=["ocr"],
                        )
                    )
                cur.page_to = page_no
                table_idx += 1
            table = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|"):
                flush_paragraph()
                table.append(_split_row(stripped))
                continue
            flush_table()
            m = HEADING_RE.match(stripped)
            if m:
                flush_paragraph()
                title = m.group(2).strip().strip("*")
                n = SECTION_NO_RE.match(title)
                number = n.group(1) if n else None
                depth = number.count(".") + 1 if number else len(m.group(1))
                stack = [s for s in stack if s[0] < depth] + [(depth, title)]
                sections.append(
                    Section(heading_path=[t for _, t in stack], section_no=number, page_from=page_no,
                            page_to=page_no)
                )
            elif stripped:
                paragraph.append(stripped)
            else:
                flush_paragraph()
        flush_paragraph()
        flush_table()
    return [s for s in sections if s.blocks]
