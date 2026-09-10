from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi_users import BaseUserManager, IntegerIDMixin, exceptions
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.db_dep import get_db
from vibetrading.config import get_settings
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import seed_default_watchlist_if_empty

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8


class UserManager(IntegerIDMixin, BaseUserManager[UserORM, int]):
    @property
    def reset_password_token_secret(self) -> str:
        return get_settings().auth_secret_key

    @property
    def verification_token_secret(self) -> str:
        return get_settings().auth_secret_key

    async def validate_password(self, password: str, user) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise exceptions.InvalidPasswordException(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )

    async def on_after_register(self, user: UserORM, request=None) -> None:
        logger.info("New account registered: user_id=%s", user.id)
        # Reuse the same session user_db was constructed with (see
        # get_user_db below) rather than opening a fresh one -- this is
        # what keeps the dependency-override in tests (and, more subtly,
        # a swapped database_url in production) effective for this
        # follow-up write too.
        session = self.user_db.session
        await seed_default_watchlist_if_empty(session, user.id)
        await session.commit()


async def get_user_db(session: AsyncSession = Depends(get_db)) -> AsyncIterator[SQLAlchemyUserDatabase]:
    yield SQLAlchemyUserDatabase(session, UserORM)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncIterator[UserManager]:
    yield UserManager(user_db)
