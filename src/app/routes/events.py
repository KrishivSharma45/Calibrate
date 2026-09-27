"""Events (public pages, organizer management) and teams (invite links)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from app.auth import STAFF, current_user, require_role, require_user
from app.db import get_db
from app.models import Event, Project, Role, Team, User
from app.services import events as ev_svc
from app.services import teams as team_svc
from app.services.projects import db_now, is_member, submissions_open, user_team
from app.web import read_body, render, wants_html

router = APIRouter()


def get_event(db: DbSession, event_id: str) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


# --- public event pages ------------------------------------------------------------


@router.get("/events")
def events_page(request: Request, db: DbSession = Depends(get_db)):
    events = db.scalars(select(Event).order_by(Event.submissions_close.desc())).all()
    now = db_now(db)
    return render(request, "events.html", {"events": events, "open": {e.id: submissions_open(e, now) for e in events}})


@router.get("/api/events")
def events_api(db: DbSession = Depends(get_db)):
    return [ev_svc.event_out(e) for e in db.scalars(select(Event).order_by(Event.submissions_close.desc()))]


@router.get("/events/{event_id}")
def event_page(request: Request, event_id: str, db: DbSession = Depends(get_db)):
    event = get_event(db, event_id)
    user = current_user(request)
    team = user_team(db, user, event.id) if user else None
    return render(request, "event_detail.html", {
        "event": event, "team": team, "is_open": submissions_open(event, db_now(db)),
    })


@router.get("/api/events/{event_id}")
def event_api(event_id: str, db: DbSession = Depends(get_db)):
    return ev_svc.event_out(get_event(db, event_id))


# --- organizer: create / edit ---------------------------------------------------------


def event_input(fields: dict, is_json: bool) -> ev_svc.EventInput:
    raw_size = fields.get("max_team_size")
    try:
        size = 4 if raw_size in (None, "") else int(raw_size)  # not `or 4`: 0 must fail validation
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=["Team size must be a number."])
    if is_json:
        tracks = [str(t).strip() for t in fields.get("tracks") or [] if str(t).strip()]
        prizes = [(str(p.get("name", "")).strip(), str(p.get("description", "")).strip())
                  for p in fields.get("prizes") or [] if isinstance(p, dict)]
        questions = [(str(q.get("prompt", "")).strip(), bool(q.get("required")))
                     for q in fields.get("questions") or [] if isinstance(q, dict)]
    else:
        tracks = ev_svc.lines(fields.get("tracks", ""))
        prizes = ev_svc.parse_prizes(fields.get("prizes", ""))
        questions = ev_svc.parse_questions(fields.get("questions", ""))
    return ev_svc.EventInput(
        name=str(fields.get("name") or ""), description=str(fields.get("description") or ""),
        submissions_open=ev_svc.parse_utc(fields.get("submissions_open")),
        submissions_close=ev_svc.parse_utc(fields.get("submissions_close")),
        max_team_size=size, new_tracks=tracks, new_prizes=prizes, new_questions=questions,
    )


def _form_page(request: Request, event: Event | None, fields: dict | None = None, errors=None, status=200):
    return render(request, "event_form.html", {"event": event, "f": fields or {}, "errors": errors or []},
                  status_code=status)


@router.get("/organizer")
def organizer_home(request: Request, user: User = Depends(require_role(*STAFF)),
                   db: DbSession = Depends(get_db)):
    events = db.scalars(select(Event).order_by(Event.submissions_close.desc())).all()
    return render(request, "organizer.html", {"events": events})


@router.get("/organizer/events/new")
def new_event_page(request: Request, user: User = Depends(require_role(*STAFF))):
    return _form_page(request, None)


async def _save_event(request: Request, db: DbSession, user: User, event: Event | None):
    fields, is_json = await read_body(request)

    def work():
        try:
            data = event_input(fields, is_json)
            if event is None:
                return ev_svc.create_event(db, user, data), None
            return ev_svc.update_event(db, user, event, data), None
        except HTTPException as e:
            if e.status_code == 422 and wants_html(request):
                errors = e.detail if isinstance(e.detail, list) else [str(e.detail)]
                return None, _form_page(request, event, fields, errors, status=422)
            raise

    saved, error_page = await run_in_threadpool(work)
    if error_page is not None:
        return error_page
    if wants_html(request):
        return RedirectResponse(f"/events/{saved.id}", status_code=303)
    return JSONResponse(ev_svc.event_out(saved), status_code=201 if event is None else 200)


@router.post("/organizer/events/new")
async def create_event_form(request: Request, user: User = Depends(require_role(*STAFF)),
                            db: DbSession = Depends(get_db)):
    return await _save_event(request, db, user, None)


@router.post("/api/events")
async def create_event_api(request: Request, user: User = Depends(require_role(*STAFF)),
                           db: DbSession = Depends(get_db)):
    return await _save_event(request, db, user, None)


@router.get("/organizer/events/{event_id}/edit")
def edit_event_page(request: Request, event_id: str, user: User = Depends(require_role(*STAFF)),
                    db: DbSession = Depends(get_db)):
    event = get_event(db, event_id)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M") if d else ""  # noqa: E731
    fields = {"name": event.name, "description": event.description, "max_team_size": event.max_team_size,
              "submissions_open": fmt(event.submissions_open), "submissions_close": fmt(event.submissions_close)}
    return _form_page(request, event, fields)


@router.post("/organizer/events/{event_id}/edit")
async def edit_event_form(request: Request, event_id: str, user: User = Depends(require_role(*STAFF)),
                          db: DbSession = Depends(get_db)):
    return await _save_event(request, db, user, get_event(db, event_id))


@router.post("/api/events/{event_id}")
async def edit_event_api(request: Request, event_id: str, user: User = Depends(require_role(*STAFF)),
                         db: DbSession = Depends(get_db)):
    return await _save_event(request, db, user, get_event(db, event_id))


# --- teams ----------------------------------------------------------------------------------


def team_out(team: Team, include_invite: bool, base_url: str) -> dict:
    out = {"id": team.id, "event_id": team.event_id, "name": team.name,
           "members": [{"id": m.user.id, "name": m.user.name} for m in team.members]}
    if include_invite:
        out["invite_url"] = f"{base_url.rstrip('/')}/teams/join/{team.invite_token}"
    return out


def _visible_team(db: DbSession, user: User, team_id: str) -> Team:
    team = db.get(Team, team_id)
    if team is None or not (user.role in STAFF or is_member(db, user, team.id)):
        raise HTTPException(status_code=404, detail="Team not found")
    return team


def _done(request: Request, team: Team, status: int = 200):
    if wants_html(request):
        return RedirectResponse(f"/teams/{team.id}", status_code=303)
    return JSONResponse(team_out(team, True, str(request.base_url)), status_code=status)


@router.post("/events/{event_id}/teams")
async def create_team(request: Request, event_id: str, user: User = Depends(require_user),
                      db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    team = await run_in_threadpool(
        lambda: team_svc.create_team(db, user, get_event(db, event_id), str(fields.get("name") or "")))
    return _done(request, team, 201)


@router.get("/teams/{team_id}")
def team_page(request: Request, team_id: str, user: User = Depends(require_user),
              db: DbSession = Depends(get_db)):
    team = _visible_team(db, user, team_id)
    event = db.get(Event, team.event_id)
    project = db.scalar(select(Project).where(Project.team_id == team.id, Project.duplicate_of_id.is_(None)))
    return render(request, "team.html", {
        "team": team, "event": event, "project": project,
        "is_member": is_member(db, user, team.id),
        "invite_url": f"{str(request.base_url).rstrip('/')}/teams/join/{team.invite_token}",
        "is_open": submissions_open(event, db_now(db)),
    })


@router.get("/api/teams/{team_id}")
def team_api(request: Request, team_id: str, user: User = Depends(require_user),
             db: DbSession = Depends(get_db)):
    team = _visible_team(db, user, team_id)
    return team_out(team, is_member(db, user, team.id), str(request.base_url))


@router.get("/teams/join/{token}")
def join_page(request: Request, token: str, user: User = Depends(require_user),
              db: DbSession = Depends(get_db)):
    # Showing a page, never joining on GET: a GET must not change state, or any
    # <img src="/teams/join/..."> on another site could add you to a team.
    team = team_svc.team_by_token(db, token)
    event = db.get(Event, team.event_id)
    return render(request, "team_join.html", {
        "team": team, "event": event, "token": token,
        "count": team_svc.member_count(db, team.id),
        "already": is_member(db, user, team.id),
        "can_join": user.role == Role.participant,
        "is_open": submissions_open(event, db_now(db)),
    })


@router.post("/teams/join/{token}")
def join_team(request: Request, token: str, user: User = Depends(require_user),
              db: DbSession = Depends(get_db)):
    return _done(request, team_svc.join_team(db, user, token))


@router.post("/teams/{team_id}/invite/rotate")
def rotate_invite(request: Request, team_id: str, user: User = Depends(require_user),
                  db: DbSession = Depends(get_db)):
    team = _visible_team(db, user, team_id)
    return _done(request, team_svc.rotate_invite(db, user, team))
