"""Container entry point: prepare the database, then start the web server.

Run as `python -m app.boot`. Steps:
  1. create tables (idempotent)
  2. seed fixtures (idempotent)  -- added in the next step
  3. start uvicorn on 0.0.0.0:8080
"""

import uvicorn

from app.db import Base, engine


def prepare_database() -> None:
    import app.models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)


def main() -> None:
    prepare_database()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, proxy_headers=True)


if __name__ == "__main__":
    main()
