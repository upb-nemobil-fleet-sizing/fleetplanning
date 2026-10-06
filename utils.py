#!/usr/bin/python3

import sys
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

import json

# ------------------------------------------------------------------------------

def _parse_iso_dt(value) -> datetime:
	'''
	Parse an ISO datetime string, same missing-timezone-means-UTC convention used
	elsewhere in the repo (see output_details_utils._parse_iso_as_utc).
	'''
	dt = datetime.fromisoformat(str(value))
	return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

# ------------------------------------------------------------------------------

def read_request_json(_filepath: str) -> list[dict]:
	'''
	Read instance data from json file
	'''
	# open file
	with open(_filepath, 'r') as file:
		data = json.load(file)
	
	# get request entries
	simulation_steps = data.get("simulationSteps", [])
	
	# process entries
	entries = []
	for step in simulation_steps:
		new_entry = {}
		# Accessing specific data from each step
		request_params = step.get("requestParameter", {})
		new_entry["_id"] = int(request_params.get("UserGuid", None))
		pu_location = request_params.get("CurrentLocation",{})
		new_entry["_pu_lat"] = pu_location.get("Latitude", None)
		new_entry["_pu_lon"] = pu_location.get("Longitude", None)
		do_location = request_params.get("TargetLocation",{})
		new_entry["_do_lat"] = do_location.get("Latitude", None)
		new_entry["_do_lon"] = do_location.get("Longitude", None)
		new_entry["_register_time"] = _parse_iso_dt(step.get("SimulatedTime", None))
		pu_window = request_params.get("pickupTime",{})
		do_window = request_params.get("targetTime",{})
		if pu_window == None:
			new_entry["_tw_type"] = False
			new_entry["_tw_lower"] = _parse_iso_dt(do_window.get("StartTime", None))
			new_entry["_tw_upper"] = _parse_iso_dt(do_window.get("EndTime", None))
		elif do_window == None:
			new_entry["_tw_type"] = True
			new_entry["_tw_lower"] = _parse_iso_dt(pu_window.get("StartTime", None))
			new_entry["_tw_upper"] = _parse_iso_dt(pu_window.get("EndTime", None))
		else:
			print("Error, both TWs specified")
			sys.exit(-1)
		new_entry["_num_persons"] = request_params.get("requestedAdults", None)
		new_entry["_need_ramp"] = request_params.get("needRamp", None)
		preferences = request_params.get("personalPreferences",{})
		new_entry["_ride_sharing"] = preferences.get("allowCarpooling", None)
		new_entry["_tol_upper"] = preferences.get("toleratedDelayBefore", None)
		new_entry["_tol_lower"] = preferences.get("toleratedDelayAfter", None)
		
		# Add the entire step to the list of entries
		entries.append(new_entry)
	
	return entries

def build_pro_timetable(base_data: dict, pair_spacing_minutes=10, turnaround_minutes=5) -> list[dict]:
	'''
	Build Pro timetable entries from line templates and routed durations.
	Output shape stays identical to the original JSON timetable.
	'''
	from custom_sim.routing.router import Router

	old_trips = list(base_data.get("chain_route_schedules", []))
	routes = list(base_data.get("chain_routes", []))
	# old_trips itself may be empty (or missing entries for some lines) - that's not fatal on
	# its own, see the "uncovered lines" fallback below. Only bail out when there's nothing to
	# build a schedule from at all: no route geometry, or no Pro fleet to derive speed/energy/
	# operating hours from.
	if not routes or not base_data.get("pro_schedules"):
		return old_trips
	if pair_spacing_minutes <= 0:
		raise ValueError("pair_spacing_minutes must be positive")
	if turnaround_minutes is not None and turnaround_minutes <= 0:
		raise ValueError("turnaround_minutes must be positive or None")

	# Old trips are only templates for line starts and first Pro ids.
	chain_locations = {entry["_id"]: entry for entry in base_data.get("chaining_location", [])}
	route_by_id = {route["_id"]: route for route in routes}

	# Build the same Pro router profile that CustomSimulation will later use.
	base_dir = Path(__file__).resolve().parent
	with open(base_dir / "custom_sim" / "routing" / "vehicle_profiles.json", "r", encoding="utf-8") as file:
		profiles = json.load(file)

	pro_template = base_data["pro_schedules"][0]
	profiles["pro"]["max_speed_mps"] = pro_template["_max_speed"]
	profiles["pro"]["energy_per_wh_m"] = pro_template["_default_energy_consumption_per_m"]
	router = Router(
		[
			(vertex["_longitude"], vertex["_latitude"])
			for vertex in base_data["operation_area"][0]["_location_border"]
		],
		profile="pro",
		area_id=base_data["operation_area"][0]["_desc"],
		profiles=profiles,
	)

	# Replace JSON route estimates by routed durations/distances.
	route_metrics = {}
	for route in routes:
		chain_loc = chain_locations[route["_start_location"]]
		unchain_loc = chain_locations[route["_end_location"]]
		result = router.shortest_path(
			(float(chain_loc["_end_location_lat"]), float(chain_loc["_end_location_lon"])),
			(float(unchain_loc["_start_location_lat"]), float(unchain_loc["_start_location_lon"])),
		)
		duration = max(1, int(round(float(result["time_s"]))))
		distance = max(0, int(round(float(result.get("distance_m", 0.0) or 0.0))))
		route["_duration"] = duration
		route["_distance"] = distance
		route_metrics[route["_id"]] = duration

	# Pair directions into bidirectional lines.
	# Chain/Unchain are different points; only the station identity is inferred.
	directed_routes = {}
	for route in routes:
		start_loc = chain_locations[route["_start_location"]]
		end_loc = chain_locations[route["_end_location"]]
		if start_loc["_type"] != "Chain" or end_loc["_type"] != "Unchain":
			raise ValueError(f"Unsupported Pro route direction: {route['_id']}")
		start_base = route["_start_location"].removesuffix("_Chain").removesuffix("_Unchain")
		end_base = route["_end_location"].removesuffix("_Chain").removesuffix("_Unchain")
		directed_routes[(start_base, end_base)] = route

	for (start_base, end_base), route in directed_routes.items():
		if (end_base, start_base) not in directed_routes:
			raise ValueError(f"Unsupported Pro timetable input: route {route['_id']} has no reverse route")

	# Group the old timetable by line. The first old trip per line determines
	# the first generated Pro's direction, start time, and old Pro name.
	trips_by_line = {}
	for trip in old_trips:
		route = route_by_id[trip["_chain_route"]]
		endpoints = tuple(sorted([
			route["_start_location"].removesuffix("_Chain").removesuffix("_Unchain"),
			route["_end_location"].removesuffix("_Chain").removesuffix("_Unchain"),
		]))
		trips_by_line.setdefault(endpoints, []).append(trip)

	old_pro_ids = [
		int(str(trip["_pro_schedule"]).partition("Pro")[-1])
		for trip in old_trips
		if trip.get("_pro_schedule") is not None
	]
	# Keep each line's first old Pro id; generated slots get new ids after that.
	next_pro_id = max(old_pro_ids) + 1 if old_pro_ids else 1

	schedule_start = _parse_iso_dt(base_data["pro_schedules"][0]["_schedule_start"])
	schedule_end = _parse_iso_dt(base_data["pro_schedules"][0]["_schedule_end"])

	# A line (chain route pair) with no uploaded trips at all has no template to seed from and
	# would otherwise be silently dropped - never represented in the generated schedule, even
	# though its route geometry exists. Fall back to the Pro fleet's own operating-hours start
	# (the same schedule_start used as the window bound above) as a synthetic first departure
	# instead, so every configured line gets generated coverage. Deterministic: routes are
	# visited in sorted (start_base, end_base) order, and only the lexicographically first
	# direction per uncovered line seeds it - the other direction is filled in symmetrically by
	# the per-line generation below either way.
	for (start_base, end_base), route in sorted(directed_routes.items()):
		endpoints = tuple(sorted([start_base, end_base]))
		if endpoints in trips_by_line:
			continue
		trips_by_line[endpoints] = [{
			"_pro_schedule": f"Pro{next_pro_id}",
			"_chain_route": route["_id"],
			"_departure": schedule_start.isoformat(),
		}]
		next_pro_id += 1

	generated_trips = []
	line_items = []
	for endpoints, line_trips in trips_by_line.items():
		# sorted(..., key=...) only ever compares the extracted departure values, never falls
		# back to comparing the trip dicts themselves - unlike sorting (departure, trip) tuples
		# directly, which raises as soon as two trips share a departure (a real occurrence: two
		# different Pros on the same line, opposite directions, departing at the same time).
		line_trips = sorted(line_trips, key=lambda trip: trip["_departure"])
		first_pro_id = int(str(line_trips[0]["_pro_schedule"]).partition("Pro")[-1])
		line_items.append((first_pro_id, endpoints, line_trips))
	line_items.sort()

	for _, endpoints, line_trips in line_items:
		# Pick the template trip and its reverse route.
		first_old_trip = line_trips[0]
		first_route = route_by_id[first_old_trip["_chain_route"]]
		first_route_id = first_route["_id"]
		reverse_route = directed_routes[(
			first_route["_end_location"].removesuffix("_Chain").removesuffix("_Unchain"),
			first_route["_start_location"].removesuffix("_Chain").removesuffix("_Unchain"),
		)]
		reverse_route_id = reverse_route["_id"]

		# Use the configured turnaround unless explicitly asked to infer it from
		# consecutive trips of the same old Pro.
		if turnaround_minutes is None:
			turnaround_seconds = 5 * 60
			turnaround_candidates = []
			trips_by_pro = {}
			for trip in line_trips:
				trips_by_pro.setdefault(trip["_pro_schedule"], []).append(trip)
			for pro_trips in trips_by_pro.values():
				pro_trips = sorted(pro_trips, key=lambda trip: trip["_departure"])
				for previous, current in zip(pro_trips, pro_trips[1:]):
					gap = int(round((
						_parse_iso_dt(current["_departure"])
						- _parse_iso_dt(previous["_arrival"])
					).total_seconds()))
					if gap > 0:
						turnaround_candidates.append(gap)
			if turnaround_candidates:
				turnaround_seconds = min(turnaround_candidates)
		else:
			turnaround_seconds = int(round(turnaround_minutes * 60))

		# Transition between service trips:
		# unchain buffer + routed deadhead + chain buffer.
		# Each half-buffer is rounded up to full minutes.
		turnaround_half_seconds = int(ceil(turnaround_seconds / 120) * 60)
		transition_seconds = {}
		for current_route_id, next_route_id in [(first_route_id, reverse_route_id), (reverse_route_id, first_route_id)]:
			current_route = route_by_id[current_route_id]
			next_route = route_by_id[next_route_id]
			unchain_loc = chain_locations[current_route["_end_location"]]
			chain_loc = chain_locations[next_route["_start_location"]]
			deadhead = router.shortest_path(
				(float(unchain_loc["_start_location_lat"]), float(unchain_loc["_start_location_lon"])),
				(float(chain_loc["_end_location_lat"]), float(chain_loc["_end_location_lon"])),
			)
			transition_seconds[(current_route_id, next_route_id)] = (
				turnaround_half_seconds
				+ max(0, int(ceil(float(deadhead["time_s"]))))
				+ turnaround_half_seconds
			)

		duration_first = route_metrics[first_route_id]
		duration_reverse = route_metrics[reverse_route_id]
		cycle_seconds = (
			duration_first
			+ transition_seconds[(first_route_id, reverse_route_id)]
			+ duration_reverse
			+ transition_seconds[(reverse_route_id, first_route_id)]
		)

		min_pair_spacing_seconds = int(round(pair_spacing_minutes * 60))
		# Create opposite-direction pairs while pair starts stay far enough apart.
		max_pairs = max(1, int(cycle_seconds // min_pair_spacing_seconds))
		max_slots = 2 * max_pairs

		slots_per_direction = max(1, max_slots // 2)
		offset_seconds = cycle_seconds / slots_per_direction
		first_departure = max(_parse_iso_dt(first_old_trip["_departure"]), schedule_start)

		slot_specs = [(first_old_trip["_pro_schedule"], first_route_id, first_departure)]

		# Add further Pros in the original direction, evenly spaced over the
		# revolution if the capacity threshold allows them.
		for offset_index in range(1, slots_per_direction):
			slot_specs.append((
				f"Pro{next_pro_id}",
				first_route_id,
				first_departure + timedelta(seconds=round(offset_index * offset_seconds)),
			))
			next_pro_id += 1

		# Add matching Pros in the opposite direction. The first opposite Pro
		# departs at the same time as the first original-direction Pro.
		for offset_index in range(slots_per_direction):
			slot_specs.append((
				f"Pro{next_pro_id}",
				reverse_route_id,
				first_departure + timedelta(seconds=round(offset_index * offset_seconds)),
			))
			next_pro_id += 1

		for pro_key, start_route_id, departure in slot_specs[:max_slots]:
			current_route_id = start_route_id
			current_departure = departure
			trip_number_by_route = {}

			# Build this Pro's full day by alternating between both directions.
			while current_departure < schedule_end:
				duration = route_metrics[current_route_id]
				arrival = current_departure + timedelta(seconds=duration)
				if arrival > schedule_end:
					break

				trip_number_by_route[current_route_id] = trip_number_by_route.get(current_route_id, 0) + 1
				generated_trips.append({
					"_id": f"{current_route_id}_{pro_key}_{trip_number_by_route[current_route_id]}",
					"_chain_route": current_route_id,
					"_pro_schedule": pro_key,
					"_departure": current_departure.isoformat(),
					"_arrival": arrival.isoformat(),
				})

				next_route_id = reverse_route_id if current_route_id == first_route_id else first_route_id
				current_departure = arrival + timedelta(seconds=transition_seconds[(current_route_id, next_route_id)])
				current_route_id = next_route_id

	return generated_trips

def read_base_json(_filepath: str, _no_pro: bool=False) -> dict[dict]:
	'''
	Read instance data from json file
	'''
	# open file
	with open(_filepath, 'r') as file:
		data = json.load(file)
	
	def normalize_keys(obj):
		if isinstance(obj, dict):
			return {k.lower(): normalize_keys(v) for k, v in obj.items()}
		elif isinstance(obj, list):
			return [normalize_keys(item) for item in obj]
		else:
			return obj
	
	data = normalize_keys(data)
	
	# return dictionary
	base_data = {}
	
	# get request entries
	cab_schedules = data.get("cabschedules", [])
	
	# process entries
	cab_entries = []
	for cab in cab_schedules:
		new_entry = {}
		# Accessing specific data from each schedule
		new_entry["_id"] = int(cab.get("id", None).partition("Cab")[-1])
		schedule = cab.get("schedule", {})
		new_entry["_schedule_start"] = _parse_iso_dt(schedule.get("starttime", None))
		new_entry["_schedule_end"] = _parse_iso_dt(schedule.get("endtime", None))
		new_entry["_label"] = cab.get("label", None)
		new_entry["_license_plate"] = cab.get("licenseplate", None)
		power_consumption = cab.get("powerconsumption", {})
		new_entry["_pc_speed"] = power_consumption.get("speed", None)
		new_entry["_pc_wind"] = power_consumption.get("wind", None)
		new_entry["_pc_load_capacity"] = power_consumption.get("loadcapacity", None)
		new_entry["_pc_tire_traction"] = power_consumption.get("tiretraction", None)
		new_entry["_pc_weather_conditions"] = power_consumption.get("weatherconditions", None)
		new_entry["_pc_climatronic"] = power_consumption.get("climatronic", None)
		new_entry["_pc_regenerative_braking"] = power_consumption.get("regenerativebraking", None)
		new_entry["_flatrate_energy_consumption"] = cab.get("flatratenergyconsumption", None)
		#new_entry["_total_energy_capacity"] = cab.get("TotalEnergyCapacity", None)
		new_entry["_energy_capacity"] = cab.get("totalenergycapacity", None)
		new_entry["_initial_energy_capacity"] = cab.get("initialenergycapacity", None)
		init_location = cab.get("initiallocation", {})
		new_entry["_init_location_lon"] = init_location.get("longitude", None)
		new_entry["_init_location_lat"] = init_location.get("latitude", None)
		new_entry["_max_speed"] = cab.get("maxspeed", None)
		new_entry["_regenerative_power"] = cab.get("regenerativepower", None)
		new_entry["_operating_company"] = cab.get("operatingcompany", None)
		new_entry["_max_regenerative_power"] = cab.get("maxiumregenerativePower", None)
		new_entry["_default_energy_consumption_per_m"] = cab.get("defaultenergyconsuptionperm", None)
		new_entry["_vehicle_type"] = cab.get("vehicletype", None)
		new_entry["_state_of_schedule"] = cab.get("stateofschedule", None)
		new_entry["_ramp"] = cab.get("hasramp", None)
		new_entry["_num_seats"] = cab.get("seats", None)
		new_entry["_child_seats"] = cab.get("childseats", None)
		new_entry["_luggage"] = cab.get("luggage", None)
		charging_curve = cab.get("chargingcurve", [])
		new_entry["_charging_curve"] = []
		for curve in charging_curve:
			new_entry["_charging_curve"].append(curve)
		new_entry["_max_speed_autonomous"] = cab.get("maxspeedautonomous", None)
		
		# Add the entire step to the list of entries
		cab_entries.append(new_entry)
	
	# reduce output to necessary keys
	cab_keys = ["_id", "_init_location_lon", "_init_location_lat", "_schedule_start", "_schedule_end", "_max_speed", "_default_energy_consumption_per_m", "_energy_capacity", "_initial_energy_capacity", "_num_seats", "_ramp"]
	for i in range(len(cab_entries)):
		cab_entries[i] = { key: cab_entries[i][key] for key in cab_keys}
	base_data["cab_schedules"] = cab_entries
	
	# pros
	pro_schedules = data.get("proschedules", [])
	
	pro_entries = []
	for pro in pro_schedules:
		if ( _no_pro ):
			continue
		new_entry = {}
		# Accessing specific data from each schedule
		new_entry["_max_cabs"] = pro.get("maxcabchain", None)
		additional_energy = pro.get("additionalenergyconsumptionpercab", [])
		new_entry["_additional_energy"] = []
		for add in additional_energy:
			new_entry["_additional_energy"].append(add)
		new_entry["_id"] = int(pro.get("id", None).partition("Pro")[-1])
		schedule = pro.get("schedule", {})
		new_entry["_schedule_start"] = schedule.get("starttime", None)
		new_entry["_schedule_end"] = schedule.get("endtime", None)
		new_entry["_label"] = pro.get("label", None)
		new_entry["_license_plate"] = pro.get("licenseplate", None)
		power_consumption = pro.get("powerconsumption", {})
		new_entry["_pc_speed"] = power_consumption.get("speed", None)
		new_entry["_pc_wind"] = power_consumption.get("wind", None)
		new_entry["_pc_load_capacity"] = power_consumption.get("loadcapacity", None)
		new_entry["_pc_tire_traction"] = power_consumption.get("tiretraction", None)
		new_entry["_pc_weather_conditions"] = power_consumption.get("weatherconditions", None)
		new_entry["_pc_climatronic"] = power_consumption.get("climatronic", None)
		new_entry["_pc_regenerative_braking"] = power_consumption.get("regenerativebraking", None)
		new_entry["_flatrate_energy_consumption"] = pro.get("flatratenergyconsumption", None)
		new_entry["_energy_capacity"] = pro.get("totalenergycapacity", None)
		new_entry["_initial_energy_capacity"] = pro.get("initialenergycapacity", None)
		init_location = pro.get("initiallocation", {})
		new_entry["_init_location_lon"] = init_location.get("longitude", None)
		new_entry["_init_location_lat"] = init_location.get("latitude", None)
		new_entry["_max_speed"] = pro.get("maxspeed", None)
		new_entry["_max_power_supply"] = pro.get("maxcabchargingpower", None)
		new_entry["_regenerative_power"] = pro.get("regenerativepower", None)
		new_entry["_operating_company"] = pro.get("operatingcompany", None)
		new_entry["_max_regenerative_power"] = pro.get("maxiumregenerativePower", None)
		new_entry["_default_energy_consumption_per_m"] = pro.get("defaultenergyconsuptionperm", None)
		new_entry["_vehicle_type"] = pro.get("vehicletype", None)
		new_entry["_state_of_schedule"] = pro.get("stateofschedule", None)
		
		# Add the entire step to the list of entries
		pro_entries.append(new_entry)

	# reduce output to necessary keys
	pro_keys = ["_id", "_max_cabs", "_additional_energy", "_init_location_lon", "_init_location_lat", "_schedule_start", "_schedule_end", "_max_speed", "_max_power_supply", "_default_energy_consumption_per_m", "_energy_capacity", "_initial_energy_capacity", "_max_cabs", "_max_speed"]
	for i in range(len(pro_entries)):
		pro_entries[i] = { key: pro_entries[i][key] for key in pro_keys}
	base_data["pro_schedules"] = pro_entries
	
	# charging stations
	charging_points = data.get("chargingpoints", [])
	
	charging_entries = []
	for charge in charging_points:
		new_entry = {}
		# Accessing specific data from each charging station
		new_entry["_id"] = charge.get("guid", None)
		location = charge.get("location", {})
		new_entry["_location_lon"] = location.get("longitude", None)
		new_entry["_location_lat"] = location.get("latitude", None)
		new_entry["_maximum_stopping_time"] = charge.get("maximumstoppingtime", None)
		new_entry["_type"] = charge.get("type", None)
		new_entry["_state"] = charge.get("state", None)
		#new_entry["_maximum_power_supply"] = charge.get("maximumPowerSupply", None)
		new_entry["_max_supply"] = charge.get("maximumpowersupply", None)
		new_entry["_maximum_regenerative_power"] = charge.get("maximumregenerativepower", None)
		opening_hours = charge.get("openinghours", [])
		new_entry["_opening_hours"] = []
		for hours in opening_hours:
			hours_entry = {}
			hours_entry["WeekDays"] = hours.get("weekdays", None)
			hours_entry["min"] = hours.get("min", None)
			hours_entry["max"] = hours.get("max", None)
			new_entry["_opening_hours"].append(hours_entry)
		new_entry["_operators"] = charge.get("operators", None)
		new_entry["_voltage"] = charge.get("voltage", None)
		new_entry["_amperage"] = charge.get("amperage", None)
		new_entry["_owner"] = charge.get("owner", None)
		charging_entries.append(new_entry)
	
	# reduce to necessary keys
	charging_keys = ["_id", "_location_lon", "_location_lat", "_opening_hours", "_max_supply"]
	for i in range(len(charging_entries)):
		charging_entries[i] = { key: charging_entries[i][key] for key in charging_keys}
		charging_entries[i]["_schedule_start"] = charging_entries[i]["_opening_hours"][0]["min"]
		charging_entries[i]["_schedule_end"] = charging_entries[i]["_opening_hours"][0]["max"]
		charging_entries[i].pop("_opening_hours",None)
	base_data["charging_points"] = charging_entries
	
	# chaining locations
	chain_location = data.get("chaininglocations", [])
	
	chain_loc_entries = []
	for chain in chain_location:
		if ( _no_pro ):
			continue
		new_entry = {}
		# Accessing specific data from each chaining locations
		new_entry["_id"] = chain.get("guid", None)
		locationstart = chain.get("locationstart", {})
		new_entry["_start_location_lon"] = locationstart.get("longitude", None)
		new_entry["_start_location_lat"] = locationstart.get("latitude", None)
		locationend = chain.get("locationend", {})
		new_entry["_end_location_lon"] = locationend.get("longitude", None)
		new_entry["_end_location_lat"] = locationend.get("latitude", None)
		new_entry["_type"] = chain.get("type", None)
		new_entry["_additional_time"] = chain.get("additionaltime", None)
		chain_loc_entries.append(new_entry)
	
	base_data["chaining_location"] = chain_loc_entries

	chain_routes = data.get("chainRoutes", [])
	chain_route_entries = []

	for chain_route in chain_routes:
		new_entry = {}
		new_entry["_id"] = chain_route.get("Guid", None)
		new_entry["_start_location"] = chain_route.get("StartLocation", None)
		new_entry["_end_location"] = chain_route.get("EndLocation", None)
		new_entry["_intermediate_locations"] = chain_route.get("IntermediateChainingLocations", None)
		new_entry["_duration"] = chain_route.get("Duration", None)
		new_entry["_distance"] = chain_route.get("Distance", None)
		
		chain_route_entries.append(new_entry)

	base_data["chain_routes"] = chain_route_entries
	
	# parking locations
	park_location = data.get("parkinglocations", [])
	
	park_loc_entries = []
	for park in park_location:
		pass
	
	base_data["parking_location"] = park_loc_entries
	
	# chain routes
	chain_routes = data.get("chainroutes", [])
	chain_route_entries = []

	for chainroute in chain_routes:
		if ( _no_pro ):
			continue
		new_entry = {}
		# Accessing specific data from each chaining routes
		new_entry["_id"] = chainroute.get("guid", None)
		new_entry["_start_location"] = chainroute.get("startlocation", None)
		new_entry["_end_location"] = chainroute.get("endlocation", None)
		new_entry["_intermediate_locations"] = chainroute.get("intermediatechaininglocations", None)
		new_entry["_duration"] = int(chainroute.get("duration", None))
		new_entry["_distance"] = int(chainroute.get("distance", None))
		
		chain_route_entries.append(new_entry)

	base_data["chain_routes"] = chain_route_entries
	
	# chain routes
	chain_route_schedules = data.get("chainrouteschedules", [])
	chain_route_schedule_entries = []
	for chainroutesched in chain_route_schedules:
		new_entry = {}
		# Accessing specific data from each chaining routes
		new_entry["_id"] = chainroutesched.get("guid", None)
		new_entry["_chain_route"] = chainroutesched.get("chainroute", None)
		new_entry["_pro_schedule"] = chainroutesched.get("proschedule", None)
		new_entry["_departure"] = chainroutesched.get("departure", None)
		new_entry["_arrival"] = chainroutesched.get("arrival", None)
		#new_entry["_duration"] = chainroutesched.get("duration", None)
		chain_route_schedule_entries.append(new_entry)
	
	base_data["chain_route_schedules"] = chain_route_schedule_entries
	
	# operation areas
	operation_area = data.get("operationareas", [])
	operation_area_entries = []
	for area in operation_area:
		new_entry = {}
		# Accessing specific data from each charging station
		new_entry["_id"] = area.get("guid", None)
		new_entry["_desc"] = area.get("shorthandle", None)
		location_border  = area.get("locationborder", [])
		new_entry["_location_border"] = []
		for vertex in location_border:
			vert = {}
			
			vert["_longitude"] = vertex.get("longitude", None)
			vert["_latitude"] = vertex.get("latitude", None)
			new_entry["_location_border"].append(vert)
		guilty_range  = area.get("guiltyrange", {})
		new_entry["_guilty_range_start"] = guilty_range.get("starttime", None)
		new_entry["_guilty_range_end"] = guilty_range.get("endtime", None)
		new_entry["_operator"] = area.get("operator", None)
		operation_area_entries.append(new_entry)
	
	# reduce to necessary keys
	operations_keys = ["_id", "_desc", "_location_border"]
	op_rectangle = {}
	for i in range(len(operation_area_entries)):
		operation_area_entries[i] = { key: operation_area_entries[i][key] for key in operations_keys}
	base_data["operation_area"] = operation_area_entries
	
	# start of simulation
	simulation_time = data.get("simulationinittime", None)
	base_data["simulation_init_time"] = simulation_time
	
	# configuration of the simulation
	planning_configuration = data.get("planningconfiguration", {})
	new_entry = {}
	new_entry["_tolerance_driving_time"] = planning_configuration.get("tolerancedrivingtime", None)
	new_entry["_tolerance_capacity"] = planning_configuration.get("tolerancecapacity", None)
	new_entry["_chain_time"] = planning_configuration.get("chaintime", None)
	new_entry["_proposal_timer"] = planning_configuration.get("proposaltimer", None)
	new_entry["_proposal_count"] = planning_configuration.get("proposalcount", None)
	new_entry["_planning_horizon"] = planning_configuration.get("planninghorizon", None)
	new_entry["_chargin_start_threshold"] = planning_configuration.get("chargingstartthreshhold", None)
	new_entry["_chargin_end_threshold"] = planning_configuration.get("chargingendthreshhold", None)
	new_entry["_use_pro_threshold"] = planning_configuration.get("useprothreshhold", None)
	new_entry["_mode"] = planning_configuration.get("Mode", None)
	
	base_data["_planning_configuration"] = new_entry
	
	return base_data

# ------------------------------------------------------------------------------


if __name__ == "__main__":
	
	from pathlib import Path
	
	fname = Path(__file__).resolve().parent / "data" / "input" / "example_demand.json"
	inst = read_request_json(fname)
	
	print(fname)
	print(inst)
	
	base_fname = Path(__file__).resolve().parent / "data" / "input" / "example_basedata.json"
	base = read_base_json(base_fname)
	print(base)
