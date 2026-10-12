"""Initial schema (spec §7).

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = r"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------- identity ----------
CREATE TABLE groups (
  id          text PRIMARY KEY,
  description text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('region','family','cert','role'))
);

CREATE TABLE users (
  id          text PRIMARY KEY,
  name        text NOT NULL,
  title       text,
  groups      text[] NOT NULL DEFAULT '{}',
  tech_codes  text[] NOT NULL DEFAULT '{}'
);

-- ---------- documents ----------
CREATE TABLE documents (
  id               uuid PRIMARY KEY,
  path             text UNIQUE NOT NULL,
  title            text NOT NULL,
  doc_number       text,
  revision         text,
  effective_date   date,
  source_system    text,
  doc_type         text NOT NULL,
  authority_rank   smallint NOT NULL,
  status           text NOT NULL DEFAULT 'current',
  superseded_by    uuid REFERENCES documents(id),
  models           text[] NOT NULL DEFAULT '{}',
  language         text[] NOT NULL DEFAULT '{en}',
  required_groups  text[] NOT NULL DEFAULT '{}',
  acl_status       text NOT NULL DEFAULT 'listed' CHECK (acl_status IN ('listed','unlisted')),
  sha256           text NOT NULL,
  duplicate_of     uuid REFERENCES documents(id),
  parse_status     text NOT NULL,
  parse_notes      text,
  page_count       int,
  metadata         jsonb NOT NULL DEFAULT '{}',
  ingested_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sections (
  id            uuid PRIMARY KEY,
  document_id   uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  heading_path  text[] NOT NULL,
  section_no    text,
  page_from     int,
  page_to       int,
  ord           int NOT NULL
);

CREATE TABLE chunks (
  id               uuid PRIMARY KEY,
  document_id      uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  section_id       uuid REFERENCES sections(id) ON DELETE CASCADE,
  ord              int NOT NULL,
  kind             text NOT NULL,
  text             text NOT NULL,
  embed_text       text NOT NULL,
  locator          jsonb NOT NULL,
  models           text[] NOT NULL DEFAULT '{}',
  asset_ids        text[] NOT NULL DEFAULT '{}',
  fault_codes      text[] NOT NULL DEFAULT '{}',
  part_numbers     text[] NOT NULL DEFAULT '{}',
  doc_numbers      text[] NOT NULL DEFAULT '{}',
  event_date       date,
  required_groups  text[] NOT NULL,
  acl_status       text NOT NULL,
  authority_rank   smallint NOT NULL,
  doc_status       text NOT NULL,
  flags            text[] NOT NULL DEFAULT '{}',
  tsv              tsvector GENERATED ALWAYS AS (to_tsvector('simple', embed_text)) STORED,
  embedding        halfvec(3072),
  embed_sha256     text
);
CREATE INDEX chunks_doc_idx    ON chunks (document_id);
CREATE INDEX chunks_tsv_idx    ON chunks USING gin (tsv);
CREATE INDEX chunks_trgm_idx   ON chunks USING gin (text gin_trgm_ops);
CREATE INDEX chunks_acl_idx    ON chunks USING gin (required_groups);
CREATE INDEX chunks_models_idx ON chunks USING gin (models);
CREATE INDEX chunks_assets_idx ON chunks USING gin (asset_ids);
CREATE INDEX chunks_codes_idx  ON chunks USING gin (fault_codes);
CREATE INDEX chunks_parts_idx  ON chunks USING gin (part_numbers);
CREATE INDEX chunks_vec_idx    ON chunks USING hnsw (embedding halfvec_cosine_ops);

-- ---------- structured knowledge ----------
CREATE TABLE assets (
  asset_id      text PRIMARY KEY,
  model         text NOT NULL,
  serial        text NOT NULL,
  serial_prefix text NOT NULL,
  serial_num    int  NOT NULL,
  site          text NOT NULL,
  site_name     text,
  customer      text,
  region        text NOT NULL,
  install_date  date,
  firmware      text,
  oil_type      text,
  contract_end  date,
  legacy_ids    text[] NOT NULL DEFAULT '{}',
  source_chunk  uuid REFERENCES chunks(id) ON DELETE SET NULL
);

CREATE TABLE asset_events (
  id              uuid PRIMARY KEY,
  asset_id        text,
  source          text NOT NULL,
  source_ref      text NOT NULL,
  event_date      date,
  event_type      text,
  fault_code      text,
  technician      text,
  notes           text,
  notes_en        text,
  parts           text,
  run_hours       int,
  status          text,
  linked_refs     text[] NOT NULL DEFAULT '{}',
  chunk_id        uuid REFERENCES chunks(id) ON DELETE CASCADE,
  document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  required_groups text[] NOT NULL,
  acl_status      text NOT NULL
);
CREATE INDEX asset_events_asset_idx ON asset_events (asset_id, event_date DESC);

CREATE TABLE bulletins (
  id            uuid PRIMARY KEY,
  document_id   uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  number        text NOT NULL,
  oem           text NOT NULL,
  category      text,
  issue_date    date,
  campaign_code text,
  claim_code    text,
  summary       text
);

CREATE TABLE bulletin_applicability (
  id            uuid PRIMARY KEY,
  bulletin_id   uuid NOT NULL REFERENCES bulletins(id) ON DELETE CASCADE,
  model         text NOT NULL,
  serial_prefix text NOT NULL,
  serial_from   int,
  serial_to     int,
  region        text,
  firmware_lt   text,
  effect        text NOT NULL,
  caveat        text,
  source_chunk  uuid REFERENCES chunks(id) ON DELETE SET NULL
);

CREATE TABLE parts (
  part_no       text NOT NULL,
  description   text,
  models        text[] NOT NULL DEFAULT '{}',
  status        text,
  superseded_by text,
  source_chunk  uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  PRIMARY KEY (part_no, source_chunk)
);

CREATE TABLE conflicts (
  id               text PRIMARY KEY,
  topic            text NOT NULL,
  models           text[] NOT NULL DEFAULT '{}',
  doc_numbers      text[] NOT NULL DEFAULT '{}',
  resolution       text NOT NULL,
  resolution_doc   text
);

-- ---------- conversations, work orders, audit ----------
-- Ownership is structural: composite FKs make cross-user links impossible to store.
CREATE TABLE conversations (
  id         uuid PRIMARY KEY,
  user_id    text NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, user_id)
);

CREATE TABLE messages (
  id                uuid PRIMARY KEY,
  conversation_id   uuid NOT NULL,
  user_id           text NOT NULL,
  question          text NOT NULL,
  answer            jsonb,
  validation        jsonb,
  context_chunk_ids uuid[] NOT NULL DEFAULT '{}',
  restricted_hints  jsonb,
  timings           jsonb,
  model             text,
  tokens            jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (conversation_id, user_id) REFERENCES conversations (id, user_id) ON DELETE CASCADE,
  UNIQUE (id, user_id)
);

CREATE TABLE work_orders (
  id              uuid PRIMARY KEY,
  status          text NOT NULL CHECK (status IN ('draft','pending_approval','approved','rejected','sent')),
  created_by      text NOT NULL REFERENCES users(id),
  source_message  uuid,
  payload         jsonb NOT NULL,
  validation      jsonb NOT NULL DEFAULT '[]',
  approved_by     text REFERENCES users(id),
  approved_at     timestamptz,
  rejected_reason text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (source_message, created_by) REFERENCES messages (id, user_id),
  CHECK (status NOT IN ('approved','sent') OR approved_by IS NOT NULL)
);

CREATE TABLE work_order_events (
  id            bigserial PRIMARY KEY,
  work_order_id uuid NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
  actor         text NOT NULL,
  action        text NOT NULL,
  diff          jsonb,
  at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox (
  id            bigserial PRIMARY KEY,
  work_order_id uuid UNIQUE NOT NULL REFERENCES work_orders(id),
  payload       jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  user_id     text,
  action      text NOT NULL,
  object_type text,
  object_id   text,
  decision    text,
  detail      jsonb
);

CREATE TABLE eval_runs (
  id         uuid PRIMARY KEY,
  started_at timestamptz NOT NULL DEFAULT now(),
  config     jsonb,
  summary    jsonb
);

CREATE TABLE eval_results (
  run_id      uuid NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  question_id text NOT NULL,
  user_id     text NOT NULL,
  answer      jsonb,
  checks      jsonb,
  judge       jsonb,
  verdict     text
);
"""

TABLES = [
    "eval_results", "eval_runs", "audit_log", "outbox", "work_order_events", "work_orders", "messages",
    "conversations", "conflicts", "parts", "bulletin_applicability", "bulletins", "asset_events", "assets",
    "chunks", "sections", "documents", "users", "groups",
]


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
