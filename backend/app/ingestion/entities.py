"""Deterministic entity extraction (spec M6): asset IDs, serials, models, fault codes, parts, doc numbers.

Used on every chunk at ingestion and on every question at query time.
"""

import re
from dataclasses import dataclass, field

MODEL_FAMILIES: dict[str, set[str]] = {
    "HCP": {"200", "350", "500"},
    "VRS": {"45", "75", "110"},
    "NK": {"300", "600"},
    "BX": {"12", "20"},
}
# Controller fault-code letter -> equipment family.
FAULT_FAMILY = {"E": "VRS", "P": "HCP", "L": "NK", "C": "BX"}

ASSET_RE = re.compile(r"\b(?:FRS|TAC|RTM|GDN)-[PCHV]-\d{4}\b")
SERIAL_RE = re.compile(r"\b(HCP(?:200|350|500)|VRS(?:45|75|110)|NK(?:300|600)|BX(?:12|20))-(\d{6})\b")
MODEL_RE = re.compile(r"\b(HCP|VRS|NK|BX)[- ]?(\d{2,3})\b")
FAULT_RE = re.compile(r"\b([EPLC])-(\d{2})\b")
# Part numbers like 63-1180; not inside dates (15-03-2021) or longer digit runs.
PART_RE = re.compile(r"(?<![\d-])(\d{2})-(\d{4})(?![\d-])")
DOCNUM_RE = re.compile(
    r"\b(SB-\d{4}-\d{3}|HV-PROC-\d{2}|CS-PROC-\d{2}|SOP-\d{3}|FS-STD-\d{3}|FS-REC-\d{3}|KT-WI-\d{2}"
    r"|HDN-[A-Z]{2,4}-\d{2,4}|NK-SM-\d{3}-EN|BC-OM-\d{3})\b"
)
CASE_RE = re.compile(r"\b000\d{5}\b")
CAMPAIGN_RE = re.compile(r"\b[A-Z]{2}-C\d{2}-\d{2}\b")


def normalize_model(prefix: str, number: str) -> str | None:
    prefix = prefix.upper()
    if number in MODEL_FAMILIES.get(prefix, set()):
        return f"{prefix}-{number}"
    return None


@dataclass
class Entities:
    asset_ids: list[str] = field(default_factory=list)
    serials: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    fault_codes: list[str] = field(default_factory=list)
    part_numbers: list[str] = field(default_factory=list)
    doc_numbers: list[str] = field(default_factory=list)
    case_numbers: list[str] = field(default_factory=list)
    campaign_codes: list[str] = field(default_factory=list)


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def extract_entities(text: str) -> Entities:
    serials = [f"{m.group(1)}-{m.group(2)}" for m in SERIAL_RE.finditer(text)]
    models = [normalize_model(m.group(1), m.group(2)) for m in MODEL_RE.finditer(text)]
    models += [normalize_model(re.match(r"[A-Z]+", s).group(), re.search(r"\d+", s).group()) for s in serials]
    return Entities(
        asset_ids=_unique(ASSET_RE.findall(text)),
        serials=_unique(serials),
        models=_unique(m for m in models if m),
        fault_codes=_unique(f"{m.group(1)}-{m.group(2)}" for m in FAULT_RE.finditer(text)),
        part_numbers=_unique(f"{m.group(1)}-{m.group(2)}" for m in PART_RE.finditer(text)),
        doc_numbers=_unique(DOCNUM_RE.findall(text)),
        case_numbers=_unique(CASE_RE.findall(text)),
        campaign_codes=_unique(CAMPAIGN_RE.findall(text)),
    )


def split_serial(serial: str) -> tuple[str, int] | None:
    """'HCP350-241187' -> ('HCP350', 241187)."""
    m = re.fullmatch(r"([A-Z]+\d+)-(\d{6})", serial.strip())
    return (m.group(1), int(m.group(2))) if m else None


def model_family(model: str) -> str:
    return model.split("-")[0]
