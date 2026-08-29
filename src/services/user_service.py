"""
User Management Service

Handles user CRUD operations, authentication via API key,
and user status management for the multi-user subscription system.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.data.models import Subscription, User
from src.services.base import BaseService

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger("user_service")


class UserService(BaseService):
    """User management service for multi-user subscription system.

    Provides CRUD operations for users, API key authentication,
    and user status management.

    Attributes:
        db: Database manager instance
    """

    def get_user_by_id(self, user_id: int) -> User | None:
        """Get a user by their ID.

        Args:
            user_id: The user's ID

        Returns:
            User object if found, None otherwise
        """
        with self.readonly_session() as session:
            return session.query(User).filter(User.id == user_id).first()

    def get_user_by_email(self, email: str) -> User | None:
        """Get a user by their email address.

        Args:
            email: The user's email address

        Returns:
            User object if found, None otherwise
        """
        with self.readonly_session() as session:
            return session.query(User).filter(User.email == email.lower()).first()

    def get_user_by_api_key(self, api_key: str) -> User | None:
        """Get a user by their API key.

        Args:
            api_key: The user's API key

        Returns:
            User object if found and active, None otherwise
        """
        if not api_key:
            return None

        with self.readonly_session() as session:
            return (
                session.query(User).filter(User.api_key == api_key, User.status == "active").first()
            )

    def create_user(
        self,
        email: str,
        name: str | None = None,
        tier: str = "basic",
        generate_api_key: bool = False,
    ) -> User | None:
        """Create a new user with optional subscription.

        Args:
            email: User's email address (must be unique)
            name: Optional display name
            tier: Subscription tier (basic, premium, enterprise)
            generate_api_key: Whether to generate an API key

        Returns:
            Created User object, or None if creation failed
        """
        try:
            with self.session_scope() as session:
                # Check for existing user
                existing = session.query(User).filter(User.email == email.lower()).first()
                if existing:
                    logger.warning(f"User with email {email} already exists")
                    return None

                # Create user
                user = User(email=email.lower(), name=name, status="active")

                if generate_api_key:
                    user.generate_api_key()

                session.add(user)
                session.flush()  # Get the user ID

                # Create default subscription
                subscription = Subscription(
                    user_id=user.id,
                    tier=tier,
                    status="active",
                    starts_at=datetime.now(),
                )
                session.add(subscription)

                logger.info(f"Created user {email} with {tier} subscription")

                # Expunge user from session so it can be used after session closes
                session.expunge(user)
                return user

        except Exception as e:
            logger.error(f"Error creating user {email}: {e}")
            return None

    def update_user(self, user_id: int, name: str | None = None, status: str | None = None) -> bool:
        """Update user information.

        Args:
            user_id: The user's ID
            name: New display name (optional)
            status: New status (optional)

        Returns:
            True if update successful, False otherwise
        """
        try:
            with self.session_scope() as session:
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    logger.warning(f"User {user_id} not found")
                    return False

                if name is not None:
                    user.name = name
                if status is not None:
                    if status not in ("active", "suspended", "deleted"):
                        logger.warning(f"Invalid status: {status}")
                        return False
                    user.status = status

                logger.info(f"Updated user {user_id}")
                return True

        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}")
            return False

    def delete_user(self, user_id: int, hard_delete: bool = False) -> bool:
        """Delete a user.

        Args:
            user_id: The user's ID
            hard_delete: If True, permanently delete. If False, soft delete (set status to 'deleted')

        Returns:
            True if deletion successful, False otherwise
        """
        try:
            with self.session_scope() as session:
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    logger.warning(f"User {user_id} not found")
                    return False

                if hard_delete:
                    session.delete(user)
                    logger.info(f"Hard deleted user {user_id}")
                else:
                    user.status = "deleted"
                    logger.info(f"Soft deleted user {user_id}")

                return True

        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}")
            return False

    def regenerate_api_key(self, user_id: int) -> str | None:
        """Regenerate API key for a user.

        Args:
            user_id: The user's ID

        Returns:
            New API key if successful, None otherwise
        """
        try:
            with self.session_scope() as session:
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    logger.warning(f"User {user_id} not found")
                    return None

                new_key = user.generate_api_key()
                logger.info(f"Regenerated API key for user {user_id}")
                return new_key

        except Exception as e:
            logger.error(f"Error regenerating API key for user {user_id}: {e}")
            return None

    def get_active_subscribers(self) -> list[dict[str, Any]]:
        """Get all users with active subscriptions.

        Returns:
            List of user dictionaries with subscription info
        """
        with self.readonly_session() as session:
            users = session.query(User).filter(User.status == "active").all()

            result = []
            for user in users:
                # Get active subscription
                subscription = (
                    session.query(Subscription)
                    .filter(Subscription.user_id == user.id, Subscription.status == "active")
                    .first()
                )

                if subscription and subscription.is_active():
                    user_dict = user.to_dict()
                    user_dict["subscription"] = subscription.to_dict()
                    result.append(user_dict)

            return result

    def list_users(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List users with optional filtering.

        Args:
            status: Filter by status (optional)
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of user dictionaries
        """
        with self.readonly_session() as session:
            query = session.query(User)

            if status:
                query = query.filter(User.status == status)

            users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

            result = []
            for user in users:
                user_dict = user.to_dict()

                # Get active subscription
                subscription = (
                    session.query(Subscription)
                    .filter(Subscription.user_id == user.id, Subscription.status == "active")
                    .first()
                )

                if subscription:
                    user_dict["subscription"] = subscription.to_dict()
                else:
                    user_dict["subscription"] = None

                result.append(user_dict)

            return result

    def get_user_count(self, status: str | None = None) -> int:
        """Get total count of users.

        Args:
            status: Filter by status (optional)

        Returns:
            Count of users
        """
        with self.readonly_session() as session:
            query = session.query(User)

            if status:
                query = query.filter(User.status == status)

            return query.count()

    def authenticate_by_api_key(self, api_key: str) -> dict[str, Any] | None:
        """Authenticate a user by API key.

        Args:
            api_key: The API key to authenticate

        Returns:
            User dictionary with subscription info if valid, None otherwise
        """
        user = self.get_user_by_api_key(api_key)
        if not user:
            return None

        with self.readonly_session() as session:
            # Get active subscription
            subscription = (
                session.query(Subscription)
                .filter(Subscription.user_id == user.id, Subscription.status == "active")
                .first()
            )

            if not subscription or not subscription.is_active():
                logger.warning(f"User {user.id} has no active subscription")
                return None

            result = user.to_dict()
            result["subscription"] = subscription.to_dict()
            return result

    def _ensure_free_tier_subscription(self, session: Session, user_id: int) -> Subscription:
        """Create an active free-tier subscription if the user has none.

        Called from the ``find_or_create_by_*`` native-login helpers so
        that a freshly-minted account has an active subscription the
        moment its api_key first flows through
        :meth:`authenticate_by_api_key` (which rejects users without one).

        Runs inside an existing session so the caller controls the
        transaction — the whole "create user + create subscription"
        sequence has to commit atomically or the row will be visible in
        one query and invisible to the next.
        """
        active: Subscription | None = (
            session.query(Subscription)
            .filter(Subscription.user_id == user_id, Subscription.status == "active")
            .first()
        )
        if active is not None:
            return active

        subscription = Subscription(
            user_id=user_id,
            tier="free",
            status="active",
            starts_at=datetime.now(),
        )
        session.add(subscription)
        session.flush()
        logger.info("Created free-tier subscription for user %s", user_id)
        return subscription

    def _finalise_native_user(self, session: Session, user: User) -> User:
        """Common tail for native-login helpers.

        Makes sure the row has an api_key and an active subscription,
        detaches from the session, returns the User.

        The ``session.refresh(user)`` before expunge is load-bearing:
        ``created_at`` / ``updated_at`` are server-side defaults, so
        after INSERT they exist in the DB but not in the mapped Python
        instance. Accessing them post-expunge would trigger a lazy
        refresh against a detached instance and raise
        :class:`DetachedInstanceError`. Refreshing while the object is
        still attached forces those columns into the instance's
        ``__dict__``, which then survives detachment intact.
        """
        if not user.api_key:
            user.generate_api_key()
        session.flush()
        self._ensure_free_tier_subscription(session, user.id)
        session.refresh(user)
        session.expunge(user)
        return user

    def find_or_create_by_wechat(
        self,
        openid: str,
        unionid: str | None = None,
        platform: str = "mp",
        name: str | None = None,
    ) -> User | None:
        """Look up (or create) the ``users`` row for a Weixin sign-in.

        Match order:

        1. ``wechat_unionid`` — same person across the mini-program and
           the iOS app when both AppIDs are under one Open Platform
           account.
        2. ``(wechat_openid, wechat_platform)`` — same person on the
           same platform.

        Creates a placeholder ``email`` (``wechat:{openid}@no-email``)
        when neither match yields a row, because ``users.email`` is
        ``NOT NULL UNIQUE``. Weixin doesn't hand back an email; the user
        can update it later via a settings endpoint.

        Returns ``None`` on database error rather than raising, matching
        the shape of :meth:`create_user`.
        """
        if not openid:
            return None
        if platform not in ("mp", "app"):
            logger.warning("Unknown wechat platform %r; defaulting to 'mp'", platform)
            platform = "mp"

        try:
            with self.session_scope() as session:
                user: User | None = None
                if unionid:
                    user = session.query(User).filter(User.wechat_unionid == unionid).first()
                if user is None:
                    user = (
                        session.query(User)
                        .filter(
                            User.wechat_openid == openid,
                            User.wechat_platform == platform,
                        )
                        .first()
                    )
                if user is None:
                    user = User(
                        # Placeholder email — unique because it includes the
                        # openid. The user can replace it via settings once
                        # they log in.
                        email=f"wechat:{openid}@no-email",
                        name=name,
                        status="active",
                        wechat_openid=openid,
                        wechat_unionid=unionid,
                        wechat_platform=platform,
                    )
                    session.add(user)
                    session.flush()
                    logger.info(
                        "Created wechat user (openid=%s, platform=%s, unionid=%s)",
                        openid,
                        platform,
                        unionid,
                    )
                else:
                    # Backfill unionid on subsequent logins if it wasn't
                    # available the first time (older mini-program AppID
                    # setups only expose it after Open Platform linkage).
                    if unionid and not user.wechat_unionid:
                        user.wechat_unionid = unionid
                    # Backfill openid+platform for a match that came via
                    # unionid on a different platform.
                    if not user.wechat_openid:
                        user.wechat_openid = openid
                        user.wechat_platform = platform

                return self._finalise_native_user(session, user)
        except Exception as e:
            logger.error("Error resolving wechat user openid=%s: %s", openid, e)
            return None

    def find_or_create_by_google_sub(
        self,
        sub: str,
        email: str,
        name: str | None = None,
    ) -> User | None:
        """Look up (or create) the ``users`` row for a Google sign-in.

        Match order:

        1. ``google_sub`` — Google's stable per-user identifier.
        2. ``email`` — pre-existing account created by an admin or by a
           previous session-based Google login; the ``sub`` gets attached
           on this pass.
        """
        if not sub:
            return None

        try:
            with self.session_scope() as session:
                user = session.query(User).filter(User.google_sub == sub).first()
                if user is None and email:
                    user = session.query(User).filter(User.email == email.lower()).first()
                    if user is not None:
                        user.google_sub = sub
                if user is None:
                    user = User(
                        email=(email or f"google:{sub}@no-email").lower(),
                        name=name,
                        status="active",
                        google_sub=sub,
                    )
                    session.add(user)
                    session.flush()
                    logger.info("Created google user sub=%s email=%s", sub, email)

                return self._finalise_native_user(session, user)
        except Exception as e:
            logger.error("Error resolving google user sub=%s: %s", sub, e)
            return None

    def find_or_create_by_apple_sub(
        self,
        sub: str,
        email: str | None = None,
        name: str | None = None,
    ) -> User | None:
        """Look up (or create) the ``users`` row for a Sign-in-with-Apple.

        Apple only exposes an email on the *first* login; subsequent logins
        deliver just the ``sub``. So the match is ``apple_sub``-first,
        with email as a fallback for pre-existing rows.

        Apple's ``sub`` is the primary key of the account, not the email:
        the same Apple ID can produce different emails via the "hide my
        email" relay, and the ``sub`` is what stays stable.
        """
        if not sub:
            return None

        try:
            with self.session_scope() as session:
                user = session.query(User).filter(User.apple_sub == sub).first()
                if user is None and email:
                    user = session.query(User).filter(User.email == email.lower()).first()
                    if user is not None:
                        user.apple_sub = sub
                if user is None:
                    user = User(
                        email=(email or f"apple:{sub}@no-email").lower(),
                        name=name,
                        status="active",
                        apple_sub=sub,
                    )
                    session.add(user)
                    session.flush()
                    logger.info("Created apple user sub=%s email=%s", sub, email)

                return self._finalise_native_user(session, user)
        except Exception as e:
            logger.error("Error resolving apple user sub=%s: %s", sub, e)
            return None

    def get_user_with_subscription(self, user_id: int) -> dict[str, Any] | None:
        """Get user with their subscription details.

        Args:
            user_id: The user's ID

        Returns:
            User dictionary with subscription info, None if not found
        """
        with self.readonly_session() as session:
            user = session.query(User).filter(User.id == user_id).first()
            if not user:
                return None

            user_dict = user.to_dict()

            # Get all subscriptions
            subscriptions = (
                session.query(Subscription)
                .filter(Subscription.user_id == user_id)
                .order_by(Subscription.created_at.desc())
                .all()
            )

            user_dict["subscriptions"] = [s.to_dict() for s in subscriptions]

            # Get active subscription
            active_sub = next((s for s in subscriptions if s.is_active()), None)
            user_dict["active_subscription"] = active_sub.to_dict() if active_sub else None

            return user_dict
