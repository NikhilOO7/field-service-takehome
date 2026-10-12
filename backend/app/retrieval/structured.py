"""Structured lookups that run before text search (spec M8.3, M9.1).

- resolve_assets: asset IDs, short IDs ('0142'), serials, legacy Servio IDs ('MFL-KM07').
- applicable_bulletins: serial-range / region / firmware match, done in code, filtered by the user's ACL.
- asset_history: permission-filtered history with repeat-pattern facts.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import Connection, text

from app.auth.acl import acl_clause, acl_params, can_view
from app.auth.users import UserContext
from app.ingestion.entities import Entities


@dataclass
class Asset:
    asset_id: str
    model: str
    serial: str
    serial_prefix: str
    serial_num: int
    site: str
    site_name: str | None
    customer: str | None
    region: str
    install_date: date | None
    firmware: str | None
    oil_type: str | None
    legacy_ids: list[str]
    source_chunk: str | None


@dataclass
class AssetResolution:
    assets: list[Asset] = field(default_factory=list)
    ambiguous: list[list[Asset]] = field(default_factory=list)  # each inner list = candidates for one mention


def _asset_rows(conn: Connection, where: str, params: dict) -> list[Asset]:
    rows = conn.execute(text(f"SELECT * FROM assets WHERE {where} ORDER BY asset_id"), params).mappings().all()
    return [Asset(**{k: (str(v) if k == "source_chunk" and v else v) for k, v in r.items()
                     if k in Asset.__dataclass_fields__}) for r in rows]


SITE_WORDS = {
    "FRS": ["fresno", "frs", "sierra springs"],
    "TAC": ["tacoma", "tac", "puget"],
    "RTM": ["rotterdam", "rtm", "maasvlakte"],
    "GDN": ["gdansk", "gdańsk", "gdn", "baltic prime"],
}


def sites_mentioned(question: str) -> set[str]:
    q = question.lower()
    return {site for site, words in SITE_WORDS.items() if any(re.search(rf"\b{w}\b", q) for w in words)}


def resolve_assets(conn: Connection, question: str, ent: Entities) -> AssetResolution:
    res = AssetResolution()
    seen: set[str] = set()

    def add(assets: list[Asset]) -> None:
        for a in assets:
            if a.asset_id not in seen:
                seen.add(a.asset_id)
                res.assets.append(a)

    if ent.asset_ids:
        add(_asset_rows(conn, "asset_id = ANY(:ids)", {"ids": ent.asset_ids}))
    if ent.serials:
        add(_asset_rows(conn, "serial = ANY(:s)", {"s": ent.serials}))
    legacy = re.findall(r"\b[A-Z]{3}-[A-Z]{2}\d{2}\b", question)
    if legacy:
        add(_asset_rows(conn, "legacy_ids && :l", {"l": legacy}))

    # short IDs like '0142' (not part of a longer ID, part number or date)
    sites = sites_mentioned(question)
    for short in re.findall(r"(?<![-\w.,])0\d{3}(?![-\w.,])", question):
        if any(a.asset_id.endswith(short) for a in res.assets):
            continue
        candidates = _asset_rows(conn, "asset_id LIKE :s", {"s": f"%-{short}"})
        if sites:
            candidates = [c for c in candidates if c.site in sites] or candidates
        if len(candidates) == 1:
            add(candidates)
        elif candidates:
            res.ambiguous.append(candidates)

    # model + site ("the 350 at Fresno"): only when no specific asset was named
    if not res.assets and sites:
        models = ent.models or [f"HCP-{n}" for n in re.findall(r"\bthe (200|350|500)s?\b", question.lower())]
        for model in models:
            candidates = _asset_rows(conn, "model = :m AND site = ANY(:s)", {"m": model, "s": sorted(sites)})
            if len(candidates) == 1:
                add(candidates)
            elif len(candidates) > 1:
                res.ambiguous.append(candidates)
    return res


@dataclass
class BulletinMatch:
    number: str
    document_id: str
    doc_title: str
    effect: str
    caveat: str | None
    campaign_code: str | None
    claim_code: str | None
    source_chunk: str | None
    visible: bool  # can the user see the bulletin document?
    required_groups: list[str]
    acl_status: str


def _version_lt(installed: str | None, threshold: str) -> bool:
    if not installed:
        return True  # unknown firmware: treat as possibly affected
    def parts(v: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", v)]
    return parts(installed) < parts(threshold)


def applicable_bulletins(conn: Connection, asset: Asset, user: UserContext) -> list[BulletinMatch]:
    rows = conn.execute(
        text("""
        SELECT b.number, b.document_id, d.title, a.effect, a.caveat, a.serial_from, a.serial_to, a.region,
               a.firmware_lt, b.campaign_code, b.claim_code, a.source_chunk, d.required_groups, d.acl_status
        FROM bulletin_applicability a
        JOIN bulletins b ON b.id = a.bulletin_id
        JOIN documents d ON d.id = b.document_id
        WHERE a.model = :model AND a.serial_prefix = :prefix
        """),
        {"model": asset.model, "prefix": asset.serial_prefix},
    ).mappings().all()
    matches = []
    for r in rows:
        if r["serial_from"] is not None and asset.serial_num < r["serial_from"]:
            continue
        if r["serial_to"] is not None and asset.serial_num > r["serial_to"]:
            continue
        if r["region"] and r["region"] != asset.region:
            continue
        if r["firmware_lt"] and not _version_lt(asset.firmware, r["firmware_lt"]):
            continue
        matches.append(
            BulletinMatch(
                number=r["number"], document_id=str(r["document_id"]), doc_title=r["title"], effect=r["effect"],
                caveat=r["caveat"], campaign_code=r["campaign_code"], claim_code=r["claim_code"],
                source_chunk=str(r["source_chunk"]) if r["source_chunk"] else None,
                visible=can_view(user, r["required_groups"], r["acl_status"]),
                required_groups=list(r["required_groups"]), acl_status=r["acl_status"],
            )
        )
    return matches


@dataclass
class HistoryEvent:
    event_date: date | None
    source: str
    source_ref: str
    event_type: str | None
    fault_code: str | None
    technician: str | None
    notes: str | None
    parts: str | None
    chunk_id: str | None
    linked_refs: list[str]


@dataclass
class HistoryFacts:
    events: list[HistoryEvent]
    summary: list[str]  # computed repeat-pattern facts, each backed by the event rows


def asset_history(conn: Connection, asset_id: str, user: UserContext, fault_code: str | None = None,
                  months: int = 24, today: date | None = None) -> HistoryFacts:
    today = today or date.today()
    since = today - timedelta(days=30 * months)
    rows = conn.execute(
        text(f"""
        SELECT e.event_date, e.source, e.source_ref, e.event_type, e.fault_code, e.technician, e.notes, e.parts,
               e.chunk_id, e.linked_refs
        FROM asset_events e
        WHERE e.asset_id = :asset AND {acl_clause('e')}
          AND (e.event_date IS NULL OR e.event_date >= :since)
          AND e.source <> 'salesforce_comment'
        ORDER BY e.event_date DESC NULLS LAST
        """),
        {"asset": asset_id, "since": since, **acl_params(user)},
    ).mappings().all()
    events = [HistoryEvent(**{**r, "chunk_id": str(r["chunk_id"]) if r["chunk_id"] else None}) for r in rows]
    relevant = [e for e in events
                if (fault_code and e.fault_code == fault_code)
                or (e.event_type or "").lower() in {"cm", "alarm", "storing"}
                or e.source == "salesforce_case" and (e.event_type or "").startswith("Case")]
    summary: list[str] = []
    if fault_code:
        same = sorted([e for e in events if e.fault_code == fault_code and e.event_date], key=lambda e: e.event_date)
        if same:
            # one occurrence per day/source pair is enough for the count
            days = sorted({e.event_date for e in same})
            summary.append(f"{fault_code} recorded on {asset_id} on {len(days)} day(s) in the last {months} months: "
                           + ", ".join(d.isoformat() for d in days))
    # parts replaced repeatedly
    part_days: dict[str, list[date]] = {}
    for e in events:
        for part in re.findall(r"\b\d{2}-\d{4}\b", e.parts or ""):
            if e.event_date:
                part_days.setdefault(part, []).append(e.event_date)
    for part, days in part_days.items():
        days = sorted(set(days))
        if len(days) >= 2 and part not in {"41-0046", "41-1480", "41-1512", "41-1510", "41-1475", "18-1121"}:
            summary.append(f"Part {part} replaced on {asset_id} on {', '.join(d.isoformat() for d in days)}")
    return HistoryFacts(events=relevant[:25], summary=summary)
