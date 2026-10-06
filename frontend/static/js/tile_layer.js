// Builds the tile layer used by every Leaflet map on the dashboard, honoring the tile source the
// backend picked (see maptiles_manager.py, exposed as window.TILE_URL_TEMPLATE via base.html).
// The primary URL is either the public OpenStreetMap servers or the dashboard's own /tiles route,
// which forwards to the local tile server. In TILE_SOURCE=fallback mode the primary is the
// /tiles route and the backend also sets window.TILE_URL_FALLBACK to the public OpenStreetMap
// servers: any individual tile the local tile server cannot provide is retried once against
// them. If that fails as well, for example without internet access, the tile stays empty.
function createDashboardTileLayer(options) {
  const primaryUrl = window.TILE_URL_TEMPLATE || "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const fallbackUrl = window.TILE_URL_FALLBACK;
  const layer = L.tileLayer(primaryUrl, options);

  if (fallbackUrl) {
    layer.on("tileerror", function (e) {
      if (e.tile._dashboardFallbackTried) return;
      e.tile._dashboardFallbackTried = true;
      // The public URL template has a subdomain placeholder that Leaflet fills only for the
      // primary URL.
      const data = Object.assign({ s: "abc"[(e.coords.x + e.coords.y) % 3] }, e.coords);
      e.tile.src = L.Util.template(fallbackUrl, data);
    });
  }

  return layer;
}
