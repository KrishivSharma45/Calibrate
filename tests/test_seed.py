import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.config import FIXTURES_PATH
from app.models import Event, Project, Role, Score, ScoreItem, Session, TeamMember, User, utcnow
from app.security import hash_token, verify_password
from app.seed import DEMO_EVENT_ID, DEMO_PASSWORD, DEMO_SESSION_TOKENS, run_seed

FIXTURES = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def session_user(db, label):
    s = db.get(Session, hash_token(DEMO_SESSION_TOKENS[label]))
    return s and db.get(User, s.user_id)


def test_counts_match_fixtures(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    assert count(db, Project, Project.event_id == "evt_01") == len(FIXTURES["projects"])
    assert count(db, Score) == len(FIXTURES["scores"])
    assert count(db, ScoreItem) == 3 * len(FIXTURES["scores"])
    assert count(db, User, User.role == Role.judge) == len(FIXTURES["judges"])
    members = sum(len(t["members"]) for t in FIXTURES["teams"])
    assert count(db, TeamMember) == members


def test_seed_twice_changes_nothing(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    assert run_seed(db, FIXTURES_PATH, demo_sessions=True) == {}
    assert count(db, Score) == len(FIXTURES["scores"])


def test_fixture_event_is_closed_and_demo_event_is_open(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    assert db.get(Event, "evt_01").submissions_close == datetime(2026, 3, 1, 18, tzinfo=UTC)
    assert db.get(Event, DEMO_EVENT_ID).submissions_close > utcnow()


def test_later_resubmission_is_canonical_and_earlier_is_flagged(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    assert db.get(Project, "prj_41").duplicate_of_id is None
    assert db.get(Project, "prj_07").duplicate_of_id == "prj_41"
    assert count(db, Project, Project.duplicate_of_id.is_not(None)) == 1


def test_demo_sessions_belong_to_the_right_roles(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    assert session_user(db, "organizer").role == Role.organizer
    assert session_user(db, "participant").role == Role.participant
    a, b = session_user(db, "judge_a"), session_user(db, "judge_b")
    assert a.role == b.role == Role.judge and a.id != b.id
    for judge in (a, b):
        assert count(db, Score, Score.judge_id == judge.id) > 0


def test_demo_sessions_removed_when_disabled(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    run_seed(db, FIXTURES_PATH, demo_sessions=False)
    assert all(session_user(db, label) is None for label in DEMO_SESSION_TOKENS)


def test_seeded_accounts_can_use_demo_password(db):
    run_seed(db, FIXTURES_PATH, demo_sessions=True)
    assert verify_password(DEMO_PASSWORD, db.get(User, "usr_organizer").password_hash)
    assert not verify_password("wrong", db.get(User, "usr_organizer").password_hash)
