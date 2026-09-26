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

# Fixed, publicly known demo session tokens (see seed.py). On for the hackathon
# demo; set DEMO_SESSIONS=0 on a real deployment and boot deletes them.
DEMO_SESSIONS = os.environ.get("DEMO_SESSIONS", "1") == "1"

STATIC_DIR =SRC_DIR / "static"
TEMPLATES_DIR = SRC_DIR / "templates"
