"""Common output of every loader (spec M4): sections of blocks with citation locators."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Block:
    """One citable unit of content.

    kind: prose | table_row | warning | step | comment | email_msg | chat_thread | case | case_comment |
          cmms_row | asset_row
    locator: where a citation points (page, section, step, case number, message time, ...).
    merge_key: consecutive blocks with the same non-None key may be merged into one chunk
               (e.g. paragraphs of one section, small table rows). None = always its own chunk.
    """

    kind: str
    text: str
    locator: dict
    merge_key: str | None = None
    event_date: date | None = None
    asset_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    record_ref: str | None = None  # links the block to an asset_event / asset row produced by the loader


@dataclass
class Section:
    heading_path: list[str]
    section_no: str | None = None
    page_from: int | None = None
    page_to: int | None = None
    blocks: list[Block] = field(default_factory=list)


@dataclass
class AssetEvent:
    """One history row (MaintPro, Servio, Salesforce case or comment)."""

    ref: str  # unique within the document, matches Block.record_ref
    asset_id: str | None
    source: str
    source_ref: str
    event_date: date | None
    event_type: str | None = None
    fault_code: str | None = None
    technician: str | None = None
    notes: str | None = None
    parts: str | None = None
    run_hours: int | None = None
    status: str | None = None
    linked_refs: list[str] = field(default_factory=list)


@dataclass
class AssetRow:
    ref: str
    asset_id: str
    model: str
    serial: str
    site: str
    site_name: str | None
    customer: str | None
    region: str
    install_date: date | None
    firmware: str | None
    oil_type: str | None
    contract_end: date | None
    legacy_ids: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    sections: list[Section] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    parse_status: str = "ok"  # ok | partial | ocr | ocr_pending | empty | failed
    parse_notes: list[str] = field(default_factory=list)
    page_count: int | None = None
    events: list[AssetEvent] = field(default_factory=list)
    assets: list[AssetRow] = field(default_factory=list)

    @property
    def block_count(self) -> int:
        return sum(len(s.blocks) for s in self.sections)
