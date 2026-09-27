"""Event creation and editing by organizers."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session as DbSession

from app.audit import audit
from app.models import CustomQuestion, Event, Prize, RubricCriterion, Track, User, new_id
from app.seed import DEFAULT_RUBRIC

MAX_LINES = 50


@dataclass
class EventInput:
    name: str = ""
    description: str = ""
    submissions_open: datetime | None = None
    submissions_close: datetime | None = None
    max_team_size: int = 4
    # Lists only ever *add* items on edit; existing tracks may already have projects.
    new_tracks: list[str] = field(default_factory=list)
    new_prizes: list[tuple[str, str]] = field(default_factory=list)
    new_questions: list[tuple[str, bool]] = field(default_factory=list)


def parse_utc(value: str | None) -> datetime | None:
    """Accept ISO 8601 ('2026-10-01T18:00:00Z') or a form's datetime-local ('2026-10-01T18:00').

    A value without an offset is taken as UTC: the form labels its fields "UTC".
    """
    if not value or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=[f"'{value}' is not a valid date and time."])
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse_prizes(text: str) -> list[tuple[str, str]]:
    """One prize per line: 'Name: description' (description optional)."""
    out = []
    for line in lines(text):
        name, _, desc = line.partition(":")
        out.append((name.strip(), desc.strip()))
    return out


def parse_questions(text: str) -> list[tuple[str, bool]]:
    """One question per line; a leading '*' marks it required."""
    return [(line.lstrip("*").strip(), line.startswith("*")) for line in lines(text)]


def validate(data: EventInput, existing: Event | None = None) -> list[str]:
    errors = []
    if not data.name.strip() or len(data.name) > 200:
        errors.append("Event name is required (up to 200 characters).")
    if data.submissions_close is None:
        errors.append("Submission deadline is required.")
    elif data.submissions_open and data.submissions_open >= data.submissions_close:
        errors.append("Submissions must open before they close.")
    if not 1 <= data.max_team_size <= 20:
        errors.append("Team size must be between 1 and 20.")
    if existing is None and not data.new_tracks:
        errors.append("Add at least one track.")
    taken = {t.name.lower() for t in existing.tracks} if existing else set()
    for name in data.new_tracks:
        if len(name) > 200:
            errors.append("Track names are limited to 200 characters.")
        if name.lower() in taken:
            errors.append(f"Duplicate track '{name}'.")
        taken.add(name.lower())
    for items in (data.new_tracks, data.new_prizes, data.new_questions):
        if len(items) > MAX_LINES:
            errors.append(f"At most {MAX_LINES} lines per list.")
    if any(not name for name, _ in data.new_prizes):
        errors.append("Each prize needs a name.")
    if any(len(q) > 500 for q, _ in data.new_questions):
        errors.append("Questions are limited to 500 characters.")
    return errors


def _add_children(event: Event, data: EventInput) -> None:
    # Append through the relationships (not db.add) so the loaded collections stay current.
    for name in data.new_tracks:
        event.tracks.append(Track(id=new_id("trk"), name=name))
    for name, desc in data.new_prizes:
        event.prizes.append(Prize(id=new_id("prz"), name=name, description=desc))
    start = len(event.questions)
    for i, (prompt, required) in enumerate(data.new_questions):
        event.questions.append(CustomQuestion(id=new_id("q"), prompt=prompt, required=required,
                                              position=start + i))


def create_event(db: DbSession, actor: User, data: EventInput) -> Event:
    errors = validate(data)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    event = Event(id=new_id("evt"), name=data.name.strip(), description=data.description.strip(),
                  submissions_open=data.submissions_open, submissions_close=data.submissions_close,
                  max_team_size=data.max_team_size)
    db.add(event)
    _add_children(event, data)
    for pos, (key, label, weight) in enumerate(DEFAULT_RUBRIC):
        event.criteria.append(RubricCriterion(id=f"crt_{event.id}_{key}", key=key, label=label,
                                              weight=weight, position=pos))
    db.flush()
    audit(db, actor, "event.create", "event", event.id, name=event.name,
          submissions_close=event.submissions_close)
    db.commit()
    return event


def update_event(db: DbSession, actor: User, event: Event, data: EventInput) -> Event:
    errors = validate(data, existing=event)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    old_open, old_close = event.submissions_open, event.submissions_close
    event.name = data.name.strip()
    event.description = data.description.strip()
    event.submissions_open = data.submissions_open
    event.submissions_close = data.submissions_close
    event.max_team_size = data.max_team_size
    _add_children(event, data)
    if (old_open, old_close) != (event.submissions_open, event.submissions_close):
        # Its own action so deadline moves stand out in the audit log.
        audit(db, actor, "event.deadline_change", "event", event.id,
              old_open=old_open, new_open=event.submissions_open,
              old_close=old_close, new_close=event.submissions_close)
    audit(db, actor, "event.update", "event", event.id, added_tracks=data.new_tracks,
          added_prizes=[p for p, _ in data.new_prizes],
          added_questions=[q for q, _ in data.new_questions])
    db.commit()
    return event


def event_out(event: Event) -> dict:
    iso = lambda d: d.isoformat().replace("+00:00", "Z") if d else None  # noqa: E731
    return {
        "id": event.id, "name": event.name, "description": event.description,
        "submissions_open": iso(event.submissions_open), "submissions_close": iso(event.submissions_close),
        "max_team_size": event.max_team_size,
        "tracks": [{"id": t.id, "name": t.name} for t in event.tracks],
        "prizes": [{"id": p.id, "name": p.name, "description": p.description} for p in event.prizes],
        "questions": [{"id": q.id, "prompt": q.prompt, "required": q.required} for q in event.questions],
        "rubric": [{"key": c.key, "label": c.label, "weight": float(c.weight)} for c in event.criteria],
    }
