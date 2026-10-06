#!/usr/bin/python3

import sys
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
import copy
import json
import time

from pathlib import Path

# Ensure the project root (the folder containing "custom_sim") is in sys.path
#sys.path.append(str(Path(__file__).resolve().parents[1]))
# Get the parent directory
parent_dir = Path(__file__).resolve().parent.parent

# Add parent directory to sys.path
sys.path.append(str(parent_dir))

# fleetplanning.models
from models import (Request, DemandScenario,
					Cab, Pro, VehicleFleet,
					OperationalVertices, OperationalArea,
					FleetAndRequests,
					ProRoutesAndTrips)

# fleetplanning.custom_sim.models_cs
from .models_cs import (Customer,
						CabEntryType, CabScheduleEntry, CabVehicle,
						SimChargingStation, SimChainingLocation,
						ProEntryType, ProScheduleEntry, ProVehicle,
						EventType, Event, NCCandidate, ScheduleEnergyStats,
						RuntimeDiagnostics)

# fleetplanning.custom_sim.utils_cs
from .utils_cs import (EventQueue,
						format_ts_to_hhmm,
						nearest_straight_line, best_station_between_weighted, haversine_distance,
						check_timeline_fleet, check_energy_bounds_fleet, check_energy_continuity_fleet, check_location_continuity_fleet, check_entry_energy_deltas_fleet, check_entry_distance_plausibility_fleet, check_timewindows_fleet,
						visualize_timeline_plotly_overlay_soc, plot_routes_plotly2,
						create_fleet_movement_gif, write_rw_style_iteration_files)

from custom_sim.routing.router import Router
from custom_sim.routing.utils_rt import point_in_polygon

from custom_sim.routing.visualization import plot_graph_plotly


# ===============================================================


@dataclass(frozen=True)
class _ImportedProTrip:
	trip_id: str
	route_id: str
	pro_key: str
	departure: int
	arrival: int
	chain_loc: SimChainingLocation
	unchain_loc: SimChainingLocation
	scheduled_duration_s: float
	scheduled_distance_m: float


@dataclass
class _ProSearchLine:
	sort_key: str
	chain_loc: SimChainingLocation
	unchain_loc: SimChainingLocation
	pt_selection_duration_s: float
	trips: list[tuple[ProScheduleEntry, ProVehicle]]
	trip_start_times: list[int]


@dataclass
class _SelectedProTrip:
	trip: ProScheduleEntry
	pro_vehicle: ProVehicle
	chain_loc: SimChainingLocation
	unchain_loc: SimChainingLocation
	fm_res: dict
	lm_res: dict
	score_s: float


class CustomSimulation:
	'''
	Main class for operational planning
	
	_operations_area : 	Infrastructure to initialize the model (Router, etc.)
	_demand :			Requests (which won't change during fleetplanning, so building once should be enough)
	'''
	def __init__(self, _operations_area: OperationalArea,
					_router_info: dict=None,
					_parameter_overrides: dict=None,
					_verbose: int=1):
		'''
		Constructor
		'''
		
		self.verbose = _verbose
		
		self.current_dir = Path(__file__).resolve().parent
		
		# arguments
		self.operations_area 	= _operations_area
		self.parameter_overrides = copy.deepcopy(_parameter_overrides or {})
		
		# more members
		self.router_cab 		= None
		self.router_pro 		= None
		self.charging_stations 	= []
		self.chaining_locations = []
		self.parking_locations 	= []
		self.sim_charging_stations = []
		self.sim_chaining_locations = []
		self.pro_routes_and_trips = None
		self.demand_scenario 	= []
		self.cab_fleet 			= []
		self.pro_fleet 			= []
		self.pro_timetable		= []
		self.pro_lines			= []
		self.chain_loc_by_id 	= {}
		self.current_time 		= -1
		self.horizon_start 		= -1
		self.horizon_end 		= -1
		
		self.num_rejects 		= -1
		
		# parameter for optimization and problem
		self.parameters = {}

		# console progress output and diagnostics after sim
		self.runtime_diagnostics = RuntimeDiagnostics()
		self.request_log_interval = 1
		self.request_log_batch = {}
		
		# build stuff
		self._initialize_system_model(_router_info)

	# ---------------------------------------------------------
	
	def _reset_model(self):
		'''
		Reset system model data
		'''
		self.num_rejects = 0
		self.router_cab.num_calls = 0
		self.router_cab.num_calls_fallback = 0
		self.router_pro.num_calls = 0
		self.router_pro.num_calls_fallback = 0
		self.runtime_diagnostics.reset()
		self.request_log_interval = int(self.parameters.get("debug", {}).get("request_output_interval", 1))
		self.request_log_batch = {
			"count": 0,
			"accepted": 0,
			"rejected": 0,
			"pure": 0,
			"convoy": 0,
			"early": 0,
			"start_time": "-",
			"end_time": "-",
			"start_req": "-",
			"end_req": "-",
		}
		
		return
	
	# ---------------------------------------------------------

	def _load_and_override_profiles(self, router_info: dict | None) -> dict:
		'''
		Override existing profiles if provided
		'''
		profiles_path = self.current_dir / "routing" / "vehicle_profiles.json"

		with open(profiles_path, "r", encoding="utf-8") as f:
			profiles = json.load(f)

		if router_info is None:
			return profiles

		profiles = copy.deepcopy(profiles)

		for profile_name, overrides in router_info.items():
			if profile_name not in profiles:
				raise ValueError(f"Unknown profile '{profile_name}'")

			profiles[profile_name].update(overrides)

		return profiles

	# ---------------------------------------------------------
	
	def _initialize_routers(self, _profile: dict=None) -> None:
		'''
		Create Router objects for each vehicle
		'''

		# only re-initialize routers if necessary
		if ( self.router_cab is not None and self.router_pro is not None ):
			return

		# raw (lon, lat) vertices; Router builds and owns the polygon itself
		vertices = self.operations_area.operational_area.vertices

		# instantiate Router (profile "car" by default)
		self.router_cab = Router(vertices,
						   profile="cab",
						   area_id=self.operations_area.operational_area.description,
						   profiles=self.router_profiles)
		self.router_pro = Router(vertices,
						   profile="pro",
						   area_id=self.operations_area.operational_area.description,
						   profiles=self.router_profiles)

		# used by _point_in_polygon for infrastructure checks
		self.area_polygon = self.router_cab.area_polygon

		return
	
	# ---------------------------------------------------------
	
	def _initialize_system_model(self, _router_info: dict=None):
		'''
		Build system model from operations_area
		'''
		
		# Load + override profiles ONCE
		self.router_profiles = self._load_and_override_profiles(_router_info)
		
		# initialize Routers
		self._initialize_routers()

		# define charging stations etc.
		self.charging_stations  = self.operations_area.charging_stations
		self.chaining_locations = self.operations_area.chaining_locations
		self.parking_locations  = self.operations_area.parking_locations
		self.pro_routes_and_trips = self.operations_area.pro_routes_and_trips

		# router queries and schedule entries use these snapped points
		self.sim_charging_stations = [
			SimChargingStation(cs, i, *self.router_cab.snap(cs.location_lat, cs.location_lon)[0])
			for i, cs in enumerate(self.charging_stations)
		]
		self.sim_chaining_locations = [
			SimChainingLocation(
				cl, i,
				*self.router_cab.snap(cl.start_location_lat, cl.start_location_lon)[0],
				*self.router_cab.snap(cl.end_location_lat, cl.end_location_lon)[0],
			)
			for i, cl in enumerate(self.chaining_locations)
		]
		self.chain_loc_by_id = {str(sim_cl.location.id): sim_cl for sim_cl in self.sim_chaining_locations}

		# load parameter settings
		self.parameters = self._load_config(self.current_dir / "parameters.json")
		self._apply_parameter_overrides(self.parameter_overrides)
			
		return 0
	
	# ---------------------------------------------------------
	
	def _load_config(self, path: str) -> dict:
		with open(path, "r") as f:
			return json.load(f)

	# ---------------------------------------------------------

	def _apply_parameter_overrides(self, overrides: dict) -> None:
		if not overrides:
			return

		def merge(base: dict, changes: dict) -> None:
			for key, value in changes.items():
				if isinstance(value, dict) and isinstance(base.get(key), dict):
					merge(base[key], value)
				else:
					base[key] = copy.deepcopy(value)

		merge(self.parameters, overrides)

	# ---------------------------------------------------------

	def _save_config(self, path: str, data: dict) -> None:
		with open(path, "w") as f:
			json.dump(data, f, indent='\t')
		return
	
	# ---------------------------------------------------------
	
	def _point_in_polygon(self, lon, lat, polygon=None, allow_boundary=True):
		"""
		Check whether a single point is inside (or on the boundary of) a polygon.
		"""

		if polygon is None:
			polygon = self.area_polygon

		return point_in_polygon(lon, lat, polygon, allow_boundary=allow_boundary)

	# ---------------------------------------------------------

	def _to_unix_timestamp(self, value) -> int:
		"""
		Normalize supported timestamp representations to unix seconds.
		"""
		if isinstance(value, (int, float)):
			return int(value)
		if isinstance(value, datetime):
			return int(value.timestamp())
		if isinstance(value, str):
			return int(datetime.fromisoformat(value).timestamp())
		raise TypeError(f"Unsupported timestamp value type: {type(value)}")

	# ---------------------------------------------------------

	def _read_pro_timetable(self) -> list[_ImportedProTrip]:
		"""
		Read the external Pro timetable into one normalized, simulation-friendly
		shape. This keeps format quirks at the import boundary.
		"""
		if self.pro_routes_and_trips is None:
			return []

		imported_trips: list[_ImportedProTrip] = []
		routes_by_id = {
			str(route.id): route
			for route in self.pro_routes_and_trips.chain_routes
		}

		for trip in self.pro_routes_and_trips.chain_route_trips:
			route = routes_by_id.get(trip.chain_route)
			if route is None:
				continue

			chain_loc = self.chain_loc_by_id.get(route.start_location)
			unchain_loc = self.chain_loc_by_id.get(route.end_location)
			if chain_loc is None or unchain_loc is None:
				continue

			departure = self._to_unix_timestamp(trip.departure)
			arrival = self._to_unix_timestamp(trip.arrival)
			if arrival <= departure:
				continue

			pro_key = trip.pro_schedule if trip.pro_schedule.startswith("Pro") else f"Pro{trip.pro_schedule}"
			imported_trips.append(
				_ImportedProTrip(
					trip_id=str(trip.id),
					route_id=str(route.id),
					pro_key=pro_key,
					departure=departure,
					arrival=arrival,
					chain_loc=chain_loc,
					unchain_loc=unchain_loc,
					scheduled_duration_s=float(route.duration or (arrival - departure)),
					scheduled_distance_m=float(route.distance or 0.0),
				)
			)

		imported_trips.sort(key=lambda trip: (trip.departure, trip.trip_id))
		return imported_trips

	# ---------------------------------------------------------

	def _get_pro_operation_window(self, pro_vehicle: ProVehicle) -> tuple[int | None, int | None]:
		"""
		Return the operating window of one Pro in unix seconds.
		"""
		return (pro_vehicle.start_schedule, pro_vehicle.end_schedule)

	# ---------------------------------------------------------

	def _create_pro_schedule_entry(
		self,
		imported_trip: _ImportedProTrip,
		pt_res: dict,
	) -> ProScheduleEntry:
		"""
		Create one canonical Pro schedule entry from a normalized timetable trip.

		pt_res is the router result for this route_id, computed once by the
		caller and reused here. distance_m and coordinates come from pt_res;
		the timetable's scheduled_distance_m has no route behind it.
		"""
		start_lat, start_lon = pt_res["proj_orig"]
		end_lat, end_lon = pt_res["proj_dest"]
		entry = ProScheduleEntry(
			ProEntryType.ST,
			_distance_m=float(pt_res.get("distance_m", 0.0) or 0.0),
			_source_trip_id=imported_trip.trip_id,
			_pt_routed_duration_s=float(pt_res["time_s"]),
			_pt_routed_distance_m=float(pt_res.get("distance_m", 0.0) or 0.0),
			_s_lat=start_lat,
			_s_lon=start_lon,
			_e_lat=end_lat,
			_e_lon=end_lon,
			_s_time=int(imported_trip.departure),
			_e_time=int(imported_trip.arrival),
			_s_charge=0.0,
			_e_charge=0.0,
			_route=pt_res["path_lat_lon"],
		)

		return entry

	# ---------------------------------------------------------

	def _initialize_pro_schedules(self, _current_solution: FleetAndRequests):
		"""
		Initialize canonical Pro schedules from the static timetable input.

		Trips are stored on the owning ProVehicle.schedule, while any fast lookup
		structures are built separately afterwards.
		"""
		self.pro_fleet = [ProVehicle(pro) for pro in _current_solution.vehicle_fleet.pros]
		self.pro_timetable = self._read_pro_timetable()
		if not self.pro_timetable:
			return

		pro_vehicle_by_key = {
			f"Pro{pro_vehicle.pro.id}": pro_vehicle
			for pro_vehicle in self.pro_fleet
		}
		pro_windows_by_key = {
			f"Pro{pro_vehicle.pro.id}": self._get_pro_operation_window(pro_vehicle)
			for pro_vehicle in self.pro_fleet
		}

		pt_res_by_route_id: dict[str, dict] = {}

		for imported_trip in self.pro_timetable:
			pro_vehicle = pro_vehicle_by_key.get(imported_trip.pro_key)
			pro_window = pro_windows_by_key.get(imported_trip.pro_key)
			if pro_vehicle is None or pro_window is None:
				continue

			start_limit, end_limit = pro_window
			if start_limit is not None and imported_trip.departure < start_limit:
				continue
			if end_limit is not None and imported_trip.arrival > end_limit:
				continue

			pt_res = pt_res_by_route_id.get(imported_trip.route_id)
			if pt_res is None:
				pt_start = (imported_trip.chain_loc.end_lat, imported_trip.chain_loc.end_lon)
				pt_end = (imported_trip.unchain_loc.start_lat, imported_trip.unchain_loc.start_lon)
				pt_res = self.router_pro.shortest_path(pt_start, pt_end)
				self.runtime_diagnostics.router_calls["pro"] += 1
				self.runtime_diagnostics.router_time_s["pro"] += pt_res["query_time_s"]
				pt_res_by_route_id[imported_trip.route_id] = pt_res

			pro_vehicle.schedule.append(
				self._create_pro_schedule_entry(
					imported_trip=imported_trip,
					pt_res=pt_res,
				)
			)

		for pro_vehicle in self.pro_fleet:
			pro_vehicle.schedule.sort(key=lambda entry: (entry.start_time, entry.source_trip_id or ""))
			entries_with_deadheads = []
			for index, entry in enumerate(pro_vehicle.schedule):
				if index > 0:
					previous = pro_vehicle.schedule[index - 1]
					gap = entry.start_time - previous.end_time
					if previous.type == ProEntryType.ST and entry.type == ProEntryType.ST and gap > 0:
						# Timetable input only stores service trips.
						# DT placement: centered in the existing gap.
						deadhead_res = self.router_pro.shortest_path(
							(previous.end_lat, previous.end_lon),
							(entry.start_lat, entry.start_lon),
						)
						self.runtime_diagnostics.router_calls["pro"] += 1
						self.runtime_diagnostics.router_time_s["pro"] += deadhead_res["query_time_s"]
						deadhead_duration = max(0, int(ceil(float(deadhead_res["time_s"]))))
						if deadhead_duration <= gap:
							deadhead_start = previous.end_time + ((gap - deadhead_duration) // 2)
							dt_start = deadhead_res["proj_orig"]
							dt_end = deadhead_res["proj_dest"]
							deadhead_entry = ProScheduleEntry(
								ProEntryType.DT,
								_distance_m=float(deadhead_res.get("distance_m", 0.0) or 0.0),
								_pt_routed_duration_s=float(deadhead_res["time_s"]),
								_pt_routed_distance_m=float(deadhead_res.get("distance_m", 0.0) or 0.0),
								_s_lat=dt_start[0],
								_s_lon=dt_start[1],
								_e_lat=dt_end[0],
								_e_lon=dt_end[1],
								_s_time=deadhead_start,
								_e_time=deadhead_start + deadhead_duration,
								_s_charge=0.0,
								_e_charge=0.0,
								_route=deadhead_res["path_lat_lon"],
							)
							entries_with_deadheads.append(deadhead_entry)
				entries_with_deadheads.append(entry)
			pro_vehicle.schedule = entries_with_deadheads

	# ---------------------------------------------------------

	def _build_pro_search_index(self):
		"""
		Build a derived line-based search index over canonical Pro schedules.

		The index stores stable trip references plus parallel sorted start times
		so later selection can skip obviously-too-early trips via bisect.
		Rebuild this helper after structural Pro schedule changes; occupancy-only
		updates on the canonical trip entries do not require rebuilding.
		"""
		self.pro_lines = []
		rows_by_trip_id = {
			imported_trip.trip_id: imported_trip
			for imported_trip in self.pro_timetable
		}

		grouped_lines: dict[str, dict] = {}
		for pro_vehicle in self.pro_fleet:
			for entry in pro_vehicle.schedule:
				if entry.type != ProEntryType.ST or entry.source_trip_id is None:
					continue

				imported_trip = rows_by_trip_id.get(entry.source_trip_id)
				if imported_trip is None:
					continue

				line_data = grouped_lines.setdefault(
					imported_trip.route_id,
					{
						"chain_loc": imported_trip.chain_loc,
						"unchain_loc": imported_trip.unchain_loc,
						"pt_selection_duration_s": float(imported_trip.scheduled_duration_s),
						"trips": [],
					},
				)
				line_data["trips"].append((entry, pro_vehicle))

		for sort_key, line_data in grouped_lines.items():
			line_trips = line_data["trips"]
			line_trips.sort(key=lambda item: (item[0].start_time, item[0].source_trip_id or ""))
			self.pro_lines.append(
				_ProSearchLine(
					sort_key=sort_key,
					chain_loc=line_data["chain_loc"],
					unchain_loc=line_data["unchain_loc"],
					pt_selection_duration_s=line_data["pt_selection_duration_s"],
					trips=line_trips,
					trip_start_times=[int(trip.start_time) for trip, _ in line_trips],
				)
			)

		self.pro_lines.sort(key=lambda line: line.sort_key)

	# ---------------------------------------------------------

	def _select_pro_line_candidates_for_convoy(
		self,
		customer: Customer,
		cab: CabVehicle,
	) -> list[tuple[float, _ProSearchLine]]:
		"""
		Select the most promising directional Pro lines for convoy insertion.

		The score is intentionally cheap: straight-line cab detours to/from the
		chaining locations plus the currently selected PT duration model
		(scheduled duration for now).
		"""
		if not self.pro_lines:
			return []

		max_candidates = int(
			self.parameters.get("algorithm", {}).get(
				"max_pro_line_candidates_for_convoy",
				1,
			)
		)
		max_candidates = max(1, max_candidates)

		cab_speed = max(1.0, float(cab.speed))
		ranked_lines: list[tuple[float, str, _ProSearchLine]] = []

		for line_bucket in self.pro_lines:
			# Same end (chain) / start (unchain) point used for the real fm_res/lm_res
			# routing below and for the entries themselves - see the comment there.
			# Low-stakes here either way (this only ranks candidate lines before the
			# real distance/energy get computed), but no reason to disagree.
			pu_to_chain_m = haversine_distance(
				customer.pu_snap_lat,
				customer.pu_snap_lon,
				line_bucket.chain_loc.end_lat,
				line_bucket.chain_loc.end_lon,
			)
			unchain_to_do_m = haversine_distance(
				line_bucket.unchain_loc.start_lat,
				line_bucket.unchain_loc.start_lon,
				customer.do_snap_lat,
				customer.do_snap_lon,
			)
			score_s = (
				pu_to_chain_m / cab_speed
				+ float(line_bucket.pt_selection_duration_s)
				+ unchain_to_do_m / cab_speed
			)
			ranked_lines.append((score_s, line_bucket.sort_key, line_bucket))

		ranked_lines.sort(key=lambda item: (item[0], item[1]))
		return [(score_s, line_bucket) for score_s, _, line_bucket in ranked_lines[:max_candidates]]

	# ---------------------------------------------------------

	def _select_pro_trip_candidates_for_line(
		self,
		customer: Customer,
		line_bucket: _ProSearchLine,
		time_left: int,
		time_right: int,
		t_ca: int,
		t_fm: int,
		t_lm: int,
		t_sa: int,
		t_lm_pre_drop: int,
	) -> list[tuple[ProScheduleEntry, ProVehicle]]:
		"""
		Prescreen trips on one selected Pro line with cheap temporal bounds.

		The resulting list is still validated with _find_convoy_insertion_times,
		but the coarse bounds avoid scanning obviously impossible departures.
		"""
		service = int(customer.entry_time)
		latest_start_lm_cap = int(time_right - t_sa - t_lm)

		if customer.tw_type:
			earliest_start_fm = max(int(time_left + t_ca), int(customer.tw_lower))
			latest_start_fm_cap = int(customer.tw_upper - service)
			if earliest_start_fm > latest_start_fm_cap:
				return []
			min_pt_start = earliest_start_fm + t_fm
			max_pt_end = latest_start_lm_cap
		else:
			lm_lb_cap = int(customer.tw_lower - t_lm_pre_drop)
			lm_ub_cap = min(latest_start_lm_cap, int(customer.tw_upper - t_lm))
			if lm_lb_cap > lm_ub_cap:
				return []
			min_pt_start = int(time_left + t_ca + t_fm)
			max_pt_end = lm_ub_cap

		if max_pt_end < min_pt_start:
			return []

		# bisect acts as a cheap per-line cursor without introducing mutable
		# cursor state yet; if profiling ever points here again, an explicit
		# advancing cursor can be layered on later.
		start_idx = bisect_left(line_bucket.trip_start_times, min_pt_start)
		selected_trips: list[tuple[ProScheduleEntry, ProVehicle]] = []

		for pro_trip, pro_vehicle in line_bucket.trips[start_idx:]:
			if pro_trip.start_time > max_pt_end:
				break
			if pro_trip.end_time > max_pt_end:
				continue
			if pro_trip.start_time >= time_right:
				break
			max_cabs = int(pro_vehicle.max_cabs)
			if max_cabs > 0 and len(pro_trip.cabs) >= max_cabs:
				continue
			selected_trips.append((pro_trip, pro_vehicle))

		return selected_trips

	# ---------------------------------------------------------

	def _select_convoy_trip_candidates(
		self,
		customer: Customer,
		cab: CabVehicle,
		time_left: int,
		time_right: int,
		t_ca: int,
		t_sa: int,
	) -> list[_SelectedProTrip]:
		"""
		Return a flat list of promising Pro trip contexts for convoy generation.

		Line ranking and trip prescreening stay inside the helper so convoy
		candidate construction does not need to know about the line abstraction.
		"""
		selected_trip_contexts: list[_SelectedProTrip] = []
		service = int(customer.entry_time)

		for score_s, line_bucket in self._select_pro_line_candidates_for_convoy(customer, cab):
			chain_loc = line_bucket.chain_loc
			unchain_loc = line_bucket.unchain_loc
			chain_add = int(chain_loc.location.additional_time)
			unchain_add = int(unchain_loc.location.additional_time)

			# Route to/from the chaining location's own end (chain) / start (unchain)
			# point, not the other half of the start/end pair - that's the point the
			# Pro's real, timetable-sourced route actually touches (see
			# _create_pro_schedule_entry) and the point the FM/PT/LM entries below
			# record as their own coordinate. The other field isn't a real vehicle
			# position in this model (we treat the whole convoy as one point, not
			# per-cab queue positions), so routing to/from it here would compute a
			# distance/energy for a point the entry doesn't actually claim to be at.
			# Routing to/from the wrong half of the pair is easy to miss when a
			# location's two points are close together, but a router snap can still
			# land far apart for points only a few meters apart, producing a
			# materially wrong distance/energy.
			fm_res = self.router_cab.shortest_path(
				(customer.pu_snap_lat, customer.pu_snap_lon),
				(chain_loc.end_lat, chain_loc.end_lon)
			)
			self.runtime_diagnostics.router_calls["cab"] += 1
			self.runtime_diagnostics.router_time_s["cab"] += fm_res["query_time_s"]
			lm_res = self.router_cab.shortest_path(
				(unchain_loc.start_lat, unchain_loc.start_lon),
				(customer.do_snap_lat, customer.do_snap_lon)
			)
			self.runtime_diagnostics.router_calls["cab"] += 1
			self.runtime_diagnostics.router_time_s["cab"] += lm_res["query_time_s"]

			t_fm_drive = int(fm_res["time_s"])
			t_lm_drive = int(lm_res["time_s"])

			t_fm = service + t_fm_drive + chain_add
			t_lm = unchain_add + t_lm_drive + service
			t_lm_pre_drop = unchain_add + t_lm_drive

			line_trips = self._select_pro_trip_candidates_for_line(
				customer=customer,
				line_bucket=line_bucket,
				time_left=time_left,
				time_right=time_right,
				t_ca=t_ca,
				t_fm=t_fm,
				t_lm=t_lm,
				t_sa=t_sa,
				t_lm_pre_drop=t_lm_pre_drop,
			)

			for pro_trip, pro_vehicle in line_trips:
				selected_trip_contexts.append(
					_SelectedProTrip(
						trip=pro_trip,
						pro_vehicle=pro_vehicle,
						chain_loc=chain_loc,
						unchain_loc=unchain_loc,
						fm_res=fm_res,
						lm_res=lm_res,
						score_s=score_s,
					)
				)

		selected_trip_contexts.sort(
			key=lambda ctx: (
				ctx.score_s,
				ctx.trip.start_time,
				ctx.trip.source_trip_id or "",
			)
		)
		return selected_trip_contexts

	# ---------------------------------------------------------

	def _objective_metric_name(self, objective_cfg: dict) -> str:
		name = objective_cfg.get("metric")
		if name is None:
			raise ValueError("Candidate objective needs a 'metric'")
		return str(name)

	# ---------------------------------------------------------

	def _score_candidate(self, candidate: NCCandidate) -> float:
		selection_cfg = getattr(self, "parameters", {}).get("candidate_selection", {})
		scoring_cfg = selection_cfg.get("scoring", {})
		objectives = scoring_cfg.get("objectives", [])

		if not objectives:
			score = candidate.metrics.value("time_request_completion_s")
			candidate.score = float(score)
			candidate.score_breakdown = {"time_request_completion_s": score}
			return candidate.score

		if scoring_cfg.get("strategy", "weighted_sum") != "weighted_sum":
			raise ValueError(
				"Unsupported candidate scoring strategy: "
				f"{scoring_cfg.get('strategy')}"
			)

		score = 0.0
		breakdown = {}
		for objective_cfg in objectives:
			name = self._objective_metric_name(objective_cfg)
			raw_value = candidate.metrics.value(name)
			scale = float(objective_cfg.get("scale", 1.0))
			if scale == 0.0:
				raise ValueError(f"Candidate objective scale must not be zero: {name}")

			direction = objective_cfg.get("direction", "min")
			if direction == "min":
				direction_factor = 1.0
			elif direction == "max":
				direction_factor = -1.0
			else:
				raise ValueError(f"Unsupported candidate objective direction: {direction}")

			weight = float(objective_cfg.get("weight", 1.0))
			term = direction_factor * weight * raw_value / scale
			score += term
			breakdown[name] = {
				"value": raw_value,
				"weight": weight,
				"scale": scale,
				"direction": direction,
				"term": term,
			}

		candidate.score = float(score)
		candidate.score_breakdown = breakdown
		return candidate.score

	# ---------------------------------------------------------

	def _candidate_admissibility_reject_reason(self, candidate: NCCandidate) -> str | None:
		'''
		Return None if admissible, otherwise a short debug reason.
		'''
		selection_cfg = getattr(self, "parameters", {}).get("candidate_selection", {})
		admissibility_cfg = selection_cfg.get("admissibility") or []
		admissibility_rules = admissibility_cfg if isinstance(admissibility_cfg, list) else []
		for rule_cfg in admissibility_rules:
			metric = rule_cfg.get("metric")
			if metric is None:
				raise ValueError("Candidate admissibility rule needs a 'metric'")

			threshold = float(rule_cfg["value"])
			value = candidate.metrics.value(str(metric))
			operator = rule_cfg.get("operator")
			if operator == "<=":
				rejected = value > threshold
			elif operator == "<":
				rejected = value >= threshold
			elif operator == ">=":
				rejected = value < threshold
			elif operator == ">":
				rejected = value <= threshold
			elif operator in ("=", "=="):
				rejected = value != threshold
			else:
				raise ValueError(f"Unsupported admissibility rule operator: {operator}")

			if rejected:
				return "admissibility"

		max_customer_in_vehicle_wait_s = (
			admissibility_cfg.get("max_customer_in_vehicle_wait_s")
			if isinstance(admissibility_cfg, dict)
			else None
		)

		if max_customer_in_vehicle_wait_s is not None:
			customer_wait_s = candidate.metrics.value("time_customer_in_vehicle_wait_s")
			if customer_wait_s > float(max_customer_in_vehicle_wait_s):
				return "admissibility"

		return None

	# ---------------------------------------------------------

	def _candidate_rank_key(self, candidate: NCCandidate):
		score = candidate.score
		if score is None:
			score = self._score_candidate(candidate)
		completion_time = candidate.metrics.value("time_request_completion_s")
		energy_key = candidate.metrics.value("energy_inserted_consumption_wh")
		cab_id = candidate.cab.cab.id if candidate.cab is not None else int(1e9)
		pro_trip_dep = (
			candidate.selected_pro_trip.start_time
			if candidate.selected_pro_trip is not None
			else int(1e18)
		)
		return (score, completion_time, energy_key, cab_id, pro_trip_dep)
	
	# ---------------------------------------------------------
	
	def _check_infrastructure(self):
		"""
		Check whether all infrastructure and vehicle start locations
		are within the operational area polygon.
		"""

		# -------------------------
		# Charging stations
		# -------------------------
		for sim_cs in self.sim_charging_stations:
			cs = sim_cs.station
			if not self._point_in_polygon(cs.location_lon, cs.location_lat):
				raise ValueError(
					f"Charging station {cs.id} outside operational area: "
					f"lat={cs.location_lat}, lon={cs.location_lon}"
				)

		# -------------------------
		# Parking locations
		# -------------------------
		for pl in self.parking_locations:
			if not self._point_in_polygon(pl.location_lon, pl.location_lat):
				raise ValueError(
					f"Parking location {pl.id} outside operational area: "
					f"lat={pl.location_lat}, lon={pl.location_lon}"
				)

		# -------------------------
		# Chaining locations
		# -------------------------
		for sim_cl in self.sim_chaining_locations:
			cl = sim_cl.location
			if ( not self._point_in_polygon(cl.start_location_lon, cl.start_location_lat) or
				not self._point_in_polygon(cl.end_location_lon, cl.end_location_lat) ):
				raise ValueError(
					f"Chaining location {cl.id} outside operational area: "
					f"start_lat={cl.start_location_lat}, start_lon={cl.start_location_lon}"
					f"end_lat={cl.end_location_lat}, end_lon={cl.end_location_lon}"
				)

		# -------------------------
		# Vehicle start locations
		# -------------------------
		for cab in self.cab_fleet:
			lat = cab.cab.init_location_lat
			lon = cab.cab.init_location_lon
			if not self._point_in_polygon(lon, lat):
				raise ValueError(
					f"Cab start location outside operational area: "
					f"cab_id={cab.cab.id}, "
					f"lat={lat}, lon={lon}"
				)
		
		return
	
	# ---------------------------------------------------------
	
	def _find_candidates_for_new_request(self, _cust: Customer) -> tuple[list[NCCandidate], dict[str, int]]:
		'''
		This is where the magic happens
		Computes possible Candidate "solutions"
		'''
		
		candidate_list = []
		stats = {
			"num_time_feasible": 0,
			"num_energy_feasible": 0,
			"any_cab_schedule_overlap": False,
			"impossible_tw_too_narrow": False,
		}

		if _cust.tw_upper - _cust.tw_lower < 2 * int(_cust.entry_time):
			stats["impossible_tw_too_narrow"] = True
			return candidate_list, stats

		for curr_cab in self._find_cab_order():

			# skip timewindows outside the time horizon (vehicle dependent)
			if _cust.tw_upper < int(curr_cab.cab.schedule_start_time.timestamp()):
				self.runtime_diagnostics.time_infeasible_by_reason["horizon_too_early"] += 1
				continue
			if _cust.tw_lower > int(curr_cab.cab.schedule_end_time.timestamp()):
				self.runtime_diagnostics.time_infeasible_by_reason["horizon_too_late"] += 1
				continue

			stats["any_cab_schedule_overlap"] = True
			curr_index = 0
			allow_first_gap = True # switch to stop infinity loop ...
			if ( len(candidate_list) > self.parameters["algorithm"]["max_candidates"] ):
				break
			flag = True
			loop_cnt = 0
			while ( True ):
				loop_cnt += 1
				flag, idx_left, idx_right = self._find_next_interval(curr_cab, _cust, index=curr_index, allow_first_gap=allow_first_gap)
				if ( not flag ):
					break # no more intervals
				replace_stop = idx_right if idx_right > -1 else len(curr_cab.schedule)
				replaced_entries = curr_cab.schedule[idx_left + 1:replace_stop]
				if any(entry.start_time <= self.current_time for entry in replaced_entries):
					if idx_right == -1:
						self.runtime_diagnostics.time_infeasible_by_reason["trailing_gap_entry_active"] += 1
						break
					# The first entry in the replacement slice has already started and
					# must not be modified. Advance idx_left to use it as the left
					# boundary instead, so the remainder of the gap is still usable.
					idx_left += 1
				candd_list = self._find_time_feasible_candidates(_cust, curr_cab, idx_left, idx_right)
				stats["num_time_feasible"] += len(candd_list)
				for candidate in candd_list:
					candidate_type = str(candidate.mode)
					if candidate_type == "convoy" and candidate.early_unchaining:
						candidate_type = "convoy_early_unchaining"
					self.runtime_diagnostics.generated_by_type[candidate_type] += 1
				remaining_cands = self._find_energy_feasible_candidates(curr_cab, candd_list)
				stats["num_energy_feasible"] += len(remaining_cands)
				candidate_list.extend(remaining_cands)
				if idx_right == -1:
					break
				curr_index = idx_right
				allow_first_gap = False
				
		return candidate_list, stats
	
	# ---------------------------------------------------------
	
	def _select_candidate_for_new_request(self, candidates: list[NCCandidate]) -> NCCandidate | None:
		'''
		Apply hard admissibility limits, then score and choose the best candidate.
		'''
		
		admissible_candidates = []
		for candidate in candidates:
			reject_reason = self._candidate_admissibility_reject_reason(candidate)
			if reject_reason is not None:
				self.runtime_diagnostics.infeasible_by_reason[reject_reason] += 1
				continue
			self._score_candidate(candidate)
			admissible_candidates.append(candidate)
			candidate_type = str(candidate.mode)
			if candidate_type == "convoy" and candidate.early_unchaining:
				candidate_type = "convoy_early_unchaining"
			self.runtime_diagnostics.admissible_by_type[candidate_type] += 1

		if len(admissible_candidates) > 0:
			selected_candidate = min(admissible_candidates, key=self._candidate_rank_key)
		else:
			selected_candidate = None
		
		return selected_candidate
	
	# ---------------------------------------------------------
	
	def _insert_candidate_for_new_request(self, candidate: NCCandidate):
		'''
		Commit the schedule of the chosen Candidate
		'''
		
		customer = candidate.req
		cab = candidate.cab
		changed_schedule = candidate.new_cab_schedule
		cab.update_schedule(changed_schedule)

		if candidate.selected_pro_trip is not None:
			pro_trip = candidate.selected_pro_trip
			if cab not in pro_trip.cabs:
				pro_trip.cabs.append(cab)
		
		return

	# ---------------------------------------------------------

	def _shift_actual_chp_charge_to_pt_charging(
		self,
		cab: CabVehicle,
		schedule: list[CabScheduleEntry],
	) -> None:
		'''
		Move materialized ChP charge into earlier PT charge where possible.

		This only changes the current schedule's energy realization. ChP blocker
		capacity is left untouched here; shortening reserved charging time is a
		separate reachability decision.
		'''
		energy_eps_wh = float(self.parameters["algorithm"]["energy_tolerance_wh"])

		for pt_index, pt_entry in enumerate(schedule):
			if (
				pt_entry.type != CabEntryType.PT
				or pt_entry.start_time < self.current_time
			):
				continue

			available_pt_charge = (
				float(pt_entry.max_charge_amount)
				- float(pt_entry.charge_amount)
			)
			if available_pt_charge <= energy_eps_wh:
				continue

			for chp_index in range(pt_index + 1, len(schedule)):
				if available_pt_charge <= energy_eps_wh:
					break

				chp_entry = schedule[chp_index]
				if chp_entry.type != CabEntryType.ChP:
					continue

				chp_charge = float(chp_entry.charge_amount)
				if chp_charge <= energy_eps_wh:
					continue

				interval_max_energy = float(pt_entry.end_charge)
				for entry in schedule[pt_index + 1:chp_index]:
					interval_max_energy = max(
						interval_max_energy,
						float(entry.start_charge),
						float(entry.end_charge),
					)
				interval_max_energy = max(
					interval_max_energy,
					float(chp_entry.start_charge),
				)
				upper_energy_room = float(cab.charge_ub) - interval_max_energy

				transfer_charge = min(
					available_pt_charge,
					chp_charge,
					upper_energy_room,
				)
				if transfer_charge <= energy_eps_wh:
					continue

				pt_entry.charge_amount = float(pt_entry.charge_amount) + transfer_charge
				pt_entry.end_charge += transfer_charge
				for entry in schedule[pt_index + 1:chp_index]:
					entry.start_charge += transfer_charge
					entry.end_charge += transfer_charge
				chp_entry.start_charge += transfer_charge
				chp_entry.charge_amount = chp_charge - transfer_charge
				available_pt_charge -= transfer_charge

		return

	# ---------------------------------------------------------

	def _plan_chp_reserve_reductions(
		self,
		cab: CabVehicle,
		schedule: list[CabScheduleEntry],
	) -> dict[int, float]:
		'''
		Compute safe ChP reserve reductions from blocker reachability.

		The first future ChP is checked from the Cab's current state. Later
		intervals start from the previous ChP and keep the blocker assumption
		that the Cab may leave that ChP full.
		'''
		energy_eps_wh = float(self.parameters["algorithm"]["energy_tolerance_wh"])
		charge_restore_fraction = float(
			self.parameters["algorithm"].get("charge_restore_fraction", 1.0)
		)
		burn_rate_wh_per_s = (
			float(cab.consumption) * float(cab.speed) * charge_restore_fraction
		)
		max_energy_wh = float(cab.charge_ub)
		reductions_by_index: dict[int, float] = {}
		previous_chp_index: int | None = None

		for chp_index, chp_entry in enumerate(schedule):
			if chp_entry.type != CabEntryType.ChP:
				continue

			if chp_entry.start_time < self.current_time:
				previous_chp_index = chp_index
				continue

			if previous_chp_index is None:
				if chp_index == 0 or schedule[chp_index - 1].type != CabEntryType.ChA:
					previous_chp_index = chp_index
					continue

				guaranteed_energy_wh = float(cab.current_charge)
				last_time = self.current_time
				interval_start_index = 0
			else:
				previous_chp = schedule[previous_chp_index]
				if previous_chp.end_time < self.current_time:
					previous_chp_index = chp_index
					continue

				guaranteed_energy_wh = max_energy_wh
				last_time = previous_chp.end_time
				interval_start_index = previous_chp_index + 1

			for entry in schedule[interval_start_index:chp_index]:
				if (
					entry.type != CabEntryType.PT
					or entry.start_time < self.current_time
				):
					continue

				guaranteed_energy_wh -= burn_rate_wh_per_s * max(
					0.0,
					float(entry.start_time - last_time),
				)
				guaranteed_energy_wh = min(
					max_energy_wh,
					guaranteed_energy_wh + float(entry.max_charge_amount),
				)
				last_time = entry.end_time

			guaranteed_energy_wh -= burn_rate_wh_per_s * max(
				0.0,
				float(chp_entry.start_time - last_time),
			)
			required_reserve_wh = max(0.0, max_energy_wh - guaranteed_energy_wh)
			old_max_charge = float(chp_entry.max_charge_amount)
			actual_charge = float(chp_entry.charge_amount)
			charging_power_w = float(chp_entry.charge_station.station.max_supply)
			shortening_burn_factor = burn_rate_wh_per_s * 3600.0 / charging_power_w
			# Shortening from the start delays the next ChP. That extra gap also
			# has to be reachable under the blocker invariant.
			required_reserve_after_shortening_wh = (
				required_reserve_wh + shortening_burn_factor * old_max_charge
			) / (1.0 + shortening_burn_factor)
			new_max_charge = min(
				old_max_charge,
				max(actual_charge, required_reserve_after_shortening_wh),
			)
			reduction = old_max_charge - new_max_charge
			if reduction > energy_eps_wh:
				chp_entry.max_charge_amount = new_max_charge
				reductions_by_index[chp_index] = reduction

			previous_chp_index = chp_index

		return reductions_by_index

	# ---------------------------------------------------------

	def _apply_chp_reserve_reductions(
		self,
		cab: CabVehicle,
		schedule: list[CabScheduleEntry],
		reductions_by_index: dict[int, float],
	) -> None:
		'''
		Shorten ChP blockers after their new reserve amounts are known.

		Shortening happens from the start, keeping the ChP end fixed and moving
		the glued ChA later to enlarge the preceding schedule gap.
		'''
		energy_eps_wh = float(self.parameters["algorithm"]["energy_tolerance_wh"])

		for chp_index in sorted(reductions_by_index, reverse=True):
			chp_entry = schedule[chp_index]
			cha_entry = schedule[chp_index - 1]

			active_duration_s = int(
				chp_entry.end_time - chp_entry.start_time - chp_entry.service_time
			)
			if active_duration_s < 0:
				raise RuntimeError(
					"ChP service time exceeds its duration during charging replanning "
					f"(cab_id={cab.cab.id}, schedule_index={chp_index})"
				)
			charging_power_w = float(chp_entry.charge_station.station.max_supply)

			reduced_charge_wh = reductions_by_index[chp_index]
			released_duration_s = int(reduced_charge_wh * 3600.0 / charging_power_w)
			if released_duration_s <= 0:
				continue

			# If the glued ChA has already started, do not shift it forward.
			# Shifting an in-progress ChA past current_time would make it appear
			# future to the pruner, which would then delete it and leave
			# cab.current_lat frozen at a stale interpolated position.
			if cha_entry.start_time < self.current_time:
				continue

			remaining_active_duration_s = active_duration_s - released_duration_s
			if (
				remaining_active_duration_s <= 0
				and float(chp_entry.charge_amount) > energy_eps_wh
			):
				raise RuntimeError(
					"Charging replanning removed all ChP active duration while charge remains "
					f"(cab_id={cab.cab.id}, schedule_index={chp_index}, "
					f"remaining_charge={chp_entry.charge_amount})"
				)

			chp_entry.start_time += released_duration_s
			cha_entry.start_time += released_duration_s
			cha_entry.end_time += released_duration_s

		return

	# ---------------------------------------------------------

	def _prune_redundant_future_charging_blocks(
		self,
		cab: CabVehicle,
		schedule: list[CabScheduleEntry],
	) -> None:
		'''
		Delete future ChPs whose remaining active duration is negligible.

		If the glued future ChA can be replaced by a direct approach to the next
		approach entry without breaking energy bounds, delete that ChA too.
		Otherwise keep the ChA as relocation and only delete the ChP.
		'''
		energy_eps_wh = float(self.parameters["algorithm"]["energy_tolerance_wh"])
		chp_deletion_active_duration_threshold_s = float(
			self.parameters.get("algorithm", {}).get("charging_replanning", {}).get(
				"chp_deletion_active_duration_threshold_s",
				0.0,
			)
		)

		chp_index = len(schedule) - 1
		# Scan backwards because this cleanup may delete schedule entries.
		while chp_index >= 0:
			chp_entry = schedule[chp_index]
			# Only future ChPs with a preceding future ChA are cleanup candidates.
			if (
				chp_entry.type != CabEntryType.ChP
				or chp_entry.start_time < self.current_time
				or chp_index == 0
			):
				chp_index -= 1
				continue

			cha_entry = schedule[chp_index - 1]
			if (
				cha_entry.type != CabEntryType.ChA
				or cha_entry.start_time < self.current_time
			):
				chp_index -= 1
				continue

			active_duration_s = int(
				chp_entry.end_time - chp_entry.start_time - chp_entry.service_time
			)
			# The ChP itself is redundant only if no relevant active charging remains.
			if (
				active_duration_s <= chp_deletion_active_duration_threshold_s
				and float(chp_entry.charge_amount) <= energy_eps_wh
			):
				next_index = chp_index + 1
				deleted_charging_approach = False
				if next_index < len(schedule):
					next_entry = schedule[next_index]
					# Try to remove the station detour as well. This is only safe
					# when the next entry is a future approach we can retime directly.
					# Approach entries can be rerouted without changing a
					# fixed service entry; other next entries keep the ChA.
					if (
						next_entry.type in (CabEntryType.CA, CabEntryType.ChA)
						and next_entry.start_time >= self.current_time
					):
						direct_res = self.router_cab.shortest_path(
							(float(cha_entry.start_lat), float(cha_entry.start_lon)),
							(float(next_entry.end_lat), float(next_entry.end_lon)),
						)
						self.runtime_diagnostics.router_calls["cab"] += 1
						self.runtime_diagnostics.router_time_s["cab"] += direct_res["query_time_s"]
						direct_time_s = float(direct_res["time_s"])
						direct_energy_wh = float(direct_res["energy_wh"])
						direct_start_time = int(float(next_entry.end_time) - direct_time_s)
						earliest_start_time = max(
							int(self.current_time),
							int(cha_entry.start_time),
						)
						# Keep the next approach glued to its old end time.
						if direct_start_time >= earliest_start_time:
							direct_start_charge = float(cha_entry.start_charge)
							direct_end_charge = direct_start_charge - direct_energy_wh
							charge_delta = direct_end_charge - float(next_entry.end_charge)
							charge_lb = float(cab.charge_lb)
							charge_ub = float(cab.charge_ub)
							# Replacing the station detour shifts all later SoC
							# values by one constant delta.
							energy_feasible = (
								charge_lb - energy_eps_wh
								<= direct_start_charge
								<= charge_ub + energy_eps_wh
								and charge_lb - energy_eps_wh
								<= direct_end_charge
								<= charge_ub + energy_eps_wh
							)
							if energy_feasible:
								for later_entry in schedule[next_index + 1:]:
									if (
										float(later_entry.start_charge) + charge_delta
										< charge_lb - energy_eps_wh
										or float(later_entry.start_charge) + charge_delta
										> charge_ub + energy_eps_wh
										or float(later_entry.end_charge) + charge_delta
										< charge_lb - energy_eps_wh
										or float(later_entry.end_charge) + charge_delta
										> charge_ub + energy_eps_wh
									):
										energy_feasible = False
										break
							if energy_feasible:
								# Apply the direct replacement and propagate the SoC shift.
								direct_start = direct_res["proj_orig"]
								next_entry.start_lat = direct_start[0]
								next_entry.start_lon = direct_start[1]
								next_entry.start_time = direct_start_time
								next_entry.start_charge = direct_start_charge
								next_entry.end_charge = direct_end_charge
								next_entry.distance_m = float(direct_res.get("distance_m", 0.0) or 0.0)
								for later_entry in schedule[next_index + 1:]:
									later_entry.start_charge += charge_delta
									later_entry.end_charge += charge_delta
								schedule.pop(chp_index)
								schedule.pop(chp_index - 1)
								deleted_charging_approach = True

				if not deleted_charging_approach:
					cha_shift_s = int(chp_entry.end_time - cha_entry.end_time)
					cha_entry.start_time += cha_shift_s
					cha_entry.end_time += cha_shift_s
					# If the direct bridge is not safe, keep the ChA as relocation
					# and only remove the now-redundant ChP.
					schedule.pop(chp_index)

			chp_index -= 1
		return

	# ---------------------------------------------------------

	def _replan_charging(
		self,
		cab: CabVehicle,
		schedule: list[CabScheduleEntry],
	) -> list[CabScheduleEntry]:
		'''
		Replan PT and ChP charging in two separate steps.

		Actual charge shifting only changes the current schedule's energy levels.
		ChP timing reduction is then based on the reachability reserve invariant,
		not on where the current materialized charge happened to be placed.
		'''
		self._shift_actual_chp_charge_to_pt_charging(cab, schedule)
		reductions_by_index = self._plan_chp_reserve_reductions(cab, schedule)
		self._apply_chp_reserve_reductions(cab, schedule, reductions_by_index)
		self._prune_redundant_future_charging_blocks(cab, schedule)
		return schedule

	# ---------------------------------------------------------

	def _set_customer_assignment_state(self, customer: Customer, assigned: bool,
									 cab_id: int | None = None,
									 mode: str | None = None,
									 reject_reason: str | None = None):
		'''
		Update the simulation-side assignment state of one customer.
		'''
		state = customer.sim_state
		state.assigned = assigned
		if assigned:
			state.assigned_cab_id = cab_id
			state.assigned_mode = mode
			state.reject_reason = None
		else:
			state.assigned_cab_id = None
			state.assigned_mode = None
			state.reject_reason = reject_reason
			state.time_customer_direct_s = None
			state.distance_customer_direct_m = None
			state.time_customer_excess_travel_s = None
			state.time_customer_in_vehicle_wait_s = None

	# ---------------------------------------------------------

	def _flush_request_log_summary(self):
		'''
		Print and reset the aggregated request-output row.
		'''
		batch = self.request_log_batch
		if batch["count"] == 0:
			return

		print(
			f"{batch['start_time']:>6s} "
			f"{batch['end_time']:>6s} "
			f"{batch['start_req']:>6s} "
			f"{batch['end_req']:>6s} "
			f"{batch['accepted']:>8d} "
			f"{batch['rejected']:>8d} "
			f"{batch['pure']:>6d} "
			f"{batch['convoy']:>6d} "
			f"{batch['early']:>6d}"
		)
		batch["count"] = 0
		batch["accepted"] = 0
		batch["rejected"] = 0
		batch["pure"] = 0
		batch["convoy"] = 0
		batch["early"] = 0
		batch["start_time"] = "-"
		batch["end_time"] = "-"
		batch["start_req"] = "-"
		batch["end_req"] = "-"

	# ---------------------------------------------------------
	
	def _dispatch(self, _cust: Customer):
		'''
		Wrapper function for Candidate computation and processing (called in self.optimize())
		'''
		
			
		start_candidate_generation = time.perf_counter()
		can_list, can_stats = self._find_candidates_for_new_request(_cust)
		self.runtime_diagnostics.candidate_generation_s += time.perf_counter() - start_candidate_generation
		can = self._select_candidate_for_new_request(can_list)
		event_time = "-"
		if self.horizon_start <= self.current_time <= self.horizon_end:
			event_time = format_ts_to_hhmm(self.current_time)
		if can:
			accepted = True
			selected_mode = str(can.mode)
			selected_early = bool(can.early_unchaining)
			candidate_type = str(can.mode)
			if candidate_type == "convoy" and can.early_unchaining:
				candidate_type = "convoy_early_unchaining"
			self.runtime_diagnostics.selected_by_type[candidate_type] += 1
			if self.parameters.get("algorithm", {}).get("charging_replanning", {}).get("enabled", False):
				can.new_cab_schedule = self._replan_charging(
					can.cab,
					can.new_cab_schedule,
				)
				# Replanning runs after selection and energy repair; structural changes
				# may invalidate candidate insertion indices, which are unused from here.
			self._set_customer_assignment_state(
				_cust,
				assigned=True,
				cab_id=int(can.cab.cab.id),
				mode=can.mode,
			)
			_cust.sim_state.time_customer_direct_s = can.metrics.time_customer_direct_s
			_cust.sim_state.distance_customer_direct_m = can.metrics.distance_customer_direct_m
			_cust.sim_state.time_customer_excess_travel_s = can.metrics.time_customer_excess_travel_s
			_cust.sim_state.time_customer_in_vehicle_wait_s = can.metrics.time_customer_in_vehicle_wait_s
			_cust.sim_state.pickup_offset_m = can.metrics.distance_customer_pickup_offset_m
			_cust.sim_state.dropoff_offset_m = can.metrics.distance_customer_dropoff_offset_m
			pickup_ts = None
			dropoff_ts = None
			for entry in can.new_cab_schedule:
				pu_ts, do_ts, _, _ = self._extract_request_events_from_entry(entry)
				if entry.customer is _cust and pu_ts is not None:
					pickup_ts = pu_ts if pickup_ts is None else min(pickup_ts, pu_ts)
				if entry.customer is _cust and do_ts is not None:
					dropoff_ts = do_ts if dropoff_ts is None else max(dropoff_ts, do_ts)
			pro_id = "-"
			early_unchaining = "-"
			if can.pro is not None:
				pro_id = can.pro.pro.id
				early_unchaining = str(can.early_unchaining)
			wait_seconds = float(can.metrics.time_customer_in_vehicle_wait_s or 0.0)
			if self.request_log_interval == 1:
				print(
					f"{event_time:>6s} "
					f"{int(_cust.request.id):>6d} "
					f"{'accepted':>10s} "
					f"{str(can.mode):>8s} "
					f"{int(can.cab.cab.id):>6d} "
					f"{str(pro_id):>6s} "
					f"{early_unchaining:>6s} "
					f"{format_ts_to_hhmm(pickup_ts) if pickup_ts is not None else '-':>6s} "
					f"{format_ts_to_hhmm(dropoff_ts) if dropoff_ts is not None else '-':>6s} "
					f"{wait_seconds:>8.0f} "
					f"{'-':>30s}"
				)
			self._insert_candidate_for_new_request(can)
		else:
			accepted = False
			selected_mode = "-"
			selected_early = False
			self.num_rejects += 1
			if can_stats["impossible_tw_too_narrow"]:
				reject_reason = "impossible_tw_too_narrow"
			elif can_stats["num_time_feasible"] == 0 and not can_stats["any_cab_schedule_overlap"]:
				reject_reason = "impossible_outside_horizon"
			elif can_stats["num_time_feasible"] == 0:
				reject_reason = "no_time_feasible_candidate"
			elif can_stats["num_energy_feasible"] == 0:
				reject_reason = "no_energy_feasible_candidate"
			else:
				reject_reason = "no_admissible_candidate"
			self.runtime_diagnostics.request_rejections_by_reason[reject_reason] += 1
			self._set_customer_assignment_state(
				_cust,
				assigned=False,
				reject_reason=reject_reason,
			)
			if self.request_log_interval == 1:
				print(
					f"{event_time:>6s} "
					f"{int(_cust.request.id):>6d} "
					f"{'rejected':>10s} "
					f"{'-':>8s} "
					f"{'-':>6s} "
					f"{'-':>6s} "
					f"{'-':>6s} "
					f"{'-':>6s} "
					f"{'-':>6s} "
					f"{'-':>8s} "
					f"{reject_reason:>30s}"
				)
		if self.request_log_interval > 1:
			batch = self.request_log_batch
			if batch["count"] == 0:
				batch["start_time"] = event_time
				batch["start_req"] = str(_cust.request.id)
			batch["end_time"] = event_time
			batch["end_req"] = str(_cust.request.id)
			batch["count"] += 1
			if accepted:
				batch["accepted"] += 1
				if selected_mode == "pure":
					batch["pure"] += 1
				elif selected_mode == "convoy":
					batch["convoy"] += 1
				if selected_early:
					batch["early"] += 1
			else:
				batch["rejected"] += 1
			if batch["count"] >= self.request_log_interval:
				self._flush_request_log_summary()
		
		return
	
	# ---------------------------------------------------------
	
	def _find_cab_order(self) -> list[CabVehicle]:
		'''
		Order in which to check cabs
		'''
		cab_order = sorted(self.cab_fleet, key=lambda entry: entry.cab.id)
			
		return cab_order
	
	# ---------------------------------------------------------
	
	def _find_next_interval(self, cab: CabVehicle, _cust: Customer, index: int = 0,
							allow_first_gap: bool = True,
							fixed_entry_type: tuple[CabEntryType, ...] = (CabEntryType.CT, CabEntryType.ChP,
												CabEntryType.FM, CabEntryType.LM,
												CabEntryType.PT)) -> tuple[bool, int, int]:
		"""
		Returns the next valid interval boundary (left, right).

		Returned values:
			(False, -1, -1) -> no interval found; stop scanning completely
			(True, -1, -1) -> entire schedule is free (empty); stop afterwards
			(True, -1, R) -> first gap before fixed entry R; next scan starts at R
			(True, L, R) -> normal middle gap between L and R; next scan starts at R
			(True, L, -1) -> trailing gap after L; stop afterwards
		"""

		schedule = cab.schedule
		n = len(schedule)

		t_now 		= _cust.register_time
		tw_lower 	= _cust.tw_lower
		tw_upper 	= _cust.tw_upper
		
		if tw_upper < t_now:
			return False, -1, -1
		
		# ------------------------------------------------------------
		# CASE 0: empty schedule -> whole timeline free
		# ------------------------------------------------------------
		if n == 0:
			return True, -1, -1

		# ------------------------------------------------------------
		# FIRST CALL: allow_first_gap == True
		# ------------------------------------------------------------
		if allow_first_gap:
			# Find the first fixed entry (if any)
			first_fixed = None
			for i in range(n):
				e = schedule[i]
				if e.type in fixed_entry_type:
					first_fixed = i
					break

			if first_fixed is None:
				# No fixed entries at all -> entire schedule free
				return True, -1, -1

			ff_entry = schedule[first_fixed]

			if allow_first_gap:
				# Does a first gap exist?
				# entry must start after NOW
				if ff_entry.start_time > t_now:
					# entry starts before the time window -> skip it
					if ff_entry.start_time <= tw_lower:
						# No valid first-gap interval; fall through into normal scanning
						allow_first_gap = False
						index = first_fixed
					else:
						# Valid first gap
						return True, -1, first_fixed
				# entry had already started before NOW
				else:
					# No first-gap; treat this as a normal scan
					allow_first_gap = False
					index = first_fixed
			

		# ------------------------------------------------------------
		# From here on, index must point to a fixed entry
		# ------------------------------------------------------------

		# ------------------------------------------------------------
		# Scan forward to find the next fixed entry
		# ------------------------------------------------------------
		while ( True ):
			if index < 0 or index >= n:
				# Out of range -> nothing left
				return False, -1, -1

			left_entry = schedule[index]

			if left_entry.type not in fixed_entry_type:
				# This should NEVER happen after first-gap logic; means schedule structure mismatch
				raise RuntimeError("Index passed to _find_next_interval is not a fixed-entry index.")
			
			# stop scanning
			if ( left_entry.end_time > tw_upper ):
				# left entry is done beyond the time window -> nothing else to scan
				return False, -1, -1
			
			# ------------------------------------------------------------
			
			next_fixed = None
			for j in range(index + 1, n):
				e = schedule[j]

				if e.type in fixed_entry_type:
					next_fixed = j
					break

			# trailing gap only if no further fixed entries exist
			if next_fixed is None:
				return True, index, -1

			right_entry = schedule[next_fixed]

			# if the right fixed starts before TW, skip internally
			if right_entry.start_time < tw_lower:
				index = next_fixed
				continue
				
			# normal pair, even if right starts after tw_upper
			return True, index, next_fixed
	
	# ---------------------------------------------------------

	def _find_next_fixed_entry_after_index(self,
										 cab: CabVehicle,
										 index_start: int,
										 fixed_entry_type: tuple[CabEntryType, ...] = (CabEntryType.CT, CabEntryType.ChP,
																	 CabEntryType.FM, CabEntryType.LM,
																	 CabEntryType.PT)) -> CabScheduleEntry | None:
		'''
		Return next fixed schedule entry after index_start, or None.
		'''
		for entry in cab.schedule[index_start + 1:]:
			if entry.type in fixed_entry_type:
				return entry
		return None

	# ---------------------------------------------------------

	def _select_charging_station_candidates_for_chp(self,
												 do_pos: tuple[float, float],
												 right_entry: CabScheduleEntry,
												 next_fixed_entry: CabScheduleEntry | None,
												 max_candidates: int | None = None,
												 distance_weight: float | None = None) -> list[SimChargingStation]:
		'''
		Select charging-station candidates for a right-side ChP case.

		Requires: right_entry.charge_station must be a valid SimChargingStation (not None).
		If an argument is None, the value is read from self.parameters["algorithm"].
		The current ChP station is always kept as first candidate.

		Anchor semantics:
		- If next_fixed_entry exists, anchor at that fixed entry's start location.
		- If next_fixed_entry is None, fallback selection is pure nearest-to-dropoff
		  for alternative stations (current station still kept as first candidate).
		'''
		if max_candidates is None:
			max_candidates = int(
				self.parameters.get("algorithm", {}).get(
					"max_station_candidates_for_right_charging_process",
					2,
				)
			)

		if distance_weight is None:
			distance_weight = float(
				self.parameters.get("algorithm", {}).get(
					"dropoff_to_station_distance_weight_for_right_charging_process",
					0.5,
				)
			)

		max_candidates = max(1, int(max_candidates))
		w_dropoff = max(0.0, min(1.0, float(distance_weight)))

		curr_cs = right_entry.charge_station

		if next_fixed_entry is not None:
			# future-aware mode: bias toward next fixed schedule constraint
			anchor = (float(next_fixed_entry.start_lat), float(next_fixed_entry.start_lon))
			w_rank = w_dropoff
		else:
			# fallback mode: no future constraint -> rank alternatives by dropoff distance only
			anchor = do_pos
			w_rank = 1.0
		# keep current station first, then add alternatives by weighted ranking
		selected = []
		remaining_stations = []
		for sim_station in self.sim_charging_stations:
			if sim_station is curr_cs:
				selected.append(sim_station)
			else:
				remaining_stations.append(sim_station)

		while len(selected) < max_candidates and len(remaining_stations) > 0:
			best_idx = best_station_between_weighted(
				A=do_pos,
				B=anchor,
				stations=[(station.lat, station.lon) for station in remaining_stations],
				weight_from_A=w_rank,
			)
			if best_idx is None:
				break

			best_station = remaining_stations.pop(best_idx)
			selected.append(best_station)

		return selected

	# ---------------------------------------------------------
	
	def _find_time_feasible_candidates(self, customer: Customer, cab: CabVehicle, index_left: int=0, index_right: int=1) -> list[NCCandidate]:
		'''
		Build time-feasible pure-cab candidates for one interval.

		Station variants are enumerated here for right-side ChP and trailing intervals.
		Energy feasibility is evaluated later in _find_energy_feasible_candidates.
		'''
		feasible_candidates = []

		# shared gap context: compute once, reuse in pure cab and convoy candidate paths
		
		# get entries if they exist
		left_entry = cab.schedule[index_left] if ( index_left > -1 ) else None
		right_entry = cab.schedule[index_right] if ( index_right > -1 ) else None

		# Do not insert into the internal slack of an already planned convoy block.
		if right_entry is not None and right_entry.type in (CabEntryType.PT, CabEntryType.LM):
			return feasible_candidates

		# get bounds for available time
		time_left = left_entry.end_time if ( index_left > -1 and left_entry is not None ) else max(self.current_time, cab.start_schedule)
		time_right = right_entry.start_time if ( index_right > -1 and right_entry is not None ) else cab.end_schedule
		
		# handle positions in case of first/last interval
		pos_left = (left_entry.end_lat, left_entry.end_lon) if ( index_left > -1 ) else (cab.current_lat, cab.current_lon)

		trailing_station: SimChargingStation | None = None
		if ( index_right < 0 ):
			cs_idx = nearest_straight_line(
				(customer.do_snap_lat, customer.do_snap_lon),
				[(sim_cs.lat, sim_cs.lon) for sim_cs in self.sim_charging_stations],
			)
			trailing_station = self.sim_charging_stations[cs_idx]
		
		# build station variants outside pure-cab helper
		# mode "legacy-single": one variant (None -> no station override)
		station_variants: list[SimChargingStation | None] = [None]

		# mode "trailing": always needs an explicit charging station target
		if ( index_right < 0 ):
			station_variants = [trailing_station]
		# mode "right-chp": enumerate station candidates (first keeps current station)
		elif ( right_entry.type == CabEntryType.ChP ):
			# evaluate each station variant separately in pure-cab helper
			next_fixed_entry = self._find_next_fixed_entry_after_index(cab, index_right)
			station_candidates = self._select_charging_station_candidates_for_chp(
				do_pos=(customer.do_snap_lat, customer.do_snap_lon),
				right_entry=right_entry,
				next_fixed_entry=next_fixed_entry,
			)
			# helper always includes current ChP station as first candidate
			station_variants = station_candidates

		for cs_variant in station_variants:
			# approach target depends on mode:
			# - right-chp: candidate station location
			# - normal pair: start location of right fixed entry
			# - trailing: selected station location
			if ( index_right > -1 and right_entry is not None and right_entry.type == CabEntryType.ChP ):
				target_cs = cs_variant
				pos_right = (cs_variant.lat, cs_variant.lon)
			else:
				if ( index_right > -1 ):
					target_cs = None
					pos_right = (right_entry.start_lat, right_entry.start_lon)
				else:
					target_cs = cs_variant
					pos_right = (cs_variant.lat, cs_variant.lon)

			# pure cab (single-station variant)
			feasible_candidates.extend(
				self._find_time_feasible_candidates_pure_cab(
					customer=customer,
					cab=cab,
					index_left=index_left,
					index_right=index_right,
					left_entry=left_entry,
					right_entry=right_entry,
					time_left=time_left,
					time_right=time_right,
					pos_left=pos_left,
					pos_right=pos_right,
					cs=target_cs,
				)
			)

			feasible_candidates.extend(
				self._find_time_feasible_candidates_convoy(
					customer=customer,
					cab=cab,
					index_left=index_left,
					index_right=index_right,
					left_entry=left_entry,
					right_entry=right_entry,
					time_left=time_left,
					time_right=time_right,
					pos_left=pos_left,
					pos_right=pos_right,
					target_cs=target_cs,
				)
			)
		
			
		return feasible_candidates

	# ---------------------------------------------------------

	def _adjust_entries_for_right_boundary(
		self,
		cab: CabVehicle,
		index_right: int,
		right_entry: CabScheduleEntry | None,
		new_entries: list[CabScheduleEntry],
	) -> tuple[list[CabScheduleEntry], int] | None:
		"""
		Adjust inserted entries against the right schedule boundary.

		Handles right-ChP station switches, bridging to the next fixed entry,
		and trailing fallback rebuilds after a switched ChP.
		Returns the updated entries plus the right replacement bound, or None
		if the adjusted suffix is infeasible.
		"""
		replace_until = index_right

		if right_entry is None or right_entry.type != CabEntryType.ChP:
			return new_entries, replace_until

		target_cs = new_entries[-1].charge_station
		if target_cs is None or right_entry.charge_station is target_cs:
			return new_entries, replace_until

		# the replaced ChP sits where the new approach ends
		approach_entry = new_entries[-1]
		new_right_entry = right_entry.clone_for_candidate()
		new_right_entry.charge_station = target_cs
		new_right_entry.start_lat = float(approach_entry.end_lat)
		new_right_entry.start_lon = float(approach_entry.end_lon)
		new_right_entry.end_lat = float(approach_entry.end_lat)
		new_right_entry.end_lon = float(approach_entry.end_lon)

		chp_active_duration_s = float(
			right_entry.end_time - right_entry.start_time - right_entry.service_time
		)
		charge_amount_raw = right_entry.charge_amount
		if charge_amount_raw is None:
			raise RuntimeError(
				"Missing charge_amount on right-side ChP entry during station replacement "
				f"(cab_id={cab.cab.id}, index_right={index_right})"
			)
		chp_amount_wh = float(charge_amount_raw)
		available_energy_wh = float(target_cs.station.max_supply) * (chp_active_duration_s / 3600.0)

		if available_energy_wh < chp_amount_wh:
			return None
		new_right_entry.max_charge_amount = max(chp_amount_wh, available_energy_wh)

		new_right_entry.start_charge = float(new_entries[-1].end_charge)
		new_right_entry.end_charge = float(new_right_entry.start_charge + chp_amount_wh)

		next_fixed_entry = self._find_next_fixed_entry_after_index(cab, index_right)
		if next_fixed_entry is not None:
			# bridge from moved chp to next fixed anchor
			next_start = (float(new_right_entry.end_lat), float(new_right_entry.end_lon))
			next_end = (float(next_fixed_entry.start_lat), float(next_fixed_entry.start_lon))
			next_res = self.router_cab.shortest_path(next_start, next_end)
			self.runtime_diagnostics.router_calls["cab"] += 1
			self.runtime_diagnostics.router_time_s["cab"] += next_res["query_time_s"]

			if new_right_entry.end_time + next_res["time_s"] > next_fixed_entry.start_time:
				return None

			new_entries.append(new_right_entry)

			if next_fixed_entry.type in (CabEntryType.CT, CabEntryType.FM):
				next_approach_type = CabEntryType.CA
				next_approach_cs = None
			elif next_fixed_entry.type == CabEntryType.ChP:
				next_approach_type = CabEntryType.ChA
				next_approach_cs = next_fixed_entry.charge_station
			elif next_fixed_entry.type in (CabEntryType.LM, CabEntryType.PT):
				raise RuntimeError(
					"PlA bridge after right-ChP station replacement is not implemented yet "
					"(future no-customer platoon approach case). "
					f"cab_id={cab.cab.id}, next_type={next_fixed_entry.type.name}"
				)
			else:
				raise RuntimeError(
					"Unsupported next fixed entry type after right-ChP replacement "
					f"(cab_id={cab.cab.id}, next_type={next_fixed_entry.type})"
				)

			# reuse next_start/next_end directly - both are already-fixed schedule points
			next_approach_start = next_start
			next_approach_end = next_end
			next_approach = CabScheduleEntry(
				_type=next_approach_type,
				_charging_s=next_approach_cs,
				_s_lat=float(next_approach_start[0]),
				_s_lon=float(next_approach_start[1]),
				_e_lat=float(next_approach_end[0]),
				_e_lon=float(next_approach_end[1]),
				_s_time=int(next_fixed_entry.start_time - next_res["time_s"]),
				_e_time=int(next_fixed_entry.start_time),
				_s_charge=float(new_right_entry.end_charge),
				_e_charge=float(new_right_entry.end_charge - next_res["energy_wh"]),
				_distance_m=float(next_res.get("distance_m", 0.0) or 0.0),
			)
			new_entries.append(next_approach)
			next_fixed_index = None
			for idx in range(index_right + 1, len(cab.schedule)):
				# object-identity lookup keeps exact boundary to preserved next fixed entry
				if cab.schedule[idx] is next_fixed_entry:
					next_fixed_index = idx
					break
			if next_fixed_index is None:
				raise RuntimeError(
					"Invariant violated: next_fixed_entry not found in schedule after index_right "
					f"(cab_id={cab.cab.id}, index_right={index_right}, schedule_len={len(cab.schedule)})"
				)
			# replace up to (but excluding) next fixed entry
			replace_until = next_fixed_index
		else:
			# no fixed entry after right_entry: rebuild trailing final approach and overwrite full suffix
			new_entries.append(new_right_entry)

			tail_start = (float(new_right_entry.end_lat), float(new_right_entry.end_lon))
			final_end_time = int(cab.end_schedule)
			final_cs_idx = nearest_straight_line(
				tail_start,
				[(sim_cs.lat, sim_cs.lon) for sim_cs in self.sim_charging_stations],
			)
			final_cs = self.sim_charging_stations[final_cs_idx]
			tail_end = (final_cs.lat, final_cs.lon)
			tail_res = self.router_cab.shortest_path(tail_start, tail_end)
			self.runtime_diagnostics.router_calls["cab"] += 1
			self.runtime_diagnostics.router_time_s["cab"] += tail_res["query_time_s"]

			# final ChA is non-fixed and horizon-glued, but do not compress time if route is unreachable
			tail_start_time = int(float(final_end_time) - float(tail_res["time_s"]))
			if tail_start_time < int(new_right_entry.end_time):
				return None

			tail_start_proj = tail_res["proj_orig"]
			tail_end_proj = tail_res["proj_dest"]
			new_tail_entry = CabScheduleEntry(
				_type=CabEntryType.ChA,
				_charging_s=final_cs,
				_s_lat=tail_start_proj[0],
				_s_lon=tail_start_proj[1],
				_e_lat=tail_end_proj[0],
				_e_lon=tail_end_proj[1],
				_s_time=tail_start_time,
				_e_time=final_end_time,
				_s_charge=float(new_right_entry.end_charge),
				_e_charge=float(new_right_entry.end_charge - tail_res["energy_wh"]),
				_distance_m=float(tail_res.get("distance_m", 0.0) or 0.0),
			)
			new_entries.append(new_tail_entry)
			# no next fixed entry exists -> overwrite complete trailing suffix
			replace_until = len(cab.schedule)

		return new_entries, replace_until

	# ---------------------------------------------------------

	def _find_time_feasible_candidates_pure_cab(self,
												customer: Customer,
												cab: CabVehicle,
												index_left: int,
												index_right: int,
												left_entry: CabScheduleEntry | None,
												right_entry: CabScheduleEntry | None,
												time_left: int,
												time_right: int,
												pos_left: tuple[float, float],
												pos_right: tuple[float, float],
												cs: SimChargingStation | None) -> list[NCCandidate]:
		'''
		Compute pure-cab candidate schedules for one gap and one station variant.

		Handles right-side ChP station replacement and schedule slice materialization.
		Returns time-feasible candidates only; energy is validated later.
		'''
		feasible_candidates: list[NCCandidate] = []
		pu = (float(customer.pu_snap_lat), float(customer.pu_snap_lon))
		do = (float(customer.do_snap_lat), float(customer.do_snap_lon))
		is_pickup_window = bool(customer.tw_type)
		service_time = int(customer.entry_time)
		time_avail = int(time_right - time_left)

		# fast reject: even without driving, service alone would not fit
		if 2 * service_time > time_avail:
			return feasible_candidates

		# ------------------------------------------------------------
		# 1) route the three movement legs (single place, no split legacy path)
		# ------------------------------------------------------------
		ca1_res = self.router_cab.shortest_path(pos_left, pu)
		ct_res = self.router_cab.shortest_path(pu, do)
		ca2_res = self.router_cab.shortest_path(do, pos_right)
		self.runtime_diagnostics.router_calls["cab"] += 3
		self.runtime_diagnostics.router_time_s["cab"] += ca1_res["query_time_s"]
		self.runtime_diagnostics.router_time_s["cab"] += ct_res["query_time_s"]
		self.runtime_diagnostics.router_time_s["cab"] += ca2_res["query_time_s"]

		ca1_t = float(ca1_res["time_s"])
		ct_t = float(ct_res["time_s"])
		ca2_t = float(ca2_res["time_s"])
		time_insert = ca1_t + ct_t + ca2_t + 2 * service_time

		# ------------------------------------------------------------
		# 2) evaluate time-window feasibility explicitly
		# ------------------------------------------------------------
		if is_pickup_window:
			# pickup must be reachable before tw closes
			pickup_feasible = (time_left + ca1_t + service_time) <= float(customer.tw_upper)
			# with pickup-window semantics, previous logic constrained latest completion by right bound
			window_feasible = (float(customer.tw_lower) + ct_t + 2 * service_time + ca2_t) <= float(time_right)
			fits_tw = pickup_feasible and window_feasible
			if not fits_tw:
				if not pickup_feasible:
					self.runtime_diagnostics.time_infeasible_by_reason["pickup_tw_early"] += 1
				else:
					self.runtime_diagnostics.time_infeasible_by_reason["dropoff_tw_late"] += 1
		else:
			# dropoff must be achievable before right bound (legacy semantics)
			dropoff_feasible = float(customer.tw_lower) <= (float(time_right) - ca2_t - service_time)
			# pickup-side upper bound must still be respected
			pickup_side_feasible = (float(time_left) + ca1_t + ct_t + 2 * service_time) <= float(customer.tw_upper)
			fits_tw = dropoff_feasible and pickup_side_feasible
			if not fits_tw:
				if not pickup_side_feasible:
					self.runtime_diagnostics.time_infeasible_by_reason["pickup_tw_early"] += 1
				else:
					self.runtime_diagnostics.time_infeasible_by_reason["dropoff_tw_late"] += 1

		if not (time_insert <= time_avail and fits_tw):
			return feasible_candidates

		# ------------------------------------------------------------
		# 3) derive inserted entry times and energy levels
		# ------------------------------------------------------------
		# Keep the final approach glued to the right boundary/horizon.
		# Immediate final approach is an explicit timing choice, not a
		# consequence of having no right schedule entry.
		start_fa, end_fa, start_ct, end_ct, start_sa, end_sa = self._find_insertion_times(
			customer,
			time_left,
			time_right,
			ca1_t,
			ct_t,
			ca2_t,
			False,
		)

		energy_left = left_entry.end_charge if (index_left > -1 and left_entry is not None) else cab.current_charge
		charge = (
			float(energy_left),
			float(energy_left - ca1_res["energy_wh"]),
			float(energy_left - ca1_res["energy_wh"] - ct_res["energy_wh"]),
			float(energy_left - ca1_res["energy_wh"] - ct_res["energy_wh"] - ca2_res["energy_wh"]),
		)
		# ------------------------------------------------------------
		# 4) build core inserted entries with local continuity invariants
		# ------------------------------------------------------------
		new_entries: list[CabScheduleEntry] = []

		# Entries at a customer address record the router's projected point. Both
		# sides of a shared boundary use the projection from the same result, so
		# their coordinates agree exactly.
		pu_proj = ca1_res["proj_dest"]
		do_proj = ct_res["proj_dest"]
		# distance from the requested address to these points, recorded as a
		# candidate quality dimension and copied to the customer's sim state at dispatch
		pu_offset = customer.pu_offset_m
		do_offset = customer.do_offset_m

		new_ca_entry = CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_lat=float(pos_left[0]),
			_s_lon=float(pos_left[1]),
			_e_lat=float(pu_proj[0]),
			_e_lon=float(pu_proj[1]),
			_s_time=int(start_fa),
			_e_time=int(end_fa),
			_s_charge=charge[0],
			_e_charge=charge[1],
			_distance_m=float(ca1_res.get("distance_m", 0.0) or 0.0),
		)

		new_ct_entry = CabScheduleEntry(
			_type=CabEntryType.CT,
			_cust=customer,
			_s_lat=float(pu_proj[0]),
			_s_lon=float(pu_proj[1]),
			_e_lat=float(do_proj[0]),
			_e_lon=float(do_proj[1]),
			_s_time=int(start_ct),
			_e_time=int(end_ct),
			_serv_t=2 * service_time,
			_s_charge=charge[1],
			_e_charge=charge[2],
			_distance_m=float(ct_res.get("distance_m", 0.0) or 0.0),
		)

		if index_right > -1:
			if right_entry is None:
				raise RuntimeError(
					"Invariant violated: index_right indicates right entry, but right_entry is None "
					f"(cab_id={cab.cab.id}, index_right={index_right}, schedule_len={len(cab.schedule)})"
				)
			if right_entry.type == CabEntryType.CT:
				new_type = CabEntryType.CA
				new_cha = None
			elif right_entry.type == CabEntryType.ChP:
				new_type = CabEntryType.ChA
				new_cha = cs
			elif right_entry.type == CabEntryType.FM:
				new_type = CabEntryType.CA
				new_cha = None
			else:
				raise RuntimeError(f"Right entry type {right_entry.type} not treated yet")
		else:
			# trailing insertion always ends in charging approach
			new_type = CabEntryType.ChA
			new_cha = cs

		sca_end = ca2_res["proj_dest"]
		new_sca_entry = CabScheduleEntry(
			_type=new_type,
			_charging_s=new_cha,
			_s_lat=float(do_proj[0]),
			_s_lon=float(do_proj[1]),
			_e_lat=float(sca_end[0]),
			_e_lon=float(sca_end[1]),
			_s_time=int(start_sa),
			_e_time=int(end_sa),
			_s_charge=charge[2],
			_e_charge=charge[3],
			_distance_m=float(ca2_res.get("distance_m", 0.0) or 0.0),
			)

		new_entries.extend([new_ca_entry, new_ct_entry, new_sca_entry])
		service_entries = list(new_entries)

		adjusted_result = self._adjust_entries_for_right_boundary(
			cab=cab,
			index_right=index_right,
			right_entry=right_entry,
			new_entries=new_entries,
		)
		if adjusted_result is None:
			return feasible_candidates
		new_entries, replace_until = adjusted_result
		# cab.schedule is still the original schedule here; this slice is the
		# old material that will be overwritten by new_entries.
		if index_right > -1:
			old_replacement_entries = cab.schedule[index_left + 1:replace_until]
		else:
			old_replacement_entries = cab.schedule[index_left + 1:]

		# ------------------------------------------------------------
		# 6) materialize candidate schedule
		# ------------------------------------------------------------
		new_cab_schedule = [entry.clone_for_candidate() for entry in cab.schedule]
		if index_right > -1:
			# end-exclusive slice: preserve the original right boundary unless a
			# right-ChP station switch extends replacement up to a later boundary
			new_cab_schedule[index_left + 1:replace_until] = new_entries
		else:
			# trailing mode: replace everything after left boundary
			new_cab_schedule[index_left + 1:] = new_entries

		new_cand = NCCandidate(customer, cab)
		new_cand.mode = "pure"
		new_cand.new_cab_schedule = new_cab_schedule
		new_cand.cab_insert_position_left = index_left
		new_cand.cab_insert_position_right = index_left + 1 + len(new_entries)
		new_cand.metrics.time_customer_direct_s = (
			float(ct_res["time_s"]) + 2 * float(service_time)
		)
		new_cand.metrics.distance_customer_direct_m = float(ct_res["distance_m"])
		new_cand.metrics.distance_customer_pickup_offset_m = float(pu_offset)
		new_cand.metrics.distance_customer_dropoff_offset_m = float(do_offset)
		new_cand.metrics.record_inserted_entries(
			service_entries,
			"pure",
			customer,
			old_replacement_entries=old_replacement_entries,
			new_replacement_entries=new_entries,
			is_trailing_replacement=(index_right == -1),
		)
		feasible_candidates.append(new_cand)

		return feasible_candidates
	
	# ---------------------------------------------------------

	def _shift_schedule_charge(self, schedule: list[CabScheduleEntry], delta: float):
		'''
		Progresses all charge levels in the schedule by a value delta
		'''
		for entry in schedule:
			entry.start_charge += delta
			entry.end_charge += delta
		
		return

	# ---------------------------------------------------------
	
	def _find_insertion_times(self, customer: Customer, time_left: int, time_right: int, t_fa: int, t_ct: int, t_sa: int, final_approach_immediately: bool=False) -> list[int]:
		'''
		Determine times for insertion of the necessary trips
		 - Assumption: Trip has been veryfied as feasible above
		 - final_approach_immediately controls whether the post-service approach
		   starts right after service or is glued to the right boundary
		'''
		tw_type  = customer.tw_type		# True=Pickup
		tw_lower = customer.tw_lower
		tw_upper = customer.tw_upper
		service  = customer.entry_time
		
		# First Customer Approach and Transport
		# pick up case
		if ( tw_type ):
			# arrive at customer at the start of the tw
			if ( time_left + t_fa < tw_lower ):
				end_fa = tw_lower
				start_fa = end_fa - t_fa
			# start trip immediately
			else:
				start_fa = time_left
				end_fa = start_fa + t_fa
			# terminate customer transport directly after the approach
			start_ct = end_fa
			end_ct = start_ct + t_ct + 2*service
		# drop off case
		else:
			# arrive at drop off at the start of the tw
			# (adding one service time is enough to trigger this case)
			if ( time_left + t_fa + t_ct + 1*service < tw_lower ):
				end_ct = tw_lower + service
				start_ct = end_ct - t_ct - 2*service
			# start trip immediately
			else:
				end_ct = time_left + t_fa + t_ct + 2*service
				start_ct = end_ct - t_ct - 2*service
			# terminate customer approach directly before the transport
			end_fa = start_ct
			start_fa = end_fa - t_fa
		
		# Second Customer approach
		if ( final_approach_immediately ):
			# Explicit timing choice: perform the final approach directly after
			# serving the customer.
			start_sa = end_ct
			end_sa = start_sa + t_sa
		else:
			# just glue second approach to right entry
			end_sa = time_right
			start_sa = end_sa - t_sa
		
		return start_fa, end_fa, start_ct, end_ct, start_sa, end_sa
		
	# ---------------------------------------------------------

	def _find_convoy_insertion_times(self, customer: Customer, time_left: int, time_right: int,
									t_ca: int, t_fm: int, t_lm: int, t_sa: int,
									pt_start: int, pt_end: int, t_lm_pre_drop: int,
									final_approach_immediately: bool=False) -> tuple[int, ...] | None:
		'''
		Determine concrete times for a feasible convoy insertion.
		 - PT is fixed by the chosen Pro trip
		 - left side may shift earlier for PU windows
		 - right side may shift later for DO windows
		 - final_approach_immediately controls whether the post-service approach
		   starts right after service or is glued to the right boundary
		 - NOTE: t_lm_pre_drop is just t_lm - service_time. It's just here to mirror its usage inside _select_convoy_trip_candidates
		'''
		service = customer.entry_time

		earliest_start_fm = time_left + t_ca
		latest_start_fm = pt_start - t_fm

		earliest_start_lm = pt_end
		latest_start_lm = time_right - t_sa - t_lm

		if customer.tw_type:
			earliest_start_fm = max(earliest_start_fm, customer.tw_lower)
			latest_start_fm = min(latest_start_fm, customer.tw_upper - service)
		else:
			earliest_start_lm = max(earliest_start_lm, customer.tw_lower - t_lm_pre_drop)
			latest_start_lm = min(latest_start_lm, customer.tw_upper - t_lm)

		if earliest_start_fm > latest_start_fm:
			return None
		if earliest_start_lm > latest_start_lm:
			return None

		start_fm = latest_start_fm
		end_fm = start_fm + t_fm
		start_ca = start_fm - t_ca
		end_ca = start_fm

		start_pt = pt_start
		end_pt = pt_end

		start_lm = earliest_start_lm
		end_lm = start_lm + t_lm

		if final_approach_immediately:
			start_sa = end_lm
			end_sa = start_sa + t_sa
		else:
			end_sa = time_right
			start_sa = end_sa - t_sa

		return (
			int(start_ca), int(end_ca),
			int(start_fm), int(end_fm),
			int(start_pt), int(end_pt),
			int(start_lm), int(end_lm),
			int(start_sa), int(end_sa),
		)

	# ---------------------------------------------------------
	
	def _find_charge_levels(self, cab: CabVehicle, customer: Customer, left: CabScheduleEntry, right: CabScheduleEntry, d_fa: float, d_ct: float, d_sa: float):
		'''
		WARNING Deprecated, since provided by the Router???
		'''
		lb, ub = cab.charge_lb, cab.charge_ub
		cons = cab.consumption

		# Start-Charge:
		if left is not None:
			c_start = left.end_charge
		else:
			c_start = cab.current_charge

		c_after_fa = c_start - d_fa*cons
		c_after_ct = c_after_fa - d_ct*cons
		c_after_sa = c_after_ct - d_sa*cons
			
		return (c_start, c_after_fa, c_after_ct, c_after_sa)

	# ---------------------------------------------------------

	def _find_time_feasible_candidates_convoy(
		self,
		customer: Customer,
		cab: CabVehicle,
		index_left: int,
		index_right: int,
		left_entry: CabScheduleEntry | None,
		right_entry: CabScheduleEntry | None,
		time_left: int,
		time_right: int,
		pos_left: tuple[float, float],
		pos_right: tuple[float, float],
		target_cs: SimChargingStation | None,
	) -> list[NCCandidate]:
		"""
		Generate convoy candidates (FM -> PT -> LM) for a gap using scheduled Pro trips.
		"""
		if not self.pro_lines:
			return []

		feasible_candidates: list[NCCandidate] = []
		service = int(customer.entry_time)

		ca1_res = self.router_cab.shortest_path(pos_left, (customer.pu_snap_lat, customer.pu_snap_lon))
		sa_res = self.router_cab.shortest_path((customer.do_snap_lat, customer.do_snap_lon), pos_right)
		self.runtime_diagnostics.router_calls["cab"] += 2
		self.runtime_diagnostics.router_time_s["cab"] += ca1_res["query_time_s"]
		self.runtime_diagnostics.router_time_s["cab"] += sa_res["query_time_s"]
		t_ca = int(ca1_res["time_s"])
		t_sa = int(sa_res["time_s"])

		energy_left = left_entry.end_charge if (index_left > -1) else cab.current_charge
		direct_customer_time_s = None
		direct_customer_distance_m = None

		for trip_ctx in self._select_convoy_trip_candidates(
			customer=customer,
			cab=cab,
			time_left=time_left,
			time_right=time_right,
			t_ca=t_ca,
			t_sa=t_sa,
		):
			pro_trip = trip_ctx.trip
			chain_loc = trip_ctx.chain_loc
			unchain_loc = trip_ctx.unchain_loc
			chain_add = int(chain_loc.location.additional_time)
			unchain_add = int(unchain_loc.location.additional_time)
			fm_res = trip_ctx.fm_res
			lm_res = trip_ctx.lm_res
			t_fm = int(service + int(fm_res["time_s"]) + chain_add)
			t_lm = int(unchain_add + int(lm_res["time_s"]) + service)
			t_lm_pre_drop = int(unchain_add + int(lm_res["time_s"]))

			pt_start = int(pro_trip.start_time)
			pt_end = int(pro_trip.end_time)

			# Keep the final approach glued to the right boundary/horizon.
			# Immediate final approach is an explicit timing choice, not a
			# consequence of having no right schedule entry.
			times = self._find_convoy_insertion_times(
				customer=customer,
				time_left=time_left,
				time_right=time_right,
				t_ca=t_ca,
				t_fm=t_fm,
				t_lm=t_lm,
				t_sa=t_sa,
				pt_start=pt_start,
				pt_end=pt_end,
				t_lm_pre_drop=t_lm_pre_drop,
				final_approach_immediately=False,
			)
			if times is None:
				continue

			(
				start_ca, end_ca,
				start_fm, end_fm,
				start_pt, end_pt,
				start_lm, end_lm,
				start_sa, end_sa,
			) = times

			new_type = None
			new_cha = None
			if index_right > -1:
				if right_entry.type == CabEntryType.CT:
					new_type = CabEntryType.CA
				elif right_entry.type == CabEntryType.ChP:
					if target_cs is None:
						raise RuntimeError(
							"Invariant violated: right ChP convoy candidate requires a target charging station "
							f"(cab_id={cab.cab.id}, index_right={index_right})"
						)
					new_type = CabEntryType.ChA
					new_cha = target_cs
				elif right_entry.type == CabEntryType.FM:
					new_type = CabEntryType.CA
				else:
					continue
			else:
				if target_cs is None:
					raise RuntimeError(
						"Invariant violated: trailing convoy candidate requires a target charging station "
						f"(cab_id={cab.cab.id})"
					)
				new_type = CabEntryType.ChA
				new_cha = target_cs

			e0 = float(energy_left)
			e1 = e0 - float(ca1_res["energy_wh"])
			e2 = e1 - float(fm_res["energy_wh"])
			e3 = e2
			e4 = e3 - float(lm_res["energy_wh"])
			e5 = e4 - float(sa_res["energy_wh"])
			pt_max_charge_amount = (
				max(0.0, float(pt_end - pt_start))
				* float(trip_ctx.pro_vehicle.cab_recharge_power)
				/ 3600.0
			)

			new_entries: list[CabScheduleEntry] = []

			# As in the direct-trip path. pu_proj comes from ca1_res (computed once
			# outside this loop), do_proj from this trip's lm_res, reused for LM's
			# end and the final entry's start.
			pu_proj = ca1_res["proj_dest"]
			do_proj = lm_res["proj_dest"]
			# chain_proj is FM's end and PT's start, unchain_proj is PT's end and
			# LM's start. PT keeps the Pro's trip distance and route.
			chain_proj = fm_res["proj_dest"]
			unchain_proj = lm_res["proj_orig"]
			# offsets as in the direct-trip path; early unchaining does not change the dropoff point
			pu_offset = customer.pu_offset_m
			do_offset = customer.do_offset_m

			new_entries.append(
				CabScheduleEntry(
					_type=CabEntryType.CA,
					_s_lat=pos_left[0], _s_lon=pos_left[1],
					_e_lat=pu_proj[0], _e_lon=pu_proj[1],
					_s_time=int(start_ca), _e_time=int(end_ca),
					_s_charge=e0, _e_charge=e1,
					_distance_m=float(ca1_res.get("distance_m", 0.0) or 0.0),
				)
			)
			new_entries.append(
				CabScheduleEntry(
					_type=CabEntryType.FM,
					_cust=customer,
					_s_lat=pu_proj[0], _s_lon=pu_proj[1],
					_e_lat=chain_proj[0], _e_lon=chain_proj[1],
					_s_time=int(start_fm), _e_time=int(end_fm),
					_serv_t=int(service + chain_add),
					_s_charge=e1, _e_charge=e2,
					_distance_m=float(fm_res.get("distance_m", 0.0) or 0.0),
				)
			)
			new_entries.append(
				CabScheduleEntry(
					_type=CabEntryType.PT,
					_cust=customer,
					_s_lat=chain_proj[0], _s_lon=chain_proj[1],
					_e_lat=unchain_proj[0], _e_lon=unchain_proj[1],
					_s_time=int(start_pt), _e_time=int(end_pt),
					_s_charge=e2, _e_charge=e3,
					_distance_m=float(pro_trip.distance_m),
					_route = pro_trip.route,
					_charge_amount=0.0,
					_max_charge_amount=float(pt_max_charge_amount),
					_pro=trip_ctx.pro_vehicle,
				)
			)
			new_entries.append(
				CabScheduleEntry(
					_type=CabEntryType.LM,
					_cust=customer,
					_s_lat=unchain_proj[0], _s_lon=unchain_proj[1],
					_e_lat=do_proj[0], _e_lon=do_proj[1],
					_s_time=int(start_lm), _e_time=int(end_lm),
					_serv_t=int(unchain_add + service),
					_s_charge=e3, _e_charge=e4,
					_distance_m=float(lm_res.get("distance_m", 0.0) or 0.0),
				)
			)
			sca_end = sa_res["proj_dest"]
			new_entries.append(
				CabScheduleEntry(
					_type=new_type,
					_charging_s=new_cha,
					_s_lat=do_proj[0], _s_lon=do_proj[1],
					_e_lat=sca_end[0], _e_lon=sca_end[1],
					_s_time=int(start_sa), _e_time=int(end_sa),
					_s_charge=e4, _e_charge=e5,
					_distance_m=float(sa_res.get("distance_m", 0.0) or 0.0),
				)
			)
			service_entries = list(new_entries)

			adjusted_result = self._adjust_entries_for_right_boundary(
				cab=cab,
				index_right=index_right,
				right_entry=right_entry,
				new_entries=new_entries,
			)

			if adjusted_result is None:
				continue
			new_entries, replace_until = adjusted_result
			# cab.schedule is still the original schedule here; this slice is the
			# old material that will be overwritten by new_entries.
			if index_right > -1:
				old_replacement_entries = cab.schedule[index_left + 1:replace_until]
			else:
				old_replacement_entries = cab.schedule[index_left + 1:]

			new_cab_schedule = [entry.clone_for_candidate() for entry in cab.schedule]
			new_start = index_left
			new_end = index_left + len(new_entries)

			if index_right > -1:
				new_cab_schedule[index_left+1:replace_until] = new_entries
			else:
				new_cab_schedule[index_left+1:] = new_entries

			new_cand = NCCandidate(customer, cab)
			new_cand.mode = "convoy"
			new_cand.selected_pro_trip = pro_trip
			new_cand.pro = trip_ctx.pro_vehicle
			new_cand.new_cab_schedule = new_cab_schedule
			new_cand.cab_insert_position_left = new_start
			new_cand.cab_insert_position_right = new_end + 1
			if direct_customer_time_s is None:
				direct_res = self.router_cab.shortest_path(
					(customer.pu_snap_lat, customer.pu_snap_lon),
					(customer.do_snap_lat, customer.do_snap_lon),
				)
				self.runtime_diagnostics.router_calls["cab"] += 1
				self.runtime_diagnostics.router_time_s["cab"] += direct_res["query_time_s"]
				direct_customer_time_s = (
					float(direct_res["time_s"]) + 2 * float(service)
				)
				direct_customer_distance_m = float(direct_res["distance_m"])
			new_cand.metrics.time_customer_direct_s = direct_customer_time_s
			new_cand.metrics.distance_customer_direct_m = direct_customer_distance_m
			new_cand.metrics.distance_customer_pickup_offset_m = float(pu_offset)
			new_cand.metrics.distance_customer_dropoff_offset_m = float(do_offset)
			new_cand.metrics.record_inserted_entries(
				service_entries,
				"convoy",
				customer,
				old_replacement_entries=old_replacement_entries,
				new_replacement_entries=new_entries,
				is_trailing_replacement=(index_right == -1),
			)
			feasible_candidates.append(new_cand)
			if self.parameters.get("algorithm", {}).get("early_unchaining", {}).get("enabled", True):
				candidate_early_stopping, changed, new_entries_early_stopping = self.check_candidate_for_early_stopping(convoy_candidate=new_cand.clone_for_early_unchaining())
				if changed:
					candidate_early_stopping.early_unchaining = True
					early_service_entries = new_entries_early_stopping[:len(service_entries)]
					candidate_early_stopping.metrics.time_customer_direct_s = direct_customer_time_s
					candidate_early_stopping.metrics.distance_customer_direct_m = direct_customer_distance_m
					candidate_early_stopping.metrics.distance_customer_pickup_offset_m = float(pu_offset)
					candidate_early_stopping.metrics.distance_customer_dropoff_offset_m = float(do_offset)
					candidate_early_stopping.metrics.record_inserted_entries(
						service_entries=early_service_entries,
						mode="convoy",
						customer=customer,
						old_replacement_entries=old_replacement_entries,
						new_replacement_entries=new_entries_early_stopping,
						is_trailing_replacement=(index_right == -1),
					)
					feasible_candidates.append(candidate_early_stopping)

		return feasible_candidates

	# ---------------------------------------------------------

	def check_candidate_for_early_stopping(self, convoy_candidate):
		# find platoon transport and last mile entries
		new_entries = []
		for index in range(convoy_candidate.cab_insert_position_left + 1, convoy_candidate.cab_insert_position_right):
			new_entries.append(convoy_candidate.new_cab_schedule[index])
			if convoy_candidate.new_cab_schedule[index].type == CabEntryType.PT: 
				platoon_transport = convoy_candidate.new_cab_schedule[index] 
			elif convoy_candidate.new_cab_schedule[index].type == CabEntryType.LM:
				last_mile = convoy_candidate.new_cab_schedule[index]
				last_mile_index = index
		request = convoy_candidate.req 
		# compute shortest possible path from node on convoy route to do point
		if request.tw_type or not (last_mile.start_time > platoon_transport.end_time):
			lm_end_lat_lon = (last_mile.end_lat, last_mile.end_lon) 
			best_pt_end_lat_lon =  (platoon_transport.end_lat, platoon_transport.end_lon)
			shortest_path = self.router_cab.shortest_path(best_pt_end_lat_lon, lm_end_lat_lon)
			self.runtime_diagnostics.router_calls["cab"] += 1
			self.runtime_diagnostics.router_time_s["cab"] += shortest_path["query_time_s"]
			shortest_duration = shortest_path["time_s"]
			changed = False
			for pt_end_lat_lon in platoon_transport.route:
				path = self.router_cab.shortest_path(pt_end_lat_lon, lm_end_lat_lon)
				self.runtime_diagnostics.router_calls["cab"] += 1
				self.runtime_diagnostics.router_time_s["cab"] += path["query_time_s"]
				duration = path["time_s"]

				if duration < shortest_duration:
					shortest_duration = duration
					shortest_path = path
					best_pt_end_lat_lon = pt_end_lat_lon
					unchain_loc = pt_end_lat_lon
					changed = True

			if changed:
				# get new pro path 
				pt_start_lat_lon = (platoon_transport.start_lat, platoon_transport.start_lon)
				new_pro_path = self.router_pro.shortest_path(pt_start_lat_lon, best_pt_end_lat_lon)
				self.runtime_diagnostics.router_calls["pro"] += 1
				self.runtime_diagnostics.router_time_s["pro"] += new_pro_path["query_time_s"]
				new_pt_end_time = platoon_transport.start_time + new_pro_path["time_s"]
				# Temporary guard while original PTs still use timetable durations.
				# Remove once Pro trips are timed consistently with our router.
				if new_pt_end_time > platoon_transport.end_time:
					return convoy_candidate, False, new_entries
				# update candidate and entries for new unchaing location
				platoon_transport.early_unchaining = True
				platoon_transport.end_lat = best_pt_end_lat_lon[0]
				platoon_transport.end_lon = best_pt_end_lat_lon[1]
				platoon_transport.end_time = new_pt_end_time
				platoon_transport.distance_m = new_pro_path["distance_m"]
				platoon_transport.route = new_pro_path["path_lat_lon"]
				pt_max_charge_amount = (
										max(0.0, float(platoon_transport.get_duration()))
										* float(platoon_transport.pro.cab_recharge_power)
										/ 3600.0
									)

				platoon_transport.max_charge_amount = pt_max_charge_amount
				last_mile.start_lat = best_pt_end_lat_lon[0]
				last_mile.start_lon = best_pt_end_lat_lon[1]
				last_mile.distance_m = shortest_path["distance_m"]
				last_mile.end_charge = last_mile.start_charge - shortest_path["energy_wh"]

				start_charge = last_mile.end_charge
				# get_energy_delta() reads (end_charge - start_charge) live off the
				# entry, so it must be snapshotted *before* start_charge is
				# overwritten below - calling it after would return
				# (old_end_charge - new_start_charge), which algebraically forces
				# end_charge back to the stale old_end_charge regardless of the new
				# start_charge, fabricating an energy delta with no physical basis.
				# Snapshotting the delta first preserves this entry's real,
				# distance/duration-based consumption while re-chaining it onto the
				# new upstream charge level.
				# NOTE: this whole function is superseded by compute_optimal_unchain_pos
				# on the (unmerged) feasibility_early_unchaining branch, which builds
				# every entry's charge fresh instead of patching it in place and does
				# not have this bug. When that branch merges, this function - and this
				# fix along with it - is expected to disappear; if that merge instead
				# conflicts here, resolve by taking the incoming deletion.
				for index in range(last_mile_index + 1, convoy_candidate.cab_insert_position_right):
					entry = convoy_candidate.new_cab_schedule[index]
					energy_delta = entry.get_energy_delta()
					entry.start_charge = start_charge
					entry.end_charge = entry.start_charge + energy_delta
					start_charge = entry.end_charge


				# compute last mile start with logic as in _find_convoy_insertion_times
				if request.tw_type:
					last_mile.start_time = platoon_transport.end_time
					last_mile.end_time = last_mile.start_time + shortest_duration + last_mile.service_time
				else:
					earliest_start_lm = max(platoon_transport.end_time, 
											request.tw_lower - shortest_duration - last_mile.service_time + request.entry_time)
					last_mile.start_time = earliest_start_lm
					last_mile.end_time = last_mile.start_time + shortest_duration + last_mile.service_time

		else:
			convoy_candidate, changed = None, False
		
		return convoy_candidate, changed, new_entries


	# ---------------------------------------------------------

	def _apply_convoy_pt_charging(self, cab: CabVehicle, candidate: NCCandidate) -> float:
		'''
		Apply usable moving charge to inserted PT entries after the neutral
		baseline energy has already been materialized.
		'''
		if candidate.mode != "convoy":
			return 0.0

		schedule = candidate.new_cab_schedule
		start_idx = max(0, int(candidate.cab_insert_position_left) + 1)
		end_idx = min(len(schedule), int(candidate.cab_insert_position_right))
		energy_eps_wh = float(self.parameters["algorithm"]["energy_tolerance_wh"])

		pt_options: list[tuple[int, float, float]] = []
		suffix_max_energy = float("-inf")

		# Single decision scan: for every inserted PT, record its available
		# charge and the future upper-bound room from PT end onward.
		for entry_index in range(len(schedule) - 1, start_idx - 1, -1):
			entry = schedule[entry_index]
			is_inserted_pt = (
				start_idx <= entry_index < end_idx
				and entry.type == CabEntryType.PT
			)

			if is_inserted_pt:
				if entry.charge_amount is None:
					raise RuntimeError(
						"Missing charge_amount on inserted PT entry "
						f"(cab_id={cab.cab.id}, schedule_index={entry_index})"
					)
				if entry.max_charge_amount is None:
					raise RuntimeError(
						"Missing max_charge_amount on inserted PT entry "
						f"(cab_id={cab.cab.id}, schedule_index={entry_index})"
					)

				used_charge = float(entry.charge_amount)
				max_charge = float(entry.max_charge_amount)
				if used_charge < -energy_eps_wh:
					raise RuntimeError(
						"Negative charge_amount on inserted PT entry "
						f"(cab_id={cab.cab.id}, schedule_index={entry_index})"
					)
				if max_charge + energy_eps_wh < used_charge:
					raise RuntimeError(
						"max_charge_amount below charge_amount on inserted PT entry "
						f"(cab_id={cab.cab.id}, schedule_index={entry_index}, "
						f"max={max_charge}, used={used_charge})"
					)

				future_max_energy = max(float(entry.end_charge), suffix_max_energy)
				future_room = max(0.0, float(cab.charge_ub) - future_max_energy)
				available_charge = max(0.0, max_charge - used_charge)
				pt_options.append((entry_index, available_charge, future_room))

			suffix_max_energy = max(
				suffix_max_energy,
				float(entry.start_charge),
				float(entry.end_charge),
			)

		if not pt_options:
			return 0.0

		planned_charge_by_index: dict[int, float] = {}
		total_added = 0.0

		for entry_index, available_charge, future_room in reversed(pt_options):
			remaining_room = max(0.0, future_room - total_added)
			charge_to_add = min(available_charge, remaining_room)
			if charge_to_add > energy_eps_wh:
				planned_charge_by_index[entry_index] = charge_to_add
				total_added += charge_to_add

		if total_added <= energy_eps_wh:
			return 0.0

		cumulative_added = 0.0
		for entry_index in range(start_idx, len(schedule)):
			entry = schedule[entry_index]
			entry.start_charge += cumulative_added
			entry.end_charge += cumulative_added

			charge_to_add = planned_charge_by_index.get(entry_index, 0.0)
			if charge_to_add > 0.0:
				entry.charge_amount = float(entry.charge_amount) + charge_to_add
				entry.end_charge += charge_to_add
				cumulative_added += charge_to_add

		return total_added

	# ---------------------------------------------------------

	def _add_candidate_energy_metrics(self, cab: CabVehicle, candidate: NCCandidate) -> None:
		stats = candidate.energy_stats
		schedule = candidate.new_cab_schedule
		start_idx = max(0, int(candidate.cab_insert_position_left) + 1)
		end_idx = min(len(schedule), int(candidate.cab_insert_position_right))
		stationary_charging_power_w = (
			min(float(sim_cs.station.max_supply) for sim_cs in self.sim_charging_stations)
			if self.sim_charging_stations else None
		)

		candidate.metrics.record_energy_result(
			cab=cab,
			schedule=schedule,
			energy_stats=stats,
			inserted_start_idx=start_idx,
			inserted_end_idx=end_idx,
			stationary_charging_power_w=stationary_charging_power_w,
		)
		return

	# ---------------------------------------------------------
	
	def _find_energy_feasible_candidates(self, cab: CabVehicle, candidates: list[NCCandidate] | None = None) -> list[NCCandidate]:
		'''
		Check energy feasibility for candidate list
		Create candidate schedules for time feasible options
		TODO: Use repair mechanism in case of infeasiblity (just returns the unchanged candidate atm)
		'''
		if candidates is None:
			candidates = []

		lb = cab.charge_lb
		ub = cab.charge_ub

		feasible_candidates: list[NCCandidate] = []

		for candidate in candidates:
			index_left = candidate.cab_insert_position_left #if candidate.cab_insert_position_left>-1 else 0
			index_right = candidate.cab_insert_position_right
			schedule = candidate.new_cab_schedule

			# finalize schedule (update energy level of entries after the new inserted entries)
			if index_right != len(schedule):
				delta_charge = schedule[index_right-1].end_charge  - schedule[index_right].start_charge
			else: 
				delta_charge = 0.0
			
			stats = self._compute_schedule_energy_stats(schedule, lb, ub, index_left, index_right, delta_charge)
			added_pt_charge = self._apply_convoy_pt_charging(cab, candidate)
			if added_pt_charge > 0.0:
				stats = self._compute_schedule_energy_stats(schedule, lb, ub, index_left, index_right, 0.0)
			candidate.energy_stats = stats
			
			if stats.is_feasible:
				self._add_candidate_energy_metrics(cab, candidate)
				feasible_candidates.append(candidate)
				candidate_type = str(candidate.mode)
				if candidate_type == "convoy" and candidate.early_unchaining:
					candidate_type = "convoy_early_unchaining"
				self.runtime_diagnostics.energy_feasible_by_type[candidate_type] += 1
			else:
				initial_violation = stats.first_violation_type or "unknown"
				candidate = self._try_energy_repair(cab,candidate)
				if candidate.energy_stats.is_feasible:
					self._add_candidate_energy_metrics(cab, candidate)
					feasible_candidates.append(candidate)
					candidate_type = str(candidate.mode)
					if candidate_type == "convoy" and candidate.early_unchaining:
						candidate_type = "convoy_early_unchaining"
					self.runtime_diagnostics.energy_feasible_by_type[candidate_type] += 1
					self.runtime_diagnostics.energy_repaired_by_type[candidate_type] += 1
				else:
					reject_reason = f"energy:{initial_violation}"
					self.runtime_diagnostics.infeasible_by_reason[reject_reason] += 1
			
		return feasible_candidates
		
	# ---------------------------------------------------------

	def _compute_schedule_energy_stats(self,
									schedule,
									lb: float, ub: float,
									index_left: int, index_right: int,
									delta: float) -> ScheduleEnergyStats:
		'''
		Apply a charge delta to the affected suffix and compute the resulting
		energy statistics for the schedule segment after index_left.
		'''
		energy_samples: list[tuple[int, float, float]] = []

		for i, entry in enumerate(schedule[index_left+1:]):
			index = i + index_left+1

			if index >= index_right:
				entry.start_charge += delta
				entry.end_charge += delta

			energy_samples.append((index, entry.start_time, entry.start_charge))
			energy_samples.append((index, entry.end_time, entry.end_charge))

		return self._energy_stats_from_energy_samples(energy_samples, lb, ub)
	
	# ---------------------------------------------------------

	def _energy_stats_from_energy_samples(self,
										  energy_samples: list[tuple[int, float, float]],
										  lb: float, ub: float) -> ScheduleEnergyStats:
		'''
		Reduce already materialized energy samples to ScheduleEnergyStats.
		Callers decide whether producing those samples also mutates the schedule.
		'''
		min_energy = float("inf")
		max_energy = float("-inf")
		min_energy_time = None
		max_energy_time = None

		worst_under_amount = 0.0
		worst_under_time = None
		worst_under_index  = None

		worst_over_amount = 0.0
		worst_over_time = None
		worst_over_index = None

		first_violation_time = None
		first_violation_type = None
		first_violation_index = None

		is_feasible = True
		energy_timeseries: list[tuple[float, float]] = []

		for index, t, charge in energy_samples:
			energy_timeseries.append((t, charge))

			if charge < min_energy:
				min_energy, min_energy_time = charge, t
			if charge > max_energy:
				max_energy, max_energy_time = charge, t

			if charge < lb:
				is_feasible = False
				under_amount = lb - charge

				if first_violation_time is None or t < first_violation_time:
					first_violation_time = t
					first_violation_type = "under"
					first_violation_index = index

				if under_amount > worst_under_amount:
					worst_under_amount = under_amount
					worst_under_time = t
					worst_under_index = index

			elif charge > ub:
				is_feasible = False
				over_amount = charge - ub

				if first_violation_time is None or t < first_violation_time:
					first_violation_time = t
					first_violation_type = "over"
					first_violation_index = index

				if over_amount > worst_over_amount:
					worst_over_amount = over_amount
					worst_over_time = t
					worst_over_index = index

		if min_energy == float("inf"):
			min_energy = lb
		if max_energy == float("-inf"):
			max_energy = lb

		required_additional_charge = max(0.0, lb - min_energy)
		required_additional_discharge = max(0.0, max_energy - ub)

		margin_to_lower_bound = min_energy - lb
		margin_to_upper_bound = ub - max_energy

		return ScheduleEnergyStats(
			is_feasible=is_feasible,
			min_energy=min_energy,
			min_energy_time=min_energy_time,
			max_energy=max_energy,
			max_energy_time=max_energy_time,
			worst_under_amount=worst_under_amount,
			worst_under_time=worst_under_time,
			worst_under_index=worst_under_index,
			worst_over_amount=worst_over_amount,
			worst_over_time=worst_over_time,
			worst_over_index=worst_over_index,
			required_additional_charge=required_additional_charge,
			required_additional_discharge=required_additional_discharge,
			first_violation_time=first_violation_time,
			first_violation_type=first_violation_type,
			first_violation_index=first_violation_index,
			margin_to_lower_bound=margin_to_lower_bound,
			margin_to_upper_bound=margin_to_upper_bound,
			energy_timeseries=energy_timeseries
		)
	
	# ---------------------------------------------------------
	
	def _initialize_charging(self) -> None:
		"""
		Initialize charging processes before the simulation starts by inserting
		(ChA -> ChP) pairs in chronological order.

		Core idea:
		- We construct a conservative charging scaffold without modeling actual trips.
		- Charging is triggered early enough such that, even under worst-case
		continuous driving, the cab would still be able to reach a charger
		without violating the lower SoC bound.

		Key modeling choices:
		- Only charging-related movements are explicitly represented:
		- ChA: routed approach to a charging station (with real time/energy from router)
		- ChP: charging process blocker (fixed duration, conservative)
		- Free time between charging events is not explicitly modeled:
		- No movement entries are created
		- No energy is deducted during that time

		Group staggering:
		- If num_groups > 1, the fleet is split into almost equally sized groups.
		- Only the first blocker of each cab is shifted earlier depending on its group.
		- Later blockers follow the normal logic unchanged.
		- This creates a phase shift between groups without changing the long-run
		blocker spacing.

		Charging process blocker (ChP):
		- Fixed duration based on the slowest charging station (LB -> UB + service times)
		- Stored SoC is topped up to UB only if arrival charge is below UB;
		otherwise the blocker is inserted with zero charge_amount
		"""

		# slowest charging power -> conservative ChP duration
		min_cs = min(self.sim_charging_stations, key=lambda sim_cs: sim_cs.station.max_supply)
		min_charging_power_w = float(min_cs.station.max_supply)
		
		# Only block time to restore a certain amount of energy
		charge_restore_fraction = float(self.parameters["algorithm"].get("charge_restore_fraction", 1.0))
		# clamp it to [0,1] for non-sense values
		charge_restore_fraction = max(0.0, min(1.0, charge_restore_fraction))
		energy_eps_wh = float(self.parameters["algorithm"].get("energy_tolerance_wh", 1e-6))
		
		# number of fleet groups for staggered first blocker placement
		num_groups = int(self.parameters["algorithm"].get("num_groups", 1))
		if num_groups < 1:
			num_groups = 1

		# deterministic fleet order for stable group assignment
		fleet_sorted = sorted(
			self.cab_fleet,
			key=lambda cab: cab.cab.id,
		)

		for cab_idx, cab in enumerate(fleet_sorted):
			# round-robin group assignment
			group_idx = cab_idx % num_groups

			# basic values
			consumption_wh_per_m = float(cab.consumption)
			speed_m_per_s = float(cab.speed)
			min_energy_wh = float(cab.charge_lb)
			max_energy_wh = float(cab.charge_ub)
			schedule_start = cab.start_schedule
			schedule_end = cab.end_schedule

			# symmetric service time (before and after), inside ChP window
			serv_t = int(self.parameters["problem"]["cs_connect_time"])

			# fixed ChP duration: LB->UB at slowest station + 2*service
			#  - nominal: to spread out the processes
			#  - actual:  for blocker size (if decreased by "charge_restore_fraction")
			pure_charge_time_s = ((max_energy_wh - min_energy_wh) / min_charging_power_w) * 3600.0
			full_chp_duration_nominal_s = int(round(pure_charge_time_s + 2 * serv_t))
			full_chp_duration_actual_s = int(round(charge_restore_fraction * pure_charge_time_s + 2 * serv_t))

			# current state (before adding any charging processes)
			curr_time = int(max(self.horizon_start, schedule_start))
			curr_energy_wh = float(cab.charge_init)
			curr_lat = float(cab.current_lat)
			curr_lon = float(cab.current_lon)

			# worst-case driving burn rate used only to decide when charging
			# must be triggered; it is not used for actual energy updates
			burn_rate_wh_per_s = consumption_wh_per_m * speed_m_per_s

			# only the first blocker is phase-shifted by group
			#  - to shorten charging processes if shifted backwards
			is_first_blocker = True

			# add charging processes (blockers) to cab schedule
			while True:
				# 1) compute time for next charging process
				#    (decide station + approach metrics)
				cs_idx = nearest_straight_line(
					(curr_lat, curr_lon),
					[(sim_cs.lat, sim_cs.lon) for sim_cs in self.sim_charging_stations],
				)
				cs = self.sim_charging_stations[cs_idx]

				res = self.router_cab.shortest_path(
					(curr_lat, curr_lon),
					(cs.lat, cs.lon),
				)
				self.runtime_diagnostics.router_calls["cab"] += 1
				self.runtime_diagnostics.router_time_s["cab"] += res["query_time_s"]
				approach_time_s = float(res["time_s"])
				approach_energy_wh = float(res["energy_wh"])

				# 2) feasibility check: must arrive with at least LB
				# If we started the approach right now, would we arrive >= LB energy?
				if curr_energy_wh - approach_energy_wh < min_energy_wh - energy_eps_wh:
					raise ValueError(
						f"Cab {cab.cab.id} infeasible: nearest charger not reachable "
						f"from ({curr_lat},{curr_lon}) with SoC={curr_energy_wh:.2f}Wh "
						f"(need {approach_energy_wh:.2f}Wh for approach, LB={min_energy_wh:.2f}Wh)."
					)

				# latest time we can start the approach, assuming free time may be
				# used for driving; need at least (LB + approach_energy) at approach start
				required_energy_at_approach_start_wh = min_energy_wh + approach_energy_wh
				margin_wh = max(0.0, curr_energy_wh - required_energy_at_approach_start_wh)
				free_drive_time_s = margin_wh / burn_rate_wh_per_s

				# group-based one-time phase shift for the first blocker only
				if is_first_blocker and num_groups > 1:
					phase = group_idx / num_groups
					shift_s = phase * free_drive_time_s
					effective_free_drive_time_s = max(0.0, free_drive_time_s - shift_s)
				else:
					effective_free_drive_time_s = free_drive_time_s

				# approach will be glued to the process: ChA ends at ChP start
				chp_start_f = curr_time + effective_free_drive_time_s + approach_time_s

				# checking the start could be skipped
				chp_start = int(chp_start_f) + (int(chp_start_f) < chp_start_f) # ceil
				
				# time ChA start and end
				cha_end = chp_start
				cha_start = int(round(cha_end - approach_time_s))
				if cha_start < curr_time:
					raise ValueError(
						f"Cab {cab.cab.id} infeasible: not enough time to glue approach to process."
					)

				# 3) create approach entry
				# Move time forward to the start of the approach.
				# No energy is deducted for the preceding free interval,
				# since it is not explicitly modeled.
				curr_time = cha_start

				# Create approach (ChA):
				# Represents the actual routed trip to the charging station.
				# This is where energy is explicitly reduced using router estimates.
				cha_s_charge = float(curr_energy_wh)
				cha_e_charge = float(curr_energy_wh - approach_energy_wh)
				
				if is_first_blocker:
					# this energy level is only used for timing, not actually consumed!
					virtual_arrival_energy_wh = max(
						min_energy_wh, # it was checked above, that this is possible!
						curr_energy_wh
						- burn_rate_wh_per_s * effective_free_drive_time_s
						- approach_energy_wh
					)
					charge_needed_wh = max(0.0, max_energy_wh - virtual_arrival_energy_wh)
					pure_charge_time_s_this = (charge_needed_wh / min_charging_power_w) * 3600.0
					
					# again, we use "nominal" and "actual" here, in case only a fraction of the time should be blocked
					chp_duration_nominal_s_this = int(round(pure_charge_time_s_this + 2 * serv_t))
					chp_duration_actual_s_this = int(round(charge_restore_fraction * pure_charge_time_s_this + 2 * serv_t))
				else:
					chp_duration_nominal_s_this = full_chp_duration_nominal_s
					chp_duration_actual_s_this = full_chp_duration_actual_s
				
				chp_end = chp_start + chp_duration_actual_s_this
				# stop if ChP finishes after operations
				if chp_end > min(self.horizon_end, schedule_end):
					# The next ChP would be outside the horizon; cap the last
					# blocker to the reserve still needed until operations end.
					final_time = int(min(self.horizon_end, schedule_end))
					if (
						cab.schedule
						and cab.schedule[-1].type == CabEntryType.ChP
					):
						last_chp = cab.schedule[-1]
						horizon_burn_rate_wh_per_s = (
							burn_rate_wh_per_s * charge_restore_fraction
						)
						charging_rate_wh_per_s = (
							float(last_chp.charge_station.station.max_supply) / 3600.0
						)
						old_max_charge = float(last_chp.max_charge_amount)
						available_time_s = max(
							0.0,
							float(final_time - last_chp.start_time - last_chp.service_time),
						)
						# Shortest final blocker that still reserves enough
						# energy for worst-case driving from ChP end to horizon.
						horizon_reserve_wh = (
							horizon_burn_rate_wh_per_s
							* available_time_s
							* charging_rate_wh_per_s
						) / (horizon_burn_rate_wh_per_s + charging_rate_wh_per_s)
						new_max_charge = max(
							float(last_chp.charge_amount),
							min(old_max_charge, horizon_reserve_wh),
						)
						if old_max_charge - new_max_charge > energy_eps_wh:
							active_duration_f = new_max_charge / charging_rate_wh_per_s
							new_active_duration_s = int(active_duration_f) + (
								int(active_duration_f) < active_duration_f
							)
							last_chp.max_charge_amount = new_max_charge
							last_chp.end_time = (
								last_chp.start_time
								+ int(last_chp.service_time)
								+ new_active_duration_s
							)
					break
				
				# the ChP entry below starts here
				station_proj = res["proj_dest"]
				cab.schedule.append(
					CabScheduleEntry(
						_type=CabEntryType.ChA,
						_charging_s=cs,
						_s_lat=curr_lat,
						_s_lon=curr_lon,
						_e_lat=station_proj[0],
						_e_lon=station_proj[1],
						_s_time=int(cha_start),
						_e_time=int(cha_end),
						# _serv_t=0,
						_s_charge=cha_s_charge,
						_e_charge=cha_e_charge,
						_distance_m=float(res.get("distance_m", 0.0) or 0.0),
					)
				)

				# update to station arrival (approach end)
				curr_time = int(cha_end)
				curr_lat = float(station_proj[0])
				curr_lon = float(station_proj[1])
				curr_energy_wh = float(cha_e_charge)

				# 4) create charging process blocker (fixed)
				chp_active_duration_s = max(0.0, float(chp_end - chp_start - 2 * serv_t))
				chp_capacity_wh = chp_active_duration_s * float(cs.station.max_supply) / 3600.0
				charged_wh_init = min(max(0.0, max_energy_wh - curr_energy_wh), chp_capacity_wh)
				chp_s_charge = curr_energy_wh
				# the block charges what it can deliver in its active time, up to the UB
				chp_e_charge = curr_energy_wh + charged_wh_init

				chp_entry = CabScheduleEntry(
					_type=CabEntryType.ChP,
					_charging_s=cs,
					_s_lat=curr_lat,
					_s_lon=curr_lon,
					_e_lat=curr_lat,
					_e_lon=curr_lon,
					_s_time=int(chp_start),
					_e_time=int(chp_end),
					_serv_t=2 * serv_t,
					_s_charge=chp_s_charge,
					_e_charge=chp_e_charge,
					_distance_m=0.0,
					_charge_amount=float(charged_wh_init),
					_max_charge_amount=float(chp_capacity_wh),
				)
				cab.schedule.append(chp_entry)

				# first blocker has now been placed; later blockers use normal timing
				is_first_blocker = False

				# 5) update current state and continue
				#    (use nominal "duration" to keep gap length [in case only a fraction was blocked])
				curr_time = int(chp_start + chp_duration_nominal_s_this)
				curr_energy_wh = float(chp_e_charge)
				# curr_lat/lon already at station
			# while end

			# Keep the trailing idle period explicit without adding another fixed
			# blocker. ChA is non-fixed, so later trailing candidate insertions can
			# overwrite this zero-distance horizon anchor.
			final_time = int(min(self.horizon_end, schedule_end))
			if (
				cab.schedule
				and cab.schedule[-1].type == CabEntryType.ChP
				and cab.schedule[-1].end_time < final_time
			):
				last_entry = cab.schedule[-1]
				cab.schedule.append(
					CabScheduleEntry(
						_type=CabEntryType.ChA,
						_charging_s=last_entry.charge_station,
						_s_lat=float(last_entry.end_lat),
						_s_lon=float(last_entry.end_lon),
						_e_lat=float(last_entry.end_lat),
						_e_lon=float(last_entry.end_lon),
						_s_time=final_time,
						_e_time=final_time,
						_s_charge=float(last_entry.end_charge),
						_e_charge=float(last_entry.end_charge),
						_distance_m=0.0,
					)
				)
		# for cab end

		return
	
	# ---------------------------------------------------------

	def _plan_repair_charge_adjustments(self, cab: CabVehicle,
										candidate: NCCandidate,
										violation_type: str
										) -> tuple[dict[int, float], bool]:
		'''
		Plan charge additions or reductions at preceding ChP/PT entries without
		mutating the candidate schedule.

		The boolean says whether the sparse plan fully covers the current repair
		type. Because this planner uses the worst under/over amount and protects
		the affected suffix against the opposite bound, one successful plan is
		expected to make the candidate feasible with the current repair strategy.
		'''
		energy_eps_wh = float(self.parameters["algorithm"]["energy_tolerance_wh"])
		schedule = candidate.new_cab_schedule
		stats = candidate.energy_stats

		if violation_type == "under":
			remaining = float(stats.worst_under_amount)
		elif violation_type == "over":
			remaining = float(stats.worst_over_amount)
		else:
			raise ValueError(f"Unsupported energy repair type: {violation_type}")

		violation_time = stats.first_violation_time
		# Sparse schedule-indexed repair plan: positive values add charge,
		# negative values reduce already planned charge.
		planned_adjustments: dict[int, float] = {}
		suffix_min_energy = float("inf")
		suffix_max_energy = float("-inf")

		# Scan backwards. A charge change at the current entry affects that
		# entry's end charge and all later entries, but not its start charge.
		for search_index in range(len(schedule) - 1, -1, -1):
			entry = schedule[search_index]
			suffix_min_energy = min(suffix_min_energy, float(entry.end_charge))
			suffix_max_energy = max(suffix_max_energy, float(entry.end_charge))
			is_charge_entry = entry.type == CabEntryType.ChP or entry.pro is not None

			# Only charge-capable entries ending before the first violation can
			# still influence that violation.
			if (
				remaining > energy_eps_wh
				and is_charge_entry
				and entry.end_time <= violation_time
			):
				if violation_type == "under":
					upper_bound_room = float(cab.charge_ub) - suffix_max_energy
					used_charge = float(entry.charge_amount)
					max_charge = float(entry.max_charge_amount)
					if max_charge + energy_eps_wh < used_charge:
						raise RuntimeError(
							"max_charge_amount below charge_amount on charge entry during energy repair "
							f"(cab_id={cab.cab.id}, schedule_index={search_index}, "
							f"type={entry.type.name}, max={max_charge}, used={used_charge})"
					)
					available_charge = max(0.0, max_charge - used_charge)
					charge_to_add = min(available_charge, remaining, upper_bound_room)
					if charge_to_add > energy_eps_wh:
						planned_adjustments[search_index] = charge_to_add
						remaining -= charge_to_add
						suffix_min_energy += charge_to_add
						suffix_max_energy += charge_to_add
				else:
					lower_bound_room = suffix_min_energy - float(cab.charge_lb)
					used_charge = float(entry.charge_amount)
					reduction = min(used_charge, remaining, lower_bound_room)
					if reduction > energy_eps_wh:
						planned_adjustments[search_index] = -reduction
						remaining -= reduction
						suffix_min_energy -= reduction
						suffix_max_energy -= reduction

			if remaining <= energy_eps_wh:
				return planned_adjustments, True

			# From the next iteration's perspective, this whole entry lies in
			# the affected suffix, so its start charge must be covered too.
			suffix_min_energy = min(suffix_min_energy, float(entry.start_charge))
			suffix_max_energy = max(suffix_max_energy, float(entry.start_charge))

		return {}, False

	# ---------------------------------------------------------

	def _apply_repair_charge_adjustments(self, cab: CabVehicle,
										 candidate: NCCandidate,
										 planned_adjustments: dict[int, float]
										 ) -> ScheduleEnergyStats:
		'''
		Apply planned charge deltas to the candidate schedule and return fresh
		post-repair energy statistics.
		'''
		schedule = candidate.new_cab_schedule
		stats_start_idx = max(0, int(candidate.cab_insert_position_left) + 1)
		cumulative_delta = 0.0
		energy_samples: list[tuple[int, float, float]] = []

		# Materialize the sparse repair plan in schedule order; each charge
		# adjustment shifts every later charge level by the cumulative delta.
		for entry_index, entry in enumerate(schedule):
			entry.start_charge += cumulative_delta
			entry.end_charge += cumulative_delta

			# Missing index means this entry has no planned charge adjustment.
			entry_delta = planned_adjustments.get(entry_index, 0.0)
			if entry_delta != 0.0:
				entry.charge_amount = float(entry.charge_amount) + entry_delta
				entry.end_charge += entry_delta
				cumulative_delta += entry_delta

			if entry_index >= stats_start_idx:
				energy_samples.append((entry_index, entry.start_time, entry.start_charge))
				energy_samples.append((entry_index, entry.end_time, entry.end_charge))

		return self._energy_stats_from_energy_samples(
			energy_samples,
			cab.charge_lb,
			cab.charge_ub,
		)

	# ---------------------------------------------------------
	
	def _try_energy_repair(self, cab: CabVehicle, candidate: NCCandidate) -> NCCandidate:
		'''
		Attempt to repair detected energy constraint violations by modifying
		preceding charge-capable entries if possible.

		Undercharge handling:
		- Search backwards from the first violation.
		- Add charge at preceding ChP/PT entries with remaining charge room.
		- Continue farther back if nearer entries cannot provide enough charge.

		Overcharge handling:
		- Search backwards from the first violation.
		- Reduce already planned charge at preceding ChP/PT entries.
		- Continue farther back if nearer entries cannot reduce enough.

		The current planner is expected to need only one pass. Additional
		iterations are a future hook for more local/greedy repair strategies.
		'''

		max_repair_iterations = int(
			self.parameters["algorithm"].get("max_energy_repair_iterations", 1)
		)

		for _ in range(max_repair_iterations):
			stats = candidate.energy_stats

			if stats.is_feasible:
				return candidate

			type_first = stats.first_violation_type
			time_first = stats.first_violation_time
			index_first = stats.first_violation_index

			# Debugging guard only: infeasible stats are expected to carry complete
			# first-violation metadata. This can be removed once the repair path is stable.
			if type_first is None or time_first is None or index_first is None:
				raise RuntimeError(
					"Inconsistent energy stats: infeasible candidate has incomplete "
					"first-violation metadata"
				)

			new_schedule = candidate.new_cab_schedule
			# defensive guard against inconsistent candidate metadata
			if index_first < 0 or index_first >= len(new_schedule):
				return candidate

			planned_adjustments, covers_current_violation = self._plan_repair_charge_adjustments(
				cab,
				candidate,
				type_first,
			)
			if not covers_current_violation:
				return candidate

			candidate.energy_stats = self._apply_repair_charge_adjustments(
				cab,
				candidate,
				planned_adjustments,
			)

		return candidate
		
	# ---------------------------------------------------------

	def _update_model(self):
		'''
		Iterate over all cab schedules, remove finished entries (entries with end_time <= current_time),
		and log them in the cab-specific list "past_entries"
		'''
		# mid-entry position: straight-line estimate by default, router-based if enabled
		use_router_position = self.parameters.get("algorithm", {}).get(
			"cab_live_position_router", {}).get("enabled", False)

		for cab_vehicle in self.cab_fleet:
			cab_id = cab_vehicle.cab.id
			
			old_schedule = cab_vehicle.schedule
			cutoff = 0
			while cutoff < len(old_schedule) and old_schedule[cutoff].end_time <= self.current_time:
				cutoff += 1
			finished_entries = old_schedule[:cutoff]
			remaining_entries = old_schedule[cutoff:]

			for entry in finished_entries:
				self._update_customer_sim_state_from_entry(entry, cab_vehicle)
			
			# Append finished entries to cab-specific log
			cab_vehicle.past_entries.extend(finished_entries)
			
			# Determine vehicle's new physical state (charge + position)
			new_charge = cab_vehicle.current_charge
			new_lat = cab_vehicle.current_lat
			new_lon = cab_vehicle.current_lon

			# 1) If there are finished entries, use the last one's end state
			if finished_entries:
				last_finished = max(finished_entries, key=lambda e: e.end_time)
				new_charge = last_finished.end_charge
				new_lat = last_finished.end_lat
				new_lon = last_finished.end_lon
			# 2) If no finished entries but there is a currently active one, interpolate between start and end
			else:
				current_entry = None
				for entry in remaining_entries:
					if entry.start_time <= self.current_time < entry.end_time:
						current_entry = entry
						break

				if current_entry is not None:
					duration = current_entry.end_time - current_entry.start_time
					if duration > 0.0:
						progress = (self.current_time - current_entry.start_time) / duration
						# Normalize progress to [0, 1] just to be safe
						progress = max(0.0, min(1.0, progress))

						if use_router_position:
							router = self.router_pro if current_entry.type == CabEntryType.PT else self.router_cab
							new_lat, new_lon = router.position_at_fraction(
								(current_entry.start_lat, current_entry.start_lon),
								(current_entry.end_lat, current_entry.end_lon),
								progress,
							)
						else:
							# Linear interpolation of position
							new_lat = current_entry.start_lat + progress * (current_entry.end_lat - current_entry.start_lat)
							new_lon = current_entry.start_lon + progress * (current_entry.end_lon - current_entry.start_lon)

						# Linear interpolation of charge
						new_charge = current_entry.start_charge + progress * (current_entry.end_charge - current_entry.start_charge)
					else:
						# Entry of zero duration: same start and end time
						# should not happen
						new_lat = current_entry.end_lat
						new_lon = current_entry.end_lon
						new_charge = current_entry.end_charge
			
			# Apply the state update to the vehicle
			cab_vehicle.update_status(self.current_time, new_charge, new_lat, new_lon)
			
			# Update cab schedule
			cab_vehicle.update_schedule(remaining_entries)
		
		return

	# ---------------------------------------------------------

	def _to_datetime_like_request(self, ts: float, req: Request) -> datetime:
		'''
		Convert unix timestamp to datetime while preserving request timezone semantics.
		'''
		tzinfo = req.register_time.tzinfo if isinstance(req.register_time, datetime) else None
		if tzinfo is not None:
			return datetime.fromtimestamp(ts, tz=tzinfo)
		return datetime.fromtimestamp(ts)

	# ---------------------------------------------------------

	def _extract_request_events_from_entry(self, entry: CabScheduleEntry):
		'''
		Extract actual pickup/dropoff timestamps and coordinates for a
		request-carrying entry.

		These are the event boundaries exposed to FleetPlanning:
		- pickup: when the request-carrying leg starts
		- dropoff: when the request-carrying leg ends

		service_time stays part of the schedule duration, but it is not folded
		into the request timestamps.

		Coordinates returned alongside the timestamps are the entry's own
		(lat, lon), the router's projected point. Returns
		(pu_ts, do_ts, pu_coords, do_coords).
		'''
		if entry.customer is None:
			return None, None, None, None

		if entry.type == CabEntryType.CT:
			return entry.start_time, entry.end_time, (entry.start_lat, entry.start_lon), (entry.end_lat, entry.end_lon)
		if entry.type == CabEntryType.FM:
			return entry.start_time, None, (entry.start_lat, entry.start_lon), None
		if entry.type == CabEntryType.LM:
			return None, entry.end_time, None, (entry.end_lat, entry.end_lon)
		return None, None, None, None

	# ---------------------------------------------------------

	def _update_customer_sim_state_from_entry(self, entry: CabScheduleEntry, cab_vehicle: CabVehicle):
		'''
		Record actual request milestones once a request-carrying entry is finished.
		'''
		if entry.customer is None:
			return

		req_id = int(entry.customer.request.id)
		state = entry.customer.sim_state
		if state.assigned is not True:
			raise RuntimeError(
				"Finished request-carrying entry without prior assigned state "
				f"(req_id={req_id}, cab_id={cab_vehicle.cab.id}, "
				f"entry_type={entry.type.name})"
			)
		if state.assigned_cab_id is None:
			raise RuntimeError(
				"Finished request-carrying entry without assigned cab id "
				f"(req_id={req_id}, cab_id={cab_vehicle.cab.id}, "
				f"entry_type={entry.type.name})"
			)
		if int(state.assigned_cab_id) != int(cab_vehicle.cab.id):
			raise RuntimeError(
				"Finished request-carrying entry on unexpected cab "
				f"(req_id={req_id}, assigned_cab_id={state.assigned_cab_id}, "
				f"actual_cab_id={cab_vehicle.cab.id}, entry_type={entry.type.name})"
			)

		pu_ts, do_ts, pu_coords, do_coords = self._extract_request_events_from_entry(entry)

		if pu_ts is not None and (state.pickup_ts is None or pu_ts < state.pickup_ts):
			state.pickup_ts = pu_ts
			state.pickup_entry_type = entry.type.name
			state.pickup_lat, state.pickup_lon = pu_coords

		if do_ts is not None and (state.dropoff_ts is None or do_ts > state.dropoff_ts):
			state.dropoff_ts = do_ts
			state.dropoff_entry_type = entry.type.name
			state.dropoff_lat, state.dropoff_lon = do_coords

	# ---------------------------------------------------------

	def _playback_results_to_fleetplanning(self, current_solution: FleetAndRequests):
		'''
		Project custom simulation results back to FleetAndRequests KPI fields.
		'''
		state_by_req = {
			int(customer.request.id): customer.sim_state
			for customer in self.demand_scenario
		}
		request_by_id = {
			int(req.id): req
			for req in current_solution.demand_scenario.requests
		}

		# write request KPIs
		for req in current_solution.demand_scenario.requests:
			state = state_by_req.get(int(req.id))
			req.sim_invalid = False
			req.sim_system_reject = False

			if state is None or state.assigned is not True:
				req.sim_custom_reject = True
				req.sim_prop_pu_time = None
				req.sim_prop_do_time = None
				req.sim_pu_time = None
				req.sim_do_time = None
				req.sim_wait_time_prop = None
				req.sim_customer_in_vehicle_wait_time = None
				req.sim_customer_in_vehicle_time = None
				req.sim_customer_direct_time = None
				req.sim_customer_direct_distance = None
				req.sim_customer_distance = None
				req.sim_customer_excess_travel_time = None
				req.vehicle = None
				continue

			if state.pickup_ts is None or state.dropoff_ts is None:
				raise RuntimeError(
					"Assigned request missing finalized pickup/dropoff timestamps during playback "
					f"(req_id={req.id}, cab_id={state.assigned_cab_id}, "
					f"pickup_ts={state.pickup_ts}, dropoff_ts={state.dropoff_ts})"
				)

			req.sim_custom_reject = False
			req.vehicle = int(state.assigned_cab_id) if state.assigned_cab_id is not None else None
			req.sim_pu_time = self._to_datetime_like_request(state.pickup_ts, req)
			req.sim_prop_pu_time = req.sim_pu_time
			req.sim_do_time = self._to_datetime_like_request(state.dropoff_ts, req)
			req.sim_prop_do_time = req.sim_do_time

			if req.tw_type and req.sim_prop_pu_time is not None:
				req.sim_wait_time_prop = req.sim_prop_pu_time - req.tw_lower
			elif (not req.tw_type) and req.sim_prop_do_time is not None:
				req.sim_wait_time_prop = req.sim_prop_do_time - req.tw_lower
			else:
				req.sim_wait_time_prop = timedelta(0)
			req.sim_customer_in_vehicle_wait_time = state.time_customer_in_vehicle_wait_s
			req.sim_customer_in_vehicle_time = None
			req.sim_customer_direct_time = state.time_customer_direct_s
			req.sim_customer_direct_distance = state.distance_customer_direct_m
			req.sim_customer_distance = None
			req.sim_customer_excess_travel_time = state.time_customer_excess_travel_s

		# write fleet KPIs
		for cab_vehicle in self.cab_fleet:
			cab = cab_vehicle.cab

			# Total operation KPIs
			total_distance_m = 0.0
			total_driving_s = 0.0
			total_service_s = 0.0
			total_energy_wh = 0.0
			served_reqs = set()

			# Store non-driving time per entry so overlapping service times are not counted twice.

			# Customer operation
			customer_trip_count = 0

			customer_distance_m = 0.0
			customer_distance_with_pro_m = 0.0
			customer_distance_without_pro_m = 0.0

			customer_driving_time_s = 0.0
			customer_driving_time_with_pro_s = 0.0
			customer_driving_time_without_pro_s = 0.0

			customer_service_time_s = 0.0

			# Empty total operation
			empty_distance_m = 0.0
			empty_time_s = 0.0
			# Empty pickup operation
			empty_pickup_distance_m = 0.0
			empty_pickup_time_s = 0.0
			empty_pickup_service_time_s = 0.0
			empty_pro_reposition_distance_m = 0.0
			empty_pro_reposition_time_s = 0.0
			empty_pro_reposition_service_time_s = 0.0
			empty_pro_reposition_count = 0

			# Charging access to stationary charging
			charging_stationary_access_distance_m = 0.0
			charging_stationary_access_time_s = 0.0
			charging_stationary_time_s = 0.0
			charging_stationary_energy_wh = 0.0
			charging_stationary_event_count = 0

			# Pro charging support
			charging_pro_energy_wh = 0.0
			request_entries_by_id = {}

			for entry in cab_vehicle.past_entries:
				# entry.service_time is already entry-level total non-driving time
				non_driving_s = float(entry.service_time or 0)
				duration_s = float(entry.end_time - entry.start_time)
				driving_s = max(0.0, duration_s - non_driving_s)
				delta_e = float(entry.end_charge) - float(entry.start_charge)

				if entry.type != CabEntryType.ChP:
					total_driving_s += max(0.0, duration_s - non_driving_s)
					dist_m = entry.distance_m
					if dist_m is None:
						raise RuntimeError(
							"Missing distance_m on movement entry during playback "
							f"(cab_id={cab.id}, type={entry.type.name}, "
							f"start={entry.start_time}, end={entry.end_time})"
						)
					total_distance_m += float(dist_m)

				if delta_e < 0:
					total_energy_wh += -delta_e

				total_service_s += max(0.0, non_driving_s)

				# Customer transport
				if entry.type in (CabEntryType.CT, CabEntryType.FM, CabEntryType.LM) or (entry.type == CabEntryType.PT and entry.customer is not None):
					customer_distance_m += dist_m
					customer_driving_time_s += driving_s
					customer_service_time_s += non_driving_s

					if entry.customer is not None:
						req_id = int(entry.customer.request.id)
						request_entries_by_id.setdefault(req_id, []).append(entry)
						if req_id not in served_reqs:
							customer_trip_count += 1
							served_reqs.add(req_id)

					if entry.type == CabEntryType.PT:
						customer_distance_with_pro_m += dist_m
						customer_driving_time_with_pro_s += driving_s
						# Positive delta_e means the Cab's own battery gained charge from the
						# Pro during this PT segment (see the energy-repair pass that raises
						# entry.end_charge for PT entries) - mirrors the ChP branch below.
						if delta_e > 0:
							charging_pro_energy_wh += delta_e
					else:
						customer_distance_without_pro_m += dist_m
						customer_driving_time_without_pro_s += driving_s

				# empty pickup
				elif entry.type == CabEntryType.CA:
					empty_distance_m += dist_m
					empty_time_s += driving_s
					empty_pickup_distance_m += dist_m
					empty_pickup_time_s += driving_s
					empty_pickup_service_time_s += non_driving_s

				# empty repositioning with pro support
				elif entry.type == CabEntryType.PT and entry.customer is None:
					empty_distance_m += dist_m
					empty_time_s += driving_s
					empty_pro_reposition_distance_m += dist_m
					empty_pro_reposition_time_s += driving_s
					empty_pro_reposition_service_time_s += non_driving_s
					empty_pro_reposition_count += 1
					# Same as the customer-transport PT branch above: capture any charge the
					# Cab gained from the Pro during this (customer-less) PT segment too.
					if delta_e > 0:
						charging_pro_energy_wh += delta_e

				# charging access to stationary charging
				elif entry.type == CabEntryType.ChA:
					empty_distance_m += dist_m
					empty_time_s += driving_s
					charging_stationary_access_distance_m += dist_m
					charging_stationary_access_time_s += driving_s

				# ChP is not modeled as non-driving time. Count only ChA service time here;
				# excluding ChP also prevents it from being counted as driving time.
				elif entry.type == CabEntryType.ChP:
					charging_stationary_time_s += non_driving_s
					charging_stationary_event_count += 1

					# Positive delta_e means charging energy gained
					if delta_e > 0:
						charging_stationary_energy_wh += delta_e

				# NOTE: there is no "elif entry.type == CabEntryType.PT" branch here - every PT
				# entry is already caught above, either by the customer-transport branch (PT
				# with a customer) or the empty-repositioning branch (PT without one), both of
				# which now capture charging_pro_energy_wh directly. A branch here would be
				# unreachable dead code (this used to be exactly that, as a bare "pass").

				elif entry.type == CabEntryType.PlA:
					# Pro platoon approach: can optionally be modeled here later.
					pass

			for req_id, entries in request_entries_by_id.items():
				req = request_by_id.get(req_id)
				if req is None:
					continue
				entries = sorted(entries, key=lambda item: (item.start_time, item.end_time))
				in_vehicle_time_s = float(entries[-1].end_time - entries[0].start_time)
				covered_time_s = sum(float(entry.end_time - entry.start_time) for entry in entries)
				in_vehicle_wait_s = max(0.0, in_vehicle_time_s - covered_time_s)
				req.sim_customer_in_vehicle_time = in_vehicle_time_s
				req.sim_customer_in_vehicle_wait_time = in_vehicle_wait_s
				req.sim_customer_distance = sum(float(entry.distance_m) for entry in entries)
				if req.sim_customer_direct_time is not None:
					req.sim_customer_excess_travel_time = (
						in_vehicle_time_s - float(req.sim_customer_direct_time)
					)


			cab.sim_cum_distance = total_distance_m
			cab.sim_cum_driving_time = total_driving_s
			cab.sim_cum_energy_cons = total_energy_wh
			cab.sim_cum_service_time = total_service_s
			cab.sim_num_requests = len(served_reqs)

			# Customer
			cab.sim_customer_distance = customer_distance_m
			cab.sim_customer_distance_with_pro = customer_distance_with_pro_m
			cab.sim_customer_distance_without_pro = customer_distance_without_pro_m
			cab.sim_customer_driving_time = customer_driving_time_s
			cab.sim_customer_driving_time_with_pro = customer_driving_time_with_pro_s
			cab.sim_customer_driving_time_without_pro = customer_driving_time_without_pro_s
			cab.sim_customer_service_time = customer_service_time_s
			cab.sim_customer_trip_count = customer_trip_count

			# Empty totals
			cab.sim_empty_distance = empty_distance_m
			cab.sim_empty_time = empty_time_s

			# Empty pickup
			cab.sim_empty_pickup_distance = empty_pickup_distance_m
			cab.sim_empty_pickup_time = empty_pickup_time_s
			cab.sim_empty_pickup_service_time = empty_pickup_service_time_s

			#empty pro repositioning
			cab.sim_empty_pro_reposition_distance = empty_pro_reposition_distance_m
			cab.sim_empty_pro_reposition_time = empty_pro_reposition_time_s
			cab.sim_empty_pro_reposition_service_time = empty_pro_reposition_service_time_s
			cab.sim_empty_pro_reposition_count = empty_pro_reposition_count

			# Charging access to stationary charging + stationary charging itself
			cab.sim_charging_stationary_access_distance = charging_stationary_access_distance_m
			cab.sim_charging_stationary_access_time = charging_stationary_access_time_s
			cab.sim_charging_stationary_time = charging_stationary_time_s
			cab.sim_charging_stationary_energy = charging_stationary_energy_wh
			cab.sim_charging_stationary_event_count = charging_stationary_event_count

			# Pro charging support KPIs
			# TODO:
			# Add Pro charging support energy accounting
			# during platoon transport if modeled explicitly.
			cab.sim_charging_pro_energy = charging_pro_energy_wh

			# customer consistency check
			customer_split_distance = (cab.sim_customer_distance_with_pro + cab.sim_customer_distance_without_pro)
			if abs(cab.sim_customer_distance - customer_split_distance) > 1e-6:
				print("[custom_sim KPI WARNING] Warning: customer distance mismatch for cab {}: total {} vs split {} + {}".format(
					cab.id,
					cab.sim_customer_distance,
					cab.sim_customer_distance_with_pro,
					cab.sim_customer_distance_without_pro,
				))
			# Driving time consistency
			customer_split_time = (cab.sim_customer_driving_time_with_pro + cab.sim_customer_driving_time_without_pro)
			if abs(cab.sim_customer_driving_time - customer_split_time) > 1e-6:
				print("[custom_sim KPI WARNING] Warning: customer driving time mismatch for cab {}: total {} vs split {} + {}".format(
					cab.id,
					cab.sim_customer_driving_time,
					cab.sim_customer_driving_time_with_pro,
					cab.sim_customer_driving_time_without_pro,
				))
			# Distance decomposition sanity check
			classified_distance = (cab.sim_customer_distance + cab.sim_empty_pickup_distance + cab.sim_empty_pro_reposition_distance + cab.sim_charging_stationary_access_distance)
			if abs(cab.sim_cum_distance - classified_distance) > 1e-6:
				print("[custom_sim KPI WARNING] Warning: customer distance mismatch for cab {}: total {} vs split {}".format(
					cab.id,
					cab.sim_cum_distance,
					classified_distance,
				))
			# Driving decomposition check
			classified_driving_time = (cab.sim_customer_driving_time + cab.sim_empty_pickup_time + cab.sim_empty_pro_reposition_time + cab.sim_charging_stationary_access_time)
			if abs(cab.sim_cum_driving_time - classified_driving_time) > 1e-6:
				print("[custom_sim KPI WARNING] Warning: driving time mismatch for cab {}: total {} vs split {}".format(
					cab.id,
					cab.sim_cum_driving_time,
					classified_driving_time,
				))
			# Empty distance consistency check
			empty_split_distance = (cab.sim_empty_pickup_distance + cab.sim_empty_pro_reposition_distance + cab.sim_charging_stationary_access_distance)
			if abs(cab.sim_empty_distance - empty_split_distance) > 1e-6:
				print("[custom_sim KPI WARNING] Warning: empty distance mismatch for cab {}: total {} vs split {} + {} + {}".format(
					cab.id,
					cab.sim_empty_distance,
					cab.sim_empty_pickup_distance,
					cab.sim_empty_pro_reposition_distance,
					cab.sim_charging_stationary_access_distance,
				))
			# Empty driving time consistency check
			empty_split_time = (cab.sim_empty_pickup_time + cab.sim_empty_pro_reposition_time + cab.sim_charging_stationary_access_time)
			if abs(cab.sim_empty_time - empty_split_time) > 1e-6:
				print("[custom_sim KPI WARNING] Warning: empty driving time mismatch for cab {}: total {} vs split {} + {} + {}".format(
					cab.id,
					cab.sim_empty_time,
					cab.sim_empty_pickup_time,
					cab.sim_empty_pro_reposition_time,
					cab.sim_charging_stationary_access_time,
				))

			"""
			print("------------ DEBUG OUTPUT: Custom-Sim KPIs--------------------------------------")
			print(f"Number of Cabs in Fleet: {len(self.cab_fleet)}")
			print(f"Information for Cab {cab.id}")

			print("REQUESTS")
			print(f"Requests served: {cab.sim_customer_trip_count}")

			print("DISTANCE")
			print(f"Customer total: {cab.sim_customer_distance:.2f} m")
			print(f"Customer with Pro: {cab.sim_customer_distance_with_pro:.2f} m")
			print(f"Customer without Pro: {cab.sim_customer_distance_without_pro:.2f} m")
			print(f"Empty total: {cab.sim_empty_distance:.2f} m")
			print(f"Empty pickup: {cab.sim_empty_pickup_distance:.2f} m")
			print(f"Empty reposition: {cab.sim_empty_pro_reposition_distance:.2f} m")
			print(f"Charging access: {cab.sim_charging_stationary_access_distance:.2f} m")

			print("TIME")
			print(f"Customer total: {cab.sim_customer_driving_time:.2f} s")
			print(f"Customer with Pro: {cab.sim_customer_driving_time_with_pro:.2f} s")
			print(f"Customer without Pro: {cab.sim_customer_driving_time_without_pro:.2f} s")
			print(f"Customer service time: {cab.sim_customer_service_time:.2f} s")
			print(f"Empty total: {cab.sim_empty_time:.2f} s")
			print(f"Empty pickup: {cab.sim_empty_pickup_time:.2f} s")
			print(f"Empty reposition: {cab.sim_empty_pro_reposition_time:.2f} s")
			print(f"Charging access: {cab.sim_charging_stationary_access_time:.2f} s")
			print(f"Charging stationary: {cab.sim_charging_stationary_time:.2f} s")

			print(f"Trips: {cab.sim_customer_trip_count}")
			print(f"Total distance: {cab.sim_cum_distance:.2f} m")
			print(f"Total driving time: {cab.sim_cum_driving_time:.2f} s")
			print(f"Total service time: {cab.sim_cum_service_time:.2f} s")

			print("ENERGY")
			print(f"Charging stationary energy: {cab.sim_charging_stationary_energy:.2f} Wh")
			print(f"Total consumed energy: {cab.sim_cum_energy_cons:.2f} Wh")
			print(f"Charging Pro energy: {cab.sim_charging_pro_energy:.2f} Wh")
			print(f"Charging events stationary: {cab.sim_charging_stationary_event_count}")
			print(f"Empty Pro reposition count: {cab.sim_empty_pro_reposition_count}")
			"""

		# Pro KPIs from Cab usage.
		# Realized Pro service trips actually used by cabs, keyed by the Pro trip
		# departure time. A PT cab entry shares its start_time with the Pro trip it
		# joined (see _find_convoy_insertion_times: start_pt = pro_trip.start_time),
		# and early unchaining only moves the PT end, so this key stays stable and
		# is unique per Pro. The number of keys is the count of convoy trips this
		# Pro performed; the set of cab ids per trip dedups distance/duration to one
		# count per realized trip and yields the chained-cab count. All usage KPIs
		# (cabs used, per-cab trip count, total trips) are derived from this one
		# structure so they share the same trip-based unit.
		pro_used_trip_cabs = {}  # {pro_id: {pt_start_time: set(cab_id)}}

		for cab_vehicle in self.cab_fleet:
			cab = cab_vehicle.cab
			for entry in cab_vehicle.past_entries:
				# Track Pro usage from PT entries
				if entry.type == CabEntryType.PT and entry.pro is not None:
					pro_id = entry.pro.pro.id  # entry.pro is ProVehicle, .pro is Pro

					# Track the distinct cabs per realized Pro service trip
					pro_used_trip_cabs.setdefault(pro_id, {}).setdefault(
						int(entry.start_time), set()
					).add(cab.id)

		# Lookup of canonical Pro service trips by (pro_id, departure time). The core
		# KPIs are taken from the Pro's OWN trip metrics (full trip distance/duration),
		# not from the cab PT segment, which early unchaining shortens per cab.
		pro_trip_by_key = {
			(pro_vehicle.pro.id, int(trip.start_time)): trip
			for pro_vehicle in self.pro_fleet
			for trip in pro_vehicle.schedule
			if trip.type == ProEntryType.ST
		}

		# Write Pro KPIs to Pro objects
		for pro in current_solution.vehicle_fleet.pros:
			if pro.id in pro_used_trip_cabs:
				trips_by_start = pro_used_trip_cabs[pro.id]  # {pt_start_time: set(cab_id)}

				# Per-cab convoy trip count: how many distinct Pro trips each cab
				# joined (a cab counts once per trip, not once per PT segment).
				cab_usage_count = {}
				for cab_ids in trips_by_start.values():
					for cab_id in cab_ids:
						cab_usage_count[cab_id] = cab_usage_count.get(cab_id, 0) + 1

				pro.sim_cabs_used = list(cab_usage_count.keys())
				pro.sim_cab_usage_count = cab_usage_count
				# Number of distinct convoy trips this Pro performed (one Pro
				# departure = one trip, regardless of cab/customer count).
				pro.sim_total_trips_with_pro = len(trips_by_start)

				# Core Pro KPIs: aggregate each realized service trip exactly once.
				cum_distance_m = 0.0
				cum_driving_time_s = 0.0
				cum_service_time_s = 0.0
				cum_chained_cabs = 0
				for pt_start_time, cab_ids in trips_by_start.items():
					cum_chained_cabs += len(cab_ids)
					pro_trip = pro_trip_by_key.get((pro.id, pt_start_time))
					if pro_trip is None:
						# Realized PT with no matching canonical Pro trip should not
						# happen; skip its metrics but keep the chained-cab count.
						print(
							f"[custom_sim KPI WARNING] No Pro trip found for pro {pro.id} "
							f"at departure {pt_start_time}; metrics skipped."
						)
						continue
					cum_distance_m += float(pro_trip.distance_m)
					cum_driving_time_s += float(pro_trip.pt_routed_duration_s or pro_trip.get_duration())
					cum_service_time_s += float(pro_trip.get_duration())

				pro.sim_cum_distance = cum_distance_m
				pro.sim_cum_driving_time = cum_driving_time_s
				pro.sim_cum_service_time = cum_service_time_s
				pro.sim_cum_chained_cabs = cum_chained_cabs

				# Debug output
				print(f"[PRO KPI] Pro {pro.id}:")
				print(f"  - Cabs used: {pro.sim_cabs_used}")
				print(f"  - Cab usage count: {pro.sim_cab_usage_count}")
				print(f"  - Convoy trips with Pro: {pro.sim_total_trips_with_pro}")
				print(f"  - Cumulative distance: {pro.sim_cum_distance:.2f} m")
				print(f"  - Cumulative driving time: {pro.sim_cum_driving_time:.2f} s")
				print(f"  - Cumulative service time: {pro.sim_cum_service_time:.2f} s")
				print(f"  - Cumulative chained cabs: {pro.sim_cum_chained_cabs}")
			else:
				# No usage of this Pro
				pro.sim_cabs_used = []
				pro.sim_cab_usage_count = {}
				pro.sim_total_trips_with_pro = 0
				pro.sim_cum_distance = 0.0
				pro.sim_cum_driving_time = 0.0
				pro.sim_cum_service_time = 0.0
				pro.sim_cum_chained_cabs = 0
				print(f"[PRO KPI] Pro {pro.id}: Not used in this solution")

		return state_by_req



	# ---------------------------------------------------------
	# ---------------------------------------------------------
	# ---------------------------------------------------------



	def optimize(self, _current_solution: FleetAndRequests, _iteration: int=0):
		'''
		Operation planning
		'''
		optimize_start = time.perf_counter()
		
		# initialize request
		self.demand_scenario = [Customer.from_model_fp(req) for req in _current_solution.demand_scenario.requests]
		req_events = [Event(EventType.NC, i, cust.register_time, _req=cust) for i, cust in enumerate(self.demand_scenario)]
		
		# initialize fleet
		if not self.demand_scenario:
			raise ValueError("optimize() needs at least one request in the demand scenario")
		
		self.cab_fleet = [CabVehicle(cab) for cab in _current_solution.vehicle_fleet.cabs]
		# start positions are snapped once, so entries that copy a cab's position record a point on the network
		for cab_vehicle in self.cab_fleet:
			cab_vehicle.current_lat, cab_vehicle.current_lon = self.router_cab.snap(cab_vehicle.current_lat, cab_vehicle.current_lon)[0]
		self._initialize_pro_schedules(_current_solution)
		self._build_pro_search_index()
		
		self.horizon_end = max([cab.end_schedule for cab in self.cab_fleet])
		
		idle_events = [Event(EventType.TI, i+len(req_events), cabv.end_schedule, _cab=cabv.cab) for i, cabv in enumerate(self.cab_fleet)]
		
		# WARNING: Since the Routers use edge snapping, routing still works
		# -> check if the infrastructure is actually in the operational area
		# (requires cab info so it's checked here and not sooner)
		self._check_infrastructure()
		
		# init event queue
		event_queue = EventQueue(req_events,idle_events)
		
		# track past activities
		past_events = []
		
		# get date from cabs' start of operations
		self.horizon_start = min([cab.start_schedule for cab in self.cab_fleet])
		self.current_time = self.horizon_start
		print(
			"\n[CustomSimulation] "
			f"{format_ts_to_hhmm(self.horizon_start)} - {format_ts_to_hhmm(self.horizon_end)}"
		)

		# init charging strategy
		self._initialize_charging()
		
		# reset certain data from previous calls (like number of rejects)
		self._reset_model()
		self.runtime_diagnostics.chp_blocked_initial_s = sum(
			entry.end_time - entry.start_time
			for cab in self.cab_fleet
			for entry in cab.schedule
			if entry.type == CabEntryType.ChP
		)

		# console log headline
		if self.request_log_interval == 1:
			print(
				f"{'time':>6s} "
				f"{'req':>6s} "
				f"{'status':>10s} "
				f"{'mode':>8s} "
				f"{'cab':>6s} "
				f"{'pro':>6s} "
				f"{'early':>6s} "
				f"{'PU':>6s} "
				f"{'DO':>6s} "
				f"{'wait_s':>8s} "
				f"{'reason':>30s}"
			)
		elif self.request_log_interval > 1:
			print(
				f"{'from':>6s} "
				f"{'to':>6s} "
				f"{'req0':>6s} "
				f"{'req1':>6s} "
				f"{'accepted':>8s} "
				f"{'rejected':>8s} "
				f"{'pure':>6s} "
				f"{'convoy':>6s} "
				f"{'early':>6s}"
			)
		
		# MAIN LOOP
		it = 0
		while ( len(event_queue) > 0 ):
			it += 1
			
			curr_event = event_queue.pop()
						
			self.current_time = curr_event.time
			self._update_model()
			if ( curr_event.event_type == EventType.NC ):
				# the request becomes known: snap its pickup and dropoff
				customer = curr_event.req
				(customer.pu_snap_lat, customer.pu_snap_lon), customer.pu_offset_m = self.router_cab.snap(customer.pu_lat, customer.pu_lon)
				(customer.do_snap_lat, customer.do_snap_lon), customer.do_offset_m = self.router_cab.snap(customer.do_lat, customer.do_lon)

				# process request
				self._dispatch(customer)
				
			else:
				pass
			
			past_events.append(curr_event)
		
		if self.request_log_interval > 1:
			self._flush_request_log_summary()
		
		remaining_schedule_counts = {
			int(cab_vehicle.cab.id): len(cab_vehicle.schedule)
			for cab_vehicle in self.cab_fleet
			if len(cab_vehicle.schedule) > 0
		}
		if remaining_schedule_counts:
			raise RuntimeError(
				"Simulation finished with non-empty cab schedules before playback "
				f"(current_time={self.current_time}, remaining={remaining_schedule_counts})"
			)

		state_by_req = self._playback_results_to_fleetplanning(_current_solution)
		experiment_output = self.parameters.get("experiment_output", {})
		custom_sim_output_dir = None
		prefix_convoy_charging_power_w = None
		if isinstance(experiment_output, dict):
			config_folder = str(experiment_output.get("config_folder", "")).strip()
			if config_folder:
				custom_sim_output_dir = self.current_dir / "output" / config_folder
			prefix_convoy_charging_power_w = experiment_output.get("prefix_convoy_charging_power_w")
		output_paths = write_rw_style_iteration_files(
			current_solution=_current_solution,
			operations_area=self.operations_area,
			cab_fleet=self.cab_fleet,
			pro_fleet=self.pro_fleet,
			demand_scenario=self.demand_scenario,
			iteration=_iteration,
			output_dir=custom_sim_output_dir,
			prefix_convoy_charging_power_w=prefix_convoy_charging_power_w,
		)
		past_entries_fleet_path = output_paths["past_entries_fleet"]
		request_results_log_path = output_paths["request_results_log"]
		
		# log vehicle schedules
		with open(past_entries_fleet_path, "w") as file:
			# Cab entry fields (indices):
			#   [0] s_time  [1] e_time  [2] type  [3] s_charge  [4] e_charge
			#   [5] customer_id  [6] tw_type  [7] tw_lower  [8] tw_upper
			#   [9] service_time  [10] charge_amount  [11] charging_power
			#   [12] s_lat  [13] s_lon  [14] e_lat  [15] e_lon
			#   [16] max_charge_amount  [17] linked_pro_id  [18] distance_m
			# Pro entry fields (indices):
			#   [0] s_time  [1] e_time  [2] type
			#   [3] s_lat  [4] s_lon  [5] e_lat  [6] e_lon
			#   [7] distance_m  [8] s_charge  [9] e_charge
			#   [10] [cab_id, ...]
			json.dump(
				{
					"cabs": {
						cab.cab.id: [
							(
								entry.start_time,
								entry.end_time,
								entry.type.name,
								entry.start_charge,
								entry.end_charge,
								entry.customer.request.id if entry.type.name == "CT" else None,
								entry.customer.tw_type if entry.type.name == "CT" else None,
								entry.customer.tw_lower if entry.type.name == "CT" else None,
								entry.customer.tw_upper if entry.type.name == "CT" else None,
								entry.service_time,
								entry.charge_amount,
								entry.charge_station.station.max_supply if entry.charge_station is not None else (
									entry.pro.cab_recharge_power
									if entry.pro is not None and hasattr(entry.pro, "cab_recharge_power")
									else None
								),
								entry.start_lat,
								entry.start_lon,
								entry.end_lat,
								entry.end_lon,
								entry.max_charge_amount,
								entry.pro.pro.id if entry.pro is not None and hasattr(entry.pro, "pro") else None,
								entry.distance_m,
							)
							for entry in cab.past_entries
						]
						for cab in self.cab_fleet
					},
					"pros": {
						pv.pro.id: [
							(
								entry.start_time,
								entry.end_time,
								entry.type.name,
								entry.start_lat,
								entry.start_lon,
								entry.end_lat,
								entry.end_lon,
								entry.distance_m,
								entry.start_charge,
								entry.end_charge,
								[cv.cab.id for cv in entry.cabs],
							)
							for entry in pv.schedule
						]
						for pv in self.pro_fleet
					},
				},
				file,
				indent='\t'
			)
		# log request
		with open(request_results_log_path, "w") as file:
			json.dump(
				{
					"summary": {
						"num_requests": len(self.demand_scenario),
						"num_rejects": int(self.num_rejects),
					},
					"request_fields": [
						"request_id",
						"sim_custom_reject",
						"vehicle",
						"pickup_ts",
						"dropoff_ts",
						# point reached, the router's projection of the requested address
						"pickup_lat", "pickup_lon",
						"dropoff_lat", "dropoff_lon",
						# distance from the requested address to the point above
						"pickup_offset_m",
						"dropoff_offset_m",
					],
					"requests": [
						(
							int(req.id),
							req.sim_custom_reject,
							req.vehicle,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].pickup_ts,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].dropoff_ts,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].pickup_lat,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].pickup_lon,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].dropoff_lat,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].dropoff_lon,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].pickup_offset_m,
							None if state_by_req.get(int(req.id)) is None else state_by_req[int(req.id)].dropoff_offset_m,
						)
						for req in _current_solution.demand_scenario.requests
					],
				},
				file,
				indent='\t'
			)
		
		# sanity checks on the timeline and energy bounds, continuity, and plausibility
		check_timeline_fleet(filename=past_entries_fleet_path)
		check_energy_bounds_fleet(self.cab_fleet[0].charge_lb, self.cab_fleet[0].charge_ub, filename=past_entries_fleet_path)
		check_energy_continuity_fleet(filename=past_entries_fleet_path)
		check_location_continuity_fleet(filename=past_entries_fleet_path)
		check_entry_distance_plausibility_fleet(filename=past_entries_fleet_path)
		# cab_energy_wh_per_m: the router's own per-meter rate for the "cab" profile -
		# the value that actually produced every entry's real energy_wh, distinct from
		# (and not necessarily equal to) any individual Cab's default_energy_consumption_per_m,
		# which is only ever used for planning-time estimates, not materialized entries.
		cab_energy_wh_per_m = self.router_cab.profiles[self.router_cab.profile].get("energy_per_wh_m")
		check_entry_energy_deltas_fleet(filename=past_entries_fleet_path, cab_energy_wh_per_m=cab_energy_wh_per_m)

		check_timewindows_fleet(filename=past_entries_fleet_path)

		# extra output for debugging and analysis
		if self.parameters.get("debug", {}).get("print_runtime_diagnostics", False):
			self.runtime_diagnostics.chp_blocked_final_s = sum(
				entry.end_time - entry.start_time
				for cab in self.cab_fleet
				for entry in cab.past_entries
				if entry.type == CabEntryType.ChP
			)
			self.runtime_diagnostics.print_summary(
				time.perf_counter() - optimize_start
			)
		
		if self.parameters.get("debug", {}).get("show_timeline", False):
			visualize_timeline_plotly_overlay_soc(
				self.cab_fleet[0].start_schedule_dt,
				self.cab_fleet[0].end_schedule_dt,
				filename=past_entries_fleet_path,
			)
		# optional manual debug artifact; keep hardcoded and off by default
		generate_movement_gif = False
		if generate_movement_gif:
			create_fleet_movement_gif(filename=past_entries_fleet_path, router_cab=self.router_cab, router_pro=self.router_pro, step_seconds=60, fps=5, max_frames=400, dpi=80)
		# visualization for debugging purposes
		if False:
			p1 = (51.7144530395, 8.6403403)
			p2 = (51.6780258395, 8.6167707)

			route1 = self.router_cab.shortest_path(p1,p2)
			route2 = self.router_pro.shortest_path(p1,p2)
			print(route1["distance_m"], route2["distance_m"])

			plot_routes_plotly2(self.router_cab, route1, self.router_pro, route2)

			# view graph only
			plot_graph_plotly(self.router_cab,
								bbox_polygon=self.area_polygon)
		return -1
	



# ===============================================================




if __name__ == "__main__":
	# smoke run: builds the objects and the simulation, no optimize call
	print("Smoke run: building one cab, one Pro and an empty demand scenario, then the simulation")
	start = datetime.now()
	end = start + timedelta(hours=1)

	cab = Cab(0, 51.7185, 8.7555, start, end, 8.333333, 0.1, 10000, 10000)
	pro = Pro(0, 51.7185, 8.7555, start, end, 19.444444, 11000, 0.18, 10000, 10000, 3, [0.1])
	vf = VehicleFleet([cab], [pro], None, None)
	far = FleetAndRequests(0, DemandScenario([]), vf)

	prat = ProRoutesAndTrips([], [])
	ov = OperationalVertices([(8.7534, 51.71675), (8.7606, 51.71675), (8.7606, 51.72125), (8.7534, 51.72125)])
	oa = OperationalArea(ov, [], [], [], prat)

	op_obj = CustomSimulation(oa)
	print("done")
