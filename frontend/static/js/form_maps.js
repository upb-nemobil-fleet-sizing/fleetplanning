(function () {
  const DEFAULT_CENTER = [51.1315, 9.2127];
  const DEFAULT_ZOOM = 6;
  const FOCUS_ZOOM = 13;
  const CONTEXT_OPACITY = 0.45;

  const ICON_BASE = "/static/vendor/leaflet-color-markers";
  const SHADOW_URL = "/static/vendor/leaflet-color-markers/marker-shadow.png";
  const ICON_CACHE = {};

  const PRIMARY_COLOR = {
    cab: "blue",
    charging: "green",
    pro: "orange",
    chaining: "violet",
    ride: "red",
    route: "violet",
  };

  /** Create and cache a Leaflet marker icon for the requested color. */
  function createIcon(color) {
    const key = color || "blue";
    if (ICON_CACHE[key]) return ICON_CACHE[key];
    ICON_CACHE[key] = new L.Icon({
      iconUrl: `${ICON_BASE}/marker-icon-${key}.png`,
      iconRetinaUrl: `${ICON_BASE}/marker-icon-2x-${key}.png`,
      shadowUrl: SHADOW_URL,
      iconSize: [25, 41],
      iconAnchor: [12, 41],
      popupAnchor: [1, -34],
      shadowSize: [41, 41],
    });
    return ICON_CACHE[key];
  }

  /** Convert form values to finite numbers while treating empty input as missing. */
  function toNum(v) {
    if (v === null || v === undefined) return null;
    if (typeof v === "string" && v.trim() === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  /** Normalize a point-like object into the internal {lat, lng} shape. */
  function normalizePoint(point) {
    if (!point || typeof point !== "object") return null;
    const lat = toNum(point.lat ?? point.latitude ?? point.Latitude);
    const lng = toNum(point.lng ?? point.longitude ?? point.Longitude);
    if (lat === null || lng === null) return null;
    return { lat, lng };
  }

  /** Normalize a list of point-like objects and discard invalid entries. */
  function normalizePoints(points) {
    if (!Array.isArray(points)) return [];
    return points.map(normalizePoint).filter(Boolean);
  }

  /** Return a safe overlay object for optional contextual map layers. */
  function getOverlay(overlay) {
    if (overlay && typeof overlay === "object") return overlay;
    return {};
  }

  /** Extract normalized operation-area points from overlay data. */
  function getOperationAreaPoints(overlay) {
    return normalizePoints(getOverlay(overlay).operationArea);
  }

  /** Fit a map to one or more points and return whether focusing succeeded. */
  function fitToPoints(map, points, zoomFallback) {
    const pts = normalizePoints(points);
    if (!pts.length) return false;
    if (pts.length === 1) {
      map.setView([pts[0].lat, pts[0].lng], zoomFallback || FOCUS_ZOOM);
      return true;
    }
    map.fitBounds(pts.map((p) => [p.lat, p.lng]), { padding: [30, 30] });
    return true;
  }

  /** Fit create-mode maps tightly to the operation area when it is available. */
  function fitOperationAreaForCreate(map, areaPoints) {
    const pts = normalizePoints(areaPoints);
    if (!pts.length) return false;
    if (pts.length === 1) {
      map.setView([pts[0].lat, pts[0].lng], FOCUS_ZOOM);
      return true;
    }
    map.fitBounds(pts.map((p) => [p.lat, p.lng]), { padding: [8, 8] });
    return true;
  }

  /** Choose the best initial map focus based on create/edit mode, selected points, and operation area. */
  function focusByMode(map, mode, overlay, selectedPoints) {
    const areaPoints = getOperationAreaPoints(overlay);
    if ((mode || "").toLowerCase() === "create") {
      if (fitOperationAreaForCreate(map, areaPoints)) return;
      map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
      return;
    }

    if (fitToPoints(map, selectedPoints, FOCUS_ZOOM)) return;
    if (fitToPoints(map, areaPoints, FOCUS_ZOOM)) return;
    map.setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  }

  /** Draw the operation-area polygon as a muted contextual boundary. */
  function drawOperationArea(map, overlay) {
    const points = getOperationAreaPoints(overlay);
    if (points.length < 3) return;
    L.polygon(
      points.map((p) => [p.lat, p.lng]),
      {
        color: "#4c6f8a",
        weight: 2,
        fillColor: "#a9bfd0",
        fillOpacity: 0.15,
        dashArray: "6 4",
        opacity: 0.6,
      }
    ).addTo(map);
  }

  /** Draw contextual charging, PRO, and chaining layers around the entity being edited. */
  function drawContextLayers(map, overlay, options) {
    const opts = options || {};
    const primaryType = (opts.primaryType || "").toLowerCase();
    const primaryId = String(opts.primaryId || "");

    const chargingIcon = createIcon("green");
    const proIcon = createIcon("orange");
    const chainingIcon = createIcon("violet");

    const charging = Array.isArray(overlay.chargingPoints) ? overlay.chargingPoints : [];
    for (const cp of charging) {
      if (!cp || typeof cp !== "object") continue;
      const id = String(cp.id || "");
      if (primaryType === "charging" && primaryId && id === primaryId) continue;
      const pt = normalizePoint(cp);
      if (!pt) continue;
      L.marker([pt.lat, pt.lng], { icon: chargingIcon, opacity: CONTEXT_OPACITY }).addTo(map);
    }

    const pros = Array.isArray(overlay.pros) ? overlay.pros : [];
    for (const pro of pros) {
      if (!pro || typeof pro !== "object") continue;
      const id = String(pro.id || "");
      if (primaryType === "pro" && primaryId && id === primaryId) continue;
      const pt = normalizePoint(pro);
      if (!pt) continue;
      L.marker([pt.lat, pt.lng], { icon: proIcon, opacity: CONTEXT_OPACITY }).addTo(map);
    }

    const chainingLocations = Array.isArray(overlay.chainingLocations) ? overlay.chainingLocations : [];
    for (const loc of chainingLocations) {
      if (!loc || typeof loc !== "object") continue;
      const id = String(loc.id || "");
      if (primaryType === "chaining" && primaryId && id === primaryId) continue;
      const start = normalizePoint(loc.start);
      const end = normalizePoint(loc.end);
      if (start) {
        L.marker([start.lat, start.lng], { icon: chainingIcon, opacity: CONTEXT_OPACITY }).addTo(map);
      }
      if (end) {
        L.marker([end.lat, end.lng], { icon: chainingIcon, opacity: CONTEXT_OPACITY }).addTo(map);
      }
      if (start && end) {
        L.polyline(
          [
            [start.lat, start.lng],
            [end.lat, end.lng],
          ],
          {
            color: "#9C2BCB",
            opacity: 0.35,
            weight: 2,
            dashArray: "5 5",
          }
        ).addTo(map);
      }
    }
  }

  /** Create a Leaflet base map and attach shared operation-area/context overlays. */
  function createBaseMap(config) {
    const cfg = config || {};
    const mapId = cfg.mapId || "map";
    const map = L.map(mapId).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
    createDashboardTileLayer({
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    const overlay = getOverlay(cfg.overlay);
    drawOperationArea(map, overlay);
    drawContextLayers(map, overlay, cfg);
    return { map, overlay };
  }

  /** Read a latitude/longitude pair from two input fields. */
  function readInputPoint(latInputId, lngInputId) {
    if (!latInputId || !lngInputId) return null;
    const latEl = document.getElementById(latInputId);
    const lngEl = document.getElementById(lngInputId);
    if (!latEl || !lngEl) return null;
    const lat = toNum(latEl.value);
    const lng = toNum(lngEl.value);
    if (lat === null || lng === null) return null;
    return { lat, lng };
  }

  /** Write a normalized point into latitude and longitude input fields. */
  function writeInputPoint(point, latInputId, lngInputId) {
    if (!point || !latInputId || !lngInputId) return;
    const latEl = document.getElementById(latInputId);
    const lngEl = document.getElementById(lngInputId);
    if (!latEl || !lngEl) return;
    latEl.value = point.lat.toFixed(6);
    lngEl.value = point.lng.toFixed(6);
  }

  /** Initialize an editable or read-only map for entities with one location point. */
  function initSinglePointEntityMap(config) {
    const cfg = config || {};
    const { map, overlay } = createBaseMap(cfg);
    const editable = !!cfg.editable;
    const primaryColor = PRIMARY_COLOR[(cfg.primaryType || "").toLowerCase()] || "blue";
    const primaryIcon = createIcon(primaryColor);
    let marker = null;

    /** Create or move the primary marker and optionally refocus the map. */
    function updateMarker(point, refocus) {
      if (!point) return;
      if (!marker) {
        marker = L.marker([point.lat, point.lng], { icon: primaryIcon, draggable: editable }).addTo(map);
        if (editable) {
          marker.on("dragend", () => {
            const pos = marker.getLatLng();
            writeInputPoint({ lat: pos.lat, lng: pos.lng }, cfg.latInputId, cfg.lngInputId);
          });
        }
      } else {
        marker.setLatLng([point.lat, point.lng]);
      }
      if (refocus) {
        map.setView([point.lat, point.lng], FOCUS_ZOOM);
      }
    }

    const initialPoint = normalizePoint({ lat: cfg.initialLat, lng: cfg.initialLng }) || readInputPoint(cfg.latInputId, cfg.lngInputId);
    if (initialPoint) {
      updateMarker(initialPoint, false);
    }

    focusByMode(map, cfg.mode, overlay, initialPoint ? [initialPoint] : []);

    if (editable) {
      const latEl = document.getElementById(cfg.latInputId || "");
      const lngEl = document.getElementById(cfg.lngInputId || "");
      const onInput = () => {
        const p = readInputPoint(cfg.latInputId, cfg.lngInputId);
        if (p) updateMarker(p, true);
      };
      if (latEl) latEl.addEventListener("input", onInput);
      if (lngEl) lngEl.addEventListener("input", onInput);

      map.on("click", (e) => {
        const p = { lat: e.latlng.lat, lng: e.latlng.lng };
        writeInputPoint(p, cfg.latInputId, cfg.lngInputId);
        updateMarker(p, false);
      });
    }
  }

  /** Initialize an editable or read-only map for entities with start and end points. */
  function initDualPointEntityMap(config) {
    const cfg = config || {};
    const { map, overlay } = createBaseMap(cfg);
    const editable = !!cfg.editable;
    const primaryColor = PRIMARY_COLOR[(cfg.primaryType || "").toLowerCase()] || "violet";
    const icon = createIcon(primaryColor);
    const lineColor = cfg.lineColor || (primaryColor === "red" ? "#CB2B3E" : "#9C2BCB");

    let startMarker = null;
    let endMarker = null;
    let activePoint = null;
    const line = L.polyline([], { color: lineColor, weight: 3 }).addTo(map);

    /** Create or move the start marker and synchronize its input fields. */
    function setStart(point) {
      if (!point) return;
      if (!startMarker) {
        startMarker = L.marker([point.lat, point.lng], { icon, draggable: editable }).addTo(map);
        if (editable) {
          startMarker.on("dragend", () => {
            const pos = startMarker.getLatLng();
            writeInputPoint({ lat: pos.lat, lng: pos.lng }, cfg.startLatInputId, cfg.startLngInputId);
            updateLine();
          });
        }
      } else {
        startMarker.setLatLng([point.lat, point.lng]);
      }
      writeInputPoint(point, cfg.startLatInputId, cfg.startLngInputId);
      updateLine();
    }

    /** Create or move the end marker and synchronize its input fields. */
    function setEnd(point) {
      if (!point) return;
      if (!endMarker) {
        endMarker = L.marker([point.lat, point.lng], { icon, draggable: editable }).addTo(map);
        if (editable) {
          endMarker.on("dragend", () => {
            const pos = endMarker.getLatLng();
            writeInputPoint({ lat: pos.lat, lng: pos.lng }, cfg.endLatInputId, cfg.endLngInputId);
            updateLine();
          });
        }
      } else {
        endMarker.setLatLng([point.lat, point.lng]);
      }
      writeInputPoint(point, cfg.endLatInputId, cfg.endLngInputId);
      updateLine();
    }

    /** Redraw the line connecting the current start and end markers. */
    function updateLine() {
      const pts = [];
      if (startMarker) {
        const s = startMarker.getLatLng();
        pts.push([s.lat, s.lng]);
      }
      if (endMarker) {
        const e = endMarker.getLatLng();
        pts.push([e.lat, e.lng]);
      }
      line.setLatLngs(pts);
    }

    const startInitial = normalizePoint({ lat: cfg.startLat, lng: cfg.startLng }) || readInputPoint(cfg.startLatInputId, cfg.startLngInputId);
    const endInitial = normalizePoint({ lat: cfg.endLat, lng: cfg.endLng }) || readInputPoint(cfg.endLatInputId, cfg.endLngInputId);
    if (startInitial) setStart(startInitial);
    if (endInitial) setEnd(endInitial);

    const selected = [];
    if (startInitial) selected.push(startInitial);
    if (endInitial) selected.push(endInitial);
    focusByMode(map, cfg.mode, overlay, selected);

    if (editable) {
      const startBtn = document.getElementById(cfg.startButtonId || "");
      const endBtn = document.getElementById(cfg.endButtonId || "");
      if (startBtn) {
        startBtn.addEventListener("click", () => {
          activePoint = "start";
        });
      }
      if (endBtn) {
        endBtn.addEventListener("click", () => {
          activePoint = "end";
        });
      }

      map.on("click", (e) => {
        const point = { lat: e.latlng.lat, lng: e.latlng.lng };
        if (activePoint === "start") {
          setStart(point);
          return;
        }
        if (activePoint === "end") {
          setEnd(point);
          return;
        }
        if (!startMarker) {
          setStart(point);
        } else if (!endMarker) {
          setEnd(point);
        } else {
          setEnd(point);
        }
      });
    }
  }

  /** Build a lookup from chaining-location id to normalized start/end points. */
  function buildChainingLocationLookup(overlay) {
    const map = {};
    const locations = Array.isArray(overlay.chainingLocations) ? overlay.chainingLocations : [];
    for (const loc of locations) {
      if (!loc || typeof loc !== "object") continue;
      const id = String(loc.id || "");
      if (!id) continue;
      map[id] = {
        start: normalizePoint(loc.start),
        end: normalizePoint(loc.end),
      };
    }
    return map;
  }

  /** Initialize the chain-route map by drawing selected start and end chaining locations. */
  function initChainRouteMap(config) {
    const cfg = config || {};
    const { map, overlay } = createBaseMap(cfg);
    const locationById = buildChainingLocationLookup(overlay);
    const icon = createIcon("violet");
    let startMarker = null;
    let endMarker = null;
    let routeLine = null;

    /** Remove the currently highlighted chain-route markers and line. */
    function clearPrimary() {
      if (startMarker) {
        map.removeLayer(startMarker);
        startMarker = null;
      }
      if (endMarker) {
        map.removeLayer(endMarker);
        endMarker = null;
      }
      if (routeLine) {
        map.removeLayer(routeLine);
        routeLine = null;
      }
    }

    /** Read the selected or fixed start/end chaining-location ids. */
    function getSelectedIds() {
      const startId = cfg.startSelectId ? String((document.getElementById(cfg.startSelectId)?.value || "")) : String(cfg.fixedStartId || "");
      const endId = cfg.endSelectId ? String((document.getElementById(cfg.endSelectId)?.value || "")) : String(cfg.fixedEndId || "");
      return { startId, endId };
    }

    /** Draw the route preview for the currently selected start and end chaining locations. */
    function drawSelected() {
      clearPrimary();
      const { startId, endId } = getSelectedIds();
      const startLoc = locationById[startId]?.start || locationById[startId]?.end || null;
      const endLoc = locationById[endId]?.end || locationById[endId]?.start || null;
      const points = [];
      if (startLoc) {
        startMarker = L.marker([startLoc.lat, startLoc.lng], { icon }).addTo(map);
        points.push(startLoc);
      }
      if (endLoc) {
        endMarker = L.marker([endLoc.lat, endLoc.lng], { icon }).addTo(map);
        points.push(endLoc);
      }
      if (startLoc && endLoc) {
        routeLine = L.polyline(
          [
            [startLoc.lat, startLoc.lng],
            [endLoc.lat, endLoc.lng],
          ],
          { color: "#9C2BCB", weight: 3 }
        ).addTo(map);
      }
      return points;
    }

    const points = drawSelected();
    focusByMode(map, cfg.mode, overlay, points);

    if (cfg.startSelectId) {
      const el = document.getElementById(cfg.startSelectId);
      if (el) {
        el.addEventListener("change", () => {
          const pts = drawSelected();
          if (pts.length) fitToPoints(map, pts, FOCUS_ZOOM);
        });
      }
    }
    if (cfg.endSelectId) {
      const el = document.getElementById(cfg.endSelectId);
      if (el) {
        el.addEventListener("change", () => {
          const pts = drawSelected();
          if (pts.length) fitToPoints(map, pts, FOCUS_ZOOM);
        });
      }
    }
  }

  /** Build a lookup from chain-route GUID/id to its configured start and end locations. */
  function buildChainRouteLookup(overlay) {
    const routes = Array.isArray(overlay.chainRoutes) ? overlay.chainRoutes : [];
    const lookup = {};
    for (const route of routes) {
      if (!route || typeof route !== "object") continue;
      const guid = String(route.guid || "");
      const id = String(route.id || "");
      const entry = {
        startLocation: String(route.startLocation || ""),
        endLocation: String(route.endLocation || ""),
      };
      if (guid) lookup[guid] = entry;
      if (id && !lookup[id]) lookup[id] = entry;
    }
    return lookup;
  }

  /** Initialize the chain-route-schedule map by drawing the currently selected chain route. */
  function initChainRouteScheduleMap(config) {
    const cfg = config || {};
    const { map, overlay } = createBaseMap(cfg);
    const locationById = buildChainingLocationLookup(overlay);
    const routeByGuid = buildChainRouteLookup(overlay);
    const icon = createIcon("violet");
    let startMarker = null;
    let endMarker = null;
    let routeLine = null;

    /** Remove highlighted route-schedule markers and line from the map. */
    function clearPrimary() {
      if (startMarker) {
        map.removeLayer(startMarker);
        startMarker = null;
      }
      if (endMarker) {
        map.removeLayer(endMarker);
        endMarker = null;
      }
      if (routeLine) {
        map.removeLayer(routeLine);
        routeLine = null;
      }
    }

    /** Read the selected or fixed chain-route GUID for this schedule form. */
    function getSelectedRouteGuid() {
      if (cfg.chainRouteSelectId) {
        return String((document.getElementById(cfg.chainRouteSelectId)?.value || ""));
      }
      return String(cfg.fixedChainRouteGuid || "");
    }

    /** Draw the route-schedule preview for the selected chain route. */
    function drawSelectedRoute() {
      clearPrimary();
      const routeGuid = getSelectedRouteGuid();
      const route = routeByGuid[routeGuid];
      if (!route) return [];
      const startLoc = locationById[route.startLocation]?.start || locationById[route.startLocation]?.end || null;
      const endLoc = locationById[route.endLocation]?.end || locationById[route.endLocation]?.start || null;
      const points = [];
      if (startLoc) {
        startMarker = L.marker([startLoc.lat, startLoc.lng], { icon }).addTo(map);
        points.push(startLoc);
      }
      if (endLoc) {
        endMarker = L.marker([endLoc.lat, endLoc.lng], { icon }).addTo(map);
        points.push(endLoc);
      }
      if (startLoc && endLoc) {
        routeLine = L.polyline(
          [
            [startLoc.lat, startLoc.lng],
            [endLoc.lat, endLoc.lng],
          ],
          { color: "#9C2BCB", weight: 3 }
        ).addTo(map);
      }
      return points;
    }

    const points = drawSelectedRoute();
    focusByMode(map, cfg.mode, overlay, points);

    if (cfg.chainRouteSelectId) {
      const el = document.getElementById(cfg.chainRouteSelectId);
      if (el) {
        el.addEventListener("change", () => {
          const pts = drawSelectedRoute();
          if (pts.length) fitToPoints(map, pts, FOCUS_ZOOM);
        });
      }
    }
  }

  window.initSinglePointEntityMap = initSinglePointEntityMap;
  window.initDualPointEntityMap = initDualPointEntityMap;
  window.initChainRouteMap = initChainRouteMap;
  window.initChainRouteScheduleMap = initChainRouteScheduleMap;
})();
