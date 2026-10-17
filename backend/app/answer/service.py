"""The ask pipeline (spec §4.1): analysis -> structured lookups -> hybrid search -> rerank -> restricted probe
-> context -> generation -> validation/repair -> computed fields -> persistence.
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

from app.answer.context import ContextPack, build_context, render_context
from app.answer.schema import AnswerDraft, AnswerJSON, QueryPlan
from app.answer.validate import (
    ValidationContext,
    apply_failures,
    build_fingerprints,
    mark_superseded,
    procedure_failing,
    validate_answer,
)
from app.auth.acl import acl_clause
from app.auth.users import CERT_LABELS, UserContext
from app.config import get_settings
from app.conversations import add_message, audit, create_conversation, get_conversation
from app.ingestion.entities import extract_entities, normalize_model
from app.llm.openai_client import Usage, get_llm
from app.retrieval.search import asset_notes, hybrid_search, llm_rerank, restricted_probe
from app.retrieval.structured import applicable_bulletins, asset_history, resolve_assets

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

ANALYSIS_SYSTEM = """Analyse a field service technician's question for retrieval. Equipment models in this fleet:
HCP-200, HCP-350, HCP-500 (Halden pumps, P-codes), VRS-45, VRS-75, VRS-110 (Vastra compressors, E-codes),
NK-300, NK-600 (Nordkyl chillers, L-codes), BX-12, BX-20 (Belton conveyors, C-codes). Write two short search
rewrites that use the manual's vocabulary (e.g. 'casing bolt tightening torque', 'oil change interval')."""

GROUP_WORDS = {
    "region-na": "works North America", "region-eu": "works Europe",
    "family-pumps": "trained on Halden pumps", "family-compressors": "trained on Vastra compressors",
    "family-chillers": "trained on Nordkyl chillers", "family-conveyors": "trained on Belton conveyors",
    "role-lead": "team lead",
}


class AskError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class AskResult:
    response: dict
    message_id: str
    conversation_id: str


def _user_block(user: UserContext) -> str:
    certs = {label: (g in user.groups) for g, label in CERT_LABELS.items()}
    words = [GROUP_WORDS[g] for g in sorted(user.groups) if g in GROUP_WORDS]
    cert_text = "; ".join(f"{k}-certified: {'yes' if v else 'NO'}" for k, v in certs.items())
    return f"USER: {user.name} ({user.title}); {', '.join(words)}. Certifications: {cert_text}."


def _plan(question: str, usage: Usage) -> QueryPlan:
    return get_llm().structured("fast", ANALYSIS_SYSTEM, question, QueryPlan, usage=usage)


STAGE_MESSAGES = {
    "analysis": "Understanding the question…",
    "structured": "Checking the asset, bulletins and history…",
    "search": "Searching your documents…",
    "rerank": "Ranking the best sources…",
    "context": "Reading the sources…",
    "generate": "Writing the answer…",
    "validate": "Checking every value against its source…",
}


@dataclass
class Prepared:
    """Everything the answer model is given (user_message is its entire input besides SYSTEM_PROMPT)."""
    user_message: str
    vctx: ValidationContext
    pack: ContextPack
    assets: list
    bulletins: dict
    history: dict
    hints: list
    ambiguous: list


def prepare(conn, user: UserContext, question: str, usage: Usage, lap=lambda name: None) -> Prepared:
    """Steps 1-5: analysis, structured lookups, search, restricted probe and context. No answer-model call,
    so tests can inspect exactly what the model would see."""
    settings = get_settings()
    # 1. analysis
    ent = extract_entities(question)
    plan = _plan(question, usage)
    lap("analysis")
    resolution = resolve_assets(conn, question, ent)
    assets = resolution.assets
    models = set(ent.models) | {a.model for a in assets}
    for m in plan.equipment_models:
        mm = re.match(r"([A-Za-z]+)-?(\d+)", m)
        if mm and normalize_model(mm.group(1), mm.group(2)):
            models.add(normalize_model(mm.group(1), mm.group(2)))
    models = sorted(models)
    codes = sorted(set(ent.fault_codes) | {c for c in plan.fault_codes if re.fullmatch(r"[EPLC]-\d{2}", c)})

    # 2. structured lookups
    bulletins = {a.asset_id: applicable_bulletins(conn, a, user) for a in assets}
    history = {}
    if plan.needs_history or codes or plan.intent in {"troubleshoot", "history"}:
        history = {a.asset_id: asset_history(conn, a.asset_id, user, codes[0] if codes else None)
                   for a in assets}
    lap("structured")

    # 3. hybrid search
    queries = [question] + [r for r in plan.rewrites[:2] if r.strip()]
    vectors = get_llm().embed(queries)
    boost = {b.document_id for bs in bulletins.values() for b in bs if b.visible}
    candidates = hybrid_search(conn, user, queries, vectors, models, [a.asset_id for a in assets], codes,
                               ent.part_numbers, ent.doc_numbers, boost, top_k=settings.top_k)
    lap("search")
    if settings.reranker == "llm" and candidates:
        candidates = llm_rerank(question, candidates, usage=usage)
    lap("rerank")
    coverage = max((c.rerank or 0) for c in candidates) if candidates else 0

    # 4. restricted probe (metadata only)
    hidden_bulletin_docs = {b.document_id for bs in bulletins.values() for b in bs if not b.visible}
    hints, restricted_chunk_ids = restricted_probe(conn, user, vectors[0], codes, models, hidden_bulletin_docs)
    for h in hints:
        audit(conn, user.id, "restricted_hint", "document", h.document_id, h.disclosure,
              {"title": h.title, "missing": h.missing_groups})

    # 5. context
    pack = build_context(conn, user, question, candidates, assets, bulletins, history, models,
                         settings.context_k,
                         expand_procedures=plan.needs_procedure or plan.intent in {"procedure", "troubleshoot"},
                         asset_note_chunks=asset_notes(conn, user, [a.asset_id for a in assets], vectors[0]))
    lap("context")

    # restricted texts stay on the server: used only to build the leak fingerprint set
    restricted_texts = [r[0] for r in conn.execute(
        text("SELECT text FROM chunks WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": restricted_chunk_ids})]
    permitted_text = "\n".join([question] + [pack.grounding(i) for i in pack.ids()])
    fingerprints = build_fingerprints(restricted_texts, permitted_text)

    hint_lines = [f"- {h.title}" + (f" ({h.doc_number})" if h.doc_number else "")
                  + f": requires {', '.join(CERT_LABELS.get(g, g) for g in h.missing_groups)}"
                  for h in hints if h.disclosure == "certification"]
    hint_lines += [f"- documents for equipment the user is not trained on ({', '.join(h.missing_groups)})"
                   for h in hints if h.disclosure == "family"]
    ambiguous = [[a.asset_id for a in group] for group in resolution.ambiguous]
    coverage_note = "high" if coverage >= 2 else ("medium" if coverage >= 1 else "low")
    if models and not any(set(s.models) & set(models) for s in pack.sources if s.models):
        coverage_note += f" (no source covers the asked model(s) {', '.join(models)})"

    user_message = "\n".join([
        _user_block(user),
        f"QUESTION: {question}",
        f"COVERAGE: {coverage_note}",
        "AMBIGUOUS ASSETS: " + (json.dumps(ambiguous) if ambiguous else "none"),
        "RESTRICTED HINTS (procedures this user may not see; never describe their content):",
        "\n".join(hint_lines) or "- none",
        "",
        render_context(pack),
    ])

    vctx = ValidationContext(
        pack=pack, question_models=models,
        question_identifiers={*ent.asset_ids, *ent.fault_codes, *ent.part_numbers, *ent.serials,
                              *ent.doc_numbers, *ent.case_numbers} | {a.asset_id for a in assets},
        restricted_fingerprints=fingerprints,
        clarify_options=sorted({a for g in ambiguous for a in g}),
    )
    return Prepared(user_message, vctx, pack, assets, bulletins, history, hints, ambiguous)


def ask(engine: Engine, user: UserContext, question: str, conversation_id: str | None = None,
        persist: bool = True, progress=None) -> AskResult:
    settings = get_settings()
    usage = Usage()
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    def lap(name: str) -> None:
        timings[name] = round(time.perf_counter() - t0, 2)
        if progress:
            stages = list(STAGE_MESSAGES)
            nxt = stages[stages.index(name) + 1] if name in stages and stages.index(name) + 1 < len(stages) else None
            if nxt:
                progress(nxt, STAGE_MESSAGES[nxt])

    if progress:
        progress("analysis", STAGE_MESSAGES["analysis"])

    with engine.begin() as conn:
        if conversation_id and get_conversation(conn, conversation_id, user) is None:
            audit(conn, user.id, "ownership_denied", "conversation", conversation_id, "404")
            raise AskError(404, "conversation not found")

        prep = prepare(conn, user, question, usage, lap)
        user_message, vctx, pack = prep.user_message, prep.vctx, prep.pack
        assets, bulletins, history, hints, ambiguous = prep.assets, prep.bulletins, prep.history, prep.hints, prep.ambiguous

        # 6. generate, validate, repair
        llm = get_llm()
        answer = llm.structured("answer", SYSTEM_PROMPT, user_message, AnswerDraft, usage=usage).full()
        lap("generate")
        report = validate_answer(answer, vctx)
        if report.leak:
            audit(conn, user.id, "leak_blocked", "message", None, "regenerate", {"tokens": report.leak_tokens})
            answer = llm.structured(
                "answer", SYSTEM_PROMPT,
                user_message + "\n\nIMPORTANT: your previous answer contained details of a restricted procedure. "
                "Do not include any steps, values, parameters or test points for restricted work.",
                AnswerDraft, effort="high", usage=usage).full()
            report = validate_answer(answer, vctx)
        # A full repair call costs as much as the answer; only worth it when the headline or several fields
        # fail. Otherwise the failing fields are dropped below.
        if report.failures and ("headline" in report.failures or len(report.failures) > 2
                                or procedure_failing(answer, report)):
            failing = "\n".join(f"- {p}: {'; '.join(r)}" for p, r in report.failures.items())
            answer = llm.structured(
                "answer", SYSTEM_PROMPT,
                user_message + "\n\nYour previous answer had these problems; fix them by citing the source that "
                f"contains each value or removing the unsupported value:\n{failing}\n\nPrevious answer:\n"
                + answer.model_dump_json(),
                AnswerDraft, effort="high", usage=usage).full()
            report = validate_answer(answer, vctx)
            for r in report.fields.values():
                if not r.reasons:
                    r.status = "ok"
            repaired = True
        else:
            repaired = False
        if report.leak:
            answer = _safe_restricted_answer(answer)
            report = validate_answer(answer, vctx)
        answer = apply_failures(answer, report, vctx)
        mark_superseded(answer, pack)
        lap("validate")

        # 7. computed fields + response
        response = _response(answer, report, pack, user, assets, bulletins, history, hints, ambiguous, repaired)
        response["timings"] = timings
        response["usage"] = usage.calls
        if not persist:
            return AskResult(response, "", conversation_id or "")
        conv_id = conversation_id or create_conversation(conn, user)
        message_id = add_message(
            conn, conv_id, user, question, response, pack.chunk_ids(),
            [{"title": h.title, "missing": h.missing_groups, "disclosure": h.disclosure} for h in hints],
            timings, settings.answer_model, usage.calls)
        response["message_id"] = message_id
        response["conversation_id"] = conv_id
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :id"),
                     {"a": json.dumps(response, default=str), "id": message_id})
        return AskResult(response, message_id, conv_id)


def _safe_restricted_answer(answer: AnswerJSON) -> AnswerJSON:
    answer.status = "restricted"
    answer.blocks = [b for b in answer.blocks if b.type != "step"]
    if answer.restricted_notice:
        answer.restricted_notice.what_you_can_do = []
    answer.follow_up.work_order_hint = None
    return answer


def _response(answer: AnswerJSON, report, pack: ContextPack, user: UserContext, assets, bulletins, history,
              hints, ambiguous, repaired: bool) -> dict:
    cited = {c for _, _, _, cits in _all_citations(answer) for c in cits}
    required_certs = sorted({CERT_LABELS[g] for h in hints if h.disclosure == "certification"
                             for g in h.missing_groups if g in CERT_LABELS})
    follow_up = answer.follow_up.model_dump()
    reasons = set(follow_up["reasons"])
    if required_certs and answer.status == "restricted":
        reasons.add("certified technician required")
    if any(b.campaign_code for bs in bulletins.values() for b in bs if b.visible):
        reasons.add("OEM campaign")
    follow_up["reasons"] = sorted(reasons)
    follow_up["needed"] = bool(follow_up["needed"] or reasons)
    return {
        "answer": {**answer.model_dump(), "follow_up": follow_up},
        "validation": report.as_dict(),
        "repaired": repaired,
        "procedure_withheld": report.procedure_withheld,
        "restricted": {"required_certifications": required_certs,
                       "procedures": [{"title": h.title, "doc_number": h.doc_number} for h in hints
                                      if h.disclosure == "certification"]} if hints else None,
        "clarify_options": sorted({a for g in ambiguous for a in g}),
        "assets": [{"asset_id": a.asset_id, "model": a.model, "serial": a.serial, "site": a.site_name,
                    "region": a.region, "firmware": a.firmware, "oil_type": a.oil_type} for a in assets],
        "bulletins": [{"asset_id": aid, "number": b.number, "effect": b.effect, "campaign_code": b.campaign_code}
                      for aid, bs in bulletins.items() for b in bs if b.visible],
        "history": {aid: {"summary": h.summary,
                          "events": [{"date": str(e.event_date), "source": e.source, "ref": e.source_ref,
                                      "fault_code": e.fault_code, "notes": (e.notes or "")[:300]}
                                     for e in h.events[:10]]}
                    for aid, h in history.items()},
        "sources": [{"id": s.id, "chunk_id": s.chunk_id, "label": s.label, "doc_id": s.doc_id,
                     "doc_type": s.doc_type, "doc_status": s.doc_status, "locator": s.locator,
                     "cited": s.id in cited, "text": s.text} for s in pack.sources],
        "facts": [{"id": f.id, "label": f.label, "text": f.text, "cited": f.id in cited, "chunk_ids": f.chunk_ids}
                  for f in pack.facts],
        "context_conflicts": [c.id for c in pack.conflicts],
        "user": {"id": user.id, "name": user.name, "groups": sorted(user.groups)},
    }


def _all_citations(answer: AnswerJSON):
    from app.answer.validate import iter_fields

    return list(iter_fields(answer))


def visible_chunk(conn, user: UserContext, chunk_id: str) -> dict | None:
    from app.auth.acl import acl_params

    row = conn.execute(
        text(f"""SELECT c.id, c.text, c.locator, c.flags, c.doc_status, d.id AS doc_id, d.title, d.doc_type,
                        d.doc_number, d.revision, d.effective_date, d.path
                 FROM chunks c JOIN documents d ON d.id = c.document_id
                 WHERE c.id = CAST(:id AS uuid) AND {acl_clause('c')}"""),
        {"id": chunk_id, **acl_params(user)},
    ).mappings().first()
    return dict(row) if row else None
