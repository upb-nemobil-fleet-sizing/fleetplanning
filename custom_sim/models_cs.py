#!/usr/bin/python3

from datetime import datetime
from dataclasses import dataclass
from typing import Optional

from enum import Enum

from models import Request, Cab, Pro, ChargingStation, ChainingLocation

# --------------------------------------------------------------------

class Customer:
	'''
	Customer Request class
	'''
	def __init__(self, _req: Request,_register_time: datetime,
				_pu_lat: float, _pu_lon: float, _do_lat: float, _do_lon: float,
				_tw_lower: datetime, _tw_upper: datetime, _tw_type: bool,
				_entry_time: int=60, _num_persons: int=1, _need_ramp: bool=False):
		'''
		Constructor
		'''
		
		self.request 			= _req
		self.register_time_dt	= _register_time
		self.register_time 		= int(_register_time.timestamp())
		self.pu_lat 			= _pu_lat
		self.pu_lon 			= _pu_lon
		self.do_lat 			= _do_lat
		self.do_lon 			= _do_lon
		# the router's points for the pickup and dropoff address and the distance to them,
		# set when the request becomes known
		self.pu_snap_lat 		= None
		self.pu_snap_lon 		= None
		self.pu_offset_m 		= None
		self.do_snap_lat 		= None
		self.do_snap_lon 		= None
		self.do_offset_m 		= None
		self.tw_lower_dt 		= _tw_lower
		self.tw_upper_dt		= _tw_upper
		self.tw_lower 			= int(_tw_lower.timestamp())
		self.tw_upper 			= int(_tw_upper.timestamp())
		self.tw_type 			= _tw_type			# False=Dropoff; True=Pickup
		self.entry_time 		= _entry_time 		# on-/offboarding 
													# (currently not specified anywhere but 60s for RW)
		
		self.num_persons 		= _num_persons
		self.need_ramp 			= _need_ramp
		self.sim_state			= CustomerSimState()
	
	@classmethod
	def from_model_fp(cls, _requ: Request) -> "Customer":
		'''
		Create a Customer object directly from a Request object.
		(decorator needed to use it without an instance of the class)
		'''
		return cls(
			_req=_requ,
			_register_time=_requ.register_time,
			_pu_lat=_requ.pu_lat,
			_pu_lon=_requ.pu_lon,
			_do_lat=_requ.do_lat,
			_do_lon=_requ.do_lon,
			_tw_lower=_requ.tw_lower,
			_tw_upper=_requ.tw_upper,
			_tw_type=_requ.tw_type,
			_entry_time=60,
			_num_persons=_requ.num_persons,
			_need_ramp=_requ.need_ramp
		)
	
	def __str__(self):
		# Generate a summary of the Customer object
		summary = f"Customer("
		summary += f"id: {self.request.id},"
		summary += f" time: {datetime.fromtimestamp(self.register_time).strftime('%H:%M')},"
		summary += f" twlb: {self.tw_lower}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------

@dataclass
class CustomerSimState:
	"""
	Simulation-side runtime state for one customer.
	Kept separate from the base Request model and easy to extend later.
	"""
	assigned: Optional[bool] = None
	assigned_cab_id: Optional[int] = None
	assigned_mode: Optional[str] = None
	reject_reason: Optional[str] = None
	pickup_ts: Optional[int] = None
	dropoff_ts: Optional[int] = None
	pickup_entry_type: Optional[str] = None
	dropoff_entry_type: Optional[str] = None
	# Point reached, the router's projection of the requested address, captured
	# alongside the timestamps above from the same request-carrying entry.
	pickup_lat: Optional[float] = None
	pickup_lon: Optional[float] = None
	dropoff_lat: Optional[float] = None
	dropoff_lon: Optional[float] = None
	time_customer_direct_s: Optional[float] = None
	distance_customer_direct_m: Optional[float] = None
	time_customer_excess_travel_s: Optional[float] = None
	time_customer_in_vehicle_wait_s: Optional[float] = None
	# Copied from the winning candidate's metrics at dispatch time, same as
	# the time_customer_* fields above.
	pickup_offset_m: Optional[float] = None
	dropoff_offset_m: Optional[float] = None

# --------------------------------------------------------------------

class RuntimeDiagnostics:
	'''
	Optional debug counters for one CustomSimulation run.
	'''
	def __init__(self):
		self.reset()

	def reset(self):
		'''
		Start a fresh diagnostic run.
		'''
		self.candidate_generation_s = 0.0
		self.chp_blocked_initial_s = 0
		self.chp_blocked_final_s = 0
		self.router_calls = {
			"cab": 0,
			"pro": 0,
		}
		self.router_time_s = {
			"cab": 0.0,
			"pro": 0.0,
		}
		candidate_types = ("pure", "convoy", "convoy_early_unchaining")
		self.generated_by_type = {candidate_type: 0 for candidate_type in candidate_types}
		self.energy_feasible_by_type = {candidate_type: 0 for candidate_type in candidate_types}
		self.energy_repaired_by_type = {candidate_type: 0 for candidate_type in candidate_types}
		self.admissible_by_type = {candidate_type: 0 for candidate_type in candidate_types}
		self.selected_by_type = {candidate_type: 0 for candidate_type in candidate_types}
		self.infeasible_by_reason = {
			"energy:under": 0,
			"energy:over": 0,
			"energy:unknown": 0,
			"admissibility": 0,
		}
		self.time_infeasible_by_reason = {
			"horizon_too_early": 0,
			"horizon_too_late": 0,
			"trailing_gap_entry_active": 0,
			"pickup_tw_early": 0,
			"dropoff_tw_late": 0,
		}
		self.request_rejections_by_reason = {
			"impossible_outside_horizon": 0,
			"impossible_tw_too_narrow": 0,
			"no_time_feasible_candidate": 0,
			"no_energy_feasible_candidate": 0,
			"no_admissible_candidate": 0,
		}

	def summary(self, total_seconds: float) -> dict:
		'''
		Return a compact debug summary for printing or later logging.
		'''
		return {
			"time_total_s": float(total_seconds),
			"time_candidate_generation_s": float(self.candidate_generation_s),
			"chp_blocked_initial_s": int(self.chp_blocked_initial_s),
			"chp_blocked_final_s": int(self.chp_blocked_final_s),
			"router_calls": dict(self.router_calls),
			"router_time_s": dict(self.router_time_s),
			"candidates": {
				"generated_by_type": dict(self.generated_by_type),
				"energy_feasible_by_type": dict(self.energy_feasible_by_type),
				"energy_repaired_by_type": dict(self.energy_repaired_by_type),
				"admissible_by_type": dict(self.admissible_by_type),
				"selected_by_type": dict(self.selected_by_type),
				"infeasible_by_reason": dict(self.infeasible_by_reason),
				"time_infeasible_by_reason": dict(self.time_infeasible_by_reason),
			},
			"request_rejections_by_reason": dict(self.request_rejections_by_reason),
		}

	def print_summary(self, total_seconds: float):
		'''
		Print routing and candidate diagnostics.
		'''
		summary = self.summary(total_seconds)
		candidates = summary["candidates"]
		router_calls = summary["router_calls"]
		router_time_s = summary["router_time_s"]
		print("\n[CustomSimulation runtime diagnostics]")
		print("time")
		print(f"  {'total':34s}{summary['time_total_s']:10.3f} s")
		print(f"  {'candidate generation':34s}{summary['time_candidate_generation_s']:10.3f} s")
		print(f"  {'router cab':34s}{router_time_s['cab']:10.3f} s")
		print(f"  {'router pro':34s}{router_time_s['pro']:10.3f} s")
		print("ChP blocker time:")
		print(f"  {'initial':34s}{summary['chp_blocked_initial_s'] / 60.0:10.1f} min")
		print(f"  {'final':34s}{summary['chp_blocked_final_s'] / 60.0:10.1f} min")
		saved_percent = 0.0
		if summary["chp_blocked_initial_s"] > 0:
			saved_percent = (
				100.0
				* (summary["chp_blocked_initial_s"] - summary["chp_blocked_final_s"])
				/ summary["chp_blocked_initial_s"]
			)
		print(f"  {'saved':34s}{saved_percent:10.1f} %")
		print("shortest_path calls:")
		print(f"  {'cab':34s}{router_calls['cab']:10d}")
		print(f"  {'pro':34s}{router_calls['pro']:10d}")
		print("candidates:")
		print(f"  {'type':28s}{'time feasible':>14s}{'energy feasible':>17s}{'repaired':>10s}{'admissible':>12s}{'selected':>10s}")
		for candidate_type in self.generated_by_type:
			print(
				f"  {candidate_type:28s}"
				f"{candidates['generated_by_type'][candidate_type]:14d}"
				f"{candidates['energy_feasible_by_type'][candidate_type]:17d}"
				f"{candidates['energy_repaired_by_type'][candidate_type]:10d}"
				f"{candidates['admissible_by_type'][candidate_type]:12d}"
				f"{candidates['selected_by_type'][candidate_type]:10d}"
			)
		print("infeasible reasons:")
		for reason, count in candidates["infeasible_by_reason"].items():
			print(f"  {reason:34s}{count:10d}")
		print("request reject reasons:")
		for reason, count in summary["request_rejections_by_reason"].items():
			print(f"  {reason:34s}{count:10d}")

# --------------------------------------------------------------------

class CabEntryType(Enum):
	CT  = 0	# customer transport (direct, w customer)
	CA  = 1	# customer approach (w customer)
	ChP = 2	# charging process (w charge_station)
	ChA = 3	# charging approach (w charge_station)
	FM  = 4	# first mile (convoy, w customer)
	LM  = 5	# last mile (convoy, w customer)
	PT  = 6	# platoon transport (convoy, w/wo customer)
	PA  = 7	# parking approach (wo customer)
	PlA = 8	# platoon approach (wo customer)

# --------------------------------------------------------------------

class CabScheduleEntry:
	'''
	Cab schedule entry class
	'''
	def __init__ (self, _type: CabEntryType, _cust: Customer=None, _charging_s: "SimChargingStation"=None,
			   _s_lat: float=-1.0, _s_lon: float=-1.0,
			   _e_lat: float=-1.0, _e_lon: float=-1.0,
			   _s_time: int=-1, _e_time: int=-1, _serv_t: int=0,
			   _s_charge: float=0.0, _e_charge: float=0.0,
			   _distance_m: float=0.0, _route: list=[],_charge_amount: float=None,
			   _max_charge_amount: float=None, _pro=None, _early_unchaining: bool=False):
		self.type 			= _type

		self.route 			= _route
		self.customer 		= _cust			# None if no request (type Request otherwise)
		self.charge_station	= _charging_s 			# None if no charging (type SimChargingStation otherwise)
		self.pro 			= _pro					# None if no platoon charging context exists

		self.start_lat		= _s_lat
		self.start_lon 		= _s_lon
		self.end_lat		= _e_lat
		self.end_lon 		= _e_lon
		self.start_time		= _s_time
		self.end_time 		= _e_time
		self.start_charge	= _s_charge
		self.end_charge		= _e_charge
		
		self.service_time   = _serv_t 	# on-/off-boarding, coupling, or whatever
		self.distance_m 	= _distance_m
		
		self.charge_amount 	= _charge_amount 	# used/accepted charge for charging-capable entries
		self.max_charge_amount = _max_charge_amount 	# maximum charge available in this entry
		self.early_unchaining = _early_unchaining
		
	def get_duration(self):
		return self.end_time - self.start_time
	
	def get_energy_delta(self):
		return self.end_charge - self.start_charge

	def clone_for_candidate(self) -> "CabScheduleEntry":
		'''
		Create an independent schedule entry for candidate generation.

		Mutable entry fields (times, charge levels, coordinates, route list) are
		detached, while shared domain objects like customer and charge_station keep
		their original references.
		'''
		cloned_entry = CabScheduleEntry(
			_type=self.type,
			_cust=self.customer,
			_charging_s=self.charge_station,
			_s_lat=self.start_lat,
			_s_lon=self.start_lon,
			_e_lat=self.end_lat,
			_e_lon=self.end_lon,
			_s_time=self.start_time,
			_e_time=self.end_time,
			_serv_t=self.service_time,
			_s_charge=self.start_charge,
			_e_charge=self.end_charge,
			_distance_m=self.distance_m,
			_charge_amount=self.charge_amount,
			_max_charge_amount=self.max_charge_amount,
			_pro=self.pro,
			_early_unchaining=self.early_unchaining,
		)
		cloned_entry.route = list(self.route)
		return cloned_entry
	
	def __str__(self):
		# Generate a summary of the CabScheduleEntry object
		summary = f"CabScheduleEntry("
		summary += f"type: {self.type.name},"
		summary += f" stime: {datetime.fromtimestamp(self.start_time).strftime('%H:%M')},"
		summary += f" ttime: {datetime.fromtimestamp(self.end_time).strftime('%H:%M')},"
		summary += f" senergy: {self.start_charge},"
		summary += f" eenergy: {self.end_charge},"
		if ( self.type.name == "ChP" or self.type.name == "ChA" ):
			summary += f" cs: {self.charge_station.station.id if self.charge_station is not None else None }"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------

class SimChargingStation:
	'''
	Charging station together with its point on the routing network
	'''
	def __init__ (self, _station: ChargingStation, _index: int, _lat: float, _lon: float):
		self.station 	= _station
		self.index 		= _index	# position in CustomSimulation.sim_charging_stations
		self.lat 		= _lat
		self.lon 		= _lon

# --------------------------------------------------------------------

class SimChainingLocation:
	'''
	Chaining location together with its start and end points on the routing network
	'''
	def __init__ (self, _location: ChainingLocation, _index: int,
				  _start_lat: float, _start_lon: float, _end_lat: float, _end_lon: float):
		self.location 	= _location
		self.index 		= _index	# position in CustomSimulation.sim_chaining_locations
		self.start_lat 	= _start_lat
		self.start_lon 	= _start_lon
		self.end_lat 	= _end_lat
		self.end_lon 	= _end_lon

# --------------------------------------------------------------------

class CabVehicle:
	'''
	Cab vehicle class
	'''
	def __init__ (self, _cab: Cab, _schedule: list[CabScheduleEntry] | None = None):
		self.cab 				= _cab
		self.schedule 			= [] if _schedule is None else _schedule
		
		self.start_schedule_dt 	= _cab.schedule_start_time
		self.end_schedule_dt	= _cab.schedule_end_time
		self.start_schedule 	= int(self.start_schedule_dt.timestamp())
		self.end_schedule 		= int(self.end_schedule_dt.timestamp())
		'''
		self.energy_curve
		self.time_curve
		'''
		
		self.current_charge		= self.cab.init_energy_capacity
		self.current_lat		= self.cab.init_location_lat
		self.current_lon		= self.cab.init_location_lon
		self.current_idle		= True

		self.past_entries		= []
		
		# static values for quick access
		self.charge_init 		= self.cab.init_energy_capacity
		self.charge_lb			= 0
		self.charge_ub 			= self.cab.total_energy_capacity
		self.speed				= self.cab.max_speed
		self.consumption		= self.cab.default_energy_consumption_per_m
	
	def update_status(self, _timestamp: float, new_charge: float, new_lat: float, new_lon: float):
		'''
		update vehicle status
		'''
		self.current_charge		= new_charge
		self.current_lat		= new_lat
		self.current_lon		= new_lon
	
	def update_schedule(self, _schedule):
		self.schedule = _schedule

	def __str__(self):
		# Generate a summary of the CabVehicle object
		summary = f"CabVehicle("
		summary += f"id: {self.cab.id},"
		summary += f" l_sched: {len(self.schedule)},"
		summary += f" charge: {self.current_charge}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------

class ProEntryType(Enum):
	ST =  0	# service trip (scheduled convoy service, w/wo )
	DT =  1	# deadhead trip (empty drive)
	RP =  2	# refuel process
	RA =  3	# refuel approach

# --------------------------------------------------------------------

class ProScheduleEntry:
	'''
	Pro schedule entry class
	'''
	def __init__ (self,
				_type: ProEntryType,
				_distance_m: float=0.0,
				_source_trip_id: str | None = None,
				_pt_routed_duration_s: float = 0.0,
				_pt_routed_distance_m: float = 0.0,
				_s_lat: float=-1.0, _s_lon: float=-1.0,
				_e_lat: float=-1.0, _e_lon: float=-1.0,
				_s_time: int=0, _e_time: int=600,
				_s_charge: float=100.0, _e_charge: float=90.0,
				_route: list | None=None,
				_cabs: list | None=None,
				_fuel_station=None):
		self.type = 		_type
		self.route 			= [] if _route is None else _route
		self.cabs 			= [] if _cabs is None else _cabs	# [] if no cabs attached (List of type CabVehicle otherwise)
		self.fuel_station	= _fuel_station 			# None if no charging (type "tbd" otherwise)
		self.distance_m 	= _distance_m
		
		self.start_lat		= _s_lat
		self.start_lon 		= _s_lon
		self.end_lat		= _e_lat
		self.end_lon 		= _e_lon
		self.start_time		= _s_time
		self.end_time 		= _e_time
		self.start_charge	= _s_charge
		self.end_charge		= _e_charge

		# optional provenance + PT metrics
		self.source_trip_id = _source_trip_id
		self.pt_routed_duration_s = _pt_routed_duration_s
		self.pt_routed_distance_m = _pt_routed_distance_m
		
	def get_duration(self):
		return self.end_time - self.start_time
	
	def get_energy_delta(self):
		return self.end_charge - self.start_charge
	
	def __str__(self):
		# Generate a summary of the ProScheduleEntry object
		summary = f"ProScheduleEntry("
		summary += f"type: {self.type.name},"
		summary += f" stime: {self.start_time},"
		summary += f" ttime: {self.end_time}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------

class ProVehicle:
	'''
	Pro vehicle class
	'''
	def __init__ (self, _pro: Pro, _schedule: list[ProScheduleEntry] | None = None):
		self.pro = _pro
		self.schedule = [] if _schedule is None else _schedule
		self.past_entries = []

		self.start_schedule_dt = (
			datetime.fromisoformat(_pro.schedule_start)
			if isinstance(_pro.schedule_start, str) else _pro.schedule_start
		)
		self.end_schedule_dt = (
			datetime.fromisoformat(_pro.schedule_end)
			if isinstance(_pro.schedule_end, str) else _pro.schedule_end
		)
		self.start_schedule = int(self.start_schedule_dt.timestamp())
		self.end_schedule = int(self.end_schedule_dt.timestamp())
		
		self.current_charge		= -1
		self.current_lat		= -1
		self.current_lon		= -1
		self.current_idle		= False
		
		# static values for quick access
		self.charge_init 			= self.pro.init_energy_capacity
		self.charge_lb				= 0
		self.charge_ub 				= self.pro.total_energy_capacity
		self.speed					= self.pro.max_speed
		self.consumption			= self.pro.default_energy_consumption_per_m
		self.comsumption_per_cab 	= self.pro.additional_consumption_per_cab
		self.cab_recharge_power		= float(self.pro.max_power_supply)
		self.max_cabs				= self.pro.max_cabs
	
	def update_status(self, _timestamp: float):
		'''
		update vehicle status
		'''
		self.current_charge		+= 1
		self.current_lat		+= 1
		self.current_lon		+= 1
		self.current_idle		= not self.current_idle

	def update_schedule(self, _schedule):
		self.schedule = _schedule

	def __str__(self):
		# Generate a summary of the ProVehicle object
		summary = f"ProVehicle("
		summary += f"id: {self.pro.id},"
		summary += f" l_sched: {len(self.schedule)},"
		summary += f" charge: {self.current_charge}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------

class EventType(Enum):
	NC =  0	# new customer request
	TI =  1	# vehicle turns idle

# --------------------------------------------------------------------

class Event:
	'''
	Different events
	'''
	def __init__ (self, _etype: EventType, _id: int, _timestamp: float=-1,
					_req: Customer=None, _cab: CabVehicle=None ):
		self.event_type = _etype		# event type
		self.id 		= _id			# event id
		self.time 		= _timestamp	# time of the event occuring
		self.req 		= _req			# points to customer in case of NC event
		self.cab 		= _cab			# points to vehicle in case of TI event
	
	def __str__(self):
		# Generate a summary of the Event object
		summary = f"Event("
		summary += f"id: {self.id},"
		summary += f" time: {datetime.fromtimestamp(self.time).strftime('%H:%M')},"
		summary += f" type: {self.event_type.name}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------

class NCCandidate:
	'''
	Candidates to service new request
	'''
	def __init__ (self, _req: Customer=None, _cab: CabVehicle=None, _pro: ProVehicle=None ):
		self.req 		= _req			# points to customer
		self.cab 		= _cab			# points to cab vehicle (original schedule)
		self.pro 		= _pro			# points to pro vehicle (original schedule)
		
		self.new_cab_schedule = [] # list[CabScheduleEntry] = []
		self.cab_insert_position_left = -1
		self.cab_insert_position_right = -1
		
		self.new_pro_schedule = [] # list[ProScheduleEntry] = []
		self.selected_pro_trip = None
		self.mode = "pure"
		self.early_unchaining = False
		self.metrics = CandidateMetrics()
		self.energy_stats = None
		
		# scores
		self.score = None
		self.score_breakdown = {}
		
	
	def __str__(self):
		# Generate a summary of the Event object
		summary = f"Candidate("
		summary += f"req: {self.req.request.id},"
		summary += f" cabid: {self.cab.cab.id},"
		summary += f" lsched: {len(self.new_cab_schedule)}"
		summary += f")"
		
		return summary


	def clone_for_early_unchaining(self):
		'''
		Create an independent schedule entry for candidate generation.

		Mutable entry fields (times, charge levels, coordinates, route list) are
		detached, while shared domain objects like customer and charge_station keep
		their original references.
		'''
		cloned_entry = NCCandidate(
			_req = self.req, 
			_cab = self.cab, 
			_pro = self.pro
			)

		cloned_entry.cab_insert_position_left = self.cab_insert_position_left
		cloned_entry.cab_insert_position_right = self.cab_insert_position_right
		cloned_entry.mode = self.mode
		cloned_entry.early_unchaining = self.early_unchaining
		cloned_entry.selected_pro_trip = self.selected_pro_trip

		cloned_entry.new_cab_schedule = [entry.clone_for_candidate() for entry in self.new_cab_schedule]
		
		return cloned_entry
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# --------------------------------------------------------------------	

@dataclass
class ScheduleEnergyStats:
	is_feasible: bool

	min_energy: float
	min_energy_time: Optional[float]

	max_energy: float
	max_energy_time: Optional[float]

	worst_under_amount: float
	worst_under_time: Optional[float]
	worst_under_index: Optional[int]

	worst_over_amount: float
	worst_over_time: Optional[float]
	worst_over_index: Optional[int]

	required_additional_charge: float
	required_additional_discharge: float

	first_violation_time: Optional[float]
	first_violation_type: Optional[str]  # "under" / "over" / None
	first_violation_index: Optional[int]

	margin_to_lower_bound: Optional[float]
	margin_to_upper_bound: Optional[float]
	
	energy_timeseries: list[tuple[float, float]]  # (time, charge)

	#gap_list: list[tuple[int,int],int] # ((id_left, id_right), gap_duration)

# --------------------------------------------------------------------

@dataclass
class CandidateMetrics:
	mode: Optional[str] = None

	distance_vehicle_total_m: Optional[float] = None
	distance_vehicle_customer_m: Optional[float] = None
	distance_vehicle_empty_m: Optional[float] = None
	# Router's own real distance (meters) from the customer's literal
	# pickup/dropoff coordinate to the point actually reached for it. Not a
	# ranking objective by default, but available as one via the
	# candidate_selection.scoring.objectives config, same as any other field
	# here.
	distance_customer_pickup_offset_m: Optional[float] = None
	distance_customer_dropoff_offset_m: Optional[float] = None
	# direct point-to-point distance for this customer, same reference ride time_customer_direct_s
	# describes - used for fare purposes so a convoy detour doesn't get charged to the customer.
	distance_customer_direct_m: Optional[float] = None
	distance_vehicle_powered_m: Optional[float] = None
	distance_vehicle_convoy_m: Optional[float] = None
	delta_distance_vehicle_total_m: Optional[float] = None
	delta_distance_vehicle_empty_m: Optional[float] = None
	delta_distance_vehicle_powered_m: Optional[float] = None
	delta_distance_vehicle_convoy_m: Optional[float] = None

	time_vehicle_inserted_span_s: Optional[float] = None
	time_inserted_entries_s: Optional[float] = None
	time_vehicle_wait_s: Optional[float] = None
	delta_time_vehicle_occupied_s: Optional[float] = None
	delta_time_vehicle_active_entries_s: Optional[float] = None
	delta_time_vehicle_wait_s: Optional[float] = None
	time_customer_approach_s: Optional[float] = None
	time_customer_in_vehicle_s: Optional[float] = None
	time_customer_drive_s: Optional[float] = None
	time_customer_in_vehicle_wait_s: Optional[float] = None
	time_customer_wait_s: Optional[float] = None
	time_customer_wait_after_pt_s: Optional[float] = None
	time_customer_direct_s: Optional[float] = None
	time_customer_excess_travel_s: Optional[float] = None
	time_request_completion_s: Optional[float] = None

	energy_inserted_consumption_wh: Optional[float] = None
	delta_energy_consumption_wh: Optional[float] = None
	energy_min_wh: Optional[float] = None
	energy_max_wh: Optional[float] = None
	energy_margin_lower_wh: Optional[float] = None
	energy_margin_upper_wh: Optional[float] = None
	energy_final_wh: Optional[float] = None
	energy_delta_final_wh: Optional[float] = None
	energy_convoy_charge_wh: Optional[float] = None
	time_stationary_charging_equivalent_s: Optional[float] = None

	structure_num_pt: Optional[int] = None
	structure_num_chp: Optional[int] = None

	def value(self, name: str) -> float:
		# Scoring config names a metric field as a string; getattr is the
		# narrow bridge from that config value to this explicit dataclass.
		try:
			value = getattr(self, name)
		except AttributeError as exc:
			raise ValueError(f"Unknown candidate metric: {name}") from exc

		if value is None:
			raise RuntimeError(f"Candidate metric '{name}' has not been computed")
		if not isinstance(value, (int, float)):
			raise TypeError(f"Candidate metric '{name}' is not numeric")
		return float(value)

	def record_inserted_entries(self, service_entries: list[CabScheduleEntry],
								mode: str, customer: Customer,
								old_replacement_entries: Optional[list[CabScheduleEntry]] = None,
								new_replacement_entries: Optional[list[CabScheduleEntry]] = None,
								is_trailing_replacement: bool = False) -> None:
		'''
		Record candidate metrics from two schedule views.

		service_entries are the entries created to serve the current request.
		Vehicle-side metrics use the net difference between the new replacement
		slice and the old schedule slice it overwrites.
		For trailing replacements, the final horizon-glued approach is counted by
		its own duration, without charging the idle gap before it to the request.
		'''
		if mode not in ("pure", "convoy"):
			raise ValueError(f"Unsupported candidate mode for metrics: {mode}")

		if old_replacement_entries is None:
			old_replacement_entries = []
		if new_replacement_entries is None:
			new_replacement_entries = service_entries
		if not new_replacement_entries:
			raise RuntimeError("Cannot derive vehicle metrics without new replacement entries")

		request_entries = [
			entry for entry in service_entries
			if entry.customer is customer
		]
		if not request_entries:
			raise RuntimeError("Cannot derive customer metrics without request entries")

		self.mode = mode
		# Vehicle-side metrics are net local costs relative to the schedule
		# slice that this candidate replaces.
		new_distance_total_m = sum(float(entry.distance_m) for entry in new_replacement_entries)
		old_distance_total_m = sum(float(entry.distance_m) for entry in old_replacement_entries)
		self.distance_vehicle_total_m = new_distance_total_m - old_distance_total_m
		self.delta_distance_vehicle_total_m = self.distance_vehicle_total_m
		self.distance_vehicle_customer_m = sum(
			float(entry.distance_m)
			for entry in request_entries
		)
		self.distance_vehicle_empty_m = self.distance_vehicle_total_m - self.distance_vehicle_customer_m
		self.delta_distance_vehicle_empty_m = self.distance_vehicle_empty_m
		new_convoy_distance_m = sum(
			float(entry.distance_m)
			for entry in new_replacement_entries
			if entry.pro is not None
		)
		old_convoy_distance_m = sum(
			float(entry.distance_m)
			for entry in old_replacement_entries
			if entry.pro is not None
		)
		self.distance_vehicle_convoy_m = new_convoy_distance_m - old_convoy_distance_m
		self.delta_distance_vehicle_convoy_m = self.distance_vehicle_convoy_m
		self.distance_vehicle_powered_m = (
			self.distance_vehicle_total_m - self.distance_vehicle_convoy_m
		)
		self.delta_distance_vehicle_powered_m = self.distance_vehicle_powered_m

		new_entry_duration_s = sum(
			float(entry.end_time - entry.start_time)
			for entry in new_replacement_entries
		)
		old_entry_duration_s = sum(
			float(entry.end_time - entry.start_time)
			for entry in old_replacement_entries
		)
		self.time_inserted_entries_s = new_entry_duration_s - old_entry_duration_s
		if is_trailing_replacement:
			final_approach_entry = new_replacement_entries[-1]
			last_request_index = next(
				(
					i for i in range(len(new_replacement_entries) - 1, -1, -1)
					if new_replacement_entries[i].customer is customer
				),
				None,
			)
			if last_request_index is None:
				raise RuntimeError("Cannot derive trailing span without request entries")
			if final_approach_entry.type != CabEntryType.ChA:
				raise RuntimeError("Trailing replacement must end with ChA")

			new_vehicle_occupied_s = (
				float(new_replacement_entries[last_request_index].end_time)
				- float(new_replacement_entries[0].start_time)
			) + sum(
				float(entry.end_time) - float(entry.start_time)
				for entry in new_replacement_entries[last_request_index + 1:]
			)
			old_vehicle_occupied_s = old_entry_duration_s
		else:
			new_vehicle_occupied_s = (
				float(new_replacement_entries[-1].end_time)
				- float(new_replacement_entries[0].start_time)
			)
			old_vehicle_occupied_s = (
				float(old_replacement_entries[-1].end_time)
				- float(old_replacement_entries[0].start_time)
				if old_replacement_entries else 0.0
			)
		self.time_vehicle_inserted_span_s = (
			new_vehicle_occupied_s - old_vehicle_occupied_s
		)
		self.delta_time_vehicle_occupied_s = self.time_vehicle_inserted_span_s
		self.time_vehicle_wait_s = self.time_vehicle_inserted_span_s - self.time_inserted_entries_s
		self.delta_time_vehicle_active_entries_s = self.time_inserted_entries_s
		self.delta_time_vehicle_wait_s = self.time_vehicle_wait_s

		# Current candidate builders start the inserted block with the cab movement
		# toward pickup; this is the nearest-cab proxy used by scoring.
		pickup_approach_entry = service_entries[0]
		self.time_customer_approach_s = (
			float(pickup_approach_entry.end_time) - float(pickup_approach_entry.start_time)
		)

		new_energy_consumption_wh = sum(
			float(entry.start_charge) - float(entry.end_charge)
			for entry in new_replacement_entries
			if entry.pro is None and entry.type != CabEntryType.ChP
		)
		old_energy_consumption_wh = sum(
			float(entry.start_charge) - float(entry.end_charge)
			for entry in old_replacement_entries
			if entry.pro is None and entry.type != CabEntryType.ChP
		)
		self.energy_inserted_consumption_wh = (
			new_energy_consumption_wh - old_energy_consumption_wh
		)
		self.delta_energy_consumption_wh = self.energy_inserted_consumption_wh

		self.structure_num_pt = sum(1 for entry in service_entries if entry.type == CabEntryType.PT)
		self.structure_num_chp = sum(1 for entry in service_entries if entry.type == CabEntryType.ChP)

		first_request_entry = request_entries[0]
		last_request_entry = request_entries[-1]
		self.time_customer_in_vehicle_s = (
			float(last_request_entry.end_time) - float(first_request_entry.start_time)
		)
		self.time_request_completion_s = float(last_request_entry.end_time)

		self.time_customer_drive_s = sum(
			float(entry.end_time - entry.start_time - entry.service_time)
			for entry in request_entries
		)

		request_duration_sum = sum(
			float(entry.end_time) - float(entry.start_time)
			for entry in request_entries
		)
		self.time_customer_in_vehicle_wait_s = (
			self.time_customer_in_vehicle_s - request_duration_sum
		)
		self.time_customer_wait_s = self.time_customer_in_vehicle_wait_s

		wait_after_pt = 0.0
		for previous_entry, next_entry in zip(request_entries, request_entries[1:]):
			if previous_entry.type == CabEntryType.PT and next_entry.type == CabEntryType.LM:
				wait_after_pt += float(next_entry.start_time) - float(previous_entry.end_time)
		self.time_customer_wait_after_pt_s = wait_after_pt
		if self.time_customer_direct_s is None:
			raise RuntimeError("Cannot derive customer excess travel time without direct customer time")
		self.time_customer_excess_travel_s = (
			self.time_customer_in_vehicle_s - self.time_customer_direct_s
		)

	def record_energy_result(self, cab, schedule: list[CabScheduleEntry],
							 energy_stats, inserted_start_idx: int,
							 inserted_end_idx: int,
							 stationary_charging_power_w: Optional[float] = None) -> None:
		final_energy = float(schedule[-1].end_charge)

		original_schedule = cab.schedule
		if original_schedule:
			original_final_energy = float(original_schedule[-1].end_charge)
		else:
			original_final_energy = float(cab.current_charge)

		self.energy_min_wh = float(energy_stats.min_energy)
		self.energy_max_wh = float(energy_stats.max_energy)
		self.energy_margin_lower_wh = float(energy_stats.margin_to_lower_bound)
		self.energy_margin_upper_wh = float(energy_stats.margin_to_upper_bound)
		self.energy_final_wh = final_energy
		self.energy_delta_final_wh = final_energy - original_final_energy
		self.energy_convoy_charge_wh = sum(
			float(entry.charge_amount)
			for entry in schedule[inserted_start_idx:inserted_end_idx]
			if entry.pro is not None
		)
		self.time_stationary_charging_equivalent_s = 0.0
		if (
			stationary_charging_power_w is not None
			and self.energy_convoy_charge_wh > 0.0
		):
			self.time_stationary_charging_equivalent_s = (
				self.energy_convoy_charge_wh
				* 3600.0
				/ float(stationary_charging_power_w)
			)
