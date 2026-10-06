# NeMo.bil Strategic Planning

This repository contains a heuristic fleet-sizing search developed within the
[NeMo.bil](https://nemo-bil.de/) research project. Three interchangeable operational-planning backends
evaluate candidate fleets, and a Flask dashboard provides the user interface. This document covers the
search core. One further folder, `line_planning/`, is a separate tool that is not part of the search
(see [Project Structure](#project-structure)).

**Documentation:** [Dashboard](frontend/README.md) | [Custom Simulator](custom_sim/README.md) |
[Custom Simulator Routing](custom_sim/routing/README.md) |
[Convoy Candidate Generation](custom_sim/README_CS_CONVOY.MD) | [RW API Backend](rw/README.md) |
[SUMO Backend](sumo/README.md) | [Line Planning](line_planning/README.md) |
[XML Preprocessing](frontend/xml-pre-processing/README.md) |
[Local Tile Server](frontend/maptiles/README.md)

Start with the document that matches the task:

| Task | Documentation |
|------|---------------|
| Run or understand the fleet-sizing search | This README |
| Prepare and run scenarios in a browser | [Dashboard](frontend/README.md) |
| Create Pro lines from demand data | [Line Planning](line_planning/README.md) |
| Understand or modify road-network routing | [Custom Simulator Routing](custom_sim/routing/README.md) |
| Convert MATSim population XML to RideData | [XML Preprocessing](frontend/xml-pre-processing/README.md) |
| Operate the offline map service | [Local Tile Server](frontend/maptiles/README.md) |

---

## Fleet Planning

The optimizer searches for a fleet configuration: the number of Cab and Pro vehicles and the active Pro
lines. It evaluates each candidate against a fixed demand scenario and infrastructure through the selected
operational-planning backend. Static search stops when fleet growth stagnates. Adaptive search then also
attempts to reduce the Cab and Pro fleet. Both modes stop when the iteration or time limit is reached.

### Table of Contents

*   [Setup](#setup)
*   [Usage](#usage)
*   [The Search Algorithm](#the-search-algorithm)
*   [Data & Files](#data--files)
*   [Backends](#backends)
*   [Acknowledgment](#acknowledgment)

---

## Setup

Install the dependencies, run a first search, and get oriented in the repo layout.

### Requirements

Developed and run with Python 3.12; the pinned versions need Python 3.11 or newer. Install the
dependencies before running anything else.

```bash
python -m pip install -r requirements.txt
```

Installs all three backends, composed via `-r` from `custom_sim/requirements.txt` (itself from
`custom_sim/routing/requirements.txt`), `rw/requirements.txt`, and `sumo/requirements.txt`. Each file
lists only its own module's direct imports.

A separate file covers the dashboard (`frontend/`), an alternative to calling `fleet_planning.py`
directly: it launches the same search from a web UI, in the background. See [frontend/README.md](frontend/README.md) for
how it works.

```bash
python -m pip install -r frontend/requirements.txt
```

Composes `custom_sim/routing/requirements.txt`. `rw`/`sumo` need their own requirements files installed
separately. Also covers `frontend/xml-pre-processing/`, which has no requirements file of its own.

`sumo` additionally needs `SUMO_HOME` and a working SUMO install (`sumolib`/`traci` come from there, not
pip). `rw` needs network access and authorization to its external API.

### Quick Start

The `custom` backend needs no external service or credentials. `data/input/` has an example demand file
and a matching BaseData file:

```bash
python fleet_planning.py data/input/example_demand.json --base_data data/input/example_basedata.json --use_sim custom --verbose
```

The search prints per-iteration and final fleet KPIs to the console and ends with a summary.

### Project Structure

Top-level layout, backend folders and the dashboard each own their part of the pipeline:

```
.
|-- fleet_planning.py       # search: FleetPlanning class, CLI entry point (main())
|-- models.py               # shared domain model: requests, vehicles, infrastructure, evaluated solutions
|-- utils.py                # input JSON parsing, Pro timetable generation
|-- custom_sim/             # "custom" backend: discrete-event simulator + OSM routing + movement viz
|   `-- routing/            # OSM-based routing module, own README.md
|-- rw/                     # "rw" backend: client for the external Reisewitz simulation API
|-- sumo/                   # "sumo" backend: SUMO-based simulator, validation and replay tooling
|-- frontend/               # Flask dashboard: upload data, launch runs, inspect results
|-- line_planning/          # determines Pro lines from request data and writes the BaseData input of the fleet planning
|-- data/                   # input scenarios (demand JSON, BaseData JSON)
`-- tests/                  # pytest suites for the search in fleet_planning.py
```

`line_planning/` is separate from the fleet-planning search and has its own requirements file. See its
[README](line_planning/README.md).

Each subfolder with enough of its own logic to warrant it has its own `README.md`, linked above.

---

## Usage

The general invocation shape, with individual flags detailed in the table below:

```bash
python fleet_planning.py <demand.json> --base_data <BaseData.json> --use_sim {custom,rw,sumo}
```

| Flag | Purpose |
|------|---------|
| `--use_sim {custom,rw,sumo}` | Backend to evaluate candidates with. Default `custom`. |
| `--iter_limit`, `--time_limit` | Stop the search after N iterations or a wall-clock budget, whichever first. |
| `--initial_cabs`, `--cab_add_step` | Starting fleet size and growth-step size overrides. |
| `--search_mode {static,adaptive}` | See [The Search Algorithm](#the-search-algorithm). |
| `--objective_terms`, `--objective_weight` | Select and blend the search objective. |
| `--budget_eur`, `--service_level_min` | Optional hard constraints. |
| `--shrink_pros_allow_retry`, `--shrink_pros_max_tries`, `--no_shrink_pros_trial_probe_below_start`, `--shrink_pros_trial_overshoot_correction` | Tune the `adaptive`-mode Pro/cab trade-off trial mechanism. |
| `--stagnation_tolerance`, `--stagnation_tolerance_patience` | Let a step miss the reference score by a small margin and still count as continuing, instead of stopping instantly. |
| `--use_original_pro_timetable` | Use the uploaded Pro schedule verbatim instead of regenerating a routed one; may be operationally infeasible, since it's never checked against real routing times. |
| `--custom_sim_experiment_config <file.json>` | JSON overrides for `custom`-backend parameters, plus an optional cost-model override that applies regardless of backend. Its `experiment_output.config_folder` also sets where `rw`-backend dashboard jobs write output. |
| `--rw_cache {auto,force,off}`, `--resume_from`, `--rw_retries` | `rw` backend only: reuse cached per-iteration API output instead of re-calling it. |

Full flag list and defaults: `python fleet_planning.py --help`, or `fleet_planning.py:main`.

---

## The Search Algorithm

`FleetPlanning` (`fleet_planning.py`) is the class implementing this search. Each instance holds a
pool of evaluated candidates (`solution_pool`, each a `FleetAndRequests`: one fleet plus the resulting
request assignments and KPIs), and repeatedly:

1. picks a **move** based on the current search **phase**,
2. applies it to get a candidate fleet,
3. hands the candidate to whichever simulator backend is selected to compute an operational plan and
   its resulting KPIs,
4. ranks the result and keeps searching until `--iter_limit`/`--time_limit` is hit or growth stagnates.

Already-simulated fleet compositions are cached (`visited_fleets`, keyed by cab count plus the exact
set of active Pro ids), so revisiting one doesn't re-run the backend.

Each phase keeps applying its move while the result improves, subject to the configured stagnation
tolerance, and then switches phase or stops.

### Phases

Controlled by `--search_mode {static,adaptive}`:

- **`grow`**: adds cabs. `static` mode adds a fixed number per step (`--cab_add_step`); `adaptive`
  mode sizes the step with `estimate_cabs_for_requests()` instead, extrapolating the fleet's current
  requests-served-per-cab rate to the number of cabs needed for full demand coverage, so a fleet far
  from capacity can jump by more than one cab in a single step.
- **`shrink`** (`adaptive` mode only, entered once `grow` stagnates): removes one cab at a time while
  the objective still improves.
- **`shrink_pros`** (`adaptive` mode only, entered once `shrink` stagnates): deactivates the
  least-used Pro, then tries to recover the fleet's serving capacity with cabs alone before deciding
  whether the trade-off was worth it, see [The `shrink_pros` trial](#the-shrink_pros-trial) below. This
  phase depends on Pro timetables being regenerated from routed line templates rather than fixed (see
  [Data & Files](#data--files)); a manually authored timetable would need to already cover every
  reachable subset of active Pros.

`static` mode can be summarized as follows:

| Aspect | Behavior |
|--------|----------|
| Pro fleet | Fixed for the entire run |
| Cab fleet | Grows by `--cab_add_step` each iteration |
| Stop condition | The objective stops improving, or a grown fleet contains an unused cab |
| Result | The best fleet found; rejected candidates remain visible in `solution_pool` |
| Main use | Read any KPI as a curve over increasing cab counts and identify plateaus for one Pro configuration |

**Stagnation tolerance** handles a non-smooth objective, where one small dip may occur before a later
improvement:

| Setting | Effect |
|---------|--------|
| `--stagnation_tolerance` | Maximum shortfall that still allows the phase to continue |
| `--stagnation_tolerance_patience` | Number of consecutive tolerated steps allowed before stopping |

The tolerance applies to the top-level `grow` and `shrink` phases and to the `shrink_pros` trial.

### The `shrink_pros` trial

A bare Pro removal is a coarse trade-off: each Pro carries several cabs' worth of chaining benefit
removed in one step. An optional nested trial evaluates it more carefully:

- reruns `grow`/`shrink` moves right after the removal, trying to recover as much serving capacity as
  cabs alone can provide,
- succeeds if the best rank it reaches, counting the removal itself, beats the rank from before the
  removal,
- ranks its own steps against that local peak (`trial_bubble_rank`), not the search's global best,
  using the same rank comparison used everywhere else: `_solution_rank()` returns
  `(is_feasible, score)`, so a feasible candidate always outranks an infeasible one regardless of
  score, and two solutions of equal feasibility compare by score.

Three further knobs adjust the trial:

- **Retry** (`--shrink_pros_allow_retry`, off by default): whether a Pro that already failed its trial
  becomes eligible again later in the same `shrink_pros` round, once a different trial succeeds.
- **Recovery-below-start probe** (`--no_shrink_pros_trial_probe_below_start` to disable, on by
  default): if recovery found nothing meaningful, also check whether the removed Pro was actively
  hurting the schedule by trying fewer cabs than the fleet had when the trial started.
- **Overshoot correction** (`--shrink_pros_trial_overshoot_correction` to enable, off by default): if
  recovery reached its peak via a single jump of more than one cab, walk part of that jump back down
  to check the full jump was actually necessary.

`--shrink_pros_max_tries` bounds how many distinct Pros are tried per `shrink_pros` round, since each
trial is a genuinely expensive nested search.

**Objective:** `--objective_terms` selects one or two of these metrics:

- `total_served`
- `avg_cost_per_trip_eur`
- `avg_profit_per_trip_eur`
- `avg_in_vehicle_time_s`

With two metrics, `--objective_weight` (alpha) blends them. Cost and profit use the instance's
`CostModel` (see [`models.py`](models.py)). The default alpha of `1.0` uses only the first metric. The
default objective therefore maximizes served requests, using `1 / (cabs + pros + 1)` as a small,
unweighted preference for fewer vehicles when breaking ties.

**Hard constraints** are independent of the objective:

| Setting | Constraint | When checked |
|---------|------------|--------------|
| `--budget_eur` | Maximum raw fleet purchase price | During growth; `adaptive` may shrink back under it, while `static` stops |
| `--service_level_min` | Minimum served/valid request ratio | During shrinking, so it affects only `adaptive` mode |

---

## Data & Files

**Input** (`--base_data` and the positional demand file): JSON in the same shape the `rw` API itself
uses (`utils.py`'s `read_request_json`/`read_base_json` parse it directly, field names like
`requestParameter`, `CurrentLocation`, `pickupTime` are the API's own).

`data/input/example_basedata.json`, `data/input/example_plans.xml` and `data/input/example_demand.json`
form a small example for the Quick Start and the dashboard. The BaseData has one cab, one Pro, one charging
station and one Pro line between `Station1` and `Station2`. The XML is a [MATSim](https://www.matsim.org/)
population extract of 14 real agents that the dashboard converts to 24 ride requests; `example_demand.json`
is that conversion.
Upload the BaseData and the XML on the dashboard's upload page.

**Domain model** (`models.py`), shared by every backend and `frontend`:

- `Request` / `DemandScenario`: customer demand.
- `Cab` / `Pro` / `VehicleFleet`: vehicles.
- `ChargingStation` / `ChainingLocation` / `ParkingLocation`: infrastructure.
- `ChainRoute` / `ChainRouteTrip` / `ProRoutesAndTrips`: Pro routes and their timetable trips, in the
  same shape as the BaseData/API JSON, from either the uploaded timetable or `build_pro_timetable()`'s
  regenerated one.
- `ProLine` / `ProScheduleSlot` / `ProFleetPlan`: the search's own per-line catalog of activatable Pro
  slots, built from `ProRoutesAndTrips` to support the `shrink_pros` phase.
- `OperationalArea`: geography, infrastructure, and Pro plan, bundled.
- `FleetAndRequests`: one evaluated solution (a fleet plus the resulting request assignments/KPIs);
  `write_api_req_file()`/`write_api_base_file()` write it back out in the same API-compatible shape.
- `SearchParameters`: every `fleet_planning.py` CLI flag's runtime counterpart, self-documented in
  its own constructor docstring.
- `CostModel`: vehicle/energy prices, amortization, and the per-trip fare model.

**Pro timetable regeneration** (`utils.py`'s `build_pro_timetable()`, used unless
`--use_original_pro_timetable` is set): the uploaded timetable only supplies one template trip per
line, its first departure and route. From that:

- a routed cycle time is computed per line (outbound leg, turnaround and deadhead, inbound leg,
  turnaround and deadhead again, via the same router `custom_sim` uses),
- the maximum number of alternating-direction departure slots that still respect a 10-minute minimum
  pair spacing are packed into that cycle,
- each Pro's full-day schedule is generated by alternating direction until the fleet's operating
  window closes.

This is what makes an arbitrary active-Pro subset simulatable: the `shrink_pros` phase (see
[The Search Algorithm](#the-search-algorithm)) deactivates individual lines one at a time, and a
fixed, manually authored timetable would need to already cover every reachable subset of active Pros.

**Output**: each backend writes its own schema-compliant output folder
(`custom_sim/output/<setup>`, `rw/output/<setup>`, `sumo/output/<setup>`). A standalone (non-dashboard)
`custom` run also writes a `<prefix>_simulation_metadata.json` with the full cost-model and
search-parameter values used for that run; `rw`/`sumo` standalone runs don't write this file yet.

---

## Backends

Selected via `--use_sim`, each implementing the same `optimize(solution, iteration, ...)` interface so
`FleetPlanning` doesn't need to know which one is active:

- **`custom`** (`custom_sim/`, see its [README](custom_sim/README.md)): in-repo discrete-event
  simulator, no external services required. Own OSM-based routing module, and evaluates Pro-chaining
  as an additional candidate type inside the same insertion search as direct Cab trips.
- **`rw`** (`rw/rw_operations.py`, see its [README](rw/README.md)): calls the external Reisewitz HTTP
  simulation API, with `--rw_cache`/`--resume_from` support for reusing previously-written
  per-iteration files instead of re-calling it.
- **`sumo`** (`sumo/`, see its [README](sumo/README.md)): drives a SUMO traffic simulation; requires
  `SUMO_HOME` and a SUMO install.

Only the selected backend's dependencies are imported (lazily, inside `FleetPlanning.__init__`), so
picking `custom` never requires SUMO or RW API access.

---

## Acknowledgment

Developed in part within the [NeMo.bil](https://nemo-bil.de/) project, funded by the German Federal
Ministry for Economic Affairs and Energy (BMWE, formerly BMWK).

[![NeMo.bil funding acknowledgment banner](frontend/static/images/BMWE2025_NextGenEU_web_small.png)](https://www.bundeswirtschaftsministerium.de/Navigation/DE/Home/home.html)
