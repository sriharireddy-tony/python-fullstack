"""Reusable model mixins.

Every table in this system shares the same handful of concerns: a UUID primary
key, timestamps, soft deletion, and -- for most -- a tenant. Defining them once
means a new model cannot accidentally omit one, and the column definitions stay
identical everywhere.

Columns are declared with plain ``mapped_column`` rather than ``declared_attr``.
SQLAlchemy 2.0 copies mixin columns (including foreign keys) onto each mapped
class, and the plain form gives type checkers the *unwrapped* attribute type on
instances -- ``uuid.UUID`` rather than ``Mapped[uuid.UUID]``.

See docs/04-database.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column


def pg_enum(enum_cls: type[PyEnum], name: str) -> SAEnum:
    """A Postgres enum column bound to a Python enum.

    Two settings are load-bearing and easy to get wrong:

    * ``values_callable`` -- by default SQLAlchemy persists the enum *member
      name* (``ENTERPRISE``), not its value (``enterprise``). Our database types
      are declared with the lowercase values, so without this every insert fails
      with "invalid input value for enum".

    * ``create_type=False`` -- migrations own all DDL. Left at the default, the
      ORM would try to emit CREATE TYPE for a type the migration already made.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_type=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


# uuid7 landed in the standard library in Python 3.14. This project supports
# 3.12+, so it is resolved once at import rather than assumed.
_uuid7: Callable[[], uuid.UUID] | None = getattr(uuid, "uuid7", None)


def new_id() -> uuid.UUID:
    """Generate a primary key.

    UUIDv7 is time-ordered, so inserts append to the end of the index instead of
    scattering across it the way v4 does. That keeps B-tree pages dense and
    avoids the write amplification random keys cause as a table grows.

    Falls back to uuid4 below Python 3.14. Both are valid UUIDs and the schema
    is unaffected -- only index locality differs, and mixed keys are harmless.
    """
    if _uuid7 is not None:
        return _uuid7()
    return uuid.uuid4()


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=new_id)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # onupdate is a Python callable, not func.now(), and that matters under
    # async SQLAlchemy: a SQL-side onupdate leaves the attribute expired after
    # every UPDATE, so the next read of `updated_at` triggers an implicit
    # refresh -- which async sessions forbid, raising MissingGreenlet from
    # whatever happened to touch it (usually response serialisation).
    #
    # A Python value is set during flush and is immediately readable. The cost
    # is that the timestamp comes from the application clock rather than the
    # database's; for an audit-adjacent field measured in seconds that is fine.
    # server_default stays so rows written outside the ORM still get a value.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class SoftDeleteMixin:
    """Nothing is hard-deleted in this system.

    Rows are flagged and hidden. This application measures people's work, and
    the first time someone disputes a reassignment or a priority override, an
    immutable history is what settles it.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class TenantScopedMixin:
    """Marks a table as belonging to a tenant.

    Applying this mixin is only half of tenant isolation -- the matching
    row-level-security policy must be created in the same migration. The table
    lists in ``app.models_registry`` keep the two in step, because new tables
    get added under deadline pressure and a missing policy is silent.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class VersionedMixin:
    """Optimistic locking.

    Every update carries the version the client read; ``UPDATE ... WHERE
    version = ?`` either matches or affects zero rows. Without it, a manager
    reassigning while a developer changes status means one silently overwrites
    the other -- a bug that is close to impossible to diagnose from a support
    report months later.
    """

    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
