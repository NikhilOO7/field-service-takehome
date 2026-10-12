"""Owner-scoped access to conversations and messages (spec M2 object ownership).

These are the ONLY functions that read the conversations/messages tables. Every query filters on the
caller's user id; a missing or foreign object returns None, which the API turns into 404.
"""

import json
import uuid

from sqlalchemy import Connection, text

from app.auth.users import UserContext


def create_conversation(conn: Connection, user: UserContext) -> str:
    cid = uuid.uuid4()
    conn.execute(text("INSERT INTO conversations (id, user_id) VALUES (:id, :u)"), {"id": cid, "u": user.id})
    return str(cid)


def get_conversation(conn: Connection, conversation_id: str, user: UserContext) -> dict | None:
    try:
        uuid.UUID(str(conversation_id))
    except ValueError:
        return None
    row = conn.execute(
        text("SELECT id, user_id, created_at FROM conversations WHERE id = :id AND user_id = :u"),
        {"id": conversation_id, "u": user.id},
    ).mappings().first()
    return dict(row) if row else None


def list_conversations(conn: Connection, user: UserContext) -> list[dict]:
    rows = conn.execute(
        text("""SELECT c.id, c.created_at,
                       (SELECT question FROM messages m WHERE m.conversation_id = c.id AND m.user_id = :u
                        ORDER BY created_at LIMIT 1) AS first_question
                FROM conversations c WHERE c.user_id = :u ORDER BY c.created_at DESC"""),
        {"u": user.id},
    ).mappings().all()
    return [dict(r) for r in rows]


def add_message(conn: Connection, conversation_id: str, user: UserContext, question: str, response: dict,
                context_chunk_ids: list[str], restricted_hints: list[dict], timings: dict, model: str,
                tokens: list[dict]) -> str:
    mid = uuid.uuid4()
    conn.execute(
        text("""INSERT INTO messages (id, conversation_id, user_id, question, answer, validation, context_chunk_ids,
                                      restricted_hints, timings, model, tokens)
                VALUES (:id, :c, :u, :q, CAST(:a AS jsonb), CAST(:v AS jsonb), CAST(:ctx AS uuid[]),
                        CAST(:rh AS jsonb), CAST(:t AS jsonb), :m, CAST(:tok AS jsonb))"""),
        {"id": mid, "c": conversation_id, "u": user.id, "q": question,
         "a": json.dumps(response, default=str), "v": json.dumps(response.get("validation", {})),
         "ctx": context_chunk_ids, "rh": json.dumps(restricted_hints), "t": json.dumps(timings), "m": model,
         "tok": json.dumps(tokens)},
    )
    return str(mid)


def get_message(conn: Connection, message_id: str, user: UserContext) -> dict | None:
    try:
        uuid.UUID(str(message_id))
    except ValueError:
        return None
    row = conn.execute(
        text("""SELECT id, conversation_id, user_id, question, answer, validation, context_chunk_ids, created_at
                FROM messages WHERE id = :id AND user_id = :u"""),
        {"id": message_id, "u": user.id},
    ).mappings().first()
    return dict(row) if row else None


def conversation_messages(conn: Connection, conversation_id: str, user: UserContext) -> list[dict] | None:
    if get_conversation(conn, conversation_id, user) is None:
        return None
    rows = conn.execute(
        text("""SELECT id, question, answer, created_at FROM messages
                WHERE conversation_id = :c AND user_id = :u ORDER BY created_at"""),
        {"c": conversation_id, "u": user.id},
    ).mappings().all()
    return [dict(r) for r in rows]


def audit(conn: Connection, user_id: str | None, action: str, object_type: str | None = None,
          object_id: str | None = None, decision: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        text("""INSERT INTO audit_log (user_id, action, object_type, object_id, decision, detail)
                VALUES (:u, :a, :t, :o, :d, CAST(:x AS jsonb))"""),
        {"u": user_id, "a": action, "t": object_type, "o": object_id, "d": decision,
         "x": json.dumps(detail or {}, default=str)},
    )
