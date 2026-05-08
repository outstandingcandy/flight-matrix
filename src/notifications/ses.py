"""
AWS SES email notifier - sends emails via Amazon SES.

This module provides AWS SES-based email sending functionality.
It ONLY handles sending prepared content, not content generation.
"""

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .base import BaseEmailNotifier, EmailContent

logger = logging.getLogger("notifications.ses")


class SESEmailNotifier(BaseEmailNotifier):
    """AWS SES-based email notifier.

    Sends emails using Amazon Simple Email Service.
    Only responsible for sending - content must be prepared beforehand.
    """

    def __init__(
        self,
        aws_region: str,
        sender_email: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        """Initialize AWS SES notifier.

        Args:
            aws_region: AWS region (e.g., 'us-east-1')
            sender_email: Verified sender email address
            aws_access_key_id: AWS access key (optional, uses default credentials if not provided)
            aws_secret_access_key: AWS secret key (optional)
        """
        self.aws_region = aws_region
        self.sender_email = sender_email

        # Initialize boto3 client
        try:
            import boto3

            if aws_access_key_id and aws_secret_access_key:
                self.ses_client = boto3.client(
                    "ses",
                    region_name=aws_region,
                    aws_access_key_id=aws_access_key_id,
                    aws_secret_access_key=aws_secret_access_key,
                )
            else:
                # Use default credentials (IAM role, environment, etc.)
                self.ses_client = boto3.client("ses", region_name=aws_region)

            logger.info(f"AWS SES notifier initialized (region: {aws_region})")

        except ImportError:
            logger.error("boto3 not installed. Install with: pip install boto3")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize AWS SES client: {e}")
            raise

    def send(self, recipients: list[str], content: EmailContent) -> bool:
        """Send email via AWS SES.

        Args:
            recipients: List of recipient email addresses
            content: Prepared email content

        Returns:
            True if sent successfully, False otherwise
        """
        if not recipients:
            logger.warning("No recipients specified")
            return False

        try:
            # Build MIME message
            message = self._build_message(recipients, content)

            # Send via SES
            response = self.ses_client.send_raw_email(
                Source=self.sender_email,
                Destinations=recipients,
                RawMessage={"Data": message.as_string()},
            )

            message_id = response.get("MessageId", "unknown")
            logger.info(
                f"Email sent via SES (MessageId: {message_id}) to {len(recipients)} recipients"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to send email via SES: {e}")
            return False

    def test_connection(self) -> bool:
        """Test AWS SES connection.

        Returns:
            True if connection successful
        """
        try:
            # Get send quota to verify credentials
            response = self.ses_client.get_send_quota()
            logger.info(
                f"SES connection OK. Daily quota: {response.get('Max24HourSend', 'unknown')}"
            )
            return True
        except Exception as e:
            logger.error(f"SES connection test failed: {e}")
            return False

    def _build_message(self, recipients: list[str], content: EmailContent) -> MIMEMultipart:
        """Build MIME message from content.

        Args:
            recipients: List of recipients
            content: Email content

        Returns:
            MIMEMultipart message ready to send
        """
        # Structure: related -> alternative (text/html) + images
        message = MIMEMultipart("related")
        message["Subject"] = content.subject
        message["From"] = self.sender_email
        message["To"] = ", ".join(recipients)

        # Create alternative part for text/html
        alternative_part = MIMEMultipart("alternative")

        # Add text and HTML parts
        text_part = MIMEText(content.text_body, "plain", "utf-8")
        html_part = MIMEText(content.html_body, "html", "utf-8")
        alternative_part.attach(text_part)
        alternative_part.attach(html_part)
        message.attach(alternative_part)

        # Attach images
        for attachment in content.attachments:
            try:
                image_mime = self._create_mime_image(attachment)
                message.attach(image_mime)
                logger.debug(f"Attached image: {attachment.filename}")
            except Exception as e:
                logger.warning(f"Failed to attach {attachment.filename}: {e}")

        return message

    def get_send_statistics(self) -> dict:
        """Get SES sending statistics.

        Returns:
            Dictionary with send statistics
        """
        try:
            quota = self.ses_client.get_send_quota()
            stats = self.ses_client.get_send_statistics()

            return {
                "max_24_hour_send": quota.get("Max24HourSend"),
                "max_send_rate": quota.get("MaxSendRate"),
                "sent_last_24_hours": quota.get("SentLast24Hours"),
                "send_data_points": len(stats.get("SendDataPoints", [])),
            }
        except Exception as e:
            logger.error(f"Failed to get SES statistics: {e}")
            return {}
