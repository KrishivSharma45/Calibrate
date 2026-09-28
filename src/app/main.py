"""FastAPI application entry point."""

from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import attach_auth, csrf_protect
from app.config import STATIC_DIR
from app.db import get_db
from app.routes import auth as auth_routes
from app.routes import community as community_routes
from app.routes import events as event_routes
from app.routes import judging as judging_routes
from app.routes import organizer as organizer_routes
from app.routes import people as people_routes
from app.routes import results as results_routes
from app.routes import projects as project_routes
from app.web import render, wants_html

# Every route, present and future, runs attach_auth + csrf_protect first.
app = FastAPI(title="Dogfood Portal", version="0.1.0",
              dependencies=[Depends(attach_auth), Depends(csrf_protect)])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(auth_routes.router)
app.include_router(project_routes.router)
app.include_router(event_routes.router)
app.include_router(judging_routes.router)
app.include_router(organizer_routes.router)  # after events: /organizer/events/new must match first
app.include_router(results_routes.router)
app.include_router(people_routes.router)
app.include_router(community_routes.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Everything is served locally, so nothing else is ever needed.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; frame-ancestors 'none'; form-action 'self'",
    )
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """API clients get JSON with the real status; browsers get a page."""
    if not wants_html(request):
        return await http_exception_handler(request, exc)
    if exc.status_code == 401 and request.method == "GET":
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    detail = exc.detail
    if isinstance(detail, dict):
        detail = detail.get("message")
    elif isinstance(detail, list):
        detail = " ".join(map(str, detail))
    message = detail if isinstance(detail, str) and detail else "Request refused."
    return render(request, "error.html", {"status": exc.status_code, "message": message},
                  status_code=exc.status_code)


@app.get("/")
def home(request: Request):
    return render(request, "home.html")


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    """Liveness + DB check. Used by humans and by the compose healthcheck."""
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
