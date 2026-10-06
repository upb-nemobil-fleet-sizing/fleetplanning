'''
Run CustomSimulation once on a given instance (BaseData + RideData), with no
FleetPlanning search involved. Every cab and every Pro entry in the BaseData file
becomes its own vehicle - unlike fleet_planning.py's own instance loading, which reads
only the first cab/Pro entry as a repeatable template for a search-driven fleet. The
Pro trip timetable is always taken exactly as recorded in the BaseData file, never
re-derived (build_pro_timetable() is a fleet_planning.py/utils.py concern only).

Writes the same five RW-style output files fleet_planning.py's custom_sim path writes
(see custom_sim/utils_cs.py's write_rw_style_iteration_files), so downstream tooling -
the dashboard included - reads the result the same way regardless of how it was produced.
'''
import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
	sys.path.insert(0, str(PROJECT_ROOT))

from models import (
	Cab, Pro, Request, DemandScenario, VehicleFleet, FleetAndRequests,
	OperationalArea, OperationalVertices, ChargingStation, ChainingLocation,
	ProRoutesAndTrips, ChainRoute, ChainRouteTrip,
)
from utils import read_base_json, read_request_json
from custom_sim.custom_simulation import CustomSimulation


def _normalize_datetimes(obj):
	'''
	Change datetime format to omit "ms" and include the "T" - copy of
	fleet_planning.py's read_initial_data_from_json() helper of the same name, kept
	local so this module has no dependency on fleet_planning.py.
	'''
	if isinstance(obj, dict):
		return {k: _normalize_datetimes(v) for k, v in obj.items()}
	elif isinstance(obj, list):
		return [_normalize_datetimes(item) for item in obj]
	elif isinstance(obj, datetime):
		return obj.replace(microsecond=0)
	elif isinstance(obj, str):
		try:
			dt = datetime.fromisoformat(obj.replace(" ", "T"))
			return dt.replace(microsecond=0).isoformat()
		except ValueError:
			return obj
	else:
		return obj


def build_instance(base_data_path: str, ride_data_path: str):
	'''
	Parse BaseData/RideData JSON into the full instance - every cab and Pro entry in
	the file, not just the first.
	'''
	requests = _normalize_datetimes(read_request_json(ride_data_path))
	base = _normalize_datetimes(read_base_json(base_data_path))

	demand_scenario = DemandScenario([Request(**entry) for entry in requests])

	operation = {
		"_vertex_list": [
			(vertex["_longitude"], vertex["_latitude"])
			for vertex in base["operation_area"][0]["_location_border"]
		],
		"_id": base["operation_area"][0]["_id"],
		"_desc": base["operation_area"][0]["_desc"],
	}
	operations_area = OperationalArea(
		OperationalVertices(**operation),
		[ChargingStation(**entry) for entry in base["charging_points"]],
		[ChainingLocation(**entry) for entry in base["chaining_location"]],
		[],
		ProRoutesAndTrips(
			[ChainRoute(**entry) for entry in base["chain_routes"]],
			# always the schedule as recorded in the file - it is already the resolved
			# timetable this instance was solved/uploaded with, never re-derived here.
			[ChainRouteTrip(**entry) for entry in base["chain_route_schedules"]],
		),
	)

	cabs = [Cab(**entry) for entry in base["cab_schedules"]]
	if not cabs:
		raise ValueError("BaseData contains no cabs to simulate.")
	pros = [Pro(**entry) for entry in base["pro_schedules"]]

	vehicle_fleet = VehicleFleet(
		cabs, pros,
		(cabs[0].init_location_lat, cabs[0].init_location_lon),
		(pros[0].init_location_lat, pros[0].init_location_lon) if pros else None,
	)
	return operations_area, demand_scenario, vehicle_fleet, cabs[0], (pros[0] if pros else None)


def load_custom_sim_overrides(experiment_config_path: str | None) -> dict:
	'''
	Read the "custom_sim" section of a dashboard-style simulation_metadata.json - the
	same section fleet_planning.py's own --custom_sim_experiment_config reads (see
	apply_custom_sim_experiment_config()), e.g. experiment_output.config_folder and
	algorithm.early_unchaining.enabled.
	'''
	if not experiment_config_path:
		return {}
	with open(experiment_config_path, "r", encoding="utf-8") as f:
		config = json.load(f)
	if not isinstance(config, dict):
		raise ValueError("custom_sim experiment config must contain a JSON object")
	return copy.deepcopy(config.get("custom_sim") or {})


def run(base_data_path: str, ride_data_path: str, iteration: int = 0,
		experiment_config_path: str | None = None, verbose: int = 1):
	operations_area, demand_scenario, vehicle_fleet, base_cab, base_pro = build_instance(
		base_data_path, ride_data_path
	)
	solution = FleetAndRequests(0, demand_scenario, vehicle_fleet)

	router_info = {
		"cab": {
			"max_speed_mps": base_cab.max_speed,
			"energy_per_wh_m": base_cab.default_energy_consumption_per_m,
		}
	}
	if base_pro is not None:
		router_info["pro"] = {
			"max_speed_mps": base_pro.max_speed,
			"energy_per_wh_m": base_pro.default_energy_consumption_per_m,
		}

	simulation = CustomSimulation(
		operations_area,
		_router_info=router_info,
		_parameter_overrides=load_custom_sim_overrides(experiment_config_path),
		_verbose=verbose,
	)
	simulation.optimize(solution, iteration)
	return solution


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--base_data", required=True)
	parser.add_argument("--ride_data", required=True)
	parser.add_argument("--iteration", type=int, default=0)
	parser.add_argument("--custom_sim_experiment_config", default=None)
	parser.add_argument("--verbose", action="store_true")
	args = parser.parse_args()

	run(
		args.base_data,
		args.ride_data,
		iteration=args.iteration,
		experiment_config_path=args.custom_sim_experiment_config,
		verbose=1 if args.verbose else 0,
	)


if __name__ == "__main__":
	main()
