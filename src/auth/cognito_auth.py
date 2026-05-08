"""
Cognito Authentication Module

Provides JWT verification and token exchange for AWS Cognito.

JWT verification uses offline JWKS validation - no network access required.
JWKS is loaded from environment variable COGNITO_JWKS or fetched once at startup.
"""

import json
import logging
import os
import time
from urllib.parse import urlencode

import requests
from jose import JWTError, jwk, jwt

logger = logging.getLogger(__name__)

# Static JWKS cache - loaded from environment or fetched once
_jwks_cache: dict | None = None


class CognitoAuth:
    """AWS Cognito authentication handler."""

    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        client_secret: str | None,
        domain: str,
        callback_url: str,
        logout_url: str,
    ):
        """Initialize Cognito auth handler.

        Args:
            user_pool_id: Cognito User Pool ID (e.g., us-west-2_xxxxx)
            client_id: Cognito App Client ID
            client_secret: Cognito App Client Secret
            domain: Cognito domain (e.g., flight-matrix.auth.us-west-2.amazoncognito.com)
            callback_url: OAuth2 callback URL
            logout_url: Logout redirect URL
        """
        self.user_pool_id = user_pool_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.domain = domain
        self.callback_url = callback_url
        self.logout_url = logout_url

        # Extract region from user pool ID
        self.region = user_pool_id.split("_")[0] if "_" in user_pool_id else "us-east-1"

        # JWKS URL
        self.jwks_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{self.user_pool_id}/.well-known/jwks.json"
        )

        # Token endpoint
        self.token_endpoint = f"https://{self.domain}/oauth2/token"

        # Issuer for token validation
        self.issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    def get_login_url(self, state: str | None = None) -> str:
        """Get Cognito Hosted UI login URL.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Login URL string
        """
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "email openid profile",
            "redirect_uri": self.callback_url,
        }
        if state:
            params["state"] = state

        return f"https://{self.domain}/login?{urlencode(params)}"

    def get_logout_url(self) -> str:
        """Get Cognito Hosted UI logout URL.

        Returns:
            Logout URL string
        """
        params = {
            "client_id": self.client_id,
            "logout_uri": self.logout_url,
        }
        return f"https://{self.domain}/logout?{urlencode(params)}"

    def get_jwks(self) -> dict:
        """Get Cognito JWKS (JSON Web Key Set) for offline JWT verification.

        JWKS is loaded in this priority order:
        1. In-memory cache (if already loaded)
        2. COGNITO_JWKS environment variable (JSON string)
        3. Network fetch (fallback, requires internet access)

        Returns:
            JWKS dictionary with 'keys' array

        Raises:
            RuntimeError: If JWKS cannot be loaded from any source
        """
        global _jwks_cache

        # Return cached keys if available
        if _jwks_cache is not None:
            return _jwks_cache

        # Try loading from environment variable (offline mode)
        jwks_env = os.environ.get("COGNITO_JWKS")
        if jwks_env:
            try:
                _jwks_cache = json.loads(jwks_env)
                logger.info("Loaded JWKS from COGNITO_JWKS environment variable (offline mode)")
                return _jwks_cache
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse COGNITO_JWKS: {e}")

        # Fallback: fetch from network (requires internet access)
        try:
            logger.warning(
                "COGNITO_JWKS not set, fetching from network. "
                "Set COGNITO_JWKS env var for offline JWT verification."
            )
            response = requests.get(self.jwks_url, timeout=10)
            response.raise_for_status()
            _jwks_cache = response.json()
            logger.info("Fetched JWKS from Cognito (network)")
            return _jwks_cache

        except Exception as e:
            logger.error(f"Failed to fetch JWKS from network: {e}")
            raise RuntimeError(
                f"Cannot load JWKS. Set COGNITO_JWKS env var or ensure network access. Error: {e}"
            )

    def get_signing_key(self, token: str) -> dict | None:
        """Get the signing key for a JWT token.

        Args:
            token: JWT token string

        Returns:
            JWK dictionary for the signing key, or None if not found
        """
        try:
            # Get the key ID from the token header
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")

            if not kid:
                logger.error("Token has no 'kid' in header")
                return None

            # Find the matching key in JWKS
            jwks = self.get_jwks()
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    return key

            logger.error(f"Signing key not found for kid: {kid}")
            return None

        except Exception as e:
            logger.error(f"Error getting signing key: {e}")
            return None

    def verify_token(self, token: str) -> dict | None:
        """Verify a JWT token from Cognito.

        Args:
            token: JWT token string (id_token or access_token)

        Returns:
            Decoded token claims if valid, None otherwise
        """
        try:
            # Get signing key
            signing_key = self.get_signing_key(token)
            if not signing_key:
                return None

            # Convert JWK to PEM for verification
            public_key = jwk.construct(signing_key)

            # Verify and decode the token
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
                options={
                    "verify_at_hash": False,  # Disable at_hash verification
                },
            )

            # Additional validation
            current_time = time.time()

            # Check token expiration
            if claims.get("exp", 0) < current_time:
                logger.error("Token has expired")
                return None

            # Check token use (for access tokens)
            token_use = claims.get("token_use")
            if token_use and token_use not in ["id", "access"]:
                logger.error(f"Invalid token_use: {token_use}")
                return None

            return claims

        except JWTError as e:
            logger.error(f"JWT verification failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            return None

    def get_user_from_token(self, token: str) -> dict | None:
        """Extract user information from a verified token.

        Args:
            token: JWT token string (id_token preferred)

        Returns:
            User info dict with email, sub, etc., or None if invalid
        """
        claims = self.verify_token(token)
        if not claims:
            return None

        groups = claims.get("cognito:groups", [])
        email = claims.get("email")
        logger.debug("token_groups user=%s groups=%s", email, groups)

        return {
            "sub": claims.get("sub"),
            "email": email,
            "email_verified": claims.get("email_verified", False),
            "name": claims.get("name") or claims.get("cognito:username"),
            "role": claims.get("custom:role", "user"),
            "groups": groups,
        }

    def exchange_code_for_tokens(self, code: str) -> dict | None:
        """Exchange authorization code for tokens.

        Args:
            code: Authorization code from OAuth2 callback

        Returns:
            Token response dict with id_token, access_token, refresh_token,
            or None on failure
        """
        try:
            # Prepare request data
            data = {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": code,
                "redirect_uri": self.callback_url,
            }

            # Add client secret if available
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            if self.client_secret:
                import base64

                credentials = f"{self.client_id}:{self.client_secret}"
                encoded_credentials = base64.b64encode(credentials.encode()).decode()
                headers["Authorization"] = f"Basic {encoded_credentials}"

            # Exchange code for tokens
            response = requests.post(
                self.token_endpoint,
                data=data,
                headers=headers,
                timeout=10,
            )

            if response.status_code != 200:
                logger.error(f"Token exchange failed: {response.status_code} - {response.text}")
                return None

            tokens = response.json()
            logger.info("Successfully exchanged code for tokens")
            return tokens

        except Exception as e:
            logger.error(f"Token exchange error: {e}")
            return None

    def refresh_tokens(self, refresh_token: str) -> dict | None:
        """Refresh access and ID tokens using refresh token.

        Args:
            refresh_token: Refresh token string

        Returns:
            New token response dict, or None on failure
        """
        try:
            data = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": refresh_token,
            }

            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            if self.client_secret:
                import base64

                credentials = f"{self.client_id}:{self.client_secret}"
                encoded_credentials = base64.b64encode(credentials.encode()).decode()
                headers["Authorization"] = f"Basic {encoded_credentials}"

            response = requests.post(
                self.token_endpoint,
                data=data,
                headers=headers,
                timeout=5,  # Short timeout to prevent Lambda blocking
            )

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code} - {response.text}")
                return None

            return response.json()

        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return None


# Singleton instance
_cognito_auth: CognitoAuth | None = None


def get_cognito_auth() -> CognitoAuth | None:
    """Get or create the CognitoAuth singleton instance.

    Returns:
        CognitoAuth instance, or None if not configured
    """
    global _cognito_auth

    if _cognito_auth is not None:
        return _cognito_auth

    # Get configuration from environment
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    client_secret = os.environ.get("COGNITO_CLIENT_SECRET", "")
    domain = os.environ.get("COGNITO_DOMAIN", "")
    callback_url = os.environ.get("COGNITO_CALLBACK_URL", "")
    logout_url = os.environ.get("COGNITO_LOGOUT_URL", "")

    # Validate required configuration
    if not all([user_pool_id, client_id, domain, callback_url]):
        logger.warning(
            "Cognito not configured. Set COGNITO_USER_POOL_ID, "
            "COGNITO_CLIENT_ID, COGNITO_DOMAIN, and COGNITO_CALLBACK_URL"
        )
        return None

    _cognito_auth = CognitoAuth(
        user_pool_id=user_pool_id,
        client_id=client_id,
        client_secret=client_secret or None,
        domain=domain,
        callback_url=callback_url,
        logout_url=logout_url,
    )

    return _cognito_auth


# Convenience functions
def verify_token(token: str) -> dict | None:
    """Verify a JWT token.

    Args:
        token: JWT token string

    Returns:
        Decoded token claims if valid, None otherwise
    """
    auth = get_cognito_auth()
    if not auth:
        return None
    return auth.verify_token(token)


def get_user_from_token(token: str) -> dict | None:
    """Get user info from a token.

    Args:
        token: JWT token string

    Returns:
        User info dict, or None if invalid
    """
    auth = get_cognito_auth()
    if not auth:
        return None
    return auth.get_user_from_token(token)


def exchange_code_for_tokens(code: str) -> dict | None:
    """Exchange authorization code for tokens.

    Args:
        code: Authorization code

    Returns:
        Token response dict, or None on failure
    """
    auth = get_cognito_auth()
    if not auth:
        return None
    return auth.exchange_code_for_tokens(code)
