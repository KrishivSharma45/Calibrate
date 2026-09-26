"""FastAPI application entry point."""

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import STATIC_DIR
from app.db import get_db

app = FastAPI(title="Dogfood Portal", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    """Liveness + DB check. Used by humans and by the compose healthcheck."""
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
