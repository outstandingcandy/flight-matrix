# flight-matrix iOS — spec + starter skeleton (阶段 3)

The iOS app lives in a **separate repo** (`flight-matrix-ios`) per the
plan. That repo doesn't exist yet — pending user setup on the Apple
Developer Portal + Xcode. This file collects what needs to go into
that repo when it's created so nothing has to be re-derived.

## Prerequisites (user tasks)

- Apple Developer Program membership.
- Register **App Bundle ID** at developer.apple.com — set as
  `APPLE_BUNDLE_ID` on the backend (`config/auth.yaml`
  `auth.apple.bundle_id`).
- Register a WeChat Open Platform App (open.weixin.qq.com, distinct
  from mp) — the mp AppID and app AppID must live under the *same
  Open Platform account* for cross-app `unionid` to work. Set as
  `WECHAT_APP_APPID` / `WECHAT_APP_APPSECRET`.
- Google OAuth iOS client at console.cloud.google.com — set the iOS
  client_id as `GOOGLE_OAUTH_IOS_CLIENT_ID`.

## Backend endpoints already in place

All three native login endpoints and every read endpoint the app needs
were shipped on `feat/fastapi-migration` and are covered by
integration tests (`tests/web/test_native_auth_route_fastapi.py`).

- `POST /api/auth/apple/native`  `{identity_token}`
- `POST /api/auth/google/native` `{id_token}`
- `POST /api/auth/wechat/login`  `{code, platform: "app"}`
- `GET  /api/me` (bearer-auth)
- `POST /api/me/api-key/rotate` (bearer-auth)

`AppleAuth` uses `APPLE_BUNDLE_ID` as the accepted `aud` claim.
`GoogleAuth.additional_audiences` accepts the iOS + Android client_ids
so the iOS SDK's native token verifies against the same instance.

## Project layout when the repo is created

```
FlightMatrix/                           swift package / .xcodeproj
  App/
    FlightMatrixApp.swift               @main, WindowGroup, .task { AuthStore.warm() }
  Networking/
    APIClient.swift                     URLSession + async/await, Bearer header,
                                        401 → LoginProvider.reAuthenticate()
    Endpoint.swift                      Enum of routes + URL builder
    AuthStore.swift                     Keychain-backed api_key + user cache
  Auth/
    LoginProvider.swift                 protocol { func signIn() async throws -> String }
    AppleLoginService.swift             AuthenticationServices
                                        (ASAuthorizationAppleIDProvider)
    GoogleLoginService.swift            GoogleSignIn-iOS SDK
    WechatLoginService.swift            WechatOpenSDK (WXApi.SendAuthReq)
  Models/
    Aircraft.swift  Airport.swift ...   Codable, or codegen'd from OpenAPI
  Screens/
    Home/  AircraftDetail/  AirportBoard/
    FlightSchedules/  SearchTrack/
    UserDashboard/  UserFilters/
  Map/
    MapView.swift                       MapKit-native, MKMapView UIViewRepresentable
```

## Login flow (all three provider methods)

```swift
protocol LoginProvider {
  /// Runs the provider's native SDK flow, returns the token/code the
  /// server understands.
  func signIn() async throws -> ProviderToken
}

enum ProviderToken {
  case apple(identityToken: String)                // → /api/auth/apple/native
  case google(idToken: String)                     // → /api/auth/google/native
  case wechat(code: String)                        // → /api/auth/wechat/login (platform=app)
}

extension APIClient {
  func exchange(_ token: ProviderToken) async throws -> (apiKey: String, user: User) {
    let (path, body): (String, Encodable) = switch token {
      case .apple(let t):  ("/api/v1/auth/apple/native",  ["identity_token": t])
      case .google(let t): ("/api/v1/auth/google/native", ["id_token": t])
      case .wechat(let c): ("/api/v1/auth/wechat/login",  ["code": c, "platform": "app"])
    }
    let res: AuthResponse = try await post(path, body: body)
    return (res.apiKey, res.user)
  }
}
```

## APIClient invariants

- One shared instance, reads api_key from `AuthStore` on every request.
- `x-app-platform: ios-<bundle-version>` header for analytics / debug.
- 401 handling: single retry after `AuthStore.reAuthenticate()`, matching
  the wxapp's `utils/request.js` behaviour.
- Never `URLCache` on POST/PUT/DELETE.

## Map

MapKit-native, not Google Maps — free, no API key, no bundle-size hit.
`airport-board` and `search-track` both use the same overlay pattern:
polyline for the trail, `MKPointAnnotation` for markers.

## FlightSchedules 30 s poll

`Timer.publish(every: 30, on: .main, in: .common).autoconnect()`
scoped to `.task(id: view)` so the timer stops the moment the view
disappears. Same lifecycle contract as the wxapp's `onHide`.

## App Store Guideline 4.8

If any of Google/WeChat is offered, Sign in with Apple **must** also be
offered. The three providers are shipped together in `Screens/Auth/`
for exactly this reason.

## OpenAPI codegen (optional)

FastAPI mounts `/docs` with the OpenAPI schema. Running:

```
swagger-codegen generate -i https://api.flightmatrix.top/openapi.json \
    -l swift5 -o Sources/GeneratedAPI
```

produces the `Models/` + `Networking/` layer above with real
type-safe request/response shapes. Worth doing after the first hand-
written pass to see how much boilerplate it eliminates.

## Testing

- Xcode Simulator: full auth round-trip through each provider.
- TestFlight for 5–10 internal testers before App Store submission.
- Sign-in-with-Apple E2E only works on a real device (Simulator can't
  reach Apple's servers with the right entitlement).
