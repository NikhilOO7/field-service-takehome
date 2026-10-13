"""Document access control (spec M2, §9).

Rule from permissions.json: a user may see a document only if they belong to EVERY group in its
required_groups; an empty list means everyone. Documents missing from permissions.json are
'unlisted' and denied to everyone (default deny).

Every retrieval path filters with ACL_SQL in the same query that searches, so restricted rows are
never fetched and then dropped in Python.
"""

from app.auth.users import UserContext, group_kind

# `<@` = "is contained by": every required group is one of the user's groups.
ACL_SQL = "({alias}.acl_status = 'listed' AND {alias}.required_groups <@ CAST(:user_groups AS text[]))"

# Inverse filter, used only by the restricted probe, which returns metadata and never text.
NOT_ACL_SQL = (
    "({alias}.acl_status <> 'listed' OR NOT ({alias}.required_groups <@ CAST(:user_groups AS text[])))"
)


def acl_clause(alias: str = "c") -> str:
    return ACL_SQL.format(alias=alias)


def not_acl_clause(alias: str = "c") -> str:
    return NOT_ACL_SQL.format(alias=alias)


def acl_params(user: UserContext) -> dict:
    return {"user_groups": sorted(user.groups)}


def can_view(user: UserContext, required_groups: list[str] | tuple[str, ...], acl_status: str) -> bool:
    return acl_status == "listed" and set(required_groups) <= set(user.groups)


def missing_groups(user: UserContext, required_groups: list[str]) -> list[str]:
    return sorted(set(required_groups) - set(user.groups))


def disclosure(missing: list[str], acl_status: str) -> str:
    """How much the copilot may say about a document the user can't see (spec §9).

    - 'certification': name the procedure and the certification it needs (safety gate)
    - 'family': say it is covered for equipment the user isn't trained on, no titles
    - 'none': behave as if the document does not exist (region, role-lead, unknown group kinds, unlisted)
    """
    if acl_status != "listed" or not missing:
        return "none"
    kinds = {group_kind(g) for g in missing}
    if "other" in kinds:
        return "none"
    if kinds == {"cert"}:
        return "certification"
    if kinds <= {"cert", "family"} and "family" in kinds:
        return "family"
    return "none"
