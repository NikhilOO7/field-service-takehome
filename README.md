# Field Service Copilot

Answers a field technician's question from Corvane's documents, using only what that technician may see. Every claim has a citation. When nothing they can see covers the question, it says so. It drafts work orders that a lead must approve.

## Results

- **Official questions:** 10/10 on the latest run (`eval_20261016_180354`). Across all full runs, 8–10. This is in-distribution: the prompt and the curated bulletin table were written while looking at these questions.
- **Held-out questions** (10 we wrote, graded by an LLM judge against reference answers): 6/10 on the latest run, 5–7 across runs. This is the honest generalisation number.
- **Permission leaks:** 0 real leaks in every run. This is checked on the whole model input and the output, not just the citations.
- **Latency:** median about 20 s, worst 37 s. It was 32 s and 77 s before the measured cuts below.

## Scope

The brief is a 6–8 hour build. **The core** is:
- ingestion with permissions
- permission-filtered search
- cited answers with value checks
- the restricted / not-covered behaviour
- a work-order draft with approval
- the eval

**Added beyond the core**, after a security review: per-field validation details, reviewer access rules, a prompt-level permission test and ingestion safety checks. Judge the core on its own.

**Deliberately left out:** real SSO and Salesforce (a signed demo session and an outbox file stand in), offline mode, multi-turn memory (each question stands alone; the UI says so), an admin UI, other model providers, and certification expiry.

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Node 20+, an OpenAI API key.

```bash
cp .env.example .env          # set OPENAI_API_KEY
docker compose up -d --build  # db (:55432) + api (:8000) + web (:5173)
make migrate ingest           # first ingest ~15 min (OCR of one scanned manual), cached afterwards
```

Open http://localhost:5173, pick a technician, ask. After editing `.env`, run `docker compose up -d api`; a plain `restart` keeps the old environment.

```bash
make test        # 77 tests, no LLM calls
make eval        # official questions -> reports/eval_<timestamp>.md
make leak-test   # 12 users × every document, plus 15 permission probes
```

## How it works

1. **Search only what the user may see.** The permission filter is part of the same SQL query as the vector, keyword and exact-ID search, so restricted text is never fetched.
2. **Rerank, then write.** A fast model reranks the top results. The answer model then writes a short answer in which every sentence cites its source.
3. **Check before showing.** Every number, unit and ID must appear in the source it cites; failing fields are dropped. If any step of a procedure fails, the whole procedure is withheld rather than shown with a gap.
4. **Restricted work.** For a missing certification, the copilot names the procedure and the certification but never its content. Documents hidden by region or role are not even acknowledged.
5. **Work orders.** Rules fill the system fields (certification, priority, campaign). The text is drafted when the technician clicks "Create work order". A lead who can read all the evidence approves it.

## What generalises and what needs manual setup

| Works on new documents with no setup | Needs a person |
|---|---|
| Permissions from `permissions.json` (default deny; changes apply on re-ingest) | Registering a bulletin's serial range in `config/bulletins.yaml`. Until then the bulletin is found by search but not checked against the serial |
| Search, citations, value and unit checks | Marking an old revision superseded in `config/corpus_manifest.yaml` |
| **Resolving disagreements between documents by rule**: newer revision over older, an applicable bulletin over the manual, higher authority over lower. The model names the rule and code checks it against document metadata | **Business-judgment overrides** in `config/conflicts.yaml` (e.g. "the stricter rule applies"). Optional: with the register switched off (`CONFLICT_REGISTER=false`) answers rely on the rules alone; the latest run scored 10/10 that way, with the model finding q03 and q09's disagreements by rule |
| Typing new files from folder or name (bulletin, manual, procedure, wiki) | Approver roles and who can review what |

## Latency: what each stage costs and buys

Measured on the 10 official questions. Every variant was a full eval run.

| Change | Median | Worst | Correct | Kept? |
|---|---|---|---|---|
| Before | 32 s | 77 s | 10/10 | |
| Draft the work order only when asked; cap the supporting notes | 26 s | 62 s | 9/10 | yes |
| + low reasoning effort | **18 s** | **31 s** | 10/10 | **yes** |
| Without the reranker (saves 3–4 s) | 21 s | 55 s | 8/10 | no: q10 lost the email with the fix |
| Final default, four later runs | 16–21 s | 28–37 s | 9–10/10 (held-out 6/10) | |

Almost all of the time is the answer model writing, at roughly 45 tokens/s. Creating a work order now takes about 4 s.

## Adding documents

1. Copy the files into the project (e.g. `new_docs/`). PDF, DOCX, HTML, EML, JSON and CSV are supported.
2. Add each file to `permissions.json`, or to a separate file with the same shape kept outside that folder. Keys are paths from the project root. A file with no entry is hidden from everyone.
3. `cd backend && uv run copilot ingest --path ../new_docs --permissions ../new_docs_permissions.json`. The CLI flags permission keys that match no file and suggests the right one.
4. Ask. No restart is needed.

Pass the same permissions file on every later run. A file left out of it becomes hidden. For new bulletins and manual revisions, see the table above.

## What the copilot says when you can't see something

| Why you can't see it | What it does |
|---|---|
| Missing a certification | Says which certification and which procedure. Offers a work order. Lists what you may do now. Nothing from the procedure itself. |
| Missing an equipment family | Says it's covered for equipment you're not trained on. |
| Region, role-lead, unknown group, not in permissions.json | Behaves as if the document doesn't exist. |

The certification case is disclosed because Corvane's own standards tell an uncertified technician exactly this. Hiding it pushes people towards unofficial copies, and one exists in the corpus.

## Evaluation details

A question counts as correct only if:
- its status is right
- every critical fact has the right values
- nothing on the must-not list appears
- an expected source is cited
- there are no permission failures
- the work-order fields and quantities match where one is required

Facts that need understanding are decided by one LLM judge run. A missing verdict fails.

Permission failures are checked three ways: every cited source, everything given to the model (sources, computed facts, conflicts), and a scan of the output for values found only in hidden documents. **Not checked:** whether a citation semantically supports its sentence.

All full runs of the official set:

| Report | Correct | Notes |
|---|---|---|
| `eval_20261012_134813` | 9/10 | q09 didn't explain the fault code (prompt rule added) |
| `eval_20261012_163100` | 10/10 | |
| `eval_20261012_172736` | 8/10 | a brevity rule dropped readings (q04, q08); tightened |
| `eval_20261012_175801` | 9/10 | rubric regex missed "first pass … second pass"; widened |
| `eval_20261012_184550` | 9/10 | rubric regex missed "output current exceeded"; widened |
| `eval_20261012_185500` | 9/10 | reports 1 leak: a false positive on the system's own "24 months" wording, fixed |
| `eval_20261012_190152` | 10/10 | |
| `eval_20261016_174059` | 9/10 | variant A above; q10 answered "partial" |
| `eval_20261016_173928` | 10/10 | variant B (now the default) |
| `eval_20261016_174008` | 8/10 | variant C, without the reranker |
| `eval_20261016_175056` | 8/10 | `.env` still pinned medium effort by mistake; q04, q08 partial |
| `eval_20261016_175030` | 8/10 | same, register off |
| `eval_20261016_175620` | 9/10 | low effort; q09's work order lost its part: the drafted `63-1180 rev 3` didn't match the source verbatim. Part numbers are now normalised before checking |
| `eval_20261016_175619` | 10/10 | low effort, conflict register off |
| `eval_20261016_180354` | 10/10 | final code; median 21 s, worst 37 s; q09 work order has both parts and quantities |

q04 and q08 are the unstable questions. Each sometimes drops one supporting detail.

Held-out runs: 6/10 and 7/10 (the second after the judge was told to accept paraphrase), 5/10 (`eval_20261012_190718`), 7/10 (`eval_20261016_175004`, medium effort) and 6/10 (`eval_20261016_175554`, low effort). None of the misses leaked. Most are the judge wanting a reference item the answer left out.

## Corpus issues handled

- **Unofficial copy of a restricted procedure:** not in permissions.json, so it is hidden from everyone.
- **Empty file:** marked as empty.
- **Truncated bulletin PDF:** page 1 recovered; answers say the file is damaged.
- **Byte-identical manual copy:** deduplicated.
- **Image-only 1998 scanned manual:** OCR'd into tables.
- **Same fault code meaning different things per model:** chunks are tagged by model, and validation rejects values from the wrong model.
- **Dutch maintenance export:** semicolons, decimal commas, legacy IDs (mapped to assets).
- **Short asset IDs in chat:** resolved against the fleet register.
- **Email threads:** split into individual messages.

## With another week

1. Extract bulletin serial ranges and supersession automatically, with a review queue. This removes most of the manual setup.
2. Stream the answer text as it's written.
3. Offline mode: prefetch the documents for the day's jobs.
4. Grow the eval from resolved cases; measure judge variance.

## What breaks first at 100,000 documents

1. **Manual setup:** the bulletin table and supersession don't scale; they need extraction plus review.
2. **Ingestion throughput:** move it to queue workers and batch embedding.
3. **Retrieval precision:** revisions and copies crowd the results, so cluster near-duplicates and keep the latest revision.
4. **Vector memory:** use shorter vectors or quantisation.
5. **Permissions as a static file:** sync them from the source systems.

## Notes

- Stack: Python 3.12, FastAPI, PostgreSQL 16 + pgvector, React, OpenAI (`gpt-6.1-sol` answers, `gpt-6-luna` rerank and work-order drafts, `text-embedding-3-large`), uv.
- `users.json` groups stand in for both reading rights and certifications.
- Approvals go to team leads by default (`WO_APPROVER_ROLE`). Nobody approves their own work order. A reviewer only sees orders whose evidence they can read. When nobody qualifies, the technician is warned. With this org chart that happens for chiller work.
- Built with Claude Code as a pair programmer. The access-control design, the restricted-content policy and the rubrics were decided and checked by hand.
- Full design: [FIELD_SERVICE_COPILOT_SPEC.md](FIELD_SERVICE_COPILOT_SPEC.md).

---

## Data (from the original package README)

This is a document export from a fictional customer, Corvane Field Services. Corvane services pumps, compressors, chillers and conveyors at four customer sites in North America and Europe. In 2024 it bought a Dutch service company, Kyltek, and the integration isn't finished.

- `corpus/`: 42 files exported from Corvane's systems: manufacturer manuals and service bulletins (PDF), controlled procedures and work instructions (SharePoint, DOCX), the field wiki (Confluence HTML export), emails, a Teams channel export, maintenance-system exports from two different systems, Salesforce case exports, and an ops review. It's a real-world export, so expect some mess.
- `users.json`: the technicians, their groups, and what each group means.
- `permissions.json`: for each document, the groups a user needs. A user may see a document only if they belong to every group in `required_groups`. An empty list means every technician may see it.
- `eval/questions.json`: 10 questions, each asked by a specific user, with a reference answer from Corvane's experts.
