"""Citation labels (spec M5): 'HCP-500 IOM Rev B · p.9 · §9.1 (P-07)'."""

from datetime import date


def _short_title(title: str, doc_number: str | None, revision: str | None, doc_type: str) -> str:
    if doc_type in {"oem_bulletin", "controlled_procedure", "legacy_wi"} and doc_number:
        return f"{doc_number}" + (f" Rev {revision}" if revision and doc_type != "oem_bulletin" else "")
    return title


def citation_label(title: str, doc_type: str, doc_number: str | None, revision: str | None, locator: dict,
                   effective_date: date | str | None = None, flags: list[str] | None = None) -> str:
    loc = locator or {}
    parts = [_short_title(title, doc_number, revision, doc_type)]
    if loc.get("case"):
        part = f"case {loc['case']}"
        if loc.get("comment"):
            part += f" comment ({loc.get('date')})"
        elif loc.get("email"):
            part += f" email from {loc.get('from')} ({loc.get('date')})"
        return f"Salesforce {part}"
    if loc.get("wo"):
        return f"{title} · WO {loc['wo']} · {loc.get('date')}"
    if loc.get("row") and doc_type == "asset_register":
        return f"Fleet register · {loc['row']}"
    if loc.get("message"):
        return f"Email: {loc.get('from')} → {loc.get('to') or '…'}, {loc.get('date')}" + (
            f" · p.{loc['page']}" if loc.get("page") else "")
    if loc.get("thread"):
        return f"{title} · {str(loc.get('first_message_utc', ''))[:10]}"
    if loc.get("comment_id"):
        return f"{title} (wiki) · comment by {loc.get('author')}, {loc.get('date')}"
    if loc.get("page"):
        parts.append(f"p.{loc['page']}" + (f"–{loc['page_to']}" if loc.get("page_to") else ""))
    if loc.get("section"):
        parts.append(f"§{loc['section']}")
    elif loc.get("heading") and loc["heading"] not in {"(start)", "(page)"}:
        parts.append(f"“{loc['heading'][:60]}”")
    if loc.get("step"):
        parts.append(f"step {loc['step']}")
    if loc.get("label") and not loc.get("step"):
        parts.append(f"({loc['label']})")
    if doc_type == "wiki":
        parts[0] += " (wiki)"
    if flags and "ocr" in flags:
        parts[0] += " (scan, OCR)"
    return " · ".join(parts)
