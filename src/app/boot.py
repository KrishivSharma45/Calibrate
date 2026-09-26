"""Container entry point: prepare the database, then start the web server.

Run as `python -m app.boot`. Steps:
  1. create tables (idempotent)
  2. seed fixtures + demo data (idempotent) and print the demo session headers
  3. start uvicorn on 0.0.0.0:8080
"""

import uvicorn

from app.config import DEMO_SESSIONS, FIXTURES_PATH
from app.db import Base, SessionLocal, engine
from app.seed import dogfood_headers, run_seed


def prepare_database() -> None:
    import app.models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        added = run_seed(db, FIXTURES_PATH, demo_sessions=DEMO_SESSIONS)
    print(f"seeded from {FIXTURES_PATH}: {added or 'nothing new, already seeded'}", flush=True)
    if DEMO_SESSIONS:
        print("demo session headers for .dogfood.toml [auth] (DEMO ONLY):", flush=True)
        print(dogfood_headers(), flush=True)
    else:
        print("DEMO_SESSIONS=0: fixed demo sessions removed", flush=True)


def main() -> None:
    prepare_database()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, proxy_headers=True)


if __name__ == "__main__":
    main()
