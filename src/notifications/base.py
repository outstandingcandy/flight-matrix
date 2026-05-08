"""
Base email notifier - handles only email sending.

This module provides the abstract interface for email sending.
Email notifiers are responsible ONLY for sending emails,
NOT for content generation or analysis.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.mime.image import MIMEImage

logger = logging.getLogger("notifications.base")


@dataclass
class EmailAttachment:
    """Represents an email attachment.

    Attributes:
        data: Binary content of the attachment
        content_id: Content-ID for inline images (e.g., "map_image")
        filename: Display filename
        subtype: MIME subtype (e.g., "jpeg", "png")
        inline: Whether this is an inline image (uses cid: in HTML)
    """

    data: bytes
    content_id: str
    filename: str
    subtype: str = "jpeg"
    inline: bool = True


@dataclass
class EmailContent:
    """Prepared email content ready to be sent.

    This class holds all the content needed to send an email.
    Content should be prepared BEFORE creating this object.

    Attributes:
        subject: Email subject line
        html_body: HTML content of the email
        text_body: Plain text fallback content
        attachments: List of attachments (images, etc.)
    """

    subject: str
    html_body: str
    text_body: str
    attachments: list[EmailAttachment] = field(default_factory=list)


class BaseEmailNotifier(ABC):
    """Abstract base class for email sending.

    Email notifiers are responsible ONLY for:
    - Connecting to email service (SMTP, SES, etc.)
    - Sending prepared email content
    - Handling delivery errors

    Email notifiers are NOT responsible for:
    - Generating email content
    - Running AI analysis
    - Creating maps or images
    - Database operations

    This separation ensures clean architecture and testability.
    """

    @abstractmethod
    def send(self, recipients: list[str], content: EmailContent) -> bool:
        """Send an email with prepared content.

        Args:
            recipients: List of recipient email addresses
            content: Prepared email content (subject, body, attachments)

        Returns:
            True if email was sent successfully, False otherwise
        """
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        """Test the connection to the email service.

        Returns:
            True if connection is successful, False otherwise
        """
        pass

    def _get_image_subtype(self, file_path: str) -> str:
        """Get MIME subtype from file extension.

        Args:
            file_path: Path to the image file

        Returns:
            MIME subtype (e.g., 'jpeg', 'png')
        """
        ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else ""
        subtype_map = {
            "jpg": "jpeg",
            "jpeg": "jpeg",
            "png": "png",
            "gif": "gif",
            "webp": "webp",
            "bmp": "bmp",
        }
        return subtype_map.get(ext, "jpeg")

    def _create_mime_image(self, attachment: EmailAttachment) -> MIMEImage:
        """Create a MIMEImage from an EmailAttachment.

        Args:
            attachment: EmailAttachment object

        Returns:
            MIMEImage ready to attach to email
        """
        image_mime = MIMEImage(attachment.data, _subtype=attachment.subtype)
        image_mime.add_header("Content-ID", f"<{attachment.content_id}>")

        disposition = "inline" if attachment.inline else "attachment"
        image_mime.add_header("Content-Disposition", disposition, filename=attachment.filename)

        return image_mime

    def load_attachment_from_file(
        self, file_path: str, content_id: str, filename: str | None = None, inline: bool = True
    ) -> EmailAttachment | None:
        """Load an attachment from a file.

        Args:
            file_path: Path to the file
            content_id: Content-ID for the attachment
            filename: Display filename (defaults to basename of file_path)
            inline: Whether this is an inline image

        Returns:
            EmailAttachment object, or None if file cannot be read
        """
        try:
            with open(file_path, "rb") as f:
                data = f.read()

            if filename is None:
                import os

                filename = os.path.basename(file_path)

            subtype = self._get_image_subtype(file_path)

            return EmailAttachment(
                data=data, content_id=content_id, filename=filename, subtype=subtype, inline=inline
            )
        except Exception as e:
            logger.warning(f"Failed to load attachment from {file_path}: {e}")
            return None
