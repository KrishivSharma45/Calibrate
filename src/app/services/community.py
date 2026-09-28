"""T3: community voting, randomized ballots, comments, and the anti-abuse rules around them.

Who may vote: logged-in participants only (judges and staff already influence the outcome),
never for their own team's project, only while the event's voting window is open
(database clock), at most once per project (DB unique constraint), and at most
30 votes per 10 minutes. Every vote and un-vote is audited.
Tallies are visible to staff at any time and to everyone else only after the window closes.
"""

import hashlib
import random
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession, selectinload

from app.audit import audit
from app.auth import STAFF, comment_limit, vote_limit
from app.models import (
    Comment, Event, Project, ProjectStatus, Role, TeamMember, User, Vote, VotingWindow, new_id,
)
from app.services.projects import db_now

MAX_COMMENT = 2000


# --- window -------------------------------------------------------------------------------------


def window_state(db: DbSession, event_id: str, now: datetime | None = None) -> str:
    """'none' | 'upcoming' | 'open' | 'closed'."""
    window = db.get(VotingWindow, event_id)
    if window is None:
        return "none"
    now = now or db_now(db)
    if now < window.opens_at:
        return "upcoming"
    return "open" if now < window.closes_at else "closed"


def set_window(db: DbSession, actor: User, event: Event, opens_at: datetime | None, closes_at: datetime | None):
    if opens_at is None or closes_at is None or opens_at >= closes_at:
        raise HTTPException(status_code=422, detail=["Voting needs an opening time before its closing time."])
    window = db.get(VotingWindow, event.id)
    before = (window.opens_at, window.closes_at) if window else None
    if window is None:
        window = VotingWindow(event_id=event.id, opens_at=opens_at, closes_at=closes_at)
        db.add(window)
    else:
        window.opens_at, window.closes_at = opens_at, closes_at
    audit(db, actor, "voting.window", "event", event.id, before=before, after=(opens_at, closes_at))
    db.commit()
    return window


# --- ballot ----------------------------------------------------------------------------------------


def ballot_projects(db: DbSession, event_id: str) -> list[Project]:
    return list(db.scalars(select(Project).where(
        Project.event_id == event_id, Project.status == ProjectStatus.submitted,
        Project.duplicate_of_id.is_(None)).options(selectinload(Project.track), selectinload(Project.team))
        .order_by(Project.id)))


def shuffled_for(user_id: str, event_id: str, projects: list[Project]) -> list[Project]:
    """Per-user, per-event stable shuffle. Same user sees the same order on every visit
    (no reroll-until-first), different users see different orders (no position bias)."""
    seed = int.from_bytes(hashlib.sha256(f"{event_id}:{user_id}".encode()).digest()[:8], "big")
    order = list(projects)
    random.Random(seed).shuffle(order)
    return order


def my_votes(db: DbSession, user: User, event_id: str) -> set[str]:
    return set(db.scalars(select(Vote.project_id).where(Vote.user_id == user.id, Vote.event_id == event_id)))


# --- voting -------------------------------------------------------------------------------------------


def _voteable(db: DbSession, user: User, project_id: str) -> Project:
    if user.role != Role.participant:
        raise HTTPException(status_code=403, detail="Only participants vote (judges and staff already decide)")
    project = db.get(Project, project_id)
    if project is None or project.status != ProjectStatus.submitted or project.duplicate_of_id is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    state = window_state(db, project.event_id)
    if state != "open":
        raise HTTPException(status_code=409, detail={"error": "voting_not_open", "message": f"Voting is {state}."})
    if db.get(TeamMember, (project.team_id, user.id)) is not None:
        raise HTTPException(status_code=403, detail={"error": "own_project",
                                                     "message": "You cannot vote for your own team."})
    if not vote_limit.allow(f"user:{user.id}"):
        audit(db, user, "vote.rate_limited", "project", project.id)
        db.commit()
        raise HTTPException(status_code=429, detail="Too many votes; slow down.")
    return project


def cast_vote(db: DbSession, user: User, project_id: str) -> Vote:
    project = _voteable(db, user, project_id)
    vote = Vote(id=new_id("vot"), event_id=project.event_id, project_id=project.id, user_id=user.id)
    db.add(vote)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        audit(db, user, "vote.duplicate", "project", project.id)   # anti-abuse trail
        db.commit()
        raise HTTPException(status_code=409, detail={"error": "already_voted",
                                                     "message": "You already voted for this project."})
    audit(db, user, "vote.cast", "project", project.id)
    db.commit()
    return vote


def remove_vote(db: DbSession, user: User, project_id: str) -> None:
    project = _voteable(db, user, project_id)
    vote = db.scalar(select(Vote).where(Vote.user_id == user.id, Vote.project_id == project.id))
    if vote is not None:
        db.delete(vote)
        audit(db, user, "vote.remove", "project", project.id)
        db.commit()


def tallies(db: DbSession, event: Event, viewer: User | None) -> dict:
    """Vote counts. Hidden from non-staff until the window closes."""
    state = window_state(db, event.id)
    is_staff = viewer is not None and viewer.role in STAFF
    if not is_staff and state != "closed":
        raise HTTPException(status_code=403, detail={
            "error": "votes_hidden", "message": "Vote counts are hidden until voting closes."})
    window = db.get(VotingWindow, event.id)
    counts = dict(db.execute(select(Vote.project_id, func.count()).where(Vote.event_id == event.id)
                             .group_by(Vote.project_id)).tuples().all())
    late = {}
    if is_staff and window is not None:
        # Anti-abuse signal: votes from accounts created after voting opened (possible sock puppets).
        late = dict(db.execute(select(Vote.project_id, func.count()).join(User, User.id == Vote.user_id)
                               .where(Vote.event_id == event.id, User.created_at >= window.opens_at)
                               .group_by(Vote.project_id)).tuples().all())
    rows = sorted(({"project_id": p.id, "title": p.title, "votes": counts.get(p.id, 0),
                    **({"votes_from_new_accounts": late.get(p.id, 0)} if is_staff else {})}
                   for p in ballot_projects(db, event.id)), key=lambda r: (-r["votes"], r["title"].lower()))
    return {"event_id": event.id, "state": state, "projects": rows}


# --- comments -----------------------------------------------------------------------------------------


def visible_comments(db: DbSession, project_id: str, include_hidden: bool = False) -> list[Comment]:
    stmt = select(Comment).where(Comment.project_id == project_id).options(selectinload(Comment.user)) \
        .order_by(Comment.created_at, Comment.id)
    if not include_hidden:
        stmt = stmt.where(Comment.hidden_at.is_(None))
    return list(db.scalars(stmt))


def add_comment(db: DbSession, user: User, project_id: str, body: str) -> Comment:
    project = db.get(Project, project_id)
    if project is None or project.status != ProjectStatus.submitted or project.duplicate_of_id is not None:
        raise HTTPException(status_code=404, detail="Project not found")
    body = body.strip()
    if not body or len(body) > MAX_COMMENT:
        raise HTTPException(status_code=422, detail=[f"Comments are 1 to {MAX_COMMENT} characters."])
    if not comment_limit.allow(f"user:{user.id}"):
        raise HTTPException(status_code=429, detail="Too many comments; wait a minute.")
    comment = Comment(id=new_id("cmt"), project_id=project.id, user_id=user.id, body=body)
    db.add(comment)
    db.flush()
    audit(db, user, "comment.create", "comment", comment.id, project_id=project.id)
    db.commit()
    return comment


def hide_comment(db: DbSession, actor: User, comment_id: str) -> Comment:
    comment = db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.hidden_at is None:
        comment.hidden_at = db_now(db)
        audit(db, actor, "comment.hide", "comment", comment.id, project_id=comment.project_id)
        db.commit()
    return comment


def comment_out(c: Comment) -> dict:
    return {"id": c.id, "project_id": c.project_id, "author": c.user.name, "body": c.body,
            "created_at": c.created_at.isoformat(), "hidden": c.hidden_at is not None}
