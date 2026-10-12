"""Test fixtures. Tests run against the separate copilot_test database (created by docker compose)."""

import subprocess

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import make_engine


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def engine(settings):
    subprocess.run(["uv", "run", "alembic", "-x", f"url={settings.test_database_url}", "upgrade", "head"],
                   check=True, capture_output=True)
    eng = make_engine(settings.test_database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def ingested(engine, settings):
    """Ingest the corpus into the test DB once (incremental: unchanged files are skipped, no embeddings)."""
    from app.ingestion.pipeline import ingest

    report = ingest(engine, settings, embed=False)
    return report


@pytest.fixture
def conn(engine, ingested):
    with engine.connect() as c:
        yield c


def visible_paths(conn, user_groups: list[str]) -> set[str]:
    from app.auth.acl import acl_clause

    rows = conn.execute(
        text(f"SELECT DISTINCT d.path FROM chunks c JOIN documents d ON d.id = c.document_id WHERE {acl_clause('c')}"),
        {"user_groups": sorted(user_groups)},
    )
    return {r[0] for r in rows}
