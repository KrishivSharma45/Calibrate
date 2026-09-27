"""Organizer event management and team formation by invite link, over HTTP."""

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, Event, Team, TeamMember, utcnow
from app.seed import DEMO_EVENT_ID, DEMO_SESSION_TOKENS

HTML = {"accept": "text/html"}


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def new_participant(client, n: int) -> dict:
    """Register a fresh participant (no team yet) and return auth headers."""
    resp = client.post("/api/register", json={"email": f"p{n}@test.org", "name": f"P{n}",
                                              "password": "long-enough-pw"})
    assert resp.status_code == 201
    token = resp.cookies["session"]
    client.cookies.clear()
    return {"cookie": f"session={token}"}


def event_body(**over):
    close = (utcnow() + timedelta(days=5)).isoformat()
    return {"name": "Spring Hack", "description": "d", "submissions_close": close,
            "max_team_size": 2, "tracks": ["Web", "Hardware"],
            "prizes": [{"name": "Best in show", "description": "top"}],
            "questions": [{"prompt": "Why?", "required": True}], **over}


def audit_actions(db, entity_id: str) -> list[str]:
    return list(db.scalars(select(AuditLog.action).where(AuditLog.entity_id == entity_id)
                           .order_by(AuditLog.id)))


# --- events ------------------------------------------------------------------------------


def test_organizer_creates_event_with_tracks_prizes_questions_rubric(client, seeded):
    resp = client.post("/api/events", headers=demo("organizer"), json=event_body())
    assert resp.status_code == 201
    ev = resp.json()
    assert [t["name"] for t in ev["tracks"]] == ["Hardware", "Web"]  # tracks are ordered by name
    seeded.expire_all()
    assert client.get(f"/api/events/{ev['id']}").json()["tracks"] == ev["tracks"]  # same order after reload
    assert ev["prizes"][0]["name"] == "Best in show"
    assert ev["questions"] == [{"id": ev["questions"][0]["id"], "prompt": "Why?", "required": True}]
    assert sum(c["weight"] for c in ev["rubric"]) == pytest.approx(1.0)
    assert audit_actions(seeded, ev["id"]) == ["event.create"]


@pytest.mark.parametrize("label", ["judge_a", "participant"])
def test_non_staff_cannot_create_or_edit_events(client, seeded, label):
    assert client.post("/api/events", headers=demo(label), json=event_body()).status_code == 403
    assert client.post(f"/api/events/{DEMO_EVENT_ID}", headers=demo(label),
                       json=event_body()).status_code == 403
    assert client.get("/organizer", headers=demo(label)).status_code == 403


def test_anonymous_cannot_create_events(client, seeded):
    assert client.post("/api/events", json=event_body()).status_code == 401


@pytest.mark.parametrize("over", [
    {"name": ""},
    {"tracks": []},
    {"tracks": ["Web", "web"]},
    {"max_team_size": 0},
    {"submissions_close": "not a date"},
    {"submissions_open": (utcnow() + timedelta(days=9)).isoformat()},  # opens after it closes
])
def test_event_validation(client, seeded, over):
    assert client.post("/api/events", headers=demo("organizer"), json=event_body(**over)).status_code == 422


def test_deadline_change_is_audited_with_old_and_new(client, seeded):
    new_close = "2026-12-31T18:00:00Z"
    resp = client.post(f"/api/events/{DEMO_EVENT_ID}", headers=demo("organizer"), json={
        "name": "Raptors Live Demo 2026", "submissions_close": new_close, "max_team_size": 4,
        "tracks": ["Hardware hacks"]})
    assert resp.status_code == 200
    assert "Hardware hacks" in [t["name"] for t in resp.json()["tracks"]]
    assert len(resp.json()["tracks"]) == 4  # existing three kept
    row = seeded.scalar(select(AuditLog).where(AuditLog.action == "event.deadline_change"))
    detail = json.loads(row.detail)
    assert detail["new_close"].startswith("2026-12-31 18:00") and row.actor_id == "usr_organizer"


def test_html_event_form_treats_times_as_utc(client, seeded):
    client.headers.update(demo("organizer"))
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      client.get("/organizer/events/new", headers=HTML).text).group(1)
    resp = client.post("/organizer/events/new", headers=HTML, follow_redirects=False, data={
        "csrf_token": token, "name": "Form Hack", "submissions_close": "2030-01-02T03:04",
        "max_team_size": "3", "tracks": "A\nB\n", "prizes": "Gold: best\nSilver",
        "questions": "*Required one\nOptional one"})
    assert resp.status_code == 303
    event = seeded.get(Event, resp.headers["location"].rsplit("/", 1)[1])
    assert event.submissions_close.isoformat() == "2030-01-02T03:04:00+00:00"
    assert [q.required for q in event.questions] == [True, False]
    assert sorted((p.name, p.description) for p in event.prizes) == [("Gold", "best"), ("Silver", "")]


# --- teams -----------------------------------------------------------------------------------


def test_create_team_and_join_by_invite(client, seeded):
    a, b = new_participant(client, 1), new_participant(client, 2)
    team = client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a, json={"name": "Owls"})
    assert team.status_code == 201
    invite = team.json()["invite_url"]
    token = invite.rsplit("/", 1)[1]

    assert client.get(f"/teams/join/{token}", headers={**b, **HTML}).status_code == 200
    members = seeded.scalars(select(TeamMember).where(TeamMember.team_id == team.json()["id"])).all()
    assert len(members) == 1  # opening the link (GET) did not join

    joined = client.post(f"/teams/join/{token}", headers=b, json={})
    assert joined.status_code == 200 and len(joined.json()["members"]) == 2
    assert audit_actions(seeded, team.json()["id"]) == ["team.create", "team.join"]


def test_team_size_limit(client, seeded):
    seeded.get(Event, DEMO_EVENT_ID).max_team_size = 2
    seeded.commit()
    a, b, c = (new_participant(client, i) for i in (1, 2, 3))
    token = client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a,
                        json={"name": "Duo"}).json()["invite_url"].rsplit("/", 1)[1]
    assert client.post(f"/teams/join/{token}", headers=b, json={}).status_code == 200
    full = client.post(f"/teams/join/{token}", headers=c, json={})
    assert full.status_code == 409 and full.json()["detail"]["error"] == "team_full"


def test_one_team_per_event(client, seeded):
    a, b = new_participant(client, 1), new_participant(client, 2)
    client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a, json={"name": "One"})
    token = client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=b,
                        json={"name": "Two"}).json()["invite_url"].rsplit("/", 1)[1]
    assert client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a, json={"name": "Again"}).status_code == 409
    resp = client.post(f"/teams/join/{token}", headers=a, json={})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "already_on_team"


def test_judges_and_organizers_cannot_join_teams(client, seeded):
    a = new_participant(client, 1)
    token = client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a,
                        json={"name": "T"}).json()["invite_url"].rsplit("/", 1)[1]
    for label in ("judge_a", "organizer"):
        assert client.post(f"/teams/join/{token}", headers=demo(label), json={}).status_code == 403
        assert client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=demo(label),
                           json={"name": "x"}).status_code == 403


def test_no_team_changes_after_deadline(client, seeded):
    token = seeded.get(Team, "tm_02").invite_token  # fixture event is closed
    b = new_participant(client, 1)
    assert client.post(f"/teams/join/{token}", headers=b, json={}).status_code == 409
    assert client.post("/events/evt_01/teams", headers=b, json={"name": "Late"}).status_code == 409


def test_rotated_invite_link_stops_working(client, seeded):
    a, b = new_participant(client, 1), new_participant(client, 2)
    team = client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a, json={"name": "T"}).json()
    old = team["invite_url"].rsplit("/", 1)[1]
    new = client.post(f"/teams/{team['id']}/invite/rotate", headers=a,
                      json={}).json()["invite_url"].rsplit("/", 1)[1]
    assert old != new
    assert client.post(f"/teams/join/{old}", headers=b, json={}).status_code == 404
    assert client.post(f"/teams/join/{new}", headers=b, json={}).status_code == 200


def test_outsiders_cannot_see_team_or_its_invite_link(client, seeded):
    a, b = new_participant(client, 1), new_participant(client, 2)
    team_id = client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a, json={"name": "T"}).json()["id"]
    assert client.get(f"/api/teams/{team_id}", headers=b).status_code == 404
    assert client.post(f"/teams/{team_id}/invite/rotate", headers=b, json={}).status_code == 404
    staff_view = client.get(f"/api/teams/{team_id}", headers=demo("organizer")).json()
    assert "invite_url" not in staff_view  # staff can see members, not the joining secret


def test_invite_link_needs_login(client, seeded):
    token = seeded.get(Team, "tm_02").invite_token
    resp = client.get(f"/teams/join/{token}", headers=HTML, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login?next=")


def test_full_flow_team_then_submission(client, seeded):
    a = new_participant(client, 1)
    client.post(f"/events/{DEMO_EVENT_ID}/teams", headers=a, json={"name": "Flow"})
    resp = client.post("/projects/new", headers=a, json={"title": "Flow App", "track_id": "trk_demo_1",
                                                          "submit": True})
    assert resp.status_code == 201 and resp.json()["event_id"] == DEMO_EVENT_ID
