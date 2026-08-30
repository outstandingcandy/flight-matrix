// Bearer-wrapped `wx.request`. Every page and helper that talks to
// the flight-matrix API goes through here — never call `wx.request`
// directly except during the login exchange itself (that would loop).
//
// Contract with the backend:
// - Every response is either `{success: true, ...}` or `{success: false,
//   error: "..."}`. Non-2xx status is a hard failure.
// - 401 means the api_key is stale (deleted user, rotated key). One
//   retry after `ensureLogin({force: true})` — if that still 401s, we
//   surface the failure to the caller rather than looping.
//
// The wrapper resolves with the parsed response body (i.e. the
// `{success, ...}` object), or rejects with an Error carrying the
// server-provided error message where available.

const { API_BASE } = require("./config.js");
const { ensureLogin, readStoredAuth } = require("./auth.js");

function _wxRequest({ url, method, data, header }) {
  return new Promise((resolve, reject) => {
    wx.request({
      url,
      method,
      data,
      header,
      success: resolve,
      fail: reject,
    });
  });
}

function _buildHeader(apiKey, extra) {
  const header = { "Content-Type": "application/json", ...extra };
  if (apiKey) header["Authorization"] = `Bearer ${apiKey}`;
  return header;
}

/**
 * Make an authenticated API call.
 *
 * @param {string} path - `/api/v1/...` — combined with API_BASE.
 * @param {object} opts
 * @param {"GET"|"POST"|"PUT"|"DELETE"} [opts.method="GET"]
 * @param {object} [opts.data] - request body (POST/PUT) or query string (GET)
 * @param {object} [opts.header] - extra headers merged into the default
 * @returns {Promise<object>} - the parsed `{success, ...}` body
 * @throws {Error} on non-2xx / connection failure / login exhaustion
 */
async function apiFetch(path, { method = "GET", data, header } = {}) {
  const url = `${API_BASE}${path}`;
  const { apiKey } = readStoredAuth();

  const attempt = async (key) => {
    return _wxRequest({ url, method, data, header: _buildHeader(key, header) });
  };

  let res = await attempt(apiKey);

  if (res.statusCode === 401) {
    // Api-key stale — re-login once and retry.
    const { apiKey: fresh } = await ensureLogin({ force: true });
    res = await attempt(fresh);
  }

  if (res.statusCode < 200 || res.statusCode >= 300) {
    const errBody = res.data || {};
    throw new Error(
      `API ${method} ${path} → HTTP ${res.statusCode}: ${errBody.error || JSON.stringify(errBody)}`
    );
  }

  const body = res.data;
  if (body && body.success === false) {
    throw new Error(`API ${method} ${path} → ${body.error || "unknown error"}`);
  }
  return body;
}

module.exports = { apiFetch };
