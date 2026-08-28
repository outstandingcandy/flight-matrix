// WeChat login flow — the one place code exchange with the backend happens.
//
// Flow:
//   1. `wx.login()` — WeChat gives us a short-lived `code`.
//   2. POST /api/auth/wechat/login with {code, platform: "mp"}.
//   3. Backend replies with {api_key, user}.
//   4. Persist api_key + user in `wx.setStorageSync` so subsequent
//      cold starts skip the round-trip (login is cheap but the
//      round-trip isn't).
//
// `ensureLogin()` is the single entry point every page or the
// request wrapper calls when it needs a valid api_key. It's
// re-entrancy-safe via `pendingLogin` — several parallel requests
// finding an expired 401 will all await the same `wx.login` promise
// rather than triggering the flow multiple times.

const { API_BASE } = require("./config.js");

const STORAGE_API_KEY = "api_key";
const STORAGE_USER = "user";

let pendingLogin = null;

function clearStoredAuth() {
  try {
    wx.removeStorageSync(STORAGE_API_KEY);
    wx.removeStorageSync(STORAGE_USER);
  } catch (e) {
    // Best-effort — corrupted storage isn't fatal, next login rewrites it.
  }
}

function readStoredAuth() {
  try {
    const apiKey = wx.getStorageSync(STORAGE_API_KEY) || null;
    const user = wx.getStorageSync(STORAGE_USER) || null;
    return { apiKey, user };
  } catch (e) {
    return { apiKey: null, user: null };
  }
}

function wxLoginPromise() {
  return new Promise((resolve, reject) => {
    wx.login({
      success: (res) => (res && res.code ? resolve(res.code) : reject(new Error("wx.login returned no code"))),
      fail: reject,
    });
  });
}

function exchangeCode(code) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${API_BASE}/api/auth/wechat/login`,
      method: "POST",
      data: { code, platform: "mp" },
      header: { "Content-Type": "application/json" },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300 && res.data && res.data.success) {
          resolve(res.data);
        } else {
          reject(new Error(`wechat login failed: HTTP ${res.statusCode} ${JSON.stringify(res.data)}`));
        }
      },
      fail: reject,
    });
  });
}

/**
 * Return `{apiKey, user}`, reusing the storage-backed pair if fresh
 * and re-doing the WeChat exchange when `force` is true (i.e. after
 * a 401 hit).
 *
 * Concurrent callers get the same in-flight promise via `pendingLogin`.
 */
async function ensureLogin({ force = false } = {}) {
  if (!force) {
    const stored = readStoredAuth();
    if (stored.apiKey) return stored;
  }

  if (pendingLogin) return pendingLogin;

  pendingLogin = (async () => {
    try {
      clearStoredAuth();
      const code = await wxLoginPromise();
      const body = await exchangeCode(code);
      wx.setStorageSync(STORAGE_API_KEY, body.api_key);
      wx.setStorageSync(STORAGE_USER, body.user);
      return { apiKey: body.api_key, user: body.user };
    } finally {
      pendingLogin = null;
    }
  })();
  return pendingLogin;
}

module.exports = {
  ensureLogin,
  clearStoredAuth,
  readStoredAuth,
};
