"""HTML rendering helpers shared by all page routes."""

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.auth import csrf_token_for, current_user, set_anon_csrf_cookie
from app.config import TEMPLATES_DIR

templates = Jinja2Templates(directory=TEMPLATES_DIR)  # autoescape is on for .html


def render(request: Request, template: str, context: dict | None = None, status_code: int = 200):
    token, is_new = csrf_token_for(request)
    response = templates.TemplateResponse(
        request, template,
        {"user": current_user(request), "csrf_token": token, **(context or {})},
        status_code=status_code,
    )
    if is_new:
        set_anon_csrf_cookie(response, token)
    return response


def wants_html(request: Request) -> bool:
    """Browsers get pages; curl, run.py and /api/* get JSON."""
    return not request.url.path.startswith("/api") and "text/html" in request.headers.get("accept", "")
