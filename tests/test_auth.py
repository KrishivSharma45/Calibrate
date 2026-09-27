"""Login, sessions, roles and CSRF, tested over HTTP like curl would."""

import re
from datetime import timedelta

import pytest
from fastapi import APIRouter, Depends

from app.auth import STAFF, require_role, safe_next
from app.main import app
from app.models import Role, Session, User, utcnow
from app.security import hash_token
from app.seed import DEMO_PASSWORD, DEMO_SESSION_TOKENS, ORGANIZER_EMAIL

# A throwaway route to exercise require_role directly.
_probe = APIRouter()


@_probe.get("/api/__test/staff-only")
def _staff_only(user: User = Depends(require_role(*STAFF))):
    return {"ok": user.id}


app.include_router(_probe)

HTML = {"accept": "text/html"}


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


# --- sessions and roles ---------------------------------------------------------


def test_me_requires_login(client):
    assert client.get("/api/me").status_code == 401


def test_demo_session_identifies_user(client, seeded):
    resp = client.get("/api/me", headers=demo("organizer"))
    assert resp.status_code == 200 and resp.json()["role"] == "organizer"


@pytest.mark.parametrize("label,status", [
    ("organizer", 200), ("judge_a", 403), ("participant", 403),
])
def test_require_role(client, seeded, label, status):
    assert client.get("/api/__test/staff-only", headers=demo(label)).status_code == status


def test_require_role_anonymous_is_401(client):
    assert client.get("/api/__test/staff-only").status_code == 401


def test_garbage_or_expired_session_is_anonymous(client, seeded):
    assert client.get("/api/me", headers={"cookie": "session=not-a-real-token"}).status_code == 401
    s = seeded.get(Session, hash_token(DEMO_SESSION_TOKENS["organizer"]))
    s.expires_at = utcnow() - timedelta(seconds=1)
    seeded.commit()
    assert client.get("/api/me", headers=demo("organizer")).status_code == 401
    assert seeded.get(Session, hash_token(DEMO_SESSION_TOKENS["organizer"])) is None


# --- login / logout ----------------------------------------------------------------


def test_json_login_sets_hardened_cookie(client, seeded):
    resp = client.post("/api/login", json={"email": ORGANIZER_EMAIL, "password": DEMO_PASSWORD})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/me").json()["email"] == ORGANIZER_EMAIL


def test_login_rotates_session(client, seeded):
    resp = client.post("/api/login", headers=demo("organizer"),
                       json={"email": ORGANIZER_EMAIL, "password": DEMO_PASSWORD})
    assert resp.cookies.get("session") not in (None, DEMO_SESSION_TOKENS["organizer"])
    assert seeded.get(Session, hash_token(DEMO_SESSION_TOKENS["organizer"])) is None


def test_wrong_password_and_unknown_email_look_the_same(client, seeded):
    a = client.post("/api/login", json={"email": ORGANIZER_EMAIL, "password": "wrong"})
    b = client.post("/api/login", json={"email": "nobody@x.org", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_login_throttled_after_repeated_failures(client, seeded):
    for _ in range(10):
        client.post("/api/login", json={"email": ORGANIZER_EMAIL, "password": "wrong"})
    resp = client.post("/api/login", json={"email": ORGANIZER_EMAIL, "password": DEMO_PASSWORD})
    assert resp.status_code == 429


def test_logout_kills_session_server_side(client, seeded):
    client.post("/api/login", json={"email": ORGANIZER_EMAIL, "password": DEMO_PASSWORD})
    token = client.cookies.get("session")
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/me", headers={"cookie": f"session={token}"}).status_code == 401


def test_html_login_flow_and_no_open_redirect(client, seeded):
    token = csrf_from(client.get("/login", headers=HTML).text)
    resp = client.post("/login", follow_redirects=False, data={
        "csrf_token": token, "email": ORGANIZER_EMAIL, "password": DEMO_PASSWORD,
        "next": "//evil.example"})
    assert resp.status_code == 303 and resp.headers["location"] == "/"


@pytest.mark.parametrize("target,expected", [
    ("/projects?x=1", "/projects?x=1"), ("//evil.com", "/"), ("https://evil.com", "/"),
    ("/\\evil.com", "/"), ("", "/"), (None, "/"),
])
def test_safe_next(target, expected):
    assert safe_next(target) == expected


# --- registration ---------------------------------------------------------------------


def test_register_cannot_choose_role(client, db):
    token = csrf_from(client.get("/register", headers=HTML).text)
    resp = client.post("/register", follow_redirects=False, data={
        "csrf_token": token, "email": "New@X.org", "name": "New", "password": "long-enough-pw",
        "role": "organizer"})
    assert resp.status_code == 303
    me = client.get("/api/me").json()
    assert me["role"] == Role.participant.value and me["email"] == "new@x.org"


def test_register_rejects_weak_password_and_duplicate_email(client, seeded):
    bad = client.post("/api/register", json={"email": "a@b.org", "name": "A", "password": "short"})
    assert bad.status_code == 400
    dup = client.post("/api/register", json={"email": ORGANIZER_EMAIL, "name": "X",
                                             "password": "long-enough-pw"})
    assert dup.status_code == 400


# --- CSRF ------------------------------------------------------------------------------


def test_form_post_without_csrf_token_is_refused(client, seeded):
    resp = client.post("/logout", headers=demo("organizer"), data={})
    assert resp.status_code == 403
    assert client.get("/api/me", headers=demo("organizer")).status_code == 200  # still logged in


def test_form_post_with_wrong_csrf_token_is_refused(client, seeded):
    resp = client.post("/logout", headers=demo("organizer"), data={"csrf_token": "guess"})
    assert resp.status_code == 403


def test_text_plain_post_is_refused(client, seeded):
    """The classic trick: an HTML form with enctype=text/plain faking a JSON body."""
    resp = client.post("/api/logout", headers={**demo("organizer"), "content-type": "text/plain"},
                       content=b'{"a":1}')
    assert resp.status_code == 403


def test_anonymous_form_needs_matching_cookie(client, db):
    resp = client.post("/login", data={"csrf_token": "made-up", "email": "x@y.org", "password": "p"})
    assert resp.status_code == 403


def test_logged_in_form_post_with_session_token_works(client, seeded):
    client.cookies.set("session", DEMO_SESSION_TOKENS["participant"])
    token = csrf_from(client.get("/", headers=HTML).text)
    assert client.post("/logout", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    assert client.get("/api/me").status_code == 401


# --- misc ---------------------------------------------------------------------------------


def test_security_headers(client):
    headers = client.get("/").headers
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
