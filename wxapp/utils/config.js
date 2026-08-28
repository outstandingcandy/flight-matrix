// Environment-specific config. Kept in one place so the request +
// auth wrappers don't have to reach into anywhere else.
//
// If you need per-build configuration later, switch on
// `__wxConfig.envVersion` (returns "release" | "trial" | "develop")
// or add a `project.config.json` extAppid + read `wx.getExtConfigSync()`.

const API_BASE = "https://api.flightmatrix.top";
// CDN origin the JS fetches images / static assets from. Currently
// unused in wrapper code (pages construct their own <image src=...>
// URLs), but centralised here so the domain allowlist in the WeChat
// admin console has a single source of truth.
const CDN_BASE = "https://cdn.flightmatrix.top";

module.exports = { API_BASE, CDN_BASE };
