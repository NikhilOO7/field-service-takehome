"""Command line interface: `uv run copilot <command>` (from backend/)."""

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings, resolve_inside
from app.db.session import get_engine

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Field Service Copilot")
console = Console()


def project_path(value: Path) -> Path:
    """Accept a path relative to the current directory or to the project root; it must stay inside the project."""
    settings = get_settings()
    candidate = value if value.is_absolute() else (Path.cwd() / value)
    if not candidate.exists():
        candidate = settings.project_root / value
    return resolve_inside(settings.project_root, candidate.resolve())


def check_permission_keys(settings, perms_file: Path, only: Path | None) -> None:
    """Warn before ingesting if permission keys don't match file paths: unmatched files are denied to everyone."""
    from app.ingestion.manifest import discover, load_permissions

    keys = set(load_permissions(perms_file))
    if not keys:
        console.print(f"[red]{perms_file.name} has no entries under \"documents\"; "
                      "expected {\"documents\": {\"<path from the project root>\": {...}}}[/red]")
        return
    found = {s.path for s in discover(settings, only=only)}
    by_name = {Path(p).name: p for p in found}
    for key in sorted(keys - found):
        hint = f" — did you mean [bold]{by_name[Path(key).name]}[/bold]?" if Path(key).name in by_name else ""
        console.print(f"[red]Permission key matches no file: {key}{hint}[/red]")
    unlisted = sorted(found - keys - set(load_permissions(settings.permissions_file)))
    for p in unlisted:
        console.print(f"[yellow]No permission entry for {p}: it will be hidden from every user.[/yellow]")
    console.print("Keys are paths relative to the project root, e.g. new_docs/my_file.pdf or corpus/manuals/x.pdf.")


@app.command()
def ingest(
    full: bool = typer.Option(False, help="Re-ingest every file, even if unchanged."),
    path: Path | None = typer.Option(None, help="Only ingest files under this folder (inside the project)."),
    permissions: Path | None = typer.Option(None, help="Extra permissions JSON for new documents."),
    report: bool = typer.Option(True, help="Write reports/ingestion_report.md"),
    embed: bool = typer.Option(True, help="Embed new/changed chunks (needs OPENAI_API_KEY)."),
):
    """Load the corpus into Postgres: parse, chunk, enrich, store, embed."""
    from app.ingestion.pipeline import ingest as run_ingest
    from app.ingestion.pipeline import write_report

    settings = get_settings()
    try:
        only = project_path(path) if path else None
        perms = project_path(permissions) if permissions else None
    except ValueError as e:
        console.print(f"[red]{e}[/red]\nCopy the files into the project first (e.g. new_docs/), then pass that folder.")
        raise typer.Exit(1) from e
    if perms:
        check_permission_keys(settings, perms, only)
    result = run_ingest(get_engine(), settings, full=full, only=only, permissions_file=perms, embed=embed)

    table = Table(title="Ingestion")
    for col in ("File", "Result", "Parse", "Access", "Chunks", "Notes"):
        table.add_column(col, overflow="fold")
    for r in sorted(result.results, key=lambda r: r.path):
        style = "red" if r.status == "failed" or r.acl == "unlisted" or r.parse_status in {"empty", "ocr_pending"} \
            else ("yellow" if r.notes else None)
        table.add_row(r.path.removeprefix("corpus/"), r.status, r.parse_status, r.acl, str(r.chunks),
                      "; ".join(r.notes), style=style)
    console.print(table)
    console.print(f"Embedded {result.embedded} chunks; {result.embedding_pending} still without embeddings.")
    if not result.llm_available:
        console.print("[yellow]OPENAI_API_KEY not set: embeddings and OCR of scanned pages are pending.[/yellow]")
    if report:
        console.print(f"Report: {write_report(result, settings)}")


def print_answer(resp: dict) -> None:
    a = resp["answer"]
    colour = {"answered": "green", "partial": "yellow", "restricted": "dark_orange", "not_covered": "grey50",
              "clarify": "blue"}.get(a["status"], "white")
    labels = {s["id"]: s["label"] for s in resp["sources"]} | {f["id"]: f["label"] for f in resp["facts"]}

    def cite(ids):
        return " " + " ".join(f"[{i}]" for i in ids) if ids else ""

    console.print(f"\n[bold {colour}]{a['status'].upper()}[/] [bold]{a['headline']['text']}[/]"
                  f"{cite(a['headline']['citations'])}")
    if resp.get("restricted"):
        console.print(f"[dark_orange]Needs certification: {', '.join(resp['restricted']['required_certifications'])}"
                      f" — {', '.join(p['title'] for p in resp['restricted']['procedures'])}[/]")
    for b in a["blocks"]:
        marker = {"step": "→", "warning": "⚠", "history": "⏱", "fact": "•", "note": "·"}[b["type"]]
        console.print(f" {marker} {b['text']}{cite(b['citations'])}")
    if a.get("restricted_notice"):
        console.print("[bold]What you can do:[/]")
        for item in a["restricted_notice"]["what_you_can_do"]:
            console.print(f"   - {item['text']}{cite(item['citations'])}")
    for c in a["conflicts"]:
        console.print(f"[yellow]Conflict ({c['conflict_id']}): {c['topic']}[/]")
        for p in c["positions"]:
            console.print(f"   - {p['text']}{cite(p['citations'])}")
        console.print(f"   = {c['resolution']['text']}{cite(c['resolution']['citations'])}")
    if a.get("clarify_question"):
        console.print(f"[blue]? {a['clarify_question']['text']}[/]")
    fu = a["follow_up"]
    if fu["needed"]:
        console.print(f"[magenta]Work order suggested: {', '.join(fu['reasons'])}[/]")
    cited = sorted({c for c in labels if c in str(a)}, key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0))
    console.print("[dim]Sources:[/]")
    for cid in cited:
        console.print(f"[dim]  [{cid}] {labels[cid]}[/]")
    dropped = {p: v for p, v in resp["validation"].items() if v["status"] != "ok"}
    if dropped:
        console.print(f"[dim]validation: {dropped}[/]")
    console.print(f"[dim]timings {resp['timings']} confidence {a['confidence']}[/]")


@app.command()
def ask(
    question: str = typer.Argument(..., help="The technician's question"),
    user: str = typer.Option(..., "--user", "-u", help="User id, e.g. u03"),
    show_json: bool = typer.Option(False, "--json", help="Print the raw response JSON"),
):
    """Ask a question as a user and print the cited answer."""
    import json

    from app.answer.service import ask as run_ask
    from app.auth.users import get_user

    engine = get_engine()
    with engine.connect() as conn:
        ctx = get_user(conn, user)
    if ctx is None:
        raise typer.BadParameter(f"unknown user {user}")
    console.print(f"[bold]{ctx.name}[/] ({', '.join(sorted(ctx.groups))}) asks: {question}")
    result = run_ask(engine, ctx, question)
    if show_json:
        console.print_json(json.dumps(result.response, default=str))
    else:
        print_answer(result.response)


@app.command("eval")
def eval_cmd(
    questions: Path | None = typer.Option(None, help="Questions JSON (default eval/questions.json)"),
    rubrics: Path | None = typer.Option(None, help="Rubrics JSON (default eval/rubrics.json; none = judge-only)"),
    only: str | None = typer.Option(None, help="Comma-separated question ids, e.g. q01,q06"),
):
    """Run the eval questions through the copilot and grade them."""
    from app.eval.runner import run_eval

    settings = get_settings()
    qfile = project_path(questions) if questions else settings.questions_file
    if rubrics is not None and str(rubrics) == "none":
        rfile = None  # judge-only: each answer is graded against the question's reference_answer
    else:
        rfile = project_path(rubrics) if rubrics else settings.project_root / "eval" / "rubrics.json"
    out = run_eval(get_engine(), settings, qfile, rfile, only.split(",") if only else None,
                   progress=lambda m: console.print(f"[dim]running {m}[/]"))
    table = Table(title="Evaluation")
    for col in ("Q", "User", "Status", "Verdict", "Failed checks"):
        table.add_column(col, overflow="fold")
    for r in out["results"]:
        colour = {"correct": "green", "partial": "yellow", "incorrect": "red"}[r.verdict]
        table.add_row(r.id, r.user_id, r.status, f"[{colour}]{r.verdict}[/]",
                      "; ".join(c["check"] for c in r.checks if not c["ok"]) or "—")
    console.print(table)
    s = out["summary"]
    console.print(f"[bold]{s['correct']} / {s['total']} correct[/] ({s['partial']} partial). Leaks: {s['leaks']}. "
                  f"Report: {out['report']}")
    if s["leaks"]:
        raise typer.Exit(1)


@app.command("leak-test")
def leak_test(
    probes: Path | None = typer.Option(None, help="Probe questions JSON (default eval/leak_probes.json)"),
    skip_probes: bool = typer.Option(False, help="Only run the deterministic ACL matrix (no LLM calls)."),
):
    """Permission leak suite: ACL matrix for every user x document, then hand-written probe questions."""
    from app.eval.leak_suite import acl_matrix, run_probes

    settings = get_settings()
    problems = acl_matrix(get_engine(), settings.users_file, settings.permissions_file)
    console.print("[green]ACL matrix: all users x documents match permissions.json[/]" if not problems
                  else f"[red]ACL matrix mismatches: {problems}[/]")
    failures = len(problems)
    if not skip_probes:
        pfile = project_path(probes) if probes else settings.project_root / "eval" / "leak_probes.json"
        results = run_probes(get_engine(), pfile, progress=lambda m: console.print(f"[dim]probing {m}[/]"))
        table = Table(title="Leak probes")
        for col in ("Probe", "User", "Status", "Result", "Details"):
            table.add_column(col, overflow="fold")
        for r in results:
            detail = "; ".join(f"{k}: {r[k]}" for k in ("forbidden_in_context", "forbidden_cited", "forbidden_tokens")
                               if r[k]) or (f"disclosed: {[p['title'] for p in r['restricted_hint']]}"
                                            if r["restricted_hint"] else "—")
            table.add_row(r["id"], r["user"], r["status"], "[green]ok[/]" if r["ok"] else "[red]LEAK[/]", detail)
        console.print(table)
        failures += sum(not r["ok"] for r in results)
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        (settings.reports_dir / "leak_test.json").write_text(json.dumps(results, indent=2))
    console.print(f"[bold]{'No leaks' if not failures else f'{failures} failures'}[/]")
    if failures:
        raise typer.Exit(1)


@app.command()
def users():
    """List the demo users and their groups."""
    from app.auth.users import list_users

    with get_engine().connect() as conn:
        rows = list_users(conn)
    table = Table(title="Users")
    for col in ("ID", "Name", "Title", "Groups"):
        table.add_column(col)
    for u in rows:
        table.add_row(u["id"], u["name"], u["title"] or "", ", ".join(u["groups"]))
    console.print(table)


if __name__ == "__main__":
    app()
