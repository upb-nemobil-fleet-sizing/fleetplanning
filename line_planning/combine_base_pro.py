#!/usr/bin/python3

'''
Combine the chaining data of a line planning result with an existing BaseData into a complete BaseData.

python -m line_planning.combine_base_pro <chain_file> --base_data <BaseData.json> --requests <requests.json> [--output <file.json>]

Run from the repository root, so that the root modules utils and models can be imported.

The chain file provides the chaining locations, chain routes and chain route schedules. The Pros are
derived from the chain route schedules. Everything else (charging points, cabs, operation area) comes
from the BaseData. The request file is needed because only FleetAndRequests can write a BaseData.
'''

import argparse
import json

from utils import read_base_json, read_request_json

from models import Request, DemandScenario, Cab, Pro, VehicleFleet, ChargingStation, ChainingLocation, ParkingLocation, ChainRoute, ChainRouteTrip, ProRoutesAndTrips, OperationalVertices, OperationalArea, FleetAndRequests


# properties of the standard Pro, as in the Pro of InitSimulationBaseData_pb_3cs_7lines.json
PRO_PROPERTIES = {
	"_energy_capacity": 10000,
	"_initial_energy_capacity": 10000,
	"_max_cabs": 3,
	"_max_speed": 19.444444,
	"_default_energy_consumption_per_m": 0.18,
	"_max_power_supply": 11000,
}


def read_chain_only(_filepath, _cab: dict):
	'''
	Read the chaining data from a line planning result file. A Pro is created for every Pro of the
	chain route schedules, starting at the start location of its first route and operating during
	the schedule of the given cab entry.
	'''
	data = read_base_json(_filepath)
	base_data = {key: data[key] for key in ("chaining_location", "parking_location", "chain_routes", "chain_route_schedules")}

	chain_route_by_id = {route["_id"]: route for route in data["chain_routes"]}
	chain_location_by_id = {loc["_id"]: loc for loc in data["chaining_location"]}

	pro_schedules = []
	added_ids = set()
	for schedule in data["chain_route_schedules"]:
		pro_id = schedule["_pro_schedule"]
		if pro_id in added_ids:
			continue
		added_ids.add(pro_id)

		start_location = chain_location_by_id.get(chain_route_by_id[schedule["_chain_route"]]["_start_location"])
		if start_location is None:
			print(f"Warning: Could not find the start location for {pro_id}")
			continue

		pro_schedules.append({
			"_id": int(pro_id.partition("Pro")[-1]),
			"_init_location_lat": start_location["_start_location_lat"],
			"_init_location_lon": start_location["_start_location_lon"],
			"_schedule_start": _cab["_schedule_start"],
			"_schedule_end": _cab["_schedule_end"],
			**PRO_PROPERTIES,
		})

	base_data["pro_schedules"] = pro_schedules
	return base_data

# ==============================================

def combine_base_and_chain(base_file, chain_file):

	# read old base data file
	cab_base = read_base_json(base_file)

	# read chaining data file
	chain_base = read_chain_only(chain_file, cab_base["cab_schedules"][0])

	# overwrite placeholders in old file
	new_base = cab_base
	for key, val in chain_base.items():
		new_base[key] = val

	# init operational area
	area = new_base["operation_area"][0]
	operation = {
		"_vertex_list": [(vertex["_longitude"], vertex["_latitude"]) for vertex in area["_location_border"]],
		"_id": area["_id"],
		"_desc": area["_desc"],
	}
	charge = new_base["charging_points"]
	chain = new_base["chaining_location"]
	park = []
	chain_route = new_base["chain_routes"]
	chain_route_schedules = new_base["chain_route_schedules"]

	pro_routes_and_trips = ProRoutesAndTrips([ChainRoute(**entry) for entry in chain_route],
											 [ChainRouteTrip(**entry) for entry in chain_route_schedules])

	init_operations = OperationalArea(OperationalVertices(**operation),
										[ChargingStation(**entry) for entry in charge],
										[ChainingLocation(**entry) for entry in chain],
										[ParkingLocation(**entry) for entry in park],
										pro_routes_and_trips)

	# init base vehicles
	cabs = new_base["cab_schedules"]
	init_cab = Cab(**cabs[0])
	pros = new_base["pro_schedules"]
	init_pro = Pro(**pros[0])
	fleet = VehicleFleet([init_cab], [init_pro],
						(init_cab.init_location_lat, init_cab.init_location_lon),
						(init_pro.init_location_lat, init_pro.init_location_lon))

	return init_operations, fleet

# ------------------------------------------------------------------------------

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Combine the chaining data of a line planning result with an existing BaseData into a complete BaseData.")
	parser.add_argument("chain_file", help="line planning result with the chaining locations, chain routes and chain route schedules")
	parser.add_argument("--base_data", required=True, help="BaseData with the charging points, cabs and operation area")
	parser.add_argument("--requests", required=True, help="request file, needed because only FleetAndRequests can write a BaseData")
	parser.add_argument("--output", default="./combined_base_data.json", help="file to write the complete BaseData to")
	args = parser.parse_args()

	requests = read_request_json(args.requests)
	demand = DemandScenario([Request(**entry) for entry in requests])
	operations, fleet = combine_base_and_chain(args.base_data, args.chain_file)

	far = FleetAndRequests(0, demand, fleet)
	final_structure = far.write_api_base_file(operations)

	with open(args.output, "w") as f:
		json.dump(final_structure, f, indent='\t')
