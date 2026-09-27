"""Gallery (public) and project submission (team members, until the deadline)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from app.auth import current_user, require_user
from app.db import get_db
from app.models import Event, Project, Track, User
from app.services import projects as svc
from app.web import read_body, render, wants_html

router = APIRouter()


# --- reading input (form or JSON, same rules) -----------------------------------------


async def read_input(request: Request) -> tuple[svc.ProjectInput, str | None]:
    body, is_json = await read_body(request)
    if is_json:
        text = lambda k, default="": str(body.get(k) or default)  # noqa: E731
        data = svc.ProjectInput(
            title=text("title"), tagline=text("tagline", body.get("summary") or ""),
            description=text("description"), thumbnail_url=text("thumbnail_url"),
            demo_video_url=text("demo_video_url"), repo_url=text("repo_url"),
            live_url=text("live_url"), track_id=body.get("track_id") or None,
            tags=svc.parse_tags(body.get("tags") or []),
            answers={str(k): str(v) for k, v in (body.get("answers") or {}).items()},
            submit=bool(body.get("submit")),
        )
        return data, body.get("event_id")
    form = body
    get = lambda k: str(form.get(k) or "")  # noqa: E731
    data = svc.ProjectInput(
        title=get("title"), tagline=get("tagline"), description=get("description"),
        thumbnail_url=get("thumbnail_url"), demo_video_url=get("demo_video_url"),
        repo_url=get("repo_url"), live_url=get("live_url"), track_id=get("track_id") or None,
        tags=svc.parse_tags(get("tags")),
        answers={k.removeprefix("answer_"): str(v) for k, v in form.items() if k.startswith("answer_")},
        submit=get("action") == "submit",
    )
    return data, get("event_id") or None


def form_values(source: Project | svc.ProjectInput | None) -> dict:
    """Flatten a saved project or unsaved input into plain strings for the form."""
    keys = ("title", "tagline", "description", "track_id", *svc.URL_FIELDS)
    if source is None:
        return {k: "" for k in keys} | {"tags": "", "answers": {}}
    values = {k: getattr(source, k) or "" for k in keys}
    if isinstance(source, Project):
        values["tags"] = ", ".join(t.tag for t in source.tags)
        values["answers"] = {a.question_id: a.answer for a in source.answers}
    else:
        values["tags"] = ", ".join(source.tags)
        values["answers"] = dict(source.answers)
    return values


def form_context(db: DbSession, event: Event, project: Project | None, data=None, errors=None) -> dict:
    return {
        "event": event, "project": project, "errors": errors or [],
        "tracks": db.scalars(select(Track).where(Track.event_id == event.id).order_by(Track.id)).all(),
        "questions": svc.questions_for(db, event.id),
        "form": form_values(data or project),
        "is_open": svc.submissions_open(event, svc.db_now(db)),
    }


# --- gallery ----------------------------------------------------------------------------


def _gallery_query(q: str, event: str, track: str, tag: str, page: int) -> svc.GalleryQuery:
    return svc.GalleryQuery(q=q, event=event, track=track, tag=tag, page=page)


@router.get("/projects")
def gallery_page(request: Request, q: str = "", event: str = "", track: str = "", tag: str = "",
                 page: int = 1, db: DbSession = Depends(get_db)):
    query = _gallery_query(q, event, track, tag, page)
    projects, total = svc.gallery(db, query)
    events = db.scalars(select(Event).order_by(Event.submissions_close)).all()
    return render(request, "gallery.html", {
        "projects": projects, "total": total, "query": query, "events": events,
        "tags": svc.all_tags(db), "page_size": svc.PAGE_SIZE,
    })


@router.get("/api/projects")
def gallery_api(q: str = "", event: str = "", track: str = "", tag: str = "", page: int = 1,
                db: DbSession = Depends(get_db)):
    projects, total = svc.gallery(db, _gallery_query(q, event, track, tag, page))
    return {"total": total, "page": max(page, 1), "page_size": svc.PAGE_SIZE,
            "projects": [svc.project_out(p) for p in projects]}


# --- create -------------------------------------------------------------------------------


@router.get("/projects/new")
def new_project_page(request: Request, event: str | None = None,
                     user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    ev = svc.resolve_event(db, user, event)
    team = svc.user_team(db, user, ev.id)
    if team is not None:
        existing = db.scalar(select(Project).where(Project.team_id == team.id,
                                                   Project.duplicate_of_id.is_(None)))
        if existing is not None:
            return RedirectResponse(f"/projects/{existing.id}/edit", status_code=303)
    ctx = form_context(db, ev, None)
    ctx["team"] = team
    return render(request, "project_form.html", ctx)


@router.post("/projects/new")
async def create_project(request: Request, user: User = Depends(require_user),
                         db: DbSession = Depends(get_db)):
    data, event_id = await read_input(request)

    def work():
        event = svc.resolve_event(db, user, event_id)
        try:
            return svc.create_project(db, user, event, data), None
        except HTTPException as e:
            if e.status_code == 422 and wants_html(request):
                return None, render(request, "project_form.html",
                                    {**form_context(db, event, None, data, e.detail),
                                     "team": svc.user_team(db, user, event.id)}, status_code=422)
            raise

    project, error_page = await run_in_threadpool(work)
    if error_page is not None:
        return error_page
    if wants_html(request):
        return RedirectResponse(f"/projects/{project.id}/edit?saved=1", status_code=303)
    return JSONResponse(svc.project_out(project), status_code=201)


# --- view -----------------------------------------------------------------------------------


def _visible_project(db: DbSession, user: User | None, project_id: str) -> Project:
    project = db.get(Project, project_id)
    # 404, not 403, so drafts are not even confirmed to exist.
    if project is None or not svc.can_view(db, user, project):
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}")
def project_page(request: Request, project_id: str, db: DbSession = Depends(get_db)):
    user = current_user(request)
    project = _visible_project(db, user, project_id)
    return render(request, "project_detail.html", {
        "project": project, "event": db.get(Event, project.event_id),
        "questions": {q.id: q for q in svc.questions_for(db, project.event_id)},
        "can_edit": user is not None and svc.is_member(db, user, project.team_id),
    })


@router.get("/api/projects/{project_id}")
def project_api(request: Request, project_id: str, db: DbSession = Depends(get_db)):
    return svc.project_out(_visible_project(db, current_user(request), project_id))


# --- edit ----------------------------------------------------------------------------------------


def _own_project(db: DbSession, user: User, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None or not svc.can_view(db, user, project):
        raise HTTPException(status_code=404, detail="Project not found")
    if not svc.is_member(db, user, project.team_id):
        raise HTTPException(status_code=403, detail="Only the team can edit its project")
    return project


@router.get("/projects/{project_id}/edit")
def edit_project_page(request: Request, project_id: str, saved: int = 0,
                      user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    project = _own_project(db, user, project_id)
    ctx = form_context(db, db.get(Event, project.event_id), project)
    ctx["saved"] = bool(saved)
    return render(request, "project_form.html", ctx)


@router.post("/projects/{project_id}/edit")
async def edit_project(request: Request, project_id: str, user: User = Depends(require_user),
                       db: DbSession = Depends(get_db)):
    data, _ = await read_input(request)

    def work():
        project = _own_project(db, user, project_id)
        try:
            return svc.update_project(db, user, project, data), None
        except HTTPException as e:
            if e.status_code == 422 and wants_html(request):
                event = db.get(Event, project.event_id)
                return None, render(request, "project_form.html",
                                    form_context(db, event, project, data, e.detail), status_code=422)
            raise

    project, error_page = await run_in_threadpool(work)
    if error_page is not None:
        return error_page
    if wants_html(request):
        return RedirectResponse(f"/projects/{project.id}/edit?saved=1", status_code=303)
    return svc.project_out(project)
