"""Loaders for structured exports: fleet register, MaintPro, Servio, Salesforce cases, Teams chat.

Each record becomes (a) a citable block and, for history sources, (b) an AssetEvent row so the
copilot can answer "has this happened before?" with exact dates, codes and parts.
"""

import csv
import html
import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from app.ingestion.entities import ASSET_RE, CASE_RE, FAULT_RE
from app.ingestion.types import AssetEvent, AssetRow, Block, ParsedDocument, Section


# ---------------------------------------------------------------- helpers
def _date(value: str | None, fmt: str) -> date | None:
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip().replace(".", "").replace(",", ".")
    try:
        return int(float(value))
    except ValueError:
        return None


def _linked_cases(text: str) -> list[str]:
    """'SF 00041207', 'sf 00044010', '(00041055)' -> case numbers mentioned in a note."""
    return sorted(set(CASE_RE.findall(text or "")))


# ---------------------------------------------------------------- fleet register
def load_fleet(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    section = Section(heading_path=["Fleet register"])
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            asset_id = row["asset_id"].strip()
            ref = f"asset:{asset_id}"
            asset = AssetRow(
                ref=ref,
                asset_id=asset_id,
                model=row["model"].strip(),
                serial=row["serial"].strip(),
                site=row["site"].strip(),
                site_name=row.get("site_name") or None,
                customer=row.get("customer") or None,
                region=row["region"].strip(),
                install_date=_date(row.get("install_date"), "%m/%d/%Y"),
                firmware=(row.get("firmware") or "").strip() or None,
                oil_type=(row.get("oil_type") or "").strip() or None,
                contract_end=_date(row.get("contract_end"), "%m/%d/%Y"),
            )
            out.assets.append(asset)
            text = (
                f"Fleet register: {asset_id} is a {asset.model}, serial {asset.serial}, at {asset.site_name} "
                f"({asset.customer}, region {asset.region}), installed {asset.install_date}, "
                f"controller firmware {asset.firmware or 'n/a'}"
                + (f", oil {asset.oil_type}" if asset.oil_type else "")
                + f", contract until {asset.contract_end}."
            )
            section.blocks.append(
                Block(kind="asset_row", text=text, locator={"row": asset_id, "label": asset_id},
                      asset_ids=[asset_id], record_ref=ref)
            )
    out.sections = [section]
    return out


# ---------------------------------------------------------------- MaintPro (NA CMMS)
def load_maintpro(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    by_asset: dict[str, Section] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):  # handles multi-line quoted Notes
            wo = row["WO_No"].strip()
            asset_id = row["Asset"].strip()
            when = _date(row["Date"], "%m/%d/%Y")
            notes = re.sub(r"\s+", " ", row.get("Notes") or "").strip()
            ref = f"maintpro:{wo}"
            out.events.append(
                AssetEvent(
                    ref=ref, asset_id=asset_id, source="maintpro", source_ref=wo, event_date=when,
                    event_type=row.get("Event_Type") or None, fault_code=(row.get("Fault") or "").strip() or None,
                    technician=(row.get("Tech") or "").strip() or None, notes=notes,
                    parts=(row.get("Parts") or "").strip() or None, run_hours=_int(row.get("Hours")),
                    status=row.get("Status") or None, linked_refs=_linked_cases(notes),
                )
            )
            fault = f" fault {row['Fault'].strip()}," if (row.get("Fault") or "").strip() else ""
            parts = f" Parts: {row['Parts'].strip()}." if (row.get("Parts") or "").strip() else ""
            hours = f" {row['Hours'].strip()} h." if (row.get("Hours") or "").strip() else ""
            text = (f"MaintPro WO {wo}, {when}, {asset_id} ({row.get('Model', '').strip()}), "
                    f"{row.get('Event_Type', '').strip()},{fault} tech {row.get('Tech', '').strip() or 'n/a'}:"
                    f" {notes}{parts}{hours}")
            sec = by_asset.setdefault(asset_id, Section(heading_path=[f"MaintPro history {asset_id}"]))
            sec.blocks.append(
                Block(kind="cmms_row", text=text, locator={"wo": wo, "date": when.isoformat() if when else None,
                                                           "label": f"WO {wo}"},
                      event_date=when, asset_ids=[asset_id], record_ref=ref)
            )
    out.sections = list(by_asset.values())
    return out


# ---------------------------------------------------------------- Servio (ex-Kyltek EU CMMS, Dutch)
SERVIO_COLUMNS = {
    "werkorder": "wo", "datum": "date", "object": "asset_id", "object_oud": "legacy_id", "locatie": "site",
    "type": "event_type", "storingscode": "fault_code", "omschrijving": "notes", "monteur": "technician",
    "onderdelen": "parts", "uren": "labour_h", "draaiuren": "run_hours", "status": "status",
}


def load_servio(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    by_asset: dict[str, Section] = {}
    legacy: dict[str, set[str]] = defaultdict(set)
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for raw in reader:
            row = {SERVIO_COLUMNS.get(k.strip(), k.strip()): (v or "").strip() for k, v in raw.items()}
            asset_id = row["asset_id"]
            if row.get("legacy_id"):
                legacy[asset_id].add(row["legacy_id"])
            when = _date(row["date"], "%d-%m-%Y")
            ref = f"servio:{row['wo']}"
            out.events.append(
                AssetEvent(
                    ref=ref, asset_id=asset_id or None, source="servio", source_ref=row["wo"], event_date=when,
                    event_type=row.get("event_type") or None, fault_code=row.get("fault_code") or None,
                    technician=row.get("technician") or None, notes=row.get("notes"),
                    parts=row.get("parts") or None, run_hours=_int(row.get("run_hours")),
                    status=row.get("status") or None, linked_refs=_linked_cases(row.get("notes", "")),
                )
            )
            fault = f" storingscode {row['fault_code']}," if row.get("fault_code") else ""
            parts = f" Onderdelen/parts: {row['parts']}." if row.get("parts") else ""
            text = (f"Servio werkorder {row['wo']}, {when}, {asset_id}"
                    + (f" (old id {row['legacy_id']})" if row.get("legacy_id") else "")
                    + f", type {row.get('event_type')},{fault} monteur {row.get('technician') or 'n/a'}:"
                    f" {row.get('notes')}{parts}")
            sec = by_asset.setdefault(asset_id or "unknown", Section(heading_path=[f"Servio history {asset_id}"]))
            sec.blocks.append(
                Block(kind="cmms_row", text=text,
                      locator={"wo": row["wo"], "date": when.isoformat() if when else None,
                               "label": f"werkorder {row['wo']}"},
                      event_date=when, asset_ids=[asset_id] if asset_id else [], record_ref=ref,
                      flags=["dutch"])
            )
    out.sections = list(by_asset.values())
    out.metadata["legacy_ids"] = {k: sorted(v) for k, v in legacy.items()}
    return out


# ---------------------------------------------------------------- Salesforce cases (NA + EU)
def _sf_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None


def load_salesforce(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    data = json.loads(path.read_text(encoding="utf-8"))
    for case in data.get("records", []):
        number = case["CaseNumber"]
        asset_id = (case.get("Asset__r") or {}).get("Name")
        created = _sf_datetime(case.get("CreatedDate"))
        created_date = created.date() if created else None
        section = Section(heading_path=[f"Case {number}: {case.get('Subject')}"])
        all_text = " ".join(str(case.get(k) or "") for k in ("Subject", "Description", "Resolution__c"))
        ref = f"case:{number}"
        out.events.append(
            AssetEvent(
                ref=ref, asset_id=asset_id, source="salesforce_case", source_ref=number,
                event_date=created_date, event_type=f"Case ({case.get('Priority')})",
                fault_code=case.get("Fault_Code__c"), technician=(case.get("Owner") or {}).get("Name"),
                notes=f"{case.get('Subject')}. {case.get('Description') or ''} Resolution: "
                      f"{case.get('Resolution__c') or '(none yet)'}",
                parts=case.get("Parts_Used__c"), status=case.get("Status"),
                linked_refs=[c for c in _linked_cases(all_text) if c != number],
            )
        )
        lines = [
            f"Salesforce case {number} ({case.get('Status')}, priority {case.get('Priority')}), "
            f"opened {created_date}, asset {asset_id} ({case.get('Equipment_Model__c')}, "
            f"serial {case.get('Serial_Number__c')}), site {case.get('Site__c')}, "
            f"fault code {case.get('Fault_Code__c') or 'none'}, owner {(case.get('Owner') or {}).get('Name')}.",
            f"Subject: {case.get('Subject')}",
            f"Description: {case.get('Description') or '(empty)'}",
            f"Resolution: {case.get('Resolution__c') or '(not resolved yet)'}",
        ]
        if case.get("Parts_Used__c"):
            lines.append(f"Parts used: {case['Parts_Used__c']}")
        section.blocks.append(
            Block(kind="case", text="\n".join(lines), locator={"case": number, "label": f"case {number}"},
                  event_date=created_date, asset_ids=[asset_id] if asset_id else [], record_ref=ref)
        )
        for i, comment in enumerate((case.get("CaseComments") or {}).get("records", []), start=1):
            when = _sf_datetime(comment.get("CreatedDate"))
            author = (comment.get("CreatedBy") or {}).get("Name")
            flags = []
            if when and created and when < created:
                flags.append("date_suspect")  # comment dated before its case (export error)
            c_ref = f"case:{number}:c{i}"
            out.events.append(
                AssetEvent(
                    ref=c_ref, asset_id=asset_id, source="salesforce_comment", source_ref=f"{number}#c{i}",
                    event_date=when.date() if when else None, event_type="Case comment",
                    technician=author, notes=comment.get("CommentBody"),
                    linked_refs=[c for c in _linked_cases(comment.get("CommentBody", "")) if c != number],
                )
            )
            section.blocks.append(
                Block(kind="case_comment",
                      text=f"Comment on case {number} by {author or 'unknown'} ({when.date() if when else '?'}): "
                           f"{comment.get('CommentBody')}",
                      locator={"case": number, "comment": i, "date": when.date().isoformat() if when else None,
                               "label": f"case {number} comment"},
                      event_date=when.date() if when else None, asset_ids=[asset_id] if asset_id else [],
                      flags=flags, record_ref=c_ref)
            )
        for i, msg in enumerate((case.get("EmailMessages") or {}).get("records", []), start=1):
            when = _sf_datetime(msg.get("MessageDate"))
            section.blocks.append(
                Block(kind="email_msg",
                      text=f"Email on case {number} from {msg.get('FromName')} ({when.date() if when else '?'}),"
                           f" subject '{msg.get('Subject')}': {msg.get('TextBody')}",
                      locator={"case": number, "email": i, "from": msg.get("FromName"),
                               "date": when.date().isoformat() if when else None,
                               "label": f"case {number} email"},
                      event_date=when.date() if when else None, asset_ids=[asset_id] if asset_id else [])
            )
        out.sections.append(section)
    return out


# ---------------------------------------------------------------- Teams channel export
def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def load_teams(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    data = json.loads(path.read_text(encoding="utf-8"))
    channel = (data.get("channel") or {}).get("displayName", "channel")
    out.metadata = {"channel": channel, "exported": data.get("exportedDateTime")}
    messages = [m for m in data.get("value", []) if not m.get("deletedDateTime")]
    threads: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        threads[m.get("replyToId") or m["id"]].append(m)
    section = Section(heading_path=[f"Teams #{channel}"])
    for root_id, msgs in sorted(threads.items(), key=lambda kv: kv[1][0]["createdDateTime"]):
        msgs.sort(key=lambda m: m["createdDateTime"])
        lines = []
        for m in msgs:
            body = _strip_html(m["body"]["content"])
            if not body:
                continue
            who = ((m.get("from") or {}).get("user") or {}).get("displayName") or "unknown"
            lines.append(f"[{m['createdDateTime'][:16].replace('T', ' ')} UTC] {who}: {body}")
        if not lines:
            continue
        # Long threads are windowed so each chunk stays focused but keeps neighbouring context.
        window = 12
        for start in range(0, len(lines), window - 2):
            part = lines[start:start + window]
            first = msgs[min(start, len(msgs) - 1)]
            when = datetime.fromisoformat(first["createdDateTime"].replace("Z", "+00:00"))
            text = f"Teams #{channel} thread:\n" + "\n".join(part)
            section.blocks.append(
                Block(kind="chat_thread", text=text,
                      locator={"thread": root_id, "first_message_utc": first["createdDateTime"],
                               "label": f"Teams #{channel} {when:%d %b %Y}"},
                      event_date=when.date(),
                      asset_ids=sorted(set(ASSET_RE.findall(text))))
            )
            if start + window >= len(lines):
                break
    out.sections = [section]
    return out


def fault_codes_in(text: str) -> list[str]:
    return sorted({f"{m.group(1)}-{m.group(2)}" for m in FAULT_RE.finditer(text)})
