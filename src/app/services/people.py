"""Judge invitations (organizers) and role changes (admins only)."""

import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.audit import audit
from app.models import Invite, JudgeTrack, Role, TeamMember, Track, User, new_id, utcnow
from app.routes.auth import EMAIL_RE, MIN_PASSWORD
from app.security import hash_password, hash_token

INVITE_LIFETIME = timedelta(days=7)


def as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _check_tracks(db: DbSession, track_ids: list[str]) -> list[str]:
    found = set(db.scalars(select(Track.id).where(Track.id.in_(track_ids))))
    missing = sorted(set(track_ids) - found)
    if missing:
        raise HTTPException(status_code=422, detail=[f"Unknown track: {', '.join(missing)}"])
    return sorted(found)


def _new_invite(db: DbSession, user: User, actor: User) -> str:
    # Re-inviting cancels earlier unused links, so only the newest one works.
    db.execute(update(Invite).where(Invite.user_id == user.id, Invite.used_at.is_(None))
               .values(used_at=utcnow()))
    token = secrets.token_urlsafe(32)
    db.add(Invite(token_hash=hash_token(token), user_id=user.id, created_by=actor.id,
                  expires_at=utcnow() + INVITE_LIFETIME))
    return token


def invite_judge(db: DbSession, actor: User, email: str, name: str, track_ids: list[str]) -> tuple[User, str | None]:
    """Create or update a judge. Returns (judge, raw invite token or None if they can already log in)."""
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail=["Enter a valid email address."])
    tracks = _check_tracks(db, track_ids)
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.role != Role.judge:
        raise HTTPException(status_code=409, detail={
            "error": "not_a_judge",
            "message": f"{email} already has a {user.role.value} account. Only an admin can change a role."})
    if user is None:
        if not name.strip():
            raise HTTPException(status_code=422, detail=["Enter the judge's name."])
        user = User(id=new_id("jdg"), email=email, name=name.strip(), role=Role.judge, password_hash=None)
        db.add(user)
        db.flush()
    current = set(db.scalars(select(JudgeTrack.track_id).where(JudgeTrack.judge_id == user.id)))
    for track_id in set(tracks) - current:
        db.add(JudgeTrack(judge_id=user.id, track_id=track_id))
    token = _new_invite(db, user, actor) if user.password_hash is None else None
    audit(db, actor, "judge.invite", "user", user.id, email=email, added_tracks=sorted(set(tracks) - current),
          link_issued=token is not None)
    db.commit()
    return user, token


def set_judge_tracks(db: DbSession, actor: User, judge_id: str, track_ids: list[str]) -> User:
    judge = db.get(User, judge_id)
    if judge is None or judge.role != Role.judge:
        raise HTTPException(status_code=404, detail="No such judge")
    tracks = set(_check_tracks(db, track_ids))
    current = {jt.track_id: jt for jt in judge.judge_tracks}
    for track_id, row in current.items():
        if track_id not in tracks:
            db.delete(row)
    for track_id in tracks - set(current):
        db.add(JudgeTrack(judge_id=judge.id, track_id=track_id))
    audit(db, actor, "judge.tracks", "user", judge.id, before=sorted(current), after=sorted(tracks))
    db.commit()
    db.refresh(judge)  # the loaded judge_tracks list does not see rows added/deleted via the session
    return judge


def find_invite(db: DbSession, token: str) -> Invite:
    invite = db.get(Invite, hash_token(token))
    if invite is None or invite.used_at is not None or invite.expires_at <= utcnow():
        raise HTTPException(status_code=404, detail="This invite link is invalid, used or expired")
    return invite


def accept_invite(db: DbSession, token: str, password: str) -> User:
    invite = find_invite(db, token)
    if len(password) < MIN_PASSWORD:
        raise HTTPException(status_code=422, detail=[f"Password must be at least {MIN_PASSWORD} characters."])
    user = db.get(User, invite.user_id)
    user.password_hash = hash_password(password)
    invite.used_at = utcnow()
    audit(db, user, "judge.accept_invite", "user", user.id)
    db.commit()
    return user


def change_role(db: DbSession, admin: User, user_id: str, new_role: str) -> User:
    try:
        role = Role(new_role)
    except ValueError:
        raise HTTPException(status_code=422, detail=[f"Role must be one of: {', '.join(r.value for r in Role)}"])
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="Admins cannot change their own role")
    if role != Role.participant and db.scalar(select(TeamMember.team_id).where(TeamMember.user_id == user.id)):
        raise HTTPException(status_code=409, detail={
            "error": "on_a_team",
            "message": "This user is on a team; making them staff or a judge would be a conflict of interest."})
    old = user.role
    if old != role:
        user.role = role
        audit(db, admin, "user.role_change", "user", user.id, old=old.value, new=role.value)
        db.commit()
    return user
