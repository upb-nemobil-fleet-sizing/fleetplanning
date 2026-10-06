from __future__ import annotations

import os, re, json, io, sys, subprocess, shutil, threading, hashlib, tempfile, signal
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, Response
import hmac
from datetime import datetime
from functools import wraps
from urllib.parse import urlencode
from werkzeug.utils import secure_filename
from shapely.geometry import Point, Polygon
from export_utils import write_api_base_file_from_loaded_data, write_api_ride_file_from_loaded_data
from output_utils import parse_output_cab_json, parse_output_sim_json, parse_output_pro_json, build_output_payload
from output_details_utils import build_gantt_payload, build_vehicle_payload, clear_route_caches
import maptiles_manager
from movement_visualization import build_movement_html

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from models import CostModel
FRONTEND_ROOT = os.path.abspath(os.path.dirname(__file__))
FLEETPLANNING_OUTPUT_ROOT = os.path.abspath(os.environ.get("FLEETPLANNING_OUTPUT_ROOT", PROJECT_ROOT))
# Optional deployment override: force every new job's output into one fixed setup folder,
# regardless of search mode or other settings. Deliberately opt-in only (None when unset, not a
# baked-in default - see _build_sim_folder_name) - a deployer who sets this is choosing to
# collapse every setup's output back into one folder, on purpose, not by accident.
FLEETPLANNING_SETUP_NAME = os.environ.get("FLEETPLANNING_SETUP_NAME") or None
DEMO_MODE = os.environ.get("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
DEMO_LOADING_SECONDS = float(os.environ.get("DEMO_LOADING_SECONDS", "5"))
DEMO_SCENARIO_NAMES_FILE = os.path.join(FRONTEND_ROOT, "demo_scenario_names.txt")
DEMO_KPIS_FILE = os.path.join(FRONTEND_ROOT, "demo_kpis.txt")
DEMO_CHART_EXCLUSIONS_FILE = os.path.join(FRONTEND_ROOT, "demo_chart_exclusions.txt")
# Frontend-owned state, not backend output: unlike ALGORITHM_OUTPUT_ROOTS below, these are not
# repositioned by FLEETPLANNING_OUTPUT_ROOT or any other override - they live with this module,
# always, regardless of where a deployment points backend output.
UPLOAD_FOLDER = os.path.join(FRONTEND_ROOT, "uploads_temp")
SIM_RESULTS_ROOT = os.path.join(FRONTEND_ROOT, "sim_results")
DASHBOARD_JOB_STATE_ROOT = os.path.join(SIM_RESULTS_ROOT, "dashboard_jobs")


def _migrate_frontend_state_from_project_root() -> None:
    """One-time move of uploads_temp/sim_results out of the repo root (their old, pre-move
    location) into frontend/ (current). Both are frontend-specific state that never belonged
    anywhere else - see the ownership note above. Only acts when the old folder exists and the
    new one does not, so a fresh checkout (nothing to migrate) and an already-migrated one
    (nothing left at the old path) are both no-ops."""
    for legacy, current in (
        (os.path.join(PROJECT_ROOT, "uploads_temp"), UPLOAD_FOLDER),
        (os.path.join(PROJECT_ROOT, "sim_results"), SIM_RESULTS_ROOT),
    ):
        if os.path.isdir(legacy) and not os.path.isdir(current):
            try:
                shutil.move(legacy, current)
            except OSError:
                continue


_migrate_frontend_state_from_project_root()

ALGORITHM_OUTPUT_ROOTS = {
    "custom": os.path.join(FLEETPLANNING_OUTPUT_ROOT, "custom_sim", "output"),
    "rwapi": os.path.join(FLEETPLANNING_OUTPUT_ROOT, "rw", "output"),
    "sumo": os.path.join(FLEETPLANNING_OUTPUT_ROOT, "sumo", "output"),
}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SIM_RESULTS_ROOT, exist_ok=True)
os.makedirs(DASHBOARD_JOB_STATE_ROOT, exist_ok=True)
for _path in ALGORITHM_OUTPUT_ROOTS.values():
    os.makedirs(_path, exist_ok=True)
XML_PREPROCESS_DIR = os.path.join(os.path.dirname(__file__), "xml-pre-processing")
XML_PIPELINE_SCRIPT = os.path.join(XML_PREPROCESS_DIR, "run_preprocessing_pipeline.py")
SOLVER_RUNNER_SCRIPT = os.path.join(os.path.dirname(__file__), "solver_runner.py")
# Semaphores, not plain locks: the count is the real cap on concurrently *executing* processes
# (1 for now, in each pool). Solver/fixedfleet runs and XML conversions are separate pools - an
# XML conversion never waits behind a FleetPlanning run or vice versa. Raising either count later
# (to actually run more than one at a time) only ever means changing the number passed in here.
SOLVER_RUN_SEMAPHORE = threading.BoundedSemaphore(1)
XML_CONVERSION_RUN_SEMAPHORE = threading.BoundedSemaphore(1)
SIMULATION_JOB_LOCK = threading.RLock()
XML_UPLOAD_JOB_LOCK = threading.Lock()
_XML_UPLOAD_JOB_COUNTER = 0  # distinguishes job attempts within this process, no uuid needed

# job_id -> the live Popen for a solver job actually executing right now (i.e. holding
# SOLVER_RUN_SEMAPHORE) - lets a "Löschen" click reach in and kill the real process, not just
# the dashboard's tracking entry. Absent while a job is merely queued (waiting for the
# semaphore) - nothing to kill yet at that point, deleting the entry is enough.
_RUNNING_SOLVER_PROCESSES: dict[str, tuple[subprocess.Popen, bytes | None]] = {}
_RUNNING_PROCESSES_LOCK = threading.Lock()
# Same idea for the one XML conversion allowed to run at a time - a single slot is enough since
# a second XML upload is rejected outright while one is in flight (see /upload-ride-json), never
# queued behind it.
_RUNNING_XML_PROCESS: dict = {"popen": None, "identity": None}


def _process_identity(pid: int) -> bytes | None:
    """Read /proc/<pid>/cmdline as a lightweight, dependency-free fingerprint of what's actually
    running at a PID right now. PIDs (and the process-group IDs start_new_session=True derives
    from them) can in principle be recycled by the OS for an unrelated process in the gap
    between a liveness check and a kill signal - comparing the real command line immediately
    before killing closes that gap, without needing a third-party library like psutil. Returns
    None if the /proc entry doesn't exist (process already gone) or can't be read (non-Linux,
    permissions) - callers fall back to trusting Popen's own state when that happens, rather
    than refusing to kill anything at all on a platform without /proc."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read()
    except OSError:
        return None


def _kill_process_tree(popen: subprocess.Popen | None, expected_identity: bytes | None = None):
    """Terminate a tracked subprocess and its whole process group. A plain Popen.terminate()
    only reaches the top process - the XML pipeline wrapper spawns a further subprocess per
    stage, so killing just the wrapper would leave whichever stage is currently running alive.
    Launched with start_new_session=True so it and its children share one killable group, no
    other unrelated processes. SIGTERM first, SIGKILL if it hasn't exited shortly after. No-op
    if it already exited or was never started.

    `expected_identity` (from _process_identity() at spawn time) is re-checked right before each
    signal - if the PID no longer matches what we actually spawned (recycled for something
    else), this refuses to touch it instead of risking an unrelated process. Only enforced when
    an expected_identity was actually captured (i.e. /proc was available) - absent that, this
    falls back to the plain poll()-based check that existed before, unchanged."""
    if popen is None or popen.poll() is not None:
        return

    def identity_still_matches() -> bool:
        return expected_identity is None or _process_identity(popen.pid) == expected_identity

    try:
        pgid = os.getpgid(popen.pid)
    except ProcessLookupError:
        return
    if not identity_still_matches():
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        popen.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if not identity_still_matches():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        popen.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

app = Flask(__name__)

# If TILE_SOURCE=local or fallback, check once whether the independently-run local tile
# server (see maptiles/) is reachable - see maptiles_manager.py. The dashboard never starts or
# stops that server itself. TILE_SOURCE=local raises (and aborts startup) if it isn't already
# reachable, since there is no runtime fallback to the public tiles in that mode;
# TILE_SOURCE=fallback only logs a warning and lets startup continue, since the public tiles
# serve every map until then (and the local tile server is used as soon as it appears -
# reachability is rechecked periodically at runtime).
maptiles_manager.init_tile_source()

BASIC_AUTH_USER = os.environ.get("DASHBOARD_USER", "admin")
BASIC_AUTH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")

@app.before_request
def require_basic_auth():
    if not BASIC_AUTH_PASSWORD:
        return None

    auth = request.authorization
    valid = (
        auth
        and hmac.compare_digest(auth.username or "", BASIC_AUTH_USER)
        and hmac.compare_digest(auth.password or "", BASIC_AUTH_PASSWORD)
    )

    if valid:
        return None

    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Fleetplanning Dashboard"'}
    )

@app.before_request
def enforce_demo_read_only():
    if not DEMO_MODE:
        return None
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    return Response("Diese Aktion ist in der aktuellen Ansicht nicht verfuegbar.", 403)


loaded_data = {
    "cabs": [], "chargingPoints": [], "proSchedules": [],
    "chainingLocations": [], "chainRoutes": [], "chainRouteSchedules": [], "rideRequests": [],
    "operationArea": {}, "startTime": None, "endTime": None,
    "_rawBaseData": None, "_rawRideData": None,
    "_baseDataFileName": None, "_rideDataFileName": None,
    "_operationAreaSource": None,
    # Structural property of the input data (whether the solver uses chainRouteSchedules as-is
    # instead of regenerating a routed Pro timetable from it) - deliberately a top-level key, not
    # part of simulationDraft: it describes the uploaded schedule, not a search/algorithm choice,
    # and it isn't reachable from simulation_overview.html's form. Set via its own toggle next to
    # the Chain Route Schedules card on index.html (see set_use_original_pro_timetable()).
    "useOriginalProTimetable": False,
}

loaded_data.setdefault("outputCabRuns", [])
loaded_data.setdefault("outputSimRuns", [])
loaded_data.setdefault("outputProRuns", [])
loaded_data.setdefault("simulationJobs", [])
loaded_data.setdefault(
    "simulationDraft",
    {
        "scenarioName": "",
        "algorithm": "custom",
        "searchMode": "static",
        "cabPrice": 100000.0,
        "proPrice": 250000.0,
        "stationaryKwhPrice": 0.2,
        "proKwhPrice": 0.2,
        "cabLifetimeYears": 8.0,
        "proLifetimeYears": 8.0,
        "interestRatePercent": 0.0,
        "operatingDaysPerYear": 365.0,
        "fareBaseEur": 0.5,
        "fareDistanceEurPerM": 0.0003,
        "fareTimeEurPerHour": 0.0,
        "fareMinPerTripEur": 2.0,
        "fareDailySubscriptionEur": 0.0,
    },
)

SIM_DEFAULT_COST_MODEL = CostModel()
SIM_DEFAULT_CAB_PRICE = SIM_DEFAULT_COST_MODEL.cab_price
SIM_DEFAULT_PRO_PRICE = SIM_DEFAULT_COST_MODEL.pro_price
SIM_DEFAULT_STATIONARY_KWH_PRICE = SIM_DEFAULT_COST_MODEL.stationary_kwh_price
SIM_DEFAULT_SEARCH_MODE = "static"
SIM_DEFAULT_ITER_LIMIT = 100  # fleet_planning.py's own --iter_limit default
SIM_DEFAULT_TIME_LIMIT = 18000.0  # fleet_planning.py's own --time_limit default, in seconds
SIM_DEFAULT_CAB_ADD_STEP = 5  # fleet_planning.py's own --cab_add_step default
# fleet_planning.py's own effective default (apply_custom_sim_experiment_config's
# default_initial_cabs=1) - the dashboard's own generated config file never sets its own
# "initial_cabs" key, so this is what a dashboard-launched job always gets when the field is
# left blank, not some other config-dependent value. Shown explicitly for that reason, unlike
# budget_eur/service_level_min (which are genuinely optional constraints, off by default).
SIM_DEFAULT_INITIAL_CABS = 1
# fleet_planning.py's own --shrink_pros_* defaults - adaptive-mode-only search parameters.
SIM_DEFAULT_SHRINK_PROS_ALLOW_RETRY = False
SIM_DEFAULT_SHRINK_PROS_MAX_TRIES = 1
SIM_DEFAULT_SHRINK_PROS_TRIAL_PROBE_BELOW_START = True
SIM_DEFAULT_SHRINK_PROS_TRIAL_OVERSHOOT_CORRECTION = False
SIM_DEFAULT_OBJECTIVE_WEIGHT = 1.0  # fleet_planning.py's own --objective_weight default
# fleet_planning.py's own --stagnation_tolerance(_patience) defaults. Unlike the shrink_pros_*
# parameters above, these apply to the grow phase in both search modes, not just adaptive mode.
SIM_DEFAULT_STAGNATION_TOLERANCE = 0.0
SIM_DEFAULT_STAGNATION_TOLERANCE_PATIENCE = 1
# mirrors fleet_planning.py's OBJECTIVE_METRICS keys - kept as a local constant, not imported,
# since app.py deliberately never imports fleet_planning.py itself (that pulls in plotly/osmnx/
# networkit; solver_runner.py loads it as source with stubs specifically to avoid that here).
# Two perspective groups, always exactly one pick per group in the dashboard (radio buttons, see
# _read_objective_terms_form_fields) - fleet_planning.py itself has no such restriction, this is
# a dashboard-only UX choice. alpha=0/1 already give the "pure single term" cases, so there's no
# need for a group to contribute zero terms. Group and within-group order together define
# --objective_weight's "first term vs second term" meaning.
OBJECTIVE_PERSPECTIVE_GROUPS = (
    ("customer", ("total_served", "avg_in_vehicle_time_s")),
    ("operator", ("avg_cost_per_trip_eur", "avg_profit_per_trip_eur")),
)
OBJECTIVE_METRIC_NAMES = tuple(name for _, options in OBJECTIVE_PERSPECTIVE_GROUPS for name in options)
SIM_DEFAULT_OBJECTIVE_TERMS = ["total_served", "avg_cost_per_trip_eur"]

# (camelCase JSON/draft key, CostModel snake_case attribute) - also used as the form field name
# for every entry beyond the three original prices (cab_price/pro_price/stationary_kwh_price
# already match).
COST_MODEL_FIELDS: list[tuple[str, str]] = [
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

# (min, max) per COST_MODEL_FIELDS attribute, matching each field's own HTML min/max in
# simulation_overview.html - used by _validate_cost_model_form_fields() to reject out-of-range
# input the same way the browser's own constraint validation would. None = no bound on that side.
# operating_days_per_year is the only genuinely discrete field (a day count); every other field
# is a continuous EUR/percent/years quantity, hence step="any" on all of them in the template.
COST_MODEL_FIELD_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "cab_price": (0, None),
    "pro_price": (0, None),
    "stationary_kwh_price": (0, None),
    "pro_kwh_price": (0, None),
    "cab_lifetime_years": (0.1, None),
    "pro_lifetime_years": (0.1, None),
    "interest_rate_percent": (0, None),
    "operating_days_per_year": (1, 366),
    "fare_base_eur": (0, None),
    "fare_distance_eur_per_m": (0, None),
    "fare_time_eur_per_hour": (0, None),
    "fare_min_per_trip_eur": (0, None),
    "fare_daily_subscription_eur": (0, None),
}


def _cost_model_defaults() -> dict:
    """Return the default cost-model field values (camelCase keys) from CostModel()'s own defaults."""
    return {json_key: getattr(SIM_DEFAULT_COST_MODEL, attr) for json_key, attr in COST_MODEL_FIELDS}


def _read_cost_model_fields(source: dict | None) -> dict:
    """Resolve all cost-model fields (camelCase keys) from a dict source, falling back to defaults."""
    source = source or {}
    return {
        json_key: _to_float_or_default(source.get(json_key), getattr(SIM_DEFAULT_COST_MODEL, attr))
        for json_key, attr in COST_MODEL_FIELDS
    }


def _validate_bounded_number_input(raw, default: float, minimum=None, maximum=None, integer: bool = False):
    """Strictly validate one numeric form field against its own (min, max) bound.

    Returns the parsed value only if blank (-> default) or a real number within range; None for
    anything unparseable or out of bounds, so the caller can reject the submission with a clear
    notice instead of silently falling back to the default - matching what the field's own
    min/max would already do in-browser. integer=True additionally rejects non-whole values
    (operating_days_per_year is the only field this applies to)."""
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return None
    if integer and not value.is_integer():
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return int(value) if integer else value


def _validate_cost_model_form_fields() -> dict | None:
    """Strictly validate all cost-model fields (camelCase keys) from the current request's form
    data, against COST_MODEL_FIELD_BOUNDS.

    Mirrors _validate_objective_weight_input for the cost-model fields: returns the resolved
    dict only if every field is blank (-> default) or in range; None if any single field fails,
    so the caller can reject the whole submission with one notice instead of silently falling
    back to the default per field (as _read_cost_model_fields does for lenient sources like an
    imported metadata file or another job's saved settings)."""
    result = {}
    for json_key, attr in COST_MODEL_FIELDS:
        minimum, maximum = COST_MODEL_FIELD_BOUNDS[attr]
        value = _validate_bounded_number_input(
            request.form.get(attr),
            getattr(SIM_DEFAULT_COST_MODEL, attr),
            minimum=minimum,
            maximum=maximum,
            integer=(attr == "operating_days_per_year"),
        )
        if value is None:
            return None
        result[json_key] = value
    return result


def _sanitize_early_unchaining_enabled(value) -> bool:
    """Normalize an early-unchaining-enabled value from a dict source (job/import/demo
    metadata), defaulting to True when the key is entirely absent - matches
    custom_simulation.py's own default when its config's algorithm.early_unchaining.enabled key
    is missing (`self.parameters.get("algorithm", {}).get("early_unchaining", {}).get("enabled",
    True)`). Only for custom_sim jobs; harmless (never read) for rw/sumo. A direct form
    submission doesn't need this - an unchecked checkbox is a real, deliberate False, not an
    absent key, so the POST handler reads it as plain bool(request.form.get(...)) instead."""
    return True if value is None else bool(value)


def _sanitize_search_mode(value) -> str:
    """Normalize a search-mode value to one of fleet_planning.py's --search_mode choices."""
    token = str(value or "").strip().lower()
    return token if token in {"adaptive", "static"} else SIM_DEFAULT_SEARCH_MODE


def _sanitize_objective_weight(value) -> float:
    """Clamp a search objective-weight value to [0,1], falling back to the default on bad input.

    Deliberately lenient - used for inputs that should never hard-fail an otherwise-valid
    payload (an imported metadata file, another job's saved settings): a bad value there just
    falls back/clamps silently. For a value the user just typed into this page's own field, use
    _validate_objective_weight_input instead - that one rejects instead of silently clamping,
    since the field's own min="0" max="1" would have blocked submission in-browser and a
    server-side bypass shouldn't silently succeed where the browser would have warned.
    """
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_OBJECTIVE_WEIGHT
    return max(0.0, min(1.0, weight))


def _sanitize_iter_limit(value) -> int:
    """Clamp an iter_limit value to >= 1, falling back to the default on bad input - lenient
    counterpart of _validate_iter_limit_input, see _sanitize_objective_weight for why the split
    exists."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_ITER_LIMIT
    return max(1, int(parsed))


def _sanitize_time_limit(value) -> float:
    """Clamp a time_limit value to > 0, falling back to the default on bad input - lenient
    counterpart of _validate_time_limit_input, see _sanitize_objective_weight for why the split
    exists."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_TIME_LIMIT
    return parsed if parsed > 0 else SIM_DEFAULT_TIME_LIMIT


def _validate_iter_limit_input(raw) -> int | None:
    """Strictly validate a directly-submitted iter_limit field - mirrors
    _validate_objective_weight_input: blank -> SIM_DEFAULT_ITER_LIMIT, a whole number >= 1 ->
    that value, anything else -> None so the caller can reject instead of silently falling
    back - matches fleet_planning.py's own --iter_limit validation (>= 1)."""
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_ITER_LIMIT
    try:
        value = float(raw)
    except ValueError:
        return None
    if not value.is_integer() or value < 1:
        return None
    return int(value)


def _sanitize_cab_add_step(value) -> int:
    """Clamp a cab_add_step value to >= 1, falling back to the default on bad input - lenient
    counterpart of _validate_cab_add_step_input, see _sanitize_objective_weight for why the
    split exists."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_CAB_ADD_STEP
    return max(1, int(parsed))


def _validate_cab_add_step_input(raw) -> int | None:
    """Strictly validate a directly-submitted cab_add_step field - see
    _validate_iter_limit_input. Valid range matches fleet_planning.py's own --cab_add_step
    validation: >= 1."""
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_CAB_ADD_STEP
    try:
        value = float(raw)
    except ValueError:
        return None
    if not value.is_integer() or value < 1:
        return None
    return int(value)


def _sanitize_stagnation_tolerance(value) -> float:
    """Clamp a stagnation_tolerance value to >= 0, falling back to the default on bad input,
    lenient counterpart of _validate_stagnation_tolerance_input, see _sanitize_objective_weight
    for why the split exists."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_STAGNATION_TOLERANCE
    return parsed if parsed >= 0 else SIM_DEFAULT_STAGNATION_TOLERANCE


def _validate_stagnation_tolerance_input(raw) -> float | None:
    """Strictly validate a directly-submitted stagnation_tolerance field, see
    _validate_iter_limit_input. Valid range matches fleet_planning.py's own
    --stagnation_tolerance validation: >= 0."""
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_STAGNATION_TOLERANCE
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def _sanitize_stagnation_tolerance_patience(value) -> int:
    """Clamp a stagnation_tolerance_patience value to >= 0, falling back to the default on bad
    input, lenient counterpart of _validate_stagnation_tolerance_patience_input."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_STAGNATION_TOLERANCE_PATIENCE
    return max(0, int(parsed))


def _validate_stagnation_tolerance_patience_input(raw) -> int | None:
    """Strictly validate a directly-submitted stagnation_tolerance_patience field, see
    _validate_iter_limit_input. Valid range matches fleet_planning.py's own
    --stagnation_tolerance_patience validation: >= 0."""
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_STAGNATION_TOLERANCE_PATIENCE
    try:
        value = float(raw)
    except ValueError:
        return None
    if not value.is_integer() or value < 0:
        return None
    return int(value)


def _sanitize_shrink_pros_allow_retry(value) -> bool:
    """Normalize a dict-source shrink_pros_allow_retry value, defaulting to False (matches
    fleet_planning.py's own --shrink_pros_allow_retry default) when the key is absent."""
    return False if value is None else bool(value)


def _sanitize_shrink_pros_max_tries(value) -> int:
    """Clamp a shrink_pros_max_tries value to >= 0 (0 is a real, meaningful value - it disables
    the trial mechanism entirely, see fleet_planning.py's own help text), falling back to the
    default on bad input."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return SIM_DEFAULT_SHRINK_PROS_MAX_TRIES
    return max(0, int(parsed))


def _validate_shrink_pros_max_tries_input(raw) -> int | None:
    """Strictly validate a directly-submitted shrink_pros_max_tries field - see
    _validate_iter_limit_input. Valid range matches fleet_planning.py's own
    --shrink_pros_max_tries validation: >= 0 (unlike iter_limit/cab_add_step, 0 is a real,
    meaningful value here, not a lower bound to reject)."""
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_SHRINK_PROS_MAX_TRIES
    try:
        value = float(raw)
    except ValueError:
        return None
    if not value.is_integer() or value < 0:
        return None
    return int(value)


def _sanitize_shrink_pros_trial_probe_below_start(value) -> bool:
    """Normalize a dict-source shrink_pros_trial_probe_below_start value, defaulting to True
    (matches fleet_planning.py's own default - the CLI flag is a --no_... disable switch)
    when the key is absent."""
    return True if value is None else bool(value)


def _sanitize_shrink_pros_trial_overshoot_correction(value) -> bool:
    """Normalize a dict-source shrink_pros_trial_overshoot_correction value, defaulting to
    False (matches fleet_planning.py's own default) when the key is absent."""
    return False if value is None else bool(value)


def _validate_time_limit_input(raw) -> float | None:
    """Strictly validate a directly-submitted time_limit field - see
    _validate_iter_limit_input. Valid range matches fleet_planning.py's own --time_limit
    validation: > 0 (seconds)."""
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_TIME_LIMIT
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _validate_objective_weight_input(raw) -> float | None:
    """Strictly validate a directly-submitted objective_weight field.

    Returns the parsed value only if blank (-> default) or a real number in [0,1]; None for
    anything out of range or unparseable, so the caller can reject the submission with a clear
    notice instead of silently clamping it - matching what the field's own min="0" max="1"
    would already do in-browser.
    """
    raw = (raw or "").strip()
    if not raw:
        return SIM_DEFAULT_OBJECTIVE_WEIGHT
    try:
        value = float(raw)
    except ValueError:
        return None
    if not (0.0 <= value <= 1.0):
        return None
    return value


# Sentinel distinguishing "rejected" from "blank, and blank is itself a valid value" for
# budget_eur/service_level_min - unlike objective_weight/the cost-model fields, these two have
# no meaningful default to fall back to: blank genuinely means "constraint disabled" (None,
# matching fleet_planning.py's own None-disables-it default), not "use some other number".
_INVALID_NUMBER = object()


def _sanitize_budget_eur(value):
    """Lenient: parse to a positive float, else None (constraint disabled) - for imported/job-
    loaded values, mirroring _sanitize_objective_weight's role. The strict counterpart for a
    directly-submitted form is _validate_budget_eur_input."""
    parsed = _to_float_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _sanitize_service_level_min(value):
    """Lenient: parse to a float in (0, 1], else None (constraint disabled) - see
    _sanitize_budget_eur. Strict counterpart: _validate_service_level_min_input."""
    parsed = _to_float_or_none(value)
    return parsed if parsed is not None and 0 < parsed <= 1 else None


def _validate_budget_eur_input(raw):
    """Strictly validate a directly-submitted budget_eur field - mirrors
    _validate_objective_weight_input, but blank is itself a valid result (None, the constraint
    disabled) rather than a default: returns None for blank, the parsed float for a real number
    > 0 (matching fleet_planning.py's own --budget_eur validation), or the _INVALID_NUMBER
    sentinel for anything else, so the caller can tell "disabled" and "reject" apart."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _INVALID_NUMBER
    if value <= 0:
        return _INVALID_NUMBER
    return value


def _validate_service_level_min_input(raw):
    """Strictly validate a directly-submitted service_level_min field - see
    _validate_budget_eur_input. Valid range matches fleet_planning.py's own
    --service_level_min validation: (0, 1]."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _INVALID_NUMBER
    if not (0 < value <= 1):
        return _INVALID_NUMBER
    return value


def _sanitize_initial_cabs(value):
    """Lenient: parse to a whole number >= 1, else None (not overridden - fleet_planning.py
    falls back to the experiment config's own value, or 1) - see _sanitize_budget_eur. Strict
    counterpart: _validate_initial_cabs_input."""
    parsed = _to_float_or_none(value)
    if parsed is None or not float(parsed).is_integer() or parsed < 1:
        return None
    return int(parsed)


def _validate_initial_cabs_input(raw):
    """Strictly validate a directly-submitted initial_cabs field - see
    _validate_budget_eur_input. Blank means "don't override" (None), matching
    fleet_planning.py's own --initial_cabs default (None = use the experiment config's own
    value, or 1). Valid range: a whole number >= 1."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _INVALID_NUMBER
    if not value.is_integer() or value < 1:
        return _INVALID_NUMBER
    return int(value)


def _sanitize_objective_terms(value) -> list[str]:
    """Keep only known objective metrics, at most one per OBJECTIVE_PERSPECTIVE_GROUPS entry, in
    group then within-group order, falling back to the default pair if nothing valid is left.
    General-purpose sanitizer for arbitrary stored/uploaded objectiveTerms values (drafts, old
    jobs) - the dashboard's own form always yields a valid pair directly, see
    _read_objective_terms_form_fields."""
    selected = set(value) if value else set()
    terms = []
    for _, options in OBJECTIVE_PERSPECTIVE_GROUPS:
        for name in options:
            if name in selected:
                terms.append(name)
                break
    return terms or list(SIM_DEFAULT_OBJECTIVE_TERMS)


def _read_objective_terms_form_fields() -> list[str]:
    """Resolve the selected objective metrics from the current request's two perspective radio
    groups - always exactly one term per OBJECTIVE_PERSPECTIVE_GROUPS entry, falling back to a
    group's first option if the submitted value isn't a real member of it."""
    terms = []
    for group_name, options in OBJECTIVE_PERSPECTIVE_GROUPS:
        selected = request.form.get(f"objective_term_{group_name}")
        terms.append(selected if selected in options else options[0])
    return terms

def _has_operation_area() -> bool:
    """Return whether the current state contains enough points for a usable operation area."""
    area = loaded_data.get("operationArea") or {}
    points = area.get("points") if isinstance(area, dict) else None
    return isinstance(points, list) and len(points) >= 3


def _default_scenario_name() -> str:
    """Timestamp-based fallback used whenever scenario_name is left blank - shared by the
    queue/export actions and the page's own placeholder preview, so what's shown matches what
    actually gets used (barring the few seconds between page load and submit)."""
    return f"Szenario_{datetime.now():%Y%m%d_%H%M%S}"


def _instance_ready() -> bool:
    """Return whether enough input data is loaded to actually start a simulation - the same
    three conditions the "queue" action gates on, factored out so the page can show/disable
    things consistently with what a submit would actually do."""
    return bool(loaded_data.get("rideRequests") and loaded_data.get("cabs") and _has_operation_area())


def _build_instance_summary() -> dict:
    """Read-only summary of the currently uploaded input data, for display only - not used to
    drive anything, just so it's visible what a run would actually be configured against.
    No startTime/endTime here on purpose - that's sourced from GuiltyRange, an RW-API-specific
    field that shouldn't surface in the UI at all."""
    return {
        "baseDataFileName": loaded_data.get("_baseDataFileName") or None,
        "rideDataFileName": loaded_data.get("_rideDataFileName") or None,
        "requestCount": len(loaded_data.get("rideRequests") or []),
        "cabCount": len(loaded_data.get("cabs") or []),
        "chargingPointCount": len(loaded_data.get("chargingPoints") or []),
        "chainingLocationCount": len(loaded_data.get("chainingLocations") or []),
        "hasOperationArea": _has_operation_area(),
    }

def _require_operation_area(view):
    """Redirect protected views to the index page until a valid operation area exists."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        """Run the protected route only after an operation area has been defined."""
        if _has_operation_area():
            return view(*args, **kwargs)
        return redirect(url_for("index"))
    return wrapped

def _parse_required_coordinates(form, lat_field="latitude", lng_field="longitude"):
    """Parse a required lat/lng pair from submitted form data.

    Returns (lat, lng) or None if either field is missing, blank, or not a valid number - e.g.
    the user never clicked a position on the map. Every entity-CRUD route with a map picker
    used to do this with a bare float(request.form[...]), which raised an uncaught exception
    (500 Internal Server Error) in exactly that case instead of a validation message.
    """
    try:
        lat = float(form.get(lat_field, ""))
        lng = float(form.get(lng_field, ""))
    except (TypeError, ValueError):
        return None
    return lat, lng


def _build_ui_data(state: dict | None = None) -> dict:
    """Build the data snapshot passed to templates and frontend map scripts."""
    data = state if isinstance(state, dict) else loaded_data
    ui = {
        "cabs": data.get("cabs", []),
        "chargingPoints": data.get("chargingPoints", []),
        "proSchedules": data.get("proSchedules", []),
        "chainingLocations": data.get("chainingLocations", []),
        "chainRoutes": data.get("chainRoutes", []),
        "chainRouteSchedules": data.get("chainRouteSchedules", []),
        "rideRequests": data.get("rideRequests", []),
        "operationArea": data.get("operationArea") or {},
        "startTime": data.get("startTime"),
        "endTime": data.get("endTime"),
    }
    if data.get("_operationAreaSource") == "output":
        ui["operationArea"] = {}
        ui["startTime"] = None
        ui["endTime"] = None
    return ui

def _as_float(value):
    """Convert values to float while returning None for invalid coordinates."""
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

def _point_from_location(loc):
    """Normalize a location dictionary into a frontend-friendly lat/lng point."""
    if not isinstance(loc, dict):
        return None
    lat = (
        loc.get("Latitude")
        if loc.get("Latitude") is not None
        else loc.get("latitude")
        if loc.get("latitude") is not None
        else loc.get("lat")
    )
    lng = (
        loc.get("Longitude")
        if loc.get("Longitude") is not None
        else loc.get("longitude")
        if loc.get("longitude") is not None
        else loc.get("lng")
    )
    latf = _as_float(lat)
    lngf = _as_float(lng)
    if latf is None or lngf is None:
        return None
    return {"lat": latf, "lng": lngf}

def _build_map_overlay_data(state: dict | None = None) -> dict:
    """Collect normalized map overlay points for operation areas, vehicles, chargers, and ride requests."""
    data = state if isinstance(state, dict) else loaded_data
    op_area_points = []
    area = data.get("operationArea") or {}
    points = area.get("points") if isinstance(area, dict) else None
    if isinstance(points, list):
        for p in points:
            pt = _point_from_location(p if isinstance(p, dict) else {})
            if pt:
                op_area_points.append(pt)

    charging_points = []
    for cp in data.get("chargingPoints", []) or []:
        if not isinstance(cp, dict):
            continue
        pt = _point_from_location(cp.get("Location") if isinstance(cp.get("Location"), dict) else {})
        if not pt:
            continue
        charging_points.append(
            {
                "id": cp.get("id") or cp.get("Guid") or "",
                "lat": pt["lat"],
                "lng": pt["lng"],
            }
        )

    pros = []
    for pro in data.get("proSchedules", []) or []:
        if not isinstance(pro, dict):
            continue
        pt = _point_from_location(pro.get("InitialLocation") if isinstance(pro.get("InitialLocation"), dict) else {})
        if not pt:
            continue
        pros.append(
            {
                "id": pro.get("id") or pro.get("Guid") or pro.get("scheduleId") or "",
                "label": pro.get("label") or "",
                "lat": pt["lat"],
                "lng": pt["lng"],
            }
        )

    chaining_locations = []
    for loc in data.get("chainingLocations", []) or []:
        if not isinstance(loc, dict):
            continue
        start = _point_from_location(loc.get("LocationStart") if isinstance(loc.get("LocationStart"), dict) else {})
        end = _point_from_location(loc.get("LocationEnd") if isinstance(loc.get("LocationEnd"), dict) else {})
        chaining_locations.append(
            {
                "id": loc.get("id") or loc.get("Guid") or loc.get("locationId") or "",
                "start": start,
                "end": end,
            }
        )

    chain_routes = []
    for route in data.get("chainRoutes", []) or []:
        if not isinstance(route, dict):
            continue
        chain_routes.append(
            {
                "id": route.get("id") or "",
                "guid": route.get("Guid") or "",
                "startLocation": route.get("StartLocation") or "",
                "endLocation": route.get("EndLocation") or "",
            }
        )

    return {
        "operationArea": op_area_points,
        "chargingPoints": charging_points,
        "pros": pros,
        "chainingLocations": chaining_locations,
        "chainRoutes": chain_routes,
    }

def _nav_context_query() -> str:
    """Query string preserving whichever scenario/job the current request is about, for the
    persistent nav bar.

    Individual routes also build their own page-scoped output_data_query for their own AJAX
    calls (output_page, output_details), but relying on that alone meant the nav bar's links
    only carried the scenario forward when you happened to already be on one of those two pages
    - clicking through from /upload or the locked view of /index dropped it again, the same bug from a
    different starting point. Computed once here, from the request itself, so every page gets
    it regardless of whether its own route bothered to pass one.
    """
    if DEMO_MODE:
        return _demo_query(_selected_demo_scenario())
    job = _resolve_output_view_job()
    return _job_query(job) if job else ""


@app.context_processor
def inject_map_overlay_data():
    """Expose normalized map overlay data to all templates through the Flask context."""
    tile_url_template, tile_url_fallback = maptiles_manager.current_tile_urls()
    return {
        "map_overlay_data": _build_map_overlay_data(),
        "tile_url_template": tile_url_template,
        "tile_url_fallback": tile_url_fallback,
        "demo_mode": DEMO_MODE,
        "demo_loading_seconds": DEMO_LOADING_SECONDS,
        "nav_context_query": _nav_context_query(),
    }


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def local_tile(z, x, y):
    """Serve one map tile from the local tile server (see maptiles_manager.py), so browsers only
    ever talk to the dashboard and never to the tile server itself."""
    status, body = maptiles_manager.fetch_local_tile(z, x, y)
    if status != 200:
        return Response(status=status)
    return Response(body, mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.route('/index')
def index():
    """Render the input overview page - editable by default, or locked (read-only) with
    ?locked=1, e.g. from /output's "Zur Read-Only Seite" link. Used to be a separate /overview
    route/view function rendering the same template with read_only always True; folded into one
    route since the two never differed in anything beyond that one flag, and keeping them apart
    meant every fix (naming aside, this is literally how the "can't collapse the accordion" bug
    from a duplicated <script> tag ended up needing to be found and fixed on both). Demo mode is
    always locked, regardless of ?locked - there is nothing to edit there."""
    locked = DEMO_MODE or request.args.get("locked") == "1"
    if DEMO_MODE:
        scenarios = _discover_demo_scenarios()
        selected = _selected_demo_scenario(scenarios)
        demo_state = _load_demo_input_state(selected)
        ui_data = _build_ui_data(demo_state)
        return render_template(
            'index.html',
            data=demo_state,
            loaded_data=demo_state,
            read_only=locked,
            ui_data=ui_data,
            demo_scenarios=scenarios,
            selected_demo_scenario=selected,
            requests_outside_area_count=_count_requests_outside_operation_area(demo_state),
        )
    ui_data = _build_ui_data()
    return render_template(
        'index.html',
        data=loaded_data,
        loaded_data=loaded_data,
        read_only=locked,
        ui_data=ui_data,
        requests_outside_area_count=_count_requests_outside_operation_area(),
    )

@app.route('/')
def landing_page():
    """Render the landing page for the dashboard."""
    return render_template('landing.html')

@app.route('/upload')
def upload_page():
    """Render the input upload page with the current UI data snapshot."""
    if DEMO_MODE:
        scenarios = _discover_demo_scenarios()
        selected = _selected_demo_scenario(scenarios)
        demo_state = _load_demo_input_state(selected)
        return render_template(
            'input_upload.html',
            data=demo_state,
            loaded_data=demo_state,
            ui_data=_build_ui_data(demo_state),
            demo_scenarios=scenarios,
            selected_demo_scenario=selected,
            demo_price_values=_demo_price_values(),
            requests_outside_area_count=_count_requests_outside_operation_area(demo_state),
        )
    ui_data = _build_ui_data()
    return render_template(
        'input_upload.html',
        data=loaded_data,
        loaded_data=loaded_data,
        ui_data=ui_data,
        xml_upload_job=_build_xml_upload_job_view(),
        instance_summary=_build_instance_summary(),
        requests_outside_area_count=_count_requests_outside_operation_area(),
    )


def _build_xml_upload_job_view() -> dict:
    """Add a server-computed runtimeText to the tracked XML-conversion job, same as
    _build_running_simulations()'s runtimeText - rendering real elapsed-time text on every
    fragment poll (instead of an empty placeholder the client fills in a moment later) is what
    keeps the "(Ns)" counter from visibly blanking out on every swap. See
    _runtime_text_from_started()."""
    job = dict(loaded_data.get("xmlUploadJob") or {})
    if job.get("status") in ("queued", "running"):
        job["runtimeText"] = _runtime_text_from_started(job.get("startedAt"))
    return job


@app.route('/upload/xml-job-fragment')
def xml_upload_job_fragment():
    """Render just the background XML-conversion status/error box, for the upload page's AJAX
    poll - see _xml_upload_status.html and _run_xml_conversion_job. Recycles the same
    server-rendered-fragment + poll-while-running pattern simulation_jobs_fragment() already
    uses for the running-simulations table, instead of a separate client-only polling mechanism."""
    if DEMO_MODE:
        return ("", 404)
    return render_template("_xml_upload_status.html", xml_upload_job=_build_xml_upload_job_view())

@app.route('/scenario-preview-data')
def scenario_preview_data():
    """Return read-only UI data for the selected prepared scenario."""
    if not DEMO_MODE:
        return jsonify({"status": "error", "message": "Nicht verfuegbar"}), 404
    scenarios = _discover_demo_scenarios()
    selected = _selected_demo_scenario(scenarios)
    state = _load_demo_input_state(selected)
    return jsonify({"status": "ok", "uiData": _build_ui_data(state)})

@app.route("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
    """Return an empty response for harmless Chrome DevTools probe requests."""
    return ("", 204)

@app.route('/output', methods=['GET'])
def output_page():
    """Render the output overview and optionally focus it on one selected simulation job."""
    if DEMO_MODE:
        scenarios = _discover_demo_scenarios()
        selected = _selected_demo_scenario(scenarios)
        demo_state = _load_demo_output_state(selected)
        output_data = demo_state.get("_outputParsed")
        selected_simulation = (
            {
                "title": selected.get("title"),
                "detail": selected.get("detail"),
                "searchMode": _sanitize_search_mode(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("searchMode")
                ),
                # absent on demo scenarios recorded before this flag existed - defaults to False
                # (the safe/regenerated behavior), same as bool() on any other missing key.
                "useOriginalProTimetable": bool(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("useOriginalProTimetable")
                ),
                "budgetEur": _sanitize_budget_eur(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("budgetEur")
                ),
                "serviceLevelMin": _sanitize_service_level_min(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("serviceLevelMin")
                ),
                "earlyUnchainingEnabled": _sanitize_early_unchaining_enabled(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("earlyUnchainingEnabled")
                ),
                "iterLimit": _sanitize_iter_limit(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("iterLimit")
                ),
                "timeLimit": _sanitize_time_limit(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("timeLimit")
                ),
                "initialCabs": _sanitize_initial_cabs(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("initialCabs")
                ),
                "cabAddStep": _sanitize_cab_add_step(
                    (demo_state.get("_outputSimulationMetadata") or {}).get("cabAddStep")
                ),
                "scenarioFacts": _parse_scenario_facts_from_prefix(selected.get("prefix")),
            }
            if selected
            else None
        )
        return render_template(
            'output.html',
            output_data=output_data,
            has_output=bool(output_data and (output_data.get("paired", {}).get("runs") or output_data.get("runs"))),
            selected_simulation=selected_simulation,
            demo_scenarios=scenarios,
            selected_demo_scenario=selected,
            demo_price_values=_demo_price_values(),
            output_data_query=_demo_query(selected),
        )

    selected_job_id = (request.args.get("sim_job_id") or "").strip()
    job = _resolve_output_view_job()
    selected_simulation = None
    if job:
        naming_parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else None
        job_prefix = _build_sim_file_prefix(naming_parts) if naming_parts else None
        selected_simulation = {
            "title": job.get("scenarioName") or "Simulation",
            "detail": str(job.get("folderName") or ""),
            "searchMode": _sanitize_search_mode(job.get("searchMode")),
            "useOriginalProTimetable": bool(job.get("useOriginalProTimetable")),
            "budgetEur": _sanitize_budget_eur(job.get("budgetEur")),
            "serviceLevelMin": _sanitize_service_level_min(job.get("serviceLevelMin")),
            "earlyUnchainingEnabled": _sanitize_early_unchaining_enabled(job.get("earlyUnchainingEnabled")),
            "iterLimit": _sanitize_iter_limit(job.get("iterLimit")),
            "timeLimit": _sanitize_time_limit(job.get("timeLimit")),
            "initialCabs": _sanitize_initial_cabs(job.get("initialCabs")),
            "cabAddStep": _sanitize_cab_add_step(job.get("cabAddStep")),
            "scenarioFacts": _parse_scenario_facts_from_prefix(job_prefix),
        }
    if selected_simulation is None:
        for item in _build_completed_simulations():
            if item.get("id") == selected_job_id:
                selected_simulation = item
                break
    # neither output_data nor has_output is actually read by output.html - the real payload is
    # always fetched fresh via /output-data - kept here only as long-standing template params
    output_data = loaded_data.get("_outputParsed")
    return render_template(
        'output.html',
        output_data=output_data,
        has_output=bool(job or output_data),
        selected_simulation=selected_simulation,
        output_data_query=_job_query(job),
        sim_job_id=job.get("id") if job else "",
        job_counters_mismatch=_job_counters_mismatch(),
    )


@app.route('/simulation-overview', methods=['GET', 'POST'])
def simulation_overview():
    """Render and process the simulation setup page, including job creation and metadata imports."""
    if DEMO_MODE:
        selected = _selected_demo_scenario()
        if selected:
            return redirect(url_for("upload_page", demo_scenario=selected.get("id")))
        return redirect(url_for("upload_page"))

    notice = request.args.get("notice")

    if request.method == "POST":
        action = (request.form.get("action") or "save").strip().lower()

        if action == "import_metadata":
            uploaded_file = request.files.get("simulation_metadata_file")
            imported_metadata = _read_simulation_metadata_upload(uploaded_file)
            if not imported_metadata:
                return redirect(url_for("simulation_overview", notice="metadata_invalid"))

            loaded_data["simulationDraft"] = imported_metadata
            return redirect(url_for("simulation_overview", notice="metadata_imported"))

        if action == "load_job_settings":
            # explicit, button-triggered only (next to that job's own row) - never automatic
            # from merely navigating here with a job in context, which is confusing/surprising
            # (the earlier version of this feature did exactly that and got rightly pushed back on)
            job = _find_job_by_id((request.form.get("job_id") or "").strip())
            job_metadata = _read_simulation_metadata_file(_sim_job_folder_path(job), job) if job else None
            if not job_metadata:
                return redirect(url_for("simulation_overview", notice="settings_load_failed"))

            loaded_data["simulationDraft"] = _metadata_payload_to_draft(job_metadata)
            return redirect(url_for("simulation_overview", notice="settings_loaded"))

        if action == "delete_running":
            delete_key = (request.form.get("delete_key") or "").strip()
            _delete_running_simulation(delete_key)
            return redirect(url_for("simulation_overview", notice="deleted_running"))

        if action == "delete_completed":
            delete_key = (request.form.get("delete_key") or "").strip()
            _delete_completed_simulation(delete_key)
            return redirect(url_for("simulation_overview", notice="deleted_completed"))

        scenario_name = (request.form.get("scenario_name") or "").strip()
        algorithm = (request.form.get("algorithm") or "custom").strip()
        cost_fields = _validate_cost_model_form_fields()
        if cost_fields is None:
            return redirect(url_for("simulation_overview", notice="cost_model_invalid"))
        cab_price = cost_fields["cabPrice"]
        pro_price = cost_fields["proPrice"]
        stationary_kwh_price = cost_fields["stationaryKwhPrice"]
        search_mode = _sanitize_search_mode(request.form.get("search_mode"))
        iter_limit = _validate_iter_limit_input(request.form.get("iter_limit"))
        if iter_limit is None:
            return redirect(url_for("simulation_overview", notice="iter_limit_invalid"))
        time_limit = _validate_time_limit_input(request.form.get("time_limit"))
        if time_limit is None:
            return redirect(url_for("simulation_overview", notice="time_limit_invalid"))
        initial_cabs = _validate_initial_cabs_input(request.form.get("initial_cabs"))
        if initial_cabs is _INVALID_NUMBER:
            return redirect(url_for("simulation_overview", notice="initial_cabs_invalid"))
        cab_add_step = _validate_cab_add_step_input(request.form.get("cab_add_step"))
        if cab_add_step is None:
            return redirect(url_for("simulation_overview", notice="cab_add_step_invalid"))
        shrink_pros_max_tries = _validate_shrink_pros_max_tries_input(request.form.get("shrink_pros_max_tries"))
        if shrink_pros_max_tries is None:
            return redirect(url_for("simulation_overview", notice="shrink_pros_max_tries_invalid"))
        stagnation_tolerance = _validate_stagnation_tolerance_input(request.form.get("stagnation_tolerance"))
        if stagnation_tolerance is None:
            return redirect(url_for("simulation_overview", notice="stagnation_tolerance_invalid"))
        stagnation_tolerance_patience = _validate_stagnation_tolerance_patience_input(
            request.form.get("stagnation_tolerance_patience")
        )
        if stagnation_tolerance_patience is None:
            return redirect(url_for("simulation_overview", notice="stagnation_tolerance_patience_invalid"))
        # checkboxes: unchecked is simply absent from form data - no default-fallback needed/
        # wanted here, unlike the _sanitize_shrink_pros_*'s dict-source case (see
        # early_unchaining_enabled just below for the same reasoning).
        shrink_pros_allow_retry = bool(request.form.get("shrink_pros_allow_retry"))
        shrink_pros_trial_probe_below_start = bool(request.form.get("shrink_pros_trial_probe_below_start"))
        shrink_pros_trial_overshoot_correction = bool(request.form.get("shrink_pros_trial_overshoot_correction"))
        objective_weight = _validate_objective_weight_input(request.form.get("objective_weight"))
        if objective_weight is None:
            return redirect(url_for("simulation_overview", notice="objective_weight_invalid"))
        objective_terms = _read_objective_terms_form_fields()
        budget_eur = _validate_budget_eur_input(request.form.get("budget_eur"))
        if budget_eur is _INVALID_NUMBER:
            return redirect(url_for("simulation_overview", notice="budget_eur_invalid"))
        service_level_min = _validate_service_level_min_input(request.form.get("service_level_min"))
        if service_level_min is _INVALID_NUMBER:
            return redirect(url_for("simulation_overview", notice="service_level_min_invalid"))
        # a checkbox that's unchecked is simply absent from form data - no default-fallback
        # needed/wanted here, unlike _sanitize_early_unchaining_enabled's dict-source case.
        early_unchaining_enabled = bool(request.form.get("early_unchaining_enabled"))

        loaded_data["simulationDraft"] = {
            "scenarioName": scenario_name,
            "algorithm": algorithm,
            "searchMode": search_mode,
            "iterLimit": iter_limit,
            "timeLimit": time_limit,
            "initialCabs": initial_cabs,
            "cabAddStep": cab_add_step,
            "shrinkProsAllowRetry": shrink_pros_allow_retry,
            "shrinkProsMaxTries": shrink_pros_max_tries,
            "shrinkProsTrialProbeBelowStart": shrink_pros_trial_probe_below_start,
            "shrinkProsTrialOvershootCorrection": shrink_pros_trial_overshoot_correction,
            "stagnationTolerance": stagnation_tolerance,
            "stagnationTolerancePatience": stagnation_tolerance_patience,
            "objectiveWeight": objective_weight,
            "objectiveTerms": objective_terms,
            "budgetEur": budget_eur,
            "serviceLevelMin": service_level_min,
            "earlyUnchainingEnabled": early_unchaining_enabled,
            **cost_fields,
        }

        if action == "export_metadata":
            export_payload = {
                "scenarioName": scenario_name or _default_scenario_name(),
                "algorithm": algorithm,
                "searchMode": search_mode,
                "iterLimit": iter_limit,
                "timeLimit": time_limit,
                "initialCabs": initial_cabs,
                "cabAddStep": cab_add_step,
                "shrinkProsAllowRetry": shrink_pros_allow_retry,
                "shrinkProsMaxTries": shrink_pros_max_tries,
                "shrinkProsTrialProbeBelowStart": shrink_pros_trial_probe_below_start,
                "shrinkProsTrialOvershootCorrection": shrink_pros_trial_overshoot_correction,
                "stagnationTolerance": stagnation_tolerance,
                "stagnationTolerancePatience": stagnation_tolerance_patience,
                "objectiveWeight": objective_weight,
                "objectiveTerms": objective_terms,
                "budgetEur": budget_eur,
                "serviceLevelMin": service_level_min,
                "earlyUnchainingEnabled": early_unchaining_enabled,
                **cost_fields,
            }
            export_name = secure_filename(export_payload["scenarioName"]) or "szenario"
            response = Response(
                json.dumps(export_payload, indent=2, ensure_ascii=False),
                mimetype="application/json",
            )
            response.headers["Content-Disposition"] = f'attachment; filename="{export_name}_simulation_metadata.json"'
            return response

        scenario_name = scenario_name or _default_scenario_name()

        if action == "queue":
            if not _instance_ready():
                return redirect(url_for("simulation_overview", notice="missing_input"))

            jobs = loaded_data.setdefault("simulationJobs", [])
            base_parts = _build_auto_naming_base()
            base_signature = (_input_content_hash(),)
            algorithm_token = _sanitize_token(algorithm, "custom")
            full_signature = (
                base_signature,
                algorithm_token,
                _cost_model_signature(cost_fields),
                search_mode,
                # a truncated search (lower iter_limit/time_limit) can genuinely produce a
                # different, less-converged result - belongs in the signature for the same
                # reason as the other fields that actually change what gets simulated.
                iter_limit,
                round(time_limit, 1),
                # a different starting fleet size or growth step is a different search
                # trajectory - initial_cabs=None means "not overridden" (see
                # _validate_initial_cabs_input), kept as None here rather than resolved to
                # whatever the experiment config would fall back to, matching how the other
                # nullable fields (budget_eur/service_level_min) stay None in the signature too.
                initial_cabs,
                cab_add_step,
                # applies to the grow phase in both search modes (not adaptive-only, unlike
                # the shrink_pros_* fields below), so no search_mode canonicalization here.
                round(stagnation_tolerance, 6),
                stagnation_tolerance_patience,
                # unlike the cost-model fields (reporting-only, don't affect the simulation),
                # objective_terms/objective_weight change the search itself - a different
                # selection is a genuinely different job, not just a different price label on
                # the same run. alpha only matters (and only enters the signature) with 2 terms
                # selected - with 1, it's ignored by compute_objective(), so two runs that only
                # differ in alpha while a single term is selected are the same job.
                tuple(objective_terms),
                round(objective_weight, 4) if len(objective_terms) == 2 else None,
                # structural, not a search parameter, but still changes what actually gets
                # simulated - belongs in the signature for the same reason search_mode does.
                bool(loaded_data.get("useOriginalProTimetable")),
                # hard constraints - also change what actually gets simulated (a run capped at
                # a budget is a different search than an uncapped one), so they belong here too.
                round(budget_eur, 2) if budget_eur is not None else None,
                round(service_level_min, 4) if service_level_min is not None else None,
                # only reaches custom_sim (see _build_sim_metadata_payload) - canonicalize to
                # True for rw/sumo jobs so an unrelated leftover draft value never makes two
                # otherwise-identical rw/sumo launches look like different jobs.
                early_unchaining_enabled if algorithm_token == "custom" else True,
                # adaptive-mode-only search parameters (fleet_planning.py's --shrink_pros_*
                # flags) - meaningless in static mode, so canonicalized to their defaults there
                # for the same reason early_unchaining_enabled is canonicalized above.
                shrink_pros_allow_retry if search_mode == "adaptive" else SIM_DEFAULT_SHRINK_PROS_ALLOW_RETRY,
                shrink_pros_max_tries if search_mode == "adaptive" else SIM_DEFAULT_SHRINK_PROS_MAX_TRIES,
                (
                    shrink_pros_trial_probe_below_start if search_mode == "adaptive"
                    else SIM_DEFAULT_SHRINK_PROS_TRIAL_PROBE_BELOW_START
                ),
                (
                    shrink_pros_trial_overshoot_correction if search_mode == "adaptive"
                    else SIM_DEFAULT_SHRINK_PROS_TRIAL_OVERSHOOT_CORRECTION
                ),
            )
            existing_job = _find_matching_job(full_signature)
            if existing_job:
                existing_status = str(existing_job.get("status") or "").lower()
                if existing_status == "completed":
                    return redirect(url_for("output_page", sim_job_id=str(existing_job.get("id") or "")))
                if existing_status == "failed":
                    existing_job.update(
                        {
                            "status": "queued",
                            "startedAt": None,
                            "completedAt": None,
                            "failedAt": None,
                            "solverReturnCode": None,
                            "solverError": None,
                        }
                    )
                    _save_input_files_for_job(existing_job, iteration=0)
                    _start_solver_for_job(existing_job)
                    return redirect(url_for("simulation_overview", notice="queued"))
                return redirect(url_for("simulation_overview", notice="existing_running"))

            run_id = _next_run_id_for_base(base_signature)
            naming_parts = _build_sim_naming_parts(base_parts, algorithm, run_id, search_mode)
            folder_name = _build_sim_folder_name(search_mode, full_signature)
            if request.form.get("confirm_output_reuse") != "1" and os.path.isdir(
                os.path.join(_algorithm_output_root(algorithm_token), folder_name)
            ):
                return redirect(url_for("simulation_overview", notice="output_folder_exists"))
            job = {
                "id": _next_job_id(),
                "status": "queued",
                "scenarioName": scenario_name,
                "namingParts": naming_parts,
                "folderName": folder_name,
                "inputContentHash": base_signature[0],
                "searchMode": search_mode,
                "iterLimit": iter_limit,
                "timeLimit": time_limit,
                "initialCabs": initial_cabs,
                "cabAddStep": cab_add_step,
                "shrinkProsAllowRetry": shrink_pros_allow_retry,
                "shrinkProsMaxTries": shrink_pros_max_tries,
                "shrinkProsTrialProbeBelowStart": shrink_pros_trial_probe_below_start,
                "shrinkProsTrialOvershootCorrection": shrink_pros_trial_overshoot_correction,
                "stagnationTolerance": stagnation_tolerance,
                "stagnationTolerancePatience": stagnation_tolerance_patience,
                "objectiveWeight": objective_weight,
                "objectiveTerms": objective_terms,
                "budgetEur": budget_eur,
                "serviceLevelMin": service_level_min,
                "earlyUnchainingEnabled": early_unchaining_enabled,
                # snapshot at launch time, same as everything else above - a later toggle change
                # on index.html shouldn't retroactively change what an already-queued job used.
                "useOriginalProTimetable": bool(loaded_data.get("useOriginalProTimetable")),
                "createdAt": _utc_now_iso(),
                **cost_fields,
            }
            jobs.append(job)
            _ensure_folder(_sim_job_folder_path(job))
            _save_input_files_for_job(job, iteration=0)
            _start_solver_for_job(job)
            return redirect(url_for("simulation_overview", notice="queued"))

        if action == "queue_fixedfleet":
            # no FleetPlanning search - custom_sim/run_instance.py evaluates the currently
            # loaded fleet (e.g. loaded via "Als Eingabedaten laden" from a past job) exactly
            # as-is, once. Only meaningful for the custom backend (see run_instance.py).
            if _sanitize_token(algorithm, "custom") != "custom":
                return redirect(url_for("simulation_overview", notice="fixedfleet_requires_custom"))
            if not _instance_ready():
                return redirect(url_for("simulation_overview", notice="missing_input"))

            jobs = loaded_data.setdefault("simulationJobs", [])
            base_parts = _build_auto_naming_base()
            input_content_hash = _input_content_hash()
            pseudo_job = {
                "namingParts": base_parts,
                "runKind": "fixedfleet",
                "inputContentHash": input_content_hash,
                "earlyUnchainingEnabled": early_unchaining_enabled,
                **cost_fields,
            }
            full_signature = _job_full_signature(pseudo_job)

            existing_job = _find_matching_job(full_signature)
            if existing_job:
                existing_status = str(existing_job.get("status") or "").lower()
                if existing_status == "completed":
                    return redirect(url_for("output_page", sim_job_id=str(existing_job.get("id") or "")))
                if existing_status == "failed":
                    existing_job.update(
                        {
                            "status": "queued",
                            "startedAt": None,
                            "completedAt": None,
                            "failedAt": None,
                            "solverReturnCode": None,
                            "solverError": None,
                        }
                    )
                    _save_input_files_for_job(existing_job, iteration=0)
                    _start_solver_for_job(existing_job)
                    return redirect(url_for("simulation_overview", notice="queued"))
                return redirect(url_for("simulation_overview", notice="existing_running"))

            run_id = _next_run_id_for_base(_job_signature(pseudo_job))
            naming_parts = _build_sim_naming_parts(base_parts, "custom", run_id, search_mode=None)
            folder_name = _build_sim_folder_name("fixedfleet", full_signature)
            if request.form.get("confirm_output_reuse") != "1" and os.path.isdir(
                os.path.join(_algorithm_output_root("custom"), folder_name)
            ):
                return redirect(url_for("simulation_overview", notice="output_folder_exists"))
            job = {
                "id": _next_job_id(),
                "status": "queued",
                "runKind": "fixedfleet",
                "scenarioName": scenario_name,
                "namingParts": naming_parts,
                "folderName": folder_name,
                "inputContentHash": input_content_hash,
                "earlyUnchainingEnabled": early_unchaining_enabled,
                "createdAt": _utc_now_iso(),
                **cost_fields,
            }
            jobs.append(job)
            _ensure_folder(_sim_job_folder_path(job))
            _save_input_files_for_job(job, iteration=0)
            _start_solver_for_job(job)
            return redirect(url_for("simulation_overview", notice="queued"))

        return redirect(url_for("simulation_overview", notice="saved"))

    draft = loaded_data.get("simulationDraft", {}) or {}
    running_simulations = _build_running_simulations()
    completed_simulations = _build_completed_simulations()

    return render_template(
        "simulation_overview.html",
        draft=draft,
        running_simulations=running_simulations,
        completed_simulations=completed_simulations,
        instance_summary=_build_instance_summary(),
        instance_ready=_instance_ready(),
        default_scenario_name=_default_scenario_name(),
        notice=notice,
        job_counters_mismatch=_job_counters_mismatch(),
        sim_defaults={
            "searchMode": SIM_DEFAULT_SEARCH_MODE,
            "iterLimit": SIM_DEFAULT_ITER_LIMIT,
            "timeLimit": SIM_DEFAULT_TIME_LIMIT,
            "cabAddStep": SIM_DEFAULT_CAB_ADD_STEP,
            "initialCabs": SIM_DEFAULT_INITIAL_CABS,
            "shrinkProsMaxTries": SIM_DEFAULT_SHRINK_PROS_MAX_TRIES,
            "stagnationTolerance": SIM_DEFAULT_STAGNATION_TOLERANCE,
            "stagnationTolerancePatience": SIM_DEFAULT_STAGNATION_TOLERANCE_PATIENCE,
            "objectiveWeight": SIM_DEFAULT_OBJECTIVE_WEIGHT,
            "objectiveTerms": SIM_DEFAULT_OBJECTIVE_TERMS,
            **_cost_model_defaults(),
        },
    )


@app.route('/simulation-overview/jobs-fragment')
def simulation_jobs_fragment():
    """Render just the running/completed simulation tables, for the page's AJAX poll - avoids
    a full-page reload (and the scroll-position reset / mid-edit form data loss that came with
    it, see simulation_overview.html's poll script)."""
    if DEMO_MODE:
        return ("", 404)
    return render_template(
        "_simulation_jobs.html",
        running_simulations=_build_running_simulations(),
        completed_simulations=_build_completed_simulations(),
        demo_mode=DEMO_MODE,
    )


def _cleanup_upload_temp(keep_filenames: set[str] | None = None) -> int:
    """Remove temporary JSON uploads while keeping files that should survive across views."""
    keep_filenames = {k.lower() for k in (keep_filenames or set())}
    keep_filenames.add("operation_area.json")
    removed = 0
    if not os.path.isdir(UPLOAD_FOLDER):
        return 0
    for fn in os.listdir(UPLOAD_FOLDER):
        full = os.path.join(UPLOAD_FOLDER, fn)
        if not os.path.isfile(full):
            continue
        if not fn.lower().endswith(".json"):
            continue
        if fn.lower() in keep_filenames:
            continue
        try:
            os.remove(full)
            removed += 1
        except OSError:
            pass
    return removed

def _extract_num_from_run_name(run_name: str) -> int | None:
    """Extract the numeric run index from an output filename or label."""
    name = os.path.basename(str(run_name or "").replace("\\", "/")).lower()
    m = re.search(r"output_(?:cab|sim|pro)_(\d+)(?:\.json)?$", name)
    if m:
        return int(m.group(1))
    nums = re.findall(r"(\d+)", name)
    if not nums:
        return None
    return int(nums[-1])


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in the app's compact ISO format."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _metadata_timestamp(payload: dict | None, key: str) -> str | None:
    """Read one timestamp field out of an uploaded simulation_metadata.json payload, used by
    upload_output_auto() to recover a real historic startedAt/completedAt instead of stamping
    upload time - only if it actually parses as a timestamp, since the file is user-supplied."""
    value = (payload or {}).get(key)
    return value if isinstance(value, str) and _parse_iso_utc(value) else None


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO timestamp into a datetime used for runtime display."""
    if not value:
        return None
    try:
        txt = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(txt)
    except Exception:
        return None


def _runtime_text_from_started(started_at: str | None) -> str:
    """Format the elapsed runtime since a simulation job started."""
    started = _parse_iso_utc(started_at)
    if not started:
        return "-"
    try:
        now = datetime.utcnow().replace(tzinfo=started.tzinfo)
        delta = now - started
        total = max(0, int(delta.total_seconds()))
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"
    except Exception:
        return "-"


def _to_float_or_none(value):
    """Convert form or JSON values to float while preserving empty values as None."""
    try:
        if value is None:
            return None
        s = str(value).strip()
        if s == "":
            return None
        return float(s)
    except Exception:
        return None


def _to_float_or_default(value, default: float) -> float:
    """Convert a value to float and fall back to a configured default when missing."""
    parsed = _to_float_or_none(value)
    return default if parsed is None else parsed


def _metadata_payload_to_draft(payload: dict) -> dict:
    """Normalize a simulation_metadata.json-shaped payload into the editable simulation draft
    format - shared by the "Metadaten importieren" upload and by prefilling the Konfigurator
    from an existing job's own saved settings (see simulation_overview()'s GET handler)."""
    algorithm = _sanitize_token(payload.get("algorithm"), "custom")
    if algorithm not in {"rwapi", "sumo", "custom"}:
        algorithm = "custom"

    return {
        "scenarioName": str(payload.get("scenarioName") or "").strip(),
        "algorithm": algorithm,
        "runKind": "fixedfleet" if payload.get("runKind") == "fixedfleet" else "search",
        "searchMode": _sanitize_search_mode(payload.get("searchMode")),
        "iterLimit": _sanitize_iter_limit(payload.get("iterLimit")),
        "timeLimit": _sanitize_time_limit(payload.get("timeLimit")),
        "initialCabs": _sanitize_initial_cabs(payload.get("initialCabs")),
        "cabAddStep": _sanitize_cab_add_step(payload.get("cabAddStep")),
        "shrinkProsAllowRetry": _sanitize_shrink_pros_allow_retry(payload.get("shrinkProsAllowRetry")),
        "shrinkProsMaxTries": _sanitize_shrink_pros_max_tries(payload.get("shrinkProsMaxTries")),
        "shrinkProsTrialProbeBelowStart": _sanitize_shrink_pros_trial_probe_below_start(
            payload.get("shrinkProsTrialProbeBelowStart")
        ),
        "shrinkProsTrialOvershootCorrection": _sanitize_shrink_pros_trial_overshoot_correction(
            payload.get("shrinkProsTrialOvershootCorrection")
        ),
        "stagnationTolerance": _sanitize_stagnation_tolerance(payload.get("stagnationTolerance")),
        "stagnationTolerancePatience": _sanitize_stagnation_tolerance_patience(
            payload.get("stagnationTolerancePatience")
        ),
        "objectiveWeight": _sanitize_objective_weight(payload.get("objectiveWeight")),
        "objectiveTerms": _sanitize_objective_terms(payload.get("objectiveTerms")),
        "budgetEur": _sanitize_budget_eur(payload.get("budgetEur")),
        "serviceLevelMin": _sanitize_service_level_min(payload.get("serviceLevelMin")),
        "earlyUnchainingEnabled": _sanitize_early_unchaining_enabled(payload.get("earlyUnchainingEnabled")),
        **_read_cost_model_fields(payload),
    }


def _read_simulation_metadata_upload(uploaded_file) -> dict | None:
    """Parse an uploaded simulation_metadata.json file into the editable simulation draft format."""
    if not uploaded_file:
        return None
    filename = str(getattr(uploaded_file, "filename", "") or "").strip()
    if not filename:
        return None
    try:
        raw_bytes = uploaded_file.read()
        if not raw_bytes:
            return None
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _metadata_payload_to_draft(payload)


def _sim_metadata_filename(job: dict | None = None) -> str:
    """Return the metadata filename for a job, prefixed when schema naming is available."""
    parts = job.get("namingParts") if isinstance(job, dict) else None
    if isinstance(parts, dict) and parts:
        return f"{_build_sim_file_prefix(parts)}_simulation_metadata.json"
    return "simulation_metadata.json"


def _read_simulation_metadata_file(folder: str | None, job: dict | None = None, prefix: str | None = None) -> dict | None:
    """Load saved dashboard metadata, preferring the frontend-owned job state folder.

    ``prefix`` scopes the lookup to one scenario's metadata file when several
    scenario prefixes can share the same output folder (demo-mode discovery) -
    without it, a bare "simulation_metadata.json" would apply to every scenario
    found in that folder.
    """
    folders = []
    if isinstance(job, dict):
        state_folder = _dashboard_job_state_folder_path(job)
        if state_folder:
            folders.append(state_folder)
    if folder:
        folders.append(folder)

    names = []
    if prefix:
        names.append(f"{prefix}_simulation_metadata.json")
    job_name = _sim_metadata_filename(job)
    if job_name not in names:
        names.append(job_name)
    if "simulation_metadata.json" not in names:
        names.append("simulation_metadata.json")
    seen = set()
    for source_folder in folders:
        folder_key = os.path.normcase(os.path.abspath(source_folder))
        if folder_key in seen:
            continue
        seen.add(folder_key)
        for name in names:
            metadata_path = os.path.join(source_folder, name)
            if not os.path.isfile(metadata_path):
                continue
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                continue
            return payload if isinstance(payload, dict) else None
    return None


def _sanitize_token(value, default="na"):
    """Normalize a value into a lowercase alphanumeric token suitable for filenames."""
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "", raw)
    return cleaned or default


def _area_token_from_short_handle(value) -> str | None:
    """Extract the region token from operation-area metadata."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    token = re.split(r"[_\-\s]+", raw, maxsplit=1)[0]
    aliases = {"hoexter": "hx", "paderborn": "pb"}
    return aliases.get(token, _sanitize_token(token, ""))


def _derive_area_token() -> str:
    """Derive a short area token from operation-area metadata, falling back to legacy cab plates."""
    raw_base = loaded_data.get("_rawBaseData")
    if isinstance(raw_base, dict):
        op_areas = raw_base.get("operationAreas") or raw_base.get("operationArea") or []
        if isinstance(op_areas, dict):
            op_areas = [op_areas]
        if isinstance(op_areas, list):
            for area in op_areas:
                if not isinstance(area, dict):
                    continue
                token = _area_token_from_short_handle(area.get("ShortHandle") or area.get("shortHandle"))
                if token:
                    return token

    cabs = loaded_data.get("cabs", []) or []
    for cab in cabs:
        plate = str((cab or {}).get("licensePlate") or "").strip()
        if not plate:
            continue
        m = re.match(r"^([A-Za-z]+)-", plate)
        if m:
            return _sanitize_token(m.group(1), "pb")
    return "pb"


def _derive_stations_count() -> int:
    """Return the number of charging stations in the loaded base data."""
    return len(loaded_data.get("chargingPoints", []) or [])


def _derive_line_config_count() -> int:
    """Estimate the number of configured lines from the available chain routes."""
    routes = loaded_data.get("chainRoutes", []) or []
    return int(len(routes) / 2)


def _derive_battery_capacity_max() -> int:
    """Return the maximum cab battery capacity in kWh for simulation file naming."""
    values = []
    for cab in loaded_data.get("cabs", []) or []:
        try:
            values.append(
                float((cab or {}).get("TotalEnergyCapacity") or (cab or {}).get("InitialEnergyCapacity"))
            )
        except Exception:
            pass
    if not values:
        return 0
    value = max(values)
    return int(round(value / 1000)) if value >= 1000 else int(round(value))


def _derive_charging_power_max() -> int:
    """Return the maximum charging-point power in kW for simulation file naming."""
    values = []
    for cp in loaded_data.get("chargingPoints", []) or []:
        try:
            values.append(float((cp or {}).get("maximumPowerSupply")))
        except Exception:
            pass
    if not values:
        return 0
    value = max(values)
    return int(round(value / 1000)) if value >= 1000 else int(round(value))


def _derive_scenario_count() -> int:
    """Return the number of ride requests in the current scenario."""
    return len(loaded_data.get("rideRequests", []) or [])


def _parse_schema_datetime(value) -> datetime | None:
    """Parse request time-window timestamps used for schema naming."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except Exception:
        return None


def _request_timewindow_minutes(ride_request: dict) -> int | None:
    """Return the pickup or target time-window duration in minutes for one request."""
    if not isinstance(ride_request, dict):
        return None
    for key in ("pickupTime", "targetTime"):
        window = ride_request.get(key)
        if not isinstance(window, dict):
            continue
        start = _parse_schema_datetime(window.get("StartTime") or window.get("startTime"))
        end = _parse_schema_datetime(window.get("EndTime") or window.get("endTime"))
        if not start or not end or end <= start:
            continue
        return int(round((end - start).total_seconds() / 60))
    return None


def _derive_timewindow_minutes() -> int:
    """Derive the request time-window token from pickup/target intervals."""
    windows = []
    for rr in loaded_data.get("rideRequests", []) or []:
        minutes = _request_timewindow_minutes(rr)
        if minutes is not None:
            windows.append(minutes)
    unique = set(windows)
    if len(unique) == 1:
        return windows[0]
    return 0


def _build_auto_naming_base() -> dict:
    """Derive the human-readable naming tokens used for generated input/output filenames.

    Counts and maxima only, so this is a cosmetic label. Job identity comes from
    _input_content_hash()."""
    return {
        "area": _derive_area_token(),
        "stations": f"{_derive_stations_count()}cs",
        "lineConfig": f"{_derive_line_config_count()}lines",
        "batteryCapacity": f"{_derive_battery_capacity_max()}bc",
        "chargingPower": f"{_derive_charging_power_max()}cp",
        "scenario": f"{_derive_scenario_count()}rq",
        "timewindow": f"{_derive_timewindow_minutes()}tw",
    }


def _input_content_hash() -> str:
    """Short hash of the solver input files as they would be written for a job right now (see
    _save_input_files_for_job), so edits made in the dashboard change it as well as uploads.
    It is the input part of a job's signature and changes with any content change, for example
    a moved depot."""
    base = write_api_base_file_from_loaded_data(loaded_data)
    requests = write_api_ride_file_from_loaded_data(loaded_data) if loaded_data.get("rideRequests") else None
    blob = json.dumps([base, requests], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _build_sim_naming_parts(base_parts: dict, algorithm: str, run_id: int, search_mode=None) -> dict:
    """Combine derived scenario tokens with algorithm, run id, and search mode for consistent
    simulation naming - kept on namingParts for display/lookup purposes (_build_sim_folder_name
    itself now takes search_mode and the full settings signature directly, see there for why)."""
    base = dict(base_parts or {})
    base["algorithm"] = _sanitize_token(algorithm, "custom")
    base["runId"] = str(max(1, int(run_id)))
    base["searchMode"] = _sanitize_search_mode(search_mode)
    return base


def _job_signature(job: dict) -> tuple:
    """Return the input-data portion of a simulation job signature."""
    return (str((job or {}).get("inputContentHash") or ""),)


def _cost_model_signature(source: dict | None) -> tuple:
    """All 13 cost-model fields (camelCase keys), in COST_MODEL_FIELDS order, rounded for
    signature comparison. cabPrice/proPrice/stationaryKwhPrice/proKwhPrice and the amortization
    fields (cabLifetimeYears/proLifetimeYears/interestRatePercent/operatingDaysPerYear) already
    change the search itself - avg_cost_per_trip_eur (CostModel.daily_fleet_cost(), which all of
    those feed) is the default search objective term, and budget_eur's hard constraint compares
    against CostModel.fleet_price_eur(). The fare-model fields feed avg_profit_per_trip_eur
    (CostModel.trip_fare()), a selectable objective term too - deliberately the whole model, not
    just the fields that were relevant when this was first written."""
    fields = _read_cost_model_fields(source)
    return tuple(round(fields[json_key], 6) for json_key, _ in COST_MODEL_FIELDS)


def _job_fixedfleet_signature(job: dict) -> tuple:
    """Signature for a fixed-fleet job (custom_sim/run_instance.py, no FleetPlanning search) -
    only the fields that actually change what gets simulated or how its KPIs are computed
    afterwards: the input-data signature, cost model prices, and earlyUnchainingEnabled. Every
    other field _job_full_signature() below cares about (searchMode, iterLimit, budget_eur, ...)
    only ever steers a search, meaningless once only one fleet is being evaluated. Deliberately a
    different-length tuple than the search-job signature, and led by a fixed "fixedfleet" marker,
    so the two kinds can never accidentally compare equal."""
    return (
        "fixedfleet",
        _job_signature(job),
        _to_float_or_default((job or {}).get("cabPrice"), SIM_DEFAULT_CAB_PRICE),
        _to_float_or_default((job or {}).get("proPrice"), SIM_DEFAULT_PRO_PRICE),
        _to_float_or_default((job or {}).get("stationaryKwhPrice"), SIM_DEFAULT_STATIONARY_KWH_PRICE),
        _sanitize_early_unchaining_enabled((job or {}).get("earlyUnchainingEnabled")),
    )


def _job_full_signature(job: dict) -> tuple:
    """Return the full simulation signature - every field that makes a run a genuinely different
    simulation rather than just a different price/reporting label on the same one. Field-for-
    field mirror of simulation_overview()'s queue-time full_signature construction, so
    _find_matching_job() can actually compare a candidate launch against existing jobs (the two
    used to silently diverge in shape - a 5-tuple here vs. an 8-tuple there - so the comparison
    could never match anything; fixed together with adding useOriginalProTimetable below).
    Fixed-fleet jobs (see _job_fixedfleet_signature) branch off entirely - they have no search
    parameters to mirror here."""
    if (job or {}).get("runKind") == "fixedfleet":
        return _job_fixedfleet_signature(job)
    np = (job or {}).get("namingParts") or {}
    algorithm_token = _sanitize_token(np.get("algorithm"), "custom")
    objective_terms = _sanitize_objective_terms((job or {}).get("objectiveTerms"))
    objective_weight = _sanitize_objective_weight((job or {}).get("objectiveWeight"))
    budget_eur = _sanitize_budget_eur((job or {}).get("budgetEur"))
    service_level_min = _sanitize_service_level_min((job or {}).get("serviceLevelMin"))
    early_unchaining_enabled = _sanitize_early_unchaining_enabled((job or {}).get("earlyUnchainingEnabled"))
    search_mode = _sanitize_search_mode((job or {}).get("searchMode"))
    shrink_pros_allow_retry = _sanitize_shrink_pros_allow_retry((job or {}).get("shrinkProsAllowRetry"))
    shrink_pros_max_tries = _sanitize_shrink_pros_max_tries((job or {}).get("shrinkProsMaxTries"))
    shrink_pros_trial_probe_below_start = _sanitize_shrink_pros_trial_probe_below_start(
        (job or {}).get("shrinkProsTrialProbeBelowStart")
    )
    shrink_pros_trial_overshoot_correction = _sanitize_shrink_pros_trial_overshoot_correction(
        (job or {}).get("shrinkProsTrialOvershootCorrection")
    )
    return (
        _job_signature(job),
        algorithm_token,
        _cost_model_signature(job),
        search_mode,
        _sanitize_iter_limit((job or {}).get("iterLimit")),
        round(_sanitize_time_limit((job or {}).get("timeLimit")), 1),
        _sanitize_initial_cabs((job or {}).get("initialCabs")),
        _sanitize_cab_add_step((job or {}).get("cabAddStep")),
        round(_sanitize_stagnation_tolerance((job or {}).get("stagnationTolerance")), 6),
        _sanitize_stagnation_tolerance_patience((job or {}).get("stagnationTolerancePatience")),
        tuple(objective_terms),
        round(objective_weight, 4) if len(objective_terms) == 2 else None,
        bool((job or {}).get("useOriginalProTimetable")),
        round(budget_eur, 2) if budget_eur is not None else None,
        round(service_level_min, 4) if service_level_min is not None else None,
        early_unchaining_enabled if algorithm_token == "custom" else True,
        shrink_pros_allow_retry if search_mode == "adaptive" else SIM_DEFAULT_SHRINK_PROS_ALLOW_RETRY,
        shrink_pros_max_tries if search_mode == "adaptive" else SIM_DEFAULT_SHRINK_PROS_MAX_TRIES,
        (
            shrink_pros_trial_probe_below_start if search_mode == "adaptive"
            else SIM_DEFAULT_SHRINK_PROS_TRIAL_PROBE_BELOW_START
        ),
        (
            shrink_pros_trial_overshoot_correction if search_mode == "adaptive"
            else SIM_DEFAULT_SHRINK_PROS_TRIAL_OVERSHOOT_CORRECTION
        ),
    )


def _next_run_id_for_base(base_signature: tuple) -> int:
    """Find the next run id for jobs that share the same input-data signature."""
    jobs = loaded_data.get("simulationJobs", []) or []
    max_run_id = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if _job_signature(job) != base_signature:
            continue
        np = job.get("namingParts") or {}
        try:
            rid = int(str(np.get("runId") or "0"))
            max_run_id = max(max_run_id, rid)
        except Exception:
            pass
    return max_run_id + 1


def _find_matching_job(full_signature: tuple) -> dict | None:
    """Return an existing simulation job whose full signature matches the requested setup."""
    jobs = loaded_data.get("simulationJobs", []) or []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if _job_full_signature(job) == full_signature:
            return job
    return None


def _build_sim_folder_name(label: str, full_signature: tuple) -> str:
    """Build the result folder name for the active fleet-planning setup.

    FLEETPLANNING_SETUP_NAME, when a deployer has explicitly set it, overrides this outright -
    their deliberate choice to collapse every setup into one fixed folder. Otherwise: `label` as
    a readable prefix, plus a short deterministic hash of the full settings signature (the same
    tuple _job_full_signature()/_find_matching_job() use for dedup) - see frontend/README.md's
    "Output folder naming" section for the full rationale and the determinism guarantees this
    relies on. `label` is used as-is, not validated against fleet_planning.py's own
    --search_mode choices - callers pass a real (already-sanitized) search mode for dashboard-
    launched jobs, or a fixed label like "upload" for jobs that didn't come from a search at all
    (see upload_output_auto()) - sanitizing here would silently coerce the latter back to the
    default search mode, which is exactly the bug this docstring note exists to prevent someone
    from reintroducing.
    """
    if FLEETPLANNING_SETUP_NAME:
        return FLEETPLANNING_SETUP_NAME
    settings_hash = hashlib.sha256(repr(full_signature).encode()).hexdigest()[:8]
    return f"{label}_{settings_hash}"


def _build_sim_file_prefix(parts: dict) -> str:
    """Build the common filename prefix used by generated input and output files."""
    return (
        f"{parts['area']}_{parts['stations']}_{parts['lineConfig']}_{parts['batteryCapacity']}_"
        f"{parts['chargingPower']}_{parts['scenario']}_{parts['timewindow']}"
    )


_SCENARIO_FACT_PREFIX_RE = re.compile(
    r"_(?P<stations>\d+)cs_(?P<lines>\d+)lines_(?P<batteryCapacity>\d+)bc_"
    r"(?P<chargingPower>\d+)cp_(?P<requests>\d+)rq_(?P<timewindow>\d+)tw_"
)


def _parse_scenario_facts_from_prefix(prefix: str | None) -> dict:
    """Extract fixed, iteration-independent scenario facts from the generated filename prefix.

    These describe the operational-area/demand setup (charging stations, PRO lines, total
    requests, ...) - values that never change across a run's iterations, unlike per-iteration
    fleet-planning results, so they are surfaced separately from the iteration-scoped summary.
    """
    if not prefix:
        return {}
    match = _SCENARIO_FACT_PREFIX_RE.search(f"_{prefix}_")
    if not match:
        return {}
    return {
        "numChargingStations": int(match.group("stations")),
        "numProLines": int(match.group("lines")),
        "batteryCapacityKwh": int(match.group("batteryCapacity")),
        "chargingPowerKw": int(match.group("chargingPower")),
        "numRequests": int(match.group("requests")),
        "timeWindowMin": int(match.group("timewindow")),
    }


def _build_sim_filename(parts: dict, file_io_type: str, iteration: int) -> str:
    """Build a generated simulation filename for one IO type and iteration."""
    prefix = _build_sim_file_prefix(parts)
    return f"{prefix}_{file_io_type}_{iteration}.json"


DEMO_OUTPUT_FILE_RE = re.compile(r"^(?P<prefix>.+)_output_(?P<kind>cab|sim|pro)_(?P<iteration>\d+)\.json$", re.IGNORECASE)


def _read_simple_text_config_lines(path: str) -> list[str]:
    """Read a small line-based config file, ignoring empty lines and comments."""
    if not os.path.isfile(path):
        return []
    lines = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        return []
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _load_demo_scenario_name_overrides() -> dict[str, dict[str, str]]:
    """Load the demo-scenario config: which discovered scenarios are selected to appear in demo
    mode, and optionally their display title/summary - see _discover_demo_scenarios, which uses
    this same dict as both the selection allow-list and the naming override (one file, one
    curation pass - listing a scenario here is what makes it show up at all).

    Format, one entry per active line - key is the generated scenario id, its raw filename
    prefix, or "algorithm/setup/prefix" (see _demo_scenario_lookup_keys):
      key                     -> select it, keep the generated fallback title/summary
      key = Title             -> select it, with this title
      key = Title | Summary   -> select it, with this title and summary

    Missing file, or a file with no active lines, means no curation has happened yet: every
    discovered scenario shows, with its generated fallback title - same show-everything-until-
    configured default demo_kpis.txt uses."""
    overrides: dict[str, dict[str, str]] = {}
    for line in _read_simple_text_config_lines(DEMO_SCENARIO_NAMES_FILE):
        sep = "=" if "=" in line else ":" if ":" in line else ""
        if not sep:
            key = line.strip()
            if key:
                overrides.setdefault(key, {})
            continue
        key, value = line.split(sep, 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        entry = overrides.setdefault(key, {})
        title, summary = (value.split("|", 1) + [""])[:2]
        if title.strip():
            entry["title"] = title.strip()
        if summary.strip():
            entry["summary"] = summary.strip()
    return overrides


def _demo_scenario_lookup_keys(scenario: dict) -> list[str]:
    """Every string a demo_scenario_names.txt entry is allowed to key a discovered scenario by."""
    return [
        str(scenario.get("id") or ""),
        str(scenario.get("prefix") or ""),
        str(scenario.get("detail") or ""),
        f"{scenario.get('algorithm')}/{scenario.get('setup')}/{scenario.get('prefix')}",
    ]


def _apply_demo_scenario_name_override(scenario: dict, overrides: dict[str, dict[str, str]]) -> dict:
    """Apply a matching scenario title/summary override while preserving discovery fallbacks."""
    if not overrides:
        return scenario
    override = next(
        (overrides.get(key) for key in _demo_scenario_lookup_keys(scenario) if key in overrides), None
    )
    if not override:
        return scenario
    if override.get("title"):
        scenario["title"] = override["title"]
    if override.get("summary"):
        scenario["summary"] = override["summary"]
    return scenario


def _load_demo_kpi_filter() -> list[str]:
    """Load the optional ordered KPI allow-list for demo output selectors."""
    keys = []
    seen = set()
    for line in _read_simple_text_config_lines(DEMO_KPIS_FILE):
        key = line.split("=", 1)[0].strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _load_demo_chart_exclusions() -> set[str]:
    """Load demo-mode KPI keys excluded from chart Y-axis eligibility (KPI table stays unaffected)."""
    return {line for line in _read_simple_text_config_lines(DEMO_CHART_EXCLUSIONS_FILE) if line}


def _apply_demo_kpi_filter(payload: dict | None) -> dict | None:
    """Limit visible demo KPIs and mark chart-axis-ineligible ones, per the optional text configs."""
    if not isinstance(payload, dict):
        return payload
    paired = payload.get("paired")
    if not isinstance(paired, dict):
        return payload
    labels = paired.get("labels")
    if not isinstance(labels, dict):
        return payload

    filtered_payload = dict(payload)
    filtered_paired = dict(paired)

    keys = _load_demo_kpi_filter()
    active_keys = ["numCabs"] + [key for key in keys if key != "numCabs" and key in labels]
    if keys and len(active_keys) > 1:
        filtered_paired["labels"] = {key: labels[key] for key in active_keys if key in labels}

    filtered_paired["chartIneligible"] = sorted(_load_demo_chart_exclusions())
    filtered_payload["paired"] = filtered_paired
    return filtered_payload


def _demo_scenario_id(algorithm: str, setup: str, prefix: str) -> str:
    """Build a stable URL-safe id for a prepared demo scenario."""
    return _sanitize_token(f"{algorithm}__{setup}__{prefix}", "demo")


def _demo_scenario_title(prefix: str, algorithm: str) -> str:
    """Build a compact display title from a schema filename prefix."""
    tokens = str(prefix or "").split("_")
    if len(tokens) >= 7:
        area, stations, lines, _, _, requests, timewindow = tokens[:7]
        return f"{area.upper()} | {lines} | {stations} | {requests} | {timewindow}"
    return f"{algorithm}: {prefix}"


def _demo_scenario_summary(prefix: str) -> str:
    """Build a non-technical display summary from a schema filename prefix."""
    tokens = str(prefix or "").split("_")
    if len(tokens) >= 7:
        _, stations, lines, battery, power, requests, timewindow = tokens[:7]
        return f"{lines} Linien, {stations} Ladepunkte, {requests} Fahranfragen, {timewindow} Zeitfenster"
    return "Konfiguriertes Flottenplanungsszenario"


def _discover_demo_scenarios() -> list[dict]:
    """Discover prepared demo scenarios from schema-compliant backend output folders, filtered
    through demo_scenario_names.txt when it has any active lines (see
    _load_demo_scenario_name_overrides) - an allow-list, not a deny-list: real output folders
    accumulate all kinds of parameter-sweep/one-off/fixedfleet/test runs over time, and naming
    the handful actually worth showing a demo visitor is far shorter than excluding new noise as
    it appears. A configured key can match more than one output folder (the same prefix run
    under different search settings, or evaluated as a fixed fleet) - all matches show, keeping
    the deployed output folders free of that is on whoever curates them, not checked here."""
    scenarios = []
    seen = set()
    scenario_name_overrides = _load_demo_scenario_name_overrides()
    for algorithm, root in sorted(ALGORITHM_OUTPUT_ROOTS.items()):
        if not os.path.isdir(root):
            continue
        for setup in sorted(os.listdir(root)):
            folder = os.path.join(root, setup)
            if not os.path.isdir(folder):
                continue
            grouped = {}
            for fn in os.listdir(folder):
                match = DEMO_OUTPUT_FILE_RE.match(fn)
                if not match:
                    continue
                prefix = match.group("prefix")
                kind = match.group("kind").lower()
                entry = grouped.setdefault(
                    prefix,
                    {
                        "id": _demo_scenario_id(algorithm, setup, prefix),
                        "algorithm": algorithm,
                        "setup": setup,
                        "prefix": prefix,
                        "folder": folder,
                        "counts": {"sim": 0, "cab": 0, "pro": 0},
                    },
                )
                entry["counts"][kind] += 1
            for scenario in grouped.values():
                if sum(scenario["counts"].values()) <= 0:
                    continue
                key = scenario["id"]
                if key in seen:
                    continue
                seen.add(key)
                scenario["title"] = _demo_scenario_title(scenario["prefix"], scenario["algorithm"])
                scenario["summary"] = _demo_scenario_summary(scenario["prefix"])
                scenario["detail"] = f"{scenario['algorithm']} / {scenario['setup']} / {scenario['prefix']}"
                if scenario_name_overrides and not any(
                    lookup_key in scenario_name_overrides for lookup_key in _demo_scenario_lookup_keys(scenario)
                ):
                    continue
                scenario_metadata = _read_simulation_metadata_file(
                    scenario["folder"], None, prefix=scenario["prefix"]
                )
                scenario["searchMode"] = _sanitize_search_mode(
                    (scenario_metadata or {}).get("searchMode")
                )
                _apply_demo_scenario_name_override(scenario, scenario_name_overrides)
                scenarios.append(scenario)
    scenarios.sort(key=lambda item: (item.get("title") or "", item.get("detail") or ""))
    return scenarios


def _selected_demo_scenario(scenarios: list[dict] | None = None) -> dict | None:
    """Resolve the selected demo scenario from the query string or fall back to the first available one."""
    scenarios = scenarios if scenarios is not None else _discover_demo_scenarios()
    if not scenarios:
        return None
    selected_id = (request.args.get("demo_scenario") or "").strip()
    for scenario in scenarios:
        if scenario.get("id") == selected_id:
            return scenario
    return scenarios[0]


def _demo_query(scenario: dict | None = None) -> str:
    """Return a query string preserving the selected demo scenario and price assumptions."""
    params = {}
    if scenario:
        params["demo_scenario"] = scenario.get("id") or ""
    for key, value in _demo_price_values().items():
        params[key] = str(value)
    if not params:
        return ""
    return "?" + urlencode(params)


def _demo_price_values() -> dict:
    """Read temporary demo price assumptions from the request query string."""
    return {
        "cab_price": _to_float_or_default(request.args.get("cab_price"), SIM_DEFAULT_CAB_PRICE),
        "pro_price": _to_float_or_default(request.args.get("pro_price"), SIM_DEFAULT_PRO_PRICE),
        "stationary_kwh_price": _to_float_or_default(
            request.args.get("stationary_kwh_price"), SIM_DEFAULT_STATIONARY_KWH_PRICE
        ),
    }


def _empty_state() -> dict:
    """Return an isolated state object with the same shape as the mutable dashboard state."""
    return {
        "cabs": [],
        "chargingPoints": [],
        "proSchedules": [],
        "chainingLocations": [],
        "chainRoutes": [],
        "chainRouteSchedules": [],
        "rideRequests": [],
        "operationArea": {},
        "startTime": None,
        "endTime": None,
        "outputCabRuns": [],
        "outputSimRuns": [],
        "outputProRuns": [],
        "_outputParsed": None,
        "_outputSimulationMetadata": None,
    }


def _scenario_file_path(folder: str | None, prefix: str | None, file_io_type: str, iteration: int = 0) -> str:
    """Build a path to a schema-compliant file inside a scenario's folder.

    A "scenario" here is any (folder, prefix) pair pointing at a directory of
    input_base_file/input_req_file/output_cab/sim/pro/simulation_metadata files following the
    naming convention shared by prepared demo scenarios and real dashboard-job output alike.
    """
    return os.path.join(folder or "", f"{prefix}_{file_io_type}_{iteration}.json")


def _load_scenario_input_state(folder: str | None, prefix: str | None) -> dict:
    """Load one scenario's BaseData/RideData input into an isolated state object.

    Shared by demo-scenario viewing (via _load_demo_input_state below) and anything else that
    only needs a scenario's inputs, not its output runs.
    """
    state = _empty_state()
    if not folder or not prefix:
        return state

    base_path = _scenario_file_path(folder, prefix, "input_base_file", 0)
    if os.path.isfile(base_path):
        try:
            with open(base_path, "r", encoding="utf-8") as f:
                raw_base = json.load(f)
            state.update(normalize_base_data(raw_base))
            state["_rawBaseData"] = raw_base
            state["_operationAreaSource"] = "base"
        except Exception:
            pass

    req_path = _scenario_file_path(folder, prefix, "input_req_file", 0)
    if os.path.isfile(req_path):
        try:
            with open(req_path, "r", encoding="utf-8") as f:
                raw_req = json.load(f)
            state.update(normalize_ride_data(raw_req))
            state["_rawRideData"] = raw_req
        except Exception:
            pass
    return state


def _load_demo_input_state(scenario: dict | None) -> dict:
    """Load prepared input data for a demo scenario without changing global loaded_data."""
    if not scenario:
        return _empty_state()
    return _load_scenario_input_state(scenario.get("folder"), scenario.get("prefix"))


def _load_scenario_output_state(folder: str | None, prefix: str | None, job: dict | None = None) -> dict:
    """Load one scenario's full input+output state from its own files.

    The single shared loader behind both demo-scenario viewing and finished-job viewing: both
    reduce to the same (folder, prefix) pointing at a directory of schema-compliant files, so
    both read through here instead of each keeping their own file-scanning/parsing logic.
    ``job`` is optional and only widens the simulation-metadata lookup to also check the
    frontend-owned dashboard job-state folder (see _read_simulation_metadata_file) - demo
    scenarios have no such folder and never pass it.
    """
    state = _load_scenario_input_state(folder, prefix)
    if not folder or not prefix:
        state["_outputParsed"] = build_output_payload(state)
        return state

    state["_outputSimulationMetadata"] = _read_simulation_metadata_file(folder, job, prefix=prefix)

    derived_points = []
    output_pattern = re.compile(rf"^{re.escape(prefix)}_output_(cab|sim|pro)_\d+\.json$", re.IGNORECASE)
    if os.path.isdir(folder):
        for fn in sorted(os.listdir(folder)):
            match = output_pattern.match(fn)
            if not match:
                continue
            path = os.path.join(folder, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                continue
            run_type = match.group(1).lower()
            if run_type == "cab":
                state["outputCabRuns"].append(parse_output_cab_json(raw, fn))
                derived_points.extend(_collect_points_from_output(raw))
            elif run_type == "sim":
                state["outputSimRuns"].append(parse_output_sim_json(raw, fn))
            elif run_type == "pro":
                state["outputProRuns"].append(parse_output_pro_json(raw, fn))
                derived_points.extend(_collect_points_from_output(raw))

    # only consumed by the normal-mode job adapter's crude-bounding-box fallback below - demo
    # scenarios always ship a real BaseData operation area, so this is unused there.
    state["_derivedPoints"] = derived_points
    state["_outputParsed"] = build_output_payload(state)
    return state


def _load_demo_output_state(scenario: dict | None) -> dict:
    """Load prepared output data for a demo scenario without changing global loaded_data."""
    state = _load_scenario_output_state(
        scenario.get("folder") if scenario else None,
        scenario.get("prefix") if scenario else None,
    )
    if not scenario:
        return state

    metadata = state.get("_outputSimulationMetadata") or {}
    # `or`, not setdefault: a saved metadata file can have these keys present but blank (e.g.
    # a run queued with no scenario name typed in) - setdefault only fills a missing key, so an
    # explicit "" would otherwise never fall back to the scenario's own real title/algorithm.
    metadata["scenarioName"] = metadata.get("scenarioName") or scenario.get("title") or ""
    metadata["algorithm"] = metadata.get("algorithm") or scenario.get("algorithm") or "custom"
    metadata.setdefault("cabPrice", SIM_DEFAULT_CAB_PRICE)
    metadata.setdefault("proPrice", SIM_DEFAULT_PRO_PRICE)
    metadata.setdefault("stationaryKwhPrice", SIM_DEFAULT_STATIONARY_KWH_PRICE)
    metadata["searchMode"] = _sanitize_search_mode(metadata.get("searchMode"))
    prices = _demo_price_values()
    metadata["cabPrice"] = prices["cab_price"]
    metadata["proPrice"] = prices["pro_price"]
    metadata["stationaryKwhPrice"] = prices["stationary_kwh_price"]
    state["_outputSimulationMetadata"] = metadata
    return state


def _normalize_sim_setup_name(folder_name: str) -> str:
    """Normalize a folder or metadata filename into a safe simulation setup name."""
    normalized = os.path.basename(str(folder_name or "").replace("\\", "/").strip())
    if normalized.lower().endswith(".json"):
        normalized = normalized[:-5]
    return normalized


def _job_algorithm_token(job: dict) -> str:
    """Resolve the algorithm token from a job, preferring naming parts over legacy fields."""
    parts = job.get("namingParts") if isinstance(job, dict) else None
    if isinstance(parts, dict):
        token = _sanitize_token(parts.get("algorithm"), "")
        if token:
            return token
    return _sanitize_token((job or {}).get("algorithm"), "custom")


def _algorithm_output_root(algorithm: str) -> str:
    """Return the output root folder for a simulation algorithm."""
    token = _sanitize_token(algorithm, "custom")
    return ALGORITHM_OUTPUT_ROOTS.get(token, ALGORITHM_OUTPUT_ROOTS["custom"])


def _job_setup_folder_name(job: dict) -> str:
    """Resolve the canonical setup folder name for a simulation job."""
    folder_name = _normalize_sim_setup_name((job or {}).get("folderName"))
    if folder_name:
        return folder_name
    parts = (job or {}).get("namingParts")
    if isinstance(parts, dict) and parts:
        label = "fixedfleet" if (job or {}).get("runKind") == "fixedfleet" else _sanitize_search_mode(job.get("searchMode"))
        return _build_sim_folder_name(label, _job_full_signature(job))
    return ""


def _build_sim_metadata_payload(job: dict) -> dict:
    """Create the JSON payload persisted as simulation_metadata.json for a simulation job."""
    parts = job.get("namingParts") if isinstance(job, dict) else {}
    if not isinstance(parts, dict):
        parts = {}
    return {
        "id": (job or {}).get("id"),
        "inputContentHash": (job or {}).get("inputContentHash"),
        "scenarioName": (job or {}).get("scenarioName") or "",
        "algorithm": parts.get("algorithm") or (job or {}).get("algorithm") or "custom",
        "runKind": (job or {}).get("runKind") or "search",
        "searchMode": _sanitize_search_mode((job or {}).get("searchMode")),
        "iterLimit": _sanitize_iter_limit((job or {}).get("iterLimit")),
        "timeLimit": _sanitize_time_limit((job or {}).get("timeLimit")),
        "initialCabs": _sanitize_initial_cabs((job or {}).get("initialCabs")),
        "cabAddStep": _sanitize_cab_add_step((job or {}).get("cabAddStep")),
        "shrinkProsAllowRetry": _sanitize_shrink_pros_allow_retry((job or {}).get("shrinkProsAllowRetry")),
        "shrinkProsMaxTries": _sanitize_shrink_pros_max_tries((job or {}).get("shrinkProsMaxTries")),
        "shrinkProsTrialProbeBelowStart": _sanitize_shrink_pros_trial_probe_below_start(
            (job or {}).get("shrinkProsTrialProbeBelowStart")
        ),
        "shrinkProsTrialOvershootCorrection": _sanitize_shrink_pros_trial_overshoot_correction(
            (job or {}).get("shrinkProsTrialOvershootCorrection")
        ),
        "stagnationTolerance": _sanitize_stagnation_tolerance((job or {}).get("stagnationTolerance")),
        "stagnationTolerancePatience": _sanitize_stagnation_tolerance_patience(
            (job or {}).get("stagnationTolerancePatience")
        ),
        "objectiveWeight": _sanitize_objective_weight((job or {}).get("objectiveWeight")),
        "objectiveTerms": _sanitize_objective_terms((job or {}).get("objectiveTerms")),
        "budgetEur": _sanitize_budget_eur((job or {}).get("budgetEur")),
        "serviceLevelMin": _sanitize_service_level_min((job or {}).get("serviceLevelMin")),
        "useOriginalProTimetable": bool((job or {}).get("useOriginalProTimetable")),
        "earlyUnchainingEnabled": _sanitize_early_unchaining_enabled((job or {}).get("earlyUnchainingEnabled")),
        **_read_cost_model_fields(job),
        "folderName": _job_setup_folder_name(job),
        # read back by fleet_planning.py itself via --custom_sim_experiment_config (this same
        # file), so custom_sim's own output actually lands in the folder the dashboard expects
        # (custom_simulation.py only writes to a config_folder subfolder when told to - see
        # apply_custom_sim_experiment_config()/CustomSimulation's experiment_output handling),
        # and so early_unchaining actually reaches CustomSimulation's own
        # parameters["algorithm"]["early_unchaining"]["enabled"] read (see
        # custom_simulation.py) - the flat earlyUnchainingEnabled key above is this file's own
        # round-trip representation (import/job-load), this nested one is what
        # apply_custom_sim_experiment_config() actually looks for. Both harmless for rw/sumo
        # jobs, which never read either key.
        "custom_sim": {
            "experiment_output": {"config_folder": _job_setup_folder_name(job)},
            "algorithm": {
                "early_unchaining": {
                    "enabled": _sanitize_early_unchaining_enabled((job or {}).get("earlyUnchainingEnabled"))
                }
            },
        },
        "status": (job or {}).get("status"),
        "createdAt": (job or {}).get("createdAt"),
        "startedAt": (job or {}).get("startedAt"),
        "completedAt": (job or {}).get("completedAt"),
        "failedAt": (job or {}).get("failedAt"),
        "solver": (job or {}).get("solver"),
        "solverReturnCode": (job or {}).get("solverReturnCode"),
        "solverError": (job or {}).get("solverError"),
    }


def _dashboard_job_state_folder_path(job: dict) -> str:
    """Return the frontend-owned folder for dashboard metadata and solver logs."""
    job_id = secure_filename(str((job or {}).get("id") or "").strip())
    if not job_id:
        parts = (job or {}).get("namingParts")
        job_id = secure_filename(_build_sim_file_prefix(parts)) if isinstance(parts, dict) and parts else "job"
    return os.path.join(DASHBOARD_JOB_STATE_ROOT, job_id)


def _write_sim_metadata_file(job: dict) -> str:
    """Persist dashboard metadata outside the backend-owned simulation output folder."""
    target_folder = _dashboard_job_state_folder_path(job)
    if not target_folder:
        return ""
    _ensure_folder(target_folder)
    metadata_path = os.path.join(target_folder, _sim_metadata_filename(job))
    _write_json_file(metadata_path, _build_sim_metadata_payload(job))
    return metadata_path


def _legacy_sim_job_folder_candidates(job: dict) -> list[str]:
    """Return possible legacy result-folder paths for a job before algorithm-specific roots existed."""
    candidates = []
    seen = set()
    for raw_name in (str((job or {}).get("folderName") or "").strip(), _job_setup_folder_name(job)):
        if not raw_name:
            continue
        candidate = os.path.join(SIM_RESULTS_ROOT, raw_name)
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        candidates.append(candidate)
    return candidates


def _migrate_legacy_job_folder(job: dict, target_folder: str) -> str:
    """Move one job's legacy folder to its canonical target path when needed."""
    if not target_folder:
        return ""
    if os.path.isdir(target_folder):
        return target_folder
    for legacy_folder in _legacy_sim_job_folder_candidates(job):
        if not os.path.isdir(legacy_folder):
            continue
        _ensure_folder(os.path.dirname(target_folder))
        try:
            shutil.move(legacy_folder, target_folder)
            return target_folder
        except OSError:
            return legacy_folder
    return target_folder


def _sim_job_folder_path(job: dict) -> str:
    """Return the canonical filesystem path for a simulation job's result folder."""
    setup_name = _job_setup_folder_name(job)
    if not setup_name:
        return ""
    target_folder = os.path.join(_algorithm_output_root(_job_algorithm_token(job)), setup_name)
    return _migrate_legacy_job_folder(job, target_folder)


def _infer_algorithm_from_legacy_folder_name(folder_name: str) -> str:
    """Infer the algorithm token from an older folder name when metadata is unavailable."""
    tokens = [tok for tok in _normalize_sim_setup_name(folder_name).split("_") if tok]
    for token in reversed(tokens):
        if token in ALGORITHM_OUTPUT_ROOTS:
            return token
    return "custom"


def _migrate_legacy_sim_results() -> int:
    """Move legacy result folders into algorithm-specific output roots when old paths still exist."""
    if not os.path.isdir(SIM_RESULTS_ROOT):
        return 0
    migrated = 0
    for entry in os.listdir(SIM_RESULTS_ROOT):
        legacy_folder = os.path.join(SIM_RESULTS_ROOT, entry)
        if not os.path.isdir(legacy_folder):
            continue
        if os.path.normcase(os.path.abspath(legacy_folder)) == os.path.normcase(os.path.abspath(DASHBOARD_JOB_STATE_ROOT)):
            continue
        setup_name = _normalize_sim_setup_name(entry)
        if not setup_name:
            continue
        target_root = _algorithm_output_root(_infer_algorithm_from_legacy_folder_name(entry))
        target_folder = os.path.join(target_root, setup_name)
        if os.path.normcase(os.path.abspath(legacy_folder)) == os.path.normcase(os.path.abspath(target_folder)):
            continue
        if os.path.exists(target_folder):
            continue
        _ensure_folder(target_root)
        try:
            shutil.move(legacy_folder, target_folder)
            migrated += 1
        except OSError:
            continue
    return migrated


def _find_latest_running_job() -> dict | None:
    """Return the most recently created running simulation job."""
    jobs = loaded_data.get("simulationJobs", []) or []
    running = [j for j in jobs if isinstance(j, dict) and str(j.get("status") or "").lower() == "running"]
    if not running:
        return None
    return running[-1]


def _ensure_folder(path: str):
    """Create a folder path if it is non-empty and does not already exist."""
    if path:
        os.makedirs(path, exist_ok=True)


_migrate_legacy_sim_results()


def _write_json_file(path: str, payload: dict):
    """Write a JSON payload using the app's standard UTF-8 pretty-print format."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


JOB_COUNTERS_PATH = os.path.join(SIM_RESULTS_ROOT, "job_counters.json")


def _read_job_counters() -> dict:
    """Read the persisted job-id counters, defaulting to a fresh start."""
    try:
        with open(JOB_COUNTERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"nextId": 1, "deletedCount": 0, "interruptedCount": 0}


def _job_state_folder_numbers() -> set:
    """N of every job-N folder currently in dashboard_jobs/."""
    if not os.path.isdir(DASHBOARD_JOB_STATE_ROOT):
        return set()
    return {
        int(name[4:]) for name in os.listdir(DASHBOARD_JOB_STATE_ROOT)
        if re.match(r"^job-\d+$", name) and os.path.isdir(os.path.join(DASHBOARD_JOB_STATE_ROOT, name))
    }


def _next_job_id() -> str:
    """Issue a job id that is never reused after its job is deleted. The number comes from a
    persisted counter and is kept above every existing job-N folder and live job id, so a lost
    or reset counter file cannot hand out an id that is in use. The job's state folder is
    created here, so an issued id and its folder exist together."""
    with SIMULATION_JOB_LOCK:
        counters = _read_job_counters()
        live = [
            int(j["id"][4:]) for j in loaded_data.get("simulationJobs", [])
            if isinstance(j, dict) and re.match(r"^job-\d+$", str(j.get("id") or ""))
        ]
        number = max(int(counters.get("nextId") or 1), max(_job_state_folder_numbers() | set(live) | {0}) + 1)
        counters["nextId"] = number + 1
        os.makedirs(os.path.join(DASHBOARD_JOB_STATE_ROOT, f"job-{number}"), exist_ok=True)
        _write_json_file(JOB_COUNTERS_PATH, counters)
        return f"job-{number}"


def _record_job_deletion(interrupted: bool):
    """Count a job deletion so the consistency check can notice a manually removed job folder."""
    with SIMULATION_JOB_LOCK:
        counters = _read_job_counters()
        key = "interruptedCount" if interrupted else "deletedCount"
        counters[key] = int(counters.get(key) or 0) + 1
        _write_json_file(JOB_COUNTERS_PATH, counters)


def _job_counters_mismatch() -> bool:
    """True if dashboard_jobs/ does not hold exactly the folders job-1 to job-<ids issued>, which
    means a job folder was added or removed by hand."""
    issued = int(_read_job_counters().get("nextId") or 1) - 1
    return _job_state_folder_numbers() != set(range(1, issued + 1))


def _job_input_paths(job: dict, iteration: int = 0) -> dict:
    """Return the expected BaseData and RideData input file paths for a job iteration."""
    folder = _sim_job_folder_path(job)
    parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else {}
    return {
        "folder": folder,
        "base": os.path.join(folder, _build_sim_filename(parts, "input_base_file", iteration)) if folder else "",
        "req": os.path.join(folder, _build_sim_filename(parts, "input_req_file", iteration)) if folder else "",
    }


def _save_input_files_for_job(job: dict, iteration: int = 0):
    """Write solver input BaseData and RideData files for a simulation job."""
    folder = _sim_job_folder_path(job)
    if not folder:
        return {}
    _ensure_folder(folder)
    parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else {}
    base_payload = write_api_base_file_from_loaded_data(loaded_data)
    base_name = _build_sim_filename(parts, "input_base_file", iteration)
    _write_json_file(os.path.join(folder, base_name), base_payload)
    written = {"folder": folder, "base": os.path.join(folder, base_name)}

    if loaded_data.get("rideRequests"):
        req_payload = write_api_ride_file_from_loaded_data(loaded_data)
        req_name = _build_sim_filename(parts, "input_req_file", iteration)
        _write_json_file(os.path.join(folder, req_name), req_payload)
        written["req"] = os.path.join(folder, req_name)
    return written


def _solver_cli_token(algorithm: str) -> str:
    """Translate UI algorithm tokens into solver_runner command-line tokens."""
    token = _sanitize_token(algorithm, "custom")
    if token == "rwapi":
        return "rw"
    if token in {"sumo", "custom"}:
        return token
    return "custom"


def _find_job_by_id(job_id: str) -> dict | None:
    """Find a simulation job by its stored job id."""
    for job in loaded_data.get("simulationJobs", []) or []:
        if isinstance(job, dict) and str(job.get("id") or "") == str(job_id or ""):
            return job
    return None


def _resolve_output_view_job() -> dict | None:
    """Resolve which dashboard job the current request's output view is about, from its own
    sim_job_id query parameter - the normal-mode counterpart of demo mode's ?demo_scenario=.
    Lets output-viewing routes be self-contained per request instead of trusting whatever
    loaded_data happens to still hold from a previous, possibly different, page visit."""
    job_id = (request.args.get("sim_job_id") or "").strip()
    return _find_job_by_id(job_id) if job_id else None


def _job_query(job: dict | None) -> str:
    """Return a query string preserving the selected dashboard job - the normal-mode
    counterpart of _demo_query."""
    if not job:
        return ""
    return "?" + urlencode({"sim_job_id": str(job.get("id") or "")})


def _load_job_output_state(job: dict | None) -> dict:
    """Load one dashboard job's output state without touching global loaded_data - the
    normal-mode read-only counterpart of _load_demo_output_state, for routes that resolve their
    job from sim_job_id directly (see _resolve_output_view_job) rather than relying on a
    previous request having mutated loaded_data."""
    if not job:
        return _empty_state()
    folder = _sim_job_folder_path(job)
    parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else {}
    prefix = _build_sim_file_prefix(parts) if parts else ""
    return _load_scenario_output_state(folder, prefix, job=job)


def _update_solver_job(job_id: str, **changes) -> dict | None:
    """Apply status changes to a solver job while keeping its metadata file in sync."""
    with SIMULATION_JOB_LOCK:
        job = _find_job_by_id(job_id)
        if not job:
            return None
        job.update(changes)
        try:
            _write_sim_metadata_file(job)
        except Exception:
            pass
        return job


def _write_solver_text(folder: str, filename: str, content: str):
    """Write solver log text into a job folder."""
    if not folder:
        return
    _ensure_folder(folder)
    with open(os.path.join(folder, filename), "w", encoding="utf-8", errors="replace") as f:
        f.write(content or "")


def _start_solver_for_job(job: dict):
    """Start the solver for a job in a background daemon thread."""
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    thread = threading.Thread(target=_run_solver_job, args=(job_id,), daemon=True)
    thread.start()


def _prepare_solver_runtime_dirs(cli_solver: str, req_path: str):
    """Create solver-specific runtime folders before launching a solver process."""
    _ensure_folder(os.path.join(PROJECT_ROOT, "debug"))
    if cli_solver == "sumo":
        req_stem = os.path.splitext(os.path.basename(req_path))[0]
        sumo_output = os.path.join(PROJECT_ROOT, "sumo", f"output_{req_stem}")
        for sub in (
            "console_output",
            "event_log",
            "general_kpi",
            "platoon_results",
            "runtime",
            "tripinfos",
            "cab_results",
            "request_results",
        ):
            _ensure_folder(os.path.join(sumo_output, sub))


def _solver_preflight_error(cli_solver: str) -> str | None:
    """Return a user-facing startup error when a selected solver is missing required setup."""
    if cli_solver == "sumo":
        if not os.environ.get("SUMO_HOME"):
            return "SUMO kann nicht gestartet werden: Umgebungsvariable SUMO_HOME ist nicht gesetzt."
    if cli_solver == "rw":
        cred_path = os.path.join(PROJECT_ROOT, "rw_credentials.txt")
        if not os.path.isfile(cred_path):
            return "RWAPI kann nicht gestartet werden: Datei 'rw_credentials.txt' fehlt im Projekt-Root."
    return None


def _build_search_solver_cmd(job: dict, state_folder: str, base_path: str, req_path: str) -> tuple[list, str]:
    """Build the solver_runner.py command line for a normal FleetPlanning search job."""
    cli_solver = _solver_cli_token(_job_algorithm_token(job))
    cmd = [
        sys.executable,
        SOLVER_RUNNER_SCRIPT,
        req_path,
        "--base_data",
        base_path,
        "--use_sim",
        cli_solver,
        "--search_mode",
        _sanitize_search_mode(job.get("searchMode")),
        "--iter_limit",
        str(_sanitize_iter_limit(job.get("iterLimit"))),
        "--time_limit",
        str(_sanitize_time_limit(job.get("timeLimit"))),
        "--cab_add_step",
        str(_sanitize_cab_add_step(job.get("cabAddStep"))),
        "--shrink_pros_max_tries",
        str(_sanitize_shrink_pros_max_tries(job.get("shrinkProsMaxTries"))),
        "--stagnation_tolerance",
        str(_sanitize_stagnation_tolerance(job.get("stagnationTolerance"))),
        "--stagnation_tolerance_patience",
        str(_sanitize_stagnation_tolerance_patience(job.get("stagnationTolerancePatience"))),
        "--objective_weight",
        str(_sanitize_objective_weight(job.get("objectiveWeight"))),
        "--objective_terms",
        ",".join(_sanitize_objective_terms(job.get("objectiveTerms"))),
        "--summary_output",
        os.path.join(state_folder, "solver_solution_summary.json"),
        "--verbose",
    ]
    if job.get("useOriginalProTimetable"):
        cmd.append("--use_original_pro_timetable")
    budget_eur = _sanitize_budget_eur(job.get("budgetEur"))
    if budget_eur is not None:
        cmd.extend(["--budget_eur", str(budget_eur)])
    service_level_min = _sanitize_service_level_min(job.get("serviceLevelMin"))
    if service_level_min is not None:
        cmd.extend(["--service_level_min", str(service_level_min)])
    initial_cabs = _sanitize_initial_cabs(job.get("initialCabs"))
    if initial_cabs is not None:
        cmd.extend(["--initial_cabs", str(initial_cabs)])
    if _sanitize_shrink_pros_allow_retry(job.get("shrinkProsAllowRetry")):
        cmd.append("--shrink_pros_allow_retry")
    if not _sanitize_shrink_pros_trial_probe_below_start(job.get("shrinkProsTrialProbeBelowStart")):
        cmd.append("--no_shrink_pros_trial_probe_below_start")
    if _sanitize_shrink_pros_trial_overshoot_correction(job.get("shrinkProsTrialOvershootCorrection")):
        cmd.append("--shrink_pros_trial_overshoot_correction")
    return cmd, cli_solver


def _build_fixedfleet_solver_cmd(job: dict, state_folder: str, base_path: str, req_path: str) -> tuple[list, str]:
    """Build the solver_runner.py command line for a fixed-fleet job, no FleetPlanning search,
    just one CustomSimulation evaluation of the uploaded fleet as-is via run_instance.py."""
    cmd = [
        sys.executable,
        SOLVER_RUNNER_SCRIPT,
        req_path,
        "--base_data",
        base_path,
        "--use_sim",
        "custom",
        "--job_type",
        "fixedfleet",
        "--iteration",
        "0",
        "--verbose",
    ]
    return cmd, "custom"


def _run_solver_job(job_id: str):
    """Run the selected solver in the background and update the job with status, logs, and
    artifacts. Status goes queued -> running -> completed/failed: "running" (and startedAt,
    which drives the runtime counter) is only set once SOLVER_RUN_SEMAPHORE is actually
    acquired, not when this thread starts - otherwise a job waiting behind another one already
    shows a ticking clock for work that hasn't started yet."""
    job = _update_solver_job(job_id, status="queued", startedAt=None, solverError=None)
    if not job:
        return

    state_folder = _dashboard_job_state_folder_path(job)
    paths = _job_input_paths(job, iteration=0)
    base_path = paths.get("base") or ""
    req_path = paths.get("req") or ""
    if job.get("runKind") == "fixedfleet":
        cmd, cli_solver = _build_fixedfleet_solver_cmd(job, state_folder, base_path, req_path)
    else:
        cmd, cli_solver = _build_search_solver_cmd(job, state_folder, base_path, req_path)
    job["solver"] = cli_solver
    job["solverCommand"] = cmd
    metadata_path = _write_sim_metadata_file(job)
    if cli_solver in ("custom", "rw", "sumo") and metadata_path:
        # the metadata file just written already carries the custom_sim.experiment_output.
        # config_folder key added above - reuse it directly, no separate config file needed
        cmd.extend(["--custom_sim_experiment_config", metadata_path])

    if not os.path.isfile(base_path) or not os.path.isfile(req_path):
        _update_solver_job(
            job_id,
            status="failed",
            failedAt=_utc_now_iso(),
            solverReturnCode=None,
            solverError="Input-Dateien fuer den Solver wurden nicht vollstaendig erstellt.",
        )
        return

    preflight_error = _solver_preflight_error(cli_solver)
    if preflight_error:
        _write_solver_text(state_folder, "solver_stderr.log", preflight_error)
        _update_solver_job(
            job_id,
            status="failed",
            failedAt=_utc_now_iso(),
            solverReturnCode=None,
            solverError=preflight_error,
        )
        return

    _prepare_solver_runtime_dirs(cli_solver, req_path)
    with SOLVER_RUN_SEMAPHORE:
        # the job may have been deleted (Löschen) while this thread was waiting its turn - don't
        # spend the slot running something nobody is tracking or waiting on anymore.
        if not _find_job_by_id(job_id):
            return

        _update_solver_job(job_id, status="running", startedAt=_utc_now_iso())
        try:
            popen = subprocess.Popen(
                cmd,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,  # own process group, so "Löschen" can kill it and any
                                          # children it spawns, not just this one process
            )
            with _RUNNING_PROCESSES_LOCK:
                _RUNNING_SOLVER_PROCESSES[job_id] = (popen, _process_identity(popen.pid))
            try:
                stdout, stderr = popen.communicate()
                returncode = popen.returncode
            finally:
                with _RUNNING_PROCESSES_LOCK:
                    _RUNNING_SOLVER_PROCESSES.pop(job_id, None)
        except Exception as exc:
            _write_solver_text(state_folder, "solver_stderr.log", str(exc))
            _update_solver_job(
                job_id,
                status="failed",
                failedAt=_utc_now_iso(),
                solverReturnCode=None,
                solverError=str(exc),
            )
            return

    # a killed job (Löschen while running) already has its dashboard entry gone by the time we
    # get here - _update_solver_job below is then a no-op, same as the existing-status branches.
    _write_solver_text(state_folder, "solver_stdout.log", stdout)
    _write_solver_text(state_folder, "solver_stderr.log", stderr)

    status = "completed" if returncode == 0 else "failed"
    changes = {
        "status": status,
        "solverReturnCode": returncode,
    }
    if status == "completed":
        changes["completedAt"] = _utc_now_iso()
        changes["solverError"] = None
    else:
        changes["failedAt"] = _utc_now_iso()
        changes["solverError"] = (stderr or stdout or "Solver wurde mit Fehlercode beendet.").strip()[-3000:]
    _update_solver_job(job_id, **changes)

def _scan_job_output_files(job: dict) -> dict:
    """Inspect a job folder and report how many SIM, CAB, and PRO output files it contains."""
    folder = _sim_job_folder_path(job)
    counts = {"sim": 0, "cab": 0, "pro": 0}
    if not folder or not os.path.isdir(folder):
        return counts
    parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else {}
    prefix = _build_sim_file_prefix(parts) if parts else ""
    if not prefix:
        return counts
    for fn in os.listdir(folder):
        if re.match(rf"^{re.escape(prefix)}_output_sim_\d+\.json$", fn, re.IGNORECASE):
            counts["sim"] += 1
        elif re.match(rf"^{re.escape(prefix)}_output_cab_\d+\.json$", fn, re.IGNORECASE):
            counts["cab"] += 1
        elif re.match(rf"^{re.escape(prefix)}_output_pro_\d+\.json$", fn, re.IGNORECASE):
            counts["pro"] += 1
    return counts


def _next_iteration_for_type(job: dict, file_io_type: str) -> int:
    """Find the next available iteration number for a generated file type in a job folder."""
    folder = _sim_job_folder_path(job)
    if not folder or not os.path.isdir(folder):
        return 0
    parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else {}
    prefix = _build_sim_file_prefix(parts) if parts else ""
    if not prefix:
        return 0
    max_iter = -1
    pattern = re.compile(rf"^{re.escape(prefix)}_{re.escape(file_io_type)}_(\d+)\.json$", re.IGNORECASE)
    for fn in os.listdir(folder):
        m = pattern.search(fn)
        if not m:
            continue
        try:
            max_iter = max(max_iter, int(m.group(1)))
        except Exception:
            pass
    return max_iter + 1


def _build_completed_simulations() -> list[dict]:
    """Build the table model for completed simulation runs shown on the overview page."""
    jobs = loaded_data.get("simulationJobs", []) or []
    completed = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "").lower() != "completed":
            continue
        counts = _scan_job_output_files(job)
        naming_parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else None
        job_prefix = _build_sim_file_prefix(naming_parts) if naming_parts else None
        completed.append(
            {
                "id": str(job.get("id") or ""),
                "title": job.get("scenarioName") or str(job.get("folderName") or "Simulation"),
                "detail": str(job.get("folderName") or ""),
                "algorithm": _job_algorithm_token(job),
                "runKind": "fixedfleet" if job.get("runKind") == "fixedfleet" else "search",
                **_read_cost_model_fields(job),
                "searchMode": _sanitize_search_mode(job.get("searchMode")),
                "objectiveTerms": _sanitize_objective_terms(job.get("objectiveTerms")),
                "objectiveWeight": _sanitize_objective_weight(job.get("objectiveWeight")),
                "useOriginalProTimetable": bool(job.get("useOriginalProTimetable")),
                "budgetEur": _sanitize_budget_eur(job.get("budgetEur")),
                "serviceLevelMin": _sanitize_service_level_min(job.get("serviceLevelMin")),
                "earlyUnchainingEnabled": _sanitize_early_unchaining_enabled(job.get("earlyUnchainingEnabled")),
                "iterLimit": _sanitize_iter_limit(job.get("iterLimit")),
                "timeLimit": _sanitize_time_limit(job.get("timeLimit")),
                "initialCabs": _sanitize_initial_cabs(job.get("initialCabs")),
                "cabAddStep": _sanitize_cab_add_step(job.get("cabAddStep")),
                "shrinkProsAllowRetry": _sanitize_shrink_pros_allow_retry(job.get("shrinkProsAllowRetry")),
                "shrinkProsMaxTries": _sanitize_shrink_pros_max_tries(job.get("shrinkProsMaxTries")),
                "shrinkProsTrialProbeBelowStart": _sanitize_shrink_pros_trial_probe_below_start(
                    job.get("shrinkProsTrialProbeBelowStart")
                ),
                "shrinkProsTrialOvershootCorrection": _sanitize_shrink_pros_trial_overshoot_correction(
                    job.get("shrinkProsTrialOvershootCorrection")
                ),
                "stagnationTolerance": _sanitize_stagnation_tolerance(job.get("stagnationTolerance")),
                "stagnationTolerancePatience": _sanitize_stagnation_tolerance_patience(
                    job.get("stagnationTolerancePatience")
                ),
                "scenarioFacts": _parse_scenario_facts_from_prefix(job_prefix),
                "statusText": "Abgeschlossen",
                "hasSim": counts["sim"] > 0,
                "hasCab": counts["cab"] > 0,
                "hasPro": counts["pro"] > 0,
                "completedAt": job.get("completedAt"),
                "outputUrl": url_for("output_page", sim_job_id=str(job.get("id") or "")),
                "deleteKey": str(job.get("id") or ""),
            }
        )
    # newest-completed-first - completedAt is an ISO string (_utc_now_iso()), sorts correctly
    # lexically; a missing value (shouldn't happen, but recovered jobs read from disk are less
    # guaranteed) falls back to "", which sorts last here rather than crashing or floating to top.
    completed.sort(key=lambda sim: sim["completedAt"] or "", reverse=True)
    return completed


def _discover_persisted_jobs() -> list[dict]:
    """Rebuild completed-job entries from disk, for seeding loaded_data['simulationJobs'] once
    at startup - it's otherwise purely in-memory and empty after every process restart, even
    though each job's own simulation_metadata.json (and its real output) survives on disk.

    Only recovers status=="completed" jobs. namingParts isn't itself persisted, but the
    metadata filename already encodes the same prefix _build_sim_file_prefix() would produce
    (f"{prefix}_simulation_metadata.json") - reconstructed here by splitting it back into the
    7 underscore-joined components, so output files can still be found by prefix after
    recovery. Falls back to namingParts=None (matches an in-memory job before that field is
    ever set) if the split doesn't look like a real prefix - degrades to a working list entry
    without scenarioFacts, rather than a broken one.

    A metadata file's own "id" (see _build_sim_metadata_payload) is the job's permanent id. A
    file without one takes the id of the folder it lives in if no other file holds that id,
    otherwise a freshly issued id, and the id is written back to the file. A missing counter
    file is seeded above the highest existing job-N folder.
    A folder written by an earlier dashboard version can hold several jobs' metadata files, so
    _metadataPath stays tracked and deletion can remove a single file without touching the
    others.
    """
    jobs = []
    if not os.path.isdir(DASHBOARD_JOB_STATE_ROOT):
        return jobs
    for job_folder_name in sorted(os.listdir(DASHBOARD_JOB_STATE_ROOT)):
        state_folder = os.path.join(DASHBOARD_JOB_STATE_ROOT, job_folder_name)
        if not os.path.isdir(state_folder):
            continue
        for fn in sorted(os.listdir(state_folder)):
            if not fn.endswith("simulation_metadata.json"):
                continue
            metadata_path = os.path.join(state_folder, fn)
            try:
                with open(metadata_path, "r", encoding="utf-8") as file:
                    metadata = json.load(file)
            except Exception:
                continue
            if not isinstance(metadata, dict) or str(metadata.get("status") or "").lower() != "completed":
                continue

            prefix = fn[: -len("_simulation_metadata.json")]
            parts = prefix.split("_") if prefix else []
            naming_parts = None
            if len(parts) == 7:
                naming_parts = {
                    "area": parts[0], "stations": parts[1], "lineConfig": parts[2],
                    "batteryCapacity": parts[3], "chargingPower": parts[4],
                    "scenario": parts[5], "timewindow": parts[6],
                    "algorithm": _sanitize_token(metadata.get("algorithm"), "custom"),
                }

            job = dict(metadata)
            job["namingParts"] = naming_parts
            job["_metadataPath"] = metadata_path
            jobs.append((job_folder_name, job, metadata))

    claimed = {job["id"] for _, job, _ in jobs if job.get("id")}
    for folder_name, job, metadata in jobs:
        if job.get("id"):
            continue
        own = folder_name if re.match(r"^job-\d+$", folder_name) and folder_name not in claimed else None
        job["id"] = own or _next_job_id()
        claimed.add(job["id"])
        try:
            _write_json_file(job["_metadataPath"], {**metadata, "id": job["id"]})
        except OSError:
            pass

    with SIMULATION_JOB_LOCK:
        numbers = _job_state_folder_numbers()
        if numbers and not os.path.isfile(JOB_COUNTERS_PATH):
            _write_json_file(JOB_COUNTERS_PATH, {**_read_job_counters(), "nextId": max(numbers) + 1})
    return [job for _, job, _ in jobs]


def _build_running_simulations() -> list[dict]:
    """Build the table model for queued, running, or failed simulation jobs shown on the overview page."""
    jobs = loaded_data.get("simulationJobs", []) or []
    running = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        status = str(job.get("status", "")).lower()
        if status not in {"queued", "running", "failed"}:
            continue
        # "queued": created, waiting for SOLVER_RUN_SEMAPHORE - nothing is executing yet, so no
        # runtime counter (startedAt is None until the semaphore is actually acquired, see
        # _run_solver_job) - showing a ticking clock here would claim work that hasn't started.
        status_text = {"failed": "Fehlgeschlagen", "queued": "In Warteschlange"}.get(status, "Laufend")
        badge_class = {"failed": "text-bg-danger", "queued": "text-bg-secondary"}.get(status, "text-bg-warning")
        error = str(job.get("solverError") or "").strip()
        running.append(
            {
                "id": job.get("id"),
                "title": job.get("scenarioName") or "Unbenanntes Szenario",
                "detail": (
                    f"Cab: {job.get('cabPrice') if job.get('cabPrice') is not None else '-'}€ | "
                    f"Pro: {job.get('proPrice') if job.get('proPrice') is not None else '-'}€ | "
                    f"kWh: {job.get('stationaryKwhPrice') if job.get('stationaryKwhPrice') is not None else '-'}€"
                ),
                "folderName": str(job.get("folderName") or ""),
                "createdAt": job.get("createdAt"),
                "startedAt": job.get("startedAt"),
                "runtimeText": _runtime_text_from_started(job.get("startedAt")),
                "statusText": status_text,
                "badgeClass": badge_class,
                "error": error,
                "algorithm": _job_algorithm_token(job),
                "deleteKey": str(job.get("id") or ""),
            }
        )
    return running


def _delete_dashboard_job_state(job: dict):
    """Purge a deleted job's movement animation files. Metadata (status set to "deleted"), logs
    and summary stay as a tombstone, so the job's id is never reused and the counter consistency
    check still finds its folder.

    A recovered job whose file lives in a folder shared with other old jobs is tracked via
    "_metadataPath". Only that one file is removed there.
    """
    root = os.path.normcase(os.path.abspath(DASHBOARD_JOB_STATE_ROOT))
    own_folder = _dashboard_job_state_folder_path(job)
    metadata_path = (job or {}).get("_metadataPath")
    if metadata_path and os.path.normcase(os.path.dirname(os.path.abspath(metadata_path))) != os.path.normcase(
        os.path.abspath(own_folder or "")
    ):
        target = os.path.normcase(os.path.abspath(metadata_path))
        if os.path.commonpath([root, target]) == root and os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass
        return

    if not own_folder or not os.path.isdir(own_folder):
        return
    target = os.path.normcase(os.path.abspath(own_folder))
    if os.path.commonpath([root, target]) != root:
        return
    for fn in os.listdir(own_folder):
        path = os.path.join(own_folder, fn)
        try:
            if "_movement_" in fn and fn.endswith(".html"):
                os.remove(path)
            elif fn.endswith("simulation_metadata.json"):
                # recovery loads only completed jobs, so "deleted" keeps this one from returning
                with open(path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                _write_json_file(path, {**metadata, "status": "deleted"})
        except (OSError, ValueError):
            pass


def _delete_running_simulation(job_id: str) -> bool:
    """Delete a running or failed dashboard job without removing backend output files - and, if
    its subprocess is actually executing right now, kill it for real (see _kill_process_tree).
    A merely queued job (waiting for SOLVER_RUN_SEMAPHORE, no process yet) has nothing to kill -
    _run_solver_job notices its entry is gone once it gets a turn and skips running it."""
    if not job_id:
        return False
    with _RUNNING_PROCESSES_LOCK:
        tracked = _RUNNING_SOLVER_PROCESSES.get(job_id)
    if tracked is not None:
        popen, identity = tracked
        _kill_process_tree(popen, identity)
    # under the job lock, so a solver thread finishing at the same moment cannot write a status
    # over the tombstone (_update_solver_job only finds jobs that are still in the list)
    with SIMULATION_JOB_LOCK:
        jobs = loaded_data.get("simulationJobs", []) or []
        before = len(jobs)
        kept = []
        for j in jobs:
            if str(j.get("id") or "") == job_id:
                _delete_dashboard_job_state(j)
                _record_job_deletion(interrupted=True)
                continue
            kept.append(j)
        jobs = kept
        loaded_data["simulationJobs"] = jobs
        return len(jobs) < before


def _delete_completed_simulation(delete_key: str) -> bool:
    """Delete a completed dashboard job and clear loaded output state when it was active."""
    if not delete_key:
        return False
    jobs = loaded_data.get("simulationJobs", []) or []
    before = len(jobs)
    kept = []
    for j in jobs:
        if str(j.get("id") or "") == delete_key:
            _delete_dashboard_job_state(j)
            _record_job_deletion(interrupted=False)
            continue
        kept.append(j)
    loaded_data["simulationJobs"] = kept

    if len(kept) < before:
        loaded_data["outputCabRuns"] = []
        loaded_data["outputSimRuns"] = []
        loaded_data["outputProRuns"] = []
        loaded_data["_outputParsed"] = build_output_payload(loaded_data)
        return True
    return False

#Import/Export Json
def to_datetime_local_string(value):
    """Convert an ISO timestamp into the value format expected by datetime-local inputs."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "").replace(" ", "T"))
        return dt.strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return value
    
def normalize_base_data(raw):
    """Convert uploaded BaseData JSON into the normalized in-memory structures used by the frontend."""
    norm = {}
    
    #Operation Area/Guilty Range
    op_areas = raw.get("operationAreas") or raw.get("operationArea") or []
    if isinstance(op_areas, list) and op_areas:
        area = op_areas[0]
        points = []
        for p in area.get("LocationBorder", []):
            points.append({"Latitude": p.get("Latitude"), "Longitude": p.get("Longitude")})
        norm["operationArea"] = {"points": points}

        guilty = area.get("GuiltyRange", {})
        norm["startTime"] = to_datetime_local_string(guilty.get("StartTime"))
        norm["endTime"] = to_datetime_local_string(guilty.get("EndTime"))
    else:
        norm["operationArea"] = raw.get("operationArea", {})
        norm["startTime"] = raw.get("startTime")
        norm["endTime"] = raw.get("endTime")

    # Cabs
    norm["cabs"] = []
    for i, cab in enumerate(raw.get("cabSchedules", raw.get("cabs", [])), start=1):
        cab = dict(cab)
        cab["id"] = cab.get("id") or cab.get("Guid") or cab.get("label") or f"Cab{i}"
        norm["cabs"].append(cab)

    # Charging Points
    norm["chargingPoints"] = []
    for i, cp in enumerate(raw.get("chargingPoints", []), start=1):
        cp = dict(cp)
        cp["id"] = cp.get("id") or cp.get("Guid") or f"Charger{i}"
        norm["chargingPoints"].append(cp)

    # Pros
    norm["proSchedules"] = []
    for i, pro in enumerate(raw.get("proSchedules", []), start=1):
        pro = dict(pro)
        pro["id"] = pro.get("id") or pro.get("Guid") or pro.get("scheduleId") or f"Pro{i}"
        norm["proSchedules"].append(pro)

    # Chaining Locations
    norm["chainingLocations"] = []
    for i, loc in enumerate(raw.get("chainingLocations", []), start=1):
        loc = dict(loc)
        loc["id"] = loc.get("id") or loc.get("Guid") or loc.get("locationId") or f"ChainLoc{i}"
        norm["chainingLocations"].append(loc)

    # Chain Routes
    norm["chainRoutes"] = []
    for i, r in enumerate(raw.get("chainRoutes", []), start=1):
        r = dict(r)
        r["id"] = r.get("id") or r.get("Guid") or r.get("routeId") or f"ChainRoute{i}"
        norm["chainRoutes"].append(r)
        
    # Chain Route Schedules
    norm["chainRouteSchedules"] = []
    for i, crs in enumerate(raw.get("chainRouteSchedules", []), start=1):
        crs = dict(crs)
        crs["id"] = crs.get("id") or crs.get("Guid") or crs.get("routeId") or f"chainRouteSchedule{i}"
        norm["chainRouteSchedules"].append(crs)

    return norm

def _persist_operation_area():
    """Persist the current operation area to uploads_temp so output detail pages can reuse it."""
    source = str(loaded_data.get("_operationAreaSource") or "explicit").strip().lower()
    if source == "output":
        _remove_persisted_operation_area()
        return
    return _persist_operation_area_from_area(loaded_data.get("operationArea") or {})


def _operation_area_payload_from_area(area: dict | None) -> dict | None:
    """Build the router operation-area payload from normalized frontend area data."""
    points = area.get("points") if isinstance(area, dict) else None
    if not isinstance(points, list):
        return None
    normalized_points = []
    for p in points:
        if not isinstance(p, dict):
            continue
        lat = p.get("Latitude", p.get("lat"))
        lng = p.get("Longitude", p.get("lng"))
        if lat is None or lng is None:
            continue
        normalized_points.append({"lat": lat, "lng": lng})
    if len(normalized_points) < 3:
        return None
    return {"points": normalized_points}


def _operation_area_polygon(state: dict | None = None) -> Polygon | None:
    """Build a shapely Polygon from the loaded operation area, or None if there isn't one."""
    data = state if isinstance(state, dict) else loaded_data
    payload = _operation_area_payload_from_area(data.get("operationArea"))
    if not payload:
        return None
    return Polygon([(p["lng"], p["lat"]) for p in payload["points"]])


def _request_inside_operation_area(rr, polygon: Polygon) -> bool | None:
    """Whether a ride request's pickup and dropoff both fall inside the polygon. None if either
    location is missing/unparseable, i.e. can't be evaluated either way."""
    if not isinstance(rr, dict):
        return None
    pickup = _point_from_location(rr.get("CurrentLocation"))
    dropoff = _point_from_location(rr.get("TargetLocation"))
    if not pickup or not dropoff:
        return None
    pickup_pt = Point(pickup["lng"], pickup["lat"])
    dropoff_pt = Point(dropoff["lng"], dropoff["lat"])
    return (
        (polygon.contains(pickup_pt) or polygon.touches(pickup_pt))
        and (polygon.contains(dropoff_pt) or polygon.touches(dropoff_pt))
    )


def _count_requests_outside_operation_area(state: dict | None = None) -> int:
    """Count loaded ride requests whose pickup or dropoff falls outside the operation area.
    Requests with missing/unparseable coordinates are skipped, not counted as outside."""
    data = state if isinstance(state, dict) else loaded_data
    polygon = _operation_area_polygon(data)
    if polygon is None:
        return 0
    return sum(
        1 for rr in (data.get("rideRequests", []) or [])
        if _request_inside_operation_area(rr, polygon) is False
    )


def _remove_persisted_operation_area():
    """Remove the router operation-area file and clear routing caches."""
    try:
        os.remove(os.path.join(UPLOAD_FOLDER, "operation_area.json"))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    try:
        clear_route_caches()
    except Exception:
        pass


def _persist_operation_area_from_area(area: dict | None) -> bool:
    """Persist one operation area to uploads_temp so output detail pages can reuse it."""
    try:
        payload = _operation_area_payload_from_area(area)
        if not payload:
            return False
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        with open(os.path.join(UPLOAD_FOLDER, "operation_area.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        try:
            clear_route_caches()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _persist_operation_area_for_scenario(folder: str | None, prefix: str | None) -> bool:
    """Persist one scenario's own real BaseData operation area for detail routing.

    Shared by demo-scenario viewing and finished-job viewing alike (both resolve to a
    (folder, prefix) pair first - see _load_scenario_output_state). Without this, viewing a
    scenario without its BaseData having been freshly (re-)uploaded in the current process left
    no router area file behind (loaded_data["operationArea"] is a single global slot, not
    per-scenario, and the crude bounding-box fallback derived from output points is deliberately
    never persisted as a router constraint - see _persist_operation_area's "output" source
    check) - so every route silently fell back to a straight line."""
    state = _load_scenario_input_state(folder, prefix)
    if _persist_operation_area_from_area(state.get("operationArea") or {}):
        return True
    _remove_persisted_operation_area()
    return False

def _derive_operation_area_from_points(points: list) -> bool:
    """Create a simple bounding operation area from available output coordinates when no area was uploaded."""
    if not points:
        _remove_persisted_operation_area()
        return False
    lats = [p[0] for p in points if isinstance(p, (list, tuple)) and len(p) == 2]
    lngs = [p[1] for p in points if isinstance(p, (list, tuple)) and len(p) == 2]
    if not lats or not lngs:
        _remove_persisted_operation_area()
        return False
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    if min_lat == max_lat:
        min_lat -= 0.001
        max_lat += 0.001
    if min_lng == max_lng:
        min_lng -= 0.001
        max_lng += 0.001
    pad_lat = (max_lat - min_lat) * 0.05
    pad_lng = (max_lng - min_lng) * 0.05
    min_lat -= pad_lat
    max_lat += pad_lat
    min_lng -= pad_lng
    max_lng += pad_lng
    loaded_data["operationArea"] = {
        "points": [
            {"Latitude": min_lat, "Longitude": min_lng},
            {"Latitude": min_lat, "Longitude": max_lng},
            {"Latitude": max_lat, "Longitude": max_lng},
            {"Latitude": max_lat, "Longitude": min_lng},
        ]
    }
    loaded_data["_operationAreaSource"] = "output"
    _persist_operation_area()
    return True

def _collect_points_from_output(raw) -> list:
    """Extract route and stop coordinates from solver output payloads for operation-area derivation."""
    points = []
    entries = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = [raw]
    for e in entries:
        if not isinstance(e, dict):
            continue
        trip_stops = e.get("tripStops") or (e.get("vehicle") or {}).get("tripStops") or []
        if isinstance(trip_stops, list):
            for s in trip_stops:
                if not isinstance(s, dict):
                    continue
                loc = s.get("location") or {}
                lat = loc.get("latitude") if isinstance(loc, dict) else None
                lng = loc.get("longitude") if isinstance(loc, dict) else None
                if lat is not None and lng is not None:
                    points.append((float(lat), float(lng)))
        chaining_stops = e.get("chainingStops") or e.get("ChainingStops") or []
        if isinstance(chaining_stops, list):
            for s in chaining_stops:
                if not isinstance(s, dict):
                    continue
                loc = s.get("location") or {}
                lat = loc.get("latitude") if isinstance(loc, dict) else None
                lng = loc.get("longitude") if isinstance(loc, dict) else None
                if lat is not None and lng is not None:
                    points.append((float(lat), float(lng)))
    return points


def normalize_ride_data(raw):
    """Convert uploaded RideData JSON into normalized ride request rows for editing and simulation."""
    rides = []
    for i, step in enumerate(raw.get("simulationSteps", raw.get("rideRequests", [])), start=1):
        rp = step.get("requestParameter", step)
        ride = {
            "id": rp.get("UserGuid") or rp.get("id") or f"RideRequest{i}",
            "CurrentLocation": rp.get("CurrentLocation"),
            "TargetLocation": rp.get("TargetLocation"),
            "pickupTime": rp.get("pickupTime"),
            "targetTime": rp.get("targetTime"),
            "needRamp": rp.get("needRamp"),
            "requestedAdults": rp.get("requestedAdults"),
            "requestedChilds": rp.get("requestedChilds"),
            "luggage": rp.get("luggage"),
            "personalPreferences": rp.get("personalPreferences", {
                "allowCarpooling": False,
                "toleratedDelayBefore": 0,
                "toleratedDelayAfter": 0
            }),
            "SimulatedTime": step.get("SimulatedTime"),
            "bookProposal": step.get("bookProposal"),
            "createReport": step.get("createReport"),
        }
        rides.append(ride)
    return {"rideRequests": rides}

def _convert_ride_xml_to_json(xml_path: str, options: dict | None = None) -> dict:
    """Run the XML preprocessing pipeline and return the converted RideData JSON payload.

    `options` mirrors run_preprocessing_pipeline.py's own forwardable flags (see its --help
    and frontend/xml-pre-processing/generator_tudo.py for what each does) - sample_size, seed,
    drop_prebooking, tw_minutes, and, only when guarantee_feasible is set, depot_lat, depot_lon,
    service_seconds, horizon_start, horizon_end, max_shift_minutes, and base_data_payload (a
    BaseData JSON dict, written to a real temp file for the subprocess call and deleted right
    after - the pipeline only accepts a file path, not stdin). Missing/None values fall back to
    the pipeline's own defaults. --python/--keep-intermediate stay untouched (server-side
    plumbing, not something a dashboard user would set). --geojson defaults to the loaded
    operation area, same temp-file mechanism as base_data_payload, falling back to the
    pipeline's own default area only when none is loaded."""
    if not os.path.isfile(XML_PIPELINE_SCRIPT):
        raise RuntimeError("XML preprocessing pipeline not found (run_preprocessing_pipeline.py).")

    options = options or {}
    xml_path_abs = os.path.abspath(xml_path)
    output_json = os.path.splitext(xml_path_abs)[0] + "_preprocessed.json"
    cmd = [
        sys.executable,
        XML_PIPELINE_SCRIPT,
        xml_path_abs,
        "--output",
        output_json,
        "--sample-size",
        str(options.get("sample_size", -1)),
        "--seed",
        str(options.get("seed", 0)),
        "--tw-minutes",
        str(options.get("tw_minutes", 10)),
    ]
    if options.get("drop_prebooking"):
        cmd.append("--drop-prebooking")

    # Use the loaded operation area as the spatial filter when one exists, not the fixed default.
    # Not strictly required, generator_tudo.py falls back to a rectangle on its own otherwise;
    # kept for a possible future direct geojson-upload feature.
    operation_area_temp_path = None
    area_payload = _operation_area_payload_from_area(loaded_data.get("operationArea"))
    if area_payload:
        coords = [[p["lng"], p["lat"]] for p in area_payload["points"]]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        geojson_doc = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [coords]},
            }],
        }
        fd, operation_area_temp_path = tempfile.mkstemp(
            suffix=".geojson", prefix="operation_area_", dir=XML_PREPROCESS_DIR
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(geojson_doc, f)
        cmd.extend(["--geojson", operation_area_temp_path])

    base_data_temp_path = None
    if options.get("guarantee_feasible"):
        cmd.append("--guarantee-feasible")
        if options.get("depot_lat") is not None:
            cmd.extend(["--depot-lat", str(options["depot_lat"])])
        if options.get("depot_lon") is not None:
            cmd.extend(["--depot-lon", str(options["depot_lon"])])
        if options.get("service_seconds") is not None:
            cmd.extend(["--service-seconds", str(options["service_seconds"])])
        if options.get("horizon_start"):
            cmd.extend(["--horizon-start", options["horizon_start"]])
        if options.get("horizon_end"):
            cmd.extend(["--horizon-end", options["horizon_end"]])
        if options.get("max_shift_minutes") is not None:
            cmd.extend(["--max-shift-minutes", str(options["max_shift_minutes"])])

        base_data_payload = options.get("base_data_payload")
        if base_data_payload is not None:
            fd, base_data_temp_path = tempfile.mkstemp(
                suffix=".json", prefix="guarantee_feasible_base_", dir=XML_PREPROCESS_DIR
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(base_data_payload, f)
            cmd.extend(["--base-data", base_data_temp_path])

    try:
        try:
            # Popen (not run()) so the live process can be tracked in _RUNNING_XML_PROCESS and
            # actually killed by /cancel-xml-upload - start_new_session=True groups it with the
            # per-stage subprocess run_preprocessing_pipeline.py itself spawns, so killing the
            # group reaches whichever stage is currently running too, not just the wrapper.
            popen = subprocess.Popen(
                cmd,
                cwd=XML_PREPROCESS_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with _RUNNING_PROCESSES_LOCK:
                _RUNNING_XML_PROCESS["popen"] = popen
                _RUNNING_XML_PROCESS["identity"] = _process_identity(popen.pid)
            try:
                stdout, stderr = popen.communicate()
            finally:
                with _RUNNING_PROCESSES_LOCK:
                    _RUNNING_XML_PROCESS["popen"] = None
                    _RUNNING_XML_PROCESS["identity"] = None
            if popen.returncode != 0:
                detail = (stderr or "").strip() or (stdout or "").strip() or f"exit code {popen.returncode}"
                raise RuntimeError(f"XML preprocessing failed: {detail}")
        except OSError as exc:
            raise RuntimeError(f"XML preprocessing failed to start: {exc}") from exc

        try:
            with open(output_json, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise RuntimeError(f"Failed to read converted XML output JSON: {exc}") from exc
    finally:
        if base_data_temp_path:
            try:
                os.remove(base_data_temp_path)
            except OSError:
                pass
        if operation_area_temp_path:
            try:
                os.remove(operation_area_temp_path)
            except OSError:
                pass
        try:
            os.remove(output_json)
        except OSError:
            pass


@app.route('/upload-base-json', methods=['POST'])
def upload_base_json():
    """Accept BaseData uploads, normalize them, and refresh the in-memory frontend state."""
    global loaded_data
    file = request.files['file']
    if not file:
        return 'No file', 400
    filename = secure_filename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".xml":
        return jsonify({
            "status": "error",
            "message": "XML wird aktuell nur fuer Fahrtdaten unterstuetzt. Bitte die XML im Feld 'Fahrtdaten importieren' hochladen."
        }), 400

    file_bytes = file.read()
    raw = json.loads(file_bytes.decode("utf-8"))

    # catches the wrong-file-selected case (e.g. RideData uploaded here by mistake) before it
    # silently normalizes to an all-empty instance - normalize_base_data() itself has no
    # concept of "this doesn't look like BaseData", it just defaults every missing field to [].
    if not isinstance(raw, dict) or ("cabSchedules" not in raw and "cabs" not in raw):
        return jsonify({
            "status": "error",
            "message": (
                "Diese Datei sieht nicht wie BaseData aus (kein cabSchedules-Feld gefunden). "
                "Wurde vielleicht die Fahrtdaten-Datei ausgewählt?"
            ),
        }), 400

    loaded_data["_rawBaseData"] = raw
    loaded_data["_rawBaseDataText"] = file_bytes.decode("utf-8")
    loaded_data["_baseDataFileName"] = filename

    normalized = normalize_base_data(raw)
    loaded_data.update(normalized)
    loaded_data["_operationAreaSource"] = "base"
    _persist_operation_area()

    return jsonify({"status": "ok", "message": "BaseData geladen"})


def _read_xml_pipeline_options_from_form(form) -> tuple[dict, str | None]:
    """Parse the XML-preprocessing settings submitted alongside a RideData XML upload.

    Blank fields fall back to the pipeline's own defaults (see _convert_ride_xml_to_json);
    a field that is filled in but not parseable is a hard error, same convention as the other
    "*_input" validators in this file. Returns (options, error_message) - error_message is None
    on success."""
    def _optional_int(name):
        raw = str(form.get(name) or "").strip()
        if not raw:
            return None, False
        try:
            return int(raw), False
        except ValueError:
            return None, True

    options: dict = {}

    sample_size, bad = _optional_int("sample_size")
    if bad:
        return {}, "Die Sample-Größe muss eine ganze Zahl sein (-1 = alle Anfragen)."
    if sample_size is not None:
        options["sample_size"] = sample_size

    seed, bad = _optional_int("seed")
    if bad:
        return {}, "Der Zufalls-Seed muss eine ganze Zahl sein."
    if seed is not None:
        options["seed"] = seed

    tw_minutes, bad = _optional_int("tw_minutes")
    if bad or (tw_minutes is not None and tw_minutes <= 0):
        return {}, "Das Zeitfenster muss eine ganze Zahl größer als 0 sein."
    if tw_minutes is not None:
        options["tw_minutes"] = tw_minutes

    options["drop_prebooking"] = bool(form.get("drop_prebooking"))
    options["guarantee_feasible"] = bool(form.get("guarantee_feasible"))

    if options["guarantee_feasible"]:
        for name, label in (("depot_lat", "Depot-Breitengrad"), ("depot_lon", "Depot-Längengrad")):
            raw = str(form.get(name) or "").strip()
            if not raw:
                continue
            try:
                options[name] = float(raw)
            except ValueError:
                return {}, f"{label} muss eine Zahl sein."

        service_seconds, bad = _optional_int("service_seconds")
        if bad or (service_seconds is not None and service_seconds < 0):
            return {}, "Die Ein-/Ausstiegszeit muss eine ganze Zahl größer oder gleich 0 sein."
        if service_seconds is not None:
            options["service_seconds"] = service_seconds

        max_shift_minutes, bad = _optional_int("max_shift_minutes")
        if bad or (max_shift_minutes is not None and max_shift_minutes < 0):
            return {}, "Die maximale Verschiebung muss eine ganze Zahl größer oder gleich 0 sein."
        if max_shift_minutes is not None:
            options["max_shift_minutes"] = max_shift_minutes

        for name, label in (("horizon_start", "Betriebsbeginn"), ("horizon_end", "Betriebsende")):
            raw = str(form.get(name) or "").strip()
            if not raw:
                continue
            if not re.match(r"^\d{1,2}:\d{2}$", raw):
                return {}, f"{label} muss im Format HH:MM angegeben werden."
            options[name] = raw

    return options, None


def _validate_ride_data_shape(raw) -> str | None:
    """Return an error message if `raw` doesn't look like RideData at all (the wrong file was
    selected) - normalize_ride_data() itself has no concept of this, it just defaults every
    missing field to an empty list. None means it looks fine."""
    if not isinstance(raw, dict) or ("simulationSteps" not in raw and "rideRequests" not in raw):
        return (
            "Diese Datei sieht nicht wie Fahrtdaten aus (kein simulationSteps/rideRequests-"
            "Feld gefunden). Wurde vielleicht die BaseData-Datei ausgewählt?"
        )
    return None


def _update_xml_upload_job(**changes) -> dict:
    """Apply status changes to the single background XML-conversion job tracked in loaded_data."""
    with XML_UPLOAD_JOB_LOCK:
        job = loaded_data.setdefault("xmlUploadJob", {})
        job.update(changes)
        return dict(job)


def _try_claim_xml_upload_job(filename: str) -> int | None:
    """Atomically check that no XML conversion is already queued/running and, if so, claim the
    next job id and mark it queued - one atomic step under the lock, so two near-simultaneous
    upload requests can't both pass a separate check before either claims the slot. Returns None
    if one is already in flight - the caller must then reject the upload outright (see
    /upload-ride-json): unlike FleetPlanning jobs, XML conversions are never allowed to pile up
    behind each other, there is exactly one slot, ever."""
    global _XML_UPLOAD_JOB_COUNTER
    with XML_UPLOAD_JOB_LOCK:
        current = loaded_data.get("xmlUploadJob") or {}
        if current.get("status") in ("queued", "running"):
            return None
        _XML_UPLOAD_JOB_COUNTER += 1
        job_id = _XML_UPLOAD_JOB_COUNTER
        loaded_data["xmlUploadJob"] = {
            "id": job_id,
            "status": "queued",
            "filename": filename,
            "startedAt": None,
            "completedAt": None,
            "error": None,
        }
        return job_id


def _run_xml_conversion_job(xml_path: str, filename: str, options: dict, job_id: int):
    """Run the XML preprocessing pipeline in a background thread and record the result on
    loaded_data['xmlUploadJob'], polled via /upload/xml-job-fragment. A real MATSim XML takes on
    the order of tens of seconds to convert (measured) - doing this inline in the request would
    block the whole dashboard for everyone until it finished, since the dev server isn't
    threaded. "running" (and startedAt, which drives the elapsed-time display) is only set once
    XML_CONVERSION_RUN_SEMAPHORE is actually acquired - admission control means this is normally
    immediate (only one job is ever accepted at a time), but stays correct if that ever changes.

    `job_id` scopes every update to the job this thread was started for - /cancel-xml-upload
    replaces loaded_data['xmlUploadJob'] outright (a new id), so a cancelled or superseded run
    finishing later just gets silently ignored here instead of resurrecting a banner the user
    already dismissed, or overwriting a newer upload's result."""
    def is_current() -> bool:
        return loaded_data.get("xmlUploadJob", {}).get("id") == job_id

    def update(**changes):
        if is_current():
            _update_xml_upload_job(**changes)

    with XML_CONVERSION_RUN_SEMAPHORE:
        if not is_current():
            # cancelled while waiting its turn - nothing left to do
            try:
                os.remove(xml_path)
            except OSError:
                pass
            return

        update(status="running", startedAt=_utc_now_iso(), error=None)
        try:
            raw = _convert_ride_xml_to_json(xml_path, options)
            error = _validate_ride_data_shape(raw)
            if error:
                raise RuntimeError(error)

            if is_current():
                loaded_data["_rawRideData"] = raw
                loaded_data["_rawRideDataText"] = json.dumps(raw, ensure_ascii=False)
                loaded_data["_rideDataFileName"] = filename
                loaded_data.update(normalize_ride_data(raw))

            update(status="completed", completedAt=_utc_now_iso(), error=None)
        except Exception as exc:
            update(status="failed", completedAt=_utc_now_iso(), error=str(exc))
        finally:
            try:
                os.remove(xml_path)
            except OSError:
                pass


@app.route('/cancel-xml-upload', methods=['POST'])
def cancel_xml_upload():
    """Clear the tracked XML-conversion job and, if its subprocess is actually executing right
    now, kill it for real (see _kill_process_tree) - mirrors _delete_running_simulation. A
    merely queued job (waiting for XML_CONVERSION_RUN_SEMAPHORE) has no process yet;
    _run_xml_conversion_job notices its id no longer matches once it gets a turn and skips
    running it."""
    with _RUNNING_PROCESSES_LOCK:
        popen = _RUNNING_XML_PROCESS.get("popen")
        identity = _RUNNING_XML_PROCESS.get("identity")
    if popen is not None:
        _kill_process_tree(popen, identity)
    with XML_UPLOAD_JOB_LOCK:
        loaded_data["xmlUploadJob"] = {}
    return jsonify({"status": "ok"})


@app.route('/upload-ride-json', methods=['POST'])
def upload_ride_json():
    """Accept RideData JSON or XML uploads and normalize them for the simulation workflow.

    XML uploads only get queued here - the actual conversion runs in a background thread (see
    _run_xml_conversion_job) and is polled via /upload/xml-job-fragment. A real file can take
    tens of seconds, so converting it inside the request would keep that request open the whole
    time. Only one conversion runs at a time; a second upload is rejected while one is in
    flight."""
    global loaded_data
    file = request.files['file']
    if not file:
        return 'No file', 400
    filename = secure_filename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".xml":
        options, error = _read_xml_pipeline_options_from_form(request.form)
        if error:
            return jsonify({"status": "error", "message": error}), 400
        if options.get("guarantee_feasible"):
            if not loaded_data.get("_rawBaseData"):
                return jsonify({
                    "status": "error",
                    "message": (
                        "\"Erreichbarkeit garantieren\" benötigt BaseData. Bitte BaseData "
                        "zusätzlich auswählen (linkes Feld) und zusammen mit den Fahrtdaten "
                        "hochladen, oder zuerst separat hochladen."
                    ),
                }), 400
            options["base_data_payload"] = write_api_base_file_from_loaded_data(loaded_data)

        # Never let a second XML conversion queue up behind a running one - claimed atomically
        # right before actually starting it, so a request that fails any check above never
        # holds (and has to release) the slot for nothing.
        job_id = _try_claim_xml_upload_job(filename)
        if job_id is None:
            return jsonify({
                "status": "error",
                "message": (
                    "Es wird bereits eine XML-Datei verarbeitet. Bitte warten, bis das "
                    "abgeschlossen ist, oder den laufenden Vorgang abbrechen."
                ),
            }), 409

        safe_name = filename or f"ride_upload_{int(datetime.now().timestamp())}.xml"
        xml_path = os.path.abspath(os.path.join(UPLOAD_FOLDER, safe_name))
        file.save(xml_path)

        thread = threading.Thread(
            target=_run_xml_conversion_job, args=(xml_path, filename, options, job_id), daemon=True
        )
        thread.start()
        return jsonify({"status": "queued", "message": "XML wird im Hintergrund verarbeitet."})

    file_bytes = file.read()
    raw = json.loads(file_bytes.decode("utf-8"))

    error = _validate_ride_data_shape(raw)
    if error:
        return jsonify({"status": "error", "message": error}), 400

    loaded_data["_rawRideData"] = raw
    loaded_data["_rawRideDataText"] = json.dumps(raw, ensure_ascii=False)
    loaded_data["_rideDataFileName"] = filename
    loaded_data.update(normalize_ride_data(raw))

    return jsonify({"status": "ok", "message": "RideData geladen"})


@app.route('/export-base-json', methods=['GET'])
def export_base_json():
    """Export the current BaseData state and write it for the active simulation job when present."""
    if not loaded_data:
        return "No BaseData available", 404

    final_structure = write_api_base_file_from_loaded_data(loaded_data)

    json_bytes = json.dumps(final_structure, indent=2, ensure_ascii=False).encode("utf-8")
    download_name = "BaseData.json"
    job = _find_latest_running_job()
    if job:
        if isinstance(job.get("namingParts"), dict):
            parts = job.get("namingParts")
        else:
            draft = loaded_data.get("simulationDraft", {}) or {}
            parts = _build_sim_naming_parts(_build_auto_naming_base(), draft.get("algorithm", "custom"), 1, draft.get("searchMode"))
        iteration = _next_iteration_for_type(job, "input_base_file")
        named_file = _build_sim_filename(parts, "input_base_file", iteration)
        folder = _sim_job_folder_path(job)
        _ensure_folder(folder)
        _write_sim_metadata_file(job)
        try:
            with open(os.path.join(folder, named_file), "wb") as f:
                f.write(json_bytes)
            download_name = named_file
        except OSError:
            pass

    return send_file(
        io.BytesIO(json_bytes),
        mimetype='application/json',
        as_attachment=True,
        download_name=download_name
    )
    
@app.route('/export-ride-json', methods=['GET'])
def export_ride_json():
    """Export the current RideData state and write it for the active simulation job when present."""
    if not loaded_data.get("rideRequests"):
        return "No RideData available", 404

    final_structure = write_api_ride_file_from_loaded_data(loaded_data)
    
    json_bytes = json.dumps(final_structure, indent=2, ensure_ascii=False).encode("utf-8")
    download_name = "RideData.json"
    job = _find_latest_running_job()
    if job:
        if isinstance(job.get("namingParts"), dict):
            parts = job.get("namingParts")
        else:
            draft = loaded_data.get("simulationDraft", {}) or {}
            parts = _build_sim_naming_parts(_build_auto_naming_base(), draft.get("algorithm", "custom"), 1, draft.get("searchMode"))
        iteration = _next_iteration_for_type(job, "input_req_file")
        named_file = _build_sim_filename(parts, "input_req_file", iteration)
        folder = _sim_job_folder_path(job)
        _ensure_folder(folder)
        _write_sim_metadata_file(job)
        try:
            with open(os.path.join(folder, named_file), "wb") as f:
                f.write(json_bytes)
            download_name = named_file
        except OSError:
            pass

    return send_file(
        io.BytesIO(json_bytes),
        mimetype='application/json',
        as_attachment=True,
        download_name=download_name) 
    

def _iter_uploaded_files(req) -> list:
    """Collect uploaded files from supported multipart field names without duplicates."""
    files = []
    for key in ("files[]", "files", "file"):
        files.extend(req.files.getlist(key))
    seen = set()
    unique = []
    for f in files:
        ident = (id(f), f.filename)
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(f)
    return unique

OUTPUT_UPLOAD_DEBUG = True  # nach Fehleranalyse wieder auf False setzen

def _dbg(*args):
    """Print upload debug output when the upload debug flag is enabled."""
    if OUTPUT_UPLOAD_DEBUG:
        print("[UPLOAD-DEBUG]", *args, flush=True)


# Schema-compliant per-iteration file types (frontend/README.md "Schema-Based Simulation Files")
# - these five are required for an iteration to be considered complete/eligible for upload; the
# other two are custom_sim-only extras, included when present but never required for eligibility.
UPLOAD_REQUIRED_FILE_TYPES = ("input_base_file", "input_req_file", "output_cab", "output_sim", "output_pro")
UPLOAD_OPTIONAL_FILE_TYPES = ("past_entries_fleet", "request_results_log")

_UPLOAD_FILE_TYPE_PATTERNS = {
    file_type: re.compile(rf"(?:^|_){file_type}_(\d+)\.json$", re.IGNORECASE)
    for file_type in UPLOAD_REQUIRED_FILE_TYPES + UPLOAD_OPTIONAL_FILE_TYPES
}
_UPLOAD_METADATA_FILENAME_PATTERN = re.compile(r"(?:^|_)simulation_metadata\.json$", re.IGNORECASE)
_UPLOAD_PREFIX_RE = re.compile(
    r"^(?P<area>[a-z0-9]+)_(?P<stations>\d+cs)_(?P<lineConfig>\d+lines)_(?P<batteryCapacity>\d+bc)_"
    r"(?P<chargingPower>\d+cp)_(?P<scenario>\d+rq)_(?P<timewindow>\d+tw)$",
    re.IGNORECASE,
)


def _classify_uploaded_filename(name: str):
    """Match an uploaded filename against the schema-compliant per-iteration file types.

    Returns (prefix, file_type, iteration), or None if the name doesn't match any known type.
    `prefix` is "" for a bare/unprefixed file (a single-scenario dump with no schema prefix -
    matches the (?:^|_) anchor the same way the original single-type regex already did)."""
    base_name = os.path.basename(str(name or "").replace("\\", "/"))
    for file_type, pattern in _UPLOAD_FILE_TYPE_PATTERNS.items():
        m = pattern.search(base_name)
        if m:
            return base_name[: m.start()].rstrip("_"), file_type, int(m.group(1))
    return None


def _classify_uploaded_metadata_filename(name: str) -> str | None:
    """Match an uploaded bare/prefixed simulation_metadata.json.

    Returns the prefix it applies to ("" means it applies to every group found in this upload -
    the unprefixed convention backend output folders already use, see frontend/README.md), or
    None if the name isn't a simulation_metadata.json at all."""
    base_name = os.path.basename(str(name or "").replace("\\", "/"))
    m = _UPLOAD_METADATA_FILENAME_PATTERN.search(base_name)
    if not m:
        return None
    return base_name[: m.start()].rstrip("_")


def _naming_parts_from_upload_prefix(prefix: str, algorithm: str = "custom") -> dict:
    """Reconstruct namingParts from a schema-compliant filename prefix (frontend/README.md's
    "Schema prefix") - the real instance signature embedded in the uploaded files themselves,
    not whatever happens to be currently loaded in the dashboard's editable draft. Falls back to
    the same generic placeholder tokens _build_auto_naming_base() uses when nothing is loaded, if
    the prefix doesn't match the expected shape (a bare/malformed prefix)."""
    m = _UPLOAD_PREFIX_RE.match(prefix or "")
    if m:
        parts = {
            key: m.group(key).lower()
            for key in ("area", "stations", "lineConfig", "batteryCapacity", "chargingPower", "scenario", "timewindow")
        }
    else:
        parts = {
            "area": "pb", "stations": "0cs", "lineConfig": "0lines", "batteryCapacity": "0bc",
            "chargingPower": "0cp", "scenario": "0rq", "timewindow": "0tw",
        }
    parts["algorithm"] = _sanitize_token(algorithm, "custom")
    parts["runId"] = "1"
    return parts


@app.route("/upload-output-auto", methods=["POST"])
def upload_output_auto():
    """Upload solver output files, grouped by their own schema-compliant filename prefix so
    unrelated runs mixed into one upload never get merged into a single fake run - see
    frontend/README.md's "Schema-Based Simulation Files" for the naming convention this relies
    on. Each distinct prefix becomes its own job, written into its own backend output folder
    (under the "upload" naming, see _build_sim_folder_name) - never the old shared, unnamespaced
    uploads_temp/output_cab_0.json-style location, which a second group would have overwritten.
    Only iterations with all five schema-required files present are imported; incomplete ones are
    skipped and reported, not guessed at."""
    try:
        _dbg("---- /upload-output-auto called ----")
        files = _iter_uploaded_files(request)
        _dbg("total files resolved:", len(files))

        if not files:
            return jsonify({"status": "error", "message": "Keine Dateien im Request gefunden."}), 400

        loaded_data["outputCabRuns"] = []
        loaded_data["outputSimRuns"] = []
        loaded_data["outputProRuns"] = []
        loaded_data["_outputParsed"] = None
        loaded_data["_outputSimulationMetadata"] = None

        # --- pass 1: classify + read every file once, group by (prefix, iteration, type) ---
        groups: dict[str, dict[int, dict[str, tuple[bytes, dict]]]] = {}
        metadata_candidates: dict[str, dict] = {}  # prefix ("" = applies to every group) -> payload
        skipped = []

        for f in files:
            raw_name = (f.filename or "").strip()
            try:
                file_bytes = f.read()
                parsed = json.loads(file_bytes.decode("utf-8"))
            except Exception as e:
                _dbg("SKIP unreadable/invalid json:", raw_name, "|", repr(e))
                skipped.append(raw_name)
                continue

            meta_prefix = _classify_uploaded_metadata_filename(raw_name)
            if meta_prefix is not None:
                if isinstance(parsed, dict):
                    metadata_candidates[meta_prefix] = parsed
                else:
                    skipped.append(raw_name)
                continue

            classified = _classify_uploaded_filename(raw_name)
            if not classified:
                _dbg("SKIP pattern mismatch:", raw_name)
                skipped.append(raw_name)
                continue
            prefix, file_type, iteration = classified
            groups.setdefault(prefix, {}).setdefault(iteration, {})[file_type] = (file_bytes, parsed)

        if not groups:
            return jsonify({
                "status": "error",
                "message": "Keine schema-konformen Dateien gefunden.",
                "skipped": skipped,
            }), 400

        # --- pass 2: keep only iterations with all 5 required files present per group ---
        warnings = []
        eligible_groups: dict[str, dict[int, dict[str, tuple[bytes, dict]]]] = {}
        for prefix, by_iteration in groups.items():
            eligible_iterations = {}
            for iteration, by_type in by_iteration.items():
                missing = [t for t in UPLOAD_REQUIRED_FILE_TYPES if t not in by_type]
                if missing:
                    warnings.append(
                        f"{prefix or '(ohne Präfix)'}: Iteration {iteration} unvollständig "
                        f"(fehlt: {', '.join(missing)}) - übersprungen."
                    )
                    continue
                eligible_iterations[iteration] = by_type
            if eligible_iterations:
                eligible_groups[prefix] = eligible_iterations
            else:
                warnings.append(f"{prefix or '(ohne Präfix)'}: keine vollständige Iteration gefunden - übersprungen.")

        if not eligible_groups:
            return jsonify({
                "status": "error",
                "message": "Keine vollständige Iteration (alle 5 Dateien) in den hochgeladenen Daten gefunden.",
                "skipped": skipped,
                "warnings": warnings,
            }), 400

        # --- pass 3: optional consistency check - operation area across one group's own
        # iterations should never differ, since it's fixed for a whole run (see
        # _parse_scenario_facts_from_prefix) - a mismatch means files from different runs
        # coincidentally share one prefix. Warns only, never blocks the import.
        for prefix, by_iteration in eligible_groups.items():
            areas = []
            for iteration in sorted(by_iteration):
                _, parsed_base = by_iteration[iteration]["input_base_file"]
                try:
                    points = (normalize_base_data(parsed_base).get("operationArea") or {}).get("points") or []
                except Exception:
                    points = []
                if points:
                    areas.append((iteration, points))
            if len(areas) >= 2:
                first_iter, first_points = areas[0]
                for other_iter, other_points in areas[1:]:
                    if other_points != first_points:
                        warnings.append(
                            f"{prefix or '(ohne Präfix)'}: Einsatzgebiet unterscheidet sich zwischen "
                            f"Iteration {first_iter} und {other_iter} - vermutlich unterschiedliche Läufe "
                            f"mit zufällig gleichem Namensschema."
                        )
                        break

        # --- pass 4: one job per eligible group, written into its own backend output folder ---
        added_cab = added_sim = added_pro = 0
        created_job_ids = []
        last_cab, last_sim, last_pro = [], [], []
        last_operation_area = None
        last_derived_points = []

        for prefix, by_iteration in sorted(eligible_groups.items()):
            naming_parts = _naming_parts_from_upload_prefix(prefix, "custom")
            metadata_payload = metadata_candidates.get(prefix) or metadata_candidates.get("")
            draft_from_metadata = _metadata_payload_to_draft(metadata_payload) if metadata_payload else {}
            # Imported output has no loaded input to hash, so the upload's scenario prefix serves
            # as its identity.
            pseudo_job = {**draft_from_metadata, "namingParts": naming_parts, "inputContentHash": prefix}
            full_signature = _job_full_signature(pseudo_job)

            # same dedup rule the main launch flow uses (_find_matching_job, any status - not
            # just "running") - re-uploading the same files (same prefix, same or no metadata)
            # updates that one job in place instead of piling up duplicate list entries that
            # would all hash to, and silently overwrite each other in, the same output folder.
            job = _find_matching_job(full_signature)
            jobs = loaded_data.setdefault("simulationJobs", [])
            if job is None:
                job = {
                    "id": _next_job_id(),
                    "status": "running",
                    "scenarioName": (
                        draft_from_metadata.get("scenarioName")
                        or f"Hochgeladen ({prefix or 'ohne Präfix'})"
                    ),
                    "namingParts": naming_parts,
                    "inputContentHash": prefix,
                    **{k: v for k, v in draft_from_metadata.items() if k != "scenarioName"},
                }
                jobs.append(job)
            job["folderName"] = _build_sim_folder_name("upload", full_signature)

            target_folder = _sim_job_folder_path(job)
            _ensure_folder(target_folder)
            # Overwritten indiscriminately, not merged: unlike the regular launch flow, nothing
            # here ran a real simulation, so there's no expensive result worth protecting and no
            # way to know if a byte-identical match is actually the same run anyway - that's the
            # user's responsibility. Clearing first (rather than only writing this upload's own
            # filenames) matters when this upload has fewer iterations than what the folder
            # already held - e.g. overwriting a stored 10-iteration run with a 5-iteration one -
            # so the now-unrelated iterations 6-10 don't linger as stale files.
            for existing_name in os.listdir(target_folder):
                existing_path = os.path.join(target_folder, existing_name)
                if os.path.isfile(existing_path):
                    os.remove(existing_path)

            group_cab, group_sim, group_pro = [], [], []
            group_derived_points = []
            group_operation_area = None
            for iteration in sorted(by_iteration):
                by_type = by_iteration[iteration]
                for file_type in UPLOAD_REQUIRED_FILE_TYPES + UPLOAD_OPTIONAL_FILE_TYPES:
                    if file_type not in by_type:
                        continue
                    file_bytes, _parsed = by_type[file_type]
                    stem = f"{prefix}_{file_type}_{iteration}.json" if prefix else f"{file_type}_{iteration}.json"
                    active_name = secure_filename(stem)
                    if not active_name:
                        continue
                    with open(os.path.join(target_folder, active_name), "wb") as wf:
                        wf.write(file_bytes)

                cab_raw = by_type["output_cab"][1]
                sim_raw = by_type["output_sim"][1]
                pro_raw = by_type["output_pro"][1]
                group_cab.append(parse_output_cab_json(cab_raw, f"output_cab_{iteration}.json"))
                group_sim.append(parse_output_sim_json(sim_raw, f"output_sim_{iteration}.json"))
                group_pro.append(parse_output_pro_json(pro_raw, f"output_pro_{iteration}.json"))
                group_derived_points.extend(_collect_points_from_output(cab_raw))
                group_derived_points.extend(_collect_points_from_output(pro_raw))

                if group_operation_area is None:
                    base_raw = by_type["input_base_file"][1]
                    normalized = normalize_base_data(base_raw)
                    area = normalized.get("operationArea") or {}
                    if area.get("points"):
                        group_operation_area = area

            added_cab += len(group_cab)
            added_sim += len(group_sim)
            added_pro += len(group_pro)
            job["status"] = "completed"
            # Every field here reflects this specific upload, not anything preserved from before -
            # same "overwrite indiscriminately" reasoning as the files above. createdAt is always
            # "when this entry entered the dashboard" (this upload, matched or not - a re-upload
            # is a fresh act, not a continuation of the old one). startedAt/completedAt take the
            # metadata's own real historic values when present (that's what actually happened, if
            # the user supplied it) and fall back to unknown/upload-time otherwise - never to
            # whatever the previous upload happened to leave behind. The three timestamps are
            # deliberately not necessarily chronological for an uploaded job as a result.
            job["createdAt"] = _utc_now_iso()
            job["startedAt"] = _metadata_timestamp(metadata_payload, "startedAt")
            job["completedAt"] = _metadata_timestamp(metadata_payload, "completedAt") or _utc_now_iso()
            _write_sim_metadata_file(job)
            created_job_ids.append(job["id"])

            # last group processed (sorted by prefix) wins for immediate display, same as a
            # single-group upload always has - every group is still fully written to its own
            # job/folder above, individually selectable afterwards via the normal output page.
            last_cab, last_sim, last_pro = group_cab, group_sim, group_pro
            last_derived_points = group_derived_points
            last_operation_area = group_operation_area

        if last_operation_area:
            loaded_data["operationArea"] = last_operation_area
            loaded_data["_operationAreaSource"] = "base"
            _persist_operation_area()
        elif not (loaded_data.get("operationArea") or {}).get("points"):
            _derive_operation_area_from_points(last_derived_points)

        loaded_data["outputCabRuns"] = last_cab
        loaded_data["outputSimRuns"] = last_sim
        loaded_data["outputProRuns"] = last_pro
        loaded_data["_outputParsed"] = build_output_payload(loaded_data)
        if created_job_ids:
            last_job = next(
                (j for j in loaded_data.get("simulationJobs", []) if j.get("id") == created_job_ids[-1]), None
            )
            if last_job:
                loaded_data["_outputSimulationMetadata"] = _read_simulation_metadata_file(None, last_job)

        _dbg("RESULT groups:", len(eligible_groups), "cab:", added_cab, "sim:", added_sim, "pro:", added_pro, "skipped:", len(skipped))
        _dbg("---- /upload-output-auto done ----")

        return jsonify({
            "status": "ok",
            "groups_imported": len(eligible_groups),
            "added_cab": added_cab,
            "added_sim": added_sim,
            "added_pro": added_pro,
            "saved_total": added_cab + added_sim + added_pro,
            "skipped": skipped,
            "warnings": warnings,
            # last group processed (sorted by prefix) wins, same as loaded_data above - the
            # client uses this to navigate to that job's own output view (?sim_job_id=...),
            # since the page may currently be scoped to a different, older job entirely.
            "redirect_job_id": created_job_ids[-1] if created_job_ids else None,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        _dbg("EXCEPTION:", repr(e))
        return jsonify({"status": "error", "message": str(e)}), 500


    

@app.route("/clear-output-runs", methods=["POST"])
def clear_output_runs():
    """Clear parsed output runs and temporary uploaded output JSON files."""
    loaded_data["outputCabRuns"] = []
    loaded_data["outputSimRuns"] = []
    loaded_data["outputProRuns"] = []
    loaded_data["_outputParsed"] = None
    loaded_data["_outputSimulationMetadata"] = None
    removed = _cleanup_upload_temp()
    return jsonify({"status": "ok", "removed_json": removed})


def _current_output_view_state() -> dict:
    """Resolve the output state the current request's charts/details should read - the demo
    scenario or sim_job_id-resolved job for this request, or the legacy manual-upload flow's
    global state when neither applies (see _resolve_output_view_job)."""
    if DEMO_MODE:
        return _load_demo_output_state(_selected_demo_scenario())
    job = _resolve_output_view_job()
    return _load_job_output_state(job) if job else loaded_data


@app.route("/output-data", methods=["GET"])
def output_data():
    """Return the currently parsed output payload for frontend charts and detail views."""
    state = _current_output_view_state()
    payload = state.get("_outputParsed") or build_output_payload(state)
    return jsonify(_apply_demo_kpi_filter(payload) if DEMO_MODE else payload)


@app.route("/chart-data", methods=["GET"])
def chart_data():
    """Return chart-ready data for the selected dataset and KPI axes."""
    try:
        dataset = request.args.get("dataset", "paired")
        if dataset == "paired":
            y1 = request.args.get("y1", "rejects")
            y2 = request.args.get("y2", "(none)")
        else:
            y1 = request.args.get("y1", "sumDistance")
            y2 = request.args.get("y2", "rejects")
        view = request.args.get("view", "progression")

        from output_utils import build_chart_payload
        state = _current_output_view_state()
        data = build_chart_payload(state, dataset, y1, y2, view)
        return jsonify(data)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/output/load-iteration-input", methods=["POST"])
def load_iteration_input():
    """Load one chosen iteration's own saved BaseData/RideData input files into the live
    editable draft, overwriting whatever cabs/chargingPoints/chainingLocations/rideRequests/
    operationArea are currently loaded there - the same destructive effect a manual BaseData/
    RideData upload already has today, just sourced from a specific solved iteration of a past
    job instead of a freshly uploaded file. Explicit, button-triggered action only (see the
    "Als Eingabedaten laden" button on the Flotten-Details card in output.html) - never
    automatic from merely viewing a job's output."""
    if DEMO_MODE:
        return redirect(url_for("output_page"))

    job_id = (request.form.get("sim_job_id") or "").strip()
    job = _find_job_by_id(job_id)
    try:
        iteration = int(request.form.get("iteration"))
    except (TypeError, ValueError):
        job = None

    folder = _sim_job_folder_path(job) if job else ""
    parts = job.get("namingParts") if job and isinstance(job.get("namingParts"), dict) else None
    prefix = _build_sim_file_prefix(parts) if parts else ""
    if not job or not folder or not prefix:
        return redirect(url_for("output_page", sim_job_id=job_id, notice="load_input_invalid"))

    base_path = _scenario_file_path(folder, prefix, "input_base_file", iteration)
    req_path = _scenario_file_path(folder, prefix, "input_req_file", iteration)
    loaded_any = False

    if os.path.isfile(base_path):
        with open(base_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        raw = json.loads(raw_text)
        loaded_data["_rawBaseData"] = raw
        loaded_data["_rawBaseDataText"] = raw_text
        loaded_data["_baseDataFileName"] = os.path.basename(base_path)
        loaded_data.update(normalize_base_data(raw))
        loaded_data["_operationAreaSource"] = "base"
        _persist_operation_area()
        loaded_any = True

    if os.path.isfile(req_path):
        with open(req_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        raw = json.loads(raw_text)
        loaded_data["_rawRideData"] = raw
        loaded_data["_rawRideDataText"] = raw_text
        loaded_data["_rideDataFileName"] = os.path.basename(req_path)
        loaded_data.update(normalize_ride_data(raw))
        loaded_any = True

    notice = "input_loaded" if loaded_any else "load_input_invalid"
    return redirect(url_for("output_page", sim_job_id=job_id, notice=notice))


#Output details page
@app.route("/output/details/<int:num_cabs>")
def output_details(num_cabs):
    """Render the detailed output page for the selected number of cabs."""
    state = _current_output_view_state()
    query = _demo_query(_selected_demo_scenario()) if DEMO_MODE else _job_query(_resolve_output_view_job())
    cab_runs = state.get("outputCabRuns", [])
    selected = None

    for r in reversed(cab_runs):
        run_name = r.get("run") or ""
        n = _extract_num_from_run_name(run_name)
        if n == num_cabs:
            selected = r
            break

    return render_template(
        "output_details.html",
        num_cabs=num_cabs,
        cab_run=selected,
        output_data_query=query,
    )


def _output_details_source() -> tuple[str | None, str | None]:
    """Resolve the (base_dir, prefix) the details/gantt/vehicle-gantt endpoints should read
    from, straight from the current request - the selected demo scenario in demo mode, or the
    sim_job_id-resolved job in normal mode (see _resolve_output_view_job). Falls back to
    (None, None), i.e. the legacy uploads_temp manual-upload location with no prefix, when
    nothing resolves in normal mode."""
    if DEMO_MODE:
        scenario = _selected_demo_scenario()
        return (scenario.get("folder"), scenario.get("prefix")) if scenario else (None, None)
    job = _resolve_output_view_job()
    if not job:
        return None, None
    folder = _sim_job_folder_path(job)
    parts = job.get("namingParts") if isinstance(job.get("namingParts"), dict) else {}
    prefix = _build_sim_file_prefix(parts) if parts else ""
    return folder, prefix


def _refresh_operation_area_for_details(base_dir: str | None, prefix: str | None) -> None:
    """Keep the router's operation-area file in sync with whichever scenario a details/gantt
    request resolved to. A resolved (base_dir, prefix) - demo scenario or dashboard job alike -
    always gets its own real area persisted. When nothing resolved: in demo mode that means "no
    scenario selected", so any stale area is explicitly cleared; in normal mode it means "the
    legacy uploads_temp manual-upload flow is in play", which manages that same file itself, so
    it's left untouched here."""
    if base_dir and prefix:
        _persist_operation_area_for_scenario(base_dir, prefix)
    elif DEMO_MODE:
        _remove_persisted_operation_area()


@app.route("/output/details/gantt/<int:num_cabs>")
def output_details_gantt(num_cabs):
    """Build the fleet-level Gantt payload for a selected cab-count scenario."""
    try:
        base_dir, prefix = _output_details_source()
        _refresh_operation_area_for_details(base_dir, prefix)
        data = build_gantt_payload(num_cabs, base_dir=base_dir, prefix=prefix)
        return jsonify(data)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/output/details/gantt/vehicle/<int:num_cabs>/<vehicle_id>")
def output_details_vehicle_gantt(num_cabs, vehicle_id):
    """Build the vehicle-level route and Gantt payload for one selected vehicle."""
    try:
        base_dir, prefix = _output_details_source()
        _refresh_operation_area_for_details(base_dir, prefix)
        data = build_vehicle_payload(num_cabs, vehicle_id, base_dir=base_dir, prefix=prefix)
        return jsonify(data)
    except FileNotFoundError as fnf:
        return jsonify({"error": str(fnf)}), 404
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _movement_cache_path(num_cabs: int) -> tuple[str | None, str | None]:
    """Resolve (cache_path, generation_source_dir) for the movement animation of the
    currently selected scenario/job and fleet size.

    Demo mode: cache_path sits in the demo scenario's own output folder, next to its
    simulation_metadata.json - the frontend never writes it, only checks whether a curator
    already placed it there (generation_source_dir is always None here, on purpose - see
    output_details_movement_generate, which enforce_demo_read_only blocks anyway).
    Normal mode: cache_path sits in the job's own frontend-owned folder, next to its
    simulation_metadata.json; generation_source_dir is the backend output folder to read
    output_cab/output_pro/input_base_file from.
    """
    base_dir, prefix = _output_details_source()
    if not base_dir or not prefix:
        return None, None
    filename = f"{prefix}_movement_{num_cabs}.html"
    if DEMO_MODE:
        return os.path.join(base_dir, filename), None
    job = _resolve_output_view_job()
    if not job:
        return None, None
    # frontend-owned per-job folder (next to that job's own simulation_metadata.json) -
    # NOT _sim_job_folder_path(job), which is the backend output folder base_dir already is.
    job_folder = _dashboard_job_state_folder_path(job)
    return os.path.join(job_folder, filename), base_dir


@app.route("/output/details/movement/<int:num_cabs>")
def output_details_movement(num_cabs):
    """Serve the fleet-movement animation for this fleet size, if it already exists."""
    cache_path, _ = _movement_cache_path(num_cabs)
    if not cache_path or not os.path.isfile(cache_path):
        return jsonify({"error": "not_generated"}), 404
    return send_file(cache_path)


# cache_path (absolute) -> {"status": "running"|"completed"|"failed", "error": str|None}.
# Movement generation involves real router work and can take a real amount of time (the same
# reason XML conversion is backgrounded below), so it runs in a background thread and the
# request returns immediately. It is expensive, so MOVEMENT_VIZ_RUN_SEMAPHORE lets only one
# generation run at a time; further generations wait for their turn.
_MOVEMENT_VIZ_JOBS: dict[str, dict] = {}
_MOVEMENT_VIZ_JOBS_LOCK = threading.Lock()
MOVEMENT_VIZ_RUN_SEMAPHORE = threading.BoundedSemaphore(1)


def _run_movement_generation(cache_path: str, source_dir: str, prefix: str, num_cabs: int) -> None:
    """Build and cache one movement animation in a background thread. See _MOVEMENT_VIZ_JOBS.

    startedAt (which drives the elapsed-time display) is only set once
    MOVEMENT_VIZ_RUN_SEMAPHORE is actually acquired - admission control means this is normally
    immediate (only one generation runs at a time), but stays correct if that ever changes,
    same reasoning as the XML-conversion job's own startedAt.
    """
    with MOVEMENT_VIZ_RUN_SEMAPHORE:
        with _MOVEMENT_VIZ_JOBS_LOCK:
            _MOVEMENT_VIZ_JOBS[cache_path] = {"status": "running", "error": None, "startedAt": _utc_now_iso()}
        try:
            _refresh_operation_area_for_details(source_dir, prefix)

            cab_path = _scenario_file_path(source_dir, prefix, "output_cab", num_cabs)
            if not os.path.isfile(cab_path):
                raise FileNotFoundError("missing_output_files")
            with open(cab_path, "r", encoding="utf-8") as f:
                cab_runs = json.load(f)

            pro_path = _scenario_file_path(source_dir, prefix, "output_pro", num_cabs)
            pro_runs = []
            if os.path.isfile(pro_path):
                with open(pro_path, "r", encoding="utf-8") as f:
                    pro_runs = json.load(f)

            base_data_path = _scenario_file_path(source_dir, prefix, "input_base_file", num_cabs)
            base_data = None
            if os.path.isfile(base_data_path):
                with open(base_data_path, "r", encoding="utf-8") as f:
                    base_data = json.load(f)

            html = build_movement_html(cab_runs, pro_runs, base_data)
            if html is None:
                raise ValueError("no_movement_data")

            _ensure_folder(os.path.dirname(cache_path))
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(html)
            with _MOVEMENT_VIZ_JOBS_LOCK:
                _MOVEMENT_VIZ_JOBS[cache_path] = {"status": "completed", "error": None}
        except Exception as e:
            with _MOVEMENT_VIZ_JOBS_LOCK:
                _MOVEMENT_VIZ_JOBS[cache_path] = {"status": "failed", "error": str(e)}


@app.route("/output/details/movement/<int:num_cabs>/generate", methods=["POST"])
def output_details_movement_generate(num_cabs):
    """Start (or report on) background generation of this fleet size's movement animation.

    Normal mode only - enforce_demo_read_only blocks every non-GET request, so this can
    never run in demo mode regardless of what the UI shows. Returns immediately either way;
    the actual work (if any is needed) happens in a background thread, polled via the
    /status route below.
    """
    cache_path, source_dir = _movement_cache_path(num_cabs)
    if not cache_path or not source_dir:
        return jsonify({"error": "no_scenario_selected"}), 404
    if os.path.isfile(cache_path):
        return jsonify({"status": "completed"})

    with _MOVEMENT_VIZ_JOBS_LOCK:
        existing = _MOVEMENT_VIZ_JOBS.get(cache_path)
        if existing and existing["status"] == "running":
            return jsonify(existing)
        _MOVEMENT_VIZ_JOBS[cache_path] = {"status": "running", "error": None, "startedAt": None}

    _, prefix = _output_details_source()
    thread = threading.Thread(
        target=_run_movement_generation, args=(cache_path, source_dir, prefix, num_cabs), daemon=True
    )
    thread.start()
    # startedAt is very likely still None here - the thread has barely had a chance to run,
    # let alone acquire the semaphore - but report whatever is actually there rather than a
    # hardcoded stand-in, same shape /status returns.
    with _MOVEMENT_VIZ_JOBS_LOCK:
        return jsonify(_MOVEMENT_VIZ_JOBS.get(cache_path) or {"status": "running", "error": None, "startedAt": None})


@app.route("/output/details/movement/<int:num_cabs>/status")
def output_details_movement_status(num_cabs):
    """Poll the status of a movement animation: already cached, in progress, or failed."""
    cache_path, _ = _movement_cache_path(num_cabs)
    if not cache_path:
        return jsonify({"error": "no_scenario_selected"}), 404
    if os.path.isfile(cache_path):
        return jsonify({"status": "completed"})
    with _MOVEMENT_VIZ_JOBS_LOCK:
        job = _MOVEMENT_VIZ_JOBS.get(cache_path)
    if not job:
        return jsonify({"status": "not_started"})
    return jsonify(job)


#Operation Area
@app.route('/save-operation-area', methods=['POST'])
def save_operation_area():
    """Persist a user-drawn operation area and its simulation time window."""
    global loaded_data
    data = request.json

    loaded_data['operationArea'] = {
        'points': data.get('points', []),
    }
    loaded_data['startTime'] = data.get('startTime')
    loaded_data['endTime']   = data.get('endTime')
    loaded_data["_operationAreaSource"] = "manual"
    _persist_operation_area()

    return jsonify({"status": "ok"})


@app.route('/set-use-original-pro-timetable', methods=['POST'])
def set_use_original_pro_timetable():
    """Toggle whether a launched simulation uses the uploaded chainRouteSchedules verbatim
    instead of regenerating a routed Pro timetable from it (see build_pro_timetable() in
    utils.py, and fleet_planning.py's --use_original_pro_timetable). A structural property of
    the input data, not a search parameter - stored at the top level of loaded_data, not in
    simulationDraft."""
    global loaded_data
    data = request.json or {}
    loaded_data["useOriginalProTimetable"] = bool(data.get("enabled"))
    return jsonify({"status": "ok"})

#Cabs
@app.route('/cabs/create', methods=['GET', 'POST'])
@_require_operation_area
def create_cab():
    """Create a new CAB schedule from the form and append it to the loaded base data."""
    if request.method == 'POST':
        coords = _parse_required_coordinates(request.form)
        if coords is None:
            return render_template(
                "cab_create.html",
                error="Bitte Breitengrad und Längengrad für das Fahrzeug angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        lat, lng = coords
        new_cab = {
            "id": f"Cab{len(loaded_data.get('cabs', [])) + 1}",
            "schedule": {
                "StartTime": request.form.get("schedule_start") or None,
                "EndTime": request.form.get("schedule_end") or None
            },
            "label": request.form["label"],
            "licensePlate": request.form["licensePlate"],
            "powerConsumption": {
                "speed": float(request.form.get("pc_speed", 0)),
                "wind": float(request.form.get("pc_wind", 0)),
                "loadCapacity": float(request.form.get("pc_loadCapacity", 0)),
                "tireTraction": float(request.form.get("pc_tireTraction", 0)),
                "weatherConditions": float(request.form.get("pc_weatherConditions", 0)),
                "climatronic": float(request.form.get("pc_climatronic", 0)),
                "regenerativeBraking": float(request.form.get("pc_regenerativeBraking", 0))
            },
            "FlatratEnergyConsumption": float(request.form.get("FlatratEnergyConsumption", 0.01)),
            "Risk": float(request.form.get("Risk", 0.0)),
            "TotalEnergyCapacity": int(request.form.get("TotalEnergyCapacity", 10000)),
            "InitialEnergyCapacity": int(request.form.get("InitialEnergyCapacity", 10000)),
            "InitialLocation": {
                "Latitude": lat,
                "Longitude": lng
            },
            "maxSpeed": float(request.form.get("maxSpeed", 0)),
            "regenerativePower": float(request.form.get("regenerativePower", 0)),
            "operatingCompany": request.form.get("operatingCompany", ""),
            "maxiumRegenerativePower": float(request.form.get("maxiumRegenerativePower", 0)),
            "defaultEnergyConsuptionPerM": float(request.form.get("defaultEnergyConsuptionPerM", 0.1)),
            "vehicleType": request.form.get("vehicleType", "cab"),
            "stateOfSchedule": request.form.get("stateOfSchedule", "Planned"),
            "hasRamp": bool(request.form.get("hasRamp")),
            "seats": int(request.form.get("seats", 4)),
            "childSeats": int(request.form.get("childSeats", 0)),
            "luggage": int(request.form.get("luggage", 0)),
            "chargingCurve": [float(request.form.get("chargingCurve", 0))],
            "maxSpeedAutonomous": float(request.form.get("maxSpeedAutonomous", 0))
        }
        loaded_data.setdefault("cabs", []).append(new_cab)
        return redirect(url_for("index"))
    return render_template("cab_create.html")

@app.route('/cabs/<cab_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_cab(cab_id):
    """Edit an existing CAB schedule identified by its route parameter id."""
    cab = next((c for c in loaded_data.get('cabs', []) if c['id'] == cab_id), None)
    if not cab:
        return "Cab not found", 404

    if request.method == 'POST':
        coords = _parse_required_coordinates(request.form)
        if coords is None:
            return render_template(
                "cab_edit.html", cab=cab,
                error="Bitte Breitengrad und Längengrad für das Fahrzeug angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400

        cab["schedule"]["StartTime"] = request.form.get("schedule_start") or None
        cab["schedule"]["EndTime"] = request.form.get("schedule_end") or None
        cab["label"] = request.form["label"]
        cab["licensePlate"] = request.form["licensePlate"]

        cab["powerConsumption"]["speed"] = float(request.form.get("pc_speed", 0))
        cab["powerConsumption"]["wind"] = float(request.form.get("pc_wind", 0))
        cab["powerConsumption"]["loadCapacity"] = float(request.form.get("pc_loadCapacity", 0))
        cab["powerConsumption"]["tireTraction"] = float(request.form.get("pc_tireTraction", 0))
        cab["powerConsumption"]["weatherConditions"] = float(request.form.get("pc_weatherConditions", 0))
        cab["powerConsumption"]["climatronic"] = float(request.form.get("pc_climatronic", 0))
        cab["powerConsumption"]["regenerativeBraking"] = float(request.form.get("pc_regenerativeBraking", 0))

        cab["FlatratEnergyConsumption"] = float(request.form.get("FlatratEnergyConsumption", 0.01))
        cab["Risk"] = float(request.form.get("Risk", 0.0))
        cab["TotalEnergyCapacity"] = int(request.form.get("TotalEnergyCapacity", 10000))
        cab["InitialEnergyCapacity"] = int(request.form.get("InitialEnergyCapacity", 10000))

        cab["InitialLocation"]["Latitude"], cab["InitialLocation"]["Longitude"] = coords

        cab["maxSpeed"] = float(request.form.get("maxSpeed", 0))
        cab["regenerativePower"] = float(request.form.get("regenerativePower", 0))
        cab["operatingCompany"] = request.form.get("operatingCompany", "")
        cab["maxiumRegenerativePower"] = float(request.form.get("maxiumRegenerativePower", 0))
        cab["defaultEnergyConsuptionPerM"] = float(request.form.get("defaultEnergyConsuptionPerM", 0.1))
        cab["vehicleType"] = request.form.get("vehicleType", "cab")
        cab["stateOfSchedule"] = request.form.get("stateOfSchedule", "Planned")
        cab["hasRamp"] = bool(request.form.get("hasRamp"))

        cab["seats"] = int(request.form.get("seats", 4))
        cab["childSeats"] = int(request.form.get("childSeats", 0))
        cab["luggage"] = int(request.form.get("luggage", 0))
        cab["chargingCurve"] = [float(request.form.get("chargingCurve", 0))]
        cab["maxSpeedAutonomous"] = float(request.form.get("maxSpeedAutonomous", 0))

        return redirect(url_for("index"))

    return render_template("cab_edit.html", cab=cab)

@app.route('/cabs/<cab_id>/delete', methods=['GET', 'POST'])
def delete_cab(cab_id):
    """Delete a CAB schedule from the currently loaded base data."""
    cab = next((c for c in loaded_data.get('cabs', []) if c['id'] == cab_id), None)
    if not cab:
        return "Cab not found", 404
    if request.method == 'POST':
        loaded_data['cabs'].remove(cab)
        return redirect(url_for('index'))
    return render_template("cab_delete.html", cab=cab)


#Charging Points
@app.route("/chargingPoints/create", methods=["GET", "POST"])
@_require_operation_area
def create_chargingpoint():
    """Create a new charging point from the submitted form data."""
    if request.method == "POST":
        coords = _parse_required_coordinates(request.form)
        if coords is None:
            return render_template(
                "chargingPoints_create.html",
                error="Bitte Breitengrad und Längengrad für den Ladepunkt angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        new_cp = {
            "id": f"Charger{len(loaded_data.get('chargingPoints', [])) + 1}",
            "Guid": f"Charger{len(loaded_data.get('chargingPoints', [])) + 1}",
            "Location": {
                "Latitude": coords[0],
                "Longitude": coords[1]
            },
            "MaximumStoppingTime": float(request.form.get("MaximumStoppingTime", 3600.0)),
            "type": request.form.get("type", "Electric"),
            "state": request.form.get("state", "Free"),
            "maximumPowerSupply": float(request.form.get("maximumPowerSupply", 0)),
            "MaximumRegenerativePower": float(request.form.get("MaximumRegenerativePower", 0)),
            "OpeningHours": [
                {
                    "WeekDays": request.form.get("WeekDays", "All"),
                    "Min": request.form.get("OpeningHourMin", "05:00:00"),
                    "Max": request.form.get("OpeningHourMax", "23:00:00")
                }
            ],
            "operators": [],
            "voltage": float(request.form.get("voltage", 0)),
            "amperage": float(request.form.get("amperage", 0)),
            "owner": request.form.get("owner", "")
        }
        loaded_data.setdefault("chargingPoints", []).append(new_cp)
        return redirect(url_for("index"))
    return render_template("chargingPoints_create.html")

@app.route('/chargingPoints/<cp_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_chargingpoint(cp_id):
    """Edit an existing charging point identified by its Guid."""
    cp = next((c for c in loaded_data.get('chargingPoints', []) if c['Guid'] == cp_id), None)
    if not cp:
        return "ChargingPoint not found", 404
    if request.method == 'POST':
        coords = _parse_required_coordinates(request.form)
        if coords is None:
            return render_template(
                "chargingPoints_edit.html", cp=cp,
                error="Bitte Breitengrad und Längengrad für den Ladepunkt angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        cp["Location"]["Latitude"], cp["Location"]["Longitude"] = coords
        cp["MaximumStoppingTime"] = float(request.form['MaximumStoppingTime'])
        cp["type"] = request.form['type']
        cp["state"] = request.form['state']
        cp["maximumPowerSupply"] = float(request.form['maximumPowerSupply'])
        cp["MaximumRegenerativePower"] = float(request.form['MaximumRegenerativePower'])
        cp["OpeningHours"] = [
            {
                "WeekDays": request.form.get("WeekDays", "All"),
                "Min": request.form.get("OpeningHourMin", "05:00:00"),
                "Max": request.form.get("OpeningHourMax", "23:00:00")
            }
        ]
        cp["operators"] = []
        cp["voltage"] = float(request.form['voltage'])
        cp["amperage"] = float(request.form['amperage'])
        cp["owner"] = request.form['owner']
        return redirect(url_for('index'))
    return render_template("chargingPoints_edit.html", cp=cp)

@app.route('/chargingPoints/<cp_id>/delete', methods=['GET', 'POST'])
def delete_chargingpoint(cp_id):
    """Delete a charging point from the currently loaded base data."""
    cp = next((c for c in loaded_data.get('chargingPoints', []) if c['Guid'] == cp_id), None)
    if not cp:
        return "ChargingPoint not found", 404
    if request.method == 'POST':
        loaded_data['chargingPoints'].remove(cp)
        return redirect(url_for('index'))
    return render_template("chargingPoints_delete.html", cp=cp)


#Pros
@app.route('/pros/create', methods=['GET', 'POST'])
@_require_operation_area
def create_pro():
    """Create a new PRO schedule from the submitted form data."""
    if request.method == 'POST':
        coords = _parse_required_coordinates(request.form)
        if coords is None:
            return render_template(
                "pro_create.html",
                error="Bitte Breitengrad und Längengrad für das Fahrzeug angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        new_pro = {
            "id": f"Pro{len(loaded_data.get('proSchedules', [])) + 1}",
            "InitialLocation": {
                "Latitude": coords[0],
                "Longitude": coords[1]
            },
            "MaxCabChain": int(request.form.get("MaxCabChain", 3)),
            "AdditionalEnergyConsumptionPerCab": [float(request.form.get("AdditionalEnergyConsumptionPerCab", 0.1))],
            "schedule": {
                "StartTime": request.form.get("schedule_start"),
                "EndTime": request.form.get("schedule_end")
            },
            "label": request.form["label"],
            "licensePlate": request.form["licensePlate"],
            "powerConsumption": {
                "speed": float(request.form.get("pc_speed", 0)),
                "wind": float(request.form.get("pc_wind", 0)),
                "loadCapacity": float(request.form.get("pc_loadCapacity", 0)),
                "tireTraction": float(request.form.get("pc_tireTraction", 0)),
                "weatherConditions": float(request.form.get("pc_weatherConditions", 0)),
                "climatronic": float(request.form.get("pc_climatronic", 0)),
                "regenerativeBraking": float(request.form.get("pc_regenerativeBraking", 0))
            },
            "FlatratEnergyConsumption": float(request.form.get("FlatratEnergyConsumption", 0.01)),
            "Risk": float(request.form.get("Risk", 0.0)),
            "TotalEnergyCapacity": int(request.form.get("TotalEnergyCapacity", 10000)),
            "InitialEnergyCapacity": int(request.form.get("InitialEnergyCapacity", 10000)),
            "maxSpeed": float(request.form.get("maxSpeed", 0)),
            "regenerativePower": float(request.form.get("regenerativePower", 0)),
            "operatingCompany": request.form["operatingCompany"],
            "maxiumRegenerativePower": float(request.form.get("maxiumRegenerativePower", 0)),
            "defaultEnergyConsuptionPerM": float(request.form.get("defaultEnergyConsuptionPerM", 0.1)),
            "vehicleType": str(request.form.get("vehicleType", "pro")),
            "stateOfSchedule": request.form["stateOfSchedule"]
        }
        loaded_data.setdefault("proSchedules", []).append(new_pro)
        return redirect(url_for("index"))
    return render_template("pro_create.html")

@app.route('/pros/<pro_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_pro(pro_id):
    """Edit an existing PRO schedule identified by its route parameter id."""
    pro = next((p for p in loaded_data.get('proSchedules', []) if p['id'] == pro_id), None)
    if not pro:
        return "Pro not found", 404
    if request.method == 'POST':
        coords = _parse_required_coordinates(request.form)
        if coords is None:
            return render_template(
                "pro_edit.html", pro=pro,
                error="Bitte Breitengrad und Längengrad für das Fahrzeug angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        pro["InitialLocation"]["Latitude"], pro["InitialLocation"]["Longitude"] = coords
        pro["MaxCabChain"] = int(request.form.get("MaxCabChain", 3))
        raw_val = request.form.get("AdditionalEnergyConsumptionPerCab", "").strip()
        pro["AdditionalEnergyConsumptionPerCab"] = [float(raw_val)] if raw_val else [0.1]
        # pro["AdditionalEnergyConsumptionPerCab"] = [float(request.form.get("AdditionalEnergyConsumptionPerCab", 0.1))]
        pro["schedule"] = {
            "StartTime": request.form.get("schedule_start"),
            "EndTime": request.form.get("schedule_end")
        }
        pro["label"] = request.form["label"]
        pro["licensePlate"] = request.form["licensePlate"]
        pro["powerConsumption"] = {
            "speed": float(request.form.get("pc_speed", 0)),
            "wind": float(request.form.get("pc_wind", 0)),
            "loadCapacity": float(request.form.get("pc_loadCapacity", 0)),
            "tireTraction": float(request.form.get("pc_tireTraction", 0)),
            "weatherConditions": float(request.form.get("pc_weatherConditions", 0)),
            "climatronic": float(request.form.get("pc_climatronic", 0)),
            "regenerativeBraking": float(request.form.get("pc_regenerativeBraking", 0))
        }
        pro["FlatratEnergyConsumption"] = float(request.form.get("FlatratEnergyConsumption", 0.01))
        pro["Risk"] = float(request.form.get("Risk", 0.0))
        pro["TotalEnergyCapacity"] = int(request.form.get("TotalEnergyCapacity", 10000))
        pro["InitialEnergyCapacity"] = int(request.form.get("InitialEnergyCapacity", 10000))
        pro["maxSpeed"] = float(request.form.get("maxSpeed", 0))
        pro["regenerativePower"] = float(request.form.get("regenerativePower", 0))
        pro["operatingCompany"] = request.form["operatingCompany"]
        pro["maxiumRegenerativePower"] = float(request.form.get("maxiumRegenerativePower", 0))
        pro["defaultEnergyConsuptionPerM"] = float(request.form.get("defaultEnergyConsuptionPerM", 0.1))
        pro["vehicleType"] = str(request.form.get("vehicleType", "pro"))
        pro["stateOfSchedule"] = request.form["stateOfSchedule"]
        return redirect(url_for("index"))
    return render_template("pro_edit.html", pro=pro)

@app.route('/pros/<pro_id>/delete', methods=['GET', 'POST'])
def delete_pro(pro_id):
    """Delete a PRO schedule from the currently loaded base data."""
    pro = next((p for p in loaded_data.get('proSchedules', []) if p['id'] == pro_id), None)
    if not pro:
        return "Pro not found", 404
    if request.method == 'POST':
        loaded_data['proSchedules'].remove(pro)
        return redirect(url_for('index'))
    return render_template("pro_delete.html", pro=pro)


#Chaining Location
@app.route('/chainingLocations/create', methods=['GET', 'POST'])
@_require_operation_area
def create_chaininglocation():
    """Create a chaining location with start and end coordinates."""
    if request.method == 'POST':
        start = _parse_required_coordinates(request.form, "start_latitude", "start_longitude")
        end = _parse_required_coordinates(request.form, "end_latitude", "end_longitude")
        if start is None or end is None:
            return render_template(
                "chainingLocation_create.html",
                error="Bitte Breitengrad und Längengrad für Start- und Zielposition angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        new_location = {
            "id": f"ChainLoc{len(loaded_data.get('chainingLocations', [])) + 1}",
            "Guid": request.form.get("Guid"),
            "LocationStart": {
                "Latitude": start[0],
                "Longitude": start[1]
            },
            "LocationEnd": {
                "Latitude": end[0],
                "Longitude": end[1]
            },
            "Type": request.form["Type"],
            "AdditionalTime": int(request.form.get("AdditionalTime", 60))
        }
        loaded_data.setdefault("chainingLocations", []).append(new_location)
        return redirect(url_for("index"))
    return render_template("chainingLocation_create.html")

@app.route('/chainingLocations/<loc_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_chaininglocation(loc_id):
    """Edit an existing chaining location identified by its route parameter id."""
    loc = next((l for l in loaded_data.get('chainingLocations', []) if l['id'] == loc_id), None)
    if not loc:
        return "Chaining Location not found", 404

    if request.method == 'POST':
        start = _parse_required_coordinates(request.form, "start_latitude", "start_longitude")
        end = _parse_required_coordinates(request.form, "end_latitude", "end_longitude")
        if start is None or end is None:
            return render_template(
                "chainingLocation_edit.html", loc=loc,
                error="Bitte Breitengrad und Längengrad für Start- und Zielposition angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        loc["Guid"] = request.form.get("Guid")
        loc["LocationStart"]["Latitude"], loc["LocationStart"]["Longitude"] = start
        loc["LocationEnd"]["Latitude"], loc["LocationEnd"]["Longitude"] = end
        loc["Type"] = request.form["Type"]
        loc["AdditionalTime"] = int(request.form.get("AdditionalTime"))
        return redirect(url_for("index"))

    return render_template("chainingLocation_edit.html", loc=loc)

@app.route('/chainingLocations/<loc_id>/delete', methods=['GET', 'POST'])
def delete_chaininglocation(loc_id):
    """Delete a chaining location from the currently loaded base data."""
    loc = next((l for l in loaded_data.get('chainingLocations', []) if l['id'] == loc_id), None)
    if not loc:
        return "Chaining Location not found", 404

    if request.method == 'POST':
        loaded_data['chainingLocations'].remove(loc)
        return redirect(url_for("index"))

    return render_template("chainingLocation_delete.html", loc=loc)


#Chain Routes
@app.route('/chainRoutes/create', methods=['GET', 'POST'])
@_require_operation_area
def create_chainroute():
    """Create a chain route linking two chaining locations."""
    if request.method == 'POST':
        start_id = request.form["StartLocation"]
        end_id = request.form["EndLocation"]

        if start_id == end_id:
            return "Start and End Location cannot be the same", 400

        new_route = {
            "id": f"ChainRoute{len(loaded_data.get('chainRoutes', [])) + 1}",
            "Guid": request.form.get("Guid"),
            "StartLocation": start_id,
            "IntermediateChainingLocations": [],
            "EndLocation": end_id,
            "Duration": float(request.form.get("Duration", 0)),
            "Distance": float(request.form.get("Distance", 0))
        }
        loaded_data.setdefault("chainRoutes", []).append(new_route)
        return redirect(url_for("index"))

    chaining_locations = loaded_data.get("chainingLocations", [])
    return render_template("chainRoutes_create.html", chaining_locations=chaining_locations)

@app.route('/chainRoutes/<route_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_chainroute(route_id):
    """Edit an existing chain route identified by its route parameter id."""
    route = next((r for r in loaded_data.get("chainRoutes", []) if r["id"] == route_id), None)
    if not route:
        return "Chain Route not found", 404

    if request.method == 'POST':
        start_id = request.form["StartLocation"]
        end_id = request.form["EndLocation"]

        if start_id == end_id:
            return "Start and End Location cannot be the same", 400

        route["Guid"] = request.form.get("Guid")
        route["StartLocation"] = start_id
        route["IntermediateChainingLocations"] = request.form.get("IntermediateChainingLocations")
        route["EndLocation"] = end_id
        route["Duration"] = float(request.form.get("Duration", 0))
        route["Distance"] = float(request.form.get("Distance", 0))
        return redirect(url_for("index"))

    chaining_locations = loaded_data.get("chainingLocations", [])
    return render_template("chainRoutes_edit.html", route=route, chaining_locations=chaining_locations)

@app.route('/chainRoutes/<route_id>/delete', methods=['GET', 'POST'])
def delete_chainroute(route_id):
    """Delete a chain route from the currently loaded base data."""
    route = next((r for r in loaded_data.get("chainRoutes", []) if r["id"] == route_id), None)
    if not route:
        return "Chain Route not found", 404

    if request.method == 'POST':
        loaded_data['chainRoutes'].remove(route)
        return redirect(url_for("index"))

    return render_template("chainRoutes_delete.html", route=route)


#Chain Route Schedule
@app.route('/chainRouteSchedules/create', methods=['GET', 'POST'])
@_require_operation_area
def create_chainrouteschedule():
    """Create a chain route schedule that assigns a PRO to a chain route."""
    if request.method == 'POST':
        new_crs = {
            "id": f"ChainRouteSchedule{len(loaded_data.get('chainRouteSchedules', [])) + 1}",
            "Guid": request.form.get("Guid"),
            "ChainRoute": request.form.get("ChainRoute"),
            "ProSchedule": request.form.get("ProSchedule"),
            "Departure": request.form.get("Departure"),
            "Arrival": request.form.get("Arrival")
        }
        loaded_data.setdefault("chainRouteSchedules", []).append(new_crs)
        return redirect(url_for("index"))

    chain_routes = loaded_data.get("chainRoutes", [])
    pro_schedules = loaded_data.get("proSchedules", [])
    return render_template(
        "chainRouteSchedules_create.html",
        chain_routes=chain_routes,
        pro_schedules=pro_schedules
    )

@app.route('/chainRouteSchedules/<crs_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_chainrouteschedule(crs_id):
    """Edit an existing chain route schedule identified by its route parameter id."""
    crs = next((c for c in loaded_data.get('chainRouteSchedules', []) if c['id'] == crs_id), None)
    if not crs:
        return "Chain Route Schedule not found", 404

    if request.method == 'POST':
        crs["Guid"] = request.form.get("Guid")
        crs["ChainRoute"] = request.form.get("ChainRoute")
        crs["ProSchedule"] = request.form.get("ProSchedule")
        crs["Departure"] = request.form.get("Departure")
        crs["Arrival"] = request.form.get("Arrival")
        return redirect(url_for("index"))

    chain_routes = loaded_data.get("chainRoutes", [])
    pro_schedules = loaded_data.get("proSchedules", [])
    return render_template(
        "chainRouteSchedules_edit.html",
        crs=crs,
        chain_routes=chain_routes,
        pro_schedules=pro_schedules
    )

@app.route('/chainRouteSchedules/<crs_id>/delete', methods=['GET', 'POST'])
def delete_chainrouteschedule(crs_id):
    """Delete a chain route schedule from the currently loaded base data."""
    crs = next((c for c in loaded_data.get('chainRouteSchedules', []) if c['id'] == crs_id), None)
    if not crs:
        return "Chain Route Schedule not found", 404

    if request.method == 'POST':
        loaded_data['chainRouteSchedules'].remove(crs)
        return redirect(url_for("index"))

    return render_template("chainRouteSchedules_delete.html", crs=crs)


#Ride Requests
@app.route('/rideRequests/create', methods=['GET', 'POST'])
@_require_operation_area
def create_ride_request():
    """Create a new ride request from pickup, target, time-window, and preference form fields."""
    if request.method == 'POST':
        start = _parse_required_coordinates(request.form, "start_latitude", "start_longitude")
        end = _parse_required_coordinates(request.form, "end_latitude", "end_longitude")
        if start is None or end is None:
            return render_template(
                "rideRequests_create.html",
                error="Bitte Breitengrad und Längengrad für Abhol- und Zielposition angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        new_request = {
            "id": f"RideRequest{len(loaded_data.get('rideRequests', [])) + 1}",
            "CurrentLocation": {
                "Latitude": start[0],
                "Longitude": start[1]
            },
            "TargetLocation": {
                "Latitude": end[0],
                "Longitude": end[1]
            },
            "targetTime": {
                "StartTime": request.form.get("target_start_time") or None,
                "EndTime": request.form.get("target_end_time") or None
            },
            "pickupTime": {
                "StartTime": request.form.get("pickup_start_time") or None,
                "EndTime": request.form.get("pickup_end_time") or None
            },
            "needRamp": bool(request.form.get("needRamp")),
            "requestedAdults": int(request.form.get("requestedAdults", 0)),
            "requestedChilds": int(request.form.get("requestedChilds", 0)),
            "luggage": int(request.form.get("luggage", 0)),
            "personalPreferences": {
                "allowCarpooling": bool(request.form.get("allowCarpooling")),
                "toleratedDelayBefore": int(request.form.get("toleratedDelayBefore", 0)),
                "toleratedDelayAfter": int(request.form.get("toleratedDelayAfter", 0))
            },
            "SimulatedTime": request.form.get("SimulatedTime") or None,
            "bookProposal": bool(request.form.get("bookProposal")),
            "createReport": bool(request.form.get("createReport"))
        }
        loaded_data.setdefault("rideRequests", []).append(new_request)
        return redirect(url_for('index'))
    return render_template("rideRequests_create.html")

@app.route('/rideRequests/<req_id>/edit', methods=['GET', 'POST'])
@_require_operation_area
def edit_ride_request(req_id):
    """Edit an existing ride request and ensure optional nested fields exist before rendering."""
    req = next((r for r in loaded_data.get('rideRequests', []) if r['id'] == req_id), None)
    if not req:
        return "RideRequest not found", 404
    
    if "targetTime" not in req or req["targetTime"] is None:
        req["targetTime"] = {"StartTime": None, "EndTime": None}
    if "pickupTime" not in req or req["pickupTime"] is None:
        req["pickupTime"] = {"StartTime": None, "EndTime": None}
    if "personalPreferences" not in req or req["personalPreferences"] is None:
        req["personalPreferences"] = {
            "allowCarpooling": False,
            "toleratedDelayBefore": 0,
            "toleratedDelayAfter": 0
        }
        
    if request.method == 'POST':
        start = _parse_required_coordinates(request.form, "start_latitude", "start_longitude")
        end = _parse_required_coordinates(request.form, "end_latitude", "end_longitude")
        if start is None or end is None:
            return render_template(
                "rideRequests_edit.html", req=req,
                error="Bitte Breitengrad und Längengrad für Abhol- und Zielposition angeben (z. B. per Klick auf die Karte oder manuell eingeben).",
            ), 400
        req["CurrentLocation"]["Latitude"], req["CurrentLocation"]["Longitude"] = start
        req["TargetLocation"]["Latitude"], req["TargetLocation"]["Longitude"] = end
        req["targetTime"]["StartTime"] = request.form.get("target_start_time") or None
        req["targetTime"]["EndTime"] = request.form.get("target_end_time") or None
        req["pickupTime"]["StartTime"] = request.form.get("pickup_start_time") or None
        req["pickupTime"]["EndTime"] = request.form.get("pickup_end_time") or None
        req["needRamp"] = bool(request.form.get("needRamp"))
        req["requestedAdults"] = int(request.form.get("requestedAdults", 0))
        req["requestedChilds"] = int(request.form.get("requestedChilds", 0))
        req["luggage"] = int(request.form.get("luggage", 0))
        req["personalPreferences"]["allowCarpooling"] = bool(request.form.get("allowCarpooling"))
        req["personalPreferences"]["toleratedDelayBefore"] = int(request.form.get("toleratedDelayBefore", 0))
        req["personalPreferences"]["toleratedDelayAfter"] = int(request.form.get("toleratedDelayAfter", 0))
        req["SimulatedTime"] = request.form.get("SimulatedTime") or None
        req["bookProposal"] = bool(request.form.get("bookProposal"))
        req["createReport"] = bool(request.form.get("createReport"))
        return redirect(url_for('index'))
    return render_template("rideRequests_edit.html", req=req)

@app.route('/rideRequests/<req_id>/delete', methods=['GET', 'POST'])
def delete_ride_request(req_id):
    """Delete a ride request from the currently loaded ride data."""
    req = next((r for r in loaded_data.get('rideRequests', []) if r['id'] == req_id), None)
    if not req:
        return "RideRequest not found", 404
    if request.method == 'POST':
        loaded_data['rideRequests'].remove(req)
        return redirect(url_for('index'))
    return render_template("rideRequests_delete.html", req=req)


@app.route('/rideRequests/filter-outside-area', methods=['POST'])
def filter_requests_outside_area():
    """Remove every loaded ride request whose pickup or dropoff falls outside the operation
    area. Opt-in only, triggered from the warning on /index - never runs on its own."""
    polygon = _operation_area_polygon()
    if polygon is None:
        return redirect(url_for('index'))
    loaded_data['rideRequests'] = [
        rr for rr in (loaded_data.get('rideRequests', []) or [])
        if _request_inside_operation_area(rr, polygon) is not False
    ]
    return redirect(url_for('index'))

# Seed completed jobs from disk once at process start (module-level, not inside the
# __name__ guard below - the documented launch command imports `app` directly rather than
# running this file as __main__, so that guard never fires there). Runs after every route/
# helper this depends on is already defined. _discover_persisted_jobs() resolves every job's
# permanent id itself.
if not DEMO_MODE:
    loaded_data["simulationJobs"].extend(_discover_persisted_jobs())

if __name__ == '__main__':
    # app.run(debug=True)
    app.run(host="0.0.0.0", port=80, debug=False)
