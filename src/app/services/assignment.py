"""Judge assignment, the rubric editor and the judging progress view.

Auto-assignment (documented in JUDGING.md):
  eligible project = submitted, not a flagged duplicate, has a track
  eligible judge   = a judge attached to that track
  1. Order projects most-constrained first: fewest eligible judges, then fewest
     current assignments, then id. Otherwise easy projects use up the judges
     the hard ones need.
  2. For each missing slot (target - current), pick the eligible judge with the
     lowest load in this event (ties: judge id). Deterministic and repeatable.
  3. Only ever add. Existing assignments (and the scores behind them) are kept,
     so a second run adds nothing. A track with fewer judges than the target is
     reported as short instead of borrowing judges from other tracks.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.audit import audit
from app.models import (
    Assignment,
    Event,
    JudgeTrack,
    Project,
    ProjectStatus,
    Role,
    RubricCriterion,
    Score,
    Track,
    User,
)

DEFAULT_TARGET = 3
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


# --- shared queries -----------------------------------------------------------------------


def eligible_projects(db: DbSession, event_id: str) -> list[Project]:
    return list(db.scalars(select(Project).where(
        Project.event_id == event_id, Project.status == ProjectStatus.submitted,
        Project.duplicate_of_id.is_(None), Project.track_id.is_not(None)).order_by(Project.id)))


def judges_by_track(db: DbSession, event_id: str) -> dict[str, list[str]]:
    rows = db.execute(select(JudgeTrack.track_id, JudgeTrack.judge_id)
                      .join(Track, Track.id == JudgeTrack.track_id)
                      .join(User, User.id == JudgeTrack.judge_id)
                      .where(Track.event_id == event_id, User.role == Role.judge)
                      .order_by(JudgeTrack.judge_id))
    out: dict[str, list[str]] = defaultdict(list)
    for track_id, judge_id in rows:
        out[track_id].append(judge_id)
    return out


def event_assignments(db: DbSession, event_id: str) -> list[Assignment]:
    return list(db.scalars(select(Assignment).join(Project, Project.id == Assignment.project_id)
                           .where(Project.event_id == event_id)))


def has_scores(db: DbSession, event_id: str) -> bool:
    return db.scalar(select(func.count()).select_from(Score).join(Project, Project.id == Score.project_id)
                     .where(Project.event_id == event_id)) > 0


# --- auto-assignment ------------------------------------------------------------------------------


@dataclass
class AssignResult:
    added: list[tuple[str, str]] = field(default_factory=list)   # (judge_id, project_id)
    short: dict[str, int] = field(default_factory=dict)          # project_id -> slots unfilled


def plan_assignments(projects: list[Project], judges_in_track: dict[str, list[str]],
                     existing: set[tuple[str, str]], target: int) -> AssignResult:
    """Pure function (no DB) so the algorithm is easy to test and to explain."""
    per_project = defaultdict(set)
    load = defaultdict(int)
    for judge_id, project_id in existing:
        per_project[project_id].add(judge_id)
        load[judge_id] += 1

    order = sorted(projects, key=lambda p: (len(judges_in_track.get(p.track_id, [])),
                                            len(per_project[p.id]), p.id))
    result = AssignResult()
    for project in order:
        need = target - len(per_project[project.id])
        candidates = [j for j in judges_in_track.get(project.track_id, []) if j not in per_project[project.id]]
        while need > 0 and candidates:
            judge = min(candidates, key=lambda j: (load[j], j))
            candidates.remove(judge)
            per_project[project.id].add(judge)
            load[judge] += 1
            result.added.append((judge, project.id))
            need -= 1
        if need > 0:
            result.short[project.id] = need
    return result


def auto_assign(db: DbSession, actor: User, event: Event, target: int = DEFAULT_TARGET) -> AssignResult:
    if not 1 <= target <= 10:
        raise HTTPException(status_code=422, detail=["Target reviews per project must be 1-10."])
    existing = {(a.judge_id, a.project_id) for a in event_assignments(db, event.id)}
    result = plan_assignments(eligible_projects(db, event.id), judges_by_track(db, event.id), existing, target)
    for judge_id, project_id in result.added:
        db.add(Assignment(judge_id=judge_id, project_id=project_id))
    audit(db, actor, "assignment.auto", "event", event.id, target=target, added=len(result.added),
          short=result.short)
    db.commit()
    return result


def add_assignment(db: DbSession, actor: User, event: Event, judge_id: str, project_id: str) -> None:
    project = db.get(Project, project_id)
    judge = db.get(User, judge_id)
    if project is None or project.event_id != event.id:
        raise HTTPException(status_code=404, detail="Project not in this event")
    if judge is None or judge.role != Role.judge:
        raise HTTPException(status_code=422, detail=["That user is not a judge."])
    if project.track_id not in judges_by_track(db, event.id) or \
            judge_id not in judges_by_track(db, event.id)[project.track_id]:
        raise HTTPException(status_code=422, detail=["That judge is not attached to this project's track."])
    if db.get(Assignment, (judge_id, project_id)) is None:
        db.add(Assignment(judge_id=judge_id, project_id=project_id))
        audit(db, actor, "assignment.add", "assignment", f"{judge_id}:{project_id}")
        db.commit()


def remove_assignment(db: DbSession, actor: User, event: Event, judge_id: str, project_id: str) -> None:
    project = db.get(Project, project_id)
    assignment = db.get(Assignment, (judge_id, project_id))
    if project is None or project.event_id != event.id or assignment is None:
        raise HTTPException(status_code=404, detail="No such assignment")
    if db.scalar(select(Score.id).where(Score.judge_id == judge_id, Score.project_id == project_id)):
        raise HTTPException(status_code=409, detail="This judge already scored the project; keep the assignment")
    db.delete(assignment)
    audit(db, actor, "assignment.remove", "assignment", f"{judge_id}:{project_id}")
    db.commit()


# --- rubric --------------------------------------------------------------------------------------------


@dataclass
class CriterionInput:
    key: str
    label: str
    weight: Decimal


def parse_weight(raw, percent: bool) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail=[f"'{raw}' is not a number."])
    return value / 100 if percent else value


def update_rubric(db: DbSession, actor: User, event: Event, rows: list[CriterionInput]) -> list[RubricCriterion]:
    """Replace the rubric with `rows` (matched by key). Weights must sum to 1."""
    errors = []
    if not 1 <= len(rows) <= 10:
        errors.append("A rubric needs 1 to 10 criteria.")
    keys = [r.key for r in rows]
    if len(set(keys)) != len(keys):
        errors.append("Criterion keys must be unique.")
    for r in rows:
        if not KEY_RE.match(r.key):
            errors.append(f"Key '{r.key}' must be lower-case letters, digits or _ (starting with a letter).")
        if not r.label.strip() or len(r.label) > 100:
            errors.append(f"Criterion '{r.key}' needs a label (up to 100 characters).")
        if not Decimal(0) <= r.weight <= Decimal(1):
            errors.append(f"Weight of '{r.key}' must be between 0 and 1 (0% and 100%).")
    total = sum((r.weight for r in rows), Decimal(0))
    if abs(total - 1) > Decimal("0.001"):
        errors.append(f"Weights must add up to 100% (they add up to {float(total) * 100:g}%).")

    current = {c.key: c for c in event.criteria}
    if has_scores(db, event.id) and set(keys) != set(current):
        errors.append("Judging has started: you can change weights and labels, "
                      "but not add or remove criteria (existing scores would be incomplete).")
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    before = {k: float(c.weight) for k, c in current.items()}
    for key, criterion in current.items():
        if key not in keys:  # only reachable before any score exists (checked above)
            event.criteria.remove(criterion)
            db.delete(criterion)
    for pos, r in enumerate(rows):
        criterion = current.get(r.key)
        if criterion is None:
            criterion = RubricCriterion(id=f"crt_{event.id}_{r.key}", key=r.key)
            event.criteria.append(criterion)
        criterion.label, criterion.weight, criterion.position = r.label.strip(), r.weight, pos
    audit(db, actor, "rubric.update", "event", event.id, before=before,
          after={r.key: float(r.weight) for r in rows})
    db.commit()
    db.refresh(event)
    return list(event.criteria)


# --- progress -----------------------------------------------------------------------------------------


def progress(db: DbSession, event: Event, target: int = DEFAULT_TARGET) -> dict:
    """Per judge: assigned / done / status. Per project: reviews vs target."""
    projects = {p.id: p for p in eligible_projects(db, event.id)}
    assignments = event_assignments(db, event.id)
    scored = {(j, p) for j, p in db.execute(select(Score.judge_id, Score.project_id)
                                            .join(Project, Project.id == Score.project_id)
                                            .where(Project.event_id == event.id))}
    track_judges = judges_by_track(db, event.id)
    judge_ids = {j for js in track_judges.values() for j in js} | {a.judge_id for a in assignments}
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_(judge_ids)))}

    judges = []
    for judge_id in sorted(judge_ids):
        mine = [a for a in assignments if a.judge_id == judge_id]
        done = sum(1 for a in mine if (judge_id, a.project_id) in scored)
        if not mine:
            status = "no_assignments"
        elif done == 0:
            status = "not_started"
        elif done < len(mine):
            status = "incomplete"
        else:
            status = "complete"
        judges.append({"judge_id": judge_id, "name": names.get(judge_id, judge_id),
                       "assigned": len(mine), "done": done, "remaining": len(mine) - done, "status": status})

    project_rows = []
    for pid, p in sorted(projects.items(), key=lambda kv: kv[1].title.lower()):
        assigned = [a.judge_id for a in assignments if a.project_id == pid]
        reviews = sum(1 for (j, sp) in scored if sp == pid)
        project_rows.append({"project_id": pid, "title": p.title, "track_id": p.track_id,
                             "assigned": len(assigned), "reviews": reviews,
                             "eligible_judges": len(track_judges.get(p.track_id, [])),
                             "below_target": reviews < target})
    total_assigned = len(assignments)
    total_done = sum(1 for a in assignments if (a.judge_id, a.project_id) in scored)
    return {"event_id": event.id, "target": target, "judges": judges, "projects": project_rows,
            "totals": {"assignments": total_assigned, "done": total_done,
                       "percent": round(100 * total_done / total_assigned) if total_assigned else 0,
                       "judges_not_started": sum(1 for j in judges if j["status"] == "not_started"),
                       "judges_incomplete": sum(1 for j in judges if j["status"] == "incomplete"),
                       "projects_below_target": sum(1 for p in project_rows if p["below_target"]),
                       "projects_unreviewed": sum(1 for p in project_rows if p["reviews"] == 0)}}
