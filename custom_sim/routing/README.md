# Routing

This module provides road-network routing on top of OpenStreetMap data. A
`Router` instance is typically created once for one map area and one active
vehicle profile, then reused for many route queries. Internally, the module
handles graph loading or rebuilds, speed assignment, profile-specific edge
weights, caching, and coordinate snapping.

## Quick start

In normal use, there are only two steps:

1. Create a `Router`.
2. Call `Router.shortest_path(orig, dest)` repeatedly.

The expensive work happens during `Router` creation. Query-time use is centered
on `shortest_path()`.

Assuming this directory is exposed as package `routing`:

```python
from pathlib import Path

from shapely.geometry import Polygon

from routing.router import Router

profiles = {
    "vehicle_a": {
        "max_speed_mps": 13.89,
        "energy_per_wh_m": 0.18,
        "forbid_highways": set(),
        "min_allowed_osm_speed_mps": None,
        "avoid_highways": {},
        "global_speed_factor": 1.0,
    }
}

routing_config = {
    "build": {
        "source": {
            "osm_source_path": None,
        },
        "graph": {
            "network_type": "drive_service",
            "keep_only_largest_strong_component": True,
        },
        "speed_model": {
            "cap_explicit_speeds_with_practical_model": False,
            "cap_explicit_speeds_with_strong_practical_tags": True,
        },
    },
    "runtime": {
        "routing": {
            "default_precise_mode": "fast",
        },
    },
}

# Shapely polygons use (lon, lat) coordinates.
polygon = Polygon([
    (7.45, 51.50),
    (7.50, 51.50),
    (7.50, 51.53),
    (7.45, 51.53),
])

router = Router(
    area_polygon=polygon,
    profile="vehicle_a",
    use_ch=False,
    storage_dir=Path("routing/data"),
    profiles=profiles,
    routing_config=routing_config,
    area_id="example_area",
    force_rebuild=False,
)

res = router.shortest_path(
    orig=(51.5140, 7.4650),   # (lat, lon)
    dest=(51.5200, 7.4900),   # (lat, lon)
)

print(res["time_s"], res["distance_m"], res["energy_wh"])
print(res["path_osm_nodes"])
```

Notes:

- `orig` and `dest` are always `(lat, lon)`.
- `area_polygon` is only needed when the graph must be rebuilt from OSM. If a
  matching GraphML already exists, `area_polygon` can be `None`.
- `profile` must be a key in the supplied `profiles` dict.
- if `precise_mode` is not passed to `shortest_path()`, the router uses
  `routing_config["runtime"]["routing"]["default_precise_mode"]`
- if `routing_config["build"]["source"]["osm_source_path"]` is not `None`,
  the graph is built from that local OSM source file instead of downloading
  from Overpass
- local source rebuilds first reduce the raw source to a buffered polygon
  bbox before OSMnx imports it

## Requirements

Run from this directory:

```bash
pip install -r requirements.txt
```

One package per direct import: `osmnx`, `networkit`, `shapely`, `numpy`, `osmium`, `plotly`,
`scikit-learn`.

`plotly` is only used by `visualization.py`'s debug helpers, not the core algorithm, but is a normal
entry regardless. `scikit-learn` is technically optional for `osmnx.distance.nearest_nodes`, but calling
`Router` directly (as `fleet_planning.py` does) has no fallback for its absence, unlike
`frontend/solver_runner.py`'s dashboard jobs, so it's listed as a real requirement here.

Standard-library modules such as `math`, `os`, `pathlib`, `time`, `pickle`,
`json`, and `re` are also used, but they are part of Python itself and are not
separate install dependencies.

## Public API

### Constructor

Full signature:

```python
Router(
    area_polygon: Polygon | Sequence[tuple[float, float]] | None,
    profile: str,
    use_ch: bool = False,
    storage_dir: str | Path | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    profiles_path: str | Path | None = None,
    routing_config: dict[str, Any] | None = None,
    routing_config_path: str | Path | None = None,
    area_id: str | None = None,
    force_rebuild: bool = False,
)
```

Important arguments:

- `area_polygon`
  Used for OSM download or rebuild. Either a Shapely `Polygon` or a raw
  `(lon, lat)` vertex sequence (built into a `Polygon` internally); `None` is
  also accepted if a matching GraphML already exists, since the graph is then loaded from it instead of rebuilt.
- `profile`
  Active profile name. This selects the edge weight attribute
  `<profile>_time`.
- `use_ch`
  Legacy flag. Not part of supported active behavior.
- `storage_dir`
  Directory for GraphML and cache artifacts.
- `profiles`
  Profile-rule dict injected by the caller.
- `profiles_path`
  Optional path to the JSON profile file if `profiles` is not supplied.
- `routing_config`
  Routing-module config injected by the caller.
- `routing_config_path`
  Optional path to the JSON routing config file if `routing_config` is not
  supplied.
- `area_id`
  Stable map identifier used in cache filenames.
- `force_rebuild`
  Ignore existing caches and rebuild artifacts.

### Profile example

Minimal:

```python
profiles = {
    "vehicle_a": {
        "max_speed_mps": 13.89,
        "energy_per_wh_m": 0.18,
    }
}
```

With optional keys currently used by the builder-side weight functions:

```python
profiles = {
    "vehicle_a": {
        "max_speed_mps": 13.89,
        "energy_per_wh_m": 0.18,
        "forbid_highways": {"motorway_link"},
        "min_allowed_osm_speed_mps": None,
        "avoid_highways": {"residential": 1.2},
        "global_speed_factor": 1.0,
    }
}
```

Meaning of these keys:

- `max_speed_mps: float`
  Vehicle speed cap used when the road allows more.
- `energy_per_wh_m: float`
  Energy use per meter.
- `forbid_highways: set[str]`
  Road classes that should be excluded completely.
- `min_allowed_osm_speed_mps: float | None`
  Optional lower bound for accepted road speed.
- `avoid_highways: dict[str, float]`
  Time-penalty factors for selected road classes.
- `global_speed_factor: float`
  Global multiplier applied to effective speed.

### Routing config example

The routing config is separate from the vehicle profiles. It controls:

- build-time graph creation and speed derivation
- runtime query defaults

Current default file:

```json
{
  "build": {
    "source": {
      "osm_source_path": null
    },
    "graph": {
      "network_type": "drive_service",
      "keep_only_largest_strong_component": true
    },
    "speed_model": {
      "cap_explicit_speeds_with_practical_model": false,
      "cap_explicit_speeds_with_strong_practical_tags": true
    }
  },
  "runtime": {
    "routing": {
      "default_precise_mode": "fast"
    },
    "od": {
      "enabled_by_default": false,
      "enabled_for": {
        "cab": false,
        "pro": false
      },
      "max_nodes": 10000,
      "cache": {
        "backend": "numpy"
      }
    },
    "route_result_cache": {
      "enabled": true,
      "max_entries": 40000,
      "enabled_for": {
        "cab": true,
        "pro": false
      }
    }
  }
}
```

Meaning of the current fields:

| Field | Purpose |
|-------|---------|
| `build.source.osm_source_path` | Base-graph source. `null` downloads from Overpass; a path uses a local `.osm.pbf`, `.osm`, `.xml`, `.osm.gz`, or `.xml.gz` file. Relative paths resolve against `routing_config.json`. Local files are reduced to a buffered bounding box with `osmium` before OSMnx imports them. |
| `build.graph.network_type` | OSMnx extraction mode used during a rebuild. |
| `build.graph.keep_only_largest_strong_component` | Restrict the graph to its largest strongly connected component. |
| `build.speed_model.cap_explicit_speeds_with_practical_model` | Allow the full practical fallback model to cap legal speeds. |
| `build.speed_model.cap_explicit_speeds_with_strong_practical_tags` | Allow the smaller strong-practical tag set to cap legal speeds. |
| `runtime.routing.default_precise_mode` | Query mode used when `shortest_path()` receives no `precise_mode`. |
| `runtime.od.enabled_by_default` | Default OD-routing behavior without a profile override. |
| `runtime.od.enabled_for` | Per-profile OD overrides, typically for `cab` and `pro`. |
| `runtime.od.max_nodes` | OD size guard; `null` disables it. |
| `runtime.od.cache.backend` | OD cache backend: `pickle` or `numpy`. |
| `runtime.route_result_cache.enabled` | Default state of the full route-result cache. |
| `runtime.route_result_cache.max_entries` | Maximum cached results; `0` disables the cache. |
| `runtime.route_result_cache.enabled_for` | Per-profile route-result-cache overrides. |

The first local-source rebuild can take a while. Later runs reuse the normal GraphML and derived
caches.

### Route query

Signature:

```python
Router.shortest_path(
    orig: tuple[float, float],
    dest: tuple[float, float],
    precise_mode: str | None = None,
    eps: float = 1e-9,
) -> dict
```

Input types:

- `orig: tuple[float, float]`
  `(lat, lon)` coordinate pair.
- `dest: tuple[float, float]`
  `(lat, lon)` coordinate pair.
- `precise_mode: str | None`
  `"fast"` or `"exact"`. If `None`, the router uses
  `routing_config["runtime"]["routing"]["default_precise_mode"]`.

Returned dict fields include:

- `path_osm_nodes: list[Any]`
- `time_s: int`
- `distance_m: float`
- `energy_wh: float`
- `entry_energy_wh: float`
- `exit_energy_wh: float`
- `entry_time_s: float`
- `exit_time_s: float`
- `combo: str`
- `query_time_s: float`
- `proj_orig: tuple[float, float]`
- `proj_dest: tuple[float, float]`
- `proj_offsets_m: dict[str, float]`
  Current keys: `"orig"` and `"dest"`.
- `projection_lines: list[list[tuple[float, float]]]`
- `precise_mode: str`

Important semantics:

- `orig` and `dest` do not need to lie on graph nodes.
- Both are first snapped to the nearest graph edges.
- `path_osm_nodes` is the node-to-node route on the graph.
- `path_osm_nodes` uses the original OSM node ids, so the concrete node-id
  type depends on the loaded graph.
- `time_s`, `distance_m`, and `energy_wh` already include the partial edge
  segments between the projected points and the routed start/end nodes.
- `proj_orig` and `proj_dest` are the projected points on the chosen start/end
  edges.
- `projection_lines` is mainly for debugging or visualization.
- If `orig` and `dest` differ by less than `eps` in both coordinates, the result is a zero route:
  `distance_m`, `time_s` and `energy_wh` are 0, `path_osm_nodes` is empty, `combo` is `"same_point"`,
  and `proj_orig` equals `proj_dest`.
- The zero route depends on the input coordinates only. Two different inputs that project onto the
  same point are routed through the nearest graph node at each end, and the result includes both
  partial edge segments. A caller that needs a zero-cost move between a place and itself passes the
  same coordinates as `orig` and `dest`.
- Projecting an already projected point returns that point up to floating point rounding, about
  1e-15 degrees.

### Route segment geometry

Signature:

```python
Router.route_segment_geometry(path_osm_nodes: list[Any]) -> list[tuple]
```

Returns a `(lat_list, lon_list, edge_data)` tuple per hop of `path_osm_nodes`
(as returned by `shortest_path`'s `path_osm_nodes` field). Resolves the same
parallel edge routing itself used for that hop; falls back to a straight line
between the two nodes when the edge has no geometry. Hops with no edge data at
all are skipped.

### Snap

Signature:

```python
Router.snap(lat: float, lon: float) -> tuple[tuple[float, float], float]
```

Returns the point on the nearest edge for `(lat, lon)` and the distance in meters from
the coordinate to that point. These are the values a route query reports as `proj_orig` and
`proj_offsets_m["orig"]`, computed without routing.

## Module layout

Files under this directory:

```text
routing/
|-- README.md                # Documentation
|-- __init__.py              # Package marker
|-- builder.py               # OSM graph construction, speed assignment, profile-specific edge weights
|-- persistence.py           # Cache serialization helpers
|-- router.py                # Main entry point: builds/loads caches, answers route queries
|-- routing_config.json      # Build-time and runtime routing defaults
|-- utils_rt.py              # Routing-local helper functions
|-- vehicle_profiles.json    # Per-vehicle speed/energy profile definitions
|-- visualization.py         # Optional debug plotting helpers, not part of the routing algorithm
`-- data/                    # On-disk caches (OSM requests, GraphML, topology, networkit)
```

## Internal flow

The normal flow inside `Router` is:

1. Reuse or rebuild the base GraphML.
2. Build or load the topology cache.
3. Build the STRtree for nearest-edge snapping.
4. Add profile-specific edge attributes:
   - `<profile>_time`
   - `<profile>_energy_wh`
5. Build or load the `networkit` graph for the active profile.
6. Answer route queries by:
   - snapping both coordinates to graph edges
   - routing between candidate edge endpoints
   - adding partial edge costs at origin and destination

If `build.source.osm_source_path` is set during step 1, the rebuild path is:

1. read the local OSM source file
2. reduce it to a buffered polygon bbox
3. import the reduced temporary file with OSMnx
4. continue with the same polygon-style truncation/simplification flow as the
   normal rebuild path

## Technical notes

### STRtree and snapping

The module does not snap coordinates directly to graph nodes. Instead, it builds
a Shapely STRtree over edge geometries and uses that for nearest-edge lookup.
After the nearest edge is found, the raw coordinate is projected onto that edge.

Technical reference:

- Leutenegger, S. T., Lopez, M. A., & Edgington, J. (1997). STR: A simple and efficient algorithm for
  R-tree packing. In *Proceedings 13th International Conference on Data Engineering* (pp. 497-506). IEEE.
  https://doi.org/10.1109/ICDE.1997.582015

Implications:

- snapping is one of the main query-time costs besides the route call
- route results depend on both the snapped edge and the chosen endpoint
- partial entry and exit costs are computed from the projected point, not just
  from the nearest node

### Query modes

`shortest_path()` supports:

- `precise_mode="fast"`
  Choose the closer endpoint on the start edge and the closer endpoint on the
  end edge, then perform one route query.
- `precise_mode="exact"`
  Evaluate all four start/end endpoint combinations, then keep the best result.

`exact` is more expensive because it can trigger up to four route calls per
query.

### Shortest-path algorithms

The weighted routing graph is a directed `networkit` graph built from the OSM
graph after profile-specific edge times have been added.

At query time, `_route_nodes()` currently uses:

- `networkit.distance.BidirectionalDijkstra` as the main shortest-path method
- `networkit.distance.Dijkstra` as a fallback when path reconstruction fails

This fallback exists because the current implementation has seen cases where the
bidirectional distance is correct, but the reconstructed path is incomplete or
empty. The code therefore:

1. runs bidirectional Dijkstra
2. restores missing endpoints when possible
3. falls back to plain Dijkstra if the returned path is still empty

Implications:

- shortest-path computation is one of the main runtime drivers besides snapping
- `precise_mode="exact"` can multiply the number of route calls by up to four
- if you want to experiment with another shortest-path algorithm, `_route_nodes()`
  is the main place to do that
- if you change the graph representation or edge-weight semantics, this is one
  of the first places that should be rechecked

### Runtime shape

Typical cost distribution is:

- expensive setup during `Router` creation:
  - OSM graph load or rebuild
  - speed preprocessing
  - STRtree construction
  - `networkit` graph build or load
- cheaper repeated query path:
  - edge snapping
  - one or more shortest-path calls
  - partial edge cost reconstruction

Primary performance considerations:

- how often graphs are rebuilt instead of loaded from cache
- whether `precise_mode="exact"` is really needed
- how much time is spent in snapping vs shortest-path calls

## Speed, time, and energy model

The module separates two layers:

1. Base road speed on the shared OSM graph
   - stored as `speed_kph`
   - derived from explicit legal tags when available
   - otherwise derived from fallback and practical logic
2. Vehicle-profile-specific routing weights
   - `<profile>_time`
   - `<profile>_energy_wh`

Useful edge metadata written during preprocessing includes:

- `speed_kph`
- `legal_speed_kph`
- `practical_speed_kph`
- `strong_practical_speed_cap_kph`
- `speed_kph_source`

### Edge speed derivation

The current edge-speed model lives in `builder.py`. The practical fallback and
capping logic is partly aligned with OSRM-style car-profile logic. This applies
especially to:

- fallback speeds by road class
- selected service-road handling
- selected surface caps
- selected tracktype caps
- selected smoothness caps

At a high level:

1. try explicit legal speed tags such as:
   - `maxspeed`
   - `maxspeed:forward`
   - `maxspeed:backward`
   - `zone:maxspeed`
   - `maxspeed:type`
2. if none are usable, fall back mainly on:
   - `highway`
   - `service`
   - `surface`
   - `tracktype`
   - `smoothness`
3. optionally cap explicit legal speed with a smaller set of strong practical
   tags, depending on the current policy switches in `builder.py`

For multi-valued OSM tags such as `highway`, `service`, `surface`,
`tracktype`, and `smoothness`, the current model is order-independent and
conservative. All listed values are considered, and the most restrictive
result is used. This avoids routing differences caused only by reordered
list serialization in GraphML.

### Edge time derivation

`compute_edge_time_for_profile()` currently does:

1. start from edge length
2. convert `speed_kph` to m/s
3. cap by the profile's `max_speed_mps`
4. apply `global_speed_factor`
5. apply optional time penalties from `avoid_highways`
6. compute travel time from effective speed and length

Current time-model features:

- [x] edge-length based travel time
- [x] speed derived from OSM tags and fallback rules
- [x] profile max-speed cap
- [x] optional global speed scaling
- [x] optional highway-specific time penalties
- [ ] turn costs
- [ ] intersection delays
- [ ] acceleration and braking dynamics
- [ ] time-of-day traffic
- [ ] gradient-based speed adjustments

### Edge energy derivation

`compute_edge_energy_for_profile()` currently uses a constant
energy-per-meter formula:

- `energy_wh = energy_per_wh_m * length_m`

The active energy model is currently length-based, not speed-dependent.

Current energy-model features:

- [x] distance-based energy consumption
- [x] profile-specific energy-per-meter parameter
- [ ] active speed-dependent energy model
- [ ] gradient-dependent energy model
- [ ] vehicle-mass or payload effects
- [ ] weather or rolling-resistance effects

### Main change points for a richer model

For adaptation work, the main change points are:

- `add_edge_speeds_from_tags()` in `builder.py`
  for base road-speed derivation
- `compute_edge_time_for_profile()` in `builder.py`
  for travel-time modeling
- `compute_edge_energy_for_profile()` in `builder.py`
  for energy modeling
- `_partial_time()` and `_partial_energy()` in `router.py`
  to keep partial entry and exit costs consistent with full-edge formulas

## Determinism

The module tries to keep repeated rebuilds stable when the underlying OSM graph
is unchanged.

Important measures:

- deterministic sorting of mixed node and edge identifiers
- deterministic selection of the canonical parallel edge for one directed
  `(u, v)` pair
- stable edge ordering when building the STRtree

This matters because OSM graphs can contain multiple parallel edges for the same
directed node pair.

## Cache files

The routing module uses five important cache layers:

| Layer | Contents | Invalidated by |
|-------|----------|----------------|
| OSM request cache | Raw Overpass responses in `data/osm_cache` | Explicit cache removal or a different request |
| GraphML | Durable base OSM graph | Changed build settings or source metadata |
| GraphML metadata sidecar | Settings and source metadata for the GraphML build | Rewritten with GraphML |
| Topology cache | Loaded graph and GraphML stamp | A changed GraphML stamp |
| `networkit` cache | Weighted graph, lookups, GraphML stamp, and profile metadata | Changed GraphML or profile rules |

Additionally, local-source rebuilds create one temporary reduced OSM file and
one temporary location-index file during preprocessing. These are transient
helper files, not durable cache layers.

What is inside them:

- OSM request cache
  - cached Overpass response JSON for exact request URLs
- GraphML
  - OSM graph structure
  - edge and node attributes saved by OSMnx
- GraphML metadata sidecar
  - `build` section from `routing_config.json`
  - source metadata for the base graph build
- topology cache
  - `G`: loaded graph object
  - `graphml_stamp`: size and mtime-based GraphML stamp
- `networkit` cache
  - `nkG`: weighted `networkit` graph
  - `node_map`: OSM node id -> `networkit` node index
  - `edge_choice`: canonical edge key for each directed `(u, v)` pair
  - `graphml_stamp`: size and mtime-based GraphML stamp
  - `profile_name`
  - `profile_rules`

How they relate:

1. The OSM request cache sits below GraphML rebuilds.
2. GraphML is the base artifact.
3. GraphML metadata states which build settings and source mode produced that
   GraphML.
4. The topology cache is derived from GraphML.
5. Profile-specific edge attributes are added to the in-memory graph.
6. The `networkit` cache is derived from that profiled graph.

Important consequence:

- the OSM request cache is only relevant when GraphML must be rebuilt from OSM
- file-source builds do not use the OSM request cache
- GraphML and topology cache depend on `routing_config["build"]`, not on the
  runtime query settings
- the topology cache stays profile-agnostic
- the `networkit` cache is profile-specific

Load order at startup is roughly:

1. check whether the existing GraphML metadata matches the current
   `routing_config["build"]`
2. if yes, try the topology cache using the current GraphML stamp
3. if the topology cache misses, load GraphML directly
4. if the GraphML is missing or its metadata does not match, rebuild GraphML
   from OSM and write a new `.meta.json` sidecar
   During this rebuild, OSMnx first checks `data/osm_cache` for matching raw
   Overpass responses and only downloads missing requests.
   If `build.source.osm_source_path` is non-null, the rebuild uses that local
   source file instead, reduces it to a buffered polygon bbox first, and
   does not query Overpass.
5. rebuild the STRtree from the loaded graph
6. add profile-specific edge attributes in memory
7. try the `networkit` cache if both its GraphML stamp and full profile
   metadata still match
8. otherwise rebuild the `networkit` graph from the current profiled graph

Practical implications:

- deleting GraphML does not necessarily force a fresh OSM download if the
  matching raw requests are already present in `data/osm_cache`
- clearing `data/osm_cache` forces the next GraphML rebuild to fetch fresh
  Overpass responses
- changing the local OSM source file or switching between Overpass and file
  source invalidates the existing GraphML through the GraphML metadata
- local-source rebuilds depend on `osmium`; Overpass-only use does not need the
  local-source path, but importing the module still requires the dependency
  to be installed
- changing `routing_config["build"]` invalidates the existing GraphML and all
  derived caches
- changing only `routing_config["runtime"]` does not rebuild stored artifacts
- changing profile values does not require rebuilding GraphML or the topology
  cache, but it does invalidate the `networkit` cache
- the `networkit` cache filename includes `max_speed_mps`; the full profile
  dict is still stored and checked in its metadata
- if the topology cache is stale, the module falls back to GraphML
- if the `networkit` cache is stale or incomplete, the module rebuilds it from
  the current in-memory graph
- CH-related helpers and filenames still exist in the codebase, but they are
  legacy leftovers and should not be treated as an active feature

## Debugging

`visualization.py` contains optional inspection helpers for:

- whole-graph views colored by speed bands
- whole-graph views colored by highway type

These functions are intended for debugging and analysis, not for the routing
core.

## Notes

- Rebuilding the graph from OSM can change routing results even when the code
  did not change, because the underlying map snapshot changed.
- Profile-specific debug edge fields exist, but they are only for inspection
  and are not part of the routing decision itself.

## Sources

- [OpenStreetMap](https://www.openstreetmap.org/copyright): the road network data, from the
  OpenStreetMap contributors and available under the Open Database License (ODbL).
- [OSRM](https://github.com/Project-OSRM/osrm-backend) (BSD-2-Clause): the fallback speeds and the
  surface, track type and smoothness caps of the edge-speed model in `builder.py` are adapted from its
  car profile. Its license text is in `OSRM_LICENSE.txt`.
