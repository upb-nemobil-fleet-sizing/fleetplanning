#!/usr/bin/python3

import os
import copy
import re
from datetime import datetime, timezone, timedelta
from typing import Tuple
import json



# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------



class Request:
	'''
	Request objects
	'''
	def __init__(self, _id: int,
				_pu_lat: float, _pu_lon: float, _do_lat: float, _do_lon: float,
				_register_time: datetime, _tw_lower: datetime, _tw_upper: datetime,
				_tw_type: bool=True, _tol_lower: float=0.0, _tol_upper: float=0.0,
				_num_persons: int=1, _need_ramp: bool=False, _ride_sharing: bool=False):
		'''
		Constructor
		'''

		# Fixed Parameters (from instance-file?)
		self.id 				= _id					# requestParameter UserGuid? 
		self.pu_lat 			= _pu_lat				# requestParameter CurrentLocation Latitude?
		self.pu_lon 			= _pu_lon				# requestParameter CurrentLocation Longitude?
		self.do_lat 			= _do_lat				# requestParameter TargetLocation Latitude?
		self.do_lon 			= _do_lon				# requestParameter TargetLocation Longitude?
		self.register_time 		= _register_time		# SimulatedTime?
		self.tw_lower 			= _tw_lower				# requestParameter targetTime/ pickupTime StartTime?
		self.tw_upper 			= _tw_upper				# requestParameter targetTime/ pickupTime EndTime?
		self.tw_type 			= _tw_type				# requestParameter targetTime/ pickupTime not null? False=Dropoff; True=Pickup
		self.tol_lower 			= _tol_lower			# requestParameter personalPreferences toleratedDelayBefore? -> ???
		self.tol_upper 			= _tol_upper			# requestParameter personalPreferences toleratedDelayAfter? -> ???
		self.num_persons 		= _num_persons			# requestParameter requestedAdults & requestedChilds?
		self.need_ramp 			= _need_ramp			# requestParameter needRamp?
		self.ride_sharing 		= _ride_sharing			# personalPreferences allowCarpooling?
		
		# Parameters of simulation (from output_sim.json)
		self.sim_invalid		= None					# stepResults invalidRequest?
		self.sim_system_reject	= None					# stepResults tripRequestResponse successful?
		self.sim_custom_reject	= None					# stepResults bookTripResponse successful?
		
		#self.sim_served 		= False					# stepResults trip status?
		self.sim_prop_pu_time 	= None					# stepResults tripRequestResponse proposals pickupTime / stepResults trip estimatedPickupTime
		self.sim_prop_do_time 	= None					# stepResults tripRequestResponse proposals dropoffTime / stepResults trip estimatedDropoffTime
		self.sim_pu_time 		= None					# stepResults trip estimatedPickupTime / stepResults trip customerCheckin
		self.sim_do_time 		= None					# stepResults trip estimatedDropoff / stepResults trip customerCheckout
		self.sim_wait_time_prop	= None					# self.sim_prop_pu_time / self.sim_prop_do_time - self.tw_lower
		self.vehicle			= ""					# stepResults trip vhicleLabel
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"Request("
		summary += f"ID: {self.id}"
		summary += f", PU: ({self.pu_lat:.3f},{self.pu_lon:.3f})"
		summary += f", DO: ({self.do_lat:.3f},{self.do_lon:.3f})"
		summary += f", regTime: {self.register_time}"
		summary += f", puTime: {self.tw_lower}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		summary = f"Request(ID: {self.id})"
		return summary
	
	def get_wait_time(self):
		'''
		Example: Auxiliary function
		'''
		if ( self.served ):
			return self.wait_time
        
# ------------------------------------------------------------------------------

class DemandScenario:
	'''
	Demand scenario class	
	'''
	
	def __init__(self, _requests: list[Request]):
		'''
		Constructor
		'''
		self.requests = _requests
		self.num_requests = len(_requests)
		self.time_start = 0
		self.time_end = 18*60*60 
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"DemandScenario("
		summary += f"num: {self.num_requests}"
		summary += f"; Time Window: {self.time_start} to {self.time_end}"
		
		summary += f"; requests: ("
		# Limit the number of requests shown
		#for i, request in enumerate(self.requests[:3]):  # Show only the first 3 requests
			#formatted_request = {k: (v.strftime('%Y-%m-%d %H:%M') if isinstance(v, datetime) else v)
								#for k, v in vars(request).items()}
			#summary += f"  Request {i+1}: {formatted_request}\n"
		for i, request in enumerate(self.requests[:3]):  # Show only the first 3 requests
			summary += f"{repr(request)}, "
		
		if self.num_requests > 3:
			summary += f"; ... and {self.num_requests - 3} more requests)"
		
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()
	
	def aggregate_total_req_kpis(self):
		num_req = 0
		num_invalid = 0
		num_sys_reject = 0
		num_cus_reject = 0
		tot_wait_time = 0

		for r in self.requests:
			num_req += 1
			if r.sim_invalid:
				num_invalid += 1
			if r.sim_system_reject:
				num_sys_reject += 1
			if r.sim_custom_reject:
				num_cus_reject += 1
			if r.sim_custom_reject==False:
				if isinstance(r.sim_wait_time_prop, datetime) or isinstance(r.sim_wait_time_prop, timedelta):
					tot_wait_time += r.sim_wait_time_prop.total_seconds()
				else:
					tot_wait_time += r.sim_wait_time_prop


		num_served = num_req-num_invalid-num_sys_reject-num_cus_reject
		avg_wait_time = tot_wait_time/num_served if num_served > 0 else 0.0

		kpi_dict = {
			"num_req": num_req,
			"num_invalid" : num_invalid,
			"num_sys_reject": num_sys_reject,
			"num_cus_reject": num_cus_reject,
			"tot_wait_time": tot_wait_time,
			"avg_wait_time": avg_wait_time
		}

		return kpi_dict
			

# ------------------------------------------------------------------------------

class Cab:
	'''
	Cab Vehicle class	
	'''
	
	def __init__(self, _id: int, 
				  _init_location_lat: float, _init_location_lon: float,
				  _schedule_start: datetime, _schedule_end: datetime,
				  _max_speed, _default_energy_consumption_per_m,_energy_capacity: float, _initial_energy_capacity,
				  _num_seats: int=3, _ramp: int=False):
		'''
		Constructor
		'''
		# Fixed Parameters (from base-data?)
		self.id 					= _id					# cabSchedules id 
		self.init_location_lat 		= _init_location_lat	# cabSchedules InitialLocation Latitude
		self.init_location_lon 		= _init_location_lon	# cabSchedules InitialLocation Longitude
		self.schedule_start_time	= _schedule_start		# cabSchedules schedule StartTime
		self.schedule_end_time		= _schedule_end			# cabSchedules schedule EndTime
		# schedule duration
		if isinstance((_schedule_start), str):
			self.schedule_duration		= (datetime.fromisoformat(_schedule_end) - datetime.fromisoformat(_schedule_start)).total_seconds()
		elif isinstance(_schedule_start, datetime):
			self.schedule_duration		= (_schedule_end - _schedule_start).total_seconds()
		else:
			self.schedule_duration		= 0.0
		self.total_energy_capacity 	= _energy_capacity		# cabSchedules TotalEnergyCapacity
		self.init_energy_capacity 	= _initial_energy_capacity		# cabSchedules InitialEnergyCapacity
		self.seats 					= _num_seats			# cabSchedules seats
		self.has_ramp 				= _ramp					# cabSchedules hasRamp
		self.max_speed 				= _max_speed						# cabSchedules maxSpeed

		# Parameters of simulation (from output_cab.json)
		self.sim_cum_distance		= 0						# tripStops distance -> sum
		self.sim_cum_driving_time	= 0						# tripStops drivingTime -> sum
		self.sim_cum_energy_cons	= 0						# tripStops consumedEnergy -> sum
		self.sim_num_requests		= 0						# tripStops entries with stopType==Pickup -> sum
		self.sim_cum_service_time	= 0						# tripStops duration -> sum
		#New parameters for simulation direktly from the choosen backend simulation
		# customer cab trips
		self.sim_customer_distance = 0
		self.sim_customer_distance_with_pro = 0
		self.sim_customer_distance_without_pro = 0
		self.sim_customer_driving_time = 0
		self.sim_customer_driving_time_with_pro = 0
		self.sim_customer_driving_time_without_pro = 0
		self.sim_customer_service_time = 0
		self.sim_customer_trip_count = 0

		# empty cab trips
		self.sim_empty_distance = 0
		self.sim_empty_time = 0
		self.sim_empty_pickup_distance = 0
		self.sim_empty_pickup_time = 0
		self.sim_empty_pro_reposition_distance = 0
		self.sim_empty_pro_reposition_time = 0
		self.sim_empty_pro_reposition_service_time = 0
		self.sim_empty_pro_reposition_count = 0

		# charging cab at charging station
		self.sim_charging_stationary_access_distance = 0
		self.sim_charging_stationary_access_time = 0
		self.sim_charging_stationary_time = 0
		self.sim_charging_stationary_energy = 0
		self.sim_charging_stationary_event_count = 0

		# charging cab in convoi via pro
		self.sim_charging_pro_energy = 0
		
		# more stuff for BaseData output
		self.label = f"Cab{_id}"
		self.licensePlate = f"PB-NE-{_id}",
		self.pc_speed = 0
		self.pc_wind = 0
		self.pc_load_capacity = 0
		self.pc_tire_traction = 0
		self.pc_weather_condition = 0
		self.pc_climatronic = 0
		self.pc_regenerative_breaking = 0
		self.flatrate_power_consumtion = 0.01
		self.risc = 0.0
		self.regenerative_power = 0
		self.operating_company = "74d2b5aa-2d62-4c06-91ae-c8ed9f4797ed"
		self.maximum_regenerative_power = 0
		self.default_energy_consumption_per_m = _default_energy_consumption_per_m
		self.vehicle_type = "cab"
		self.state_of_schedule = "Planned"
		self.child_seats = 0
		self.luggage = 0
		self.charging_curve = [0]
		self.max_speed_autonomous = 0

	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"Cab("
		summary += f"id: {self.id}"
		summary += f"; battery: {self.total_energy_capacity}"
		summary += f"; seats: {self.seats}"
		summary += f"; ramp: {self.has_ramp}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()
	
	# change id
	def set_id(self, _id):
		self.id = _id
		self.label = f"Cab{_id}"
		self.licensePlate = f"PB-NE-{_id}"
		return

# ------------------------------------------------------------------------------

class Pro:
	'''
	Pro Vehicle class	
	'''
	
	def __init__(self, _id: int, 
				  _init_location_lat: float, _init_location_lon: float,
				  _schedule_start: datetime, _schedule_end: datetime,
				  _max_speed, _max_power_supply,
				  _default_energy_consumption_per_m, _energy_capacity: float, _initial_energy_capacity,
				  _max_cabs: int=3, _additional_energy: list | None=None):
		'''
		Constructor
		'''
		self.id 					= _id
		self.init_location_lat 		= _init_location_lat
		self.init_location_lon 		= _init_location_lon
		self.schedule_start 		= _schedule_start
		self.schedule_end 			= _schedule_end
		self.total_energy_capacity	= _energy_capacity
		self.init_energy_capacity 	= _energy_capacity
		self.max_cabs 				= _max_cabs
		
	# Parameters of simulation (from output_pro.json)
		self.sim_cum_distance		= 0						# chainingStops distance -> sum
		self.sim_cum_driving_time	= 0						# chainingStops drivingTime -> sum
		self.sim_cum_chained_cabs	= 0						# chainingStops chainedCabs -> sum
		self.sim_cum_service_time	= 0						# chainingStops duration -> sum

		# Pro usage tracking
		self.sim_cabs_used = []  # List of unique Cab IDs that used this Pro
		self.sim_cab_usage_count = {}  # Dict with {cab_id: count}
		self.sim_total_trips_with_pro = 0  # Number of unique trips/requests served with this Pro

		# more stuff for BaseData output
		self.additional_consumption_per_cab = _additional_energy if _additional_energy is not None else [0.1]
		self.label = f"Pro{_id}"
		self.licensePlate = f"PB-PR-{_id}",
		self.max_speed = _max_speed
		self.max_power_supply = _max_power_supply
		self.pc_speed = 0
		self.pc_wind = 0
		self.pc_load_capacity = 0
		self.pc_tire_traction = 0
		self.pc_weather_condition = 0
		self.pc_climatronic = 0
		self.pc_regenerative_breaking = 0
		self.flatrate_power_consumtion = 0.01
		self.risc = 0.0
		self.regenerative_power = 0
		self.operating_company = "74d2b5aa-2d62-4c06-91ae-c8ed9f4797ed"
		self.maximum_regenerative_power = 0
		self.default_energy_consumption_per_m = _default_energy_consumption_per_m
		self.vehicle_type = "pro"
		self.state_of_schedule = "Planned"
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"Pro("
		summary += f"id: {self.id}"
		summary += f"; cabs: {self.max_cabs}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()
	
	# change id
	def set_id(self, _id):
		self.id = _id
		self.label = f"Pro{_id}"
		self.licensePlate = f"PB-PR-{_id}"
		return

# ------------------------------------------------------------------------------

class VehicleFleet:
	'''
	Vehicle Fleet class	
	'''
	
	def __init__(self, _cabs: list[Cab], _pros: list[Pro],
				 _cab_depot: tuple[float, float], _pro_depot: tuple[float, float]=None,
				 _use_depots: bool=False):
		'''
		Constructor
		'''
		self.cabs = _cabs
		self.pros = _pros
		
		# more stuff
		self.types = [None] * len(self.cabs)
		self.cab_depot = _cab_depot
		self.pro_depot = _pro_depot
		self.use_depots = _use_depots
		if self.use_depots and self.cab_depot is not None:
			for cab in self.cabs:
				cab.init_location_lat, cab.init_location_lon = self.cab_depot
		if self.use_depots and self.pro_depot is not None:
			for pro in self.pros:
				pro.init_location_lat, pro.init_location_lon = self.pro_depot
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"VehicleFleet("
		summary += f"num_cabs: {len(self.cabs)}"
		summary += f"; num_pros: {len(self.pros)}"
		#summary += f"; battery: {self.energy_capacity}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()
	
	def add_cab(self, new_cab: Cab):
		if self.use_depots and self.cab_depot is not None:
			new_cab.init_location_lat, new_cab.init_location_lon = self.cab_depot
		self.cabs.append(new_cab)
		self.types.append(None)
		
	def remove_cab(self, cab_id: int):
		rem_index = -1
		for i,cab in enumerate(self.cabs):
			if ( cab.id == cab_id ):
				rem_index = i
				break
		if ( rem_index > -1 ):
			self.cabs.pop(rem_index)
			self.types.pop(rem_index)

	# Pro fleets are rebuilt from line activation state by ProFleetPlan,
	# so VehicleFleet intentionally has no add_pro/remove_pro helpers.
	def set_pros(self, new_pros: list):
		'''
		Replace the active Pro list wholesale.

		Pro fleets are rebuilt from line activation state (see ProFleetPlan),
		not patched incrementally, so this takes the full rebuilt list. The
		depot start location is re-applied here because rebuilt Pros otherwise
		start at their chaining location, not at the configured Pro depot.
		'''
		self.pros = new_pros
		if self.use_depots and self.pro_depot is not None:
			for pro in self.pros:
				pro.init_location_lat, pro.init_location_lon = self.pro_depot
	
	def aggregate_total_cab_kpis(self):
		num_cabs = 0
		tot_driving_dist = 0
		tot_driving_time = 0
		tot_service_time = 0
		tot_schedule_time = 0
		tot_energy_cons = 0
		
		for c in self.cabs:
			num_cabs += 1
			tot_driving_dist += c.sim_cum_distance
			tot_driving_time += c.sim_cum_driving_time
			tot_service_time += c.sim_cum_service_time
			tot_schedule_time += c.schedule_duration
			tot_energy_cons += c.sim_cum_energy_cons
		
		avg_driving_dist = tot_driving_dist/num_cabs
		avg_driving_time = tot_driving_time/num_cabs
		avg_energy_cons = tot_energy_cons/num_cabs
		if tot_schedule_time!=0:
			avg_utilization = (tot_driving_time+tot_service_time)/tot_schedule_time
		else:
			avg_utilization=0
		
		kpi_dict = {
			"num_cabs": num_cabs,
			"tot_driving_dist" : tot_driving_dist,
			"avg_driving_dist": avg_driving_dist,
			"tot_driving_time": tot_driving_time,
			"avg_driving_time": avg_driving_time,
			"tot_service_time": tot_service_time,
			"tot_schedule_time": tot_schedule_time,
			"tot_energy_cons": tot_energy_cons,
			"avg_energy_cons": avg_energy_cons,
			"avg_utilization": avg_utilization
		}

		return kpi_dict
	
	def aggregate_total_pro_kpis(self):
		num_pros = 0
		tot_driving_dist = 0
		tot_driving_time = 0
		tot_service_time = 0
		tot_chained_cabs = 0
		avg_driving_dist = 0
		avg_driving_time = 0
		

		for p in self.pros:
			num_pros += 1
			tot_driving_dist += p.sim_cum_distance
			tot_driving_time += p.sim_cum_driving_time
			tot_service_time += p.sim_cum_service_time
			tot_chained_cabs += p.sim_cum_chained_cabs
		
		if num_pros > 0:
			avg_driving_dist = tot_driving_dist/num_pros
			avg_driving_time = tot_driving_time/num_pros
		else:
			avg_driving_dist = 0
			avg_driving_time = 0

		kpi_dict = {
			"num_pros": num_pros,
			"tot_driving_dist" : tot_driving_dist,
			"avg_driving_dist": avg_driving_dist,
			"tot_driving_time": tot_driving_time,
			"avg_driving_time": avg_driving_time,
			"tot_service_time": tot_service_time,
			"tot_chained_cabs": tot_chained_cabs
		}
		return kpi_dict

# ------------------------------------------------------------------------------

class ChargingStation:
	'''
	Charging Point class	
	'''
	
	def __init__(self, _id: int, 
				  _location_lat: float, _location_lon: float,
				  _max_supply: int = 11000,
				  _schedule_start: str="05:00:00", _schedule_end: str="23:00:00"):
		'''
		Constructor
		'''
		self.id 				= _id
		self.location_lat 		= _location_lat
		self.location_lon 		= _location_lon
		
		# stuff
		self.max_supply 		= _max_supply
		self.schedule_start 	= _schedule_start
		self.schedule_end 		= _schedule_end
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"CharginStation("
		summary += f"id: {self.id}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ChainingLocation:
	'''
	Chaining Location class	
	'''
	
	def __init__(self, _id: int, 
				  _start_location_lat: float, _start_location_lon: float, 
				  _end_location_lat: float, _end_location_lon: float, 
				  _type: str, _additional_time: int 
				  ):
		'''
		Constructor
		'''
		self.id 				= _id
		self.start_location_lat = _start_location_lat
		self.start_location_lon = _start_location_lon
		self.end_location_lat   = _end_location_lat
		self.end_location_lon   = _end_location_lon
		self.type 				= _type 
		self.additional_time    = _additional_time
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"ChainingLocation("
		summary += f"id: {self.id}"
		summary += f"type: {self.type}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ParkingLocation:
	'''
	Chaiuing Location class	
	'''
	
	def __init__(self, _id: int, 
				  _location_lat: float, _location_lon: float):
		'''
		Constructor
		'''
		self.id 				= _id
		self.location_lat 		= _location_lat
		self.location_lon 		= _location_lon
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"ParkingLocation("
		summary += f"id: {self.id}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ChainRoute:
	'''
	Pro Route class	
	'''
	
	def __init__(self, _id: int, 
				  _start_location: str, _end_location: str, 
				  _intermediate_locations: list[str], 
				  _duration: int=0, _distance: int=0):
		'''
		Constructor
		'''
		self.id 					 = _id
		self.start_location 		 = _start_location
		self.end_location			 = _end_location
		self.intermediate_locations  = _intermediate_locations
		self.duration 				 = _duration 
		self.distance 				 = _distance
		
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"ChainRoute("
		summary += f"id: {self.id}"
		summary += f"StartLocation: {self.start_location.id}"
		summary += f"EndLocation: {self.end_location.id}"
		summary += f"intermediate_locations: {[chain.id for chain in self.intermediate_locations]}"
		summary += f"duration: {self.duration}"
		summary += f"distance: {self.distance}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ChainRouteTrip:
	'''
	Pro Trip class	
	'''
	
	def __init__(self, _id: int, 
				  _chain_route: str, _pro_schedule: str, 
				  _departure: str, _arrival: str):
		'''
		Constructor
		'''
		self.id 					 = _id
		self.chain_route 			 = _chain_route 
		self.pro_schedule 			 = _pro_schedule
		self.departure 				 = _departure
		self.arrival 				 = _arrival

	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"ChainRouteTrip("
		summary += f"id: {self.id}"
		summary += f"ChainRoute: {self.chain_route.id}"
		summary += f"Pro: {self.pro_schedule.id}"
		summary += f"Departure: {self.departure}"
		summary += f"Arrival: {self.arrival}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ProRoutesAndTrips:
	'''
	Container class	for Pro Routes and Pro Schedules
	'''
	
	def __init__(self,_chain_routes: list[ChainRoute], _chain_route_trips: list[ChainRouteTrip]):
		'''
		Constructor
		'''
		self.chain_routes 		= _chain_routes
		self.chain_route_trips 	= _chain_route_trips
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"ProRoutesAndTrips("
		summary += f"num_trips: {len(self.chain_route_trips)}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

class ProScheduleSlot:
	'''
	One concrete Pro vehicle schedule that can be activated on a line.
	'''

	def __init__(self, _pro_id: int, _line_id: str,
				  _init_location_lat: float, _init_location_lon: float,
				  _activation_order: int=0):
		self.pro_id = _pro_id
		self.pro_key = f"Pro{_pro_id}"
		self.line_id = _line_id
		self.init_location_lat = _init_location_lat
		self.init_location_lon = _init_location_lon
		self.activation_order = _activation_order

	def build_pro(self, _base_pro: Pro) -> Pro:
		'''
		Create the concrete Pro vehicle that matches this timetable slot.
		'''
		new_pro = copy.deepcopy(_base_pro)
		new_pro.set_id(self.pro_id)
		new_pro.init_location_lat = self.init_location_lat
		new_pro.init_location_lon = self.init_location_lon
		return new_pro

	def __str__(self):
		return f"ProScheduleSlot({self.pro_key}, line={self.line_id})"

	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ProLine:
	'''
	Bidirectional Pro line with its activatable vehicle schedules.
	'''

	def __init__(self, _id: str, _route_ids: tuple[str, str], _slots: list[ProScheduleSlot]):
		self.id = _id
		self.route_ids = _route_ids
		self.slots = _slots

	def ordered_slots(self) -> list[ProScheduleSlot]:
		'''
		Return slots in activation order, independent of their Pro ids.
		'''
		return sorted(self.slots, key=lambda slot: slot.activation_order)

	def max_count(self) -> int:
		'''
		Return how many Pro vehicles can be activated on this line.
		'''
		return len(self.slots)

	def slots_for_count(self, count: int) -> list[ProScheduleSlot]:
		'''
		Choose the concrete slots for a requested number of active Pros.
		'''
		if count < 0 or count > self.max_count():
			raise ValueError(f"Invalid Pro count {count} for line {self.id}")

		if count == 0:
			return []

		ordered = self.ordered_slots()
		num_slots = len(ordered)
		# Spread the selected slots over the generated timetable instead of
		# taking the first N. This is a simple spacing rule, not an optimizer.
		indices = [int(i * num_slots / count) for i in range(count)]
		return [ordered[index] for index in indices]

	def __str__(self):
		return f"ProLine(id={self.id}, slots={len(self.slots)})"

	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class ProFleetPlan:
	'''
	Catalog that maps line-level Pro counts to concrete Pro objects.
	'''

	def __init__(self, _lines: list[ProLine]):
		self.lines = {line.id: line for line in _lines}
		self.slots_by_pro_id = {}
		for line in _lines:
			for slot in line.slots:
				if slot.pro_id in self.slots_by_pro_id:
					raise ValueError(f"Duplicate Pro schedule slot id: {slot.pro_id}")
				self.slots_by_pro_id[slot.pro_id] = slot

	@classmethod
	def from_pro_routes_and_trips(cls, _pro_routes_and_trips: ProRoutesAndTrips,
								  _chaining_locations: list[ChainingLocation]):
		'''
		Build the line/slot catalog from the Pro routes and timetable trips.

		The backend timetable is keyed by Pro schedule, but FleetPlanning changes
		Pro counts per line. This method:
		- pair directed routes into bidirectional lines
		- group trips by Pro schedule
		- turn each Pro schedule into one activatable slot
		- order slots by first direction/departure, not by numeric Pro id
		'''
		if _pro_routes_and_trips is None:
			return cls([])

		routes = _pro_routes_and_trips.chain_routes
		trips = _pro_routes_and_trips.chain_route_trips
		if len(routes) == 0 or len(trips) == 0:
			return cls([])

		route_by_id = {str(route.id): route for route in routes}
		chain_loc_by_id = {str(loc.id): loc for loc in _chaining_locations}

		# First identify every directed route by its normalized endpoints.
		# The current Pro model only supports complete bidirectional lines.
		directed_routes = {}
		for route in routes:
			start_id = str(getattr(route.start_location, "id", route.start_location))
			end_id = str(getattr(route.end_location, "id", route.end_location))
			start_base = cls._normalize_endpoint_id(start_id)
			end_base = cls._normalize_endpoint_id(end_id)
			if start_base == end_base:
				raise ValueError(f"Unsupported Pro route with identical endpoints: {route.id}")
			directed_key = (start_base, end_base)
			if directed_key in directed_routes:
				raise ValueError(f"Unsupported duplicate Pro route direction: {start_base}->{end_base}")
			directed_routes[directed_key] = route

		# Pair each route with its reverse and assign one stable line id to both.
		line_data_by_endpoint = {}
		line_id_by_route_id = {}
		for (start_base, end_base), route in directed_routes.items():
			reverse_route = directed_routes.get((end_base, start_base))
			if reverse_route is None:
				raise ValueError(f"Unsupported Pro timetable input: route {route.id} has no reverse route")

			endpoints = tuple(sorted([start_base, end_base], key=cls._natural_sort_key))
			line_id = f"{endpoints[0]}_{endpoints[1]}"
			line_data = line_data_by_endpoint.setdefault(
				endpoints,
				{
					"id": line_id,
					"route_ids": set(),
				},
			)
			line_data["route_ids"].add(str(route.id))
			line_data["route_ids"].add(str(reverse_route.id))
			line_id_by_route_id[str(route.id)] = line_id
			line_id_by_route_id[str(reverse_route.id)] = line_id

		# Group timetable trips by ProSchedule. One ProSchedule may only serve
		# one bidirectional line; otherwise line-level activation is ambiguous.
		trips_by_pro_key = {}
		line_id_by_pro_key = {}
		for trip in trips:
			if trip.pro_schedule is None:
				continue

			route_id = str(trip.chain_route)
			if route_id not in line_id_by_route_id:
				raise ValueError(f"Unsupported Pro timetable input: trip {trip.id} references unknown route {route_id}")

			pro_key = cls._normalize_pro_key(trip.pro_schedule)
			line_id = line_id_by_route_id[route_id]
			previous_line_id = line_id_by_pro_key.get(pro_key)
			if previous_line_id is not None and previous_line_id != line_id:
				raise ValueError(f"Unsupported Pro timetable input: {pro_key} appears on multiple lines")

			line_id_by_pro_key[pro_key] = line_id
			trips_by_pro_key.setdefault(pro_key, []).append(trip)

		# One Pro schedule is one vehicle timetable.
		# Store route/departure temporarily so slots can be ordered per line.
		slot_rows_by_line_id = {}
		for pro_key, pro_trips in trips_by_pro_key.items():
			first_trip = min(pro_trips, key=cls._trip_sort_key)
			route = route_by_id.get(str(first_trip.chain_route))
			if route is None:
				raise ValueError(f"Unsupported Pro timetable input: trip {first_trip.id} references unknown route")

			chain_loc_id = str(getattr(route.start_location, "id", route.start_location))
			chain_loc = chain_loc_by_id.get(chain_loc_id)
			if chain_loc is None:
				raise ValueError(f"Unsupported Pro timetable input: route {route.id} references unknown chaining location")

			line_id = line_id_by_pro_key[pro_key]
			slot = ProScheduleSlot(
				cls._pro_id_from_key(pro_key),
				line_id,
				float(chain_loc.end_location_lat),
				float(chain_loc.end_location_lon),
			)
			slot_rows_by_line_id.setdefault(line_id, []).append((
				str(route.id),
				cls._trip_departure_sort_value(first_trip.departure),
				slot.pro_id,
				slot,
			))

		# Activation order per line:
		# - earliest slot defines the first direction
		# - first-direction slots come before reverse-direction slots
		# - slots within one direction are ordered by departure
		lines = []
		for line_data in line_data_by_endpoint.values():
			slot_rows = slot_rows_by_line_id.get(line_data["id"], [])
			if not slot_rows:
				continue
			route_ids = tuple(sorted(line_data["route_ids"], key=cls._natural_sort_key))
			first_slot = min(slot_rows, key=lambda row: (row[1], row[2]))
			route_order = (
				first_slot[0],
				next(route_id for route_id in route_ids if route_id != first_slot[0]),
			)
			# Store the order once, so later selection does not depend on Pro ids.
			slots = []
			for activation_order, slot_row in enumerate(sorted(
				slot_rows,
				key=lambda row: (route_order.index(row[0]), row[1], row[2]),
			)):
				slot_row[3].activation_order = activation_order
				slots.append(slot_row[3])
			lines.append(ProLine(line_data["id"], route_ids, slots))

		return cls(sorted(lines, key=lambda line: cls._natural_sort_key(line.id)))

	def count_on_line(self, pros: list[Pro], line_id: str) -> int:
		'''
		Count currently active Pro vehicles that belong to one line.
		'''
		if line_id not in self.lines:
			raise ValueError(f"Unknown Pro line: {line_id}")
		active_ids = {int(pro.id) for pro in pros}
		unknown_ids = active_ids - set(self.slots_by_pro_id.keys())
		if unknown_ids:
			raise ValueError(f"Active Pro ids are not part of the Pro fleet plan: {sorted(unknown_ids)}")
		return sum(1 for pro_id in active_ids if self.slots_by_pro_id[pro_id].line_id == line_id)

	def max_count(self, line_id: str) -> int:
		'''
		Return the maximum number of Pro vehicles available for one line.
		'''
		if line_id not in self.lines:
			raise ValueError(f"Unknown Pro line: {line_id}")
		return self.lines[line_id].max_count()

	def set_line_count(self, pros: list[Pro], line_id: str, count: int, base_pro: Pro) -> list[Pro]:
		'''
		Rebuild the full active Pro list after changing one line's count.

		The simulation receives one full VehicleFleet, not a patch. Therefore a
		local line change still returns all active Pros: the requested line is
		reselected from its count, while every other line keeps the slots already
		active in the input list.
		'''
		if line_id not in self.lines:
			raise ValueError(f"Unknown Pro line: {line_id}")
		target_line = self.lines[line_id]
		target_line.slots_for_count(count)
		active_ids = {int(pro.id) for pro in pros}
		unknown_ids = active_ids - set(self.slots_by_pro_id.keys())
		if unknown_ids:
			raise ValueError(f"Active Pro ids are not part of the Pro fleet plan: {sorted(unknown_ids)}")

		# Requested line: select by count.
		# Other lines: keep currently active slots by id.
		selected_slots = []
		for line in sorted(self.lines.values(), key=lambda entry: self._natural_sort_key(entry.id)):
			if line.id == line_id:
				selected_slots.extend(line.slots_for_count(count))
			else:
				selected_slots.extend([slot for slot in line.ordered_slots() if slot.pro_id in active_ids])

		return [
			slot.build_pro(base_pro)
			for slot in sorted(selected_slots, key=lambda slot: slot.pro_id)
		]

	def build_pros(self, line_counts: dict[str, int], base_pro: Pro) -> list[Pro]:
		'''
		Build a full active Pro list from requested counts per line.

		Used for initial fleet construction, where there is no previous Pro list
		to preserve. Missing line ids mean zero active Pros on that line.
		'''
		for line_id in line_counts:
			if line_id not in self.lines:
				raise ValueError(f"Unknown Pro line: {line_id}")

		# Convert line-level counts into the concrete Pro ids expected by the
		# timetable. The simulation backend only sees normal Pro vehicles.
		selected_slots = []
		for line in sorted(self.lines.values(), key=lambda entry: self._natural_sort_key(entry.id)):
			selected_slots.extend(line.slots_for_count(line_counts.get(line.id, 0)))

		return [
			slot.build_pro(base_pro)
			for slot in sorted(selected_slots, key=lambda slot: slot.pro_id)
		]

	@staticmethod
	def _natural_sort_key(value):
		'''
		Sort strings with embedded numbers in human order.
		'''
		return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(value))]

	@staticmethod
	def _normalize_endpoint_id(value) -> str:
		'''
		Strip Chain/Unchain suffixes to get the station identity.
		'''
		endpoint = str(value)
		for suffix in ("_Chain", "_Unchain"):
			if endpoint.endswith(suffix):
				return endpoint[:-len(suffix)]
		raise ValueError(f"Unsupported Pro endpoint name: {endpoint}")

	@staticmethod
	def _normalize_pro_key(value) -> str:
		'''
		Normalize a Pro id value to the timetable key format.
		'''
		key = str(value)
		return key if key.startswith("Pro") else f"Pro{key}"

	@staticmethod
	def _pro_id_from_key(pro_key: str) -> int:
		'''
		Extract the numeric vehicle id from a timetable Pro key.
		'''
		try:
			return int(str(pro_key).partition("Pro")[-1])
		except ValueError as exc:
			raise ValueError(f"Unsupported Pro schedule key: {pro_key}") from exc

	@staticmethod
	def _trip_sort_key(trip: ChainRouteTrip):
		'''
		Sort trips by departure without caring about input timestamp type.
		'''
		return ProFleetPlan._trip_departure_sort_value(trip.departure)

	@staticmethod
	def _trip_departure_sort_value(departure):
		'''
		Convert a departure value into a stable sortable representation.
		'''
		if isinstance(departure, datetime):
			return departure.isoformat()
		return str(departure)

# ------------------------------------------------------------------------------

class OperationalVertices:
	'''
	Operational Area (bounding polygon) class   
	'''
	
	def __init__(self, _vertex_list: list[tuple[float,float]], _id: str="0c14d3cc-fb16-49ff-b999-4c26921132cb", _desc: str="default"):
		'''
		Constructor
		'''
		self.id = _id
		self.vertices = _vertex_list
		self.description = _desc
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"OperationalVertices("
		summary += f"num_vertex: {len(self.vertices)}"
		summary += f", desc: {self.description}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class OperationalArea:
	'''
	Operational Area class (infrastructure)	
	'''
	def __init__(self, _op_area: OperationalVertices, _char_sta: list[ChargingStation],
					_chain_loc: list[ChainingLocation], _park_loc: list[ParkingLocation], 
					_pro_routes_and_trips: ProRoutesAndTrips):
		'''
		Constructor
		'''
		self.operational_area 	  = _op_area
		self.charging_stations 	  = _char_sta
		self.chaining_locations   = _chain_loc
		self.parking_locations 	  = _park_loc
		self.pro_routes_and_trips = _pro_routes_and_trips
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"OperationalArea("
		summary += f"num_charge: {len(self.charging_stations):}"
		summary += f"; num_chain: {len(self.chaining_locations):}"
		summary += f"; num_park: {len(self.parking_locations):}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------

class FleetAndRequests:
	'''
	Fleet and request class	(variable data)
	'''
	
	def __init__(self, _id: int, _dem_scen: DemandScenario, _veh_fle: VehicleFleet):
		'''
		Constructor
		'''
		self.id 				= _id
		self.demand_scenario 	= _dem_scen
		self.vehicle_fleet 		= _veh_fle

		self.demand_dict		= None
		self.fleet_dict			= None

		self.sim_aggregates		= None  # only filled by RW-API
	
	# ------------------
	
	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"FleetAndRequests("
		summary += f"id: {self.id}"
		summary += f"; num_req: {self.demand_scenario.num_requests}"
		summary += f"; num_cab: {len(self.vehicle_fleet.cabs)}"
		summary += f"; num_pro: {len(self.vehicle_fleet.pros)}"
		#summary += "; num_rej: {}".format(self.demand_dict["num_cus_reject"])
		summary += f")"
		
		return summary
	
	# ------------------
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()
	
	# ------------------

	# update attributes after operational planning was executed
	def update_attributes(self):
		vehicle_fleet_dict = {}
		demand_scenario_dict = self.demand_scenario.aggregate_total_req_kpis()
		vehicle_fleet_dict["cabs"] = self.vehicle_fleet.aggregate_total_cab_kpis()
		vehicle_fleet_dict["pros"] = self.vehicle_fleet.aggregate_total_pro_kpis()

		self.demand_dict = demand_scenario_dict
		self.fleet_dict = vehicle_fleet_dict
	
	# ------------------
	
	def write_api_req_file(self):
		
		# ------------------
		def format_time_with_timezone(dt):
			tz = timezone(timedelta(hours=2))
			# Convert the datetime object to the desired timezone
			dt_with_tz = dt.astimezone(tz)
			# Format the datetime object as a string in the desired ISO 8601 format
			formatted =  dt_with_tz.strftime('%Y-%m-%dT%H:%M:%S%z')
			return f"{formatted[:-2]}:{formatted[-2:]}"
		# ------------------
			
		simulation_steps = []
		
		for req in self.demand_scenario.requests:
			
			# ------------------
			
			if ( req.tw_type == False ) :
				target_time = {
					"StartTime": format_time_with_timezone(req.tw_lower),
					"EndTime": format_time_with_timezone(req.tw_upper)
				}
				pickup_time = None
			else:
				target_time = None
				pickup_time = {
					"StartTime": format_time_with_timezone(req.tw_lower),
					"EndTime": format_time_with_timezone(req.tw_upper)
				}
			
			# ------------------
			
			step = {
				"requestParameter": {
					"UserGuid": str(req.id),
					"CurrentLocation": {
						"Longitude": req.pu_lon,
						"Latitude": req.pu_lat
					},
					"TargetLocation": {
						"Longitude": req.do_lon,
						"Latitude": req.do_lat
					},
					
					"targetTime": target_time,
					"pickupTime": pickup_time,
					"needRamp": req.need_ramp,
					"requestedAdults": req.num_persons,
					"requestedChilds": 0,  # Assuming no children as data is not in DataFrame
					"luggage": 0,  # Assuming no luggage as data is not in DataFrame
					"personalPreferences": {
						"allowCarpooling": False,
						"toleratedDelayBefore": 300,
						"toleratedDelayAfter": 300
					}
				},
				"SimulatedTime": format_time_with_timezone(req.register_time),
				"bookProposal": True,
				"createReport": True
			}
			
			simulation_steps.append(step)
		
		# ------------------
		
		# Final JSON structure
		final_structure = {
			"simulationSteps": simulation_steps,
			"createInitialReport": True
		}
		
		return final_structure
	
	# ------------------
	
	def write_api_base_file(self, _oa: OperationalArea):
		
		# cab schedule entries
		cab_schedules = []
		for cab in self.vehicle_fleet.cabs:
			cab_entry = {
				"id": f"Cab{cab.id}",
				"schedule": {
					#"StartTime": f"{cab.schedule_start_time}",
					#"EndTime": f"{cab.schedule_end_time}"
					"StartTime": "2025-10-18T05:45:00+02:00",
					"EndTime": "2025-10-18T22:30:00+02:00"
				},
				"label": f"Cab{cab.id}",
				"licensePlate": f"PB-NE-{cab.id}",
				"powerConsumption": {
					"speed": cab.pc_speed,
					"wind": cab.pc_wind,
					"loadCapacity": cab.pc_load_capacity,
					"tireTraction": cab.pc_tire_traction,
					"weatherConditions": cab.pc_weather_condition,
					"climatronic": cab.pc_climatronic,
					"regenerativeBraking": cab.pc_regenerative_breaking
				},
				"FlatratEnergyConsumption": cab.flatrate_power_consumtion,
				"Risc": cab.risc,
				"TotalEnergyCapacity": cab.total_energy_capacity,
				"InitialEnergyCapacity": cab.init_energy_capacity,
				#"TotalEnergyCapacity": 20000,
				#"InitialEnergyCapacity": 20000,
				"InitialLocation": {
					"Longitude": cab.init_location_lon,
					"Latitude": cab.init_location_lat
				},
				"maxSpeed": cab.max_speed,
				"regenerativePower": cab.regenerative_power,
				"operatingCompany": f"{cab.operating_company}",
				"maxiumRegenerativePower": cab.maximum_regenerative_power,
				"defaultEnergyConsuptionPerM": cab.default_energy_consumption_per_m,
				"vehicleType": f"{cab.vehicle_type}",
				"stateOfSchedule": f"{cab.state_of_schedule}",
				"hasRamp": cab.has_ramp,
				"seats": cab.seats,
				"childSeats": cab.child_seats,
				"luggage": cab.luggage,
				"chargingCurve": [
					cab.charging_curve[0]
				],
				"maxSpeedAutonomous": cab.max_speed_autonomous
			}
			cab_schedules.append(cab_entry)
		
		# ------------------
		
		# pro schedule entry
		pro_schedules = []
		for pro in self.vehicle_fleet.pros:
			#continue
			pro_entry = {
				"MaxCabChain": pro.max_cabs,
				"AdditionalEnergyConsumptionPerCab": pro.additional_consumption_per_cab,
				"id": f"Pro{pro.id}",
				"schedule": {
					#"StartTime": f"{cab.schedule_start_time}",
					#"EndTime": f"{cab.schedule_end_time}"
					"StartTime": "2025-10-18T05:00:00+02:00",
					"EndTime": "2025-10-18T23:00:00+02:00"
				},
				"label": f"Pro{pro.id}",
				"licensePlate": f"PB-PR-{pro.id}",
				"powerConsumption": {
					"speed": pro.pc_speed,
					"wind": pro.pc_wind,
					"loadCapacity": pro.pc_load_capacity,
					"tireTraction": pro.pc_tire_traction,
					"weatherConditions": pro.pc_weather_condition,
					"climatronic": pro.pc_climatronic,
					"regenerativeBraking": pro.pc_regenerative_breaking
				},
				"FlatratEnergyConsumption": pro.flatrate_power_consumtion,
				"Risk": pro.risc,
				"TotalEnergyCapacity": pro.total_energy_capacity,
				"InitialEnergyCapacity": pro.init_energy_capacity,
				#"TotalEnergyCapacity": 20000,
				#"InitialEnergyCapacity": 20000,
				"InitialLocation": {
					"Longitude": pro.init_location_lon,
					"Latitude": pro.init_location_lat
				},
				"maxSpeed": pro.max_speed,
				"regenerativePower": pro.regenerative_power,
				"operatingCompany": f"{pro.operating_company}",
				"maxiumRegenerativePower": pro.maximum_regenerative_power,
				"defaultEnergyConsuptionPerM": pro.default_energy_consumption_per_m,
				"vehicleType": f"{pro.vehicle_type}",
				"stateOfSchedule": f"{pro.state_of_schedule}",
			}
			if pro.max_power_supply is not None:
				pro_entry["MaxCabChargingPower"] = pro.max_power_supply
			pro_schedules.append(pro_entry)
		
		# ------------------
		
		# chargin stations
		charging_stations = []
		for cs in _oa.charging_stations:
			cs_entry = {
				"Guid": f"{cs.id}",
				"Location": {
					"Longitude": cs.location_lon,
					"Latitude": cs.location_lat
				},
				"MaximumStoppingTime": 3600.0,
				"type": "Electric",
				"state": "Free",
				"maximumPowerSupply": cs.max_supply,
				"MaximumRegenerativePower": 0,
				"OpeningHours": [
					{
						"WeekDays": "All",
						"Min": f"{cs.schedule_start}",
						"Max": f"{cs.schedule_end}"
					}
				],
				"operators": [
					#{
						#"chargingOperatorGuid": "",
						#"name": "",
						#"defaultPrice": 1
					#}
				],
				"voltage": 0,
				"amperage": 0,
				"owner": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
			}
			charging_stations.append(cs_entry)
		
		# ------------------
		
		# parking locatins
		chaining_locations = []
		for cl in _oa.chaining_locations:
			#continue
			cl_entry = {
				"Guid": f"{cl.id}",
				"LocationStart": {
					"Longitude": cl.start_location_lon,
					"Latitude": cl.start_location_lat
				},
				"LocationEnd": {
					"Longitude": cl.end_location_lon,
					"Latitude": cl.end_location_lat
				},
				"Type": cl.type,
				"AdditionalTime": cl.additional_time,
			}
			chaining_locations.append(cl_entry)
		# ------------------
		
		# parking locatins
		parking_locations = []
		for pl in _oa.parking_locations:
			#pl_entry = {
				#"guid": "",
				#"location": {
					#"longitude": 1,
					#"latitude": 1
				#},
				#"maximumStoppingTime": 1
			#}
			pass
		
		# ------------------
		
		# chaining routes
		chain_routes = []
		for cr in _oa.pro_routes_and_trips.chain_routes:
			#continue
			cr_entry = {
				"Guid": f"{cr.id}",
				"StartLocation": f"{cr.start_location}",
				"IntermediateChainingLocations": [
					#{
						#"chainingLocation": "",
						#"type": "NotAllowed",
						#"offsetArrival": "",
						#"offsetDeparture": ""
					#}
				],
				"EndLocation": f"{cr.end_location}",
				"Duration": cr.duration,
				"Distance": cr.distance
			}
			chain_routes.append(cr_entry)
			
		# ------------------
		
		# chain route schedules
		chain_route_schedules = []
		for crt in _oa.pro_routes_and_trips.chain_route_trips:
			#continue
			crt_entry = {
				"Guid": f"{crt.id}",
				"ChainRoute": f"{crt.chain_route}",
				"ProSchedule": f"{crt.pro_schedule}",
				"Departure": crt.departure,
				"Arrival": crt.arrival,
				#"duration": "null"
			}
			chain_route_schedules.append(crt_entry)
		
		# ------------------
		
		# operational area
		operational_areas = []
		location_border = [
			{
				"Longitude": lon,
				"Latitude": lat
			}
			for lon, lat in _oa.operational_area.vertices
		]
		oa_entry = {
			"Guid": _oa.operational_area.id,
			"ShortHandle": _oa.operational_area.description,
			"LocationBorder": location_border,
			"GuiltyRange": {
				"StartTime": "2025-07-03T00:00:00+02:00",
				"EndTime": "2099-01-19T00:00:00+01:00"
			},
			"Operator": "74d2b5aa-2d62-4c06-91ae-c8ed9f4797ed"
		}
		operational_areas.append(oa_entry)
			
		# ------------------
		
		# start of simulation
		simulation_init_time = "2025-01-01T12:58:53.865Z"
		
		# ------------------
		
		# planning parameters
		planning_config = {
			"ToleranceDrivingTime": 2.0,
			"ToleranceCapacity": 5.0,
			"ChainTime": 120,
			"ProposalTimer": 120,
			"ProposalCount": 3,
			"PlanningHorizon": 6,
			"ChargingStartThreshhold": 20.0,
			"ChargingEndThreshhold": 100.0,
			"UseProThreshhold": 2000,
			"Mode": "Insert",
			"MapStartAndOnMap": False,
			"AnalyzeMode": False
		}
		
		# ------------------
		
		# Final JSON structure
		final_structure = {
			"cabSchedules": cab_schedules,
			"proSchedules": pro_schedules,
			"chargingPoints": charging_stations,
			"chainingLocations": chaining_locations,
			"chainRoutes": chain_routes,
			"chainRouteSchedules": chain_route_schedules,
			"parkingLocations": parking_locations,
			"operationAreas": operational_areas,
			"simulationInitTime": simulation_init_time,
			"planningConfiguration": planning_config
		}
		
		# ------------------
		
		return final_structure


# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------


class SearchParameters:
	'''
	Parameter setting for the heuristic
	'''
	
	def __init__(self, _iter: int=6, _time_limit: float=18000, _moves: list[str] | None = None, _rate: float=0,
				_initial_cabs: int=1, _cab_add_step: int=5, _search_mode: str="static",
				_budget_eur: float | None = None, _service_level_min: float | None = None,
				_shrink_pros_allow_retry: bool = False, _shrink_pros_max_tries: int = 1,
				_shrink_pros_trial_probe_below_start: bool = True,
				_shrink_pros_trial_overshoot_correction: bool = False,
				_stagnation_tolerance: float = 0.0,
				_stagnation_tolerance_patience: int = 1,
				_objective_weight: float = 1.0, _objective_terms: list[str] | None = None):
		'''
		Constructor

		_search_mode: "adaptive" (grow -> shrink -> shrink_pros, estimation-based cab growth) or
		"static" (fixed initial Pro count, cabs added in constant steps until growth stagnates).

		_budget_eur: hard cap on the fleet's raw purchase price (CostModel.fleet_price_eur(), not
		amortized, no energy cost). None disables the constraint.

		_service_level_min: hard floor on service_rate (served / valid requests, same 0..1 fraction
		evaluate_solution() already computes). None disables the constraint.

		_shrink_pros_allow_retry: whether a Pro that already failed its Pro-for-cabs trade-off trial
		(see FleetPlanning.optimize()'s shrink_pros trial mechanism) becomes eligible again after a
		later trial succeeds. False (default): a failed Pro is excluded for the rest of shrink_pros.

		_shrink_pros_max_tries: max number of distinct Pros tried per shrink_pros round before
		giving up on it. Each try is a full nested grow+shrink recovery attempt, so this bounds a
		genuinely expensive operation - keep small. 0 disables the trial mechanism entirely (old
		shrink_pros behavior).

		_shrink_pros_trial_probe_below_start: whether a trial whose nested grow found no
		improvement at all (not even one Cab helped) also tries reducing the Cab count below
		what it was at the start of the trial - checking whether the just-removed Pro was
		actively hurting the schedule rather than merely being redundant. Only ever considered
		when grow found nothing; if grow succeeded at all, this is not consulted. True
		(default): try it, at the cost of a probe iteration whenever nested growth didn't help
		at all (common once the search is past the point where Cabs are the bottleneck). False:
		conclude the trial immediately in that case instead.

		_shrink_pros_trial_overshoot_correction: whether a trial whose nested grow succeeded via
		a single jump of more than one Cab also walks back down toward (never below) the Cab
		count the trial started with, checking whether the full jump was actually necessary.
		Only relevant when such a jump happened - a peak reached one Cab at a time has already
		had every intermediate count verified, nothing left to correct. False (default): accept
		grow's peak as final, since with the current estimate this case is expected to be rare;
		True: spend the extra iteration(s) to verify.

		_stagnation_tolerance: fraction of a reference rank's own score a step is allowed to
		fall short by and still count as "close enough" to continue, instead of ending the
		current phase/trial the instant a step fails to strictly improve. Applies to the top-
		level grow and shrink phases, and to the shrink_pros trial mechanism (both an ordinary
		step against the trial's best-so-far, and judging whether a grow stage's peak improved
		meaningfully over the bare removal it started from, see
		_shrink_pros_trial_probe_below_start). Not applied to shrink_pros's own outer
		stagnation check (only reached when trials are disabled or exhausted): removing a Pro
		isn't a single homogeneous lever the way adding/removing a Cab is, so tolerating a bad
		removal there has no equivalent recovery mechanism backing it up. Never softens a
		feasibility regression regardless of value. 0.0 (default): no tolerance, a step must
		strictly improve, identical to today's behavior.

		_stagnation_tolerance_patience: max number of consecutive tolerance-driven steps (steps
		kept only because of _stagnation_tolerance, not because they improved) taken before
		giving up for real, even if still within tolerance. Bounds a long plateau to a fixed
		number of extra iterations instead of open-ended stepping. Only relevant when
		_stagnation_tolerance > 0.

		_objective_terms: 1 or 2 names from fleet_planning.py's OBJECTIVE_METRICS registry,
		selecting which KPIs make up evaluate_solution()'s objective_val. None (default):
		["total_served", "avg_cost_per_trip_eur"], today's two selectable metrics.

		_objective_weight: alpha in [0,1], weight on the first selected term vs. the second.
		Only meaningful when _objective_terms has 2 entries - with 1, this is ignored
		entirely (see fleet_planning.py's compute_objective()). 1.0 (default): pure first
		term, same as the old total_served+tiebreaker objective when terms are left default.
		'''
		self.iter_limit		= _iter
		self.time_limit 	= _time_limit
		self.initial_cabs 	= _initial_cabs
		self.cab_add_step 	= _cab_add_step
		self.search_mode 	= _search_mode
		self.budget_eur 	= _budget_eur
		self.service_level_min = _service_level_min
		self.shrink_pros_allow_retry = _shrink_pros_allow_retry
		self.shrink_pros_max_tries = _shrink_pros_max_tries
		self.shrink_pros_trial_probe_below_start = _shrink_pros_trial_probe_below_start
		self.shrink_pros_trial_overshoot_correction = _shrink_pros_trial_overshoot_correction
		self.stagnation_tolerance = _stagnation_tolerance
		self.stagnation_tolerance_patience = _stagnation_tolerance_patience
		self.objective_weight = _objective_weight
		self.objective_terms = list(_objective_terms) if _objective_terms else ["total_served", "avg_cost_per_trip_eur"]

	# print information
	def __str__(self):
		# Generate a summary of the DemandScenario object
		summary = f"SearchParameters("
		summary += f"iter_limit: {self.iter_limit}"
		summary += f"time_limit: {self.time_limit}"
		summary += f"initial_cabs: {self.initial_cabs}"
		summary += f"cab_add_step: {self.cab_add_step}"
		summary += f"search_mode: {self.search_mode}"
		summary += f"budget_eur: {self.budget_eur}"
		summary += f"service_level_min: {self.service_level_min}"
		summary += f"shrink_pros_allow_retry: {self.shrink_pros_allow_retry}"
		summary += f"shrink_pros_max_tries: {self.shrink_pros_max_tries}"
		summary += f"shrink_pros_trial_probe_below_start: {self.shrink_pros_trial_probe_below_start}"
		summary += f"shrink_pros_trial_overshoot_correction: {self.shrink_pros_trial_overshoot_correction}"
		summary += f"stagnation_tolerance: {self.stagnation_tolerance}"
		summary += f"stagnation_tolerance_patience: {self.stagnation_tolerance_patience}"
		summary += f"objective_weight: {self.objective_weight}"
		summary += f"objective_terms: {self.objective_terms}"
		summary += f")"
		
		return summary
	
	# for now, use __str__
	def __repr__(self):
		return self.__str__()

# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------


class CostModel:
	'''
	Fleet economics: vehicle/energy prices, amortization assumptions, and the DRT-style
	fare model used to price a customer trip. All values are best-guess placeholders until
	real figures are available. daily_fleet_cost()/split_grid_and_pro_energy_kwh() are used
	both by dashboard-side KPI reporting and (as of the averageCustomerTripCostEur search
	objective) by the fleet-planning heuristic itself - see cabPrice/proPrice as the future
	home for an optional hard fleet-size/budget constraint on top of that.
	'''

	def __init__(self, _cab_price: float=100000.0, _pro_price: float=250000.0,
				_stationary_kwh_price: float=0.2, _pro_kwh_price: float=0.2,
				_cab_lifetime_years: float=8.0, _pro_lifetime_years: float=8.0,
				_interest_rate_percent: float=0.0, _operating_days_per_year: float=365.0,
				_fare_base_eur: float=0.5, _fare_distance_eur_per_m: float=0.0003,
				_fare_time_eur_per_hour: float=0.0, _fare_min_per_trip_eur: float=2.0,
				_fare_daily_subscription_eur: float=0.0):
		'''
		Constructor

		_cab_price / _pro_price: one-time purchase price per vehicle (EUR).
		_stationary_kwh_price: grid electricity price (EUR/kWh) for energy a Cab draws from
		a stationary charging station, including an assumed overnight top-off for any
		charge consumed but not replaced within the simulated day (Pro runs a fixed
		daytime timetable, so that top-off is assumed to happen at a stationary charger).
		_pro_kwh_price: price (EUR/kWh) for energy a Cab receives while chained to a Pro.
		Pro runs on a hydrogen fuel cell, not grid electricity, and we have neither the
		fuel cell's conversion efficiency nor a hydrogen price - so rather than model
		hydrogen mass consumption (which we can't), this is a flat EUR/kWh conversion
		applied to the already-tracked kWh a Cab receives from a Pro. Defaults to parity
		with _stationary_kwh_price (no basis yet to assume a premium or discount); override
		once real hydrogen economics are known.
		_cab_lifetime_years / _pro_lifetime_years: assumed useful life for amortizing the
		purchase price down to a daily cost.
		_interest_rate_percent: 0 => straight-line depreciation; >0 => annuity-style
		amortization (like a loan repayment schedule).
		_operating_days_per_year: converts an annual amortized cost into a daily one, to be
		comparable against a single simulated day of revenue.
		_fare_*: MATSim DRT-style fare model (base fare + distance rate + time rate,
		floored at a minimum fare per trip; daily subscription fee is not counted per-trip).
		'''
		self.cab_price 					= _cab_price
		self.pro_price 					= _pro_price
		self.stationary_kwh_price 			= _stationary_kwh_price
		self.pro_kwh_price 					= _pro_kwh_price
		self.cab_lifetime_years 			= _cab_lifetime_years
		self.pro_lifetime_years 			= _pro_lifetime_years
		self.interest_rate_percent 		= _interest_rate_percent
		self.operating_days_per_year 		= _operating_days_per_year
		self.fare_base_eur 					= _fare_base_eur
		self.fare_distance_eur_per_m 		= _fare_distance_eur_per_m
		self.fare_time_eur_per_hour 		= _fare_time_eur_per_hour
		self.fare_min_per_trip_eur 			= _fare_min_per_trip_eur
		self.fare_daily_subscription_eur 	= _fare_daily_subscription_eur

	# print information
	def __str__(self):
		summary = f"CostModel("
		summary += f"cab_price: {self.cab_price}, "
		summary += f"pro_price: {self.pro_price}, "
		summary += f"stationary_kwh_price: {self.stationary_kwh_price}, "
		summary += f"pro_kwh_price: {self.pro_kwh_price}, "
		summary += f"cab_lifetime_years: {self.cab_lifetime_years}, "
		summary += f"pro_lifetime_years: {self.pro_lifetime_years}, "
		summary += f"interest_rate_percent: {self.interest_rate_percent}, "
		summary += f"operating_days_per_year: {self.operating_days_per_year}, "
		summary += f"fare_base_eur: {self.fare_base_eur}, "
		summary += f"fare_distance_eur_per_m: {self.fare_distance_eur_per_m}, "
		summary += f"fare_time_eur_per_hour: {self.fare_time_eur_per_hour}, "
		summary += f"fare_min_per_trip_eur: {self.fare_min_per_trip_eur}, "
		summary += f"fare_daily_subscription_eur: {self.fare_daily_subscription_eur}"
		summary += f")"

		return summary

	# for now, use __str__
	def __repr__(self):
		return self.__str__()

	def daily_cab_cost(self) -> float:
		'''
		Amortized daily cost of one Cab (purchase price spread over its assumed lifetime).
		'''
		return self._amortize(self.cab_price, self.cab_lifetime_years)

	def daily_pro_cost(self) -> float:
		'''
		Amortized daily cost of one Pro (purchase price spread over its assumed lifetime).
		'''
		return self._amortize(self.pro_price, self.pro_lifetime_years)

	def _amortize(self, purchase_price: float, lifetime_years: float) -> float:
		'''
		Spread a one-time purchase price over its assumed useful life into a comparable daily
		cost. interest_rate_percent == 0 => straight-line depreciation (purchase_price /
		lifetime_years). interest_rate_percent > 0 => annuity-style amortization (like a loan
		repayment schedule), which front-loads the effective cost relative to straight-line.
		'''
		if lifetime_years <= 0 or self.operating_days_per_year <= 0:
			return 0.0

		rate = self.interest_rate_percent / 100.0
		if rate == 0:
			annual_cost = purchase_price / lifetime_years
		else:
			annual_cost = purchase_price * (rate / (1 - (1 + rate) ** (-lifetime_years)))

		return annual_cost / self.operating_days_per_year

	@staticmethod
	def split_grid_and_pro_energy_kwh(consumed_kwh: float, stationary_kwh: float, pro_kwh: float) -> Tuple[float, float]:
		'''
		Split a day's Cab energy into (grid_energy_kwh, pro_energy_kwh) for separate pricing.

		grid_energy_kwh = stationary-charged energy + the day-end SOC deficit (energy the fleet
		actually consumed driving but that never got recharged within the simulated window).
		That deficit is assumed to need an overnight top-off at a stationary depot charger,
		since Pro runs a fixed daytime timetable and isn't available to recharge Cabs overnight
		- so it belongs on the grid side, not the Pro side, of the split.

		pro_energy_kwh is energy a Cab received while chained to a Pro - hydrogen fuel-cell-
		derived, priced separately via pro_kwh_price instead of the grid rate.

		Single source of truth for this split: both the dashboard's post-hoc KPI reporting
		(frontend/output_utils.py) and the fleet-planning search objective (fleet_planning.py)
		call this instead of each re-deriving the deficit assumption independently.
		'''
		deficit_kwh = max(0.0, consumed_kwh - stationary_kwh - pro_kwh)
		return (stationary_kwh + deficit_kwh, pro_kwh)

	def daily_fleet_cost(self, num_cabs: float, num_pros: float, grid_energy_kwh: float, pro_energy_kwh: float) -> float:
		'''
		One day's total fleet cost: amortized vehicle cost (num_cabs*daily_cab_cost() +
		num_pros*daily_pro_cost()) plus that day's energy cost (grid_energy_kwh*
		stationary_kwh_price + pro_energy_kwh*pro_kwh_price). Energy inputs are expected to
		already be split via split_grid_and_pro_energy_kwh().
		'''
		vehicle_cost = num_cabs * self.daily_cab_cost() + num_pros * self.daily_pro_cost()
		energy_cost = grid_energy_kwh * self.stationary_kwh_price + pro_energy_kwh * self.pro_kwh_price
		return vehicle_cost + energy_cost

	def fleet_price_eur(self, num_cabs: float, num_pros: float) -> float:
		'''
		Raw fleet purchase price (num_cabs*cab_price + num_pros*pro_price) - not amortized, no
		energy cost. Distinct from daily_fleet_cost() on purpose: this is the figure a hard
		budget constraint on the fleet-planning search compares against, not a per-day cost.
		'''
		return num_cabs * self.cab_price + num_pros * self.pro_price

	def trip_fare(self, distance_m: float, driving_time_s: float) -> float:
		'''
		MATSim DRT-style fare for a single customer trip: base fare + distance-based fare +
		time-based fare, floored at a minimum fare per trip.
		'''
		hours = driving_time_s / 3600.0
		fare = self.fare_base_eur + self.fare_distance_eur_per_m * distance_m + self.fare_time_eur_per_hour * hours
		return max(fare, self.fare_min_per_trip_eur)

# ------------------------------------------------------------------------------


if __name__ == "__main__":
	print("# models/main")
	
	now = datetime.now()
	
	req = Request(0,0.0,0.0,0.1,0.1,now,now,now)
	print(req)
	demand = DemandScenario([req])
	print(demand)
	agg_req_kpi = demand.aggregate_total_req_kpis()
	print(agg_req_kpi)
	cab = Cab(0,0.0,0.0,now,now)
	print(cab)
	pro = Pro(0,0.0,0.0,now,now)
	print(pro)
	vf = VehicleFleet([cab],[pro], (cab.init_location_lat, cab.init_location_lon), (pro.init_location_lat, pro.init_location_lon))
	print(vf)
	agg_cab_kpi = vf.aggregate_total_cab_kpis()
	print(agg_cab_kpi)
	vf.add_cab(Cab(1,0.0,0.0,now,now))
	print(vf)
	vf.remove_cab(0)
	print(vf)
	vf.add_cab(Cab(2,0.0,0.0,now,now))
	cs = ChargingStation(0,0.0,0.0)
	print(cs)
	cl = ChainingLocation(0,0.0,0.0)
	print(cl)
	pl = ParkingLocation(0,0.0,0.0)
	print(pl)
	ov = OperationalVertices([(0.0,0.0),(0.1,0.1)])
	print(ov)
	oa = OperationalArea(ov,[cs],[cl],[pl])
	print(oa)
	far = FleetAndRequests(0,demand,vf)
	print(far)
	basedata = far.write_api_base_file(oa)
	print(basedata)
	
	#basedata2 = ic.parse_base_file()
	#print(basedata2)
	filename = "./BaseDataTest.json"
	filename2 = "./BaseDataTest2.json"
	with open(filename, "w") as f:
		import json
		json.dump(basedata, f, indent='\t')
	import sys
	sys.exit(-1)
	#with open(filename2, "w") as f:
		#import json
		#json.dump(basedata2, f, indent='\t')
