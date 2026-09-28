"""Database tables.

Conventions
- Primary keys are short strings. Rows imported from fixtures.json keep their
  fixture id (evt_01, jdg_07, prj_41) so the import is traceable and
  idempotent. Rows created in the app get a random id with the same prefix
  style (see new_id).
- All timestamps are stored as UTC. `UTCDateTime` makes sure values read back
  are timezone-aware even on SQLite, which drops tz info.
- Roles are global (one per user). Judges are scoped further by the tracks
  they are attached to (judge_tracks) and the projects assigned to them.
"""

import enum
import secrets
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime that is always UTC going in and coming out."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Role(str, enum.Enum):
    participant = "participant"
    judge = "judge"
    organizer = "organizer"
    admin = "admin"


class ProjectStatus(str, enum.Enum):
    draft = "draft"
    submitted = "submitted"


# --- people and sessions ---------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-case
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(100))  # bcrypt; NULL = cannot log in
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    judge_tracks: Mapped[list["JudgeTrack"]] = relationship(back_populates="judge")


class Session(Base):
    """Server-side login session. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship()


class Invite(Base):
    """One-time link for an invited judge to set a password. Stored by SHA-256, like sessions."""

    __tablename__ = "invites"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# --- events ----------------------------------------------------------------


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    submissions_open: Mapped[datetime | None] = mapped_column(UTCDateTime)
    submissions_close: Mapped[datetime] = mapped_column(UTCDateTime)
    max_team_size: Mapped[int] = mapped_column(Integer, default=4)
    results_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # Explicit order everywhere: without it, a collection comes back in insertion
    # order within one session but in arbitrary DB order after a reload.
    tracks: Mapped[list["Track"]] = relationship(back_populates="event", order_by="Track.name")
    prizes: Mapped[list["Prize"]] = relationship(back_populates="event", order_by="Prize.name")
    questions: Mapped[list["CustomQuestion"]] = relationship(
        back_populates="event", order_by="CustomQuestion.position"
    )
    criteria: Mapped[list["RubricCriterion"]] = relationship(
        back_populates="event", order_by="RubricCriterion.position"
    )

    __table_args__ = (CheckConstraint("max_team_size >= 1", name="ck_event_team_size"),)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))

    event: Mapped[Event] = relationship(back_populates="tracks")


class Prize(Base):
    __tablename__ = "prizes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    event: Mapped[Event] = relationship(back_populates="prizes")


class CustomQuestion(Base):
    """Organizer-defined extra field on the submission form."""

    __tablename__ = "custom_questions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(String(500))
    required: Mapped[bool] = mapped_column(default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)

    event: Mapped[Event] = relationship(back_populates="questions")


# --- teams and projects ----------------------------------------------------


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    invite_token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    members: Mapped[list["TeamMember"]] = relationship(back_populates="team")


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Copied from the team so the DB can enforce "one team per user per event".
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship()

    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_one_team_per_event"),)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    tagline: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    thumbnail_url: Mapped[str] = mapped_column(String(500), default="")
    demo_video_url: Mapped[str] = mapped_column(String(500), default="")
    repo_url: Mapped[str] = mapped_column(String(500), default="")
    live_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, native_enum=False, length=20), default=ProjectStatus.draft
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    # Set when this row is a superseded copy of another project (fixture prj_07 -> prj_41).
    # Duplicates stay visible to organizers but are excluded from the gallery and results.
    duplicate_of_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))

    team: Mapped[Team] = relationship()
    track: Mapped[Track | None] = relationship()
    tags: Mapped[list["ProjectTag"]] = relationship(back_populates="project", cascade="all, delete-orphan",
                                                    order_by="ProjectTag.tag")
    answers: Mapped[list["ProjectAnswer"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # A team has at most one *active* project per event. Flagged duplicates are exempt,
        # which is how the fixture duplicate can be imported without breaking the rule.
        Index(
            "uq_one_active_project_per_team",
            "team_id",
            unique=True,
            postgresql_where=text("duplicate_of_id IS NULL"),
            sqlite_where=text("duplicate_of_id IS NULL"),
        ),
    )


class ProjectTag(Base):
    __tablename__ = "project_tags"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(50), primary_key=True, index=True)  # lower-case

    project: Mapped[Project] = relationship(back_populates="tags")


class ProjectAnswer(Base):
    __tablename__ = "project_answers"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("custom_questions.id", ondelete="CASCADE"), primary_key=True
    )
    answer: Mapped[str] = mapped_column(Text, default="")

    project: Mapped[Project] = relationship(back_populates="answers")


# --- judging ---------------------------------------------------------------


class JudgeTrack(Base):
    """Which tracks a judge may judge. The basis for track isolation."""

    __tablename__ = "judge_tracks"

    judge_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    track_id: Mapped[str] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)

    judge: Mapped[User] = relationship(back_populates="judge_tracks")
    track: Mapped[Track] = relationship()


class Assignment(Base):
    """A judge is asked to review a project."""

    __tablename__ = "assignments"

    judge_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RubricCriterion(Base):
    """One scoring criterion with its weight. Weights per event must sum to 1 (checked in app)."""

    __tablename__ = "rubric_criteria"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(50))  # e.g. "functionality"
    label: Mapped[str] = mapped_column(String(100))
    weight: Mapped[float] = mapped_column(Numeric(5, 4))
    position: Mapped[int] = mapped_column(Integer, default=0)

    event: Mapped[Event] = relationship(back_populates="criteria")

    __table_args__ = (
        UniqueConstraint("event_id", "key", name="uq_criterion_key"),
        CheckConstraint("weight >= 0 AND weight <= 1", name="ck_criterion_weight"),
    )


class Score(Base):
    """One judge's review of one project. Per-criterion values live in score_items."""

    __tablename__ = "scores"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    judge_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list["ScoreItem"]] = relationship(back_populates="score", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("judge_id", "project_id", name="uq_one_score_per_judge_project"),)


class ScoreItem(Base):
    __tablename__ = "score_items"

    score_id: Mapped[str] = mapped_column(ForeignKey("scores.id", ondelete="CASCADE"), primary_key=True)
    criterion_id: Mapped[str] = mapped_column(
        ForeignKey("rubric_criteria.id", ondelete="CASCADE"), primary_key=True
    )
    value: Mapped[int] = mapped_column(Integer)

    score: Mapped[Score] = relationship(back_populates="items")

    __table_args__ = (CheckConstraint("value BETWEEN 1 AND 5", name="ck_score_value_range"),)


# --- community (T3) ------------------------------------------------------------
# Separate tables (not new columns on events) so an existing database picks them
# up with create_all, without a migration.


class VotingWindow(Base):
    __tablename__ = "voting_windows"

    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    opens_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closes_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (CheckConstraint("opens_at < closes_at", name="ck_voting_window_order"),)


class Vote(Base):
    """One community vote. The unique constraint is the anti-stuffing rule."""

    __tablename__ = "votes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_one_vote_per_user_project"),)


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    hidden_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # moderation; never deleted

    user: Mapped[User] = relationship()


# --- audit -----------------------------------------------------------------


class AuditLog(Base):
    """Append-only record of sensitive actions. The app never updates or deletes rows."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(50), index=True)  # e.g. "score.update"
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")  # JSON text
