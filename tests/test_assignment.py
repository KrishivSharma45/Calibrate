"""Assignment algorithm, rubric editing and the progress dashboard."""

import csv
import io
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Assignment, AuditLog, JudgeTrack, Project
from app.seed import DEMO_EVENT_ID, DEMO_SESSION_TOKENS, JUDGE_A
from app.services.assignment import plan_assignments


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def proj(pid, track):
    return SimpleNamespace(id=pid, track_id=track)


# --- the algorithm on its own ------------------------------------------------------------------


def test_plan_fills_to_target_within_track_and_balances_load():
    projects = [proj(f"p{i}", "t1") for i in range(6)]
    judges = {"t1": ["a", "b", "c", "d"]}
    result = plan_assignments(projects, judges, set(), target=2)
    per_project = Counter(p for _, p in result.added)
    load = Counter(j for j, _ in result.added)
    assert set(per_project.values()) == {2}
    assert max(load.values()) - min(load.values()) <= 1        # 12 slots over 4 judges -> 3 each
    assert result.short == {}


def test_plan_never_crosses_tracks_and_reports_shortfall():
    projects = [proj("p1", "small"), proj("p2", "big")]
    judges = {"small": ["a"], "big": ["b", "c", "d"]}
    result = plan_assignments(projects, judges, set(), target=3)
    assert {j for j, p in result.added if p == "p1"} == {"a"}
    assert result.short == {"p1": 2}


def test_plan_most_constrained_first():
    """Judge 'a' is the only judge for p_rare; p_common must not use 'a' up first."""
    projects = [proj("p_common", "t_common"), proj("p_rare", "t_rare")]
    judges = {"t_common": ["a", "b"], "t_rare": ["a"]}
    result = plan_assignments(projects, judges, set(), target=1)
    assert ("a", "p_rare") in result.added and ("b", "p_common") in result.added


def test_plan_keeps_existing_and_is_idempotent():
    projects = [proj("p1", "t"), proj("p2", "t")]
    judges = {"t": ["a", "b", "c"]}
    first = plan_assignments(projects, judges, {("a", "p1")}, target=2)
    assert ("a", "p1") not in first.added and len(first.added) == 3
    again = plan_assignments(projects, judges, {("a", "p1"), *first.added}, target=2)
    assert again.added == []


# --- auto-assign over HTTP on the fixture data ---------------------------------------------------------


def test_auto_assign_fixture_event(client, seeded):
    resp = client.post("/api/events/evt_01/assignments/auto", headers=demo("organizer"), json={"target": 3})
    assert resp.status_code == 200
    rows = seeded.execute(select(Assignment.judge_id, Project.id, Project.track_id)
                          .join(Project, Project.id == Assignment.project_id)
                          .where(Project.event_id == "evt_01")).all()
    tracks = {(j, t) for j, t in seeded.execute(select(JudgeTrack.judge_id, JudgeTrack.track_id))}
    assert all((judge, track) in tracks for judge, _, track in rows)       # never outside a judge's track
    per_project = Counter(pid for _, pid, _ in rows if pid != "prj_07")
    assert min(per_project.values()) >= 3
    assert all(a["project_id"] != "prj_07" for a in resp.json()["added"])  # flagged duplicate skipped
    again = client.post("/api/events/evt_01/assignments/auto", headers=demo("organizer"), json={"target": 3})
    assert again.json()["added"] == []
    assert seeded.scalar(select(AuditLog).where(AuditLog.action == "assignment.auto")) is not None


def test_manual_add_and_remove_rules(client, seeded):
    org = demo("organizer")
    # jdg_09 judges trk_01 only; prj_41 is trk_03.
    wrong = client.post("/api/events/evt_01/assignments", headers=org,
                        json={"judge_id": JUDGE_A, "project_id": "prj_41"})
    assert wrong.status_code == 422
    scored = seeded.scalar(select(Assignment).where(Assignment.judge_id == JUDGE_A))
    keep = client.post("/api/events/evt_01/assignments/remove", headers=org,
                       json={"judge_id": JUDGE_A, "project_id": scored.project_id})
    assert keep.status_code == 409


def test_manual_remove_unscored(client, seeded):
    org = demo("organizer")
    fresh = client.post("/api/events/evt_01/assignments/auto", headers=org,
                        json={"target": 3}).json()["added"][0]   # new, so certainly unscored
    resp = client.post("/api/events/evt_01/assignments/remove", headers=org, json=fresh)
    assert resp.status_code == 200
    assert seeded.get(Assignment, (fresh["judge_id"], fresh["project_id"])) is None


# --- progress -------------------------------------------------------------------------------------------


def test_progress_counts_match_fixtures(client, seeded, fixtures_data):
    p = client.get("/api/events/evt_01/progress", headers=demo("organizer")).json()
    reviews = Counter(s["project"] for s in fixtures_data["scores"] if s["project"] != "prj_07")
    expected_below = sum(1 for pr in fixtures_data["projects"]
                         if pr["id"] != "prj_07" and reviews[pr["id"]] < 3)
    assert p["totals"]["projects_below_target"] == expected_below
    assert p["totals"]["judges_not_started"] == 0   # seeded assignments are exactly the scored ones


def test_progress_flags_not_started_and_incomplete(client, seeded):
    org = demo("organizer")
    client.post("/api/events/evt_01/assignments/auto", headers=org, json={"target": 3})
    p = client.get("/api/events/evt_01/progress", headers=org).json()
    statuses = {j["status"] for j in p["judges"]}
    assert "incomplete" in statuses
    assert p["totals"]["done"] < p["totals"]["assignments"]


def test_progress_on_empty_event_does_not_crash(client, seeded):
    p = client.get(f"/api/events/{DEMO_EVENT_ID}/progress", headers=demo("organizer")).json()
    assert p["totals"]["assignments"] == 0 and p["totals"]["percent"] == 0


def test_progress_csv(client, seeded):
    body = client.get("/api/export.csv", params={"kind": "progress", "event": "evt_01"},
                      headers=demo("organizer")).text
    rows = list(csv.DictReader(io.StringIO(body)))
    assert {r["judge_id"] for r in rows} >= {JUDGE_A} and "status" in rows[0]


# --- rubric ----------------------------------------------------------------------------------------------


def rubric(**weights):
    return {"criteria": [{"key": k, "label": k.title(), "weight": w} for k, w in weights.items()]}


def test_rubric_weights_must_sum_to_one(client, seeded):
    resp = client.post(f"/api/events/{DEMO_EVENT_ID}/rubric", headers=demo("organizer"),
                       json=rubric(functionality=0.5, quality=0.3, innovation=0.3))
    assert resp.status_code == 422


def test_rubric_can_be_reshaped_before_judging(client, seeded):
    resp = client.post(f"/api/events/{DEMO_EVENT_ID}/rubric", headers=demo("organizer"),
                       json=rubric(functionality=0.5, design=0.25, impact=0.25))
    assert resp.status_code == 200
    assert [c["key"] for c in resp.json()["criteria"]] == ["functionality", "design", "impact"]


def test_rubric_locked_shape_after_scores_but_weights_change_is_audited(client, seeded):
    org = demo("organizer")
    add = client.post("/api/events/evt_01/rubric", headers=org,
                      json=rubric(functionality=0.4, quality=0.3, innovation=0.2, vibes=0.1))
    assert add.status_code == 422
    ok = client.post("/api/events/evt_01/rubric", headers=org,
                     json=rubric(functionality=0.5, quality=0.25, innovation=0.25))
    assert ok.status_code == 200
    row = seeded.scalar(select(AuditLog).where(AuditLog.action == "rubric.update"))
    detail = json.loads(row.detail)
    assert detail["before"]["functionality"] == pytest.approx(0.4)
    assert detail["after"]["functionality"] == pytest.approx(0.5)
    # weighted totals follow the new weights
    s = client.get("/api/judge/scores", params={"judge": JUDGE_A}, headers=org).json()["scores"][0]
    c = s["criteria"]
    assert s["weighted_total"] == pytest.approx(c["functionality"] * 0.5 + c["quality"] * 0.25
                                                + c["innovation"] * 0.25)


def test_rubric_html_form_uses_percent(client, seeded):
    import re
    client.headers.update(demo("organizer"))
    page = client.get(f"/organizer/events/{DEMO_EVENT_ID}/rubric", headers={"accept": "text/html"})
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = client.post(f"/organizer/events/{DEMO_EVENT_ID}/rubric", follow_redirects=False,
                       headers={"accept": "text/html"}, data={
        "csrf_token": token, "key_0": "functionality", "label_0": "Functionality", "weight_0": "50",
        "key_1": "quality", "label_1": "Quality", "weight_1": "50"})
    assert resp.status_code == 303
    assert [c["weight"] for c in client.get(f"/api/events/{DEMO_EVENT_ID}/rubric").json()] == [0.5, 0.5]


# --- access -----------------------------------------------------------------------------------------------


ORGANIZER_ROUTES = [
    ("GET", "/organizer/events/evt_01"), ("GET", "/organizer/events/evt_01/rubric"),
    ("GET", "/organizer/events/evt_01/assignments"), ("GET", "/api/events/evt_01/progress"),
    ("GET", "/api/events/evt_01/rubric"),
    ("POST", "/api/events/evt_01/rubric"), ("POST", "/api/events/evt_01/assignments/auto"),
    ("POST", "/api/events/evt_01/assignments"), ("POST", "/api/events/evt_01/assignments/remove"),
]


@pytest.mark.parametrize("method,path", ORGANIZER_ROUTES)
@pytest.mark.parametrize("label,status", [("judge_a", 403), ("participant", 403), (None, 401)])
def test_organizer_tools_are_staff_only(client, seeded, method, path, label, status):
    headers = demo(label) if label else {}
    resp = client.request(method, path, headers=headers, json={} if method == "POST" else None)
    assert resp.status_code == status
