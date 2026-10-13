"""The ENTIRE answer-model input must respect the ACL, not just the S-sources: computed facts, the conflict
register and everything else rendered into the prompt. Built with a fake LLM, so no network is needed."""

import re

import pytest
from sqlalchemy import text

from app.answer import service
from app.answer.context import conflict_sources
from app.answer.schema import QueryPlan
from app.answer.validate import build_fingerprints, leak_hits
from app.auth.acl import acl_clause, acl_params, not_acl_clause
from app.auth.users import get_user
from app.llm.openai_client import Usage


class FakeLLM:
    def __init__(self, plan: QueryPlan):
        self.plan = plan

    def structured(self, role, system, user, schema, **kw):
        assert schema is QueryPlan, "only query analysis may run before the answer model"
        return self.plan

    def embed(self, texts):
        return [[0.01] * 3072 for _ in texts]


CASES = [
    # Grace: NA chillers + refrigerant. The EXV campaign bulletin (SB-2025-014) is EU-only, so conflicts C6/C7
    # (manual vs bulletin) must not reach her prompt, nor the bulletin's 15% threshold or brazing method.
    ("u11", "NK-600 EXV position reads 10% while the request is 60%. Is the valve stuck, and how do I replace it?",
     ["NK-600"], []),
    # Jakub: EU chillers without the refrigerant cert. C5 restates SOP-020 (refrigerant-only).
    ("u06", "NK-600 L-03 low suction alarm. Can I top up the refrigerant without a leak test?", ["NK-600"], ["L-03"]),
    # Priya: HV procedure for E-31 is certification-gated.
    ("u03", "TAC-C-0143 tripped on E-31. How do I reset it?", ["VRS-110"], ["E-31"]),
    # Olu: conveyors only; asks about a chiller.
    ("u12", "How do I check the EXV on an NK-600?", ["NK-600"], []),
    # Maria: pumps; C2 is fully visible and should be present.
    ("u01", "What torque do I use on the casing bolts for FRS-P-0031 after a seal change?", ["HCP-350"], []),
]


def _prompt(engine, monkeypatch, settings, user_id, question, models, codes):
    plan = QueryPlan(intent="troubleshoot", equipment_models=models, fault_codes=codes, needs_history=True,
                     needs_procedure=True, safety_topics=[], rewrites=[])
    monkeypatch.setattr(service, "get_llm", lambda: FakeLLM(plan))
    monkeypatch.setattr(settings, "reranker", "none")
    with engine.connect() as conn:
        user = get_user(conn, user_id)
        prep = service.prepare(conn, user, question, Usage())
        visible = "\n".join(r[0] for r in conn.execute(
            text(f"SELECT c.text FROM chunks c WHERE {acl_clause('c')}"), acl_params(user)))
        hidden = [r[0] for r in conn.execute(
            text(f"SELECT c.text FROM chunks c WHERE {not_acl_clause('c')}"), acl_params(user))]
        visible_docs = {r[0] for r in conn.execute(
            text(f"SELECT d.path FROM documents d WHERE {acl_clause('d')}"), acl_params(user))}
    return prep, visible, hidden, visible_docs


@pytest.mark.parametrize(("user_id", "question", "models", "codes"), CASES)
def test_model_input_contains_nothing_the_user_cannot_see(engine, ingested, settings, monkeypatch,
                                                        user_id, question, models, codes):
    prep, visible, hidden, visible_docs = _prompt(engine, monkeypatch, settings, user_id, question, models, codes)
    prompt = prep.user_message
    # RESTRICTED HINTS is the deliberate, policy-controlled disclosure (titles/certifications only)
    checked = re.sub(r"RESTRICTED HINTS.*?\n\n", "", prompt, flags=re.S)

    for conflict in prep.pack.conflicts:
        defs = service_conflict_defs()
        assert conflict_sources({conflict.id: defs[conflict.id]}) <= visible_docs, conflict.id

    # every computed fact must be built only from chunks this user can see
    fact_chunks = [cid for f in prep.pack.facts for cid in f.chunk_ids]
    with engine.connect() as conn:
        user = get_user(conn, user_id)
        hidden_fact_chunks = conn.execute(
            text(f"SELECT c.id FROM chunks c WHERE c.id = ANY(CAST(:ids AS uuid[])) AND {not_acl_clause('c')}"),
            {"ids": fact_chunks, **acl_params(user)}).all()
    assert hidden_fact_chunks == [], f"{user_id}: a computed fact draws on chunks the user cannot see"

    # the system's own wording in computed facts is not document content
    system_phrasing = "in the last 24 months"
    fingerprints = build_fingerprints(hidden, f"{visible}\n{question}\n{system_phrasing}")
    assert leak_hits(checked, fingerprints) == [], f"{user_id}: restricted values in the model input"


def test_grace_gets_no_restricted_conflicts(engine, ingested, settings, monkeypatch):
    user_id, question, models, codes = CASES[0]
    prep, *_ = _prompt(engine, monkeypatch, settings, user_id, question, models, codes)
    assert not {"C5", "C6", "C7"} & {c.id for c in prep.pack.conflicts}
    assert "[C6]" not in prep.user_message and "[C7]" not in prep.user_message
    assert "nitrogen purge" not in prep.user_message.lower()


def test_fully_visible_conflict_is_still_included(engine, ingested, settings, monkeypatch):
    prep, *_ = _prompt(engine, monkeypatch, settings, *CASES[4])
    assert "C2" in {c.id for c in prep.pack.conflicts}


def service_conflict_defs():
    from app.answer.context import _conflict_positions

    return _conflict_positions()
