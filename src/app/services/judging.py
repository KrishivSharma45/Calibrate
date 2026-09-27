"""Scores: who may read and write them, and the weighted total.

Isolation rules (all enforced here, in the backend):
- A judge reads only rows where score.judge_id == their own id. Asking for any
  other judge id is 403, whether or not that judge exists, so ids are not probed.
- A judge writes a score only for a project that is (a) assigned to them and
  (b) in one of their tracks. (b) is a second net under (a): a wrong assignment
  still cannot put a judge outside their tracks.
- Organizers/admins read everything. They never write scores as a judge.
- Once an event's results are published, scores are frozen.
"""

from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.audit import audit
from app.auth import STAFF
from app.models import (
    Assignment,
    Event,
    JudgeTrack,
    Project,
    ProjectStatus,
    Role,
    RubricCriterion,
    Score,
    ScoreItem,
    User,
    new_id,
)


def criteria_for(db: DbSession, event_id: str) -> list[RubricCriterion]:
    return list(db.scalars(select(RubricCriterion).where(RubricCriterion.event_id == event_id)
                           .order_by(RubricCriterion.position)))


def weighted_total(score: Score, criteria: list[RubricCriterion]) -> float | None:
    """Sum of value x weight over the rubric. None if any criterion is missing."""
    values = {item.criterion_id: item.value for item in score.items}
    if any(c.id not in values for c in criteria):
        return None
    total = sum(Decimal(values[c.id]) * Decimal(c.weight) for c in criteria)
    return round(float(total), 4)


# --- reading ----------------------------------------------------------------------------


def readable_judge_id(user: User, requested: str | None) -> str | None:
    """Which judge's scores this user may read. None means 'all' (staff only)."""
    if user.role in STAFF:
        return requested or None
    if user.role == Role.judge:
        if requested and requested != user.id:
            raise HTTPException(status_code=403, detail="Judges can only read their own scores")
        return user.id
    raise HTTPException(status_code=403, detail="Not allowed for your role")


def list_scores(db: DbSession, judge_id: str | None, event_id: str | None = None) -> list[Score]:
    stmt = (select(Score).join(Project, Project.id == Score.project_id)
            .options(selectinload(Score.items)).order_by(Score.judge_id, Score.project_id))
    if judge_id is not None:
        stmt = stmt.where(Score.judge_id == judge_id)
    if event_id:
        stmt = stmt.where(Project.event_id == event_id)
    return list(db.scalars(stmt))


def scores_out(db: DbSession, scores: list[Score]) -> list[dict]:
    projects = {p.id: p for p in db.scalars(select(Project).where(
        Project.id.in_({s.project_id for s in scores})))}
    criteria_cache: dict[str, list[RubricCriterion]] = {}
    out = []
    for s in scores:
        project = projects[s.project_id]
        criteria = criteria_cache.setdefault(project.event_id, criteria_for(db, project.event_id))
        by_id = {c.id: c.key for c in criteria}
        out.append({
            "judge_id": s.judge_id, "project_id": s.project_id, "project_title": project.title,
            "event_id": project.event_id,
            "criteria": {by_id[i.criterion_id]: i.value for i in s.items if i.criterion_id in by_id},
            "weighted_total": weighted_total(s, criteria), "comment": s.comment,
            "updated_at": s.updated_at.isoformat(),
        })
    return out


# --- assignments visible to a judge ----------------------------------------------------------


def judge_track_ids(db: DbSession, judge_id: str) -> set[str]:
    return set(db.scalars(select(JudgeTrack.track_id).where(JudgeTrack.judge_id == judge_id)))


def assigned_projects(db: DbSession, judge: User) -> list[tuple[Project, Score | None]]:
    """The judge's queue: assigned projects in their tracks, with their score if any."""
    tracks = judge_track_ids(db, judge.id)
    projects = db.scalars(
        select(Project).join(Assignment, Assignment.project_id == Project.id)
        .where(Assignment.judge_id == judge.id, Project.track_id.in_(tracks))
        .options(selectinload(Project.track)).order_by(Project.event_id, Project.title)).all()
    scores = {s.project_id: s for s in list_scores(db, judge.id)}
    return [(p, scores.get(p.id)) for p in projects]


def project_for_judge(db: DbSession, judge: User, project_id: str) -> Project:
    project = db.get(Project, project_id)
    assigned = project is not None and db.get(Assignment, (judge.id, project.id)) is not None
    in_track = project is not None and project.track_id in judge_track_ids(db, judge.id)
    if not (assigned and in_track):
        # 404: a judge learns nothing about projects outside their queue.
        raise HTTPException(status_code=404, detail="Project not in your judging queue")
    return project


# --- writing ------------------------------------------------------------------------------------


@dataclass
class ScoreInput:
    values: dict[str, object]
    comment: str = ""


def save_score(db: DbSession, judge: User, project_id: str, data: ScoreInput) -> Score:
    if judge.role != Role.judge:
        raise HTTPException(status_code=403, detail="Only judges submit scores")
    project = project_for_judge(db, judge, project_id)
    if project.status != ProjectStatus.submitted or project.duplicate_of_id is not None:
        raise HTTPException(status_code=409, detail="This project is not open for judging")
    event = db.get(Event, project.event_id)
    if event.results_published_at is not None:
        raise HTTPException(status_code=409, detail={
            "error": "results_published", "message": "Results are published; scores are frozen."})

    criteria = criteria_for(db, event.id)
    errors, clean = [], {}
    for c in criteria:
        raw = data.values.get(c.key)
        try:
            value = int(raw)
            if str(raw).strip() != str(value) or not 1 <= value <= 5:
                raise ValueError
            clean[c.id] = value
        except (TypeError, ValueError):
            errors.append(f"{c.label}: give a whole number from 1 to 5.")
    unknown = set(data.values) - {c.key for c in criteria}
    if unknown:
        errors.append(f"Unknown criteria: {', '.join(sorted(unknown))}.")
    if len(data.comment) > 5000:
        errors.append("Comment is limited to 5000 characters.")
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    score = db.scalar(select(Score).where(Score.judge_id == judge.id, Score.project_id == project.id)
                      .options(selectinload(Score.items)))
    before = None
    if score is None:
        score = Score(id=new_id("sc"), judge_id=judge.id, project_id=project.id)
        db.add(score)
        action = "score.create"
    else:
        before = {i.criterion_id: i.value for i in score.items} | {"comment": score.comment}
        action = "score.update"
    items = {i.criterion_id: i for i in score.items}
    score.items = [items.get(cid) or ScoreItem(criterion_id=cid) for cid in clean]
    for item in score.items:
        item.value = clean[item.criterion_id]
    score.comment = data.comment.strip()
    db.flush()
    after = dict(clean) | {"comment": score.comment}
    audit(db, judge, action, "score", score.id, project_id=project.id, before=before, after=after)
    db.commit()
    return score
