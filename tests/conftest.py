import os
import sys
from pathlib import Path

os.environ.setdefault("BCRYPT_ROUNDS", "4")  # minimum cost: tests only, keeps the suite fast

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import app.models  # noqa: E402,F401  (registers tables)
from app.db import Base  # noqa: E402


@pytest.fixture
def db():
    """A fresh in-memory SQLite database per test, with foreign keys enforced."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db):
    """HTTP client whose requests hit the same test database as `db`."""
    from fastapi.testclient import TestClient

    from app.auth import comment_limit, login_throttle, vote_limit
    from app.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    for limiter in (login_throttle, vote_limit, comment_limit):
        limiter.failures.clear()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def fixtures_data():
    import json

    from app.config import FIXTURES_PATH

    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def seeded(db):
    from app.config import FIXTURES_PATH
    from app.seed import run_seed

    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    return db
