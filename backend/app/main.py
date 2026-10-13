"""FastAPI application (spec M13).

Identity: a signed session cookie set by POST /api/session (demo login). Every other endpoint derives the
user from that cookie via current_user(); no endpoint accepts a user id in its body.
Ownership: conversations, messages and work orders are read only through owner-scoped functions;
not owned == not found (404).
"""

import json
import queue
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import text

from app.answer.render import citation_label
from app.answer.service import AskError, ask, visible_chunk
from app.auth.acl import acl_clause, acl_params
from app.auth.users import UserContext, get_user, list_users
from app.config import get_settings
from app.conversations import audit, conversation_messages, get_message, list_conversations
from app.db.session import get_engine
from app.workorders import service as wo

COOKIE = "fsc_session"
SESSION_MAX_AGE = 12 * 3600

app = FastAPI(title="Field Service Copilot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="fsc-session")


def current_user(request: Request) -> UserContext:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "not signed in")
    try:
        user_id = _serializer().loads(token, max_age=SESSION_MAX_AGE)["user_id"]
    except (BadSignature, KeyError, TypeError):
        raise HTTPException(401, "invalid session") from None
    with get_engine().connect() as conn:
        user = get_user(conn, user_id)
    if user is None:
        raise HTTPException(401, "unknown user")
    return user


def _user_dict(u: UserContext) -> dict:
    return {"id": u.id, "name": u.name, "title": u.title, "groups": sorted(u.groups),
            "certifications": u.certifications, "is_lead": u.is_lead, "can_approve": wo.is_approver(u)}


# ------------------------------------------------------------------ identity
@app.get("/api/health")
def health():
    with get_engine().connect() as conn:
        docs = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
        chunks = conn.execute(text("SELECT count(*), count(embedding) FROM chunks")).one()
    return {"ok": True, "documents": docs, "chunks": chunks[0], "embedded": chunks[1],
            "openai_configured": bool(get_settings().openai_api_key)}


@app.get("/api/users")
def users():
    with get_engine().connect() as conn:
        rows = list_users(conn)
        groups = {r[0]: r[1] for r in conn.execute(text("SELECT id, description FROM groups"))}
    return {"users": rows, "groups": groups}


class SessionIn(BaseModel):
    user_id: str


@app.post("/api/session")
def login(body: SessionIn, response: Response):
    if not get_settings().demo_auth:
        raise HTTPException(403, "demo login disabled")
    with get_engine().connect() as conn:
        user = get_user(conn, body.user_id)
    if user is None:
        raise HTTPException(404, "unknown user")
    token = _serializer().dumps({"user_id": user.id})
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return _user_dict(user)


@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/session")
def me(user: UserContext = Depends(current_user)):
    return _user_dict(user)


# ------------------------------------------------------------------ ask
class AskIn(BaseModel):
    question: str
    conversation_id: str | None = None


@app.post("/api/ask")
def ask_endpoint(body: AskIn, stream: bool = False, user: UserContext = Depends(current_user)):
    if not body.question.strip():
        raise HTTPException(422, "empty question")
    if not stream:
        try:
            return ask(get_engine(), user, body.question.strip(), body.conversation_id).response
        except AskError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    events: queue.Queue = queue.Queue()

    def progress(stage: str, message: str) -> None:
        events.put({"event": "status", "data": {"stage": stage, "message": message}})

    def worker() -> None:
        try:
            result = ask(get_engine(), user, body.question.strip(), body.conversation_id, progress=progress)
            events.put({"event": "answer", "data": result.response})
        except AskError as exc:
            events.put({"event": "error", "data": {"status": exc.status, "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001
            events.put({"event": "error", "data": {"status": 500, "message": repr(exc)}})
        events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            item = events.get()
            if item is None:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------ conversations (owner-scoped)
@app.get("/api/conversations")
def conversations(user: UserContext = Depends(current_user)):
    with get_engine().connect() as conn:
        return {"conversations": list_conversations(conn, user)}


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: UserContext = Depends(current_user)):
    with get_engine().begin() as conn:
        msgs = conversation_messages(conn, conversation_id, user)
        if msgs is None:
            audit(conn, user.id, "ownership_denied", "conversation", conversation_id, "404")
            raise HTTPException(404, "conversation not found")
    return {"id": conversation_id, "messages": msgs}


@app.get("/api/messages/{message_id}")
def message(message_id: str, user: UserContext = Depends(current_user)):
    with get_engine().begin() as conn:
        msg = get_message(conn, message_id, user)
        if msg is None:
            audit(conn, user.id, "ownership_denied", "message", message_id, "404")
            raise HTTPException(404, "message not found")
    return msg


# ------------------------------------------------------------------ sources and documents (ACL-checked)
@app.get("/api/sources/{chunk_id}")
def source(chunk_id: str, user: UserContext = Depends(current_user)):
    with get_engine().begin() as conn:
        try:
            row = visible_chunk(conn, user, chunk_id)
        except Exception:  # noqa: BLE001 - malformed id
            row = None
        if row is None:
            audit(conn, user.id, "acl_denied", "chunk", chunk_id, "403")
            raise HTTPException(403, "you do not have access to this source")
        neighbours = conn.execute(
            text(f"""SELECT c.id, c.text, c.locator FROM chunks c WHERE c.document_id = :d AND {acl_clause('c')}
                     AND c.ord BETWEEN (SELECT ord FROM chunks WHERE id = CAST(:id AS uuid)) - 1
                                   AND (SELECT ord FROM chunks WHERE id = CAST(:id AS uuid)) + 1
                     ORDER BY c.ord"""),
            {"d": row["doc_id"], "id": chunk_id, **acl_params(user)}).mappings().all()
    return {
        "chunk_id": chunk_id, "text": row["text"], "locator": row["locator"], "doc_id": str(row["doc_id"]),
        "title": row["title"], "doc_type": row["doc_type"], "doc_number": row["doc_number"],
        "revision": row["revision"], "effective_date": row["effective_date"], "doc_status": row["doc_status"],
        "flags": row["flags"], "file_type": Path(row["path"]).suffix.lstrip("."),
        "label": citation_label(row["title"], row["doc_type"], row["doc_number"], row["revision"], row["locator"],
                                row["effective_date"], row["flags"]),
        "context": [{"id": str(n["id"]), "text": n["text"], "locator": n["locator"]} for n in neighbours],
    }


@app.get("/api/documents")
def documents(user: UserContext = Depends(current_user)):
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(f"""SELECT DISTINCT d.id, d.title, d.doc_type, d.doc_number, d.revision, d.status, d.effective_date
                     FROM documents d JOIN chunks c ON c.document_id = d.id WHERE {acl_clause('c')}
                     ORDER BY d.title"""), acl_params(user)).mappings().all()
    return {"documents": [dict(r) for r in rows]}


@app.get("/api/documents/{document_id}/file")
def document_file(document_id: str, user: UserContext = Depends(current_user)):
    with get_engine().begin() as conn:
        try:
            row = conn.execute(
                text("""SELECT d.path FROM documents d WHERE d.id = CAST(:id AS uuid)
                         AND d.acl_status = 'listed' AND d.required_groups <@ CAST(:user_groups AS text[])"""),
                {"id": document_id, **acl_params(user)}).first()
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            audit(conn, user.id, "acl_denied", "document", document_id, "403")
            raise HTTPException(403, "you do not have access to this document")
    path = (get_settings().project_root / row[0]).resolve()
    if not path.is_relative_to(get_settings().project_root):
        raise HTTPException(403, "invalid path")
    media = {".pdf": "application/pdf", ".html": "text/html", ".eml": "message/rfc822", ".csv": "text/csv",
             ".json": "application/json"}.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name,
                        content_disposition_type="inline" if path.suffix.lower() in {".pdf", ".html"} else "attachment")


# ------------------------------------------------------------------ work orders
class WorkOrderIn(BaseModel):
    message_id: str


class WorkOrderPatch(BaseModel):
    changes: dict


class RejectIn(BaseModel):
    reason: str


def _wo_call(fn, *args):
    pending: list = []  # files to write once the transaction has committed (outbox export)
    try:
        with get_engine().begin() as conn:
            conn.info["pending_files"] = pending
            try:
                result = fn(conn, *args)
            finally:
                conn.info.pop("pending_files", None)
    except wo.WorkOrderError as exc:
        raise HTTPException(exc.status, {"message": str(exc), "errors": exc.details}) from None
    for path, content in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return result


def _wo_out(conn, w: dict) -> dict:
    out = {**w, "events": wo.events(conn, str(w["id"]))}
    if w["status"] in {"draft", "pending_approval"}:
        out["eligible_reviewers"] = wo.eligible_reviewers(conn, w)
    return out


@app.post("/api/work-orders")
def create_work_order(body: WorkOrderIn, user: UserContext = Depends(current_user)):
    return _wo_call(lambda c: _wo_out(c, wo.create_from_message(c, user, body.message_id)))


@app.get("/api/assets")
def assets(user: UserContext = Depends(current_user)):
    with get_engine().connect() as conn:
        return {"assets": wo.list_assets(conn, user)}


@app.get("/api/work-orders")
def work_orders(user: UserContext = Depends(current_user)):
    with get_engine().connect() as conn:
        return {"work_orders": wo.list_work_orders(conn, user), "can_approve": wo.is_approver(user)}


@app.get("/api/work-orders/{wo_id}")
def work_order(wo_id: str, user: UserContext = Depends(current_user)):
    with get_engine().begin() as conn:
        w = wo.get_work_order(conn, wo_id, user)
        if w is None:
            raise HTTPException(404, "work order not found")
        return _wo_out(conn, w)


@app.patch("/api/work-orders/{wo_id}")
def edit_work_order(wo_id: str, body: WorkOrderPatch, user: UserContext = Depends(current_user)):
    return _wo_call(lambda c: _wo_out(c, wo.update_draft(c, user, wo_id, body.changes)))


@app.post("/api/work-orders/{wo_id}/submit")
def submit_work_order(wo_id: str, user: UserContext = Depends(current_user)):
    return _wo_call(lambda c: _wo_out(c, wo.submit(c, user, wo_id)))


@app.post("/api/work-orders/{wo_id}/approve")
def approve_work_order(wo_id: str, user: UserContext = Depends(current_user)):
    return _wo_call(lambda c: _wo_out(c, wo.approve(c, user, wo_id)))


@app.post("/api/work-orders/{wo_id}/reject")
def reject_work_order(wo_id: str, body: RejectIn, user: UserContext = Depends(current_user)):
    return _wo_call(lambda c: _wo_out(c, wo.reject(c, user, wo_id, body.reason)))


@app.get("/api/outbox")
def outbox(user: UserContext = Depends(current_user)):
    with get_engine().connect() as conn:
        return {"outbox": wo.list_outbox(conn, user)}
