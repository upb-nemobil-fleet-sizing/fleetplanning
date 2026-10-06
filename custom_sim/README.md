# Custom Simulator

Placeholder. Full documentation for the `custom` backend's own logic (`custom_simulation.py`,
`models_cs.py`, `utils_cs.py`, `visualize_movement.py`) is not yet written.

In the meantime:

- [Routing](routing/README.md): the OSM-based routing module this backend builds on.
- [Convoy Candidate Generation](README_CS_CONVOY.MD): the convoy/chaining candidate logic.
- The root [README.md](../README.md) covers how this backend is selected and its place among the other
  two.

## Positions

Every position the simulation routes to or records is a point on the routing network. The router
projects an input coordinate onto the nearest edge (`Router.snap`); the simulation does that once per
place and uses the projected point from then on.

- Charging stations and chaining locations are snapped when the simulation is created. The points
  live in sim-local wrappers (`SimChargingStation`, `SimChainingLocation` in `models_cs.py`). Each
  wrapper holds a pointer to the original object, its own list index and the point or points. The
  original objects stay unchanged, and schedule entries reference the wrapper.
- Cab start positions are snapped when the fleet is created and stored in the sim-local `CabVehicle`.
- A customer's pickup and dropoff are snapped when the request event is processed, the moment the
  request becomes known. The points and the distance from the requested address to each of them
  (`pu_offset_m`, `do_offset_m`) are stored on the sim-local `Customer`.
- The output files keep the requested address as given (for example `requestedStartLocation`). The
  request results log holds the point reached (`pickup_lat`, `pickup_lon`, `dropoff_lat`,
  `dropoff_lon`) and its distance to the requested address (`pickup_offset_m`, `dropoff_offset_m`).

The router returns a zero route only for (nearly) identical input coordinates (see
[Routing](routing/README.md), "Route query"). One snapped point per place makes a move from a place to
itself cost nothing.

## Usage

Selected via `--use_sim custom` on `fleet_planning.py` (the default), for a full search run.

For a single fixed-fleet evaluation instead, no search: `custom_sim/run_instance.py --base_data <file>
--ride_data <file> [--iteration N] [--custom_sim_experiment_config <file.json>] [--verbose]`.

## Requirements

Run from this directory:

```bash
pip install -r requirements.txt
```

Composes `routing/requirements.txt`. `matplotlib` and `plotly` are this module's own direct imports,
used for `create_fleet_movement_gif` and the timeline/SOC/route-comparison plots in `utils_cs.py`.
