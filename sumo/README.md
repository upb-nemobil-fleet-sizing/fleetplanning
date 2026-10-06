# SUMO Simulation

This backend uses [SUMO 1.20](https://eclipse.dev/sumo/) for road-network modeling, routing, and
microscopic traffic simulation. It does not use SUMO's energy model. Instead, it estimates travel
time from a fitted relationship between route distance and average speed. See
[Moving the Simulation to a New Operating Area](#workflow-moving-the-simulation-to-a-new-operating-area).

## Setting Up the Simulation

Install and configure SUMO as described in the [SUMO documentation](https://sumo.dlr.de/docs/index.html).

Install Python 3.12, then create and activate a virtual environment:

```powershell
python -m venv /env/
source env/bin/activate
```

```powershell
source env/bin/activate
```

Install the Python dependencies:

```powershell
pip install -r requirements.txt
```

## Workflow: Moving the Simulation to a New Operating Area

Create or adapt BaseData and request files in the format described by the [root README](../README.md),
then place them in `data/input/`. For the first run, set `self.cab_speed_model = self.linear_model` in
`SumoSimulation`. This conservative model helps produce enough completed trips to fit the operating
area's distance/speed model. Use enough cabs to serve as many requests as possible.

Run the simulation from the repository root:

### Running a Simulation
```powershell
python fleet_planning.py data/input/your_request_file.json --base_data data/input/your_basedata_file.json --use_sim "sumo"
```

The simulation automatically creates two folders: `output_your_basedata_file` and
`network_data_your_basedata_file` (see [Folders](#folders)). After the simulation finishes, use
[`fit_cab_models.py`](validation/fit_cab_models.py) to fit the new operating area's model from the
realized driving times in `output_your_basedata_file/eventlog/eventlog_your_request_file.txt`:

```powershell
python validation/fit_cab_models.py output_your_basedata_file/event_log/
```
The script then produces a json file called `hill_model_params.json`. To integrate the model for your next simulation run set the attribute in the SumoSimulation class back to `self.cab_speed_model = self.parse_cab_speed_approx_model`.

- If the net file of the area is not in `road_network_data/`, it is built from Overpass data. The run stops with an error if Overpass does not return OSM XML, or if the net file build produces no file. The error message names the cause.
- If the net file exists, the build is skipped.
- A chain route trip whose Pro is not part of the current iteration's fleet is not scheduled in that iteration. The timetable covers every Pro slot a line could need, and a fleet usually uses fewer. Skipped trips are not printed.

### Output files for the dashboard
After every iteration the simulation writes the files the dashboard reads, `<prefix>_output_cab_<iteration>.json`, `<prefix>_output_pro_<iteration>.json` and `<prefix>_output_sim_<iteration>.json`, into `output/`. The prefix describes the scenario the way the dashboard names it, e.g. `pb_3cs_2lines_10bc_11cp_166rq_60tw` (area, charging stations, lines, battery kWh, charging power kW, requests, time window minutes).

- Started from the command line, they are written to `output/your_request_file/`. The inputs of every iteration are kept separately, in `output_your_basedata_file/rw_output/your_request_file/` (`<prefix>_input_base_file_<iteration>.json`, `<prefix>_input_req_file_<iteration>.json`).
- Started from the dashboard, the folder name is the dashboard job's own folder name, passed in through `--custom_sim_experiment_config`: `output/<job folder>/`, next to the input files the dashboard put there. The inputs of every iteration are kept in `output_your_basedata_file/rw_output/<job folder>/`.

An iteration whose inputs are the same as those of a stored earlier run is not simulated again, its output files are rebuilt from the stored event log.

### Run status
- Each run writes `<run>_iter_<iteration>_SUCCESS.txt` or `<run>_iter_<iteration>_FAIL.txt` to
  `output_your_basedata_file/console_output/`. `<run>` is the folder name described under
  [Output Files for the Dashboard](#output-files-for-the-dashboard).
- A FAIL file holds the exception and the full traceback.
- A failed run writes no output files and does not store its inputs for a warm start.

In case you want to analyze/visualize the results from your runs you can use the existing script in the `./validation` and `./visualization` directories in the following manner after navigating into the `./sumo` directory: 

### Validation
checks the eventlog for violations of the driving time approximations
```powershell
python validation/check_for_shifts.py output_your_basedata_file/event_log
```
checks the net file for the max speed distribution over all streets
```powershell
python validation/check_max_speed.py road_network_data/your_net_file.net.xml.gz 
```

compares multiple simulation runs characterized by a configuration in form of a basedata
```powershell
python validation/compare_configurations.py output_your_basedata_file
```

fits a model linking distance and average speed based on realised values of a previous run, also outputs a figure comparing model vs realised
```powershell
python validation/fit_cab_models.py output_your_basedata_file/event_log
```

takes as input an eventlog by the sumo simulation and outputs file formats used by the dashboard (the simulation writes these itself, see [Output files for the dashboard](#output-files-for-the-dashboard), this recreates them from an eventlog)
```powershell
python validation/turn_eventlog_into_rw_output.py output_your_basedata_file/event_log/eventlog_your_request_file_iter_0.txt ../data/input/your_base_data_file.json --request_data ../data/input/your_request_file.json --output_dir your_output_dir --prefix your_prefix --iter 0
```
### Visualization 
visualizes spacial and temporal characteristics of rejected reservations
```powershell
python visualization/visualize_rejected_reservations.py --instance ../data/input/your_request_file.json  --eventlog output_your_basedata_file/event_log/eventlog_your_request_file_iter_0.txt --config network_data_your_basedata_file/sumo.sumocfg 
```

visualizes spacial and temporal characteristics of the reservations with short detour distance but not served by platoon, requires adaption for every net file
```powershell
python visualization/visualize_rejected_platoon_reservations.py --instance ../data/input/your_request_file.json  --platoonfile output_pb_3cs_pros_included/platoon_results/platoon_info_your_request_file_iter_0.json --config network_data_your_basedata_file/sumo.sumocfg
```

visualizes spacial and temporal characteristics of the reservation served using a platoon
```powershell
python visualization/visualize_platoon_reservations.py  --instance ../data/input/your_request_file.json  --platoonfile output_your_basedata_file/platoon_results/platoon_info_your_request_file_iter_0.json --config network_data_your_basedata_file/sumo.sumocfg 
```

---

## Repository Structure

```
.
|-- network_data_your_basedata_file/
|-- output/
|-- output_your_basedata_file/
|-- road_network_data/
|-- validation/
|   |-- check_for_shifts.py
|   |-- check_max_speed.py
|   |-- compare_configurations.py
|   |-- fit_cab_models.py
|   `-- turn_eventlog_into_rw_output.py
|-- visualization/
|   |-- fcdReplay.py
|   |-- visualize_platoon_reservations.py
|   |-- visualize_rejected_platoon_reservations.py
|   `-- visualize_rejected_reservations.py
|-- dispatcher.py
|-- hill_model_params.json
|-- __init__.py
|-- models_sumo.py
|-- README.md
|-- runner.py
|-- sumo_simulation.py
|-- sumo_utilities.py
`-- utilities.py
```

### Folders

| Folder | Description |
|--------|-------------|
| `./network_data_your_basedata_file` |automatically created when starting a simulation run, contains files characterizing the traffic network used in the run|
| `./output` |created on the first run that needs it, holds the per-iteration files the dashboard reads, one subfolder per request file or dashboard job (see [Output files for the dashboard](#output-files-for-the-dashboard))|
| `./output_your_basedata_file` |automatically created when starting a simulation run, contains the run's own event logs, tripinfos, console output and KPI files, shared across every request run against that area|
| `./road_network_data` |archive of available net files which do not have be create (which is done automatically if needed) for the run|
| `./validation` |directory containing scripts for validation of a simulation run|
| `./visualization` |directory containing scripts visualization of a simulation run |

`road_network_data/pb.net.xml.gz` is the net file for the pb area, included so the example
scenario (`data/input/example_basedata.json`, `data/input/example_demand.json`) can be run
through this backend without access to the public Overpass API, the source a net file build
pulls road data from.

### Files

| File | Description |
|------|-------------|
| `dispatcher.py` |dispatches the cab based on the `cab_schedule_management` using the traci interface with `cab_schedule_communication`|
| `hill_model_params.json` |contains parameters for the model which links route length to average speed in the simulation, produced by `fit_cab_models.py`|
| `models_sumo.py` | contains the important data structures: Pro, Taxi, Reservation and ScheduleEntry|
| `runner.py` |contains the main simulation loop and creates the interface to sumo using traci|
| `sumo_simulation.py` | bundles important information and methods for the sumo simulation, interfaces the simulation to fleeplanning.py|
| `sumo_utilities.py` |contains sumo helper functions for the simulation (e.g. edge snapping, route setting)|
| `utilities.py` |contains helper functions unrelated to sumo (e.g. convert datetime to simulation time)|
| `check_for_shifts.py` |checks the eventlog for violations of the driving time approximations|
| `check_max_speed.py` |checks the net file for the max speed distribution over all streets|
| `compare_configurations.py` |compares multiple simulation runs characterized by a configuration in form of a basedata|
| `fit_cab_models.py` |fits a model linking distance and average speed based on realised values of a previous run, also outputs a figure comparing model vs realised|
| `turn_eventlog_into_rw_output.py` | takes as input an eventlog by the sumo simulation and outputs file formats used by the dashboard |
| `fcdReplay.py` |takes in a fcd file (creation has to be enabled in runner.py as command line argument) and produces a visual recreation of the simulation|
| `visualize_platoon_reservations.py` |visualizes spacial and temporal characteristics of the reservation served using a platoon|
| `visualize_rejected_platoon_reservations.py` |visualizes spacial and temporal characteristics of the reservations with short detour distance but not served by platoon, requires adaption for every net file|
| `visualize_rejected_reservations.py` |visualizes spacial and temporal characteristics of rejected reservations|

---

## Sources

- [OpenStreetMap](https://www.openstreetmap.org/copyright): the road network data a new operating
  area's net file is built from, queried through the public Overpass API, from the OpenStreetMap
  contributors and available under the Open Database License (ODbL).
- [Eclipse SUMO](https://eclipse.dev/sumo/) (EPL-2.0 OR GPL-2.0-or-later): `road_network_data/osmBuild.py`
  is SUMO's own tool script, with one local addition: a `nemo` vehicle-class entry that keeps only
  this project's taxi and bus edges when building the net file. Its original license header is
  unchanged at the top of the file.
