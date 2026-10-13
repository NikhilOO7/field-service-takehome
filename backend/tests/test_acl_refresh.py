"""Permission changes must take effect on re-ingest even when the file bytes are unchanged (no --full):
grant -> ingest -> revoke -> re-ingest -> search, source view and file download all deny."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.ingestion.pipeline import ingest
from tests.conftest import visible_paths

DOC = "var/test_acl_refresh/pit_pump_note.html"


@pytest.fixture
def setup(engine, ingested, settings):
    from app import main

    folder = settings.project_root / "var" / "test_acl_refresh"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pit_pump_note.html").write_text(
        "<html><head><title>Pit pump note</title></head><body><h1>Pit pump note</h1>"
        "<p>FRS-P-0007 impeller nut is a left-hand thread; use the ZX-77 holding tool.</p></body></html>")
    perms = settings.project_root / "var" / "test_acl_refresh_permissions.json"
    original = main.get_engine
    main.get_engine = lambda: engine
    yield folder, perms, TestClient(main.app)
    main.get_engine = original
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE path = :p"), {"p": DOC})
    shutil.rmtree(folder, ignore_errors=True)
    perms.unlink(missing_ok=True)


def _ingest(engine, settings, folder, perms, groups):
    perms.write_text(json.dumps({"documents": {DOC: {"title": "Pit pump note", "required_groups": groups}}}))
    return {r.path: r.status for r in ingest(engine, settings, only=folder, permissions_file=perms, embed=False).results}


def _login(client, user_id):
    c = TestClient(client.app)
    assert c.post("/api/session", json={"user_id": user_id}).status_code == 200
    return c


def test_revoking_access_takes_effect_without_full_reingest(engine, settings, setup):
    folder, perms, client = setup
    priya_groups = ["region-na", "family-compressors"]

    assert _ingest(engine, settings, folder, perms, ["region-na"])[DOC] == "ingested"
    with engine.connect() as conn:
        assert DOC in visible_paths(conn, priya_groups)
        doc_id, chunk_id = conn.execute(text("""SELECT d.id, c.id FROM documents d JOIN chunks c
                                                ON c.document_id = d.id WHERE d.path = :p LIMIT 1"""),
                                        {"p": DOC}).one()
    priya = _login(client, "u03")
    assert priya.get(f"/api/sources/{chunk_id}").status_code == 200
    assert priya.get(f"/api/documents/{doc_id}/file").status_code == 200

    # revoke: pumps only. Same bytes, no --full.
    assert _ingest(engine, settings, folder, perms, ["family-pumps"])[DOC] == "acl updated"
    with engine.connect() as conn:
        assert DOC not in visible_paths(conn, priya_groups)            # search filter (chunk ACL)
        assert DOC in visible_paths(conn, ["region-na", "family-pumps"])
        doc_acl = conn.execute(text("SELECT required_groups FROM documents WHERE path = :p"), {"p": DOC}).scalar_one()
        assert doc_acl == ["family-pumps"]
    assert priya.get(f"/api/sources/{chunk_id}").status_code == 403
    assert priya.get(f"/api/documents/{doc_id}/file").status_code == 403
    assert _login(client, "u01").get(f"/api/sources/{chunk_id}").status_code == 200

    # a re-run with nothing changed is a no-op
    assert _ingest(engine, settings, folder, perms, ["family-pumps"])[DOC] == "unchanged"
