"""Field-registry validation (spec M11) on crafted answers, and work order validation (M12). No LLM calls."""

import typing

import pytest
from pydantic import BaseModel

from app.answer.context import ConflictInfo, ContextPack, Fact, Source
from app.answer.schema import AnswerJSON
from app.answer.validate import (
    FIELD_REGISTRY,
    NON_FACTUAL_FIELDS,
    ValidationContext,
    apply_failures,
    build_fingerprints,
    validate_answer,
)
from app.auth.users import get_user


def src(sid, text, models, status="current"):
    return Source(sid, f"chunk-{sid}", f"label {sid}", "doc", "title", "oem_manual", None, status, 2, text, {},
                  models, [])


PACK = ContextPack(
    sources=[
        src("S1", "SB-2025-009: casing bolts 38 N-m, cross pattern, two passes, from HCP350-240001.", ["HCP-350"]),
        src("S2", "HDN-IOM-350 Rev D: casing bolt torque 45 N-m.", ["HCP-350"]),
        src("S3", "Belton warrants the BX-12 conveyor for 12 months from commissioning.", ["BX-12"]),
        src("S4", "SOP-020: never top up to clear a low suction pressure alarm.", ["NK-600"]),
        src("S5", "E-31 is VFD overcurrent; it cannot be reset from the control panel.", ["VRS-110"]),
    ],
    facts=[Fact("F1", "Fleet register", "Asset FRS-P-0031: HCP-350, serial HCP350-241187.")],
    conflicts=[ConflictInfo("C5", "Refrigerant top-up", ["KT-WI-14 allows", "SOP-020 forbids"],
                            "The stricter rule applies: no top-up without a leak test.", "S4")],
    question="What torque for FRS-P-0031?",
)


def answer(**kw) -> AnswerJSON:
    base = {"status": "answered", "headline": {"text": "Use 38 N-m.", "citations": ["S1"]}, "blocks": [],
            "conflicts": [], "restricted_notice": None, "clarify_question": None,
            "follow_up": {"needed": False, "reasons": [], "work_order_hint": None}, "confidence": "high"}
    base.update(kw)
    return AnswerJSON.model_validate(base)


def vctx(models=("HCP-350",), fingerprints=frozenset(), options=()):
    return ValidationContext(PACK, list(models), {"FRS-P-0031"}, set(fingerprints), list(options))


def failing(a, ctx=None):
    return {p: r.reasons for p, r in validate_answer(a, ctx or vctx()).fields.items() if r.reasons}


def test_good_answer_passes():
    a = answer(blocks=[{"type": "step", "text": "Tighten to 38 N-m in a cross pattern, two passes.",
                        "citations": ["S1"]}])
    assert failing(a) == {}


def test_uncited_number_in_headline_is_replaced():
    a = answer(headline={"text": "Torque is 45 N-m for FRS-P-0031.", "citations": ["S1"]})
    report = validate_answer(a, vctx())
    assert "headline" in {p for p, r in report.fields.items() if r.reasons}
    fixed = apply_failures(a, report, vctx())
    assert "45" not in fixed.headline.text and report.fields["headline"].status == "replaced"


def test_number_with_wrong_unit_fails_even_if_the_bare_number_appears():
    # S1 says 38 N-m; "38 bar" must not pass just because 38 is in the source
    a = answer(headline={"text": "Set the pressure to 38 bar.", "citations": ["S1"]})
    assert "headline" in failing(a)
    assert any("source gives 38 n-m" in r for r in failing(a)["headline"])


def test_units_explicit_conversions_and_table_headers():
    from app.answer.validate import _grounded_numbers, numbers_in

    def missing(answer_text, source):
        return _grounded_numbers(numbers_in(answer_text), source)

    assert missing("insulation 2400 MOhm", "insulation resistance 2.4 GOhm") == []
    assert missing("hold for 2 min", "hold the circuit for 120 s") == []
    assert missing("torque 38 N-m", "Bolt | Torque (N-m)\nCasing | 38") == []   # unit in the header
    assert missing("38 bar", "Bolt | Torque (N-m)\nCasing | 38") != []          # header unit is N-m
    assert missing("38 bar", "Item 38 on the parts list") != []                  # no bar anywhere
    assert missing("4 bar", "below 4 C for 60 s") != []                          # unit clash on a bare match


def test_failed_step_withholds_the_whole_procedure():
    # reviewer's reproduction: the isolation step is unsupported; "Press RESET" alone must not be shown
    a = answer(blocks=[
        {"type": "step", "text": "Isolate the drive and wait 15 min for the DC bus to discharge.", "citations": ["S5"]},
        {"type": "step", "text": "Press RESET.", "citations": ["S5"]},
    ])
    report = validate_answer(a, vctx())
    assert "blocks[0]" in {p for p, r in report.fields.items() if r.reasons}
    fixed = apply_failures(a, report, vctx())
    assert [b for b in fixed.blocks if b.type == "step"] == []
    assert fixed.status == "partial" and report.procedure_withheld
    assert "could not be checked" in fixed.blocks[0].text and fixed.blocks[0].citations == ["S5"]


def test_failed_note_does_not_withhold_valid_steps():
    a = answer(blocks=[
        {"type": "step", "text": "Tighten to 38 N-m in a cross pattern, two passes.", "citations": ["S1"]},
        {"type": "note", "text": "Re-check after 99 h.", "citations": ["S1"]},
    ])
    report = validate_answer(a, vctx())
    fixed = apply_failures(a, report, vctx())
    assert [b.type for b in fixed.blocks] == ["step"] and fixed.status == "answered"


def test_failed_warning_without_steps_does_not_claim_a_procedure():
    a = answer(status="not_covered", headline={"text": "No document covers this.", "citations": ["S3"]},
               blocks=[{"type": "warning", "text": "Never exceed 99 bar.", "citations": ["S3"]}])
    report = validate_answer(a, vctx())
    fixed = apply_failures(a, report, vctx())
    assert fixed.blocks == [] and not report.procedure_withheld and fixed.status == "not_covered"


def test_model_found_conflicts_are_kept_only_when_metadata_supports_the_rule():
    from app.answer.context import Source

    def s(sid, doc, status, dtype, rank, text):
        return Source(sid, f"c-{sid}", sid, doc, doc, dtype, None, status, rank, text, {}, ["VRS-75"], [])

    pack = ContextPack(sources=[s("S1", "revB", "superseded", "oem_manual", 2, "Change oil every 4,000 h."),
                                s("S2", "revC", "current", "oem_manual", 2, "Change oil every 6,000 h."),
                                s("S3", "wiki", "current", "wiki", 5, "Change oil every 5,000 h.")],
                       facts=[], conflicts=[], question="VRS-75 oil interval?")
    ctx = ValidationContext(pack, ["VRS-75"], set(), set(), [])

    def conflict(basis, winner):
        return answer(headline={"text": "Every 6,000 h.", "citations": ["S2"]}, conflicts=[{
            "conflict_id": "auto", "basis": basis, "topic": "Oil interval",
            "positions": [{"text": "Rev B: 4,000 h.", "citations": ["S1"]}, {"text": "Rev C: 6,000 h.", "citations": ["S2"]}],
            "resolution": {"text": f"Use {'6,000' if winner == 'S2' else '4,000'} h.", "citations": [winner]}}])

    ok = conflict("newer_revision", "S2")
    assert apply_failures(ok, validate_answer(ok, ctx), ctx).conflicts  # current beats superseded
    wrong = conflict("newer_revision", "S1")  # claims the superseded revision wins
    assert apply_failures(wrong, validate_answer(wrong, ctx), ctx).conflicts == []
    unsupported = conflict("applicable_bulletin", "S2")  # no bulletin, no serial check
    assert apply_failures(unsupported, validate_answer(unsupported, ctx), ctx).conflicts == []


def test_stray_step_in_a_not_covered_answer_is_dropped_without_a_procedure_notice():
    a = answer(status="not_covered", headline={"text": "No document covers this.", "citations": ["S3"]},
               blocks=[{"type": "step", "text": "Retension to 99 N-m.", "citations": ["S3"]}])
    report = validate_answer(a, vctx())
    fixed = apply_failures(a, report, vctx())
    assert fixed.blocks == [] and not report.procedure_withheld


def test_failing_step_triggers_a_repair_first():
    from app.answer.validate import procedure_failing
    a = answer(blocks=[{"type": "step", "text": "Tighten to 99 N-m.", "citations": ["S1"]}])
    assert procedure_failing(a, validate_answer(a, vctx()))
    ok = answer(blocks=[{"type": "step", "text": "Tighten to 38 N-m.", "citations": ["S1"]}])
    assert not procedure_failing(ok, validate_answer(ok, vctx()))


def test_cross_model_value_in_headline():
    a = answer(headline={"text": "The BX-20 warranty is 12 months.", "citations": ["S3"]})
    assert "headline" in failing(a, vctx(models=("BX-20",)))


def test_conflict_resolution_contradicting_register_is_replaced():
    a = answer(conflicts=[{"conflict_id": "C5", "basis": "curated", "topic": "Refrigerant top-up",
                           "positions": [{"text": "SOP-020 forbids topping up.", "citations": ["S4"]}],
                           "resolution": {"text": "KT-WI-14 applies: top up 2,5 kg.", "citations": ["S4"]}}])
    report = validate_answer(a, vctx(models=("NK-600",)))
    fixed = apply_failures(a, report, vctx(models=("NK-600",)))
    assert fixed.conflicts[0].resolution.text == PACK.conflicts[0].resolution


def test_unknown_conflict_id_is_dropped():
    a = answer(conflicts=[{"conflict_id": "C99", "basis": "curated", "topic": "x", "positions": [{"text": "y", "citations": ["S1"]}],
                           "resolution": {"text": "z", "citations": ["S1"]}}])
    assert apply_failures(a, validate_answer(a, vctx()), vctx()).conflicts == []


def test_restricted_token_in_what_you_can_do_is_a_leak():
    fp = build_fingerprints(["Set P9.01 = 1 and check TP1 below 50 V DC."], PACK.question)
    a = answer(status="restricted", headline={"text": "Needs HV.", "citations": ["S5"]},
               restricted_notice={"what_you_can_do": [{"text": "Set P9.01 = 1 on the keypad.", "citations": ["S5"]}]})
    report = validate_answer(a, vctx(models=("VRS-110",), fingerprints=fp))
    assert report.leak and "P9.01" in report.leak_tokens


def test_invented_part_in_work_order_hint_is_dropped():
    hint = {"fault_code": None, "summary": None, "observations": None, "procedure_refs": [], "work_steps": [],
            "estimated_duration_h": None, "parts": [{"part_no": "63-1190", "qty": 2, "citations": ["S1"]}]}
    a = answer(follow_up={"needed": True, "reasons": ["parts required"], "work_order_hint": hint})
    report = validate_answer(a, vctx())
    fixed = apply_failures(a, report, vctx())
    assert fixed.follow_up.work_order_hint.parts == []


def test_case_number_not_in_sources_is_flagged():
    a = answer(blocks=[{"type": "history", "text": "See case 00049999.", "citations": ["S1"]}])
    assert "blocks[0]" in failing(a)


def test_unknown_citation_and_uncited_field():
    a = answer(blocks=[{"type": "fact", "text": "Cross pattern.", "citations": ["S42"]},
                       {"type": "fact", "text": "Two passes.", "citations": []}])
    f = failing(a)
    assert "blocks[0]" in f and "blocks[1]" in f


def test_clarify_options_must_come_from_resolver():
    a = answer(status="clarify", clarify_question={"text": "FRS-P-0012 or FRS-P-0099?", "citations": ["F1"]})
    assert "clarify_question" in failing(a, vctx(options=("FRS-P-0012", "FRS-P-0031")))


def _leaf_paths(model: type[BaseModel], prefix: str = "") -> list[str]:
    paths = []
    for name, field in model.model_fields.items():
        if name == "citations":
            continue
        inner = field.annotation
        if typing.get_origin(inner) in (typing.Union, __import__("types").UnionType):
            inner = next(a for a in typing.get_args(inner) if a is not type(None))
        is_list = typing.get_origin(inner) is list
        if is_list:
            inner = typing.get_args(inner)[0]
        path = f"{prefix}{name}" + ("[]" if is_list else "")
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            paths += _leaf_paths(inner, path + ".")
        else:
            paths.append(path)
    return paths


def test_every_displayable_field_is_in_the_registry():
    """A new string/number field in AnswerJSON must be added to FIELD_REGISTRY (or declared non-factual)."""
    covered = set(FIELD_REGISTRY) | NON_FACTUAL_FIELDS | {"blocks[].type", "follow_up.work_order_hint.parts[].qty"}
    for path in _leaf_paths(AnswerJSON):
        owner = path.rsplit(".", 1)[0] if path.endswith((".text", ".value", ".part_no", ".doc_number")) else path
        assert path in covered or owner in covered, f"{path} is not validated"


# ------------------------------------------------------------------ work order validation (M12)
@pytest.mark.parametrize("change, message", [
    ({"required_certifications": {"value": ["None"]}}, "must include Refrigerant"),
    ({"campaign_code": {"value": "NK-C25-99"}}, "applicable campaign NK-C25-14 must be set"),
    ({"parts": {"value": [{"part_no": "63-1170", "qty": 2}]}}, "blocked for sale"),
    ({"parts": {"value": [{"part_no": "18-0355", "qty": 1}]}}, "superseded by 18-0356"),
    ({"asset_id": {"value": "TAC-C-9999"}}, "not in the fleet register"),
    ({"fault_code": {"value": "E-31"}}, "is not a NK-600 code"),
    ({"work_steps": {"value": ["Set P9.01 = 1 then hold RESET"]}}, "restricted procedure details"),
])
def test_work_order_rules(conn, change, message):
    from app.workorders.service import validate_work_order

    jakub = get_user(conn, "u06")  # EU chillers, no refrigerant cert; cannot see HV-PROC-07
    payload = {"asset_id": {"value": "RTM-H-0007"}, "fault_code": {"value": "L-03"},
               "required_certifications": {"value": ["Refrigerant"]}, "campaign_code": {"value": "NK-C25-14"},
               "priority": {"value": "Critical"}, "priority_reason": {"value": "open Critical case"},
               "parts": {"value": [{"part_no": "63-1180", "qty": 2}]},
               "site_permits": {"value": ["Work at height permit (roof, Rotterdam site rules)"]}}
    payload.update(change)
    issues = [i.message for i in validate_work_order(conn, jakub, payload, "")]
    assert any(message in m for m in issues), issues


def test_valid_work_order_has_no_errors(conn):
    from app.workorders.service import validate_work_order

    sofie = get_user(conn, "u05")
    payload = {"asset_id": {"value": "RTM-H-0007"}, "fault_code": {"value": "L-03"},
               "required_certifications": {"value": ["Refrigerant"]}, "campaign_code": {"value": "NK-C25-14"},
               "priority": {"value": "Critical"}, "priority_reason": {"value": "open Critical case"},
               "parts": {"value": [{"part_no": "63-1180", "qty": 2}, {"part_no": "63-0410", "qty": 2}]},
               "procedure_refs": {"value": ["SB-2025-014", "SOP-020"]},
               "site_permits": {"value": ["Work at height permit (roof, Rotterdam site rules)"]}}
    errors = [i for i in validate_work_order(conn, sofie, payload, "") if i.severity == "error"]
    assert errors == []
