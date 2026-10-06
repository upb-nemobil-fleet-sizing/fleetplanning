import copy
from datetime import datetime
from types import SimpleNamespace

from custom_sim.custom_simulation import CustomSimulation
from custom_sim.models_cs import (
	CabEntryType, CabScheduleEntry, CandidateMetrics, Customer, NCCandidate, ProVehicle,
	RuntimeDiagnostics,
)
from models import ChargingStation, Pro, Request


def _ts(iso_str: str) -> datetime:
	return datetime.fromisoformat(iso_str)


def _build_request(req_id: int = 1) -> Request:
	return Request(
		_id=req_id,
		_pu_lat=51.0,
		_pu_lon=8.0,
		_do_lat=51.1,
		_do_lon=8.1,
		_register_time=_ts("2025-10-18T08:00:00+02:00"),
		_tw_lower=_ts("2025-10-18T09:00:00+02:00"),
		_tw_upper=_ts("2025-10-18T09:10:00+02:00"),
		_tw_type=True,
	)


def test_clone_for_candidate_preserves_domain_refs_and_detaches_mutable_entry_state():
	customer = Customer.from_model_fp(_build_request())
	station = ChargingStation("CS_A", 51.2, 8.2)
	pro_context = SimpleNamespace(id="pro-context")
	entry = CabScheduleEntry(
		_type=CabEntryType.CT,
		_cust=customer,
		_charging_s=station,
		_pro=pro_context,
		_s_lat=51.0,
		_s_lon=8.0,
		_e_lat=51.1,
		_e_lon=8.1,
		_s_time=100,
		_e_time=200,
		_serv_t=60,
		_s_charge=9000.0,
		_e_charge=8500.0,
		_distance_m=1234.5,
		_charge_amount=111.0,
		_max_charge_amount=222.0,
		_early_unchaining=True,
	)
	entry.route = [10, 20, 30]

	cloned = entry.clone_for_candidate()
	cloned.start_time = 150
	cloned.end_charge = 8000.0
	cloned.route.append(40)

	assert cloned is not entry
	assert cloned.customer is customer
	assert cloned.charge_station is station
	assert cloned.pro is pro_context
	assert cloned.max_charge_amount == 222.0
	assert cloned.early_unchaining is True
	assert entry.start_time == 100
	assert entry.end_charge == 8500.0
	assert entry.route == [10, 20, 30]
	assert cloned.route == [10, 20, 30, 40]


def test_deepcopy_breaks_identity_based_station_filtering_used_by_right_chp_selection():
	current_station = ChargingStation("CS_A", 51.2, 8.2)
	other_station = ChargingStation("CS_B", 51.3, 8.3)
	stations = [current_station, other_station]

	right_entry = CabScheduleEntry(
		_type=CabEntryType.ChP,
		_charging_s=current_station,
	)
	cloned_entry = right_entry.clone_for_candidate()
	deepcopied_entry = copy.deepcopy(right_entry)

	remaining_with_clone = [
		station for station in stations if station is not cloned_entry.charge_station
	]
	remaining_with_deepcopy = [
		station for station in stations if station is not deepcopied_entry.charge_station
	]

	assert remaining_with_clone == [other_station]
	assert remaining_with_deepcopy == stations


def test_update_customer_sim_state_from_cloned_entry_updates_canonical_customer():
	customer = Customer.from_model_fp(_build_request())
	customer.sim_state.assigned = True
	customer.sim_state.assigned_cab_id = 7

	entry = CabScheduleEntry(
		_type=CabEntryType.CT,
		_cust=customer,
		_s_time=100,
		_e_time=220,
	)
	cloned_entry = entry.clone_for_candidate()

	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	cab_vehicle = SimpleNamespace(cab=SimpleNamespace(id=7))

	sim._update_customer_sim_state_from_entry(cloned_entry, cab_vehicle)

	assert customer.sim_state.pickup_ts == 100
	assert customer.sim_state.dropoff_ts == 220
	assert customer.sim_state.pickup_entry_type == "CT"
	assert customer.sim_state.dropoff_entry_type == "CT"


def test_pro_vehicle_reads_cab_recharge_power_from_pro_data():
	pro = Pro(
		1,
		51.0,
		8.0,
		_ts("2025-10-18T08:00:00+02:00"),
		_ts("2025-10-18T18:00:00+02:00"),
		20.0,
		22000.0,
		0.1,
		100000.0,
		100000.0,
	)

	pro_vehicle = ProVehicle(pro)

	assert pro_vehicle.cab_recharge_power == 22000.0


def _candidate_with_metrics(
	cab_id: int,
	time_customer_in_vehicle_s: float,
	distance_vehicle_total_m: float,
	time_request_completion_s: float,
	time_customer_approach_s: float = 0.0,
	time_customer_in_vehicle_wait_s: float = 0.0,
	energy_inserted_consumption_wh: float = 0.0,
	distance_vehicle_powered_m: float | None = None,
	time_customer_excess_travel_s: float | None = None,
) -> NCCandidate:
	candidate = NCCandidate()
	candidate.cab = SimpleNamespace(cab=SimpleNamespace(id=cab_id))
	candidate.metrics.time_customer_approach_s = time_customer_approach_s
	candidate.metrics.time_customer_in_vehicle_s = time_customer_in_vehicle_s
	candidate.metrics.time_customer_in_vehicle_wait_s = time_customer_in_vehicle_wait_s
	candidate.metrics.time_customer_wait_s = time_customer_in_vehicle_wait_s
	candidate.metrics.distance_vehicle_total_m = distance_vehicle_total_m
	candidate.metrics.distance_vehicle_powered_m = (
		distance_vehicle_total_m
		if distance_vehicle_powered_m is None else distance_vehicle_powered_m
	)
	candidate.metrics.time_request_completion_s = time_request_completion_s
	candidate.metrics.energy_inserted_consumption_wh = energy_inserted_consumption_wh
	if time_customer_excess_travel_s is not None:
		candidate.metrics.time_customer_excess_travel_s = time_customer_excess_travel_s
	return candidate


def test_candidate_selection_scores_configured_metric_weighted_sum():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {
		"candidate_selection": {
			"scoring": {
				"strategy": "weighted_sum",
				"objectives": [
					{
						"metric": "time_customer_in_vehicle_s",
						"weight": 1.0,
						"direction": "min",
						"scale": 60.0,
					},
					{
						"metric": "distance_vehicle_powered_m",
						"weight": 1.0,
						"direction": "min",
						"scale": 1000.0,
					},
				],
			}
		}
	}
	fast_but_far = _candidate_with_metrics(
		cab_id=1,
		time_customer_in_vehicle_s=300.0,
		distance_vehicle_total_m=10000.0,
		distance_vehicle_powered_m=10000.0,
		time_request_completion_s=300.0,
	)
	slow_but_near = _candidate_with_metrics(
		cab_id=2,
		time_customer_in_vehicle_s=600.0,
		distance_vehicle_total_m=1000.0,
		distance_vehicle_powered_m=1000.0,
		time_request_completion_s=600.0,
	)

	selected = sim._select_candidate_for_new_request([fast_but_far, slow_but_near])

	assert selected is slow_but_near
	assert slow_but_near.score == 11.0
	assert fast_but_far.score == 15.0
	assert "time_customer_in_vehicle_s" in slow_but_near.score_breakdown


def test_candidate_selection_without_objectives_keeps_old_completion_first_order():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {"candidate_selection": {"scoring": {"objectives": []}}}
	early_high_energy = _candidate_with_metrics(
		cab_id=1,
		time_customer_in_vehicle_s=100.0,
		distance_vehicle_total_m=1000.0,
		time_request_completion_s=100.0,
		energy_inserted_consumption_wh=100.0,
	)
	late_low_energy = _candidate_with_metrics(
		cab_id=2,
		time_customer_in_vehicle_s=50.0,
		distance_vehicle_total_m=500.0,
		time_request_completion_s=200.0,
		energy_inserted_consumption_wh=0.0,
	)
	equal_time_low_energy = _candidate_with_metrics(
		cab_id=3,
		time_customer_in_vehicle_s=80.0,
		distance_vehicle_total_m=800.0,
		time_request_completion_s=100.0,
		energy_inserted_consumption_wh=50.0,
	)

	selected = sim._select_candidate_for_new_request([
		early_high_energy,
		late_low_energy,
		equal_time_low_energy,
	])

	assert selected is equal_time_low_energy
	assert early_high_energy.score == 100.0
	assert late_low_energy.score == 200.0
	assert equal_time_low_energy.score == 100.0


def test_candidate_selection_can_score_nearest_customer_approach():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {
		"candidate_selection": {
			"scoring": {
				"strategy": "weighted_sum",
				"objectives": [
					{
						"metric": "time_customer_approach_s",
						"weight": 1.0,
						"direction": "min",
						"scale": 1.0,
					},
				],
			}
		}
	}
	near_late = _candidate_with_metrics(
		cab_id=1,
		time_customer_approach_s=120.0,
		time_customer_in_vehicle_s=300.0,
		distance_vehicle_total_m=1000.0,
		time_request_completion_s=600.0,
	)
	far_early = _candidate_with_metrics(
		cab_id=2,
		time_customer_approach_s=300.0,
		time_customer_in_vehicle_s=300.0,
		distance_vehicle_total_m=1000.0,
		time_request_completion_s=400.0,
	)

	selected = sim._select_candidate_for_new_request([near_late, far_early])

	assert selected is near_late
	assert near_late.score == 120.0
	assert far_early.score == 300.0


def test_candidate_selection_applies_customer_in_vehicle_wait_limit():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {
		"candidate_selection": {
			"admissibility": {
				"max_customer_in_vehicle_wait_s": 300.0,
			},
			"scoring": {
				"strategy": "weighted_sum",
				"objectives": [
					{
						"metric": "time_request_completion_s",
						"weight": 1.0,
						"direction": "min",
						"scale": 1.0,
					},
				],
			},
		}
	}
	early_with_long_wait = _candidate_with_metrics(
		cab_id=1,
		time_customer_in_vehicle_s=900.0,
		time_customer_in_vehicle_wait_s=600.0,
		distance_vehicle_total_m=1000.0,
		time_request_completion_s=100.0,
	)
	later_without_wait = _candidate_with_metrics(
		cab_id=2,
		time_customer_in_vehicle_s=300.0,
		time_customer_in_vehicle_wait_s=0.0,
		distance_vehicle_total_m=1000.0,
		time_request_completion_s=200.0,
	)

	selected = sim._select_candidate_for_new_request([
		early_with_long_wait,
		later_without_wait,
	])

	assert selected is later_without_wait
	assert early_with_long_wait.score is None
	assert later_without_wait.score == 200.0


def test_candidate_scoring_can_use_customer_excess_travel_time():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {
		"candidate_selection": {
			"scoring": {
				"strategy": "weighted_sum",
				"objectives": [
					{
						"metric": "time_customer_excess_travel_s",
						"weight": 1.0,
						"direction": "min",
						"scale": 1.0,
					},
				],
			}
		}
	}
	candidate = _candidate_with_metrics(
		cab_id=1,
		time_customer_in_vehicle_s=420.0,
		distance_vehicle_total_m=1000.0,
		time_request_completion_s=600.0,
		time_customer_excess_travel_s=120.0,
	)

	score = sim._score_candidate(candidate)

	assert score == 120.0
	assert candidate.score_breakdown["time_customer_excess_travel_s"]["value"] == 120.0


def test_candidate_metrics_derive_convoy_customer_times_from_entries():
	customer = Customer.from_model_fp(_build_request())
	pro = SimpleNamespace()
	metrics = CandidateMetrics()
	entries = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=40,
			_e_time=100,
			_s_charge=95.0,
			_e_charge=90.0,
			_distance_m=300.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.FM,
			_cust=customer,
			_s_time=100,
			_e_time=200,
			_serv_t=60,
			_s_charge=90.0,
			_e_charge=85.0,
			_distance_m=500.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_cust=customer,
			_pro=pro,
			_s_time=200,
			_e_time=300,
			_s_charge=85.0,
			_e_charge=85.0,
			_distance_m=1000.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_cust=customer,
			_s_time=480,
			_e_time=600,
			_serv_t=90,
			_s_charge=85.0,
			_e_charge=80.0,
			_distance_m=600.0,
		),
	]

	metrics.time_customer_direct_s = 320.0
	metrics.record_inserted_entries(entries, "convoy", customer)

	assert metrics.time_customer_in_vehicle_s == 500.0
	assert metrics.time_customer_excess_travel_s == 180.0
	assert metrics.time_customer_drive_s == 170.0
	assert metrics.time_customer_in_vehicle_wait_s == 180.0
	assert metrics.time_customer_wait_s == 180.0
	assert metrics.time_customer_wait_after_pt_s == 180.0
	assert metrics.time_customer_approach_s == 60.0
	assert metrics.distance_vehicle_total_m == 2400.0
	assert metrics.distance_vehicle_convoy_m == 1000.0
	assert metrics.distance_vehicle_powered_m == 1400.0
	assert metrics.value("time_customer_in_vehicle_s") == 500.0


def test_candidate_metrics_count_convoy_distance_by_pro_attachment_not_customer():
	customer = Customer.from_model_fp(_build_request())
	pro = SimpleNamespace()
	metrics = CandidateMetrics()
	entries = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=0,
			_e_time=10,
			_s_charge=40.0,
			_e_charge=39.0,
			_distance_m=100.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=pro,
			_s_time=10,
			_e_time=20,
			_s_charge=39.0,
			_e_charge=39.0,
			_distance_m=250.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CT,
			_cust=customer,
			_s_time=20,
			_e_time=80,
			_s_charge=39.0,
			_e_charge=34.0,
			_distance_m=500.0,
		),
	]

	metrics.time_customer_direct_s = 40.0
	metrics.record_inserted_entries(entries, "convoy", customer)

	assert metrics.distance_vehicle_customer_m == 500.0
	assert metrics.distance_vehicle_convoy_m == 250.0
	assert metrics.distance_vehicle_powered_m == 600.0
	assert metrics.time_customer_approach_s == 10.0


def test_candidate_metrics_use_replacement_delta_for_vehicle_costs():
	customer = Customer.from_model_fp(_build_request())
	metrics = CandidateMetrics()
	service_entries = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=0,
			_e_time=10,
			_s_charge=100.0,
			_e_charge=98.0,
			_distance_m=100.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CT,
			_cust=customer,
			_s_time=20,
			_e_time=50,
			_s_charge=98.0,
			_e_charge=90.0,
			_distance_m=300.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_s_time=80,
			_e_time=100,
			_s_charge=90.0,
			_e_charge=85.0,
			_distance_m=200.0,
		),
	]
	new_replacement_entries = service_entries + [
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_s_time=100,
			_e_time=160,
			_s_charge=85.0,
			_e_charge=120.0,
			_distance_m=0.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=170,
			_e_time=200,
			_s_charge=120.0,
			_e_charge=110.0,
			_distance_m=400.0,
		),
	]
	old_replacement_entries = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_s_time=90,
			_e_time=100,
			_s_charge=100.0,
			_e_charge=96.0,
			_distance_m=150.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_s_time=100,
			_e_time=160,
			_s_charge=96.0,
			_e_charge=130.0,
			_distance_m=0.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=180,
			_e_time=200,
			_s_charge=130.0,
			_e_charge=124.0,
			_distance_m=250.0,
		),
	]

	metrics.time_customer_direct_s = 25.0
	metrics.record_inserted_entries(
		service_entries,
		"pure",
		customer,
		old_replacement_entries=old_replacement_entries,
		new_replacement_entries=new_replacement_entries,
	)

	assert metrics.distance_vehicle_total_m == 600.0
	assert metrics.delta_distance_vehicle_total_m == 600.0
	assert metrics.distance_vehicle_customer_m == 300.0
	assert metrics.distance_vehicle_empty_m == 300.0
	assert metrics.delta_distance_vehicle_empty_m == 300.0
	assert metrics.time_inserted_entries_s == 60.0
	assert metrics.delta_time_vehicle_active_entries_s == 60.0
	assert metrics.time_vehicle_inserted_span_s == 90.0
	assert metrics.delta_time_vehicle_occupied_s == 90.0
	assert metrics.energy_inserted_consumption_wh == 15.0
	assert metrics.delta_energy_consumption_wh == 15.0
	assert metrics.structure_num_chp == 0
	assert metrics.time_customer_in_vehicle_s == 30.0


def test_candidate_metrics_trailing_span_excludes_idle_before_final_approach():
	customer = Customer.from_model_fp(_build_request())
	metrics = CandidateMetrics()
	service_entries = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=0,
			_e_time=10,
			_s_charge=100.0,
			_e_charge=98.0,
			_distance_m=100.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CT,
			_cust=customer,
			_s_time=10,
			_e_time=40,
			_s_charge=98.0,
			_e_charge=90.0,
			_distance_m=300.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_s_time=900,
			_e_time=1000,
			_s_charge=90.0,
			_e_charge=80.0,
			_distance_m=500.0,
		),
	]
	old_replacement_entries = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_s_time=950,
			_e_time=1000,
			_s_charge=100.0,
			_e_charge=95.0,
			_distance_m=250.0,
		),
	]

	metrics.time_customer_direct_s = 50.0
	metrics.record_inserted_entries(
		service_entries,
		"pure",
		customer,
		old_replacement_entries=old_replacement_entries,
		new_replacement_entries=service_entries,
		is_trailing_replacement=True,
	)

	assert metrics.time_inserted_entries_s == 90.0
	assert metrics.delta_time_vehicle_active_entries_s == 90.0
	assert metrics.time_vehicle_inserted_span_s == 90.0
	assert metrics.delta_time_vehicle_occupied_s == 90.0
	assert metrics.time_vehicle_wait_s == 0.0
	assert metrics.delta_time_vehicle_wait_s == 0.0
	assert metrics.distance_vehicle_total_m == 650.0
	assert metrics.delta_distance_vehicle_total_m == 650.0
	assert metrics.energy_inserted_consumption_wh == 15.0
	assert metrics.delta_energy_consumption_wh == 15.0


def test_energy_feasibility_applies_pt_charging_after_baseline_suffix_materialization():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {"algorithm": {
		"energy_tolerance_wh": 1e-6,
	}}
	sim.sim_charging_stations = [SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))]
	cab = SimpleNamespace(
		charge_lb=0.0,
		charge_ub=100.0,
		schedule=[],
		current_charge=80.0,
		cab=SimpleNamespace(id=3),
	)
	candidate = NCCandidate()
	candidate.mode = "convoy"
	candidate.cab_insert_position_left = -1
	candidate.cab_insert_position_right = 3
	pro = SimpleNamespace()
	candidate.new_cab_schedule = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=0,
			_e_time=10,
			_s_charge=80.0,
			_e_charge=70.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=pro,
			_s_time=10,
			_e_time=20,
			_s_charge=70.0,
			_e_charge=70.0,
			_charge_amount=0.0,
			_max_charge_amount=20.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_s_time=20,
			_e_time=30,
			_s_charge=70.0,
			_e_charge=60.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_s_time=40,
			_e_time=50,
			_s_charge=40.0,
			_e_charge=75.0,
			_charge_amount=35.0,
			_max_charge_amount=35.0,
		),
	]

	feasible = sim._find_energy_feasible_candidates(cab, [candidate])

	assert feasible == [candidate]
	pt_entry = candidate.new_cab_schedule[1]
	lm_entry = candidate.new_cab_schedule[2]
	chp_entry = candidate.new_cab_schedule[3]
	assert pt_entry.charge_amount == 5.0
	assert pt_entry.end_charge == 75.0
	assert lm_entry.start_charge == 75.0
	assert lm_entry.end_charge == 65.0
	assert chp_entry.start_charge == 65.0
	assert chp_entry.end_charge == 100.0
	assert candidate.metrics.energy_convoy_charge_wh == 5.0
	assert candidate.metrics.time_stationary_charging_equivalent_s == 5.0
	assert candidate.metrics.energy_final_wh == 100.0
	assert candidate.energy_stats.is_feasible is True


def test_pt_charging_decision_handles_multiple_pt_entries_in_one_candidate():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {"algorithm": {
		"energy_tolerance_wh": 1e-6,
	}}
	sim.sim_charging_stations = []
	cab = SimpleNamespace(
		charge_lb=0.0,
		charge_ub=100.0,
		schedule=[],
		current_charge=90.0,
		cab=SimpleNamespace(id=4),
	)
	candidate = NCCandidate()
	candidate.mode = "convoy"
	candidate.cab_insert_position_left = -1
	candidate.cab_insert_position_right = 5
	pro = SimpleNamespace()
	candidate.new_cab_schedule = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=0,
			_e_time=10,
			_s_charge=90.0,
			_e_charge=80.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=pro,
			_s_time=10,
			_e_time=20,
			_s_charge=80.0,
			_e_charge=80.0,
			_charge_amount=0.0,
			_max_charge_amount=5.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_s_time=20,
			_e_time=30,
			_s_charge=80.0,
			_e_charge=70.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=pro,
			_s_time=30,
			_e_time=40,
			_s_charge=70.0,
			_e_charge=70.0,
			_charge_amount=0.0,
			_max_charge_amount=20.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_s_time=40,
			_e_time=50,
			_s_charge=70.0,
			_e_charge=60.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_s_time=60,
			_e_time=70,
			_s_charge=40.0,
			_e_charge=60.0,
			_charge_amount=20.0,
			_max_charge_amount=20.0,
		),
	]

	feasible = sim._find_energy_feasible_candidates(cab, [candidate])

	assert feasible == [candidate]
	first_pt = candidate.new_cab_schedule[1]
	second_pt = candidate.new_cab_schedule[3]
	chp_entry = candidate.new_cab_schedule[5]
	assert first_pt.charge_amount == 5.0
	assert second_pt.charge_amount == 15.0
	assert first_pt.end_charge == 85.0
	assert second_pt.start_charge == 75.0
	assert second_pt.end_charge == 90.0
	assert chp_entry.start_charge == 80.0
	assert chp_entry.end_charge == 100.0
	assert candidate.metrics.energy_convoy_charge_wh == 20.0
	assert candidate.energy_stats.is_feasible is True


def test_pt_charging_requires_explicit_max_charge_bookkeeping():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(charge_ub=100.0, cab=SimpleNamespace(id=4))
	candidate = NCCandidate()
	candidate.mode = "convoy"
	candidate.cab_insert_position_left = -1
	candidate.cab_insert_position_right = 1
	candidate.new_cab_schedule = [
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_s_time=10,
			_e_time=20,
			_s_charge=70.0,
			_e_charge=70.0,
			_charge_amount=0.0,
		),
	]

	try:
		sim._apply_convoy_pt_charging(cab, candidate)
	except RuntimeError as exc:
		assert "Missing max_charge_amount" in str(exc)
	else:
		raise AssertionError("Expected missing PT max_charge_amount to fail loudly")


def test_charging_replanning_transfers_actual_chp_charge_without_shortening_blocker():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_ub=100.0,
		current_charge=0.0,
		consumption=1.0,
		speed=1.0,
		cab=SimpleNamespace(id=7),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=10,
			_e_time=20,
			_s_charge=40.0,
			_e_charge=40.0,
			_charge_amount=0.0,
			_max_charge_amount=10.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_s_time=20,
			_e_time=30,
			_s_charge=40.0,
			_e_charge=30.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=30.0,
			_e_charge=25.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=80,
			_serv_t=10,
			_s_charge=25.0,
			_e_charge=35.0,
			_charge_amount=10.0,
			_max_charge_amount=20.0,
		),
	]

	result = sim._replan_charging(cab, schedule)

	pt_entry, lm_entry, cha_entry, chp_entry = result
	assert pt_entry.charge_amount == 10.0
	assert pt_entry.end_charge == 50.0
	assert lm_entry.start_charge == 50.0
	assert lm_entry.end_charge == 40.0
	assert cha_entry.start_charge == 40.0
	assert cha_entry.end_charge == 35.0
	assert chp_entry.start_charge == 35.0
	assert chp_entry.end_charge == 35.0
	assert chp_entry.charge_amount == 0.0
	assert chp_entry.max_charge_amount == 20.0
	assert cha_entry.start_time == 40
	assert cha_entry.end_time == 50
	assert chp_entry.start_time == 50
	assert chp_entry.end_time == 80


def test_charging_replanning_shortens_chp_reserve_from_reachability_invariant():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_ub=100.0,
		current_charge=100.0,
		consumption=1.0,
		speed=1.0,
		cab=SimpleNamespace(id=8),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=0,
			_e_time=20,
			_serv_t=10,
			_s_charge=60.0,
			_e_charge=100.0,
			_charge_amount=40.0,
			_max_charge_amount=40.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=40,
			_e_time=50,
			_s_charge=70.0,
			_e_charge=70.0,
			_charge_amount=0.0,
			_max_charge_amount=30.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=90,
			_e_time=100,
			_s_charge=50.0,
			_e_charge=50.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=100,
			_e_time=220,
			_serv_t=20,
			_s_charge=50.0,
			_e_charge=50.0,
			_charge_amount=0.0,
			_max_charge_amount=100.0,
		),
	]

	result = sim._replan_charging(cab, schedule)

	assert result[-1].max_charge_amount == 75.0
	assert result[-2].start_time == 115
	assert result[-2].end_time == 125
	assert result[-1].start_time == 125
	assert result[-1].end_time == 220


def test_charging_replanning_deletes_tiny_zero_charge_chp_reserve():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 5,
			},
		},
	}
	cab = SimpleNamespace(cab=SimpleNamespace(id=8))
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=30.0,
			_e_charge=25.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=70,
			_serv_t=10,
			_s_charge=25.0,
			_e_charge=25.0,
			_charge_amount=0.0,
			_max_charge_amount=3.0,
		),
	]

	sim._apply_chp_reserve_reductions(cab, schedule, {1: 7.0})
	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.ChA]
	assert schedule[0].start_time == 60
	assert schedule[0].end_time == 70


def test_charging_replanning_deletes_zero_charge_chp_after_full_release():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 0,
			},
		},
	}
	cab = SimpleNamespace(cab=SimpleNamespace(id=8))
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=30.0,
			_e_charge=25.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=70,
			_serv_t=10,
			_s_charge=25.0,
			_e_charge=25.0,
			_charge_amount=0.0,
			_max_charge_amount=10.0,
		),
	]

	sim._apply_chp_reserve_reductions(cab, schedule, {1: 10.0})
	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.ChA]
	assert schedule[0].start_time == 60
	assert schedule[0].end_time == 70


def test_charging_replanning_deletes_exact_zero_chp_reserve_without_threshold():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 0,
			},
		},
	}
	cab = SimpleNamespace(cab=SimpleNamespace(id=8))
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=30.0,
			_e_charge=25.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=60,
			_serv_t=10,
			_s_charge=25.0,
			_e_charge=25.0,
			_charge_amount=0.0,
			_max_charge_amount=0.0,
		),
	]

	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.ChA]
	assert schedule[0].start_time == 50
	assert schedule[0].end_time == 60


def test_charging_replanning_deletes_existing_tiny_zero_charge_chp_reserve():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 5,
			},
		},
	}
	cab = SimpleNamespace(cab=SimpleNamespace(id=8))
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=30.0,
			_e_charge=25.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=65,
			_serv_t=10,
			_s_charge=25.0,
			_e_charge=25.0,
			_charge_amount=0.0,
			_max_charge_amount=5.0,
		),
	]

	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.ChA]
	assert schedule[0].start_time == 55
	assert schedule[0].end_time == 65


def test_charging_replanning_keeps_tiny_chp_if_glued_cha_already_started():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 45
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 5,
			},
		},
	}
	cab = SimpleNamespace(cab=SimpleNamespace(id=8))
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=30.0,
			_e_charge=25.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=65,
			_serv_t=10,
			_s_charge=25.0,
			_e_charge=25.0,
			_charge_amount=0.0,
			_max_charge_amount=5.0,
		),
	]

	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.ChA, CabEntryType.ChP]
	assert schedule[0].start_time == 40
	assert schedule[0].end_time == 50
	assert schedule[1].start_time == 50
	assert schedule[1].end_time == 65


def test_charging_replanning_deletes_future_cha_when_direct_bridge_is_energy_feasible():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 5,
			},
		},
	}
	route_calls = []

	def shortest_path(start, end):
		route_calls.append((start, end))
		return {"time_s": 12.0, "energy_wh": 12.0, "distance_m": 120.0, "query_time_s": 0.0, "proj_orig": (0.0, 0.0)}

	sim.router_cab = SimpleNamespace(shortest_path=shortest_path)
	cab = SimpleNamespace(
		charge_lb=0.0,
		charge_ub=100.0,
		cab=SimpleNamespace(id=8),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_lat=0.0,
			_s_lon=0.0,
			_e_lat=1.0,
			_e_lon=0.0,
			_s_time=40,
			_e_time=50,
			_s_charge=90.0,
			_e_charge=80.0,
			_distance_m=100.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_lat=1.0,
			_s_lon=0.0,
			_e_lat=1.0,
			_e_lon=0.0,
			_s_time=50,
			_e_time=65,
			_serv_t=10,
			_s_charge=80.0,
			_e_charge=80.0,
			_charge_amount=0.0,
			_max_charge_amount=5.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_lat=1.0,
			_s_lon=0.0,
			_e_lat=2.0,
			_e_lon=0.0,
			_s_time=70,
			_e_time=80,
			_s_charge=80.0,
			_e_charge=75.0,
			_distance_m=50.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CT,
			_s_lat=2.0,
			_s_lon=0.0,
			_e_lat=3.0,
			_e_lon=0.0,
			_s_time=80,
			_e_time=90,
			_s_charge=75.0,
			_e_charge=70.0,
			_distance_m=50.0,
		),
	]

	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.CA, CabEntryType.CT]
	assert route_calls == [((0.0, 0.0), (2.0, 0.0))]
	direct_entry = schedule[0]
	assert direct_entry.start_time == 68
	assert direct_entry.end_time == 80
	assert direct_entry.start_lat == 0.0
	assert direct_entry.start_lon == 0.0
	assert direct_entry.end_lat == 2.0
	assert direct_entry.end_lon == 0.0
	assert direct_entry.start_charge == 90.0
	assert direct_entry.end_charge == 78.0
	assert direct_entry.distance_m == 120.0
	assert schedule[1].start_charge == 78.0
	assert schedule[1].end_charge == 73.0


def test_charging_replanning_keeps_future_cha_when_direct_bridge_breaks_energy_bounds():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"charging_replanning": {
				"chp_deletion_active_duration_threshold_s": 5,
			},
		},
	}
	sim.router_cab = SimpleNamespace(
		shortest_path=lambda start, end: {
			"time_s": 12.0,
			"energy_wh": 95.0,
			"distance_m": 120.0,
			"query_time_s": 0.0,
		}
	)
	cab = SimpleNamespace(
		charge_lb=0.0,
		charge_ub=100.0,
		cab=SimpleNamespace(id=8),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_lat=0.0,
			_s_lon=0.0,
			_e_lat=1.0,
			_e_lon=0.0,
			_s_time=40,
			_e_time=50,
			_s_charge=90.0,
			_e_charge=80.0,
			_distance_m=100.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_lat=1.0,
			_s_lon=0.0,
			_e_lat=1.0,
			_e_lon=0.0,
			_s_time=50,
			_e_time=65,
			_serv_t=10,
			_s_charge=80.0,
			_e_charge=80.0,
			_charge_amount=0.0,
			_max_charge_amount=5.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_lat=1.0,
			_s_lon=0.0,
			_e_lat=2.0,
			_e_lon=0.0,
			_s_time=70,
			_e_time=80,
			_s_charge=80.0,
			_e_charge=75.0,
			_distance_m=50.0,
		),
	]

	sim._prune_redundant_future_charging_blocks(cab, schedule)

	assert [entry.type for entry in schedule] == [CabEntryType.ChA, CabEntryType.CA]
	assert schedule[0].start_time == 55
	assert schedule[0].end_time == 65
	assert schedule[1].start_time == 70
	assert schedule[1].end_time == 80
	assert schedule[1].start_charge == 80.0
	assert schedule[1].end_charge == 75.0


def test_charging_replanning_shortens_first_future_chp_from_current_state():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {"algorithm": {
		"energy_tolerance_wh": 1e-6,
		"charge_restore_fraction": 0.8,
	}}
	cab = SimpleNamespace(
		charge_ub=100.0,
		current_charge=100.0,
		consumption=1.0,
		speed=1.0,
		cab=SimpleNamespace(id=8),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=20,
			_e_time=30,
			_s_charge=90.0,
			_e_charge=90.0,
			_charge_amount=0.0,
			_max_charge_amount=10.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=90,
			_e_time=100,
			_s_charge=70.0,
			_e_charge=70.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=100,
			_e_time=220,
			_serv_t=20,
			_s_charge=70.0,
			_e_charge=70.0,
			_charge_amount=0.0,
			_max_charge_amount=100.0,
		),
	]

	result = sim._replan_charging(cab, schedule)

	assert result[-1].max_charge_amount == 78.88888888888889
	assert result[-2].start_time == 111
	assert result[-2].end_time == 121
	assert result[-1].start_time == 121
	assert result[-1].end_time == 220


def test_charging_replanning_keeps_charge_when_interval_has_no_upper_energy_room():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_ub=100.0,
		current_charge=0.0,
		consumption=1.0,
		speed=1.0,
		cab=SimpleNamespace(id=9),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=10,
			_e_time=20,
			_s_charge=95.0,
			_e_charge=95.0,
			_charge_amount=0.0,
			_max_charge_amount=10.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_s_time=20,
			_e_time=30,
			_s_charge=95.0,
			_e_charge=100.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=40,
			_e_time=50,
			_s_charge=100.0,
			_e_charge=90.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=50,
			_e_time=80,
			_serv_t=10,
			_s_charge=90.0,
			_e_charge=100.0,
			_charge_amount=10.0,
			_max_charge_amount=20.0,
		),
	]

	result = sim._replan_charging(cab, schedule)

	assert result[0].charge_amount == 0.0
	assert result[-1].charge_amount == 10.0
	assert result[-1].start_time == 50


def test_charging_replanning_propagates_multiple_pt_opportunities_between_chps():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_ub=100.0,
		current_charge=100.0,
		consumption=1.0,
		speed=1.0,
		cab=SimpleNamespace(id=10),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=0,
			_e_time=20,
			_serv_t=10,
			_s_charge=60.0,
			_e_charge=100.0,
			_charge_amount=40.0,
			_max_charge_amount=40.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=30,
			_e_time=50,
			_s_charge=70.0,
			_e_charge=70.0,
			_charge_amount=0.0,
			_max_charge_amount=20.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=70,
			_e_time=80,
			_s_charge=60.0,
			_e_charge=60.0,
			_charge_amount=0.0,
			_max_charge_amount=20.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=90,
			_e_time=100,
			_s_charge=60.0,
			_e_charge=60.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=100,
			_e_time=220,
			_serv_t=20,
			_s_charge=60.0,
			_e_charge=60.0,
			_charge_amount=0.0,
			_max_charge_amount=100.0,
		),
	]

	result = sim._replan_charging(cab, schedule)

	assert result[-1].max_charge_amount == 60.0
	assert result[-2].start_time == 130
	assert result[-2].end_time == 140
	assert result[-1].start_time == 140


def test_charging_replanning_keeps_reserve_when_pt_opportunity_does_not_cover_reachability():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.current_time = 0
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_ub=100.0,
		current_charge=100.0,
		consumption=1.0,
		speed=1.0,
		cab=SimpleNamespace(id=11),
	)
	station = SimpleNamespace(station=SimpleNamespace(max_supply=3600.0))
	schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=0,
			_e_time=20,
			_serv_t=10,
			_s_charge=60.0,
			_e_charge=100.0,
			_charge_amount=40.0,
			_max_charge_amount=40.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=SimpleNamespace(),
			_s_time=30,
			_e_time=40,
			_s_charge=90.0,
			_e_charge=90.0,
			_charge_amount=0.0,
			_max_charge_amount=8.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChA,
			_charging_s=station,
			_s_time=190,
			_e_time=200,
			_s_charge=58.0,
			_e_charge=58.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_charging_s=station,
			_s_time=200,
			_e_time=230,
			_serv_t=10,
			_s_charge=0.0,
			_e_charge=0.0,
			_charge_amount=0.0,
			_max_charge_amount=8.0,
		),
	]

	result = sim._replan_charging(cab, schedule)

	assert result[-1].max_charge_amount == 8.0
	assert result[-2].start_time == 190
	assert result[-1].start_time == 200


def test_overcharge_repair_reduces_pt_charge():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_lb=0.0,
		charge_ub=100.0,
		cab=SimpleNamespace(id=5),
	)
	candidate = NCCandidate()
	pro = SimpleNamespace()
	candidate.new_cab_schedule = [
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=0,
			_e_time=10,
			_s_charge=90.0,
			_e_charge=90.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.PT,
			_pro=pro,
			_s_time=10,
			_e_time=20,
			_s_charge=90.0,
			_e_charge=110.0,
			_charge_amount=20.0,
			_max_charge_amount=20.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.LM,
			_s_time=20,
			_e_time=30,
			_s_charge=110.0,
			_e_charge=100.0,
		),
	]
	candidate.energy_stats = sim._compute_schedule_energy_stats(
		candidate.new_cab_schedule,
		cab.charge_lb,
		cab.charge_ub,
		-1,
		len(candidate.new_cab_schedule),
		0.0,
	)

	sim._try_energy_repair(cab, candidate)

	pt_entry = candidate.new_cab_schedule[1]
	lm_entry = candidate.new_cab_schedule[2]
	assert candidate.energy_stats.is_feasible is True
	assert pt_entry.charge_amount == 10.0
	assert pt_entry.end_charge == 100.0
	assert lm_entry.start_charge == 100.0
	assert lm_entry.end_charge == 90.0


def test_undercharge_repair_walks_back_past_charge_entry_without_room():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {"algorithm": {"energy_tolerance_wh": 1e-6}}
	cab = SimpleNamespace(
		charge_lb=0.0,
		charge_ub=100.0,
		cab=SimpleNamespace(id=6),
	)
	candidate = NCCandidate()
	candidate.new_cab_schedule = [
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_s_time=0,
			_e_time=10,
			_s_charge=50.0,
			_e_charge=60.0,
			_charge_amount=10.0,
			_max_charge_amount=30.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=10,
			_e_time=20,
			_s_charge=60.0,
			_e_charge=40.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.ChP,
			_s_time=20,
			_e_time=30,
			_s_charge=40.0,
			_e_charge=40.0,
			_charge_amount=0.0,
			_max_charge_amount=0.0,
		),
		CabScheduleEntry(
			_type=CabEntryType.CA,
			_s_time=30,
			_e_time=40,
			_s_charge=40.0,
			_e_charge=-10.0,
		),
	]
	candidate.energy_stats = sim._compute_schedule_energy_stats(
		candidate.new_cab_schedule,
		cab.charge_lb,
		cab.charge_ub,
		-1,
		len(candidate.new_cab_schedule),
		0.0,
	)

	sim._try_energy_repair(cab, candidate)

	first_chp = candidate.new_cab_schedule[0]
	nearest_chp = candidate.new_cab_schedule[2]
	last_entry = candidate.new_cab_schedule[3]
	assert candidate.energy_stats.is_feasible is True
	assert first_chp.charge_amount == 20.0
	assert first_chp.end_charge == 70.0
	assert nearest_chp.charge_amount == 0.0
	assert nearest_chp.start_charge == 50.0
	assert nearest_chp.end_charge == 50.0
	assert last_entry.start_charge == 50.0
	assert last_entry.end_charge == 0.0


def test_energy_repair_can_iterate_after_successful_partial_repair():
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.runtime_diagnostics = RuntimeDiagnostics()
	sim.parameters = {
		"algorithm": {
			"energy_tolerance_wh": 1e-6,
			"max_energy_repair_iterations": 2,
		},
	}
	cab = SimpleNamespace(cab=SimpleNamespace(id=12))
	candidate = NCCandidate()
	candidate.new_cab_schedule = [
		CabScheduleEntry(_type=CabEntryType.ChP, _s_time=0, _e_time=10),
	]
	candidate.energy_stats = SimpleNamespace(
		is_feasible=False,
		first_violation_type="under",
		first_violation_time=10,
		first_violation_index=0,
		worst_under_amount=5.0,
		worst_over_amount=0.0,
	)
	plan_calls = []

	def plan_repair(_cab, _candidate, violation_type):
		plan_calls.append(violation_type)
		return {0: 1.0}, True

	def apply_repair(_cab, _candidate, _planned_adjustments):
		if len(plan_calls) == 1:
			return SimpleNamespace(
				is_feasible=False,
				first_violation_type="over",
				first_violation_time=20,
				first_violation_index=0,
				worst_under_amount=0.0,
				worst_over_amount=3.0,
			)
		return SimpleNamespace(is_feasible=True)

	sim._plan_repair_charge_adjustments = plan_repair
	sim._apply_repair_charge_adjustments = apply_repair

	sim._try_energy_repair(cab, candidate)

	assert candidate.energy_stats.is_feasible is True
	assert plan_calls == ["under", "over"]
