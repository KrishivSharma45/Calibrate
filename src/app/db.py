"""Database engine, session factory and the declarative Base for models."""

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

if engine.dialect.name == "sqlite":
    # SQLite ignores foreign keys unless asked; Postgres always enforces them.
    @event.listens_for(engine, "connect")
    def _sqlite_fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one DB session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
