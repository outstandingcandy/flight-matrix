# src/auth/

OIDC authentication. Two providers coexist; which one is active follows the
deployment target, not a code change.

| `DEPLOY_TARGET` | Provider | Group membership from |
|---|---|---|
| `aws` | `CognitoAuth` (Cognito Hosted UI) | the `cognito:groups` ID-token claim |
| `gcp` | `GoogleAuth` (Google Sign-In) | `auth.google.groups` in `config/auth.yaml` |
| `local` | none — auth bypassed | `LOCAL_DEV_GROUPS` env var |

## Components

| File | Purpose |
|------|---------|
| `base.py` | `OIDCProvider` — the Protocol both providers satisfy structurally |
| `cognito_auth.py` | `CognitoAuth` — JWT verification and OAuth2 token exchange |
| `google_auth.py` | `GoogleAuth` — same surface against Google's endpoints |
| `factory.py` | `get_auth_provider()` and the provider-agnostic convenience wrappers |
| `decorators.py` | `login_required`, `admin_required`, `flight_schedules_required`, `group_required`, `optional_login`, `get_current_user` |

## Getting the provider

Always go through the factory. Importing `CognitoAuth` directly hard-codes the
aws target.

```python
from src.auth.factory import get_auth_provider, get_user_from_token

auth = get_auth_provider()  # CognitoAuth | GoogleAuth | None
if auth:
    login_url = auth.get_login_url(state=state)

user = get_user_from_token(id_token)  # dispatches to the active provider
```

`get_auth_provider()` returns `None` when the provider is `none` **or** when its
required environment variables are incomplete. That `None` is load-bearing:
`decorators.py` uses it to choose between redirecting to `/login` and rendering
a 403. Do not make it raise. An *unrecognised* `auth.provider` value does raise
`ConfigurationError` — a typo must not silently authenticate against the wrong
identity provider.

## The user shape is a contract

Every provider's `get_user_from_token()` returns the same fields, because
`decorators.py`, the `is_admin` template helper, and `/auth/debug` read them by
name:

```python
{"sub": ..., "email": ..., "email_verified": ..., "name": ..., "role": ..., "groups": [...]}
```

`role` is `"admin"` when the user is in the `admins` group, `"user"` otherwise.
Renaming or dropping a field here breaks authorisation quietly rather than
loudly — `tests/auth/test_google_auth.py::test_user_shape_matches_cognito`
guards it.

## Route protection

```python
from src.web.auth_shim import login_required, admin_required, flight_schedules_required


@app.route("/api/protected")
@login_required
def protected_endpoint():
    from src.auth.decorators import get_current_user

    return jsonify({"user": get_current_user()})
```

Import decorators from `src.web.auth_shim`, not from `src.auth.decorators`: the
shim hands out no-ops when `--skip-auth` / `SKIP_AUTH=true` is set, which is how
the local target runs.

## Provider differences that bite

Writing `GoogleAuth` the Cognito way fails in ways that are not obvious:

1. **Client credentials go in the token-request body**, not an HTTP Basic
   header. Cognito wants Basic; Google answers `invalid_client` to it.
2. **`access_type=offline` + `prompt=consent` are mandatory** on the
   authorization URL. Without them Google issues no refresh token and the
   refresh fallback in `get_current_user()` stops working with no error.
3. **Google has no group claim.** Groups come from `config/auth.yaml`. An email
   listed in no group signs in successfully and is then denied by the group
   decorators — that is intended.
4. **Google has no RP-initiated logout.** `get_logout_url()` returns `""` and
   callers must treat that as "session cleared, go to the landing page".
5. **No `token_use` claim** to validate, and no region to derive from a user
   pool ID.

## Callback flows differ by provider

- **Cognito** exchanges the code **in the browser**: `/auth/callback` renders
  `auth_callback.html`, which POSTs the tokens to `/auth/set-session`. This is
  because Lambda runs in a VPC (needed for Aurora) with no NAT, so it cannot
  reach the Cognito token endpoint. `templates/auth_callback.html` and
  `/auth/set-session` are live code for this path, not leftovers.
- **Google** exchanges the code **server-side** inside `/auth/callback`. No
  template is rendered and no client secret reaches the page. `inject_auth_config`
  in `web_app.py` deliberately returns `None` for Google for the same reason.

## Debugging

`GET /auth/debug` (login-gated) reports `resolved_provider`, `groups_source`
(`claims` / `config` / `env`), the verified claims, and the resolved groups.
That is the fastest way to diagnose a three-target authorisation problem.

## Environment Variables

Shared: `STAGE`, `SKIP_AUTH`, `LOCAL_DEV_EMAIL`, `LOCAL_DEV_GROUPS`.

aws (`cognito`), read directly from the environment:

- `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`
- `COGNITO_DOMAIN`, `COGNITO_CALLBACK_URL`, `COGNITO_LOGOUT_URL`
- `COGNITO_JWKS` — pre-loaded JWKS for offline verification (Lambda has no egress)

gcp (`google`), interpolated into `config/auth.yaml`:

- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_CALLBACK_URL`, `GOOGLE_OAUTH_LOGOUT_URL`
- `GOOGLE_JWKS` — pre-loaded JWKS for offline verification
