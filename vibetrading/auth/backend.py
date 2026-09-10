from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, WebSocket, WebSocketException, status
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.manager import BaseUserManager

from vibetrading.auth.manager import get_user_manager
from vibetrading.config import get_settings
from vibetrading.logging_conf import bind_tenant_id
from vibetrading.persistence.orm_models import UserORM

# 14-day session, matching a "stay signed in" dashboard convention rather
# than a short API-token lifetime -- this is a browser session cookie, not
# a machine-to-machine credential.
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 14

cookie_transport = CookieTransport(
    cookie_name="vibetrading_session",
    cookie_max_age=SESSION_LIFETIME_SECONDS,
    cookie_secure=get_settings().auth_cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=get_settings().auth_secret_key, lifetime_seconds=SESSION_LIFETIME_SECONDS)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[UserORM, int](get_user_manager, [auth_backend])

# The raw fastapi-users-built dependencies. Wrapped below (current_active_user,
# current_dashboard_user) so every authenticated request gets its tenant_id
# bound into logging_conf's contextvar for the request's whole duration --
# every log line downstream (agents, risk engine, broker calls) is then
# attributable to the tenant it ran for, without threading a logger
# parameter through every function. dependency_overrides in tests targets
# the wrapper (the same object every route's Depends() uses), so overriding
# current_active_user with a fake user still works exactly as before --
# it just bypasses the tenant_id binding too, which no test needs.
_current_active_user_raw = fastapi_users.current_user(active=True)
_current_user_optional = fastapi_users.current_user(active=True, optional=True)


async def current_active_user(user: UserORM = Depends(_current_active_user_raw)) -> AsyncIterator[UserORM]:
    """For pure API/JSON consumers: raises a 401 JSON response when unauthenticated."""
    with bind_tenant_id(user.id):
        yield user


class NotAuthenticated(Exception):
    """Raised by current_dashboard_user; handled by an app-wide exception
    handler that redirects to /login (a plain 303 for a normal page
    navigation, or an HX-Redirect for an in-flight HTMX request -- see
    api/app.py)."""


async def current_dashboard_user(user: UserORM | None = Depends(_current_user_optional)) -> AsyncIterator[UserORM]:
    """For dashboard HTML/HTMX routes: never raises 401 JSON directly (see
    NotAuthenticated + its exception handler in api/app.py), redirecting a
    human to /login instead of showing them a raw JSON error page."""
    if user is None:
        raise NotAuthenticated()
    with bind_tenant_id(user.id):
        yield user


async def current_websocket_user(
    websocket: WebSocket, user_manager: BaseUserManager[UserORM, int] = Depends(get_user_manager)
) -> UserORM:
    """fastapi-users' current_user()-built dependencies (current_active_user,
    current_dashboard_user) are built on transports/strategies that assume
    an HTTP Request -- CookieTransport's underlying APIKeyCookie dependency
    outright crashes (TypeError, not a clean 401) when FastAPI resolves it
    against a WebSocket scope instead. This reads and verifies the same
    session cookie by hand -- same JWTStrategy, same secret, same 14-day
    lifetime -- for the one route (the dashboard WebSocket) that needs
    auth on a WebSocket rather than a Request.
    """
    token = websocket.cookies.get(cookie_transport.cookie_name)
    user = await get_jwt_strategy().read_token(token, user_manager) if token else None
    if user is None or not user.is_active:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    return user


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        cookie_transport.cookie_name,
        token,
        max_age=cookie_transport.cookie_max_age,
        path=cookie_transport.cookie_path,
        domain=cookie_transport.cookie_domain,
        secure=cookie_transport.cookie_secure,
        httponly=cookie_transport.cookie_httponly,
        samesite=cookie_transport.cookie_samesite,
    )


def clear_session_cookie(response) -> None:
    response.set_cookie(
        cookie_transport.cookie_name,
        "",
        max_age=0,
        path=cookie_transport.cookie_path,
        domain=cookie_transport.cookie_domain,
        secure=cookie_transport.cookie_secure,
        httponly=cookie_transport.cookie_httponly,
        samesite=cookie_transport.cookie_samesite,
    )
