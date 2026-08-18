"""Deployment target resolution.

The application supports three deployment targets, selected by the
``DEPLOY_TARGET`` environment variable:

- ``aws``   — Lambda + API Gateway, Aurora, S3/CloudFront, Cognito, Bedrock, SES
- ``gcp``   — single GCE VM, local PostgreSQL, GCS, Google OIDC, Gemini, SMTP
- ``local`` — developer machine, SQLite, local filesystem, auth bypassed

``DEPLOY_TARGET`` is orthogonal to ``STAGE``: ``STAGE`` selects the
configuration tier and log level, while ``DEPLOY_TARGET`` selects *which
cloud implementation* each provider factory instantiates.

This module is the single source of truth for that mapping. Every provider
factory asks it rather than branching on the target itself — without that
discipline the three-way matrix drifts out of sync across packages.

Every provider config block follows the same convention: an empty
``provider`` field means "resolve from the deployment target", while an
explicit value overrides the resolution. The override is what makes it
possible to debug Gemini on a laptop or temporarily fall back to Bedrock on
GCP, so it is a supported path, not a leftover.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum

from src.core.exceptions import ConfigurationError

logger = logging.getLogger("core.deploy_target")

__all__ = [
    "DeployTarget",
    "current_target",
    "default_auth_provider",
    "default_email_provider",
    "default_llm_provider",
    "default_scaler_provider",
    "default_storage_provider",
    "resolve_provider",
]

ENV_VAR = "DEPLOY_TARGET"


class DeployTarget(StrEnum):
    """Supported deployment targets."""

    AWS = "aws"
    GCP = "gcp"
    LOCAL = "local"


def current_target() -> DeployTarget:
    """Return the active deployment target.

    Returns:
        The target named by ``DEPLOY_TARGET``, or :attr:`DeployTarget.LOCAL`
        when the variable is unset or empty.

    Raises:
        ConfigurationError: If ``DEPLOY_TARGET`` is set to an unknown value.
            This deliberately fails loudly instead of falling back to
            ``local``: a silent downgrade would let a production deployment
            pick the wrong providers with nothing in the logs to say so.
    """
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    if not raw:
        return DeployTarget.LOCAL

    try:
        return DeployTarget(raw)
    except ValueError:
        supported = ", ".join(t.value for t in DeployTarget)
        raise ConfigurationError(
            f"Invalid {ENV_VAR}={raw!r}. Supported targets: {supported}."
        ) from None


def resolve_provider(configured: str | None, default: str) -> str:
    """Apply the "empty means auto-resolve" convention.

    Args:
        configured: The ``provider`` value read from configuration. An empty
            string or ``None`` means the caller wants target-based resolution.
        default: The provider resolved from the deployment target.

    Returns:
        ``configured`` when it holds a non-empty value, otherwise ``default``.
    """
    if configured and configured.strip():
        explicit = configured.strip()
        if explicit != default:
            logger.info(
                "Provider explicitly configured as %r, overriding target default %r",
                explicit,
                default,
            )
        return explicit
    return default


def default_storage_provider() -> str:
    """Return the object-storage provider for the active target.

    Returns:
        One of ``"s3"``, ``"gcs"`` or ``"local"``.
    """
    target = current_target()
    if target is DeployTarget.AWS:
        return "s3"
    if target is DeployTarget.GCP:
        return "gcs"
    return "local"


def default_auth_provider() -> str:
    """Return the authentication provider for the active target.

    Returns:
        ``"cognito"`` on AWS, ``"google"`` on GCP, ``"none"`` locally (where
        authentication is bypassed via ``SKIP_AUTH``).
    """
    target = current_target()
    if target is DeployTarget.AWS:
        return "cognito"
    if target is DeployTarget.GCP:
        return "google"
    return "none"


def default_llm_provider() -> str:
    """Return the LLM provider for the active target.

    On ``local`` there is no single right answer, so a Gemini key in the
    environment decides it; see :func:`_pick_local_llm_provider`.

    Returns:
        ``"aws_bedrock"`` or ``"gemini"``.
    """
    target = current_target()
    if target is DeployTarget.AWS:
        return "aws_bedrock"
    if target is DeployTarget.GCP:
        return "gemini"
    return _pick_local_llm_provider()


def default_email_provider() -> str:
    """Return the email provider for the active target.

    Returns:
        ``"aws_ses"`` on AWS, ``"smtp"`` on GCP and locally (GCP has no
        native email service, and cross-cloud SES would require AWS
        credentials on the VM).
    """
    return "aws_ses" if current_target() is DeployTarget.AWS else "smtp"


def default_scaler_provider() -> str:
    """Return the scraper worker scaler for the active target.

    Returns:
        ``"asg"`` on AWS (EC2 Auto Scaling Group), ``"noop"`` on GCP and
        locally — the GCP deployment is a single VM, so there is nothing to
        scale and systemd handles worker restarts.
    """
    return "asg" if current_target() is DeployTarget.AWS else "noop"


def _pick_local_llm_provider() -> str:
    """Choose an LLM provider for local development based on available keys.

    Only providers the analysis call sites actually implement are candidates —
    ``gemini`` and ``aws_bedrock``. ``anthropic`` and ``openai`` remain valid as
    an *explicit* ``llm.provider`` override, but auto-selecting one would resolve
    to a provider :mod:`src.llm.factory` cannot build.

    Returns:
        ``"gemini"`` when ``GEMINI_API_KEY`` is set, otherwise ``"aws_bedrock"``
        — which is what local development used before the target switch existed,
        via whatever AWS credentials the developer already has.
    """
    if os.environ.get("GEMINI_API_KEY", "").strip():
        logger.debug("Local LLM provider resolved to 'gemini' via GEMINI_API_KEY")
        return "gemini"

    logger.debug("No GEMINI_API_KEY in the environment; local LLM provider is 'aws_bedrock'")
    return "aws_bedrock"
