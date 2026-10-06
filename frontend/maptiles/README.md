# Local Map Tile Server

The dashboard's Leaflet maps ([static/js/main.js](../static/js/main.js),
[static/js/form_maps.js](../static/js/form_maps.js),
[static/js/output_details.js](../static/js/output_details.js)) draw the base map by requesting
raster tiles for the visible bounding box. By default, these requests use the public
`{s}.tile.openstreetmap.org` servers and therefore require internet access and remain subject to the
server's usage policy.

This directory provides a self-hosted OSM tile server using
[overv/openstreetmap-tile-server](https://github.com/Overv/openstreetmap-tile-server), a
Docker image wrapping the standard Mapnik/renderd/mod_tile stack. It pre-renders
("preloads") the tiles of the imported region up to a chosen zoom level. Tiles above that level are
rendered by the tile server on first request and cached. After setup the tile server needs no
internet access. The sources of these instructions are listed under [Sources](#sources).

## Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version` must work). On Ubuntu:
  `sudo apt-get install docker.io docker-compose-v2`. The plugin is a separate package on some
  distributions; Docker's own apt repository calls it `docker-compose-plugin`.
- Permission to use the Docker daemon without `sudo`: `sudo usermod -aG docker $USER`, then log
  out and back in. Members of the `docker` group have root-equivalent access to the machine.
  Running the scripts with `sudo` also works, but the files they create, such as `data/`, are
  then owned by root.
- `curl` and `python3`.
- Internet access during setup, for the Docker image, the OSM extract (unless already present),
  the region outline, and the water-polygon data described under
  [What Gets Downloaded and Rendered](#what-gets-downloaded-and-rendered).
  The tile server needs no internet access after setup.
- Free disk space, which depends on the extract. For the Detmold extract the Docker image takes
  about 2 GB and the database volume about 8 GB. The tile cache comes on top. Most tiles at high
  zoom are nearly empty, so the cache stays small: for Detmold it was about 80 MB after zoom 14,
  1.5 GB after zoom 16 and 4.9 GB after zoom 18. `--max-zoom` limits it.

`setup.sh` checks the tools and Docker access first and stops with a message before creating
anything.

## Setup

This service has its own lifecycle, independent of the dashboard. Run the setup once. See
[Dashboard Integration](#dashboard-integration) for how the dashboard finds the running service.

```bash
cd frontend/maptiles
./setup.sh
```

Both arguments are optional:

```bash
./setup.sh [MAP_FILE] [--max-zoom N]
```

- `MAP_FILE`: the OSM extract (`.osm.pbf`) to import. Without it, the extract is the simulation's
  routing extract or a download of the Detmold extract, as described under
  [What Gets Downloaded and Rendered](#what-gets-downloaded-and-rendered).
- `--max-zoom N`: the highest zoom level to pre-render, 0 to 18. The default is 18.

`setup.sh` performs these steps:

1. Provide the extract and its outline.
2. Import the extract into the `maptiles_osm-data` PostGIS volume, about 8 GB for Detmold.
3. Start the tile server on `localhost:8080`.
4. Pre-render zoom levels 0 through `--max-zoom` into `maptiles_osm-tiles`.

On later runs, it starts the existing server and skips completed downloads, imports, and rendered
tiles. The `planet-import-complete` marker identifies a completed import. If an import was interrupted,
or `data/imported-extract` records a different map file, the script stops and names the volumes to
remove. It does not remove them automatically.

## Dashboard Integration

This service is independent of the Flask dashboard and has its own lifecycle. The dashboard
never starts, stops, or otherwise manages it. Whichever `TILE_SOURCE` mode you want, start this
service yourself first (see [Day-to-day operation](#day-to-day-operation) below) if you want the dashboard to use it.

The dashboard health-checks `http://localhost:8080/tile/0/0/0.png` and forwards tile requests to
this server (configurable via the `LOCAL_TILE_SERVER_URL` env var, e.g. to point at a tile server
running on a different host or port), see [maptiles_manager.py](../maptiles_manager.py).

`init_tile_source()` runs once from [app.py](../app.py) at startup; reachability is then rechecked
periodically at runtime (see `_CHECK_TTL_SECONDS` in [app.py](../app.py)), so starting, stopping, or
restarting this service later is picked up by a running dashboard within that window, without
needing to restart the dashboard.

Browsers never contact this server. They request tiles from the dashboard's own
`/tiles/{z}/{x}/{y}.png` route, which forwards each request. The container therefore listens on
`127.0.0.1:8080` only, so it is not reachable from other machines, while dashboard visitors on any
machine still get local tiles. It needs no credentials. The `/tiles` route follows the
dashboard's own access rules, including Basic Auth when `DASHBOARD_PASSWORD` is set.

| Mode | Local coverage | Missing local tiles | Server unavailable at startup |
|------|----------------|---------------------|-------------------------------|
| `fallback` | Only tiles fully inside `data/region.poly` | Use public OpenStreetMap | Log a warning and continue; detect the server later |
| `local` | Every tile overlapping the outline | Refuse the tile; never use the public server | Abort startup |
| `web` (default) | Local server is not used | Use public OpenStreetMap | Not applicable |

Without `data/region.poly`, `fallback` sends all requests to the public server. `local` instead uses
the extract header's bounding box, or accepts every tile if that is also missing. If neither the
selected local source nor the public source can provide a tile, it remains empty.

## Day-to-Day Operation

```bash
docker compose up -d tile-server   # start
docker compose down                # stop, keeps the imported data and rendered tiles
docker compose down -v             # stop and wipe the database/tile cache; re-run setup.sh afterward
```

## What Gets Downloaded and Rendered

`download_extract.sh` makes the Geofabrik Regierungsbezirk Detmold extract (~130 MB) available at
`data/region.osm.pbf` for the tile server to import. This is a normal bulk OSM data download, not tile
scraping, so it isn't subject to the
[OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/). This covers every
demo scenario area currently in use ([demo_scenario_names.txt](../demo_scenario_names.txt)):
Paderborn (a superset of the operation area,
[xml-pre-processing/sicpArea_merged_final.geojson](../xml-pre-processing/sicpArea_merged_final.geojson))
and Hoexter.

The simulation's own routing graph builder (`custom_sim/routing`) needs an OSM extract of the same
region for a different purpose, configured as `osm_source_path` in
`custom_sim/routing/routing_config.json`. Rather than download that ~130 MB twice,
`download_extract.sh` checks whether that file already exists and, if so, symlinks
`data/region.osm.pbf` to it instead of downloading a separate copy. If it doesn't exist either, the
script offers to download it directly to that path (so both sides end up sharing the one file) -
declining aborts so you can download it and place it there yourself, then re-run the script. This
check is skipped (falling back to a plain download straight into `data/region.osm.pbf`) only when
`custom_sim/routing/routing_config.json` itself isn't found, e.g. when this `maptiles` directory is
used standalone without the rest of the repository.

`download_extract.sh` also downloads the outline of the region, the polygon the extract was cut
with, as `data/region.poly` (about 10 KB). A Geofabrik
extract names its origin in its header (`osmosis_replication_base_url`, ending in `-updates`); the
script downloads the outline from that address with `-updates` replaced by `.poly`. The header is
read with the `osmium` tool from the tile server image. An extract without such an address has no
outline to download. The dashboard uses the outline in `TILE_SOURCE=fallback` and
`TILE_SOURCE=local` mode to tell which map tiles this service has data for, see [Dashboard integration](#dashboard-integration). Without an outline, `local` mode uses the bounding box in the extract's header
and `fallback` mode takes every tile from the public servers. When a run leaves the same extract
in place, an existing outline is kept, also after a failed download; when the extract changed, it
is removed, because it belongs to another region. A region's outline covers only part of its
bounding rectangle, about 57% for Detmold.

### Outline for a Map File from Another Source

An extract without an origin address can get its outline from the extract itself, if the region
is an administrative boundary that the extract contains as a relation. This needs the OSM id of
that relation, shown on the region's page on openstreetmap.org (73347 for the Regierungsbezirk
Detmold). The relation is extracted and exported as GeoJSON in the directory `frontend/maptiles`:

```bash
IMAGE=$(docker compose config --images | head -n 1)
docker run --rm --entrypoint sh -v "$PWD/data/region.osm.pbf:/data/map.osm.pbf:ro" "$IMAGE" -c \
  'osmium getid -r /data/map.osm.pbf r73347 -o /tmp/region.osm.pbf && osmium export /tmp/region.osm.pbf -f geojson --geometry-types=polygon -o /dev/stdout --overwrite' > data/region.geojson
```

The GeoJSON is converted to the `.poly` format that the dashboard reads:

```bash
python3 - <<'EOF'
import json

geojson = json.load(open("data/region.geojson"))
lines = ["region"]
section = 0
for feature in geojson["features"]:
    geometry = feature["geometry"]
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    for polygon in polygons:
        for index, ring in enumerate(polygon):
            section += 1
            lines.append(("!" if index else "") + str(section))
            lines += [f"   {lon:.7f}   {lat:.7f}" for lon, lat in ring]
            lines.append("END")
lines.append("END")
open("data/region.poly", "w").write("\n".join(lines) + "\n")
EOF
```

Later runs of `setup.sh` with the same map file keep this outline. The relation has to be the
region the extract was cut with: a different relation makes the decision about which tiles this
service can provide wrong. The dashboard reads the outline once it exists; it is picked up without
a restart.

The tile server's import step also downloads about 1 GB of global water polygon and coastline
data (from `osmdata.openstreetmap.de`, plus a small Natural Earth boundary file from
`naturalearth.s3.amazonaws.com`) and imports it into the database. This data does not depend on the
region.

`seed_tiles.sh` pre-renders the bounding rectangle of the imported region from zoom 0 up to
`--max-zoom` (default 18, the highest level the dashboard's maps use). The rectangle is the
bounding box of the outline; without an outline it is the bounding box in the extract's header.
An outline is irregular, so its bounding rectangle also touches some area with no imported data,
which renders blank.

Each zoom level has about four times as many tiles as the one below, so a higher `--max-zoom`
takes much longer. The counts scale with the size of the area. For example, the Detmold rectangle
has about 7,000 tiles from zoom 0 to 14 and about 1.3 million at zoom 18 alone, and rendering
zoom 18 took about an hour with 8 render threads. Tiles above the given level are rendered on
first request, so a lower limit defers that work to the first view of an area.
`./seed_tiles.sh --max-zoom 16` renders the levels up to 16, and a later run with a higher value
adds the missing levels.

`seed_tiles.sh` skips tiles that already exist by default, so re-running it after a small change
is cheap. Set `FORCE=1 ./seed_tiles.sh` to force a re-render of everything it touches. This is only needed
if the database changed under an area that was already cached with different content (e.g. a tile
cached blank before that area's data was imported), since `render_list` has no way to detect that
on its own and just treats any existing tile file as current. Forcing a re-render of a large area
can take hours, so scope `FORCE=1` runs to just the bounding box that actually needs it rather than
re-running the whole script that way.

## Image Version

`docker-compose.yml` pins the Docker image `overv/openstreetmap-tile-server` to one exact build, identified by its digest (`@sha256:...`). Every machine then runs the same software. A floating tag such as `latest` resolves to the current build on the day the image is downloaded.

Changing the version:

1. Download the wanted version and print its digest. `latest` is replaced by a tag for a specific version.
   ```bash
   docker pull overv/openstreetmap-tile-server:latest
   docker image inspect overv/openstreetmap-tile-server:latest --format '{{index .RepoDigests 0}}'
   ```
2. Set the `image:` line of `docker-compose.yml` to the printed `overv/openstreetmap-tile-server@sha256:...` value.
3. Recreate the container. Its volumes are kept.
   ```bash
   docker compose up -d tile-server
   ```

A newer image can use a different PostgreSQL version, and a database volume written by another major version cannot be started by it. If the container does not start after the change, remove the volumes (`docker volume rm maptiles_osm-data maptiles_osm-tiles`) and run `./setup.sh` again to import the region anew. `seed_tiles.sh` relies on the behavior of `render_list` in this image, so a seed run has to be checked after a change.

## Files

- `docker-compose.yml`: the tile-server service definition (image, volumes, port 8080 on
  localhost only).
- `download_extract.sh [MAP_FILE]`: makes an extract available at `data/region.osm.pbf`: the
  given map file, else the Regierungsbezirk Detmold Geofabrik extract, reusing
  `custom_sim/routing`'s own extract via a symlink when present instead of downloading a second
  copy (see [What gets downloaded and rendered](#what-gets-downloaded-and-rendered) above). It also downloads the region outline to
  `data/region.poly`, replacing any earlier copy.
- `seed_tiles.sh [--max-zoom N]`: pre-renders the bounding box above via the image's built-in
  `render_list`.
- `setup.sh [MAP_FILE] [--max-zoom N]`: runs all of the above in order; the only script needed
  for a first-time setup.
- `data/`: holds the `.osm.pbf`, the `.poly` outline and the `imported-extract` record
  (gitignored and regeneratable via `download_extract.sh` and `setup.sh`; the extract is usually a
  symlink into `custom_sim/routing/data/`, see [What gets downloaded and rendered](#what-gets-downloaded-and-rendered) above).

## Sources

The tile server is not part of this project. The setup steps in this README follow the documentation of
the projects below, so that a reader does not have to research each step.

- [overv/openstreetmap-tile-server](https://github.com/Overv/openstreetmap-tile-server): the Docker
  image. Its README documents importing a map file and running the server, which `setup.sh` and
  `docker-compose.yml` follow.
- [Geofabrik downloads](https://download.geofabrik.de/): the source of the OSM extracts (`.osm.pbf`)
  and region outlines (`.poly`) that `download_extract.sh` downloads.
- [Osmium Tool](https://osmcode.org/osmium-tool/): the command line tool used for the region outline
  and the extract header (`osmium getid`, `osmium export`, `osmium fileinfo`). It runs inside the
  image, and its manual documents the commands.
- [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/): the rules for the
  public tile servers, which apply to the default `TILE_SOURCE=web` mode.

The pre-rendering script `seed_tiles.sh`, the handling of the region outline and the dashboard
integration are written for this project.
