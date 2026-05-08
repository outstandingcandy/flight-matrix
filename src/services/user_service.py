"""
User Management Service

Handles user CRUD operations, authentication via API key,
and user status management for the multi-user subscription system.
"""

import logging
from datetime import datetime

from src.data.models import Subscription, User
from src.services.base import BaseService

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

    def get_active_subscribers(self) -> list[dict]:
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
    ) -> list[dict]:
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

    def authenticate_by_api_key(self, api_key: str) -> dict | None:
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

    def get_user_with_subscription(self, user_id: int) -> dict | None:
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
