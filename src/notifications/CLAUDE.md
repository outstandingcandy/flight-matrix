# src/notifications/

Email notification system supporting AWS SES and SMTP.

## Architecture

```
NotificationOrchestrator
    ├── FlightAnalysisService (AI analysis)
    ├── MediaService (maps, images)
    ├── NotificationContentBuilder (HTML/text)
    └── BaseEmailNotifier (SES or SMTP)
```

## Components

| File | Purpose |
|------|---------|
| `orchestrator.py` | `NotificationOrchestrator` - coordinates the full notification workflow |
| `content.py` | `NotificationContentBuilder` - builds HTML/text email content |
| `base.py` | `BaseEmailNotifier` - abstract email sender interface |
| `ses.py` | `SESEmailNotifier` - AWS SES implementation |
| `smtp.py` | `SMTPEmailNotifier` - SMTP implementation |
| `factory.py` | Factory function to create notifier from config |

## Workflow

1. `NotificationOrchestrator.send_notification()` is called with aircraft data
2. AI analysis runs (if enabled)
3. Maps generated, images retrieved (if enabled)
4. `NotificationContentBuilder` creates HTML email
5. `BaseEmailNotifier` sends via configured provider

## Usage

```python
from src.notifications.factory import create_notifier
from src.notifications.orchestrator import NotificationOrchestrator

notifier = create_notifier(config)
orchestrator = NotificationOrchestrator(
    notifier=notifier, analysis_service=analysis_service, media_service=media_service
)

orchestrator.send_notification(
    recipients=["user@example.com"], aircraft_data=aircraft_list, subject="Flight Alert"
)
```

## Configuration

```yaml
email:
  provider: "aws_ses"  # or "smtp"
  aws_ses:
    region: "us-east-1"
    sender: "alerts@example.com"
  features:
    enable_maps: true
    enable_aircraft_images: true
    enable_flight_analysis: true
```
