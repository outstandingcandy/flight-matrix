// Aircraft detail — mirrors aircraft_detail.html. Reads
//   /api/aircraft/{identifier}/details for the header block
//   /api/aircraft/{identifier}/history for recent snapshots
//   /api/aircraft/{identifier}/images for the photo carousel
const { apiFetch } = require("../../utils/request.js");

Page({
  data: {
    identifier: "",
    details: null,
    history: [],
    images: [],
    loading: true,
    error: null,
  },

  onLoad(query) {
    const identifier = (query && (query.identifier || query.registration)) || "";
    if (!identifier) {
      this.setData({ loading: false, error: "no aircraft specified" });
      return;
    }
    this.setData({ identifier });
    this.refresh();
  },

  async refresh() {
    this.setData({ loading: true, error: null });
    try {
      const [details, history, images] = await Promise.all([
        apiFetch(`/api/v1/aircraft/${this.data.identifier}/details`),
        apiFetch(`/api/v1/aircraft/${this.data.identifier}/history?limit=500`),
        apiFetch(`/api/v1/aircraft/${this.data.identifier}/images`),
      ]);
      this.setData({
        details: details.aircraft || details.data || details,
        history: history.history || history.data || [],
        images: images.images || [],
        loading: false,
      });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },
});
