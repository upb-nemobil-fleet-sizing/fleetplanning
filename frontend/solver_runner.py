import argparse
import json
import math
import os
import sys
import types


def _install_sumo_stub_if_needed(use_sim: str):
    """Install a SUMO placeholder module when another solver is selected, so shared imports can still resolve."""
    if use_sim == "sumo":
        return

    module = types.ModuleType("sumo.sumo_simulation")

    class SumoSimulation:
        """Raise a clear error if SUMO code is imported while another solver backend is active."""
        def __init__(self, *args, **kwargs):
            """Prevent accidental SUMO construction when the wrapper is not running SUMO."""
            raise RuntimeError("SUMO is only available when --use_sim sumo is selected.")

    module.SumoSimulation = SumoSimulation
    sys.modules["sumo.sumo_simulation"] = module


def _install_custom_stub_if_needed(use_sim: str):
    """Install a CustomSimulation placeholder when custom simulation is not the active solver."""
    if use_sim == "custom":
        return

    module = types.ModuleType("custom_sim.custom_simulation")

    class CustomSimulation:
        """Raise a clear error if custom simulation code is imported for a non-custom run."""
        def __init__(self, *args, **kwargs):
            """Prevent accidental custom-simulation construction when another backend is selected."""
            raise RuntimeError("CustomSimulation is only available when --use_sim custom is selected.")

    module.CustomSimulation = CustomSimulation
    sys.modules["custom_sim.custom_simulation"] = module


def _install_optional_plotly_stub():
    """Provide lightweight Plotly stand-ins when plotting dependencies are not installed in the runtime."""
    try:
        import plotly.graph_objects  # noqa: F401
        import plotly.subplots  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    plotly = types.ModuleType("plotly")
    graph_objects = types.ModuleType("plotly.graph_objects")
    subplots = types.ModuleType("plotly.subplots")

    class _UnavailableFigure:
        """Represent unavailable Plotly objects and fail only when plotting is actually used."""
        def __getattr__(self, name):
            """Raise an explanatory error for any attempted Plotly figure operation."""
            raise RuntimeError("Plotly is not installed; plotting is unavailable.")

    def make_subplots(*args, **kwargs):
        """Return an unavailable figure placeholder for Plotly subplot calls."""
        return _UnavailableFigure()

    graph_objects.Scatter = _UnavailableFigure
    graph_objects.Bar = _UnavailableFigure
    subplots.make_subplots = make_subplots
    sys.modules["plotly"] = plotly
    sys.modules["plotly.graph_objects"] = graph_objects
    sys.modules["plotly.subplots"] = subplots


class _NoOp:
    """Absorb optional plotting calls when matplotlib is unavailable."""
    def __call__(self, *args, **kwargs):
        """Return the no-op object so chained matplotlib calls remain harmless."""
        return self

    def __iter__(self):
        """Behave like an empty iterable for optional unpacking paths."""
        return iter(())

    def __getitem__(self, key):
        """Return the no-op object for index access used by plotting code."""
        return self

    def __getattr__(self, name):
        """Return the no-op object for any optional matplotlib attribute."""
        return self


def _install_optional_matplotlib_stub():
    """Provide no-op matplotlib modules so solver code can run without rendering dependencies."""
    try:
        import matplotlib.pyplot  # noqa: F401
        import matplotlib.dates  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    matplotlib = types.ModuleType("matplotlib")
    pyplot = types.ModuleType("matplotlib.pyplot")
    dates = types.ModuleType("matplotlib.dates")
    noop = _NoOp()

    def subplots(*args, **kwargs):
        """Return no-op figure and axes objects for matplotlib subplot creation."""
        return noop, noop

    pyplot.subplots = subplots
    pyplot.tight_layout = noop
    pyplot.show = noop
    pyplot.savefig = noop
    dates.DateFormatter = lambda *args, **kwargs: noop
    dates.AutoDateLocator = lambda *args, **kwargs: noop
    sys.modules["matplotlib"] = matplotlib
    sys.modules["matplotlib.pyplot"] = pyplot
    sys.modules["matplotlib.dates"] = dates


def _load_fleet_planning_module(project_root: str):
    """Load fleet_planning.py from source after applying the small runtime-only patch needed by this wrapper."""
    source_path = os.path.join(project_root, "fleet_planning.py")
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    # The solver file currently contains a quote mismatch in an f-string.
    # Keep the solver file untouched and patch only the in-memory source used by this runner.
    source = source.replace(
        'strftime("%Y-%m-%d %H:%M:%S %z")',
        "strftime('%Y-%m-%d %H:%M:%S %z')",
    )

    module = types.ModuleType("fleet_planning_runtime")
    module.__file__ = source_path
    module.__name__ = "fleet_planning_runtime"
    exec(compile(source, source_path, "exec"), module.__dict__)
    return module


def _load_run_instance_module(project_root: str):
    """Load custom_sim/run_instance.py from source, mirroring the fleet_planning loader."""
    source_path = os.path.join(project_root, "custom_sim", "run_instance.py")
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    module = types.ModuleType("run_instance_runtime")
    module.__file__ = source_path
    module.__name__ = "run_instance_runtime"
    exec(compile(source, source_path, "exec"), module.__dict__)
    return module


def _load_module_from_source(module_name: str, source_path: str, replacements: dict[str, str] | None = None):
    """Load a Python module from source while optionally applying in-memory text replacements."""
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    for old, new in (replacements or {}).items():
        source = source.replace(old, new)

    module = types.ModuleType(module_name)
    module.__file__ = source_path
    module.__package__ = module_name.rpartition(".")[0]
    sys.modules[module_name] = module
    exec(compile(source, source_path, "exec"), module.__dict__)
    return module


def _preload_custom_runtime_patches(project_root: str, use_sim: str):
    """Preload patched custom-simulation modules that need source fixes before fleet_planning imports them."""
    if use_sim != "custom":
        return
    router_path = os.path.join(project_root, "custom_sim", "routing", "router.py")
    _load_module_from_source(
        "custom_sim.routing.router",
        router_path,
        {
            'self.router_setting["map"]': "self.router_setting['map']",
        },
    )


def _install_osmnx_nearest_nodes_fallback(use_sim: str):
    """Use a local nearest-node fallback when osmnx lacks its optional scikit-learn dependency."""
    if use_sim != "custom":
        return
    try:
        import osmnx as ox
    except Exception:
        return

    try:
        import sklearn  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    original_nearest_nodes = ox.distance.nearest_nodes

    def _fallback_nearest_nodes(G, X, Y, return_dist=False):
        xs = X if isinstance(X, (list, tuple)) else [X]
        ys = Y if isinstance(Y, (list, tuple)) else [Y]
        node_items = [
            (node_id, float(data.get("x", 0.0)), float(data.get("y", 0.0)))
            for node_id, data in G.nodes(data=True)
        ]
        if not node_items:
            raise ValueError("Cannot search nearest node in an empty graph.")

        nearest_ids = []
        nearest_distances = []
        for lon, lat in zip(xs, ys):
            lat_f = float(lat)
            lon_f = float(lon)
            lat_scale = math.cos(math.radians(lat_f))
            best_node = None
            best_dist = float("inf")
            for node_id, node_lon, node_lat in node_items:
                dx = (node_lon - lon_f) * lat_scale
                dy = node_lat - lat_f
                dist = dx * dx + dy * dy
                if dist < best_dist:
                    best_dist = dist
                    best_node = node_id
            nearest_ids.append(best_node)
            nearest_distances.append(math.sqrt(best_dist))

        scalar = not isinstance(X, (list, tuple)) and not isinstance(Y, (list, tuple))
        if return_dist:
            if scalar:
                return nearest_ids[0], nearest_distances[0]
            return nearest_ids, nearest_distances
        if scalar:
            return nearest_ids[0]
        return nearest_ids

    try:
        ox.distance.nearest_nodes = _fallback_nearest_nodes
    except Exception:
        ox.distance.nearest_nodes = original_nearest_nodes


def _json_safe(value):
    """Convert solver objects into JSON-safe primitive, list, and dictionary structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _build_solution_summary(heuristic):
    """Extract serializable iteration and KPI data from the captured FleetPlanning heuristic instance."""
    if heuristic is None:
        return {}
    iterations = []
    for index, solution in enumerate(getattr(heuristic, "solution_pool", []) or []):
        vehicle_fleet = getattr(solution, "vehicle_fleet", None)
        demand_scenario = getattr(solution, "demand_scenario", None)
        iterations.append(
            {
                "iteration": index,
                "numCabs": len(getattr(vehicle_fleet, "cabs", []) or []),
                "numPros": len(getattr(vehicle_fleet, "pros", []) or []),
                "numRequests": getattr(demand_scenario, "num_requests", None),
                "fleetKpis": _json_safe(getattr(solution, "fleet_dict", None)),
                "demandKpis": _json_safe(getattr(solution, "demand_dict", None)),
            }
        )
    return {
        "useSim": getattr(heuristic, "use_sim", None),
        "iteration": getattr(heuristic, "iteration", None),
        "runtimeSeconds": getattr(heuristic, "time", None),
        "oracleRuntimeSeconds": getattr(heuristic, "time_oracle", None),
        "iterations": iterations,
    }


def _install_solution_capture(fleet_planning):
    """Wrap FleetPlanning construction so the runner can write a summary after the solver finishes."""
    captured = {"heuristic": None}
    original_init = fleet_planning.FleetPlanning.__init__

    def wrapped_init(self, *args, **kwargs):
        """Capture the FleetPlanning instance after its original initializer has completed."""
        original_init(self, *args, **kwargs)
        captured["heuristic"] = self

    fleet_planning.FleetPlanning.__init__ = wrapped_init
    return captured


def _write_solution_summary(output_path: str, heuristic):
    """Write the captured solver summary JSON if an output path and heuristic instance are available."""
    if not output_path or heuristic is None:
        return
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(_build_solution_summary(heuristic), f, ensure_ascii=False, indent=2)


def main():
    """Run fleet_planning.py as a subprocess-friendly wrapper with dependency stubs and optional summary output."""
    parser = argparse.ArgumentParser()
    parser.add_argument("file_name")
    parser.add_argument("--base_data", required=True)
    parser.add_argument("--use_sim", choices=["rw", "sumo", "custom"], required=True)
    parser.add_argument("--job_type", choices=["search", "fixedfleet"], default="search")
    parser.add_argument("--iteration", type=int, default=0)
    parser.add_argument("--search_mode", choices=["adaptive", "static"], default="static")
    parser.add_argument("--iter_limit", type=int, default=100)
    parser.add_argument("--time_limit", type=float, default=18000)
    parser.add_argument("--cab_add_step", type=int, default=5)
    parser.add_argument("--stagnation_tolerance", type=float, default=0.0)
    parser.add_argument("--stagnation_tolerance_patience", type=int, default=1)
    parser.add_argument("--objective_weight", type=float, default=1.0)
    parser.add_argument("--objective_terms", default="total_served,avg_cost_per_trip_eur")
    parser.add_argument("--use_original_pro_timetable", action="store_true")
    parser.add_argument("--budget_eur", type=float, default=None)
    parser.add_argument("--service_level_min", type=float, default=None)
    parser.add_argument("--initial_cabs", type=int, default=None)
    parser.add_argument("--shrink_pros_allow_retry", action="store_true")
    parser.add_argument("--shrink_pros_max_tries", type=int, default=1)
    parser.add_argument("--no_shrink_pros_trial_probe_below_start", dest="shrink_pros_trial_probe_below_start", action="store_false", default=True)
    parser.add_argument("--shrink_pros_trial_overshoot_correction", action="store_true")
    parser.add_argument("--custom_sim_experiment_config", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--summary_output", default="")
    args = parser.parse_args()

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    _install_optional_plotly_stub()
    _install_optional_matplotlib_stub()
    _install_sumo_stub_if_needed(args.use_sim)
    _install_custom_stub_if_needed(args.use_sim)
    _preload_custom_runtime_patches(project_root, args.use_sim)
    _install_osmnx_nearest_nodes_fallback(args.use_sim)

    if args.job_type == "fixedfleet":
        run_instance = _load_run_instance_module(project_root)
        sys.argv = [
            "run_instance.py",
            "--base_data",
            args.base_data,
            "--ride_data",
            args.file_name,
            "--iteration",
            str(args.iteration),
        ]
        if args.custom_sim_experiment_config:
            sys.argv.extend(["--custom_sim_experiment_config", args.custom_sim_experiment_config])
        if args.verbose:
            sys.argv.append("--verbose")
        run_instance.main()
        return

    fleet_planning = _load_fleet_planning_module(project_root)
    captured = _install_solution_capture(fleet_planning)

    sys.argv = [
        "fleet_planning.py",
        args.file_name,
        "--base_data",
        args.base_data,
        "--use_sim",
        args.use_sim,
        "--search_mode",
        args.search_mode,
        "--iter_limit",
        str(args.iter_limit),
        "--time_limit",
        str(args.time_limit),
        "--cab_add_step",
        str(args.cab_add_step),
        "--stagnation_tolerance",
        str(args.stagnation_tolerance),
        "--stagnation_tolerance_patience",
        str(args.stagnation_tolerance_patience),
        "--objective_weight",
        str(args.objective_weight),
        "--objective_terms",
        args.objective_terms,
    ]
    if args.use_original_pro_timetable:
        sys.argv.append("--use_original_pro_timetable")
    if args.budget_eur is not None:
        sys.argv.extend(["--budget_eur", str(args.budget_eur)])
    if args.service_level_min is not None:
        sys.argv.extend(["--service_level_min", str(args.service_level_min)])
    if args.initial_cabs is not None:
        sys.argv.extend(["--initial_cabs", str(args.initial_cabs)])
    if args.shrink_pros_allow_retry:
        sys.argv.append("--shrink_pros_allow_retry")
    sys.argv.extend(["--shrink_pros_max_tries", str(args.shrink_pros_max_tries)])
    if not args.shrink_pros_trial_probe_below_start:
        sys.argv.append("--no_shrink_pros_trial_probe_below_start")
    if args.shrink_pros_trial_overshoot_correction:
        sys.argv.append("--shrink_pros_trial_overshoot_correction")
    if args.custom_sim_experiment_config:
        sys.argv.extend(["--custom_sim_experiment_config", args.custom_sim_experiment_config])
    if args.verbose:
        sys.argv.append("--verbose")
    try:
        fleet_planning.main()
    finally:
        _write_solution_summary(args.summary_output, captured.get("heuristic"))


if __name__ == "__main__":
    main()
