"""Write one row to the append-only audit log."""

import json

from sqlalchemy.orm import Session as DbSession

from app.models import AuditLog, User


def audit(db: DbSession, actor: User | None, action: str, entity_type: str, entity_id: str,
          **detail) -> None:
    db.add(AuditLog(actor_id=actor.id if actor else None, action=action,
                    entity_type=entity_type, entity_id=entity_id,
                    detail=json.dumps(detail, sort_keys=True, default=str)))
