"""Ingestion pipeline (spec M3-M7): discover -> parse -> chunk -> enrich -> store -> embed -> report.

Incremental: a file whose SHA-256 is unchanged is skipped unless --full. Each document is written in
its own transaction, so a failure in one file doesn't block the rest.
"""

import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml
from sqlalchemy import Connection, Engine, text

from app.auth.users import sync_users
from app.config import Settings
from app.ingestion.chunking import ChunkDraft, build_chunks
from app.ingestion.entities import extract_entities, split_serial
from app.ingestion.loaders.confluence_html import load_confluence
from app.ingestion.loaders.docx_loader import load_docx
from app.ingestion.loaders.email_loader import load_email_pdf, load_eml
from app.ingestion.loaders.pdf_text import load_pdf
from app.ingestion.loaders.records import load_fleet, load_maintpro, load_salesforce, load_servio, load_teams
from app.ingestion.manifest import DocSpec, discover, document_metadata
from app.ingestion.types import ParsedDocument

log = structlog.get_logger()
SUFFIX_RE = re.compile(r"(?<![-\w.,])0\d{3}(?![-\w.,])")
RECORD_DOC_TYPES = {"cmms_export", "case_export", "chat", "asset_register"}


@dataclass
class DocResult:
    path: str
    status: str  # ingested | unchanged | acl updated | duplicate | failed
    parse_status: str
    acl: str
    doc_type: str
    chunks: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class IngestReport:
    results: list[DocResult] = field(default_factory=list)
    embedded: int = 0
    embedding_pending: int = 0
    llm_available: bool = False


# ---------------------------------------------------------------- loader dispatch
def parse_file(spec: DocSpec, ocr) -> ParsedDocument:
    suffix = spec.abs_path.suffix.lower()
    name = spec.abs_path.name.lower()
    if suffix == ".pdf":
        return load_email_pdf(spec.abs_path) if spec.doc_type == "email" else load_pdf(spec.abs_path, ocr=ocr)
    if suffix == ".docx":
        return load_docx(spec.abs_path)
    if suffix in {".html", ".htm"}:
        return load_confluence(spec.abs_path)
    if suffix == ".eml":
        return load_eml(spec.abs_path)
    if suffix == ".json":
        head = spec.abs_path.read_text(encoding="utf-8")[:2000]
        if '"records"' in head and '"CaseNumber"' in spec.abs_path.read_text(encoding="utf-8"):
            return load_salesforce(spec.abs_path)
        if '"value"' in head or "teams" in name:
            return load_teams(spec.abs_path)
    if suffix == ".csv":
        header = spec.abs_path.read_text(encoding="utf-8", errors="replace").splitlines()[0].lower()
        if header.startswith("asset_id,model,serial"):
            return load_fleet(spec.abs_path)
        if header.startswith("wo_no,"):
            return load_maintpro(spec.abs_path)
        if header.startswith("werkorder;"):
            return load_servio(spec.abs_path)
    out = ParsedDocument(parse_status="failed")
    out.parse_notes.append(f"no loader for {spec.abs_path.name}")
    return out


# ---------------------------------------------------------------- enrichment
class Enricher:
    """Adds entity metadata to chunks: models, asset IDs (incl. '0142'-style short IDs), codes, parts."""

    def __init__(self, fleet: list, glossary: dict[str, str]):
        self.asset_model = {a.asset_id: a.model for a in fleet}
        suffixes = defaultdict(list)
        for a in fleet:
            suffixes[a.asset_id[-4:]].append(a.asset_id)
        self.suffix_map = {s: ids[0] for s, ids in suffixes.items() if len(ids) == 1}
        self.glossary = glossary

    def apply(self, chunk: ChunkDraft, doc: dict) -> dict:
        ent = extract_entities(chunk.text)
        assets = set(chunk.asset_ids) | set(ent.asset_ids)
        for short in SUFFIX_RE.findall(chunk.text):
            if short in self.suffix_map:
                assets.add(self.suffix_map[short])
        models = set(ent.models) | {self.asset_model[a] for a in assets if a in self.asset_model}
        if doc["doc_type"] not in RECORD_DOC_TYPES:
            models |= set(doc["models"])
        glossary_hits = [f"{k} = {v}" for k, v in self.glossary.items()
                         if re.search(rf"\b{re.escape(k)}\b", chunk.text, re.I)]
        embed_text = chunk.embed_text + (f"\n(glossary: {'; '.join(glossary_hits)})" if glossary_hits else "")
        return {
            "asset_ids": sorted(assets),
            "models": sorted(models),
            "fault_codes": ent.fault_codes,
            "part_numbers": ent.part_numbers,
            "doc_numbers": ent.doc_numbers,
            "embed_text": embed_text,
        }


# ---------------------------------------------------------------- storage
def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def store_document(conn: Connection, spec: DocSpec, parsed: ParsedDocument, meta: dict,
                   enricher: Enricher, duplicate_of: uuid.UUID | None = None) -> tuple[uuid.UUID, int]:
    doc_id = uuid.uuid4()
    parse_status = "duplicate" if duplicate_of else parsed.parse_status
    conn.execute(
        text("""
        INSERT INTO documents (id, path, title, doc_number, revision, effective_date, source_system, doc_type,
          authority_rank, status, models, language, required_groups, acl_status, sha256, duplicate_of,
          parse_status, parse_notes, page_count, metadata)
        VALUES (:id, :path, :title, :doc_number, :revision, :effective_date, :source_system, :doc_type,
          :authority_rank, :status, :models, :language, :required_groups, :acl_status, :sha256, :duplicate_of,
          :parse_status, :parse_notes, :page_count, CAST(:metadata AS jsonb))
        """),
        {
            "id": doc_id, "path": spec.path, **{k: meta[k] for k in (
                "title", "doc_number", "revision", "effective_date", "source_system", "doc_type",
                "authority_rank", "status", "models", "language")},
            "required_groups": spec.required_groups, "acl_status": spec.acl_status, "sha256": spec.sha256,
            "duplicate_of": duplicate_of, "parse_status": parse_status,
            "parse_notes": "; ".join(parsed.parse_notes) or None, "page_count": parsed.page_count,
            "metadata": json.dumps(_jsonable(meta["metadata"])),
        },
    )
    if duplicate_of:
        return doc_id, 0

    doc_for_chunks = {**meta, "status": meta["status"]}
    drafts = build_chunks(parsed, doc_for_chunks)
    section_ids = []
    for i, section in enumerate(parsed.sections):
        sid = uuid.uuid4()
        section_ids.append(sid)
        conn.execute(
            text("""INSERT INTO sections (id, document_id, heading_path, section_no, page_from, page_to, ord)
                    VALUES (:id, :doc, :hp, :no, :pf, :pt, :ord)"""),
            {"id": sid, "doc": doc_id, "hp": section.heading_path, "no": section.section_no,
             "pf": section.page_from, "pt": section.page_to, "ord": i},
        )
    ref_to_chunk: dict[str, uuid.UUID] = {}
    rows = []
    for d in drafts:
        cid = uuid.uuid4()
        e = enricher.apply(d, meta)
        for ref in d.record_refs:
            ref_to_chunk[ref] = cid
        rows.append({
            "id": cid, "doc": doc_id, "sec": section_ids[d.section_index], "ord": d.ord, "kind": d.kind,
            "text": d.text, "embed_text": e["embed_text"], "locator": json.dumps(_jsonable(d.locator)),
            "models": e["models"], "asset_ids": e["asset_ids"], "fault_codes": e["fault_codes"],
            "part_numbers": e["part_numbers"], "doc_numbers": e["doc_numbers"], "event_date": d.event_date,
            "rg": spec.required_groups, "acl": spec.acl_status, "rank": meta["authority_rank"],
            "status": meta["status"], "flags": d.flags,
        })
    if rows:
        conn.execute(
            text("""
            INSERT INTO chunks (id, document_id, section_id, ord, kind, text, embed_text, locator, models,
              asset_ids, fault_codes, part_numbers, doc_numbers, event_date, required_groups, acl_status,
              authority_rank, doc_status, flags)
            VALUES (:id, :doc, :sec, :ord, :kind, :text, :embed_text, CAST(:locator AS jsonb), :models,
              :asset_ids, :fault_codes, :part_numbers, :doc_numbers, :event_date, :rg, :acl, :rank, :status,
              :flags)
            """),
            rows,
        )
    for a in parsed.assets:
        prefix_num = split_serial(a.serial) or ("", 0)
        conn.execute(
            text("""
            INSERT INTO assets (asset_id, model, serial, serial_prefix, serial_num, site, site_name, customer,
              region, install_date, firmware, oil_type, contract_end, source_chunk)
            VALUES (:asset_id, :model, :serial, :sp, :sn, :site, :site_name, :customer, :region, :install_date,
              :firmware, :oil_type, :contract_end, :chunk)
            ON CONFLICT (asset_id) DO UPDATE SET model = EXCLUDED.model, serial = EXCLUDED.serial,
              serial_prefix = EXCLUDED.serial_prefix, serial_num = EXCLUDED.serial_num, site = EXCLUDED.site,
              site_name = EXCLUDED.site_name, customer = EXCLUDED.customer, region = EXCLUDED.region,
              install_date = EXCLUDED.install_date, firmware = EXCLUDED.firmware, oil_type = EXCLUDED.oil_type,
              contract_end = EXCLUDED.contract_end, source_chunk = EXCLUDED.source_chunk
            """),
            {**{k: getattr(a, k) for k in ("asset_id", "model", "serial", "site", "site_name", "customer",
                                           "region", "install_date", "firmware", "oil_type", "contract_end")},
             "sp": prefix_num[0], "sn": prefix_num[1], "chunk": ref_to_chunk.get(a.ref)},
        )
    if parsed.events:
        conn.execute(
            text("""
            INSERT INTO asset_events (id, asset_id, source, source_ref, event_date, event_type, fault_code,
              technician, notes, parts, run_hours, status, linked_refs, chunk_id, document_id, required_groups,
              acl_status)
            VALUES (:id, :asset_id, :source, :source_ref, :event_date, :event_type, :fault_code, :technician,
              :notes, :parts, :run_hours, :status, :linked_refs, :chunk_id, :doc, :rg, :acl)
            """),
            [
                {"id": uuid.uuid4(), "asset_id": ev.asset_id, "source": ev.source, "source_ref": ev.source_ref,
                 "event_date": ev.event_date, "event_type": ev.event_type, "fault_code": ev.fault_code,
                 "technician": ev.technician, "notes": ev.notes, "parts": ev.parts, "run_hours": ev.run_hours,
                 "status": ev.status, "linked_refs": ev.linked_refs, "chunk_id": ref_to_chunk.get(ev.ref),
                 "doc": doc_id, "rg": spec.required_groups, "acl": spec.acl_status}
                for ev in parsed.events
            ],
        )
    return doc_id, len(rows)


# ---------------------------------------------------------------- post-processing
def rebuild_catalogues(conn: Connection, settings: Settings) -> None:
    """Supersession links, legacy IDs, bulletins + applicability, conflicts, parts catalogue."""
    manifest = yaml.safe_load((settings.config_dir / "corpus_manifest.yaml").read_text()) or {}
    for path, m in manifest.items():
        # Status comes from the manifest and is refreshed on every run, so marking an old revision superseded
        # works without re-parsing it (its bytes are unchanged). The chunks carry a copy for search scoring.
        status = m.get("status") or ("superseded" if m.get("superseded_by") else None)
        if status:
            conn.execute(text("UPDATE documents SET status = :s WHERE path = :p AND status <> :s"),
                         {"p": path, "s": status})
            conn.execute(text("""UPDATE chunks SET doc_status = :s WHERE doc_status <> :s
                                 AND document_id = (SELECT id FROM documents WHERE path = :p)"""),
                         {"p": path, "s": status})
        if m.get("superseded_by"):
            conn.execute(
                text("""UPDATE documents d SET superseded_by = s.id FROM documents s
                        WHERE d.path = :p AND s.path = :s"""),
                {"p": path, "s": m["superseded_by"]},
            )

    # legacy Servio object IDs -> assets.legacy_ids
    for (meta,) in conn.execute(text("SELECT metadata FROM documents WHERE metadata ? 'legacy_ids'")):
        for asset_id, ids in meta["legacy_ids"].items():
            conn.execute(text("UPDATE assets SET legacy_ids = :ids WHERE asset_id = :a"), {"ids": ids, "a": asset_id})

    conn.execute(text("DELETE FROM bulletins"))
    bulletins = yaml.safe_load((settings.config_dir / "bulletins.yaml").read_text()) or {}
    for number, b in bulletins.items():
        doc = conn.execute(text("SELECT id FROM documents WHERE path = :p"), {"p": b["path"]}).first()
        if doc is None:
            continue
        bid = uuid.uuid4()
        conn.execute(
            text("""INSERT INTO bulletins (id, document_id, number, oem, category, issue_date, campaign_code,
                    claim_code, summary) VALUES (:id, :doc, :n, :oem, :cat, :d, :cc, :claim, :s)"""),
            {"id": bid, "doc": doc[0], "n": number, "oem": b["oem"], "cat": b.get("category"),
             "d": b.get("issue_date"), "cc": b.get("campaign_code"), "claim": b.get("claim_code"),
             "s": b.get("summary")},
        )
        for rule in b.get("applies", []):
            needle = str(rule.get("serial_from") or "")
            src = conn.execute(
                text("""SELECT id FROM chunks WHERE document_id = :doc ORDER BY (text LIKE :n) DESC, ord LIMIT 1"""),
                {"doc": doc[0], "n": f"%{needle}%"},
            ).first()
            conn.execute(
                text("""INSERT INTO bulletin_applicability (id, bulletin_id, model, serial_prefix, serial_from,
                        serial_to, region, firmware_lt, effect, caveat, source_chunk)
                        VALUES (:id, :b, :m, :sp, :sf, :st, :r, :fw, :e, :c, :src)"""),
                {"id": uuid.uuid4(), "b": bid, "m": rule["model"], "sp": rule["serial_prefix"],
                 "sf": rule.get("serial_from"), "st": rule.get("serial_to"), "r": rule.get("region"),
                 "fw": rule.get("firmware_lt"), "e": rule["effect"], "c": rule.get("caveat"),
                 "src": src[0] if src else None},
            )

    conn.execute(text("DELETE FROM conflicts"))
    conflicts = yaml.safe_load((settings.config_dir / "conflicts.yaml").read_text()) or {}
    for cid, c in conflicts.items():
        conn.execute(
            text("""INSERT INTO conflicts (id, topic, models, doc_numbers, resolution, resolution_doc)
                    VALUES (:id, :t, :m, :d, :r, :rd)"""),
            {"id": cid, "t": c["topic"], "m": c.get("models", []), "d": c.get("doc_numbers", []),
             "r": c["resolution"], "rd": c.get("resolution_doc")},
        )

    # parts catalogue: every part number seen in a chunk, with that chunk's ACL via source_chunk
    conn.execute(text("DELETE FROM parts"))
    overrides = yaml.safe_load((settings.config_dir / "parts.yaml").read_text()) or {}
    rows = conn.execute(
        text("""SELECT c.id, c.text, c.models, unnest(c.part_numbers) AS part
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE d.doc_type IN ('oem_manual','oem_bulletin','controlled_procedure','legacy_wi','wiki',
                                     'email','case_export','cmms_export','chat')""")
    ).all()
    for chunk_id, chunk_text, models, part in rows:
        # descriptions only from parts-table rows ("Part no.: X | Description: Y" or "Part no.: X | Y")
        m = re.search(rf"Part no\.?:\s*{re.escape(part)}\s*\|\s*(?:Description:\s*)?([^|\n]{{3,90}})", chunk_text)
        o = overrides.get(part, {})
        conn.execute(
            text("""INSERT INTO parts (part_no, description, models, status, superseded_by, source_chunk)
                    VALUES (:p, :d, :m, :s, :sb, :c) ON CONFLICT DO NOTHING"""),
            {"p": part, "d": (m.group(1).strip() if m else None), "m": models,
             "s": o.get("status", "current"), "sb": o.get("superseded_by"), "c": chunk_id},
        )


def embed_pending(engine: Engine) -> tuple[int, int]:
    """Embed chunks whose embed_text changed. Returns (embedded, still_pending)."""
    from app.llm.openai_client import get_llm, llm_available

    with engine.connect() as conn:
        pending = conn.execute(
            text("SELECT id, embed_text FROM chunks WHERE embedding IS NULL OR embed_sha256 IS DISTINCT FROM "
                 "encode(sha256(convert_to(embed_text, 'UTF8')), 'hex')")
        ).all()
    if not pending or not llm_available():
        return 0, len(pending)
    llm = get_llm()
    done = 0
    for i in range(0, len(pending), 100):
        batch = pending[i : i + 100]
        vectors = llm.embed([row[1] for row in batch])
        with engine.begin() as conn:
            for (cid, emb_text), vec in zip(batch, vectors):
                conn.execute(
                    text("UPDATE chunks SET embedding = CAST(:v AS halfvec), embed_sha256 = :h WHERE id = :id"),
                    {"v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                     "h": hashlib.sha256(emb_text.encode("utf-8")).hexdigest(), "id": cid},
                )
        done += len(batch)
        log.info("embedded", done=done, total=len(pending))
    return done, len(pending) - done


# ---------------------------------------------------------------- orchestration
def ingest(engine: Engine, settings: Settings, full: bool = False, only: Path | None = None,
           permissions_file: Path | None = None, embed: bool = True) -> IngestReport:
    from app.llm.openai_client import llm_available

    report = IngestReport(llm_available=llm_available())
    ocr = None
    if report.llm_available:
        from app.ingestion.loaders.pdf_ocr import ocr_pages

        ocr = ocr_pages

    with engine.begin() as conn:
        sync_users(conn, settings.users_file)

    specs = discover(settings, permissions_file=permissions_file, only=only)
    # The fleet register tags chunks with asset IDs, so load it even when only a sub-folder is being ingested.
    fleet_spec = next((s for s in (discover(settings) if only else specs) if s.doc_type == "asset_register"), None)
    fleet = load_fleet(fleet_spec.abs_path).assets if fleet_spec else []
    glossary_path = settings.config_dir / "glossary.yaml"
    glossary = yaml.safe_load(glossary_path.read_text()) if glossary_path.exists() else {}
    enricher = Enricher(fleet, glossary)

    # Byte-identical files: ingest the canonical copy, record the others as duplicates of it.
    by_hash: dict[str, list[DocSpec]] = defaultdict(list)
    for s in specs:
        by_hash[s.sha256].append(s)
    canonical_of: dict[str, DocSpec] = {}
    for group in by_hash.values():
        canon = sorted(group, key=lambda s: (not s.manifest.get("canonical"), s.source_system != "manual_library",
                                             s.path))[0]
        for s in group:
            if s is not canon:
                canonical_of[s.path] = canon
    ordered = sorted(specs, key=lambda s: (s.doc_type != "asset_register", s.path in canonical_of, s.path))

    for spec in ordered:
        with engine.begin() as conn:
            existing = conn.execute(
                text("SELECT sha256, parse_status FROM documents WHERE path = :p"), {"p": spec.path}
            ).first()
            retry_ocr = existing is not None and existing[1] == "ocr_pending" and ocr is not None
            if existing and existing[0] == spec.sha256 and not full and not retry_ocr:
                # Content is the same, but permissions may have changed: the ACL is copied onto every row
                # the retrieval filter reads, so update it everywhere.
                acl = {"p": spec.path, "g": spec.required_groups, "s": spec.acl_status}
                changed = conn.execute(text("""UPDATE documents SET required_groups = :g, acl_status = :s
                    WHERE path = :p AND (required_groups <> CAST(:g AS text[]) OR acl_status <> :s)"""), acl).rowcount
                if changed:
                    for table in ("chunks", "asset_events"):
                        conn.execute(text(f"""UPDATE {table} SET required_groups = :g, acl_status = :s
                            WHERE document_id = (SELECT id FROM documents WHERE path = :p)"""), acl)
                report.results.append(DocResult(spec.path, "acl updated" if changed else "unchanged", existing[1],
                                                spec.acl_status, spec.doc_type))
                continue
            if existing:
                conn.execute(text("DELETE FROM asset_events WHERE document_id IN "
                                  "(SELECT id FROM documents WHERE path = :p)"), {"p": spec.path})
                conn.execute(text("UPDATE assets SET source_chunk = NULL WHERE source_chunk IN (SELECT c.id FROM "
                                  "chunks c JOIN documents d ON d.id = c.document_id WHERE d.path = :p)"),
                             {"p": spec.path})
                conn.execute(text("UPDATE documents SET duplicate_of = NULL, superseded_by = NULL WHERE "
                                  "duplicate_of IN (SELECT id FROM documents WHERE path = :p) OR superseded_by IN "
                                  "(SELECT id FROM documents WHERE path = :p)"), {"p": spec.path})
                conn.execute(text("DELETE FROM documents WHERE path = :p"), {"p": spec.path})
            try:
                if spec.path in canonical_of:
                    canon_id = conn.execute(text("SELECT id FROM documents WHERE path = :p"),
                                            {"p": canonical_of[spec.path].path}).scalar_one()
                    meta = document_metadata(spec, {})
                    store_document(conn, spec, ParsedDocument(parse_notes=[
                        f"byte-identical duplicate of {canonical_of[spec.path].path}"]), meta, enricher,
                        duplicate_of=canon_id)
                    report.results.append(DocResult(spec.path, "duplicate", "duplicate", spec.acl_status,
                                                    spec.doc_type, notes=[f"duplicate of {canonical_of[spec.path].path}"]))
                    continue
                parsed = parse_file(spec, ocr)
                meta = document_metadata(spec, parsed.metadata)
                _, n_chunks = store_document(conn, spec, parsed, meta, enricher)
                notes = list(parsed.parse_notes)
                if spec.acl_status == "unlisted":
                    notes.append("NOT in permissions.json: denied to every user (default deny)")
                if meta["status"] in {"superseded", "stale", "unofficial"}:
                    notes.append(f"status: {meta['status']}")
                if meta["metadata"].get("review_overdue"):
                    notes.append("next review date is overdue")
                report.results.append(DocResult(spec.path, "ingested", parsed.parse_status, spec.acl_status,
                                                meta["doc_type"], n_chunks, notes))
            except Exception as exc:
                log.exception("ingest_failed", path=spec.path)
                report.results.append(DocResult(spec.path, "failed", "failed", spec.acl_status, spec.doc_type,
                                                notes=[repr(exc)]))

    with engine.begin() as conn:
        rebuild_catalogues(conn, settings)

    if embed:
        report.embedded, report.embedding_pending = embed_pending(engine)
    return report


def write_report(report: IngestReport, settings: Settings) -> Path:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / "ingestion_report.md"
    lines = [
        "# Ingestion report",
        "",
        f"- Files: {len(report.results)}",
        f"- Chunks embedded this run: {report.embedded}; still without embeddings: {report.embedding_pending}"
        + ("" if report.llm_available else " (no OPENAI_API_KEY: embeddings and OCR are pending)"),
        "",
        "| File | Result | Parse | Access | Type | Chunks | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(report.results, key=lambda r: r.path):
        lines.append(f"| `{r.path}` | {r.status} | {r.parse_status} | {r.acl} | {r.doc_type} | {r.chunks} | "
                     f"{'; '.join(r.notes)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
