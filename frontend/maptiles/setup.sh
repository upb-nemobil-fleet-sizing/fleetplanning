#!/usr/bin/env bash
# One-shot setup for the local map tile server: provides the OSM extract, imports it into a
# fresh tile-server database, starts the server, and pre-renders its tiles (see seed_tiles.sh).
# Safe to re-run, each step skips work that is already done.
#
# Usage: setup.sh [MAP_FILE] [--max-zoom N]
#   MAP_FILE      OSM extract (.osm.pbf) to import. Without it, download_extract.sh provides the
#                 extract: the simulation's routing extract, or a download of the Detmold extract.
#   --max-zoom N  highest zoom level to pre-render, 0 to 18 (default 18)
set -euo pipefail

fail() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  echo "Usage: setup.sh [MAP_FILE] [--max-zoom N]"
  echo "  MAP_FILE      OSM extract (.osm.pbf) to import; without it, the routing extract or the"
  echo "                Detmold extract is used (see download_extract.sh)"
  echo "  --max-zoom N  highest zoom level to pre-render, 0 to 18 (default 18)"
}

MAP_FILE=""
SEED_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --max-zoom) [ $# -ge 2 ] || fail "--max-zoom needs a value"; SEED_ARGS=(--max-zoom "$2"); shift 2 ;;
    --max-zoom=*) SEED_ARGS=(--max-zoom "${1#*=}"); shift ;;
    -*) fail "unknown option '$1' (see --help)" ;;
    *) [ -z "$MAP_FILE" ] || fail "only one map file can be given"; MAP_FILE="$1"; shift ;;
  esac
done
if [ "${#SEED_ARGS[@]}" -gt 0 ]; then
  { [[ "${SEED_ARGS[1]}" =~ ^[0-9]+$ ]] && [ "${SEED_ARGS[1]}" -le 18 ]; } \
    || fail "--max-zoom must be an integer from 0 to 18 (got '${SEED_ARGS[1]}')."
fi
if [ -n "$MAP_FILE" ]; then
  [ -f "$MAP_FILE" ] || fail "map file not found: $MAP_FILE"
  MAP_FILE="$(readlink -f "$MAP_FILE")"
fi

cd "$(dirname "$0")"

# Prerequisite check, run before anything is downloaded or created.
for tool in curl python3 docker; do
  command -v "$tool" >/dev/null 2>&1 || fail "'$tool' is not installed."
done
docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose v2 plugin is missing (package 'docker-compose-v2' on Ubuntu, 'docker-compose-plugin' from Docker's own repository)."
if ! DOCKER_INFO=$(docker info 2>&1); then
  grep -qi "permission denied" <<<"$DOCKER_INFO" \
    && fail "no permission for the Docker daemon. Run 'sudo usermod -aG docker \$USER', then log out and back in."
  fail "the Docker daemon is not running."
fi

if [ -n "$MAP_FILE" ] || [ ! -f data/region.osm.pbf ] || [ ! -f data/region.poly ]; then
  ./download_extract.sh ${MAP_FILE:+"$MAP_FILE"}
else
  echo "data/region.osm.pbf and data/region.poly already present, skipping download."
fi

# The tile server image writes this file into the database volume once an import has finished.
IMPORT_MARKER=/data/database/planet-import-complete
RESET_HINT="docker volume rm maptiles_osm-data maptiles_osm-tiles"
# Records which extract the database was imported from: its path and size.
EXTRACT_RECORD=data/imported-extract
EXTRACT_ID="$(readlink -f data/region.osm.pbf) $(stat -Lc %s data/region.osm.pbf)"

# Checked only when the volume already exists, because 'docker compose run' creates a missing one.
import_finished() {
  docker compose run --rm --no-deps --entrypoint test tile-server -f "$IMPORT_MARKER" >/dev/null 2>&1
}

if ! docker volume inspect maptiles_osm-data >/dev/null 2>&1; then
  echo "Importing region extract into the tile server database (this can take a while)..."
  docker compose run --rm tile-server import
  echo "$EXTRACT_ID" > "$EXTRACT_RECORD"
elif import_finished; then
  if [ -f "$EXTRACT_RECORD" ] && [ "$(cat "$EXTRACT_RECORD")" != "$EXTRACT_ID" ]; then
    fail "The tile server database was imported from '$(cat "$EXTRACT_RECORD")' (path and size), but the map file is '$EXTRACT_ID'. Remove the volumes and run this script again to import the map file: $RESET_HINT"
  fi
  echo "Tile server database already imported, skipping import (remove it with"
  echo "  $RESET_HINT"
  echo "to force a clean re-import)."
else
  fail "The maptiles_osm-data volume exists, but its import never finished." \
    "Remove the volume and run this script again: $RESET_HINT"
fi

echo "Starting tile server..."
docker compose up -d tile-server

echo "Waiting for the tile server to accept connections..."
WAIT_ATTEMPTS=60
WAIT_SECONDS=5
tile_server_up=0
for _ in $(seq 1 "$WAIT_ATTEMPTS"); do
  if curl -fsS -o /dev/null "http://localhost:8080/tile/0/0/0.png"; then
    tile_server_up=1
    break
  fi
  sleep "$WAIT_SECONDS"
done
[ "$tile_server_up" = 1 ] || fail "The tile server did not answer at http://localhost:8080 within" \
  "$((WAIT_ATTEMPTS * WAIT_SECONDS)) seconds. See 'docker logs nemo-tile-server' for the reason."

./seed_tiles.sh ${SEED_ARGS[@]+"${SEED_ARGS[@]}"}

echo
echo "Local tile server is up at http://localhost:8080 and its tiles are seeded."
echo "Start the dashboard with TILE_SOURCE=local to serve its maps from it, see frontend/README.md."
