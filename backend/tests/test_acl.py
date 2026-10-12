"""Document ACL (spec M2): the SQL filter must agree with permissions.json for every user and document."""

import json

import pytest
from sqlalchemy import text

from app.auth.acl import can_view, disclosure, missing_groups
from app.auth.users import UserContext
from tests.conftest import visible_paths


def _users(settings):
    return json.loads(settings.users_file.read_text())["users"]


def _permissions(settings):
    return json.loads(settings.permissions_file.read_text())["documents"]


def test_acl_matrix_matches_permissions_json(conn, settings):
    """12 users x every document with chunks: SQL visibility == the rule computed from permissions.json."""
    perms = _permissions(settings)
    docs_with_chunks = {r[0] for r in conn.execute(text(
        "SELECT DISTINCT d.path FROM documents d JOIN chunks c ON c.document_id = d.id"))}
    for user in _users(settings):
        groups = set(user["groups"])
        expected = {p for p in docs_with_chunks if p in perms and set(perms[p]["required_groups"]) <= groups}
        assert visible_paths(conn, user["groups"]) == expected, user["id"]


def test_unlisted_document_denied_to_everyone(conn, settings):
    unofficial = "corpus/confluence/VRS-110-VFD-quick-reset-unofficial.html"
    status = conn.execute(text("SELECT acl_status FROM documents WHERE path = :p"), {"p": unofficial}).scalar_one()
    assert status == "unlisted"
    all_groups = sorted({g for u in _users(settings) for g in u["groups"]})
    assert unofficial not in visible_paths(conn, all_groups)  # not even a user with every group


@pytest.mark.parametrize("user_id, path, allowed", [
    ("u03", "corpus/sharepoint/HV-PROC-07_VRS-110_VFD_Fault_Reset.docx", False),
    ("u02", "corpus/sharepoint/HV-PROC-07_VRS-110_VFD_Fault_Reset.docx", True),
    ("u03", "corpus/confluence/Lessons-Learned-2025.html", False),
    ("u03", "corpus/data/Q2_2026_Field_Service_Ops_Review.pdf", False),
    ("u03", "corpus/sharepoint/KT-WI-14_Low_Suction_Pressure_Chillers_EN.docx", False),
    ("u03", "corpus/data/salesforce_cases_EU_2025-2026.json", False),
    ("u03", "corpus/chat/Teams_na-compressors_2026-07.json", True),
    ("u08", "corpus/sharepoint/CS-PROC-03_Confined_Space_Entry_Pump_Pits.docx", True),
    ("u01", "corpus/sharepoint/CS-PROC-03_Confined_Space_Entry_Pump_Pits.docx", False),
    ("u06", "corpus/sharepoint/SOP-020_Chiller_Leak_Test_and_Recharge.docx", False),
    ("u05", "corpus/sharepoint/SOP-020_Chiller_Leak_Test_and_Recharge.docx", True),
    ("u11", "corpus/bulletins/Nordkyl_SB-2025-014_NK-600_EXV_Campaign.pdf", False),
    ("u12", "corpus/email/RE_BX-12_tracking_after_belt_change.eml", True),
])
def test_named_cases(conn, settings, user_id, path, allowed):
    user = next(u for u in _users(settings) if u["id"] == user_id)
    assert (path in visible_paths(conn, user["groups"])) is allowed


def test_disclosure_policy():
    priya = UserContext("u03", "Priya", None, frozenset({"region-na", "family-compressors"}))
    # HV-PROC-07: only the certification is missing -> say it needs HV
    assert disclosure(missing_groups(priya, ["cert-hv", "family-compressors"]), "listed") == "certification"
    # lead-only and region-gated material is never disclosed
    assert disclosure(missing_groups(priya, ["role-lead"]), "listed") == "none"
    assert disclosure(missing_groups(priya, ["family-chillers", "region-eu"]), "listed") == "none"
    # unlisted documents are never disclosed
    assert disclosure(missing_groups(priya, ["cert-hv"]), "unlisted") == "none"
    olu = UserContext("u12", "Olu", None, frozenset({"region-na", "family-conveyors"}))
    assert disclosure(missing_groups(olu, ["cert-hv", "family-compressors"]), "listed") == "family"
    assert can_view(priya, [], "listed") and not can_view(priya, [], "unlisted")
