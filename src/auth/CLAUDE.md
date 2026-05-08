# src/auth/

AWS Cognito authentication module.

## Components

| File | Purpose |
|------|---------|
| `cognito_auth.py` | `CognitoAuth` - JWT verification and OAuth2 token exchange |
| `decorators.py` | `@require_auth` decorator for protected routes |

## CognitoAuth

Handles:
- JWT token verification (offline using JWKS)
- OAuth2 authorization code flow
- Token refresh
- Login/logout URL generation

```python
from src.auth.cognito_auth import CognitoAuth

auth = CognitoAuth(
    user_pool_id="us-west-2_xxxxx",
    client_id="xxxxxxxxx",
    client_secret="xxxxxxxxx",
    domain="flight-matrix.auth.us-west-2.amazoncognito.com",
    callback_url="https://example.com/callback",
    logout_url="https://example.com/logout"
)

# Verify JWT token
claims = auth.verify_token(id_token)

# Exchange authorization code
tokens = auth.exchange_code(auth_code)
```

## Route Protection

```python
from src.auth.decorators import require_auth

@app.route("/api/protected")
@require_auth
def protected_endpoint():
    user = g.user  # User claims from JWT
    return jsonify({"user": user})
```

## Environment Variables

- `COGNITO_JWKS` - Pre-loaded JWKS for offline JWT verification (Lambda)
- `COGNITO_USER_POOL_ID`
- `COGNITO_CLIENT_ID`
- `COGNITO_CLIENT_SECRET`
- `COGNITO_DOMAIN`
