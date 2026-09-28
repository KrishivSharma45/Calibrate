"""HTML rendering helpers shared by all page routes."""

import json

from fastapi import HTTPException, Request
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


async def read_body(request: Request) -> tuple[dict, bool]:
    """Return (fields, is_json) for either a JSON object or an HTML form post."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(status_code=422, detail="Body is not valid JSON")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Body must be a JSON object")
        return body, True
    fields: dict = {}
    for key, value in (await request.form()).multi_items():
        if key in fields:  # repeated names (checkboxes) become a list
            fields[key] = [*fields[key], value] if isinstance(fields[key], list) else [fields[key], value]
        else:
            fields[key] = value
    return fields, False


def wants_html(request: Request) -> bool:
    """Browsers get pages; curl, run.py and /api/* get JSON."""
    return not request.url.path.startswith("/api") and "text/html" in request.headers.get("accept", "")
