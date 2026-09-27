"""Team formation by invite link."""

import secrets

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.audit import audit
from app.models import Event, Role, Team, TeamMember, User, new_id
from app.services.projects import ensure_submissions_open, is_member, user_team


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)  # 192 bits: not guessable, safe to put in a URL


def _participants_only(user: User) -> None:
    if user.role != Role.participant:
        # Judges and organizers on teams would be a conflict of interest.
        raise HTTPException(status_code=403, detail="Only participants can be on teams")


def _not_already_on_a_team(db: DbSession, user: User, event: Event) -> None:
    existing = user_team(db, user, event.id)
    if existing is not None:
        raise HTTPException(status_code=409, detail={
            "error": "already_on_team", "team_id": existing.id,
            "message": f"You are already on team {existing.name} for this event."})


def create_team(db: DbSession, user: User, event: Event, name: str) -> Team:
    _participants_only(user)
    ensure_submissions_open(db, event)
    _not_already_on_a_team(db, user, event)
    name = name.strip()
    if not name or len(name) > 200:
        raise HTTPException(status_code=422, detail=["Team name is required (up to 200 characters)."])
    team = Team(id=new_id("tm"), event_id=event.id, name=name, invite_token=new_invite_token())
    db.add(team)
    db.flush()
    db.add(TeamMember(team_id=team.id, user_id=user.id, event_id=event.id))
    audit(db, user, "team.create", "team", team.id, event_id=event.id, name=name)
    db.commit()
    return team


def team_by_token(db: DbSession, token: str, lock: bool = False) -> Team:
    stmt = select(Team).where(Team.invite_token == token)
    if lock:
        # Postgres row lock: concurrent joins to the same team queue up here, so the
        # size check below cannot be raced past. (SQLite ignores it; single writer anyway.)
        stmt = stmt.with_for_update()
    team = db.scalar(stmt)
    if team is None:
        raise HTTPException(status_code=404, detail="This invite link is not valid (it may have been replaced)")
    return team


def member_count(db: DbSession, team_id: str) -> int:
    return db.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team_id))


def join_team(db: DbSession, user: User, token: str) -> Team:
    _participants_only(user)
    team = team_by_token(db, token, lock=True)
    event = db.get(Event, team.event_id)
    ensure_submissions_open(db, event)
    if is_member(db, user, team.id):
        return team
    _not_already_on_a_team(db, user, event)
    if member_count(db, team.id) >= event.max_team_size:
        raise HTTPException(status_code=409, detail={
            "error": "team_full", "message": f"This team is full ({event.max_team_size} members)."})
    db.add(TeamMember(team_id=team.id, user_id=user.id, event_id=event.id))
    audit(db, user, "team.join", "team", team.id)
    try:
        db.commit()
    except IntegrityError:
        # uq_one_team_per_event caught a concurrent join to another team.
        db.rollback()
        raise HTTPException(status_code=409, detail="You joined another team for this event meanwhile")
    return team


def rotate_invite(db: DbSession, user: User, team: Team) -> Team:
    if not is_member(db, user, team.id):
        raise HTTPException(status_code=403, detail="Only team members can change the invite link")
    team.invite_token = new_invite_token()
    audit(db, user, "team.invite_rotate", "team", team.id)
    db.commit()
    return team
