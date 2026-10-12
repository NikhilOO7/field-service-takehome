"""Turn parsed blocks into chunks (spec M5).

- A chunk never spans two sections.
- Consecutive blocks with the same merge_key are merged up to MAX_CHARS (about 400 tokens);
  blocks with merge_key=None (fault-code rows, procedure steps, records, messages) stay alone.
- Over-long prose is split on sentence boundaries.
- embed_text = contextual header + text. The header (document, revision, date, models, section, page)
  is what lets a lone table row be retrieved for the right manual and model.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from app.ingestion.types import Block, ParsedDocument

MAX_CHARS = 1600

DOC_TYPE_LABEL = {
    "oem_bulletin": "OEM service bulletin",
    "oem_manual": "OEM manual",
    "controlled_procedure": "Corvane controlled procedure",
    "legacy_wi": "Legacy Kyltek work instruction",
    "wiki": "Confluence wiki (uncontrolled)",
    "email": "Email",
    "chat": "Teams chat",
    "cmms_export": "Maintenance history",
    "case_export": "Salesforce case",
    "asset_register": "Fleet register",
    "ops_review": "Ops review",
    "record": "Record",
}


@dataclass
class ChunkDraft:
    section_index: int
    ord: int
    kind: str
    text: str
    embed_text: str
    locator: dict
    event_date: date | None = None
    asset_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    record_refs: list[str] = field(default_factory=list)


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    parts, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > MAX_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def _merge_locators(locators: list[dict]) -> dict:
    loc = dict(locators[0])
    pages = [p for p in (l.get("page") for l in locators) if p]
    if pages and min(pages) != max(pages):
        loc["page"] = min(pages)
        loc["page_to"] = max(pages)
    labels = [l.get("label") for l in locators if l.get("label")]
    if len(labels) > 1:
        loc["label"] = f"{labels[0]} – {labels[-1]}"
    return loc


def context_header(doc: dict, heading_path: list[str], locator: dict) -> str:
    parts = [doc["title"], DOC_TYPE_LABEL.get(doc["doc_type"], doc["doc_type"])]
    if doc.get("doc_number"):
        parts.append(doc["doc_number"] + (f" Rev {doc['revision']}" if doc.get("revision") else ""))
    if doc.get("effective_date"):
        parts.append(str(doc["effective_date"]))
    if doc.get("status") and doc["status"] != "current":
        parts.append(doc["status"].upper())
    if doc.get("models"):
        parts.append("models: " + ", ".join(doc["models"]))
    heading = " > ".join(h for h in heading_path if h not in {"(start)", "(page)"})
    if heading:
        parts.append(heading)
    if locator.get("page"):
        parts.append(f"p.{locator['page']}")
    return "[" + " | ".join(parts) + "]"


def build_chunks(parsed: ParsedDocument, doc: dict) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    ord_ = 0
    for s_i, section in enumerate(parsed.sections):
        groups: list[list[Block]] = []
        for block in section.blocks:
            if (
                groups
                and block.merge_key is not None
                and groups[-1][-1].merge_key == block.merge_key
                and sum(len(b.text) for b in groups[-1]) + len(block.text) <= MAX_CHARS
            ):
                groups[-1].append(block)
            else:
                groups.append([block])
        for group in groups:
            text = "\n".join(b.text for b in group)
            pieces = _split_long(text) if len(group) == 1 and group[0].kind == "prose" else [text]
            locator = _merge_locators([b.locator for b in group])
            if section.section_no and not locator.get("section"):
                locator["section"] = section.section_no
            locator.setdefault("heading", section.heading_path[-1] if section.heading_path else None)
            kind = group[0].kind if len({b.kind for b in group}) == 1 else "prose"
            for piece in pieces:
                chunks.append(
                    ChunkDraft(
                        section_index=s_i,
                        ord=ord_,
                        kind=kind,
                        text=piece,
                        embed_text=f"{context_header(doc, section.heading_path, locator)}\n{piece}",
                        locator=locator,
                        event_date=next((b.event_date for b in group if b.event_date), None),
                        asset_ids=sorted({a for b in group for a in b.asset_ids}),
                        flags=sorted({f for b in group for f in b.flags}),
                        record_refs=[b.record_ref for b in group if b.record_ref],
                    )
                )
                ord_ += 1
    return chunks
