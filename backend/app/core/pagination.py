"""Shared pagination primitives.

Offset-based with a hard cap. The UI needs page numbers and a total count, and
keyset pagination provides neither. Offset only degrades on very large tables --
revisit past a few hundred thousand tickets in one tenant (docs/06-api.md, D-12).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class PageParams(BaseModel):
    """Query parameters for a paginated list endpoint."""

    page: Annotated[int, Query(ge=1, description="1-indexed page number")] = 1
    page_size: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description=f"Items per page (max {MAX_PAGE_SIZE})"),
    ] = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class Page[T](BaseModel):
    """Envelope returned by every list endpoint."""

    items: Sequence[T]
    total: int = Field(description="Total matching rows, ignoring pagination")
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))  # ceiling division

    @classmethod
    def create(cls, items: Sequence[T], total: int, params: PageParams) -> Page[T]:
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)


class SortParams(BaseModel):
    """``?sort=-created_at`` -- a leading minus means descending."""

    sort: Annotated[
        str | None,
        Query(description="Field to sort by. Prefix with '-' for descending."),
    ] = None

    def parse(self, allowed: set[str], default: str = "-created_at") -> tuple[str, bool]:
        """Return ``(field, descending)``, validated against an allowlist.

        The allowlist matters: sort fields reach the ORDER BY clause, and an
        unvalidated one is both an injection surface and a way to force an
        unindexed sort over the whole table.
        """
        raw = self.sort or default
        descending = raw.startswith("-")
        field = raw.lstrip("-")
        if field not in allowed:
            field = default.lstrip("-")
            descending = default.startswith("-")
        return field, descending
