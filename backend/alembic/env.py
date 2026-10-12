"""Alembic environment: the URL comes from app settings (or -x url=... for the test DB)."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings


def run_migrations_online() -> None:
    url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
