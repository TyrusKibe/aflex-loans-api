import json
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog


def write_audit_log(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    record = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=True),
        ip_address=ip_address,
    )
    db.add(record)

