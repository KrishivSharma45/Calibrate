"""Settings read from environment variables.

Docker Compose sets DATABASE_URL to the Postgres container. Without it we
fall back to a local SQLite file so the app and tests run without Docker.
"""

import os
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SRC_DIR.parent

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{REPO_DIR / 'dev.db'}")
FIXTURES_PATH = Path(os.environ.get("FIXTURES_PATH", REPO_DIR / "fixtures.json"))

STATIC_DIR = SRC_DIR / "static"
TEMPLATES_DIR = SRC_DIR / "templates"
