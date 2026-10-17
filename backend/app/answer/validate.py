"""Answer validation (spec M11).

Every field that can reach a screen, a work order or the outbox is listed in FIELD_REGISTRY and checked:
citation integrity, numbers grounded in the field's own citations, identifiers grounded, model guard,
restricted-content leak scan, superseded sources, status consistency. Fields that fail are repaired once
(by the caller), then dropped or replaced with a safe template.
"""

import re
from dataclasses import dataclass, field

from app.answer.context import ContextPack
from app.answer.schema import AnswerJSON, Block
from app.ingestion.entities import extract_entities

# path pattern -> profile; '[]' = list index. The renderer and work order builder only use these paths.
FIELD_REGISTRY: dict[str, str] = {
    "headline": "text",
    "blocks[]": "text",
    "conflicts[].topic": "topic",
    "conflicts[].positions[]": "text",
    "conflicts[].resolution": "resolution",
    "restricted_notice.what_you_can_do[]": "text_strict_leak",
    "clarify_question": "clarify",
    "follow_up.work_order_hint.fault_code": "identifier",
    "follow_up.work_order_hint.summary": "summary",
    "follow_up.work_order_hint.observations": "observation",
    "follow_up.work_order_hint.parts[]": "part",
    "follow_up.work_order_hint.procedure_refs[]": "docref",
    "follow_up.work_order_hint.work_steps[]": "text",
    "follow_up.work_order_hint.estimated_duration_h": "number",
}
# Non-text fields that are enums or computed in code (no grounding needed).
NON_FACTUAL_FIELDS = {"status", "confidence", "follow_up.needed", "follow_up.reasons[]", "conflicts[].conflict_id",
                      "conflicts[].basis"}  # basis is an enum, checked against document metadata in code

UNIT = r"(?:N[-·\s]?m|Nm|bar|mm|m3/h|m³/h|h|hours?|°C|C|K|kg|g|V\s?DC|V|A|MOhm|GOhm|%|min|minutes?|s|seconds?|ppm|months?)"
NUM_RE = re.compile(rf"(?<![\w.-])(\d{{1,3}}(?:[.,]\d{{3}})+|\d+(?:[.,]\d+)?)\s?({UNIT})?(?![\w])")
PARAM_RE = re.compile(r"\b(?:[A-Z]\d+\.\d+|TP\d|[A-Z]{1,3}\d?-\d{1,2}\b)")

INCOMPLETE_PROCEDURE = ("Part of this procedure could not be checked against your documents, so the steps are "
                        "not shown. Follow the procedure document itself (see sources) and do not work from memory.")

STATUS_TEMPLATES = {
    "answered": "See the steps below.",
    "partial": "Partly covered by your documents: see below.",
    "not_covered": "No document you can access covers this.",
    "restricted": "This work needs a certified technician: see what you can do.",
    "clarify": "Which asset do you mean?",
}


@dataclass
class FieldResult:
    path: str
    status: str = "ok"  # ok | repaired | dropped | replaced
    reasons: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    fields: dict[str, FieldResult] = field(default_factory=dict)
    leak: bool = False
    leak_tokens: list[str] = field(default_factory=list)
    procedure_withheld: bool = False

    @property
    def failures(self) -> dict[str, list[str]]:
        return {p: r.reasons for p, r in self.fields.items() if r.reasons and r.status in {"ok", "failed"}}

    def as_dict(self) -> dict:
        return {p: {"status": r.status, "reasons": r.reasons} for p, r in self.fields.items()}


# ------------------------------------------------------------------ normalisation helpers
def _norm_number(raw: str) -> str:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        return raw.replace(",", "").replace(".", "")
    return raw.replace(",", ".")


def _norm_unit(unit: str | None) -> str:
    if not unit:
        return ""
    u = unit.lower().replace("·", "-").replace(" ", "")
    return {"nm": "n-m", "n-m": "n-m", "°c": "c", "hours": "h", "hour": "h", "minutes": "min", "minute": "min",
            "seconds": "s", "second": "s", "vdc": "v", "month": "months"}.get(u, u)


def numbers_in(text: str) -> set[tuple[str, str]]:
    found = set()
    for m in NUM_RE.finditer(text):
        num, unit = _norm_number(m.group(1)), _norm_unit(m.group(2))
        if not unit and len(num.replace(".", "")) < 2:
            continue  # small counts ("two passes", step numbers) are not checked without a unit
        found.add((num, unit))
    return found


def _strip_identifiers(text: str) -> str:
    ent = extract_entities(text)
    for ident in ent.asset_ids + ent.serials + ent.part_numbers + ent.doc_numbers + ent.case_numbers + \
            ent.campaign_codes + ent.fault_codes:
        text = text.replace(ident, " ")
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)  # ISO dates are checked as identifiers
    return text


def identifiers_in(text: str) -> set[str]:
    ent = extract_entities(text)
    ids = set(ent.asset_ids + ent.serials + ent.part_numbers + ent.doc_numbers + ent.case_numbers +
              ent.campaign_codes + ent.fault_codes)
    return ids


# Explicit conversions only: (dimension, factor to the base unit). Anything else must match its unit exactly.
CONVERSIONS = {"s": ("time", 1), "min": ("time", 60), "h": ("time", 3600), "g": ("mass", 1), "kg": ("mass", 1000),
               "mohm": ("resistance", 1e6), "gohm": ("resistance", 1e9)}


def _same_quantity(a: tuple[str, str], b: tuple[str, str]) -> bool:
    (na, ua), (nb, ub) = a, b
    if ua == ub:
        return na == nb
    if ua in CONVERSIONS and ub in CONVERSIONS and CONVERSIONS[ua][0] == CONVERSIONS[ub][0]:
        va, vb = float(na) * CONVERSIONS[ua][1], float(nb) * CONVERSIONS[ub][1]
        return abs(va - vb) <= 0.005 * max(abs(va), abs(vb))
    return False


def _grounded_numbers(nums: set[tuple[str, str]], grounding: str) -> list[str]:
    """Every number in the answer must be in its cited sources. With a unit, the unit must agree: the same
    number with a different unit ("38 bar" against "38 N-m") fails even if the bare number also appears."""
    stripped = numbers_in(_strip_identifiers(grounding))
    with_unit = {(n, u) for n, u in stripped if u}
    bare_any = {n for n, _ in numbers_in(grounding)}
    bare_plain = {n for n, u in stripped if not u}
    units_in_source = {u for _, u in with_unit} | {
        _norm_unit(m) for m in re.findall(rf"(?<![\w]){UNIT}(?![\w])", grounding)}
    missing = []
    for num, unit in nums:
        if not unit:
            if num not in bare_any:
                missing.append(num)
            continue
        if any(_same_quantity((num, unit), q) for q in with_unit):
            continue
        clash = sorted(u for n, u in with_unit if n == num)
        if clash:
            missing.append(f"{num} {unit} (source gives {num} {', '.join(clash)})")
        elif num in bare_plain and unit in units_in_source:
            continue  # unit stated elsewhere in the source, e.g. a table header "Torque (N-m)"
        else:
            missing.append(f"{num} {unit}")
    return missing


# ------------------------------------------------------------------ field walking
def iter_fields(answer: AnswerJSON):
    """Yield (path, profile, text, citations) for every registry field present in the answer."""
    yield "headline", "text", answer.headline.text, answer.headline.citations
    for i, b in enumerate(answer.blocks):
        yield f"blocks[{i}]", "text", b.text, b.citations
    for i, c in enumerate(answer.conflicts):
        yield f"conflicts[{i}].topic", "topic", c.topic, [x for p in c.positions for x in p.citations]
        for j, p in enumerate(c.positions):
            yield f"conflicts[{i}].positions[{j}]", "text", p.text, p.citations
        yield f"conflicts[{i}].resolution", "resolution", c.resolution.text, c.resolution.citations
    if answer.restricted_notice:
        for i, item in enumerate(answer.restricted_notice.what_you_can_do):
            yield f"restricted_notice.what_you_can_do[{i}]", "text_strict_leak", item.text, item.citations
    if answer.clarify_question:
        yield "clarify_question", "clarify", answer.clarify_question.text, answer.clarify_question.citations
    hint = answer.follow_up.work_order_hint
    if hint:
        base = "follow_up.work_order_hint"
        if hint.fault_code:
            yield f"{base}.fault_code", "identifier", hint.fault_code.value, hint.fault_code.citations
        if hint.summary:
            yield f"{base}.summary", "summary", hint.summary.text, hint.summary.citations
        if hint.observations:
            yield f"{base}.observations", "observation", hint.observations.text, hint.observations.citations
        for i, p in enumerate(hint.parts):
            yield f"{base}.parts[{i}]", "part", p.part_no, p.citations
        for i, d in enumerate(hint.procedure_refs):
            yield f"{base}.procedure_refs[{i}]", "docref", d.doc_number, d.citations
        for i, s in enumerate(hint.work_steps):
            yield f"{base}.work_steps[{i}]", "text", s.text, s.citations
        if hint.estimated_duration_h:
            yield f"{base}.estimated_duration_h", "number", hint.estimated_duration_h.value, \
                hint.estimated_duration_h.citations


def registry_path(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


# ------------------------------------------------------------------ checks
@dataclass
class ValidationContext:
    pack: ContextPack
    question_models: list[str]
    question_identifiers: set[str]
    restricted_fingerprints: set[str]
    clarify_options: list[str]


def build_fingerprints(restricted_texts: list[str], permitted_text: str) -> set[str]:
    """Distinctive tokens from restricted chunks that do not appear in anything the user may see."""
    permitted_nums = numbers_in(permitted_text)
    permitted_lower = permitted_text.lower()
    tokens: set[str] = set()
    for t in restricted_texts:
        for num, unit in numbers_in(t):
            if unit and (num, unit) not in permitted_nums:
                tokens.add(f"{num} {unit}")
        for p in PARAM_RE.findall(t):
            if p.lower() not in permitted_lower and not re.fullmatch(r"[EPLC]-\d{2}", p):
                tokens.add(p)
    return tokens


def leak_hits(text_value: str, fingerprints: set[str]) -> list[str]:
    hits = []
    nums = {f"{n} {u}" for n, u in numbers_in(text_value) if u}
    for token in fingerprints:
        if " " in token:
            if token in nums:
                hits.append(token)
        elif re.search(rf"(?<![\w.]){re.escape(token)}(?![\w])", text_value):
            hits.append(token)
    return hits


def check_field(vctx: ValidationContext, path: str, profile: str, value: str, citations: list[str]) -> list[str]:
    pack = vctx.pack
    reasons: list[str] = []
    valid_ids = pack.ids()
    if not citations:
        reasons.append("no citation")
    bad = [c for c in citations if c not in valid_ids]
    if bad:
        reasons.append(f"unknown citation ids {bad}")
    if "Q" in citations and profile not in {"observation", "identifier", "summary"}:
        reasons.append("question cited as a source")
    grounding = "\n".join(pack.grounding(c) for c in citations if c in valid_ids)
    if profile in {"observation", "identifier", "summary"} and "Q" in citations:
        grounding += "\n" + pack.question

    # numbers
    if profile in {"text", "text_strict_leak", "resolution", "observation", "topic", "summary"}:
        missing = _grounded_numbers(numbers_in(_strip_identifiers(value)), grounding)
        if missing:
            reasons.append(f"values not in cited sources: {missing}")
    if profile == "number":
        missing = _grounded_numbers({(_norm_number(value), "")}, grounding)
        if missing:
            reasons.append(f"value not in cited sources: {missing}")

    # identifiers (anything the user said is allowed)
    ids = identifiers_in(value)
    if profile in {"part", "docref", "identifier"}:
        ids |= {value.strip()}
    ungrounded = sorted(i for i in ids if i not in grounding and i not in vctx.question_identifiers)
    if ungrounded:
        reasons.append(f"identifiers not in cited sources: {ungrounded}")

    # model guard: spec values must come from the asked model's documents
    if vctx.question_models and numbers_in(_strip_identifiers(value)) and profile != "observation":
        cited_sources = [pack.source(c) for c in citations]
        cited_models = {m for s in cited_sources if s for m in s.models}
        if cited_sources and all(s for s in cited_sources) and cited_models and \
                not (cited_models & set(vctx.question_models)):
            reasons.append(f"values cited from other models {sorted(cited_models)}")

    if profile == "clarify":
        mentioned = set(re.findall(r"\b(?:FRS|TAC|RTM|GDN)-[PCHV]-\d{4}\b", value))
        if mentioned - set(vctx.clarify_options):
            reasons.append("clarify options not from the resolver")
    return reasons


def validate_answer(answer: AnswerJSON, vctx: ValidationContext) -> ValidationReport:
    report = ValidationReport()
    for path, profile, value, citations in iter_fields(answer):
        res = FieldResult(path)
        res.reasons = check_field(vctx, path, profile, value, citations)
        hits = leak_hits(value, vctx.restricted_fingerprints)
        if hits:
            report.leak = True
            report.leak_tokens += hits
            res.reasons.append(f"restricted content: {hits}")
        report.fields[path] = res
    # status consistency
    if answer.status == "not_covered":
        for path, _, value, _ in iter_fields(answer):
            if numbers_in(_strip_identifiers(value)) and path in {"headline"}:
                report.fields[path].reasons.append("not_covered answer states a value")
    if answer.status == "restricted":
        for i, b in enumerate(answer.blocks):
            if b.type == "step":
                report.fields[f"blocks[{i}]"].reasons.append("restricted answer contains procedure steps")
    for i, c in enumerate(answer.conflicts):
        if c.basis == "curated" or c.conflict_id != "auto":
            if c.conflict_id not in {ci.id for ci in vctx.pack.conflicts}:
                report.fields[f"conflicts[{i}].resolution"].reasons.append("unknown conflict id")
        else:
            report.fields[f"conflicts[{i}].resolution"].reasons += check_conflict_basis(c, vctx.pack)
    return report


def check_conflict_basis(c, pack: ContextPack) -> list[str]:
    """A conflict the model found itself is kept only if document metadata supports the rule it names.
    These rules need no curation, so they apply to new documents as they are ingested."""
    def cited(ids):
        return [s for s in (pack.source(x) for x in ids if x.startswith("S")) if s]

    sides = [cited(p.citations) for p in c.positions]
    if len(sides) < 2 or any(not s for s in sides):
        return ["a conflict needs two positions, each citing a source"]
    if len({frozenset(x.doc_id for x in s) for s in sides}) < 2:
        return ["both positions cite the same document"]
    winners = cited(c.resolution.citations)
    win_docs = {w.doc_id for w in winners}
    losers = [x for s in sides for x in s if x.doc_id not in win_docs]
    if c.basis == "unresolved":
        return ["an unresolved conflict must not pick a value"] if numbers_in(_strip_identifiers(c.resolution.text)) else []
    if not winners or not losers:
        return ["the resolution must cite the winning side"]
    if c.basis == "newer_revision":
        ok = any(w.doc_status == "current" for w in winners) and any(
            x.doc_status in {"superseded", "stale"} for x in losers)
    elif c.basis == "applicable_bulletin":
        checked = {f.label.split(" · ")[0] for f in pack.facts if "serial check" in f.label}
        ok = any(w.doc_type == "oem_bulletin" and w.doc_number in checked for w in winners)
    elif c.basis == "higher_authority":
        ok = min(w.authority_rank for w in winners) < min(x.authority_rank for x in losers)
    else:
        ok = False
    return [] if ok else [f"basis '{c.basis}' is not supported by the cited documents' metadata"]


# ------------------------------------------------------------------ apply failures after repair
def apply_failures(answer: AnswerJSON, report: ValidationReport, vctx: ValidationContext) -> AnswerJSON:
    """Drop or replace every field that still fails. Mutates and returns the answer."""
    failing = {p for p, r in report.fields.items() if r.reasons}
    for p in failing:
        report.fields[p].status = "dropped"

    # A procedure with a step or safety warning removed can be dangerous (drop "isolate first" and what's left
    # is "press RESET"). So if any step or warning fails, withhold every step and say the answer is incomplete.
    # only an answer that offers a procedure can have one withheld; elsewhere failing blocks are just dropped
    has_procedure = answer.status in {"answered", "partial"} and any(b.type == "step" for b in answer.blocks)
    procedure_broken = has_procedure and any(f"blocks[{i}]" in failing for i, b in enumerate(answer.blocks)
                                             if b.type in {"step", "warning"})
    if procedure_broken:
        step_sources = sorted({c for b in answer.blocks if b.type == "step" for c in b.citations
                               if c in vctx.pack.ids() and c != "Q"})
        answer.blocks = [b for i, b in enumerate(answer.blocks)
                         if f"blocks[{i}]" not in failing and b.type != "step"]
        answer.blocks.insert(0, Block(type="warning", text=INCOMPLETE_PROCEDURE, citations=step_sources))
        if answer.status == "answered":
            answer.status = "partial"
        report.procedure_withheld = True
    else:
        answer.blocks = [b for i, b in enumerate(answer.blocks) if f"blocks[{i}]" not in failing]

    if "headline" in failing:
        answer.headline.text = STATUS_TEMPLATES[answer.status]
        answer.headline.citations = []
        report.fields["headline"].status = "replaced"

    curated = {c.id: c for c in vctx.pack.conflicts}
    kept = []
    for i, c in enumerate(answer.conflicts):
        if f"conflicts[{i}].topic" in failing or any(p.startswith(f"conflicts[{i}].positions") for p in failing):
            continue
        if c.conflict_id not in curated:  # found by the model: kept only if every field, basis included, passed
            if f"conflicts[{i}].resolution" in failing:
                continue
            kept.append(c)
            continue
        if f"conflicts[{i}].resolution" in failing:
            ci = curated[c.conflict_id]
            c.resolution.text = ci.resolution
            c.resolution.citations = [ci.resolution_source] if ci.resolution_source else []
            report.fields[f"conflicts[{i}].resolution"].status = "replaced"
        kept.append(c)
    answer.conflicts = kept

    if answer.restricted_notice:
        answer.restricted_notice.what_you_can_do = [
            item for i, item in enumerate(answer.restricted_notice.what_you_can_do)
            if f"restricted_notice.what_you_can_do[{i}]" not in failing
        ]

    if answer.clarify_question and "clarify_question" in failing:
        options = " or ".join(vctx.clarify_options) if vctx.clarify_options else "the asset ID"
        answer.clarify_question.text = f"Which asset do you mean: {options}?"
        answer.clarify_question.citations = []
        report.fields["clarify_question"].status = "replaced"

    hint = answer.follow_up.work_order_hint
    if hint:
        base = "follow_up.work_order_hint"
        if f"{base}.fault_code" in failing:
            hint.fault_code = None
        if f"{base}.summary" in failing:
            hint.summary = None
        if f"{base}.observations" in failing:
            hint.observations = None
        if f"{base}.estimated_duration_h" in failing:
            hint.estimated_duration_h = None
        hint.parts = [p for i, p in enumerate(hint.parts) if f"{base}.parts[{i}]" not in failing]
        hint.procedure_refs = [d for i, d in enumerate(hint.procedure_refs) if f"{base}.procedure_refs[{i}]" not in failing]
        # same rule for work-order steps: a partial step list is not offered as the work to do
        if any(p.startswith(f"{base}.work_steps") for p in failing):
            hint.work_steps = []

    if failing and answer.confidence == "high":
        answer.confidence = "medium"
    return answer


def mark_superseded(answer: AnswerJSON, pack: ContextPack) -> None:
    """A block citing only superseded/stale sources must say so."""
    for b in answer.blocks:
        cited = [pack.source(c) for c in b.citations if c.startswith("S")]
        if cited and all(s and s.doc_status in {"superseded", "stale", "unofficial"} for s in cited):
            if not re.search(r"out of date|superseded|outdated|older|no longer|stale", b.text, re.I):
                b.text += " (out of date: this source has been superseded)"


def procedure_failing(answer: AnswerJSON, report: ValidationReport) -> bool:
    """A step or safety warning failed in an answer that offers a procedure: worth one repair before withholding."""
    return answer.status in {"answered", "partial"} and any(b.type == "step" for b in answer.blocks) and any(
        report.fields.get(f"blocks[{i}]") and report.fields[f"blocks[{i}]"].reasons
        for i, b in enumerate(answer.blocks) if b.type in {"step", "warning"})
