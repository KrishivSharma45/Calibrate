"""Judge scoring (API + pages) and organizer CSV exports."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from app.audit import audit
from app.auth import STAFF, require_role, require_user
from app.db import get_db
from app.models import Event, Role, User
from app.services import export as export_svc
from app.services import judging as svc
from app.services.projects import questions_for
from app.web import read_body, render, wants_html

router = APIRouter()
judge_only = require_role(Role.judge)


# --- scores API (the run.py T2 routes) -------------------------------------------------------


@router.get("/api/judge/scores")
def judge_scores(judge: str | None = None, event: str | None = None,
                 user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    """Judges: their own scores only (?judge=<other id> is 403). Staff: any judge, or all."""
    judge_id = svc.readable_judge_id(user, judge)
    if judge_id is not None and user.role in STAFF and db.get(User, judge_id) is None:
        raise HTTPException(status_code=404, detail="No such judge")
    return {"judge_id": judge_id, "scores": svc.scores_out(db, svc.list_scores(db, judge_id, event))}


@router.get("/api/judge/assignments")
def judge_assignments(user: User = Depends(judge_only), db: DbSession = Depends(get_db)):
    return [{"project_id": p.id, "title": p.title, "event_id": p.event_id,
             "track": p.track.name if p.track else None, "scored": s is not None}
            for p, s in svc.assigned_projects(db, user)]


def _score_input(fields: dict, is_json: bool) -> tuple[str, svc.ScoreInput]:
    if is_json:
        values = fields.get("criteria") or {}
        if not isinstance(values, dict):
            raise HTTPException(status_code=422, detail="criteria must be an object")
        return str(fields.get("project_id") or ""), svc.ScoreInput(values, str(fields.get("comment") or ""))
    values = {k.removeprefix("c_"): v for k, v in fields.items() if k.startswith("c_")}
    return str(fields.get("project_id") or ""), svc.ScoreInput(values, str(fields.get("comment") or ""))


@router.post("/api/judge/scores")
async def submit_score(request: Request, user: User = Depends(judge_only), db: DbSession = Depends(get_db)):
    fields, is_json = await read_body(request)
    project_id, data = _score_input(fields, is_json)
    score = await run_in_threadpool(svc.save_score, db, user, project_id, data)
    return svc.scores_out(db, [score])[0]


# --- judge pages ------------------------------------------------------------------------------


@router.get("/judge")
def judge_home(request: Request, user: User = Depends(judge_only), db: DbSession = Depends(get_db)):
    queue = svc.assigned_projects(db, user)
    return render(request, "judge_home.html", {
        "queue": queue, "done": sum(1 for _, s in queue if s is not None)})


def _score_page(request, db, user, project, errors=None, values=None, comment=None, status=200):
    event = db.get(Event, project.event_id)
    criteria = svc.criteria_for(db, event.id)
    score = next((s for p, s in svc.assigned_projects(db, user) if p.id == project.id), None)
    current = {c.key: i.value for c in criteria for i in (score.items if score else []) if i.criterion_id == c.id}
    return render(request, "judge_score.html", {
        "project": project, "event": event, "criteria": criteria, "score": score,
        "values": values if values is not None else current,
        "comment": comment if comment is not None else (score.comment if score else ""),
        "errors": errors or [], "locked": event.results_published_at is not None,
        "questions": {q.id: q for q in questions_for(db, event.id)},
    }, status_code=status)


@router.get("/judge/projects/{project_id}")
def score_page(request: Request, project_id: str, user: User = Depends(judge_only),
               db: DbSession = Depends(get_db)):
    return _score_page(request, db, user, svc.project_for_judge(db, user, project_id))


@router.post("/judge/projects/{project_id}")
async def score_form(request: Request, project_id: str, user: User = Depends(judge_only),
                     db: DbSession = Depends(get_db)):
    fields, is_json = await read_body(request)
    _, data = _score_input(fields, is_json)

    def work():
        try:
            svc.save_score(db, user, project_id, data)
            return None
        except HTTPException as e:
            if e.status_code == 422 and wants_html(request):
                project = svc.project_for_judge(db, user, project_id)
                return _score_page(request, db, user, project, e.detail, data.values, data.comment, 422)
            raise

    page = await run_in_threadpool(work)
    return page or RedirectResponse("/judge?saved=1", status_code=303)


# --- CSV export -----------------------------------------------------------------------------------

EXPORTS = {"scores": export_svc.scores_csv, "projects": export_svc.projects_csv,
           "progress": export_svc.progress_csv, "results": export_svc.results_csv,
           "audit": export_svc.audit_csv}


@router.get("/api/export.csv")
def export_csv(kind: str = "scores", event: str | None = None,
               user: User = Depends(require_role(*STAFF)), db: DbSession = Depends(get_db)):
    if kind not in EXPORTS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {', '.join(EXPORTS)}")
    body = EXPORTS[kind](db, event)
    audit(db, user, "export.csv", "export", kind, event_id=event)
    db.commit()
    name = f"{kind}{'-' + event if event else ''}.csv"
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
