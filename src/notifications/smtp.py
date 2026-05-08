"""
SMTP email notifier - sends emails via SMTP.

This module provides SMTP-based email sending functionality.
It ONLY handles sending prepared content, not content generation.
"""

import logging
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .base import BaseEmailNotifier, EmailContent

logger = logging.getLogger("notifications.smtp")


class SMTPEmailNotifier(BaseEmailNotifier):
    """SMTP-based email notifier.

    Sends emails using SMTP protocol with TLS encryption.
    Only responsible for sending - content must be prepared beforehand.
    """

    def __init__(self, smtp_server: str, port: int, sender_email: str, password: str):
        """Initialize SMTP notifier.

        Args:
            smtp_server: SMTP server address (e.g., smtp.gmail.com)
            port: SMTP port (e.g., 587 for TLS)
            sender_email: Sender email address
            password: Email password or app password
        """
        self.smtp_server = smtp_server
        self.port = port
        self.sender_email = sender_email
        self.password = password

        logger.info(f"SMTP notifier initialized (server: {smtp_server}:{port})")

    def send(self, recipients: list[str], content: EmailContent) -> bool:
        """Send email via SMTP.

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

            # Send with retry
            return self._send_with_retry(recipients, message)

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    def test_connection(self) -> bool:
        """Test SMTP connection.

        Returns:
            True if connection successful
        """
        try:
            context = ssl.create_default_context()
            # Port 465 uses implicit SSL (SMTP_SSL)
            # Port 587 uses explicit TLS (SMTP + starttls)
            if self.port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.port, context=context, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_server, self.port, timeout=30)
                server.starttls(context=context)

            with server:
                server.login(self.sender_email, self.password)
            return True
        except Exception as e:
            logger.error(f"SMTP connection test failed: {e}")
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

    def _send_with_retry(
        self, recipients: list[str], message: MIMEMultipart, max_retries: int = 3
    ) -> bool:
        """Send message with retry logic.

        Args:
            recipients: List of recipients
            message: MIME message
            max_retries: Maximum retry attempts

        Returns:
            True if sent successfully
        """
        context = ssl.create_default_context()

        for attempt in range(1, max_retries + 1):
            try:
                # Port 465 uses implicit SSL (SMTP_SSL)
                # Port 587 uses explicit TLS (SMTP + starttls)
                if self.port == 465:
                    server = smtplib.SMTP_SSL(
                        self.smtp_server, self.port, context=context, timeout=60
                    )
                else:
                    server = smtplib.SMTP(self.smtp_server, self.port, timeout=60)
                    server.starttls(context=context)

                with server:
                    server.login(self.sender_email, self.password)

                    # Check message size
                    message_str = message.as_string()
                    message_size = len(message_str.encode("utf-8"))
                    logger.info(f"Email size: {message_size:,} bytes")

                    # Warn if too large
                    if message_size > 25 * 1024 * 1024:
                        logger.warning(f"Email may be too large ({message_size:,} bytes)")

                    server.sendmail(self.sender_email, recipients, message_str)

                logger.info(f"Email sent to {len(recipients)} recipients")
                return True

            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as e:
                logger.warning(f"SMTP error (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2**attempt)
                    continue
                raise

            except Exception as e:
                logger.error(f"SMTP send error: {e}")
                raise

        return False
