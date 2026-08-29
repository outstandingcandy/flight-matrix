// Airport arrivals/departures board — mirrors airport_board.html.
// Reads /api/airports/{code} + /api/flight-schedules?airport={code}
// and renders the polling loop the web version has at 30 s.
const { apiFetch } = require("../../utils/request.js");

const POLL_MS = 30000;

Page({
  data: {
    airport: "PEK",
    info: null,
    schedules: [],
    loading: false,
    error: null,
  },

  onLoad(query) {
    if (query && query.airport) this.setData({ airport: query.airport.toUpperCase() });
    this.refresh();
    // 30-second poll — use wx.setInterval so lifecycle events stop it.
    this._timer = setInterval(() => this.refresh(true), POLL_MS);
  },

  onHide() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  },

  onUnload() {
    if (this._timer) clearInterval(this._timer);
  },

  async refresh(quiet = false) {
    if (!quiet) this.setData({ loading: true, error: null });
    try {
      const [info, schedules] = await Promise.all([
        apiFetch(`/api/airports/${this.data.airport}`),
        apiFetch(`/api/flight-schedules?airport=${this.data.airport}&limit=100`),
      ]);
      this.setData({
        info: info.airport || info.data || info,
        schedules: schedules.schedules || [],
        loading: false,
      });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },

  onSearchAirport(e) {
    const code = (e.detail.value || "").trim().toUpperCase();
    if (code.length >= 3 && code.length <= 4) {
      this.setData({ airport: code });
      this.refresh();
    }
  },
});
