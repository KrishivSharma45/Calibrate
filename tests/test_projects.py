"""Gallery, submission lifecycle and deadline enforcement, over HTTP."""

import re
from datetime import timedelta

import pytest

from app.models import CustomQuestion, Event, Project, Team, TeamMember, User, utcnow
from app.seed import DEMO_EVENT_ID, DEMO_SESSION_TOKENS
from app.security import create_session

HTML = {"accept": "text/html"}


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def login_as(db, user_id: str) -> dict:
    token = create_session(db, db.get(User, user_id))
    db.commit()
    return {"cookie": f"session={token}"}


@pytest.fixture
def open_team(seeded):
    """A fixture participant on a fresh team in the open demo event, plus a teammate."""
    db = seeded
    member_ids = [m.user_id for m in db.get(Team, "tm_02").members]
    team = Team(id="tm_demo", event_id=DEMO_EVENT_ID, name="Demo Team", invite_token="t-demo")
    db.add(team)
    db.flush()
    for uid in member_ids[:2]:
        db.add(TeamMember(team_id="tm_demo", user_id=uid, event_id=DEMO_EVENT_ID))
    db.commit()
    return {"auth": login_as(db, member_ids[0]), "user_ids": member_ids}


def new_project(client, auth, **fields):
    body = {"event_id": DEMO_EVENT_ID, "title": "Night Owl", "tagline": "sleep less",
            "track_id": "trk_demo_1", **fields}
    return client.post("/projects/new", headers=auth, json=body)


# --- the three T1 checks from run.py -------------------------------------------------------


def test_gallery_is_public_and_shows_fixture_titles(client, seeded, fixtures_data):
    resp = client.get("/projects")
    assert resp.status_code == 200
    for p in fixtures_data["projects"][:3]:
        assert p["title"] in resp.text


def test_closed_event_refuses_submission_like_run_py(client, seeded):
    resp = client.post("/projects/new", headers=demo("participant"),
                       json={"title": "dogfood-late-submission-probe", "summary": "probe"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "submissions_closed"


def test_closed_event_refuses_edit_of_existing_project(client, seeded):
    resp = client.post("/projects/prj_01/edit", headers=demo("participant"), json={"title": "changed"})
    assert resp.status_code == 409
    assert seeded.get(Project, "prj_01").title != "changed"


# --- gallery -------------------------------------------------------------------------------


def test_gallery_hides_flagged_duplicate(client, seeded):
    data = client.get("/api/projects", params={"q": "dry harbour"}).json()
    assert [p["id"] for p in data["projects"]] == ["prj_41"]


def test_gallery_search_and_filters(client, seeded, fixtures_data):
    by_track = client.get("/api/projects", params={"track": "trk_06"}).json()
    assert by_track["total"] == 3 and all(p["track_id"] == "trk_06" for p in by_track["projects"])
    assert client.get("/api/projects", params={"q": "glass signal"}).json()["total"] == 1
    assert client.get("/api/projects", params={"q": "%"}).json()["total"] == 0  # LIKE wildcard escaped
    assert client.get("/api/projects", params={"event": "evt_01"}).json()["total"] == 40


def test_gallery_tag_filter(client, open_team):
    pid = new_project(client, open_team["auth"], tags=["Rust", "webgpu"], submit=True).json()["id"]
    assert [p["id"] for p in client.get("/api/projects", params={"tag": "rust"}).json()["projects"]] == [pid]


def test_gallery_escapes_html(client, open_team):
    new_project(client, open_team["auth"], title="<script>alert(1)</script>", submit=True)
    body = client.get("/projects").text
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


# --- lifecycle on the open event ----------------------------------------------------------------


def test_draft_is_private_until_submitted(client, open_team):
    resp = new_project(client, open_team["auth"])
    assert resp.status_code == 201 and resp.json()["status"] == "draft"
    pid = resp.json()["id"]
    assert client.get(f"/api/projects/{pid}").status_code == 404           # anonymous
    assert client.get(f"/api/projects/{pid}", headers=demo("judge_a")).status_code == 404
    assert client.get(f"/api/projects/{pid}", headers=open_team["auth"]).status_code == 200
    assert pid not in client.get("/projects").text

    resp = client.post(f"/projects/{pid}/edit", headers=open_team["auth"],
                       json={"title": "Night Owl", "track_id": "trk_demo_1", "submit": True})
    assert resp.status_code == 200 and resp.json()["status"] == "submitted"
    assert client.get(f"/api/projects/{pid}").status_code == 200


def test_teammate_can_edit_until_deadline_then_not(client, open_team, seeded):
    pid = new_project(client, open_team["auth"], submit=True).json()["id"]
    teammate = login_as(seeded, open_team["user_ids"][1])
    ok = client.post(f"/projects/{pid}/edit", headers=teammate,
                     json={"title": "Night Owl v2", "track_id": "trk_demo_1"})
    assert ok.status_code == 200 and ok.json()["status"] == "submitted"  # stays submitted

    seeded.get(Event, DEMO_EVENT_ID).submissions_close = utcnow() - timedelta(seconds=1)
    seeded.commit()
    late = client.post(f"/projects/{pid}/edit", headers=teammate, json={"title": "too late"})
    assert late.status_code == 409
    assert client.get(f"/api/projects/{pid}").json()["title"] == "Night Owl v2"


def test_not_yet_open_event_refuses(client, open_team, seeded):
    seeded.get(Event, DEMO_EVENT_ID).submissions_open = utcnow() + timedelta(days=1)
    seeded.commit()
    assert new_project(client, open_team["auth"]).status_code == 409


def test_non_member_cannot_edit(client, open_team):
    pid = new_project(client, open_team["auth"], submit=True).json()["id"]
    resp = client.post(f"/projects/{pid}/edit", headers=demo("participant"), json={"title": "hijack"})
    assert resp.status_code == 403


def test_team_gets_one_project(client, open_team):
    assert new_project(client, open_team["auth"]).status_code == 201
    second = new_project(client, open_team["auth"], title="Another")
    assert second.status_code == 409 and second.json()["detail"]["error"] == "team_already_has_project"


def test_user_without_team_cannot_submit(client, seeded):
    resp = client.post("/projects/new", headers=demo("judge_a"),
                       json={"event_id": DEMO_EVENT_ID, "title": "x"})
    assert resp.status_code == 403


@pytest.mark.parametrize("fields", [
    {"repo_url": "javascript:alert(1)"},
    {"live_url": "ftp://example.org"},
    {"track_id": "trk_01"},                    # a track from another event
    {"title": ""},
    {"tags": ["x" * 40]},
])
def test_bad_input_is_rejected(client, open_team, fields):
    assert new_project(client, open_team["auth"], **fields).status_code == 422


def test_required_question_only_blocks_final_submit(client, open_team, seeded):
    seeded.get(CustomQuestion, "q_demo_1").required = True
    seeded.commit()
    assert new_project(client, open_team["auth"], submit=True).status_code == 422
    draft = new_project(client, open_team["auth"])
    assert draft.status_code == 201
    done = client.post(f"/projects/{draft.json()['id']}/edit", headers=open_team["auth"], json={
        "title": "Night Owl", "track_id": "trk_demo_1", "submit": True,
        "answers": {"q_demo_1": "A mobile app"}})
    assert done.status_code == 200 and done.json()["status"] == "submitted"


def test_anonymous_submit_is_401(client, seeded):
    assert client.post("/projects/new", json={"title": "x"}).status_code == 401


# --- HTML form path ---------------------------------------------------------------------------------


def test_html_form_submission(client, open_team):
    client.headers.update(open_team["auth"])
    page = client.get("/projects/new", params={"event": DEMO_EVENT_ID}, headers=HTML)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = client.post("/projects/new", headers=HTML, follow_redirects=False, data={
        "csrf_token": token, "event_id": DEMO_EVENT_ID, "title": "Form Project",
        "track_id": "trk_demo_2", "tags": "python, fastapi", "action": "submit"})
    assert resp.status_code == 303
    edit_page = client.get(resp.headers["location"], headers=HTML)
    assert "python, fastapi" in edit_page.text and "submitted" in edit_page.text


def test_html_closed_event_shows_page_not_form(client, seeded):
    resp = client.get("/projects/prj_01/edit", headers={**HTML, **demo("participant")})
    assert resp.status_code == 200 and "closed" in resp.text and "<textarea" not in resp.text
