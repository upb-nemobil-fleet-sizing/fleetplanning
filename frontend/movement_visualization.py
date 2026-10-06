"""
Fleet movement animation for one job's fleet-size scenario.

Builds a self-contained Leaflet/vanilla-JS animated page from the same schema-compliant
output_cab/output_pro/input_base_file files everything else in the dashboard already reads
(tripStops/chainingStops), so this works for any backend (custom/rwapi/sumo), not only
custom_sim. Independent of custom_sim/visualize_movement.py by design - that script reads
custom_sim's own past_entries_fleet.json, a different, custom_sim-only file the solver keeps
lean by never storing route polylines on most entries; this module gets polylines the same
way the rest of the dashboard's route map already does, via output_details_utils's cached
local-router lookup.

Meant to be embedded in another page (an iframe on the output-details page), not opened as
its own top-level page - no page title, no assumption of owning the whole browser window
beyond whatever box the embedding page gives it. The generated file holds no tile address: its
tile source follows the embedding page's TILE_SOURCE setting.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

try:
    from output_details_utils import _fetch_route_cached
except ModuleNotFoundError:
    from frontend.output_details_utils import _fetch_route_cached

CAB_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]
PRO_COLOR = "#FF4500"


def _parse_ts(value: str | None) -> float | None:
    """Parse an ISO timestamp string into unix epoch seconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _loc(stop: dict) -> tuple[float, float] | None:
    loc = stop.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    return float(lat), float(lon)


def _drive_window(cur: dict, departure_prev: float, arrival_cur: float) -> float:
    """Real drive start for this leg: arrival minus drivingTime, clamped into
    [departure_prev, arrival_cur]. The gap before it is schedule slack, not driving -
    the caller renders it as a separate stationary wait.
    """
    driving_sec = cur.get("drivingTime")
    if not isinstance(driving_sec, (int, float)) or driving_sec <= 0:
        return departure_prev
    return max(departure_prev, min(arrival_cur - driving_sec, arrival_cur))


def _build_cab_segments(cab_runs: list[dict]) -> dict[str, list[dict]]:
    """Three kinds of leg per cab: an optional wait at the previous stop's own location while
    schedule slack runs out before driving actually needs to start, the drive itself (only the
    current stop's own drivingTime, see _drive_window), and a dwell within one stop when its own
    arrival and departure differ (e.g. a Charging stop's arrival is when the cab reaches the
    station, departure is when charging actually finishes - the charging itself happens between
    those two timestamps on that single stop, not as a leg to the next one). Each entry:
    {t0,t1,la0,lo0,la1,lo1,tp,occupied,charging,poly?}.

    occupied tracks whether a customer is physically in the cab: true from the moment a Pickup
    stop is reached until the following Dropoff stop, including any Chaining/Unchaining/
    platoon-transport legs (and dwells) in between - the customer never leaves the vehicle for
    those, only at an actual Pickup/Dropoff. charging is true only for a Charging stop's own
    dwell.
    """
    cab_segs: dict[str, list[dict]] = {}
    for cab in cab_runs:
        vehicle = cab.get("vehicle") or {}
        cab_id = vehicle.get("id") or vehicle.get("label")
        stops = cab.get("tripStops") or []
        segs = []
        occupied = False
        for i in range(1, len(stops)):
            prev, cur = stops[i - 1], stops[i]
            prev_pos, cur_pos = _loc(prev), _loc(cur)
            t0, t1 = _parse_ts(prev.get("departure") or prev.get("arrival")), _parse_ts(cur.get("arrival") or cur.get("departure"))
            if prev_pos is None or cur_pos is None or t0 is None or t1 is None:
                continue
            la0, lo0 = prev_pos
            la1, lo1 = cur_pos
            stop_type = cur.get("stopType")
            drive_t0 = _drive_window(cur, t0, t1)
            if drive_t0 > t0:
                segs.append({
                    "t0": t0, "t1": drive_t0, "la0": la0, "lo0": lo0, "la1": la0, "lo1": lo0,
                    "tp": "Wait", "occupied": occupied, "dwell": True,
                })
            seg = {
                "t0": drive_t0, "t1": t1, "la0": la0, "lo0": lo0, "la1": la1, "lo1": lo1,
                "tp": stop_type, "occupied": occupied,
            }
            # the platoon-transport leg (cab riding behind a Pro) sits between a Chaining
            # stop and the Unchaining stop that follows it - route it on the Pro's own
            # profile, same distinction custom_sim's own router calls make.
            profile = "pro" if prev.get("stopType") == "Chaining" else "driving"
            poly = _fetch_route_cached(la0, lo0, la1, lo1, profile)
            if poly and len(poly) > 2:
                seg["poly"] = poly
            segs.append(seg)

            if stop_type == "Pickup":
                occupied = True
            elif stop_type == "Dropoff":
                occupied = False

            dwell_t0, dwell_t1 = _parse_ts(cur.get("arrival")), _parse_ts(cur.get("departure"))
            if dwell_t0 is not None and dwell_t1 is not None and dwell_t1 > dwell_t0:
                segs.append({
                    "t0": dwell_t0, "t1": dwell_t1, "la0": la1, "lo0": lo1, "la1": la1, "lo1": lo1,
                    "tp": stop_type, "occupied": occupied, "charging": stop_type == "Charging",
                    "dwell": True,
                })
        if segs:
            cab_segs[str(cab_id)] = segs
    return cab_segs


def _pro_leg_destination(key: str, pro_id: str) -> str | None:
    """The named next station for a real line-service leg, parsed from the chaining stop's own
    key (e.g. Station4_Station3_Pro1_1-End -> Station3). None for a leg that isn't between two
    named stations (a short local move at a chaining point instead).
    """
    m = re.match(rf"^(.+)_(.+)_{re.escape(pro_id or '')}_\d+(?:-Start|-End)?$", key or "", re.IGNORECASE)
    return m.group(2) if m else None


def _build_pro_segments(pro_runs: list[dict]) -> dict[str, list[dict]]:
    """One drive leg per consecutive chainingStops pair, per Pro, plus an optional wait at the
    previous stop (same drivingTime-based split as _build_cab_segments). Each entry:
    {t0,t1,la0,lo0,la1,lo1,cabs,activity?,poly?}.
    """
    pro_segs: dict[str, list[dict]] = {}
    for pro in pro_runs:
        vehicle = pro.get("vehicle") or {}
        pro_id = vehicle.get("id") or vehicle.get("label")
        stops = pro.get("chainingStops") or []
        segs = []
        for i in range(1, len(stops)):
            prev, cur = stops[i - 1], stops[i]
            prev_pos, cur_pos = _loc(prev), _loc(cur)
            t0, t1 = _parse_ts(prev.get("departure") or prev.get("arrival")), _parse_ts(cur.get("arrival") or cur.get("departure"))
            if prev_pos is None or cur_pos is None or t0 is None or t1 is None:
                continue
            la0, lo0 = prev_pos
            la1, lo1 = cur_pos
            drive_t0 = _drive_window(cur, t0, t1)
            if drive_t0 > t0:
                segs.append({
                    "t0": t0, "t1": drive_t0, "la0": la0, "lo0": lo0, "la1": la0, "lo1": lo0,
                    "cabs": int(prev.get("chainedCabs") or 0), "tp": "Wait", "dwell": True,
                })
            seg = {
                "t0": drive_t0, "t1": t1, "la0": la0, "lo0": lo0, "la1": la1, "lo1": lo1,
                "cabs": int(cur.get("chainedCabs") or 0),
            }
            # chainingStops pairs a leg's own -Start/-End, but also connects one leg's -End to
            # the next leg's -Start - when those are the same point (common: minutes actually
            # spent waiting at a station between legs), that's a dwell, not a drive, regardless
            # of drivingTime on the stop itself.
            if (la0, lo0) == (la1, lo1):
                seg["dwell"] = True
            else:
                destination = _pro_leg_destination(str(cur.get("key") or ""), str(pro_id or ""))
                if destination:
                    seg["activity"] = f"\u2192{destination}"
                poly = _fetch_route_cached(la0, lo0, la1, lo1, "pro")
                if poly and len(poly) > 2:
                    seg["poly"] = poly
            segs.append(seg)
        if segs:
            pro_segs[str(pro_id)] = segs
    return pro_segs


def _load_static_layers(base_data: dict) -> dict:
    """Operation-area outline, charging stations, and chain/unchain markers from input_base_file."""
    areas = base_data.get("operationAreas") or []
    ops_area = [
        [pt.get("Latitude"), pt.get("Longitude")]
        for pt in (areas[0].get("LocationBorder") or [])
    ] if areas else []

    charging_stations = [
        {
            "lat": cp.get("Location", {}).get("Latitude"),
            "lon": cp.get("Location", {}).get("Longitude"),
            "name": cp.get("Guid"),
            "power_kw": round((cp.get("maximumPowerSupply") or 0) / 1000, 1),
        }
        for cp in (base_data.get("chargingPoints") or [])
    ]

    chain_locations = [
        {
            "lat": cl.get("LocationStart", {}).get("Latitude"),
            "lon": cl.get("LocationStart", {}).get("Longitude"),
            "name": cl.get("Guid"),
            "type": cl.get("Type"),
        }
        for cl in (base_data.get("chainingLocations") or [])
    ]

    return {"ops_area": ops_area, "charging_stations": charging_stations, "chain_locations": chain_locations}


def _estimate_zoom(lat_span: float, lon_span: float) -> int:
    span = max(lat_span, lon_span)
    if span < 0.01: return 15
    if span < 0.03: return 14
    if span < 0.07: return 13
    if span < 0.15: return 12
    if span < 0.4: return 11
    if span < 1.0: return 10
    return 9


HTML_TEMPLATE = r"""<style>
  #movement-viz-root { position: relative; width: 100%; height: 600px; }
  #movement-viz-root .mv-map { position: absolute; top: 0; left: 0; right: 0; bottom: 64px; }
  #movement-viz-root .mv-timeline {
    position: absolute; bottom: 64px; left: 0; right: 0; height: 6px;
    background: #555; cursor: pointer; z-index: 999;
  }
  #movement-viz-root .mv-timeline-fill { height: 100%; background: #FF4500; width: 0%; pointer-events: none; }
  #movement-viz-root .mv-controls {
    position: absolute; bottom: 0; left: 0; right: 0; height: 64px;
    background: rgba(20,20,20,0.93); color: #ddd;
    display: flex; align-items: center; gap: 12px; padding: 0 16px;
    font-family: monospace; font-size: 13px; z-index: 1000;
  }
  #movement-viz-root button {
    background: #3a3a3a; border: 1px solid #777; color: #ddd;
    padding: 5px 13px; cursor: pointer; border-radius: 3px; font-size: 13px; min-width: 80px;
  }
  #movement-viz-root button:hover { background: #555; }
  #movement-viz-root select {
    background: #3a3a3a; border: 1px solid #777; color: #ddd;
    padding: 5px 8px; border-radius: 3px; font-size: 13px;
  }
  #movement-viz-root .mv-time { font-size: 15px; min-width: 130px; letter-spacing: 1px; }
  #movement-viz-root .mv-tooltip-small {
    padding: 1px 4px; font-size: 11px; background: rgba(255,255,255,0.85);
    border: none; box-shadow: none; white-space: nowrap;
  }
  #movement-viz-root .mv-legend {
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: rgba(255,255,255,0.93); padding: 8px 12px; border-radius: 4px;
    font-family: sans-serif; font-size: 12px; box-shadow: 0 1px 5px rgba(0,0,0,0.3);
    max-height: calc(100% - 80px); overflow-y: auto;
  }
  #movement-viz-root .mv-legend-row { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; }
  #movement-viz-root .mv-legend-row-clickable { cursor: pointer; border-radius: 3px; padding: 1px 3px; margin-left: -3px; }
  #movement-viz-root .mv-legend-row-clickable:hover { background: rgba(0,0,0,0.06); }
  #movement-viz-root .mv-legend-row-selected { background: rgba(0,0,0,0.1); font-weight: bold; }
  #movement-viz-root .mv-dot { width: 13px; height: 13px; border-radius: 50%; flex-shrink: 0; border: 1.5px solid #fff; box-shadow: 0 0 0 1px #aaa; }
  #movement-viz-root .mv-dot-pro { width: 17px; height: 17px; border-radius: 50%; flex-shrink: 0; background: __PRO_COLOR__; border: 2px solid #fff; box-shadow: 0 0 0 1px #aaa; }
</style>
<div id="movement-viz-root">
  <div class="mv-map" id="movement-viz-map"></div>
  <div class="mv-timeline" id="movement-viz-timeline"><div class="mv-timeline-fill" id="movement-viz-timeline-fill"></div></div>
  <div class="mv-controls">
    <button id="movement-viz-play">&#9654; Play</button>
    <span class="mv-time" id="movement-viz-time">--:--:--</span>
    <span style="color:#aaa; font-size:11px;">Speed:</span>
    <select id="movement-viz-speed">
      <option value="1">1&times;</option>
      <option value="10">10&times;</option>
      <option value="30">30&times;</option>
      <option value="60" selected>60&times;</option>
      <option value="120">120&times;</option>
      <option value="300">300&times;</option>
      <option value="600">600&times;</option>
    </select>
    <button id="movement-viz-follow">Follow: Off</button>
  </div>
  <div class="mv-legend">
    <div style="font-weight:bold; margin-bottom:6px">Fleet</div>
    <div id="movement-viz-legend-cabs"></div>
    <div id="movement-viz-legend-pros"></div>
    <div class="mv-legend-row" style="margin-top:6px; padding-top:6px; border-top:1px solid #ddd; color:#888; font-size:11px;">
      P1 (+2) = 2 cabs in convoy
    </div>
    <div class="mv-legend-row" style="color:#888; font-size:11px;">
      hollow cab = empty, filled cab = customer aboard
    </div>
    <div class="mv-legend-row" style="color:#888; font-size:11px;">
      &#9889; next to a cab's name = charging at a station
    </div>
  </div>
</div>
<script src="/static/vendor/leaflet-1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="/static/vendor/leaflet-1.9.4/leaflet.css"/>
<script src="/static/js/tile_layer.js"></script>
<script>
(function () {
  const CAB_DATA = __CAB_DATA__;
  const PRO_DATA = __PRO_DATA__;
  const STATIC = __STATIC_LAYERS__;
  const T_MIN = __T_MIN__;
  const T_MAX = __T_MAX__;
  const CAB_COLORS = __CAB_COLORS__;
  const PRO_COLOR = "__PRO_COLOR__";

  // preferCanvas: SVG (Leaflet's default) means every circleMarker is its own DOM element,
  // restyled/repositioned via DOM writes on every render - expensive with many vehicles
  // updated continuously. Canvas draws them all into one bitmap instead, far cheaper at
  // animation frame rates.
  // fadeAnimation:false - skips the crossfade Leaflet plays after a tile has already loaded,
  // purely client-side (no extra tile requests either way), so a loaded tile just appears
  // immediately instead of after that delay.
  const map = L.map('movement-viz-map', {scrollWheelZoom: true, preferCanvas: true, fadeAnimation: false}).setView([__LAT_CENTER__, __LON_CENTER__], __ZOOM__);
  // The tile source is not part of this file: it follows the dashboard page that embeds it, which
  // sets these two values from TILE_SOURCE (see maptiles_manager.py). Without them,
  // createDashboardTileLayer uses the public OpenStreetMap servers.
  try {
    window.TILE_URL_TEMPLATE = parent.TILE_URL_TEMPLATE;
    window.TILE_URL_FALLBACK = parent.TILE_URL_FALLBACK;
  } catch (e) {}
  createDashboardTileLayer({
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
    // Leaflet's own default (2) - present explicitly so it's a one-line bump later, e.g. once
    // tiles come from a local/fast server where prefetching more of them is actually cheap.
    keepBuffer: 2
  }).addTo(map);

  if (STATIC.ops_area && STATIC.ops_area.length > 1) {
    L.polygon(STATIC.ops_area, {color: '#555', weight: 1.5, dashArray: '6 4', fillOpacity: 0.04, interactive: false}).addTo(map);
  }

  function svgIcon(svgContent, w, h) {
    return L.divIcon({html: svgContent, className: '', iconSize: [w, h], iconAnchor: [w/2, h/2], tooltipAnchor: [0, -h/2]});
  }

  (STATIC.charging_stations || []).forEach(cs => {
    const icon = svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14"><rect x="1" y="1" width="12" height="12" rx="2" fill="#2ca02c" stroke="#1a7a1a" stroke-width="1.5"/></svg>`, 14, 14);
    L.marker([cs.lat, cs.lon], {icon}).addTo(map).bindTooltip(`CS: ${cs.name} (${cs.power_kw} kW)`, {direction: 'top'});
  });

  (STATIC.chain_locations || []).forEach(cl => {
    const isChain = cl.type === 'Chain';
    const icon = isChain
      ? svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="13" viewBox="0 0 14 13"><polygon points="7,1 13,12 1,12" fill="#17becf" stroke="#0d8fa0" stroke-width="1.5"/></svg>`, 14, 13)
      : svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="13" viewBox="0 0 14 13"><polygon points="7,12 13,1 1,1" fill="#ff7f0e" stroke="#c45e00" stroke-width="1.5"/></svg>`, 14, 13);
    L.marker([cl.lat, cl.lon], {icon}).addTo(map).bindTooltip(`${cl.type}: ${cl.name}`, {direction: 'top'});
  });

  const cabIds = Object.keys(CAB_DATA).sort((a,b) => (+a||0) - (+b||0) || a.localeCompare(b));
  const proIds = Object.keys(PRO_DATA).sort((a,b) => (+a||0) - (+b||0) || a.localeCompare(b));

  const legendCabs = document.getElementById('movement-viz-legend-cabs');
  const cabActivityEls = {};
  const cabLegendRows = {};
  let selectedCab = null;
  cabIds.forEach((cid, i) => {
    const color = CAB_COLORS[i % CAB_COLORS.length];
    const row = document.createElement('div');
    row.className = 'mv-legend-row mv-legend-row-clickable';
    row.innerHTML = `<span class="mv-dot" style="background:${color}"></span><span>${cid}:</span><span class="mv-activity"></span>`;
    row.addEventListener('click', () => selectCab(cid));
    legendCabs.appendChild(row);
    cabActivityEls[cid] = row.querySelector('.mv-activity');
    cabLegendRows[cid] = row;
  });

  // Human-readable current activity for one cab, from its current segment - shown in the
  // legend since the on-map marker changes (hollow/filled, charging ring) are subtle and easy
  // to miss at a glance.
  function cabActivityLabel(pos) {
    if (!pos) return '';
    if (pos.charging) return '\u26A1';
    if (pos.dwell) {
      if (pos.tp === 'Pickup') return 'pickup';
      if (pos.tp === 'Dropoff') return 'dropoff';
      if (pos.tp === 'Chaining') return 'chaining';
      if (pos.tp === 'Unchaining') return 'unchaining';
      // Wait (schedule slack before the next drive) is idle time, which has no label; "stopped"
      // is any other dwell.
      if (pos.tp === 'Wait') return '';
      return 'stopped';
    }
    switch (pos.tp) {
      case 'Pickup': return '\u2192pickup';
      case 'Dropoff': return '\u2192dropoff';
      case 'Charging': return '\u2192charge';
      case 'Chaining': return '\u2192Pro';
      // this leg is the cab still travelling with the Pro on the way to the unchain
      // point - not "leaving" it yet, that only happens at the Unchaining dwell above.
      case 'Unchaining': return '\u2192unchain';
      case 'Depot': return '\u2192depot';
      default: return '';
    }
  }
  // Human-readable current activity for one Pro, same idea as cabActivityLabel. activity is the
  // real next station name for a named line-service leg (see _pro_leg_destination); falls back
  // to driving for a leg that isn't between two named stations. A stop with cabs chained is
  // "stopped"; without cabs it is idle and has no label.
  function proActivityLabel(pos) {
    if (!pos) return '';
    const activity = pos.activity || (pos.dwell ? (pos.cabs > 0 ? 'stopped' : '') : 'driving');
    return pos.cabs > 0 ? `${activity} +${pos.cabs}` : activity;
  }
  const legendPros = document.getElementById('movement-viz-legend-pros');
  const proActivityEls = {};
  const proLegendRows = {};
  let selectedPro = null;
  proIds.forEach(pid => {
    const row = document.createElement('div');
    row.className = 'mv-legend-row mv-legend-row-clickable';
    row.innerHTML = `<span class="mv-dot-pro"></span><span>${pid}:</span><span class="mv-activity"></span>`;
    row.addEventListener('click', () => selectPro(pid));
    legendPros.appendChild(row);
    proActivityEls[pid] = row.querySelector('.mv-activity');
    proLegendRows[pid] = row;
  });

  // Shared by both the legend rows and a click directly on a marker on the map - either one
  // selects the same way. selecting a cab clears any selected Pro and vice versa.
  function selectCab(cid) {
    selectedCab = selectedCab === cid ? null : cid;
    if (selectedCab) {
      selectedPro = null;
      Object.values(proLegendRows).forEach(el => el.classList.remove('mv-legend-row-selected'));
    }
    Object.entries(cabLegendRows).forEach(([id, el]) => el.classList.toggle('mv-legend-row-selected', id === selectedCab));
    needsRender = true;
  }
  function selectPro(pid) {
    selectedPro = selectedPro === pid ? null : pid;
    if (selectedPro) {
      selectedCab = null;
      Object.values(cabLegendRows).forEach(el => el.classList.remove('mv-legend-row-selected'));
    }
    Object.entries(proLegendRows).forEach(([id, el]) => el.classList.toggle('mv-legend-row-selected', id === selectedPro));
    needsRender = true;
  }

  // a click on the map itself (not a drag-pan) clears whichever selection is active. A marker's
  // own click handler must set bubblingMouseEvents: false or this fires right after it too.
  map.on('click', () => {
    if (!selectedCab && !selectedPro) return;
    selectedCab = null;
    selectedPro = null;
    Object.values(cabLegendRows).forEach(el => el.classList.remove('mv-legend-row-selected'));
    Object.values(proLegendRows).forEach(el => el.classList.remove('mv-legend-row-selected'));
    needsRender = true;
  });

  function makeMarker(lat, lon, color, radius, label, isPro) {
    // bubblingMouseEvents: false - L.CircleMarker defaults this to true, so a click here would
    // otherwise also fire the map's own click handler (which clears the selection this same
    // click just made).
    const m = L.circleMarker([lat, lon], {radius, fillColor: color, fillOpacity: 0.92, color: isPro ? '#cc3300' : 'rgba(255,255,255,0.8)', weight: isPro ? 2 : 1.5, bubblingMouseEvents: false}).addTo(map);
    // interactive: true - a Leaflet tooltip is pointer-events:none by default, so without this
    // a click on the label passes straight through to the map underneath instead of the marker.
    if (label) m.bindTooltip(label, {permanent: true, interactive: true, direction: 'right', className: 'mv-tooltip-small', offset: [radius + 2, 0]});
    return m;
  }

  // The always-visible number/status bubble is a separate DOM element from the circle marker
  // itself - fading the marker alone still leaves a fully-opaque, easy-to-spot label next to
  // it, especially misleading for an empty cab (just a faint hollow ring to begin with).
  function setMarkerOpacity(marker, opacity) {
    const tooltip = marker.getTooltip && marker.getTooltip();
    const el = tooltip && tooltip.getElement && tooltip.getElement();
    if (el) el.style.opacity = String(opacity);
  }

  // Shows the current leg's road-following path for whichever cab is selected in the legend -
  // empty (and so invisible) whenever nothing is selected, or while the selected cab is
  // stationary (a dwell leg has no route to show). Split into an already-driven part (faded)
  // and what's still ahead (full brightness), so progress along the leg is visible at a glance.
  // solid, not dashed: a dash pattern re-anchors at each polyline's own start point, and
  // remaining's start is the split point itself, moving every frame - the dash phase would
  // shift along the whole line each redraw and read as the gaps crawling forward.
  const selectedRouteTraveled = L.polyline([], {color: '#ff00c8', weight: 4, opacity: 0.35, interactive: false}).addTo(map);
  const selectedRouteRemaining = L.polyline([], {color: '#ff00c8', weight: 4, opacity: 0.9, interactive: false}).addTo(map);

  // wires both the marker's own click and its always-visible label's click to the same
  // handler, and stops the label's native DOM click from bubbling to the map's own click
  // (which would otherwise immediately undo the selection this click just made).
  function wireVehicleClick(marker, onClick) {
    marker.on('click', onClick);
    const tooltip = marker.getTooltip && marker.getTooltip();
    const el = tooltip && tooltip.getElement && tooltip.getElement();
    if (el) el.addEventListener('click', e => { e.stopPropagation(); onClick(); });
  }

  const cabMarkers = {};
  cabIds.forEach((cid, i) => {
    const segs = CAB_DATA[cid];
    if (!segs || !segs.length) return;
    cabMarkers[cid] = makeMarker(segs[0].la0, segs[0].lo0, CAB_COLORS[i % CAB_COLORS.length], 9, String(cid), false);
    wireVehicleClick(cabMarkers[cid], () => selectCab(cid));
  });
  const proMarkers = {};
  proIds.forEach(pid => {
    const segs = PRO_DATA[pid];
    if (!segs || !segs.length) return;
    proMarkers[pid] = makeMarker(segs[0].la0, segs[0].lo0, PRO_COLOR, 13, pid, true);
    wireVehicleClick(proMarkers[pid], () => selectPro(pid));
  });

  // poly here is [{lat,lng,t}, ...]. t is cumulative real travel time per point (vehicle-capped,
  // see _fetch_local_route/_edge_point_times), so a slow street doesn't get the same share of
  // the animation as a highway. Falls back to arc length if t is missing or degenerate.
  function polylineWeights(poly) {
    if (poly.every(p => typeof p.t === 'number') && poly[poly.length-1].t > 1e-9) {
      return poly.map(p => p.t);
    }
    const dists = [0];
    for (let i = 1; i < poly.length; i++) {
      const dlat = poly[i].lat - poly[i-1].lat, dlon = poly[i].lng - poly[i-1].lng;
      dists.push(dists[i-1] + Math.sqrt(dlat*dlat + dlon*dlon));
    }
    return dists;
  }

  function interpPolyline(poly, frac) {
    if (!poly || poly.length < 2) return null;
    const weights = polylineWeights(poly);
    const total = weights[weights.length-1];
    if (total < 1e-12) return poly[poly.length-1];
    const target = Math.max(0, Math.min(1, frac)) * total;
    for (let i = 1; i < weights.length; i++) {
      if (target <= weights[i]) {
        const r = (target - weights[i-1]) / Math.max(1e-12, weights[i] - weights[i-1]);
        return {lat: poly[i-1].lat + r*(poly[i].lat-poly[i-1].lat), lng: poly[i-1].lng + r*(poly[i].lng-poly[i-1].lng)};
      }
    }
    return poly[poly.length-1];
  }

  // Same walk as interpPolyline, but returns the two halves of the line on either side of
  // frac instead of just the point - used to show already-driven vs. still-ahead separately.
  function splitPolylineAtFraction(poly, frac) {
    if (!poly || poly.length < 2) return {traveled: [], remaining: poly || []};
    const weights = polylineWeights(poly);
    const total = weights[weights.length-1];
    if (total < 1e-12) return {traveled: [], remaining: poly};
    const target = Math.max(0, Math.min(1, frac)) * total;
    for (let i = 1; i < weights.length; i++) {
      if (target <= weights[i]) {
        const r = (target - weights[i-1]) / Math.max(1e-12, weights[i] - weights[i-1]);
        const mid = {lat: poly[i-1].lat + r*(poly[i].lat-poly[i-1].lat), lng: poly[i-1].lng + r*(poly[i].lng-poly[i-1].lng)};
        return {traveled: poly.slice(0, i).concat([mid]), remaining: [mid].concat(poly.slice(i))};
      }
    }
    return {traveled: poly, remaining: [poly[poly.length-1]]};
  }

  // track is sorted by t0 (segments are built in chronological order) - binary search for the
  // *first* segment whose own end covers t, instead of scanning from the start every call.
  // Matters at the exact instant two segments touch (one ending as the next begins): this
  // must resolve to the earlier segment (frac 1, "just finished"), matching what a linear scan
  // finds first - not the later one, which a naive "last segment starting at-or-before t"
  // search would pick instead.
  function getPos(track, t) {
    if (!track.length || t < track[0].t0) return null;
    const lastSeg = track[track.length - 1];
    if (t > lastSeg.t1) {
      // frozen after the last entry - occupied/charging forced false: whatever that entry was
      // doing is done by definition once frozen after it. Usually already false here (a real
      // schedule normally ends at a Dropoff dwell, which already flips it before this segment
      // is built), but not guaranteed if a vehicle's very last logged action ends mid-trip.
      return {lat: lastSeg.la1, lon: lastSeg.lo1, cabs: lastSeg.cabs || 0, occupied: false, charging: false, tp: lastSeg.tp, dwell: !!lastSeg.dwell, activity: lastSeg.activity, poly: null, frac: 0};
    }
    let lo = 0, hi = track.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (track[mid].t1 >= t) hi = mid; else lo = mid + 1;
    }
    const s = track[lo];
    const dur = s.t1 - s.t0;
    const frac = dur > 1e-9 ? (t - s.t0) / dur : 0;
    let lat, lon;
    if (s.poly) {
      const pt = interpPolyline(s.poly, frac);
      lat = pt.lat; lon = pt.lng;
    } else {
      lat = s.la0 + frac * (s.la1 - s.la0);
      lon = s.lo0 + frac * (s.lo1 - s.lo0);
    }
    return {lat, lon, cabs: s.cabs || 0, occupied: !!s.occupied, charging: !!s.charging, tp: s.tp, dwell: !!s.dwell, activity: s.activity, poly: s.poly || null, frac};
  }

  let simTime = T_MIN, playing = false, lastReal = null, speed = 60;
  // Rendering (repositioning/restyling every marker) is the expensive part, not advancing
  // simTime - so it's decoupled from requestAnimationFrame's own ~60fps tick. While paused,
  // render() only runs once per actual change (scrub, selection), not continuously; while
  // playing, it's capped to RENDER_INTERVAL_MS regardless of display refresh rate, since a
  // fleet moving over a simulated day doesn't need 60 real repaints a second to read as smooth.
  let needsRender = true;
  let lastRenderReal = 0;
  const RENDER_INTERVAL_MS = 66;
  const DIM_OPACITY = 0.3;
  const btnPlay = document.getElementById('movement-viz-play');
  const timeDisplay = document.getElementById('movement-viz-time');
  const speedSelect = document.getElementById('movement-viz-speed');
  const timelineFill = document.getElementById('movement-viz-timeline-fill');
  const btnFollow = document.getElementById('movement-viz-follow');
  let followEnabled = false;

  btnPlay.addEventListener('click', () => {
    if (simTime >= T_MAX) simTime = T_MIN;
    playing = !playing;
    btnPlay.innerHTML = playing ? '&#9646;&#9646; Pause' : '&#9654; Play';
    if (playing) lastReal = null;
    needsRender = true;
  });
  speedSelect.addEventListener('change', () => { speed = +speedSelect.value; });
  // off by default - always-on would fight a manual drag on every render tick.
  btnFollow.addEventListener('click', () => {
    followEnabled = !followEnabled;
    btnFollow.textContent = followEnabled ? 'Follow: On' : 'Follow: Off';
    needsRender = true;
  });
  document.getElementById('movement-viz-timeline').addEventListener('click', e => {
    const rect = e.currentTarget.getBoundingClientRect();
    simTime = T_MIN + Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * (T_MAX - T_MIN);
    needsRender = true;
  });

  function fmt(ts) {
    return new Date(ts * 1000).toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  }

  function render(t) {
    // selecting either a cab or a Pro dims every other vehicle of both kinds - the spotlight
    // is on the one selected vehicle, full stop.
    let selectedPos = null;
    proIds.forEach(pid => {
      const pos = getPos(PRO_DATA[pid], t);
      if (!proMarkers[pid]) return;
      if (pid === selectedPro) selectedPos = pos;
      if (pos) {
        const dim = selectedCab || (selectedPro && selectedPro !== pid);
        const opacity = dim ? DIM_OPACITY : 1;
        proMarkers[pid].setLatLng([pos.lat, pos.lon]).setStyle({opacity, fillOpacity: opacity * 0.92});
        setMarkerOpacity(proMarkers[pid], opacity);
        proMarkers[pid].setTooltipContent(pos.cabs > 0 ? `${pid} (+${pos.cabs})` : pid);
      } else {
        proMarkers[pid].setStyle({opacity:0, fillOpacity:0});
        setMarkerOpacity(proMarkers[pid], 0);
      }
      if (proActivityEls[pid]) {
        proActivityEls[pid].textContent = ' ' + proActivityLabel(pos);
      }
    });
    cabIds.forEach(cid => {
      const pos = getPos(CAB_DATA[cid], t);
      if (!cabMarkers[cid]) return;
      if (cid === selectedCab) selectedPos = pos;
      if (pos) {
        // hollow (fillOpacity 0) while empty, filled while a customer is aboard - the stroke
        // stays visible either way so an empty cab doesn't disappear. Charging is shown via
        // the marker's own always-visible number label (same technique as a Pro's convoy
        // count), not a stroke color - a ring blends into the also-green charging-station
        // icon the cab sits right on top of. Clicking a cab in the legend dims every other
        // cab (and every Pro) - the label bubble is faded right along with the marker, since
        // an empty cab's ring alone is already faint and easy to miss.
        const dim = selectedPro || (selectedCab && selectedCab !== cid);
        const opacity = dim ? DIM_OPACITY : 1;
        cabMarkers[cid].setLatLng([pos.lat, pos.lon]).setStyle({
          opacity,
          fillOpacity: (pos.occupied ? 0.92 : 0) * opacity,
        });
        setMarkerOpacity(cabMarkers[cid], opacity);
        cabMarkers[cid].setTooltipContent(pos.charging ? `${cid} \u26A1` : String(cid));
      } else {
        cabMarkers[cid].setStyle({opacity:0, fillOpacity:0});
        setMarkerOpacity(cabMarkers[cid], 0);
      }
      if (cabActivityEls[cid]) {
        cabActivityEls[cid].textContent = ' ' + cabActivityLabel(pos);
        cabActivityEls[cid].style.color = pos && pos.charging ? '#2ecc40' : '';
        cabActivityEls[cid].style.fontWeight = pos && pos.charging ? 'bold' : '';
      }
    });
    // the selected vehicle's (cab or Pro) current leg, road-following where available - empty
    // (so invisible) whenever nothing is selected or the selection is between legs / stationary.
    if (selectedPos && selectedPos.poly) {
      const split = splitPolylineAtFraction(selectedPos.poly, selectedPos.frac);
      selectedRouteTraveled.setLatLngs(split.traveled);
      selectedRouteRemaining.setLatLngs(split.remaining);
    } else {
      selectedRouteTraveled.setLatLngs([]);
      selectedRouteRemaining.setLatLngs([]);
    }
    // follow the selected vehicle - keeps it centered, zoom untouched. animate:false so this
    // snaps instead of stacking a pan animation on every render tick during playback.
    if (followEnabled && selectedPos) {
      map.panTo([selectedPos.lat, selectedPos.lon], {animate: false});
    }
    timeDisplay.textContent = fmt(t);
    timelineFill.style.width = ((t - T_MIN) / (T_MAX - T_MIN) * 100).toFixed(3) + '%';
  }

  function loop(now) {
    requestAnimationFrame(loop);
    let shouldRender = needsRender;
    if (playing) {
      if (lastReal === null) lastReal = now;
      const dt = Math.min((now - lastReal) / 1000, 0.1);
      lastReal = now;
      simTime += dt * speed;
      if (simTime >= T_MAX) { simTime = T_MAX; playing = false; btnPlay.innerHTML = '&#9654; Play'; }
      shouldRender = true;
    }
    if (!shouldRender) return;
    // only throttle continuous playback - a one-off render from a click (paused) should
    // apply immediately, not wait out the interval.
    if (playing && now - lastRenderReal < RENDER_INTERVAL_MS) return;
    lastRenderReal = now;
    needsRender = false;
    render(simTime);
  }

  render(T_MIN);
  requestAnimationFrame(loop);
})();
</script>
"""


def build_movement_html(cab_runs: list[dict], pro_runs: list[dict], base_data: dict | None) -> str | None:
    """Build the self-contained movement-animation HTML fragment for one fleet-size scenario.

    Returns None if there is no valid movement data to show (e.g. an empty fleet).
    """
    cab_segs = _build_cab_segments(cab_runs)
    pro_segs = _build_pro_segments(pro_runs)
    static_layers = _load_static_layers(base_data) if base_data else {"ops_area": [], "charging_stations": [], "chain_locations": []}

    all_lats: list[float] = []
    all_lons: list[float] = []
    t_min = t_max = None
    for segs in list(cab_segs.values()) + list(pro_segs.values()):
        for s in segs:
            all_lats.extend([s["la0"], s["la1"]])
            all_lons.extend([s["lo0"], s["lo1"]])
            t_min = s["t0"] if t_min is None else min(t_min, s["t0"])
            t_max = s["t1"] if t_max is None else max(t_max, s["t1"])

    if not all_lats:
        return None

    lat_center = sum(all_lats) / len(all_lats)
    lon_center = sum(all_lons) / len(all_lons)
    zoom = _estimate_zoom(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons))

    cab_ids_sorted = sorted(cab_segs.keys(), key=lambda x: int(x) if x.isdigit() else x)
    cab_color_map = {cid: CAB_COLORS[i % len(CAB_COLORS)] for i, cid in enumerate(cab_ids_sorted)}

    import json as _json
    html = HTML_TEMPLATE
    html = html.replace("__CAB_DATA__", _json.dumps(cab_segs, separators=(",", ":")))
    html = html.replace("__PRO_DATA__", _json.dumps(pro_segs, separators=(",", ":")))
    html = html.replace("__STATIC_LAYERS__", _json.dumps(static_layers, separators=(",", ":")))
    html = html.replace("__T_MIN__", str(t_min))
    html = html.replace("__T_MAX__", str(t_max))
    html = html.replace("__CAB_COLORS__", _json.dumps([cab_color_map[cid] for cid in cab_ids_sorted]))
    html = html.replace("__PRO_COLOR__", PRO_COLOR)
    html = html.replace("__LAT_CENTER__", f"{lat_center:.6f}")
    html = html.replace("__LON_CENTER__", f"{lon_center:.6f}")
    html = html.replace("__ZOOM__", str(zoom))
    return html
