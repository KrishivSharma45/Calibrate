"""Judge management (organizers), invite acceptance (public link), role changes (admins)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload
from starlette.concurrency import run_in_threadpool

from app.auth import STAFF, require_role
from app.db import get_db
from app.models import Event, Role, User
from app.routes.auth import start_session
from app.services import people as svc
from app.web import read_body, render, wants_html

router = APIRouter()
staff = require_role(*STAFF)
admin_only = require_role(Role.admin)


def _judges_page(request: Request, db: DbSession, invite_url: str | None = None, errors=None, status=200):
    judges = db.scalars(select(User).where(User.role == Role.judge).options(selectinload(User.judge_tracks))
                        .order_by(User.name)).all()
    events = db.scalars(select(Event).order_by(Event.submissions_close.desc())).all()
    return render(request, "org_judges.html", {"judges": judges, "events": events, "invite_url": invite_url,
                                               "errors": errors or []}, status_code=status)


@router.get("/organizer/judges")
def judges_page(request: Request, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    return _judges_page(request, db)


@router.get("/api/judges")
def judges_api(user: User = Depends(staff), db: DbSession = Depends(get_db)):
    judges = db.scalars(select(User).where(User.role == Role.judge).options(selectinload(User.judge_tracks))
                        .order_by(User.id)).all()
    return [{"id": j.id, "name": j.name, "email": j.email, "can_log_in": j.password_hash is not None,
             "tracks": sorted(t.track_id for t in j.judge_tracks)} for j in judges]


@router.post("/organizer/judges/invite")
@router.post("/api/judges/invite")
async def invite(request: Request, user: User = Depends(staff), db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)

    def work():
        try:
            judge, token = svc.invite_judge(db, user, str(fields.get("email") or ""), str(fields.get("name") or ""),
                                            svc.as_list(fields.get("track_ids")))
        except HTTPException as e:
            if wants_html(request) and e.status_code in (409, 422):
                detail = e.detail.get("message") if isinstance(e.detail, dict) else e.detail
                return None, None, _judges_page(request, db, errors=detail if isinstance(detail, list) else [detail],
                                                status=e.status_code)
            raise
        return judge, token, None

    judge, token, page = await run_in_threadpool(work)
    if page is not None:
        return page
    url = f"{str(request.base_url).rstrip('/')}/invite/{token}" if token else None
    if wants_html(request):
        return _judges_page(request, db, invite_url=url)
    return JSONResponse({"judge_id": judge.id, "invite_url": url}, status_code=201)


@router.post("/organizer/judges/{judge_id}/tracks")
@router.post("/api/judges/{judge_id}/tracks")
async def judge_tracks(request: Request, judge_id: str, user: User = Depends(staff),
                       db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    judge = await run_in_threadpool(svc.set_judge_tracks, db, user, judge_id, svc.as_list(fields.get("track_ids")))
    if wants_html(request):
        return RedirectResponse("/organizer/judges", status_code=303)
    return {"judge_id": judge.id, "tracks": sorted(t.track_id for t in judge.judge_tracks)}


# --- invite acceptance (anyone holding the link) ------------------------------------------------


@router.get("/invite/{token}")
def invite_page(request: Request, token: str, db: DbSession = Depends(get_db)):
    invite = svc.find_invite(db, token)
    return render(request, "invite.html", {"judge": db.get(User, invite.user_id), "token": token})


@router.post("/invite/{token}")
async def accept_invite(request: Request, token: str, db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)

    def work():
        try:
            return svc.accept_invite(db, token, str(fields.get("password") or "")), None
        except HTTPException as e:
            if e.status_code == 422 and wants_html(request):
                invite = svc.find_invite(db, token)
                return None, render(request, "invite.html", {"judge": db.get(User, invite.user_id), "token": token,
                                                             "errors": e.detail}, status_code=422)
            raise

    judge, page = await run_in_threadpool(work)
    if page is not None:
        return page
    response = RedirectResponse("/judge", status_code=303) if wants_html(request) \
        else JSONResponse({"id": judge.id, "role": judge.role.value})
    start_session(db, request, response, judge)
    return response


# --- admin: roles ----------------------------------------------------------------------------------


@router.get("/admin/users")
def users_page(request: Request, q: str = "", user: User = Depends(admin_only), db: DbSession = Depends(get_db)):
    stmt = select(User).order_by(User.role, User.name).limit(200)
    if q.strip():
        needle = q.strip().lower()
        stmt = stmt.where(User.email.contains(needle, autoescape=True) | User.name.ilike(f"%{needle}%"))
    return render(request, "admin_users.html", {"users": db.scalars(stmt).all(), "roles": list(Role), "q": q})


@router.post("/admin/users/{user_id}/role")
@router.post("/api/admin/users/{user_id}/role")
async def change_role(request: Request, user_id: str, user: User = Depends(admin_only),
                      db: DbSession = Depends(get_db)):
    fields, _ = await read_body(request)
    target = await run_in_threadpool(svc.change_role, db, user, user_id, str(fields.get("role") or ""))
    if wants_html(request):
        return RedirectResponse("/admin/users", status_code=303)
    return {"id": target.id, "role": target.role.value}
