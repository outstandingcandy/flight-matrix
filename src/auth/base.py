"""Provider-agnostic OIDC interface shared by the Cognito and Google backends.

`CognitoAuth` and `GoogleAuth` both satisfy `OIDCProvider` *structurally* —
neither inherits from it. That is deliberate: `CognitoAuth` predates this
abstraction and serves production traffic, so it is not modified at all.

The contract that matters most is `get_user_from_token()`. Every
implementation must return the same field set — `sub`, `email`,
`email_verified`, `name`, `role`, `groups` — because `src/auth/decorators.py`
and the `/auth/debug` route read those names directly. A provider that
renames or omits one of them breaks authorisation silently rather than
loudly.
"""

from __future__ import annotations

from typing import Protocol


class OIDCProvider(Protocol):
    """The surface every authentication backend must expose.

    Attributes:
        client_id: OAuth2 client identifier for this application.
        client_secret: OAuth2 client secret, or None when the client is public.
        callback_url: Redirect URI registered with the identity provider.
        logout_url: Where the provider should send the user after logout.
            Empty when the provider has no RP-initiated logout endpoint.
    """

    client_id: str
    client_secret: str | None
    callback_url: str
    logout_url: str

    def get_login_url(self, state: str | None = None) -> str:
        """Build the provider's authorization URL.

        Args:
            state: Opaque CSRF token echoed back to the callback.

        Returns:
            Absolute URL the browser should be redirected to.
        """
        ...

    def get_logout_url(self) -> str:
        """Build the provider's logout URL.

        Returns:
            Absolute logout URL, or an empty string when the provider has no
            RP-initiated logout endpoint (Google). Callers must treat an empty
            string as "clear the local session and go to the landing page".
        """
        ...

    def get_jwks(self) -> dict:
        """Return the provider's JSON Web Key Set.

        Returns:
            JWKS dictionary containing a `keys` array.
        """
        ...

    def verify_token(self, token: str) -> dict | None:
        """Verify a JWT's signature, audience, issuer and expiry.

        Args:
            token: Encoded JWT.

        Returns:
            Decoded claims when the token is valid, None otherwise.
        """
        ...

    def get_user_from_token(self, token: str) -> dict | None:
        """Verify a token and project it onto the shared user shape.

        Args:
            token: Encoded ID token.

        Returns:
            Dict with `sub`, `email`, `email_verified`, `name`, `role` and
            `groups`, or None when the token is invalid or the account is not
            permitted to sign in.
        """
        ...

    def exchange_code_for_tokens(self, code: str) -> dict | None:
        """Exchange an authorization code for tokens.

        Args:
            code: Authorization code from the OAuth2 callback.

        Returns:
            Token response containing at least `id_token`, or None on failure.
        """
        ...

    def refresh_tokens(self, refresh_token: str) -> dict | None:
        """Mint new tokens from a refresh token.

        Args:
            refresh_token: Refresh token issued at login.

        Returns:
            New token response, or None on failure.
        """
        ...
