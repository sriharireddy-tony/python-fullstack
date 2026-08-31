"""Request and response models for the identity module."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.permissions import Role


class LoginRequest(BaseModel):
    """Credentials.

    ``email`` is deliberately a plain string, not ``EmailStr``. At login the
    address is a lookup key, not a value to validate: rejecting a malformed one
    with 422 while a wrong password gets 401 tells an attacker something, and it
    would also refuse perfectly valid internal addresses that the strict
    validator treats as special-use domains. Creation still validates properly.
    """

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    # Length is the only rule. Composition rules (a symbol, a digit, a capital)
    # push people towards predictable substitutions and writing passwords down;
    # length is what actually resists guessing.
    new_password: str = Field(min_length=12, max_length=256)


class TeamSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class UserPreferences(BaseModel):
    theme: str = "system"
    density: str = "comfortable"


class PreferencesUpdate(BaseModel):
    theme: str | None = Field(default=None, pattern="^(light|dark|system)$")
    density: str | None = Field(default=None, pattern="^(comfortable|compact)$")


class ProfileUpdate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)


class SessionResponse(BaseModel):
    """Returned by /auth/me and /auth/login.

    The resolved permission list is included so the frontend can hide controls
    the user cannot use. That is a UX convenience only -- the API enforces every
    one of these independently.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role | None
    is_platform_admin: bool = False
    permissions: list[str]
    teams: list[TeamSummary] = []
    preferences: UserPreferences
    tenant_id: uuid.UUID | None = None
    last_login_at: datetime | None = None


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None = None
