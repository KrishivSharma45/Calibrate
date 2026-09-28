"""T3 routes: voting window, ballot, votes, tallies, comments."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from app.auth import STAFF, current_user, require_role, require_user
from app.db import get_db
from app.models import Project, User, VotingWindow
from app.routes.events import get_event
from app.services import community as svc
from app.services.events import parse_utc
from app.services.projects import can_view
from app.web import read_body, render, wants_html

router = APIRouter()
staff = require_role(*STAFF)


# --- voting window (organizer) --------------------------------------------------------------------


@router.post("/organizer/events/{event_id}/voting")
@router.post("/api/events/{event_id}/voting")
async def set_voting(request: Request, event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    window = await run_in_threadpool(svc.set_window, db, user, get_event(db, event_id),
                                     parse_utc(fields.get("opens_at")), parse_utc(fields.get("closes_at")))
    if wants_html(request):
        return RedirectResponse(f"/organizer/events/{event_id}", status_code=303)
    return {"event_id": event_id, "opens_at": window.opens_at.isoformat(), "closes_at": window.closes_at.isoformat()}


# --- ballot ------------------------------------------------------------------------------------------


def _ballot(db: DbSession, user: User, event_id: str) -> dict:
    event = get_event(db, event_id)
    projects = svc.shuffled_for(user.id, event.id, svc.ballot_projects(db, event.id))
    return {"event": event, "projects": projects, "voted": svc.my_votes(db, user, event.id),
            "state": svc.window_state(db, event.id), "window": db.get(VotingWindow, event.id)}


@router.get("/events/{event_id}/ballot")
def ballot_page(request: Request, event_id: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    ctx = _ballot(db, user, event_id)
    ctx["tallies"] = None
    if ctx["state"] == "closed" or user.role in STAFF:
        ctx["tallies"] = {r["project_id"]: r for r in svc.tallies(db, ctx["event"], user)["projects"]}
    return render(request, "ballot.html", ctx)


@router.get("/api/events/{event_id}/ballot")
def ballot_api(event_id: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    ctx = _ballot(db, user, event_id)
    return {"event_id": event_id, "state": ctx["state"],
            "projects": [{"project_id": p.id, "title": p.title, "voted": p.id in ctx["voted"]}
                         for p in ctx["projects"]]}


@router.get("/api/events/{event_id}/votes")
def votes_api(request: Request, event_id: str, db: DbSession = Depends(get_db)):
    return svc.tallies(db, get_event(db, event_id), current_user(request))


def _after_vote(request: Request, project_id: str, event_id: str, voted: bool):
    if wants_html(request):
        return RedirectResponse(f"/events/{event_id}/ballot#{project_id}", status_code=303)
    return JSONResponse({"project_id": project_id, "voted": voted})


@router.post("/projects/{project_id}/vote")
def vote(request: Request, project_id: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    v = svc.cast_vote(db, user, project_id)
    return _after_vote(request, project_id, v.event_id, True)


@router.post("/projects/{project_id}/unvote")
def unvote(request: Request, project_id: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    svc.remove_vote(db, user, project_id)
    return _after_vote(request, project_id, db.get(Project, project_id).event_id, False)


# --- comments ------------------------------------------------------------------------------------------


@router.get("/api/projects/{project_id}/comments")
def comments_api(request: Request, project_id: str, db: DbSession = Depends(get_db)):
    project = db.get(Project, project_id)
    user = current_user(request)
    if project is None or not can_view(db, user, project):
        raise HTTPException(status_code=404, detail="Project not found")
    include_hidden = user is not None and user.role in STAFF
    return [svc.comment_out(c) for c in svc.visible_comments(db, project_id, include_hidden)]


@router.post("/projects/{project_id}/comments")
async def add_comment(request: Request, project_id: str, user: User = Depends(require_user),
                      db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    comment = await run_in_threadpool(svc.add_comment, db, user, project_id, str(fields.get("body") or ""))
    if wants_html(request):
        return RedirectResponse(f"/projects/{project_id}#comments", status_code=303)
    return JSONResponse(svc.comment_out(comment), status_code=201)


@router.post("/comments/{comment_id}/hide")
def hide_comment(request: Request, comment_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    comment = svc.hide_comment(db, user, comment_id)
    if wants_html(request):
        return RedirectResponse(f"/projects/{comment.project_id}#comments", status_code=303)
    return svc.comment_out(comment)
