"""Login, logout and registration (HTML forms and a JSON API)."""

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.audit import audit
from app.auth import (
    clear_session_cookie,
    current_user,
    login_throttle,
    require_user,
    safe_next,
    set_session_cookie,
)
from app.db import get_db
from app.models import Role, Session, User, new_id
from app.security import create_session, hash_password, hash_token, verify_password
from app.web import render

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10
# Checked against when the email is unknown, so a wrong email costs the same
# bcrypt time as a wrong password and response timing does not reveal accounts.
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


class LoginError(Exception):
    def __init__(self, message: str, status_code: int):
        self.message, self.status_code = message, status_code


def authenticate(db: DbSession, request: Request, email: str, password: str) -> User:
    email = email.strip().lower()
    ip = request.client.host if request.client else "unknown"
    keys = (f"email:{email}", f"ip:{ip}")
    if login_throttle.blocked(*keys):
        raise LoginError("Too many failed attempts. Try again in 15 minutes.", 429)
    user = db.scalar(select(User).where(User.email == email))
    if not verify_password(password, user.password_hash if user else _DUMMY_HASH) or user is None:
        login_throttle.fail(*keys)
        raise LoginError("Wrong email or password.", 401)
    login_throttle.reset(*keys)
    return user


def start_session(db: DbSession, request: Request, response, user: User) -> None:
    """Issue a brand-new session (and drop the old one) so a pre-login token can't be reused."""
    old = request.cookies.get("session")
    if old:
        stale = db.get(Session, hash_token(old))
        if stale is not None:
            db.delete(stale)
    token = create_session(db, user)
    db.commit()
    set_session_cookie(response, token)


def end_session(db: DbSession, request: Request, response) -> None:
    session = getattr(request.state, "session", None)
    if session is not None:
        db.delete(session)
        db.commit()
    clear_session_cookie(response)


def validate_registration(db: DbSession, email: str, name: str, password: str) -> list[str]:
    errors = []
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if not name.strip() or len(name) > 200:
        errors.append("Enter your name (up to 200 characters).")
    if len(password) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    if not errors and db.scalar(select(User.id).where(User.email == email)):
        errors.append("An account with this email already exists.")
    return errors


def create_participant(db: DbSession, email: str, name: str, password: str) -> User:
    # Role is hard-coded: self-registration can never create a judge or organizer.
    user = User(id=new_id("usr"), email=email, name=name.strip(),
                role=Role.participant, password_hash=hash_password(password))
    db.add(user)
    db.flush()  # audit_log has no ORM relationship to users, so insert the user first
    audit(db, user, "user.register", "user", user.id)
    return user


# --- HTML --------------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
def login_form(request: Request, email: str = Form(""), password: str = Form(""),
               next: str = Form("/"), db: DbSession = Depends(get_db)):
    try:
        user = authenticate(db, request, email, password)
    except LoginError as e:
        return render(request, "login.html", {"error": e.message, "email": email, "next": safe_next(next)},
                      status_code=e.status_code)
    response = RedirectResponse(safe_next(next), status_code=303)
    start_session(db, request, response, user)
    return response


@router.get("/register")
def register_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "register.html")


@router.post("/register")
def register_form(request: Request, email: str = Form(""), name: str = Form(""),
                  password: str = Form(""), db: DbSession = Depends(get_db)):
    email = email.strip().lower()
    errors = validate_registration(db, email, name, password)
    if errors:
        return render(request, "register.html", {"errors": errors, "email": email, "name": name},
                      status_code=400)
    user = create_participant(db, email, name, password)
    db.flush()
    response = RedirectResponse("/", status_code=303)
    start_session(db, request, response, user)
    return response


@router.post("/logout")
def logout_form(request: Request, db: DbSession = Depends(get_db)):
    response = RedirectResponse("/", status_code=303)
    end_session(db, request, response)
    return response


# --- JSON API ------------------------------------------------------------------


class LoginIn(BaseModel):
    email: str
    password: str


class RegisterIn(BaseModel):
    email: str
    name: str
    password: str


def user_out(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role.value}


@router.post("/api/login")
def api_login(body: LoginIn, request: Request, db: DbSession = Depends(get_db)):
    try:
        user = authenticate(db, request, body.email, body.password)
    except LoginError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    response = JSONResponse(user_out(user))
    start_session(db, request, response, user)
    return response


@router.post("/api/register", status_code=201)
def api_register(body: RegisterIn, request: Request, db: DbSession = Depends(get_db)):
    email = body.email.strip().lower()
    errors = validate_registration(db, email, body.name, body.password)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    user = create_participant(db, email, body.name, body.password)
    db.flush()
    response = JSONResponse(user_out(user), status_code=201)
    start_session(db, request, response, user)
    return response


@router.post("/api/logout")
def api_logout(request: Request, db: DbSession = Depends(get_db)):
    response = JSONResponse({"ok": True})
    end_session(db, request, response)
    return response


@router.get("/api/me")
def api_me(user: User = Depends(require_user)):
    return user_out(user)
