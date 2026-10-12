# Field Service Copilot

A copilot for Corvane field technicians. It answers questions from Corvane's documents, cites the document and page/section/step/case behind every displayed claim, uses only the sources the asking user may see, says when nothing permitted covers the question, and drafts work orders that a person must approve before anything is sent.

The full design (corpus analysis, decisions, module specs, rubrics) is in [FIELD_SERVICE_COPILOT_SPEC.md](FIELD_SERVICE_COPILOT_SPEC.md). Everything for this project lives in this folder; the given files (`corpus/`, `users.json`, `permissions.json`, `eval/questions.json`) are read in place and never modified.

**Result:** 10 / 10 eval questions correct, 0 permission leaks (`reports/eval_20261012_163100.md`). Leak suite: ACL matrix + 15 probes, no leaks. 49 tests pass.

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Node 20+, an OpenAI API key (the only model provider).

```bash
cp .env.example .env          # then set OPENAI_API_KEY
make db                       # Postgres 16 + pgvector on localhost:55432
make install migrate          # uv sync, npm ci, alembic upgrade
make ingest                   # parse, chunk, OCR, embed (first run ~15 min: OCR of the 8-page scan is the slow part; cached afterwards)
make api                      # FastAPI on :8000    (or: docker compose up -d api)
make web                      # React app on :5173  (or: docker compose up -d web)
```

Open http://localhost:5173, pick a technician, ask. `docker compose up -d --build` runs db + api + web together.

Other commands (all Python goes through uv; run from `backend/` or via `make`):

```bash
make eval                     # 10 eval questions, graded -> reports/eval_<timestamp>.md
make leak-test                # ACL matrix (12 users x 42 docs) + 15 hand-written permission probes
make test                     # 49 tests against a separate copilot_test DB, no LLM calls
cd backend
uv run copilot ask -u u02 "TAC-C-0143 tripped on E-31. How do I reset it?"
uv run copilot ask -u u03 "TAC-C-0143 tripped on E-31. How do I reset it?"   # same question, no HV cert
uv run copilot ingest --path ../new_docs --permissions ../new_docs/permissions.json   # add documents
uv run copilot eval --questions ../new_questions.json --rubrics none                   # judge-only grading
uv run copilot ingest --report  # reports/ingestion_report.md
```

## How it works

```
question ─► analysis (regex entities + gpt-6-luna plan)
         ─► structured lookups: asset (fleet register), bulletins by SERIAL RANGE in code, history (ACL-filtered)
         ─► hybrid search in ONE SQL query per list: pgvector + tsvector + exact codes/parts/assets, all WHERE acl ok
         ─► RRF fusion, re-score (model match, authority, superseded/stale, asset), gpt-6-luna rerank
         ─► + full procedure expansion for how-to questions, + field notes about the named asset
         ─► restricted probe over documents the user CAN'T see -> metadata only (title + missing certification)
         ─► gpt-6.1-sol writes structured JSON; every displayed field carries its own citations
         ─► validator: citations exist, numbers + identifiers appear in the cited source, model guard,
            leak scan against restricted-procedure fingerprints, superseded check -> repair once, else drop/replace
         ─► computed fields (required certification, bulletins, history) + owner-scoped persistence
```

Stack: Python 3.12, FastAPI, SQLAlchemy Core (explicit SQL so the ACL filter is visible in every query), PostgreSQL 16 + pgvector (HNSW over `halfvec(3072)`), PyMuPDF, python-docx, BeautifulSoup, OpenAI (`gpt-6.1-sol` answers, `gpt-6-luna` analysis/rerank, `gpt-6-astra` OCR + eval judge, `text-embedding-3-large`), React 19 + Vite + Tailwind + TanStack Query, Playwright, uv.

## Key decisions

1. **One database.** Documents, chunks, vectors, ACLs, assets, history, work orders: Postgres. Permission filtering is a `WHERE required_groups <@ :user_groups` clause inside the same query as the search, so nothing restricted is fetched and dropped later.
2. **Default deny.** `confluence/VRS-110-VFD-quick-reset-unofficial.html` is not in `permissions.json`. It copies the HV-restricted E-31 reset and says the insulation test can be skipped. Unlisted = denied to everyone; it only shows up in the ingestion report.
3. **Authority in code, not in the prompt.** Bulletin applicability is a serial-range/region/firmware comparison against the fleet register (`config/bulletins.yaml`). Superseded and stale documents are tagged (VRS-75 Rev B, the 2024 wiki quick references) and down-weighted. Known conflicts (KT-WI-14 vs SOP-020, IOM vs SB-2025-009, …) have curated resolutions the answer must agree with.
4. **Validate every displayed field.** Headline, blocks, conflict positions and resolutions, "what you can do", work-order hint fields: each needs its own citations, and every number/unit and identifier must appear in what it cites. A schema test fails if a new field isn't in the validation registry.
5. **Ownership on the server.** A signed session cookie identifies the user. Conversations, messages and work orders are loaded only through owner-scoped queries (not owned = 404), and composite foreign keys make cross-user links impossible to store. A route-table-generated test checks every owned-object endpoint (it caught a 409-vs-404 existence leak on `/reject` during development).
6. **Work orders: rules fill, people decide.** System fields (asset, required certification, permits, campaign code, priority) come from rules; text comes from the validated answer; edits are marked "entered by technician". Errors (missing required certification, wrong campaign code, blocked part, restricted steps) block submit and approval. Nothing reaches the outbox (`var/outbox/<id>.json`, simulated Salesforce) without an approver; a DB check enforces it.
7. **Fixed pipeline, not an agent.** Predictable, testable, explainable in a code walkthrough.

## What the copilot says when you can't see something

| Why you can't see the source | What it does |
|---|---|
| Missing a **certification** (e.g. HV, refrigerant, confined space) | Says the work needs that certification and that a restricted procedure covers it (e.g. HV-PROC-07), explains what the code means and what you **may** do (from sources you can see), offers a work order with that certification. Never any step, value or parameter from the restricted document. |
| Missing an **equipment family** | Says it's covered for equipment you're not trained on; no titles. |
| Missing a **region** or **role-lead** | Behaves as if the document doesn't exist ("no document you can access covers this"). |
| Not in permissions.json | Treated as nonexistent for everyone. |

Why: certification gates exist for safety, and Corvane's own documents (FS-STD-001 §6.4/§7.4, HV-PROC-07 §4, the playbook) tell a non-certified technician exactly this; hiding it would push people towards improvising or towards unofficial copies (one exists). Restricted text never reaches the model: the probe returns metadata only, and a leak scan checks every output field against tokens from the restricted chunks. Lead-only pages name customers, KPIs and certificate numbers, so even their existence stays hidden.

## Corpus issues found and how they're handled

| Issue | Handling |
|---|---|
| Unofficial copy of a restricted procedure, not in permissions.json | default deny, flagged in the ingestion report |
| `Inspection_Checklist_Q3_FINAL.docx` is 0 bytes | parse status `empty` |
| `Vastra_SB-2026-015` PDF truncated | PyMuPDF repair, page 1 recovered, marked `partial`; answers say the file is damaged |
| `NK600_manual_final_v2.pdf` byte-identical to NK-600 Rev C | SHA-256 dedupe, recorded as duplicate |
| HCP-200 manual is an image-only 1998 scan (German/English) | OCR with `gpt-6-astra` to Markdown tables (cached), table rows become their own chunks |
| Same fault code, different meaning per model (HCP-200 P-01 = dry running) | chunks tagged by model; model guard in validation |
| Superseded/stale documents | manifest status, down-weighted, labelled "out of date" in answers |
| Conflicting procedures (KT-WI-14 vs SOP-020) | curated conflict register; answer shows both and the stricter rule |
| Servio export: `;`, Dutch headers, decimal commas, legacy IDs (MFL-KM07) | dedicated parser; legacy IDs mapped to assets; Dutch glossary added to search text |
| MaintPro multi-line notes, short asset IDs ("0142") in chat | robust CSV parsing; suffix resolution against the fleet register |
| Email threads (quoted-printable, newest first, banners) | split into individual messages with sender/date/page |
| Salesforce comment dated before its case | flagged `date_suspect` |

## Evaluation

`make eval` runs each question as its user through the same pipeline and grades it with `eval/rubrics.json`.

**How we decided what counts as right:** an answer is *correct* only if it behaves correctly (answers, says it isn't covered, or restricts, as the reference requires), states every critical fact from the experts' reference answer with the right values, contains nothing on the must-not list, cites at least one expected source, cites nothing the user can't see, and, where follow-up is required, drafts a work order with the expected key fields. Paraphrase is fine; wrong numbers are not. Numbers and forbidden tokens are checked with regular expressions over every rendered field; facts that need understanding are decided by one `gpt-6-astra` judge run. A must-not hit or an inaccessible citation fails the question outright.

| Q | User | Status | Verdict |
|---|---|---|---|
| q01 HCP-500 P-07 | u01 Maria | answered | correct |
| q02 HCP-200 clearance (scanned manual) | u01 Maria | answered | correct |
| q03 VRS-75 oil interval (Rev C vs Rev B) | u03 Priya | answered | correct |
| q04 FRS-P-0031 casing torque (bulletin by serial) | u04 Tom | answered | correct |
| q05 E-31 reset, HV-certified | u02 Daniel | answered | correct |
| q06 E-31 reset, no HV certification | u03 Priya | restricted | correct |
| q07 BX-20 warranty | u12 Olu | not covered | correct |
| q08 TAC-C-0142 E-14 history | u03 Priya | answered | correct |
| q09 RTM-H-0007 L-03 + work order | u05 Sofie | answered | correct |
| q10 BX-12 tracking after belt change | u12 Olu | answered | correct |

**10 / 10 correct, 0 leaks** (final run, `reports/eval_20261012_163100.md`; an earlier full run was 9/10 with q09 partial, fixed by a prompt rule to always explain the fault code).

Extra adversarial set (`eval/extra_questions.json`, judge-only against reference answers, no rubric): **7 / 10** (`reports/eval_20261012_163703.md`). Of the three marked incorrect, none leaked: x06 points to the quarterly ops review because the public FS-STD-001 §13 names it; x07 correctly withholds confined-space entry details that only the restricted CS-PROC-03 contains (the reference answer expected one of them); x04 (an NA user asking about an EU asset) correctly uses no EU sources but mentions checking bulletins generically. Judge-only grading is stricter than the rubric grading.

Permission checks: `make leak-test` (ACL matrix for 12 users × all documents + 15 probes for restricted, other-region, lead-only and unlisted content) and `make test` (ownership, validation, ingestion invariants: 49 tests).

## What we cut

- Real SSO and Salesforce (a signed demo session and an outbox stand in; both are single swap points).
- Full offline mode (Tacoma compressor room B has no signal); answers are slow on purpose-built pipelines (~20-45 s, streamed progress).
- Other model providers (Voyage contextual embeddings/rerank) and provider comparisons; model-generated leak-probe sets and multi-judge voting.
- Admin UI and a side-by-side compare screen (CLI reports instead; switching users shows the difference).
- Certification expiry logic (the matrix is lead-only; `users.json` groups are the proxy), answering in Dutch/Polish, photo understanding.

## With another week

1. Offline-first PWA: prefetch the asset pack for the day's work orders.
2. Bulletin push: when a bulletin is ingested, compute affected assets and notify the team (Lessons Learned item 3: "bulletins don't reach the technician").
3. Latency: stream the answer text, run analysis/embedding in parallel, cache per-asset context.
4. Grow the eval to 100+ cases from resolved cases; model-generated leak probes; judge variance measurement.
5. Try Voyage contextual embeddings + reranker behind an interface; keep only if recall improves.
6. Tool-calling for multi-hop questions; conflict-detection job with an owner workflow.

## What breaks first at 100,000 documents

1. **Ingestion throughput** (synchronous parsing, OCR per scanned page, embedding calls): move to queue workers, batch APIs, OCR only when there's no text layer.
2. **Metadata curation** (the manifest and bulletin ranges are hand-curated): automated extraction with validation and a review queue.
3. **Retrieval precision** (revisions, copies, quoted email threads crowd the top-k): near-duplicate clustering, latest-revision-per-cluster, stronger filters, a cross-encoder reranker.
4. **Filtered ANN recall and memory** (3072-dim halfvec ≈ 6 KB/vector): pgvector iterative scans (on), shorter vectors via `dimensions`, binary quantization with rescoring, partitioning, or a vector store with native filtered HNSW while Postgres stays the system of record.
5. **Permissions as a static file**: sync ACLs from SharePoint/Confluence and re-check at citation time.

## Where AI helped

Built with Claude Code (Anthropic) as a pair programmer:
- Reading and analysing the 42-file corpus, and drafting the spec.
- Boilerplate: loaders, the FastAPI routes, React components, SQL, Dockerfile/Makefile.
- Test scaffolding and the eval/leak harnesses.
- Prompt drafts.

Decided and checked by hand: the access-control and ownership design and its tests, default deny, the restricted-content policy, bulletin applicability ranges, the conflict register, the eval rubrics (from the expert reference answers), and the reading of every eval answer.

## Assumptions

- `users.json` groups decide both what a user may read and, as a proxy, which certified work they may do.
- Work order approval: the brief says "a person", FS-STD-001 says "a supervisor"; `WO_APPROVER_ROLE=any|role-lead` (demo default `any`).
- The fleet register is public (empty groups); asset history stays region-gated.
- Model IDs were checked against OpenAI's docs in October 2026 and are configuration.

---

## Data (from the original package README)

This is a document export from a fictional customer, Corvane Field Services. Corvane services pumps, compressors, chillers and conveyors at four customer sites in North America and Europe. In 2024 it bought a Dutch service company, Kyltek, and the integration isn't finished.

- `corpus/`: 42 files exported from Corvane's systems: manufacturer manuals and service bulletins (PDF), controlled procedures and work instructions (SharePoint, DOCX), the field wiki (Confluence HTML export), emails, a Teams channel export, maintenance-system exports from two different systems, Salesforce case exports, and an ops review. It's a real-world export, so expect some mess.
- `users.json`: the technicians, their groups, and what each group means.
- `permissions.json`: for each document, the groups a user needs. A user may see a document only if they belong to every group in `required_groups`. An empty list means every technician may see it.
- `eval/questions.json`: 10 questions, each asked by a specific user, with a reference answer from Corvane's experts.
