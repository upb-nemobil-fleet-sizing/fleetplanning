from __future__ import annotations

import os
import json
import sys
import importlib.util
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, List, Optional, Tuple
from functools import lru_cache
import math
try:
    from output_utils import _compute_operations_utilization_for_cab_entry
except ModuleNotFoundError:
    from frontend.output_utils import _compute_operations_utilization_for_cab_entry

ROUTE_TIME_PRECISION = 2  # decimal places for a polyline point's "t" field, seconds

VEHICLE_KPI_ALIASES = {
    "totalDistance": "total_vehicle_distance_m",
    "totalDrivingTime": "total_vehicle_driving_time_s",
    "totalConsumedEnergy": "total_consumed_energy_wh",
    "transportedCustomers": "total_customer_trips",
    "convoyTrips": "total_empty_reposition_trips",
    "convoyDistance": "total_empty_reposition_distance_m",
    "stationaryChargedEnergy": "total_stationary_charging_energy_wh",
    "convoyChargedEnergy": "total_pro_charging_energy_wh",
    "stationaryChargingTime": "total_charging_stationary_time_s",
    "utilization": "average_utilization",
    "idleTimeSeconds": "idle_time_seconds",
    "idleRatio": "idle_ratio",
    "customerVsEmptyRatio": "customer_vs_empty_ratio",
}


def _attach_detailed_kpi_aliases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add legacy-friendly KPI aliases to a detailed vehicle payload."""
    if not isinstance(payload, dict):
        return payload
    kpis = payload.get("kpis")
    if not isinstance(kpis, dict):
        return payload
    for legacy_key, detailed_key in VEHICLE_KPI_ALIASES.items():
        if legacy_key in kpis and detailed_key not in kpis:
            kpis[detailed_key] = kpis.get(legacy_key)
    return payload

# --------------------------------------------------------------------------
# Local Routing Helpers
# --------------------------------------------------------------------------
def _uploads_dir() -> str:
    """Return the uploads_temp folder used as the active output-file workspace.

    Frontend-owned state, lives in frontend/ (next to this file) - not the repo root, and not
    reachable via os.getcwd(), which depends on how the process happens to be launched.
    """
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "uploads_temp"))

UPLOADS_DIR = _uploads_dir()
ROUTER_AREA_FILE = os.environ.get("ROUTER_AREA_FILE", os.path.join(UPLOADS_DIR, "operation_area.json"))
ROUTER_PROFILES_PATH = os.environ.get(
    "ROUTER_PROFILES_PATH",
    os.path.join(os.path.dirname(__file__), "..", "custom_sim", "routing", "vehicle_profiles.json")
)
ROUTER_PROFILE_MAP = {
    "driving": "cab",
    "car": "cab",
    "cab": "cab",
    "pro": "pro"
}

def _coords_valid(lat: Any, lng: Any) -> bool:
    """Validate that latitude and longitude values are numeric coordinates."""
    return isinstance(lat, (int, float)) and isinstance(lng, (int, float))

_ROUTER_IMPORT_ERROR_LOGGED = False
_ROUTER_CACHE: Dict[Tuple[str, str], Any] = {}
_OSMNX_GRAPH_CACHE: Dict[str, Any] = {}
_OSMNX_FALLBACK_ERRORS_LOGGED: set[Tuple[str, str]] = set()

def _load_operation_area_points() -> Optional[List[Dict[str, float]]]:
    """Load the operation-area polygon used to constrain local routing for detail views."""
    if not os.path.isfile(ROUTER_AREA_FILE):
        return None
    try:
        with open(ROUTER_AREA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list):
        return None

    normalized = []
    for p in points:
        if not isinstance(p, dict):
            continue
        lat = p.get("lat", p.get("Latitude"))
        lng = p.get("lng", p.get("Longitude"))
        if lat is None or lng is None:
            continue
        try:
            normalized.append({"lat": float(lat), "lng": float(lng)})
        except Exception:
            continue
    return normalized or None

def _collect_points_from_output_files() -> List[Dict[str, float]]:
    """Collect route coordinates from uploaded output files when an explicit operation area is missing."""
    points = []
    if not os.path.isdir(UPLOADS_DIR):
        return points
    candidates = []
    for fn in os.listdir(UPLOADS_DIR):
        lf = fn.lower()
        if lf.endswith(".json") and ("output_cab_" in lf or "output_pro_" in lf):
            candidates.append(os.path.join(UPLOADS_DIR, fn))
    # read only a few files (cab + pro if available)
    candidates = sorted(candidates)[:4]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        entries = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for e in entries:
            if not isinstance(e, dict):
                continue
            trip_stops = e.get("tripStops") or (e.get("vehicle") or {}).get("tripStops") or []
            if isinstance(trip_stops, list):
                for s in trip_stops:
                    if not isinstance(s, dict):
                        continue
                    loc = s.get("location") or {}
                    lat = loc.get("latitude") if isinstance(loc, dict) else None
                    lng = loc.get("longitude") if isinstance(loc, dict) else None
                    if lat is not None and lng is not None:
                        points.append({"lat": float(lat), "lng": float(lng)})
            chaining_stops = e.get("chainingStops") or e.get("ChainingStops") or []
            if isinstance(chaining_stops, list):
                for s in chaining_stops:
                    if not isinstance(s, dict):
                        continue
                    loc = s.get("location") or {}
                    lat = loc.get("latitude") if isinstance(loc, dict) else None
                    lng = loc.get("longitude") if isinstance(loc, dict) else None
                    if lat is not None and lng is not None:
                        points.append({"lat": float(lat), "lng": float(lng)})
    return points

def _ensure_operation_area_file() -> None:
    """Create a fallback operation_area.json from output coordinates if no uploaded area exists."""
    if os.path.isfile(ROUTER_AREA_FILE):
        return
    points = _collect_points_from_output_files()
    if not points:
        return
    lats = [p["lat"] for p in points]
    lngs = [p["lng"] for p in points]
    if not lats or not lngs:
        return
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    if min_lat == max_lat:
        min_lat -= 0.001
        max_lat += 0.001
    if min_lng == max_lng:
        min_lng -= 0.001
        max_lng += 0.001
    pad_lat = (max_lat - min_lat) * 0.05
    pad_lng = (max_lng - min_lng) * 0.05
    min_lat -= pad_lat
    max_lat += pad_lat
    min_lng -= pad_lng
    max_lng += pad_lng
    try:
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        payload = {
            "points": [
                {"lat": min_lat, "lng": min_lng},
                {"lat": min_lat, "lng": max_lng},
                {"lat": max_lat, "lng": max_lng},
                {"lat": max_lat, "lng": min_lng},
            ]
        }
        with open(ROUTER_AREA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        return

def _operation_area_signature():
    """Return the current operation-area points together with a stable hash signature."""
    points = _load_operation_area_points()
    if not points:
        _ensure_operation_area_file()
        points = _load_operation_area_points()
    if not points:
        return None, None
    try:
        import hashlib, json as _json
        area_hash = hashlib.md5(_json.dumps(points, sort_keys=True).encode("utf-8")).hexdigest()
    except Exception:
        area_hash = "unknown"
    return points, area_hash

def _build_polygon(points: List[Dict[str, float]]):
    """Build a Shapely polygon from normalized operation-area points when Shapely is available."""
    if not points or len(points) < 3:
        return None
    try:
        from shapely.geometry import Polygon
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Shapely not available for polygon: %s", e)
        return None
    try:
        return Polygon([(p["lng"], p["lat"]) for p in points])
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Polygon build failed: %s", e)
        return None

def _load_router_class():
    """Load the custom router class, applying a runtime patch for known source issues when needed."""
    global _ROUTER_IMPORT_ERROR_LOGGED
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from custom_sim.routing.router import Router
        return Router
    except SyntaxError:
        # try patched load below
        pass
    except Exception as e:
        if not _ROUTER_IMPORT_ERROR_LOGGED:
            import logging
            logging.getLogger(__name__).warning("Router import failed: %s", e)
            _ROUTER_IMPORT_ERROR_LOGGED = True

    # Fallback: patch known f-string quote issue in router.py
    router_path = os.path.abspath(os.path.join(repo_root, "custom_sim", "routing", "router.py"))
    if not os.path.isfile(router_path):
        return None
    try:
        with open(router_path, "r", encoding="utf-8") as f:
            code = f.read()
        code = code.replace('self.router_setting["map"]', "self.router_setting['map']")
        mod_name = "custom_sim.routing._router_patched"
        spec = importlib.util.spec_from_loader(mod_name, loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = router_path
        module.__package__ = "custom_sim.routing"
        sys.modules[mod_name] = module
        exec(compile(code, router_path, "exec"), module.__dict__)
        return getattr(module, "Router", None)
    except Exception as e:
        if not _ROUTER_IMPORT_ERROR_LOGGED:
            import logging
            logging.getLogger(__name__).warning("Router fallback import failed: %s", e)
            _ROUTER_IMPORT_ERROR_LOGGED = True
        return None


def _router_config_with_available_source() -> Optional[Dict[str, Any]]:
    """Load router config and ignore a configured local OSM source when that file is missing."""
    config_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "custom_sim", "routing", "routing_config.json")
    )
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return None

    source_cfg = config.get("build", {}).get("source", {})
    source_path = source_cfg.get("osm_source_path")
    if source_path:
        resolved = source_path
        if not os.path.isabs(str(resolved)):
            resolved = os.path.abspath(os.path.join(os.path.dirname(config_path), str(resolved)))
        if os.path.isfile(resolved):
            source_cfg["osm_source_path"] = resolved
        else:
            import logging
            logging.getLogger(__name__).warning(
                "Router OSM source missing, falling back to OSMnx/Overpass: %s",
                resolved,
            )
            source_cfg["osm_source_path"] = None
    return config


def _ensure_router(profile: str):
    """Create a router instance for the current operation area and vehicle profile."""
    Router = _load_router_class()
    if Router is None:
        return None

    points, area_hash = _operation_area_signature()
    if not points:
        return None
    poly = _build_polygon(points)
    if poly is None:
        return None

    area_id = f"op_{area_hash[:8]}" if area_hash else "operation_area"

    try:
        return Router(
            area_polygon=poly,
            profile=profile,
            use_ch=False,
            profiles_path=ROUTER_PROFILES_PATH,
            routing_config=_router_config_with_available_source(),
            area_id=area_id
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Router init failed: %s", e)
        return None

def _edge_point_times(seg: List[Tuple[float, float]], edge_time: float) -> List[float]:
    """Cumulative time (seconds, starting at 0) for each point of one edge's own geometry,
    splitting its total travel_time across sub-points by planar distance. An edge's speed_kph
    is constant along its own length (see routing/builder.py), so this is an exact split of a
    known total, not a per-point speed guess."""
    if len(seg) < 2:
        return [0.0] * len(seg)
    sub_lens = [0.0] + [math.hypot(seg[i][0] - seg[i-1][0], seg[i][1] - seg[i-1][1]) for i in range(1, len(seg))]
    total_len = sum(sub_lens)
    cum = [0.0]
    for i in range(1, len(seg)):
        share = (sub_lens[i] / total_len) if total_len > 1e-9 else (1.0 / (len(seg) - 1))
        cum.append(cum[-1] + edge_time * share)
    return cum


def _append_route_edge(pts: List[Tuple[float, float]], times: List[float],
                        seg: List[Tuple[float, float]], edge_time: float) -> None:
    """Append one edge's geometry onto a running route, skipping its leading point when it
    duplicates the route's current last point (consecutive edges share the connecting node),
    and offsetting the edge's own point-by-point cumulative time onto the route's running total."""
    local = _edge_point_times(seg, edge_time)
    start_idx = 1 if (pts and seg and pts[-1] == seg[0]) else 0
    base = times[-1] if times else 0.0
    for i in range(start_idx, len(seg)):
        pts.append(seg[i])
        times.append(base + local[i])


def _route_with_osmnx(start_lat: float, start_lng: float,
                      end_lat: float, end_lng: float,
                      area_hash: str,
                      polygon) -> Optional[List[Dict[str, float]]]:
    """Calculate a drivable fallback route with OSMnx when the custom router is unavailable."""
    def log_once(kind: str, exc: Exception) -> None:
        key = (area_hash or "unknown", kind)
        if key in _OSMNX_FALLBACK_ERRORS_LOGGED:
            return
        _OSMNX_FALLBACK_ERRORS_LOGGED.add(key)
        import logging
        logging.getLogger(__name__).warning("OSMnx fallback %s failed: %s", kind, exc)

    try:
        import osmnx as ox
        import networkx as nx
    except Exception as exc:
        log_once("import", exc)
        return None

    if area_hash not in _OSMNX_GRAPH_CACHE:
        try:
            G = ox.graph_from_polygon(polygon, network_type="drive_service")
            G = ox.add_edge_speeds(G)
            G = ox.add_edge_travel_times(G)
            _OSMNX_GRAPH_CACHE[area_hash] = G
        except Exception as exc:
            log_once("graph build", exc)
            return None

    G = _OSMNX_GRAPH_CACHE[area_hash]
    try:
        orig = ox.distance.nearest_nodes(G, start_lng, start_lat)
        dest = ox.distance.nearest_nodes(G, end_lng, end_lat)
        route = nx.shortest_path(G, orig, dest, weight="travel_time")
    except Exception as exc:
        log_once("path lookup", exc)
        return None

    pts: List[Tuple[float, float]] = []
    times: List[float] = []
    for u, v in zip(route[:-1], route[1:]):
        data_dict = G.get_edge_data(u, v) or {}
        data = None
        if isinstance(data_dict, dict):
            # pick first edge
            first_key = next(iter(data_dict.keys()), None)
            data = data_dict.get(first_key) if first_key is not None else None
        if data and "geometry" in data:
            xs, ys = data["geometry"].xy
            seg = [(float(y), float(x)) for x, y in zip(xs, ys)]
        else:
            seg = [
                (float(G.nodes[u]["y"]), float(G.nodes[u]["x"])),
                (float(G.nodes[v]["y"]), float(G.nodes[v]["x"])),
            ]
        # fallback graph has no vehicle-profile enrichment: road speed only, not capped
        _append_route_edge(pts, times, seg, float((data or {}).get("travel_time") or 0.0))

    if not pts:
        return None
    # include exact start/end points
    if pts[0] != (start_lat, start_lng):
        pts.insert(0, (float(start_lat), float(start_lng)))
        times.insert(0, times[0] if times else 0.0)
    if pts[-1] != (end_lat, end_lng):
        pts.append((float(end_lat), float(end_lng)))
        times.append(times[-1] if times else 0.0)
    coords = [{"lat": la, "lng": lo, "t": round(tt, ROUTE_TIME_PRECISION)} for (la, lo), tt in zip(pts, times)]
    return coords
def _get_router(profile: str, area_hash: str):
    """Return a cached router instance for the given profile and operation-area hash."""
    key = (profile, area_hash)
    if key in _ROUTER_CACHE:
        return _ROUTER_CACHE[key]
    router = _ensure_router(profile)
    if router is not None:
        _ROUTER_CACHE[key] = router
    return router

def _fetch_local_route(start_lat, start_lng, end_lat, end_lng, profile="driving"):
    """Resolve a route through the custom router first and fall back to OSMnx or straight lines."""
    router_profile = ROUTER_PROFILE_MAP.get(profile, "cab")
    points, area_hash = _operation_area_signature()
    if not area_hash or not points:
        import logging
        logging.getLogger(__name__).warning("Local router unavailable: missing operation area")
        return None
    polygon = _build_polygon(points)
    if polygon is None:
        import logging
        logging.getLogger(__name__).warning("Local router unavailable: invalid operation area polygon")
        return None
    router = _get_router(router_profile, area_hash)
    if router is None:
        import logging
        logging.getLogger(__name__).warning("Local router unavailable: import/init failed")
        # fallback: osmnx direct routing (no networkit)
        fallback = _route_with_osmnx(start_lat, start_lng, end_lat, end_lng, area_hash, polygon)
        if fallback:
            return fallback
        return None
    try:
        res = router.shortest_path((start_lat, start_lng), (end_lat, end_lng), precise_mode="fast")
        nodes = res.get("path_osm_nodes") or []
        pts: List[Tuple[float, float]] = []
        times: List[float] = []
        for lat, lon, data in router.route_segment_geometry(nodes):
            seg = [(float(la), float(lo)) for la, lo in zip(lat, lon)]
            # profile-specific time (e.g. "cab_time"), not travel_time: caps road speed at
            # the vehicle's own max speed, matching what shortest_path itself costs edges with
            edge_time = data.get(f"{router_profile}_time")
            if edge_time is None:
                edge_time = data.get("travel_time") or 0.0
            _append_route_edge(pts, times, seg, float(edge_time))
        if not pts:
            if len(nodes) <= 1:
                # both endpoints snapped to the same graph node — straight line is correct
                return [{"lat": float(start_lat), "lng": float(start_lng)},
                        {"lat": float(end_lat), "lng": float(end_lng)}]
            fallback = _route_with_osmnx(start_lat, start_lng, end_lat, end_lng, area_hash, polygon)
            if fallback:
                return fallback
            return None
        # include exact start/end points for nicer visualization
        if pts[0] != (start_lat, start_lng):
            pts.insert(0, (float(start_lat), float(start_lng)))
            times.insert(0, times[0] if times else 0.0)
        if pts[-1] != (end_lat, end_lng):
            pts.append((float(end_lat), float(end_lng)))
            times.append(times[-1] if times else 0.0)
        # t: cumulative real travel-time share per point (see _edge_point_times)
        return [{"lat": la, "lng": lo, "t": round(tt, ROUTE_TIME_PRECISION)} for (la, lo), tt in zip(pts, times)]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Local route failed %s -> %s (%s)", 
                                            {"lat": start_lat, "lng": start_lng},
                                            {"lat": end_lat, "lng": end_lng},
                                            e)
        fallback = _route_with_osmnx(start_lat, start_lng, end_lat, end_lng, area_hash, polygon)
        if fallback:
            return fallback
        return None


@lru_cache(maxsize=2048)
def _fetch_route_cached(*args):
    """Cache local route lookups so repeated map rendering does not rerun routing work."""
    try:
        return _fetch_local_route(*args)
    except Exception:
        return None


def clear_route_caches() -> None:
    """Clear route and graph caches after the operation area file changes."""
    try:
        _fetch_route_cached.cache_clear()
    except Exception:
        pass
    _ROUTER_CACHE.clear()
    _OSMNX_GRAPH_CACHE.clear()
    

def get_route_between_points(start_loc: Dict[str, float],
                             end_loc: Dict[str, float],
                             profile: str = "driving") -> List[Dict[str, float]]:
    """Return a cached route polyline between two normalized latitude/longitude points."""
    if not start_loc or not end_loc:
        return []
    if not (_coords_valid(start_loc.get("lat"), start_loc.get("lng")) and
            _coords_valid(end_loc.get("lat"), end_loc.get("lng"))):
        return []
    if _haversine_m(start_loc["lat"], start_loc["lng"],
                    end_loc["lat"], end_loc["lng"]) < 1.0:
        return [start_loc, end_loc]
    if _operation_area_signature()[0] is None:
        return [start_loc, end_loc]

    route = _fetch_route_cached(
        start_loc["lat"], start_loc["lng"],
        end_loc["lat"], end_loc["lng"],
        profile
    )
    if route:
        return route
    if not route:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning("Local route failed %s -> %s", start_loc, end_loc)
    # fallback: straight line
    return [start_loc, end_loc]


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Calculate the great-circle distance between two coordinates in meters."""
    R = 6371000.0  # Radius of the Earth in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2 +
        math.cos(phi1) * math.cos(phi2) *
        math.sin(dl / 2) ** 2
    )
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def polyline_distance_m(coords: List[Dict[str, float]]) -> float:
    """Calculate total distance for a route polyline in meters."""
    if not coords or len(coords) < 2:
        return 0.0

    dist = 0.0
    for i in range(1, len(coords)):
        p1 = coords[i - 1]
        p2 = coords[i]
        if not _coords_valid(p1.get("lat"), p1.get("lng")):
            continue
        if not _coords_valid(p2.get("lat"), p2.get("lng")):
            continue
        dist += _haversine_m(
            p1["lat"], p1["lng"],
            p2["lat"], p2["lng"]
        )
    return dist

from datetime import timezone

def _parse_iso_as_utc(s: Optional[str]) -> Optional[datetime]:
    """Parse a timestamp into a timezone-aware UTC datetime for Gantt calculations."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt

def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as an ISO UTC string for frontend payloads."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()

def _loc_from_stop(stop: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Extract a normalized location from a CAB trip stop."""
    if not isinstance(stop, dict):
        return None
    loc = stop.get("location") or stop.get("currentLocation")
    if not isinstance(loc, dict):
        return None
    lat = loc.get("latitude")
    lng = loc.get("longitude")
    if lat is None or lng is None:
        return None
    return {"lat": float(lat), "lng": float(lng)}

def _parse_dt_any(value: Any) -> Optional[datetime]:
    """Parse timestamps from several output formats into UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        v = float(value)
        # ignore very small numbers (likely durations)
        if v < 1e8:
            return None
        if v > 1e12:
            v = v / 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        return _parse_iso_as_utc(value)
    return None

def _loc_from_latlng(obj: Any) -> Optional[Dict[str, float]]:
    """Normalize any object containing latitude and longitude fields into a location dict."""
    if not isinstance(obj, dict):
        return None
    lat = obj.get("lat")
    lng = obj.get("lng")
    if lat is None:
        lat = obj.get("latitude") if obj.get("latitude") is not None else obj.get("Latitude")
    if lng is None:
        lng = obj.get("longitude") if obj.get("longitude") is not None else obj.get("Longitude")
    if lat is None or lng is None:
        return None
    return {"lat": float(lat), "lng": float(lng)}

def _loc_from_chaining_stop(stop: Any, prefer: str = "start") -> Optional[Dict[str, float]]:
    """Extract the preferred start or end location from a PRO chaining stop."""
    if not isinstance(stop, dict):
        return None
    prefer = (prefer or "start").lower()
    if prefer == "start":
        for k in ("startLocation", "StartLocation", "start", "Start", "from", "fromLocation", "startLoc"):
            loc = _loc_from_latlng(stop.get(k))
            if loc:
                return loc
    if prefer == "end":
        for k in ("endLocation", "EndLocation", "end", "End", "to", "toLocation", "endLoc"):
            loc = _loc_from_latlng(stop.get(k))
            if loc:
                return loc
    loc = _loc_from_latlng(stop.get("location") or stop.get("Location"))
    if loc:
        return loc
    return None

def _get_time_from_stop(stop: Any, prefer: str = "start") -> Optional[datetime]:
    """Extract the best matching timestamp from a chaining stop for a start or end event."""
    if not isinstance(stop, dict):
        return None
    prefer = (prefer or "start").lower()
    if prefer == "start":
        keys = ("startTime", "StartTime", "start", "Start", "departure", "Departure",
                "arrival", "Arrival", "time", "Time", "timestamp", "Timestamp")
    else:
        keys = ("endTime", "EndTime", "end", "End", "arrival", "Arrival",
                "departure", "Departure", "time", "Time", "timestamp", "Timestamp")
    for k in keys:
        if k in stop:
            dt = _parse_dt_any(stop.get(k))
            if dt:
                return dt
    return None

def _get_duration_sec(stop: Any) -> Optional[float]:
    """Read a chaining stop duration in seconds from supported field names."""
    if not isinstance(stop, dict):
        return None
    for k in ("drivingTime", "driving_time", "duration", "travelTime",
              "time", "timeSec", "durationSec", "drivingTimeSec"):
        if k in stop:
            v = stop.get(k)
            if isinstance(v, (int, float)):
                val = float(v)
                if val > 1e6:
                    val = val / 1000.0
                return max(val, 0.0)
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    continue
                try:
                    val = float(s.replace(",", "."))
                    if val > 1e6:
                        val = val / 1000.0
                    return max(val, 0.0)
                except Exception:
                    pass
                # Accept HH:MM:SS(.mmm)
                m_hms = re.fullmatch(r"(\d+):([0-5]?\d):([0-5]?\d)(?:\.\d+)?", s)
                if m_hms:
                    h = int(m_hms.group(1))
                    m = int(m_hms.group(2))
                    sec = float(m_hms.group(3))
                    return float(h * 3600 + m * 60) + sec
                # Accept ISO-8601 duration like PT5M30S / PT120S / PT1H2M3S
                m_iso = re.fullmatch(
                    r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
                    s,
                    re.IGNORECASE
                )
                if m_iso:
                    h = float(m_iso.group(1) or 0.0)
                    m = float(m_iso.group(2) or 0.0)
                    sec = float(m_iso.group(3) or 0.0)
                    return max(h * 3600.0 + m * 60.0 + sec, 0.0)
    return None

def _extract_pro_id(entry: Dict[str, Any]) -> str:
    """Resolve a stable PRO vehicle identifier from common output fields."""
    for k in ("id", "label", "vehicleId", "proId", "proGuid", "Guid", "scheduleId"):
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k in ("vehicle", "pro", "proSchedule"):
        vobj = entry.get(k)
        if isinstance(vobj, dict):
            for kk in ("id", "label", "vehicleId", "proId", "Guid", "scheduleId"):
                vv = vobj.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return "Pro"

def _get_chaining_stops(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return chaining stops from a PRO entry using supported field names."""
    for k in ("chainingStops", "ChainingStops", "chainingStop", "ChainingStop", "stops", "Stops"):
        stops = entry.get(k)
        if isinstance(stops, list):
            return [s for s in stops if isinstance(s, dict)]
    return []

def _split_chain_key(key: str) -> Tuple[str, Optional[str]]:
    """Split a chaining key into its base id and optional start/end suffix."""
    k = (key or "").strip()
    if not k:
        return "", None
    m = re.search(r"(.+?)[-_]?(start|end)$", k, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2).lower()
    return k, None

def _parse_station_pair(base_key: str) -> Tuple[Optional[str], Optional[str]]:
    """Infer start and end station names from a chaining key when possible."""
    if not base_key:
        return None, None
    m = re.match(r"(.+?)_(.+?)_(\d+)$", base_key)
    if m:
        return m.group(1), m.group(2)
    parts = base_key.split("_")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None

def _locs_close(a: Optional[Dict[str, float]], b: Optional[Dict[str, float]], tol: float = 1e-6) -> bool:
    """Return whether two normalized locations are effectively identical."""
    if not a or not b:
        return False
    return abs(a["lat"] - b["lat"]) <= tol and abs(a["lng"] - b["lng"]) <= tol

def _build_station_location_map(stops: List[Dict[str, Any]]) -> Dict[str, Tuple[Dict[str, float], int]]:
    """Build a lookup from station names to observed chaining-stop locations."""
    station_map: Dict[str, Tuple[Dict[str, float], int]] = {}
    for stop in stops:
        key = str(stop.get("key") or stop.get("Key") or "").strip()
        base_key, suffix = _split_chain_key(key)
        if not base_key:
            continue
        start_name, end_name = _parse_station_pair(base_key)
        loc = _loc_from_chaining_stop(stop, "start") or _loc_from_chaining_stop(stop, "end")
        if not loc:
            continue
        if suffix == "start" and start_name:
            prev = station_map.get(start_name)
            if not prev or prev[1] < 2:
                station_map[start_name] = (loc, 2)
        elif suffix == "end" and end_name:
            prev = station_map.get(end_name)
            if not prev:
                station_map[end_name] = (loc, 1)
    return station_map

def _infer_pro_type(base_key: str, start_stop: Dict[str, Any], end_stop: Dict[str, Any]) -> str:
    """Classify a PRO trip as coupling, decoupling, or generic chaining from key and stop hints."""
    hints = [
        base_key or "",
        str(start_stop.get("type") or start_stop.get("stopType") or ""),
        str(end_stop.get("type") or end_stop.get("stopType") or "")
    ]
    text = " ".join(hints).lower()
    if any(t in text for t in ("attach", "couple", "dock", "connect", "join")):
        return "pro_coupling"
    if any(t in text for t in ("detach", "decouple", "undock", "disconnect", "release", "unjoin")):
        return "pro_decoupling"
    return "pro_trip"

# --------------------------------------------------------------------------
# Core Logic (updated to use routing)
# --------------------------------------------------------------------------
def extract_vehicle_trips(cab_json: Any) -> List[Dict[str, Any]]:
    """Extract CAB customer, empty-drive, and charging tasks for the fleet Gantt view."""
    if not isinstance(cab_json, list):
        return []
    gantt_data: List[Dict[str, Any]] = []
    for entry in cab_json:
        if not isinstance(entry, dict):
            continue
        vehicle = entry.get("vehicle", {}) or {}
        vid = vehicle.get("id") or vehicle.get("label") or "UnknownCab"
        stops = entry.get("tripStops", []) or []
        if not isinstance(stops, list):
            continue
        first_row = len(gantt_data)
        for i in range(len(stops)):
            cur = stops[i]
            if not isinstance(cur, dict):
                continue
            prev = stops[i - 1] if i > 0 and isinstance(stops[i - 1], dict) else {}
            stype = str(cur.get("stopType", "")).lower()
            if stype == "pickup":
                trip_guid = cur.get("tripGuid") or ""
                dropoff_idx = None
                for j in range(i + 1, len(stops)):
                    s2 = stops[j]
                    if not isinstance(s2, dict):
                        continue
                    if (str(s2.get("stopType", "")).lower() == "dropoff" and
                        (not trip_guid or s2.get("tripGuid") == trip_guid)):
                        dropoff_idx = j
                        break
                prev_idx = i - 1
                if prev_idx >= 0:
                    prev = stops[prev_idx]
                    if (isinstance(prev, dict) and
                        str(prev.get("stopType", "")).lower() == "dropoff"):
                        pickup_arrival = _parse_iso_as_utc(
                            cur.get("arrival") or cur.get("departure")
                        )
                        driving_sec = float(cur.get("drivingTime") or 0)
                        if pickup_arrival and driving_sec > 0:
                            start_drive = pickup_arrival - timedelta(seconds=driving_sec)
                            end_drive = pickup_arrival
                        else:
                            start_drive = None
                            end_drive = pickup_arrival
                        prev_loc = _loc_from_stop(prev)
                        cur_loc = _loc_from_stop(cur)
                        coords = get_route_between_points(prev_loc, cur_loc) \
                            if (prev_loc and cur_loc) else []
                        if coords and end_drive:
                            empty_id = f"empty_{vid}_{i}"
                            gantt_data.append({
                                "Vehicle": vid,
                                "Task": empty_id,
                                "Start": iso_utc(start_drive or end_drive),
                                "Finish": iso_utc(end_drive),
                                "type": "to_pickup",
                                "tripGuid": empty_id,
                                "vehicleType": "cab",
                                "coords": coords,
                                "remainingEnergyStart": prev.get("remainingEnergy"),
                                "remainingEnergyEnd": cur.get("remainingEnergy")
                            })
                if dropoff_idx is not None:
                    dropoff = stops[dropoff_idx]
                    start = _parse_iso_as_utc(cur.get("arrival") or cur.get("departure"))
                    end = _parse_iso_as_utc(dropoff.get("departure") or dropoff.get("arrival"))
                    if start and end and end > start:
                        pickup_loc = _loc_from_stop(cur)
                        drop_loc = _loc_from_stop(dropoff)
                        coords = get_route_between_points(pickup_loc, drop_loc) \
                            if (pickup_loc and drop_loc) else []
                        the_trip_id = (cur.get("userGuid") or cur.get("tripGuid") or
                                       dropoff.get("userGuid") or dropoff.get("tripGuid") or
                                       f"trip_{vid}_{i}")
                        gantt_data.append({
                            "Vehicle": vid,
                            "Task": the_trip_id,
                            "Start": iso_utc(start),
                            "Finish": iso_utc(end),
                            "type": "customer_trip",
                            "tripGuid": the_trip_id,
                            "vehicleType": "cab",
                            "coords": coords,
                            "remainingEnergyStart": prev.get("remainingEnergy"),
                            "remainingEnergyEnd": dropoff.get("remainingEnergy")
                        })
            elif stype == "charging":
                start = _parse_iso_as_utc(cur.get("arrival") or cur.get("departure"))
                end = _parse_iso_as_utc(cur.get("departure") or cur.get("arrival"))
                if start and end and end > start:
                    charging_loc = _loc_from_stop(cur)
                    coords = [charging_loc] if charging_loc else []
                    charging_id = f"charging_{vid}_{i}"
                    gantt_data.append({
                        "Vehicle": vid,
                        "Task": charging_id,
                        "Start": iso_utc(start),
                        "Finish": iso_utc(end),
                        "type": "charging",
                        "tripGuid": charging_id,
                        "vehicleType": "cab",
                        "coords": coords,
                        "remainingEnergyStart": prev.get("remainingEnergy"),
                        "remainingEnergyEnd": cur.get("remainingEnergy")
                    })
        # A cab without a customer trip gets one marker row spanning its schedule, so the fleet
        # Gantt shows it as an idle lane.
        if not any(r["type"] == "customer_trip" for r in gantt_data[first_row:]):
            times = [
                t for s in stops if isinstance(s, dict)
                for t in (_parse_iso_as_utc(s.get("arrival")), _parse_iso_as_utc(s.get("departure"))) if t
            ]
            if times:
                gantt_data.append({
                    "Vehicle": vid,
                    "Task": f"idle_{vid}",
                    "Start": iso_utc(min(times)),
                    "Finish": iso_utc(max(times)),
                    "type": "idle_cab",
                    "tripGuid": f"idle_{vid}",
                    "vehicleType": "cab",
                    "coords": [],
                    "remainingEnergyStart": None,
                    "remainingEnergyEnd": None
                })
    return gantt_data

def extract_pro_trips(pro_json: Any) -> List[Dict[str, Any]]:
    """Extract PRO coupling, decoupling, and chaining tasks for the fleet Gantt view."""
    entries: List[Dict[str, Any]] = []
    if isinstance(pro_json, list):
        entries = [e for e in pro_json if isinstance(e, dict)]
    elif isinstance(pro_json, dict):
        for k in ("pros", "proSchedules", "vehicles"):
            v = pro_json.get(k)
            if isinstance(v, list):
                entries = [e for e in v if isinstance(e, dict)]
                break
        if not entries:
            entries = [pro_json]

    gantt_data: List[Dict[str, Any]] = []
    for entry in entries:
        pro_id = _extract_pro_id(entry)
        stops = _get_chaining_stops(entry)
        if not stops:
            continue
        station_map = _build_station_location_map(stops)

        pending: Dict[str, Dict[str, Any]] = {}

        for stop in stops:
            key = str(stop.get("key") or stop.get("Key") or "").strip()
            base_key, suffix = _split_chain_key(key)
            if not base_key:
                continue
            if suffix == "start" or stop.get("isStart") is True:
                pending[base_key] = stop
                continue
            if suffix == "end" or stop.get("isEnd") is True:
                start_stop = pending.pop(base_key, None)
                if start_stop is None:
                    continue
                end_stop = stop

                start_dt = _get_time_from_stop(start_stop, "start")
                end_dt = _get_time_from_stop(end_stop, "end")
                duration_sec = _get_duration_sec(end_stop) or _get_duration_sec(start_stop)
                if start_dt is None and end_dt is not None and duration_sec:
                    start_dt = end_dt - timedelta(seconds=duration_sec)
                if end_dt is None and start_dt is not None and duration_sec:
                    end_dt = start_dt + timedelta(seconds=duration_sec)

                if not start_dt or not end_dt or end_dt <= start_dt:
                    continue

                start_loc = _loc_from_chaining_stop(start_stop, "start") or _loc_from_chaining_stop(start_stop, "end")
                end_loc = _loc_from_chaining_stop(end_stop, "end") or _loc_from_chaining_stop(end_stop, "start")
                if (not start_loc or not end_loc) or _locs_close(start_loc, end_loc):
                    s_name, e_name = _parse_station_pair(base_key)
                    if s_name and s_name in station_map:
                        start_loc = station_map[s_name][0]
                    if e_name and e_name in station_map:
                        end_loc = station_map[e_name][0]
                coords = get_route_between_points(start_loc, end_loc) if (start_loc and end_loc) else []

                trip_type = _infer_pro_type(base_key, start_stop, end_stop)
                trip_id = base_key or f"pro_trip_{pro_id}"
                gantt_data.append({
                    "Vehicle": pro_id,
                    "Task": trip_id,
                    "Start": iso_utc(start_dt),
                    "Finish": iso_utc(end_dt),
                    "type": trip_type,
                    "tripGuid": trip_id,
                    "vehicleType": "pro",
                    "coords": coords
                })

                pending.pop(base_key, None)

    return gantt_data

def _find_output_file(base_dir: str, kind: str, num_cabs: int, prefix: Optional[str] = None) -> Optional[str]:
    """Find the newest uploaded CAB or PRO output file matching the selected fleet size.

    When *prefix* is given only files whose name starts with that prefix are
    considered.  This is necessary when multiple scenarios share the same folder.
    """
    if not os.path.isdir(base_dir):
        return None
    candidates: List[str] = []
    pattern = re.compile(rf"(?:^|_)output_{re.escape(kind)}_(\d+)\.json$", re.IGNORECASE)
    prefix_lower = prefix.lower() if prefix else None
    for fname in os.listdir(base_dir):
        lf = fname.lower()
        if not lf.endswith(".json"):
            continue
        if prefix_lower and not lf.startswith(prefix_lower):
            continue
        m = pattern.search(lf)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except Exception:
            continue
        if idx == int(num_cabs):
            candidates.append(os.path.join(base_dir, fname))
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]

def build_gantt_payload(num_cabs, base_dir: Optional[str] = None, prefix: Optional[str] = None):
    """Load matching CAB and PRO output files and build the combined fleet Gantt payload."""
    base_dir = base_dir or UPLOADS_DIR
    cab_path = _find_output_file(base_dir, "cab", num_cabs, prefix)
    pro_path = _find_output_file(base_dir, "pro", num_cabs, prefix)

    if not cab_path and not pro_path:
        raise FileNotFoundError(
            f"Keine passende CAB/PRO-Datei für {num_cabs} gefunden in '{base_dir}'."
        )

    gantt_data: List[Dict[str, Any]] = []
    if cab_path:
        with open(cab_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        gantt_data.extend(extract_vehicle_trips(raw_data))

    if pro_path:
        with open(pro_path, "r", encoding="utf-8") as f:
            raw_pro = json.load(f)
        gantt_data.extend(extract_pro_trips(raw_pro))

    return gantt_data

def _energy_provided_to_cabs(cab_path: Optional[str], trip_keys: set[str]) -> float:
    """Sum energy a set of a PRO's own trips (by base chaining key) charged into Cabs.

    Read from each Cab's own Unchaining stop, the only place this energy is actually
    recorded (a Cab's battery gaining charge while chained), matched back to this PRO's
    trips via guidChainRouteSegment, the same trip-key convention used elsewhere in this
    file (see _pro_for_relocation)."""
    if not cab_path or not trip_keys or not os.path.isfile(cab_path):
        return 0.0
    try:
        with open(cab_path, "r", encoding="utf-8") as f:
            raw_cab = json.load(f)
    except Exception:
        return 0.0
    entries = raw_cab if isinstance(raw_cab, list) else ([raw_cab] if isinstance(raw_cab, dict) else [])
    total_wh = 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vehicle = entry.get("vehicle") if isinstance(entry.get("vehicle"), dict) else entry
        stops = entry.get("tripStops") or vehicle.get("tripStops") or []
        if not isinstance(stops, list):
            continue
        for stop in stops:
            if not isinstance(stop, dict) or str(stop.get("stopType", "")).lower() != "unchaining":
                continue
            seg = (stop.get("guidChainRouteSegment") or stop.get("guidChainRouteSegement") or
                   stop.get("chainRouteSegmentGuid") or stop.get("chainRouteSegment") or "")
            base_key, _ = _split_chain_key(str(seg))
            if base_key in trip_keys:
                total_wh += float(stop.get("chargedEnergy") or 0.0)
    return total_wh


def _build_pro_vehicle_payload(file_path: Optional[str], vehicle_id: str, cab_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build a vehicle-detail payload for one PRO entry if it exists in the selected output file."""
    if not file_path:
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    entries: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]
    elif isinstance(raw, dict):
        for k in ("pros", "proSchedules", "vehicles"):
            v = raw.get(k)
            if isinstance(v, list):
                entries = [e for e in v if isinstance(e, dict)]
                break
        if not entries:
            entries = [raw]

    target_entry = None
    for e in entries:
        pro_id = _extract_pro_id(e)
        if pro_id and (pro_id == vehicle_id or pro_id.lower() == str(vehicle_id).lower()):
            target_entry = e
            break
        for s in _get_chaining_stops(e):
            vs = s.get("vehicleId") or s.get("proId") or s.get("vehicle") or ""
            if isinstance(vs, str) and vs and (vs == vehicle_id or vs.lower() == str(vehicle_id).lower()):
                target_entry = e
                break
        if target_entry:
            break

    if not target_entry:
        return None

    pro_id = _extract_pro_id(target_entry)
    vehicle_obj = target_entry.get("vehicle") if isinstance(target_entry.get("vehicle"), dict) else {}
    stops = _get_chaining_stops(target_entry)
    legs: List[Dict[str, Any]] = []
    total_distance = 0.0
    total_driving_time = 0.0
    total_chained_cabs = 0.0
    station_map = _build_station_location_map(stops)

    pending: Dict[str, Dict[str, Any]] = {}

    chained_by_key: Dict[str, float] = {}
    start_epoch_by_key: Dict[str, int] = {}
    for stop in stops:
        if not isinstance(stop, dict):
            continue
        key = str(stop.get("key") or stop.get("Key") or "").strip()
        base_key, suffix = _split_chain_key(key)
        if not base_key:
            continue
        # Deadhead legs (plain empty repositioning between service trips, no scheduled
        # stop a Cab could chain onto) are excluded, not counted as an empty trip: a Cab
        # was never eligible to use them in the first place. Output files without a
        # tripType field are not filtered by trip type, so their trip counts include
        # deadhead legs.
        if stop.get("tripType") == "DT":
            continue
        v = stop.get("chainedCabs")
        try:
            v_num = float(v)
        except Exception:
            v_num = None
        if v_num is None:
            continue
        # Written unconditionally, including chainedCabs 0, so chained_by_key covers every
        # leg, empty ones included, and len(chained_by_key) is the true trip count.
        chained_by_key[base_key] = max(chained_by_key.get(base_key, 0.0), v_num)
        if suffix == "start" and base_key not in start_epoch_by_key:
            start_dt = _parse_dt_any(stop.get("arrival") or stop.get("departure"))
            if start_dt is not None:
                start_epoch_by_key[base_key] = int(start_dt.timestamp())
    if chained_by_key:
        total_chained_cabs = float(sum(chained_by_key.values()))

    # chained_by_key holds every one of this PRO's own trips, chained or empty (every
    # stop always carries a chainedCabs value, including deadhead legs), so it doubles
    # as the trip count.
    total_trips = float(len(chained_by_key))
    empty_trips = float(sum(1 for v in chained_by_key.values() if v <= 0))
    empty_trip_ratio = (empty_trips / total_trips) if total_trips > 0 else 0.0
    max_convoy_length = float(max(chained_by_key.values())) if chained_by_key else 0.0

    # The chain/unchain handshake time is a bare duration value on these stops (see
    # custom_sim/utils_cs.py's build_pro_output); the PRO itself never waits, so arrival
    # equals departure and the duration field is read directly, from every stop with a
    # chained cab.
    chaining_service_time_s = sum(
        float(s.get("duration") or 0.0) for s in stops
        if isinstance(s, dict) and float(s.get("chainedCabs") or 0) > 0
    )

    # A Cab's own Unchaining stop keys its guidChainRouteSegment by this trip's
    # source_trip_id normally, but falls back to f"{pro_id}_{trip_start_epoch}" whenever
    # early unchaining (on by default) truncates the Cab's own end time short of the
    # trip's real end, which fails the start/end timing match chain_segment_id (in
    # custom_sim/utils_cs.py) uses to find the trip. The trip's start time is unaffected
    # by early unchaining, so both forms are matched here.
    chained_trip_keys = {k for k, v in chained_by_key.items() if v > 0}
    chained_trip_keys |= {
        f"{pro_id}_{start_epoch_by_key[k]}" for k in chained_trip_keys if k in start_epoch_by_key
    }
    energy_provided_to_cabs_wh = _energy_provided_to_cabs(cab_path, chained_trip_keys)

    for stop in stops:
        key = str(stop.get("key") or stop.get("Key") or "").strip()
        base_key, suffix = _split_chain_key(key)
        if not base_key:
            continue
        if suffix == "start" or stop.get("isStart") is True:
            pending[base_key] = stop
            continue
        if suffix == "end" or stop.get("isEnd") is True:
            start_stop = pending.pop(base_key, None)
            if start_stop is None:
                continue
            end_stop = stop

            start_dt = _get_time_from_stop(start_stop, "start")
            end_dt = _get_time_from_stop(end_stop, "end")
            duration_sec = _get_duration_sec(end_stop) or _get_duration_sec(start_stop)
            if start_dt is None and end_dt is not None and duration_sec:
                start_dt = end_dt - timedelta(seconds=duration_sec)
            if end_dt is None and start_dt is not None and duration_sec:
                end_dt = start_dt + timedelta(seconds=duration_sec)

            if not start_dt or not end_dt or end_dt <= start_dt:
                continue

            start_loc = _loc_from_chaining_stop(start_stop, "start") or _loc_from_chaining_stop(start_stop, "end")
            end_loc = _loc_from_chaining_stop(end_stop, "end") or _loc_from_chaining_stop(end_stop, "start")
            if (not start_loc or not end_loc) or _locs_close(start_loc, end_loc):
                s_name, e_name = _parse_station_pair(base_key)
                if s_name and s_name in station_map:
                    start_loc = station_map[s_name][0]
                if e_name and e_name in station_map:
                    end_loc = station_map[e_name][0]
            coords = get_route_between_points(start_loc, end_loc) if (start_loc and end_loc) else []

            trip_type = _infer_pro_type(base_key, start_stop, end_stop)
            trip_id = base_key or f"pro_trip_{pro_id}"

            legs.append({
                "start": iso_utc(start_dt),
                "end": iso_utc(end_dt),
                "type": trip_type,
                "label": trip_id,
                "tripGuid": trip_id,
                "coords": coords
            })

            total_driving_time += max(0.0, (end_dt - start_dt).total_seconds())
            if coords:
                total_distance += polyline_distance_m(coords)

            pending.pop(base_key, None)

    def _pick_pro_location() -> Optional[Dict[str, float]]:
        """Choose the best available anchor location for PRO-only detail payloads."""
        for obj in (
            target_entry.get("currentLocation"),
            vehicle_obj.get("currentLocation"),
            vehicle_obj.get("initialLocation"),
            target_entry.get("initialLocation"),
        ):
            loc = _loc_from_latlng(obj)
            if loc:
                return loc
        for s in stops:
            loc = _loc_from_chaining_stop(s, "start") or _loc_from_chaining_stop(s, "end")
            if loc:
                return loc
        return None

    def _stop_arrival(stop: Any) -> Optional[datetime]:
        """Read the arrival-like timestamp from a PRO chaining stop."""
        if not isinstance(stop, dict):
            return None
        return _parse_dt_any(stop.get("arrival") or stop.get("departure") or stop.get("time"))

    def _stop_departure(stop: Any) -> Optional[datetime]:
        """Read the departure-like timestamp from a PRO chaining stop."""
        if not isinstance(stop, dict):
            return None
        return _parse_dt_any(stop.get("departure") or stop.get("arrival") or stop.get("time"))

    base_loc = _pick_pro_location()
    first_stop = stops[0] if stops else None
    last_stop = stops[-1] if stops else None
    first_loc = _loc_from_chaining_stop(first_stop, "start") or _loc_from_chaining_stop(first_stop, "end") if first_stop else None
    last_loc = _loc_from_chaining_stop(last_stop, "end") or _loc_from_chaining_stop(last_stop, "start") if last_stop else None

    start_anchor = _stop_arrival(first_stop)
    if start_anchor:
        start_block_start = start_anchor - timedelta(minutes=15)
        legs.insert(0, {
            "start": iso_utc(start_block_start),
            "end": iso_utc(start_anchor),
            "type": "pro_start",
            "label": "Start",
            "tripGuid": f"pro_start_{pro_id}",
            "coords": [first_loc or base_loc] if (first_loc or base_loc) else []
        })

    end_anchor = _stop_departure(last_stop)
    if end_anchor:
        end_block_end = end_anchor + timedelta(minutes=15)
        legs.append({
            "start": iso_utc(end_anchor),
            "end": iso_utc(end_block_end),
            "type": "pro_end",
            "label": "Ende",
            "tripGuid": f"pro_end_{pro_id}",
            "coords": [last_loc or base_loc] if (last_loc or base_loc) else []
        })

    payload = {
        "vehicleId": pro_id,
        "file": os.path.basename(file_path),
        "vehicleType": "pro",
        "trips": legs,
        "kpis": {
            "totalDistance": total_distance,
            "totalDrivingTime": total_driving_time,
            "totalConsumedEnergy": None,
            "remainingEnergyLastDepot": None,
            "totalChainedCabs": total_chained_cabs,
            "transportedCustomers": 0,
            "convoyTrips": 0,
            "convoyDistance": 0.0,
            "stationaryChargedEnergy": 0.0,
            "convoyChargedEnergy": 0.0,
            "stationaryChargingTime": 0.0,
            "utilization": 0.0,
            "totalTrips": total_trips,
            "emptyTrips": empty_trips,
            "emptyTripRatio": empty_trip_ratio,
            "maxConvoyLength": max_convoy_length,
            "chainingServiceTime": chaining_service_time_s,
            "energyProvidedToCabs": energy_provided_to_cabs_wh
        }
    }
    return _attach_detailed_kpi_aliases(payload)

def build_vehicle_payload(num_cabs: int, vehicle_id: str, base_dir: Optional[str] = None, prefix: Optional[str] = None) -> Dict[str, Any]:
    """Build route, timeline, and KPI details for one selected CAB or PRO vehicle."""
    import uuid
    base_dir = base_dir or UPLOADS_DIR
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(f"Upload-Ordner '{base_dir}' nicht gefunden.")
    cab_path = _find_output_file(base_dir, "cab", num_cabs, prefix)
    pro_path = _find_output_file(base_dir, "pro", num_cabs, prefix)

    if cab_path:
        with open(cab_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        entries = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    else:
        entries = []

    target_entry = None
    for e in entries:
        if not isinstance(e, dict):
            continue
        vobj = e.get("vehicle") if isinstance(e.get("vehicle"), dict) else e
        vid = (vobj.get("id") or vobj.get("label") or vobj.get("vehicleSchedule") or
               vobj.get("vehicleId") or "").strip()
        if vid and (vid == vehicle_id or vid.lower() == str(vehicle_id).lower()):
            target_entry = e
            break
        trip_stops = e.get("tripStops") or []
        if isinstance(trip_stops, list):
            for ts in trip_stops:
                if not isinstance(ts, dict):
                    continue
                vs = (ts.get("vehicleSchedule") or ts.get("vehicleId") or "")
                if vs and (vs == vehicle_id or vs.lower() == str(vehicle_id).lower()):
                    target_entry = e
                    break
        if target_entry:
            break
    if not target_entry:
        pro_payload = _build_pro_vehicle_payload(pro_path, vehicle_id, cab_path)
        if pro_payload is not None:
            return pro_payload
        if not cab_path and not pro_path:
            raise FileNotFoundError(f"Keine passende CAB/PRO-Datei für {num_cabs} gefunden in '{base_dir}'.")
        raise FileNotFoundError(f"Fahrzeug '{vehicle_id}' wurde in '{base_dir}' nicht gefunden.")

    vehicle = target_entry.get("vehicle") if isinstance(target_entry.get("vehicle"), dict) else target_entry
    trip_stops = target_entry.get("tripStops") or vehicle.get("tripStops") or []
    if not isinstance(trip_stops, list):
        trip_stops = []

    pro_chain_map: Dict[str, str] = {}
    if pro_path and os.path.isfile(pro_path):
        try:
            with open(pro_path, "r", encoding="utf-8") as f:
                raw_pro = json.load(f)
            pro_entries: List[Dict[str, Any]] = []
            if isinstance(raw_pro, list):
                pro_entries = [e for e in raw_pro if isinstance(e, dict)]
            elif isinstance(raw_pro, dict):
                for k in ("pros", "proSchedules", "vehicles"):
                    v = raw_pro.get(k)
                    if isinstance(v, list):
                        pro_entries = [e for e in v if isinstance(e, dict)]
                        break
                if not pro_entries:
                    pro_entries = [raw_pro]
            for e in pro_entries:
                pro_id = _extract_pro_id(e)
                for s in _get_chaining_stops(e):
                    if not isinstance(s, dict):
                        continue
                    key = s.get("key") or s.get("Key")
                    if not key:
                        continue
                    base_key, _ = _split_chain_key(str(key))
                    if not base_key:
                        continue
                    vs = s.get("vehicleSchedule") or s.get("vehicleId") or pro_id
                    if vs and base_key not in pro_chain_map:
                        pro_chain_map[base_key] = str(vs)
        except Exception:
            pro_chain_map = {}

    def _iso(dt: Optional[datetime]) -> Optional[str]:
        """Serialize a local timeline datetime to an ISO UTC string."""
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    
    def add_energy_point(timeline, time_dt, energy):
        """Append one energy timeline point when both timestamp and energy are available."""
        if time_dt and energy is not None:
            timeline.append({
                "time": iso_utc(time_dt),
                "energy": energy
            })
            
    def energy_delta(start, end):
        """Calculate positive energy consumption between two energy values."""
        if start is None or end is None:
            return 0.0
        try:
            return max(0.0, float(start) - float(end))
        except Exception:
            return 0.0

    def _stop_type(stop: Optional[Dict[str, Any]]) -> str:
        """Normalize the stop type for compact relocation checks."""
        return str(stop.get("stopType", "")).strip().lower() if isinstance(stop, dict) else ""

    def _is_relocation(stop: Optional[Dict[str, Any]]) -> bool:
        """Return whether a stop belongs to convoy relocation handling."""
        return _stop_type(stop) in ("relocation", "chaining", "unchaining")

    def _is_unchaining(stop: Optional[Dict[str, Any]]) -> bool:
        """Return whether a relocation stop specifically marks unchaining."""
        return _stop_type(stop) == "unchaining"

    def _dt_close(a: Optional[datetime], b: Optional[datetime], tol_sec: float = 1.0) -> bool:
        """Compare two datetimes with a small tolerance for solver timestamp jitter."""
        if a is None or b is None:
            return False
        return abs((a - b).total_seconds()) <= tol_sec

    def _split_relocs_by_window(relocs: List[Dict[str, Any]],
                                start_dt: Optional[datetime],
                                end_dt: Optional[datetime]) -> tuple[list, list]:
        """Separate relocation stops that belong inside a customer trip window from remaining stops."""
        if not relocs:
            return [], []
        if not start_dt or not end_dt or end_dt <= start_dt:
            return [], list(relocs)

        inside: List[Dict[str, Any]] = []
        outside: List[Dict[str, Any]] = []

        idx = 0
        while idx < len(relocs):
            pair = relocs[idx: idx + 2]
            idx += 2
            in_window = False
            for r in pair:
                t1 = _stop_time(r, "arrival")
                t2 = _stop_time(r, "departure")
                if t1 and start_dt <= t1 <= end_dt:
                    in_window = True
                if t2 and start_dt <= t2 <= end_dt:
                    in_window = True
                if _is_unchaining(r) and t2 and _dt_close(t2, start_dt):
                    in_window = True
            if in_window:
                inside.extend(pair)
            else:
                outside.extend(pair)

        return inside, outside

    def _convoy_group_guid(relocs: List[Dict[str, Any]], idx: int) -> str:
        """Build a stable group id for a pair of convoy relocation stops."""
        for r in relocs:
            if not isinstance(r, dict):
                continue
            seg = (r.get("guidChainRouteSegment") or r.get("guidChainRouteSegement") or
                   r.get("chainRouteSegmentGuid") or r.get("chainRouteSegment") or "")
            if seg:
                return f"convoy_{seg}_{idx}"
        return f"convoy_{idx}"

    def _stop_time(stop: Optional[Dict[str, Any]], which: str) -> Optional[datetime]:
        """Read arrival or departure time from a trip stop with fallback field names."""
        if not isinstance(stop, dict):
            return None
        if which == "arrival":
            return _parse_iso_as_utc(stop.get("arrival") or stop.get("departure"))
        return _parse_iso_as_utc(stop.get("departure") or stop.get("arrival"))

    def _num(val: Any) -> Optional[float]:
        """Convert a numeric-like value to float or return None for missing/invalid input."""
        try:
            if val is None:
                return None
            return float(val)
        except Exception:
            return None

    def _charge_target(idx: int) -> Optional[float]:
        """Estimate the expected energy target after a charging stop from the following stop."""
        if idx < 0 or idx >= n:
            return None
        next_stop = trip_stops[idx + 1] if idx + 1 < n else None
        if not isinstance(next_stop, dict):
            return None
        rem = _num(next_stop.get("remainingEnergy"))
        cons = _num(next_stop.get("consumedEnergy"))
        if rem is not None and cons is not None:
            return rem + cons
        if rem is not None:
            return rem
        return None

    def _resolve_stationary_charging_energy(idx: int,
                                            prev_stop: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
        """Infer start and end energy for stationary charging from several possible solver conventions."""
        if idx < 0 or idx >= n:
            return None, None

        stop = trip_stops[idx]
        if not isinstance(stop, dict):
            return None, None

        remaining = _num(stop.get("remainingEnergy"))
        charged = _num(stop.get("chargedEnergy"))
        target_end = _charge_target(idx)
        prev_energy = _num(prev_stop.get("remainingEnergy")) if isinstance(prev_stop, dict) else None
        consumed = _num(stop.get("consumedEnergy"))

        approach_expected = None
        if prev_energy is not None and consumed is not None:
            approach_expected = max(0.0, prev_energy - consumed)

        if charged is None or charged < 0 or remaining is None:
            energy_start = remaining
            energy_end = target_end
            if energy_end is None and charged is not None and energy_start is not None:
                energy_end = energy_start + charged
            if energy_end is None:
                energy_end = full_energy
            if energy_end is None:
                energy_end = energy_start
            return energy_start, energy_end

        candidates = [
            (remaining, remaining + charged),
            (remaining - charged, remaining)
        ]

        def _score(candidate: Tuple[Optional[float], Optional[float]]) -> float:
            """Score a possible charging-energy interpretation against capacity and target hints."""
            start_val, end_val = candidate
            score = 0.0
            if start_val is None or end_val is None:
                return float("inf")
            if start_val < -1 or end_val < -1:
                score += 1_000_000
            if full_energy is not None:
                cap = float(full_energy)
                if start_val > cap + 1:
                    score += abs(start_val - cap) * 10
                if end_val > cap + 1:
                    score += abs(end_val - cap) * 10
            if target_end is not None:
                score += abs(end_val - target_end)
            elif full_energy is not None:
                score += abs(end_val - float(full_energy))
            if approach_expected is not None:
                score += abs(start_val - approach_expected)
            return score

        best_start, best_end = min(candidates, key=_score)
        return best_start, best_end

    def _append_leg(seg_type: str,
                    start_dt: Optional[datetime],
                    end_dt: Optional[datetime],
                    coords: List[Dict[str, float]],
                    label: str,
                    group_guid: str,
                    energy_start: Any,
                    energy_end: Any,
                    count_driving: bool,
                    empty_leg_guid: Optional[str] = None,
                    user_guid: Optional[str] = None,
                    extra_fields: Optional[Dict[str, Any]] = None) -> None:
        """Append one timeline leg and update route distance, driving time, and energy aggregates."""
        nonlocal total_distance, total_driving_time, total_consumed_energy
        if not start_dt or not end_dt or end_dt <= start_dt:
            return
        leg = {
            "start": _iso(start_dt),
            "end": _iso(end_dt),
            "type": seg_type,
            "label": label,
            "tripGuid": group_guid,
            "userGuid": user_guid or "",
            "coords": coords or [],
            "remainingEnergyStart": energy_start,
            "remainingEnergyEnd": energy_end
        }
        if empty_leg_guid:
            leg["emptyLegGuid"] = empty_leg_guid
        if extra_fields:
            leg.update(extra_fields)
        legs.append(leg)
        if energy_start is not None:
            add_energy_point(energy_timeline, start_dt, energy_start)
        if energy_end is not None:
            add_energy_point(energy_timeline, end_dt, energy_end)
        if count_driving:
            total_driving_time += max(0.0, (end_dt - start_dt).total_seconds())
            if coords:
                total_distance += polyline_distance_m(coords)
            if energy_start is not None and energy_end is not None:
                total_consumed_energy += energy_delta(energy_start, energy_end)

    def _pro_for_relocation(stop: Optional[Dict[str, Any]]) -> Optional[str]:
        """Resolve the PRO vehicle associated with a CAB relocation segment."""
        if not isinstance(stop, dict) or not pro_chain_map:
            return None
        seg = (stop.get("guidChainRouteSegment") or stop.get("guidChainRouteSegement") or
               stop.get("chainRouteSegmentGuid") or stop.get("chainRouteSegment") or "")
        if not seg:
            return None
        base, _ = _split_chain_key(str(seg))
        return pro_chain_map.get(base) or pro_chain_map.get(str(seg))

    def _build_segments_with_relocation(start_dt: Optional[datetime],
                                        end_dt: Optional[datetime],
                                        start_loc: Optional[Dict[str, float]],
                                        end_loc: Optional[Dict[str, float]],
                                        base_type: str,
                                        base_label: str,
                                        group_guid: str,
                                        relocs: List[Dict[str, Any]],
                                        energy_start: Any,
                                        energy_end: Any,
                                        empty_leg_guid: Optional[str] = None,
                                        user_guid: Optional[str] = None,
                                        convoy_travel_type: str = "convoy_travel",
                                        convoy_label: str = "Konvoi Fahrt") -> None:
        """Split a base CAB leg around convoy coupling, convoy travel, and decoupling subsegments."""
        if not relocs:
            if base_type:
                coords = get_route_between_points(start_loc, end_loc) if (start_loc and end_loc) else []
                _append_leg(base_type, start_dt, end_dt, coords, base_label, group_guid,
                            energy_start, energy_end, True, empty_leg_guid, user_guid)
            return

        current_time = start_dt
        current_loc = start_loc
        current_energy = energy_start

        idx = 0
        while idx + 1 < len(relocs):
            r1 = relocs[idx]
            r2 = relocs[idx + 1]
            idx += 2

            r1_arr = _stop_time(r1, "arrival")
            r1_dep = _stop_time(r1, "departure")
            r2_arr = _stop_time(r2, "arrival")
            r2_dep = _stop_time(r2, "departure")

            if r1_arr is None:
                r1_arr = r1_dep
            if r1_dep is None:
                r1_dep = r1_arr
            if r2_arr is None:
                r2_arr = r2_dep
            if r2_dep is None:
                r2_dep = r2_arr

            r1_loc = _loc_from_stop(r1) or current_loc
            r2_loc = _loc_from_stop(r2) or r1_loc

            service_start = None
            service_end = None
            service_energy_start = None
            service_energy_end = None
            if convoy_travel_type == "convoy_travel_empty" and _stop_type(r1) == "chaining" and r1_arr:
                chaining_drive_sec = _get_duration_sec(r1)
                if chaining_drive_sec and chaining_drive_sec > 0:
                    service_start = r1_arr - timedelta(seconds=chaining_drive_sec)
                    service_end = r1_arr
                    r1_rem = _num(r1.get("remainingEnergy"))
                    r1_cons = _num(r1.get("consumedEnergy"))
                    if r1_rem is not None and r1_cons is not None:
                        service_energy_start = r1_rem + r1_cons
                        service_energy_end = r1_rem
                    else:
                        service_energy_start = current_energy
                        service_energy_end = r1_rem if r1_rem is not None else current_energy

            if base_type and current_time and r1_arr and r1_arr > current_time:
                base_end = r1_arr
                base_energy_end = r1.get("remainingEnergy")
                split_for_service = bool(service_start and service_end and service_start > current_time)
                service_covers_base_tail = bool(service_start and service_end and service_start <= current_time < service_end)
                if split_for_service:
                    base_end = service_start
                    base_energy_end = service_energy_start
                elif service_covers_base_tail:
                    base_end = current_time
                if base_end and base_end > current_time:
                    coords = []
                    if (current_loc and r1_loc) and not split_for_service:
                        coords = get_route_between_points(current_loc, r1_loc)
                    _append_leg(base_type, current_time, base_end, coords, base_label, group_guid,
                                current_energy, base_energy_end, True, empty_leg_guid, user_guid)

            if service_start and service_end and service_end > service_start:
                svc_energy_end = service_energy_end if service_energy_end is not None else r1.get("remainingEnergy")
                coords = get_route_between_points(current_loc, r1_loc) if (current_loc and r1_loc) else []
                _append_leg(
                    "to_charging",
                    service_start,
                    service_end,
                    coords,
                    "Servicefahrt",
                    group_guid,
                    service_energy_start,
                    svc_energy_end,
                    True,
                    empty_leg_guid,
                    user_guid
                )

            if r1_arr and r1_dep and r1_dep > r1_arr:
                _append_leg("convoy_couple", r1_arr, r1_dep, [r1_loc] if r1_loc else [],
                            "Konvoi Ankopplung", group_guid, r1.get("remainingEnergy"),
                            r1.get("remainingEnergy"), False, empty_leg_guid, user_guid)

            if r1_dep and r2_arr and r2_arr > r1_dep:
                coords = get_route_between_points(r1_loc, r2_loc) if (r1_loc and r2_loc) else []
                pro_vehicle = _pro_for_relocation(r1) or _pro_for_relocation(r2)
                extra = {"proVehicle": pro_vehicle} if pro_vehicle else None
                _append_leg(convoy_travel_type, r1_dep, r2_arr, coords, convoy_label,
                            group_guid, r1.get("remainingEnergy"), r2.get("remainingEnergy"), True,
                            empty_leg_guid, user_guid, extra_fields=extra)

            if r2_arr and r2_dep and r2_dep > r2_arr:
                _append_leg("convoy_decouple", r2_arr, r2_dep, [r2_loc] if r2_loc else [],
                            "Konvoi Abkopplung", group_guid, r2.get("remainingEnergy"),
                            r2.get("remainingEnergy"), False, empty_leg_guid, user_guid)

            current_time = r2_dep or r2_arr or current_time
            current_loc = r2_loc or current_loc
            if r2.get("remainingEnergy") is not None:
                current_energy = r2.get("remainingEnergy")

        if base_type and current_time and end_dt and end_dt > current_time:
            coords = get_route_between_points(current_loc, end_loc) if (current_loc and end_loc) else []
            _append_leg(base_type, current_time, end_dt, coords, base_label, group_guid,
                        current_energy, energy_end, True, empty_leg_guid, user_guid)

    def _append_convoy_only(relocs: List[Dict[str, Any]],
                            convoy_travel_type: str,
                            convoy_label: str) -> None:
        """Append relocation-only convoy segments that are not embedded in a customer or empty leg."""
        if not relocs:
            return
        idx = 0
        pair_idx = 0
        while idx < len(relocs):
            pair = relocs[idx: idx + 2]
            idx += 2
            group_guid = _convoy_group_guid(pair, pair_idx)
            pair_idx += 1
            _build_segments_with_relocation(
                None,
                None,
                None,
                None,
                None,
                "",
                group_guid,
                pair,
                None,
                None,
                None,
                "",
                convoy_travel_type=convoy_travel_type,
                convoy_label=convoy_label
            )

    legs: List[Dict[str, Any]] = []
    energy_timeline: List[Dict[str, Any]] = []
    total_distance = 0.0
    total_driving_time = 0.0
    total_consumed_energy = 0.0
    first_depot_energy = None
    last_depot_energy = None

    for s in trip_stops:
        if not isinstance(s, dict):
            continue
        if str(s.get("stopType", "")).lower() == "depot":
            if s.get("remainingEnergy") is not None:
                if first_depot_energy is None:
                    first_depot_energy = s.get("remainingEnergy")
                last_depot_energy = s.get("remainingEnergy")

    initial_loc = None
    if isinstance(vehicle, dict):
        init = vehicle.get("initialLocation")
        if isinstance(init, dict) and init.get("latitude") is not None and init.get("longitude") is not None:
            initial_loc = {"lat": init.get("latitude"), "lng": init.get("longitude")}

    full_energy = first_depot_energy
    n = len(trip_stops)
    i = 0
    while i < n:
        cur = trip_stops[i]
        if not isinstance(cur, dict):
            i += 1
            continue
        stype = str(cur.get("stopType", "") or "").strip().lower()
        

        if stype == "pickup":
            arrival = _parse_iso_as_utc(cur.get("arrival") or cur.get("departure"))
            add_energy_point(
                energy_timeline,
                arrival,
                cur.get("remainingEnergy")
            )
            driving_sec = cur.get("drivingTime") or 0
            prev = trip_stops[i-1] if i-1 >= 0 else None
            prev_non_reloc_idx = i - 1
            while prev_non_reloc_idx >= 0 and _is_relocation(trip_stops[prev_non_reloc_idx]):
                prev_non_reloc_idx -= 1
            prev_non_reloc = trip_stops[prev_non_reloc_idx] if prev_non_reloc_idx >= 0 else None
            start_drive = None
            if arrival and isinstance(driving_sec, (int, float)) and driving_sec >= 0:
                start_drive = arrival - timedelta(seconds=float(driving_sec))
            if start_drive and prev:
                prev_type = _stop_type(prev)
                if prev_type == "charging":
                    energy_start = _charge_target(i - 1)
                    if energy_start is None:
                        energy_start = prev.get("remainingEnergy")
                else:
                    energy_start = prev.get("remainingEnergy")
                add_energy_point(energy_timeline, start_drive, energy_start)

            prev_type = _stop_type(prev_non_reloc)
            if prev_type != "pickup" and arrival and start_drive:
                start_loc = _loc_from_stop(prev_non_reloc) or initial_loc
                end_loc = _loc_from_stop(cur)
                start_time = start_drive
                end_time = arrival
                relocs = []
                range_start = prev_non_reloc_idx + 1 if prev_non_reloc_idx >= 0 else 0
                for k in range(range_start, i):
                    if _is_relocation(trip_stops[k]):
                        relocs.append(trip_stops[k])

                relocs_in, relocs_out = _split_relocs_by_window(relocs, start_time, end_time)
                if prev_non_reloc:
                    if _stop_type(prev_non_reloc) == "charging":
                        energy_start = _charge_target(prev_non_reloc_idx)
                        if energy_start is None:
                            energy_start = prev_non_reloc.get("remainingEnergy")
                        if (energy_start is None or energy_start == 0) and cur.get("remainingEnergy") is not None:
                            energy_start = cur.get("remainingEnergy")
                    else:
                        energy_start = prev_non_reloc.get("remainingEnergy")
                else:
                    energy_start = None
                empty_guid = f"empty_{uuid.uuid4().hex}"
                _build_segments_with_relocation(
                    start_time,
                    end_time,
                    start_loc,
                    end_loc,
                    "to_pickup",
                    f"Way to {cur.get('userGuid') or cur.get('tripGuid') or ''}".strip(),
                    empty_guid,
                    relocs_in,
                    energy_start,
                    cur.get("remainingEnergy"),
                    empty_guid,
                    cur.get("userGuid") or "",
                    convoy_travel_type="convoy_travel_empty",
                    convoy_label="Konvoi Fahrt (ohne Kunden)"
                )
                if relocs_out:
                    _append_convoy_only(
                        relocs_out,
                        "convoy_travel_empty",
                        "Konvoi Fahrt (ohne Kunden)"
                    )

            trip_guid = cur.get("tripGuid") or ""
            dropoff_idx = None
            for j in range(i+1, n):
                s2 = trip_stops[j]
                if not isinstance(s2, dict):
                    continue
                if (str(s2.get("stopType", "")).lower() == "dropoff" and
                        (not trip_guid or s2.get("tripGuid") == trip_guid)):
                    dropoff_idx = j
                    break
            if dropoff_idx is not None:
                pickup_arrival = _parse_iso_as_utc(cur.get("arrival") or cur.get("departure"))
                dropoff = trip_stops[dropoff_idx]
                dropoff_departure = _parse_iso_as_utc(dropoff.get("departure") or dropoff.get("arrival"))
                if pickup_arrival and dropoff_departure and dropoff_departure >= pickup_arrival:
                    pickup_loc = _loc_from_stop(cur)
                    dropoff_loc = _loc_from_stop(dropoff)
                    group_guid = cur.get("tripGuid") or cur.get("userGuid") or f"trip_{vid}_{i}"
                    label = str(cur.get("userGuid") or cur.get("tripGuid") or "")[:64]
                    relocs = [
                        trip_stops[k] for k in range(i + 1, dropoff_idx)
                        if _is_relocation(trip_stops[k])
                    ]
                    _build_segments_with_relocation(
                        pickup_arrival,
                        dropoff_departure,
                        pickup_loc,
                        dropoff_loc,
                        "customer_trip",
                        label,
                        group_guid,
                        relocs,
                        cur.get("remainingEnergy"),
                        dropoff.get("remainingEnergy"),
                        None,
                        cur.get("userGuid") or ""
                    )
                add_energy_point(energy_timeline, dropoff_departure, dropoff.get("remainingEnergy"))
                i = dropoff_idx + 1
                continue
            else:
                i += 1
                continue

        elif stype == "depot":
            prev = trip_stops[i-1] if i-1 >= 0 else None
            end_drive = _parse_iso_as_utc(cur.get("arrival") or cur.get("departure"))
            prev_non_reloc_idx = i - 1
            while prev_non_reloc_idx >= 0 and _is_relocation(trip_stops[prev_non_reloc_idx]):
                prev_non_reloc_idx -= 1
            prev_non_reloc = trip_stops[prev_non_reloc_idx] if prev_non_reloc_idx >= 0 else prev
            start_drive = _parse_iso_as_utc(prev_non_reloc.get("departure") or prev_non_reloc.get("arrival")) if isinstance(prev_non_reloc, dict) else None

            relocs = []
            range_start = prev_non_reloc_idx + 1 if prev_non_reloc_idx is not None and prev_non_reloc_idx >= 0 else 0
            if start_drive and end_drive:
                for k in range(range_start, i):
                    if _is_relocation(trip_stops[k]):
                        t = _stop_time(trip_stops[k], "arrival") or _stop_time(trip_stops[k], "departure")
                        if t and (t < start_drive or t > end_drive):
                            continue
                        relocs.append(trip_stops[k])

            prev_loc = _loc_from_stop(prev_non_reloc) or initial_loc
            cur_loc = _loc_from_stop(cur)
            leg_label = cur.get("key") or "Depot"
            group_guid = cur.get("tripGuid") or f"to_depot_{vid}_{i}"
            relocs_in, relocs_out = _split_relocs_by_window(relocs, start_drive, end_drive)

            if relocs_in and start_drive and end_drive and end_drive > start_drive:
                _build_segments_with_relocation(
                    start_drive,
                    end_drive,
                    prev_loc,
                    cur_loc,
                    "to_depot",
                    leg_label,
                    group_guid,
                    relocs_in,
                    prev_non_reloc.get("remainingEnergy") if isinstance(prev_non_reloc, dict) else None,
                    cur.get("remainingEnergy"),
                    None,
                    cur.get("userGuid") or "",
                    convoy_travel_type="convoy_travel_empty",
                    convoy_label="Konvoi Fahrt (ohne Kunden)"
                )
            else:
                coords = get_route_between_points(prev_loc, cur_loc) if (prev_loc and cur_loc) else []
                if start_drive and end_drive and end_drive > start_drive and coords:
                    leg = {
                        "start": _iso(start_drive),
                        "end": _iso(end_drive),
                        "type": "to_depot",
                        "label": leg_label,
                        "tripGuid": group_guid,
                        "userGuid": cur.get("userGuid") or "",
                        "key": cur.get("key") or "",
                        "coords": coords,
                        "remainingEnergyStart": (prev_non_reloc.get("remainingEnergy")
                                                 if isinstance(prev_non_reloc, dict)
                                                 else (prev.get("remainingEnergy") if isinstance(prev, dict) else None)),
                        "remainingEnergyEnd": cur.get("remainingEnergy")
                    }
                    legs.append(leg)
                    
                    drive_time = (end_drive - start_drive).total_seconds()
                    total_driving_time += max(0.0, drive_time)

                    total_consumed_energy += energy_delta(
                        prev_non_reloc.get("remainingEnergy") if isinstance(prev_non_reloc, dict) else prev.get("remainingEnergy"),
                        cur.get("remainingEnergy")
                    )
                    
                    total_distance += polyline_distance_m(coords)
            if relocs_out:
                _append_convoy_only(
                    relocs_out,
                    "convoy_travel_empty",
                    "Konvoi Fahrt (ohne Kunde)"
                )
            i += 1
            continue
        
        elif stype == "charging":
            prev = trip_stops[i-1] if i-1 >= 0 else None

            # --- Way to charging ---
            arrival = _parse_iso_as_utc(cur.get("arrival"))
            prev_non_reloc_idx = i - 1
            while prev_non_reloc_idx >= 0 and _is_relocation(trip_stops[prev_non_reloc_idx]):
                prev_non_reloc_idx -= 1
            prev_non_reloc = trip_stops[prev_non_reloc_idx] if prev_non_reloc_idx >= 0 else prev
            start_drive = _parse_iso_as_utc(
                prev_non_reloc.get("departure") or prev_non_reloc.get("arrival")
            ) if isinstance(prev_non_reloc, dict) else None

            relocs = []
            range_start = prev_non_reloc_idx + 1 if prev_non_reloc_idx is not None and prev_non_reloc_idx >= 0 else 0
            if start_drive and arrival:
                for k in range(range_start, i):
                    if _is_relocation(trip_stops[k]):
                        t = _stop_time(trip_stops[k], "arrival") or _stop_time(trip_stops[k], "departure")
                        if t and (t < start_drive or t > arrival):
                            continue
                        relocs.append(trip_stops[k])

            prev_loc = _loc_from_stop(prev_non_reloc) or initial_loc
            cur_loc = _loc_from_stop(cur)
            charging_energy_start, charging_energy_end = _resolve_stationary_charging_energy(i, prev_non_reloc)

            relocs_in, relocs_out = _split_relocs_by_window(relocs, start_drive, arrival)

            if relocs_in and start_drive and arrival and arrival > start_drive:
                _build_segments_with_relocation(
                    start_drive,
                    arrival,
                    prev_loc,
                    cur_loc,
                    "to_charging",
                    "Servicefahrt",
                    cur.get("key") or f"to_charging_{vid}_{i}",
                    relocs_in,
                    prev_non_reloc.get("remainingEnergy") if isinstance(prev_non_reloc, dict) else None,
                    charging_energy_start,
                    None,
                    cur.get("userGuid") or "",
                    convoy_travel_type="convoy_travel_empty",
                    convoy_label="Konvoi Fahrt (ohne Kunden)"
                )
            else:
                if start_drive and arrival and prev_loc and cur_loc:
                    coords = get_route_between_points(prev_loc, cur_loc)
                    if coords:
                        legs.append({
                            "start": _iso(start_drive),
                            "end": _iso(arrival),
                            "type": "to_charging",
                            "label": "Servicefahrt",
                            "tripGuid": cur.get("key") or "",
                            "coords": coords,
                            "remainingEnergyStart": (prev_non_reloc.get("remainingEnergy")
                                                 if isinstance(prev_non_reloc, dict)
                                                 else (prev.get("remainingEnergy") if isinstance(prev, dict) else None)),
                            "remainingEnergyEnd": charging_energy_start
                        })
                        
                        drive_time = (arrival - start_drive).total_seconds()
                        total_driving_time += max(0.0, drive_time)

                        total_consumed_energy += energy_delta(
                            prev_non_reloc.get("remainingEnergy") if isinstance(prev_non_reloc, dict) else prev.get("remainingEnergy"),
                            charging_energy_start
                        )
                        
                        total_distance += polyline_distance_m(coords)

            if relocs_out:
                _append_convoy_only(
                    relocs_out,
                    "convoy_travel_empty",
                    "Konvoi Fahrt (ohne Kunden)"
                )

            # --- Charging ---
            charge_start = arrival
            charge_end = _parse_iso_as_utc(cur.get("departure"))

            energy_start = charging_energy_start
            energy_end = charging_energy_end

            if charge_start and charge_end and charge_end > charge_start:
                legs.append({
                    "start": iso_utc(charge_start),
                    "end": iso_utc(charge_end),
                    "type": "charging_idle",
                    "label": "Charging",
                    "tripGuid": cur.get("key") or "",
                    "coords": [cur_loc] if cur_loc else [],
                    "remainingEnergyStart": energy_start,
                    "remainingEnergyEnd": energy_end
                })

                # explicit energy transition during charging
                add_energy_point(energy_timeline, charge_start, energy_start)
                add_energy_point(energy_timeline, charge_end, energy_end)

            i += 1
            continue
        else:
            i += 1
            continue

    schedule = vehicle.get("schedule") or {}
    sched_start = _parse_iso_as_utc(schedule.get("startTime")) if isinstance(schedule, dict) else None
    sched_end = _parse_iso_as_utc(schedule.get("endTime")) if isinstance(schedule, dict) else None
    first_depot_dep = None
    depot_loc = None
    first_depot_energy = None
    last_depot_energy = None
    for s in trip_stops:
        if not isinstance(s, dict):
            continue
        if str(s.get("stopType", "")).lower() == "depot":
            if first_depot_energy is None:
                first_depot_energy = s.get("remainingEnergy")
            last_depot_energy = s.get("remainingEnergy")

    if sched_start and first_depot_dep and first_depot_dep > sched_start:
        legs.insert(0, {
            "start": _iso(sched_start),
            "end": _iso(first_depot_dep),
            "type": "depot_idle",
            "label": "Depot",
            "tripGuid": "",
            "userGuid": "",
            "key": "",
            "coords": [depot_loc] if depot_loc else [],
            "remainingEnergyStart": prev.get("remainingEnergy"),
            "remainingEnergyEnd": cur.get("remainingEnergy")
        })

    transported_customer_ids: set[str] = set()
    convoy_trip_count = 0
    customer_distance = 0.0
    convoy_distance = 0.0
    stationary_charged_energy = 0.0
    convoy_charged_energy = 0.0
    stationary_charging_time = 0.0

    for leg_idx, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        leg_type = str(leg.get("type") or "")
        coords = leg.get("coords") or []
        leg_distance = polyline_distance_m(coords) if coords else 0.0

        if leg_type == "customer_trip":
            customer_id = str(
                leg.get("tripGuid") or leg.get("userGuid") or leg.get("emptyLegGuid") or f"customer_{leg_idx}"
            )
            transported_customer_ids.add(customer_id)
            customer_distance += leg_distance

        if leg_type in ("convoy_travel", "convoy_travel_empty"):
            convoy_trip_count += 1
            convoy_distance += leg_distance
            e_start = _num(leg.get("remainingEnergyStart"))
            e_end = _num(leg.get("remainingEnergyEnd"))
            if e_start is not None and e_end is not None and e_end > e_start:
                convoy_charged_energy += (e_end - e_start)

        if leg_type == "charging_idle":
            sdt = _parse_iso_as_utc(leg.get("start"))
            edt = _parse_iso_as_utc(leg.get("end"))
            if sdt and edt and edt > sdt:
                stationary_charging_time += (edt - sdt).total_seconds()
            e_start = _num(leg.get("remainingEnergyStart"))
            e_end = _num(leg.get("remainingEnergyEnd"))
            if e_start is not None and e_end is not None and e_end > e_start:
                stationary_charged_energy += (e_end - e_start)

    customer_vs_empty_ratio = (customer_distance / total_distance) if total_distance > 0 else 0.0

    payload = {
        "vehicleId": vehicle_id,
        "file": os.path.basename(cab_path) if cab_path else "",
        "vehicleType": "cab",
        "trips": legs,
        "kpis": {
            "totalDistance": total_distance,
            "totalDrivingTime": total_driving_time,
            "totalConsumedEnergy": total_consumed_energy,
            "remainingEnergyLastDepot": last_depot_energy,
            "transportedCustomers": len(transported_customer_ids),
            "convoyTrips": convoy_trip_count,
            "convoyDistance": convoy_distance,
            "stationaryChargedEnergy": stationary_charged_energy,
            "convoyChargedEnergy": convoy_charged_energy,
            "stationaryChargingTime": stationary_charging_time,
            "utilization": 0.0,
            "customerVsEmptyRatio": customer_vs_empty_ratio
        }
    }
    
    if energy_timeline:
        dedup = {}
        for p in energy_timeline:
            if p.get("time") is not None:
                dedup[p["time"]] = p

        payload["energyTimeline"] = sorted(
            dedup.values(),
            key=lambda p: p["time"]
    )
    payload["tripStops"] = trip_stops

    sched_end = _parse_iso_as_utc(vehicle.get("schedule", {}).get("endTime")) or \
            (legs[-1]["end"] if legs else None)
    sched_start = _parse_iso_as_utc(vehicle.get("schedule", {}).get("startTime"))
    if isinstance(sched_end, str):
        sched_end = datetime.fromisoformat(sched_end)
    if sched_end:
        appendDepotBlock(payload, sched_end)
    if sched_start:
        appendDepotBlockAtStart(payload, sched_start)
    if depot_loc:
        payload["depotLocation"] = depot_loc

    def _compute_day_activity_from_trips(
        trips: List[Dict[str, Any]],
        sched_start_dt: Optional[datetime] = None,
        sched_end_dt: Optional[datetime] = None
    ) -> Tuple[float, float, float]:
        """Calculate utilization and idle-time fallback values from the rendered trip timeline."""
        if not isinstance(trips, list) or not trips:
            return 0.0, 0.0, 0.0
        day_start = sched_start_dt
        day_end = sched_end_dt
        active_seconds = 0.0

        for leg in trips:
            if not isinstance(leg, dict):
                continue
            sdt = _parse_iso_as_utc(leg.get("start"))
            edt = _parse_iso_as_utc(leg.get("end"))
            if sdt is None or edt is None or edt <= sdt:
                continue

            # Fallback for runs without schedule: derive window from observed legs.
            if sched_start_dt is None and (day_start is None or sdt < day_start):
                day_start = sdt
            if sched_end_dt is None and (day_end is None or edt > day_end):
                day_end = edt

            if str(leg.get("type", "")).lower() != "depot_idle":
                # Keep active-time consistent with schedule denominator by clipping to schedule window.
                seg_start = sdt
                seg_end = edt
                if day_start is not None and seg_start < day_start:
                    seg_start = day_start
                if day_end is not None and seg_end > day_end:
                    seg_end = day_end
                if seg_end > seg_start:
                    active_seconds += (seg_end - seg_start).total_seconds()

        if day_start is None or day_end is None or day_end <= day_start:
            return 0.0, 0.0, 0.0

        day_seconds = (day_end - day_start).total_seconds()
        if day_seconds <= 0:
            return 0.0, 0.0, 0.0

        util = active_seconds / day_seconds
        util = max(0.0, min(1.0, util))
        idle_seconds = max(0.0, day_seconds - active_seconds)
        idle_ratio = max(0.0, min(1.0, 1.0 - util))
        return util, idle_seconds, idle_ratio

    # Use the same cab-utilization logic as the simulation-results chart
    # so both views always show identical utilization values.
    util = _compute_operations_utilization_for_cab_entry(target_entry)
    if util is None:
        util, idle_seconds, idle_ratio = _compute_day_activity_from_trips(
            payload.get("trips") or [],
            sched_start_dt=sched_start,
            sched_end_dt=sched_end
        )
    else:
        if sched_start and sched_end and sched_end > sched_start:
            day_seconds = (sched_end - sched_start).total_seconds()
            idle_seconds = max(0.0, day_seconds * (1.0 - util))
        else:
            # schedule missing: keep idle seconds from fallback trip-window logic
            _, idle_seconds, _ = _compute_day_activity_from_trips(payload.get("trips") or [])
        idle_ratio = max(0.0, min(1.0, 1.0 - util))
    payload["kpis"]["utilization"] = util
    payload["kpis"]["idleTimeSeconds"] = idle_seconds
    payload["kpis"]["idleRatio"] = idle_ratio
    return _attach_detailed_kpi_aliases(payload)

def appendDepotBlock(vehicle_payload, scenario_end):
    """Append a short depot-idle block after the schedule end for visual context."""
    tripStops = vehicle_payload.get("tripStops", [])
    if not tripStops:
        return
    depot_stop = next((s for s in tripStops if str(s.get("stopType","")).lower() == "depot"), None)
    if not depot_stop:
        return
    loc = depot_stop.get("location", {})
    lat = loc.get("latitude")
    lng = loc.get("longitude")
    if lat is None or lng is None:
        return
    depot_start = scenario_end
    depot_end = scenario_end + timedelta(minutes=15)
    depot_block = {
        "type": "depot_idle",
        "label": "Depot",
        "coords": [{"lat": lat, "lng": lng}],
        "start": iso_utc(depot_start),
        "end": iso_utc(depot_end),
        "start_dt": depot_start,
        "end_dt": depot_end,
        "emptyLegGuid": None,
        "tripGuid": None,
        "userGuid": None
    }
    vehicle_payload.setdefault("trips", []).append(depot_block)
    vehicle_payload["depotLocation"] = {"lat": lat, "lng": lng}

def appendDepotBlockAtStart(vehicle_payload, scenario_start):
    """Insert a short depot-idle block before the schedule start for visual context."""
    tripStops = vehicle_payload.get("tripStops", [])
    if not tripStops:
        return
    depot_stop = next((s for s in tripStops if str(s.get("stopType","")).lower() == "depot"), None)
    if not depot_stop:
        return
    loc = depot_stop.get("location", {})
    lat = loc.get("latitude")
    lng = loc.get("longitude")
    if lat is None or lng is None:
        return
    depot_end = scenario_start
    depot_start = scenario_start - timedelta(minutes=15)
    depot_block = {
        "type": "depot_idle",
        "label": "Depot",
        "coords": [{"lat": lat, "lng": lng}],
        "start": iso_utc(depot_start),
        "end": iso_utc(depot_end),
        "start_dt": depot_start,
        "end_dt": depot_end,
        "emptyLegGuid": None,
        "tripGuid": None,
        "userGuid": None
    }
    vehicle_payload.setdefault("trips", []).insert(0, depot_block)
    vehicle_payload["depotLocation"] = {"lat": lat, "lng": lng}
