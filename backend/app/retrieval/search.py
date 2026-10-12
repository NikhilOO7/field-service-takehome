"""Hybrid retrieval (spec M9.2-M9.6).

Three ranked lists per query variant, all filtered by the user's ACL inside the same SQL statement:
  vector (pgvector HNSW, halfvec cosine), keyword (tsvector, OR of significant terms),
  exact (chunks tagged with the question's assets / fault codes / part numbers / doc numbers).
They are fused with Reciprocal Rank Fusion, then re-scored for model match, authority, document status
and asset match, then reranked by the fast LLM.

The restricted probe searches the documents the user can NOT see and returns metadata only.
"""

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from app.auth.acl import acl_clause, acl_params, disclosure, missing_groups, not_acl_clause
from app.auth.users import UserContext
from app.ingestion.entities import model_family

RRF_K = 60
STOPWORDS = set(["a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "by", "from", "is", "are", "was", "were", "be", "been", "do", "does", "did", "what", "whats", "how", "when", "where", "which", "who", "why", "can", "could", "should", "would", "i", "we", "you", "it", "its", "this", "that", "these", "those", "my", "our", "your", "me", "us", "again", "any", "there", "here", "has", "have", "had", "not", "no", "yes", "if", "then", "than", "so", "as", "up", "out", "just", "about", "into", "over", "after", "before"])
AUTHORITY_WEIGHT = {1: 1.3, 2: 1.2, 3: 1.15, 4: 1.0, 5: 1.0, 6: 0.85, 7: 0.75}
STATUS_WEIGHT = {"current": 1.0, "superseded": 0.4, "stale": 0.4, "unofficial": 0.4}


@dataclass
class Candidate:
    id: str
    document_id: str
    text: str
    embed_text: str
    kind: str
    locator: dict
    models: list[str]
    asset_ids: list[str]
    fault_codes: list[str]
    doc_title: str
    doc_type: str
    doc_number: str | None
    revision: str | None
    effective_date: object
    doc_status: str
    authority_rank: int
    flags: list[str]
    score: float = 0.0
    rerank: float | None = None
    sources: set = field(default_factory=set)


CANDIDATE_SQL = """
SELECT c.id, c.document_id, c.text, c.embed_text, c.kind, c.locator, c.models, c.asset_ids, c.fault_codes,
       c.flags, c.authority_rank, c.doc_status, d.title AS doc_title, d.doc_type, d.doc_number, d.revision,
       d.effective_date
FROM chunks c JOIN documents d ON d.id = c.document_id
"""


def _tsquery(question: str) -> str | None:
    tokens = re.findall(r"[a-z0-9][a-z0-9.\-]*[a-z0-9]|[a-z0-9]", question.lower())
    tokens = [t for t in tokens if t not in STOPWORDS and len(t) > 1]
    if not tokens:
        return None
    return " | ".join(f"'{t}'" for t in dict.fromkeys(tokens))


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _ranked_ids(conn: Connection, sql: str, params: dict) -> list[str]:
    return [str(r[0]) for r in conn.execute(text(sql), params)]


def hybrid_search(
    conn: Connection,
    user: UserContext,
    queries: list[str],
    query_vectors: list[list[float]],
    models: list[str],
    asset_ids: list[str],
    codes: list[str],
    parts: list[str],
    doc_numbers: list[str],
    boost_documents: set[str],
    top_k: int = 40,
) -> list[Candidate]:
    acl = acl_clause("c")
    params = acl_params(user)
    conn.execute(text("SET LOCAL hnsw.ef_search = 100"))
    conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    lists: list[tuple[str, list[str]]] = []
    for vec in query_vectors:
        lists.append(("vector", _ranked_ids(conn, f"""
            SELECT c.id FROM chunks c WHERE {acl} AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:v AS halfvec) LIMIT :k""",
            {**params, "v": _vector_literal(vec), "k": top_k})))
    for q in queries:
        tsq = _tsquery(q)
        if tsq:
            lists.append(("keyword", _ranked_ids(conn, f"""
                SELECT c.id FROM chunks c, to_tsquery('simple', :tsq) q
                WHERE {acl} AND c.tsv @@ q ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT :k""",
                {**params, "tsq": tsq, "k": top_k})))
    if asset_ids or codes or parts or doc_numbers:
        lists.append(("exact", _ranked_ids(conn, f"""
            SELECT c.id FROM chunks c WHERE {acl} AND (
                c.asset_ids && CAST(:assets AS text[]) OR c.fault_codes && CAST(:codes AS text[])
                OR c.part_numbers && CAST(:parts AS text[]) OR c.doc_numbers && CAST(:docs AS text[]))
            ORDER BY (c.asset_ids && CAST(:assets AS text[]))::int + (c.fault_codes && CAST(:codes AS text[]))::int
                     + (c.part_numbers && CAST(:parts AS text[]))::int DESC, c.authority_rank, c.event_date DESC NULLS LAST
            LIMIT :k""",
            {**params, "assets": asset_ids, "codes": codes, "parts": parts, "docs": doc_numbers, "k": top_k})))

    fused: dict[str, float] = {}
    origin: dict[str, set] = {}
    for name, ids in lists:
        for rank, cid in enumerate(ids, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            origin.setdefault(cid, set()).add(name)
    if not fused:
        return []

    rows = conn.execute(text(CANDIDATE_SQL + f" WHERE c.id = ANY(CAST(:ids AS uuid[])) AND {acl}"),
                        {**params, "ids": list(fused)}).mappings().all()
    q_models = set(models)
    q_families = {model_family(m) for m in models}
    candidates = []
    for r in rows:
        c = Candidate(**{**{k: r[k] for k in Candidate.__dataclass_fields__ if k in r},
                         "id": str(r["id"]), "document_id": str(r["document_id"])})
        score = fused[c.id]
        chunk_models = set(c.models)
        if q_models and chunk_models:
            if chunk_models & q_models:
                score *= 1.5
            elif {model_family(m) for m in chunk_models} & q_families:
                score *= 0.6
            else:
                score *= 0.3
        score *= AUTHORITY_WEIGHT.get(c.authority_rank, 1.0) * STATUS_WEIGHT.get(c.doc_status, 1.0)
        if asset_ids and set(c.asset_ids) & set(asset_ids):
            score *= 1.5
        if c.document_id in boost_documents:
            score *= 1.4
        c.score = score
        c.sources = origin[c.id]
        candidates.append(c)
    candidates.sort(key=lambda c: c.score, reverse=True)
    # near-duplicate removal (e.g. a quoted email repeated in a case)
    seen, unique = set(), []
    for c in candidates:
        key = re.sub(r"\W+", "", c.text.lower())[:300]
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique[:top_k]


class RerankItem(BaseModel):
    id: int = Field(description="candidate number as given")
    score: int = Field(ge=0, le=3, description="0 irrelevant, 1 related, 2 relevant, 3 directly answers")


class RerankResult(BaseModel):
    items: list[RerankItem]


RERANK_SYSTEM = """You rate how useful each document excerpt is for answering a field technician's question.
Score 3 if it directly answers or gives a required fact/step for the asset/model in the question, 2 if relevant
context, 1 if loosely related, 0 if irrelevant. Excerpts about a different equipment model than the one asked
about score at most 1 unless the question is general. Return a score for every candidate."""


def llm_rerank(question: str, candidates: list[Candidate], limit: int = 30, usage=None) -> list[Candidate]:
    from app.llm.openai_client import get_llm

    pool = candidates[:limit]
    if not pool:
        return []
    listing = "\n\n".join(
        f"#{i} {c.embed_text.splitlines()[0][:220]}\n{c.text[:600]}" for i, c in enumerate(pool)
    )
    result = get_llm().structured(
        "fast", RERANK_SYSTEM, f"Question: {question}\n\nCandidates:\n{listing}", RerankResult, usage=usage
    )
    scores = {item.id: item.score for item in result.items}
    for i, c in enumerate(pool):
        c.rerank = float(scores.get(i, 0))
    pool.sort(key=lambda c: (c.rerank or 0, c.score), reverse=True)
    return pool + candidates[limit:]


@dataclass
class RestrictedHint:
    document_id: str
    title: str
    doc_number: str | None
    missing_groups: list[str]
    disclosure: str  # certification | family
    similarity: float


def restricted_probe(
    conn: Connection, user: UserContext, query_vector: list[float], codes: list[str], models: list[str],
    extra_document_ids: set[str] | None = None, min_similarity: float = 0.45,
) -> tuple[list[RestrictedHint], list[str]]:
    """Search documents the user can't see. Returns (disclosable hints, ids of all relevant restricted chunks).

    Only metadata leaves this function for the prompt; the chunk ids are used by the leak scan.
    """
    rows = conn.execute(
        text(f"""
        SELECT c.id, c.document_id, c.fault_codes, c.models, d.title, d.doc_number, d.required_groups,
               d.acl_status, 1 - (c.embedding <=> CAST(:v AS halfvec)) AS sim
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE {not_acl_clause('c')} AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:v AS halfvec) LIMIT 30"""),
        {**acl_params(user), "v": _vector_literal(query_vector)},
    ).mappings().all()
    hints: dict[str, RestrictedHint] = {}
    relevant_chunks: list[str] = []
    q_codes, q_models = set(codes), set(models)
    for r in rows:
        # When the question names a fault code, a restricted document is on-topic only if it covers that code
        # (HV-PROC-07 is about E-31, not every VRS-110 question); otherwise the model must match.
        on_topic = bool(set(r["fault_codes"]) & q_codes) if q_codes else bool(q_models and set(r["models"]) & q_models)
        relevant = r["sim"] >= min_similarity and (on_topic or r["sim"] >= min_similarity + 0.15)
        if str(r["document_id"]) in (extra_document_ids or set()):
            relevant = True
        if not relevant:
            continue
        relevant_chunks.append(str(r["id"]))
        policy = disclosure(missing_groups(user, list(r["required_groups"])), r["acl_status"])
        if policy == "none":
            continue
        doc_id = str(r["document_id"])
        if doc_id not in hints or hints[doc_id].similarity < r["sim"]:
            hints[doc_id] = RestrictedHint(doc_id, r["title"], r["doc_number"],
                                           missing_groups(user, list(r["required_groups"])), policy, float(r["sim"]))
    return sorted(hints.values(), key=lambda h: -h.similarity), relevant_chunks


def asset_notes(conn: Connection, user: UserContext, asset_ids: list[str], query_vector: list[float],
                limit: int = 4) -> list[str]:
    """Field knowledge about the named assets (site notes, chat, email): often where the root cause lives,
    e.g. 'the TS-2 harness on 0142 runs along the freezer exhaust duct'."""
    if not asset_ids:
        return []
    rows = conn.execute(
        text(f"""SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id
                 WHERE {acl_clause('c')} AND c.asset_ids && CAST(:assets AS text[])
                   AND d.doc_type IN ('wiki', 'chat', 'email') AND c.embedding IS NOT NULL
                 ORDER BY c.embedding <=> CAST(:v AS halfvec) LIMIT :k"""),
        {**acl_params(user), "assets": asset_ids, "v": _vector_literal(query_vector), "k": limit},
    ).all()
    return [str(r[0]) for r in rows]
