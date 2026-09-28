"""Judge invitations and admin role changes."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, Invite, Role, User, utcnow
from app.security import create_session
from app.seed import DEMO_SESSION_TOKENS, JUDGE_A

ORG = {"cookie": f"session={DEMO_SESSION_TOKENS['organizer']}"}


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def as_user(db, user_id: str) -> dict:
    token = create_session(db, db.get(User, user_id))
    db.commit()
    return {"cookie": f"session={token}"}


def invite(client, **body):
    payload = {"email": "new.judge@test.org", "name": "New Judge", "track_ids": ["trk_demo_1"], **body}
    return client.post("/api/judges/invite", headers=ORG, json=payload)


# --- invitations ---------------------------------------------------------------------------------


def test_invite_creates_judge_who_sets_password_once(client, seeded):
    resp = invite(client)
    assert resp.status_code == 201
    token = resp.json()["invite_url"].rsplit("/", 1)[1]
    judge = seeded.get(User, resp.json()["judge_id"])
    assert judge.role == Role.judge and judge.password_hash is None
    assert client.post("/api/login", json={"email": "new.judge@test.org", "password": "x" * 12}).status_code == 401

    accepted = client.post(f"/invite/{token}", json={"password": "a-strong-password"})
    assert accepted.status_code == 200 and client.get("/api/me").json()["role"] == "judge"
    client.cookies.clear()
    assert client.post(f"/invite/{token}", json={"password": "another-password"}).status_code == 404  # single use
    assert client.post("/api/login", json={"email": "new.judge@test.org",
                                           "password": "a-strong-password"}).status_code == 200


def test_invite_token_is_stored_hashed(client, seeded):
    token = invite(client).json()["invite_url"].rsplit("/", 1)[1]
    assert seeded.get(Invite, token) is None
    assert seeded.scalar(select(Invite)).token_hash != token


def test_reinvite_cancels_old_link(client, seeded):
    old = invite(client).json()["invite_url"].rsplit("/", 1)[1]
    new = invite(client).json()["invite_url"].rsplit("/", 1)[1]
    assert client.get(f"/invite/{old}").status_code == 404
    assert client.get(f"/invite/{new}").status_code == 200


def test_expired_invite_refused(client, seeded):
    token = invite(client).json()["invite_url"].rsplit("/", 1)[1]
    row = seeded.scalar(select(Invite))
    row.expires_at = utcnow() - timedelta(seconds=1)
    seeded.commit()
    assert client.post(f"/invite/{token}", json={"password": "a-strong-password"}).status_code == 404


def test_existing_judge_gets_tracks_but_no_new_link(client, seeded):
    resp = invite(client, email=seeded.get(User, JUDGE_A).email)
    assert resp.status_code == 201 and resp.json()["invite_url"] is None
    tracks = client.get("/api/judges", headers=ORG).json()
    assert "trk_demo_1" in next(j for j in tracks if j["id"] == JUDGE_A)["tracks"]


def test_cannot_invite_a_participant_as_judge(client, seeded):
    participant = seeded.scalar(select(User).where(User.role == Role.participant))
    resp = invite(client, email=participant.email)
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "not_a_judge"


def test_invite_validation(client, seeded):
    assert invite(client, email="not-an-email").status_code == 422
    assert invite(client, track_ids=["trk_nope"]).status_code == 422


def test_invited_judge_track_edit_is_audited(client, seeded):
    resp = client.post(f"/api/judges/{JUDGE_A}/tracks", headers=ORG, json={"track_ids": ["trk_01", "trk_02"]})
    assert resp.status_code == 200 and resp.json()["tracks"] == ["trk_01", "trk_02"]
    assert seeded.scalar(select(AuditLog).where(AuditLog.action == "judge.tracks")) is not None


def test_html_invite_with_checkboxes(client, seeded):
    import re
    client.headers.update(ORG)
    page = client.get("/organizer/judges", headers={"accept": "text/html"})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = client.post("/organizer/judges/invite", headers={"accept": "text/html"}, data={
        "csrf_token": csrf, "email": "form.judge@test.org", "name": "Form Judge",
        "track_ids": ["trk_demo_1", "trk_demo_2"]})
    assert resp.status_code == 200 and "/invite/" in resp.text
    judge = seeded.scalar(select(User).where(User.email == "form.judge@test.org"))
    assert sorted(t.track_id for t in judge.judge_tracks) == ["trk_demo_1", "trk_demo_2"]


@pytest.mark.parametrize("label,status", [("judge_a", 403), ("participant", 403), (None, 401)])
def test_only_staff_invite(client, seeded, label, status):
    headers = demo(label) if label else {}
    assert client.post("/api/judges/invite", headers=headers,
                       json={"email": "x@y.org", "name": "X", "track_ids": []}).status_code == status


# --- role changes --------------------------------------------------------------------------------------


def test_admin_changes_role_and_it_is_audited_and_immediate(client, seeded):
    admin = as_user(seeded, "usr_admin")
    target = seeded.scalar(select(User).where(User.role == Role.judge, User.id != JUDGE_A))
    target_auth = as_user(seeded, target.id)
    assert client.get("/api/judge/scores", headers=target_auth).status_code == 200
    resp = client.post(f"/api/admin/users/{target.id}/role", headers=admin, json={"role": "participant"})
    assert resp.status_code == 200
    assert client.get("/api/judge/scores", headers=target_auth).status_code == 403   # existing session, new role
    row = seeded.scalar(select(AuditLog).where(AuditLog.action == "user.role_change"))
    assert '"old": "judge"' in row.detail and '"new": "participant"' in row.detail


@pytest.mark.parametrize("label", ["organizer", "judge_a", "participant"])
def test_non_admins_cannot_change_roles(client, seeded, label):
    resp = client.post(f"/api/admin/users/{JUDGE_A}/role", headers=demo(label), json={"role": "admin"})
    assert resp.status_code == 403


def test_participant_cannot_escalate_self(client, seeded):
    me = client.get("/api/me", headers=demo("participant")).json()["id"]
    resp = client.post(f"/api/admin/users/{me}/role", headers=demo("participant"), json={"role": "admin"})
    assert resp.status_code == 403
    assert seeded.get(User, me).role == Role.participant


def test_admin_cannot_change_own_role(client, seeded):
    admin = as_user(seeded, "usr_admin")
    assert client.post("/api/admin/users/usr_admin/role", headers=admin, json={"role": "judge"}).status_code == 409


def test_team_member_cannot_become_judge(client, seeded):
    admin = as_user(seeded, "usr_admin")
    me = client.get("/api/me", headers=demo("participant")).json()["id"]
    resp = client.post(f"/api/admin/users/{me}/role", headers=admin, json={"role": "judge"})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "on_a_team"


def test_bad_role_rejected(client, seeded):
    admin = as_user(seeded, "usr_admin")
    assert client.post(f"/api/admin/users/{JUDGE_A}/role", headers=admin, json={"role": "god"}).status_code == 422
