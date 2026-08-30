// User dashboard — /api/user/{email}/{profile,usage} + subscription
// feature overrides. Email comes from `getApp().globalData.user`,
// which was populated at onLaunch by the wechat login exchange.
const { apiFetch } = require("../../utils/request.js");

Page({
  data: {
    email: "",
    profile: null,
    usage: null,
    loading: true,
    error: null,
  },

  onLoad() {
    const user = (getApp().globalData || {}).user;
    if (!user || !user.email) {
      this.setData({ loading: false, error: "未登录" });
      return;
    }
    this.setData({ email: user.email });
    this.refresh();
  },

  async refresh() {
    if (!this.data.email) return;
    this.setData({ loading: true, error: null });
    try {
      const [profile, usage] = await Promise.all([
        apiFetch(`/api/v1/user/${encodeURIComponent(this.data.email)}/profile`),
        apiFetch(`/api/v1/user/${encodeURIComponent(this.data.email)}/usage`),
      ]);
      this.setData({
        profile: profile.user,
        features: profile.features,
        activeFilters: profile.active_filters_count,
        usage: usage.usage,
        loading: false,
      });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },
});
