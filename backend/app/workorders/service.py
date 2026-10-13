"""Work orders (spec M12): draft from an owned answer, rule-filled fields with provenance, validation,
state machine (draft -> pending_approval -> approved -> sent | rejected) and the simulated Salesforce outbox.

Ownership: the creator reads/edits/submits; approvers see the pending queue and what they decided.
A foreign or missing work order is reported as not found (404).
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from app.answer.validate import PARAM_RE, build_fingerprints, identifiers_in, leak_hits, numbers_in
from app.auth.acl import acl_clause, acl_params, not_acl_clause
from app.auth.users import UserContext, get_user
from app.config import get_settings
from app.conversations import audit, get_message
from app.ingestion.entities import DOCNUM_RE, FAULT_FAMILY, FAULT_RE, PART_RE, model_family

CERTS = ["None", "HV", "Refrigerant", "Confined space"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]
EDITABLE = {"fault_code", "summary", "observations", "work_steps", "procedure_refs", "parts",
            "required_certifications", "site_permits", "priority", "priority_reason", "estimated_duration_h",
            "campaign_code", "asset_id"}
HV_CODES = {"E-31", "E-33", "E-35", "E-36"}


class WorkOrderError(Exception):
    def __init__(self, status: int, message: str, details: list | None = None):
        super().__init__(message)
        self.status = status
        self.details = details or []


def _field(value, provenance: str, citations: list[str] | None = None) -> dict:
    return {"value": value, "provenance": provenance, "citations": citations or []}


# ------------------------------------------------------------------ rules (system fields)
def _asset_row(conn: Connection, asset_id: str | None) -> dict | None:
    if not asset_id:
        return None
    row = conn.execute(text("SELECT * FROM assets WHERE asset_id = :a"), {"a": asset_id}).mappings().first()
    return dict(row) if row else None


def user_regions(user: UserContext) -> list[str]:
    return [g.removeprefix("region-").upper() for g in user.groups if g.startswith("region-")]


def list_assets(conn: Connection, user: UserContext) -> list[dict]:
    """Machines a technician can raise a work order for: the fleet register for their own region(s)."""
    rows = conn.execute(
        text("""SELECT asset_id, model, serial, site_name FROM assets
                WHERE region = ANY(CAST(:r AS text[])) ORDER BY site_name, asset_id"""),
        {"r": user_regions(user)},
    ).mappings().all()
    return [dict(r) for r in rows]


def required_certifications(asset: dict | None, fault_code: str | None, text_blob: str, hints: list[dict]) -> list[str]:
    certs = set()
    labels = {"cert-hv": "HV", "cert-refrigerant": "Refrigerant", "cert-confined-space": "Confined space"}
    for h in hints:
        for g in h.get("missing", []):
            if g in labels and h.get("disclosure") == "certification":
                certs.add(labels[g])
    blob = text_blob.lower()
    if fault_code in HV_CODES or "vfd cabinet" in blob or "drive cabinet" in blob:
        certs.add("HV")
    model = (asset or {}).get("model", "")
    if model.startswith("NK") and (fault_code in {"L-03", "L-02", "L-12"} or re.search(
            r"refrigerant|exv|recharge|leak test|63-1180|63-0410|brazing", blob)):
        certs.add("Refrigerant")
    if re.search(r"\bpit\b|pp-1|pp-2|confined space", blob):
        certs.add("Confined space")
    return sorted(certs) or ["None"]


def site_permits(asset: dict | None, certs: list[str]) -> list[str]:
    permits = []
    if asset and asset["asset_id"].startswith("RTM-H-"):
        permits.append("Work at height permit (roof, Rotterdam site rules)")
    if "HV" in certs:
        permits.append("Electrical isolation record (HV-PROC-02 Appendix A)")
    if "Confined space" in certs:
        permits.append("Confined space entry permit (CS-PROC-03) + site permit")
    return permits


def priority_rule(conn: Connection, user: UserContext, asset_id: str | None, fault_code: str | None) -> tuple[str, str]:
    if not asset_id:
        return "Medium", "Corrective work (no asset history available)"
    open_case = conn.execute(
        text(f"""SELECT source_ref, event_type FROM asset_events e WHERE asset_id = :a
                 AND source = 'salesforce_case' AND status NOT IN ('Closed', 'Resolved') AND {acl_clause('e')}
                 ORDER BY event_date DESC LIMIT 1"""),
        {"a": asset_id, **acl_params(user)},
    ).first()
    if open_case and "Critical" in (open_case[1] or ""):
        return "Critical", f"Open case {open_case[0]} is Critical"
    if fault_code:
        days = conn.execute(
            text(f"""SELECT event_date, count(*) FROM asset_events e WHERE asset_id = :a AND fault_code = :f
                     AND {acl_clause('e')} AND event_date IS NOT NULL GROUP BY event_date ORDER BY event_date"""),
            {"a": asset_id, "f": fault_code, **acl_params(user)},
        ).all()
        if any(n >= 3 for _, n in days):
            return "High", "Three or more trips of the same fault in 24 h (FS-STD-001 9.2)"
        if len(days) >= 2:
            return "High", f"Repeat {fault_code} on this asset ({len(days)} occurrences in history)"
    if open_case and "High" in (open_case[1] or ""):
        return "High", f"Open case {open_case[0]} is High"
    return "Medium", "Equipment fault needing follow-up"


def bulletin_fields(conn: Connection, user: UserContext, asset: dict | None) -> tuple[str, str | None, str | None]:
    if not asset:
        return "Bulletins not checked: no asset identified", None, None
    from app.retrieval.structured import Asset, applicable_bulletins

    a = Asset(**{k: asset[k] for k in Asset.__dataclass_fields__ if k in asset}
              | {"source_chunk": str(asset["source_chunk"]) if asset.get("source_chunk") else None})
    matches = [b for b in applicable_bulletins(conn, a, user) if b.visible]
    if not matches:
        return f"Bulletins checked: none applicable to serial {asset['serial']}", None, None
    text_ = "; ".join(f"{b.number} applies to serial {asset['serial']}" + (
        f" (campaign {b.campaign_code})" if b.campaign_code else "") for b in matches)
    campaign = next((b.campaign_code for b in matches if b.campaign_code), None)
    claim = next((b.claim_code for b in matches if b.claim_code), None)
    return text_, campaign, claim


# ------------------------------------------------------------------ validation
@dataclass
class Issue:
    field: str
    severity: str  # error | warning
    message: str

    def as_dict(self) -> dict:
        return {"field": self.field, "severity": self.severity, "message": self.message}


def _visible_parts(conn: Connection, user: UserContext, part_no: str) -> list[dict]:
    rows = conn.execute(
        text(f"""SELECT p.part_no, p.description, p.status, p.superseded_by FROM parts p
                 JOIN chunks c ON c.id = p.source_chunk WHERE p.part_no = :p AND {acl_clause('c')}"""),
        {"p": part_no, **acl_params(user)},
    ).mappings().all()
    return [dict(r) for r in rows]


def _visible_doc_numbers(conn: Connection, user: UserContext) -> set[str]:
    """Doc numbers the creator may reference: documents they can see, plus procedure numbers named in them
    (FS-STD-001 names HV-PROC-07 as restricted; the number may be referenced, its steps may not)."""
    rows = conn.execute(
        text(f"""SELECT DISTINCT unnest(c.doc_numbers) FROM chunks c WHERE {acl_clause('c')}"""),
        acl_params(user)).all()
    own = conn.execute(text(f"""SELECT DISTINCT d.doc_number FROM documents d JOIN chunks c ON c.document_id = d.id
                                WHERE d.doc_number IS NOT NULL AND {acl_clause('c')}"""), acl_params(user)).all()
    return {r[0] for r in rows} | {r[0] for r in own}


def _restricted_fingerprints(conn: Connection, user: UserContext, permitted_text: str) -> set[str]:
    rows = conn.execute(
        text(f"""SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id
                 WHERE {not_acl_clause('c')} AND d.doc_type = 'controlled_procedure'"""),
        acl_params(user)).all()
    return build_fingerprints([r[0] for r in rows], permitted_text)


def validate_work_order(conn: Connection, creator: UserContext, payload: dict, permitted_text: str) -> list[Issue]:
    issues: list[Issue] = []
    get = lambda name: (payload.get(name) or {}).get("value")
    asset = _asset_row(conn, get("asset_id"))
    if asset is None:
        issues.append(Issue("asset_id", "error", "choose the machine this work is for"
                            if not get("asset_id") else "asset not in the fleet register"))
    elif asset["region"] not in user_regions(creator):
        issues.append(Issue("asset_id", "error", f"{asset['asset_id']} is outside your region"))
    else:
        for f in ("site", "model", "serial"):
            if get(f) and get(f) != (asset["site_name"] if f == "site" else asset[f]):
                issues.append(Issue(f, "error", f"does not match the fleet register for {asset['asset_id']}"))

    fault = get("fault_code")
    if fault and fault != "None":
        if not FAULT_RE.fullmatch(fault):
            issues.append(Issue("fault_code", "error", "not a controller fault code (e.g. E-31, L-03)"))
        elif asset and FAULT_FAMILY.get(fault[0]) != model_family(asset["model"]):
            issues.append(Issue("fault_code", "error", f"{fault} is not a {asset['model']} code"))

    blob = " ".join(str(get(f) or "") for f in ("summary", "observations")) + " " + " ".join(
        s for s in (get("work_steps") or []))
    fingerprints = _restricted_fingerprints(conn, creator, permitted_text)
    for name in ("summary", "observations", "work_steps"):
        value = get(name)
        values = value if isinstance(value, list) else [value] if value else []
        for v in values:
            hits = leak_hits(str(v), fingerprints)
            if hits:
                issues.append(Issue(name, "error", f"contains restricted procedure details {hits}"))
    for step in get("work_steps") or []:
        if len(step) > 220 or len(PARAM_RE.findall(step)) > 2:
            issues.append(Issue("work_steps", "warning", "reference the procedure number; don't copy its steps"))
            break

    allowed_docs = _visible_doc_numbers(conn, creator)
    for ref in get("procedure_refs") or []:
        if ref not in allowed_docs:
            issues.append(Issue("procedure_refs", "error", f"{ref} is not a document you can reference"))

    for part in get("parts") or []:
        rows = _visible_parts(conn, creator, part.get("part_no", ""))
        if not rows:
            issues.append(Issue("parts", "error", f"{part.get('part_no')} is not in the parts catalogue"))
            continue
        statuses = {r["status"] for r in rows}
        if "blocked" in statuses:
            issues.append(Issue("parts", "error", f"{part['part_no']} is blocked for sale"))
        elif "superseded" in statuses:
            repl = next((r["superseded_by"] for r in rows if r["superseded_by"]), None)
            issues.append(Issue("parts", "warning", f"{part['part_no']} is superseded by {repl}"))
        if not isinstance(part.get("qty"), int) or part["qty"] < 1:
            issues.append(Issue("parts", "error", f"quantity for {part['part_no']} must be a positive integer"))

    minimum = required_certifications(asset, fault, blob, payload.get("_hints", {}).get("value") or [])
    chosen = set(get("required_certifications") or [])
    missing = [c for c in minimum if c != "None" and c not in chosen]
    if missing:
        issues.append(Issue("required_certifications", "error", f"must include {', '.join(missing)}"))
    if not chosen:
        issues.append(Issue("required_certifications", "error", "required (FS-STD-001 7.1)"))

    _, campaign, _ = bulletin_fields(conn, creator, asset)
    if campaign and get("campaign_code") != campaign:
        issues.append(Issue("campaign_code", "error", f"applicable campaign {campaign} must be set"))
    if not campaign and get("campaign_code"):
        issues.append(Issue("campaign_code", "error", "no applicable campaign for this serial"))

    if get("priority") not in PRIORITIES:
        issues.append(Issue("priority", "error", "priority must be Critical, High, Medium or Low"))
    if not get("priority_reason"):
        issues.append(Issue("priority_reason", "error", "a reason is required for the priority"))

    for case in get("related_cases") or []:
        ok = conn.execute(text(f"""SELECT 1 FROM asset_events e WHERE source_ref = :c AND {acl_clause('e')}"""),
                          {"c": case, **acl_params(creator)}).first()
        if not ok:
            issues.append(Issue("related_cases", "error", f"case {case} not found in your history"))

    duration = get("estimated_duration_h")
    if duration not in (None, ""):
        try:
            if not 0 < float(duration) <= 24:
                raise ValueError
        except (TypeError, ValueError):
            issues.append(Issue("estimated_duration_h", "warning", "expected hours between 0 and 24"))

    permits_min = site_permits(asset, list(chosen))
    have = set(get("site_permits") or [])
    if any(p not in have for p in permits_min):
        issues.append(Issue("site_permits", "warning", "site permits below the rule minimum"))
    return issues


# ------------------------------------------------------------------ repository (owner-scoped)
def _load(conn: Connection, wo_id: str, lock: bool = False) -> dict | None:
    """lock=True takes a row lock for the rest of the transaction: state transitions (submit, approve, reject,
    edit) are serialised, so of two concurrent requests the second sees the first one's result."""
    try:
        uuid.UUID(str(wo_id))
    except ValueError:
        return None
    sql = "SELECT * FROM work_orders WHERE id = :id" + (" FOR UPDATE" if lock else "")
    row = conn.execute(text(sql), {"id": wo_id}).mappings().first()
    return dict(row) if row else None


def is_approver(user: UserContext) -> bool:
    return get_settings().wo_approver_role == "any" or user.is_lead


def reviewer_can_read(conn: Connection, reviewer: UserContext, wo: dict) -> bool:
    """Approval authority is not read access. A reviewer may see another user's work order only if they could
    have seen everything it was built from: every chunk in the source answer's context, and the machine's
    region. Otherwise the queue would carry one user's permitted evidence into another user's view."""
    if wo["source_message"]:
        hidden = conn.execute(
            text(f"""SELECT count(*) FROM messages m, unnest(m.context_chunk_ids) AS cid
                     JOIN chunks c ON c.id = cid
                     WHERE m.id = :m AND NOT {acl_clause('c')}"""),
            {"m": wo["source_message"], **acl_params(reviewer)}).scalar_one()
        missing = conn.execute(
            text("""SELECT count(*) FROM messages m, unnest(m.context_chunk_ids) AS cid
                    WHERE m.id = :m AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id = cid)"""),
            {"m": wo["source_message"]}).scalar_one()
        if hidden or missing:  # a chunk deleted since is unknown content: deny
            return False
    asset = _asset_row(conn, (wo["payload"].get("asset_id") or {}).get("value"))
    return asset is None or asset["region"] in user_regions(reviewer)


def eligible_reviewers(conn: Connection, wo: dict) -> list[dict]:
    """Who could approve this work order: approvers other than the creator who can read all of its evidence.
    Empty means it would sit in the queue unseen; the UI warns the technician before and after submitting."""
    from app.auth.users import list_users

    people = [UserContext(u["id"], u["name"], u["title"], frozenset(u["groups"])) for u in list_users(conn)]
    return [{"id": u.id, "name": u.name, "title": u.title} for u in people
            if u.id != wo["created_by"] and is_approver(u) and reviewer_can_read(conn, u, wo)]


def _reviewable(conn: Connection, reviewer: UserContext, wo: dict) -> bool:
    return (is_approver(reviewer) and wo["created_by"] != reviewer.id
            and (wo["status"] == "pending_approval" or wo["approved_by"] == reviewer.id)
            and reviewer_can_read(conn, reviewer, wo))


def get_work_order(conn: Connection, wo_id: str, user: UserContext) -> dict | None:
    wo = _load(conn, wo_id)
    if wo is None:
        return None
    if wo["created_by"] == user.id or _reviewable(conn, user, wo):
        return wo
    audit(conn, user.id, "ownership_denied", "work_order", str(wo_id), "404")
    return None


def list_work_orders(conn: Connection, user: UserContext) -> list[dict]:
    rows = conn.execute(
        text("""SELECT id, status, created_by, payload, validation, approved_by, created_at, updated_at,
                       source_message
                FROM work_orders WHERE created_by = :u
                   OR (:approver AND status = 'pending_approval') OR approved_by = :u
                ORDER BY updated_at DESC"""),
        {"u": user.id, "approver": is_approver(user)},
    ).mappings().all()
    return [dict(r) for r in rows if r["created_by"] == user.id or _reviewable(conn, user, dict(r))]


def _permitted_text_for(conn: Connection, user: UserContext, message: dict | None) -> str:
    if not message:
        return ""
    sources = (message.get("answer") or {}).get("sources", [])
    return message["question"] + "\n" + "\n".join(s.get("text", "") for s in sources)


def _event(conn: Connection, wo_id, actor: str, action: str, diff: dict | None = None) -> None:
    conn.execute(text("INSERT INTO work_order_events (work_order_id, actor, action, diff) VALUES (:w, :a, :x, "
                      "CAST(:d AS jsonb))"), {"w": wo_id, "a": actor, "x": action,
                                              "d": json.dumps(diff or {}, default=str)})


def _save_validation(conn: Connection, wo_id, issues: list[Issue]) -> None:
    conn.execute(text("UPDATE work_orders SET validation = CAST(:v AS jsonb), updated_at = now() WHERE id = :id"),
                 {"v": json.dumps([i.as_dict() for i in issues]), "id": wo_id})


def _doc_number(raw: str) -> str:
    m = DOCNUM_RE.search(raw)
    return m.group(1) if m else raw.strip()


def _part_no(raw: str) -> str:
    m = PART_RE.search(raw)
    return f"{m.group(1)}-{m.group(2)}" if m else raw.strip()


# ------------------------------------------------------------------ operations
def create_from_message(conn: Connection, user: UserContext, message_id: str) -> dict:
    message = get_message(conn, message_id, user)
    if message is None:
        audit(conn, user.id, "ownership_denied", "message", str(message_id), "404")
        raise WorkOrderError(404, "message not found")
    response = message["answer"] or {}
    # every cited chunk must still pass the creator's ACL (permissions may have changed)
    cited = [s["chunk_id"] for s in response.get("sources", []) if s.get("cited")]
    if cited:
        visible = {str(r[0]) for r in conn.execute(
            text(f"SELECT c.id FROM chunks c WHERE c.id = ANY(CAST(:ids AS uuid[])) AND {acl_clause('c')}"),
            {"ids": cited, **acl_params(user)})}
        if set(cited) - visible:
            audit(conn, user.id, "acl_denied", "message", str(message_id), "403")
            raise WorkOrderError(403, "the answer cites documents you can no longer access")

    answer = response.get("answer", {})
    hint = (answer.get("follow_up") or {}).get("work_order_hint") or {}
    assets = response.get("assets", [])
    asset = _asset_row(conn, assets[0]["asset_id"]) if assets else None
    fault = (hint.get("fault_code") or {}).get("value") or next(
        iter(re.findall(r"\b[EPLC]-\d{2}\b", message["question"])), None)
    hints = (response.get("restricted") or {}).get("procedures") and [
        {"missing": [{"HV": "cert-hv", "Refrigerant": "cert-refrigerant", "Confined space": "cert-confined-space"}[c]
                     for c in response["restricted"]["required_certifications"]], "disclosure": "certification"}] or []
    blob = " ".join([message["question"], json.dumps(hint)])
    certs = required_certifications(asset, fault, blob, hints)
    bulletins_text, campaign, claim = bulletin_fields(conn, user, asset)
    priority, reason = priority_rule(conn, user, asset["asset_id"] if asset else None, fault)
    history_cases = [e["ref"] for aid, h in (response.get("history") or {}).items() for e in h.get("events", [])
                     if e.get("source") == "salesforce_case" and (not fault or e.get("fault_code") == fault)]

    def hint_text(name):
        v = hint.get(name)
        return _field(v["text"], "answer", v["citations"]) if v else _field(None, "answer")

    payload = {
        "asset_id": _field(asset["asset_id"] if asset else None, "system"),
        "site": _field(asset["site_name"] if asset else None, "system"),
        "customer": _field(asset["customer"] if asset else None, "system"),
        "model": _field(asset["model"] if asset else None, "system"),
        "serial": _field(asset["serial"] if asset else None, "system"),
        "fault_code": _field(fault, "answer" if hint.get("fault_code") else "system",
                             (hint.get("fault_code") or {}).get("citations")),
        "summary": hint_text("summary"),
        "observations": hint_text("observations"),
        "work_steps": _field([s["text"] for s in hint.get("work_steps", [])], "answer",
                             sorted({c for s in hint.get("work_steps", []) for c in s["citations"]})),
        "procedure_refs": _field(list(dict.fromkeys(_doc_number(d["doc_number"]) for d in hint.get("procedure_refs", []))), "answer",
                                 sorted({c for d in hint.get("procedure_refs", []) for c in d["citations"]})),
        "parts": _field([{"part_no": _part_no(p["part_no"]), "qty": p["qty"]} for p in hint.get("parts", [])], "answer",
                        sorted({c for p in hint.get("parts", []) for c in p["citations"]})),
        "required_certifications": _field(certs, "system"),
        "site_permits": _field(site_permits(asset, certs), "system"),
        "priority": _field(priority, "system"),
        "priority_reason": _field(reason, "system"),
        "campaign_code": _field(campaign, "system"),
        "claim_code": _field(claim, "system"),
        "bulletins_checked": _field(bulletins_text, "system"),
        "related_cases": _field(sorted(set(history_cases))[:5], "system"),
        "estimated_duration_h": _field((hint.get("estimated_duration_h") or {}).get("value"), "answer",
                                       (hint.get("estimated_duration_h") or {}).get("citations")),
        "source_question": _field(message["question"], "technician"),
        "_hints": _field(hints, "system"),
    }
    # describe parts from the catalogue
    for p in payload["parts"]["value"]:
        rows = _visible_parts(conn, user, p["part_no"])
        p["description"] = next((r["description"] for r in rows if r["description"]), None)

    wo_id = uuid.uuid4()
    conn.execute(
        text("""INSERT INTO work_orders (id, status, created_by, source_message, payload)
                VALUES (:id, 'draft', :u, :m, CAST(:p AS jsonb))"""),
        {"id": wo_id, "u": user.id, "m": message_id, "p": json.dumps(payload, default=str)},
    )
    issues = validate_work_order(conn, user, payload, _permitted_text_for(conn, user, message))
    _save_validation(conn, wo_id, issues)
    _event(conn, wo_id, user.id, "created", {"from_message": message_id})
    return _load(conn, str(wo_id))


def update_draft(conn: Connection, user: UserContext, wo_id: str, changes: dict) -> dict:
    wo = _load(conn, wo_id, lock=True)
    if wo is None or wo["created_by"] != user.id:
        audit(conn, user.id, "ownership_denied", "work_order", str(wo_id), "404")
        raise WorkOrderError(404, "work order not found")
    if wo["status"] != "draft":
        raise WorkOrderError(409, f"work order is {wo['status']}; only drafts can be edited")
    payload = wo["payload"]
    diff = {}
    for name, value in changes.items():
        if name not in EDITABLE:
            raise WorkOrderError(422, f"field {name} cannot be edited")
        old = (payload.get(name) or {}).get("value")
        if old != value:
            payload[name] = _field(value, "technician")
            diff[name] = {"old": old, "new": value}
    if "asset_id" in diff:
        asset = _asset_row(conn, changes["asset_id"])
        for f, col in (("site", "site_name"), ("customer", "customer"), ("model", "model"), ("serial", "serial")):
            payload[f] = _field(asset[col] if asset else None, "system")
        # Rule-derived fields follow the new machine, unless the technician has set them by hand.
        get = lambda name: (payload.get(name) or {}).get("value")
        fault = get("fault_code")
        blob = " ".join(str(get(f) or "") for f in ("source_question", "summary", "observations"))
        bulletins_text, campaign, claim = bulletin_fields(conn, user, asset)
        priority, reason = priority_rule(conn, user, asset["asset_id"] if asset else None, fault)
        certs = required_certifications(asset, fault, blob, get("_hints") or [])
        derived = {"bulletins_checked": bulletins_text, "campaign_code": campaign, "claim_code": claim,
                   "priority": priority, "priority_reason": reason, "required_certifications": certs,
                   "site_permits": site_permits(asset, certs)}
        for name, value in derived.items():
            if (payload.get(name) or {}).get("provenance") != "technician":
                payload[name] = _field(value, "system")
    conn.execute(text("UPDATE work_orders SET payload = CAST(:p AS jsonb), updated_at = now() WHERE id = :id"),
                 {"p": json.dumps(payload, default=str), "id": wo_id})
    message = get_message(conn, str(wo["source_message"]), user) if wo["source_message"] else None
    _save_validation(conn, wo_id, validate_work_order(conn, user, payload, _permitted_text_for(conn, user, message)))
    _event(conn, wo_id, user.id, "edited", diff)
    return _load(conn, wo_id)


def _errors(wo: dict) -> list[dict]:
    return [i for i in wo["validation"] if i["severity"] == "error"]


def submit(conn: Connection, user: UserContext, wo_id: str) -> dict:
    wo = _load(conn, wo_id, lock=True)
    if wo is None or wo["created_by"] != user.id:
        audit(conn, user.id, "ownership_denied", "work_order", str(wo_id), "404")
        raise WorkOrderError(404, "work order not found")
    if wo["status"] != "draft":
        raise WorkOrderError(409, f"work order is {wo['status']}")
    message = get_message(conn, str(wo["source_message"]), user) if wo["source_message"] else None
    issues = validate_work_order(conn, user, wo["payload"], _permitted_text_for(conn, user, message))
    _save_validation(conn, wo_id, issues)
    errors = [i.as_dict() for i in issues if i.severity == "error"]
    if errors:
        raise WorkOrderError(422, "fix the validation errors before submitting", errors)
    conn.execute(text("UPDATE work_orders SET status = 'pending_approval', updated_at = now() WHERE id = :id"),
                 {"id": wo_id})
    _event(conn, wo_id, user.id, "submitted")
    return _load(conn, wo_id)


def approve(conn: Connection, approver: UserContext, wo_id: str) -> dict:
    wo = _load(conn, wo_id, lock=True)
    if wo is None or not (wo["created_by"] == approver.id or _reviewable(conn, approver, wo)):
        audit(conn, approver.id, "ownership_denied", "work_order", str(wo_id), "404")
        raise WorkOrderError(404, "work order not found")
    if wo["status"] != "pending_approval":
        raise WorkOrderError(409, f"work order is {wo['status']}")
    if wo["created_by"] == approver.id:  # four-eyes: nobody approves their own work order
        audit(conn, approver.id, "self_approval_denied", "work_order", str(wo_id), "403")
        raise WorkOrderError(403, "you can't approve your own work order")
    creator = get_user(conn, wo["created_by"])
    message = get_message(conn, str(wo["source_message"]), creator) if wo["source_message"] else None
    issues = validate_work_order(conn, creator, wo["payload"], _permitted_text_for(conn, creator, message))
    _save_validation(conn, wo_id, issues)
    errors = [i.as_dict() for i in issues if i.severity == "error"]
    if errors:
        raise WorkOrderError(422, "work order no longer validates", errors)
    now = datetime.now(UTC)
    conn.execute(text("""UPDATE work_orders SET status = 'approved', approved_by = :a, approved_at = :t,
                         updated_at = now() WHERE id = :id"""), {"a": approver.id, "t": now, "id": wo_id})
    _event(conn, wo_id, approver.id, "approved")
    send(conn, approver, wo_id)
    return _load(conn, wo_id)


def send(conn: Connection, actor: UserContext, wo_id: str) -> None:
    """Simulated Salesforce: write the approved payload to the outbox table and var/outbox/<id>.json."""
    wo = _load(conn, wo_id)
    if wo is None or wo["status"] != "approved" or not wo["approved_by"]:
        raise WorkOrderError(409, "only approved work orders can be sent")
    payload = {k: v["value"] for k, v in wo["payload"].items() if not k.startswith("_")}
    record = {"work_order_id": str(wo_id), "created_by": wo["created_by"], "approved_by": wo["approved_by"],
              "approved_at": str(wo["approved_at"]), "fields": payload,
              "provenance": {k: v["provenance"] for k, v in wo["payload"].items() if not k.startswith("_")},
              "validation_warnings": [i for i in wo["validation"] if i["severity"] == "warning"]}
    conn.execute(text("INSERT INTO outbox (work_order_id, payload) VALUES (:w, CAST(:p AS jsonb))"),
                 {"w": wo_id, "p": json.dumps(record, default=str)})
    conn.execute(text("UPDATE work_orders SET status = 'sent', updated_at = now() WHERE id = :id"), {"id": wo_id})
    # The outbox row is the record. The JSON file is an export, written by the caller only after the
    # transaction commits (see app.main._wo_call), so a rollback never leaves a file behind.
    pending = conn.info.get("pending_files")
    if pending is not None:
        pending.append((get_settings().var_dir / "outbox" / f"{wo_id}.json",
                        json.dumps(record, indent=2, default=str)))
    _event(conn, wo_id, actor.id, "sent")


def reject(conn: Connection, approver: UserContext, wo_id: str, reason: str) -> dict:
    wo = _load(conn, wo_id, lock=True)
    # Same visibility rule as approve: anything the user can't read is "not found" so its existence isn't revealed.
    if wo is None or not (wo["created_by"] == approver.id or _reviewable(conn, approver, wo)):
        audit(conn, approver.id, "ownership_denied", "work_order", str(wo_id), "404")
        raise WorkOrderError(404, "work order not found")
    if wo["status"] != "pending_approval":
        raise WorkOrderError(409, f"work order is {wo['status']}")
    if wo["created_by"] == approver.id:
        audit(conn, approver.id, "self_approval_denied", "work_order", str(wo_id), "403")
        raise WorkOrderError(403, "you can't reject your own work order")
    if not reason.strip():
        raise WorkOrderError(422, "a reason is required")
    conn.execute(text("""UPDATE work_orders SET status = 'rejected', rejected_reason = :r, approved_by = NULL,
                         updated_at = now() WHERE id = :id"""), {"r": reason, "id": wo_id})
    _event(conn, wo_id, approver.id, "rejected", {"reason": reason})
    return _load(conn, wo_id)


def list_outbox(conn: Connection, user: UserContext) -> list[dict]:
    rows = conn.execute(
        text("""SELECT o.id, o.work_order_id, o.payload, o.created_at FROM outbox o
                JOIN work_orders w ON w.id = o.work_order_id
                WHERE w.created_by = :u OR w.approved_by = :u ORDER BY o.created_at DESC"""),
        {"u": user.id}).mappings().all()
    return [dict(r) for r in rows]


def events(conn: Connection, wo_id: str) -> list[dict]:
    rows = conn.execute(text("SELECT actor, action, diff, at FROM work_order_events WHERE work_order_id = :w "
                             "ORDER BY id"), {"w": wo_id}).mappings().all()
    return [dict(r) for r in rows]


__all__ = [
    "WorkOrderError",
    "approve",
    "create_from_message",
    "events",
    "get_work_order",
    "identifiers_in",
    "list_outbox",
    "list_work_orders",
    "numbers_in",
    "reject",
    "submit",
    "update_draft",
]
