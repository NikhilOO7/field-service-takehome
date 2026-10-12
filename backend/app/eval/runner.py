"""Evaluation harness (spec M15).

Each question runs through the same ask pipeline as its user. Grading:
  - deterministic checks (status, regex facts, must-not tokens, citations, ACL of citations, work order)
    run over EVERY rendered field, not just the answer blocks;
  - one judge call (judge model, single run) for facts marked judge=true.
A question is CORRECT only if every check passes; a must-not hit or an inaccessible citation is always
INCORRECT. Results go to reports/eval_<timestamp>.md/.json and the eval tables.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import Engine, text

from app.answer.service import ask
from app.auth.acl import acl_clause, acl_params
from app.auth.users import get_user
from app.config import Settings
from app.llm.openai_client import get_llm

HOW_GRADED = (
    "An answer is **correct** only if it behaves correctly (answers, says it isn't covered, or restricts, as the "
    "reference requires), states every critical fact from the experts' reference answer with the right values, "
    "contains nothing on the must-not list, cites at least one of the expected sources, cites nothing the user "
    "can't see, and, where follow-up is required, produces a work order draft with the expected key fields. "
    "Paraphrase is fine; wrong numbers are not. Checks run over every rendered field (headline, blocks, "
    "conflicts, what-you-can-do, work order). Numbers and forbidden tokens are checked with regular "
    "expressions; facts that need understanding are decided by a single LLM judge run. **Partially correct** = "
    "right behaviour, no must-not hit, at least half of the critical facts."
)


class JudgeItem(BaseModel):
    id: str
    holds: bool
    evidence: str


class JudgeResult(BaseModel):
    items: list[JudgeItem]


JUDGE_SYSTEM = """You grade a field-service copilot answer against expert expectations. For each item decide
strictly whether it holds for the ANSWER TEXT. Paraphrase is fine; numbers and units must match exactly.
For 'fact' items: holds = the answer states this fact. For 'violation' items: holds = the answer commits
this violation. Quote the evidence (or say 'not found')."""


def rendered_text(response: dict, wo_payload: dict | None) -> str:
    a = response["answer"]
    parts = [a["headline"]["text"]]
    parts += [b["text"] for b in a["blocks"]]
    for c in a["conflicts"]:
        parts += [c["topic"]] + [p["text"] for p in c["positions"]] + [c["resolution"]["text"]]
    if a.get("restricted_notice"):
        parts += [i["text"] for i in a["restricted_notice"]["what_you_can_do"]]
    if a.get("clarify_question"):
        parts.append(a["clarify_question"]["text"])
    if wo_payload:
        for k, v in wo_payload.items():
            if not k.startswith("_") and v.get("value") not in (None, [], ""):
                parts.append(f"{k}: {json.dumps(v['value'], default=str)}")
    return "\n".join(parts)


def cited_labels(response: dict) -> list[str]:
    text_ = json.dumps(response["answer"])
    ids = set(re.findall(r'"(S\d+|F\d+)"', text_))
    labels = [s["label"] for s in response["sources"] if s["id"] in ids]
    labels += [f["label"] for f in response["facts"] if f["id"] in ids]
    return labels


def cited_chunk_ids(response: dict) -> list[str]:
    ids = set(re.findall(r'"(S\d+)"', json.dumps(response["answer"])))
    return [s["chunk_id"] for s in response["sources"] if s["id"] in ids]


@dataclass
class QuestionResult:
    id: str
    user_id: str
    question: str
    status: str
    verdict: str = "incorrect"
    checks: list[dict] = field(default_factory=list)
    response: dict | None = None
    work_order: dict | None = None
    latency_s: float = 0.0

    def add(self, name: str, ok: bool, detail: str = "", critical: bool = True) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical})


def _matches(patterns: list[str], text_: str, any_: bool = False) -> bool:
    hits = [bool(re.search(p, text_, re.I | re.S)) for p in patterns]
    return any(hits) if any_ else all(hits)


def grade(engine: Engine, q: dict, rubric: dict | None, response: dict, wo_payload: dict | None,
          user) -> QuestionResult:
    a = response["answer"]
    r = QuestionResult(q["id"], q["user_id"], q["question"], a["status"], response=response,
                       work_order=wo_payload, latency_s=response.get("timings", {}).get("validate", 0))
    body = rendered_text(response, wo_payload)
    labels = cited_labels(response)
    judge_items = []
    if rubric is None:
        judge_items.append({"id": "reference", "kind": "fact",
                            "text": "The answer covers the key points of this reference answer and does not contradict "
                                    "it. Paraphrase, extra correct detail from the documents, and leaving out details "
                                    "that only restricted documents contain are acceptable. Reference: "
                                    + q["reference_answer"]})
    else:
        expected = rubric.get("expected_status", [])
        r.add("status", a["status"] in expected, f"{a['status']} (expected {expected})")
        critical_total = 0
        for f in rubric.get("critical_facts", []) + rubric.get("supporting_facts", []):
            critical = f in rubric.get("critical_facts", [])
            critical_total += critical
            if f.get("judge"):
                judge_items.append({"id": f["id"], "kind": "fact", "text": f["fact"], "critical": critical})
            else:
                r.add(f"fact:{f['id']}", _matches(f["patterns"], body, f.get("any", False)), f["fact"], critical)
        for m in rubric.get("must_not", []):
            if m.get("patterns"):
                hit = [p for p in m["patterns"] if re.search(p, body, re.I)]
                r.add(f"must_not:{m['id']}", not hit, f"{m['desc']}; hits: {hit}")
            if m.get("cites"):
                hit = [c for c in m["cites"] for lab in labels if c.lower() in lab.lower()]
                r.add(f"must_not_cite:{m['id']}", not hit, f"{m['desc']}; cited: {hit}")
            if m.get("judge"):
                judge_items.append({"id": m["id"], "kind": "violation", "text": m["desc"], "critical": True})
        cite_any = rubric.get("must_cite_any", [])
        if cite_any:
            ok = any(c.lower() in lab.lower() for c in cite_any for lab in labels)
            r.add("cites_expected_source", ok, f"expected one of {cite_any}; cited {labels[:8]}")
        wo_rule = rubric.get("work_order")
        if wo_rule and wo_rule.get("required", True):
            if not wo_payload:
                r.add("work_order", False, "no work order draft")
            else:
                v = {k: x.get("value") for k, x in wo_payload.items()}
                problems = []
                if wo_rule.get("asset_id") and v.get("asset_id") != wo_rule["asset_id"]:
                    problems.append(f"asset {v.get('asset_id')}")
                if wo_rule.get("fault_code") and v.get("fault_code") != wo_rule["fault_code"]:
                    problems.append(f"fault {v.get('fault_code')}")
                for c in wo_rule.get("certifications_include", []):
                    if c not in (v.get("required_certifications") or []):
                        problems.append(f"missing cert {c}")
                for p in wo_rule.get("parts_include", []):
                    if p not in [x.get("part_no") for x in v.get("parts") or []]:
                        problems.append(f"missing part {p}")
                if wo_rule.get("campaign_code") and v.get("campaign_code") != wo_rule["campaign_code"]:
                    problems.append(f"campaign {v.get('campaign_code')}")
                r.add("work_order", not problems, "; ".join(problems) or "fields match")

    # every cited chunk must be visible to the asking user
    chunks = cited_chunk_ids(response)
    if chunks:
        with engine.connect() as conn:
            visible = {str(x[0]) for x in conn.execute(
                text(f"SELECT c.id FROM chunks c WHERE c.id = ANY(CAST(:ids AS uuid[])) AND {acl_clause('c')}"),
                {"ids": chunks, **acl_params(user)})}
        r.add("acl_of_citations", set(chunks) <= visible, f"{len(set(chunks) - visible)} inaccessible")

    if judge_items:
        listing = "\n".join(f"- id={j['id']} [{j['kind']}] {j['text']}" for j in judge_items)
        result = get_llm().structured(
            "judge", JUDGE_SYSTEM,
            f"QUESTION: {q['question']}\nEXPERT REFERENCE: {q['reference_answer']}\n\nANSWER TEXT:\n{body}\n\n"
            f"ITEMS:\n{listing}", JudgeResult)
        by_id = {i.id: i for i in result.items}
        for j in judge_items:
            item = by_id.get(j["id"])
            holds = bool(item and item.holds)
            ok = holds if j["kind"] == "fact" else not holds
            r.add(f"judge:{j['id']}", ok, f"{j['text']} | {item.evidence if item else 'no verdict'}",
                  j.get("critical", True))

    failed_critical = [c for c in r.checks if c["critical"] and not c["ok"]]
    hard_fail = any(c["check"].startswith(("must_not", "acl_of_citations")) and not c["ok"] for c in r.checks)
    fact_checks = [c for c in r.checks if c["check"].startswith(("fact:", "judge:")) and c["critical"]
                   and not c["check"].startswith("judge:" + "x")]
    facts_ok = sum(c["ok"] for c in fact_checks)
    status_ok = all(c["ok"] for c in r.checks if c["check"] == "status")
    if not failed_critical:
        r.verdict = "correct"
    elif not hard_fail and status_ok and fact_checks and facts_ok / len(fact_checks) >= 0.5:
        r.verdict = "partial"
    else:
        r.verdict = "incorrect"
    return r


def preview_work_order(engine: Engine, user, message_id: str) -> dict | None:
    """Draft a work order inside a transaction that is rolled back: graded, not saved."""
    from app.workorders.service import WorkOrderError, create_from_message

    conn = engine.connect()
    trans = conn.begin()
    try:
        wo = create_from_message(conn, user, message_id)
        return wo["payload"] | {"_validation": {"value": wo["validation"]}}
    except WorkOrderError:
        return None
    finally:
        trans.rollback()
        conn.close()


def run_eval(engine: Engine, settings: Settings, questions_file: Path, rubrics_file: Path | None,
             only: list[str] | None = None, progress=None) -> dict:
    questions = json.loads(questions_file.read_text())
    rubrics = json.loads(rubrics_file.read_text()) if rubrics_file and rubrics_file.exists() else {}
    results: list[QuestionResult] = []
    for q in questions:
        if only and q["id"] not in only:
            continue
        with engine.connect() as conn:
            user = get_user(conn, q["user_id"])
        if progress:
            progress(f"{q['id']} ({user.name}): {q['question']}")
        response = ask(engine, user, q["question"]).response
        rubric = rubrics.get(q["id"])
        needs_wo = (rubric or {}).get("work_order") or response["answer"]["follow_up"]["needed"]
        wo_payload = preview_work_order(engine, user, response["message_id"]) if needs_wo else None
        results.append(grade(engine, q, rubric, response, wo_payload, user))

    summary = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "correct": sum(r.verdict == "correct" for r in results),
        "partial": sum(r.verdict == "partial" for r in results),
        "incorrect": sum(r.verdict == "incorrect" for r in results),
        "leaks": sum(1 for r in results for c in r.checks if c["check"] in {"acl_of_citations"} and not c["ok"]),
        "models": {"answer": settings.answer_model, "fast": settings.fast_model, "judge": settings.judge_model,
                   "embed": settings.embed_model},
    }
    run_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO eval_runs (id, config, summary) VALUES (:id, CAST(:c AS jsonb), "
                          "CAST(:s AS jsonb))"),
                     {"id": run_id, "c": json.dumps({"questions": str(questions_file)}), "s": json.dumps(summary)})
        for r in results:
            conn.execute(text("""INSERT INTO eval_results (run_id, question_id, user_id, answer, checks, verdict)
                                 VALUES (:r, :q, :u, CAST(:a AS jsonb), CAST(:c AS jsonb), :v)"""),
                         {"r": run_id, "q": r.id, "u": r.user_id, "a": json.dumps(r.response, default=str),
                          "c": json.dumps(r.checks), "v": r.verdict})
    path = write_report(results, summary, settings)
    return {"summary": summary, "report": str(path), "results": results}


def write_report(results: list[QuestionResult], summary: dict, settings: Settings) -> Path:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md = settings.reports_dir / f"eval_{stamp}.md"
    lines = [
        "# Evaluation report", "",
        f"**{summary['correct']} / {summary['total']} correct** ({summary['partial']} partially correct, "
        f"{summary['incorrect']} incorrect). Permission leaks: {summary['leaks']}.", "",
        f"Run at {summary['run_at']} · models: {summary['models']}", "",
        "## How we decided what counts as right", "", HOW_GRADED, "",
        "## Results", "",
        "| Q | User | Status | Verdict | Failed checks | Latency (s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        failed = "; ".join(c["check"] for c in r.checks if not c["ok"]) or "—"
        lines.append(f"| {r.id} | {r.user_id} | {r.status} | **{r.verdict}** | {failed} | {r.latency_s} |")
    lines += ["", "## Details", ""]
    for r in results:
        a = r.response["answer"]
        lines += [f"### {r.id} · {r.user_id} · {r.verdict}", "", f"> {r.question}", "",
                  f"**{a['status']}** — {a['headline']['text']}", ""]
        lines += [f"- {b['text']}" for b in a["blocks"]]
        lines += ["", "Checks:", ""]
        lines += [f"- {'✅' if c['ok'] else '❌'} `{c['check']}` {c['detail'][:300]}" for c in r.checks]
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")
    (settings.reports_dir / f"eval_{stamp}.json").write_text(
        json.dumps({"summary": summary, "results": [r.__dict__ for r in results]}, default=str, indent=2))
    return md
