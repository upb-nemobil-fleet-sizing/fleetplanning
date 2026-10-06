#!/usr/bin/env bash
# Pre-renders ("preloads") the tiles of the imported region: its bounding rectangle, from zoom 0
# up to --max-zoom (default 18, the highest zoom level the dashboard's maps use). The number of
# tiles grows about fourfold per level, see README.md.
#
# Usage: seed_tiles.sh [--max-zoom N]
#
# The rectangle is the bounding box of the region outline data/region.poly. Without an outline it
# is the bounding box in the header of the imported extract, which is larger. It also covers some
# area outside the real (irregular) region outline; those spots render blank, which render_list
# can't avoid since it only takes a tile x/y rectangle, not an arbitrary polygon.
#
# The render_list build shipped in this image only accepts a tile x/y range per call (no lat/lon
# flags, and min-zoom must equal max-zoom for --all to make sense), so this converts the
# lat/lon bbox to a tile x/y range per zoom level itself and calls render_list once per zoom. The
# start of each range is rounded down to a multiple of the 8x8 block size, so that the last
# partial block of the area is rendered as well.
#
# By default this only renders tiles that don't already exist in the cache, so re-running it is
# cheap - already-seeded areas are skipped almost instantly. Set FORCE=1 to pass -f/--force
# instead, which re-renders even tiles that already exist. That's only needed if the database
# changed under an area that was already cached with different (e.g. blank) content - render_list
# has no way to detect that on its own, it just treats any existing tile file as current. Forcing
# a re-render across the whole area can take a while, so only set FORCE=1 when the underlying
# data actually changed, not by default.
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER="${CONTAINER:-nemo-tile-server}"
FORCE_FLAG=""
[ "${FORCE:-0}" = "1" ] && FORCE_FLAG="-f"

fail() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  echo "Usage: seed_tiles.sh [--max-zoom N]"
  echo "  --max-zoom N  highest zoom level to pre-render, 0 to 18 (default 18)"
}

MAX_ZOOM=18
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --max-zoom) [ $# -ge 2 ] || fail "--max-zoom needs a value"; MAX_ZOOM="$2"; shift 2 ;;
    --max-zoom=*) MAX_ZOOM="${1#*=}"; shift ;;
    *) fail "unknown argument '$1' (usage: seed_tiles.sh [--max-zoom N])" ;;
  esac
done
if ! [[ "$MAX_ZOOM" =~ ^[0-9]+$ ]] || [ "$MAX_ZOOM" -gt 18 ]; then
  fail "--max-zoom must be an integer from 0 to 18 (got '$MAX_ZOOM')."
fi

# Prints "min_lon min_lat max_lon max_lat" of the imported region.
region_bbox() {
  if [ -f data/region.poly ]; then
    python3 - data/region.poly <<'PYEOF'
import re, sys
lons, lats = [], []
for line in open(sys.argv[1]):
    m = re.match(r"\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\s*$", line)
    if m:
        lons.append(float(m.group(1)))
        lats.append(float(m.group(2)))
print(min(lons), min(lats), max(lons), max(lats))
PYEOF
  else
    docker exec "$CONTAINER" osmium fileinfo -g header.boxes /data/region.osm.pbf | tr '(),' '   '
  fi
}

render_bbox() {
  local label="$1" min_lon="$2" min_lat="$3" max_lon="$4" max_lat="$5" min_zoom="$6" max_zoom="$7"
  echo "Seeding $label ($min_lon,$min_lat)-($max_lon,$max_lat), zoom $min_zoom-$max_zoom ..."
  python3 - "$min_lon" "$min_lat" "$max_lon" "$max_lat" <<'PYEOF' |
import math, sys
min_lon, min_lat, max_lon, max_lat = map(float, sys.argv[1:5])

def tile_xy(lat, lon, z):
    lat_rad = math.radians(lat)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))

METATILE = 8  # render_list renders 8x8 blocks and steps through the range from its first value

for z in range(0, 19):
    x0, y1 = tile_xy(min_lat, min_lon, z)
    x1, y0 = tile_xy(max_lat, max_lon, z)
    x_min, y_min = min(x0, x1), min(y0, y1)
    print(z, x_min - x_min % METATILE, max(x0, x1), y_min - y_min % METATILE, max(y0, y1))
PYEOF
  while read -r z x0 x1 y0 y1; do
    if [ "$z" -lt "$min_zoom" ] || [ "$z" -gt "$max_zoom" ]; then continue; fi
    docker exec "$CONTAINER" render_list -a $FORCE_FLAG -m default -n 8 \
      -z "$z" -Z "$z" -x "$x0" -X "$x1" -y "$y0" -Y "$y1"
  done
}

read -r MIN_LON MIN_LAT MAX_LON MAX_LAT <<<"$(region_bbox)"
if [ -z "${MAX_LAT:-}" ]; then
  fail "the extent of the imported region is unknown: there is no data/region.poly and the extract has no bounding box in its header."
fi

render_bbox "the imported region" "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" 0 "$MAX_ZOOM"

echo "Seeding complete."
