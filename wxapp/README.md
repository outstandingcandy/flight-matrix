# flight-matrix wxapp

WeChat mini-program frontend for flight-matrix. Native framework (no
Taro / uni-app) — WXML/WXSS/JS. Talks to the same FastAPI backend as
the web frontend via `https://api.flightmatrix.top/api/*` with a
per-user api_key stored in `wx.setStorageSync`.

## Prerequisites

- Register a mini-program AppID at <https://mp.weixin.qq.com/> (公众平台).
- WeChat DevTools installed locally.
- The backend deployed with `WECHAT_MP_APPID` / `WECHAT_MP_APPSECRET`
  in `.env` — see `.env.example` and `config/auth.yaml`.
- **Server-domain allowlist** (mini-program admin console → 开发 → 服务器域名):
  - `request` legal domains: `https://api.flightmatrix.top`
  - `downloadFile` legal domains: `https://cdn.flightmatrix.top`
    (or whichever `STATIC_BASE_URL` / bucket URL serves scraped images)

## Login flow

1. `wx.login()` → one-time code from WeChat.
2. `wx.request` POST to `/api/v1/auth/wechat/login`:
   `{code, platform: "mp"}` → `{api_key, user}`.
3. `wx.setStorageSync("api_key", ...)` for the session.
4. `utils/request.js` wraps every subsequent `wx.request` to add
   `Authorization: Bearer <api_key>`.
5. On 401 the wrapper clears storage and calls back to step 1.

## Layout

```
wxapp/
├── project.config.json      DevTools project config
├── app.js                   Bootstraps login + Bearer helper
├── app.json                 Registered pages + window chrome
├── app.wxss                 Shared styles
├── utils/
│   ├── request.js           Bearer-wrapped wx.request
│   └── auth.js              wx.login → server exchange → storage
└── pages/
    ├── home/                Landing page
    ├── aircraft-detail/     Per-aircraft detail
    ├── airport-board/       Airport arrivals/departures board
    ├── flight-schedules/    Schedule list + map tabs
    ├── search-track/        Aircraft historical track
    ├── user-dashboard/      Profile + subscription
    └── user-filters/        Filter CRUD
```

## Endpoint contract

The backend endpoints below have all been ported to FastAPI on
`feat/fastapi-migration` and are covered by integration tests:

- `POST /api/auth/wechat/login` (`platform: "mp"`)
- `GET /api/statistics`
- `GET /api/aircraft/*`
- `GET /api/airports/*`
- `GET /api/flight-schedules`, `/api/v1/flight-schedules/filter-options`
- `GET /api/user/{email}/profile|usage|cooldowns|filters`
- Filter CRUD: `POST/PUT/DELETE /api/user/{email}/filters[/{id}]`

Response shape is `{success: bool, ...data | error}` on every route.
`utils/request.js` normalises this so page code can rely on
`result.success` without re-reading `res.data`.

## Development

Open the `wxapp/` directory in WeChat DevTools with your registered
AppID. `project.config.json` ships with an empty `appid` — the tool
picks up the local `.mp-cli-config` first, or the AppID you enter in
the login dialog when the project first opens.

## What's not shipped

- Real page WXML/JS content (only page shells) — the frontend engineer
  fills these in with the domain data. Every page has a
  `pages/<name>/index.{js,json,wxml,wxss}` skeleton wired into
  `app.json`.
- `components/*` — build as needed from the page code.
- `assets/*` — images / fonts, add when needed.
