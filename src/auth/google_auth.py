"""Google OIDC authentication for the `gcp` deployment target.

Structurally this mirrors `src/auth/cognito_auth.py`: same method surface,
same three-tier JWKS lookup (memory cache -> offline environment variable ->
network fetch), same RS256 verification, and the same `get_user_from_token()`
field set. The endpoints are constants rather than derived from a user-pool
ID, and three things differ from Cognito in ways that matter:

1. The token endpoint takes `client_id` / `client_secret` in the POST **body**,
   not in an HTTP Basic header.
2. `get_login_url()` must send `access_type=offline` and `prompt=consent`.
   Without them Google issues no refresh token and the refresh fallback in
   `src/auth/decorators.py` stops working silently.
3. Google ID tokens carry no group claim (Cognito supplies `cognito:groups`),
   so group membership comes from `config/auth.yaml` — see `_groups_for_email`.

There is also no `token_use` claim to validate and no RP-initiated logout
endpoint, so `get_logout_url()` returns an empty string unless a logout
redirect was configured explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlencode

import requests
from jose import JWTError, jwk, jwt

logger = logging.getLogger(__name__)

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Google mints tokens with either form of the issuer, so both are accepted.
ISSUERS = ("https://accounts.google.com", "accounts.google.com")

SCOPES = "openid email profile"

# Static JWKS cache — mirrors the COGNITO_JWKS mechanism so that a VM or
# container with no egress to googleapis.com can still verify tokens.
_jwks_cache: dict | None = None


class GoogleAuth:
    """Google Sign-In (OIDC) authentication handler."""

    def __init__(
        self,
        client_id: str,
        client_secret: str | None,
        callback_url: str,
        logout_url: str = "",
        allowed_domains: list[str] | None = None,
        group_map: dict[str, list[str]] | None = None,
        additional_audiences: list[str] | None = None,
    ):
        """Initialize the Google auth handler.

        Args:
            client_id: OAuth 2.0 Web application client ID. Used both for the
                web (browser) hosted-UI flow and as the default audience when
                verifying tokens.
            client_secret: OAuth 2.0 client secret.
            callback_url: Redirect URI registered in the Google Cloud console.
            logout_url: Optional post-logout redirect. Google has no
                RP-initiated logout, so this is only used if the deployment
                wants somewhere other than the landing page.
            allowed_domains: Email domains permitted to sign in. Empty or None
                accepts any verified Google account.
            group_map: Group name -> list of member emails, loaded from
                `config/auth.yaml`.
            additional_audiences: Extra client_ids accepted as valid `aud`
                claims when verifying tokens. This is how iOS / Android /
                other-platform native sign-ins reuse this provider — each
                native platform has its own client_id (Google requires it),
                but the underlying signing keys and issuers are the same.
                Empty or None keeps the classic single-audience behaviour.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.logout_url = logout_url

        self.allowed_domains = [d.strip().lower() for d in (allowed_domains or []) if d.strip()]

        # jose's `audience=` accepts either a single string or a list — we
        # always pass a list here so a caller adding an iOS/Android client_id
        # doesn't have to touch verify_token.
        extras = [a.strip() for a in (additional_audiences or []) if a and a.strip()]
        self.accepted_audiences: list[str] = [client_id, *extras]

        # Normalise the group map once so every lookup is a cheap set test.
        self.group_map: dict[str, set[str]] = {
            group: {email.strip().lower() for email in emails if email and email.strip()}
            for group, emails in (group_map or {}).items()
        }

        self.jwks_url = JWKS_URL
        self.token_endpoint = TOKEN_ENDPOINT

    def get_login_url(self, state: str | None = None) -> str:
        """Get the Google consent-screen URL.

        `access_type=offline` and `prompt=consent` are mandatory: Google only
        returns a refresh token when both are present, and re-consenting is the
        only way to recover one after it has been issued once.

        Args:
            state: Optional state parameter for CSRF protection.

        Returns:
            Login URL string.
        """
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": SCOPES,
            "redirect_uri": self.callback_url,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if state:
            params["state"] = state

        return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"

    def get_logout_url(self) -> str:
        """Get the post-logout redirect target.

        Google exposes no RP-initiated logout endpoint, so signing out is
        purely local: the caller clears the Flask session. An empty string
        tells the caller to fall back to the landing page.

        Returns:
            The configured logout URL, or an empty string.
        """
        return self.logout_url

    def get_jwks(self) -> dict:
        """Get Google's JWKS for offline JWT verification.

        Loaded in priority order:
        1. In-memory cache.
        2. `GOOGLE_JWKS` environment variable (JSON string).
        3. Network fetch from `JWKS_URL`.

        Returns:
            JWKS dictionary with a 'keys' array.

        Raises:
            RuntimeError: If JWKS cannot be loaded from any source.
        """
        global _jwks_cache

        if _jwks_cache is not None:
            return _jwks_cache

        jwks_env = os.environ.get("GOOGLE_JWKS")
        if jwks_env:
            try:
                _jwks_cache = json.loads(jwks_env)
                logger.info("Loaded JWKS from GOOGLE_JWKS environment variable (offline mode)")
                return _jwks_cache
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse GOOGLE_JWKS: {e}")

        try:
            logger.warning(
                "GOOGLE_JWKS not set, fetching from network. "
                "Set GOOGLE_JWKS env var for offline JWT verification."
            )
            response = requests.get(self.jwks_url, timeout=10)
            response.raise_for_status()
            _jwks_cache = response.json()
            logger.info("Fetched JWKS from Google (network)")
            return _jwks_cache

        except Exception as e:
            logger.error(f"Failed to fetch JWKS from network: {e}")
            raise RuntimeError(
                f"Cannot load JWKS. Set GOOGLE_JWKS env var or ensure network access. Error: {e}"
            ) from e

    def get_signing_key(self, token: str) -> dict | None:
        """Get the signing key for a JWT.

        Args:
            token: JWT token string.

        Returns:
            JWK dictionary for the signing key, or None if not found.
        """
        try:
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            if not kid:
                logger.error("Token has no 'kid' in header")
                return None

            keys: list[dict] = self.get_jwks().get("keys", [])
            for key in keys:
                if key.get("kid") == kid:
                    return key

            logger.error(f"Signing key not found for kid: {kid}")
            return None

        except Exception as e:
            logger.error(f"Error getting signing key: {e}")
            return None

    def verify_token(self, token: str) -> dict | None:
        """Verify a Google-issued JWT.

        Args:
            token: JWT token string (id_token).

        Returns:
            Decoded token claims if valid, None otherwise.
        """
        try:
            signing_key = self.get_signing_key(token)
            if not signing_key:
                return None

            public_key = jwk.construct(signing_key)

            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                # Accept any registered audience — the web client's id plus
                # any additional iOS / Android / Android-TV client_ids from
                # `additional_audiences`. jose treats a list as OR-of-audiences.
                audience=self.accepted_audiences,
                issuer=ISSUERS,
                options={
                    "verify_at_hash": False,  # No access_token available here
                },
            )

            # Explicit expiry check, matching the Cognito handler: jose already
            # validates `exp`, but a leeway change upstream must not silently
            # start accepting stale tokens.
            if claims.get("exp", 0) < time.time():
                logger.error("Token has expired")
                return None

            return claims

        except JWTError as e:
            logger.error(f"JWT verification failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            return None

    def is_domain_allowed(self, email: str | None) -> bool:
        """Check an email against the configured domain allow-list.

        Args:
            email: Email address from the ID token.

        Returns:
            True when `allowed_domains` is empty, or when the address belongs
            to one of the listed domains.
        """
        if not self.allowed_domains:
            return True
        if not email or "@" not in email:
            return False
        return email.rsplit("@", 1)[1].lower() in self.allowed_domains

    def _groups_for_email(self, email: str | None) -> list[str]:
        """Look up an address in the `config/auth.yaml` group map.

        Args:
            email: Email address from the ID token.

        Returns:
            Group names the address belongs to, in config order. Empty when the
            address is listed nowhere — such a user authenticates but is denied
            by the group decorators, which is the intended behaviour.
        """
        if not email:
            return []

        normalised = email.strip().lower()
        return [group for group, members in self.group_map.items() if normalised in members]

    def get_user_from_token(self, token: str) -> dict | None:
        """Extract user information from a verified ID token.

        The returned field set matches `CognitoAuth.get_user_from_token()`
        exactly — `src/auth/decorators.py` depends on it.

        Args:
            token: JWT token string (id_token).

        Returns:
            User info dict, or None when the token is invalid, the email is
            unverified, or the domain is not allowed.
        """
        claims = self.verify_token(token)
        if not claims:
            return None

        email = claims.get("email")

        if not claims.get("email_verified", False):
            logger.warning("Rejecting Google account with unverified email: %s", email)
            return None

        if not self.is_domain_allowed(email):
            logger.warning("Rejecting Google account outside allowed_domains: %s", email)
            return None

        groups = self._groups_for_email(email)
        logger.debug("token_groups user=%s groups=%s", email, groups)

        return {
            "sub": claims.get("sub"),
            "email": email,
            "email_verified": claims.get("email_verified", False),
            "name": claims.get("name") or email,
            "role": "admin" if "admins" in groups else "user",
            "groups": groups,
        }

    def exchange_code_for_tokens(self, code: str) -> dict | None:
        """Exchange an authorization code for tokens.

        Google expects the client credentials in the request body; sending them
        as an HTTP Basic header (which is what Cognito wants) fails with
        `invalid_client`.

        Args:
            code: Authorization code from the OAuth2 callback.

        Returns:
            Token response dict with id_token, access_token and refresh_token,
            or None on failure.
        """
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.callback_url,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret

        try:
            response = requests.post(
                self.token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )

            if response.status_code != 200:
                logger.error(f"Token exchange failed: {response.status_code} - {response.text}")
                return None

            tokens: dict = response.json()
            if not tokens.get("refresh_token"):
                logger.warning(
                    "Google returned no refresh_token. Session refresh will not work; "
                    "confirm access_type=offline and prompt=consent reached the consent screen."
                )

            logger.info("Successfully exchanged code for tokens")
            return tokens

        except requests.RequestException as e:
            logger.error(f"Token exchange error: {e}")
            return None
        except ValueError as e:
            logger.error(f"Token exchange returned invalid JSON: {e}")
            return None

    def refresh_tokens(self, refresh_token: str) -> dict | None:
        """Refresh the ID and access tokens.

        Google's refresh response does not include a new refresh token; the
        caller in `src/auth/decorators.py` already handles that.

        Args:
            refresh_token: Refresh token string.

        Returns:
            New token response dict, or None on failure.
        """
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret

        try:
            response = requests.post(
                self.token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=5,
            )

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code} - {response.text}")
                return None

            tokens: dict = response.json()
            return tokens

        except requests.RequestException as e:
            logger.error(f"Token refresh error: {e}")
            return None
        except ValueError as e:
            logger.error(f"Token refresh returned invalid JSON: {e}")
            return None
