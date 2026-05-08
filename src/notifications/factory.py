"""
Email notifier factory.

Creates email notifier instances based on configuration.
The new notifiers only handle sending - content preparation is separate.
"""

import logging
from typing import TYPE_CHECKING

from .base import BaseEmailNotifier
from .ses import SESEmailNotifier
from .smtp import SMTPEmailNotifier

if TYPE_CHECKING:
    from src.core.config import YAMLConfig

logger = logging.getLogger("notifications.factory")


class EmailNotifierFactory:
    """Factory for creating email notifier instances.

    Creates the appropriate notifier (SMTP or SES) based on configuration.
    The created notifiers only handle email sending, not content generation.
    """

    @staticmethod
    def create(yaml_config: "YAMLConfig") -> BaseEmailNotifier:
        """Create email notifier from configuration.

        Args:
            yaml_config: YAML configuration manager

        Returns:
            Configured email notifier instance

        Raises:
            ValueError: If provider is not supported
        """
        email_config = yaml_config.get_email_config()
        provider = email_config.get("provider", "smtp")

        if provider == "aws_ses":
            return EmailNotifierFactory._create_ses_notifier(email_config)
        elif provider == "smtp":
            return EmailNotifierFactory._create_smtp_notifier(email_config)
        else:
            raise ValueError(f"Unsupported email provider: {provider}")

    @staticmethod
    def _create_smtp_notifier(email_config: dict) -> SMTPEmailNotifier:
        """Create SMTP notifier from config."""
        smtp_config = email_config.get("smtp", {})

        notifier = SMTPEmailNotifier(
            smtp_server=smtp_config.get("server"),
            port=smtp_config.get("port", 587),
            sender_email=smtp_config.get("sender"),
            password=smtp_config.get("password"),
        )

        logger.info("Created SMTP email notifier")
        return notifier

    @staticmethod
    def _create_ses_notifier(email_config: dict) -> SESEmailNotifier:
        """Create AWS SES notifier from config."""
        aws_config = email_config.get("aws_ses", {})

        notifier = SESEmailNotifier(
            aws_region=aws_config.get("region", "us-east-1"),
            sender_email=aws_config.get("sender"),
            aws_access_key_id=aws_config.get("access_key_id"),
            aws_secret_access_key=aws_config.get("secret_access_key"),
        )

        logger.info("Created AWS SES email notifier")
        return notifier

    @staticmethod
    def create_from_dict(config: dict) -> BaseEmailNotifier:
        """Create notifier directly from config dictionary.

        Useful for testing or when YAMLConfig is not available.

        Args:
            config: Configuration dictionary with 'provider' and provider-specific settings

        Returns:
            Configured email notifier instance
        """
        provider = config.get("provider", "smtp")

        if provider == "aws_ses":
            return SESEmailNotifier(
                aws_region=config.get("region", "us-east-1"),
                sender_email=config.get("sender"),
                aws_access_key_id=config.get("access_key_id"),
                aws_secret_access_key=config.get("secret_access_key"),
            )
        else:
            return SMTPEmailNotifier(
                smtp_server=config.get("server"),
                port=config.get("port", 587),
                sender_email=config.get("sender"),
                password=config.get("password"),
            )
