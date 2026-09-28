"""T3: community voting, hidden tallies, shuffled ballots, comments, anti-abuse."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.auth import comment_limit, vote_limit
from app.models import AuditLog, VotingWindow, utcnow
from app.seed import DEMO_EVENT_ID, DEMO_SESSION_TOKENS
from app.services.community import shuffled_for


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def register(client, n: int) -> dict:
    resp = client.post("/api/register", json={"email": f"v{n}@test.org", "name": f"V{n}",
                                              "password": "long-enough-pw"})
    token = resp.cookies["session"]
    client.cookies.clear()
    return {"cookie": f"session={token}"}


@pytest.fixture
def world(client, seeded):
    """Two teams with submitted projects in the open demo event, plus a voter with no team."""
    out = {}
    for n, name in ((1, "Alpha"), (2, "Beta")):
        auth = register(client, n)
        client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=auth, json={"name": name})
        pid = client.post("/projects/new", headers=auth, json={
            "title": f"{name} App", "track_id": "trk_demo_1", "submit": True}).json()["id"]
        out[name] = {"auth": auth, "pid": pid}
    out["voter"] = register(client, 9)
    return out


def actions(db, action):
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def set_window(db, opens_delta, closes_delta):
    w = db.get(VotingWindow, DEMO_EVENT_ID)
    w.opens_at, w.closes_at = utcnow() + opens_delta, utcnow() + closes_delta
    db.commit()


# --- voting -------------------------------------------------------------------------------------------


def test_vote_once_per_project(client, world, seeded):
    pid = world["Alpha"]["pid"]
    assert client.post(f"/projects/{pid}/vote", headers=world["voter"], json={}).status_code == 200
    again = client.post(f"/projects/{pid}/vote", headers=world["voter"], json={})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_voted"
    assert len(actions(seeded, "vote.cast")) == 1 and len(actions(seeded, "vote.duplicate")) == 1


def test_unvote_then_vote_again(client, world):
    pid = world["Alpha"]["pid"]
    client.post(f"/projects/{pid}/vote", headers=world["voter"], json={})
    assert client.post(f"/projects/{pid}/unvote", headers=world["voter"], json={}).status_code == 200
    assert client.post(f"/projects/{pid}/vote", headers=world["voter"], json={}).status_code == 200


def test_cannot_vote_for_own_team(client, world):
    resp = client.post(f"/projects/{world['Alpha']['pid']}/vote", headers=world["Alpha"]["auth"], json={})
    assert resp.status_code == 403 and resp.json()["detail"]["error"] == "own_project"


@pytest.mark.parametrize("label,status", [("judge_a", 403), ("organizer", 403), (None, 401)])
def test_only_participants_vote(client, world, label, status):
    headers = demo(label) if label else {}
    assert client.post(f"/projects/{world['Alpha']['pid']}/vote", headers=headers, json={}).status_code == status


@pytest.mark.parametrize("opens,closes,state", [
    (timedelta(days=1), timedelta(days=2), "upcoming"), (timedelta(days=-2), timedelta(days=-1), "closed")])
def test_no_votes_outside_window(client, world, seeded, opens, closes, state):
    set_window(seeded, opens, closes)
    resp = client.post(f"/projects/{world['Alpha']['pid']}/vote", headers=world["voter"], json={})
    assert resp.status_code == 409 and state in resp.json()["detail"]["message"]


def test_vote_rate_limit_is_enforced_and_audited(client, world, seeded, monkeypatch):
    monkeypatch.setattr(vote_limit, "max_failures", 2)
    v = world["voter"]
    client.post(f"/projects/{world['Alpha']['pid']}/vote", headers=v, json={})
    client.post(f"/projects/{world['Alpha']['pid']}/unvote", headers=v, json={})
    assert client.post(f"/projects/{world['Beta']['pid']}/vote", headers=v, json={}).status_code == 429
    assert actions(seeded, "vote.rate_limited")


def test_vote_form_needs_csrf(client, world):
    assert client.post(f"/projects/{world['Alpha']['pid']}/vote", headers=world["voter"],
                       data={}).status_code == 403


def test_duplicate_project_not_on_ballot_and_not_voteable(client, seeded):
    v = register(client, 5)
    assert client.post("/projects/prj_07/vote", headers=v, json={}).status_code == 404


# --- tallies hidden until the window closes -----------------------------------------------------------


@pytest.mark.parametrize("label", [None, "participant", "judge_a"])
def test_tallies_hidden_while_voting(client, world, label):
    headers = demo(label) if label else {}
    assert client.get(f"/api/events/{DEMO_EVENT_ID}/votes", headers=headers).status_code == 403


def test_staff_see_live_tallies_with_new_account_signal(client, world):
    client.post(f"/projects/{world['Alpha']['pid']}/vote", headers=world["voter"], json={})
    rows = {r["project_id"]: r for r in
            client.get(f"/api/events/{DEMO_EVENT_ID}/votes", headers=demo("organizer")).json()["projects"]}
    alpha = rows[world["Alpha"]["pid"]]
    assert alpha["votes"] == 1 and alpha["votes_from_new_accounts"] == 1  # voter registered after voting opened


def test_public_tallies_after_close_without_staff_signal(client, world, seeded):
    client.post(f"/projects/{world['Beta']['pid']}/vote", headers=world["voter"], json={})
    set_window(seeded, timedelta(days=-3), timedelta(seconds=-1))
    resp = client.get(f"/api/events/{DEMO_EVENT_ID}/votes")
    assert resp.status_code == 200
    top = resp.json()["projects"][0]
    assert top["project_id"] == world["Beta"]["pid"] and top["votes"] == 1
    assert "votes_from_new_accounts" not in top


# --- randomized ballot --------------------------------------------------------------------------------


def test_shuffle_is_stable_per_user_and_differs_between_users():
    projects = [SimpleNamespace(id=f"p{i:02d}") for i in range(20)]
    a1 = [p.id for p in shuffled_for("usr_a", "evt", projects)]
    a2 = [p.id for p in shuffled_for("usr_a", "evt", projects)]
    b = [p.id for p in shuffled_for("usr_b", "evt", projects)]
    assert a1 == a2                                   # no rerolling for a better position
    assert a1 != b                                    # different people, different order
    assert sorted(a1) == sorted(b) == [p.id for p in projects]


def test_ballot_api_lists_only_submitted_non_duplicate(client, world):
    data = client.get(f"/api/events/{DEMO_EVENT_ID}/ballot", headers=world["voter"]).json()
    assert data["state"] == "open"
    assert {p["project_id"] for p in data["projects"]} == {world["Alpha"]["pid"], world["Beta"]["pid"]}


def test_ballot_page_renders(client, world):
    resp = client.get(f"/events/{DEMO_EVENT_ID}/ballot", headers={**world["voter"], "accept": "text/html"})
    assert resp.status_code == 200 and "Alpha App" in resp.text and "Votes</th>" not in resp.text


def test_ballot_needs_login(client, world):
    assert client.get(f"/api/events/{DEMO_EVENT_ID}/ballot").status_code == 401


# --- voting window ------------------------------------------------------------------------------------


def test_organizer_sets_window_audited(client, seeded):
    resp = client.post("/api/events/evt_01/voting", headers=demo("organizer"),
                       json={"opens_at": "2026-03-02T00:00:00Z", "closes_at": "2026-03-09T00:00:00Z"})
    assert resp.status_code == 200 and actions(seeded, "voting.window")
    bad = client.post("/api/events/evt_01/voting", headers=demo("organizer"),
                      json={"opens_at": "2026-03-09T00:00:00Z", "closes_at": "2026-03-02T00:00:00Z"})
    assert bad.status_code == 422
    assert client.post("/api/events/evt_01/voting", headers=demo("participant"),
                       json={"opens_at": "2026-03-02T00:00:00Z", "closes_at": "2026-03-09T00:00:00Z"}).status_code == 403


# --- comments ----------------------------------------------------------------------------------------------


def test_comment_lifecycle_and_moderation(client, world, seeded):
    pid = world["Alpha"]["pid"]
    assert client.post(f"/projects/{pid}/comments", json={"body": "hi"}).status_code == 401
    made = client.post(f"/projects/{pid}/comments", headers=world["voter"], json={"body": "Nice demo!"})
    assert made.status_code == 201
    assert [c["body"] for c in client.get(f"/api/projects/{pid}/comments").json()] == ["Nice demo!"]
    cid = made.json()["id"]
    assert client.post(f"/comments/{cid}/hide", headers=world["voter"], json={}).status_code == 403
    assert client.post(f"/comments/{cid}/hide", headers=demo("organizer"), json={}).status_code == 200
    assert client.get(f"/api/projects/{pid}/comments").json() == []                       # public: gone
    staff_view = client.get(f"/api/projects/{pid}/comments", headers=demo("organizer")).json()
    assert staff_view[0]["hidden"] is True                                                  # kept, not deleted
    assert actions(seeded, "comment.create") and actions(seeded, "comment.hide")


def test_comment_validation_rate_limit_and_escaping(client, world, monkeypatch):
    pid = world["Alpha"]["pid"]
    v = world["voter"]
    assert client.post(f"/projects/{pid}/comments", headers=v, json={"body": "   "}).status_code == 422
    assert client.post(f"/projects/{pid}/comments", headers=v, json={"body": "x" * 2001}).status_code == 422
    client.post(f"/projects/{pid}/comments", headers=v, json={"body": "<script>alert(1)</script>"})
    page = client.get(f"/projects/{pid}", headers={"accept": "text/html"}).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    monkeypatch.setattr(comment_limit, "max_failures", 1)
    comment_limit.failures.clear()
    client.post(f"/projects/{pid}/comments", headers=v, json={"body": "one"})
    assert client.post(f"/projects/{pid}/comments", headers=v, json={"body": "two"}).status_code == 429


def test_no_comments_on_drafts(client, seeded):
    auth = register(client, 7)
    client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=auth, json={"name": "Drafty"})
    pid = client.post("/projects/new", headers=auth, json={"title": "Draft", "track_id": "trk_demo_1"}).json()["id"]
    assert client.post(f"/projects/{pid}/comments", headers=auth, json={"body": "hm"}).status_code == 404
