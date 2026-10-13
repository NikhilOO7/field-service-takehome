# Field Service Copilot

A copilot for Corvane field technicians. It answers questions from Corvane's documents, cites the document and page/section/step/case behind every displayed claim, uses only the sources the asking user may see, says when nothing permitted covers the question, and drafts work orders that a person must approve before anything is sent.

The full design (corpus analysis, decisions, module specs, rubrics) is in [FIELD_SERVICE_COPILOT_SPEC.md](FIELD_SERVICE_COPILOT_SPEC.md). Everything for this project lives in this folder; the given files (`corpus/`, `users.json`, `permissions.json`, `eval/questions.json`) are read in place and never modified.

**Result:** Official set: **10/10 on the latest run, 8–10 across seven full runs** (q04 and q08 are unstable). 0 permission leaks in every run. The leak count now covers the whole model input and an output scan, not just citations; see [Evaluation](#evaluation). **10/10 is in-distribution:** the honest generalisation number is **5–7/10 on the held-out set** (latest run 5/10), also with 0 leaks. Leak suite: ACL matrix + 15 probes, no leaks. 72 tests pass.

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
make test                     # 72 tests against a separate copilot_test DB, no LLM calls
cd backend
uv run copilot ask -u u02 "TAC-C-0143 tripped on E-31. How do I reset it?"
uv run copilot ask -u u03 "TAC-C-0143 tripped on E-31. How do I reset it?"   # same question, no HV cert
uv run copilot ingest --path ../new_docs --permissions ../new_docs_permissions.json   # add documents (see below)
uv run copilot eval --questions ../new_questions.json --rubrics none                   # judge-only grading
uv run copilot ingest --report  # reports/ingestion_report.md
```

## Adding documents

This was rehearsed end to end: a new wiki page, an email, a new bulletin and a new manual revision, using only the commands below.

1. **Copy the files into the project**, e.g. `new_docs/` (or a folder under `corpus/`). Files outside the project folder are refused with a clear message. Supported: PDF, DOCX, HTML, EML, JSON, CSV.
2. **Say who can see each file.** Add entries to `permissions.json`, or write a separate file in the same shape, kept **outside** the folder being ingested:
   ```json
   {"documents": {"new_docs/Gdansk-Site-Notes.html": {"title": "Gdansk Site Notes", "required_groups": ["region-eu"]}}}
   ```
   Keys are paths from the project root. A file with no entry is hidden from everyone (default deny). Before ingesting, the CLI warns about keys that match no file and suggests the right one ("did you mean new_docs/…?"). It also lists files that will be hidden. Group names with a new prefix (e.g. `site-gdansk`) are enforced like any other group, and the copilot reveals nothing about documents gated by them.
3. **Ingest:** `uv run copilot ingest --path ../new_docs --permissions ../new_docs_permissions.json`. Only new or changed files are parsed and embedded. **Pass the same permissions file on every later run** (or merge it into `permissions.json`): the file is the source of truth. A run without it treats those documents as unlisted and hides them.
4. **Ask.** No restart is needed.

**Permission changes apply without `--full`.** If a file's bytes are unchanged but its permissions changed, the ACL is updated on the document, every chunk and every history row (reported as `acl updated`). A test covers grant → revoke → re-ingest, then checks that search, the source viewer and file download all deny.

**What is not automatic.** Say this before being asked:
- **A new bulletin is found by search, but it gets no serial-range check** until it is registered in `config/bulletins.yaml` (path, model, serial prefix/range, region, firmware, effect). Without that, the model reads the range from the text itself, which is unverified. After registering, re-run ingest: the bulletin becomes a computed, code-checked fact (rehearsed with a made-up SB-2026-021).
- **A new manual revision does not supersede the old one automatically.** Add `superseded_by: <new path>` to the old file's entry in `config/corpus_manifest.yaml`, plus `doc_number`/`revision` for the new file, and re-run ingest. Status changes apply without re-parsing. Doc number and revision changes need `--path <file> --full`. Un-superseding needs a manual fix.
- **Files outside the standard folders are typed from their names** (`SB-yyyy-nnn`/"bulletin" → bulletin, "IOM"/"manual" → manual, `SOP-`/`HV-PROC-` → controlled procedure, `.html` → wiki; revision from `RevC`). The manifest overrides all of this.
- **The conflict register (`config/conflicts.yaml`) is curated.** A new revision that changes a value makes the matching entry stale until it is edited.
- **Deleting a file does not remove it from the database** (`DELETE FROM documents WHERE path = …` cascades).

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
3. **Authority in code, not in the prompt.** Bulletin applicability is a serial-range/region/firmware comparison against the fleet register (`config/bulletins.yaml`). Superseded and stale documents are tagged (VRS-75 Rev B, the 2024 wiki quick references) and down-weighted. Known conflicts (KT-WI-14 vs SOP-020, IOM vs SB-2025-009, …) have curated resolutions the answer must agree with. Each conflict position and resolution names the documents it restates. A conflict reaches the model only if the user can see **every** one of them, because one hidden side would leak through the conflict text. A security review found that a NA chiller technician's prompt contained the EU-only EXV bulletin's threshold and replacement method this way. A test now builds the full model input for several users and fails on any restricted value (`tests/test_prompt_acl.py`).
4. **Validate every displayed field.** Headline, blocks, conflict positions and resolutions, "what you can do", work-order hint fields: each needs its own citations, and every number and identifier must appear in what it cites. **Units must agree:** "38 bar" fails against a source that says "38 N-m", even if the bare 38 appears. Only explicit conversions (s/min/h, g/kg, MΩ/GΩ) and a unit stated in a table header are accepted. **A procedure is never shown with a hole in it:** if any step or safety warning fails validation, all steps are withheld, the answer becomes `partial`, and a notice points to the procedure document. Dropping an unsupported "isolate first" would leave "press RESET". Work-order steps follow the same rule. Limit: matching a value to its source does not prove it refers to the same component or condition. A schema test fails if a new field isn't in the validation registry.
5. **Ownership on the server.** A signed session cookie identifies the user. Conversations, messages and work orders are loaded only through owner-scoped queries (not owned = 404), and composite foreign keys make cross-user links impossible to store. A route-table-generated test checks every owned-object endpoint (it caught a 409-vs-404 existence leak on `/reject` during development).
6. **Work orders: rules fill, people decide.** System fields (asset, required certification, permits, campaign code, priority) come from rules; text comes from the validated answer; edits are marked "entered by technician". Errors (missing required certification, wrong campaign code, blocked part, restricted steps) block submit and approval. Nothing reaches the outbox (`var/outbox/<id>.json`, simulated Salesforce) without an approver; a DB check enforces it. Approval is four-eyes (nobody approves or rejects their own work order) and defaults to team leads (`WO_APPROVER_ROLE=role-lead`). **Approval authority is not read access:** a reviewer sees a pending work order only if they could read every chunk its answer was built from and the machine is in their region. Otherwise the queue would carry one technician's permitted evidence to another. Decisions take a row lock, so concurrent approve/reject requests have one winner. The outbox file is written only after the transaction commits.
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

**How we decided what counts as right:** an answer is *correct* only if it behaves correctly (answers, says it isn't covered, or restricts, as the reference requires), states every critical fact from the experts' reference answer with the right values, contains nothing on the must-not list, cites at least one expected source, and, where follow-up is required, drafts a work order with the expected key fields **and quantities**. Paraphrase is fine; wrong numbers are not. Numbers and forbidden tokens are checked with regular expressions over every rendered field. Facts that need understanding are decided by one `gpt-6-astra` judge run; **a judge item with no verdict fails**. Any must-not hit or permission failure fails the question outright.

**Permission leaks** = three checks per question:
1. Every cited source is visible to the user.
2. **Everything the model was given** is visible: all sources, the chunks behind each computed fact, and every document behind each conflict in the context.
3. A scan of the displayed answer and work order for distinctive values (numbers with units, test points, parameters) that occur only in documents the user can't see.

The first version counted check 1 only. That missed the conflict-register bypass described under Key decisions, which no run triggered in its output.

**Not checked:** that each citation *semantically* supports its sentence. Values, units and identifiers must appear in the cited source, but that does not prove they refer to the same component or condition.

### Results across runs

The answers are not deterministic, so here is every full run of the official set, not just the best one:

| Report | Correct | Notes |
|---|---|---|
| `eval_20261012_134813` | 9/10 | q09 partial: fault code not explained (fixed with a prompt rule) |
| `eval_20261012_163100` | 10/10 | |
| `eval_20261012_172736` | 8/10 | after a "brevity" prompt change that dropped readings and method details (q04, q08); rule tightened |
| `eval_20261012_175801` | 9/10 | q04: "first pass … second pass" missed by the "two passes" regex |
| `eval_20261012_184550` | 9/10 | q06: "output current exceeded the drive's peak limit" missed by the "overcurrent" regex |
| `eval_20261012_185500` | 9/10 | q08: the new output scan flagged "24 months", the system's own history-window wording; a false positive, fixed. Also q08 omitted the TS-2 reading |
| `eval_20261012_190152` | 10/10 | latest, with every fix above; q04's supporting "45 N-m for earlier serials" item still missing |

**Summary: 8–10 correct across full runs, 0 leaks in every run.** Two questions are unstable:
- **q04** sometimes drops the "45 N-m still applies to earlier serials" scope sentence.
- **q08** sometimes drops "TS-2 read 11–16 °C above the handheld probe".

Two rubric regexes were too narrow for equivalent wording and were widened without weakening the requirement: "two passes" now also matches "first pass … second pass"; "overcurrent" also matches "output current exceeded".

**10/10 is in-distribution.** The prompt's examples echo the eval questions ("a BX-12 value for a BX-20 question", "a shim thickness", "the 45 N-m still applies to serials before X"). `config/bulletins.yaml` and `config/conflicts.yaml` encode the expert resolutions. The honest generalisation number is the held-out set below. The curated tables are the part that doesn't scale (see "What breaks first").

**Held-out set** (`eval/extra_questions.json`: 10 questions we wrote from the same corpus, separate from the official set and with no rubric, graded judge-only against reference answers. They are held-out *questions*, not held-out *data*.): **5–7/10 across runs**, 0 leaks in every run. History:
- `eval_20261012_162053`: 6/10 with the first judge prompt.
- `eval_20261012_163703`: 7/10 after the judge prompt was changed to accept paraphrase and the omission of details only restricted documents contain.
- `eval_20261012_190718`: 5/10 (latest). x03, x05 and x07 flipped: the judge wanted reference items the answers left out (reading the controller values, "ask Vastra for a new copy", "work from the deck needs no entry").

None of the misses leaked. Examples:
- **x07** correctly withholds confined-space entry details that only the restricted CS-PROC-03 contains (the reference answer expected one of them).
- **x06** points to the quarterly ops review because the public FS-STD-001 §13 names it.
- **x04** (an NA user asking about an EU unit) tells the user to check the serial against open bulletins and campaigns. The judge reads that as restricted guidance; the output scan finds no restricted values in it.

Permission tests: `make leak-test` (ACL matrix for 12 users × all documents + 15 probes for restricted, other-region, lead-only and unlisted content) and `make test` (72 tests). The tests cover:
- ownership, four-eyes approval, reviewer read access and concurrent decisions
- the full-prompt ACL test
- unit checks, procedure withholding
- the permission-refresh sequence and unknown group kinds

## What we cut

- Real SSO and Salesforce (a signed demo session and an outbox stand in; both are single swap points).
- Full offline mode (Tacoma compressor room B has no signal).
- Latency work. Measured in the latest run: 11–44 s for 8 of 10 questions, almost all of it the one answer-model call (`gpt-6.1-sol`, long structured output). q09 and q10 took 92–130 s because validation triggered the repair call, a second full generation at high effort (+50–70 s). Progress is streamed; per-stage timings are stored with every answer.
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
2. **Metadata curation.** The manifest, bulletin serial ranges and conflict register are hand-written, and they are where the expert resolutions live. That doesn't scale, and it's why the 10/10 is in-distribution. At 100k documents they become extracted metadata (bulletin ranges from the bulletin text, supersession from document number + revision, conflicts from value disagreements) with a human review queue.
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
- Work order approval: the brief says "a person", FS-STD-001 says "a supervisor"; `WO_APPROVER_ROLE=role-lead` (default) or `any`. Leads: Kenji (NA) and Lena (EU). A pending order no lead can fully read (e.g. a chiller order in NA, where no lead has chiller access) has no eligible reviewer; that is reported rather than widened.
- Each question is answered on its own. Earlier messages are stored but not used, so follow-ups must repeat the machine ID; the UI says so.
- The fleet register is public (empty groups); asset history stays region-gated.
- Model IDs were checked against OpenAI's docs in October 2026 and are configuration.

---

## Data (from the original package README)

This is a document export from a fictional customer, Corvane Field Services. Corvane services pumps, compressors, chillers and conveyors at four customer sites in North America and Europe. In 2024 it bought a Dutch service company, Kyltek, and the integration isn't finished.

- `corpus/`: 42 files exported from Corvane's systems: manufacturer manuals and service bulletins (PDF), controlled procedures and work instructions (SharePoint, DOCX), the field wiki (Confluence HTML export), emails, a Teams channel export, maintenance-system exports from two different systems, Salesforce case exports, and an ops review. It's a real-world export, so expect some mess.
- `users.json`: the technicians, their groups, and what each group means.
- `permissions.json`: for each document, the groups a user needs. A user may see a document only if they belong to every group in `required_groups`. An empty list means every technician may see it.
- `eval/questions.json`: 10 questions, each asked by a specific user, with a reference answer from Corvane's experts.
