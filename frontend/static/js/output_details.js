let map = null;
let mapLayers = [];
let mapDisplayMode = "all"; 
let fleetColorMap = {};
let latestVehiclePayload = null;
let fleetGanttData = [];
let selectedTripGuids = new Set();
let hiddenTripGuids = new Set();

/** Append the demo scenario query string to detail API URLs when present. */
function withOutputDataQuery(path) {
  const query = window.outputDataQuery || "";
  if (!query) return path;
  return `${path}${path.includes("?") ? "&" + query.slice(1) : query}`;
}

const typeColor = {
  to_pickup: "#1f77b4",
  customer_trip: "#2ca02c",
  to_depot: "#7f7f7f",
  depot_idle: "#7f7f7f",
  idle_cab: "#d0d0d0",
  to_charging: "#9fa37d",
  charging_idle: "#d8db11da",
  pro_trip: "#17a2b8",
  pro_start: "#7f7f7f",
  pro_end: "#7f7f7f",
  pro_coupling: "#ff8c00",
  pro_decoupling: "#8b6cff",
  convoy_couple: "#f5a623",
  convoy_decouple: "#f5a623",
  convoy_link: "#f5a623",
  convoy_travel: "#b255bb",
  convoy_travel_empty: "#1f9fff",
  unknown: "#ff7f0e"
};

const typeLabel = {
  customer_trip: "Kundenfahrt",
  to_pickup: "Anfahrt zu Kunde",
  to_depot: "Rückfahrt zu Depot",
  to_charging: "Servicefahrt",
  charging_idle: "Ladeprozess",
  depot_idle: "Depot",
  idle_cab: "Keine Kundenfahrten",
  pro_trip: "Pro-Fahrt",
  pro_start: "Pro Start",
  pro_end: "Pro Ende",
  pro_coupling: "Ankopplung",
  pro_decoupling: "Abkopplung",
  convoy_couple: "Konvoi Ankopplung",
  convoy_decouple: "Konvoi Abkopplung",
  convoy_link: "An-/Abkopplung",
  convoy_travel: "Konvoi Fahrt",
  convoy_travel_empty: "Konvoi Fahrt (ohne Kunde)",
  unknown: "Unbekannt"
};

/** Collapse separate convoy couple/decouple events into the shared display type. */
function normalizeType(t) {
  if (t === "convoy_couple" || t === "convoy_decouple") return "convoy_link";
  return t || "unknown";
}

let loadingCounter = 0;
const loadingOverlay = () => document.getElementById("loadingOverlay");
const loadingOverlayText = () => document.getElementById("loadingOverlayText");

/** Detect whether a Gantt trip belongs to a PRO vehicle or convoy operation. */
function isProTrip(trip) {
  if (!trip) return false;
  if (trip.vehicleType === "pro") return true;
  if (typeof trip.type === "string" && (trip.type.startsWith("pro_") || trip.type.startsWith("convoy_"))) return true;
  if (typeof trip.Vehicle === "string" && trip.Vehicle.toLowerCase().startsWith("pro")) return true;
  return false;
}

/** Resolve the route color for a fleet trip based on vehicle and trip type. */
function getTripColor(trip) {
  if (isProTrip(trip)) {
    return typeColor[trip.type] || typeColor.pro_trip;
  }
  return fleetColorMap[trip.Vehicle] || "#000";
}

const FLEET_VISIBLE_ROWS = 5;
const FLEET_ROW_HEIGHT = 90;
const FLEET_MARGIN = { l: 120, r: 40, t: 60, b: 60 };

/** Toggle the shared loading overlay while asynchronous detail data is being fetched or rendered. */
function setLoadingState(isLoading, text) {
  const overlay = loadingOverlay();
  if (!overlay) return;

  if (isLoading) {
    loadingCounter += 1;
    if (text && loadingOverlayText()) {
      loadingOverlayText().textContent = text;
    }
    overlay.style.display = "block";
    return;
  }

  loadingCounter = Math.max(loadingCounter - 1, 0);
  if (loadingCounter === 0) {
    overlay.style.display = "none";
  }
}

/** Initialize the Leaflet map used for fleet and vehicle route visualization. */
function initMap() {
  if (map) return;
  map = L.map('map').setView([51.0, 9.0], 6);
  createDashboardTileLayer({
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(map);
}

/** Remove all current Leaflet overlays from the map. */
function clearMap() {
  mapLayers.forEach(layer => map.removeLayer(layer));
  mapLayers = [];
}

/** Add a colored route polyline with popup metadata and a directional arrow marker. */
function addPolyline(coords, color, meta = {}) {
  const line = L.polyline(coords, { color, weight: 4, opacity: 0.9 }).addTo(map);
  mapLayers.push(line);

  const { vehicle, start, end, energyStart, energyEnd } = meta;
  const popupParts = [];
  if (vehicle) popupParts.push(`<strong>${vehicle}</strong>`);
  if (start && end) popupParts.push(`Zeitraum:<br>${start} - <br>${end}`);
  if (energyStart !== undefined && energyEnd !== undefined) {
    popupParts.push(`Restenergie: ${energyStart} ➜ ${energyEnd} Wh`);
  }
  if (popupParts.length) {
    line.bindPopup(popupParts.join("<br><br>"));
  }

  if (coords.length < 2) return;
  const prev = coords[coords.length - 2];
  const last = coords[coords.length - 1];
  const angle = getBearingAngle(prev, last);
  const arrowIcon = createArrowIcon(color, angle);
  const marker = L.marker(last, { icon: arrowIcon, interactive: false }).addTo(map);
  mapLayers.push(marker);
  return line;
}

/** Create a small rotated SVG arrow icon for the end of a route polyline. */
function createArrowIcon(color, angle) {
  const svg = `
    <svg width="18" height="18" viewBox="0 0 24 24"
      xmlns="http://www.w3.org/2000/svg"
      style="transform: rotate(${angle}deg)">
      <polygon points="12,2 22,22 2,22" fill="${color}" />
    </svg>
  `;
  return L.divIcon({
    html: svg,
    className: "",
    iconSize: [18, 18],
    iconAnchor: [9, 11]
  });
}

/** Calculate the compass bearing from the previous point to the final route point. */
function getBearingAngle(prev, last) {
  const lat1 = prev[0] * Math.PI / 180;
  const lon1 = prev[1] * Math.PI / 180;
  const lat2 = last[0] * Math.PI / 180;
  const lon2 = last[1] * Math.PI / 180;
  const y = Math.sin(lon2 - lon1) * Math.cos(lat2);
  const x = Math.cos(lat1)*Math.sin(lat2) -
            Math.sin(lat1)*Math.cos(lat2)*Math.cos(lon2 - lon1);
  let brng = Math.atan2(y, x);
  brng = brng * 180 / Math.PI;
  brng = (brng + 360) % 360;
  return brng;
}

/** Fetch the full fleet Gantt payload for one fleet-size configuration. */
async function fetchGanttFull(numCabs) {
  try {
    setLoadingState(true, "Flottenkonfigurations-Daten werden geladen …");
    const res = await fetch(withOutputDataQuery(`/output/details/gantt/${numCabs}`));
    if (!res.ok) {
      const txt = await res.text().catch(() => '');
      throw new Error(`Fehler beim Laden des Flotten-Gantt: ${res.status} ${txt}`);
    }
    const data = await res.json();
    return data;
  } finally {
    setLoadingState(false);
  }
}

/** Render the fleet-level Gantt chart and wire vehicle row clicks to the detail panel. */
function renderFleetGantt(data) {
  return new Promise((resolve) => {
    if (!Array.isArray(data) || data.length === 0) {
      document.getElementById("ganttChart").innerHTML =
        "<p class='text-center text-muted mt-4'>Keine Fahrten gefunden.</p>";
      resolve();
      return;
    }

    const traces = [];
    const vehicleMeta = {};
    data.forEach(d => {
      if (!d || !d.Vehicle) return;
      if (!vehicleMeta[d.Vehicle]) {
        if (d.vehicleType) vehicleMeta[d.Vehicle] = d.vehicleType;
        else if (String(d.Vehicle).toLowerCase().startsWith("pro")) vehicleMeta[d.Vehicle] = "pro";
        else vehicleMeta[d.Vehicle] = "cab";
      }
    });

    const vehicles = Object.keys(vehicleMeta).sort((a, b) => {
      const ta = vehicleMeta[a] === "pro" ? 1 : 0;
      const tb = vehicleMeta[b] === "pro" ? 1 : 0;
      if (ta !== tb) return ta - tb;
      const na = parseInt(String(a).replace(/\D+/g, ""));
      const nb = parseInt(String(b).replace(/\D+/g, ""));
      if (!Number.isNaN(na) && !Number.isNaN(nb)) return nb - na;
      return String(a).localeCompare(String(b));
    });

    fleetColorMap = {};
    /** Generate visually distinct HSL colors for CAB rows in the fleet Gantt chart. */
    function generateColor(i, count) {
      const hue = (i * 360 / count) % 360;
      return `hsl(${hue}, 70%, 50%)`;
    }

    vehicles.forEach((vehicle, i) => {
      fleetColorMap[vehicle] = generateColor(i, vehicles.length);
    });

    vehicles.forEach(vehicle => {
      const isPro = vehicleMeta[vehicle] === "pro";
      const trips = data.filter(d => d.Vehicle === vehicle && (isPro || d.type === "customer_trip" || d.type === "idle_cab"));
      const x0 = trips.map(t => new Date(t.Start));
      const x1 = trips.map(t => new Date(t.Finish));
      const durations = x1.map((e, i) => e - x0[i]);
      const customdata = trips.map(t => [t.Start, t.Finish, t.Task, vehicle, (typeLabel[t.type] || t.type)]);
      const colors = trips.map(t => (isPro ? (typeColor[t.type] || typeColor.pro_trip)
        : (t.type === "idle_cab" ? typeColor.idle_cab : (fleetColorMap[vehicle] || "#000"))));
      traces.push({
        x: durations,
        y: trips.map(() => vehicle),
        base: x0,
        orientation: "h",
        type: "bar",
        name: vehicle,
        text: trips.map(t => (t.type === "idle_cab" ? "-" : t.Task)),
        customdata,
        marker: { color: colors },
        hovertemplate:
          `<b>${vehicle}</b><br>` +
          `Typ: %{customdata[4]}<br>` +
          `Trip: %{text}<br>` +
          `Start: %{customdata[0]}<br>` +
          `Ende: %{customdata[1]}<extra></extra>`
      });
    });

    const totalRows = Math.max(vehicles.length, 1);
    const visibleRows = Math.min(totalRows, FLEET_VISIBLE_ROWS);
    const plotHeight = FLEET_MARGIN.t + FLEET_MARGIN.b + (totalRows * FLEET_ROW_HEIGHT);
    const viewportHeight = FLEET_MARGIN.t + FLEET_MARGIN.b + (visibleRows * FLEET_ROW_HEIGHT);

    const wrap = document.getElementById("ganttChartWrap");
    if (wrap) {
      wrap.style.height = `${viewportHeight}px`;
      wrap.style.overflowY = totalRows > visibleRows ? "auto" : "visible";
    }

    const layout = {
      title: "Fahrtenübersicht (Flotte)",
      barmode: "stack",
      xaxis: { title: "Zeit", type: "date" },
      yaxis: {
        title: "Fahrzeug",
        automargin: true,
        categoryorder: "array",
        categoryarray: vehicles
      },
      height: plotHeight,
      margin: { ...FLEET_MARGIN }
    };

    Plotly.newPlot("ganttChart", traces, layout, { responsive: true, scrollZoom: false })
      .then(gd => {
        gd.on("plotly_hover", () => gd.style.cursor = "pointer");
        gd.on("plotly_unhover", () => gd.style.cursor = "default");
        gd.on("plotly_click", ev => {
          if (!ev.points?.length) return;
          const cd = ev.points[0].customdata;
          const vehicle = cd[3];
          loadVehicleDetails(vehicle).then(() => {
            showVehicleTripsOnMap(latestVehiclePayload);
          });
        });
        resolve();
      });
  });
}

/** Fetch and render the detailed payload for one selected vehicle. */
async function loadVehicleDetails(vehicleId) {
  const num = parseInt(document.body.dataset.numCabs || "0", 10);
  setLoadingState(true, `Fahrzeug ${vehicleId} wird geladen …`);
  try {
    const res = await fetch(withOutputDataQuery(`/output/details/gantt/vehicle/${num}/${vehicleId}`));
    if (!res.ok) throw new Error("Fehler beim Laden der Fahrzeugdetails");
    const payload = await res.json();
    latestVehiclePayload = payload;
    selectedTripGuids = new Set();
    renderVehicleDetails(payload);
    showVehicleTripsOnMap(payload);
    showVehicleDetailsPanel();
  } finally {
    setLoadingState(false);
  }
}

/** Expand the vehicle detail panel and resize the nested Plotly chart. */
function showVehicleDetailsPanel() {
  const el = document.getElementById("vehicleDetails");
  el.style.display = "block";
  requestAnimationFrame(() => {
    el.style.maxHeight = el.scrollHeight + "px";
    el.style.opacity = "1";
    resizeVehicleGantt();
  });
}

/** Collapse the vehicle detail panel and restore the fleet map when possible. */
function hideVehicleDetailsPanel() {
  const el = document.getElementById("vehicleDetails");
  el.style.maxHeight = "0";
  el.style.opacity = "0";
  setTimeout(() => {
    el.style.display = "none";
    const gd = document.getElementById("vehicleGantt");
    if (gd) Plotly.purge(gd);
    
    if (fleetGanttData.length) {
      showAllTripsOnMap(fleetGanttData);
    }
  }, 350);
}

/** Render one vehicle's KPI summary, Gantt chart, energy timeline, and click interactions. */
function renderVehicleDetails(payload) {
  const vid = payload.vehicleId;
  document.getElementById("vehicleTitle").textContent = `Fahrzeug: ${vid}`;

  const k = payload.kpis || {};

  const totalDistance = k.total_vehicle_distance_m ?? k.totalDistance ?? 0;
  const totalDrivingTime = k.total_vehicle_driving_time_s ?? k.totalDrivingTime ?? 0;
  const totalConsumedEnergy = k.total_consumed_energy_wh ?? k.totalConsumedEnergy ?? 0;
  const remainingEnergy = k.remainingEnergyLastDepot ?? "n/a";
  const totalChainedCabs = k.totalChainedCabs ?? 0;
  const transportedCustomers = k.total_customer_trips ?? k.transportedCustomers ?? 0;
  const convoyTrips = k.total_empty_reposition_trips ?? k.convoyTrips ?? 0;
  const convoyDistance = k.total_empty_reposition_distance_m ?? k.convoyDistance ?? 0;
  const stationaryChargedEnergy = k.total_stationary_charging_energy_wh ?? k.stationaryChargedEnergy ?? 0;
  const convoyChargedEnergy = k.total_pro_charging_energy_wh ?? k.convoyChargedEnergy ?? 0;
  const stationaryChargingTime = k.total_charging_stationary_time_s ?? k.stationaryChargingTime ?? 0;
  const utilization = k.average_utilization ?? k.utilization ?? 0;
  const idleTimeSeconds = k.idle_time_seconds ?? k.idleTimeSeconds ?? 0;
  const idleRatio = k.idle_ratio ?? k.idleRatio ?? 0;
  const customerVsEmptyRatio = k.customer_vs_empty_ratio ?? k.customerVsEmptyRatio ?? 0;
  const totalTrips = k.totalTrips ?? 0;
  const emptyTrips = k.emptyTrips ?? 0;
  const emptyTripRatio = k.emptyTripRatio ?? 0;
  const maxConvoyLength = k.maxConvoyLength ?? 0;
  const chainingServiceTime = k.chainingServiceTime ?? 0;
  const energyProvidedToCabs = k.energyProvidedToCabs ?? 0;
  const isProVehicle = String(payload.vehicleType || "").toLowerCase() === "pro";

  document.getElementById("vehicleInfo").innerHTML = `
    <div class="row text-center">
      <div class="col-md-3">
        <strong>Gesamtstrecke</strong>
        <div>${Math.round(totalDistance)} m</div>
      </div>
      <div class="col-md-3">
        <strong>Fahrtzeit</strong>
        <div>${formatDuration(totalDrivingTime)}</div>
      </div>
      ${!isProVehicle ? `
        <div class="col-md-3">
          <strong>Verbrauchte Energie</strong>
          <div>${Math.round(totalConsumedEnergy)} Wh</div>
        </div>
        <div class="col-md-3">
          <strong>Restenergie</strong>
          <div>${remainingEnergy} Wh</div>
        </div>
      ` : ``}
      ${isProVehicle ? `
        <div class="col-md-3">
          <strong>Beförderte Cabs</strong>
          <div>${Math.round(totalChainedCabs)}</div>
        </div>
        <div class="col-md-3">
          <strong>Anzahl Fahrten</strong>
          <div>${Math.round(totalTrips)}</div>
        </div>
      ` : ``}
    </div>
    ${isProVehicle ? `
    <div class="row text-center mt-4">
      <div class="col-md-3">
        <strong>Leerfahrten</strong>
        <div>${Math.round(emptyTrips)}</div>
      </div>
      <div class="col-md-3">
        <strong>Leerfahrtenanteil</strong>
        <div>${formatPercent(emptyTripRatio)}</div>
      </div>
      <div class="col-md-3">
        <strong>Max. Konvoilänge</strong>
        <div>${Math.round(maxConvoyLength)}</div>
      </div>
      <div class="col-md-3">
        <strong>Kopplungszeit</strong>
        <div>${formatDuration(chainingServiceTime)}</div>
      </div>
    </div>
    <div class="row text-center mt-4">
      <div class="col-md-3">
        <strong>An Cabs abgegebene Energie</strong>
        <div>${Math.round(energyProvidedToCabs)} Wh</div>
      </div>
    </div>
    ` : ``}
    ${!isProVehicle ? `
    <div class="row text-center mt-4">
      <div class="col-md-3">
        <strong>Beförderte Kunden</strong>
        <div>${Math.round(transportedCustomers)}</div>
      </div>
      <div class="col-md-3">
        <strong>Konvoifahrten</strong>
        <div>${Math.round(convoyTrips)}</div>
      </div>
      <div class="col-md-3">
        <strong>Im Konvoi zurückgelegte Strecke</strong>
        <div>${Math.round(convoyDistance)} m</div>
      </div>
      <div class="col-md-3">
        <strong>Zeit für stationäres Laden</strong>
        <div>${formatDuration(stationaryChargingTime)}</div>
      </div>
    </div>
    <div class="row text-center mt-4">
      <div class="col-md-3">
        <strong>Geladene Energie stationär</strong>
        <div>${Math.round(stationaryChargedEnergy)} Wh</div>
      </div>
      <div class="col-md-3">
        <strong>Geladene Energie Konvoi</strong>
        <div>${Math.round(convoyChargedEnergy)} Wh</div>
      </div>
      <div class="col-md-3">
        <strong>Utilization</strong>
        <div>${formatPercent(utilization)}</div>
      </div>
      <div class="col-md-3">
        <strong>Inaktivzeit</strong>
        <div>${formatDuration(idleTimeSeconds)} (${formatPercent(idleRatio)})</div>
      </div>
    </div>
    <div class="row text-center mt-4">
      <div class="col-md-3">
        <strong>Verhältnis Kundenfahrten - Leerfahrten</strong>
        <div>${formatPercent(customerVsEmptyRatio)}</div>
      </div>
    </div>
    ` : ``}
  `;

  const energyPoints = (payload.energyTimeline || [])
    .map(p => ({
      time: new Date(p.time),
      energy: p.energy
    }))
    .filter(p => p.energy !== null)
    .sort((a, b) => a.time - b.time);

  const trips = payload.trips || [];
  if (!trips.length) {
    document.getElementById("vehicleGantt").innerHTML =
      "<p class='text-center text-muted'>Keine Fahrten.</p>";
    return;
  }

  const types = [...new Set(trips.map(t => normalizeType(t.type)))];
  const traces = types.map(type => {
    const pts = trips.filter(t => normalizeType(t.type) === type);
    const x0 = pts.map(p => new Date(p.start));
    const x1 = pts.map(p => new Date(p.end));
    const durations = x1.map((e, i) => e - x0[i]);

    const customdata = pts.map(p => [
      p.start,
      p.end,
      p.emptyLegGuid || p.tripGuid || p.userGuid || p.key || "",
      vid,
      p.proVehicle || ""
    ]);
    const showPro = type === "convoy_travel" || type === "convoy_travel_empty";

    return {
      x: durations,
      y: pts.map(() => vid),
      base: x0,
      type: "bar",
      orientation: "h",
      name: typeLabel[type] || typeLabel.unknown,
      customdata,
      text: pts.map(p => p.label || p.tripGuid || p.userGuid),
      marker: { color: typeColor[type] || typeColor.unknown },
      hovertemplate:
        `<b>${vid}</b><br>` +
        `Fahrt: ${typeLabel[type] || typeLabel.unknown}<br>` +
        (showPro ? `Pro: %{customdata[4]}<br>` : ``) +
        `Trip: %{customdata[2]}<br>` +
        `Start: %{customdata[0]}<br>` +
        `Ende: %{customdata[1]}<extra></extra>`
    };
  });

  if (energyPoints.length) {
    traces.push({
      x: energyPoints.map(p => p.time),
      y: energyPoints.map(p => p.energy),
      mode: "lines+markers",
      name: "Restenergie",
      line: { color: "#d62728", width: 2 },
      marker: { size: 5, color: "#d62728" },
      yaxis: "y2"
    });
  }

  const layout = {
    title: `Fahrten von ${vid}`,
    barmode: "stack",

    xaxis: { type: "date" },
    yaxis: {automargin: true},
    yaxis2: {
      title: "Restenergie (Wh)",
      overlaying: "y",
      side: "right",
      showgrid: false,
      zeroline: false,
      titlefont: { color: "#d62728" },
      tickfont: { color: "#d62728" }
    },

    legend: {
      orientation: "h",
      x: 0,
      y: -0.2,
      xanchor: "left",
      yanchor: "top"
    },
    height: 360,
    margin: { l: 120, r: 90, t: 60, b: 40 }
  };

  Plotly.newPlot("vehicleGantt", traces, layout, { responsive: true })
  .then(gd => {
    gd.on("plotly_hover", () => gd.style.cursor = "pointer");
    gd.on("plotly_unhover", () => gd.style.cursor = "default");

    gd.on("plotly_click", ev => {
      if (!ev.points?.length) return;

      const cd = ev.points[0].customdata;
      if (!cd || !cd[2]) return;

      const tripGuid = String(cd[2]);
      toggleTripVisibility(tripGuid);
    });
  });
}

/** Draw every fleet trip and depot marker on the map for the overview state. */
function showAllTripsOnMap(data) {
  clearMap();
  const bounds = [];

  data.forEach(trip => {
    if (!trip.coords) return;
    const coords = trip.coords.map(p => [p.lat, p.lng]);
    if (coords.length < 2) return;
    const color = getTripColor(trip);
    addPolyline(coords, color, {
      vehicle: trip.Vehicle,
      start: trip.Start,
      end: trip.Finish,
      energyStart: trip.remainingEnergyStart,
      energyEnd: trip.remainingEnergyEnd
    });
    coords.forEach(c => bounds.push(c));
  });

  const depotsShown = new Set();
  data.forEach(trip => {
    if (trip.type === "depot_idle" && trip.coords?.length > 0) {
      const d = trip.coords[0];
      const key = `${d.lat}_${d.lng}`;
      if (!depotsShown.has(key)) {
        depotsShown.add(key);
        L.circleMarker([d.lat, d.lng], {
            radius: 8,
            color: '#7f7f7f',
            fillColor: '#7f7f7f',
            fillOpacity: 1,
            weight: 2
        }).addTo(map).bindPopup(`Depot - ${trip.Vehicle || ""}`);
      }
    }
  });

  if (bounds.length) map.fitBounds(bounds);
}

/** Draw all trips for the currently selected vehicle on the map. */
function showVehicleTripsOnMap(payload) {
  clearMap();
  const trips = Array.isArray(payload.trips) ? payload.trips : [];
  if (!trips.length) return;

  const bounds = [];
  trips.forEach(tr => {
    if (!tr.coords || tr.coords.length < 2) return;
    const coords = tr.coords.map(c => [c.lat, c.lng]).filter(c => c[0] && c[1]);
    if (coords.length < 2) return;
    const color = typeColor[tr.type] || typeColor.unknown;
    const startTime = tr.start || tr.Start;
    const endTime = tr.end || tr.Finish;
    addPolyline(coords, color, {
      vehicle: payload.vehicleId,
      start: startTime,
      end: endTime,
      energyStart: tr.remainingEnergyStart,
      energyEnd: tr.remainingEnergyEnd
    });
    coords.forEach(c => bounds.push(c));
  });

  if (payload.depotLocation) {
    const d = payload.depotLocation;
    L.circleMarker([d.lat, d.lng], {
        radius: 8,
        color: '#7f7f7f',
        fillColor: '#7f7f7f',
        fillOpacity: 1,
        weight: 2
    }).addTo(map).bindPopup(`Depot - ${payload.vehicleId || ""}`);
  }

  if (bounds.length) map.fitBounds(bounds);
}

/** Toggle one trip between visible, hidden, or selected states depending on the current map mode. */
function toggleTripVisibility(tripGuid) {
  if (!latestVehiclePayload || !tripGuid) return;

  tripGuid = String(tripGuid);

  if (mapDisplayMode === "all") {
    if (hiddenTripGuids.has(tripGuid)) {
      hiddenTripGuids.delete(tripGuid);
    } else {
      hiddenTripGuids.add(tripGuid);
    }

    redrawMapByState();
    updateGanttOpacity();
    return;
  }

  if (mapDisplayMode === "none") {
    mapDisplayMode = "selection";
    selectedTripGuids.add(tripGuid);
    redrawMapByState();
    updateGanttOpacity();
    return;
  }

  if (selectedTripGuids.has(tripGuid)) {
    selectedTripGuids.delete(tripGuid);
  } else {
    selectedTripGuids.add(tripGuid);
  }

  if (selectedTripGuids.size === 0) {
    mapDisplayMode = "none";
    clearMap();
  } else {
    redrawMapByState();
  }

  updateGanttOpacity();
}

/** Redraw the map with only the selected vehicle trips visible. */
function showSelectedTripsOnMap() {
  clearMap();
  const trips = latestVehiclePayload?.trips || [];
  const bounds = [];

  trips.forEach(tr => {
    const guid =
      tr.emptyLegGuid ||
      tr.tripGuid ||
      tr.Task ||
      tr.userGuid ||
      tr.key;
    if (!guid) return;
    if (!selectedTripGuids.has(String(guid))) return;
    if (!tr.coords || tr.coords.length < 2) return;
    const coords = tr.coords.map(c => [c.lat, c.lng]).filter(c => c[0] && c[1]);
    if (coords.length < 2) return;
    const color = typeColor[tr.type] || typeColor.unknown;
    const startTime = tr.start || tr.Start;
    const endTime = tr.end || tr.Finish;
    addPolyline(coords, color, {
      vehicle: latestVehiclePayload?.vehicleId,
      start: startTime,
      end: endTime,
      energyStart: tr.remainingEnergyStart,
      energyEnd: tr.remainingEnergyEnd
    });
    coords.forEach(c => bounds.push(c));
  });

  if (latestVehiclePayload.depotLocation) {
    const d = latestVehiclePayload.depotLocation;
    L.circleMarker([d.lat, d.lng], {
        radius: 8,
        color: '#7f7f7f',
        fillColor: '#7f7f7f',
        fillOpacity: 1,
        weight: 2
    }).addTo(map).bindPopup(`Depot - ${latestVehiclePayload.vehicleId || ""}`);
  }

  if (bounds.length) map.fitBounds(bounds);
}

document.addEventListener("DOMContentLoaded", () => {
  initMap();
  const btn = document.getElementById("hideVehicleDetails");
  if (btn) btn.addEventListener("click", hideVehicleDetailsPanel);

  const numCabs = parseInt(document.body.dataset.numCabs || "0", 10);
  fetchGanttFull(numCabs)
    .then(data => {
      fleetGanttData = data;
      return renderFleetGantt(data);
    })
    .then(() => {
      showAllTripsOnMap(fleetGanttData);
    })
    .catch(err => console.error(err));
});

document.getElementById("toggleMapTrips")?.addEventListener("click", () => {
  if (mapDisplayMode === "all") {
    mapDisplayMode = "none";
    selectedTripGuids.clear();
    hiddenTripGuids.clear();
    clearMap();
    updateGanttOpacity();
    setToggleButtonText();
    return;
  }

  mapDisplayMode = "all";
  selectedTripGuids.clear();
  hiddenTripGuids.clear();

  if (latestVehiclePayload) {
    showVehicleTripsOnMap(latestVehiclePayload);
  } else {
    showAllTripsOnMap(fleetGanttData);
  }

  updateGanttOpacity();
  setToggleButtonText();
});

/** Update the map visibility toggle label to match the current display mode. */
function setToggleButtonText() {
  const btn = document.getElementById("toggleMapTrips");
  if (!btn) return;
  btn.textContent =
    mapDisplayMode === "all"
      ? "Alle Fahrten ausblenden"
      : "Alle Fahrten einblenden";
}

/** Rebuild map layers from the current vehicle payload and trip visibility state. */
function redrawMapByState() {
  clearMap();

  const trips = latestVehiclePayload?.trips || [];
  const bounds = [];

  trips.forEach(tr => {
    const guid =
      tr.emptyLegGuid ||
      tr.tripGuid ||
      tr.userGuid ||
      tr.key;

    if (!guid) return;

    if (hiddenTripGuids.has(String(guid))) return;

    if (
      mapDisplayMode === "selection" &&
      !selectedTripGuids.has(String(guid))
    ) {
      return;
    }

    if (!tr.coords || tr.coords.length < 2) return;

    const coords = tr.coords
      .map(c => [c.lat, c.lng])
      .filter(c => c[0] && c[1]);

    if (coords.length < 2) return;

    const color = typeColor[tr.type] || typeColor.unknown;

    addPolyline(coords, color, {
      vehicle: latestVehiclePayload.vehicleId,
      start: tr.start,
      end: tr.end
    });

    coords.forEach(c => bounds.push(c));
  });

  drawDepot(latestVehiclePayload);

  if (bounds.length) map.fitBounds(bounds);
}

/** Update vehicle Gantt bar opacity to match hidden or selected trip state on the map. */
function updateGanttOpacity() {
  const gd = document.getElementById("vehicleGantt");
  if (!gd || !gd.data) return;

  gd.data.forEach((trace, ti) => {
    if (
      trace.type !== "bar" ||
      !Array.isArray(trace.customdata)
    ) {
      return;
    }

    const newOpacity = trace.customdata.map(cd => {
      const guid = String(cd[2] ?? "");
      if (!guid) return 0.3;

      if (mapDisplayMode === "none") return 0.3;

      if (mapDisplayMode === "all") {
        return hiddenTripGuids.has(guid) ? 0.3 : 1.0;
      }

      if (mapDisplayMode === "selection") {
        return selectedTripGuids.has(guid) ? 1.0 : 0.3;
      }

      return 1.0;
    });

    Plotly.restyle(
      gd,
      { "marker.opacity": [newOpacity] },
      [ti]
    );
  });
}

/** Draw the selected vehicle's depot marker when depot coordinates are available. */
function drawDepot(payload) {
  if (!payload?.depotLocation) return;

  const d = payload.depotLocation;
  L.circleMarker([d.lat, d.lng], {
    radius: 8,
    color: '#7f7f7f',
    fillColor: '#7f7f7f',
    fillOpacity: 1,
    weight: 2
  })
    .addTo(map)
    .bindPopup(`Depot - ${payload.vehicleId || ""}`);
}

/** Resize the vehicle-level Plotly Gantt chart after panel layout changes. */
function resizeVehicleGantt() {
  const gd = document.getElementById("vehicleGantt");
  if (!gd) return;

  requestAnimationFrame(() => {
    Plotly.Plots.resize(gd);
    setTimeout(() => Plotly.Plots.resize(gd), 300);
  });
}

/** Format a duration in seconds as a compact human-readable string. */
function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return "0 s";

  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);

  const parts = [];
  if (h > 0) parts.push(`${h} h`);
  if (m > 0) parts.push(`${m} min`);
  if (s > 0 || parts.length === 0) parts.push(`${s} s`);

  return parts.join(" ");
}

/** Format a ratio value as a percentage string. */
function formatPercent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "0.0 %";
  return `${(num * 100).toFixed(1)} %`;
}
