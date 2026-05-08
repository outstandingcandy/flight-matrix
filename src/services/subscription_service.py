"""
Subscription Management Service

Handles subscription management, feature access control,
and usage quota tracking for the multi-user subscription system.

All configuration is user-specific and stored directly in the subscription record.
No tier-based defaults are used - each user's settings are fully customizable.
"""

import logging
from datetime import date, datetime, timedelta

from src.data.models import Subscription, UserUsage
from src.services.base import BaseService
from src.utils.database import DatabaseManager
from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger("subscription_service")


# Default values for new subscriptions (no tier-based system)
DEFAULT_SUBSCRIPTION_CONFIG = {
    "enable_maps": True,
    "enable_aircraft_images": True,
    "cooldown_hours": 12.0,
    "daily_report_limit": -1,  # -1 means unlimited
    "monthly_report_limit": -1,
    "max_filters": -1,  # -1 means unlimited
}


class SubscriptionService(BaseService):
    """Subscription management service for multi-user system.

    Handles subscription feature access control and usage quota management.
    All configuration is user-specific - no tier-based defaults are used.

    Attributes:
        db: Database manager instance
        config: YAML configuration instance
    """

    def __init__(self, db: DatabaseManager, config: YAMLConfig | None = None):
        """Initialize the subscription service.

        Args:
            db: Database manager instance
            config: Optional YAML configuration
        """
        super().__init__(db)
        self.config = config

    def get_subscription(self, subscription_id: int) -> Subscription | None:
        """Get a subscription by ID.

        Args:
            subscription_id: The subscription ID

        Returns:
            Subscription object if found, None otherwise
        """
        with self.readonly_session() as session:
            return session.query(Subscription).filter(Subscription.id == subscription_id).first()

    def get_user_active_subscription(self, user_id: int) -> Subscription | None:
        """Get a user's active subscription.

        Args:
            user_id: The user's ID

        Returns:
            Active Subscription object if found, None otherwise
        """
        with self.readonly_session() as session:
            subscription = (
                session.query(Subscription)
                .filter(Subscription.user_id == user_id, Subscription.status == "active")
                .first()
            )

            if subscription and subscription.is_active():
                return subscription
            return None

    def create_subscription(
        self,
        user_id: int,
        tier: str = "basic",
        expires_at: datetime | None = None,
        **feature_overrides,
    ) -> Subscription | None:
        """Create a new subscription for a user.

        Args:
            user_id: The user's ID
            tier: Subscription tier (kept for labeling purposes)
            expires_at: Optional expiration date
            **feature_overrides: Optional feature flag overrides

        Returns:
            Created Subscription object, or None if creation failed
        """
        try:
            with self.session_scope() as session:
                # Use default config, allowing overrides
                defaults = DEFAULT_SUBSCRIPTION_CONFIG

                subscription = Subscription(
                    user_id=user_id,
                    tier=tier,
                    status="active",
                    starts_at=datetime.now(),
                    expires_at=expires_at,
                    enable_maps=feature_overrides.get("enable_maps", defaults["enable_maps"]),
                    enable_aircraft_images=feature_overrides.get(
                        "enable_aircraft_images", defaults["enable_aircraft_images"]
                    ),
                    cooldown_hours=feature_overrides.get(
                        "cooldown_hours", defaults["cooldown_hours"]
                    ),
                    daily_report_limit=feature_overrides.get(
                        "daily_report_limit", defaults["daily_report_limit"]
                    ),
                    monthly_report_limit=feature_overrides.get(
                        "monthly_report_limit", defaults["monthly_report_limit"]
                    ),
                    max_filters=feature_overrides.get("max_filters", defaults["max_filters"]),
                )

                session.add(subscription)
                logger.info(f"Created subscription for user {user_id}")

                # Expunge before commit so it remains usable
                session.expunge(subscription)
                return subscription

        except Exception as e:
            logger.error(f"Error creating subscription for user {user_id}: {e}")
            return None

    def update_subscription(
        self,
        subscription_id: int,
        tier: str | None = None,
        status: str | None = None,
        expires_at: datetime | None = None,
        **feature_overrides,
    ) -> bool:
        """Update a subscription.

        Args:
            subscription_id: The subscription ID
            tier: New tier label (optional, no effect on features)
            status: New status (optional)
            expires_at: New expiration date (optional)
            **feature_overrides: Feature flag updates

        Returns:
            True if update successful, False otherwise
        """
        try:
            with self.session_scope() as session:
                subscription = (
                    session.query(Subscription).filter(Subscription.id == subscription_id).first()
                )

                if not subscription:
                    logger.warning(f"Subscription {subscription_id} not found")
                    return False

                # Update tier label (no longer affects features)
                if tier is not None:
                    subscription.tier = tier

                if status is not None:
                    subscription.status = status

                if expires_at is not None:
                    subscription.expires_at = expires_at

                # Apply feature overrides
                if "enable_maps" in feature_overrides:
                    subscription.enable_maps = feature_overrides["enable_maps"]
                if "enable_aircraft_images" in feature_overrides:
                    subscription.enable_aircraft_images = feature_overrides[
                        "enable_aircraft_images"
                    ]

                # Apply report configuration overrides
                if "cooldown_hours" in feature_overrides:
                    subscription.cooldown_hours = feature_overrides["cooldown_hours"]
                if "daily_report_limit" in feature_overrides:
                    subscription.daily_report_limit = feature_overrides["daily_report_limit"]
                if "monthly_report_limit" in feature_overrides:
                    subscription.monthly_report_limit = feature_overrides["monthly_report_limit"]
                if "max_filters" in feature_overrides:
                    subscription.max_filters = feature_overrides["max_filters"]

                logger.info(f"Updated subscription {subscription_id}")
                return True

        except Exception as e:
            logger.error(f"Error updating subscription {subscription_id}: {e}")
            return False

    def cancel_subscription(self, subscription_id: int) -> bool:
        """Cancel a subscription.

        Args:
            subscription_id: The subscription ID

        Returns:
            True if cancellation successful, False otherwise
        """
        return self.update_subscription(subscription_id, status="cancelled")

    def get_user_features(self, user_id: int) -> dict:
        """Get feature configuration for a user.

        All settings come directly from the user's subscription record.
        No tier-based defaults are applied.

        Args:
            user_id: The user's ID

        Returns:
            Dictionary of feature flags and settings
        """
        with self.readonly_session() as session:
            subscription = (
                session.query(Subscription)
                .filter(Subscription.user_id == user_id, Subscription.status == "active")
                .first()
            )

            if not subscription or not subscription.is_active():
                # Return default features for users without active subscription
                defaults = DEFAULT_SUBSCRIPTION_CONFIG
                return {
                    "enable_maps": defaults["enable_maps"],
                    "enable_aircraft_images": defaults["enable_aircraft_images"],
                    "cooldown_hours": defaults["cooldown_hours"],
                    "max_filters": defaults["max_filters"],
                    "daily_report_limit": defaults["daily_report_limit"],
                    "monthly_report_limit": defaults["monthly_report_limit"],
                    "tier": "none",
                }

            # All settings come directly from the subscription record
            return {
                "enable_maps": subscription.enable_maps,
                "enable_aircraft_images": subscription.enable_aircraft_images,
                "cooldown_hours": float(subscription.cooldown_hours)
                if subscription.cooldown_hours is not None
                else 12.0,
                "max_filters": subscription.max_filters
                if subscription.max_filters is not None
                else -1,
                "daily_report_limit": subscription.daily_report_limit
                if subscription.daily_report_limit is not None
                else -1,
                "monthly_report_limit": subscription.monthly_report_limit
                if subscription.monthly_report_limit is not None
                else -1,
                "tier": subscription.tier,
            }

    def get_user_cooldown_hours(self, user_id: int) -> float:
        """Get cooldown hours for a user based on their subscription.

        Args:
            user_id: The user's ID

        Returns:
            Cooldown hours for the user's subscription tier
        """
        features = self.get_user_features(user_id)
        return features.get("cooldown_hours", 24.0)

    # =========================================================================
    # Usage Quota Management
    # =========================================================================

    def get_or_create_usage(self, user_id: int, period_type: str = "monthly") -> UserUsage:
        """Get or create usage record for current period.

        Args:
            user_id: The user's ID
            period_type: Period type (daily or monthly)

        Returns:
            UserUsage object for the current period
        """
        today = date.today()

        if period_type == "daily":
            period_start = today
        else:  # monthly
            period_start = today.replace(day=1)

        # First try to get existing usage
        with self.readonly_session() as session:
            usage = (
                session.query(UserUsage)
                .filter(
                    UserUsage.user_id == user_id,
                    UserUsage.period_start == period_start,
                    UserUsage.period_type == period_type,
                )
                .first()
            )

            if usage:
                session.expunge(usage)
                return usage

        # Create new usage record if not exists
        try:
            with self.session_scope() as session:
                usage = UserUsage(
                    user_id=user_id,
                    period_start=period_start,
                    period_type=period_type,
                    reports_sent=0,
                    emails_sent=0,
                )
                session.add(usage)
                session.expunge(usage)
                return usage
        except Exception:
            # May have been created by another process, try to get it again
            with self.readonly_session() as session:
                usage = (
                    session.query(UserUsage)
                    .filter(
                        UserUsage.user_id == user_id,
                        UserUsage.period_start == period_start,
                        UserUsage.period_type == period_type,
                    )
                    .first()
                )
                if usage:
                    session.expunge(usage)
                    return usage
                # Should not happen, but create a default object
                return UserUsage(
                    user_id=user_id,
                    period_start=period_start,
                    period_type=period_type,
                    reports_sent=0,
                    emails_sent=0,
                )

    def check_quota(self, user_id: int, quota_type: str = "daily") -> bool:
        """Check if user has remaining quota.

        Args:
            user_id: The user's ID
            quota_type: Quota type (daily or monthly)

        Returns:
            True if user has remaining quota, False otherwise
        """
        features = self.get_user_features(user_id)

        if quota_type == "daily":
            limit = features.get("daily_report_limit", 10)
        else:
            limit = features.get("monthly_report_limit", 100)

        # -1 means unlimited
        if limit == -1:
            return True

        usage = self.get_or_create_usage(user_id, quota_type)
        return usage.reports_sent < limit

    def check_daily_quota(self, user_id: int) -> bool:
        """Check if user has remaining daily quota.

        Args:
            user_id: The user's ID

        Returns:
            True if user has remaining daily quota
        """
        return self.check_quota(user_id, "daily")

    def check_monthly_quota(self, user_id: int) -> bool:
        """Check if user has remaining monthly quota.

        Args:
            user_id: The user's ID

        Returns:
            True if user has remaining monthly quota
        """
        return self.check_quota(user_id, "monthly")

    def increment_usage(self, user_id: int, usage_type: str = "reports") -> bool:
        """Increment usage counter for a user.

        Args:
            user_id: The user's ID
            usage_type: Type of usage (reports, emails)

        Returns:
            True if increment successful, False otherwise
        """
        try:
            with self.session_scope() as session:
                # Update both daily and monthly usage
                for period_type in ["daily", "monthly"]:
                    today = date.today()

                    if period_type == "daily":
                        period_start = today
                    else:
                        period_start = today.replace(day=1)

                    usage = (
                        session.query(UserUsage)
                        .filter(
                            UserUsage.user_id == user_id,
                            UserUsage.period_start == period_start,
                            UserUsage.period_type == period_type,
                        )
                        .first()
                    )

                    if not usage:
                        usage = UserUsage(
                            user_id=user_id,
                            period_start=period_start,
                            period_type=period_type,
                        )
                        session.add(usage)

                    if usage_type == "reports":
                        usage.reports_sent = (usage.reports_sent or 0) + 1
                    elif usage_type == "emails":
                        usage.emails_sent = (usage.emails_sent or 0) + 1

                return True

        except Exception as e:
            logger.error(f"Error incrementing usage for user {user_id}: {e}")
            return False

    def get_usage_stats(self, user_id: int) -> dict:
        """Get usage statistics for a user.

        Args:
            user_id: The user's ID

        Returns:
            Dictionary with usage statistics
        """
        features = self.get_user_features(user_id)
        daily_usage = self.get_or_create_usage(user_id, "daily")
        monthly_usage = self.get_or_create_usage(user_id, "monthly")

        daily_limit = features.get("daily_report_limit", 10)
        monthly_limit = features.get("monthly_report_limit", 100)

        return {
            "daily": {
                "reports_sent": daily_usage.reports_sent,
                "limit": daily_limit,
                "remaining": daily_limit - daily_usage.reports_sent if daily_limit != -1 else -1,
                "unlimited": daily_limit == -1,
            },
            "monthly": {
                "reports_sent": monthly_usage.reports_sent,
                "emails_sent": monthly_usage.emails_sent,
                "limit": monthly_limit,
                "remaining": monthly_limit - monthly_usage.reports_sent
                if monthly_limit != -1
                else -1,
                "unlimited": monthly_limit == -1,
            },
            "tier": features.get("tier", "none"),
        }

    def cleanup_old_usage(self, retention_days: int = 90) -> int:
        """Clean up old usage records.

        Args:
            retention_days: Keep records newer than this many days

        Returns:
            Number of records deleted
        """
        try:
            with self.session_scope() as session:
                cutoff_date = date.today() - timedelta(days=retention_days)

                result = (
                    session.query(UserUsage).filter(UserUsage.period_start < cutoff_date).delete()
                )

                logger.info(f"Cleaned up {result} old usage records")
                return result

        except Exception as e:
            logger.error(f"Error cleaning up usage records: {e}")
            return 0
