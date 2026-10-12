"""Email loaders: .eml files and Outlook 'print to PDF' threads.

Both split the thread into individual messages (From/Sent headers, Dutch 'Van/Verzonden' too) so a
citation points at one message: 'Email: Henrik Sauer -> Olu Adeyemi, 11 May 2026'. External-sender
banners, signatures' legal disclaimers and confidentiality notices are stripped.
"""

import email
import re
from datetime import datetime
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path

import pymupdf
from dateutil import parser as dateparser

from app.ingestion.types import Block, ParsedDocument, Section

HEADER_START_RE = re.compile(r"^(From|Van):\s*(.+)$", re.M)
FIELD_RE = {
    "from": re.compile(r"^(?:From|Van):\s*(.+)$", re.M),
    "sent": re.compile(r"^(?:Sent|Verzonden|Date):\s*(.+)$", re.M),
    "to": re.compile(r"^(?:To|Aan):\s*(.+)$", re.M),
    "subject": re.compile(r"^(?:Subject|Onderwerp):\s*(.+)$", re.M),
}
NOISE_PATTERNS = [
    re.compile(r"CAUTION: This email originated from outside of Corvane Field Services\..*?safe\.", re.S),
    re.compile(r"\[EXTERNE AFZENDER / EXTERNAL SENDER\].*?ict-security@corvane\.eu", re.S),
    re.compile(r"CONFIDENTIALITY NOTICE:.*?prohibited\.", re.S),
    re.compile(r"Diese E-Mail enthält vertrauliche Informationen\..*?destroy this e-mail\.", re.S),
    re.compile(r"Halden Pumpen GmbH\s+\|\s+Sitz der Gesellschaft.*?DE \d{3} \d{3} \d{3}", re.S),
    re.compile(r"Belton Conveyors Ltd\. Registered in England.*?Belton Conveyors Ltd\.", re.S),
    re.compile(r"Dit e-mailbericht is uitsluitend bestemd.*?derived from this message\.", re.S),
    re.compile(r"^_{8,}$", re.M),
]


def _clean_body(text: str) -> str:
    for pattern in NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _name(value: str) -> str:
    return re.sub(r"\s*<.*?>", "", value).strip().strip('"')


def _parse_when(value: str | None):
    if not value:
        return None
    value = re.sub(r"^(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\s+", "", value.strip(), flags=re.I)
    dutch = {"januari": "January", "februari": "February", "maart": "March", "april": "April", "mei": "May",
             "juni": "June", "juli": "July", "augustus": "August", "september": "September",
             "oktober": "October", "november": "November", "december": "December"}
    for nl, en in dutch.items():
        value = re.sub(nl, en, value, flags=re.I)
    try:
        return dateparser.parse(value, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def split_thread(text: str, top_headers: dict | None = None) -> list[dict]:
    """Split a thread into messages. `top_headers` describes the first message when its headers are
    not in the body (.eml)."""
    starts = [m.start() for m in HEADER_START_RE.finditer(text)]
    chunks = []
    if top_headers is not None:
        first_end = starts[0] if starts else len(text)
        chunks.append({**top_headers, "body": text[:first_end], "offset": 0})
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        part = text[start:end]
        header_block = part[:600]
        headers = {k: (rx.search(header_block).group(1).strip() if rx.search(header_block) else None)
                   for k, rx in FIELD_RE.items()}
        # body starts after the subject line (or after the last header line found)
        last_header = max((m.end() for rx in FIELD_RE.values() for m in [rx.search(header_block)] if m), default=0)
        chunks.append({**headers, "body": part[last_header:], "offset": start})
    messages = []
    for c in chunks:
        body = _clean_body(c["body"])
        body = re.sub(r"^(Attachments|Bijlagen):[ \t]*\n?[^\n]*$", "", body, flags=re.M).strip()
        if len(body) < 3 or (not c.get("from") and len(body) < 80):
            continue  # print headers or empty fragments, not messages
        when = c["sent"] if isinstance(c["sent"], datetime) else _parse_when(c.get("sent"))
        messages.append({"from": _name(c["from"] or "unknown"), "to": _name(c.get("to") or ""),
                         "subject": c.get("subject"), "sent": when, "body": body, "offset": c["offset"]})
    return messages


def _messages_to_document(messages: list[dict], out: ParsedDocument, page_of=None) -> None:
    section = Section(heading_path=[out.metadata.get("subject") or "Email thread"])
    # oldest first reads naturally
    messages = sorted(messages, key=lambda m: m["sent"].timestamp() if m["sent"] else 0)
    for i, msg in enumerate(messages, start=1):
        when = msg["sent"].strftime("%d %b %Y %H:%M") if msg["sent"] else "date unknown"
        to = f" -> {msg['to']}" if msg["to"] else ""
        header = f"Email from {msg['from']}{to}, {when}"
        if msg["subject"]:
            header += f" ({msg['subject']})"
        locator = {"message": i, "from": msg["from"], "to": msg["to"],
                   "date": msg["sent"].date().isoformat() if msg["sent"] else None}
        if page_of:
            locator["page"] = page_of(msg["offset"])
        section.blocks.append(
            Block(kind="email_msg", text=f"{header}:\n{msg['body']}", locator=locator,
                  event_date=msg["sent"].date() if msg["sent"] else None)
        )
    out.sections = [section] if section.blocks else []


def load_eml(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part else ""
    if not text and msg.get_body(preferencelist=("html",)):
        from bs4 import BeautifulSoup

        text = BeautifulSoup(msg.get_body(preferencelist=("html",)).get_content(), "lxml").get_text("\n")
    sent = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    top = {"from": str(msg["From"] or ""), "to": str(msg["To"] or ""), "subject": str(msg["Subject"] or ""),
           "sent": sent}
    out.metadata = {"subject": str(msg["Subject"] or ""), "from": _name(str(msg["From"] or "")),
                    "date": sent.isoformat() if sent else None}
    _messages_to_document(split_thread(text, top_headers=top), out)
    if not out.sections:
        out.parse_status = "failed"
        out.parse_notes.append("no message body found")
    return out


def load_email_pdf(path: Path) -> ParsedDocument:
    out = ParsedDocument()
    doc = pymupdf.open(path)
    out.page_count = doc.page_count
    out.metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
    out.metadata["subject"] = out.metadata.get("title")
    pages = [p.get_text() for p in doc]
    # drop the page-number-only lines Outlook prints at the bottom
    pages = [re.sub(r"^\s*\d{1,3}\s*$", "", p, flags=re.M) for p in pages]
    text = "\n".join(pages)

    bounds, pos = [], 0
    for p in pages:
        pos += len(p) + 1
        bounds.append(pos)

    def page_of(offset: int) -> int:
        return next(i for i, end in enumerate(bounds, start=1) if offset < end or i == len(bounds))

    _messages_to_document(split_thread(text), out, page_of=page_of)
    if not out.sections:
        out.parse_status = "failed"
        out.parse_notes.append("no messages found")
    return out
