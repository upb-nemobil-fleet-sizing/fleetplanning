# RW API Backend

`rw/rw_operations.py`'s `rwAPI` class implements the `rw` operational-planning backend: it sends each
candidate fleet to an external HTTP simulation API (Reisewitz) instead of computing the plan locally, then
converts the response into KPIs on the `FleetAndRequests` object `fleet_planning.py`'s search uses to
evaluate the next fleet move.

## Requirements

Install the backend dependency from this directory:

```bash
python -m pip install -r requirements.txt
```

The only direct third-party dependency is `requests`.

## Usage

Selected via `--use_sim rw` on `fleet_planning.py`. This backend calls an external HTTP API and requires
network access and authorization to it.

`rwAPI`'s constructor also accepts `_base_url` and `_token_url` (both default to the Reisewitz endpoints),
`_timeout` (default 7200 seconds), and `_poll` (seconds between status checks, default 5).

There is no standalone, fixed-fleet-style entry point for this backend; the only way to run it is through
`fleet_planning.py --use_sim rw`, one API call per evaluated fleet.

## Module Layout

Files under this directory:

```text
rw/
|-- README.md           # Documentation
|-- __init__.py         # Package marker
|-- requirements.txt    # requests dependency
|-- rw_operations.py    # rwAPI, RWAPIError, compute_run_scenario_prefix()
`-- output/             # Per-call input/output and cache files. Gitignored
```

## `rwAPI.optimize()`

One call runs one full simulation:

1. Request an OAuth2 access token (client-credentials grant) from the identity provider's token endpoint.
2. `GET /simulations/v1/reset` to clear any existing simulation state.
3. `POST /simulations/v1/init-simulation-basedata` with the base data payload.
4. `POST /simulations/v1/simulate` with the request batch; the response body is a simulation id.
5. Poll `GET /simulations/v1/status/{id}` until its `state` field is `Completed`, or raise `RWAPIError` once
   `timeout` is exceeded.
6. `GET /simulations/v1/result/{id}` for the per-request result. `GET /schedules/v1/cabs` and
   `GET /schedules/v1/pros` for the vehicle schedules; neither of these two takes the simulation id, they
   always return the API's current cab/Pro state.
7. Convert the retrieved JSON into KPIs (see [KPI computation](#kpi-computation)).

All requests except the token request itself carry the access token in an `Authorization: Bearer` header.
On a `401`, `check_status()` fetches a fresh token once and retries that one request.

### Error handling

`RWAPIError` (subclass of `RuntimeError`) is raised when a call cannot be completed: the completion timeout
is exceeded, a cache is required (`--resume_from`) but missing or unreadable, or an HTTP request ultimately
fails. `--rw_retries` controls how many full resubmissions (steps 1-6, not the KPI conversion) are attempted
on `requests.exceptions.RequestException` before giving up and raising `RWAPIError`; any other unexpected
exception is wrapped into `RWAPIError` immediately, without retrying.

## KPI computation

`get_all_kpis()` runs the following for every cab, Pro, and request on the `FleetAndRequests` object
passed to `optimize()`, then sets its `sim_aggregates` from `sim_data`'s `finalReport` and `overview`
sections merged together.

- `get_req_kpis(request, sim_data)`: matches a `stepResults` entry to the request by id. Flags
  `sim_invalid` (no match, more than one match, or the API's own `invalidRequest` flag),
  `sim_system_reject` (the API reports failure, or there is no valid chosen proposal), and
  `sim_custom_reject` (no successful `bookTripResponse`). For a request that was actually booked, it reads
  `estimatedPickupTime`/`estimatedDropoffTime` into `sim_pu_time`/`sim_do_time` and computes
  `sim_wait_time_prop` against the request's own time-window bound.
- `get_cab_kpis(cab, cab_data)`: matches a cab by the trailing digits of its vehicle label, then classifies
  every `tripStops` entry by `stopType`: `Pickup` (empty approach), `Dropoff` (customer transport),
  `Chaining`/`Unchaining` (customer-with-Pro if the stop carries a `tripGuid`, otherwise an empty Pro
  repositioning move), `Charging` (stationary charging, split into access distance/time and the charging
  event itself). Distance, driving time, service time, and energy accumulate into total/customer/empty/
  charging buckets, written onto ~20 `sim_*` fields on the `Cab` object. Four consistency checks (customer
  distance/time split vs. total, and total distance/time decomposition) print a warning if they disagree by
  more than `1e-6`.
- `get_pro_kpis(pro, pro_data)`: matches a Pro the same way, sums its `chainingStops`' distance, driving
  time, service time, and chained-cab count onto the `Pro` object.

## Caching and resuming

- `--rw_cache {auto,force,off}` (default `auto`): `auto` reuses a matching cached call's output if
  present, `force`/`off` always call the API. `force` and `off` currently behave identically; no code path
  distinguishes them yet.
- `--resume_from N`: for calls with a `log_id` before `N`, require a matching cached output instead of
  calling the API, raising if it is missing; from `N` onward, normal `--rw_cache` behavior applies. For
  restarting a run that already completed some calls against the real API.
- `--rw_retries N` (default 1): see [Error handling](#error-handling) above.

### Output/cache files (`rw/output/`)

Each call writes five files under `rw/output/`, or `rw/output/<config_folder>/` when one is given.
Their names follow `<prefix>_<kind>_<log_id>.json`. The prefix is the same scenario prefix used by
`fleet_planning.py`: area, charging stations, Pro lines, cab battery, charging power, request count,
and time window.

- `input_base_file`, `input_req_file`: the exact payload sent that call.
- `output_cab`, `output_pro`, `output_sim`: the API's raw response.

`auto` cache mode only reuses a call's outputs if both input files exist and their content matches
what this run would send (`_cache_inputs_match`); a changed scenario invalidates the cache automatically
rather than silently reusing stale results. These files are gitignored; nothing under `rw/output/` is meant
to be committed.

The `<config_folder>` subfolder, when present, comes from `--custom_sim_experiment_config`'s
`experiment_output.config_folder` (the same flag and field `custom`-backend runs use); the dashboard sets
this for every backend it launches, `rw` included. Without it, output lands directly under `rw/output/`.
