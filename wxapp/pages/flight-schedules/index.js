// Flight schedules — list view of /api/flight-schedules with the
// filter surface /api/flight-schedules/filter-options provides.
// The web version has a heavy map + list dual tab; the wxapp starts
// with the list and grows the map tab in a follow-up.
const { apiFetch } = require("../../utils/request.js");

Page({
  data: {
    airport: "PEK",
    flightType: "",
    schedules: [],
    filters: { airports: [], aircraft_types: [], liveries: [], available_dates: [] },
    loading: false,
    error: null,
  },

  onLoad() {
    this.loadFilters();
    this.refresh();
  },

  async loadFilters() {
    try {
      const filters = await apiFetch(
        `/api/v1/flight-schedules/filter-options?airport=${this.data.airport}`
      );
      this.setData({ filters });
    } catch (err) {
      console.warn("filter-options failed:", err.message);
    }
  },

  async refresh() {
    if (!this.data.airport) return;
    this.setData({ loading: true, error: null });
    try {
      const params = new URLSearchParams({
        airport: this.data.airport,
        limit: "200",
      });
      if (this.data.flightType) params.append("flight_type", this.data.flightType);
      const body = await apiFetch(`/api/v1/flight-schedules?${params.toString()}`);
      this.setData({
        schedules: body.schedules || [],
        loading: false,
      });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },

  onAirportChange(e) {
    this.setData({ airport: (e.detail.value || "").toUpperCase() });
  },

  onSearchTap() {
    this.refresh();
    this.loadFilters();
  },

  onFilterTypeChange(e) {
    const idx = e.detail.value;
    const options = ["", "arrival", "departure"];
    this.setData({ flightType: options[idx] || "" });
    this.refresh();
  },
});
