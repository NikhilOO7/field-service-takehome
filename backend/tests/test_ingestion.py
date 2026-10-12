"""Loader and ingestion invariants on the real corpus (spec M3-M6)."""

from sqlalchemy import text


def _doc(conn, path):
    return conn.execute(text("SELECT * FROM documents WHERE path = :p"), {"p": path}).mappings().one()


def test_every_file_is_registered(conn, settings):
    files = [p for p in settings.corpus_root.rglob("*") if p.is_file() and not p.name.startswith(".")]
    count = conn.execute(text("SELECT count(*) FROM documents")).scalar_one()
    assert count == len(files) == 42


def test_problem_files_are_flagged(conn):
    assert _doc(conn, "corpus/sharepoint/Inspection_Checklist_Q3_FINAL.docx")["parse_status"] == "empty"
    assert _doc(conn, "corpus/bulletins/Vastra_SB-2026-015_VRS-45_Coupling.pdf")["parse_status"] == "partial"
    dup = _doc(conn, "corpus/manuals/NK600_manual_final_v2.pdf")
    canon = _doc(conn, "corpus/manuals/Nordkyl_NK-600_Service_Manual_RevC.pdf")
    assert dup["parse_status"] == "duplicate" and dup["duplicate_of"] == canon["id"]
    rev_b = _doc(conn, "corpus/manuals/Vastra_VRS-75_Service_Manual_RevB_2024.pdf")
    rev_c = _doc(conn, "corpus/manuals/Vastra_VRS-75_Service_Manual_RevC_2026.pdf")
    assert rev_b["status"] == "superseded" and rev_b["superseded_by"] == rev_c["id"]
    assert _doc(conn, "corpus/confluence/VRS-Series-Oil-and-Filter-Quick-Reference.html")["status"] == "stale"
    assert _doc(conn, "corpus/confluence/VRS-110-VFD-quick-reset-unofficial.html")["status"] == "unofficial"


def test_fault_code_row_is_its_own_chunk_with_page(conn):
    row = conn.execute(text("""
        SELECT c.text, c.locator, c.models FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.path LIKE '%HCP-500_IOM%' AND 'P-07' = ANY(c.fault_codes) AND c.kind = 'table_row'
          AND c.text LIKE 'Code: P-07%'""")).mappings().one()
    assert "0.30 bar" in row["text"] and "suction isolation valve" in row["text"]
    assert row["locator"]["page"] == 9 and row["locator"]["section"] == "9.1"
    assert row["models"] == ["HCP-500"]
    hcp350 = conn.execute(text("""
        SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.path LIKE '%HCP-350_IOM%' AND c.text LIKE 'Code: P-07%'""")).scalar_one()
    assert "0.25 bar" in hcp350


def test_procedure_steps_have_step_locators(conn):
    loc = conn.execute(text("""
        SELECT c.locator FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.doc_number = 'HV-PROC-07' AND c.text LIKE 'Step 14:%'""")).scalar_one()
    assert loc["section"] == "6.5" and loc["step"] == "14"


def test_scanned_manual_ocr(conn):
    doc = _doc(conn, "corpus/manuals/Halden_HCP-200_Betriebsanleitung_1998_scan.pdf")
    if doc["parse_status"] == "ocr_pending":
        import pytest
        pytest.skip("no OCR cache / API key")
    hit = conn.execute(text("""
        SELECT c.locator FROM chunks c WHERE c.document_id = :d AND c.text LIKE '%0.38 - 0.50 mm%'"""),
        {"d": doc["id"]}).scalar_one()
    assert hit["page"] == 3


def test_history_and_assets(conn):
    assert conn.execute(text("SELECT count(*) FROM assets")).scalar_one() == 30
    a = conn.execute(text("SELECT * FROM assets WHERE asset_id = 'RTM-H-0007'")).mappings().one()
    assert a["serial_prefix"] == "NK600" and a["serial_num"] == 230415 and "MFL-KM07" in a["legacy_ids"]
    e14 = conn.execute(text("""
        SELECT event_date, source FROM asset_events WHERE asset_id = 'TAC-C-0142' AND fault_code = 'E-14'
        ORDER BY event_date""")).all()
    dates = {str(d) for d, _ in e14}
    assert {"2026-03-12", "2026-07-02", "2026-07-28", "2026-08-30", "2026-09-17"} <= dates
    # Teams messages say '0142', enrichment resolves it to the asset
    teams = conn.execute(text("""
        SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.doc_type = 'chat' AND 'TAC-C-0142' = ANY(c.asset_ids)""")).scalar_one()
    assert teams >= 1


def test_bulletin_applicability_and_parts(conn):
    rows = conn.execute(text("""
        SELECT b.number, a.serial_from, a.serial_to, a.region FROM bulletin_applicability a
        JOIN bulletins b ON b.id = a.bulletin_id WHERE b.number = 'SB-2025-014'""")).all()
    assert rows == [("SB-2025-014", 220001, 231999, "EU")]
    status = conn.execute(text("SELECT DISTINCT status FROM parts WHERE part_no = '63-1170'")).scalar_one()
    assert status == "blocked"
    assert conn.execute(text("SELECT count(*) FROM parts WHERE part_no = '77-0419'")).scalar_one() >= 1
