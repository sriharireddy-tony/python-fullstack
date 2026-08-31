"""Authentication endpoints.

The HTTP layer only: parse, delegate to the service, set cookies, serialise.
No business rules live here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.errors import AuthenticationError
from app.modules.identity.dependencies import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    REFRESH_COOKIE,
    CurrentUser,
    RequestContext,
    get_scoped_db,
)
from app.modules.identity.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    PreferencesUpdate,
    ProfileUpdate,
    SessionResponse,
)
from app.modules.identity.service import AuthService, IssuedTokens

router = APIRouter()

REFRESH_PATH = f"{settings.API_V1_PREFIX}/auth"


def _set_auth_cookies(response: Response, tokens: IssuedTokens) -> None:
    """Attach the session cookies.

    The access and refresh tokens are httpOnly so no script can read them.
    The CSRF token deliberately is NOT httpOnly -- the frontend must read it to
    echo it back in a header, which is what proves the request came from our
    own page rather than a cross-site one.
    """
    secure = settings.COOKIE_SECURE
    domain = settings.COOKIE_DOMAIN
    refresh_max_age = settings.REFRESH_TOKEN_TTL_DAYS * 24 * 3600

    response.set_cookie(
        ACCESS_COOKIE,
        tokens.access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        domain=domain,
        max_age=settings.ACCESS_TOKEN_TTL_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        domain=domain,
        max_age=refresh_max_age,
        # Scoped to the auth path so the refresh token is not sent on every
        # ordinary API call -- it is only needed when renewing.
        path=REFRESH_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE,
        tokens.csrf_token,
        httponly=False,
        secure=secure,
        samesite="lax",
        domain=domain,
        max_age=refresh_max_age,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", domain=settings.COOKIE_DOMAIN)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, domain=settings.COOKIE_DOMAIN)
    response.delete_cookie(CSRF_COOKIE, path="/", domain=settings.COOKIE_DOMAIN)


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    return (
        request.headers.get("user-agent"),
        request.client.host if request.client else None,
    )


@router.post("/login", response_model=SessionResponse, summary="Sign in")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionResponse:
    user_agent, ip = _client_meta(request)
    service = AuthService(db)
    result = await service.login(
        payload.email, payload.password, user_agent=user_agent, ip_address=ip
    )
    _set_auth_cookies(response, result.tokens)
    return result.session


@router.post("/refresh", response_model=SessionResponse, summary="Renew the session")
async def refresh(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionResponse:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthenticationError("Not authenticated.")

    user_agent, ip = _client_meta(request)
    service = AuthService(db)
    result = await service.refresh(token, user_agent=user_agent, ip_address=ip)
    _set_auth_cookies(response, result.tokens)
    return result.session


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
async def logout(
    request: Request,
    response: Response,
    ctx: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    service = AuthService(db)
    await service.logout(request.cookies.get(REFRESH_COOKIE), ctx.access_jti)
    _clear_auth_cookies(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=SessionResponse, summary="Current session")
async def me(
    ctx: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_scoped_db)],
) -> SessionResponse:
    service = AuthService(db)
    principal = await service._load_principal(ctx.user_id)
    if principal is None or not principal.is_active:
        raise AuthenticationError("Session is no longer valid.")
    return await service.build_session(principal)


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change password (ends all other sessions)",
)
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    ctx: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_scoped_db)],
) -> Response:
    service = AuthService(db)
    await service.change_password(ctx.user_id, payload.current_password, payload.new_password)
    # This session's refresh token was revoked along with the others, so the
    # cookies are cleared and the user signs in again with the new password.
    _clear_auth_cookies(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------- profile

profile_router = APIRouter()


@router.patch("/me/profile", response_model=SessionResponse, summary="Update own profile")
async def update_profile(
    payload: ProfileUpdate,
    ctx: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_scoped_db)],
) -> SessionResponse:
    from app.modules.identity.repository import UserRepository

    users = UserRepository(db)
    user = await users.get_by_id(ctx.user_id)
    if user is None:
        raise AuthenticationError("Not authenticated.")
    user.full_name = payload.full_name
    await db.flush()
    return await AuthService(db).build_session(user)


@router.patch("/me/preferences", response_model=SessionResponse, summary="Update theme and density")
async def update_preferences(
    payload: PreferencesUpdate,
    ctx: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_scoped_db)],
) -> SessionResponse:
    from app.modules.identity.repository import UserRepository

    users = UserRepository(db)
    user = await users.get_by_id(ctx.user_id)
    if user is None:
        raise AuthenticationError("Not authenticated.")

    updated = dict(user.preferences or {})
    if payload.theme is not None:
        updated["theme"] = payload.theme
    if payload.density is not None:
        updated["density"] = payload.density
    # Reassign rather than mutate: SQLAlchemy does not track in-place changes
    # to a JSONB dict, so a mutation alone would never be persisted.
    user.preferences = updated
    await db.flush()

    return await AuthService(db).build_session(user)


__all__ = ["RequestContext", "profile_router", "router"]
