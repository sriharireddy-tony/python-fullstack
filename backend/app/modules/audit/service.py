"""Audit recording.

Called by other services **inside the same transaction as the change itself**.
If the audit write were a separate transaction or a background task, a crash
between the two would leave a change with no record -- precisely the case where
the record matters most.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Page, PageParams
from app.modules.audit.models import ActorType, AuditAction, AuditLog

# Ticket and comment content must never be copied into the audit payload. The
# audit log is exposed in the UI to admins and managers, and this system holds
# HR support data -- salaries, personal details, identifiers. Record *that* a
# field changed, never what it changed to.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "body",
        "steps_to_reproduce",
        "expected_result",
        "actual_result",
        "resolution_notes",
        "password",
        "password_hash",
        "reporter_email",
        "reporter_name",
        "original_filename",
    }
)

REDACTED = "[redacted]"


def _scrub(values: dict[str, Any]) -> dict[str, Any]:
    return {key: (REDACTED if key in REDACTED_FIELDS else value) for key, value in values.items()}


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_id: uuid.UUID | None,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        actor_type: ActorType = ActorType.USER,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        changes: dict[str, Any] = {}
        if before is not None:
            changes["before"] = _scrub(before)
        if after is not None:
            changes["after"] = _scrub(after)

        entry = AuditLog(
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action.value,
            entity_type=entity_type,
            entity_id=entity_id,
            changes=changes,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(entry)
        # Flushed rather than committed: the caller owns the transaction, so the
        # audit row lands or rolls back together with the change it describes.
        await self._session.flush()
        return entry

    async def list_entries(
        self,
        *,
        tenant_id: uuid.UUID,
        params: PageParams,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> Page[AuditLog]:
        conditions = [AuditLog.tenant_id == tenant_id]
        if entity_type:
            conditions.append(AuditLog.entity_type == entity_type)
        if entity_id:
            conditions.append(AuditLog.entity_id == entity_id)
        if actor_id:
            conditions.append(AuditLog.actor_id == actor_id)
        if action:
            conditions.append(AuditLog.action == action)

        total = await self._session.scalar(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
        result = await self._session.execute(
            select(AuditLog)
            .where(*conditions)
            .order_by(desc(AuditLog.created_at))
            .offset(params.offset)
            .limit(params.limit)
        )
        return Page.create(list(result.scalars().all()), total or 0, params)
