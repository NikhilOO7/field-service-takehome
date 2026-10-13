"""The eval's permission checks run without an LLM: feed them a crafted response."""

from sqlalchemy import text

from app.auth.users import get_user
from app.eval.runner import permission_checks


def test_permission_checks_cover_context_facts_conflicts_and_output(engine, ingested):
    with engine.connect() as conn:
        priya = get_user(conn, "u03")
        hv_chunk = str(conn.execute(text("""SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                                            WHERE d.doc_number = 'HV-PROC-07' LIMIT 1""")).scalar_one())
    clean = {"answer": {"headline": {"text": "x", "citations": []}}, "sources": [], "facts": [],
             "context_conflicts": []}
    assert all(ok for _, ok, _ in permission_checks(engine, priya, clean, "E-31 is a drive fault.", "q"))

    # a restricted chunk behind a computed fact, and a conflict whose bulletin she can't see
    dirty = clean | {"facts": [{"id": "F1", "chunk_ids": [hv_chunk]}], "context_conflicts": ["C6"]}
    checks = {name: ok for name, ok, _ in permission_checks(engine, priya, dirty, "x", "q")}
    assert checks["acl_of_context"] is False
