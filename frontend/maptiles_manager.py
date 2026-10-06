"""Picks which tile URL template(s) the dashboard's Leaflet maps should use, based on the
TILE_SOURCE environment variable and whether a local tile server (see maptiles/) is currently
reachable. This module never starts, stops, or otherwise manages that server - it is an
independent service with its own lifecycle (see maptiles/README.md for how to run it); this
module only ever asks "is it reachable right now?" over HTTP and forwards tile requests to it.

Browsers never contact the local tile server directly. The dashboard serves its tiles through
its own /tiles/{z}/{x}/{y}.png route (see fetch_local_tile()), so the tile server only needs to
be reachable from the dashboard host, and remote visitors, Basic Auth and https need no special
handling. LOCAL_TILE_SERVER_URL is therefore an address for the dashboard process, not for
browsers.

Controlled by the TILE_SOURCE environment variable:
- "fallback": ask the local tile server first and use the public OpenStreetMap tile
  servers for every tile it cannot provide. While a local tile server is reachable at
  LOCAL_TILE_SERVER_URL, the dashboard's Leaflet maps request tiles from it (see
  current_tile_urls()) and retry an individual tile against the public servers whenever it
  fails to return one. The /tiles route only serves tiles that lie completely inside the
  outline of the imported region (maptiles/data/region.poly, downloaded next to the extract by
  maptiles/download_extract.sh), so tiles outside it, and the low zoom levels where a single
  tile covers far more than the region, come from the public servers. Without that file, or
  while the local tile server is not reachable, all tiles come from the public servers.
  Reachability is rechecked periodically (see _CHECK_TTL_SECONDS), so this switches within that
  window as the local server is started, stopped, or restarted - no dashboard restart needed.
- "local": use the self-hosted tile server exclusively. init_tile_source(), called once at
  dashboard startup, checks reachability and raises if it isn't reachable yet - the tile
  server must already be running (see maptiles/README.md to start it); the dashboard does not
  start it itself. There is no runtime fallback: once startup succeeds the dashboard only ever
  serves local tiles, never live-switching to the public ones. The /tiles route forwards every
  tile that overlaps the region the local server has data for, including partial ones at its
  edge, because there is no other source to fill them in, and refuses the rest so that the
  server does not render empty tiles. The region is the outline (maptiles/data/region.poly),
  or, while that file is missing, the bounding box in the header of the extract
  (maptiles/data/region.osm.pbf), or, while that is missing too, everything.
- "web" (default): always use the public OpenStreetMap tile servers; the local tile server is never
  checked or used, and the /tiles route answers 404.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request

LOCAL_TILE_SERVER_URL = os.environ.get("LOCAL_TILE_SERVER_URL", "http://localhost:8080").rstrip("/")
LOCAL_TILE_URL_TEMPLATE = f"{LOCAL_TILE_SERVER_URL}/tile/{{z}}/{{x}}/{{y}}.png"
WEB_TILE_URL_TEMPLATE = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
# What browsers request: the dashboard's own tile route, relative to the page's origin.
PROXY_TILE_URL_TEMPLATE = "/tiles/{z}/{x}/{y}.png"
_HEALTH_CHECK_URL = f"{LOCAL_TILE_SERVER_URL}/tile/0/0/0.png"
_CHECK_TTL_SECONDS = 20.0
# A tile that is not rendered yet is rendered on request, which can take several seconds.
_TILE_FETCH_TIMEOUT_SECONDS = 30.0
_MAX_TILE_ZOOM = 19
_REGION_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maptiles", "data")
# Outline of the imported region as an Osmosis .poly file, written by maptiles/download_extract.sh.
COVERAGE_POLYGON_FILE = os.path.join(_REGION_DATA_DIR, "region.poly")
# The extract the tile server imported, provided by maptiles/download_extract.sh.
REGION_EXTRACT_FILE = os.path.join(_REGION_DATA_DIR, "region.osm.pbf")

TILE_SOURCE = os.environ.get("TILE_SOURCE", "web").strip().lower()

_state_lock = threading.Lock()
_last_check_time: float | None = None
_last_check_result = False
_coverage = None  # (inside, overlaps) functions for the imported region's outline, once read
_coverage_read_failed = False
_header_box = None  # (west, south, east, north) from the extract's header, once read
_header_box_read_failed = False


def _log(message: str) -> None:
    print(f"[maptiles] {message}", file=sys.stderr, flush=True)


def _read_poly_file(path: str):
    """Parse an Osmosis .poly file, as published by Geofabrik, and return two functions of
    (west, south, east, north): whether a box lies completely inside the polygon, and whether it
    overlaps the polygon at all. The first line is the polygon's name; then come sections of one
    "lon lat" pair per line, each closed by END (a section name starting with "!" marks a hole),
    and a final END. shapely is only imported here, so the modes that never read the outline do
    not load it."""
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
    from shapely.prepared import prep

    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    outers, holes = [], []
    i = 1
    while i < len(lines) and lines[i] != "END":
        is_hole = lines[i].startswith("!")
        i += 1
        ring = []
        while lines[i] != "END":
            lon, lat = lines[i].split()[:2]
            ring.append((float(lon), float(lat)))
            i += 1
        i += 1
        (holes if is_hole else outers).append(ring)
    area = unary_union([Polygon(ring) for ring in outers])
    for ring in holes:
        area = area.difference(Polygon(ring))
    prepared = prep(area)
    return (
        lambda west, south, east, north: prepared.contains(box(west, south, east, north)),
        lambda west, south, east, north: prepared.intersects(box(west, south, east, north)),
    )


def _local_coverage():
    """The (inside, overlaps) functions for the outline of the region the local tile server has
    data for (see _read_poly_file()), or None while the outline file is missing or unreadable.
    Only TILE_SOURCE=fallback and TILE_SOURCE=local call this. The file is read on first use and
    looked for again while it is missing, so downloading it later needs no dashboard restart."""
    global _coverage, _coverage_read_failed
    if _coverage is None and os.path.isfile(COVERAGE_POLYGON_FILE):
        try:
            _coverage = _read_poly_file(COVERAGE_POLYGON_FILE)
        except Exception as exc:
            if not _coverage_read_failed:
                _log(f"could not read the region outline {COVERAGE_POLYGON_FILE}: {exc}")
            _coverage_read_failed = True
    return _coverage


def _extract_header_box():
    """(west, south, east, north) from the header of the extract the tile server imported, or
    None while the extract is missing or has no such box. Only TILE_SOURCE=local calls this, and
    only while the outline is missing. osmium is only imported here."""
    global _header_box, _header_box_read_failed
    if _header_box is None and os.path.isfile(REGION_EXTRACT_FILE):
        try:
            import osmium

            reader = osmium.io.Reader(REGION_EXTRACT_FILE, osmium.osm.osm_entity_bits.NOTHING)
            try:
                box = reader.header().box()
            finally:
                reader.close()
            if box.valid():
                _header_box = (box.bottom_left.lon, box.bottom_left.lat, box.top_right.lon, box.top_right.lat)
        except Exception as exc:
            if not _header_box_read_failed:
                _log(f"could not read the header of the extract {REGION_EXTRACT_FILE}: {exc}")
            _header_box_read_failed = True
    return _header_box


def _tile_server_reachable(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(_HEALTH_CHECK_URL, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _is_local_server_reachable() -> bool:
    """Reachability of the local tile server, cached for _CHECK_TTL_SECONDS so that most calls
    (e.g. one per request, via the context processor) don't each pay for an HTTP round trip -
    only rechecked often enough to notice the independently-managed server being started,
    stopped, or restarted without needing the dashboard itself restarted."""
    global _last_check_time, _last_check_result
    now = time.monotonic()
    with _state_lock:
        if _last_check_time is not None and now - _last_check_time < _CHECK_TTL_SECONDS:
            return _last_check_result
    result = _tile_server_reachable()
    with _state_lock:
        _last_check_time = now
        _last_check_result = result
    return result


def init_tile_source() -> None:
    """Check once, at dashboard startup, whether the local tile server is reachable - used to
    fail fast for TILE_SOURCE=local and to seed the reachability cache. No-op when
    TILE_SOURCE=web. Never starts or stops the tile server; see maptiles/README.md to run it
    separately.

    - TILE_SOURCE=local: raises RuntimeError if it isn't already reachable - the dashboard must
      not start serving requests in that case, since there is no runtime fallback to the public
      tiles.
    - TILE_SOURCE=fallback: only logs a warning and lets startup continue if it isn't reachable
      yet, since the public tiles serve every map until then; the local tile server is used as
      soon as it appears (see _is_local_server_reachable()).
    """
    if TILE_SOURCE not in ("local", "fallback"):
        return

    if TILE_SOURCE == "fallback" and _local_coverage() is None:
        _log(
            f"TILE_SOURCE=fallback but the region outline {COVERAGE_POLYGON_FILE} is missing or "
            "unreadable, so all tiles come from the public tile servers. Run "
            "frontend/maptiles/download_extract.sh to download it."
        )
    if TILE_SOURCE == "local" and _local_coverage() is None:
        _log(
            f"TILE_SOURCE=local but the region outline {COVERAGE_POLYGON_FILE} is missing or "
            f"unreadable, so tiles are filtered by the header box of {REGION_EXTRACT_FILE}, or not "
            "at all if that is missing too. Run frontend/maptiles/download_extract.sh to download "
            "the outline."
        )

    if _is_local_server_reachable():
        return

    message = (
        f"local tile server not reachable at {LOCAL_TILE_SERVER_URL} (checked "
        f"{_HEALTH_CHECK_URL}). The dashboard no longer starts it automatically - start it "
        "separately first, see frontend/maptiles/README.md (typically "
        "'cd frontend/maptiles && docker compose up -d tile-server')."
    )
    if TILE_SOURCE == "local":
        raise RuntimeError(f"TILE_SOURCE=local but {message}")
    _log(f"TILE_SOURCE=fallback but {message} Continuing with only the public tile servers.")


def current_tile_urls() -> tuple[str, str | None]:
    """The (primary, fallback) tile URL templates the dashboard's Leaflet maps should use. The
    fallback is retried for an individual tile whenever the primary fails to return it, or None
    when there is nothing to fall back to. Both come from the same reachability check, so they
    always agree. In "fallback" mode the local tile server is only asked while it is reachable
    and the outline of its region is known."""
    if TILE_SOURCE == "local":
        return PROXY_TILE_URL_TEMPLATE, None
    if TILE_SOURCE == "fallback" and _local_coverage() is not None and _is_local_server_reachable():
        return PROXY_TILE_URL_TEMPLATE, WEB_TILE_URL_TEMPLATE
    return WEB_TILE_URL_TEMPLATE, None


def _tile_edges(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a tile in degrees."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _tile_in_local_area(z: int, x: int, y: int) -> bool:
    """Whether the local tile server has data for this tile, as far as the TILE_SOURCE mode
    can know. "fallback" needs the tile to lie completely inside the imported region's outline,
    so that everything else comes from the public servers. "local" has no other source, so it
    accepts every tile that overlaps the region: its outline, or while that is missing the
    bounding box in the extract's header, or while that is missing too every tile. A tile that
    only touches the edge does not overlap."""
    west, south, east, north = _tile_edges(z, x, y)
    coverage = _local_coverage()
    if TILE_SOURCE == "fallback":
        return coverage is not None and coverage[0](west, south, east, north)
    if coverage is not None:
        return coverage[1](west, south, east, north)
    header_box = _extract_header_box()
    if header_box is None:
        return True
    box_west, box_south, box_east, box_north = header_box
    return south < box_north and north > box_south and west < box_east and east > box_west


def fetch_local_tile(z: int, x: int, y: int) -> tuple[int, bytes]:
    """Fetch one tile from the local tile server for the dashboard's /tiles route.

    Returns (200, png bytes) on success, (404, b"") for a tile outside the valid z/x/y range, a
    tile the local tile server has no data for (see the TILE_SOURCE modes above), a tile the server
    does not have, or when TILE_SOURCE=web, and (502, b"") when the server cannot be reached or
    answers with an error. Only integers are ever inserted into the upstream URL, so a request can
    not make the dashboard fetch anything but a tile. Tiles that are refused for lying outside
    the local area are never requested from the tile server, so it does not render them.
    """
    if TILE_SOURCE not in ("local", "fallback"):
        return 404, b""
    if not (0 <= z <= _MAX_TILE_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return 404, b""
    if not _tile_in_local_area(z, x, y):
        return 404, b""
    url = LOCAL_TILE_URL_TEMPLATE.format(z=z, x=x, y=y)
    try:
        with urllib.request.urlopen(url, timeout=_TILE_FETCH_TIMEOUT_SECONDS) as resp:
            return 200, resp.read()
    except urllib.error.HTTPError as exc:
        return (404 if exc.code == 404 else 502), b""
    except (urllib.error.URLError, OSError):
        return 502, b""
