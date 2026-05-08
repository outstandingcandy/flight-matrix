"""
Notifications package - handles email sending and content preparation.

This package provides a clean separation between:
- Analysis (FlightAnalysisService - separate package)
- Media generation (MediaService - separate package)
- Content preparation (NotificationContentBuilder)
- Email sending (SMTPEmailNotifier, SESEmailNotifier)
- Orchestration (NotificationOrchestrator)

The NotificationContentBuilder accepts pre-generated analysis HTML,
keeping analysis logic completely separate from notifications.

The NotificationOrchestrator coordinates the full workflow:
1. Run AI analysis (via FlightAnalysisService)
2. Generate maps/images (via MediaService)
3. Build content (via NotificationContentBuilder)
4. Send email (via email notifier)
"""

from .base import BaseEmailNotifier, EmailAttachment, EmailContent
from .content import NotificationContentBuilder
from .factory import EmailNotifierFactory
from .orchestrator import NotificationOrchestrator, NotificationOrchestratorFactory
from .ses import SESEmailNotifier
from .smtp import SMTPEmailNotifier

__all__ = [
    "BaseEmailNotifier",
    "EmailAttachment",
    "EmailContent",
    "EmailNotifierFactory",
    "NotificationContentBuilder",
    "NotificationOrchestrator",
    "NotificationOrchestratorFactory",
    "SESEmailNotifier",
    "SMTPEmailNotifier",
]
