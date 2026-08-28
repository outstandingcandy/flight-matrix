// User filters — CRUD around /api/user/{email}/filters. Mirrors
// user_filters.html but scaled down to the mini-program's form UX.
const { apiFetch } = require("../../utils/request.js");

Page({
  data: {
    email: "",
    filters: [],
    loading: false,
    error: null,
    // New-filter form.
    newName: "",
    newSql: "",
  },

  onLoad() {
    const user = (getApp().globalData || {}).user;
    if (!user || !user.email) {
      this.setData({ error: "未登录" });
      return;
    }
    this.setData({ email: user.email });
    this.refresh();
  },

  async refresh() {
    if (!this.data.email) return;
    this.setData({ loading: true, error: null });
    try {
      const body = await apiFetch(`/api/user/${encodeURIComponent(this.data.email)}/filters`);
      this.setData({ filters: body.filters || [], loading: false });
    } catch (err) {
      this.setData({ error: err.message, loading: false });
    }
  },

  onNameInput(e) {
    this.setData({ newName: e.detail.value });
  },
  onSqlInput(e) {
    this.setData({ newSql: e.detail.value });
  },

  async onCreateTap() {
    if (!this.data.newName || !this.data.newSql) {
      wx.showToast({ title: "请填名字和 SQL", icon: "none" });
      return;
    }
    try {
      await apiFetch(`/api/user/${encodeURIComponent(this.data.email)}/filters`, {
        method: "POST",
        data: { name: this.data.newName, filter_sql: this.data.newSql },
      });
      this.setData({ newName: "", newSql: "" });
      this.refresh();
    } catch (err) {
      wx.showToast({ title: err.message, icon: "none" });
    }
  },

  async onDeleteTap(e) {
    const id = e.currentTarget.dataset.id;
    try {
      await apiFetch(
        `/api/user/${encodeURIComponent(this.data.email)}/filters/${id}`,
        { method: "DELETE" }
      );
      this.refresh();
    } catch (err) {
      wx.showToast({ title: err.message, icon: "none" });
    }
  },
});
