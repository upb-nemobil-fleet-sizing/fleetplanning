# Line Planning

This module derives Pro lines from request data and writes the corresponding fleet-planning BaseData:
chaining locations, chain routes, and chain-route schedules.

## Setup

Install the dependencies with `python -m pip install -r requirements.txt`.

Run the scripts and notebook from this folder. Run `combine_base_pro.py` from the repository root
(see [Usage](#usage)).

## Module Layout

Files under this directory:

```text
line_planning/
|-- README.md                        # Documentation
|-- auto_base_file_generator.py      # Script: writes a BaseData file from request data
|-- combine_base_pro.py              # Script: merges a line planning result into a BaseData file
|-- operational_vertices.py          # OperationalVertices, the bounding polygon of the area of interest
|-- pro_lines_experiments.ipynb      # Notebook: the main workflow, from request data to a base data file
|-- request_processing_utils.py      # RequestDataframe and the classes it builds
|-- requirements.txt                 # Python dependencies
`-- data/input/auto_bf_generation/   # Request files read by the scripts and the notebook, not in git
```

## Usage

### Notebook

`pro_lines_experiments.ipynb` is the main workflow. It transforms request data into a BaseData file
and visualizes intermediate results. Start Jupyter from this folder so its relative paths resolve.

| Stage | Result |
|-------|--------|
| Explore demand | Route, location, timing, distance, and duration plots |
| Find demand corridors | Pickup/dropoff clusters and origin-destination pairs |
| Define lines | Candidate lines, chaining locations, and selected routes |
| Build schedules | Assigned requests and one of two schedule variants |
| Export | A BaseData file for fleet planning |

The notebook works through these steps:

1. **Demand overview.** Creates a `RequestDataframe` from the request folder and adds the routes with `add_osmnx_route_columns()`. Shows edge and junction usage (`get_edge_frequencies()`, `get_node_frequencies()`), pick-up and drop-off locations, reservation and trip timings, and trip distances and durations.
2. **Routes over time.** Maps of pick-up and drop-off coordinates and routes for a time-of-day window, and for a single trip by `UserGuid`.
3. **Clusters and OD pairs.** `determine_lines_with_clustering()` clusters the pick-ups and drop-offs, with `eps = 1000` and `minpts = 25` in the notebook. Then the cluster centres, the OD matrix between clusters and the travel times between clusters are shown.
4. **Demand over time.** `analyze_od_stability()` and `plot_od_time_profile()` show how the OD flows change across time bins.
5. **Candidate lines.** `plot_lines_with_chaining_locations()` shows the lines and their chaining locations. `print_line_info()` gives the clusters, duration and distance of a line.
6. **Selected lines and schedules.** `reassign_trips_to_lines()` assigns the requests to the lines you keep. Two schedule methods are available: `generate_line_schedule_with_max_frequency()` for a fixed frequency with one Pro, and `generate_line_schedule()` for a demand-driven schedule with `available_pros` Pros.
7. **Base data.** `generate_ini_base_data()` writes the base data file for the selected lines and the service window. Its `pros_per_line` argument sets how many Pros each line gets. Max-frequency schedules are generated in this method.
8. **Lines between chosen coordinates.** `add_line_between_coords()` adds lines between coordinates you choose, optionally via other coordinates. Then `reassign_trips_to_lines()` and `generate_ini_base_data()` run as in steps 6 and 7.

Step 3 caches its result. The notebook saves the `RequestDataframe` object as `RDF_<eps>_<minpts>.pkl` in the current folder. On the next run, it loads that file instead of clustering again. The file name contains only `eps` and `minpts`, so after the request data changes, delete the file. Otherwise the notebook keeps the clusters of the old data. Pickle files can run code when they are loaded, so only load files you created.

### auto_base_file_generator.py

Note: this script is out of date. It creates one Pro per line. The notebook can create several Pros per line through the `pros_per_line` argument of `generate_ini_base_data()`.

The script reads the request data at `--fpath_in`, which is a single file or a folder of instance files, and writes a base data file to `--fpath_out`. The output contains ChainingLocations, ChainRoutes and ChainRouteSchedules.

By default, the script determines lines automatically and keeps the `--n_lines` most used lines. With `--manual_select_lines`, you enter the IDs of the lines to use when prompted. `--fpath_manual_lines` adds lines from a JSON file. Each line in that file has `start` and `end` coordinates and optionally `via` coordinates.

Three examples:

Three most used lines from a folder:

```bash
python auto_base_file_generator.py \
    --fpath_in data/input/auto_bf_generation \
    --fpath_out example_base_data.json \
    --auto_select_lines \
    --n_lines 3
```

Manually selected lines from a folder:

```bash
python auto_base_file_generator.py \
    --fpath_in data/input/auto_bf_generation \
    --fpath_out example_base_data.json \
    --manual_select_lines
```

Manually selected lines plus lines from a JSON file:

```bash
python auto_base_file_generator.py \
    --fpath_in data/input/auto_bf_generation \
    --fpath_out example_base_data.json \
    --manual_select_lines \
    --fpath_manual_lines data/input/manual_lines.json
```

### combine_base_pro.py

The script combines the chaining data of a line planning result with an existing BaseData file into a complete BaseData file. The chain file provides the chaining locations, chain routes and chain route schedules. The Pros are derived from the chain route schedules. Charging points, cabs and the operation area come from the BaseData file. The request file is needed because only FleetAndRequests can write a BaseData file.

Run it from the repository root, because it imports the root modules `utils` and `models`:

```bash
python -m line_planning.combine_base_pro <chain_file> \
    --base_data <BaseData.json> \
    --requests <requests.json> \
    --output <file.json>
```

## Line Determination

The steps below use the **RequestDataframe** class from `request_processing_utils.py`.

Its output is the set of lines and the assignment of requests to them. The timetables of the lines are built from that assignment.

1. **Initialize RequestDataframe**

    A **RequestDataframe** (RDF) object is initialized with:
    - A file path to an instance file, or a directory containing multiple instance files
    - An **OperationalVertices** object from `operational_vertices.py`, given as a list of `(lat, lon)` vertices. The road network is downloaded as a circle around the centre of the vertices' bounding box, with a radius that reaches the north-east corner.

    - Every request in the file or folder is loaded into `self.df`, one row each.
    - Requests outside the circle are kept. Nothing is filtered by area.
    - Each pick-up and drop-off point is snapped to its nearest road node.

2. **Cluster Pickups and Drop-offs**

    The `determine_lines_with_clustering()` method applies network-constrained **DBSCAN** (see [Sources](#sources)) clustering to both pickup and drop-off locations.

    This step assigns a cluster label to the pickup and to the drop-off point of each request. Two new columns are added to `self.df`:
    - `PickUpCluster`
    - `DropOffCluster`

    - Points that belong to no cluster get the label `-1`. Later steps ignore them.
    - The parameters are `eps` (default 250 m) and `minpts` (default 20). The notebook uses 1000 m and 25.
    - A cluster's centre is the mean coordinate of its points.

3. **Compute Cluster Distances and Durations**

    The `determine_cluster_travel_time_and_distances()` method estimates:
    - Road-network travel times between cluster centroids
    - Road-network distances between cluster centroids

    This information helps:
    - A pair can only form a line if its road distance is at least the value of `MINIMUM_CLUSTER_DISTANCE_FOR_LINE` (currently 2000 m) in both directions.
    - Two clusters are close if their road distance is at most the value of `CLOSE_CLUSTER_DISTANCE_THRESHOLD` (currently 1000 m) in both directions. A close cluster is added to the same line as the cluster it is close to.

4. **Create the lines**

    The `determine_lines_from_cluster_info()` method counts the trips between each pair of clusters, with both directions added together.
    - The most used pair that is not yet on a line becomes the seed of a new line.
    - The line takes every pair (a, b) where a is close to the seed's first cluster and b is close to the seed's second cluster.
    - Skipped: pairs already on a line, pairs without trips, and pairs under the minimum distance.
    - The next unused seed starts the next line. Lines with no pairs left are dropped.

    Because the seeds are taken in order of use, the result depends on that order.

5. **Place the chaining locations**

    The `determine_chaining_locations_from_line_info()` method places the chaining locations of each line.
    - The start point is the size-weighted centre of the line's first clusters, and the end point is the centre of its second clusters. Both are snapped to the nearest road node.
    - A node already used by another line is avoided. The nearest node at least two road steps away is used instead.
    - Each line gets two **ChainRoute** objects, one in each direction, between its start and end points.
    - A route with fewer than 4 nodes that is also shorter than 1500 m (`MINIMUM_LINE_DISTANCE`) is rejected, and its line is removed.

    The chaining locations on these routes are described in [Chaining Location Placement](#chaining-location-placement).

6. **Assign Lines and Chaining Locations to Requests**

    A request matches a line when its pick-up cluster and drop-off cluster form one of the line's pairs, in either order. The start chaining location is the one on the pick-up side, and the end chaining location is the one on the drop-off side.

    The request entries in `self.df` receive these columns:
    - `Line`: assigned line ID
    - `StartChainingLocation`: assigned `ChainingLocation` for chaining
    - `EndChainingLocation`: assigned `ChainingLocation` for unchaining

    Requests that do not match any line are assigned `-1` in all three columns.

    The requests assigned to a line are the ones its timetable has to serve at its start chaining location.

## Chaining Location Placement

**ChainingLocations** are placed while the **ChainRoute** objects of a line are created. The `determine_route()` method in `request_processing_utils.py` handles this.

- **Suitable edges:** an edge along the route qualifies if:
  - it is longer than 20 m (`MIN_CL_EDGE_LENGTH`),
  - it is not one-way,
  - neither of its end nodes is used by another line's chaining locations.
- **Chaining location:** the first qualifying edge along the route.
- **Unchaining location:** the last qualifying edge along the route.
- **Fallback:** if no edge qualifies, the chaining location uses the route's first two nodes, and the unchaining location uses its last two. These fallback edges are not checked for length, one-way status or occupancy.
- **Placement:** `set_location()` puts the location at the center of the chosen edge and gives it a fixed length of 10 m.

## Known Limitations

- **Schedules:** the base data uses `generate_chain_route_schedules2()` of **RequestDataframe**, a fixed-frequency schedule between the earliest and latest assigned request.
  - The schedule does not depend on the arrival counts in the input data.
  - The demand-driven `generate_chain_route_schedules()` builds its schedule from those counts and is kept as an alternative.
- **Reassignment:** `reassign_trips_to_lines()` of **RequestDataframe** gives the requests of dropped lines a kept line. The timetable of each kept line is built from the requests assigned to it.
  - The lines to keep are chosen after the lines are built (`lines_to_keep` in the auto-generator, `selected_lines` in the notebook). The other lines are dropped.
  - A request on a kept line keeps that line.
  - Any other request moves to the kept line with the shortest road detour (pick-up to start location, end location to drop-off), if that is at least 1 m shorter than its direct route.
  - A request whose direct route is under 2000 m (`LINE_USAGE_THRESHOLD`) gets no line, even if it was on a kept line.

## Sources

- [OpenStreetMap](https://www.openstreetmap.org/copyright): the road network data, from the
  OpenStreetMap contributors and available under the Open Database License (ODbL).
- Ester, M., Kriegel, H.-P., Sander, J., & Xu, X. (1996). A density-based algorithm for discovering
  clusters in large spatial databases with noise. In *Proceedings of the Second International
  Conference on Knowledge Discovery and Data Mining (KDD-96)* (pp. 226-231). AAAI Press.
