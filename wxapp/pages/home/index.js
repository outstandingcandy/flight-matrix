// Home page — dashboard-style landing that mirrors home.html.
// Fetches /api/statistics for the top-line counts and a slice of
// /api/aircraft/recent for the "most recent" feed.
const { apiFetch } = require("../../utils/request.js");

Page({
  data: {
    stats: null,
    recentAircraft: [],
    loading: true,
    error: null,
  },

  onLoad() {
    this.refresh();
  },

  onPullDownRefresh() {
    this.refresh().finally(() => wx.stopPullDownRefresh());
  },

  async refresh() {
    this.setData({ loading: true, error: null });
    try {
      const [stats, recent] = await Promise.all([
        apiFetch("/api/v1/statistics"),
        apiFetch("/api/v1/aircraft/recent?limit=10"),
      ]);
      this.setData({
        stats: stats.statistics || stats,
        recentAircraft: recent.aircraft || recent.data || [],
        loading: false,
      });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },
});
