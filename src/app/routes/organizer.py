"""Organizer judging tools: event hub, rubric, assignments, progress. Staff only."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from app.auth import STAFF, require_role
from app.db import get_db
from app.models import Project, Score, User
from app.routes.events import get_event
from app.services import assignment as svc
from app.web import read_body, render, wants_html

router = APIRouter()
staff = require_role(*STAFF)


def _back(request: Request, path: str, payload: dict | None = None):
    if wants_html(request):
        return RedirectResponse(path, status_code=303)
    return JSONResponse(payload or {"ok": True})


@router.get("/organizer/events/{event_id}")
def event_hub(request: Request, event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    event = get_event(db, event_id)
    return render(request, "org_event.html", {"event": event, "p": svc.progress(db, event)})


# --- rubric -------------------------------------------------------------------------------------------


def _rubric_page(request, db, event, rows=None, errors=None, status=200):
    if rows is None:
        rows = [{"key": c.key, "label": c.label, "weight": f"{float(c.weight) * 100:g}"} for c in event.criteria]
    return render(request, "org_rubric.html", {
        "event": event, "rows": rows, "errors": errors or [], "locked": svc.has_scores(db, event.id)},
        status_code=status)


@router.get("/organizer/events/{event_id}/rubric")
def rubric_page(request: Request, event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    return _rubric_page(request, db, get_event(db, event_id))


@router.get("/api/events/{event_id}/rubric")
def rubric_api(event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    return [{"key": c.key, "label": c.label, "weight": float(c.weight)} for c in get_event(db, event_id).criteria]


@router.post("/organizer/events/{event_id}/rubric")
@router.post("/api/events/{event_id}/rubric")
async def save_rubric(request: Request, event_id: str, user: User = Depends(staff),
                      db: DbSession = Depends(get_db)):
    fields, is_json = await read_body(request)
    event = get_event(db, event_id)

    def work():
        if is_json:  # {"criteria": [{"key", "label", "weight" (0-1)}]}
            raw = fields.get("criteria") or []
            if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
                raise HTTPException(status_code=422, detail=["criteria must be a list of objects"])
            rows = [svc.CriterionInput(str(r.get("key", "")).strip(), str(r.get("label", "")),
                                       svc.parse_weight(r.get("weight", ""), percent=False)) for r in raw]
            return svc.update_rubric(db, user, event, rows), None
        # form: key_N / label_N / weight_N (percent); blank key = unused row
        form_rows = [{"key": str(fields.get(f"key_{i}", "")).strip().lower(),
                      "label": str(fields.get(f"label_{i}", "")), "weight": str(fields.get(f"weight_{i}", ""))}
                     for i in range(10)]
        form_rows = [r for r in form_rows if r["key"]]
        try:
            rows = [svc.CriterionInput(r["key"], r["label"], svc.parse_weight(r["weight"] or "0", percent=True))
                    for r in form_rows]
            return svc.update_rubric(db, user, event, rows), None
        except HTTPException as e:
            if e.status_code == 422:
                return None, _rubric_page(request, db, event, form_rows, e.detail, 422)
            raise

    criteria, error_page = await run_in_threadpool(work)
    if error_page is not None:
        return error_page
    return _back(request, f"/organizer/events/{event_id}",
                 {"criteria": [{"key": c.key, "label": c.label, "weight": float(c.weight)} for c in criteria]})


# --- assignments ---------------------------------------------------------------------------------------


@router.get("/organizer/events/{event_id}/assignments")
def assignments_page(request: Request, event_id: str, user: User = Depends(staff),
                     db: DbSession = Depends(get_db)):
    event = get_event(db, event_id)
    progress = svc.progress(db, event)
    by_project: dict[str, list[str]] = {}
    for a in svc.event_assignments(db, event.id):
        by_project.setdefault(a.project_id, []).append(a.judge_id)
    names = {j["judge_id"]: j["name"] for j in progress["judges"]}
    return render(request, "org_assignments.html", {
        "event": event, "rows": progress["projects"], "by_project": by_project, "names": names,
        "track_judges": svc.judges_by_track(db, event.id), "scored": _scored_pairs(db, event.id),
        "tracks": {t.id: t.name for t in event.tracks}, "result": request.query_params.get("added")})


def _scored_pairs(db: DbSession, event_id: str) -> set[tuple[str, str]]:
    rows = db.execute(select(Score.judge_id, Score.project_id).join(Project, Project.id == Score.project_id)
                      .where(Project.event_id == event_id))
    return {(judge_id, project_id) for judge_id, project_id in rows}  # plain tuples for `in` checks


@router.post("/organizer/events/{event_id}/assignments/auto")
@router.post("/api/events/{event_id}/assignments/auto")
async def auto_assign(request: Request, event_id: str, user: User = Depends(staff),
                      db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    try:
        target = int(fields.get("target") or svc.DEFAULT_TARGET)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=["target must be a whole number"])
    result = await run_in_threadpool(svc.auto_assign, db, user, get_event(db, event_id), target)
    return _back(request, f"/organizer/events/{event_id}/assignments?added={len(result.added)}",
                 {"added": [{"judge_id": j, "project_id": p} for j, p in result.added], "short": result.short})


@router.post("/organizer/events/{event_id}/assignments/add")
@router.post("/api/events/{event_id}/assignments")
async def add_assignment(request: Request, event_id: str, user: User = Depends(staff),
                         db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    await run_in_threadpool(svc.add_assignment, db, user, get_event(db, event_id),
                            str(fields.get("judge_id") or ""), str(fields.get("project_id") or ""))
    return _back(request, f"/organizer/events/{event_id}/assignments")


@router.post("/organizer/events/{event_id}/assignments/remove")
@router.post("/api/events/{event_id}/assignments/remove")
async def remove_assignment(request: Request, event_id: str, user: User = Depends(staff),
                            db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    await run_in_threadpool(svc.remove_assignment, db, user, get_event(db, event_id),
                            str(fields.get("judge_id") or ""), str(fields.get("project_id") or ""))
    return _back(request, f"/organizer/events/{event_id}/assignments")


# --- progress --------------------------------------------------------------------------------------------


@router.get("/api/events/{event_id}/progress")
def progress_api(event_id: str, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    return svc.progress(db, get_event(db, event_id))
