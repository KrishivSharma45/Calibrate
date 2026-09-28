"""Normalization maths, results visibility, publishing and the audit log viewer."""

import csv
import io
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog, Event, JudgeTrack, Project, ProjectStatus, Role, RubricCriterion, Score, ScoreItem,
    Team, Track, User,
)
from app.seed import DEMO_EVENT_ID, DEMO_SESSION_TOKENS
from app.services.results import compute_results


def demo(label: str) -> dict:
    return {"cookie": f"session={DEMO_SESSION_TOKENS[label]}"}


def build_event(db, scores: dict[str, dict[str, float]]) -> Event:
    """A closed event where scores[judge][project] = value given on every criterion."""
    # Flush between layers: these models have FKs but no ORM relationships to each
    # other, so the unit of work would not know to insert parents first.
    event = Event(id="evt_t", name="T", submissions_close=datetime(2026, 1, 1, tzinfo=UTC))
    db.add(event)
    db.add(Track(id="trk_t", event_id="evt_t", name="Track"))
    for key, w in (("a", 0.5), ("b", 0.5)):
        db.add(RubricCriterion(id=f"crt_t_{key}", event_id="evt_t", key=key, label=key, weight=w))
    db.flush()
    projects = sorted({p for per in scores.values() for p in per})
    for p in projects:
        db.add(Team(id=f"tm_{p}", event_id="evt_t", name=p, invite_token=f"tok_{p}"))
    db.flush()
    for p in projects:
        db.add(Project(id=p, event_id="evt_t", team_id=f"tm_{p}", track_id="trk_t", title=p,
                       status=ProjectStatus.submitted))
    for judge in scores:
        db.add(User(id=judge, email=f"{judge}@t.org", name=judge, role=Role.judge))
    db.flush()
    for judge, per in scores.items():
        db.add(JudgeTrack(judge_id=judge, track_id="trk_t"))
        for p, v in per.items():
            db.add(Score(id=f"sc_{judge}_{p}", judge_id=judge, project_id=p, items=[
                ScoreItem(criterion_id="crt_t_a", value=v), ScoreItem(criterion_id="crt_t_b", value=v)]))
    db.commit()
    return event


def by_id(results):
    return {r["project_id"]: r for r in results["projects"]}


# --- maths ------------------------------------------------------------------------------------


def test_harsh_judges_favourite_beats_generous_judges_least_favourite(db):
    event = build_event(db, {
        "harsh":    {"p_h_best": 3, "p_h_mid": 2, "p_h_low": 1},
        "generous": {"p_g_best": 5, "p_g_mid": 5, "p_g_low": 4},
    })
    r = by_id(compute_results(db, event))
    assert r["p_g_low"]["raw_mean"] > r["p_h_best"]["raw_mean"]          # raw says generous wins
    assert r["p_h_best"]["normalized"] > r["p_g_low"]["normalized"]      # normalized disagrees
    assert r["p_h_best"]["rank"] < r["p_g_low"]["rank"]


def test_z_scores_and_rescaling_match_hand_calculation(db):
    event = build_event(db, {"j1": {"x": 2, "y": 4, "z": 3}, "j2": {"x": 3, "y": 5, "z": 4}})
    res = compute_results(db, event)
    r = by_id(res)
    # each judge: mean 3 (j1) / 4 (j2), pstdev sqrt(2/3); y is +1.2247 sd for both
    assert r["y"]["z_mean"] == pytest.approx(1.2247, abs=1e-4)
    assert r["x"]["z_mean"] == pytest.approx(-1.2247, abs=1e-4)
    assert r["z"]["z_mean"] == pytest.approx(0.0, abs=1e-4)
    assert r["y"]["normalized"] == pytest.approx(res["event_mean"] + 1.2247 * res["event_sd"], abs=1e-3)
    assert [p["project_id"] for p in res["projects"]] == ["y", "z", "x"]


def test_flat_judge_abstains_instead_of_voting_average(db):
    event = build_event(db, {
        "flat": {"a": 4, "b": 4, "c": 4},
        "real": {"a": 5, "b": 3, "c": 1},
    })
    res = compute_results(db, event)
    judges = {j["judge_id"]: j for j in res["judges"]}
    assert judges["flat"]["flag"] == "flat" and judges["flat"]["weight"] == 0
    r = by_id(res)
    # Only the informative judge decides; the flat judge does not pull 'a' toward the middle.
    assert r["a"]["z_mean"] == pytest.approx(1.2247, abs=1e-4)
    assert r["a"]["effective_reviews"] == 1


def test_project_with_only_uninformative_reviews_is_not_ranked(db):
    event = build_event(db, {"flat": {"a": 4, "b": 4}, "real": {"b": 5, "c": 1, "d": 3}})
    r = by_id(compute_results(db, event))
    assert r["a"]["rank"] is None and r["a"]["normalized"] is None
    assert "no_informative_reviews" in r["a"]["flags"]
    assert r["a"]["raw_mean"] == 4  # raw is still shown side by side


def test_few_scores_judge_counts_less(db):
    event = build_event(db, {"few": {"a": 5, "b": 1}, "full": {"a": 1, "b": 5, "c": 3}})
    res = compute_results(db, event)
    judges = {j["judge_id"]: j for j in res["judges"]}
    assert judges["few"]["flag"] == "few_scores" and judges["few"]["weight"] == pytest.approx(2 / 3, abs=1e-4)
    # 'a': few says +1, full says -1.2247; weighted toward full -> negative
    assert by_id(res)["a"]["z_mean"] < 0


def test_empty_event_does_not_crash(client, seeded):
    res = client.get(f"/api/events/{DEMO_EVENT_ID}/results", headers=demo("organizer")).json()
    assert res["projects"] == [] and res["event_mean"] is None


# --- on the real fixtures ---------------------------------------------------------------------------


def test_fixture_results(client, seeded, fixtures_data):
    res = client.get("/api/events/evt_01/results", headers=demo("organizer")).json()
    rows = by_id(res)
    judges = {j["judge_id"]: j for j in res["judges"]}
    assert "prj_07" not in rows and "prj_41" in rows                       # duplicate not double counted
    assert len(rows) == 40
    assert judges["jdg_07"]["flag"] == "flat" and judges["jdg_07"]["weight"] == 0
    assert judges["jdg_23"]["flag"] == "single_score"
    # jdg_01's only fixture score was on prj_07, the superseded duplicate, so it is excluded entirely.
    assert "jdg_01" not in judges
    ranks = sorted(r["rank"] for r in rows.values() if r["rank"] is not None)
    assert ranks == list(range(1, len(ranks) + 1))                          # contiguous, no gaps
    assert all(r["normalized"] is not None or r["flags"] for r in rows.values())


# --- visibility and publishing -------------------------------------------------------------------------


@pytest.mark.parametrize("label", [None, "participant", "judge_a"])
def test_results_hidden_before_publish(client, seeded, label):
    headers = demo(label) if label else {}
    assert client.get("/api/events/evt_01/results", headers=headers).status_code == 403
    assert client.get("/events/evt_01/results", headers=headers).status_code == 403


def test_cannot_publish_while_submissions_open(client, seeded):
    resp = client.post(f"/api/events/{DEMO_EVENT_ID}/results/publish", headers=demo("organizer"), json={})
    assert resp.status_code == 409


@pytest.mark.parametrize("label,status", [("judge_a", 403), ("participant", 403), (None, 401)])
def test_only_staff_publish(client, seeded, label, status):
    headers = demo(label) if label else {}
    assert client.post("/api/events/evt_01/results/publish", headers=headers, json={}).status_code == status
    assert seeded.get(Event, "evt_01").results_published_at is None


def test_publish_then_public_view_has_no_judge_data(client, seeded):
    assert client.post("/api/events/evt_01/results/publish", headers=demo("organizer"), json={}).status_code == 200
    public = client.get("/api/events/evt_01/results").json()
    assert "judges" not in public and len(public["projects"]) == 40
    assert set(public["projects"][0]) == {"rank", "track_rank", "project_id", "title", "track", "reviews",
                                          "normalized", "raw_mean"}
    assert "jdg_" not in client.get("/events/evt_01/results").text
    client.post("/api/events/evt_01/results/unpublish", headers=demo("organizer"), json={})
    assert client.get("/api/events/evt_01/results").status_code == 403
    actions = list(seeded.scalars(select(AuditLog.action).where(AuditLog.entity_id == "evt_01")))
    assert "results.publish" in actions and "results.unpublish" in actions


def test_results_csv(client, seeded):
    body = client.get("/api/export.csv", params={"kind": "results", "event": "evt_01"},
                      headers=demo("organizer")).text
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == 40 and {"rank", "raw_mean", "normalized", "flags"} <= set(rows[0])


# --- audit log viewer -------------------------------------------------------------------------------------


def test_audit_viewer_filters_and_orders(client, seeded):
    org = demo("organizer")
    client.get("/api/export.csv", params={"kind": "projects"}, headers=org)
    client.post("/api/events/evt_01/results/publish", headers=org, json={})
    data = client.get("/api/audit", headers=org).json()
    assert data["entries"][0]["action"] == "results.publish"           # newest first
    only = client.get("/api/audit", params={"action": "export"}, headers=org).json()
    assert only["total"] == 1 and only["entries"][0]["actor"] == "Olive Organizer"
    assert client.get("/organizer/audit", headers={**org, "accept": "text/html"}).status_code == 200


@pytest.mark.parametrize("label,status", [("judge_a", 403), ("participant", 403), (None, 401)])
def test_audit_is_staff_only(client, seeded, label, status):
    headers = demo(label) if label else {}
    assert client.get("/api/audit", headers=headers).status_code == status
    assert client.get("/api/export.csv", params={"kind": "audit"}, headers=headers).status_code == status
