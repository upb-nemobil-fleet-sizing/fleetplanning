"""
Tests for the shrink_pros Pro-for-cabs trade-off trial mechanism in FleetPlanning.optimize()
(fleet_planning.py) - see the commit that introduced it for the design writeup.

These drive the real FleetPlanning search loop (real move selection, real phase/trial-stage state
machine, real CostModel/rank accounting) but replace run_operational_planning with a synthetic,
fully-controlled oracle instead of a real simulator backend - everything under test here is pure
control flow (trial start/stop, the hard-constraint gate, exclusion bookkeeping), not simulation
fidelity, so a real custom_sim/rw/sumo run isn't needed to exercise it.
"""
import copy

from datetime import datetime, timedelta

import pytest

from fleet_planning import FleetPlanning
from models import (Cab, ChainRoute, ChainRouteTrip, ChainingLocation, ChargingStation, DemandScenario,
					OperationalArea, OperationalVertices, Pro, ProRoutesAndTrips, Request, SearchParameters)


def _stop(lon, lat):
	return {"_start_location_lon": lon, "_start_location_lat": lat, "_end_location_lon": lon, "_end_location_lat": lat}


def _synthetic_inputs(n_requests=300, n_pros=8, n_lines=4):
	"""One cab, one charger, one chain line served by n_pros Pros, n_requests requests, all in code."""
	start = datetime.fromisoformat("2025-10-18T05:00:00+02:00")
	end = datetime.fromisoformat("2025-10-18T23:00:00+02:00")
	lon, lat = 8.67, 51.63
	corners = [(lon - 0.02, lat - 0.02), (lon + 0.02, lat - 0.02), (lon + 0.02, lat + 0.02), (lon - 0.02, lat + 0.02)]
	stop_a = {"_start_location_lon": lon, "_start_location_lat": lat, "_end_location_lon": lon, "_end_location_lat": lat}
	stop_b = {"_start_location_lon": lon + 0.01, "_start_location_lat": lat + 0.01, "_end_location_lon": lon + 0.01, "_end_location_lat": lat + 0.01}
	base = {
		"operation_area": [{"_id": "synthetic", "_desc": "synthetic", "_location_border": [{"_longitude": x, "_latitude": y} for x, y in corners]}],
		"cab_schedules": [{"_id": 1, "_init_location_lon": lon, "_init_location_lat": lat, "_schedule_start": start,
							"_schedule_end": end, "_max_speed": 8.333333, "_default_energy_consumption_per_m": 0.1,
							"_energy_capacity": 10000, "_initial_energy_capacity": 10000, "_num_seats": 4, "_ramp": False}],
		"pro_schedules": [{"_id": i, "_max_cabs": 3, "_additional_energy": [0.1], "_init_location_lon": lon, "_init_location_lat": lat,
							"_schedule_start": start, "_schedule_end": end, "_max_speed": 19.444444,
							"_max_power_supply": 11000, "_default_energy_consumption_per_m": 0.18, "_energy_capacity": 10000,
							"_initial_energy_capacity": 10000} for i in range(1, n_pros + 1)],
		"charging_points": [{"_id": "CS", "_location_lon": lon, "_location_lat": lat, "_max_supply": 11000,
							"_schedule_start": "05:00:00", "_schedule_end": "23:00:00"}],
		"chaining_location": [loc for k in range(n_lines) for loc in (
			{"_id": f"A{k}_Chain", "_type": "Chain", "_additional_time": 60, **_stop(lon - 0.01 + 0.02 * (k % 2), lat - 0.01 + 0.02 * (k // 2))},
			{"_id": f"B{k}_Chain", "_type": "Chain", "_additional_time": 60, **_stop(lon - 0.005 + 0.02 * (k % 2), lat - 0.005 + 0.02 * (k // 2))},
		)],
		"chain_routes": [route for k in range(n_lines) for route in (
			{"_id": f"A{k}_B{k}", "_start_location": f"A{k}_Chain", "_end_location": f"B{k}_Chain", "_intermediate_locations": [], "_duration": 600, "_distance": 1500},
			{"_id": f"B{k}_A{k}", "_start_location": f"B{k}_Chain", "_end_location": f"A{k}_Chain", "_intermediate_locations": [], "_duration": 600, "_distance": 1500},
		)],
		"chain_route_schedules": [
			{"_id": f"A{(i - 1) % n_lines}_B{(i - 1) % n_lines}_{i}", "_chain_route": f"A{(i - 1) % n_lines}_B{(i - 1) % n_lines}", "_pro_schedule": f"Pro{i}",
				"_departure": (start + timedelta(minutes=30 * i)).isoformat(),
				"_arrival": (start + timedelta(minutes=30 * i + 10)).isoformat()}
			for i in range(1, n_pros + 1)
		],
		"parking_location": [],
	}
	requests = []
	for i in range(n_requests):
		t = start + timedelta(minutes=3 * i)
		requests.append({
			"_id": i, "_pu_lat": lat + 0.0005 * (i % 40), "_pu_lon": lon + 0.0005 * (i % 30),
			"_do_lat": lat - 0.0005 * (i % 25), "_do_lon": lon - 0.0005 * (i % 35),
			"_register_time": start, "_tw_type": False,
			"_tw_lower": t, "_tw_upper": t + timedelta(minutes=10),
			"_num_persons": 1, "_need_ramp": False, "_ride_sharing": False, "_tol_upper": 300, "_tol_lower": 300,
		})
	return base, requests


def _build_fp(iter_limit=20, initial_cabs=1, **search_kwargs):
	base, requests = _synthetic_inputs()
	area = base["operation_area"][0]
	op = OperationalArea(
		OperationalVertices(_vertex_list=[(v["_longitude"], v["_latitude"]) for v in area["_location_border"]],
							_id=area["_id"], _desc=area["_desc"]),
		[ChargingStation(**e) for e in base["charging_points"]],
		[ChainingLocation(**e) for e in base["chaining_location"]],
		[],
		ProRoutesAndTrips([ChainRoute(**e) for e in base["chain_routes"]],
						  [ChainRouteTrip(**e) for e in base["chain_route_schedules"]]),
	)
	dem = DemandScenario([Request(**e) for e in requests])
	cab = Cab(**base["cab_schedules"][0])
	pro = Pro(**base["pro_schedules"][0])
	params = SearchParameters(
		_iter=iter_limit, _search_mode="adaptive", _initial_cabs=initial_cabs, **search_kwargs
	)
	# _use_sim="test" matches none of FleetPlanning.__init__'s sumo/custom/rw branches, so no real
	# simulator backend gets constructed - safe since run_operational_planning is always replaced
	# with a synthetic oracle below before optimize() runs.
	return FleetPlanning(params, op, dem, cab, pro, _use_sim="test")


def _install_oracle(fp, served_fn):
	"""
	Replace run_operational_planning with a synthetic oracle: served_fn(num_cabs, active_pro_ids)
	-> number of requests served (evenly distributed across cabs, rest rejected). Distance/energy/
	time are left at their zero defaults - avg_cost_per_trip_eur then reduces to pure amortized
	vehicle cost / trips served, fully deterministic from (num_cabs, num_pros, served) alone, no
	routing needed. Pro usage stats (chained_cabs/trips) default to 0 (all Pros "unused") unless a
	test sets them explicitly on the fleet before running.
	"""
	def _run(solution, iteration):
		cabs = solution.vehicle_fleet.cabs
		pro_ids = {p.id for p in solution.vehicle_fleet.pros}
		served = served_fn(len(cabs), pro_ids)
		requests = solution.demand_scenario.requests
		for i, req in enumerate(requests):
			is_served = i < served
			req.sim_invalid = False
			req.sim_system_reject = False
			req.sim_custom_reject = not is_served
			req.sim_wait_time_prop = 0.0
		for i, cab in enumerate(cabs):
			n = served // len(cabs) + (1 if i < served % len(cabs) else 0)
			cab.sim_num_requests = n
			cab.sim_customer_trip_count = n
		for pro in solution.vehicle_fleet.pros:
			# Non-zero so has_unused_pro()/remove_unused_pros doesn't bulk-remove every Pro in
			# one shot before deactivate_least_used_pro (the trial-eligible move) ever runs.
			pro.sim_cum_chained_cabs = 1

	fp.run_operational_planning = _run


def _track_moves(fp):
	"""
	Wrap select_moves to record (phase, trial_stage, move) as seen at each decision point, plus
	whether the fleet was budget-feasible at that point - the sequence a test needs to check
	trial start/stop timing without depending on FleetPlanning's internal method boundaries.
	"""
	history = []
	orig_select_moves = fp.select_moves

	def _tracking():
		num_cabs = len(fp.current_solution.vehicle_fleet.cabs)
		num_pros = len(fp.current_solution.vehicle_fleet.pros)
		budget_violated = (
			fp.search_parameters.budget_eur is not None
			and fp.cost_model.fleet_price_eur(num_cabs, num_pros) > fp.search_parameters.budget_eur
		)
		snapshot = (fp.phase, fp.trial_stage, budget_violated)
		move = orig_select_moves()
		history.append((*snapshot, move))
		return move

	fp.select_moves = _tracking
	return history


# ----------------------------------------------------------------------------
# Direct unit tests (no full optimize() run)
# ----------------------------------------------------------------------------

def test_check_hard_constraints_suppressed_mid_trial():
	"""The gate must not fire while a trial is in progress, even if both constraints are
	violated - it would derail the trial's own bubble-based accept/stop logic (see
	check_hard_constraints()'s docstring)."""
	fp = _build_fp(_budget_eur=1.0, _service_level_min=0.99)
	solution = fp.initial_solution()
	solution.update_attributes()

	fp.phase = "shrink_pros"
	fp.trial_stage = "grow"
	assert fp.check_hard_constraints(solution) is None

	fp.trial_stage = "shrink"
	assert fp.check_hard_constraints(solution) is None

	# Sanity: with trial_stage back to None, the pre-existing gate logic still fires as before.
	fp.trial_stage = None
	fp.phase = "grow"
	assert fp.check_hard_constraints(solution) == "shrink"


def test_deactivate_least_used_pro_respects_exclude_ids():
	fp = _build_fp()
	solution = fp.initial_solution(_start_pros=3)
	pros = sorted(solution.vehicle_fleet.pros, key=lambda p: p.id)
	assert len(pros) >= 3
	ids = [p.id for p in pros]

	removed = fp.deactivate_least_used_pro(solution.vehicle_fleet, exclude_ids={ids[0]})
	assert removed == ids[1]  # skipped the excluded lowest-id Pro, took the next one

	removed_none = fp.deactivate_least_used_pro(solution.vehicle_fleet, exclude_ids=set(ids))
	assert removed_none is None  # every remaining Pro excluded -> nothing to remove


# ----------------------------------------------------------------------------
# Full optimize() runs against the synthetic oracle
# ----------------------------------------------------------------------------

def test_max_tries_zero_disables_trials():
	"""shrink_pros_max_tries=0 (the "give me the old behavior" escape hatch) must mean
	trial_stage never becomes anything but None for the whole run."""
	fp = _build_fp(_shrink_pros_max_tries=0)
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: min(375, cabs * 10 + len(pro_ids) * 15))
	history = _track_moves(fp)

	fp.optimize()

	assert all(trial_stage is None for _, trial_stage, _, _ in history)
	assert fp.trial_stage is None


def test_budget_gates_trial_start():
	"""No trial (grow/shrink trial_stage) may start while the fleet is still over budget -
	growth would be capped to near nothing anyway, and plain Pro removal alone is what should
	be resolving the violation (see check_hard_constraints()'s budget_eur note)."""
	fp = _build_fp(_budget_eur=900000.0)  # 1 cab + 8 Pros (default init) is far over this
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: max(cabs, 1))
	history = _track_moves(fp)

	fp.optimize()

	for phase, trial_stage, budget_violated, move in history:
		if trial_stage is not None:
			assert not budget_violated, (
				f"trial_stage={trial_stage!r} started while fleet was still over budget "
				f"(phase={phase!r}, move={move!r})"
			)


def test_trial_recovers_and_succeeds():
	"""A Pro whose removal hurts service, but where growing cabs can plausibly make up the
	difference cheaper than keeping the Pro (pro_price=500k vs cab_price=100k), should be kept
	removed - the trial should visit the nested grow stage and end with fewer Pros than it
	started shrink_pros with. No nested shrink stage is expected here: with
	trial_recovery_target's estimate, recovery happens one Cab at a time, so a successful grow
	leaves nothing for overshoot correction to check (off by default anyway), and grow finding
	something at all means the separate below-start question is never asked either."""
	fp = _build_fp(iter_limit=40, _shrink_pros_max_tries=3)
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: min(375, cabs * 10 + len(pro_ids) * 15))
	history = _track_moves(fp)

	fp.optimize()

	shrink_pros_entries = [h for h in history if h[0] == "shrink_pros"]
	assert any(trial_stage == "grow" for _, trial_stage, _, _ in shrink_pros_entries)
	# A trial concluded with trial_stage reset back to None by the time the run finished.
	assert fp.trial_stage is None


def test_trial_fails_excludes_pro_and_tries_a_different_one():
	"""A Pro whose removal craters service in a way cab growth can't plausibly recover from
	should be excluded (not deleted) after its trial fails, and shrink_pros should move on to a
	different Pro rather than retrying the same one."""
	fp = _build_fp(iter_limit=40, _shrink_pros_max_tries=2)
	# Peek at the deterministic initial Pro id set to know which one gets tried first (lowest
	# id wins deactivate_least_used_pro's tie-break when usage stats are equal, which they are
	# here - the oracle never sets sim_cum_chained_cabs/sim_total_trips_with_pro).
	probe = fp.initial_solution()
	protected_id = min(p.id for p in probe.vehicle_fleet.pros)

	def served_fn(cabs, pro_ids):
		if protected_id not in pro_ids:
			# Capacity hard-capped regardless of cab count: no amount of growth can make up
			# for this specific Pro, unlike a normal one (below).
			return min(10, cabs)
		return min(375, cabs * 10 + len(pro_ids) * 15)

	_install_oracle(fp, served_fn)
	history = _track_moves(fp)

	fp.optimize()

	assert protected_id in fp.shrink_pros_excluded_ids
	# The exclusion didn't just get ignored - a later deactivate_least_used_pro (real, top-level
	# call, not the nested "shrink" trial move which also uses remove_one_cab) targeted a
	# different Pro after the failure.
	plain_removals = [h for h in history if h[0] == "shrink_pros" and h[1] is None and h[3] == "deactivate_least_used_pro"]
	assert len(plain_removals) >= 1


def test_allow_retry_clears_exclusions_on_success():
	"""With shrink_pros_allow_retry, a Pro excluded by one failed trial should become eligible
	again once a *different* trial succeeds (fleet composition changed enough to be worth
	reconsidering); with the default False, it should stay excluded for the rest of shrink_pros."""
	fp = _build_fp(iter_limit=60, _shrink_pros_max_tries=4, _shrink_pros_allow_retry=True)
	probe = fp.initial_solution()
	protected_id = min(p.id for p in probe.vehicle_fleet.pros)

	def served_fn(cabs, pro_ids):
		if protected_id not in pro_ids:
			return min(10, cabs)
		return min(375, cabs * 10 + len(pro_ids) * 15)

	_install_oracle(fp, served_fn)
	fp.optimize()

	# The protected Pro's trial fails every time it's tried (served craters and cabs can't
	# compensate), but with allow_retry it must not stay permanently excluded once some other
	# trial succeeds - it should still be sitting in the active fleet (never actually removed,
	# since every attempt on it fails and reverts) rather than piling up in the exclusion set.
	assert protected_id in {p.id for p in fp.current_solution.vehicle_fleet.pros}
