"""Context pack for the answer model (spec M9.4): numbered sources S1..Sn and computed facts F1..Fn.

Every id maps to grounding text, which the validator (M11) uses to check numbers and identifiers.
"""

import re
from dataclasses import dataclass, field

import yaml
from sqlalchemy import Connection, text

from app.answer.render import citation_label
from app.auth.acl import acl_clause, acl_params
from app.auth.users import UserContext
from app.config import get_settings
from app.retrieval.search import Candidate
from app.retrieval.structured import Asset, BulletinMatch, HistoryFacts

MAX_SOURCES = 40
MAX_TOTAL_CHARS = 70000
PROCEDURE_TYPES = {"controlled_procedure", "oem_bulletin", "legacy_wi"}
MAX_SOURCE_CHARS = 2400


@dataclass
class Source:
    id: str
    chunk_id: str
    label: str
    doc_id: str
    doc_title: str
    doc_type: str
    doc_number: str | None
    doc_status: str
    authority_rank: int
    text: str
    locator: dict
    models: list[str]
    flags: list[str]
    parse_status: str = "ok"
    parse_notes: str | None = None


@dataclass
class Fact:
    id: str
    label: str
    text: str
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class ConflictInfo:
    id: str
    topic: str
    positions: list[str]
    resolution: str
    resolution_source: str | None  # S-id of the chunk that states the governing rule


@dataclass
class ContextPack:
    sources: list[Source]
    facts: list[Fact]
    conflicts: list[ConflictInfo]
    question: str

    def grounding(self, cid: str) -> str:
        if cid == "Q":
            return self.question
        for s in self.sources:
            if s.id == cid:
                return f"{s.label}\n{s.text}"
        for f in self.facts:
            if f.id == cid:
                return f.text
        return ""

    def ids(self) -> set[str]:
        return {s.id for s in self.sources} | {f.id for f in self.facts} | {"Q"}

    def source(self, sid: str) -> Source | None:
        return next((s for s in self.sources if s.id == sid), None)

    def chunk_ids(self) -> list[str]:
        return [s.chunk_id for s in self.sources] + [c for f in self.facts for c in f.chunk_ids]


CHUNK_SQL = """
SELECT c.id, c.document_id, c.text, c.locator, c.models, c.flags, c.doc_status, c.authority_rank,
       d.title, d.doc_type, d.doc_number, d.revision, d.effective_date, d.parse_status, d.parse_notes
FROM chunks c JOIN documents d ON d.id = c.document_id
"""


def fetch_chunks(conn: Connection, user: UserContext, chunk_ids: list[str]) -> dict[str, dict]:
    if not chunk_ids:
        return {}
    rows = conn.execute(
        text(CHUNK_SQL + f" WHERE c.id = ANY(CAST(:ids AS uuid[])) AND {acl_clause('c')}"),
        {"ids": chunk_ids, **acl_params(user)},
    ).mappings().all()
    return {str(r["id"]): dict(r) for r in rows}


def _source_from_row(sid: str, r: dict) -> Source:
    label = citation_label(r["title"], r["doc_type"], r["doc_number"], r["revision"], r["locator"],
                           r["effective_date"], r["flags"])
    return Source(sid, str(r["id"]), label, str(r["document_id"]), r["title"], r["doc_type"], r["doc_number"],
                  r["doc_status"], r["authority_rank"], r["text"][:MAX_SOURCE_CHARS], r["locator"], r["models"],
                  r["flags"], r.get("parse_status") or "ok", r.get("parse_notes"))


def _conflict_positions() -> dict:
    path = get_settings().config_dir / "conflicts.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def conflict_sources(defs: dict) -> set[str]:
    """Every document path a conflict's positions and resolution draw on. A conflict without declared sources
    has nothing to check against, so it gets an impossible path and is never shown (default deny)."""
    paths: set[str] = set()
    for cid, c in defs.items():
        declared = [s for p in c.get("positions", []) for s in p.get("sources", [])] + c.get("resolution_sources", [])
        paths |= set(declared) if declared and all(p.get("sources") for p in c.get("positions", [])) \
            else {f"<undeclared sources for {cid}>"}
    return paths


def _visible_documents(conn: Connection, user: UserContext, paths: set[str]) -> set[str]:
    rows = conn.execute(
        text(f"SELECT d.path FROM documents d WHERE d.path = ANY(CAST(:paths AS text[])) AND {acl_clause('d')}"),
        {"paths": sorted(paths), **acl_params(user)},
    )
    return {r[0] for r in rows}


def build_context(
    conn: Connection,
    user: UserContext,
    question: str,
    ranked: list[Candidate],
    assets: list[Asset],
    bulletins: dict[str, list[BulletinMatch]],
    history: dict[str, HistoryFacts],
    models: list[str],
    context_k: int,
    expand_procedures: bool = False,
    asset_note_chunks: list[str] | None = None,
) -> ContextPack:
    facts: list[Fact] = []
    chunk_order: list[str] = []      # bulletins + top-ranked chunks
    procedure_chunks: list[str] = []
    history_chunks: list[str] = []

    # computed facts: asset card, bulletin applicability, history patterns
    visible_cards = set(fetch_chunks(conn, user, [a.source_chunk for a in assets if a.source_chunk]))
    for a in assets:
        if a.source_chunk not in visible_cards:  # the fleet register row is itself ACL'd content
            continue
        facts.append(Fact(
            f"F{len(facts) + 1}", f"Fleet register · {a.asset_id}",
            f"Asset {a.asset_id}: {a.model}, serial {a.serial}, site {a.site_name} ({a.customer}), region "
            f"{a.region}, installed {a.install_date}, controller firmware {a.firmware or 'n/a'}"
            + (f", oil {a.oil_type}" if a.oil_type else "") + ".",
            [a.source_chunk] if a.source_chunk else []))
        for b in bulletins.get(a.asset_id, []):
            if not b.visible:
                continue
            facts.append(Fact(
                f"F{len(facts) + 1}", f"{b.number} · serial check for {a.asset_id}",
                f"Bulletin {b.number} applies to {a.asset_id} (serial {a.serial}, region {a.region}, firmware "
                f"{a.firmware or 'n/a'} checked against the bulletin's range). Effect: {b.effect}"
                + (f" Caveat: {b.caveat}" if b.caveat else "")
                + (f" Campaign code: {b.campaign_code}." if b.campaign_code else "")
                + (f" Claim code: {b.claim_code}." if b.claim_code else ""),
                [b.source_chunk] if b.source_chunk else []))
            if b.source_chunk:
                chunk_order.append(b.source_chunk)
        h = history.get(a.asset_id)
        if h:
            for line in h.summary:
                facts.append(Fact(f"F{len(facts) + 1}", f"Asset history · {a.asset_id} (computed)", line,
                                  [e.chunk_id for e in h.events if e.chunk_id][:10]))
            same_code = [e for e in h.events if e.fault_code and e.fault_code in line_codes(h.summary)]
            history_chunks.extend(e.chunk_id for e in (same_code or h.events[:4])[:10] if e.chunk_id)

    # retrieved chunks after rerank, then field notes about the named assets
    for c in ranked[:context_k]:
        chunk_order.append(c.id)
    chunk_order.extend(asset_note_chunks or [])

    # procedure expansion: a how-to question needs every step of the procedure, in order, not just the
    # steps that happened to rank highly.
    if expand_procedures:
        proc_docs = list(dict.fromkeys(c.document_id for c in ranked[:6] if c.doc_type in PROCEDURE_TYPES))[:2]
        for doc_id in proc_docs:
            rows_ = conn.execute(
                text(f"""SELECT c.id FROM chunks c WHERE c.document_id = CAST(:d AS uuid) AND {acl_clause('c')}
                         AND (c.kind IN ('step', 'warning') OR c.id = ANY(CAST(:ranked AS uuid[])))
                         ORDER BY c.ord"""),
                {"d": doc_id, "ranked": [c.id for c in ranked[:context_k]], **acl_params(user)},
            ).all()
            procedure_chunks.extend(str(r[0]) for r in rows_)

    # conflicts relevant to the question's models and the retrieved documents
    conflict_defs = _conflict_positions()
    rows = fetch_chunks(conn, user, list(dict.fromkeys(chunk_order)))
    context_doc_numbers = {r["doc_number"] for r in rows.values() if r["doc_number"]}
    conflicts: list[ConflictInfo] = []
    resolution_chunks: dict[str, str] = {}
    visible_docs = _visible_documents(conn, user, conflict_sources(conflict_defs))
    for cid, c in conflict_defs.items():
        if not (set(c.get("models", [])) & set(models)):
            continue
        # Positions and the resolution restate document content: include the conflict only if the user can
        # see every document behind it. One hidden side means the whole conflict stays out of the prompt.
        if not conflict_sources({cid: c}) <= visible_docs:
            continue
        if not (set(c.get("doc_numbers", [])) & context_doc_numbers) and cid != "C5":
            continue
        if cid == "C5" and not ({"KT-WI-14", "SOP-020", "SB-2025-014"} & context_doc_numbers):
            continue
        res_chunk = _best_chunk_in_doc(conn, user, c.get("resolution_doc"), c["resolution"])
        if res_chunk:
            resolution_chunks[cid] = res_chunk
        conflicts.append(ConflictInfo(cid, c["topic"], [p["text"] for p in c["positions"]], c["resolution"], None))

    chunk_order = chunk_order + procedure_chunks + list(resolution_chunks.values()) + history_chunks
    rows = fetch_chunks(conn, user, list(dict.fromkeys(chunk_order)))
    sources: list[Source] = []
    total = 0
    for chunk_id in dict.fromkeys(chunk_order):
        r = rows.get(chunk_id)
        if r is None or len(sources) >= MAX_SOURCES or total > MAX_TOTAL_CHARS:
            continue
        source = _source_from_row(f"S{len(sources) + 1}", r)
        total += len(source.text)
        sources.append(source)
    by_chunk = {s.chunk_id: s.id for s in sources}
    for ci in conflicts:
        ci.resolution_source = by_chunk.get(resolution_chunks.get(ci.id, ""))
    return ContextPack(sources, facts, conflicts, question)


def line_codes(lines: list[str]) -> set[str]:
    return {m for line in lines for m in re.findall(r"\b[EPLC]-\d{2}\b", line)}


def _best_chunk_in_doc(conn: Connection, user: UserContext, doc_number: str | None, phrase: str) -> str | None:
    if not doc_number:
        return None
    words = [w for w in re.findall(r"[a-z]{4,}", phrase.lower())][:12]
    if not words:
        return None
    row = conn.execute(
        text(f"""SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                 WHERE d.doc_number = :dn AND {acl_clause('c')}
                 ORDER BY ts_rank_cd(c.tsv, to_tsquery('simple', :q)) DESC LIMIT 1"""),
        {"dn": doc_number, "q": " | ".join(words), **acl_params(user)},
    ).first()
    return str(row[0]) if row else None


def render_context(pack: ContextPack) -> str:
    lines = ["FACTS (computed by the system from structured data; cite as F-ids):"]
    lines += [f"[{f.id}] {f.label}: {f.text}" for f in pack.facts] or ["(none)"]
    lines.append("\nSOURCES (cite as S-ids):")
    for s in pack.sources:
        status = "" if s.doc_status == "current" else f" [{s.doc_status.upper()}: do not present as current]"
        if s.parse_status == "partial":
            status += f" [DAMAGED FILE, tell the user: {s.parse_notes}]"
        if s.parse_status == "ocr":
            status += " [scanned document, text from OCR]"
        lines.append(f"[{s.id}] {s.label} | type: {s.doc_type}{status}\n{s.text}\n")
    if pack.conflicts:
        lines.append("CONFLICTS (known disagreements between documents; resolution is decided):")
        for c in pack.conflicts:
            lines.append(f"[{c.id}] {c.topic}")
            lines += [f"   - {p}" for p in c.positions]
            lines.append(f"   Resolution: {c.resolution}" + (f" (cite {c.resolution_source})" if c.resolution_source else ""))
    return "\n".join(lines)
