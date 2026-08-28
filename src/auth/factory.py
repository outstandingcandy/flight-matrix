"""Selects the authentication backend for the active deployment target.

Resolution follows the same convention as every other provider in the project
(see `config/deploy.yaml`): an explicit `auth.provider` in `config/auth.yaml`
wins, and an empty value resolves from `DEPLOY_TARGET` — aws to `cognito`, gcp
to `google`, local to `none`.

Two behaviours here are load-bearing and must not be "tidied up":

- `get_auth_provider()` returns **None** when the selected provider's required
  environment variables are incomplete, rather than raising.
  `src/auth/decorators.py` uses that None to decide between redirecting to
  login and rendering a 403, so an exception would turn a misconfigured
  deployment into a 500 on every page.
- An *unrecognised* `auth.provider` value does raise `ConfigurationError`.
  Silently falling back would let a typo authenticate users against the wrong
  identity provider with nobody noticing.

Reading the YAML is wrapped in a broad `except` so that a config problem can
never break the Cognito path, which needs no YAML at all: on `aws` a failed
load simply leaves the resolved provider at the target default.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from src.core.deploy_target import default_auth_provider, resolve_provider
from src.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from src.auth.apple_auth import AppleAuth
    from src.auth.base import OIDCProvider
    from src.auth.wechat_auth import WechatAuth
    from src.utils.yaml_config import YAMLConfig

logger = logging.getLogger(__name__)

COGNITO = "cognito"
GOOGLE = "google"
NONE = "none"

SUPPORTED_PROVIDERS = (COGNITO, GOOGLE, NONE)

# Where each provider's group membership comes from, reported by /auth/debug.
_GROUPS_SOURCE = {
    COGNITO: "claims",  # cognito:groups in the ID token
    GOOGLE: "config",  # auth.google.groups in config/auth.yaml
    NONE: "env",  # LOCAL_DEV_GROUPS, via decorators._get_mock_user()
}

_yaml_config: YAMLConfig | None = None
_google_auth: OIDCProvider | None = None
_apple_auth: AppleAuth | None = None
_wechat_auth: WechatAuth | None = None


def _get_yaml_config() -> YAMLConfig | None:
    """Load and cache the project config.

    Returns:
        A `YAMLConfig`, or None when it cannot be loaded.
    """
    global _yaml_config

    if _yaml_config is not None:
        return _yaml_config

    try:
        from src.utils.yaml_config import YAMLConfig

        _yaml_config = YAMLConfig(os.environ.get("CONFIG_PATH", "config/config.yaml"))
        return _yaml_config
    except Exception as e:
        logger.warning(f"Could not load config for auth resolution, using target defaults: {e}")
        return None


def resolve_auth_provider_name() -> str:
    """Resolve which authentication provider this deployment uses.

    Returns:
        One of `cognito`, `google` or `none`.

    Raises:
        ConfigurationError: If `auth.provider` holds an unrecognised value.
    """
    configured = ""
    yaml_config = _get_yaml_config()
    if yaml_config is not None:
        configured = yaml_config.get("auth.provider", "") or ""

    name = resolve_provider(configured, default_auth_provider())

    if name not in SUPPORTED_PROVIDERS:
        raise ConfigurationError(
            f"Invalid auth.provider={name!r}. Supported values: {', '.join(SUPPORTED_PROVIDERS)}"
        )

    return name


def groups_source() -> str:
    """Describe where the current provider gets group membership from.

    Returns:
        `claims`, `config` or `env` — surfaced by `/auth/debug` so a three-way
        authorisation problem can be diagnosed without reading code.
    """
    return _GROUPS_SOURCE[resolve_auth_provider_name()]


def _build_google_auth() -> OIDCProvider | None:
    """Build the Google provider from `config/auth.yaml`.

    Returns:
        A `GoogleAuth` instance, or None when the required settings are absent.
    """
    global _google_auth

    if _google_auth is not None:
        return _google_auth

    yaml_config = _get_yaml_config()
    if yaml_config is None:
        logger.warning("Google auth requires config/auth.yaml, which could not be loaded")
        return None

    client_id = yaml_config.get("auth.google.client_id", "") or ""
    client_secret = yaml_config.get("auth.google.client_secret", "") or ""
    callback_url = yaml_config.get("auth.google.callback_url", "") or ""

    if not all([client_id, client_secret, callback_url]):
        logger.warning(
            "Google auth not configured. Set GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_CALLBACK_URL"
        )
        return None

    from src.auth.google_auth import GoogleAuth

    # Native-client audiences: iOS / Android sign-ins target their own
    # client_id (Google requires it), but the signing keys / issuers stay
    # the same. Adding them here lets `/api/auth/google/native` verify a
    # token minted for any registered platform without spinning up a
    # second GoogleAuth instance.
    additional_audiences = [
        aud
        for aud in (
            yaml_config.get("auth.google.ios_client_id", "") or "",
            yaml_config.get("auth.google.android_client_id", "") or "",
        )
        if aud
    ]

    _google_auth = GoogleAuth(
        client_id=client_id,
        client_secret=client_secret,
        callback_url=callback_url,
        logout_url=yaml_config.get("auth.google.logout_url", "") or "",
        allowed_domains=yaml_config.get("auth.google.allowed_domains", []) or [],
        group_map=yaml_config.get("auth.google.groups", {}) or {},
        additional_audiences=additional_audiences,
    )

    logger.info("Created Google OIDC auth provider")
    return _google_auth


def get_auth_provider() -> OIDCProvider | None:
    """Get the authentication provider for the active deployment target.

    Returns:
        A provider satisfying `OIDCProvider`, or None when authentication is
        disabled (`none`) or the selected provider is not fully configured.

    Raises:
        ConfigurationError: If `auth.provider` holds an unrecognised value.
    """
    name = resolve_auth_provider_name()

    if name == COGNITO:
        from src.auth.cognito_auth import get_cognito_auth

        return get_cognito_auth()

    if name == GOOGLE:
        return _build_google_auth()

    return None


def get_google_auth() -> OIDCProvider | None:
    """Return the Google provider for native-app token verification.

    Distinct from :func:`get_auth_provider` because the native login
    endpoints — ``/api/auth/google/native`` — must accept Google tokens
    even on a deployment whose *primary* browser-side provider is
    Cognito (or ``none``). This just calls :func:`_build_google_auth`
    unconditionally; the same cached instance is reused if
    :func:`get_auth_provider` also happened to be Google.
    """
    return _build_google_auth()


def get_apple_auth() -> AppleAuth | None:
    """Return the Sign-in-with-Apple verifier, or ``None`` when unconfigured.

    Apple is only ever a native-mobile provider: there is no browser
    hosted-UI flow that goes through the server, so it's never returned
    by :func:`get_auth_provider`. The iOS SDK owns the whole login flow
    and hands the app an ``identityToken`` which the client POSTs to
    ``/api/auth/apple/native`` — this factory returns the verifier that
    endpoint uses.
    """
    global _apple_auth
    if _apple_auth is not None:
        return _apple_auth

    yaml_config = _get_yaml_config()
    if yaml_config is None:
        logger.warning("Apple auth requires config/auth.yaml, which could not be loaded")
        return None

    bundle_id = yaml_config.get("auth.apple.bundle_id", "") or ""
    if not bundle_id:
        logger.info("Apple auth not configured (APPLE_BUNDLE_ID unset)")
        return None

    additional = [b for b in (yaml_config.get("auth.apple.additional_bundle_ids", []) or []) if b]

    from src.auth.apple_auth import AppleAuth

    _apple_auth = AppleAuth(client_id=bundle_id, additional_audiences=additional)
    logger.info("Created Apple auth verifier (aud=%s + %d extras)", bundle_id, len(additional))
    return _apple_auth


def get_wechat_auth() -> WechatAuth | None:
    """Return the Weixin code exchanger, or ``None`` when no AppID is configured.

    Returns the same instance for both ``mp`` (mini-program) and ``app``
    (iOS) platforms — the class internally picks the right endpoint /
    AppID from :meth:`WechatAuth.code_to_session`. If neither platform
    has an AppID configured, returns ``None`` so the endpoints can 503
    with a clear message.
    """
    global _wechat_auth
    if _wechat_auth is not None:
        return _wechat_auth

    yaml_config = _get_yaml_config()
    if yaml_config is None:
        logger.warning("Wechat auth requires config/auth.yaml, which could not be loaded")
        return None

    mp_appid = yaml_config.get("auth.wechat.mp_appid", "") or ""
    mp_secret = yaml_config.get("auth.wechat.mp_appsecret", "") or ""
    app_appid = yaml_config.get("auth.wechat.app_appid", "") or ""
    app_secret = yaml_config.get("auth.wechat.app_appsecret", "") or ""

    if not any([mp_appid and mp_secret, app_appid and app_secret]):
        logger.info("Wechat auth not configured (no AppID / AppSecret pair)")
        return None

    from src.auth.wechat_auth import WechatAuth

    _wechat_auth = WechatAuth(
        mp_appid=mp_appid,
        mp_appsecret=mp_secret,
        app_appid=app_appid,
        app_appsecret=app_secret,
    )
    logger.info(
        "Created Wechat auth (mp=%s, app=%s)",
        "on" if mp_appid else "off",
        "on" if app_appid else "off",
    )
    return _wechat_auth


def reset_auth_provider() -> None:
    """Drop the cached provider and config. Intended for tests."""
    global _yaml_config, _google_auth, _apple_auth, _wechat_auth

    _yaml_config = None
    _google_auth = None
    _apple_auth = None
    _wechat_auth = None


def verify_token(token: str) -> dict | None:
    """Verify a JWT using the active provider.

    Args:
        token: Encoded JWT.

    Returns:
        Decoded claims if valid, None otherwise.
    """
    auth = get_auth_provider()
    if not auth:
        return None
    return auth.verify_token(token)


def get_user_from_token(token: str) -> dict | None:
    """Get user info from an ID token using the active provider.

    Args:
        token: Encoded ID token.

    Returns:
        User info dict, or None if invalid.
    """
    auth = get_auth_provider()
    if not auth:
        return None
    return auth.get_user_from_token(token)


def exchange_code_for_tokens(code: str) -> dict | None:
    """Exchange an authorization code for tokens using the active provider.

    Args:
        code: Authorization code from the OAuth2 callback.

    Returns:
        Token response dict, or None on failure.
    """
    auth = get_auth_provider()
    if not auth:
        return None
    return auth.exchange_code_for_tokens(code)
