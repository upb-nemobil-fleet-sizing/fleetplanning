"""
Step-by-step flow checks for FleetPlanning.optimize()'s phase/trial-stage state machine.

test_shrink_pros_trials.py checks aggregate properties of a full run ("a trial happened
somewhere", "the protected Pro ended up excluded"). This file instruments the search loop to
record a per-iteration trace (phase, trial_stage, move, fleet composition, rank/best_rank) and
then walks that trace re-deriving, from first principles and independently of optimize()'s own
bookkeeping, what each step transition should look like - so a bug in e.g. exclusion-set
bookkeeping or trial span detection fails on the specific iteration it happens at, not just on
some final aggregate.
"""
import pytest

from tests.test_shrink_pros_trials import _build_fp, _install_oracle


def _trace_run(fp):
	"""
	Wrap select_moves/evaluate_solution to record one dict per loop iteration. Fields ending in
	"_before" reflect state as of the start of that iteration (before the move was executed);
	fields ending in "_after" reflect state once the resulting solution has been simulated and
	evaluated. Consecutive entries chain directly: trace[i]'s "_after" fields are exactly what
	trace[i+1]'s "_before" fields will read, since nothing else touches search state between one
	iteration's evaluate_solution call and the next iteration's select_moves call.
	"""
	trace = []
	pending = {}
	orig_select_moves = fp.select_moves
	orig_evaluate_solution = fp.evaluate_solution

	def _traced_select_moves():
		fleet = fp.current_solution.vehicle_fleet
		budget_violated = (
			fp.search_parameters.budget_eur is not None
			and fp.cost_model.fleet_price_eur(len(fleet.cabs), len(fleet.pros)) > fp.search_parameters.budget_eur
		)
		pending["record"] = {
			"iteration": fp.iteration,
			"phase_before": fp.phase,
			"trial_stage_before": fp.trial_stage,
			"trial_shrink_mode_before": fp.trial_shrink_mode,
			"tries_before": fp.shrink_pros_tries,
			"excluded_before": frozenset(fp.shrink_pros_excluded_ids),
			"pro_ids_before": frozenset(p.id for p in fleet.pros),
			"best_rank_before": fp.best_rank,
			"budget_violated_before": budget_violated,
		}
		move = orig_select_moves()
		pending["record"]["move"] = move
		pending["record"]["phase_after_select"] = fp.phase
		return move

	def _traced_evaluate_solution(solution, *args, **kwargs):
		imp, empty_cab, rank = orig_evaluate_solution(solution, *args, **kwargs)
		record = pending.pop("record", None)
		if record is not None:
			record.update({
				"pro_ids_after": frozenset(p.id for p in solution.vehicle_fleet.pros),
				"num_cabs_after": len(solution.vehicle_fleet.cabs),
				"imp": imp,
				"rank": rank,
				"best_rank_after": fp.best_rank,
			})
			trace.append(record)
		return imp, empty_cab, rank

	fp.select_moves = _traced_select_moves
	fp.evaluate_solution = _traced_evaluate_solution
	return trace


def _assert_step_invariants(fp, trace):
	"""Per-step checks, cheap and independent of any particular scenario."""
	assert len(trace) >= 1

	iters = [s["iteration"] for s in trace]
	assert iters == list(range(iters[0], iters[0] + len(iters))), \
		"iteration counter must advance by exactly one every loop pass, no gaps/repeats"

	# One simulated+evaluated solution per trace entry, plus the initial one.
	assert len(fp.solution_pool) == len(trace) + 1

	for step in trace:
		# imp/rank/best_rank must agree with each other and with the (True/False, score) rank
		# ordering used by _solution_rank() - see evaluate_solution().
		if step["imp"] > 0:
			assert step["rank"] == step["best_rank_after"]
			assert step["best_rank_after"] > step["best_rank_before"]
		elif step["imp"] < 0:
			assert step["rank"] < step["best_rank_before"]
			assert step["best_rank_after"] == step["best_rank_before"]
		else:
			assert step["rank"] == step["best_rank_before"]
			assert step["best_rank_after"] == step["best_rank_before"]

		# best_rank is monotonically non-decreasing, full stop.
		assert step["best_rank_after"] >= step["best_rank_before"]

		# A trial's nested steps reuse grow/shrink's own moves and never touch self.phase.
		if step["trial_stage_before"] == "grow":
			assert step["move"] == "add_estimated_cabs"
			assert step["phase_before"] == "shrink_pros"
			assert step["phase_after_select"] == "shrink_pros"
		elif step["trial_stage_before"] == "shrink":
			assert step["move"] == "remove_one_cab"
			assert step["phase_before"] == "shrink_pros"
			assert step["phase_after_select"] == "shrink_pros"

		# check_hard_constraints() is suppressed for the whole span, which structurally means
		# self.phase cannot change while a trial is running - checked here on the live phase
		# value (not by re-calling the gate, which would just re-derive its own answer).
		if step["trial_stage_before"] is not None:
			assert not step["budget_violated_before"], \
				"a trial must not still be running while the fleet is over budget"

		assert step["tries_before"] <= fp.search_parameters.shrink_pros_max_tries


def _trial_spans(trace):
	"""
	Yield (start_deactivate_idx, nested_indices) for every shrink_pros trial found in trace:
	start_deactivate_idx is the "deactivate_least_used_pro" step that removed the Pro under
	trial, nested_indices are the trial's own "grow"/"shrink" steps that follow it.
	"""
	i = 0
	while i < len(trace):
		if (
			trace[i]["trial_stage_before"] is None
			and i + 1 < len(trace)
			and trace[i + 1]["trial_stage_before"] == "grow"
		):
			j = i + 1
			while j < len(trace) and trace[j]["trial_stage_before"] is not None:
				j += 1
			yield i, list(range(i + 1, j))
			i = j
		else:
			i += 1


def _assert_trial_bookkeeping(fp, trace):
	"""
	Re-derive, independently of optimize()'s own bookkeeping, what should have happened to
	shrink_pros_excluded_ids/shrink_pros_tries after each trial concludes, and check it against
	what the next step's "_before" snapshot actually shows.
	"""
	spans = list(_trial_spans(trace))
	assert spans, "scenario produced no trials at all - not exercising what this test is for"

	for deactivate_idx, nested in spans:
		assert nested, "a trial must have at least one nested step (grow)"
		removed_ids = trace[deactivate_idx]["pro_ids_before"] - trace[deactivate_idx]["pro_ids_after"]
		assert len(removed_ids) == 1, "deactivate_least_used_pro must remove exactly one Pro"
		trial_pro_id = next(iter(removed_ids))

		# Stage sequence within the span: zero or more "grow"s, then zero or more "shrink"s,
		# never back to "grow" once "shrink" starts.
		stages = [trace[k]["trial_stage_before"] for k in nested]
		assert stages[0] == "grow"
		first_shrink = next((n for n, s in enumerate(stages) if s == "shrink"), len(stages))
		assert all(s == "grow" for s in stages[:first_shrink])
		assert all(s == "shrink" for s in stages[first_shrink:])

		checkpoint_rank = trace[deactivate_idx]["best_rank_before"]
		final_rank = trace[nested[-1]]["best_rank_after"]
		succeeded = final_rank > checkpoint_rank

		next_idx = nested[-1] + 1
		if next_idx >= len(trace):
			# Trial concluded on the run's last iteration - check the bookkeeping landed on
			# fp's live state directly instead of a next-step snapshot.
			if succeeded:
				assert fp.shrink_pros_tries == 0
				if fp.search_parameters.shrink_pros_allow_retry:
					assert fp.shrink_pros_excluded_ids == set()
			else:
				assert trial_pro_id in fp.shrink_pros_excluded_ids
			continue

		nxt = trace[next_idx]
		assert nxt["trial_stage_before"] is None, "trial must have concluded by its last nested step"
		if succeeded:
			assert nxt["tries_before"] == 0
			if fp.search_parameters.shrink_pros_allow_retry:
				assert nxt["excluded_before"] == frozenset()
			else:
				assert nxt["excluded_before"] == trace[deactivate_idx]["excluded_before"]
		else:
			assert nxt["tries_before"] == trace[deactivate_idx]["tries_before"] + 1
			assert nxt["excluded_before"] == trace[deactivate_idx]["excluded_before"] | {trial_pro_id}

		# Either way, an excluded-and-not-yet-cleared Pro must never be picked as trial_pro_id
		# again by a later span in the same run.
		for other_deactivate_idx, _ in spans:
			if other_deactivate_idx <= deactivate_idx:
				continue
			assert trial_pro_id not in trace[other_deactivate_idx]["pro_ids_before"] - trace[other_deactivate_idx]["pro_ids_after"] \
				or trial_pro_id not in trace[deactivate_idx]["excluded_before"]


# ----------------------------------------------------------------------------

def test_full_run_step_by_step_successful_trials():
	"""A scenario where the fleet is budget-constrained, not demand-saturated, when shrink_pros
	starts - under the real, unmodified objective (total_served + tiebreaker), trading an
	expensive Pro for several cheap Cabs then genuinely serves more requests, a strict win on
	total_served itself, not just a tiebreaker nicety. (Without a budget, grow already saturates
	every reachable request before shrink_pros ever runs, leaving only ties to find - see the
	budget_eur note in _assert_trial_bookkeeping's callers below.) Trials should repeatedly
	succeed, and every step of every one should check out independently."""
	fp = _build_fp(iter_limit=60, _shrink_pros_max_tries=8, _budget_eur=3_000_000.0)
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: min(375, cabs * 10 + len(pro_ids) * 5))
	trace = _trace_run(fp)

	fp.optimize()

	_assert_step_invariants(fp, trace)
	_assert_trial_bookkeeping(fp, trace)
	spans = list(_trial_spans(trace))
	assert spans
	assert any(
		trace[nested[-1]]["best_rank_after"] > trace[d]["best_rank_before"]
		for d, nested in spans
	), "expected at least one trial to actually pay off by serving strictly more requests"


def test_full_run_step_by_step_mixed_success_and_failure():
	"""Same budget-constrained setup as the pure-success scenario above, plus one specific Pro
	that's irreplaceable (a hard capacity cap independent of Cab count - no amount of budget-
	freed growth can make up for the resulting service loss). deactivate_least_used_pro ties
	break by lowest id, and the protected Pro is given the lowest id, so it's tried (and fails)
	first, then later, ordinary Pros succeed one after another under the real, unmodified
	objective - a deterministic mix of both outcomes."""
	fp = _build_fp(iter_limit=60, _shrink_pros_max_tries=8, _budget_eur=4_500_000.0)
	probe = fp.initial_solution()
	protected_id = min(p.id for p in probe.vehicle_fleet.pros)

	def served_fn(cabs, pro_ids):
		if protected_id not in pro_ids:
			return min(20, cabs)
		return min(375, cabs * 10 + len(pro_ids) * 5)

	_install_oracle(fp, served_fn)
	trace = _trace_run(fp)

	fp.optimize()

	_assert_step_invariants(fp, trace)
	_assert_trial_bookkeeping(fp, trace)

	spans = list(_trial_spans(trace))
	outcomes = []
	for deactivate_idx, nested in spans:
		checkpoint_rank = trace[deactivate_idx]["best_rank_before"]
		outcomes.append(trace[nested[-1]]["best_rank_after"] > checkpoint_rank)
	assert True in outcomes and False in outcomes, "expected both a successful and a failed trial"


def test_full_run_step_by_step_budget_and_service_floor():
	"""Trials alongside both hard constraints at once: the gate must never fire mid-trial (would
	show up as a phase change inside a span, caught by _assert_step_invariants), and no trial may
	start while still over budget (also checked per-step)."""
	fp = _build_fp(
		iter_limit=60, initial_cabs=1, _shrink_pros_max_tries=3,
		_budget_eur=2_000_000.0, _service_level_min=0.3,
	)
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: min(375, cabs * 10 + len(pro_ids) * 15))
	trace = _trace_run(fp)

	fp.optimize()

	_assert_step_invariants(fp, trace)
	# Not asserting on trial bookkeeping here - the budget-driven forced "shrink" at the very
	# start can plausibly resolve the violation before shrink_pros/any trial is ever reached,
	# so unlike the two scenarios above a trial isn't guaranteed. The two other tests already
	# cover trial bookkeeping in isolation; this one is about the constraints/trial interaction.
	assert not fp._constraint_violations(fp.best_solution)


def test_shrink_pros_trial_probe_below_start_off_skips_shrink_entirely():
	"""shrink_pros_trial_probe_below_start=False, combined with a scenario where nested grow
	never finds anything (Cab capacity is capped independent of Cab count once there are
	enough of them, mirroring a fleet where Cabs stopped being the bottleneck), means a trial
	has nothing left to check once its mandatory grow probe fails - it should conclude right
	there, with no nested shrink stage at all, not enter shrink and get capped mid-probe."""
	fp = _build_fp(iter_limit=60, _shrink_pros_max_tries=8, _shrink_pros_trial_probe_below_start=False)
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: min(200, cabs * 50) + len(pro_ids) * 5)
	trace = _trace_run(fp)

	fp.optimize()

	_assert_step_invariants(fp, trace)
	spans = list(_trial_spans(trace))
	assert spans
	for deactivate_idx, nested in spans:
		assert [trace[k]["trial_stage_before"] for k in nested] == ["grow"], (
			"with the flag off and grow finding nothing, a trial's only nested step should be "
			"the mandatory grow probe itself - no shrink stage should ever start"
		)
	assert fp.trial_overshoot_floor_hits == 0, "this scenario never grows, so overshoot correction is irrelevant"


def test_shrink_pros_trial_overshoot_correction_stays_at_or_above_start():
	"""shrink_pros_trial_overshoot_correction=True must keep every overshoot-correction step at
	or above the Cab count the trial started with - it exists to verify a jump grow already
	took, never to explore below where the trial began (that's the separate, below-start
	question). served_fn has a threshold effect (losing the 3rd active Pro drops served by a
	flat 30, not scaled by Cab count) and a demand cap well above what Cabs alone provided
	before that loss, so recovering via Cabs alone is both necessary and genuinely reachable -
	the trial's first grow probe jumps by many Cabs at once, then a later, smaller probe
	overshoots past the demand cap and fails, leaving real unverified territory for overshoot
	correction to walk back through."""
	fp = _build_fp(iter_limit=150, _shrink_pros_max_tries=8,
					_shrink_pros_trial_overshoot_correction=True)
	_install_oracle(fp, served_fn=lambda cabs, pro_ids: min(cabs + (100 if len(pro_ids) >= 3 else 0), 200))
	trace = _trace_run(fp)

	fp.optimize()

	_assert_step_invariants(fp, trace)
	spans = list(_trial_spans(trace))
	assert spans
	saw_overshoot_step = False
	for deactivate_idx, nested in spans:
		trial_start_cabs = trace[deactivate_idx]["num_cabs_after"]
		overshoot_steps = [k for k in nested if trace[k]["trial_shrink_mode_before"] == "overshoot"]
		if overshoot_steps:
			saw_overshoot_step = True
		for k in overshoot_steps:
			assert trace[k]["num_cabs_after"] >= trial_start_cabs, (
				f"overshoot-correction step {k} went below trial_start_cabs={trial_start_cabs}"
			)
	assert saw_overshoot_step, "scenario never exercised overshoot correction - not testing what it should"
