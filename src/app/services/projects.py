"""Project submission rules, shared by the HTML and JSON routes.

Order of checks when a participant creates or edits a project:
  1. logged in                         (401, in the route dependency)
  2. which event                       (explicit event_id, else the user's team's event)
  3. event accepting submissions?      (409 submissions_closed, by the database clock)
  4. member of the team that owns it   (403 / 404)
  5. field validation                  (422)
The deadline is checked before membership so that a closed event says
"closed" to everyone, and it is checked on every write, create and edit alike.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.audit import audit
from app.models import (
    CustomQuestion,
    Event,
    Project,
    ProjectAnswer,
    ProjectStatus,
    ProjectTag,
    Role,
    Team,
    TeamMember,
    Track,
    User,
    new_id,
    utcnow,
)

PAGE_SIZE = 48
MAX_TAGS = 10
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 +#.\-]{0,29}$")
URL_FIELDS = ("thumbnail_url", "demo_video_url", "repo_url", "live_url")
TEXT_LIMITS = {"title": 200, "tagline": 300, "description": 20000}


# --- time and deadlines --------------------------------------------------------


def db_now(db: DbSession) -> datetime:
    """Current time from the database, so every app process agrees on one clock."""
    if db.get_bind().dialect.name == "postgresql":
        return db.scalar(select(func.now())).astimezone(UTC)
    return utcnow()  # SQLite (tests / local dev) has no timezone-aware now()


def submissions_open(event: Event, now: datetime) -> bool:
    if event.submissions_open is not None and now < event.submissions_open:
        return False
    return now < event.submissions_close


def ensure_submissions_open(db: DbSession, event: Event) -> None:
    now = db_now(db)
    if not submissions_open(event, now):
        when = event.submissions_close.isoformat().replace("+00:00", "Z")
        raise HTTPException(status_code=409, detail={
            "error": "submissions_closed",
            "message": f"Submissions for {event.name} are not open (deadline {when}).",
        })


# --- who submits for what --------------------------------------------------------


def user_team(db: DbSession, user: User, event_id: str) -> Team | None:
    return db.scalar(select(Team).join(TeamMember).where(
        TeamMember.user_id == user.id, TeamMember.event_id == event_id))


def resolve_event(db: DbSession, user: User, event_id: str | None) -> Event:
    if event_id:
        event = db.get(Event, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="No such event")
        return event
    event_ids = db.scalars(select(TeamMember.event_id).where(TeamMember.user_id == user.id)).all()
    if len(event_ids) == 1:
        return db.get(Event, event_ids[0])
    if not event_ids:
        raise HTTPException(status_code=403, detail="Join or create a team before submitting")
    raise HTTPException(status_code=422, detail="You are in several events; pass event_id")


def is_member(db: DbSession, user: User, team_id: str) -> bool:
    return db.get(TeamMember, (team_id, user.id)) is not None


def can_view(db: DbSession, user: User | None, project: Project) -> bool:
    if project.status == ProjectStatus.submitted and project.duplicate_of_id is None:
        return True
    if user is None:
        return False
    return user.role in (Role.organizer, Role.admin) or is_member(db, user, project.team_id)


# --- validation --------------------------------------------------------------------


@dataclass
class ProjectInput:
    title: str = ""
    tagline: str = ""
    description: str = ""
    thumbnail_url: str = ""
    demo_video_url: str = ""
    repo_url: str = ""
    live_url: str = ""
    track_id: str | None = None
    tags: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    submit: bool = False


def parse_tags(raw: str | list[str]) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else raw
    seen: list[str] = []
    for item in items:
        tag = " ".join(str(item).strip().lower().split())
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def safe_url(value: str) -> bool:
    """Only absolute http(s) links, so a `javascript:` URL can never reach an href."""
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def validate(db: DbSession, event: Event, data: ProjectInput) -> list[str]:
    errors = []
    if not data.title.strip():
        errors.append("Title is required.")
    for name, limit in TEXT_LIMITS.items():
        if len(getattr(data, name)) > limit:
            errors.append(f"{name} is longer than {limit} characters.")
    for name in URL_FIELDS:
        value = getattr(data, name).strip()
        if value and not safe_url(value):
            errors.append(f"{name} must be an http(s) URL.")
    if data.track_id:
        track = db.get(Track, data.track_id)
        if track is None or track.event_id != event.id:
            errors.append("Pick a track from this event.")
    if len(data.tags) > MAX_TAGS:
        errors.append(f"At most {MAX_TAGS} tags.")
    errors += [f"Tag '{t}' is not allowed (letters, digits, space, + # . -, max 30)."
               for t in data.tags if not TAG_RE.match(t)]
    questions = {q.id: q for q in event.questions}
    unknown = set(data.answers) - set(questions)
    if unknown:
        errors.append("Unknown custom question.")
    if data.submit:
        if not data.track_id and event.tracks:
            errors.append("Pick a track before submitting.")
        errors += [f"'{q.prompt}' is required before submitting." for q in questions.values()
                   if q.required and not data.answers.get(q.id, "").strip()]
    return errors


# --- writes ---------------------------------------------------------------------------


def _apply(project: Project, data: ProjectInput, now: datetime) -> None:
    project.title = data.title.strip()
    project.tagline = data.tagline.strip()
    project.description = data.description
    for name in URL_FIELDS:
        setattr(project, name, getattr(data, name).strip())
    project.track_id = data.track_id or None
    # Reuse existing child rows: replacing them with new objects that have the same
    # primary key makes the ORM insert before it deletes, which collides.
    tags = {t.tag: t for t in project.tags}
    project.tags = [tags.get(t) or ProjectTag(tag=t) for t in data.tags]
    answers = {a.question_id: a for a in project.answers}
    kept = []
    for qid, text in data.answers.items():
        if text.strip():
            row = answers.get(qid) or ProjectAnswer(question_id=qid)
            row.answer = text
            kept.append(row)
    project.answers = kept
    if data.submit and project.status != ProjectStatus.submitted:
        project.status = ProjectStatus.submitted
        project.submitted_at = now
    project.updated_at = now


def create_project(db: DbSession, user: User, event: Event, data: ProjectInput) -> Project:
    ensure_submissions_open(db, event)
    team = user_team(db, user, event.id)
    if team is None:
        raise HTTPException(status_code=403, detail="Join or create a team for this event first")
    existing = db.scalar(select(Project).where(Project.team_id == team.id,
                                               Project.duplicate_of_id.is_(None)))
    if existing is not None:
        raise HTTPException(status_code=409, detail={
            "error": "team_already_has_project", "project_id": existing.id,
            "message": "Your team already has a project; edit it instead."})
    errors = validate(db, event, data)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    now = db_now(db)
    project = Project(id=new_id("prj"), event_id=event.id, team_id=team.id, created_at=now)
    _apply(project, data, now)
    db.add(project)
    db.flush()
    audit(db, user, "project.create", "project", project.id, status=project.status.value)
    db.commit()
    return project


def update_project(db: DbSession, user: User, project: Project, data: ProjectInput) -> Project:
    event = db.get(Event, project.event_id)
    ensure_submissions_open(db, event)
    if not is_member(db, user, project.team_id):
        raise HTTPException(status_code=403, detail="Only the team can edit its project")
    errors = validate(db, event, data)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    was = project.status
    _apply(project, data, db_now(db))
    action = "project.submit" if was != project.status else "project.update"
    audit(db, user, action, "project", project.id, status=project.status.value)
    db.commit()
    return project


# --- gallery ------------------------------------------------------------------------------


@dataclass
class GalleryQuery:
    q: str = ""
    event: str = ""
    track: str = ""
    tag: str = ""
    page: int = 1


def gallery(db: DbSession, query: GalleryQuery) -> tuple[list[Project], int]:
    """Public list: submitted, non-duplicate projects, alphabetical by title.

    Alphabetical is deliberately neutral: it does not reward submitting early or late.
    """
    stmt = select(Project).where(Project.status == ProjectStatus.submitted,
                                 Project.duplicate_of_id.is_(None))
    if query.q.strip():
        needle = query.q.strip().lower()
        stmt = stmt.where(or_(
            func.lower(Project.title).contains(needle, autoescape=True),
            func.lower(Project.tagline).contains(needle, autoescape=True),
            func.lower(Project.description).contains(needle, autoescape=True),
        ))
    if query.event:
        stmt = stmt.where(Project.event_id == query.event)
    if query.track:
        stmt = stmt.where(Project.track_id == query.track)
    if query.tag:
        stmt = stmt.where(Project.tags.any(ProjectTag.tag == query.tag.strip().lower()))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(query.page, 1)
    rows = db.scalars(stmt.options(selectinload(Project.tags), selectinload(Project.track),
                                   selectinload(Project.team))
                      .order_by(func.lower(Project.title), Project.id)
                      .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return list(rows), total


def all_tags(db: DbSession) -> list[str]:
    return list(db.scalars(select(ProjectTag.tag).distinct().order_by(ProjectTag.tag)))


def questions_for(db: DbSession, event_id: str) -> list[CustomQuestion]:
    return list(db.scalars(select(CustomQuestion).where(CustomQuestion.event_id == event_id)
                           .order_by(CustomQuestion.position)))


def project_out(project: Project) -> dict:
    return {
        "id": project.id, "event_id": project.event_id, "team_id": project.team_id,
        "team": project.team.name if project.team else None,
        "track_id": project.track_id, "track": project.track.name if project.track else None,
        "title": project.title, "tagline": project.tagline, "description": project.description,
        "thumbnail_url": project.thumbnail_url, "demo_video_url": project.demo_video_url,
        "repo_url": project.repo_url, "live_url": project.live_url,
        "tags": [t.tag for t in project.tags], "status": project.status.value,
        "submitted_at": project.submitted_at.isoformat() if project.submitted_at else None,
    }
