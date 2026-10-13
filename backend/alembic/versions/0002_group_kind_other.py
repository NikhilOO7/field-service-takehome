"""Allow groups with an unknown prefix (kind 'other'), e.g. a site group added with new documents.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE groups DROP CONSTRAINT IF EXISTS groups_kind_check")
    op.execute("ALTER TABLE groups ADD CONSTRAINT groups_kind_check "
               "CHECK (kind IN ('region','family','cert','role','other'))")


def downgrade() -> None:
    op.execute("DELETE FROM groups WHERE kind = 'other'")
    op.execute("ALTER TABLE groups DROP CONSTRAINT IF EXISTS groups_kind_check")
    op.execute("ALTER TABLE groups ADD CONSTRAINT groups_kind_check "
               "CHECK (kind IN ('region','family','cert','role'))")
