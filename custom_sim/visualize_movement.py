#!/usr/bin/env python3
"""
Post-processing interactive visualization of fleet movement.

Reads a past_entries_fleet.json and writes a self-contained HTML file with a
Leaflet.js map and smooth JS-interpolated animation.

With --base_data, routers are built (or loaded from cache) and polylines follow
the actual road network. Without it, positions are linearly interpolated.

Usage:
    python -m custom_sim.visualize_movement path/to/past_entries_fleet.json
    python -m custom_sim.visualize_movement path/to/past_entries_fleet.json \
        --base_data path/to/InitSimulationBaseData.json
    python -m custom_sim.visualize_movement path/to/past_entries_fleet.json \
        --base_data path/to/InitSimulationBaseData.json --out fleet.html
"""

import argparse
import json
import math
import os


CAB_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]
PRO_COLOR = "#FF4500"

CAB_MOVING = {"CA", "CT", "ChA", "FM", "LM", "PT", "PA", "PlA"}
PRO_MOVING = {"ST", "DT", "RA"}

# entry types where the customer is physically riding in the cab; PT (platoon transport,
# "convoy, w/wo customer") carries the occupied state forward from the preceding entry instead
# of setting it directly, since one PT entry can be either a customer leg or a deadhead ride
CAB_OCCUPIED_TYPES = {"CT", "FM", "LM"}
CAB_CHARGING_TYPE = "ChP"
PRO_CHARGING_TYPE = "RP"

ROUTE_TIME_PRECISION = 2  # decimal places for a polyline point's cumulative time, seconds


def build_routers(base_data_path):
    from custom_sim.routing.router import Router

    with open(base_data_path) as f:
        bd = json.load(f)

    oa = bd["operationAreas"][0]
    area_id = oa["ShortHandle"]
    border = oa["LocationBorder"]
    coords = [(pt["Longitude"], pt["Latitude"]) for pt in border]

    print(f"[viz] building/loading routers for area '{area_id}' ...")
    router_cab = Router(coords, profile="cab", area_id=area_id)
    router_pro = Router(coords, profile="pro", area_id=area_id)
    return router_cab, router_pro


def load_static_layers(base_data_path):
    with open(base_data_path) as f:
        bd = json.load(f)

    ops_area = [
        [pt["Latitude"], pt["Longitude"]]
        for pt in bd["operationAreas"][0]["LocationBorder"]
    ]

    charging_stations = [
        {
            "lat": cp["Location"]["Latitude"],
            "lon": cp["Location"]["Longitude"],
            "name": cp["Guid"],
            "power_kw": round(cp.get("maximumPowerSupply", 0) / 1000, 1),
        }
        for cp in bd.get("chargingPoints", [])
    ]

    chain_locations = [
        {
            "lat": cl["LocationStart"]["Latitude"],
            "lon": cl["LocationStart"]["Longitude"],
            "name": cl["Guid"],
            "type": cl["Type"],
        }
        for cl in bd.get("chainingLocations", [])
    ]

    return {"ops_area": ops_area, "charging_stations": charging_stations, "chain_locations": chain_locations}


def build_segments(cab_data, pro_data, router_cab=None, router_pro=None):
    route_cache = {}

    def get_poly(router, profile, s_lat, s_lon, e_lat, e_lon):
        if router is None:
            return None
        key = (id(router), profile, round(s_lat, 6), round(s_lon, 6), round(e_lat, 6), round(e_lon, 6))
        if key in route_cache:
            return route_cache[key]
        try:
            res = router.shortest_path((s_lat, s_lon), (e_lat, e_lon))
        except Exception:
            route_cache[key] = None
            return None
        nodes = res.get("path_osm_nodes", []) if res else []
        if not nodes:
            route_cache[key] = None
            return None
        pts = [[s_lat, s_lon]]
        times = [0.0]
        for i in range(len(nodes) - 1):
            u, v = nodes[i], nodes[i + 1]
            edge_dict = router.G.get_edge_data(u, v)
            new_pts = []
            edata = {}
            if edge_dict is not None:
                edata = next(iter(edge_dict.values()))
                geom = edata.get("geometry")
                if geom is not None:
                    # geometry coords are (lon, lat); skip first point (= node u, already added)
                    for lon, lat in list(geom.coords)[1:]:
                        new_pts.append([round(lat, 6), round(lon, 6)])
            if not new_pts:
                # no geometry: fall back to node v coordinate
                n = router.G.nodes.get(v)
                if n:
                    new_pts.append([round(float(n["y"]), 6), round(float(n["x"]), 6)])
            if not new_pts:
                continue
            # profile-specific time (e.g. "cab_time"), capped to the vehicle's own max speed,
            # matching what shortest_path itself costs edges with; travel_time (road speed only,
            # not capped) is only a fallback for an edge the profile builder skipped
            edge_time = edata.get(f"{profile}_time")
            if edge_time is None:
                edge_time = edata.get("travel_time") or 0.0
            # split this edge's own time across its shape-points by planar distance - its speed
            # is constant along its own length, so this is an exact split, not a per-point guess
            sub_lens = [math.hypot(new_pts[0][0] - pts[-1][0], new_pts[0][1] - pts[-1][1])]
            for j in range(1, len(new_pts)):
                sub_lens.append(math.hypot(new_pts[j][0] - new_pts[j-1][0], new_pts[j][1] - new_pts[j-1][1]))
            total_len = sum(sub_lens)
            base = times[-1]
            cum = 0.0
            for j in range(len(new_pts)):
                share = (sub_lens[j] / total_len) if total_len > 1e-9 else (1.0 / len(new_pts))
                cum += float(edge_time) * share
                times.append(base + cum)
            pts.extend(new_pts)
        pts.append([e_lat, e_lon])
        times.append(times[-1] if times else 0.0)
        if len(pts) > 2:
            result = [[la, lo, round(t, ROUTE_TIME_PRECISION)] for (la, lo), t in zip(pts, times)]
        else:
            result = None
        route_cache[key] = result
        return result

    # customer.entry_time (on-/off-boarding) is never customer-specific in this codebase (only
    # ever the models_cs.py default, 60s) - so it's one real constant for the whole scenario,
    # recoverable from any CT entry's own st field, which _find_insertion_times builds as
    # exactly 2x that constant (boarding at pickup, alighting at dropoff, split evenly).
    # Falls back to the model's own default if the file happens to have no CT entry at all.
    boarding_alighting_s = 60.0
    for entries in cab_data.values():
        found = next((e for e in entries if len(e) > 9 and str(e[2]) == "CT" and e[9]), None)
        if found:
            boarding_alighting_s = float(found[9]) / 2.0
            break

    cab_segs = {}
    for cab_id, entries in cab_data.items():
        segs = []
        occupied = False
        for entry in entries:
            if len(entry) < 16:
                continue
            t0 = float(entry[0]);  t1 = float(entry[1])
            typ = str(entry[2])
            st = float(entry[9]) if entry[9] is not None else 0.0
            la0, lo0 = float(entry[12]), float(entry[13])
            la1, lo1 = float(entry[14]), float(entry[15])
            pro_id = str(entry[17]) if len(entry) > 17 and entry[17] is not None else None
            if la0 < 0 or lo0 < 0 or la1 < 0 or lo1 < 0:
                continue
            if typ in CAB_OCCUPIED_TYPES:
                occupied = True
            elif typ != "PT":
                occupied = False
            seg = {
                "t0": t0, "t1": t1, "la0": la0, "lo0": lo0, "la1": la1, "lo1": lo1, "tp": typ, "st": st,
                "occupied": occupied, "charging": typ == CAB_CHARGING_TYPE,
            }
            # CT's own duration bakes in 2x the boarding/alighting constant, split evenly
            # (arrive -> board -> drive -> arrive -> alight). FM (pickup -> chain point) only
            # has boarding at its own start, the rest of its st is the chaining maneuver at its
            # end; LM (unchain point -> dropoff) mirrors that, maneuver at its start, alighting
            # at its end. Matches custom_simulation.py's own _find_insertion_times/
            # _find_convoy_insertion_times construction of these entries, not a guessed split.
            if st > 0 and typ in ("CT", "FM"):
                seg["dwell_start"] = min(boarding_alighting_s, st)
                seg["dwell_end"] = max(0.0, st - seg["dwell_start"])
            elif st > 0 and typ == "LM":
                seg["dwell_end"] = min(boarding_alighting_s, st)
                seg["dwell_start"] = max(0.0, st - seg["dwell_end"])
            if typ == "PT" and pro_id is not None:
                seg["pro"] = pro_id
            if typ in CAB_MOVING:
                # PT (platoon transport) rides the Pro's own drive, so both the router and the
                # profile-time it costs edges with need to be the Pro's, not the cab's
                active_profile = "pro" if typ == "PT" else "cab"
                active_router = router_pro if typ == "PT" else router_cab
                poly = get_poly(active_router, active_profile, la0, lo0, la1, lo1)
                if poly:
                    seg["poly"] = poly
            segs.append(seg)
        segs.sort(key=lambda s: s["t0"])
        if segs:
            cab_segs[str(cab_id)] = segs

    pro_segs = {}
    for pro_id, entries in pro_data.items():
        segs = []
        for entry in entries:
            if len(entry) < 7:
                continue
            t0 = float(entry[0]);  t1 = float(entry[1])
            typ = str(entry[2])
            la0, lo0 = float(entry[3]), float(entry[4])
            la1, lo1 = float(entry[5]), float(entry[6])
            if la0 < 0 or lo0 < 0 or la1 < 0 or lo1 < 0:
                continue
            seg = {
                "t0": t0, "t1": t1, "la0": la0, "lo0": lo0, "la1": la1, "lo1": lo1, "tp": typ,
                "charging": typ == PRO_CHARGING_TYPE,
            }
            if typ in PRO_MOVING:
                poly = get_poly(router_pro, "pro", la0, lo0, la1, lo1)
                if poly:
                    seg["poly"] = poly
            segs.append(seg)
        segs.sort(key=lambda s: s["t0"])
        if segs:
            pro_segs[str(pro_id)] = segs

    return cab_segs, pro_segs


def estimate_zoom(lat_span, lon_span):
    span = max(lat_span, lon_span)
    if span < 0.01:  return 15
    if span < 0.03:  return 14
    if span < 0.07:  return 13
    if span < 0.15:  return 12
    if span < 0.4:   return 11
    if span < 1.0:   return 10
    return 9


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<title>Fleet Movement</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; overflow: hidden; }
  #map { position: absolute; top: 0; left: 0; right: 0; bottom: 80px; }
  #timeline-bar {
    position: absolute; bottom: 80px; left: 0; right: 0; height: 6px;
    background: #555; cursor: pointer; z-index: 999;
  }
  #timeline-fill { height: 100%; background: #FF4500; width: 0%; pointer-events: none; }
  #controls {
    position: absolute; bottom: 0; left: 0; right: 0; height: 80px;
    background: rgba(20,20,20,0.93); color: #ddd;
    display: flex; align-items: center; gap: 12px; padding: 0 18px;
    font-family: monospace; font-size: 13px; z-index: 1000;
  }
  button {
    background: #3a3a3a; border: 1px solid #777; color: #ddd;
    padding: 5px 13px; cursor: pointer; border-radius: 3px; font-size: 13px;
    min-width: 80px;
  }
  button:hover { background: #555; }
  select {
    background: #3a3a3a; border: 1px solid #777; color: #ddd;
    padding: 5px 8px; border-radius: 3px; font-size: 13px;
  }
  #time-display { font-size: 15px; min-width: 130px; letter-spacing: 1px; }
  #convoy-count { font-size: 12px; color: #FF4500; min-width: 90px; }
  .leaflet-tooltip-small {
    padding: 1px 4px; font-size: 11px; background: rgba(255,255,255,0.85);
    border: none; box-shadow: none; white-space: nowrap;
  }
  #legend {
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: rgba(255,255,255,0.93); padding: 8px 12px;
    border-radius: 4px; font-family: sans-serif; font-size: 12px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.3); max-height: calc(100% - 120px);
    overflow-y: auto;
  }
  .legend-row { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; }
  .dot { width: 13px; height: 13px; border-radius: 50%; flex-shrink: 0; border: 1.5px solid #fff; box-shadow: 0 0 0 1px #aaa; }
  .dot-pro { width: 17px; height: 17px; border-radius: 50%; flex-shrink: 0; background: #FF4500; border: 2px solid #fff; box-shadow: 0 0 0 1px #aaa; }
  .legend-row-clickable { cursor: pointer; border-radius: 3px; }
  .legend-row-clickable:hover { background: rgba(0,0,0,0.06); }
  .legend-row-selected { background: rgba(255,69,0,0.12); }
  .activity { color: #888; font-size: 11px; margin-left: 2px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="timeline-bar"><div id="timeline-fill"></div></div>
<div id="controls">
  <button id="btn-play">&#9654; Play</button>
  <span id="time-display">--:--:--</span>
  <span style="color:#aaa; font-size:11px;">Speed:</span>
  <select id="speed-select">
    <option value="1">1&times;</option>
    <option value="10">10&times;</option>
    <option value="30">30&times;</option>
    <option value="60" selected>60&times;</option>
    <option value="120">120&times;</option>
    <option value="300">300&times;</option>
    <option value="600">600&times;</option>
  </select>
  <button id="btn-follow">Follow: Off</button>
  <span style="flex:1"></span>
</div>
<div id="legend">
  <div style="font-weight:bold; margin-bottom:6px">Fleet</div>
  <div id="legend-cabs"></div>
  <div id="legend-pros"></div>
  <div class="legend-row" style="margin-top:6px; padding-top:6px; border-top:1px solid #ddd; color:#888; font-size:11px;">
    P1 (+2) = 2 cabs in convoy
  </div>
  <div style="font-weight:bold; margin:8px 0 4px">Infrastructure</div>
  <div class="legend-row">
    <svg width="14" height="14" style="flex-shrink:0"><rect x="1" y="1" width="12" height="12" rx="2" fill="#2ca02c" stroke="#1a7a1a" stroke-width="1.5"/></svg>
    <span>Charging station</span>
  </div>
  <div class="legend-row">
    <svg width="14" height="13" style="flex-shrink:0"><polygon points="7,1 13,12 1,12" fill="#17becf" stroke="#0d8fa0" stroke-width="1.5"/></svg>
    <span>Chain location</span>
  </div>
  <div class="legend-row">
    <svg width="14" height="13" style="flex-shrink:0"><polygon points="7,12 13,1 1,1" fill="#ff7f0e" stroke="#c45e00" stroke-width="1.5"/></svg>
    <span>Unchain location</span>
  </div>
  <div class="legend-row">
    <svg width="24" height="8" style="flex-shrink:0">
      <line x1="0" y1="4" x2="24" y2="4" stroke="#555" stroke-width="1.5" stroke-dasharray="5,3"/>
    </svg>
    <span>Operations area</span>
  </div>
</div>
<script>
const CAB_DATA = __CAB_DATA__;
const PRO_DATA = __PRO_DATA__;
const STATIC = __STATIC_LAYERS__;
const T_MIN = __T_MIN__;
const T_MAX = __T_MAX__;
const CAB_COLORS = __CAB_COLORS__;
const PRO_COLOR = "__PRO_COLOR__";

// fadeAnimation:false - skips the crossfade Leaflet plays on a loaded tile, so it appears
// immediately instead of after that delay
const map = L.map('map', {scrollWheelZoom: true, preferCanvas: true, fadeAnimation: false}).setView([__LAT_CENTER__, __LON_CENTER__], __ZOOM__);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
  // Leaflet's own default (2) - present explicitly as a one-line bump point for later
  keepBuffer: 2
}).addTo(map);

// Static infrastructure layers
if (STATIC.ops_area && STATIC.ops_area.length > 1) {
  L.polygon(STATIC.ops_area, {
    color: '#555', weight: 1.5, dashArray: '6 4', fillOpacity: 0.04, interactive: false
  }).addTo(map);
}

function svgIcon(svgContent, w, h) {
  return L.divIcon({
    html: svgContent, className: '',
    iconSize: [w, h], iconAnchor: [w/2, h/2], tooltipAnchor: [0, -h/2],
  });
}

(STATIC.charging_stations || []).forEach(cs => {
  const icon = svgIcon(
    `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">
      <rect x="1" y="1" width="12" height="12" rx="2" fill="#2ca02c" stroke="#1a7a1a" stroke-width="1.5"/>
    </svg>`, 14, 14);
  L.marker([cs.lat, cs.lon], {icon}).addTo(map)
   .bindTooltip(`CS: ${cs.name} (${cs.power_kw} kW)`, {direction: 'top'});
});

(STATIC.chain_locations || []).forEach(cl => {
  const isChain = cl.type === 'Chain';
  const icon = isChain
    ? svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="13" viewBox="0 0 14 13">
        <polygon points="7,1 13,12 1,12" fill="#17becf" stroke="#0d8fa0" stroke-width="1.5"/>
      </svg>`, 14, 13)
    : svgIcon(`<svg xmlns="http://www.w3.org/2000/svg" width="14" height="13" viewBox="0 0 14 13">
        <polygon points="7,12 13,1 1,1" fill="#ff7f0e" stroke="#c45e00" stroke-width="1.5"/>
      </svg>`, 14, 13);
  L.marker([cl.lat, cl.lon], {icon}).addTo(map)
   .bindTooltip(`${cl.type}: ${cl.name}`, {direction: 'top'});
});

const cabIds = Object.keys(CAB_DATA).sort((a,b) => (+a||0) - (+b||0) || a.localeCompare(b));
const proIds = Object.keys(PRO_DATA).sort((a,b) => (+a||0) - (+b||0) || a.localeCompare(b));

// The cab or Pro currently selected via a legend or marker click, or null if none. Dims every
// other vehicle and shows the selected one's current leg as a progress line (see below).
let selectedCab = null;
const DIM_OPACITY = 0.3;
// Render only when the visible state actually changed (play tick or a user interaction),
// instead of every animation frame - the map/legend cost nothing while paused and idle.
let needsRender = true;

// Short current-activity text shown next to each cab in the legend.
function cabActivityLabel(pos) {
  if (!pos) return '';
  if (pos.charging) return '\u26A1';
  // idle: frozen in a real gap after its last entry finished, not still doing that entry's
  // own action - blank rather than repeating a stale "->dropoff" etc. forever
  if (pos.idle) return '';
  switch (pos.tp) {
    case 'CA':  return '\u2192pickup';
    case 'CT':  return '\u2192dropoff';
    case 'FM':  return '\u2192Pro';
    // still travelling with the Pro; only "leaves" it at the unchain point
    case 'PT':  return pos.occupied ? '\u2192unchain' : '\u2192Pro';
    case 'LM':  return '\u2192dropoff';
    case 'ChA': return '\u2192charge';
    default:    return '';
  }
}

// Short current-activity text shown next to each Pro in the legend. RA is the only Pro entry
// type that names a destination (a charger); ST/DT are both just "driving" here, since the raw
// entry carries no named-station info the way a service-trip stop key does elsewhere.
function proActivityLabel(pos, cabCount) {
  if (!pos) return '';
  let activity;
  if (pos.charging) activity = '\u26A1';
  // idle: frozen in a real gap after its last entry finished (every Pro entry in real data
  // has one before it - a scheduled service vehicle waits between timetabled trips)
  else if (pos.idle) activity = '';
  else if (pos.tp === 'RA') activity = '\u2192charge';
  else activity = 'driving';
  return cabCount > 0 ? `${activity} +${cabCount}` : activity;
}

// Legend
let selectedPro = null;
const cabLegendRows = {};
const proLegendRows = {};

function clearLegendSelection() {
  document.querySelectorAll('#legend-cabs .legend-row, #legend-pros .legend-row').forEach(r => r.classList.remove('legend-row-selected'));
}

function selectCab(cid) {
  selectedCab = (selectedCab === cid) ? null : cid;
  if (selectedCab) selectedPro = null;
  clearLegendSelection();
  if (selectedCab) cabLegendRows[selectedCab].classList.add('legend-row-selected');
  needsRender = true;
}

function selectPro(pid) {
  selectedPro = (selectedPro === pid) ? null : pid;
  if (selectedPro) selectedCab = null;
  clearLegendSelection();
  if (selectedPro) proLegendRows[selectedPro].classList.add('legend-row-selected');
  needsRender = true;
}

// a click on the map itself (not a drag-pan) clears whichever selection is active. A marker's
// own click handler needs bubblingMouseEvents: false or this fires right after it too.
map.on('click', () => {
  if (!selectedCab && !selectedPro) return;
  selectedCab = null;
  selectedPro = null;
  clearLegendSelection();
  needsRender = true;
});

const legendCabs = document.getElementById('legend-cabs');
const cabActivityEls = {};
cabIds.forEach((cid, i) => {
  const color = CAB_COLORS[i % CAB_COLORS.length];
  const row = document.createElement('div');
  row.className = 'legend-row legend-row-clickable';
  row.innerHTML = `<span class="dot" style="background:${color}"></span><span>${cid}:</span><span class="activity"></span>`;
  row.addEventListener('click', () => selectCab(cid));
  legendCabs.appendChild(row);
  cabLegendRows[cid] = row;
  cabActivityEls[cid] = row.querySelector('.activity');
});
const legendPros = document.getElementById('legend-pros');
const proActivityEls = {};
proIds.forEach(pid => {
  const row = document.createElement('div');
  row.className = 'legend-row legend-row-clickable';
  row.innerHTML = `<span class="dot-pro"></span><span>Pro ${pid}:</span><span class="activity"></span>`;
  row.addEventListener('click', () => selectPro(pid));
  legendPros.appendChild(row);
  proLegendRows[pid] = row;
  proActivityEls[pid] = row.querySelector('.activity');
});

function makeMarker(lat, lon, color, radius, label, isPro) {
  const m = L.circleMarker([lat, lon], {
    radius, fillColor: color, fillOpacity: 0.92,
    color: isPro ? '#cc3300' : 'rgba(255,255,255,0.8)', weight: isPro ? 2 : 1.5,
    // Leaflet's default is true - without this, a marker click also bubbles to the map
    bubblingMouseEvents: false,
  }).addTo(map);
  if (label) {
    // interactive:true - a permanent tooltip is non-interactive (pointer-events: none) by
    // default, so a click on the label would otherwise never reach the marker
    m.bindTooltip(label, {permanent: true, direction: 'right',
                          className: 'leaflet-tooltip-small', offset: [radius + 2, 0], interactive: true});
  }
  return m;
}

// Wires both the marker itself and its always-visible tooltip label to the same click handler -
// the tooltip is a separate DOM element from the circle, so needs its own listener.
function wireVehicleClick(marker, onClick) {
  marker.on('click', onClick);
  const tooltip = marker.getTooltip && marker.getTooltip();
  const el = tooltip && tooltip.getElement && tooltip.getElement();
  if (el) el.addEventListener('click', e => { e.stopPropagation(); onClick(); });
}

// The always-visible number/status bubble is a separate DOM element from the circle marker
// itself - fading the marker alone still leaves a fully-opaque label next to it.
function setMarkerOpacity(marker, opacity) {
  const tooltip = marker.getTooltip && marker.getTooltip();
  const el = tooltip && tooltip.getElement && tooltip.getElement();
  if (el) el.style.opacity = String(opacity);
}

// Shows the current leg's road-following path for whichever cab is selected in the legend -
// empty (and so invisible) whenever nothing is selected, or while the selected cab is on a
// leg with no route (e.g. stationary). Split into an already-driven part (faded) and what's
// still ahead (full brightness), so progress along the leg is visible at a glance.
// solid, not dashed: a dash pattern re-anchors at each polyline's own start point, and
// remaining's start is the split point itself, moving every frame - the dash phase would
// shift along the whole line each redraw and read as the gaps crawling forward.
const selectedRouteTraveled = L.polyline([], {color: '#ff00c8', weight: 4, opacity: 0.35, interactive: false}).addTo(map);
const selectedRouteRemaining = L.polyline([], {color: '#ff00c8', weight: 4, opacity: 0.9, interactive: false}).addTo(map);

const cabMarkers = {};
cabIds.forEach((cid, i) => {
  const color = CAB_COLORS[i % CAB_COLORS.length];
  const segs = CAB_DATA[cid];
  if (!segs || !segs.length) return;
  cabMarkers[cid] = makeMarker(segs[0].la0, segs[0].lo0, color, 9, String(cid), false);
  wireVehicleClick(cabMarkers[cid], () => selectCab(cid));
});

const proMarkers = {};
proIds.forEach(pid => {
  const segs = PRO_DATA[pid];
  if (!segs || !segs.length) return;
  proMarkers[pid] = makeMarker(segs[0].la0, segs[0].lo0, PRO_COLOR, 13, `P${pid}`, true);
  wireVehicleClick(proMarkers[pid], () => selectPro(pid));
});


// Cumulative real travel time along a polyline [[lat,lon,t],...] - t is each point's own
// cumulative time from the route fetch, real per-edge speed already baked in, so interpolating
// by it moves at the actual pace of each road instead of a uniform speed across the whole leg.
// Falls back to arc length if a point carries no t (defensive only, every routed poly has one).
// Shared by interpPolyline and splitPolylineAtFraction so a frame needing both only walks once.
function polylineTimes(poly) {
  if (poly.length && poly[0][2] !== undefined) return poly.map(p => p[2]);
  const dists = [0];
  for (let i = 1; i < poly.length; i++) {
    const dlat = poly[i][0] - poly[i-1][0];
    const dlon = poly[i][1] - poly[i-1][1];
    dists.push(dists[i-1] + Math.sqrt(dlat*dlat + dlon*dlon));
  }
  return dists;
}

// Interpolate along a polyline [[lat,lon,t],...] at fraction frac in [0,1]
function interpPolyline(poly, frac) {
  if (!poly || poly.length < 2) return null;
  const times = polylineTimes(poly);
  const totalT = times[times.length-1];
  if (totalT < 1e-12) return poly[poly.length-1];
  const target = Math.max(0, Math.min(1, frac)) * totalT;
  for (let i = 1; i < times.length; i++) {
    if (target <= times[i]) {
      const r = (target - times[i-1]) / Math.max(1e-12, times[i] - times[i-1]);
      return [poly[i-1][0] + r*(poly[i][0]-poly[i-1][0]),
              poly[i-1][1] + r*(poly[i][1]-poly[i-1][1])];
    }
  }
  return poly[poly.length-1];
}

// Same walk as interpPolyline, but returns the two halves of the line on either side of frac
// instead of just the point - used to show already-driven vs. still-ahead separately.
function splitPolylineAtFraction(poly, frac) {
  if (!poly || poly.length < 2) return {traveled: [], remaining: poly || []};
  const times = polylineTimes(poly);
  const totalT = times[times.length-1];
  if (totalT < 1e-12) return {traveled: [], remaining: poly};
  const target = Math.max(0, Math.min(1, frac)) * totalT;
  for (let i = 1; i < times.length; i++) {
    if (target <= times[i]) {
      const r = (target - times[i-1]) / Math.max(1e-12, times[i] - times[i-1]);
      const mid = [poly[i-1][0] + r*(poly[i][0]-poly[i-1][0]), poly[i-1][1] + r*(poly[i][1]-poly[i-1][1])];
      return {traveled: poly.slice(0, i).concat([mid]), remaining: [mid].concat(poly.slice(i))};
    }
  }
  return {traveled: poly, remaining: [poly[poly.length-1]]};
}

// track is sorted by t0 (segments are built in chronological order) - binary search for the
// *first* segment whose own end covers t, instead of scanning from the start every call.
// Matters at the exact instant two segments touch: this must resolve to the earlier segment
// (frac 1, "just finished"), matching what a linear scan finds first. It must also reproduce
// the linear scan's behaviour for real gaps between entries (the cab is idle, not covered by
// any segment) by freezing at the end of the previous entry until the next one starts.
function getPos(track, t) {
  if (!track.length || t < track[0].t0) return null;
  const lastSeg = track[track.length - 1];
  if (t > lastSeg.t1) {
    // frozen after the last entry - idle:true so the activity label doesn't keep showing
    // that entry's own action (e.g. "->dropoff") forever after it actually finished.
    // occupied/charging forced false too: whatever the entry was doing is done by definition
    // once frozen after it, so its own flags would otherwise show a stale filled cab/charge icon.
    return {lat: lastSeg.la1, lon: lastSeg.lo1, tp: lastSeg.tp, pro: null, idle: true,
            occupied: false, charging: false, poly: null, frac: 0};
  }
  let lo = 0, hi = track.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (track[mid].t1 >= t) hi = mid; else lo = mid + 1;
  }
  const s = track[lo];
  if (t < s.t0) {
    // frozen in a genuine gap between two entries - same idle:true and forced-false reasoning
    const prev = track[lo - 1];
    return {lat: prev.la1, lon: prev.lo1, tp: prev.tp, pro: null, idle: true,
            occupied: false, charging: false, poly: null, frac: 0};
  }
  const dur = s.t1 - s.t0;
  let frac = dur > 1e-9 ? (t - s.t0) / dur : 0;
  // CT/FM/LM entries carry real, possibly asymmetric boarding/chaining/alighting dwell time
  // baked into their own span (see build_segments) - dwell_start/dwell_end mark it, so the
  // drive itself is only the real middle portion, not the whole t0-t1 span
  if (s.dwell_start !== undefined) {
    const ms = s.t0 + s.dwell_start, me = s.t1 - s.dwell_end;
    if (t <= ms)      frac = 0;
    else if (t >= me) frac = 1;
    else              frac = (t - ms) / Math.max(1e-9, me - ms);
  }
  let lat, lon;
  if (s.poly) {
    const pt = interpPolyline(s.poly, frac);
    lat = pt[0]; lon = pt[1];
  } else {
    lat = s.la0 + frac * (s.la1 - s.la0);
    lon = s.lo0 + frac * (s.lo1 - s.lo0);
  }
  return {lat, lon, tp: s.tp, pro: s.pro || null,
          occupied: !!s.occupied, charging: !!s.charging, poly: s.poly || null, frac};
}

let simTime = T_MIN, playing = false, lastReal = null, speed = 60;

const btnPlay = document.getElementById('btn-play');
const timeDisplay = document.getElementById('time-display');
const speedSelect = document.getElementById('speed-select');
const timelineFill = document.getElementById('timeline-fill');
const btnFollow = document.getElementById('btn-follow');
let followEnabled = false;
btnPlay.addEventListener('click', () => {
  if (simTime >= T_MAX) simTime = T_MIN;
  playing = !playing;
  btnPlay.innerHTML = playing ? '&#9646;&#9646; Pause' : '&#9654; Play';
  if (playing) lastReal = null;
  needsRender = true;
});
speedSelect.addEventListener('change', () => { speed = +speedSelect.value; });
btnFollow.addEventListener('click', () => {
  followEnabled = !followEnabled;
  btnFollow.textContent = followEnabled ? 'Follow: On' : 'Follow: Off';
  needsRender = true;
});
document.getElementById('timeline-bar').addEventListener('click', e => {
  const rect = e.currentTarget.getBoundingClientRect();
  simTime = T_MIN + Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * (T_MAX - T_MIN);
  needsRender = true;
});

function fmt(ts) {
  return new Date(ts * 1000).toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function render(t) {
  const cabPositions = {};
  let selectedPos = null;
  cabIds.forEach(cid => {
    const pos = getPos(CAB_DATA[cid], t);
    cabPositions[cid] = pos;
    if (cid === selectedCab) selectedPos = pos;
  });

  // Count cabs per Pro for convoy labels, needed before the Pro loop below
  const cabsPerPro = {};
  cabIds.forEach(cid => {
    const pos = cabPositions[cid];
    const proId = pos && pos.pro ? pos.pro : null;
    if (proId) cabsPerPro[proId] = (cabsPerPro[proId] || 0) + 1;
  });

  const dimAll = !!(selectedCab || selectedPro);
  proIds.forEach(pid => {
    const pos = getPos(PRO_DATA[pid], t);
    if (!proMarkers[pid]) return;
    if (pid === selectedPro) selectedPos = pos;
    const count = cabsPerPro[pid] || 0;
    if (pos) {
      const dim = dimAll && selectedPro !== pid;
      const opacity = dim ? DIM_OPACITY : 1;
      proMarkers[pid].setLatLng([pos.lat, pos.lon]).setStyle({opacity, fillOpacity: 0.92 * opacity});
      setMarkerOpacity(proMarkers[pid], opacity);
      proMarkers[pid].setTooltipContent(count > 0 ? `P${pid} (+${count})` : `P${pid}`);
    } else {
      proMarkers[pid].setStyle({opacity:0, fillOpacity:0});
      setMarkerOpacity(proMarkers[pid], 0);
    }
    if (proActivityEls[pid]) {
      proActivityEls[pid].textContent = ' ' + proActivityLabel(pos, count);
    }
  });

  cabIds.forEach(cid => {
    const pos = cabPositions[cid];
    if (!cabMarkers[cid]) return;
    if (pos) {
      const dim = dimAll && selectedCab !== cid;
      const opacity = dim ? DIM_OPACITY : 1;
      cabMarkers[cid].setLatLng([pos.lat, pos.lon]).setStyle({opacity, fillOpacity: (pos.occupied ? 0.92 : 0) * opacity});
      setMarkerOpacity(cabMarkers[cid], opacity);
      cabMarkers[cid].setTooltipContent(pos.charging ? `${cid} \u26A1` : String(cid));
    } else {
      cabMarkers[cid].setStyle({opacity:0, fillOpacity:0});
      setMarkerOpacity(cabMarkers[cid], 0);
    }
    if (cabActivityEls[cid]) {
      cabActivityEls[cid].textContent = ' ' + cabActivityLabel(pos);
    }
  });

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

// Render only when something changed (needsRender) - either a genuine playback tick or a
// user interaction (play/pause, speed, seek, legend selection) - and cap the redraw rate
// during playback instead of running full Leaflet/DOM work on every animation frame.
const RENDER_INTERVAL_MS = 66;
let lastRenderReal = 0;

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
  if (playing && now - lastRenderReal < RENDER_INTERVAL_MS) return;
  lastRenderReal = now;
  needsRender = false;
  render(simTime);
}

render(T_MIN);
requestAnimationFrame(loop);
</script>
</body>
</html>
"""


def generate(filename, out_html=None, base_data=None):
    with open(filename, "r") as f:
        raw = json.load(f)
    cab_data = raw.get("cabs", raw)
    pro_data = raw.get("pros", {})

    router_cab = router_pro = None
    static_layers = {"ops_area": [], "charging_stations": [], "chain_locations": []}
    if base_data:
        router_cab, router_pro = build_routers(base_data)
        static_layers = load_static_layers(base_data)

    cab_segs, pro_segs = build_segments(cab_data, pro_data, router_cab, router_pro)

    all_lats, all_lons = [], []
    t_min = t_max = None
    for segs in list(cab_segs.values()) + list(pro_segs.values()):
        for s in segs:
            all_lats.extend([s["la0"], s["la1"]])
            all_lons.extend([s["lo0"], s["lo1"]])
            if t_min is None or s["t0"] < t_min: t_min = s["t0"]
            if t_max is None or s["t1"] > t_max: t_max = s["t1"]

    if not all_lats:
        print("[viz] no valid data found")
        return

    lat_center = sum(all_lats) / len(all_lats)
    lon_center = sum(all_lons) / len(all_lons)
    zoom = estimate_zoom(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons))

    cab_ids_sorted = sorted(cab_segs.keys(), key=lambda x: int(x) if x.isdigit() else x)
    cab_color_map = {cid: CAB_COLORS[i % len(CAB_COLORS)] for i, cid in enumerate(cab_ids_sorted)}

    html = HTML_TEMPLATE
    html = html.replace("__CAB_DATA__", json.dumps(cab_segs, separators=(",", ":")))
    html = html.replace("__PRO_DATA__", json.dumps(pro_segs, separators=(",", ":")))
    html = html.replace("__STATIC_LAYERS__", json.dumps(static_layers, separators=(",", ":")))
    html = html.replace("__T_MIN__", str(t_min))
    html = html.replace("__T_MAX__", str(t_max))
    html = html.replace("__CAB_COLORS__", json.dumps([cab_color_map[cid] for cid in cab_ids_sorted]))
    html = html.replace("__PRO_COLOR__", PRO_COLOR)
    html = html.replace("__LAT_CENTER__", f"{lat_center:.6f}")
    html = html.replace("__LON_CENTER__", f"{lon_center:.6f}")
    html = html.replace("__ZOOM__", str(zoom))

    if out_html is None:
        base = os.path.splitext(filename)[0]
        out_html = base + "_interactive.html"

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(out_html) / 1024
    routed = "with road-following polylines" if (router_cab or router_pro) else "with straight-line interpolation"
    print(f"[viz] saved {out_html} ({size_kb:.0f} KB, {len(cab_segs)} cabs, {len(pro_segs)} pros, {routed})")
    return out_html


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive fleet movement visualization")
    parser.add_argument("filename", help="Path to past_entries_fleet.json")
    parser.add_argument("--base_data", default=None, help="Path to InitSimulationBaseData*.json for road-following routes")
    parser.add_argument("--out", default=None, help="Output HTML path (default: <input>_interactive.html)")
    args = parser.parse_args()
    generate(args.filename, args.out, args.base_data)
