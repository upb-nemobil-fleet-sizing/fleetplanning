#!/usr/bin/env bash
# Provides the OSM extract at data/region.osm.pbf for the tile server to import, and the outline
# of its region at data/region.poly.
#
# Usage: download_extract.sh [MAP_FILE]
#
# With MAP_FILE, that file is linked as data/region.osm.pbf. Without it, the extract is provided
# as follows. This covers every demo scenario area currently in use (Paderborn and Hoexter).
#
# If the simulation's own routing graph builder already has its OSM source extract
# (custom_sim/routing/routing_config.json's "osm_source_path") - the same region, used for a
# different purpose - this script symlinks data/region.osm.pbf to that file instead of
# downloading a second copy. If that file is missing too, it offers to download it there so
# both sides end up sharing the one extract; declining aborts so it can be placed there by
# hand and this script re-run. Only when routing_config.json itself isn't found (e.g. this
# directory used standalone, without the rest of the repo) does it fall back to downloading
# straight into data/region.osm.pbf as its own independent copy.
#
# Either way this is a normal bulk data download, not tile scraping, so it does not run into
# the OpenStreetMap tile usage policy that forbids bulk tile downloads.
#
# The outline is the polygon the extract was cut with. A Geofabrik extract names its origin in
# its header (osmosis_replication_base_url, ending in "-updates"); the outline is downloaded from
# that address with "-updates" replaced by ".poly", replacing any earlier copy on every run. The
# dashboard uses it to tell which map tiles the tile server has data for. An extract without such
# an address has no outline to download. If the download fails, this script warns and carries on.
# In both cases an existing outline is kept when the extract is the same as before, and removed
# when the extract changed, because it then belongs to another region. The header is read with
# the osmium tool from the tile server image, so Docker is needed for this step.
set -euo pipefail

cd "$(dirname "$0")"

URL="https://download.geofabrik.de/europe/germany/nordrhein-westfalen/detmold-regbez-latest.osm.pbf"
OUT="data/region.osm.pbf"
POLY_OUT="data/region.poly"
ROUTING_CONFIG="../../custom_sim/routing/routing_config.json"
MAP_FILE="${1:-}"

if [ -n "$MAP_FILE" ] && [ ! -f "$MAP_FILE" ]; then
  echo "ERROR: map file not found: $MAP_FILE" >&2
  exit 1
fi

mkdir -p data

# Links data/region.osm.pbf to the given file.
link_extract() {
  local source="$1" source_abs
  source_abs="$(cd "$(dirname "$source")" && pwd)/$(basename "$source")"
  if [ -L "$OUT" ] && [ "$(readlink -f "$OUT")" = "$source_abs" ]; then
    echo "$OUT is already linked to $source"
  else
    rm -f "$OUT"
    ln -s "$source_abs" "$OUT"
    echo "Linked $OUT -> $source"
  fi
}

# Makes the extract available at data/region.osm.pbf.
provide_extract() {
  if [ -n "$MAP_FILE" ]; then
    link_extract "$MAP_FILE"
    return
  fi

  local routing_osm_source=""
  if [ -f "$ROUTING_CONFIG" ]; then
    routing_osm_source=$(python3 -c "
import json, os, sys

config_path = sys.argv[1]
with open(config_path) as f:
    config = json.load(f)

source_path = config.get('build', {}).get('source', {}).get('osm_source_path')
if source_path:
    if not os.path.isabs(source_path):
        source_path = os.path.normpath(os.path.join(os.path.dirname(config_path), source_path))
    print(source_path)
" "$ROUTING_CONFIG")
  fi

  if [ -n "$routing_osm_source" ]; then
    if [ -f "$routing_osm_source" ]; then
      echo "Found the simulation's own routing OSM extract at $routing_osm_source - reusing it"
      echo "instead of downloading a second copy of the same region."
    else
      echo
      echo "NOTE: custom_sim/routing/routing_config.json expects an OSM extract at:"
      echo "  $routing_osm_source"
      echo "but no file exists there yet."
      echo
      read -r -p "Download the Regierungsbezirk Detmold extract to that path now? [y/N] " REPLY
      if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        mkdir -p "$(dirname "$routing_osm_source")"
        echo "Downloading $URL -> $routing_osm_source"
        curl -fL --progress-bar "$URL" -o "$routing_osm_source.tmp"
        mv "$routing_osm_source.tmp" "$routing_osm_source"
        echo "Done: $(du -h "$routing_osm_source" | cut -f1)"
      else
        echo "Aborting. Download a Regierungsbezirk Detmold .osm.pbf extract yourself and place it at:"
        echo "  $routing_osm_source"
        echo "then re-run this script so it can be reused for the tile server too."
        exit 1
      fi
    fi
    link_extract "$routing_osm_source"
    return
  fi

  echo "$ROUTING_CONFIG not found - downloading a standalone copy for the tile server."
  echo "Downloading $URL -> $OUT"
  curl -fL --progress-bar "$URL" -o "$OUT.tmp"
  mv "$OUT.tmp" "$OUT"
  echo "Done: $(du -h "$OUT" | cut -f1)"
}

# Prints one header option of the extract, empty when the extract does not have it.
header_option() {
  local image
  image="$(docker compose config --images | head -n 1)"
  docker run --rm --entrypoint osmium -v "$(readlink -f "$OUT"):/data/region.osm.pbf:ro" "$image" \
    fileinfo -g "header.option.$1" /data/region.osm.pbf 2>/dev/null
}

# Downloads the outline of the extract's region to data/region.poly. An existing outline stays
# when the extract is the same as before ($1 is "same") and is removed otherwise.
provide_outline() {
  local extract_state="$1" base poly_url
  if ! base="$(header_option osmosis_replication_base_url)"; then
    echo "WARNING: could not read the header of $OUT (Docker is needed); no outline is downloaded." >&2
  elif [[ "$base" != *-updates ]]; then
    echo "NOTE: the extract does not name where it came from, so there is no outline to download."
  else
    poly_url="${base%-updates}.poly"
    echo "Downloading the region outline $poly_url -> $POLY_OUT"
    if curl -fL --progress-bar "$poly_url" -o "$POLY_OUT.tmp"; then
      mv "$POLY_OUT.tmp" "$POLY_OUT"
      return
    fi
    rm -f "$POLY_OUT.tmp"
    echo "WARNING: could not download the region outline." >&2
  fi
  if [ "$extract_state" != same ]; then
    rm -f "$POLY_OUT"
  fi
}

previous_extract="$(readlink -f "$OUT" 2>/dev/null || true)"
provide_extract
if [ "$(readlink -f "$OUT")" = "$previous_extract" ]; then
  provide_outline same
else
  provide_outline changed
fi
