"""Results (hidden until published), publishing, and the audit log viewer."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.auth import STAFF, current_user, require_role
from app.db import get_db
from app.models import AuditLog, User
from app.routes.events import get_event
from app.services import results as svc
from app.web import render, wants_html

router = APIRouter()
staff = require_role(*STAFF)
AUDIT_PAGE = 100


def _results_for(request: Request, db: DbSession, event_id: str) -> tuple[dict, bool]:
    """(results, is_staff_view). Before publishing, only organizers/admins get anything."""
    event = get_event(db, event_id)
    user = current_user(request)
    full = svc.compute_results(db, event)
    if user is not None and user.role in STAFF:
        return full, True
    if event.results_published_at is None:
        raise HTTPException(status_code=403, detail={
            "error": "results_not_published", "message": "Results are not published yet."})
    return svc.public_results(full), False


@router.get("/api/events/{event_id}/results")
def results_api(request: Request, event_id: str, db: DbSession = Depends(get_db)):
    return _results_for(request, db, event_id)[0]


@router.get("/events/{event_id}/results")
def results_page(request: Request, event_id: str, db: DbSession = Depends(get_db)):
    results, is_staff = _results_for(request, db, event_id)
    return render(request, "results.html", {"event": get_event(db, event_id), "r": results, "staff": is_staff})


def _published(request: Request, event, action: str):
    if wants_html(request):
        return RedirectResponse(f"/events/{event.id}/results", status_code=303)
    return JSONResponse({"event_id": event.id, "action": action,
                         "published_at": event.results_published_at.isoformat()
                         if event.results_published_at else None})


@router.post("/organizer/events/{event_id}/results/publish")
@router.post("/api/events/{event_id}/results/publish")
def publish(request: Request, event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    return _published(request, svc.publish(db, user, get_event(db, event_id)), "publish")


@router.post("/organizer/events/{event_id}/results/unpublish")
@router.post("/api/events/{event_id}/results/unpublish")
def unpublish(request: Request, event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    return _published(request, svc.unpublish(db, user, get_event(db, event_id)), "unpublish")


# --- audit log ------------------------------------------------------------------------------------


def audit_query(action: str, actor: str, entity: str):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action, autoescape=True))
    if actor:
        stmt = stmt.where(AuditLog.actor_id == actor)
    if entity:
        stmt = stmt.where(AuditLog.entity_id == entity)
    return stmt


def audit_rows(db: DbSession, action: str = "", actor: str = "", entity: str = "", page: int = 1,
               limit: int | None = AUDIT_PAGE) -> tuple[list[dict], int]:
    stmt = audit_query(action, actor, entity)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    stmt = stmt.order_by(AuditLog.id.desc())
    if limit:
        stmt = stmt.offset((max(page, 1) - 1) * limit).limit(limit)
    entries = db.scalars(stmt).all()
    names = {u.id: u.name for u in db.scalars(select(User).where(
        User.id.in_({e.actor_id for e in entries if e.actor_id})))}
    return [{"id": e.id, "at": e.at.isoformat(), "actor_id": e.actor_id,
             "actor": names.get(e.actor_id, "system") if e.actor_id else "system",
             "action": e.action, "entity_type": e.entity_type, "entity_id": e.entity_id,
             "detail": json.loads(e.detail) if e.detail else {}} for e in entries], total


@router.get("/api/audit")
def audit_api(action: str = "", actor: str = "", entity: str = "", page: int = 1,
              user: User = Depends(staff), db: DbSession = Depends(get_db)):
    rows, total = audit_rows(db, action, actor, entity, page)
    return {"total": total, "page": max(page, 1), "page_size": AUDIT_PAGE, "entries": rows}


@router.get("/organizer/audit")
def audit_page(request: Request, action: str = "", actor: str = "", entity: str = "", page: int = 1,
               user: User = Depends(staff), db: DbSession = Depends(get_db)):
    rows, total = audit_rows(db, action, actor, entity, page)
    actions = db.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    return render(request, "audit.html", {
        "rows": rows, "total": total, "page": max(page, 1), "page_size": AUDIT_PAGE, "actions": actions,
        "f": {"action": action, "actor": actor, "entity": entity}})
