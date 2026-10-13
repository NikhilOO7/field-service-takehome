"""Users and groups from users.json, and the server-side UserContext."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, text

GROUP_KIND_BY_PREFIX = {"region-": "region", "family-": "family", "cert-": "cert", "role-": "role"}

# Plain-language certification names used in prompts, work orders and the restricted notice.
CERT_LABELS = {"cert-hv": "HV", "cert-refrigerant": "Refrigerant", "cert-confined-space": "Confined space"}


def group_kind(group_id: str) -> str:
    """Kind of a group by its prefix. Unknown prefixes (e.g. a new 'site-gdansk') are 'other': the ACL still
    enforces them, and the disclosure policy treats them like region groups (reveal nothing)."""
    for prefix, kind in GROUP_KIND_BY_PREFIX.items():
        if group_id.startswith(prefix):
            return kind
    return "other"


def tech_code(name: str) -> str:
    """MaintPro/Servio technician code, e.g. 'Daniel Reyes' -> 'DREYES'."""
    first, *rest = name.split()
    return (first[0] + rest[-1]).upper() if rest else first.upper()


@dataclass(frozen=True)
class UserContext:
    """Who is asking. Always built on the server; never taken from a request body."""

    id: str
    name: str
    title: str | None
    groups: frozenset[str] = field(default_factory=frozenset)

    def has(self, group: str) -> bool:
        return group in self.groups

    @property
    def certifications(self) -> list[str]:
        return [CERT_LABELS[g] for g in sorted(self.groups) if g in CERT_LABELS]

    @property
    def is_lead(self) -> bool:
        return "role-lead" in self.groups


def load_users_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sync_users(conn: Connection, path: Path) -> int:
    """Upsert groups and users from users.json. Returns the number of users."""
    data = load_users_file(path)
    for gid, description in data["groups"].items():
        conn.execute(
            text(
                "INSERT INTO groups (id, description, kind) VALUES (:id, :d, :k) "
                "ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description, kind = EXCLUDED.kind"
            ),
            {"id": gid, "d": description, "k": group_kind(gid)},
        )
    for u in data["users"]:
        conn.execute(
            text(
                "INSERT INTO users (id, name, title, groups, tech_codes) VALUES (:id, :n, :t, :g, :c) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, title = EXCLUDED.title, "
                "groups = EXCLUDED.groups, tech_codes = EXCLUDED.tech_codes"
            ),
            {"id": u["id"], "n": u["name"], "t": u.get("title"), "g": u["groups"], "c": [tech_code(u["name"])]},
        )
    return len(data["users"])


def get_user(conn: Connection, user_id: str) -> UserContext | None:
    row = conn.execute(
        text("SELECT id, name, title, groups FROM users WHERE id = :id"), {"id": user_id}
    ).mappings().first()
    if row is None:
        return None
    return UserContext(id=row["id"], name=row["name"], title=row["title"], groups=frozenset(row["groups"]))


def list_users(conn: Connection) -> list[dict]:
    rows = conn.execute(text("SELECT id, name, title, groups FROM users ORDER BY id")).mappings().all()
    return [dict(r) for r in rows]
