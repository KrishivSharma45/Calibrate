"""Load fixtures.json and demo data into the database.

Idempotent: every row is inserted only if its id is not there yet, and
existing rows are never overwritten, so restarting the container keeps any
changes an organizer made through the UI.
"""

import hashlib
import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models import (
    Assignment,
    AuditLog,
    CustomQuestion,
    Event,
    JudgeTrack,
    Prize,
    Project,
    ProjectStatus,
    ProjectTag,
    Role,
    RubricCriterion,
    Score,
    ScoreItem,
    Session,
    Team,
    TeamMember,
    Track,
    User,
    VotingWindow,
    utcnow,
)
from app.security import create_session, hash_password, hash_token

# One shared demo password for every seeded account. Demo only; see README.
DEMO_PASSWORD = "dogfood-demo"

ORGANIZER_EMAIL = "organizer@dogfood.local"
ADMIN_EMAIL = "admin@dogfood.local"

DEFAULT_RUBRIC = [  # key, label, weight
    ("functionality", "Functionality", 0.4),
    ("quality", "Quality", 0.3),
    ("innovation", "Innovation", 0.3),
]

JUDGE_A = "jdg_09"  # trk_01, 5 fixture scores
JUDGE_B = "jdg_26"  # trk_01 + trk_03, 10 fixture scores; a peer of judge_a

# Fixed tokens so .dogfood.toml stays valid across restarts. They are public
# (they live in the repo), so they only exist while DEMO_SESSIONS is on.
DEMO_SESSION_TOKENS = {
    "organizer": "demo-organizer-6c1f0e4b9a7d2358",
    "judge_a": "demo-judge-a-2b8e5d71c4f9a036",
    "judge_b": "demo-judge-b-9f3a6c0d8e1b7425",
    "participant": "demo-participant-4e7b1a9c3d5f8062",
}

DEMO_EVENT_ID = "evt_demo"


def parse_ts(value: str) -> datetime:
    """ISO 8601 with a trailing Z -> aware UTC datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def participant_id(email: str) -> str:
    """Stable id from the email, so re-seeding finds the same user."""
    return "usr_" + hashlib.sha256(email.lower().encode()).hexdigest()[:12]


def name_from_email(email: str) -> str:
    local = email.split("@")[0]
    return " ".join(part.capitalize() for part in local.replace("_", ".").split(".") if part)


class _Inserter:
    """Adds rows whose primary key is not in the DB yet; counts what it did."""

    def __init__(self, db: DbSession):
        self.db = db
        self.added: dict[str, int] = defaultdict(int)

    def add(self, obj) -> bool:
        mapper = type(obj).__mapper__
        pk = tuple(getattr(obj, col.key) for col in mapper.primary_key)
        if self.db.get(type(obj), pk if len(pk) > 1 else pk[0]) is not None:
            return False
        self.db.add(obj)
        self.added[type(obj).__tablename__] += 1
        return True


def _canonical_projects(projects: list[dict]) -> dict[str, str | None]:
    """Map project id -> id of the project it duplicates (None if canonical).

    Rule: a team has one project per event. When a team has several, the one
    submitted last is the real submission; earlier ones are duplicates of it.
    """
    by_team: dict[str, list[dict]] = defaultdict(list)
    for p in projects:
        by_team[p["team"]].append(p)
    result: dict[str, str | None] = {}
    for group in by_team.values():
        group.sort(key=lambda p: (p.get("submitted_at") or "", p["id"]))
        keeper = group[-1]["id"]
        for p in group:
            result[p["id"]] = None if p["id"] == keeper else keeper
    return result


def _add_rubric(ins: _Inserter, event_id: str) -> None:
    for pos, (key, label, weight) in enumerate(DEFAULT_RUBRIC):
        ins.add(RubricCriterion(id=f"crt_{event_id}_{key}", event_id=event_id, key=key,
                                label=label, weight=weight, position=pos))


def import_fixtures(db: DbSession, data: dict, password_hash: str) -> dict[str, int]:
    ins = _Inserter(db)
    ev = data["event"]
    event_id = ev["id"]

    ins.add(Event(id=event_id, name=ev["name"], max_team_size=4,
                  description="Imported from the shared DOGFOOD fixtures.",
                  submissions_close=parse_ts(ev["submissions_close"])))
    for t in data["tracks"]:
        ins.add(Track(id=t["id"], event_id=event_id, name=t["name"]))
    _add_rubric(ins, event_id)
    db.flush()

    for j in data["judges"]:
        ins.add(User(id=j["id"], email=j["email"].lower(), name=j["name"],
                     role=Role.judge, password_hash=password_hash))
        for track_id in j["tracks"]:
            ins.add(JudgeTrack(judge_id=j["id"], track_id=track_id))

    for t in data["teams"]:
        ins.add(Team(id=t["id"], event_id=event_id, name=t["name"],
                     invite_token=secrets.token_urlsafe(24)))
        for email in t["members"]:
            uid = participant_id(email)
            ins.add(User(id=uid, email=email.lower(), name=name_from_email(email),
                         role=Role.participant, password_hash=password_hash))
            ins.add(TeamMember(team_id=t["id"], user_id=uid, event_id=event_id))
    db.flush()

    duplicate_of = _canonical_projects(data["projects"])
    # Canonical rows first so duplicate_of always points at an existing row.
    for p in sorted(data["projects"], key=lambda p: duplicate_of[p["id"]] is not None):
        ins.add(Project(
            id=p["id"], event_id=event_id, team_id=p["team"], track_id=p["track"],
            title=p["title"], tagline=p.get("summary", ""), description=p.get("summary", ""),
            repo_url=p.get("repo_url", ""), status=ProjectStatus.submitted,
            submitted_at=parse_ts(p["submitted_at"]) if p.get("submitted_at") else None,
            duplicate_of_id=duplicate_of[p["id"]],
        ))
        db.flush()

    criteria = {c.key: c.id for c in db.scalars(
        select(RubricCriterion).where(RubricCriterion.event_id == event_id))}
    for s in data["scores"]:
        # A score implies the judge was assigned the project.
        ins.add(Assignment(judge_id=s["judge"], project_id=s["project"]))
        score = Score(id=f"sc_{s['judge']}_{s['project']}", judge_id=s["judge"],
                      project_id=s["project"], comment=s.get("comment") or "")
        if ins.add(score):
            score.items = [ScoreItem(criterion_id=criteria[k], value=v)
                           for k, v in s["criteria"].items() if k in criteria]
    db.flush()
    return dict(ins.added)


def seed_accounts_and_demo_event(db: DbSession, password_hash: str) -> dict[str, int]:
    ins = _Inserter(db)
    ins.add(User(id="usr_organizer", email=ORGANIZER_EMAIL, name="Olive Organizer",
                 role=Role.organizer, password_hash=password_hash))
    ins.add(User(id="usr_admin", email=ADMIN_EMAIL, name="Ari Admin",
                 role=Role.admin, password_hash=password_hash))

    # An open event for the live demo: submit -> judge -> publish.
    if ins.add(Event(id=DEMO_EVENT_ID, name="Raptors Live Demo 2026", max_team_size=4,
                     description="An open event for trying the full flow.",
                     submissions_open=utcnow() - timedelta(days=1),
                     submissions_close=utcnow() + timedelta(days=30))):
        db.flush()
        for i, name in enumerate(["AI tools", "Social good", "Games"], start=1):
            ins.add(Track(id=f"trk_demo_{i}", event_id=DEMO_EVENT_ID, name=name))
        ins.add(Prize(id="prz_demo_1", event_id=DEMO_EVENT_ID, name="Best overall",
                      description="Top normalized score across all tracks."))
        ins.add(CustomQuestion(id="q_demo_1", event_id=DEMO_EVENT_ID, position=0,
                               prompt="What would you build next?", required=False))
        _add_rubric(ins, DEMO_EVENT_ID)
        db.flush()
        ins.add(JudgeTrack(judge_id=JUDGE_A, track_id="trk_demo_1"))
        ins.add(JudgeTrack(judge_id=JUDGE_B, track_id="trk_demo_1"))
        # Community voting open alongside submissions, closing 2 days after them.
        ins.add(VotingWindow(event_id=DEMO_EVENT_ID, opens_at=utcnow() - timedelta(days=1),
                             closes_at=utcnow() + timedelta(days=32)))
    db.flush()
    return dict(ins.added)


def demo_session_users(db: DbSession, fixture_event: dict) -> dict[str, str]:
    """Which user each demo session belongs to."""
    first_team = db.scalars(select(Team).where(Team.event_id == fixture_event["id"])
                            .order_by(Team.id)).first()
    participant = db.scalars(select(TeamMember.user_id).where(TeamMember.team_id == first_team.id)
                             .order_by(TeamMember.user_id)).first()
    return {"organizer": "usr_organizer", "judge_a": JUDGE_A,
            "judge_b": JUDGE_B, "participant": participant}


def sync_demo_sessions(db: DbSession, enabled: bool, owners: dict[str, str]) -> None:
    """Create the fixed demo sessions when enabled; delete them when not."""
    for label, token in DEMO_SESSION_TOKENS.items():
        existing = db.get(Session, hash_token(token))
        if existing is not None:
            db.delete(existing)
        if enabled:
            db.flush()
            create_session(db, db.get(User, owners[label]), token=token,
                           expires_at=utcnow() + timedelta(days=3650))


def run_seed(db: DbSession, fixtures_path: Path, demo_sessions: bool) -> dict[str, int]:
    data = json.loads(Path(fixtures_path).read_text(encoding="utf-8"))
    # bcrypt is slow on purpose; hash the shared demo password once, not 120 times.
    existing = db.get(User, "usr_organizer")
    password_hash = existing.password_hash if existing else hash_password(DEMO_PASSWORD)

    added = import_fixtures(db, data, password_hash)
    for table, n in seed_accounts_and_demo_event(db, password_hash).items():
        added[table] = added.get(table, 0) + n
    sync_demo_sessions(db, demo_sessions, demo_session_users(db, data["event"]))
    if added:
        db.add(AuditLog(action="seed.import", entity_type="event", entity_id=data["event"]["id"],
                        detail=json.dumps(added, sort_keys=True)))
    db.commit()
    return added


def dogfood_headers() -> str:
    return "\n".join(f'{label:<11} = "Cookie: session={token}"'
                     for label, token in DEMO_SESSION_TOKENS.items())
