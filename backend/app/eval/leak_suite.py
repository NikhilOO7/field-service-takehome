"""Permission leak suite (spec M15): deterministic ACL matrix + hand-written probe questions.

For every probe, the documents the user may not see must not appear in the answer context, must not be
cited, and their distinctive tokens must not appear in any rendered field.
"""

import json
from pathlib import Path

from sqlalchemy import Engine, text

from app.answer.service import ask
from app.auth.acl import acl_clause
from app.auth.users import get_user
from app.eval.runner import rendered_text


def acl_matrix(engine: Engine, users_file: Path, permissions_file: Path) -> list[str]:
    """Return mismatches between SQL visibility and the permissions.json rule (empty list = pass)."""
    users = json.loads(users_file.read_text())["users"]
    perms = json.loads(permissions_file.read_text())["documents"]
    problems = []
    with engine.connect() as conn:
        docs = {r[0] for r in conn.execute(text(
            "SELECT DISTINCT d.path FROM documents d JOIN chunks c ON c.document_id = d.id"))}
        for u in users:
            visible = {r[0] for r in conn.execute(text(
                f"SELECT DISTINCT d.path FROM chunks c JOIN documents d ON d.id = c.document_id WHERE {acl_clause('c')}"),
                {"user_groups": sorted(u["groups"])})}
            expected = {p for p in docs if p in perms and set(perms[p]["required_groups"]) <= set(u["groups"])}
            if visible != expected:
                problems.append(f"{u['id']}: extra {sorted(visible - expected)} missing {sorted(expected - visible)}")
    return problems


def run_probes(engine: Engine, probes_file: Path, progress=None) -> list[dict]:
    probes = json.loads(probes_file.read_text())
    results = []
    for p in probes:
        with engine.connect() as conn:
            user = get_user(conn, p["user_id"])
        if progress:
            progress(f"{p['id']} ({user.name}): {p['question']}")
        response = ask(engine, user, p["question"], persist=False).response
        with engine.connect() as conn:
            paths = {str(r[0]): r[1] for r in conn.execute(
                text("SELECT id, path FROM documents WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": list({s["doc_id"] for s in response["sources"]})})}
        context_paths = [paths.get(s["doc_id"], "") for s in response["sources"]]
        cited_paths = [paths.get(s["doc_id"], "") for s in response["sources"] if s["cited"]]
        body = rendered_text(response, None)
        in_context = sorted({f for f in p["forbidden_docs"] for path in context_paths if f in path})
        cited = sorted({f for f in p["forbidden_docs"] for path in cited_paths if f in path})
        # A token only counts as leaked if it isn't in the question or in any source the user may see
        # (e.g. NK-C25-14 is also in the public Work Order Writing Guide).
        permitted = (p["question"] + "\n" + "\n".join(s["text"] for s in response["sources"])).lower()
        tokens = [t for t in p["forbidden_tokens"] if t.lower() in body.lower() and t.lower() not in permitted]
        results.append({"id": p["id"], "user": p["user_id"], "question": p["question"],
                        "status": response["answer"]["status"], "headline": response["answer"]["headline"]["text"],
                        "forbidden_in_context": in_context, "forbidden_cited": cited, "forbidden_tokens": tokens,
                        "restricted_hint": (response.get("restricted") or {}).get("procedures"),
                        "ok": not (in_context or cited or tokens)})
    return results
