// Global app state. Populated by ensureLogin(), consumed by pages via
// `getApp().globalData`. Kept tiny on purpose — anything larger belongs
// on the page's `data` or in `wx.setStorageSync`.
const { ensureLogin } = require("./utils/auth.js");

App({
  globalData: {
    // Bearer token used by utils/request.js. Populated eagerly at
    // onLaunch so the first page request has it.
    apiKey: null,
    // User payload from /api/auth/wechat/login. Shape:
    // {id, email, name, status, subscription: {...}}
    user: null,
  },

  async onLaunch() {
    try {
      const { apiKey, user } = await ensureLogin();
      this.globalData.apiKey = apiKey;
      this.globalData.user = user;
    } catch (err) {
      // Login failure is non-fatal — pages that need auth will call
      // ensureLogin() again on demand. Log for the devtools console.
      console.error("wechat login failed at onLaunch:", err);
    }
  },
});
