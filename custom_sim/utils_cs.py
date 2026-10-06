			
import bisect
import math
from datetime import datetime, timezone
import json
from pathlib import Path

import matplotlib.pyplot as plt


# ---------------------------------------------------------
# ---------------------------------------------------------

class EventQueue:
	def __init__(self, request_events, vehicle_events):
		# requests (NC) – large, never changing
		self.static = sorted(request_events, key=lambda e: e.time)
		
		# vehicle events (TI) – small, dynamically updated
		self.dynamic = sorted(vehicle_events, key=lambda e: e.time)

	def _insert_dynamic(self, event):
		"""Insert a vehicle event into the dynamic list, preserving sorted order."""
		bisect.insort(self.dynamic, event, key=lambda e: e.time)

	def update_vehicle_event(self, event, new_time):
		"""Update the timestamp of an existing vehicle event and reinsert it."""
		# Remove from dynamic list
		self.dynamic.remove(event)
		
		# Update timestamp
		event.time = new_time
		
		# Reinsert at correct sorted position
		self._insert_dynamic(event)

	def push_vehicle_event(self, event):
		"""Insert a new vehicle event into the dynamic part of the queue."""
		self._insert_dynamic(event)

	def pop(self):
		"""Return and remove the earliest event (from static or dynamic)."""
		if not self.static and not self.dynamic:
			return None
		
		# If one list empty, pop from the other
		if not self.static:
			return self.dynamic.pop(0)
		if not self.dynamic:
			return self.static.pop(0)

		# Both non-empty → compare the earliest element of each
		if self.static[0].time <= self.dynamic[0].time:
			return self.static.pop(0)
		else:
			return self.dynamic.pop(0)

	def __len__(self):
		return len(self.static) + len(self.dynamic)

# ---------------------------------------------------------

def haversine_distance(lat1, lon1, lat2, lon2):
	"""
	Compute great-circle distance between two points on Earth (in meters).
	"""
	R = 6371000  # Earth radius in meters
	dlat = math.radians(lat2 - lat1)
	dlon = math.radians(lon2 - lon1)
	a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * \
		math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
	c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
	
	return R * c

# ---------------------------------------------------------

def nearest_straight_line(vehicle_coord, stations):
	"""
	vehicle_coord: (lat, lon)
	stations: list of (lat, lon)
	
	Returns: (index, candidate_coord, distance_in_meters)
	"""
	vlat, vlon = vehicle_coord
	
	best_idx = None
	best_dist = float("inf")
	
	for i, (clat, clon) in enumerate(stations):
		d = haversine_distance(vlat, vlon, clat, clon)
		if d < best_dist:
			best_dist = d
			best_idx = i
	
	return best_idx #, candidates[best_idx], best_dist

# ---------------------------------------------------------

def best_station_between(A, B, stations):
	"""
	A: (lat, lon)
	B: (lat, lon)
	stations: list of (lat, lon)
	"""
	best_idx = None
	best_score = float("inf")
	
	for i, S in enumerate(stations):
		d = haversine_distance(*A, *S) + haversine_distance(*S, *B)
		if d < best_score:
			best_score = d
			best_idx = i
	
	return best_idx #, stations[best_idx], best_score

# ---------------------------------------------------------

def best_station_between_weighted(A, B, stations, weight_from_A=0.5):
	"""
	A: (lat, lon)
	B: (lat, lon)
	stations: list of (lat, lon)
	weight_from_A: weight for distance(A, station), clamped to [0,1]
	"""
	w = max(0.0, min(1.0, float(weight_from_A)))
	best_idx = None
	best_score = float("inf")

	for i, S in enumerate(stations):
		d = w * haversine_distance(*A, *S) + (1.0 - w) * haversine_distance(*S, *B)
		if d < best_score:
			best_score = d
			best_idx = i

	return best_idx

# ---------------------------------------------------------

def visualize_schedule(schedule, tw_lower=None, tw_upper=None, cursor=None, cutoff=None):
	"""
	timeline visualization.
	"""
	items = []
	for i, e in enumerate(schedule):
		if e.type.name == "CT":
			try:
				cid = e.customer.request.id
			except:
				print("# WARNING CT WITHOUT CUSTOMER")
				print(e, e.customer)
			items.append((e.start_time, f"[{cid}:{e.type.name}"))
		else:
			items.append((e.start_time, f"[{e.type.name}"))
		
		items.append((e.end_time, "]"))

	if tw_lower is not None:
		items.append((tw_lower, "<TW_L>"))
	if tw_upper is not None:
		items.append((tw_upper, "<TW_U>"))
	if cursor is not None:
		items.append((cursor, "<CUR>"))

	items.sort(key=lambda x: x[0])

	print("\n--- Timeline ---")
	for t, lbl in items:
		print(f"{format_ts_to_hhmm_or_date(t,cutoff)}  {lbl}")
	print("---------------\n")
	
	return

# ---------------------------------------------------------

def format_ts_to_hhmm(ts):
	"""Format unix timestamp as HH:MM in local time."""
	return datetime.fromtimestamp(ts).strftime("%H:%M")

# ---------------------------------------------------------

def format_ts_to_hhmm_or_date(ts, cutoff_ts=None):
	"""Format timestamp as HH:MM, or include date if it differs from cutoff day."""
	dt = datetime.fromtimestamp(ts)

	# no cutoff -> normal hh:mm formatting
	if cutoff_ts is None:
		return dt.strftime("%H:%M")

	cutoff_dt = datetime.fromtimestamp(cutoff_ts)

	# if not the same calendar date -> include date
	if dt.date() != cutoff_dt.date():
		return dt.strftime("%Y-%m-%d %H:%M")

	# same day -> just HH:MM
	return dt.strftime("%H:%M")

# ---------------------------------------------------------

def write_rw_style_iteration_files(current_solution, operations_area, cab_fleet, pro_fleet,
									demand_scenario, iteration, output_dir=None,
									prefix_convoy_charging_power_w=None):
	"""
	Write the five RW-style interface files for one custom-sim iteration.

	This is intentionally the only public utility added for the export. Small
	formatting and conversion helpers stay local to avoid spreading this concern
	through the module.
	"""
	output_dir = Path(output_dir) if output_dir is not None else Path(__file__).resolve().parent / "output"
	output_dir.mkdir(parents=True, exist_ok=True)

	def iso(value):
		if value is None:
			return None
		if isinstance(value, (int, float)):
			dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
		elif isinstance(value, datetime):
			dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
			dt = dt.astimezone(timezone.utc)
		elif isinstance(value, str):
			try:
				dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
				dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
				dt = dt.astimezone(timezone.utc)
			except ValueError:
				return value
		else:
			return str(value)
		return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

	def json_default(value):
		if isinstance(value, datetime):
			return iso(value)
		return str(value)

	def dump_json(path, payload):
		with open(path, "w", encoding="utf-8") as file:
			json.dump(payload, file, indent="\t", default=json_default)

	def loc(lat, lon):
		return {"longitude": float(lon), "latitude": float(lat)}

	def rounded(value):
		return int(round(float(value or 0.0)))

	def label(prefix, vehicle):
		return f"{prefix}{getattr(vehicle, 'id', '')}"

	def license_plate(vehicle, fallback):
		plate = getattr(vehicle, "licensePlate", fallback)
		if isinstance(plate, (list, tuple)):
			plate = plate[0] if plate else fallback
		return str(plate)

	def req_window_minutes(req):
		lower = req.tw_lower
		upper = req.tw_upper
		if isinstance(lower, str):
			lower = datetime.fromisoformat(lower.replace("Z", "+00:00"))
		if isinstance(upper, str):
			upper = datetime.fromisoformat(upper.replace("Z", "+00:00"))
		if isinstance(lower, datetime) and isinstance(upper, datetime) and upper > lower:
			return int(round((upper - lower).total_seconds() / 60.0))
		return None

	def build_prefix():
		raw_area = str(getattr(operations_area.operational_area, "description", "") or "").strip().lower()
		area = raw_area.split("_", 1)[0].split("-", 1)[0].split(" ", 1)[0] or "pb"
		area = {"paderborn": "pb", "hoexter": "hx"}.get(area, area)
		if area in ("default", "unknown"):
			plates = [license_plate(cab, "") for cab in current_solution.vehicle_fleet.cabs]
			plate_prefix = next((plate.split("-", 1)[0].lower() for plate in plates if "-" in plate), "")
			area = {"pb": "pb", "hx": "hx"}.get(plate_prefix, area)
		area = "".join(ch for ch in area if ch.isalnum()) or "pb"

		cab_energies = [float(getattr(cab, "total_energy_capacity", 0) or 0) for cab in current_solution.vehicle_fleet.cabs]
		if prefix_convoy_charging_power_w is not None:
			cp_powers = [float(prefix_convoy_charging_power_w)]
		else:
			cp_powers = [
				float(getattr(pro_vehicle.pro, "max_power_supply", 0) or 0)
				for pro_vehicle in pro_fleet
			]
		max_cab_energy = max(cab_energies or [0.0])
		max_cp_power = max(cp_powers or [0.0])
		energy = int(round(max_cab_energy / 1000.0)) if max_cab_energy >= 1000 else int(round(max_cab_energy))
		power = int(round(max_cp_power / 1000.0)) if max_cp_power >= 1000 else int(round(max_cp_power))

		routes = getattr(operations_area.pro_routes_and_trips, "chain_routes", []) or []
		windows = {minutes for req in current_solution.demand_scenario.requests for minutes in [req_window_minutes(req)] if minutes is not None}
		timewindow = next(iter(windows)) if len(windows) == 1 else 0

		return (
			f"{area}_{len(operations_area.charging_stations)}cs_{int(len(routes) / 2)}lines_"
			f"{energy}bc_{power}cp_{current_solution.demand_scenario.num_requests}rq_{timewindow}tw"
		)

	def trip_guid(customer):
		return "" if customer is None else f"trip_{int(customer.request.id)}"

	def appointment(customer):
		if customer is None:
			return None
		return {"startTime": iso(customer.tw_lower_dt), "endTime": iso(customer.tw_upper_dt)}

	def service_time(customer, fallback=60.0):
		return float(getattr(customer, "entry_time", fallback) if customer is not None else fallback)

	def energy_consumed(entry):
		if entry is None:
			return 0.0
		return max(0.0, float(entry.start_charge or 0.0) - float(entry.end_charge or 0.0))

	def energy_charged(entry):
		if entry is None:
			return 0.0
		return max(0.0, float(entry.end_charge or 0.0) - float(entry.start_charge or 0.0))

	def chain_segment_id(entry):
		pro_vehicle = getattr(entry, "pro", None)
		if pro_vehicle is None:
			return None
		for pro_entry in getattr(pro_vehicle, "schedule", []) or []:
			if abs(int(pro_entry.start_time) - int(entry.start_time)) <= 1 and abs(int(pro_entry.end_time) - int(entry.end_time)) <= 1:
				return str(getattr(pro_entry, "source_trip_id", None) or f"Pro{pro_vehicle.pro.id}_{entry.start_time}")
		return f"Pro{pro_vehicle.pro.id}_{entry.start_time}"

	def make_stop(vehicle_label, stop_type, *, customer=None, arrival_ts=None, departure_ts=None,
				  duration_s=0.0, driving_s=0.0, distance_m=0.0, consumed_wh=0.0,
				  charged_wh=0.0, remaining_wh=0.0, lat=None, lon=None, chain_segment=None,
				  key=None):
		user_guid = str(int(customer.request.id)) if customer is not None else ""
		tguid = trip_guid(customer)
		if key is None:
			if stop_type == "Depot":
				key = f"{vehicle_label}_Start"
			elif stop_type == "Charging":
				key = f"ChargeCharging_{chain_segment or 'Station'}_{int(arrival_ts or 0)}"
			elif stop_type == "Chaining":
				key = f"Chain_{chain_segment or 'Segment'}_{tguid or int(arrival_ts or 0)}"
			elif stop_type == "Unchaining":
				key = f"Unchain_{chain_segment or 'Segment'}_{tguid or int(arrival_ts or 0)}"
			else:
				key = f"{user_guid}_{tguid}_{stop_type}" if user_guid else f"{vehicle_label}_{stop_type}_{int(arrival_ts or 0)}"

		return {
			"status": 3,
			"vehicleSchedule": vehicle_label,
			"tripGuid": tguid,
			"userGuid": user_guid,
			"stopType": stop_type,
			"consumedEnergy": rounded(consumed_wh),
			"chargedEnergy": rounded(charged_wh),
			"remainingEnergy": rounded(remaining_wh),
			"currentQuantities": int(getattr(customer, "num_persons", 1) or 1),
			"appointment": appointment(customer),
			"guidChainRouteSegment": chain_segment,
			"key": key,
			"location": loc(lat, lon),
			"latestStartDrivingToStop": iso(float(arrival_ts or 0.0) - float(driving_s or 0.0)),
			"arrival": iso(arrival_ts),
			"departure": iso(departure_ts if departure_ts is not None else arrival_ts),
			"duration": rounded(duration_s),
			"drivingTime": rounded(driving_s),
			"distance": rounded(distance_m),
		}

	def cab_vehicle_payload(cab):
		cab_label = label("Cab", cab)
		return {
			"hasRamp": bool(getattr(cab, "has_ramp", False)),
			"seats": int(getattr(cab, "seats", 0) or 0),
			"childSeats": int(getattr(cab, "child_seats", 0) or 0),
			"luggage": int(getattr(cab, "luggage", 0) or 0),
			"chargingCurve": list(getattr(cab, "charging_curve", [0]) or [0]),
			"maxSpeedAutonomous": getattr(cab, "max_speed_autonomous", 0),
			"id": cab_label,
			"schedule": {"startTime": iso(cab.schedule_start_time), "endTime": iso(cab.schedule_end_time)},
			"label": cab_label,
			"licensePlate": license_plate(cab, f"PB-NE-{cab.id}"),
			"powerConsumption": {
				"speed": getattr(cab, "pc_speed", 0),
				"wind": getattr(cab, "pc_wind", 0),
				"loadCapacity": getattr(cab, "pc_load_capacity", 0),
				"tireTraction": getattr(cab, "pc_tire_traction", 0),
				"weatherConditions": getattr(cab, "pc_weather_condition", 0),
				"climatronic": getattr(cab, "pc_climatronic", 0),
				"regenerativeBraking": getattr(cab, "pc_regenerative_breaking", 0),
			},
			"flatratEnergyConsumption": getattr(cab, "flatrate_power_consumtion", 0),
			"risk": getattr(cab, "risc", 0),
			"totalEnergyCapacity": getattr(cab, "total_energy_capacity", 0),
			"initialEnergyCapacity": getattr(cab, "init_energy_capacity", 0),
			"initialLocation": loc(cab.init_location_lat, cab.init_location_lon),
			"maxSpeed": getattr(cab, "max_speed", 0),
			"regenerativePower": getattr(cab, "regenerative_power", 0),
			"operatingCompany": getattr(cab, "operating_company", ""),
			"maximumRegenerativePower": getattr(cab, "maximum_regenerative_power", 0),
			"defaultEnergyConsuptionPerM": getattr(cab, "default_energy_consumption_per_m", 0),
			"vehicleType": "cab",
			"stateOfSchedule": "Active",
		}

	def pro_vehicle_payload(pro):
		pro_label = label("Pro", pro)
		payload = {
			"maxCabChain": getattr(pro, "max_cabs", 0),
			"additionalEnergyConsumptionPerCab": getattr(pro, "additional_consumption_per_cab", [0]),
			"id": pro_label,
			"schedule": {"startTime": iso(pro.schedule_start), "endTime": iso(pro.schedule_end)},
			"label": pro_label,
			"licensePlate": license_plate(pro, f"PB-PR-{pro.id}"),
			"powerConsumption": {
				"speed": getattr(pro, "pc_speed", 0),
				"wind": getattr(pro, "pc_wind", 0),
				"loadCapacity": getattr(pro, "pc_load_capacity", 0),
				"tireTraction": getattr(pro, "pc_tire_traction", 0),
				"weatherConditions": getattr(pro, "pc_weather_condition", 0),
				"climatronic": getattr(pro, "pc_climatronic", 0),
				"regenerativeBraking": getattr(pro, "pc_regenerative_breaking", 0),
			},
			"flatratEnergyConsumption": getattr(pro, "flatrate_power_consumtion", 0),
			"risk": getattr(pro, "risc", 0),
			"totalEnergyCapacity": getattr(pro, "total_energy_capacity", 0),
			"initialEnergyCapacity": getattr(pro, "init_energy_capacity", 0),
			"initialLocation": loc(pro.init_location_lat, pro.init_location_lon),
			"maxSpeed": getattr(pro, "max_speed", 0),
			"regenerativePower": getattr(pro, "regenerative_power", 0),
			"operatingCompany": getattr(pro, "operating_company", ""),
			"maximumRegenerativePower": getattr(pro, "maximum_regenerative_power", 0),
			"defaultEnergyConsuptionPerM": getattr(pro, "default_energy_consumption_per_m", 0),
			"vehicleType": "pro",
			"stateOfSchedule": "Active",
		}
		if getattr(pro, "max_power_supply", None) is not None:
			payload["maxCabChargingPower"] = pro.max_power_supply
		return payload

	def next_customer_entry(entries, start_index):
		for entry in entries[start_index + 1:]:
			if getattr(entry, "customer", None) is not None:
				return entry
			if getattr(getattr(entry, "type", None), "name", "") in ("ChA", "ChP"):
				break
		return None

	def build_cab_output():
		output = []
		for cab_vehicle in sorted(cab_fleet, key=lambda item: int(item.cab.id)):
			cab = cab_vehicle.cab
			cab_label = label("Cab", cab)
			stops = [make_stop(
				cab_label,
				"Depot",
				arrival_ts=int(cab_vehicle.start_schedule),
				departure_ts=int(cab_vehicle.start_schedule),
				remaining_wh=getattr(cab, "init_energy_capacity", 0),
				lat=cab.init_location_lat,
				lon=cab.init_location_lon,
				key=f"{cab_label}_Start",
			)]
			entries = sorted(cab_vehicle.past_entries, key=lambda entry: (entry.start_time, entry.end_time))
			pending_charging_access = None

			for index, entry in enumerate(entries):
				entry_type = getattr(entry.type, "name", str(entry.type))
				if entry_type == "ChA":
					pending_charging_access = entry
					continue
				if entry_type != "ChP" and pending_charging_access is not None:
					access = pending_charging_access
					stops.append(make_stop(cab_label, "ScheduleAnchor",
						arrival_ts=access.start_time, departure_ts=access.start_time,
						remaining_wh=access.start_charge,
						lat=access.start_lat, lon=access.start_lon,
						key=f"{cab_label}_ChargingAnchor_{int(access.start_time)}"))
					stops.append(make_stop(cab_label, "Charging",
						arrival_ts=access.end_time, departure_ts=access.end_time,
						driving_s=access.end_time - access.start_time, distance_m=access.distance_m,
						consumed_wh=energy_consumed(access), remaining_wh=access.end_charge,
						lat=access.end_lat, lon=access.end_lon,
						chain_segment=getattr(getattr(getattr(access, "charge_station", None), "station", None), "id", None)))
					pending_charging_access = None

				if entry_type == "ChP":
					access = pending_charging_access
					arrival_ts = access.end_time if access is not None else entry.start_time
					anchor_ts = access.start_time if access is not None else entry.start_time
					anchor_lat = access.start_lat if access is not None else entry.start_lat
					anchor_lon = access.start_lon if access is not None else entry.start_lon
					anchor_charge = access.start_charge if access is not None else entry.start_charge
					stops.append(make_stop(cab_label, "ScheduleAnchor",
						arrival_ts=anchor_ts, departure_ts=anchor_ts,
						remaining_wh=anchor_charge,
						lat=anchor_lat, lon=anchor_lon,
						key=f"{cab_label}_ChargingAnchor_{int(anchor_ts)}"))
					stops.append(make_stop(cab_label, "Charging",
						arrival_ts=arrival_ts, departure_ts=entry.end_time,
						duration_s=max(0, entry.end_time - arrival_ts),
						driving_s=(access.end_time - access.start_time) if access is not None else 0,
						distance_m=getattr(access, "distance_m", 0.0) if access is not None else 0.0,
						consumed_wh=energy_consumed(access), charged_wh=energy_charged(entry),
						remaining_wh=entry.end_charge, lat=entry.end_lat, lon=entry.end_lon,
						chain_segment=getattr(getattr(getattr(entry, "charge_station", None), "station", None), "id", None)))
					pending_charging_access = None
					continue

				if entry_type == "CA":
					next_entry = next_customer_entry(entries, index)
					customer = getattr(next_entry, "customer", None) if next_entry is not None else None
					service_s = service_time(customer, 0.0) if customer is not None else 0.0
					stops.append(make_stop(cab_label, "Pickup" if customer is not None else "Relocation",
						customer=customer, arrival_ts=entry.end_time, departure_ts=entry.end_time + service_s,
						duration_s=service_s, driving_s=entry.end_time - entry.start_time,
						distance_m=entry.distance_m, consumed_wh=energy_consumed(entry),
						remaining_wh=entry.end_charge, lat=entry.end_lat, lon=entry.end_lon))
				elif entry_type in ("CT", "LM"):
					customer = entry.customer
					total_service = float(getattr(entry, "service_time", 0.0) or 0.0)
					drop_service = min(service_time(customer, total_service), total_service) if total_service > 0 else 0.0
					stops.append(make_stop(cab_label, "Dropoff",
						customer=customer, arrival_ts=entry.end_time - drop_service, departure_ts=entry.end_time,
						duration_s=drop_service, driving_s=max(0.0, entry.end_time - entry.start_time - total_service),
						distance_m=entry.distance_m, consumed_wh=energy_consumed(entry),
						remaining_wh=entry.end_charge, lat=entry.end_lat, lon=entry.end_lon))
				elif entry_type == "FM":
					customer = entry.customer
					total_service = float(getattr(entry, "service_time", 0.0) or 0.0)
					chain_service = max(0.0, total_service - service_time(customer, total_service))
					segment = None
					for future in entries[index + 1:]:
						if getattr(future, "customer", None) is customer and getattr(getattr(future, "type", None), "name", "") == "PT":
							segment = chain_segment_id(future)
							break
					stops.append(make_stop(cab_label, "Chaining",
						customer=customer, arrival_ts=entry.end_time - chain_service, departure_ts=entry.end_time,
						duration_s=chain_service, driving_s=max(0.0, entry.end_time - entry.start_time - total_service),
						distance_m=entry.distance_m, consumed_wh=energy_consumed(entry),
						remaining_wh=entry.end_charge, lat=entry.end_lat, lon=entry.end_lon,
						chain_segment=segment))
				elif entry_type == "PT":
					customer = entry.customer
					unchain_service = 0.0
					for future in entries[index + 1:]:
						if getattr(future, "customer", None) is customer and getattr(getattr(future, "type", None), "name", "") == "LM":
							unchain_service = max(0.0, float(future.service_time or 0.0) - service_time(customer, future.service_time))
							break
					stops.append(make_stop(cab_label, "Unchaining",
						customer=customer, arrival_ts=entry.end_time, departure_ts=entry.end_time + unchain_service,
						duration_s=unchain_service, driving_s=entry.end_time - entry.start_time,
						distance_m=entry.distance_m, consumed_wh=energy_consumed(entry),
						charged_wh=energy_charged(entry), remaining_wh=entry.end_charge,
						lat=entry.end_lat, lon=entry.end_lon, chain_segment=chain_segment_id(entry)))
				elif entry_type in ("PA", "PlA"):
					stops.append(make_stop(cab_label, "Relocation",
						arrival_ts=entry.end_time, departure_ts=entry.end_time,
						driving_s=entry.end_time - entry.start_time, distance_m=entry.distance_m,
						consumed_wh=energy_consumed(entry), charged_wh=energy_charged(entry),
						remaining_wh=entry.end_charge, lat=entry.end_lat, lon=entry.end_lon))

			if pending_charging_access is not None:
				access = pending_charging_access
				stops.append(make_stop(cab_label, "ScheduleAnchor",
					arrival_ts=access.start_time, departure_ts=access.start_time,
					remaining_wh=access.start_charge,
					lat=access.start_lat, lon=access.start_lon,
					key=f"{cab_label}_ChargingAnchor_{int(access.start_time)}"))
				stops.append(make_stop(cab_label, "Charging",
					arrival_ts=access.end_time, departure_ts=access.end_time,
					driving_s=access.end_time - access.start_time, distance_m=access.distance_m,
					consumed_wh=energy_consumed(access), remaining_wh=access.end_charge,
					lat=access.end_lat, lon=access.end_lon,
					chain_segment=getattr(getattr(getattr(access, "charge_station", None), "station", None), "id", None)))
				pending_charging_access = None

			if stops[-1]["key"] != f"{cab_label}_End":
				last_stop = stops[-1]
				last_departure = datetime.fromisoformat(last_stop["departure"].replace("Z", "+00:00")).timestamp()
				last_location = last_stop.get("location", {})
				# Frontend terminator only: keep it co-located with the previous
				# stop so it cannot imply an additional movement.
				stops.append(make_stop(cab_label, "Depot",
					arrival_ts=last_departure, departure_ts=last_departure,
					driving_s=0.0, distance_m=0.0, consumed_wh=0.0,
					remaining_wh=float(last_stop.get("remainingEnergy", 0) or 0),
					lat=last_location.get("latitude", cab.init_location_lat),
					lon=last_location.get("longitude", cab.init_location_lon),
					key=f"{cab_label}_End"))

			output.append({
				"vehicle": cab_vehicle_payload(cab),
				"currentLocation": stops[-1]["location"] if stops else loc(cab.init_location_lat, cab.init_location_lon),
				"estimatedTimeAtStop": "0001-01-01T00:00:00.000Z",
				"estimatedEnergyAtStop": "0001-01-01T00:00:00.000Z",
				"tripStops": stops,
				"archivedStops": [],
			})
		return output

	def build_pro_output():
		# Chain/unchain handshake time (ChainingLocation.additional_time) for a Pro schedule
		# entry's own trip, looked up from the same static route/trip data the live simulation
		# uses to build the timetable in the first place (CustomSimulation._read_pro_timetable),
		# so no extra state needs to be carried on the entry itself. Built once here, since
		# every entry's lookup would otherwise rebuild the same three dicts from scratch.
		chaining_location_by_id = {str(cl.id): cl for cl in operations_area.chaining_locations}
		chain_route_by_id = {
			str(route.id): route
			for route in getattr(operations_area.pro_routes_and_trips, "chain_routes", []) or []
		}
		chain_route_trip_by_id = {
			str(trip.id): trip
			for trip in getattr(operations_area.pro_routes_and_trips, "chain_route_trips", []) or []
		}

		def chain_unchain_service_s(source_trip_id, chain_route_trip_by_id, chain_route_by_id, chaining_location_by_id):
			trip = chain_route_trip_by_id.get(str(source_trip_id)) if source_trip_id is not None else None
			route = chain_route_by_id.get(trip.chain_route) if trip is not None else None
			if route is None:
				return 0.0, 0.0
			chain_loc = chaining_location_by_id.get(route.start_location)
			unchain_loc = chaining_location_by_id.get(route.end_location)
			chain_add = float(chain_loc.additional_time) if chain_loc is not None else 0.0
			unchain_add = float(unchain_loc.additional_time) if unchain_loc is not None else 0.0
			return chain_add, unchain_add

		output = []
		for pro_vehicle in sorted(pro_fleet, key=lambda item: int(item.pro.id)):
			pro = pro_vehicle.pro
			pro_label = label("Pro", pro)
			stops = []
			for index, entry in enumerate(sorted(pro_vehicle.schedule, key=lambda item: (item.start_time, item.end_time))):
				base_key = str(getattr(entry, "source_trip_id", None) or f"{pro_label}_{index}")
				chained_cabs = len(getattr(entry, "cabs", []) or [])
				# The chain/unchain handshake time is a fixed property of the location, the
				# same for every Cab using it. Reported only when a Cab actually
				# chained/unchained on this leg; start_time/end_time stay the published
				# timetable, unaffected.
				chain_service_s, unchain_service_s = (
					chain_unchain_service_s(
						entry.source_trip_id, chain_route_trip_by_id, chain_route_by_id, chaining_location_by_id
					) if chained_cabs > 0 else (0.0, 0.0)
				)
				trip_type = entry.type.name
				stops.append({
					"vehicleSchedule": pro_label, "chainedCabs": chained_cabs, "tripType": trip_type,
					"key": f"{base_key}-Start", "location": loc(entry.start_lat, entry.start_lon),
					"latestStartDrivingToStop": iso(entry.start_time),
					"arrival": iso(entry.start_time), "departure": iso(entry.start_time),
					"duration": rounded(chain_service_s), "drivingTime": 0, "distance": 0,
				})
				stops.append({
					"vehicleSchedule": pro_label, "chainedCabs": chained_cabs, "tripType": trip_type,
					"key": f"{base_key}-End", "location": loc(entry.end_lat, entry.end_lon),
					"latestStartDrivingToStop": iso(entry.start_time),
					"arrival": iso(entry.end_time), "departure": iso(entry.end_time),
					"duration": rounded(unchain_service_s), "drivingTime": rounded(entry.end_time - entry.start_time),
					"distance": rounded(entry.distance_m),
				})
			output.append({
				"vehicle": pro_vehicle_payload(pro),
				"currentLocation": stops[-1]["location"] if stops else loc(pro.init_location_lat, pro.init_location_lon),
				"chainingStops": stops,
			})
		return output

	def build_sim_output(cab_output):
		report_keys = (
			"sumConsumedEnergy", "sumDistance", "sumDrivingTime", "itemCount",
			"sumServiceTime", "counterToursWithServices", "itemsPerTour", "kmPerItems",
			"sumCustomerDistance", "sumCustomerDrivingTime", "sumCustomerConsumedEnergy",
			"chargingTime", "sumChargingDrivingTime", "sumChargingDistance",
			"sumChargingConsumedEnergy", "sumChargedEnergy",
		)
		report = {key: 0 for key in report_keys}
		used_cabs = 0
		for cab_entry in cab_output:
			has_pickup = False
			for stop in cab_entry.get("tripStops", []):
				stop_type = str(stop.get("stopType", "")).lower()
				if stop_type not in ("depot", "scheduleanchor"):
					report["itemCount"] += 1
				report["sumConsumedEnergy"] += stop.get("consumedEnergy", 0) or 0
				report["sumDistance"] += stop.get("distance", 0) or 0
				report["sumDrivingTime"] += stop.get("drivingTime", 0) or 0
				report["sumServiceTime"] += stop.get("duration", 0) or 0
				report["sumChargedEnergy"] += stop.get("chargedEnergy", 0) or 0
				if stop_type == "pickup":
					has_pickup = True
				if stop_type in ("dropoff", "chaining", "unchaining") and stop.get("tripGuid"):
					report["sumCustomerDistance"] += stop.get("distance", 0) or 0
					report["sumCustomerDrivingTime"] += stop.get("drivingTime", 0) or 0
					report["sumCustomerConsumedEnergy"] += stop.get("consumedEnergy", 0) or 0
				if stop_type == "charging":
					report["chargingTime"] += stop.get("duration", 0) or 0
					report["sumChargingDrivingTime"] += stop.get("drivingTime", 0) or 0
					report["sumChargingDistance"] += stop.get("distance", 0) or 0
					report["sumChargingConsumedEnergy"] += stop.get("consumedEnergy", 0) or 0
			if has_pickup:
				used_cabs += 1
		report["counterToursWithServices"] = used_cabs

		requests = list(current_solution.demand_scenario.requests)
		rejected = sum(1 for req in requests if getattr(req, "sim_custom_reject", True))
		zero_report = {key: 0 for key in report}
		step_results = []
		for index, req in enumerate(requests):
			is_rejected = bool(getattr(req, "sim_custom_reject", True))
			vehicle_label = f"Cab{req.vehicle}" if getattr(req, "vehicle", None) is not None else ""
			tguid = f"trip_{int(req.id)}" if not is_rejected else ""
			step_results.append({
				"proposal": f"proposal_{int(req.id)}" if not is_rejected else "",
				"invalidRequest": bool(getattr(req, "sim_invalid", False) or False),
				"tripRequestResponse": {
					"bookingTransaction": f"booking_{int(req.id)}",
					"userGuid": str(int(req.id)),
					"successful": not is_rejected,
					"possibleStartLocation": None,
					"possibleTargetLocation": None,
					"earliestPossibleStart": None,
					"proposals": [] if is_rejected else [{
						"guid": f"proposal_{int(req.id)}",
						"vehicleLabel": vehicle_label,
						"cabStartLocation": loc(req.pu_lat, req.pu_lon),
						"cabTargetLocation": loc(req.do_lat, req.do_lon),
						"requestedStartLocation": loc(req.pu_lat, req.pu_lon),
						"requestedTargetLocation": loc(req.do_lat, req.do_lon),
						"pickupTime": iso(getattr(req, "sim_pu_time", None)),
						"dropoffTime": iso(getattr(req, "sim_do_time", None)),
						"costs": 0,
						"carpoolInformation": None,
						"proposalReleaseTime": iso(getattr(req, "register_time", None)),
					}],
				},
				"bookTripResponse": None if is_rejected else {"successful": True, "statusCode": "Successful", "createdTripGuid": tguid},
				"exception": None,
				"report": report if index == len(requests) - 1 else zero_report,
				"requestDuration": "00:00:00.0000000",
				"bookDuration": "00:00:00.0000000",
				"trip": None if is_rejected else {
					"tripGuid": tguid,
					"userGuid": str(int(req.id)),
					"creationTime": iso(getattr(req, "register_time", None)),
					"vehicleLabel": vehicle_label,
					"cabStartLocation": loc(req.pu_lat, req.pu_lon),
					"cabDropoffLocation": loc(req.do_lat, req.do_lon),
					"estimatedPickupTime": iso(getattr(req, "sim_pu_time", None)),
					"estimatedDropoffTime": iso(getattr(req, "sim_do_time", None)),
					"costs": 0,
					"carpoolInformation": None,
					"rampNeeded": bool(getattr(req, "need_ramp", False)),
					"requestedAdults": int(getattr(req, "num_persons", 1) or 1),
					"requestedChilds": 0,
					"luggage": 0,
					"maxCabs": 0,
					"personalPreferences": {
						"allowCarpooling": bool(getattr(req, "ride_sharing", False)),
						"toleratedDelayBefore": int(getattr(req, "tol_lower", 0) or 0),
						"toleratedDelayAfter": int(getattr(req, "tol_upper", 0) or 0),
					},
					"status": "Planned",
					"cabArrival": "0001-01-01T00:00:00.000Z",
					"customerCheckin": "0001-01-01T00:00:00.000Z",
					"customerCheckout": "0001-01-01T00:00:00.000Z",
					"modificationDate": iso(getattr(req, "register_time", None)),
					"expectedEnergyRequired": 0,
					"expectedDrivingTime": 0,
					"expectedDistance": 0,
				},
				"requestTime": iso(getattr(req, "register_time", None)),
			})

		served = max(0, len(requests) - rejected)
		return {
			"overview": {
				"successfulBookings": served,
				"requestsWithNoProposal": rejected,
				"requestsWithProposal": served,
				"failedRequests": 0,
				"simulationDuration": "00:00:00.0000000",
				"minRequestDuaration": "00:00:00.0000000",
				"maxRequestDuaration": "00:00:00.0000000",
				"sumRequestDuration": "00:00:00.0000000",
			},
			"startReport": zero_report,
			"finalReport": report,
			"validationResults": [],
			"stepResults": step_results,
		}

	prefix = build_prefix()
	paths = {
		"input_base_file": output_dir / f"{prefix}_input_base_file_{int(iteration)}.json",
		"input_req_file": output_dir / f"{prefix}_input_req_file_{int(iteration)}.json",
		"output_cab": output_dir / f"{prefix}_output_cab_{int(iteration)}.json",
		"output_pro": output_dir / f"{prefix}_output_pro_{int(iteration)}.json",
		"output_sim": output_dir / f"{prefix}_output_sim_{int(iteration)}.json",
		"past_entries_fleet": output_dir / f"{prefix}_past_entries_fleet_{int(iteration)}.json",
		"request_results_log": output_dir / f"{prefix}_request_results_log_{int(iteration)}.json",
	}
	cab_output = build_cab_output()
	pro_output = build_pro_output()
	sim_output = build_sim_output(cab_output)

	dump_json(paths["input_base_file"], current_solution.write_api_base_file(operations_area))
	dump_json(paths["input_req_file"], current_solution.write_api_req_file())
	dump_json(paths["output_cab"], cab_output)
	dump_json(paths["output_pro"], pro_output)
	dump_json(paths["output_sim"], sim_output)
	return paths

# ---------------------------------------------------------

def check_timeline():
	"""
	Check if there are errors in the temporal sequence of schedule entries
	"""
	print("\nCHECK TIMELINE")
	
	with open("past_entries.json") as f:
		data = json.load(f)
	
	# 1) within each entry: start <= end
	for i, (a, b, c, d, e, g, h, j, k) in enumerate(data):
		if a > b:
			print("Intra-entry error at index", i, ":", a, ">", b)
	
	# 2) between entries: previous end <= current start
	for i in range(1, len(data)):
		prev_end = data[i-1][1]
		curr_start = data[i][0]
		if prev_end > curr_start:
			print("Order error between", i-1, "and", i, ":", prev_end, ">", curr_start)
	
	print("\n")
	
	return

# ---------------------------------------------------------

def check_timeline_fleet(filename="past_entries_fleet.json"):
	"""
	Check if there are errors in the temporal sequence of schedule entries
	"""
	print("\nCHECK TIMELINE")

	with open(filename) as f:
		data = json.load(f)
	data = data.get("cabs", data)

	# Iterate per cab
	for cab_id, entries in data.items():
		# 1) Within each pair: start <= end
		for i, rec in enumerate(entries):
			a, b = rec[0], rec[1]
			if a > b:
				print("Intra-entry error for cab", cab_id, "index", i, ":", a, ">", b)

		# 2) Between pairs: previous end <= current start
		for i in range(1, len(entries)):
			prev_end = entries[i - 1][1]
			curr_start = entries[i][0]
			if prev_end > curr_start:
				print("Order error for cab", cab_id, "between", i - 1, "and", i, ":", prev_end, ">", curr_start)

	print("\n")
	return

# ---------------------------------------------------------

def check_energy_bounds(lb, ub):
	"""
	Check for energy bounds outside the specified interval within schedule entries
	"""
	print("CHECK ENERGY")
	
	with open("past_entries.json", "r") as f:
		data = json.load(f)
	
	for i, entry in enumerate(data):
		start = entry[3]
		end = entry[4]
		
		# check bounds and values before comparing
		if lb is None or ub is None:
			print("Energy value missing")
		if not (lb <= start <= ub and lb <= end <= ub):
			print("Energy value out of bounds at", i)
	
	print("\n")
	
	return

# ---------------------------------------------------------

def check_energy_bounds_fleet(lb, ub, filename="past_entries_fleet.json"):
	"""
	Check for energy bounds outside the specified interval within schedule entries
	"""
	print("CHECK ENERGY")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	for cab_id, entries in data.items():
		for i, entry in enumerate(entries):
			start = entry[3]
			end = entry[4]

			# Check if bounds exist
			if lb is None or ub is None:
				print("Energy value missing for cab", cab_id)
				continue

			if not (lb <= start <= ub and lb <= end <= ub):
				print(
					"Energy value out of bounds for cab",
					cab_id,
					"Index",
					i,
					":", start, end
				)

	print("\n")
	return

# ---------------------------------------------------------

def check_energy_continuity_fleet(eps=1e-6, filename="past_entries_fleet.json"):
	"""
	Check if end_energy of one entry matches start_energy of the next entry.
	"""
	print("CHECK ENERGY CONTINUITY")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	for cab_id, entries in data.items():
		for i in range(1, len(entries)):
			prev_end = entries[i - 1][4]
			curr_start = entries[i][3]

			if prev_end is None or curr_start is None:
				print(
					"Energy value missing for cab",
					cab_id,
					"between",
					i - 1,
					"and",
					i,
					":",
					prev_end,
					curr_start,
				)
				continue

			if abs(float(prev_end) - float(curr_start)) > eps:
				print(
					"Energy continuity error for cab",
					cab_id,
					"between",
					i - 1,
					"and",
					i,
					":",
					prev_end,
					"!=",
					curr_start,
				)

	print("\n")
	return

# ---------------------------------------------------------

def check_location_continuity_fleet(max_gap_m=5.0, filename="past_entries_fleet.json"):
	"""
	Check if end location of one entry matches start location of the next entry.
	"""
	print("CHECK LOCATION CONTINUITY")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	for cab_id, entries in data.items():
		for i in range(1, len(entries)):
			prev = entries[i - 1]
			curr = entries[i]

			if len(prev) < 16 or len(curr) < 16:
				print("Position fields missing for cab", cab_id, "between", i - 1, "and", i)
				continue

			prev_end_lat = prev[14]
			prev_end_lon = prev[15]
			curr_start_lat = curr[12]
			curr_start_lon = curr[13]

			if (
				prev_end_lat is None
				or prev_end_lon is None
				or curr_start_lat is None
				or curr_start_lon is None
			):
				print(
					"Position value missing for cab",
					cab_id,
					"between",
					i - 1,
					"and",
					i,
				)
				continue

			gap_m = haversine_distance(
				float(prev_end_lat),
				float(prev_end_lon),
				float(curr_start_lat),
				float(curr_start_lon),
			)

			if gap_m > float(max_gap_m):
				print(
					"Position continuity error for cab",
					cab_id,
					"between",
					i - 1,
					"and",
					i,
					":",
					round(gap_m, 2),
					"m",
				)

	print("\n")
	return

# ---------------------------------------------------------

def check_entry_distance_plausibility_fleet(
	filename="past_entries_fleet.json",
	material_gap_m=5.0,
	straight_line_tolerance_m=1.0,
):
	"""
	Sanity checks between an entry's own start/end location and its own
	recorded distance_m - independent of energy, so it still catches a wrong
	distance_m even when the energy checks above happen to look fine.
	- coordinates differ materially (haversine gap > material_gap_m) but
	  distance_m is missing or ~0: the entry claims to not have moved/have no
	  distance while its own recorded endpoints say otherwise.
	- distance_m is shorter than the straight-line (haversine) distance between
	  its own endpoints, beyond straight_line_tolerance_m: real road distance
	  can never be less than the crow-flies distance.
	"""
	print("CHECK ENTRY DISTANCE PLAUSIBILITY")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	for cab_id, entries in data.items():
		for i, entry in enumerate(entries):
			if len(entry) <= 18:
				continue

			s_lat, s_lon, e_lat, e_lon = entry[12], entry[13], entry[14], entry[15]
			distance_m = entry[18]
			if None in (s_lat, s_lon, e_lat, e_lon):
				continue

			gap_m = haversine_distance(float(s_lat), float(s_lon), float(e_lat), float(e_lon))

			if gap_m > material_gap_m and (distance_m is None or float(distance_m) <= material_gap_m):
				print(
					"Coordinates differ materially (",
					round(gap_m, 2),
					"m) but distance_m is missing/near-zero for cab",
					cab_id,
					"index",
					i,
					":",
					distance_m,
				)
				continue

			if distance_m is not None:
				shortfall = gap_m - float(distance_m)
				if shortfall > straight_line_tolerance_m:
					print(
						"distance_m shorter than straight-line gap for cab",
						cab_id,
						"index",
						i,
						": distance_m=",
						distance_m,
						"< straight-line",
						round(gap_m, 2),
						"m",
					)

	print("\n")
	return

# ---------------------------------------------------------

def check_entry_energy_deltas_fleet(
	eps=1e-6,
	filename="past_entries_fleet.json",
	stationary_distance_m=1e-6,
	cab_energy_wh_per_m=None,
	consumption_tolerance=1e-3,
	charge_rate_tolerance=5.0,
):
	"""
	Simple per-entry energy sanity checks.
	- ChP/PT: end >= start and optional delta ~= charge_amount
	- ChP/PT: charge_amount shouldn't exceed what the station/Pro's own power
	  rating could deliver over the entry's active (non-service) duration -
	  charge_amount == delta only checks two recorded fields agree with each
	  other, not that the amount is physically achievable.
	- other entries: end <= start
	- other entries whose own distance_m is ~0 (within stationary_distance_m):
	  |delta| ~= 0 regardless of duration. This model has no time-based idle
	  drain, only distance-based consumption, so an entry that didn't actually
	  drive can't legitimately change energy in either direction.
	  Deliberately keyed off distance_m, not the start/end coordinate gap:
	  coordinates being close doesn't mean nothing was driven (a real route can
	  leave from and return to nearby points, e.g. one-way streets forcing a
	  detour - confirmed on real LM entries where a ~0m coordinate gap still
	  had a genuine 300+m routed leg). distance_m is the actual quantity that
	  determines energy, so it's the right thing to gate on; the case where
	  distance_m itself disagrees with the entry's coordinates is separately
	  covered by check_entry_distance_plausibility_fleet.
	  stationary_distance_m is deliberately tiny (not a GPS/snapping-scale
	  tolerance like a few meters): the router's own same-point shortcut
	  always returns an exact 0.0 distance_m, so any real, non-trivial routed
	  leg - even a very short one - has a real, correspondingly small energy
	  cost that the distance-magnitude check below already accounts for. Only
	  a distance_m indistinguishable from literal zero should be held to
	  "delta must be exactly 0" instead.
	- other entries with a known distance_m and cab_energy_wh_per_m: delta ~=
	  -(distance_m * cab_energy_wh_per_m) - the router's own flat per-meter rate,
	  which is what actually produced every entry's real energy_wh (see
	  custom_simulation.py's call site for why this isn't a Cab's own
	  default_energy_consumption_per_m, a planning-estimate-only field).
	"""
	print("CHECK ENTRY ENERGY DELTAS")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	for cab_id, entries in data.items():
		for i, entry in enumerate(entries):
			typ = entry[2]
			start = entry[3]
			end = entry[4]

			if start is None or end is None:
				print("Energy value missing for cab", cab_id, "index", i, ":", start, end)
				continue

			start_f = float(start)
			end_f = float(end)
			delta = end_f - start_f

			if typ in ("ChP", "PT"):
				if delta < -eps:
					print("Negative charging-entry energy delta for cab", cab_id, "index", i, ":", delta)

				charge_amount = entry[10] if len(entry) > 10 else None
				if charge_amount is not None:
					try:
						charge_amount_f = float(charge_amount)
					except Exception:
						charge_amount_f = None

					if charge_amount_f is not None and abs(delta - charge_amount_f) > eps:
						print(
							"Charging-entry delta != charge_amount for cab",
							cab_id,
							"Index",
							i,
							":",
							delta,
							"!=",
							charge_amount_f,
						)

					service_time = entry[9] if len(entry) > 9 else None
					charging_power = entry[11] if len(entry) > 11 else None
					if charge_amount_f is not None and service_time is not None and charging_power is not None:
						try:
							active_duration_s = float(entry[1]) - float(entry[0]) - float(service_time)
							charging_power_f = float(charging_power)
						except Exception:
							active_duration_s = None
							charging_power_f = None

						if active_duration_s is not None and charging_power_f is not None and active_duration_s > 0:
							max_possible = charging_power_f * active_duration_s / 3600.0
							excess = charge_amount_f - max_possible
							if excess > charge_rate_tolerance:
								print(
									"Charging-entry charge_amount exceeds station/Pro power limit for cab",
									cab_id,
									"index",
									i,
									":",
									charge_amount_f,
									"> max_possible",
									max_possible,
									"(power=",
									charging_power_f,
									"W, active_duration=",
									active_duration_s,
									"s)",
								)
			else:
				if delta > eps:
					print("Positive non-ChP energy delta for cab", cab_id, "index", i, ":", delta)

				if len(entry) > 18 and entry[18] is not None:
					distance_m = float(entry[18])
					if distance_m <= stationary_distance_m and abs(delta) > eps:
						print(
							"Nonzero energy delta on a stationary entry (distance_m",
							round(distance_m, 2),
							") for cab",
							cab_id,
							"index",
							i,
							":",
							delta,
						)

				if cab_energy_wh_per_m is not None and len(entry) > 18 and entry[18] is not None:
					distance_m = float(entry[18])
					expected_delta = -(distance_m * float(cab_energy_wh_per_m))
					if abs(delta - expected_delta) > consumption_tolerance:
						print(
							"Energy delta doesn't match distance * rate for cab",
							cab_id,
							"index",
							i,
							":",
							delta,
							"!= expected",
							expected_delta,
							"(distance_m=",
							distance_m,
							")",
						)

	print("\n")
	return

# ---------------------------------------------------------

def check_timewindows():
	"""
	Check if all CT-schedule entries are conform with timewindows
	"""
	print("CHECK TIME-WINDOWS")
	
	with open("past_entries.json", "r") as f:
		data = json.load(f)
	
	for i, entry in enumerate(data):
		
		if entry[2]=="CT":
			t_start = entry[0]
			t_end = entry[1]
			lb = entry[7]
			ub = entry[8]
			
			if entry[6]:
				if not (lb <= t_start <= ub):
					print("Time window violation at", i)
			else:
				if not (lb <= t_end <= ub):
					print("Time window violation at", i)
	
	print("\n")
	
	return

# ---------------------------------------------------------

def check_timewindows_fleet(filename="past_entries_fleet.json"):
	"""
	Check if all CT-schedule entries are conform with timewindows
	"""
	print("CHECK TIME-WINDOWS")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	for cab_id, entries in data.items():
		for i, entry in enumerate(entries):

			if entry[2] == "CT":
				t_start = entry[0]
				t_end = entry[1]
				lb = entry[7]
				ub = entry[8]
				half_svc = (entry[9] or 0) / 2

				if entry[6]:  # pickup window: cab arrives >= lb, boarding ends <= ub
					if not (lb <= t_start and t_start + half_svc <= ub):
						print("Time window violation for cab", cab_id, "index", i)
				else:  # dropoff window: alighting starts >= lb, cab departs <= ub
					if not (lb <= t_end - half_svc and t_end <= ub):
						print("Time window violation for cab", cab_id, "index", i)

	print("\n")
	return

# ---------------------------------------------------------

def create_fleet_movement_gif(
	filename="past_entries_fleet.json",
	out_gif="fleet_movement.gif",
	step_seconds=60,
	fps=5,
	router_cab=None,
	router_pro=None,
	check_drive_times=True,
	drive_time_tol_s=10.0,
	show_road_graph=True,
	max_frames=400,
	dpi=80,
):
	"""
	Create a gif animating cab and Pro vehicle positions over time.

	Reads from a past_entries_fleet.json with {"cabs": {...}, "pros": {...}}.
	Cab entries use router_cab for routing; Pro entries and cab PT entries
	(where the cab follows the Pro's road) use router_pro.
	Convoy links are drawn as dashed lines between a cab and its Pro during PT.

	Frames are rendered and written incrementally (via imageio) to avoid
	accumulating the entire animation in memory. max_frames caps the total
	number of frames regardless of step_seconds / simulation length.
	"""
	from datetime import datetime
	import os
	from matplotlib.collections import LineCollection

	with open(filename, "r") as f:
		raw = json.load(f)

	cab_data = raw.get("cabs", raw)
	pro_data = raw.get("pros", {})

	# Entry types that involve driving (worth routing)
	cab_moving_types = {"CA", "CT", "ChA", "FM", "LM", "PT", "PA", "PlA"}
	pro_moving_types = {"ST", "DT", "RA"}

	check_issues = 0
	route_cache = {}

	# Track tuple: (s_time, e_time, typ, service_time, s_lat, s_lon, e_lat, e_lon, poly, cum, total, convoy_pro_key)
	# convoy_pro_key: str key into pro_tracks for PT cab entries, else None

	def cached_route(router, s_lat, s_lon, e_lat, e_lon, label, idx):
		nonlocal check_issues
		if router is None:
			return None
		key = (id(router), round(s_lat, 6), round(s_lon, 6), round(e_lat, 6), round(e_lon, 6))
		if key in route_cache:
			return route_cache[key]
		try:
			res = router.shortest_path((s_lat, s_lon), (e_lat, e_lon))
		except Exception as err:
			print(f"[gif] routing failed for {label} entry {idx}: {err}")
			res = None
		route_cache[key] = res
		return res

	def build_polyline(route_res, router, s_lat, s_lon, e_lat, e_lon):
		nodes = route_res.get("path_osm_nodes", []) if route_res is not None else []
		if router is None or not nodes:
			return [(s_lat, s_lon), (e_lat, e_lon)]
		poly = [(s_lat, s_lon)]
		for node_id in nodes:
			node = router.G.nodes.get(node_id)
			if node is None:
				continue
			poly.append((float(node["y"]), float(node["x"])))
		poly.append((e_lat, e_lon))
		return poly

	def cumulative_distances(poly):
		cum = [0.0]
		for idx in range(1, len(poly)):
			p0, p1 = poly[idx - 1], poly[idx]
			cum.append(cum[-1] + haversine_distance(p0[0], p0[1], p1[0], p1[1]))
		return cum, cum[-1]

	def interpolate_on_polyline(poly, cum, total, frac):
		if not poly:
			return None
		if len(poly) == 1 or total <= 1e-9:
			return poly[-1]
		target = min(max(0.0, frac), 1.0) * total
		for idx in range(1, len(cum)):
			if target <= cum[idx]:
				seg = max(1e-9, cum[idx] - cum[idx - 1])
				r = (target - cum[idx - 1]) / seg
				lat = poly[idx - 1][0] + r * (poly[idx][0] - poly[idx - 1][0])
				lon = poly[idx - 1][1] + r * (poly[idx][1] - poly[idx - 1][1])
				return (lat, lon)
		return poly[-1]

	def build_cab_track(cab_id, entries):
		nonlocal check_issues
		label = f"Cab{cab_id}"
		track = []
		for i, entry in enumerate(entries):
			if len(entry) < 16:
				print(f"[gif] missing fields for {label} entry {i}")
				continue
			s_time = float(entry[0])
			e_time = float(entry[1])
			typ = str(entry[2])
			service_time = float(entry[9]) if entry[9] is not None else 0.0
			s_lat, s_lon = float(entry[12]), float(entry[13])
			e_lat, e_lon = float(entry[14]), float(entry[15])
			linked_pro_id = entry[17] if len(entry) > 17 else None

			if s_lat < 0 or s_lon < 0 or e_lat < 0 or e_lon < 0:
				continue

			# PT entries follow the Pro's road; all others use router_cab
			active_router = router_pro if typ == "PT" else router_cab
			route_res = None
			if typ in cab_moving_types:
				route_res = cached_route(active_router, s_lat, s_lon, e_lat, e_lon, label, i)

			if check_drive_times and route_res is not None and typ in cab_moving_types:
				dur = max(0.0, e_time - s_time)
				drive_budget = max(0.0, dur - max(0.0, service_time)) if typ == "CT" else dur
				routed_time = float(route_res.get("time_s", 0.0))
				if abs(routed_time - drive_budget) > float(drive_time_tol_s):
					check_issues += 1
					print(f"[gif-check] drive-time mismatch {label} entry {i} {typ} "
						  f"budget_s={round(drive_budget,2)} routed_s={round(routed_time,2)}")

			poly = build_polyline(route_res, active_router, s_lat, s_lon, e_lat, e_lon)
			cum, total = cumulative_distances(poly)
			convoy_pro_key = str(linked_pro_id) if typ == "PT" and linked_pro_id is not None else None
			track.append((s_time, e_time, typ, service_time, s_lat, s_lon, e_lat, e_lon, poly, cum, total, convoy_pro_key))

		track.sort(key=lambda x: (x[0], x[1]))
		return track

	def build_pro_track(pro_id, entries):
		label = f"Pro{pro_id}"
		track = []
		for i, entry in enumerate(entries):
			if len(entry) < 7:
				print(f"[gif] missing fields for {label} entry {i}")
				continue
			s_time = float(entry[0])
			e_time = float(entry[1])
			typ = str(entry[2])
			s_lat, s_lon = float(entry[3]), float(entry[4])
			e_lat, e_lon = float(entry[5]), float(entry[6])

			if s_lat < 0 or s_lon < 0 or e_lat < 0 or e_lon < 0:
				continue

			route_res = None
			if typ in pro_moving_types:
				route_res = cached_route(router_pro, s_lat, s_lon, e_lat, e_lon, label, i)

			poly = build_polyline(route_res, router_pro, s_lat, s_lon, e_lat, e_lon)
			cum, total = cumulative_distances(poly)
			track.append((s_time, e_time, typ, 0.0, s_lat, s_lon, e_lat, e_lon, poly, cum, total, None))

		track.sort(key=lambda x: (x[0], x[1]))
		return track

	cab_tracks = {}
	pro_tracks = {}
	all_lats = []
	all_lons = []
	t_min = None
	t_max = None

	total_entries = sum(len(v) for v in cab_data.values()) + sum(len(v) for v in pro_data.values())
	print(f"[gif] preparing tracks for {total_entries} entries ({len(cab_data)} cabs, {len(pro_data)} pros)")

	for cab_id, entries in cab_data.items():
		print(f"[gif] routing/prep Cab{cab_id}, {len(entries)} entries")
		track = build_cab_track(cab_id, entries)
		if track:
			cab_tracks[str(cab_id)] = track
			for t in track:
				all_lats.extend([t[4], t[6]])
				all_lons.extend([t[5], t[7]])
				if t_min is None or t[0] < t_min: t_min = t[0]
				if t_max is None or t[1] > t_max: t_max = t[1]

	for pro_id, entries in pro_data.items():
		print(f"[gif] routing/prep Pro{pro_id}, {len(entries)} entries")
		track = build_pro_track(pro_id, entries)
		if track:
			pro_tracks[str(pro_id)] = track
			for t in track:
				all_lats.extend([t[4], t[6]])
				all_lons.extend([t[5], t[7]])
				if t_min is None or t[0] < t_min: t_min = t[0]
				if t_max is None or t[1] > t_max: t_max = t[1]

	if t_min is None or not all_lats:
		print("[gif] no valid movement data found")
		return

	if step_seconds <= 0:
		step_seconds = 60
	frames_ts = []
	t = t_min
	while t <= t_max:
		frames_ts.append(t)
		t += step_seconds
	if frames_ts[-1] < t_max:
		frames_ts.append(t_max)

	if max_frames is not None and len(frames_ts) > max_frames:
		step = len(frames_ts) / max_frames
		frames_ts = [frames_ts[int(i * step)] for i in range(max_frames)]
		frames_ts.append(t_max)
		print(f"[gif] capped to {len(frames_ts)} frames (max_frames={max_frames})")

	print(f"[gif] total frames {len(frames_ts)}, step_seconds={step_seconds}, fps={fps}")

	lat_min, lat_max = min(all_lats), max(all_lats)
	lon_min, lon_max = min(all_lons), max(all_lons)
	lat_pad = max(1e-4, (lat_max - lat_min) * 0.05)
	lon_pad = max(1e-4, (lon_max - lon_min) * 0.05)
	lat_mid = (lat_min + lat_max) / 2.0
	cos_lat = max(1e-6, math.cos(math.radians(lat_mid)))

	fig, ax = plt.subplots(figsize=(10, 10), dpi=dpi)
	ax.set_xlim(lon_min - lon_pad, lon_max + lon_pad)
	ax.set_ylim(lat_min - lat_pad, lat_max + lat_pad)
	ax.set_aspect(1.0 / cos_lat, adjustable="box")
	ax.set_xlabel("Longitude")
	ax.set_ylabel("Latitude")
	ax.set_title("Fleet movement")
	ax.grid(True, linestyle="--", alpha=0.3)

	if show_road_graph and router_cab is not None and hasattr(router_cab, "G"):
		edge_x, edge_y = [], []
		for u, v, edata in router_cab.G.edges(data=True):
			geom = edata.get("geometry")
			if geom is not None:
				xs, ys = geom.xy
				edge_x.extend(list(xs)); edge_y.extend(list(ys))
				edge_x.append(None); edge_y.append(None)
			else:
				x1, y1 = router_cab.G.nodes[u]["x"], router_cab.G.nodes[u]["y"]
				x2, y2 = router_cab.G.nodes[v]["x"], router_cab.G.nodes[v]["y"]
				edge_x.extend([x1, x2, None]); edge_y.extend([y1, y2, None])
		ax.plot(edge_x, edge_y, color="lightgray", linewidth=0.35, alpha=0.35, zorder=0)

	def sort_num(x):
		return int(x) if str(x).isdigit() else x

	cab_ids = sorted(cab_tracks.keys(), key=sort_num)
	pro_ids = sorted(pro_tracks.keys(), key=sort_num)
	cab_colors = plt.cm.get_cmap("tab10", max(1, len(cab_ids)))
	pro_colors = plt.cm.get_cmap("Set2", max(1, len(pro_ids)))

	scat_cabs = ax.scatter([], [], s=45, marker="o", zorder=4)
	scat_pros = ax.scatter([], [], s=100, marker="D", zorder=4)
	convoy_lines = LineCollection([], linewidths=1.5, colors="dimgray", linestyles="--", alpha=0.7, zorder=3)
	ax.add_collection(convoy_lines)

	cab_labels = [ax.text(0, 0, "", fontsize=7, zorder=5) for _ in cab_ids]
	pro_labels = [ax.text(0, 0, "", fontsize=7, fontweight="bold", zorder=5) for _ in pro_ids]

	# legend proxies
	ax.scatter([], [], s=45, marker="o", color="steelblue", label="Cab")
	ax.scatter([], [], s=100, marker="D", color="tomato", label="Pro")
	ax.plot([], [], color="dimgray", linestyle="--", linewidth=1.5, label="Convoy")
	ax.legend(loc="upper right", fontsize=8)

	title_text = ax.text(0.02, 1.02, "", transform=ax.transAxes, ha="left", va="bottom")

	def position_at_time(track, ts):
		prev = None
		for s_time, e_time, typ, service_time, s_lat, s_lon, e_lat, e_lon, poly, cum, total, _ in track:
			if s_time <= ts <= e_time:
				dur = max(1e-9, e_time - s_time)
				if typ == "CT" and service_time > 0.0:
					side = max(0.0, service_time / 2.0)
					move_start = s_time + side
					move_end = e_time - side
					if ts <= move_start:
						return (s_lat, s_lon)
					if ts >= move_end:
						return (e_lat, e_lon)
					frac = (ts - move_start) / max(1e-9, move_end - move_start)
					return interpolate_on_polyline(poly, cum, total, frac)
				frac = (ts - s_time) / dur
				return interpolate_on_polyline(poly, cum, total, frac)
			if e_time < ts:
				prev = (e_lat, e_lon)
			if ts < s_time:
				return prev if prev is not None else (s_lat, s_lon)
		return prev

	def active_convoy_pro_key(track, ts):
		for s_time, e_time, _, _, _, _, _, _, _, _, _, convoy_pro_key in track:
			if s_time <= ts <= e_time and convoy_pro_key is not None:
				return convoy_pro_key
		return None

	def update(frame_idx):
		ts = frames_ts[frame_idx]
		positions = {}

		cab_xs, cab_ys, cab_cs = [], [], []
		for idx, cid in enumerate(cab_ids):
			pos = position_at_time(cab_tracks[cid], ts)
			if pos is None:
				cab_xs.append(float("nan"))
				cab_ys.append(float("nan"))
				cab_labels[idx].set_text("")
			else:
				positions[("cab", cid)] = pos
				cab_xs.append(pos[1]); cab_ys.append(pos[0])
				cab_cs.append(cab_colors(idx))
				cab_labels[idx].set_position((pos[1], pos[0]))
				cab_labels[idx].set_text(str(cid))
		scat_cabs.set_offsets(list(zip(cab_xs, cab_ys)))
		scat_cabs.set_color(cab_cs or ["steelblue"])

		pro_xs, pro_ys, pro_cs = [], [], []
		for idx, pid in enumerate(pro_ids):
			pos = position_at_time(pro_tracks[pid], ts)
			if pos is None:
				pro_xs.append(float("nan"))
				pro_ys.append(float("nan"))
				pro_labels[idx].set_text("")
			else:
				positions[("pro", pid)] = pos
				pro_xs.append(pos[1]); pro_ys.append(pos[0])
				pro_cs.append(pro_colors(idx))
				pro_labels[idx].set_position((pos[1], pos[0]))
				pro_labels[idx].set_text(f"P{pid}")
		scat_pros.set_offsets(list(zip(pro_xs, pro_ys)))
		scat_pros.set_color(pro_cs or ["tomato"])

		segments = []
		for cid in cab_ids:
			pro_key = active_convoy_pro_key(cab_tracks[cid], ts)
			cab_pos = positions.get(("cab", cid))
			pro_pos = positions.get(("pro", pro_key)) if pro_key else None
			if cab_pos and pro_pos:
				segments.append([(cab_pos[1], cab_pos[0]), (pro_pos[1], pro_pos[0])])
		convoy_lines.set_segments(segments)

		title_text.set_text(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"))
		return [scat_cabs, scat_pros, convoy_lines, title_text, *cab_labels, *pro_labels]

	import numpy as np

	base_name, ext = os.path.splitext(out_gif)
	tmp_gif = (base_name + ".partial" + ext) if ext.lower() == ".gif" else (out_gif + ".partial.gif")
	total_frames = len(frames_ts)
	print(f"[gif] start encoding {out_gif} ({total_frames} frames at {dpi} dpi)")

	try:
		try:
			import imageio

			fig.canvas.draw()
			with imageio.get_writer(tmp_gif, mode="I", fps=fps, loop=0) as writer:
				for fi in range(total_frames):
					update(fi)
					fig.canvas.draw()
					buf = fig.canvas.buffer_rgba()
					w, h = fig.canvas.get_width_height()
					frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()
					writer.append_data(frame)
					if fi == 0 or (fi + 1) % 25 == 0 or (fi + 1) == total_frames:
						print(f"[gif] encode progress {fi + 1}/{total_frames}")

		except ImportError:
			from matplotlib.animation import FuncAnimation, PillowWriter

			print("[gif] imageio not available, falling back to PillowWriter (no resource cap)")
			ani = FuncAnimation(fig, update, frames=total_frames, interval=1000 / max(1, fps), blit=False)
			ani.save(
				tmp_gif,
				writer=PillowWriter(fps=fps),
				progress_callback=lambda fi, ft: (
					print(f"[gif] encode progress {fi + 1}/{ft}")
					if (fi == 0 or (fi + 1) % 25 == 0 or (fi + 1) == ft) else None
				),
			)

		os.replace(tmp_gif, out_gif)
		print(f"[gif] saved: {out_gif}")
		if check_drive_times:
			print(f"[gif-check] drive-time mismatches: {check_issues}")
	except Exception:
		if os.path.exists(tmp_gif):
			os.remove(tmp_gif)
		raise
	finally:
		plt.close(fig)

# ---------------------------------------------------------

def visualize_timeline_plotly_overlay_soc(
	t_start,
	t_end,
	filename="past_entries_fleet.json",
	height=900,
	pad_seconds=900,
	bar_width=0.8,
	soc_color="black",
	ct_service_s=60,
):
	"""Render timeline bars with overlaid normalized SoC curves per cab."""
	from zoneinfo import ZoneInfo
	from datetime import datetime, timedelta
	import json

	import plotly.graph_objects as go

	TZ = ZoneInfo("Europe/Berlin")

	with open(filename, "r") as f:
		data = json.load(f)
	data = data.get("cabs", data)

	def sort_key(x):
		s = str(x)
		try:
			return (0, int(s))
		except Exception:
			return (1, s)

	cab_ids = sorted((str(k) for k in data.keys()), key=sort_key)
	cab_to_y = {cab_id: i for i, cab_id in enumerate(cab_ids)}

	colors = {
		"CA": "blue",
		"CT": "green",
		"ChA": "red",
		"ChP": "yellow",
		"FM": "#9467bd",
		"PT": "#ff7f0e",
		"LM": "#17becf",
	}

	fig = go.Figure()

	for cab_id in cab_ids:
		entries = data.get(cab_id, [])
		if not entries:
			continue

		y0 = cab_to_y[cab_id]

		bases = []
		widths_ms = []
		ys = []
		ws = []
		bar_colors = []
		hover_text = []

		soc_x = []
		soc_y = []
		soc_hover = []

		first_soc = entries[0][3]
		try:
			first_soc_f = float(first_soc)
		except Exception:
			first_soc_f = 0.0

		denom = first_soc_f if first_soc_f > 0.0 else 1.0
		y_top = y0 + (bar_width / 2.0)

		def soc_to_y(soc_value):
			try:
				v = float(soc_value)
			except Exception:
				v = 0.0
			norm = max(0.0, v / denom)
			# SoC goes down: norm=1 at y_top, norm=0 at y_top-bar_width
			return y_top - norm * bar_width, norm

		for entry in entries:
			start_ts = entry[0]
			end_ts = entry[1]
			typ = entry[2]
			soc_start = entry[3]
			soc_end = entry[4]
			service_time_raw = entry[9] if len(entry) > 9 else None
			charge_amount_raw = entry[10] if len(entry) > 10 else None
			charge_power_raw = entry[11] if len(entry) > 11 else None

			start_dt = datetime.fromtimestamp(float(start_ts), tz=TZ)
			end_dt = datetime.fromtimestamp(float(end_ts), tz=TZ)

			dur_ms = (end_dt - start_dt).total_seconds() * 1000.0
			if dur_ms < 0:
				dur_ms = 0.0

			bases.append(start_dt)
			widths_ms.append(dur_ms)
			ys.append(y0)
			ws.append(bar_width)
			bar_colors.append(colors.get(typ, "gray"))

			hover_text.append(
				"cab_id=%s<br>type=%s<br>start=%s<br>end=%s<br>soc_start=%s<br>soc_end=%s<br>service_time=%s<br>charge_amount=%s<br>charge_power_w=%s"
				% (
					cab_id,
					typ,
					start_dt.isoformat(),
					end_dt.isoformat(),
					str(soc_start),
					str(soc_end),
					str(service_time_raw),
					str(charge_amount_raw),
					str(charge_power_raw),
				)
			)

			y_s, norm_s = soc_to_y(soc_start)
			y_e, norm_e = soc_to_y(soc_end)

			# Build SoC curve points
			if typ == "CT":
				# CT stores total boarding+unboarding service, so use half per side if present
				if service_time_raw is not None:
					try:
						svc_seconds = max(0.0, float(service_time_raw) / 2.0)
					except Exception:
						svc_seconds = float(ct_service_s)
				else:
					svc_seconds = float(ct_service_s)

				svc = timedelta(seconds=svc_seconds)
				move_start = start_dt + svc
				move_end = end_dt - svc

				if move_end > move_start:
					# Flat during service at start
					soc_x.append(start_dt)
					soc_y.append(y_s)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
						% (cab_id, start_dt.isoformat(), str(soc_start), norm_s)
					)

					soc_x.append(move_start)
					soc_y.append(y_s)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (service) (norm=%.3f)"
						% (cab_id, move_start.isoformat(), str(soc_start), norm_s)
					)

					# Linear during movement (encoded by just connecting endpoints)
					soc_x.append(move_end)
					soc_y.append(y_e)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (move end) (norm=%.3f)"
						% (cab_id, move_end.isoformat(), str(soc_end), norm_e)
					)

					# Flat during service at end
					soc_x.append(end_dt)
					soc_y.append(y_e)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (service) (norm=%.3f)"
						% (cab_id, end_dt.isoformat(), str(soc_end), norm_e)
					)
				else:
					# Too short for 2*service; fall back to simple linear
					soc_x.append(start_dt)
					soc_y.append(y_s)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
						% (cab_id, start_dt.isoformat(), str(soc_start), norm_s)
					)
					soc_x.append(end_dt)
					soc_y.append(y_e)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
						% (cab_id, end_dt.isoformat(), str(soc_end), norm_e)
					)
			elif typ == "ChP":
				# ChP stores total plug-in+plug-off service; split per side for plotting
				try:
					chp_side_service_s = (max(0.0, float(service_time_raw)) / 2.0) if service_time_raw is not None else 0.0
				except Exception:
					chp_side_service_s = 0.0

				try:
					charge_amount = float(charge_amount_raw) if charge_amount_raw is not None else 0.0
				except Exception:
					charge_amount = 0.0

				try:
					charge_power_w = float(charge_power_raw) if charge_power_raw is not None else None
				except Exception:
					charge_power_w = None

				active_start = start_dt + timedelta(seconds=chp_side_service_s)
				active_end = end_dt - timedelta(seconds=chp_side_service_s)

				if active_end > active_start:
					active_duration_s = (active_end - active_start).total_seconds()
					if charge_amount > 0.0 and charge_power_w is not None and charge_power_w > 0.0:
						charging_duration_s = min(active_duration_s, (charge_amount / charge_power_w) * 3600.0)
					elif charge_amount > 0.0:
						# legacy file without exported station power: keep previous full-active charging behavior
						charging_duration_s = active_duration_s
					else:
						charging_duration_s = 0.0

					charge_end = active_start + timedelta(seconds=charging_duration_s)

					# start point
					soc_x.append(start_dt)
					soc_y.append(y_s)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (plug-in/idle) (norm=%.3f)"
						% (cab_id, start_dt.isoformat(), str(soc_start), norm_s)
					)

					# end of plug-in service
					soc_x.append(active_start)
					soc_y.append(y_s)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (charging starts) (norm=%.3f)"
						% (cab_id, active_start.isoformat(), str(soc_start), norm_s)
					)

					if charging_duration_s > 0.0:
						# charging phase until charge_amount is reached
						soc_x.append(charge_end)
						soc_y.append(y_e)
						soc_hover.append(
							"cab_id=%s<br>time=%s<br>soc=%s (charging ends) (norm=%.3f)"
							% (cab_id, charge_end.isoformat(), str(soc_end), norm_e)
						)

						# remaining active ChP time is idle flat
						soc_x.append(active_end)
						soc_y.append(y_e)
						soc_hover.append(
							"cab_id=%s<br>time=%s<br>soc=%s (idle after charging) (norm=%.3f)"
							% (cab_id, active_end.isoformat(), str(soc_end), norm_e)
						)

						soc_x.append(end_dt)
						soc_y.append(y_e)
						soc_hover.append(
							"cab_id=%s<br>time=%s<br>soc=%s (plug-off/idle) (norm=%.3f)"
							% (cab_id, end_dt.isoformat(), str(soc_end), norm_e)
						)
					else:
						# no effective charging in this ChP: keep SoC flat throughout
						soc_x.append(active_end)
						soc_y.append(y_s)
						soc_hover.append(
							"cab_id=%s<br>time=%s<br>soc=%s (no charging) (norm=%.3f)"
							% (cab_id, active_end.isoformat(), str(soc_start), norm_s)
						)

						soc_x.append(end_dt)
						soc_y.append(y_s)
						soc_hover.append(
							"cab_id=%s<br>time=%s<br>soc=%s (no charging) (norm=%.3f)"
							% (cab_id, end_dt.isoformat(), str(soc_start), norm_s)
						)
				else:
					# too short for explicit service windows; fall back to direct transition
					soc_x.append(start_dt)
					soc_y.append(y_s)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
						% (cab_id, start_dt.isoformat(), str(soc_start), norm_s)
					)
					soc_x.append(end_dt)
					soc_y.append(y_e)
					soc_hover.append(
						"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
						% (cab_id, end_dt.isoformat(), str(soc_end), norm_e)
					)
			else:
				# Non-CT: simple linear start -> end
				soc_x.append(start_dt)
				soc_y.append(y_s)
				soc_hover.append(
					"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
					% (cab_id, start_dt.isoformat(), str(soc_start), norm_s)
				)
				soc_x.append(end_dt)
				soc_y.append(y_e)
				soc_hover.append(
					"cab_id=%s<br>time=%s<br>soc=%s (norm=%.3f)"
					% (cab_id, end_dt.isoformat(), str(soc_end), norm_e)
				)

		fig.add_trace(
			go.Bar(
				name=cab_id,
				legendgroup=cab_id,
				y=ys,
				x=widths_ms,
				base=bases,
				width=ws,
				orientation="h",
				marker=dict(color=bar_colors),
				hovertext=hover_text,
				hoverinfo="text",
				showlegend=True,
			)
		)

		fig.add_trace(
			go.Scatter(
				name=cab_id + " SoC",
				legendgroup=cab_id,
				showlegend=False,
				x=soc_x,
				y=soc_y,
				mode="lines",
				line=dict(color=soc_color),
				hovertext=soc_hover,
				hoverinfo="text",
			)
		)

	x0 = datetime.fromtimestamp(t_start.timestamp() - pad_seconds, tz=TZ)
	x1 = datetime.fromtimestamp(t_end.timestamp() + pad_seconds, tz=TZ)
	fig.update_xaxes(type="date", range=[x0, x1], title_text="Time")

	fig.update_yaxes(
		tickmode="array",
		tickvals=list(range(len(cab_ids))),
		ticktext=cab_ids,
		autorange="reversed",
		range=[len(cab_ids) - 0.5, -0.5],
		title_text="Cab ID",
	)

	fig.add_vline(x=t_start, line_dash="dash", line_width=2)
	fig.add_vline(x=t_end, line_dash="dash", line_width=2)

	visible_all = [True] * len(fig.data)
	visible_none = [False] * len(fig.data)

	fig.update_layout(
		title="timeline with SoC overlay (CT/convoy legs + charge-only climbs)",
		height=height,
		barmode="overlay",
		bargap=0.15,
		margin=dict(l=140, r=40, t=60, b=40),
		updatemenus=[
			dict(
				type="buttons",
				direction="right",
				x=0.0,
				y=1.12,
				xanchor="left",
				yanchor="top",
				buttons=[
					dict(label="Show all", method="update", args=[{"visible": visible_all}]),
					dict(label="Hide all", method="update", args=[{"visible": visible_none}]),
				],
			)
		],
		legend=dict(
			title="Cabs (click to hide/show)",
			groupclick="togglegroup",
			itemclick="toggle",
			itemdoubleclick="toggleothers",
		),
	)

	fig.show()
	return


def plot_routes_plotly2(
		router_cab, res_cab, router_pro, res_pro, zoom=12,
		show_graph_edges=True, show_graph_nodes=True,
		graph_edge_width=1, graph_edge_color="lightgray", graph_edge_opacity=0.5):
	"""
	Interactive Plotly map:
		- shows Cab and Pro routes (colored by highway)
		- displays projection lines (original to projected)
		- optional: show all graph edges + vertices

	graph_edge_width/color/opacity: default is a thin, subtle background grid
	- pass e.g. graph_edge_width=3, graph_edge_color="black",
	graph_edge_opacity=1.0 when the question is "is there really no road near
	this point", not just "roughly where are the roads".
	"""
	import plotly.graph_objects as go

	G = router_cab.G  # both share same OSM graph

	highway_colors = {
		"motorway": "#d73027", "trunk": "#fc8d59", "primary": "#fee08b",
		"secondary": "#91cf60", "tertiary": "#1a9850", "residential": "#4575b4",
		"service": "#74add1", "living_street": "#a65628", "unclassified": "#ff00ff",
		"track": "#984ea3", "unknown": "#000000",
	}

	def make_all_edges_trace(G, width=1, color="lightgray", opacity=0.5):
		all_lat = []
		all_lon = []
		for u, v, data in G.edges(data=True):
			geom = data.get("geometry")
			if geom is not None:
				xs, ys = geom.xy
				all_lat.extend(ys)
				all_lon.extend(xs)
			else:
				y1, y2 = G.nodes[u]["y"], G.nodes[v]["y"]
				x1, x2 = G.nodes[u]["x"], G.nodes[v]["x"]
				all_lat.extend([y1, y2])
				all_lon.extend([x1, x2])
			all_lat.append(None)
			all_lon.append(None)
		return go.Scattermapbox(
			lat=all_lat, lon=all_lon, mode="lines",
			line=dict(width=width, color=color),
			hoverinfo="skip", opacity=opacity,
			name="Graph edges", showlegend=False,
		)

	def make_all_nodes_trace(G):
		lats = [d["y"] for _, d in G.nodes(data=True)]
		lons = [d["x"] for _, d in G.nodes(data=True)]
		return go.Scattermapbox(
			lat=lats, lon=lons, mode="markers",
			marker=dict(size=6, color="gray"),
			hoverinfo="skip", opacity=0.5,
			name="Graph nodes", showlegend=False,
		)

	def extract_segments(router, path_nodes):
		segs = []
		for lat, lon, data in router.route_segment_geometry(path_nodes):
			highway = data.get("highway", "unknown")
			if isinstance(highway, list):
				highway = highway[0]
			speed = data.get("speed_kph", "n/a")
			segs.append((lat, lon, highway, speed))
		return segs

	cab_segments = extract_segments(router_cab, res_cab["path_osm_nodes"])
	pro_segments = extract_segments(router_pro, res_pro["path_osm_nodes"])

	traces = []

	if show_graph_edges:
		traces.append(make_all_edges_trace(G, width=graph_edge_width, color=graph_edge_color, opacity=graph_edge_opacity))

	if show_graph_nodes:
		traces.append(make_all_nodes_trace(G))

	for lat, lon, highway, speed in cab_segments:
		color = highway_colors.get(highway, "#999999")
		traces.append(go.Scattermapbox(
			lat=lat, lon=lon, mode="lines",
			line=dict(width=3, color=color),
			name=f"Cab - {highway}",
			opacity=0.7,
			hoverinfo="text",
			text=f"Cab<br>highway: {highway}<br>speed: {speed}",
			showlegend=False
		))

	for lat, lon, highway, speed in pro_segments:
		color = highway_colors.get(highway, "#999999")
		traces.append(go.Scattermapbox(
			lat=lat, lon=lon, mode="lines",
			line=dict(width=4, color=color),
			name=f"Pro - {highway}",
			opacity=1.0,
			hoverinfo="text",
			text=f"Pro<br>highway: {highway}<br>speed: {speed}",
			showlegend=False
		))

	def add_projection_elements(label, res):
		proj_lines = res.get("projection_lines", [])
		if not proj_lines:
			return

		for i, proj_line in enumerate(proj_lines):
			if len(proj_line) != 2:
				continue
			(lat1, lon1), (lat2, lon2) = proj_line

			traces.append(go.Scattermapbox(
				lat=[lat1, lat2], lon=[lon1, lon2],
				mode="lines",
				line=dict(width=3, color="magenta"),
				hoverinfo="text",
				text=f"{label} offset line",
				showlegend=False
			))

			if i == 0:
				u, v = res["path_osm_nodes"][:2]
				nlat, nlon = G.nodes[u]["y"], G.nodes[u]["x"]
			else:
				u, v = res["path_osm_nodes"][-2:]
				nlat, nlon = G.nodes[v]["y"], G.nodes[v]["x"]

			traces.append(go.Scattermapbox(
				lat=[lat2, nlat], lon=[lon2, nlon],
				mode="lines",
				line=dict(width=3, color="gray"),
				hoverinfo="text",
				text=f"{label} cutoff segment",
				showlegend=False
			))

			traces.append(go.Scattermapbox(
				lat=[lat2], lon=[lon2],
				mode="markers",
				marker=dict(size=14, color="yellow"),
				showlegend=False
			))
			traces.append(go.Scattermapbox(
				lat=[lat1], lon=[lon1],
				mode="markers",
				marker=dict(size=14, color="red"),
				showlegend=False
			))

		if res.get("path_osm_nodes"):
			s, t = res["path_osm_nodes"][0], res["path_osm_nodes"][-1]
			slat, slon = G.nodes[s]["y"], G.nodes[s]["x"]
			tlat, tlon = G.nodes[t]["y"], G.nodes[t]["x"]

			traces.append(go.Scattermapbox(
				lat=[slat], lon=[slon],
				mode="markers",
				marker=dict(size=12, color="orange"),
				showlegend=False
			))
			traces.append(go.Scattermapbox(
				lat=[tlat], lon=[tlon],
				mode="markers",
				marker=dict(size=12, color="blue"),
				showlegend=False
			))

	add_projection_elements("Cab", res_cab)
	add_projection_elements("Pro", res_pro)

	all_lats, all_lons = [], []
	for t in traces:
		if hasattr(t, "lat") and hasattr(t, "lon") and t.lat is not None:
			for v in t.lat:
				if v is not None:
					all_lats.append(float(v))
			for v in t.lon:
				if v is not None:
					all_lons.append(float(v))

	center_lat = (max(all_lats) + min(all_lats)) / 2 if all_lats else 0
	center_lon = (max(all_lons) + min(all_lons)) / 2 if all_lons else 0

	layout = go.Layout(
		mapbox_style="open-street-map",
		mapbox_zoom=zoom,
		mapbox_center={"lat": center_lat, "lon": center_lon},
		margin={"r": 0, "t": 30, "l": 0, "b": 0},
		title="Cab vs Pro Routes (with graph)"
	)

	fig = go.Figure(data=traces, layout=layout)
	fig.update_layout(mapbox=dict(accesstoken=None), dragmode="zoom")
	fig.show(config={"scrollZoom": True})
