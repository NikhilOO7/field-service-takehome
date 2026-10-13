"""Object ownership (spec M2, §3.5a): Priya must not reach Daniel's conversation, message or work order,
through the real API with real sessions. No LLM calls: Daniel's answer is inserted directly."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.users import get_user
from app.conversations import add_message, create_conversation


@pytest.fixture(scope="module")
def client(engine, ingested):
    from app import main

    original = main.get_engine
    main.get_engine = lambda: engine
    yield TestClient(main.app)
    main.get_engine = original


def login(client, user_id) -> TestClient:
    c = TestClient(client.app)
    assert c.post("/api/session", json={"user_id": user_id}).status_code == 200
    return c


@pytest.fixture(scope="module")
def daniel_objects(engine, client):
    """Daniel's E-31 answer citing an HV-PROC-07 chunk, plus a work order drafted from it."""
    with engine.begin() as conn:
        daniel = get_user(conn, "u02")
        hv_chunk = str(conn.execute(text("""SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                                            WHERE d.doc_number = 'HV-PROC-07' LIMIT 1""")).scalar_one())
        conv = create_conversation(conn, daniel)
        response = {"answer": {"status": "answered", "headline": {"text": "x", "citations": ["S1"]}, "blocks": [],
                               "conflicts": [], "restricted_notice": None, "clarify_question": None,
                               "follow_up": {"needed": True, "reasons": [], "work_order_hint": None},
                               "confidence": "high"},
                    "sources": [{"id": "S1", "chunk_id": hv_chunk, "label": "HV-PROC-07", "cited": True, "text": ""}],
                    "facts": [], "assets": [{"asset_id": "TAC-C-0143"}], "history": {}, "restricted": None}
        msg = add_message(conn, conv, daniel, "TAC-C-0143 tripped on E-31. How do I reset it?", response,
                          [hv_chunk], [], {}, "test", [])
    d = login(client, "u02")
    wo = d.post("/api/work-orders", json={"message_id": msg})
    assert wo.status_code == 200, wo.text
    return {"conv": conv, "msg": msg, "wo": wo.json()["id"], "chunk": hv_chunk}


def test_daniel_can_reach_his_own_objects(client, daniel_objects):
    d = login(client, "u02")
    assert d.get(f"/api/conversations/{daniel_objects['conv']}").status_code == 200
    assert d.get(f"/api/messages/{daniel_objects['msg']}").status_code == 200
    assert d.get(f"/api/work-orders/{daniel_objects['wo']}").status_code == 200
    assert d.get(f"/api/sources/{daniel_objects['chunk']}").status_code == 200


def test_priya_cannot_reach_daniels_objects(engine, client, daniel_objects):
    p = login(client, "u03")
    assert p.get(f"/api/conversations/{daniel_objects['conv']}").status_code == 404
    assert daniel_objects["conv"] not in {c["id"] for c in p.get("/api/conversations").json()["conversations"]}
    assert p.get(f"/api/messages/{daniel_objects['msg']}").status_code == 404
    with engine.connect() as conn:
        before_msgs = conn.execute(text("SELECT count(*) FROM messages")).scalar_one()
        before_wos = conn.execute(text("SELECT count(*) FROM work_orders")).scalar_one()
    # posting into his conversation is refused before any processing
    assert p.post("/api/ask", json={"question": "hi", "conversation_id": daniel_objects["conv"]}).status_code == 404
    # drafting a work order from his answer is refused and creates nothing
    assert p.post("/api/work-orders", json={"message_id": daniel_objects["msg"]}).status_code == 404
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar_one() == before_msgs
        assert conn.execute(text("SELECT count(*) FROM work_orders")).scalar_one() == before_wos
    # the HV-PROC-07 chunk he cited is not visible to her
    assert p.get(f"/api/sources/{daniel_objects['chunk']}").status_code == 403
    # his work order is invisible and immutable for her (draft: not in any approval queue)
    assert p.get(f"/api/work-orders/{daniel_objects['wo']}").status_code == 404
    assert p.patch(f"/api/work-orders/{daniel_objects['wo']}", json={"changes": {"summary": "x"}}).status_code == 404
    assert p.post(f"/api/work-orders/{daniel_objects['wo']}/submit").status_code == 404
    assert daniel_objects["wo"] not in {w["id"] for w in p.get("/api/work-orders").json()["work_orders"]}
    assert all(o["work_order_id"] != daniel_objects["wo"] for o in p.get("/api/outbox").json()["outbox"])


def test_database_rejects_cross_user_work_order(engine, daniel_objects):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("""INSERT INTO work_orders (id, status, created_by, source_message, payload)
                                 VALUES (:id, 'draft', 'u03', :m, '{}')"""),
                     {"id": uuid.uuid4(), "m": daniel_objects["msg"]})


def test_database_rejects_message_in_foreign_conversation(engine, daniel_objects):
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""INSERT INTO messages (id, conversation_id, user_id, question)
                                 VALUES (:id, :c, 'u03', 'x')"""), {"id": uuid.uuid4(), "c": daniel_objects["conv"]})


def test_every_owned_route_hides_foreign_objects(client, daniel_objects):
    """Generated from the route table: any route with an owned-object id parameter returns 404 to a non-owner.
    A new owned-object route without ownership checks fails here."""
    from fastapi.routing import APIRoute

    ids = {"conversation_id": daniel_objects["conv"], "message_id": daniel_objects["msg"],
           "wo_id": daniel_objects["wo"]}
    p = login(client, "u03")
    checked = 0
    for route in client.app.routes:
        if not isinstance(route, APIRoute):
            continue
        params = [n for n in ids if "{" + n + "}" in route.path]
        if not params:
            continue
        path = route.path
        for n in params:
            path = path.replace("{" + n + "}", ids[n])
        for method in route.methods:
            body = {"changes": {}} if method == "PATCH" else {"reason": "x"} if path.endswith("/reject") else None
            res = p.request(method, path, json=body)
            assert res.status_code == 404, f"{method} {route.path} -> {res.status_code}"
            checked += 1
    assert checked >= 7


def test_session_required(client):
    anon = TestClient(client.app)
    for path in ("/api/conversations", "/api/work-orders", "/api/outbox", "/api/documents"):
        assert anon.get(path).status_code == 401
    assert anon.post("/api/ask", json={"question": "x"}).status_code == 401
    forged = TestClient(client.app, cookies={"fsc_session": "forged"})
    assert forged.get("/api/conversations").status_code == 401


def test_restricted_files_are_forbidden(engine, client):
    p = login(client, "u03")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, required_groups, acl_status FROM documents")).all()
    for doc_id, groups, acl in rows:
        allowed = acl == "listed" and set(groups) <= {"region-na", "family-compressors"}
        status = p.get(f"/api/documents/{doc_id}/file").status_code
        assert (status == 200) == allowed, (doc_id, groups, status)


def test_nobody_approves_or_rejects_their_own_work_order(engine, client, daniel_objects, settings, monkeypatch):
    monkeypatch.setattr(settings, "wo_approver_role", "any")  # so Daniel is an approver at all
    wo_id = daniel_objects["wo"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE work_orders SET status = 'pending_approval' WHERE id = :id"), {"id": wo_id})
    try:
        d = login(client, "u02")
        assert d.post(f"/api/work-orders/{wo_id}/approve").status_code == 403
        assert d.post(f"/api/work-orders/{wo_id}/reject", json={"reason": "x"}).status_code == 403
        assert d.get(f"/api/work-orders/{wo_id}").json()["status"] == "pending_approval"
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE work_orders SET status = 'draft' WHERE id = :id"), {"id": wo_id})


@pytest.mark.parametrize(("role", "user_id", "can_read"), [
    ("role-lead", "u08", True),    # Kenji: NA lead with compressors + HV, can read everything it was built on
    ("role-lead", "u07", False),   # Lena: EU lead; TAC-C-0143 is an NA machine
    ("role-lead", "u03", False),   # Priya: not a lead
    ("any", "u03", False),         # Priya: approver under "any", but can't read HV-PROC-07 behind the answer
    ("any", "u12", False),         # Olu: no compressors access
])
def test_reviewers_only_see_work_orders_they_could_read(engine, client, daniel_objects, settings, monkeypatch,
                                                        role, user_id, can_read):
    monkeypatch.setattr(settings, "wo_approver_role", role)
    wo_id = daniel_objects["wo"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE work_orders SET status = 'pending_approval' WHERE id = :id"), {"id": wo_id})
    try:
        r = login(client, user_id)
        listed = wo_id in {w["id"] for w in r.get("/api/work-orders").json()["work_orders"]}
        assert listed is can_read
        assert (r.get(f"/api/work-orders/{wo_id}").status_code == 200) is can_read
        if not can_read:
            assert r.post(f"/api/work-orders/{wo_id}/approve").status_code == 404
            assert r.post(f"/api/work-orders/{wo_id}/reject", json={"reason": "x"}).status_code == 404
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE work_orders SET status = 'draft' WHERE id = :id"), {"id": wo_id})


def test_concurrent_decisions_are_serialised(engine, client, daniel_objects, settings, monkeypatch):
    """Two rejects race on one pending work order: the row lock makes exactly one of them win."""
    import threading

    from app.workorders import service as wo

    monkeypatch.setattr(settings, "wo_approver_role", "role-lead")
    wo_id = daniel_objects["wo"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE work_orders SET status = 'pending_approval' WHERE id = :id"), {"id": wo_id})
        kenji = get_user(conn, "u08")
    first_holds_lock, release, results = threading.Event(), threading.Event(), {}

    def first():
        with engine.begin() as c:
            wo.reject(c, kenji, wo_id, "first")
            first_holds_lock.set()
            release.wait(5)
        results["first"] = "ok"

    def second():
        first_holds_lock.wait(5)
        try:
            with engine.begin() as c:
                wo.reject(c, kenji, wo_id, "second")
            results["second"] = "ok"
        except wo.WorkOrderError as exc:
            results["second"] = exc.status

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    try:
        for t in threads:
            t.start()
        first_holds_lock.wait(5)
        release.set()
        for t in threads:
            t.join(10)
        assert results["first"] == "ok" and results["second"] != "ok"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT rejected_reason FROM work_orders WHERE id = :id"),
                                {"id": wo_id}).scalar_one() == "first"
    finally:
        release.set()
        with engine.begin() as conn:
            conn.execute(text("UPDATE work_orders SET status = 'draft', rejected_reason = NULL WHERE id = :id"),
                         {"id": wo_id})


def test_outbox_file_is_written_only_after_commit(engine, settings, tmp_path):
    from fastapi import HTTPException

    from app import main
    from app.workorders.service import WorkOrderError

    original = main.get_engine
    main.get_engine = lambda: engine
    try:
        failed, ok = tmp_path / "failed.json", tmp_path / "ok.json"

        def fails(conn):
            conn.info["pending_files"].append((failed, "{}"))
            raise WorkOrderError(409, "rolled back")

        with pytest.raises(HTTPException):
            main._wo_call(fails)
        assert not failed.exists()
        main._wo_call(lambda conn: conn.info["pending_files"].append((ok, "{}")))
        assert ok.exists()
    finally:
        main.get_engine = original
