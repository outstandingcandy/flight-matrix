"""Sign in with Apple (native token verification only).

App Store Guideline 4.8 requires apps that offer any third-party
sign-in to also offer Sign in with Apple. This module is the server
side of that: an iOS client hands the app an ``identityToken`` (a JWT
signed by Apple), the app POSTs it to ``/api/auth/apple/native``, and
this class verifies it.

Structurally this mirrors :mod:`src.auth.google_auth`, but the shape is
smaller because Apple only ever gives us an ID token — there is no
browser-side hosted-UI flow the server has to know about, no
authorization-code exchange, and no refresh-token replay:

- ``get_login_url`` / ``exchange_code_for_tokens`` / ``refresh_tokens`` /
  ``get_logout_url`` all return empty / None — Apple's flows are
  entirely client-driven (the iOS SDK owns them) and the server sees
  the ``identityToken`` only.
- ``verify_token`` fetches ``https://appleid.apple.com/auth/keys``,
  looks the token's ``kid`` up, and verifies RS256 with
  ``iss=https://appleid.apple.com`` and
  ``aud=<Apple app / service ID>``.
- ``get_user_from_token`` returns the same field set as Cognito /
  Google — see :mod:`src.auth.CLAUDE.md`. Apple only exposes an
  ``email`` on the *first* sign-in; subsequent tokens carry just
  ``sub``. Callers who need the email must persist it themselves (see
  :meth:`UserService.find_or_create_by_apple_sub`).

Structurally satisfies :class:`src.auth.base.OIDCProvider`, but not by
inheritance — same duck-typed pattern as CognitoAuth / GoogleAuth.
"""

from __future__ import annotations

import logging
import time

import requests
from jose import JWTError, jwk, jwt

logger = logging.getLogger(__name__)

JWKS_URL = "https://appleid.apple.com/auth/keys"
ISSUER = "https://appleid.apple.com"

# Static cache for offline verification (mirrors COGNITO_JWKS /
# GOOGLE_JWKS). Runtime pinning of Apple's rotating keys is uncommon —
# the JWKS is public and cheap to fetch — but the hook is preserved for
# environments with no network egress.
_jwks_cache: dict | None = None


class AppleAuth:
    """Apple ID native-sign-in token verifier.

    Attributes:
        client_id: Apple Service ID or App Bundle ID — this is what the
            iOS SDK sets as the JWT's ``aud`` claim. Which one depends
            on how the app is registered in Apple's developer portal;
            the value must match verbatim.
        client_secret: Not used. Present for OIDCProvider structural
            conformance. Apple's server-to-server flows (refresh /
            exchange) *do* use a signed client_secret JWT, but this
            module only verifies incoming identity tokens.
        callback_url: Empty — the iOS SDK owns the callback locally.
        logout_url: Empty — Apple has no RP-initiated logout.
    """

    def __init__(
        self,
        client_id: str,
        additional_audiences: list[str] | None = None,
    ) -> None:
        """Initialise the Apple auth handler.

        Args:
            client_id: The Service ID or App Bundle ID registered with
                Apple. iOS SDK stamps this into the ``aud`` claim.
            additional_audiences: Extra Bundle IDs / Service IDs to
                accept — same escape hatch GoogleAuth has. Empty or
                None keeps single-audience behaviour.
        """
        self.client_id = client_id
        self.client_secret: str | None = None
        self.callback_url = ""
        self.logout_url = ""

        extras = [a.strip() for a in (additional_audiences or []) if a and a.strip()]
        self.accepted_audiences: list[str] = [client_id, *extras]

        self.jwks_url = JWKS_URL

    # --- OIDCProvider surface: hosted-UI methods are unsupported ---------

    def get_login_url(self, state: str | None = None) -> str:
        """Apple has no hosted-UI flow that goes through the server."""
        return ""

    def get_logout_url(self) -> str:
        """Apple has no RP-initiated logout endpoint."""
        return ""

    def exchange_code_for_tokens(self, code: str) -> dict | None:
        """Server-to-server code exchange isn't implemented — the iOS
        SDK gives us the identity token directly.

        Set to ``None`` rather than raising so ``get_auth_provider()``
        can still return this instance when Apple is the primary
        provider but only for native verification.
        """
        return None

    def refresh_tokens(self, refresh_token: str) -> dict | None:
        """Refresh via Apple's ``/auth/token`` needs a signed client_secret
        JWT and Apple key material; not implemented here."""
        return None

    # --- JWKS handling ---------------------------------------------------

    def get_jwks(self) -> dict:
        """Return Apple's JWKS.

        Cached per-process after the first fetch. Apple's keys rotate
        infrequently but do rotate; a mismatched ``kid`` triggers a
        cache bust in :meth:`get_signing_key`.
        """
        global _jwks_cache
        if _jwks_cache is not None:
            return _jwks_cache
        try:
            response = requests.get(self.jwks_url, timeout=10)
            response.raise_for_status()
            _jwks_cache = response.json()
            return _jwks_cache
        except Exception as e:
            logger.error("Failed to fetch Apple JWKS: %s", e)
            return {"keys": []}

    def get_signing_key(self, token: str) -> dict | None:
        """Return the JWK matching a token's ``kid``, refreshing the
        JWKS cache on miss (Apple rotated its keys)."""
        try:
            unverified = jwt.get_unverified_header(token)
            kid = unverified.get("kid")
            if not kid:
                logger.error("No 'kid' in token header")
                return None
        except JWTError as e:
            logger.error("Bad token header: %s", e)
            return None

        for attempt in range(2):
            keys: list[dict] = self.get_jwks().get("keys", [])
            for key in keys:
                if key.get("kid") == kid:
                    return key
            if attempt == 0:
                # Bust the cache — Apple probably rotated.
                global _jwks_cache
                _jwks_cache = None
        logger.error("Signing key not found for kid=%s (after cache refresh)", kid)
        return None

    # --- Token verification ---------------------------------------------

    def verify_token(self, token: str) -> dict | None:
        """Verify an Apple-issued identity token.

        Args:
            token: Encoded JWT the iOS SDK's Sign in with Apple flow
                returned to the client and the client POSTed to
                ``/api/auth/apple/native``.

        Returns:
            Decoded claims (at minimum ``sub``; ``email`` on first
            sign-in) or ``None`` on any verification failure.
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
                # jose accepts a list as an OR-of-audiences; the type stub
                # is behind reality.
                audience=self.accepted_audiences,  # type: ignore[arg-type]
                issuer=ISSUER,
                options={"verify_at_hash": False},
            )

            # Belt-and-braces expiry check on top of jose's own — see the
            # same guard on GoogleAuth.verify_token.
            if claims.get("exp", 0) < time.time():
                logger.error("Apple token has expired")
                return None

            return claims
        except JWTError as e:
            logger.error("Apple JWT verification failed: %s", e)
            return None
        except Exception as e:
            logger.error("Apple token verification error: %s", e)
            return None

    def get_user_from_token(self, token: str) -> dict | None:
        """Verify a token and project it onto the shared user shape.

        Returned dict matches :mod:`src.auth.CLAUDE.md`'s contract
        (``sub``, ``email``, ``email_verified``, ``name``, ``role``,
        ``groups``). Apple-specific quirks:

        - ``email`` is present only on the *first* sign-in. Subsequent
          tokens have ``sub`` but no ``email`` — persist it locally
          (:meth:`UserService.find_or_create_by_apple_sub` does).
        - ``email_verified`` is a string ``"true"`` / ``"false"`` in
          Apple's claim. We coerce.
        - Apple has no group claim, so ``role='user'`` / ``groups=[]``
          — same policy as bearer api-key users. Admin access via
          Apple is a separate config decision that lives outside this
          module.
        """
        claims = self.verify_token(token)
        if not claims:
            return None

        email = claims.get("email")
        # `email_verified` is a bool-string per Apple's docs.
        raw_verified = claims.get("email_verified", "false")
        email_verified = (
            raw_verified.lower() == "true" if isinstance(raw_verified, str) else bool(raw_verified)
        )

        return {
            "sub": claims.get("sub", ""),
            "email": email,
            "email_verified": email_verified,
            "name": None,  # Apple can send `name` only on the first sign-in via
            # the authorization response, not in the ID token
            # itself. Callers can override from the sign-in payload.
            "role": "user",
            "groups": [],
        }
