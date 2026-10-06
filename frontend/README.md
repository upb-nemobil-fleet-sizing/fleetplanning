# NeMo.bil Fleet Planning Dashboard

This folder contains the Flask dashboard for preparing fleet-planning inputs, starting simulations,
and analyzing their output.

The dashboard has two operating modes:

- Normal mode: upload/edit input data, create schema-compliant simulation inputs, start the configured solver backend, and read the generated outputs.
- Demo mode: expose a read-only scenario picker and prepared result views without uploads or real solver execution.

The dashboard keeps its active state in memory while the Flask process is running. Backend simulation
outputs remain in backend-owned output folders; frontend-owned job metadata and logs are stored under
`frontend/sim_results/dashboard_jobs`.

## Table of Contents

*   [What The Dashboard Does](#what-the-dashboard-does)
*   [Map Tiles](#map-tiles)
*   [Setup](#setup)
*   [Configuration](#configuration)
*   [Usage](#usage)
*   [Data & Files](#data--files)
*   [Backends](#backends)
*   [Deployment](#deployment)
*   [Reference](#reference)

---

## What The Dashboard Does

| Stage | Capabilities |
|-------|--------------|
| Prepare | Upload and normalize `BaseData` and JSON or XML `RideData`; edit vehicles, infrastructure, routes, requests, the operation area, and the scenario time window. |
| Configure | Select a backend and search strategy; set constraints, objectives, and costs; or evaluate one uploaded fleet with the `custom` backend. |
| Run | Write schema-compliant inputs and queue background runs for `custom`, `rwapi`, or `sumo`. |
| Analyze | Load generated or uploaded output; compare KPIs; inspect Gantt charts, routes, energy timelines, and vehicle details. |
| Demonstrate | Present prepared scenarios and results in a restricted, read-only mode. |

Normal mode can also export the edited inputs and restore settings from a previous
`simulation_metadata.json` file.

---

## Map Tiles

Every map uses Leaflet with raster OSM tiles. The optional [local tile server](maptiles/README.md)
covers Regierungsbezirk Detmold and avoids both internet access and public-server rate limits.

| `TILE_SOURCE` | Behavior |
|---------------|----------|
| `web` (default) | Use public OpenStreetMap servers; do not contact the local server. |
| `fallback` | Try the local server first, then request unavailable tiles from OpenStreetMap. |
| `local` | Use only the local server. |

Browsers never contact the local server directly. The dashboard proxies requests through
`/tiles/{z}/{x}/{y}.png`, so only the dashboard host must be able to reach it. See
[maptiles/README.md](maptiles/README.md) for setup and the environment variables below for connection
settings.

---

## Setup

### Requirements

The project was developed for Python 3.12. The pinned dependency versions (for example `numpy`, `scikit-learn` and `networkx`) need Python 3.11 or newer.

Install the frontend dependencies from the repository root:

```bash
python -m pip install -r frontend/requirements.txt
```

Composes `custom_sim/routing/requirements.txt`. Launching a `custom` backend solve through the
dashboard does not need `custom_sim`'s own `matplotlib`/`plotly` additions; `solver_runner.py` installs
fallback modules when either package is missing. `networkx` is the dashboard's own dependency
(OSMnx-fallback routing). `pyproj` is only needed by `xml-pre-processing/`, which shares this file.

### Quick Start

Run the dashboard locally on port `5000` from the repository root:

```bash
python -c "import sys; sys.path.insert(0, 'frontend'); from app import app; app.run(host='127.0.0.1', port=5000, debug=False)"
```

Then open:

```text
http://127.0.0.1:5000/
```

Running `frontend/app.py` directly binds to `0.0.0.0:80` with `debug=False`, so it usually needs elevated privileges on Linux:

```bash
sudo /path/to/venv/bin/python /path/to/fleetplanning/frontend/app.py
```

Same `app.run(host=..., port=...)` call either way, just different arguments. `127.0.0.1` only accepts connections from the machine itself; `0.0.0.0` accepts connections from anywhere. Port `80` is the default for plain `http://` addresses; any other port needs `:<port>` in the URL.

### Project Structure

Top-level layout:

```text
frontend/
|-- app.py                       # Flask routes, in-memory state, upload/export endpoints, demo mode, job handling, CRUD views
|-- export_utils.py              # Converts edited in-memory state back into API-compatible JSON
|-- output_utils.py              # Parses solver output files, builds KPI/chart payloads
|-- output_details_utils.py      # Builds fleet/vehicle detail payloads, routes, Gantt data, detailed KPIs
|-- movement_visualization.py    # Builds the fleet movement animation HTML
|-- solver_runner.py             # Runs fleet_planning.py and custom_sim/run_instance.py with dependency stubs
|-- maptiles_manager.py          # Picks the tile source, health-checks the optional local tile server and forwards tile requests to it for the /tiles route; init_tile_source() runs once from app.py at startup
|-- requirements.txt             # Dependencies
|-- templates/                   # Jinja2 page templates
|-- static/                      # Static assets: JS, CSS, images
|   `-- js/                      # Client-side scripts
|       |-- main.js              # Overview map, operation-area handling, input upload behavior
|       |-- form_maps.js         # Reusable map initializers for entity create/edit forms
|       |-- output.js            # Output upload and KPI chart rendering
|       |-- output_details.js    # Fleet/vehicle detail charts, route map interactions
|       |-- tile_layer.js        # Builds the tile layer shared by every map, honoring TILE_SOURCE
|       `-- movement_viz.js      # Loads and controls the movement animation iframe
|-- xml-pre-processing/          # Optional pipeline: XML population plans -> request JSON
`-- maptiles/                    # Optional self-hosted local OSM tile server (Docker-based), see maptiles/README.md
```

## Configuration

### Authentication

Basic Auth is optional. If `DASHBOARD_PASSWORD` is unset, authentication is disabled. If it is set, all routes require a username and password.

Environment variables:

- `DASHBOARD_USER`: username, defaults to `admin`
- `DASHBOARD_PASSWORD`: password, unset means no Basic Auth

Example:

```bash
DASHBOARD_USER=admin DASHBOARD_PASSWORD='change-me' \
python -c "import sys; sys.path.insert(0, 'frontend'); from app import app; app.run(host='127.0.0.1', port=5000, debug=False)"
```

### Environment Variables

- `DEMO_MODE`: enables demo mode when set to `1`, `true`, `yes`, or `on`.
- `DEMO_LOADING_SECONDS`: fake loading duration before demo results open, defaults to `5`.
- `DASHBOARD_USER`: Basic Auth username, defaults to `admin`.
- `DASHBOARD_PASSWORD`: Basic Auth password. If unset, Basic Auth is disabled.
- `FLEETPLANNING_OUTPUT_ROOT`: root directory that contains backend output folders, defaults to the repository root.
- `FLEETPLANNING_SETUP_NAME`: optional override for the setup folder name inside each backend
  output folder. It is unset by default. See [Output Folder Naming](#output-folder-naming) below for the resulting naming
  scheme. Set: every job's output goes into this one fixed folder name instead.
- `ROUTER_AREA_FILE`: operation-area file used by output-detail routing, defaults to `frontend/uploads_temp/operation_area.json`.
- `TILE_SOURCE`: selects `web`, `fallback`, or `local`; see the [Map Tiles](#map-tiles) comparison.
  The dashboard does not manage the separate local server. In `fallback` mode, an unavailable server
  produces a warning and is detected later through periodic health checks. In `local` mode, an
  unavailable server prevents dashboard startup. Tiles unavailable from either configured source
  remain empty.
- `LOCAL_TILE_SERVER_URL`: base URL the dashboard process uses to reach the local tile server
  (health check and tile requests) in `TILE_SOURCE=local`/`fallback`, defaults to
  `http://localhost:8080`. Browsers never use it. Only needs changing if that server runs on a
  different host or port.

---

## Usage

### Demo Mode

Enable demo mode with `DEMO_MODE=1`.

In demo mode:

- Uploads and other non-GET actions are blocked app-wide by a single `before_request` hook (403 on anything but GET/HEAD/OPTIONS), not per-route.
- The navigation is reduced to scenario selection and simulation results.
- `/simulation-overview` redirects to the scenario selection page.
- The scenario page lists only prepared scenarios that have schema-compliant output files.
- Hovering or focusing a scenario card updates the map preview.
- Demo maps show chain-route lines between matching chaining locations by default; the legend checkbox `Chaining Lines` can hide them.
- The simulate action shows a short loading progress and then opens the prepared output.
- No real solver process is started.
- Cost assumptions can still be edited because they only affect derived result KPIs, not the simulation output files.
- Demo scenario names and visible output KPIs can be adjusted through optional text files in the `frontend` folder.

Run demo mode locally:

```bash
DEMO_MODE=1 \
python -c "import sys; sys.path.insert(0, 'frontend'); from app import app; app.run(host='127.0.0.1', port=5000, debug=False)"
```

Run demo mode with Basic Auth:

```bash
DASHBOARD_USER=admin DASHBOARD_PASSWORD='change-me' DEMO_MODE=1 \
python -c "import sys; sys.path.insert(0, 'frontend'); from app import app; app.run(host='127.0.0.1', port=5000, debug=False)"
```

Run demo mode on port `80` when the app checkout is in `fleetplanning-demo` but the virtual environment is still in `fleetplanning`:

```bash
sudo env DASHBOARD_USER=admin DASHBOARD_PASSWORD='change-me' DEMO_MODE=1 \
  /path/to/fleetplanning/.venv/bin/python \
  /path/to/fleetplanning-demo/frontend/app.py
```

The artificial demo loading duration defaults to five seconds. Override it with:

```bash
DEMO_LOADING_SECONDS=2 DEMO_MODE=1 \
python -c "import sys; sys.path.insert(0, 'frontend'); from app import app; app.run(host='127.0.0.1', port=5000, debug=False)"
```

### Recommended Workflow

Normal mode:

1. Open the landing page at `/`.
2. Go to `/upload` and upload the input files.
3. Open `/index` and review or edit the loaded scenario data.
4. Define at least three operation-area points and set the time window.
5. Go to `/simulation-overview`, choose the algorithm, name the scenario, and set prices.
6. Start the simulation or save the metadata draft.
7. Wait until the background job is completed.
8. Open `/output` to compare KPIs across runs.
9. Click a paired output point to select a fleet configuration, then open its detailed Gantt charts and vehicle routes with the details button in the KPI table header.

Demo mode:

1. Start the app with `DEMO_MODE=1`.
2. Open `/upload` or use the landing-page start button.
3. Select a prepared scenario and optionally adjust the cost assumptions.
4. Click the simulate button and wait for the short loading step.
5. Review the prepared results on `/output` and open detail views as needed.

### Main Pages

#### Landing Page (`/`)

Introduces the NeMo.bil dashboard and links into the main workflow. In demo mode the links point to the scenario selection and prepared results.

#### Upload And Scenario Page (`/upload`)

Normal mode accepts the input files required for a scenario:

- `BaseData` JSON
- `RideData` JSON
- `RideData` XML, which is converted into the internal ride-request JSON structure

After upload, the dashboard normalizes the data into an in-memory format used by the edit forms and maps.

Warns when loaded ride requests fall outside the operation area, with an option to remove them (same
check as `/index`, shown here too so it's visible right after upload finishes).

Demo mode replaces the upload controls with prepared scenario cards, a map preview, cost-assumption inputs, and the simulate button. The page does not expose that uploads are disabled.

#### Scenario Overview (`/index`)

Shows the scenario state. In normal mode users can review and modify:

- Cabs
- Charging points
- PRO schedules
- Chaining locations
- Chain routes
- Chain route schedules
- Ride requests
- Operation area
- Scenario start and end time
- Whether to use an uploaded Pro timetable as-is instead of the routing-derived one (not recommended, warns when enabled)

Warns when loaded ride requests fall outside the operation area, with an option to remove them.

`?locked=1` renders the same page read-only (used by `/output`'s "Zur Read-Only Seite" link). Demo mode is always locked.

#### Simulation Overview (`/simulation-overview`, "Simulations-Konfigurator")

Normal mode uses this page to configure and launch a simulation. Two job kinds:

- Search-based run: solver backend, search strategy (`static`/`adaptive`) and its parameters (iteration/time limit, initial cabs, cab growth step, Pro-removal trial settings, stagnation tolerance), Early Unchaining, optional hard constraints (budget, minimum service level), and a search objective blending one customer-perspective and one operator-perspective metric.
- Fixed-fleet evaluation ("fixedfleet"): runs `custom` once on the fleet exactly as uploaded, no search.

Also supports:

- Scenario name and a full cost model (vehicle prices, lifetimes, interest rate, operating days, fare structure), used by the search objective whenever it includes a cost or profit metric. The fare structure follows the fare parameters of the MATSim DRT module ([`DrtFareParams`](https://github.com/matsim-org/matsim-libs/blob/master/contribs/drt/src/main/java/org/matsim/contrib/drt/fare/DrtFareParams.java)): base fare, minimum fare per trip, distance and time rate, and daily subscription fee
- Import of previous `simulation_metadata.json`, restoring all of the above
- A read-only settings-peek modal for completed jobs
- Background simulation start, queued one job at a time
- Running, failed, and completed simulation tables, with deletion of frontend-owned entries
- A warning before a run whose output folder already exists and belongs to no job in the list

In demo mode this page redirects back to scenario selection.

#### Output Overview (`/output`)

Visualizes uploaded, generated, or prepared solver outputs. In normal mode, it can import matching output files from a selected folder. Each imported scenario becomes a job in the job list, and importing the same scenario again updates that job. In demo mode, it reads the selected prepared scenario from the configured output root.

The output page builds paired KPI charts so different fleet configurations can be compared. Clicking a chart point selects that fleet configuration and updates the KPI table below. The details button next to the iteration number in the table header opens that iteration's detailed output page in a new tab.

In demo mode, the separate utilization bar chart below the main chart is hidden. Utilization remains available as a selectable KPI in the main chart when it is included by the demo KPI configuration.

Some paired KPIs are defined as follows:

- Customer distance is the distance driven with the customer on board, from pickup to dropoff. For convoy trips it includes the chaining and unchaining legs.
- Customer in-vehicle time runs from the pickup departure to the dropoff arrival, plus the pickup and dropoff stop's own boarding and alighting time. It includes time spent waiting while chained.
- Rejects are the requests without a customer trip in the CAB output, so every request that no cab serves counts as a reject. The rejection rate is rejects divided by all requests.
- Trip revenue applies the fare structure of the cost model to each trip's distance and the time between pickup departure and dropoff arrival; boarding and alighting are not billed.

#### Output Details (`/output/details/<num_cabs>`)

Shows a detailed fleet view for one selected fleet configuration:

- Fleet-level Gantt chart. A Cab shows its customer trips, a Pro all of its tasks. A Cab without
  any customer trip appears as a grey bar labelled "Keine Kundenfahrten" across its schedule.
- Route map
- Vehicle row click interaction
- Vehicle-level Gantt chart
- Energy timeline
- Detailed KPI cards. A PRO vehicle's card includes the number of its own scheduled service
  trips, how many of those ran without a Cab chained, the maximum number of Cabs chained at
  once, total chain/unchain handshake time, and energy delivered to chained Cabs. Deadhead
  (empty repositioning) legs are excluded, a Cab was never eligible to chain onto one.
- Trip visibility toggling on the map
- Depot markers where available
- Fleet movement animation: interactive Leaflet playback of the whole fleet, vehicles clickable
  for detail, generated on demand. The generated file holds no tile address: its tile source
  follows the `TILE_SOURCE` setting of the dashboard that embeds it, like the maps. A file
  generated from an older template contains a fixed OpenStreetMap tile address and has to be
  regenerated to follow the setting. In general, a change to the animation template reaches only
  files generated after it.

---

## Data & Files

### Schema-Based Simulation Files

The dashboard expects simulation inputs and outputs in backend-specific output folders. The root can be configured with `FLEETPLANNING_OUTPUT_ROOT`; by default it is the repository root.

Backend roots:

- `custom`: `<FLEETPLANNING_OUTPUT_ROOT>/custom_sim/output`
- `rwapi`: `<FLEETPLANNING_OUTPUT_ROOT>/rw/output`
- `sumo`: `<FLEETPLANNING_OUTPUT_ROOT>/sumo/output`

```text
<backend-root>/<setup-name>/
```

`<setup-name>` is `FLEETPLANNING_SETUP_NAME` if set, otherwise the hash-based folder name described
under [Output Folder Naming](#output-folder-naming) (e.g. `static_a1b2c3d4`).

Schema prefix:

```text
<area>_<stations>_<lineConfig>_<batteryCapacity>_<chargingPower>_<scenario>_<timewindow>
```

Example:

```text
pb_3cs_2lines_10bc_11cp_1157rq_120tw
```

Schema-compliant files:

```text
<prefix>_input_base_file_<iteration>.json
<prefix>_input_req_file_<iteration>.json
<prefix>_output_cab_<iteration>.json
<prefix>_output_sim_<iteration>.json
<prefix>_output_pro_<iteration>.json
```

The dashboard derives the prefix from the loaded input data:

- `area`: from area metadata where available, otherwise from the vehicle license-plate prefix, otherwise `pb`.
- `stations`: number of charging points, for example `3cs`.
- `lineConfig`: estimated line count from chain routes, for example `2lines`.
- `batteryCapacity`: maximum cab battery capacity in kWh, for example `10bc`.
- `chargingPower`: maximum charging-point power in kW, for example `11cp`.
- `scenario`: number of ride requests, for example `1157rq`.
- `timewindow`: pickup time-window duration in minutes; target time-window is used as fallback when pickup time is missing.

The prefix labels the generated files. Its tokens are counts and maxima, so two different inputs can
share a prefix. A job's identity is the content hash described under
[Output Folder Naming](#output-folder-naming).

Prepared demo scenarios are discovered from output files matching:

```text
<prefix>_output_cab_<iteration>.json
<prefix>_output_sim_<iteration>.json
<prefix>_output_pro_<iteration>.json
```

Matching `input_base_file` and `input_req_file` files are used to populate the demo scenario map and read-only input state. Request-only files are not enough to create a demo result scenario.

Prepared output folders may also contain an optional unprefixed `simulation_metadata.json`. Demo mode reads that file when present and then applies the current URL cost assumptions on top for the displayed results.

#### Optional Demo Text Configuration

Three optional text files in the `frontend` folder can customize the demo without code changes:

- `demo_scenario_names.txt`: selects which discovered scenarios appear in demo mode, and optionally their display name. Format, one entry per active line: `key` (select, keep the generated title), `key = Title`, or `key = Title | Optional summary`. The `key` is the generated scenario prefix. The file is an allow-list. Missing or empty files show every discovered scenario with its generated fallback title. The same prefix can occur in multiple output folders when identical input data was run with different search settings or as a fixed fleet; every match is shown.
- `demo_kpis.txt`: limits the selectable KPIs on the demo output page. Format: one KPI key per active line.
- `demo_chart_exclusions.txt`: marks specific KPI keys as ineligible for the chart's Y-axis in demo mode. They can still appear in the KPI table. Same one-key-per-line format.

All three files support `#` comments. If a file is missing, empty, or contains only comments, the dashboard keeps the default behavior.

### Output Folder Naming

Without `FLEETPLANNING_SETUP_NAME` set, a job's output folder is `<label>_<8-hex-char hash>`,
e.g. `static_a1b2c3d4` for a search job (`label` is its search mode) or `fixedfleet_a1b2c3d4`
for a fixed-fleet job. `_build_sim_folder_name()` in `app.py` computes the hash via
`hashlib.sha256` (not Python's built-in `hash()`, which is randomized per process via
`PYTHONHASHSEED`) over the job's settings signature (`_job_full_signature()`). The same signature
always produces the same hash, in any process, on any machine.

The signature contains every search and cost-model setting that changes what gets simulated, and
`inputContentHash`, a hash of the BaseData and RideData payloads exactly as they are written as solver
input (`_input_content_hash()`). Editing the loaded data in the dashboard, for example moving a station
or deleting a request, changes the signature. The prefix tokens do not enter it.

A submission that matches a job in the job list opens that job's output when it is completed, restarts
it when it failed, and reports that it is running otherwise. A submission that matches no job in the
list but whose output folder exists on disk, for example after the job was deleted from the list, shows
a warning first, because the run writes into that folder and can mix its files with the existing ones.
Pressing "Simulieren" again on that page starts the run. With `FLEETPLANNING_SETUP_NAME` set, all jobs
share one folder, so every job after the first shows this warning.

`folderName` is computed once at job creation and stored in that job's `simulation_metadata.json`. It is
not recomputed for completed jobs. If the signature's field composition changes, a new job with the same
logical settings as an existing one can hash differently and land in a different folder. Existing output
is not touched by this.

### Input And Output Ownership

For normal solver runs, the frontend writes schema-compliant input files into the selected backend output folder and starts the solver. The current integration assumption is that the simulation/FleetPlanning backend owns that output folder and writes schema-compliant output files there as well.

`folderName` reaches the solver process via `--custom_sim_experiment_config`'s `experiment_output.config_folder` field, for both `custom` and `rw` backend jobs (see [`rw/README.md`](../rw/README.md)).

The frontend does not copy native solver output artifacts into the backend output root. It only reads them once they are present.

Frontend-owned runtime state is stored separately:

```text
frontend/sim_results/job_counters.json
frontend/sim_results/dashboard_jobs/<job-id>/
```

`job_counters.json` holds `nextId`, `deletedCount` and `interruptedCount`. A job receives its id
(`job-<N>`) from `nextId` when it is created, including a job created by importing solver output, and
the id is stored in its `simulation_metadata.json` as `id`. An id is issued once. `nextId` is kept above
every existing `job-<N>` folder and every job in the list, so a lost or reset counter file cannot hand
out an id that is in use.

A job folder contains dashboard metadata and logs such as:

- `<prefix>_simulation_metadata.json`
- `solver_stdout.log`
- `solver_stderr.log`
- `solver_solution_summary.json`, when available
- cached movement animations, one per fleet size

Deleting a job stops its solver process if one is running, removes its movement animation files and sets
`status` in its metadata to `deleted`. The metadata, logs and summary stay in the folder, so the id stays
reserved. Startup recovery loads only completed jobs, so a deleted job does not return. Backend
simulation outputs are not deleted. A deleted completed job counts in `deletedCount`, any other deleted
job in `interruptedCount`.

Startup recovery reads each job's `id` from its metadata. A metadata file without an `id` takes the
number of the folder it is in when no other file holds it, otherwise a new id, and the id is written into
the file. A missing `job_counters.json` is created above the highest existing folder number.

The dashboard compares the `job-<N>` folders with the ids issued, `job-1` to `job-<nextId - 1>`. When a
folder was added or removed by hand, `/simulation-overview` and `/output` show a dismissible warning.

### Input Data

The dashboard expects API-shaped input data and normalizes it internally. The most important source structures are:

- `cabSchedules`
- `chargingPoints`
- `proSchedules`
- `chainingLocations`
- `chainRoutes`
- `chainRouteSchedules`
- `simulationSteps` or `rideRequests`
- `operationArea`

When data is exported again, `export_utils.py` merges edited records back into the original API-shaped payload so fields not edited by the dashboard are preserved where possible.

### Simulation Metadata

`simulation_metadata.json` stores dashboard-side settings and run status, not the simulation ground truth. A search-based job's file has, among others:

```json
{
  "id": "job-3",
  "inputContentHash": "a1b2c3d4e5f6",
  "scenarioName": "ExampleScenario",
  "algorithm": "custom",
  "runKind": "search",
  "searchMode": "adaptive",
  "iterLimit": 50,
  "timeLimit": 3600,
  "initialCabs": 1,
  "cabAddStep": 1,
  "stagnationTolerance": 0.0,
  "stagnationTolerancePatience": 1,
  "objectiveWeight": 1.0,
  "objectiveTerms": ["total_served", "avg_cost_per_trip_eur"],
  "budgetEur": null,
  "serviceLevelMin": null,
  "cabPrice": 100000.0,
  "proPrice": 250000.0,
  "folderName": "adaptive_a1b2c3d4",
  "status": "completed",
  "createdAt": "2026-06-02T10:00:00+00:00",
  "startedAt": "2026-06-02T10:01:00+00:00",
  "completedAt": "2026-06-02T10:05:00+00:00",
  "failedAt": null,
  "solver": "custom",
  "solverReturnCode": 0,
  "solverError": null
}
```

`id` is the job's permanent id and `inputContentHash` the input part of its signature (see [Output Folder Naming](#output-folder-naming)); an imported job carries its scenario prefix there. `status` is one of `queued`, `running`, `completed`, `failed` and `deleted`.

`objectiveTerms` is one metric per perspective group (customer, operator), not two separate named fields. A few more fields exist beyond this illustrative set (the rest of the cost model, `earlyUnchainingEnabled`, `useOriginalProTimetable`, `shrinkPros*` trial settings, a nested `custom_sim` block). See `_build_sim_metadata_payload()` in [`app.py`](app.py) for the exact current shape.

In normal mode, this file can be uploaded on `/simulation-overview` to restore editable scenario settings. In demo mode, upload actions are disabled and cost assumptions are passed through the URL for the current result view.

### Local File Storage

The dashboard writes runtime data to local folders:

- `frontend/uploads_temp`
  - Temporary upload workspace, frontend-owned (not repositioned by `FLEETPLANNING_OUTPUT_ROOT` or any other override).
  - Stores `operation_area.json`, which output-detail routing uses by default.
- `frontend/sim_results/job_counters.json`
  - Frontend-owned job id counter. Same non-repositionable status as `uploads_temp` above.
- `frontend/sim_results/dashboard_jobs`
  - Frontend-owned dashboard job metadata, logs, and summaries. Same non-repositionable status as `uploads_temp` above.
  - Also holds each fleet size's cached movement-animation HTML file once generated, one per fleet size.
- `custom_sim/output/<setup-name>`
  - Native custom-simulation input/output root.
- `rw/output/<setup-name>`
  - Native RW solver input/output root.
- `sumo/output/<setup-name>`
  - The schema-compliant input/output files the dashboard reads. SUMO's own per-run artifacts
    (event logs, tripinfos, console output, KPI files) stay under `sumo/output_<area>/`, shared
    across every request run against that area, not repositioned by this setting.

Demo data should be placed in one of the backend output roots using the schema described above. Because JSON files may be ignored by `.gitignore`, demo fixtures such as `custom_sim/output/.../*.json` and `frontend/uploads_temp/operation_area.json` may need to be added with `git add -f`.

---

## Backends

### Simulation Backends

The simulation overview supports three UI solver values:

- `custom`
- `rwapi`
- `sumo`

Internally, `solver_runner.py` translates these values into the solver command-line backend token and launches the solver in a background thread through `app.py`.

If a selected backend is not fully configured, the dashboard writes the error into the frontend-owned job metadata and solver log files so it can be inspected from `frontend/sim_results/dashboard_jobs/<job-id>`.

### XML Preprocessing

The `xml-pre-processing` subfolder contains a separate pipeline for converting MATSim/TUDO XML population files into final booking-request JSON.

See [xml-pre-processing/README.md](xml-pre-processing/README.md).

That pipeline is useful when raw XML demand data needs to be transformed before it can be uploaded as ride data.

---

## Deployment

### Auto-Start On Reboot (systemd)

Systemd unit to keep the dashboard running across reboots, no new software needed:

```ini
[Unit]
Description=FleetPlanning dashboard (demo mode)
After=network-online.target
Wants=network-online.target
# Stop retrying after 30 failed starts within 10 minutes.
StartLimitIntervalSec=600
StartLimitBurst=30

[Service]
Type=simple
User=<the user the app should run as>
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=DEMO_MODE=1
ExecStart=/path/to/venv/bin/python /path/to/fleetplanning/frontend/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/fleetplanning-demo.service`, add any other `Environment=` lines needed (`DASHBOARD_PASSWORD`, `FLEETPLANNING_OUTPUT_ROOT`, etc.), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fleetplanning-demo
```

`AmbientCapabilities=CAP_NET_BIND_SERVICE` lets a non-root user bind port 80. `network-online.target` (not just `network.target`) waits for a real network connection before starting.

This exposes Flask's development server directly. For a production deployment, run the application
through a production WSGI server and place a reverse proxy in front of it if required.

Setting `DEMO_MODE` in the unit file itself (not left to whoever starts the process) guarantees the publicly reachable instance is always read-only. See [Demo Mode](#demo-mode) under "Usage."

#### Local Tile Server

The optional local tile server (see [maptiles/README.md](maptiles/README.md)) needs nothing in the unit above. It is a Docker container with the restart policy `unless-stopped` (see [maptiles/docker-compose.yml](maptiles/docker-compose.yml)), so once [`maptiles/setup.sh`](maptiles/setup.sh) has been run by hand, Docker starts it on every boot, as long as the Docker service is enabled at boot. Docker packages usually enable it during installation; `systemctl is-enabled docker` shows whether it is, and `sudo systemctl enable docker` enables it if not. The dashboard picks it up as soon as it answers. In `local` mode the dashboard aborts at startup until then, and `Restart=on-failure` in the unit retries. The retries are capped: with `StartLimitIntervalSec` and `StartLimitBurst`, systemd gives up after 30 failed starts within 10 minutes, roughly 5 minutes of trying with the 5-second pause between attempts, and the unit stays in the `failed` state. Only `local` mode can end up there because of the tile server, since `fallback` and `web` start without it. After fixing the tile server, clear the state and start the dashboard again:

```bash
sudo systemctl reset-failed fleetplanning-demo
sudo systemctl start fleetplanning-demo
```

Add `Environment=TILE_SOURCE=local` next to `DEMO_MODE=1` to serve the maps from the local tile server. The default `web` never contacts it, and `fallback` also uses the public OSM servers, see `TILE_SOURCE` under [Environment Variables](#environment-variables) above.

Alternatively, Docker does not have to start at boot on its own. Disable it, and let the dashboard's unit start Docker and the tile server:

```bash
sudo systemctl disable docker docker.socket
```

Docker then starts only when the dashboard does. In that case, use this unit:

```ini
[Unit]
Description=FleetPlanning dashboard (demo mode)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service
# Stop retrying after 30 failed starts within 10 minutes.
StartLimitIntervalSec=600
StartLimitBurst=30

[Service]
Type=simple
User=<the user the app should run as>
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=DEMO_MODE=1
ExecStartPre=/usr/bin/docker compose -f /path/to/fleetplanning/frontend/maptiles/docker-compose.yml up -d tile-server
ExecStart=/path/to/venv/bin/python /path/to/fleetplanning/frontend/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`Requires=docker.service` makes systemd start Docker (which pulls in its socket) before the dashboard, and `ExecStartPre` starts the tile server before Flask does, also when it was stopped by hand. `ExecStartPre` only starts the existing container from its already imported volumes, it does not repeat the OSM import or the tile pre-rendering. The dashboard then depends on Docker and does not start when Docker fails. Because `User=` applies to every `Exec*=` line in the unit, that user also needs permission to talk to the Docker daemon, typically membership in the `docker` group, which is root-equivalent access to the machine. After changing the unit, run `sudo systemctl daemon-reload` and restart it.

---

## Reference

### Development Notes

- The dashboard does not currently use a database.
- Active normal-mode data is stored in the global `loaded_data` object while the Flask process is running.
- Restarting the Flask app clears uploaded/edited input data. Completed simulation jobs are rebuilt at startup from the `simulation_metadata.json` files in `dashboard_jobs`, keeping the ids stored in them.
- Demo-mode request handling uses isolated state loaded from prepared files and does not mutate `loaded_data` for scenario selection.
- Startup may create expected runtime directories if they do not exist.
- Simulation outputs remain on disk in backend-owned output folders.
- The dashboard uses Flask/Jinja for HTML rendering, Bootstrap for layout, Leaflet for maps, and Plotly for charts.
- Several UI labels are German, but this README and comments in the code are written in English as project documentation.

#### Generating A Movement Animation Without The Dashboard

`movement_visualization.build_movement_html(cab_runs, pro_runs, base_data)` works standalone, given the same `output_cab`/`output_pro`/`input_base_file` files the dashboard reads. Without an operation area, one gets derived automatically from a rough bounding box around the output data's own coordinates; the `ROUTER_AREA_FILE` block below (see also [Environment Variables](#environment-variables) above) sets up the real one from `BaseData` instead, for a precise boundary rather than an approximate one:

```python
import sys, os, json, tempfile
sys.path.insert(0, 'frontend')
os.environ['ROUTER_AREA_FILE'] = os.path.join(tempfile.gettempdir(), 'movement_viz_operation_area.json')
import output_details_utils as odu
import movement_visualization as mv

cab_runs = json.load(open('<...>_output_cab_<N>.json'))
pro_runs = json.load(open('<...>_output_pro_<N>.json'))
base_data = json.load(open('<...>_input_base_file_<N>.json'))

border = base_data['operationAreas'][0]['LocationBorder']
points = [{'lat': p['Latitude'], 'lng': p['Longitude']} for p in border]
json.dump({'points': points}, open(os.environ['ROUTER_AREA_FILE'], 'w'))
odu.clear_route_caches()

open('out.html', 'w', encoding='utf-8').write(mv.build_movement_html(cab_runs, pro_runs, base_data))
```

### Third-Party Assets

The dashboard serves self-hosted copies of these libraries from `frontend/static/vendor/`, so that the
pages work without internet access. The license of each library applies to its files. Their texts are in
`frontend/static/vendor/LICENSES.txt`.

| Library | Version | License | Source |
|---------|---------|---------|--------|
| Bootstrap | 5.3.0 | MIT | [getbootstrap.com](https://getbootstrap.com/) |
| Leaflet | 1.9.4 | BSD-2-Clause | [leafletjs.com](https://leafletjs.com/) |
| Leaflet.GeometryUtil | 0.9.3 | BSD-3-Clause | [makinacorpus/Leaflet.GeometryUtil](https://github.com/makinacorpus/Leaflet.GeometryUtil) |
| leaflet-color-markers | not versioned | BSD-2-Clause | [pointhi/leaflet-color-markers](https://github.com/pointhi/leaflet-color-markers) |
| Plotly.js | 2.33.0 | MIT | [plotly/plotly.js](https://github.com/plotly/plotly.js) |
| Material Icons | as served by Google Fonts | Apache License 2.0 | [Material Icons guide](https://developers.google.com/fonts/docs/material_icons) |

### Troubleshooting

#### Upload Button Does Not Change The Page

Check the browser console and Flask terminal output. Invalid JSON, missing expected fields, or unsupported XML structure can prevent normalization.

#### Demo Scenarios Are Not Listed

Check that `DEMO_MODE=1` is set and that the prepared files are under the configured output root:

```text
<FLEETPLANNING_OUTPUT_ROOT>/custom_sim/output/<setup-name>/
```

or the corresponding `rw/output` or `sumo/output` folder.

Each demo scenario needs schema-compliant output files such as:

```text
pb_3cs_2lines_10bc_11cp_1157rq_120tw_output_cab_0.json
pb_3cs_2lines_10bc_11cp_1157rq_120tw_output_sim_0.json
pb_3cs_2lines_10bc_11cp_1157rq_120tw_output_pro_0.json
```

Matching input files are needed for the map preview:

```text
pb_3cs_2lines_10bc_11cp_1157rq_120tw_input_base_file_0.json
pb_3cs_2lines_10bc_11cp_1157rq_120tw_input_req_file_0.json
```

#### Simulation Cannot Start

The simulation overview requires:

- Loaded base data
- Loaded ride data
- A valid operation area with at least three points

If the selected solver backend is not configured, the job may fail during preflight and write an error to the dashboard metadata and `solver_stderr.log`.

#### Warning About The dashboard_jobs Folders

`/simulation-overview` and `/output` show a dismissible warning when the `job-<N>` folders in
`frontend/sim_results/dashboard_jobs` differ from `job-1` to `job-<nextId - 1>` in `job_counters.json`.
The cause is a job folder that was deleted or added by hand. Recreate a missing folder (an empty folder
is enough) or remove the extra one. A missing counter file is recreated at the next start.

#### Warning That The Output Folder Already Exists

A run whose output folder exists but belongs to no job in the list shows a warning before it starts. The
run writes into that folder and can mix its files with the existing ones. Pressing "Simulieren" again on
the same page starts the run. Removing the old output folder beforehand avoids the mixing.

#### No Output Charts Are Shown

Make sure the output folder contains files matching the schema:

- `<prefix>_output_cab_<iteration>.json`
- `<prefix>_output_pro_<iteration>.json`
- `<prefix>_output_sim_<iteration>.json`

In normal mode, use the upload button on `/output` to import the folder or selected files, or open a completed simulation from `/simulation-overview`. In demo mode, select one of the prepared scenarios.

#### Output Detail Routes Are Straight Lines

The overview map can intentionally draw straight request/chain preview lines. The output-detail route map tries to resolve routed paths through the local custom router first, then OSMnx, then straight lines as fallback.

If detail routes are straight lines unexpectedly, check:

- `frontend/uploads_temp/operation_area.json` exists, or `ROUTER_AREA_FILE` points to a valid operation-area JSON file. If missing, one gets derived automatically from the run's own `output_cab`/`output_pro` coordinates; this only fails when those files also aren't present.
- Routing dependencies are installed in the active virtual environment.
- The app was started from the expected checkout and virtual environment.
- Local routing cache/data under `custom_sim/routing` is available or can be generated.

#### Metadata Import Does Not Fill The Form

The uploaded file must be JSON and should use the expected `simulation_metadata.json` fields, especially:

- `scenarioName`
- `algorithm`
- `cabPrice`
- `proPrice`
- `stationaryKwhPrice`
- `proKwhPrice`

Metadata import is available only in normal mode.

### Minimal Manual Test

Normal mode:

1. Upload a valid `BaseData` file on `/upload`.
2. Upload a valid `RideData` file on `/upload`.
3. Open `/index`.
4. Draw an operation area with at least three points.
5. Open `/simulation-overview`.
6. Save the settings and verify that the configured values remain visible.
7. Start a simulation if the selected backend is available.
8. Check that input files were written under the schema-compliant backend output folder.
9. Open `/output` and verify matching output files are parsed when present.

Demo mode:

1. Start the dashboard with `DEMO_MODE=1`.
2. Open `/upload`.
3. Verify prepared scenarios are listed.
4. Hover or focus different scenario cards and verify the map preview updates.
5. Adjust cost assumptions and click the simulate button.
6. Verify the loading progress appears and then opens `/output`.
7. Select the average customer trip cost KPI to confirm the cost assumptions affect derived result charts.
