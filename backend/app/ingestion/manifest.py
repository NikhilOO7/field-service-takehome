"""Corpus discovery and document metadata (spec M3).

Metadata comes from, in order of precedence: config/corpus_manifest.yaml (curated) -> what the loader
found (e.g. the DOCX control block) -> rules from the path. permissions.json supplies title, source
system and required groups; files missing from it are 'unlisted' and denied to everyone.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from app.config import Settings
from app.ingestion.entities import extract_entities

AUTHORITY_RANK = {
    "oem_bulletin": 1,
    "oem_manual": 2,
    "controlled_procedure": 3,
    "legacy_wi": 4,
    "case_export": 5,
    "cmms_export": 5,
    "email": 5,
    "asset_register": 5,
    "ops_review": 5,
    "record": 5,
    "wiki": 6,
    "chat": 7,
}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".html", ".htm", ".eml", ".json", ".csv"}


@dataclass
class DocSpec:
    path: str  # relative to project root, e.g. corpus/manuals/x.pdf
    abs_path: Path
    sha256: str
    title: str
    source_system: str | None
    required_groups: list[str]
    acl_status: str
    manifest: dict = field(default_factory=dict)

    @property
    def doc_type(self) -> str:
        return self.manifest.get("doc_type") or guess_doc_type(self.path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def guess_doc_type(rel_path: str) -> str:
    p = rel_path.lower()
    name = Path(p).name
    if "/bulletins/" in p or re.search(r"\bsb-\d{4}-\d{3}", name):
        return "oem_bulletin"
    if "/manuals/" in p:
        return "oem_manual"
    if "/confluence/" in p:
        return "wiki"
    if "/email/" in p or name.endswith(".eml"):
        return "email"
    if "/chat/" in p:
        return "chat"
    if "/sharepoint/" in p:
        if name.startswith("kt-wi"):
            return "legacy_wi"
        if re.match(r"(sop|hv-proc|cs-proc|fs-std)", name):
            return "controlled_procedure"
        return "record"
    if "fleet" in name:
        return "asset_register"
    if "salesforce" in name or "case" in name:
        return "case_export"
    if "maintpro" in name or "servio" in name or "history" in name:
        return "cmms_export"
    return "record"


def load_manifest(settings: Settings) -> dict:
    path = settings.config_dir / "corpus_manifest.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_permissions(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get("documents", {})


def discover(settings: Settings, permissions_file: Path | None = None, only: Path | None = None) -> list[DocSpec]:
    permissions = load_permissions(settings.permissions_file)
    if permissions_file is not None:
        permissions.update(load_permissions(permissions_file))
    manifest = load_manifest(settings)
    root = settings.project_root
    search_root = only or settings.corpus_root
    candidates = [search_root] if search_root.is_file() else search_root.rglob("*")
    files = sorted(p for p in candidates if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
                   and not p.name.startswith("."))
    specs = []
    for f in files:
        rel = f.resolve().relative_to(root).as_posix()
        perm = permissions.get(rel)
        specs.append(
            DocSpec(
                path=rel,
                abs_path=f,
                sha256=sha256_file(f),
                title=(perm or {}).get("title") or f.stem.replace("_", " "),
                source_system=(perm or {}).get("source_system"),
                required_groups=list((perm or {}).get("required_groups", [])),
                acl_status="listed" if perm is not None else "unlisted",
                manifest=manifest.get(rel, {}),
            )
        )
    return specs


def _as_date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def document_metadata(spec: DocSpec, parsed_meta: dict) -> dict:
    """Merge curated manifest, parser findings and path rules into the documents row."""
    m = spec.manifest
    doc_type = spec.doc_type
    entities = extract_entities(spec.path.replace("_", " ") + " " + spec.title)
    status = m.get("status", "current")
    if parsed_meta.get("personal_space"):
        status = "unofficial"
    effective = _as_date(m.get("effective_date")) or _as_date(parsed_meta.get("effective_date")) or _as_date(
        parsed_meta.get("last_modified")
    )
    metadata = {k: v for k, v in parsed_meta.items() if isinstance(v, (str, int, float, bool, list, dict))}
    if m.get("stale_reason"):
        metadata["stale_reason"] = m["stale_reason"]
    next_review = str(parsed_meta.get("next_review") or "")
    if "overdue" in next_review.lower():
        metadata["review_overdue"] = True
    return {
        "title": spec.title,
        "doc_number": m.get("doc_number") or parsed_meta.get("doc_number")
        or (entities.doc_numbers[0] if entities.doc_numbers else None),
        "revision": str(m["revision"]) if m.get("revision") is not None else parsed_meta.get("revision"),
        "effective_date": effective,
        "source_system": spec.source_system,
        "doc_type": doc_type,
        "authority_rank": AUTHORITY_RANK.get(doc_type, 5),
        "status": status,
        "models": m.get("models") or entities.models,
        "language": m.get("language") or ["en"],
        "metadata": metadata,
    }
