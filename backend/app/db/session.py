"""Database engine. SQL is written explicitly (SQLAlchemy Core + text()) so the ACL filters stay visible."""

from functools import lru_cache

from pgvector.psycopg import register_vector
from sqlalchemy import Engine, create_engine, event

from app.config import get_settings


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True, future=True)

    @event.listens_for(engine, "connect")
    def _register_vector(dbapi_connection, _record):  # pragma: no cover - trivial
        register_vector(dbapi_connection)

    return engine


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)
