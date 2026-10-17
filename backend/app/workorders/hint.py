"""Work-order hint, generated when the technician asks for a work order instead of with every answer.

The hint gets the same field validation it got inside the answer: the answer's sources are re-fetched through
the ACL filter (so access is re-checked at this moment), every hint field must cite them, and numbers, units
and identifiers must appear in what it cites. Failing fields are dropped; a failing step clears all steps.
"""

from sqlalchemy import Connection

from app.answer.context import ContextPack, Fact, _source_from_row, fetch_chunks, render_context
from app.answer.schema import AnswerJSON, WorkOrderHint
from app.answer.validate import ValidationContext, apply_failures, validate_answer
from app.auth.users import UserContext
from app.ingestion.entities import extract_entities
from app.llm.openai_client import get_llm

HINT_SYSTEM = """You draft a work order from a field technician's question and the answer they were given.
Use ONLY the SOURCES (S-ids) and FACTS (F-ids) provided; cite them on every field, and cite the question as "Q"
only for observations the technician reported. Fill: the fault code; a one-line summary; observations from the
question; parts with part numbers and quantities exactly as the sources give them; procedure document numbers;
and work steps that refer to procedure numbers. Never copy procedure steps, values or test points into the
work order. Use null or an empty list for anything the sources don't support."""


def generate_hint(conn: Connection, user: UserContext, question: str, response: dict) -> dict | None:
    """Returns the validated hint as a dict, or None if nothing could be drafted."""
    stored = response.get("sources", [])
    rows = fetch_chunks(conn, user, [s["chunk_id"] for s in stored])  # ACL re-checked here
    sources = [_source_from_row(s["id"], rows[s["chunk_id"]]) for s in stored if s["chunk_id"] in rows]
    facts = [Fact(f["id"], f["label"], f["text"], f.get("chunk_ids", [])) for f in response.get("facts", [])]
    pack = ContextPack(sources, facts, [], question)

    a = response["answer"]
    answer_text = "\n".join([f"HEADLINE: {a['headline']['text']}"] + [
        f"- [{b['type']}] {b['text']} {' '.join(b['citations'])}" for b in a["blocks"]])
    hint = get_llm().structured(
        "fast", HINT_SYSTEM,
        f"QUESTION: {question}\n\nANSWER GIVEN:\n{answer_text}\n\n{render_context(pack)}", WorkOrderHint)

    # Part numbers are compared verbatim, so strip descriptions the model appends ("63-1180 rev 3" -> "63-1180"),
    # as the work-order builder does.
    from app.workorders.service import _part_no

    for p in hint.parts:
        p.part_no = _part_no(p.part_no)

    # A part or document number cited to the wrong source is re-pointed to a permitted source that contains it
    # (deterministic; the number must still exist in what the user may see). Everything else is validated as is.
    def holders(token: str) -> list[str]:
        return [s.id for s in pack.sources if token and token in s.text]

    for item, token in [(p, p.part_no) for p in hint.parts] + [(d, d.doc_number) for d in hint.procedure_refs]:
        if not any(token in s.text for s in pack.sources if s.id in item.citations) and holders(token):
            item.citations = holders(token)[:3]

    # validate exactly as the answer's hint fields were validated before
    ent = extract_entities(question)
    vctx = ValidationContext(
        pack=pack,
        question_models=sorted(set(ent.models) | {x["model"] for x in response.get("assets", [])}),
        question_identifiers={*ent.asset_ids, *ent.fault_codes, *ent.part_numbers, *ent.serials,
                              *ent.doc_numbers, *ent.case_numbers} | {x["asset_id"] for x in response.get("assets", [])},
        restricted_fingerprints=set(),  # the work order has its own restricted-content check at validation
        clarify_options=[],
    )
    shell = AnswerJSON.model_validate({**a, "blocks": [], "conflicts": [], "restricted_notice": None,
                                       "clarify_question": None,
                                       "follow_up": {**a["follow_up"], "work_order_hint": hint.model_dump()}})
    report = validate_answer(shell, vctx)
    for path, r in report.fields.items():
        if not path.startswith("follow_up.work_order_hint"):
            r.reasons = []  # only the hint is being checked here
    checked = apply_failures(shell, report, vctx).follow_up.work_order_hint
    return checked.model_dump() if checked else None
