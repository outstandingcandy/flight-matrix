// Search-track — /api/aircraft/tracks/{registration} historical trail.
// Web version renders on Leaflet polyline; wxapp uses the built-in <map>.
const { apiFetch } = require("../../utils/request.js");

Page({
  data: {
    query: "",
    tracks: [],
    polyline: [],
    loading: false,
    error: null,
  },

  onSearchInput(e) {
    this.setData({ query: (e.detail.value || "").trim().toUpperCase() });
  },

  async onSearchTap() {
    if (!this.data.query) return;
    this.setData({ loading: true, error: null });
    try {
      const body = await apiFetch(`/api/v1/aircraft/tracks/${this.data.query}`);
      const tracks = body.tracks || [];
      const points = tracks
        .filter((t) => t.latitude && t.longitude)
        .map((t) => ({ latitude: Number(t.latitude), longitude: Number(t.longitude) }));
      this.setData({
        tracks,
        polyline: points.length > 1 ? [{ points, color: "#0d1b2a", width: 3 }] : [],
        loading: false,
      });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },
});
