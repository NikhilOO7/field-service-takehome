"""On-demand work-order hint: validated like the answer used to be, with no network (fake LLM)."""

from sqlalchemy import text

from app.answer.schema import WorkOrderHint
from app.auth.users import get_user
from app.workorders import hint as H


def test_hint_fields_are_validated_and_misplaced_part_citations_repointed(engine, ingested, monkeypatch):
    with engine.connect() as conn:
        sofie = get_user(conn, "u05")
        part_chunk, other_chunk = [str(r[0]) for r in conn.execute(text(
            """(SELECT id FROM chunks WHERE text LIKE '%63-1180%' LIMIT 1)
               UNION ALL (SELECT id FROM chunks WHERE text NOT LIKE '%63-1180%' AND required_groups = '{}' LIMIT 1)"""))]
        response = {"answer": {"status": "answered", "headline": {"text": "x", "citations": []}, "blocks": [],
                               "conflicts": [], "restricted_notice": None, "clarify_question": None,
                               "follow_up": {"needed": True, "reasons": ["parts required"]}, "confidence": "high"},
                    "sources": [{"id": "S1", "chunk_id": other_chunk}, {"id": "S2", "chunk_id": part_chunk}],
                    "facts": [], "assets": []}
        canned = WorkOrderHint.model_validate({
            "fault_code": None, "summary": {"text": "Replace the EXV.", "citations": ["S2"]}, "observations": None,
            # right part, wrong citation (S1 doesn't contain it): re-pointed to S2, kept
            "parts": [{"part_no": "63-1180 rev 3", "qty": 2, "citations": ["S1"]},
                      # invented part number: no permitted source has it, dropped
                      {"part_no": "99-9999", "qty": 1, "citations": ["S2"]}],
            "procedure_refs": [], "work_steps": [], "estimated_duration_h": None})

        class Fake:
            def structured(self, *a, **k):
                return canned

        monkeypatch.setattr(H, "get_llm", lambda: Fake())
        result = H.generate_hint(conn, sofie, "RTM-H-0007 L-03 again", response)
    assert [p["part_no"] for p in result["parts"]] == ["63-1180"]  # normalised, re-cited, kept
    assert result["parts"][0]["citations"] == ["S2"]
