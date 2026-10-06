#!/usr/bin/python3

from datetime import datetime
from pathlib import Path
import copy
import time
import math

import sys

import json
import argparse
from models import Request, DemandScenario, Cab, Pro, VehicleFleet, ChargingStation, ChainingLocation, ParkingLocation, OperationalVertices, OperationalArea, FleetAndRequests, SearchParameters, ProRoutesAndTrips, ChainRoute, ChainRouteTrip, ProFleetPlan, CostModel

from utils import read_request_json, read_base_json, build_pro_timetable

# False turns off the run log debug/fp.log
WRITE_STATUS_LOG = True



# ------------------------------------------------------------------------------


def _objective_metric_total_served(kpis: dict, num_requests: int) -> float:
	return kpis["total_served"]


def _objective_metric_avg_cost_per_trip_eur(kpis: dict, num_requests: int) -> float:
	return -kpis["avg_cost_per_trip_eur"] * (num_requests / 3.0)


def _objective_metric_avg_profit_per_trip_eur(kpis: dict, num_requests: int) -> float:
	return kpis["avg_profit_per_trip_eur"] * (num_requests / 3.0)


def _objective_metric_avg_in_vehicle_time_s(kpis: dict, num_requests: int) -> float:
	return -kpis["avg_in_vehicle_time_s"] * 7.0


# name -> (kpis, num_requests) -> objective-ready contribution (higher is better, already
# signed/scaled by the metric itself, no generic external scale/direction config, since e.g.
# avg_cost_per_trip_eur's scale depends on num_requests while avg_in_vehicle_time_s's is a flat
# constant, empirically derived from real search-trajectory spreads across both, not a rule
# either could share). Adding a new selectable objective metric is one function + one entry here.
OBJECTIVE_METRICS = {
	"total_served": _objective_metric_total_served,
	"avg_cost_per_trip_eur": _objective_metric_avg_cost_per_trip_eur,
	"avg_profit_per_trip_eur": _objective_metric_avg_profit_per_trip_eur,
	"avg_in_vehicle_time_s": _objective_metric_avg_in_vehicle_time_s,
}


def compute_objective(kpis: dict, num_requests: int, terms: list[str], alpha: float) -> float:
	"""
	Combine 1 or 2 selected OBJECTIVE_METRICS entries into a single value. With one term,
	alpha is ignored entirely, no weighting, just that term. With two, alpha weights the
	first against the second.
	"""
	values = [OBJECTIVE_METRICS[name](kpis, num_requests) for name in terms]
	if len(values) == 1:
		return values[0]
	return alpha * values[0] + (1 - alpha) * values[1]


class bcolors:
	HEADER = '\033[95m'
	OKBLUE = '\033[94m'
	OKCYAN = '\033[96m'
	OKGREEN = '\033[92m'
	WARNING = '\033[93m'
	FAIL = '\033[91m'
	ENDC = '\033[0m'
	BOLD = '\033[1m'
	UNDERLINE = '\033[4m'



# ------------------------------------------------------------------------------

class FleetPlanning:
	'''
	============================
	= Fleet Optimization Class =
	============================
	
	* Oracle based optimization
	    using  a black-box for operational planning for a provided fleet
	  to determine an optimal fleet of vehicles, consisting of two kinds of vehicles, namely
	   - "Cabs" (electric; transport of customers)
	  and
	   - "Pros" (traction engine to move several Cabs over long distances with a higher speed and having chargin capabilities)
	  to serve a fixed set of customers in a predefined region with a fixed infrastructure (e.g., service area, positions of charging stations, etc.).
	* The fleet configuration is iteratively changed by applying "moves", selected according to the
	  current search phase, using the oracle to compute an operational plan for the resulting fleet.
	   - "grow": add cabs (an estimated number in adaptive mode) while the objective improves.
	   - "shrink": remove one cab at a time while the objective improves.
	   - "shrink_pros": deactivate the least-used Pro, then try to recover fleet size with the same
	     grow/shrink moves, keeping the trade-off only if it doesn't lose ground.
	* Each candidate's resulting plan is ranked against the current best; an improving, feasible result
	  is kept and the search continues in the same phase, otherwise the phase advances (grow to shrink
	  to shrink_pros) until a stop condition is reached.
	----------------------------------------------------------------
	 - After initialization of a class object, the specified instance is solved automatically by calling the "FleetPlanning.optimize()" function.
	 
	 - The complete set of solutions until termination (iteration limit, time limit, or search stagnation) are stored in the member variable "FleetPlanning.solution_pool".
	
	 - Parameters regarding the algorithmic procedure (e.g., growth step size, iteration limits, etc.) are stored in "FleetPlanning.search_parameters" (part of initialization input).
	'''
	def __init__(self, _params: SearchParameters,
			  _operational_area: OperationalArea, _demand_scenario: DemandScenario,
			  _base_cab: Cab, _base_pro: Pro=None,
			  _verbose: int=0, _use_sim: str="custom",
			  file_name = "example_demand.json",
			  _rw_cache: str="auto", _resume_from: int=0, _rw_retries: int=1,
			  _use_depots: bool=False, _custom_sim_overrides: dict=None,
			  _cost_model: CostModel=None):
		'''
		Constructor
		'''

		#print("# call FleetPlanning.construtor __init__()")
		
		# static stuff
		self.search_parameters	= _params 						# optimization algorithm parameters
		self.operational_area 	= _operational_area 			# geograph, charging stations, etc. [OperationalArea]
		self.demand_scenario 	= _demand_scenario 				# requests [DemandScenario]
		self.base_cab 			= _base_cab 					# basic cab vehicle [Cab]
		self.base_pro 			= _base_pro 					# basic pro vehicle [Pro]
		self.use_depots			= _use_depots
		self.pro_fleet_plan		= ProFleetPlan.from_pro_routes_and_trips(self.operational_area.pro_routes_and_trips, self.operational_area.chaining_locations)
		
		# solution variables
		self.current_fleet 		= None 							# unused; fleet changes currently use local working_fleet copies
		self.current_solution 	= None 							# current solution [FleetAndRequests]
		self.solution_pool 		= [] 							# solutions so far [list[FleetAndRequests]]
		
		self.best_solution 		= None 							# we'll see [FleetAndRequests]
		# (is_feasible, score), see _solution_rank(). (False, -inf) so any first evaluated
		# solution always registers as an improvement, feasible or not, same role the old plain
		# best_val=-inf sentinel played, extended to the two-tier feasible/infeasible ordering.
		self.best_rank 			= (False, float("-inf"))
		
		# operation planning "black box"
		self.use_sim       = _use_sim
		self.input_request_file = file_name.split(".")[0].split("/")[-1]
		if self.use_sim == "sumo":
			from sumo.sumo_simulation import SumoSimulation
			sumo_overrides = _custom_sim_overrides if isinstance(_custom_sim_overrides, dict) else {}
			output_folder = sumo_overrides.get("experiment_output", {}).get("config_folder")
			self.operational_planning = SumoSimulation(self.operational_area, self.base_cab, self.base_pro, self.input_request_file, _output_folder=output_folder)
		elif self.use_sim == "custom":
			from custom_sim.custom_simulation import CustomSimulation
			router_info = {
				"cab": {
					"max_speed_mps": self.base_cab.max_speed,
					"energy_per_wh_m": self.base_cab.default_energy_consumption_per_m,
				}
			}
			if self.base_pro is not None:
				router_info["pro"] = {
					"max_speed_mps": self.base_pro.max_speed,
					"energy_per_wh_m": self.base_pro.default_energy_consumption_per_m,
				}
			self.operational_planning = CustomSimulation(
				_operational_area,
				_router_info=router_info,
				_parameter_overrides=_custom_sim_overrides,
			)
		elif self.use_sim == "rw":
			from rw.rw_operations import rwAPI
			rw_overrides = _custom_sim_overrides if isinstance(_custom_sim_overrides, dict) else {}
			output_folder = rw_overrides.get("experiment_output", {}).get("config_folder")
			self.operational_planning = rwAPI(_operational_area, _output_folder=output_folder)
		
		# I/O path definitions
		self.BASE_DIR 		= Path(__file__).resolve().parent
		self.DATA_DIR 		= self.BASE_DIR / "data"
		self.INPUT_DIR 		= self.DATA_DIR / "input"
		self.OUTPUT_DIR 	= self.DATA_DIR / "output"
		
		# more stuff
		self.iteration 		= 0
		self.time 			= 0
		self.time_oracle 	= 0
		self.verbose 		= _verbose
		self.rw_cache		= _rw_cache
		self.resume_from	= _resume_from
		self.rw_retries		= _rw_retries

		# Cost model for the search objective (evaluate_solution() below). Defaults unless a
		# caller passes one, see --custom_sim_experiment_config's "cost_model" block for the
		# CLI path.
		self.cost_model		= _cost_model if _cost_model is not None else CostModel()

		# optimization phase control
		self.phase 				= "grow" 		# grow: add cabs while objective improves; shrink: remove one cab while objective improves; shrink_pros: deactivate Pros while objective improves
		self.num_single_cab_removals = 0 			# count number of single cab removals in shrink phase to prevent infinite loop if no improvement is possible by removing more cabs
		self.num_empty_cab_removals  = 0 			# count empty cabs removed in bulk when entering the shrink phase (distinct from single-cab probing removals)
		self.num_pro_removals        = 0 			# count Pros deactivated during the shrink_pros phase
		# Pros activated per line at start (capped by each line's max). Static mode
		# has no shrink_pros phase to reduce this later, so it starts at exactly 1 per line;
		# adaptive mode starts higher and reduces it during shrink_pros.
		self.initial_pro_intensity   = 1 if self.search_parameters.search_mode == "static" else 2
		self.pro_remove_step         = 1 			# how many Pros to deactivate per shrink_pros iteration (shrink intensity)

		# fleet compositions already run through the oracle, keyed by _fleet_key(): grow/shrink
		# can revisit the same (num_cabs, active Pro ids), so this avoids a repeat oracle call
		self.visited_fleets = {}

		# shrink_pros Pro-for-cabs trade-off trials (see optimize()'s shrink_pros block):
		# trial_stage is None outside a trial, "grow"/"shrink" during one. bubble_rank/
		# peak_solution track the trial's own best so far; peak is what current_solution
		# reverts to on the grow->shrink handoff. start_cabs is the Cab count at trial start.
		# excluded_ids/tries persist across trials within one shrink_pros phase.
		self.trial_stage = None
		self.trial_bubble_rank = None
		self.trial_peak_solution = None
		self.trial_start_cabs = None
		self.trial_checkpoint = None
		self.trial_pro_id = None
		# True if a grow step in this trial jumped >1 cab, overshoot correction checks it
		self.trial_had_multi_cab_jump = False
		# nested shrink's question: "overshoot" (back toward trial_start_cabs) or "below_start"
		self.trial_shrink_mode = None
		# bare removal's own rank, captured once at trial start and never touched by
		# tolerance-driven stepping. The grow stage's "did this find anything meaningful"
		# check compares its final peak against this fixed reference (see
		# stagnation_tolerance), so it isn't fooled by peak having quietly drifted
		self.trial_bare_removal_rank = None
		# consecutive steps kept only via stagnation_tolerance (not a genuine improvement);
		# resets to 0 on every genuine improvement, capped by stagnation_tolerance_patience
		# so a plateau can't iterate indefinitely
		self.trial_tolerance_streak = 0
		# same idea as trial_tolerance_streak, for the top-level grow/shrink phases instead
		# of a shrink_pros trial. Separate counter since grow/shrink run outside any trial
		self.stagnation_tolerance_streak = 0
		# pre-removal served count, set on every deactivate_least_used_pro call; committed
		# into trial_recovery_target once the trial-start block confirms a trial is starting
		self.pending_pre_removal_served = None
		# best_rank from right before this Pro was touched, same timing as
		# pending_pre_removal_served, committed into trial_checkpoint so succeeded credits the
		# plain removal itself, not just whatever the nested grow/shrink found on top of it
		self.pending_pre_removal_best_rank = None
		# nested grow's target while trial_stage=="grow", see estimate_cabs_for_requests()
		self.trial_recovery_target = None
		self.shrink_pros_excluded_ids = set()
		self.shrink_pros_tries = 0
		# Overshoot-correction steps stopped by the trial_start_cabs floor, printed in
		# the run summary to help judge shrink_pros_trial_overshoot_correction's default.
		self.trial_overshoot_floor_hits = 0


	# ----------------------------------------
	
	def set_io_paths (self, _input: str, _output: str):
		if ( self.verbose > 0 ):
			print("# call Fleet_planning.set_io_path()")
		self.INPUT_DIR = _input
		self.OUTPUT_DIR = _output
	
	# ----------------------------------------

	def find_pro_line_order(self):
		'''
		Order in which to check Pro lines.
		'''
		return sorted(self.pro_fleet_plan.lines.values(), key=lambda line: line.id)

	# ----------------------------------------

	def _initial_pro_line_counts(self, _start_pros: int) -> dict[str, int]:
		# Keep the old start_pros meaning:
		# -1: one Pro per line
		# 0: no Pros
		# >0: assign round-robin over lines
		pro_lines = self.find_pro_line_order()
		line_counts = {line.id: 0 for line in pro_lines}

		if _start_pros == -1:
			for line in pro_lines:
				if line.max_count() > 0:
					line_counts[line.id] = 1
			return line_counts

		if _start_pros == 0:
			return line_counts

		if _start_pros < -1:
			raise ValueError(f"Invalid initial Pro count: {_start_pros}")

		remaining = _start_pros
		while remaining > 0:
			added = False
			for line in pro_lines:
				if line_counts[line.id] >= line.max_count():
					continue
				line_counts[line.id] += 1
				remaining -= 1
				added = True
				if remaining == 0:
					break

			if not added:
				raise ValueError(f"Initial Pro count {_start_pros} exceeds available Pro timetable slots")

		return line_counts

	# ----------------------------------------
	
	def initial_solution(self,_start_cabs=None,_start_pros=-1):
		'''
		Get first solution from test files
		'''
		if ( self.verbose > 0 ):
			print("# call FleetPlanning.initial_solution()")
		
		# set initial fleet
		start_cabs = self.search_parameters.initial_cabs if _start_cabs is None else _start_cabs
		start_cabs = start_cabs if start_cabs > 0 else self.search_parameters.initial_cabs
		cab_list = [copy.deepcopy(self.base_cab) for i in range(0,start_cabs)]
		for i,cab in enumerate(cab_list):
			cab_list[i].set_id(i+1)
		pro_list = []
		if self.base_pro is not None:
			if _start_pros == -1:
				# Default heuristic start: every line active at the initial Pro
				# intensity (capped per line), which the shrink_pros phase reduces.
				line_counts = {
					line.id: min(self.initial_pro_intensity, line.max_count())
					for line in self.find_pro_line_order()
				}
			else:
				line_counts = self._initial_pro_line_counts(_start_pros)
			# Convert line counts to the concrete Pro ids used by the timetable.
			pro_list = self.pro_fleet_plan.build_pros(line_counts, self.base_pro)
		fleet = VehicleFleet(
			cab_list,
			pro_list,
			(self.base_cab.init_location_lat, self.base_cab.init_location_lon),
			(self.base_pro.init_location_lat, self.base_pro.init_location_lon) if self.base_pro is not None else None,
			_use_depots=self.use_depots
		)
		
		return FleetAndRequests(self.iteration, copy.deepcopy(self.demand_scenario), fleet)
	
	# ----------------------------------------
	def select_moves(self):
		"""
		Select the next move according to the current search phase:

		grow:
		    add cabs while the objective improves

		shrink:
		    remove one cab at a time while the objective improves

		shrink_pros:
		    deactivate the least-used Pro, then try to recover fleet size with the
		    same grow/shrink moves, keeping the trade-off only if it doesn't lose ground

		Returns
		-------
		str: Name of the move to execute.
		"""
		print(f"Current phase: {self.phase}")
		if self.phase == "grow":

			# If empty cabs occur, remove them all at once and switch to
			# shrinking before growing further (adaptive mode only; static
			# mode never shrinks cabs or Pros, it just stops growing instead).
			if self.search_parameters.search_mode == "adaptive" and self.has_empty_cab(self.current_solution):
				self.phase = "shrink"
				return "remove_empty_cab"

			if self.search_parameters.search_mode == "adaptive":
				return "add_estimated_cabs"
			return "add_cab"

		elif self.phase == "shrink":
			return "remove_one_cab"

		elif self.phase == "shrink_pros":
			# Mid-trial: nested recovery steps reuse the ordinary grow/shrink moves.
			if self.trial_stage == "grow":
				return "add_estimated_cabs"
			if self.trial_stage == "shrink":
				return "remove_one_cab"

			# Remove all completely unused Pros in one shot first (analogous to
			# remove_empty_cab), then probe the remaining poorly-used ones one
			# step at a time.
			if self.has_unused_pro(self.current_solution):
				return "remove_unused_pros"
			return "deactivate_least_used_pro"

		raise RuntimeError("Unknown phase")
		
	# ----------------------------------------
	
	def execute_move(self, move):
		"""
		Apply the chosen move to generate a new solution.
		:param move: The move to apply.
		"""
		working_fleet = copy.deepcopy(self.current_solution.vehicle_fleet)
		print(f"Executing move: {move}")
		if move == "add_cab":
			for _ in range(self.search_parameters.cab_add_step):
				new_cab = copy.deepcopy(self.base_cab)
				new_cab.set_id(self.get_next_cab_id(working_fleet))
				working_fleet.add_cab(new_cab)

		elif move == "add_estimated_cabs":
			estimated_gap = self.estimate_cabs_for_requests(self.current_solution)-len(working_fleet.cabs)
			if self.trial_stage == "grow":
				# cab_add_step's floor is for climbing from a small starting fleet, not for
				# backfilling one Pro's capacity on an already near-optimal one, trust the
				# estimate, floored at 1 so "already enough" still gets a real probe.
				count = max(1, estimated_gap)
			else:
				count = max(self.search_parameters.cab_add_step, estimated_gap)  # add at least cab_add_step cabs, or more if the estimate suggests a larger gap
			if self.search_parameters.budget_eur is not None:
				# The estimate targets full demand coverage only, with no notion of cost, cap it
				# at what's actually affordable instead of growing straight past budget_eur and
				# relying on check_hard_constraints to walk it back afterwards.
				num_pros = len(working_fleet.pros)
				max_affordable_cabs = math.floor(
					(self.search_parameters.budget_eur - num_pros * self.cost_model.pro_price) / self.cost_model.cab_price
				)
				max_gap = max_affordable_cabs - len(working_fleet.cabs)
				count = max(0, min(count, max_gap))
			for _ in range(count):
				new_cab = copy.deepcopy(self.base_cab)
				new_cab.set_id(self.get_next_cab_id(working_fleet))
				working_fleet.add_cab(new_cab)

		elif move == "remove_empty_cab":
			empty_cabs = [cab for cab in working_fleet.cabs if cab.sim_num_requests == 0]
			removable_ids = [cab.id for cab in empty_cabs]

			# never remove all cabs
			max_removals = len(working_fleet.cabs) - 1

			removable_ids = removable_ids[:max_removals]
			for cab_id in removable_ids:
				working_fleet.remove_cab(cab_id)
				self.num_empty_cab_removals += 1

				if ( self.verbose > 0 ):
					print(f"Removed empty cab with ID: {cab_id}")

			if not removable_ids and ( self.verbose > 0 ):
				print("No empty cabs to remove.")

		elif move == "remove_one_cab":
			# overshoot mode floors at trial_start_cabs; below_start mode has no floor here
			at_trial_floor = (
				self.trial_shrink_mode == "overshoot"
				and len(working_fleet.cabs) <= self.trial_start_cabs
			)
			if at_trial_floor:
				self.trial_overshoot_floor_hits += 1
				if self.verbose > 0:
					print("At trial_start_cabs floor, overshoot correction stops here.")
			elif len(working_fleet.cabs) > 1:
				self.num_single_cab_removals += 1
				last_cab_id = max(c.id for c in working_fleet.cabs)
				working_fleet.remove_cab(last_cab_id)
				if ( self.verbose > 0 ):
					print(f"Removed one cab with ID: {last_cab_id}")
			elif len(working_fleet.cabs) == 1 and self.verbose > 0:
				print("Only one cab remaining, cannot remove more.")

		elif move == "activate_next_pro":
			self.activate_next_pro(working_fleet)

		elif move == "deactivate_next_pro":
			for _ in range(max(1, self.pro_remove_step)):
				if not self.deactivate_next_pro(working_fleet):
					break
				self.num_pro_removals += 1

		elif move == "remove_unused_pros":
			unused_ids = {pro.id for pro in working_fleet.pros if pro.sim_cum_chained_cabs == 0}
			if unused_ids:
				working_fleet.set_pros([pro for pro in working_fleet.pros if pro.id not in unused_ids])
				self.num_pro_removals += len(unused_ids)
				if ( self.verbose > 0 ):
					print(f"Removed {len(unused_ids)} unused Pro(s): {sorted(unused_ids)}")
			elif ( self.verbose > 0 ):
				print("No unused Pros to remove.")

		elif move == "deactivate_least_used_pro":
			# Reset first: trial_pro_id must reflect whether *this* call removed a Pro, not
			# linger from an earlier trial (optimize()'s trial-start check reads it).
			self.trial_pro_id = None
			# Served count and best_rank with the Pro still active. The trial-start block
			# below reads these as trial_recovery_target/trial_checkpoint once it confirms a
			# trial is actually starting.
			dd = self.current_solution.demand_dict
			self.pending_pre_removal_served = dd["num_req"] - dd["num_invalid"] - dd["num_sys_reject"] - dd["num_cus_reject"]
			self.pending_pre_removal_best_rank = self.best_rank
			for _ in range(max(1, self.pro_remove_step)):
				removed_id = self.deactivate_least_used_pro(working_fleet, exclude_ids=self.shrink_pros_excluded_ids)
				if removed_id is None:
					break
				self.num_pro_removals += 1
				self.trial_pro_id = removed_id
		else:
			print("Error: Unknown move selected")
			sys.exit(-1)

		return FleetAndRequests( self.iteration, copy.deepcopy(self.current_solution.demand_scenario), working_fleet)
	
	# ----------------------------------------
	
	def evaluate_solution(self, solution, alpha=0.0):
		"""
		Evaluate the quality of a solution.
		Override to suit the problem specifics.
		"""
		
		improvement = 0
		empty_cab = False

		objective_val = 0

		# update solution attributes
		solution.update_attributes()

		# check if any cab is empty, returns True if at least one cab did not serve a request
		empty_cab = self.has_empty_cab(solution)
		if empty_cab:
			print("WARNING: Empty cab(s) detected:")
			for cab in solution.vehicle_fleet.cabs:
				if cab.sim_cum_distance == 0 or cab.sim_num_requests == 0:
					print(f"  Cab ID: {cab.id}, Distance: {cab.sim_cum_distance}m, Requests served: {cab.sim_num_requests}")

		#print("OUTPUT \n distance traveled (in m):")
		#for cab in solution.vehicle_fleet.cabs:
		#	dist = cab.sim_cum_distance
		#	print(" cab", cab.id,":",dist)
		#print(f" {bcolors.BOLD}total: " + str(sum_travel) + f"{bcolors.ENDC}")

		# calculate service rate and fleet efficiency
		#service_rate = (solution.demand_dict["num_req"]-solution.demand_dict["num_invalid"]-solution.demand_dict["num_sys_reject"]-solution.demand_dict["num_cus_reject"])/(solution.demand_dict["num_req"]-solution.demand_dict["num_invalid"])
		#fleet_efficiency = 1.0

		#objective_val = alpha*service_rate + (1-alpha)*fleet_efficiency

		num_cabs = len(solution.vehicle_fleet.cabs)
		num_pros = len(solution.vehicle_fleet.pros)
		total_served = (solution.demand_dict["num_req"] - solution.demand_dict["num_invalid"] - solution.demand_dict["num_sys_reject"] - solution.demand_dict["num_cus_reject"])

		# objective_val is computed further below (see "Objective:" comment there), once the
		# per-Cab energy/trip sums it needs are available.

		#print("service_rate",service_rate)
		#print("fleet_efficiency",fleet_efficiency)
		print("served_requests", total_served)
		print("num_cabs", num_cabs)

		print(" #rejects:", solution.demand_dict["num_cus_reject"])
		print(" tot dist:", solution.fleet_dict["cabs"]["tot_driving_dist"])
		print(" avg util:", round(solution.fleet_dict["cabs"]["avg_utilization"],3))

		num_requests = solution.demand_dict["num_req"]
		num_invalid = solution.demand_dict["num_invalid"]
		num_sys_reject = solution.demand_dict["num_sys_reject"]
		num_cus_reject = solution.demand_dict["num_cus_reject"]
		valid_requests = num_requests - num_invalid
		service_rate = total_served / valid_requests if valid_requests > 0 else 0.0

		customer_distance_m = sum(cab.sim_customer_distance for cab in solution.vehicle_fleet.cabs)
		customer_distance_with_pro_m = sum(cab.sim_customer_distance_with_pro for cab in solution.vehicle_fleet.cabs)
		customer_distance_without_pro_m = sum(cab.sim_customer_distance_without_pro for cab in solution.vehicle_fleet.cabs)
		customer_driving_time_s = sum(cab.sim_customer_driving_time for cab in solution.vehicle_fleet.cabs)
		customer_driving_time_with_pro_s = sum(cab.sim_customer_driving_time_with_pro for cab in solution.vehicle_fleet.cabs)
		customer_driving_time_without_pro_s = sum(cab.sim_customer_driving_time_without_pro for cab in solution.vehicle_fleet.cabs)
		customer_service_time_s = sum(cab.sim_customer_service_time for cab in solution.vehicle_fleet.cabs)
		customer_trip_count = sum(cab.sim_customer_trip_count for cab in solution.vehicle_fleet.cabs)
		empty_distance_m = sum(cab.sim_empty_distance for cab in solution.vehicle_fleet.cabs)
		empty_time_s = sum(cab.sim_empty_time for cab in solution.vehicle_fleet.cabs)
		charging_stationary_access_distance_m = sum(cab.sim_charging_stationary_access_distance for cab in solution.vehicle_fleet.cabs)
		charging_stationary_access_time_s = sum(cab.sim_charging_stationary_access_time for cab in solution.vehicle_fleet.cabs)
		charging_stationary_time_s = sum(cab.sim_charging_stationary_time for cab in solution.vehicle_fleet.cabs)
		charging_stationary_energy_wh = sum(cab.sim_charging_stationary_energy for cab in solution.vehicle_fleet.cabs)
		charging_stationary_event_count = sum(cab.sim_charging_stationary_event_count for cab in solution.vehicle_fleet.cabs)
		charging_pro_energy_wh = sum(cab.sim_charging_pro_energy for cab in solution.vehicle_fleet.cabs)
		consumed_energy_wh = solution.fleet_dict["cabs"]["tot_energy_cons"]

		# needed here already for avg_in_vehicle_time_s, ahead of the other served_customer_requests
		# derived sums below
		served_customer_requests = [
			req for req in solution.demand_scenario.requests
			if req.sim_custom_reject is False
		]
		customer_in_vehicle_time_s = sum(
			float(getattr(req, "sim_customer_in_vehicle_time", 0.0) or 0.0)
			for req in served_customer_requests
		)

		grid_energy_kwh, pro_energy_kwh = self.cost_model.split_grid_and_pro_energy_kwh(
			consumed_energy_wh / 1000.0, charging_stationary_energy_wh / 1000.0, charging_pro_energy_wh / 1000.0
		)
		daily_fleet_cost_eur = self.cost_model.daily_fleet_cost(num_cabs, num_pros, grid_energy_kwh, pro_energy_kwh)
		# A large finite sentinel, not float("inf"): a -inf/-inf objective_val comparison would
		# be nan in Python, which would silently defeat every stagnation check below if a
		# candidate ever served zero trips.
		avg_cost_per_trip_eur = daily_fleet_cost_eur / customer_trip_count if customer_trip_count > 0 else 1e12

		# Revenue: fare per served request, summed. Each request has a direct pair (distance,
		# time of a plain point-to-point ride) and an actual pair (what was really driven,
		# convoy detour included). Fare each pair on its own and charge the cheaper of the two,
		# so a convoy detour never costs the customer more than a direct ride would have. Never
		# mixes distance from one pair with time from the other, only whole, corresponding
		# pairs are compared.
		total_revenue_eur = 0.0
		for req in served_customer_requests:
			direct_distance = getattr(req, "sim_customer_direct_distance", None)
			direct_time = getattr(req, "sim_customer_direct_time", None)
			actual_distance = getattr(req, "sim_customer_distance", None)
			actual_time = getattr(req, "sim_customer_in_vehicle_time", None)
			fares = []
			if direct_distance is not None and direct_time is not None:
				fares.append(self.cost_model.trip_fare(direct_distance, direct_time))
			if actual_distance is not None and actual_time is not None:
				fares.append(self.cost_model.trip_fare(actual_distance, actual_time))
			total_revenue_eur += min(fares) if fares else 0.0
		avg_profit_per_trip_eur = (
			(total_revenue_eur - daily_fleet_cost_eur) / customer_trip_count if customer_trip_count > 0 else -1e12
		)
		avg_in_vehicle_time_s = (
			customer_in_vehicle_time_s / customer_trip_count if customer_trip_count > 0 else 1e12
		)

		# selected via --objective_terms (1 or 2 names from OBJECTIVE_METRICS) and, with 2
		# terms, --objective_weight (alpha=1: pure first term, alpha=0: pure second). Hard
		# constraints (budget_eur/service_level_min) are gated separately in _solution_rank(),
		# not here.
		kpis = {
			"total_served": total_served,
			"avg_cost_per_trip_eur": avg_cost_per_trip_eur,
			"avg_profit_per_trip_eur": avg_profit_per_trip_eur,
			"avg_in_vehicle_time_s": avg_in_vehicle_time_s,
		}
		objective_val = compute_objective(
			kpis, num_requests, self.search_parameters.objective_terms, self.search_parameters.objective_weight
		)
		# fleet-size tiebreaker, unweighted, lets shrink see a useless vehicle's removal as imp>0
		objective_val += 1.0 / (num_cabs + num_pros + 1)
		print("avg_cost_per_trip_eur", round(avg_cost_per_trip_eur, 4))
		print("avg_profit_per_trip_eur", round(avg_profit_per_trip_eur, 4))
		print("avg_in_vehicle_time_s", round(avg_in_vehicle_time_s, 4))
		print("objective_val", objective_val)

		total_distance_m = solution.fleet_dict["cabs"]["tot_driving_dist"]
		customer_in_vehicle_wait_s = sum(
			float(getattr(req, "sim_customer_in_vehicle_wait_time", 0.0) or 0.0)
			for req in served_customer_requests
		)
		customer_excess_travel_s = sum(
			float(getattr(req, "sim_customer_excess_travel_time", 0.0) or 0.0)
			for req in served_customer_requests
		)
		customer_max_in_vehicle_wait_s = max(
			[
				float(getattr(req, "sim_customer_in_vehicle_wait_time", 0.0) or 0.0)
				for req in served_customer_requests
			],
			default=0.0,
		)

		experiment_kpis = {
			"num_requests": num_requests,
			"num_invalid": num_invalid,
			"num_sys_reject": num_sys_reject,
			"num_cus_reject": num_cus_reject,
			"served_requests": total_served,
			"service_rate": service_rate,
			"tot_wait_time_s": solution.demand_dict["tot_wait_time"],
			"avg_wait_time_s": solution.demand_dict["avg_wait_time"],
			"num_cabs": num_cabs,
			"num_pros": solution.fleet_dict["pros"]["num_pros"],
			"objective_val": objective_val,
			"cab_tot_driving_dist_m": total_distance_m,
			"cab_tot_driving_time_s": solution.fleet_dict["cabs"]["tot_driving_time"],
			"cab_tot_service_time_s": solution.fleet_dict["cabs"]["tot_service_time"],
			"cab_tot_schedule_time_s": solution.fleet_dict["cabs"]["tot_schedule_time"],
			"cab_tot_energy_cons_wh": solution.fleet_dict["cabs"]["tot_energy_cons"],
			"cab_avg_utilization": solution.fleet_dict["cabs"]["avg_utilization"],
			"customer_trip_count": customer_trip_count,
			"customer_distance_m": customer_distance_m,
			"customer_distance_with_pro_m": customer_distance_with_pro_m,
			"customer_distance_without_pro_m": customer_distance_without_pro_m,
			"customer_avg_distance_m": customer_distance_m / customer_trip_count if customer_trip_count > 0 else 0.0,
			"customer_driving_time_s": customer_driving_time_s,
			"customer_driving_time_with_pro_s": customer_driving_time_with_pro_s,
			"customer_driving_time_without_pro_s": customer_driving_time_without_pro_s,
			"customer_avg_driving_time_s": customer_driving_time_s / customer_trip_count if customer_trip_count > 0 else 0.0,
			"customer_in_vehicle_time_s": customer_in_vehicle_time_s,
			"customer_in_vehicle_wait_s": customer_in_vehicle_wait_s,
			"customer_avg_in_vehicle_wait_s": customer_in_vehicle_wait_s / customer_trip_count if customer_trip_count > 0 else 0.0,
			"customer_max_in_vehicle_wait_s": customer_max_in_vehicle_wait_s,
			"customer_excess_travel_s": customer_excess_travel_s,
			"customer_avg_excess_travel_s": customer_excess_travel_s / customer_trip_count if customer_trip_count > 0 else 0.0,
			"customer_service_time_s": customer_service_time_s,
			"empty_distance_m": empty_distance_m,
			"empty_time_s": empty_time_s,
			"empty_distance_share": empty_distance_m / total_distance_m if total_distance_m > 0 else 0.0,
			"charging_stationary_access_distance_m": charging_stationary_access_distance_m,
			"charging_stationary_access_time_s": charging_stationary_access_time_s,
			"charging_stationary_time_s": charging_stationary_time_s,
			"charging_stationary_energy_wh": charging_stationary_energy_wh,
			"charging_stationary_event_count": charging_stationary_event_count,
			"charging_pro_energy_wh": charging_pro_energy_wh,
			"grid_energy_kwh": grid_energy_kwh,
			"pro_energy_kwh": pro_energy_kwh,
			"daily_fleet_cost_eur": daily_fleet_cost_eur,
			"avg_cost_per_trip_eur": avg_cost_per_trip_eur,
			"total_revenue_eur": total_revenue_eur,
			"avg_profit_per_trip_eur": avg_profit_per_trip_eur,
			"avg_in_vehicle_time_s": avg_in_vehicle_time_s,
		}
		print_experiment_kpis = True
		if print_experiment_kpis:
			for key, value in experiment_kpis.items():
				print("experiment_kpi", key, value, sep="\t")


		# feasible beats infeasible regardless of objective_val; see _solution_rank()
		rank = self._solution_rank(solution, objective_val)
		if rank > self.best_rank:
			improvement = 1.0
			self.best_solution = copy.deepcopy(solution)
			self.best_rank = rank
		elif rank < self.best_rank:
			improvement = -1.0
		else:
			improvement = 0.0

		print(improvement)

		# rank is also returned (not just used internally) for the shrink_pros trial
		# mechanism, which needs it to seed/advance its own bubble comparison, see
		# optimize()'s shrink_pros block.
		return improvement, empty_cab, rank

	# ----------------------------------------

	def estimate_cabs_for_requests(self, solution):
		"""
		Estimate the total number of cabs needed for the request set.

		During a shrink_pros trial's nested grow, targets trial_recovery_target (the
		served count right before this specific Pro was removed) instead of raw
		num_req, since the trial only needs to recover what this removal actually cost,
		not chase full demand coverage the top-level search may not even be
		pursuing itself (and near a real service-rate plateau, raw num_req wildly
		overestimates what's reachable at all, see the shrink_pros trial memory).
		"""
		total_served = (solution.demand_dict["num_req"] - solution.demand_dict["num_invalid"] - solution.demand_dict["num_sys_reject"] - solution.demand_dict["num_cus_reject"])
		current_cabs = len(solution.vehicle_fleet.cabs)
		if ( total_served <= 0 or current_cabs <= 0 ):
			return current_cabs + 1
		served_per_cab = total_served / current_cabs
		if ( served_per_cab <= 0 ):
			return current_cabs + 1
		target = self.trial_recovery_target if self.trial_stage == "grow" else solution.demand_dict["num_req"]
		needed_total = math.ceil(target / served_per_cab)
		return max(current_cabs + 1, needed_total)

	# ----------------------------------------

	def has_empty_cab(self, solution):
		"""Return True if any cab did not serve a request."""
		return any(
			(cab.sim_num_requests == 0)
			for cab in solution.vehicle_fleet.cabs
		)

	# ----------------------------------------

	def has_unused_pro(self, solution):
		"""Return True if any active Pro served no convoy (zero chained cabs)."""
		return any(
			(pro.sim_cum_chained_cabs == 0)
			for pro in solution.vehicle_fleet.pros
		)

	# ----------------------------------------

	def _fleet_key(self, solution):
		"""
		Identity of a fleet composition for visited_fleets: cab count plus the exact set of
		active Pro ids (not just the Pro count, since Pro removal is permanent, so two runs with the
		same count could in principle carry different active ids). Two solutions with the same
		key are guaranteed to simulate identically.
		"""
		return (len(solution.vehicle_fleet.cabs), frozenset(pro.id for pro in solution.vehicle_fleet.pros))

	# ----------------------------------------

	def check_hard_constraints(self, solution):
		"""
		Evaluate active hard constraints against the just-simulated solution and return a phase
		to force for the next iteration, or None if none fire. Runs once per simulated solution
		(including the initial one), independent of the objective/stagnation-driven phase
		transitions in optimize(). Those still decide everything within a phase, this only
		steers which phase the search is allowed to be in.

		budget_eur is only checked in "grow" (the only phase that can raise fleet_price_eur), and
		forces "shrink" (not "shrink_pros" directly) so the existing shrink -> shrink_pros
		stagnation progression gets to use BOTH cost levers, cabs then Pros, in the order it
		already uses on its own, not just Pros. Not re-checked once already in shrink/
		shrink_pros: forcing the same phase again every iteration would revert to best_solution
		on every single iteration of an already-correctly-shrinking phase, discarding real
		progress instead of letting the existing imp<=0 stagnation checks drive it.

		service_level_min is only checked in "shrink"/"shrink_pros" (the only phases that can
		lower service_rate), and only once budget_eur (if any) is no longer violated. An
		open budget violation takes priority, since forcing "grow" while still over budget would
		just walk straight back into the budget wall (observed live: forcing grow after removing
		only one of two Pros, before shrink_pros had a chance to remove the second).

		Suppressed entirely while a shrink_pros trial is in progress (self.trial_stage is not
		None): a trial's nested grow/shrink steps reuse the "grow"/"shrink" moves but are not
		top-level phases, and forcing a top-level transition mid-trial would derail its own
		bubble-based accept/stop logic (see optimize()). The gate resumes the moment a trial
		concludes, on whatever solution it ends with.
		"""
		if self.trial_stage is not None:
			return None

		num_cabs = len(solution.vehicle_fleet.cabs)
		num_pros = len(solution.vehicle_fleet.pros)
		budget_violated = (
			self.search_parameters.budget_eur is not None
			and self.cost_model.fleet_price_eur(num_cabs, num_pros) > self.search_parameters.budget_eur
		)

		if budget_violated and self.phase == "grow":
			return "shrink"

		if self.search_parameters.service_level_min is not None and self.phase in ("shrink", "shrink_pros") and not budget_violated:
			d = solution.demand_dict
			valid_requests = d["num_req"] - d["num_invalid"]
			total_served = valid_requests - d["num_sys_reject"] - d["num_cus_reject"]
			service_rate = total_served / valid_requests if valid_requests > 0 else 0.0
			if service_rate < self.search_parameters.service_level_min:
				return "grow"

		return None

	# ----------------------------------------

	def _constraint_violations(self, solution):
		"""
		Return a list of human-readable violation messages for the active hard constraints
		against the given (already-simulated) solution. Empty list means feasible.
		"""
		violations = []
		num_cabs = len(solution.vehicle_fleet.cabs)
		num_pros = len(solution.vehicle_fleet.pros)

		if self.search_parameters.budget_eur is not None:
			fleet_price = self.cost_model.fleet_price_eur(num_cabs, num_pros)
			if fleet_price > self.search_parameters.budget_eur:
				violations.append(
					f"budget_eur exceeded: fleet_price_eur={fleet_price:.2f} > budget_eur={self.search_parameters.budget_eur:.2f}"
				)

		if self.search_parameters.service_level_min is not None:
			d = solution.demand_dict
			valid_requests = d["num_req"] - d["num_invalid"]
			total_served = valid_requests - d["num_sys_reject"] - d["num_cus_reject"]
			service_rate = total_served / valid_requests if valid_requests > 0 else 0.0
			if service_rate < self.search_parameters.service_level_min:
				violations.append(
					f"service_level_min not met: service_rate={service_rate:.4f} < service_level_min={self.search_parameters.service_level_min:.4f}"
				)

		return violations

	# ----------------------------------------

	def _constraint_violation_magnitude(self, solution) -> float:
		"""
		Sum of normalized overages for the active hard constraints, 0.0 exactly when the
		solution is fully feasible (see _constraint_violations() for the human-readable form of
		the same checks). Used by _solution_rank() to compare two infeasible solutions: smaller
		is closer to feasible. Each term is a unitless ratio (violation as a fraction of the
		limit) so terms for different constraint types can be summed meaningfully.

		service_level_min<=0 is treated as inactive, same as None, since a normalized ratio would
		divide by the floor itself, which is 0 in that case.
		"""
		num_cabs = len(solution.vehicle_fleet.cabs)
		num_pros = len(solution.vehicle_fleet.pros)
		total = 0.0

		if self.search_parameters.budget_eur is not None:
			fleet_price = self.cost_model.fleet_price_eur(num_cabs, num_pros)
			total += max(0.0, fleet_price / self.search_parameters.budget_eur - 1.0)

		if self.search_parameters.service_level_min is not None and self.search_parameters.service_level_min > 0:
			d = solution.demand_dict
			valid_requests = d["num_req"] - d["num_invalid"]
			served = valid_requests - d["num_sys_reject"] - d["num_cus_reject"]
			service_rate = served / valid_requests if valid_requests > 0 else 0.0
			total += max(0.0, 1.0 - service_rate / self.search_parameters.service_level_min)

		return total

	# ----------------------------------------

	def _solution_rank(self, solution, objective_val: float) -> tuple:
		"""
		Comparable (is_feasible, score) rank for accept/stagnation decisions, replacing a plain
		objective_val comparison once hard constraints are active. Python's tuple comparison
		directly implements the intended priority:
		  - a feasible solution always outranks an infeasible one, regardless of score, since
		    (True, ...) > (False, ...) unconditionally in a tuple comparison;
		  - two feasible solutions are compared by objective_val (today's rule, unchanged);
		  - two infeasible solutions are compared by closeness to feasible (negative violation
		    magnitude, so smaller violation, meaning closer to feasible, wins).

		budget_eur=None and service_level_min=None (the default, every existing caller) means
		_constraint_violation_magnitude() is always 0.0, so every solution is feasible and this
		always reduces to (True, objective_val), i.e. exactly the old plain objective_val
		comparison, unchanged.
		"""
		violation = self._constraint_violation_magnitude(solution)
		is_feasible = violation == 0.0
		return (is_feasible, objective_val if is_feasible else -violation)

	# ----------------------------------------

	def _rank_shortfall_within_tolerance(self, candidate_rank, reference_rank, tolerance: float) -> bool:
		"""
		True if candidate_rank falls strictly, but only slightly, short of reference_rank: worse
		by more than 0 but no more than tolerance (a fraction of reference_rank's own score
		magnitude). A tie is never "within tolerance", there's nothing to tolerate. Used two
		ways by the shrink_pros
		trial mechanism (see optimize()): to let an ordinary step that's slightly worse than
		the trial's best-so-far continue instead of ending the stage outright, and, with the
		arguments swapped, to judge whether a grow stage's peak improved meaningfully over the
		bare removal it started from (bare removal as candidate, peak as reference: if the bare
		removal is itself within tolerance of the peak, growth found nothing meaningful).

		Never tolerates a feasibility regression: only compares scores when both ranks share
		the same feasibility, regardless of tolerance.
		"""
		if tolerance <= 0:
			return False
		cand_feasible, cand_score = candidate_rank
		ref_feasible, ref_score = reference_rank
		if cand_feasible != ref_feasible:
			return False
		return ref_score > cand_score > ref_score - tolerance * abs(ref_score)

	# ----------------------------------------

	def get_next_cab_id(self, fleet):
		"""
		Return next unique cab id.
		"""
		if not fleet.cabs:
			return 1
		return max(c.id for c in fleet.cabs) + 1


	def run_operational_planning(self, solution, iteration):
		if self.use_sim == "rw":
			cache_required = iteration < self.resume_from
			rw_cache = "auto" if cache_required else self.rw_cache
			return self.operational_planning.optimize(
				solution,
				iteration,
				rw_cache=rw_cache,
				cache_required=cache_required,
				max_retries=self.rw_retries,
			)

		return self.operational_planning.optimize(solution, iteration)

	# ----------------------------------------

	def activate_next_pro(self, fleet=None):
		"""
		Activate one additional Pro on the line that currently has the fewest
		active Pros (ties broken by line order from find_pro_line_order), as long
		as that line still has a free timetable slot.

		The full active Pro list is rebuilt via ProFleetPlan.set_line_count and
		assigned back to the fleet. Returns True if a Pro was activated, False if
		there is no Pro fleet plan or every line is already at its maximum count.
		"""
		if self.base_pro is None or not self.pro_fleet_plan.lines:
			return False

		# Current active Pro count per line.
		counts = {
			line.id: self.pro_fleet_plan.count_on_line(fleet.pros, line.id)
			for line in self.find_pro_line_order()
		}

		# Pick the line with the fewest active Pros that still has a free slot.
		# Iterating in line order makes ties deterministic.
		target_line = None
		for line in self.find_pro_line_order():
			if counts[line.id] >= line.max_count():
				continue
			if target_line is None or counts[line.id] < counts[target_line.id]:
				target_line = line

		if target_line is None:
			if self.verbose > 0:
				print("activate_next_pro: all Pro lines are at maximum count.")
			return False

		new_count = counts[target_line.id] + 1
		new_pros = self.pro_fleet_plan.set_line_count(
			fleet.pros, target_line.id, new_count, self.base_pro
		)
		fleet.set_pros(new_pros)

		if self.verbose > 0:
			print(f"activate_next_pro: line {target_line.id} -> {new_count} active Pro(s)")
		return True

	# ----------------------------------------

	def deactivate_next_pro(self, fleet=None):
		"""
		Deactivate one Pro on the line that currently has the most active Pros
		(ties broken by line order from find_pro_line_order), as long as that line
		has at least one active Pro. A line may be reduced down to zero.

		The full active Pro list is rebuilt via ProFleetPlan.set_line_count and
		assigned back to the fleet. Returns True if a Pro was deactivated, False if
		there is no Pro fleet plan or every line is already empty.

		Note: reducing a line's count respaces its remaining slots via
		slots_for_count, so which concrete Pros stay active may change; only the
		count is guaranteed to drop by one.
		"""
		if self.base_pro is None or not self.pro_fleet_plan.lines:
			return False

		# Current active Pro count per line.
		counts = {
			line.id: self.pro_fleet_plan.count_on_line(fleet.pros, line.id)
			for line in self.find_pro_line_order()
		}

		# Pick the line with the most active Pros that still has at least one.
		# Iterating in line order makes ties deterministic.
		target_line = None
		for line in self.find_pro_line_order():
			if counts[line.id] <= 0:
				continue
			if target_line is None or counts[line.id] > counts[target_line.id]:
				target_line = line

		if target_line is None:
			if self.verbose > 0:
				print("deactivate_next_pro: no active Pros to deactivate.")
			return False

		new_count = counts[target_line.id] - 1
		new_pros = self.pro_fleet_plan.set_line_count(
			fleet.pros, target_line.id, new_count, self.base_pro
		)
		fleet.set_pros(new_pros)

		if self.verbose > 0:
			print(f"deactivate_next_pro: line {target_line.id} -> {new_count} active Pro(s)")
		return True

	# ----------------------------------------

	def deactivate_least_used_pro(self, fleet=None, exclude_ids=None):
		"""
		Deactivate the single least-used active Pro, judged by its simulation KPIs:
		fewest chained cabs first, then fewest convoy trips, then least distance
		(ties by id for determinism). Unused Pros (zero chained cabs) are therefore
		removed before poorly-used ones.

		exclude_ids: Pro ids to skip, used by the shrink_pros trial mechanism (see
		optimize()) to avoid re-trying a Pro that already failed its trial this phase.

		Unlike the line-balanced deactivate_next_pro, this removes one concrete Pro
		by filtering the active list (set_line_count cannot target a specific Pro,
		as it respaces slots). The KPIs come from the most recent evaluation of this
		solution and are refreshed on the next run. Returns the removed Pro's id, or
		None if there was no eligible (non-excluded) active Pro.
		"""
		if fleet is None or not fleet.pros:
			return None

		candidates = [p for p in fleet.pros if exclude_ids is None or p.id not in exclude_ids]
		if not candidates:
			return None

		worst = min(
			candidates,
			key=lambda p: (
				p.sim_cum_chained_cabs,
				p.sim_total_trips_with_pro,
				p.sim_cum_distance,
				p.id,
			),
		)
		fleet.set_pros([pro for pro in fleet.pros if pro.id != worst.id])

		if self.verbose > 0:
			print(
				f"deactivate_least_used_pro: removed Pro {worst.id} "
				f"(chained_cabs={worst.sim_cum_chained_cabs}, "
				f"trips={worst.sim_total_trips_with_pro})"
			)
		return worst.id

	# ----------------------------------------

	def _conclude_trial(self):
		"""
		End the current shrink_pros trial: revert to best_solution, clear trial-scoped
		state, update exclusion/tries bookkeeping. succeeded compares best_rank against
		trial_checkpoint, captured from right before this Pro was touched at all, so the
		plain removal itself counts as part of what the trial can succeed on, not just
		whatever the nested grow/shrink found on top of it.
		"""
		succeeded = self.best_rank > self.trial_checkpoint
		self.current_solution = copy.deepcopy(self.best_solution)
		self.trial_stage = None
		self.trial_bubble_rank = None
		self.trial_peak_solution = None
		self.trial_start_cabs = None
		self.trial_recovery_target = None
		self.trial_had_multi_cab_jump = False
		self.trial_shrink_mode = None
		self.trial_bare_removal_rank = None
		self.trial_tolerance_streak = 0
		if succeeded:
			self.shrink_pros_tries = 0
			if self.search_parameters.shrink_pros_allow_retry:
				self.shrink_pros_excluded_ids = set()
		else:
			self.shrink_pros_excluded_ids.add(self.trial_pro_id)

	# ----------------------------------------

	def optimize(self):
		'''
		Optimize szenario
		'''
		if ( self.verbose > 0 ):
			print("# call FleetPlanning.optimize()")

		# budget_eur below the smallest possible fleet's price (1 cab, 0 pros) is structurally
		# unsatisfiable, catch it before running a single simulation.
		if self.search_parameters.budget_eur is not None:
			min_fleet_price = self.cost_model.fleet_price_eur(1, 0)
			if self.search_parameters.budget_eur < min_fleet_price:
				print(
					f"{bcolors.WARNING}budget_eur={self.search_parameters.budget_eur:.2f} is below "
					f"the price of the smallest possible fleet ({min_fleet_price:.2f}, 1 cab/0 pros) "
					f"- no feasible solution exists. Stopping without running a simulation.{bcolors.ENDC}"
				)
				return

		time_it = time.perf_counter()

		# -----------------------------------------------

		print(f"{bcolors.OKBLUE}=== initial solution ==={bcolors.ENDC}")
		# initial solution
		self.current_solution = self.initial_solution() # create first FleetAndRequests object [with empty KPIs]
		print("INPUT \n #cabs: {} \n #pros: {}".format(len(self.current_solution.vehicle_fleet.cabs),len(self.current_solution.vehicle_fleet.pros)))

		time_o = time.perf_counter()

		fleet_key = self._fleet_key(self.current_solution)
		cached = self.visited_fleets.get(fleet_key)
		if cached is not None:
			if self.verbose > 0:
				print(f"Reusing cached simulation for fleet {fleet_key}")
			self.current_solution = copy.deepcopy(cached)
		else:
			print(f"START {self.use_sim.upper()} SIMULATION")
			self.run_operational_planning(self.current_solution,0)	# now KPIs have been filled
			self.visited_fleets[fleet_key] = copy.deepcopy(self.current_solution)
		time_o = time.perf_counter() - time_o
		self.time_oracle += time_o

		print("OUTPUT")
		imp, empty_cab, _ = self.evaluate_solution(self.current_solution) 	# placeholder for objective function evaluation -> inital solution is also evaluated to have a baseline for improvement
		if ( self.verbose > 0 ):
			if ( imp > 0 ):
				print("new improving solution found")


		self.solution_pool.append(copy.deepcopy(self.current_solution)) 	# is deepcopy actually necessary??

		# only budget_eur can fire here (service_level_min only watches shrink/shrink_pros);
		# only meaningful in adaptive mode, the only mode with a shrink phase to switch into
		if self.search_parameters.search_mode == "adaptive":
			forced_phase = self.check_hard_constraints(self.current_solution)
			if forced_phase == "shrink":
				self.phase = "shrink"

		time_it = time.perf_counter() - time_it
		self.time += time_it
		
		if ( self.verbose > 0 ):
			print(f"# time for initial solution {self.iteration}: {time_it:.3f} (oracle: {time_o:.3f})")
		if WRITE_STATUS_LOG:
			(self.BASE_DIR / "debug").mkdir(exist_ok=True)
			with open(self.BASE_DIR / "debug" / "fp.log", "a") as f:
				f.write(f"# time for iteration {self.iteration}: {time_it:.3f}\n")
		self.iteration += 1
		# -----------------------------------------------
		
		'''
		MAIN LOOP
		'''
		#empty_cab = False

		# time_limit is checked between iterations against self.time (wall-clock spent so far,
		# accumulated below); a run stops once either bound is reached, whichever first.
		while ( self.iteration < self.search_parameters.iter_limit and self.time < self.search_parameters.time_limit ):

			time_it = time.perf_counter()

			print(f"{bcolors.OKBLUE}=== iteration {self.iteration} ==={bcolors.ENDC}")
			move = self.select_moves()

			candidate_solution = self.execute_move(move)
			print("INPUT \n #cabs: {} \n #pros: {}".format(len(candidate_solution.vehicle_fleet.cabs),len(candidate_solution.vehicle_fleet.pros)))

			# deactivate_least_used_pro found no eligible Pro left to remove (all gone, or
			# all excluded), same outcome as this phase's own imp<=0 stagnation branch
			# below, just without spending an iteration/pool entry simulating a no-op fleet.
			if move == "deactivate_least_used_pro" and len(candidate_solution.vehicle_fleet.pros) == len(self.current_solution.vehicle_fleet.pros):
				if self.best_solution is not None:
					self.current_solution = copy.deepcopy(self.best_solution)
				break

			# add_estimated_cabs adding nothing = cabs capped at their ceiling (budget_eur or
			# the estimate itself); if service_level_min is still unmet there, no further move
			# can reach it, stop instead of cycling grow<->shrink for the rest of iter_limit.
			# Real top-level grow only, a trial's nested grow hitting its cap is handled by
			# its own bubble logic below, not a global stop condition.
			if move == "add_estimated_cabs" and self.trial_stage is None and len(candidate_solution.vehicle_fleet.cabs) == len(self.current_solution.vehicle_fleet.cabs):
				service_violations = [v for v in self._constraint_violations(self.current_solution) if v.startswith("service_level_min")]
				if service_violations:
					print(f"{bcolors.WARNING}Growth exhausted (budget_eur ceiling or demand estimate) while still below service_level_min, stopping.{bcolors.ENDC}")
					print(f"{bcolors.WARNING}  - {service_violations[0]}{bcolors.ENDC}")
					break

			time_o = time.perf_counter()

			fleet_key = self._fleet_key(candidate_solution)
			cached = self.visited_fleets.get(fleet_key)
			if cached is not None:
				if self.verbose > 0:
					print(f"Reusing cached simulation for fleet {fleet_key}")
				self.current_solution = copy.deepcopy(cached)
			else:
				self.current_solution = candidate_solution
				print(f"START {self.use_sim.upper()} SIMULATION")
				self.run_operational_planning(self.current_solution, self.iteration)
				self.visited_fleets[fleet_key] = copy.deepcopy(self.current_solution)
			time_o = time.perf_counter() - time_o
			self.time_oracle += time_o

			print("OUTPUT")
			imp, empty_cab, rank = self.evaluate_solution(self.current_solution)
			if imp > 0:
				self.stagnation_tolerance_streak = 0
			# Pool the just-evaluated solution before any rollback, so the pool holds
			# the complete search trajectory (every evaluated state, not only the
			# accepted ones). Result selection uses best_solution, not the pool.
			self.solution_pool.append(copy.deepcopy(self.current_solution))

			# Every iteration passes through here before any of the continues below (trial/
			# gate/stagnation): the old bottom-of-loop version of this block got skipped by
			# all of them.
			time_it = time.perf_counter() - time_it
			self.time += time_it
			if ( self.verbose > 0 ):
				print(f"# time for iteration {self.iteration}: {time_it:.3f} (oracle: {time_o:.3f})")
			if WRITE_STATUS_LOG:
				(self.BASE_DIR / "debug").mkdir(exist_ok=True)
				with open(self.BASE_DIR / "debug" / "fp.log", "a") as f:
					f.write(f"# time for iteration {self.iteration}: {time_it:.3f}\n")

			# trial in progress: bubble comparison (not global best_rank) drives whether it
			# keeps taking steps. rank > trial_bubble_rank keeps going (updates
			# trial_peak_solution); otherwise nested grow hands off to nested shrink
			# (reverting to trial_peak_solution first), which concludes the trial.
			if self.trial_stage is not None:
				if rank > self.trial_bubble_rank:
					if self.trial_stage == "grow":
						jump = len(self.current_solution.vehicle_fleet.cabs) - len(self.trial_peak_solution.vehicle_fleet.cabs)
						if jump > 1:
							self.trial_had_multi_cab_jump = True
					self.trial_bubble_rank = rank
					self.trial_peak_solution = copy.deepcopy(self.current_solution)
					self.trial_tolerance_streak = 0
				elif (
					self.trial_tolerance_streak < self.search_parameters.stagnation_tolerance_patience
					and self._rank_shortfall_within_tolerance(
						rank, self.trial_bubble_rank, self.search_parameters.stagnation_tolerance
					)
				):
					# Within the tolerated shortfall of the trial's best-so-far, and the
					# patience budget for consecutive non-improving steps isn't exhausted -
					# keep stepping in the same direction without moving the peak, so a
					# shallow dip doesn't end the stage before a later step recovers past
					# it. Peak staying fixed (not sliding to this step) keeps the tolerance
					# from compounding across a run of small drops; patience bounds a long
					# plateau to a fixed number of extra iterations either way.
					self.trial_tolerance_streak += 1
					self.iteration += 1
					continue
				elif self.trial_stage == "grow":
					# grow concluded: below_start only if it found nothing meaningful,
					# overshoot correction only if it found something via a jump >1. "Nothing
					# meaningful" is tolerance-aware: a peak that is a genuine improvement but
					# only within tolerance of the bare removal counts the same as never
					# having grown at all. At tolerance=0 the second clause is always True
					# (see _rank_shortfall_within_tolerance's tolerance<=0 short-circuit), so
					# this reduces to the plain rank comparison, identical to the old
					# cabs-count-based grew_at_all, since a peak update during grow only ever
					# happens on a genuine rank improvement, which only ever happens via a
					# cab-adding step.
					grew_meaningfully = self.trial_bubble_rank > self.trial_bare_removal_rank and not self._rank_shortfall_within_tolerance(
						self.trial_bare_removal_rank, self.trial_bubble_rank, self.search_parameters.stagnation_tolerance
					)
					if not grew_meaningfully and self.search_parameters.shrink_pros_trial_probe_below_start:
						self.trial_stage = "shrink"
						self.trial_shrink_mode = "below_start"
						self.current_solution = copy.deepcopy(self.trial_peak_solution)
						self.trial_tolerance_streak = 0
					elif grew_meaningfully and self.trial_had_multi_cab_jump and self.search_parameters.shrink_pros_trial_overshoot_correction:
						self.trial_stage = "shrink"
						self.trial_shrink_mode = "overshoot"
						self.current_solution = copy.deepcopy(self.trial_peak_solution)
						self.trial_tolerance_streak = 0
					else:
						self._conclude_trial()
				else:
					self._conclude_trial()
				self.iteration += 1
				continue

			# trial start: a Pro was just removed (not mid-trial). Decide whether to attempt
			# nested grow+shrink recovery, or defer below as a plain removal. Not while
			# budget_eur is violated (plain removal should resolve that, growth would just be
			# capped to near-nothing). Bounded by shrink_pros_max_tries.
			if self.phase == "shrink_pros" and move == "deactivate_least_used_pro" and self.trial_pro_id is not None:
				budget_violated = (
					self.search_parameters.budget_eur is not None
					and self.cost_model.fleet_price_eur(
						len(self.current_solution.vehicle_fleet.cabs), len(self.current_solution.vehicle_fleet.pros)
					) > self.search_parameters.budget_eur
				)
				if not budget_violated and self.shrink_pros_tries < self.search_parameters.shrink_pros_max_tries:
					self.trial_stage = "grow"
					self.trial_bubble_rank = rank
					self.trial_peak_solution = copy.deepcopy(self.current_solution)
					self.trial_start_cabs = len(self.current_solution.vehicle_fleet.cabs)
					self.trial_bare_removal_rank = rank
					self.trial_tolerance_streak = 0
					self.trial_checkpoint = self.pending_pre_removal_best_rank
					self.trial_recovery_target = self.pending_pre_removal_served
					self.shrink_pros_tries += 1
					self.iteration += 1
					continue

			# empty cabs = overshot. Adaptive: keep it so select_moves bulk-removes them and
			# switches to shrink next iteration. Static never shrinks: revert and stop.
			if self.phase == "grow" and empty_cab:
				if self.search_parameters.search_mode == "adaptive":
					self.iteration += 1
					continue
				if self.best_solution is not None:
					self.current_solution = copy.deepcopy(self.best_solution)
				break
			# Genuine stagnation without empty cabs: fall back to best and shrink
			# (adaptive mode) or stop growing (static mode: fixed Pro count,
			# cabs only ever grow).
			if self.phase == "grow" and imp <= 0:
				if (
					self.stagnation_tolerance_streak < self.search_parameters.stagnation_tolerance_patience
					and self._rank_shortfall_within_tolerance(rank, self.best_rank, self.search_parameters.stagnation_tolerance)
				):
					self.stagnation_tolerance_streak += 1
					self.iteration += 1
					continue
				if self.best_solution is not None:
					self.current_solution = copy.deepcopy(self.best_solution)
				if self.search_parameters.search_mode == "adaptive":
					self.phase = "shrink"
					self.iteration += 1
					continue
				break
			if self.phase == "shrink" and imp <= 0:
				if (
					self.stagnation_tolerance_streak < self.search_parameters.stagnation_tolerance_patience
					and self._rank_shortfall_within_tolerance(rank, self.best_rank, self.search_parameters.stagnation_tolerance)
				):
					self.stagnation_tolerance_streak += 1
					self.iteration += 1
					continue
				# Cab shrink exhausted: revert to best, then shrink Pro intensity.
				if self.best_solution is not None:
					self.current_solution = copy.deepcopy(self.best_solution)
				self.phase = "shrink_pros"
				# Fresh shrink_pros entry: any earlier trial exclusions/tries were scoped
				# to a fleet composition we've since left behind (e.g. after a hard-
				# constraint gate forced a real regrow and we're shrinking back down again).
				self.shrink_pros_excluded_ids = set()
				self.shrink_pros_tries = 0
				self.iteration += 1
				continue
			if self.phase == "shrink_pros" and imp <= 0:
				if self.best_solution is not None:
					self.current_solution = copy.deepcopy(self.best_solution)
				break

			# Hard-constraint gate: only reached once none of the phase-specific stagnation
			# branches above already fired. Those revert to best_solution, which by
			# construction already satisfies every active hard constraint, so there's nothing
			# left for this gate to correct in that case. This is the fallback for the
			# remaining case: still nominally improving (imp>0) yet a hard constraint is
			# violated (e.g. no feasible solution has been found at all yet, or budget_eur
			# caps growth before service_level_min is met). Neither branch reverts to
			# best_solution itself, since objective_val can legitimately prefer a
			# constraint-violating state, and reverting here would undo the gate's own
			# correction and ping-pong forever. Both branches push forward from whatever was
			# just simulated instead.
			forced_phase = self.check_hard_constraints(self.current_solution)
			if forced_phase == "shrink":
				if self.search_parameters.search_mode == "adaptive":
					self.phase = "shrink"
					self.iteration += 1
					continue
				break
			if forced_phase == "grow":
				self.phase = "grow"
				self.iteration += 1
				continue

			if ( self.verbose > 0 ):
				if ( imp > 0 ):
					print("new improving solution found")

			self.iteration += 1


		if ( self.verbose > 0 ):
			print(f"# Terminated after {self.iteration}/{self.search_parameters.iter_limit} iterations and {self.time:.3f}/{self.search_parameters.time_limit:.3f} s (oracle: {self.time_oracle:.3f})")
		# extra logging
		if WRITE_STATUS_LOG:
			(self.BASE_DIR / "debug").mkdir(exist_ok=True)
			with open(self.BASE_DIR / "debug" / "fp.log", "a") as f:
				f.write(f"# terminated after: {self.time:.3f} (empty={empty_cab})\n")
				f.write(f"# end: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}\n")

		print("\n=== Optimization Summary ===")
		print("Iterations:", self.iteration)
		print("Phase:", self.phase)
		print("Pool size:", len(self.solution_pool))
		print("Single-cab removals:", self.num_single_cab_removals)
		print("Empty-cab removals:", self.num_empty_cab_removals)
		print("Pro removals:", self.num_pro_removals)
		print("Overshoot-correction steps stopped at the start-cab floor:", self.trial_overshoot_floor_hits)
		print("Final fleet size:", len(self.current_solution.vehicle_fleet.cabs))
		print("Final Pro count:", len(self.current_solution.vehicle_fleet.pros))

		if self.best_solution is not None:
			print("Best fleet size:", len(self.best_solution.vehicle_fleet.cabs))
			print("Best Pro count:", len(self.best_solution.vehicle_fleet.pros))

			served = (
				self.best_solution.demand_dict["num_req"]
				- self.best_solution.demand_dict["num_invalid"]
				- self.best_solution.demand_dict["num_sys_reject"]
				- self.best_solution.demand_dict["num_cus_reject"]
			)

			print("Best served requests:", served)
			is_feasible, score = self.best_rank
			print("Best objective:", score if is_feasible else "n/a (best solution is infeasible)")

		if self.search_parameters.budget_eur is not None or self.search_parameters.service_level_min is not None:
			# best_solution, not current_solution, since it's the search's actual answer
			violations = self._constraint_violations(self.best_solution) if self.best_solution is not None else []
			if violations:
				print(f"{bcolors.WARNING}Hard constraints NOT met by the best solution found:{bcolors.ENDC}")
				for v in violations:
					print(f"{bcolors.WARNING}  - {v}{bcolors.ENDC}")
			else:
				print("Hard constraints met by the best solution found.")
		return
	
	# ----------------------------------------


# ------------------------------------------------------------------------------


def read_initial_data_from_json(_demand: str="data/input/example_demand.json", _base: str="data/input/example_basedata.json", _cab_only: bool=False,
								_use_original_pro_timetable: bool=False):
	'''
	Read initial data from API JSON files
	'''
	
	def normalize_datetimes(obj):
		'''
		Function to change datetime format to ommit "ms" and include the "T"
		'''
		if isinstance(obj, dict):
			return {k: normalize_datetimes(v) for k, v in obj.items()}
		elif isinstance(obj, list):
			return [normalize_datetimes(item) for item in obj]
		elif isinstance(obj, datetime):
			# Directly normalize datetime objects
			return obj.replace(microsecond=0)
		elif isinstance(obj, str):
			try:
				# Normalize string datetimes
				dt = datetime.fromisoformat(obj.replace(" ", "T"))
				return dt.replace(microsecond=0).isoformat()
			except ValueError:
				return obj
		else:
			return obj
	
	# Read requests and base data from files
	requests = read_request_json(_demand)
	base = read_base_json(_base, _cab_only)
	
	# Normalize datetime strings
	requests = normalize_datetimes(requests)
	base = normalize_datetimes(base)
	
	# init demand and cab fleet
	init_demand = DemandScenario([Request(**entry) for entry in requests])
	
	# init operational area
	operation = {"_vertex_list": [(vertex["_longitude"],vertex["_latitude"]) for vertex in base["operation_area"][0]["_location_border"]], "_id": base["operation_area"][0]["_id"], "_desc": base["operation_area"][0]["_desc"] }
	charge = base["charging_points"]
	chain = base["chaining_location"]
	park = []
	chain_routes =  base["chain_routes"]
	chain_route_trips = base["chain_route_schedules"] if _use_original_pro_timetable else build_pro_timetable(base)

	init_operations = OperationalArea(OperationalVertices(**operation),
										[ChargingStation(**entry) for entry in charge],
										[ChainingLocation(**entry) for entry in chain],
										[ParkingLocation(**entry) for entry in park], 
										ProRoutesAndTrips([ChainRoute(**entry) for entry in chain_routes],
										[ChainRouteTrip(**entry) for entry in chain_route_trips])
									)
	
	# init base vehicles
	cabs = base["cab_schedules"]
	pros = base["pro_schedules"]
	init_cab = Cab(**cabs[0])
	if pros != []:
		init_pro = Pro(**pros[0])
	else:
		init_pro = None
	
	return init_operations, init_demand, init_cab, init_pro


# ------------------------------------------------------------------------------


def load_custom_sim_experiment_config(path: str | None) -> dict:
	if path is None:
		return {}
	with open(path, "r", encoding="utf-8") as file:
		config = json.load(file)
	if not isinstance(config, dict):
		raise ValueError("Custom-sim experiment config must contain a JSON object")
	return config


def apply_custom_sim_experiment_config(config: dict, base_cab: Cab, base_pro: Pro | None,
									   default_initial_cabs: int=1) -> tuple[int, dict, dict]:
	initial_cabs = int(config.get("initial_cabs", default_initial_cabs))
	if initial_cabs < 1:
		raise ValueError("initial_cabs must be >= 1")

	if "cab_battery_wh" in config:
		cab_battery_wh = float(config["cab_battery_wh"])
		base_cab.total_energy_capacity = cab_battery_wh
		base_cab.init_energy_capacity = cab_battery_wh

	if "convoy_charging_power_w" in config and base_pro is not None:
		base_pro.max_power_supply = float(config["convoy_charging_power_w"])

	custom_sim_overrides = config.get("custom_sim", {})
	if custom_sim_overrides is None:
		custom_sim_overrides = {}
	if not isinstance(custom_sim_overrides, dict):
		raise ValueError("custom_sim experiment config section must be a JSON object")

	cost_model_overrides = config.get("cost_model", {})
	if cost_model_overrides is None:
		cost_model_overrides = {}
	if not isinstance(cost_model_overrides, dict):
		raise ValueError("cost_model experiment config section must be a JSON object")

	return initial_cabs, custom_sim_overrides, cost_model_overrides


# Mirrors frontend/app.py's COST_MODEL_FIELDS, kept as its own copy so this module doesn't
# depend on the frontend package.
RUN_METADATA_COST_MODEL_FIELDS: list[tuple[str, str]] = [
	("cabPrice", "cab_price"),
	("proPrice", "pro_price"),
	("stationaryKwhPrice", "stationary_kwh_price"),
	("proKwhPrice", "pro_kwh_price"),
	("cabLifetimeYears", "cab_lifetime_years"),
	("proLifetimeYears", "pro_lifetime_years"),
	("interestRatePercent", "interest_rate_percent"),
	("operatingDaysPerYear", "operating_days_per_year"),
	("fareBaseEur", "fare_base_eur"),
	("fareDistanceEurPerM", "fare_distance_eur_per_m"),
	("fareTimeEurPerHour", "fare_time_eur_per_hour"),
	("fareMinPerTripEur", "fare_min_per_trip_eur"),
	("fareDailySubscriptionEur", "fare_daily_subscription_eur"),
]


def build_cost_model(cost_model_overrides: dict) -> CostModel:
	"""Build a CostModel from --custom_sim_experiment_config's optional "cost_model" block.
	Keys are CostModel's own snake_case field names (e.g. "pro_price"); anything not given
	keeps CostModel's own default."""
	kwargs = {
		f"_{field}": cost_model_overrides[field]
		for _, field in RUN_METADATA_COST_MODEL_FIELDS
		if field in cost_model_overrides
	}
	return CostModel(**kwargs)


def compute_run_scenario_prefix(operational_area: OperationalArea, base_cab: Cab, base_pro: Pro | None,
								 demand_scenario: DemandScenario) -> str:
	"""Schema prefix (area_NcsNlinesNbcNcpNrqNtw) for a run's output files. Reimplements
	custom_sim/utils_cs.py's build_prefix() on the run's initial config instead of a solved
	iteration's state (they agree for iteration 0), so this module doesn't import custom_sim."""
	raw_area = str(getattr(operational_area.operational_area, "description", "") or "").strip().lower()
	area = raw_area.split("_", 1)[0].split("-", 1)[0].split(" ", 1)[0] or "pb"
	area = {"paderborn": "pb", "hoexter": "hx"}.get(area, area)
	if area in ("default", "unknown"):
		plate = str(getattr(base_cab, "licensePlate", "") or "")
		if isinstance(plate, (list, tuple)):
			plate = plate[0] if plate else ""
		plate_prefix = plate.split("-", 1)[0].lower() if "-" in plate else ""
		area = {"pb": "pb", "hx": "hx"}.get(plate_prefix, area)
	area = "".join(ch for ch in area if ch.isalnum()) or "pb"

	cab_energy = float(getattr(base_cab, "total_energy_capacity", 0) or 0)
	cp_power = float(getattr(base_pro, "max_power_supply", 0) or 0) if base_pro is not None else 0.0
	energy = int(round(cab_energy / 1000.0)) if cab_energy >= 1000 else int(round(cab_energy))
	power = int(round(cp_power / 1000.0)) if cp_power >= 1000 else int(round(cp_power))

	routes = getattr(operational_area.pro_routes_and_trips, "chain_routes", []) or []
	windows = set()
	for req in demand_scenario.requests:
		lower, upper = req.tw_lower, req.tw_upper
		if isinstance(lower, str):
			lower = datetime.fromisoformat(lower.replace("Z", "+00:00"))
		if isinstance(upper, str):
			upper = datetime.fromisoformat(upper.replace("Z", "+00:00"))
		if isinstance(lower, datetime) and isinstance(upper, datetime) and upper > lower:
			windows.add(int(round((upper - lower).total_seconds() / 60.0)))
	timewindow = next(iter(windows)) if len(windows) == 1 else 0

	return (
		f"{area}_{len(operational_area.charging_stations)}cs_{int(len(routes) / 2)}lines_"
		f"{energy}bc_{power}cp_{demand_scenario.num_requests}rq_{timewindow}tw"
	)


def write_run_metadata_file(use_sim: str, search_parameters: SearchParameters, cost_model: CostModel,
							 operational_area: OperationalArea, base_cab: Cab, base_pro: Pro | None,
							 demand_scenario: DemandScenario, custom_sim_overrides: dict,
							 use_original_pro_timetable: bool, status: str, started_at: str,
							 completed_at: str | None = None, failed_at: str | None = None) -> str | None:
	"""Write/update this run's <prefix>_simulation_metadata.json, mirroring what the dashboard
	writes for a tracked job (frontend/app.py's _build_sim_metadata_payload()), so a standalone
	run leaves the same record behind without the dashboard involved. Called once before
	optimize() (status="running") and once after (status="completed"/"failed"), same as the
	dashboard rewrites its own copy on every status change.

	Only wired up for use_sim=="custom": rw/sumo don't yet write per-iteration output into a
	folder this module knows about (custom_sim does, via
	--custom_sim_experiment_config's experiment_output.config_folder).
	"""
	if use_sim != "custom":
		return None

	experiment_output = custom_sim_overrides.get("experiment_output", {}) if isinstance(custom_sim_overrides, dict) else {}
	config_folder = str(experiment_output.get("config_folder", "")).strip() if isinstance(experiment_output, dict) else ""
	output_dir = Path(__file__).resolve().parent / "custom_sim" / "output"
	if config_folder:
		output_dir = output_dir / config_folder
	output_dir.mkdir(parents=True, exist_ok=True)

	early_unchaining_enabled = True
	algorithm_overrides = custom_sim_overrides.get("algorithm", {}) if isinstance(custom_sim_overrides, dict) else {}
	if isinstance(algorithm_overrides, dict):
		early_unchaining_overrides = algorithm_overrides.get("early_unchaining", {})
		if isinstance(early_unchaining_overrides, dict) and "enabled" in early_unchaining_overrides:
			early_unchaining_enabled = bool(early_unchaining_overrides["enabled"])

	prefix = compute_run_scenario_prefix(operational_area, base_cab, base_pro, demand_scenario)

	payload = {
		"scenarioName": "",
		"algorithm": "custom",
		"runKind": "search",
		"status": status,
		"createdAt": started_at,
		"startedAt": started_at,
		"completedAt": completed_at,
		"failedAt": failed_at,
		"searchMode": search_parameters.search_mode,
		"iterLimit": search_parameters.iter_limit,
		"timeLimit": search_parameters.time_limit,
		"initialCabs": search_parameters.initial_cabs,
		"cabAddStep": search_parameters.cab_add_step,
		"shrinkProsAllowRetry": search_parameters.shrink_pros_allow_retry,
		"shrinkProsMaxTries": search_parameters.shrink_pros_max_tries,
		"shrinkProsTrialProbeBelowStart": search_parameters.shrink_pros_trial_probe_below_start,
		"shrinkProsTrialOvershootCorrection": search_parameters.shrink_pros_trial_overshoot_correction,
		"stagnationTolerance": search_parameters.stagnation_tolerance,
		"stagnationTolerancePatience": search_parameters.stagnation_tolerance_patience,
		"objectiveWeight": search_parameters.objective_weight,
		"objectiveTerms": search_parameters.objective_terms,
		"budgetEur": search_parameters.budget_eur,
		"serviceLevelMin": search_parameters.service_level_min,
		"useOriginalProTimetable": bool(use_original_pro_timetable),
		"earlyUnchainingEnabled": early_unchaining_enabled,
		**{json_key: getattr(cost_model, attr) for json_key, attr in RUN_METADATA_COST_MODEL_FIELDS},
		"folderName": config_folder,
		"custom_sim": {
			"experiment_output": {"config_folder": config_folder},
			"algorithm": {"early_unchaining": {"enabled": early_unchaining_enabled}},
		},
	}

	metadata_path = output_dir / f"{prefix}_simulation_metadata.json"
	with open(metadata_path, "w", encoding="utf-8") as file:
		json.dump(payload, file, indent="\t")
	return str(metadata_path)


def main():
	
	parser = argparse.ArgumentParser()
	parser.add_argument('file_name')    
	parser.add_argument("--base_data", default = "data/input/example_basedata.json")  
	parser.add_argument('--verbose',
	                action='store_true') 

	parser.add_argument(
	    '--use_sim',
	    type=str,
	    choices=["sumo", "rw", "custom"],
	    default="custom",  # default value
	    help="Simulation type to use. Options: 'sumo', 'rw', 'custom'. Default is 'custom'."
	)
	parser.add_argument(
	    '--rw_cache',
	    type=str,
	    choices=["auto", "force", "off"],
	    default="auto",
	    help="RW cache mode. 'auto' reuses complete debug outputs, 'force' and 'off' always call the API."
	)
	parser.add_argument(
	    '--resume_from',
	    type=int,
	    default=0,
	    help="For RW runs, require cached outputs for iterations before this iteration."
	)
	parser.add_argument(
	    '--rw_retries',
	    type=int,
	    default=1,
	    help="Number of full RW API resubmission retries after a request failure."
	)
	parser.add_argument(
	    '--iter_limit',
	    type=int,
	    default=100,
	    help="Number of fleet-planning iterations to run."
	)
	parser.add_argument(
	    '--time_limit',
	    type=float,
	    default=18000,
	    help="Wall-clock budget for the search, in seconds. Checked between iterations against "
	         "the time already spent (self.time); a run stops once either this or --iter_limit "
	         "is reached, whichever comes first."
	)
	parser.add_argument(
	    '--initial_cabs',
	    type=int,
	    default=None,
	    help="Initial number of cabs. Overrides the experiment config value."
	)
	parser.add_argument(
	    '--cab_add_step',
	    type=int,
	    default=5,
	    help="Cabs added per growth step. 'static' mode: the literal step size, every "
	         "iteration. 'adaptive' mode: a floor under the estimate-driven growth step during "
	         "ordinary top-level growth (the estimate can still push it higher), not applied "
	         "during a shrink_pros trial's own nested regrow, which trusts the estimate "
	         "directly instead (see execute_move())."
	)
	parser.add_argument(
	    '--custom_sim_experiment_config',
	    type=str,
	    default=None,
	    help="JSON file with custom-sim experiment changes. Also accepts a top-level "
	         "\"cost_model\" object (CostModel's own snake_case field names) to override cost "
	         "model defaults, applying to any --use_sim backend, not just custom."
	)
	parser.add_argument(
	    '--use_original_pro_timetable',
	    action='store_true',
	    help="Use the uploaded chain_route_schedules verbatim instead of regenerating a full Pro "
	         "timetable from routed line templates (see build_pro_timetable() in utils.py). "
	         "Default off: a hand-authored or incomplete uploaded schedule can be operationally "
	         "infeasible (overlapping trips, gaps a Pro can't physically make), since it's never "
	         "checked against real routing/turnaround times when this is set."
	)
	parser.add_argument(
	    '--search_mode',
	    type=str,
	    choices=["adaptive", "static"],
	    default="static",
	    help="'adaptive': grow -> shrink -> shrink_pros with estimation-based cab growth. "
	         "'static': fixed initial Pro count, cabs added in constant steps until growth stagnates."
	)
	parser.add_argument(
	    '--budget_eur',
	    type=float,
	    default=None,
	    help="Hard cap on the fleet's raw purchase price (num_cabs*cab_price + num_pros*pro_price). "
	         "Unset disables the constraint."
	)
	parser.add_argument(
	    '--service_level_min',
	    type=float,
	    default=None,
	    help="Hard floor on service_rate (served/valid requests, 0..1). Unset disables the constraint."
	)
	parser.add_argument(
	    '--shrink_pros_allow_retry',
	    action='store_true',
	    help="Adaptive mode only: let a Pro that already failed its shrink_pros trade-off trial "
	         "become eligible again once a later trial succeeds. Default: excluded for the rest "
	         "of shrink_pros once it fails."
	)
	parser.add_argument(
	    '--shrink_pros_max_tries',
	    type=int,
	    default=1,
	    help="Adaptive mode only: max number of distinct Pros tried per shrink_pros round before "
	         "giving up. Each try is a full nested grow+shrink recovery attempt. 0 disables the "
	         "trade-off trial mechanism entirely (old shrink_pros behavior)."
	)
	parser.add_argument(
	    '--no_shrink_pros_trial_probe_below_start',
	    dest='shrink_pros_trial_probe_below_start',
	    action='store_false',
	    help="Adaptive mode only: when a shrink_pros trial's nested grow finds no improvement "
	         "at all, don't also try reducing the Cab count below where the trial started, "
	         "concluding the trial immediately instead. Default: try it, at the cost of one "
	         "extra iteration whenever nested growth didn't help at all."
	)
	parser.add_argument(
	    '--shrink_pros_trial_overshoot_correction',
	    action='store_true',
	    help="Adaptive mode only: when a shrink_pros trial's nested grow succeeds via a single "
	         "jump of more than one Cab, walk back down toward (never below) the trial's "
	         "starting Cab count to verify the full jump was actually necessary. Default off, "
	         "since with the current estimate this case is expected to be rare, and it's an "
	         "extra iteration or more whenever it does happen. See the run summary's "
	         "'overshoot-correction steps stopped at the start-cab floor' count."
	)
	parser.add_argument(
	    '--stagnation_tolerance',
	    type=float,
	    default=0.0,
	    help="Fraction of a reference score a step may miss by and still count as close "
	         "enough to continue, instead of stopping the instant a step fails to improve. "
	         "Applies to grow, shrink, and the shrink_pros trial mechanism, not shrink_pros's "
	         "own outer check. Never softens a feasibility regression. 0.0 (default): no "
	         "tolerance, identical to today's behavior. See models.py's SearchParameters for "
	         "the full mechanics."
	)
	parser.add_argument(
	    '--stagnation_tolerance_patience',
	    type=int,
	    default=1,
	    help="Max number of consecutive tolerance-driven steps before giving up for real, "
	         "even if still within tolerance. Only relevant when --stagnation_tolerance > 0."
	)
	parser.add_argument(
	    '--objective_weight',
	    type=float,
	    default=1.0,
	    help="Alpha in [0,1], weight on the first --objective_terms entry vs. the second. "
	         "Only used when --objective_terms selects 2 metrics; ignored with 1."
	)
	parser.add_argument(
	    '--objective_terms',
	    type=str,
	    default="total_served,avg_cost_per_trip_eur",
	    help=f"Comma-separated list of 1 or 2 metrics to combine into the search objective, "
	         f"from: {', '.join(OBJECTIVE_METRICS)}. Default: both, weighted by "
	         f"--objective_weight."
	)
	args = parser.parse_args()
	# process arguments

	file_name = args.file_name
	verbose = args.verbose
	use_sim = args.use_sim
	rw_cache = args.rw_cache
	resume_from = args.resume_from
	rw_retries = args.rw_retries
	iter_limit = args.iter_limit
	time_limit = args.time_limit
	initial_cabs_arg = args.initial_cabs
	experiment_config = load_custom_sim_experiment_config(args.custom_sim_experiment_config)
	if resume_from < 0:
		parser.error("--resume_from must be >= 0")
	if rw_retries < 0:
		parser.error("--rw_retries must be >= 0")
	if iter_limit < 1:
		parser.error("--iter_limit must be >= 1")
	if time_limit <= 0:
		parser.error("--time_limit must be > 0")
	if initial_cabs_arg is not None and initial_cabs_arg < 1:
		parser.error("--initial_cabs must be >= 1")
	if args.cab_add_step < 1:
		parser.error("--cab_add_step must be >= 1")
	if args.budget_eur is not None and args.budget_eur <= 0:
		parser.error("--budget_eur must be > 0")
	if args.service_level_min is not None and not (0 < args.service_level_min <= 1):
		parser.error("--service_level_min must be in (0, 1]")
	if args.shrink_pros_max_tries < 0:
		parser.error("--shrink_pros_max_tries must be >= 0")
	if not (0 <= args.stagnation_tolerance):
		parser.error("--stagnation_tolerance must be >= 0")
	if args.stagnation_tolerance_patience < 0:
		parser.error("--stagnation_tolerance_patience must be >= 0")
	if not (0 <= args.objective_weight <= 1):
		parser.error("--objective_weight must be in [0, 1]")
	objective_terms = [t.strip() for t in args.objective_terms.split(",") if t.strip()]
	if not (1 <= len(objective_terms) <= 2):
		parser.error("--objective_terms must list 1 or 2 metrics")
	if len(objective_terms) != len(set(objective_terms)):
		parser.error("--objective_terms must not repeat a metric")
	unknown_terms = [t for t in objective_terms if t not in OBJECTIVE_METRICS]
	if unknown_terms:
		parser.error(f"--objective_terms unknown metric(s) {unknown_terms}, choose from: {', '.join(OBJECTIVE_METRICS)}")
	base_data_file = args.base_data
	base_data_name = base_data_file.split(".")[0]
	
	if WRITE_STATUS_LOG:
		Path("debug").mkdir(exist_ok=True)
		with open("./debug/fp.log", "a") as f:
			f.write(f"# [{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}] start fleetplanning with sim: {use_sim} \n")
			f.write(f"# input_base_file: {base_data_file}\n")
			f.write(f"# input_req_file: {file_name}\n")
			f.write(f"# iter_limit: {iter_limit} time_limit: {time_limit} search_mode: {args.search_mode}\n")
			if use_sim == "rw":
				f.write(f"# rw_cache: {rw_cache}\n")
				f.write(f"# resume_from: {resume_from}\n")
				f.write(f"# rw_retries: {rw_retries}\n")
	
	# -----------------------

	if verbose:
		print("# set initial data")
	op, dem, ca, pr = read_initial_data_from_json(
		file_name, base_data_file, _use_original_pro_timetable=args.use_original_pro_timetable
	)
	try:
		initial_cabs, custom_sim_overrides, cost_model_overrides = apply_custom_sim_experiment_config(
			experiment_config,
			ca,
			pr,
			default_initial_cabs=1,
		)
	except ValueError as exc:
		parser.error(str(exc))
	if initial_cabs_arg is not None:
		initial_cabs = initial_cabs_arg
	cost_model = build_cost_model(cost_model_overrides)

	if verbose:
		print("# set SearchParameters")
	params = SearchParameters(
		iter_limit, time_limit, _initial_cabs=initial_cabs, _cab_add_step=args.cab_add_step,
		_search_mode=args.search_mode,
		_budget_eur=args.budget_eur, _service_level_min=args.service_level_min,
		_shrink_pros_allow_retry=args.shrink_pros_allow_retry, _shrink_pros_max_tries=args.shrink_pros_max_tries,
		_shrink_pros_trial_probe_below_start=args.shrink_pros_trial_probe_below_start,
		_shrink_pros_trial_overshoot_correction=args.shrink_pros_trial_overshoot_correction,
		_stagnation_tolerance=args.stagnation_tolerance,
		_stagnation_tolerance_patience=args.stagnation_tolerance_patience,
		_objective_weight=args.objective_weight,
		_objective_terms=objective_terms,
	)

	print("DEMAND SCENARIO \n #reqs:", dem.num_requests)

	if verbose:
		print("# instantiate FleetPlanning object")

	heuristic = FleetPlanning(
		params,
		op,
		dem,
		ca,
		pr,
		_use_sim = use_sim,
		file_name = file_name,
		_rw_cache = rw_cache,
		_resume_from = resume_from,
		_rw_retries = rw_retries,
		_custom_sim_overrides = custom_sim_overrides,
		_cost_model = cost_model,
	)

	def _write_metadata(status, **timestamps):
		return write_run_metadata_file(
			use_sim, params, heuristic.cost_model, op, ca, pr, dem,
			custom_sim_overrides, args.use_original_pro_timetable,
			status=status, started_at=started_at, **timestamps,
		)

	# written before optimize() runs (status "running"), then rewritten after, mirroring how
	# the dashboard rewrites its own copy on every job status change
	started_at = datetime.now().astimezone().isoformat()
	metadata_path = _write_metadata("running")
	if verbose and metadata_path:
		print(f"# wrote run metadata: {metadata_path}")

	# optimize instance
	if verbose:
		print("# call main optimize")
	try:
		heuristic.optimize()
	except Exception:
		_write_metadata("failed", failed_at=datetime.now().astimezone().isoformat())
		raise
	_write_metadata("completed", completed_at=datetime.now().astimezone().isoformat())

	# --------------------
	if verbose:
		print(heuristic.solution_pool)

	return


## ------------------------------------------------------------------------------
## ------------------------------------------------------------------------------


if __name__ == "__main__":
	#print("fleetplanning/main")
	print(f"{bcolors.HEADER}Fleetplanning START{bcolors.ENDC}")
	main()
	print(f"{bcolors.HEADER}Fleetplanning END{bcolors.ENDC}")
	
	
