from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import math
import os
import re
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from models import CostModel


# --------------------------------------------------------------------------
# Central KPI Definitions
# --------------------------------------------------------------------------
SHORT_CUSTOMER_TRIP_THRESHOLD_M = 3000.0

SIM_REPORT_KEYS: List[str] = [
    "sumConsumedEnergy",
    "sumDistance",
    "sumDrivingTime",
    "itemCount",
    "sumServiceTime",
    "counterToursWithServices",
    "itemsPerTour",
    "kmPerItems",
    "sumCustomerDistance",
    "sumCustomerDrivingTime",
    "sumCustomerConsumedEnergy",
    "chargingTime",
    "sumChargingDrivingTime",
    "sumChargingDistance",
    "sumChargingConsumedEnergy",
    "sumChargedEnergy",
]

SIM_LABELS: Dict[str, str] = {
    "index": "Step Index",
    "rejects": "Number of Rejects",
    "sumConsumedEnergy": "Consumed Energy (Wh)",
    "sumDistance": "Total Driving Distance (m)",
    "sumDrivingTime": "Total Driving Time (s)",
    "itemCount": "Items per Tour (count)",
    "sumServiceTime": "Customer Service Time (s)",
    "sumCustomerDistance": "Customer Distance (m)",
    "sumCustomerDrivingTime": "Customer Driving Time (s)",
    "sumCustomerConsumedEnergy": "Customer Consumed Energy (Wh)",
    "chargingTime": "Charging Time (s)",
    "sumChargedEnergy": "Charged Energy (Wh)",
    "sumChargingDistance": "Charging Access Distance (m)",
    "sumChargingDrivingTime": "Charging Access Time (s)",
    "sumChargingConsumedEnergy": "Charging Access Consumed Energy (Wh)",
    "counterToursWithServices": "Tours With Services",
    "itemsPerTour": "Items per Tour",
    "kmPerItems": "km per Item",
}

SIM_METRIC_LABELS: Dict[str, str] = {
    "total_requests": "Total Requests",
    "accepted_requests": "Accepted Requests",
    "rejected_requests": "Rejected Requests",
    "rejects": "Number of Rejects",
    "rejection_rate": "Rejection Rate",
    "requests_with_no_proposal": "Requests With No Proposal",
    "total_steps": "Total Simulation Steps",
    "sumConsumedEnergy": "Consumed Energy (Wh)",
    "sumDistance": "Total Driving Distance (m)",
    "sumDrivingTime": "Total Driving Time (s)",
    "itemCount": "Items per Tour (count)",
    "sumServiceTime": "Customer Service Time (s)",
    "counterToursWithServices": "Tours With Services",
    "itemsPerTour": "Items per Tour",
    "kmPerItems": "km per Item",
    "sumCustomerDistance": "Customer Distance (m)",
    "sumCustomerDrivingTime": "Customer Driving Time (s)",
    "sumCustomerConsumedEnergy": "Customer Consumed Energy (Wh)",
    "chargingTime": "Charging Time (s)",
    "sumChargingDrivingTime": "Charging Access Time (s)",
    "sumChargingDistance": "Charging Access Distance (m)",
    "sumChargingConsumedEnergy": "Charging Access Consumed Energy (Wh)",
    "sumChargedEnergy": "Charged Energy (Wh)",
    "total_vehicle_distance_m": "Total Vehicle Distance (m)",
    "total_vehicle_driving_time_s": "Total Vehicle Driving Time (s)",
    "total_customer_distance_m": "Customer Distance (m)",
    "total_customer_driving_time_s": "Customer Driving Time (s)",
    "total_charging_stationary_time_s": "Charging Stationary Time (s)",
    "total_charged_energy_wh": "Charged Energy (Wh)",
}

CAB_METRIC_LABELS: Dict[str, str] = {
    "number_of_cabs": "Number of Cabs",
    "number_of_used_cabs": "Number of Used Cabs",
    "total_customer_trips": "Total Customer Trips",
    "short_customer_trips": f"Short Customer Trips (< {int(SHORT_CUSTOMER_TRIP_THRESHOLD_M)} m)",
    "total_customer_distance_m": "Total Customer Distance (m)",
    "total_customer_distance_with_pro_m": "Customer Distance With PRO Support (m)",
    "total_customer_distance_without_pro_m": "Customer Distance Without PRO Support (m)",
    "total_customer_in_vehicle_time_s": "Total Customer In-Vehicle Time (s)",
    "total_customer_consumed_energy_wh": "Customer Consumed Energy (Wh)",
    "total_customer_service_time_s": "Total Customer Service Time (s)",
    "total_empty_pickup_distance_m": "Total Empty Pickup Distance (m)",
    "total_empty_reposition_distance_m": "Total Empty Reposition Distance (m)",
    "total_empty_service_time_s": "Total Empty Service Time (s)",
    "total_empty_pickup_time_s": "Total Empty Pickup Time (s)",
    "total_empty_reposition_time_s": "Total Empty Reposition Time (s)",
    "total_empty_reposition_trips": "Total Empty Reposition Trips",
    "total_charging_access_distance_m": "Total Charging Access Distance (m)",
    "total_charging_access_time_s": "Total Charging Access Time (s)",
    "total_charging_access_consumed_energy_wh": "Charging Access Consumed Energy (Wh)",
    "total_charging_stationary_time_s": "Total Charging Stationary Time (s)",
    "total_charging_events": "Total Charging Events",
    "total_stationary_charging_energy_wh": "Stationary Charged Energy (Wh)",
    "total_pro_charging_energy_wh": "PRO Charged Energy (Wh)",
    "total_consumed_energy_wh": "Total Consumed Energy (Wh)",
    "total_vehicle_distance_m": "Total Vehicle Distance (m)",
    "total_vehicle_driving_time_s": "Total Vehicle Driving Time (s)",
    "total_vehicle_service_time_s": "Total Vehicle Service Time (s)",
    "total_vehicle_operating_time_s": "Total Vehicle Operating Time (s)",
    "total_charging_energy_wh": "Total Charged Energy (Wh)",
    "longest_customer_trip_distance_m": "Longest Customer Trip Distance (m)",
    "customer_vs_empty_ratio": "Customer vs Empty Distance Ratio",
    "pro_charging_ratio": "PRO Charging Ratio",
}

PRO_METRIC_LABELS: Dict[str, str] = {
    "number_of_pros": "Number of PRO Vehicles",
    "number_of_active_pros": "Number of Active PRO Vehicles",
    "total_chaining_events": "Total Chaining Trips",
    "total_chained_cabs": "Total Chained Cabs",
    "total_chaining_distance_m": "Total Chaining Distance (m)",
    "total_chaining_driving_time_s": "Total Chaining Driving Time (s)",
    "total_chaining_service_time_s": "Total Chaining Service Time (s)",
    "total_charged_energy_wh": "Total PRO Charged Energy (Wh)",
}

PAIRED_BASE_LABELS: Dict[str, str] = {
    "numCabs": "Number of Cabs (X)",
    "rejects": "Number of Rejects",
    "rejectionRate": "Rejection Rate",
}

# CAB metrics kept out of the paired table and pickers: the fleet size is already shown as the
# X axis and the fleet-setup badges.
PAIRED_OMITTED_CAB_METRICS: Set[str] = {"number_of_cabs"}

# Unprefixed paired-row metric keys where a HIGHER value is better (used to compute the
# Pareto-optimal frontier on the output page). Every other known metric defaults to
# lower-is-better, which matches the large majority of KPIs here (distances, times, energy,
# rejects, vehicle/Pro counts, costs).
HIGHER_IS_BETTER: Set[str] = {
    "total_customer_trips",
    "customer_vs_empty_ratio",
    "averageUtilization",
    "totalRevenueEur",
    "profitEur",
    "averageProfitPerTripEur",
}


def _metric_prefers_higher(key: str) -> bool:
    """Return True if a higher value of this (possibly sim_/cab_/pro_-prefixed) metric is better."""
    base = key
    for prefix in ("sim_", "cab_", "pro_"):
        if key.startswith(prefix):
            base = key[len(prefix):]
            break
    return base in HIGHER_IS_BETTER


def _pareto_frontier_mask(xs: List[float], ys: List[float], x_higher_better: bool, y_higher_better: bool) -> List[bool]:
    """Return a boolean mask marking the non-dominated (Pareto-optimal) points among (xs, ys)."""
    n = len(xs)
    if n == 0:
        return []
    # Normalize both axes to "smaller is better" so dominance is a plain min/min comparison.
    nx = [-x if x_higher_better else x for x in xs]
    ny = [-y if y_higher_better else y for y in ys]

    order = sorted(range(n), key=lambda i: (nx[i], ny[i]))
    mask = [False] * n
    running_min_y = math.inf
    for i in order:
        if ny[i] < running_min_y:
            mask[i] = True
            running_min_y = ny[i]
    return mask


RELOCATION_STOP_TYPES: Set[str] = {
    "relocation",
    "reposition",
    "chaining",
    "unchaining",
    "coupling",
    "decoupling",
}

CUSTOMER_STOP_TYPES: Set[str] = {"pickup", "dropoff"}


# --------------------------------------------------------------------------
# Generic Helpers
# --------------------------------------------------------------------------
def _safe_float(v: Any) -> float | None:
    """Convert a value to float when possible and return None for invalid values."""
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None


def _coerce_number_or_none(v: Any) -> float | None:
    """Normalize numeric-like values while preserving None for empty or non-numeric inputs."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _safe_float(v)
    try:
        return _safe_float(v)
    except Exception:
        return None


def _pick_ci(d: Dict[str, Any], candidates: List[str]) -> Any:
    """Pick the first matching dictionary value using a case-insensitive key lookup."""
    if not isinstance(d, dict):
        return None
    lower_map = {str(k).lower(): k for k in d.keys()}
    for name in candidates:
        k = lower_map.get(name.lower())
        if k is not None:
            return d[k]
    return None


def _parse_iso_dt(s: Any) -> float | None:
    """Parse an ISO-like timestamp into a Unix timestamp in seconds."""
    if not s or not isinstance(s, str):
        return None
    try:
        ss = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        return dt.timestamp()
    except Exception:
        try:
            return datetime.fromisoformat(s.split("+")[0].replace("Z", "")).timestamp()
        except Exception:
            return None


def _elapsed_seconds(start_iso: Any, end_iso: Any) -> float:
    """Return the non-negative elapsed seconds between two ISO-like timestamps."""
    start = _parse_iso_dt(start_iso)
    end = _parse_iso_dt(end_iso)
    if start is None or end is None or end <= start:
        return 0.0
    return float(end - start)


def _extract_output_idx_from_run_label(run_label: Any) -> int | None:
    """Extract the zero-based output iteration index from a run filename or label."""
    s = str(run_label or "").replace("\\", "/").lower()
    b = os.path.basename(s)

    m = re.search(r"output_(?:sim|cab|pro)_(\d+)(?:\.json)?$", b)
    if m:
        return int(m.group(1))

    nums = re.findall(r"(\d+)", b)
    if nums:
        return int(nums[-1])

    return None


def _stop_type(stop: Dict[str, Any]) -> str:
    """Normalize a stop type string for consistent KPI classification."""
    return str(stop.get("stopType", "") or "").strip().lower()


def _stop_num(stop: Dict[str, Any], key: str) -> float:
    """Read a numeric stop field with a safe zero fallback."""
    v = _coerce_number_or_none(stop.get(key))
    return float(v) if v is not None else 0.0


def _remaining_energy(stop: Dict[str, Any]) -> float | None:
    """Read the remaining-energy value from a stop."""
    return _coerce_number_or_none(stop.get("remainingEnergy"))


def _estimated_transition_energy_wh(current_stop: Dict[str, Any], next_stop: Dict[str, Any]) -> float:
    """Estimate consumed energy between two stops from remaining-energy and consumed-energy fields."""
    cur_rem = _remaining_energy(current_stop)
    nxt_rem = _remaining_energy(next_stop)
    if cur_rem is None or nxt_rem is None:
        return 0.0
    nxt_cons = _coerce_number_or_none(next_stop.get("consumedEnergy"))
    if nxt_cons is not None and nxt_cons >= 0:
        return float((nxt_rem + nxt_cons) - cur_rem)
    return float(nxt_rem - cur_rem)


def _stop_duration_seconds(stop: Dict[str, Any]) -> float:
    """Calculate a stop duration from arrival/departure timestamps or an explicit duration field."""
    elapsed = _elapsed_seconds(stop.get("arrival"), stop.get("departure"))
    if elapsed > 0:
        return elapsed
    return _stop_num(stop, "duration")


def _iter_json_entries(raw: Dict[str, Any] | List[Any]) -> List[Dict[str, Any]]:
    """Return output entries regardless of whether the payload is wrapped in a top-level collection."""
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        for key in ("vehicles", "cabs", "entries", "results", "pros", "proSchedules"):
            values = raw.get(key)
            if isinstance(values, list):
                entries = [e for e in values if isinstance(e, dict)]
                if entries:
                    return entries
        return [raw]
    return []


# --------------------------------------------------------------------------
# SIM KPI Extraction
# --------------------------------------------------------------------------
def _extract_sim_requests_with_no_proposal(raw: Dict[str, Any]) -> float | None:
    """Read the requestsWithNoProposal field from the overview when available."""
    overview = (raw or {}).get("overview") or {}
    if not isinstance(overview, dict):
        return None
    v = _coerce_number_or_none(_pick_ci(overview, ["requestsWithNoProposal"]))
    if v is None:
        return None
    return float(v)


def _derive_sim_reject_count(raw: Dict[str, Any]) -> float | None:
    """Derive the true reject count from the overview when available.

    Prefers total - successfulBookings over requestsWithNoProposal because the latter
    only counts requests that received no routing proposal, missing requests that got a
    proposal but were never actually booked.
    """
    overview = (raw or {}).get("overview") or {}
    if not isinstance(overview, dict):
        return None
    successful = _coerce_number_or_none(_pick_ci(overview, ["successfulBookings"]))
    total = len((raw or {}).get("stepResults", []) or [])
    if successful is not None and total > 0:
        return float(max(total - successful, 0))
    return _extract_sim_requests_with_no_proposal(raw)


def _count_sim_rejects_from_steps(step_results: List[Dict[str, Any]]) -> int:
    """Count rejected SIM requests from individual step results when no final count is present."""
    count = 0
    for s in step_results:
        if not isinstance(s, dict):
            continue
        trr = (s or {}).get("tripRequestResponse") or {}
        invalid = bool(s.get("invalidRequest", False))
        successful = bool(trr.get("successful", False))
        trip = s.get("trip", None)
        if trip is None or invalid or not successful:
            count += 1
    return count


def _final_or_last_series_value(raw: Dict[str, Any], kpis: Dict[str, Any], key: str) -> float:
    """Prefer a final-report KPI value and otherwise fall back to the last numeric series value."""
    final_report = (raw or {}).get("finalReport") or {}
    if isinstance(final_report, dict):
        v = _coerce_number_or_none(final_report.get(key))
        if v is not None:
            return float(v)
    arr = (kpis or {}).get(key)
    if isinstance(arr, list):
        last = None
        for vv in arr:
            v = _coerce_number_or_none(vv)
            if v is not None:
                last = float(v)
        if last is not None:
            return last
    return 0.0


def parse_output_sim_json(raw: Dict[str, Any], run_label: str) -> Dict[str, Any]:
    """Parse SIM output into time-series KPIs and final request-level metrics for charts and summaries."""
    step_results: List[Dict[str, Any]] = (raw or {}).get("stepResults", []) or []
    derived_reject_count = _derive_sim_reject_count(raw or {})
    requests_with_no_proposal = _extract_sim_requests_with_no_proposal(raw or {})

    kpis: Dict[str, List[Any]] = {"index": []}
    for key in SIM_REPORT_KEYS:
        kpis[key] = []
    kpis["rejects"] = []

    for i, step in enumerate(step_results):
        kpis["index"].append(i)
        report = (step or {}).get("report") or {}

        for key in SIM_REPORT_KEYS:
            if key == "itemCount":
                kpis[key].append(int(_coerce_number_or_none(report.get(key)) or 0))
            else:
                kpis[key].append(float(_coerce_number_or_none(report.get(key)) or 0.0))

        if derived_reject_count is None:
            trr = (step or {}).get("tripRequestResponse") or {}
            invalid = bool(step.get("invalidRequest", False))
            successful = bool(trr.get("successful", False))
            trip = step.get("trip", None)
            kpis["rejects"].append(1 if (trip is None or invalid or not successful) else 0)

    if derived_reject_count is not None:
        if kpis["index"]:
            kpis["rejects"] = [None] * (len(kpis["index"]) - 1) + [float(derived_reject_count)]
        else:
            kpis["index"] = [0]
            kpis["rejects"] = [float(derived_reject_count)]

    reject_count = int(derived_reject_count) if derived_reject_count is not None else _count_sim_rejects_from_steps(step_results)
    total_requests = len(step_results)

    metrics: Dict[str, float] = {
        "total_steps": float(len(step_results)),
        "total_requests": float(total_requests),
        "accepted_requests": float(max(total_requests - reject_count, 0)),
        "rejected_requests": float(reject_count),
        "rejects": float(reject_count),
        "rejection_rate": float((reject_count / total_requests) if total_requests else 0.0),
    }
    if requests_with_no_proposal is not None:
        metrics["requests_with_no_proposal"] = float(requests_with_no_proposal)

    for key in SIM_REPORT_KEYS:
        metrics[key] = _final_or_last_series_value(raw or {}, kpis, key)

    metrics["total_vehicle_distance_m"] = metrics.get("sumDistance", 0.0)
    metrics["total_vehicle_driving_time_s"] = metrics.get("sumDrivingTime", 0.0)
    metrics["total_customer_distance_m"] = metrics.get("sumCustomerDistance", 0.0)
    metrics["total_customer_driving_time_s"] = metrics.get("sumCustomerDrivingTime", 0.0)
    metrics["total_charging_stationary_time_s"] = metrics.get("chargingTime", 0.0)
    metrics["total_charged_energy_wh"] = metrics.get("sumChargedEnergy", 0.0)

    return {
        "run": run_label,
        "raw": raw or {},
        "kpis": kpis,
        "metrics": metrics,
        "meta": {
            "metric_source": "sim_step_results",
        },
    }


# --------------------------------------------------------------------------
# CAB KPI Extraction
# --------------------------------------------------------------------------
def _extract_trip_stops_from_cab_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return trip stops from either the direct CAB entry or its nested vehicle object."""
    if not isinstance(entry, dict):
        return []
    vehicle = entry.get("vehicle") if isinstance(entry.get("vehicle"), dict) else {}
    stops = entry.get("tripStops") or vehicle.get("tripStops") or []
    if isinstance(stops, list):
        return [s for s in stops if isinstance(s, dict)]
    return []


def _entry_vehicle_type(entry: Dict[str, Any]) -> str:
    """Infer the vehicle type from common output fields."""
    if not isinstance(entry, dict):
        return ""
    vehicle = entry.get("vehicle") if isinstance(entry.get("vehicle"), dict) else {}
    vtype = vehicle.get("vehicleType")
    if vtype is None:
        vtype = entry.get("vehicleType")
    return str(vtype or "").strip().lower()


def _is_cab_entry(entry: Dict[str, Any]) -> bool:
    """Decide whether an output entry represents a CAB vehicle."""
    if not isinstance(entry, dict):
        return False
    vtype = _entry_vehicle_type(entry)
    if vtype:
        return vtype != "pro"
    if _extract_trip_stops_from_cab_entry(entry):
        return True
    chaining = entry.get("chainingStops") or entry.get("ChainingStops")
    if isinstance(chaining, list) and chaining:
        return False
    return True


def _extract_cab_entries(raw: Dict[str, Any] | List[Any]) -> List[Dict[str, Any]]:
    """Filter a raw output payload down to entries that should be treated as CAB results."""
    entries = _iter_json_entries(raw)
    return [e for e in entries if _is_cab_entry(e)]


def _cab_entry_id(entry: Dict[str, Any], fallback_idx: int) -> str:
    """Resolve a stable CAB identifier from nested vehicle fields or a fallback index."""
    vehicle = entry.get("vehicle") if isinstance(entry.get("vehicle"), dict) else {}
    for source in (vehicle, entry):
        for key in ("id", "label", "vehicleId", "licensePlate"):
            val = source.get(key) if isinstance(source, dict) else None
            if isinstance(val, str) and val.strip():
                return val.strip()
    return f"Cab{fallback_idx + 1}"


def _matching_trip_guid(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Check whether pickup and dropoff stops belong to the same trip or user when identifiers exist."""
    a_guid = str(a.get("tripGuid") or "").strip()
    b_guid = str(b.get("tripGuid") or "").strip()
    if a_guid and b_guid:
        return a_guid == b_guid
    a_user = str(a.get("userGuid") or "").strip()
    b_user = str(b.get("userGuid") or "").strip()
    if a_user and b_user:
        return a_user == b_user
    return True


def _collect_customer_trip_pairs(stops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair pickup and dropoff stops so customer trip distance and service metrics can be computed.

    A trip's distance is the distance driven while the customer is on board: the legs ending at
    every stop after the pickup up to and including the dropoff (this includes chaining and
    unchaining legs of convoy trips). Its in-vehicle time runs from the pickup departure to the
    dropoff arrival."""
    pairs: List[Dict[str, Any]] = []
    used_dropoff_idx: Set[int] = set()

    for i, stop in enumerate(stops):
        if _stop_type(stop) != "pickup":
            continue
        dropoff_idx = None
        for j in range(i + 1, len(stops)):
            if j in used_dropoff_idx:
                continue
            candidate = stops[j]
            if _stop_type(candidate) != "dropoff":
                continue
            if _matching_trip_guid(stop, candidate):
                dropoff_idx = j
                break
        if dropoff_idx is None:
            continue

        used_dropoff_idx.add(dropoff_idx)
        dropoff = stops[dropoff_idx]
        distance_m = sum(_stop_num(stops[k], "distance") for k in range(i + 1, dropoff_idx + 1))
        consumed_energy_wh = sum(_stop_num(stops[k], "consumedEnergy") for k in range(i + 1, dropoff_idx + 1))
        # Pickup departure to dropoff arrival is the window guaranteed to have the
        # customer aboard; the stops' own "duration" field (boarding/alighting service
        # time) is added on top.
        transit_time_s = _elapsed_seconds(stop.get("departure"), dropoff.get("arrival"))
        in_vehicle_time_s = transit_time_s + _stop_num(stop, "duration") + _stop_num(dropoff, "duration")
        has_pro_support = False
        for k in range(i + 1, dropoff_idx):
            if _stop_type(stops[k]) in RELOCATION_STOP_TYPES:
                has_pro_support = True
                break

        pairs.append(
            {
                "pickup_idx": i,
                "dropoff_idx": dropoff_idx,
                "pickup": stop,
                "dropoff": dropoff,
                "distance_m": distance_m,
                "consumed_energy_wh": consumed_energy_wh,
                "in_vehicle_time_s": in_vehicle_time_s,
                "transit_time_s": transit_time_s,
                "with_pro": has_pro_support,
            }
        )

    return pairs


def _compute_operations_utilization_for_cab_entry(entry: Dict[str, Any]) -> float | None:
    """Calculate active-time utilization for one CAB from its schedule, driving, and charging stops."""
    if not isinstance(entry, dict):
        return None

    vehicle = entry.get("vehicle") if isinstance(entry.get("vehicle"), dict) else entry
    schedule = vehicle.get("schedule") if isinstance(vehicle, dict) else {}
    sched_start = _parse_iso_dt(_pick_ci(schedule or {}, ["startTime", "StartTime"]))
    sched_end = _parse_iso_dt(_pick_ci(schedule or {}, ["endTime", "EndTime"]))
    if sched_start is None or sched_end is None or sched_end <= sched_start:
        return None

    stops = _extract_trip_stops_from_cab_entry(entry)
    if not stops:
        return None

    active_seconds = 0.0
    for stop in stops:
        driving_time = _coerce_number_or_none(stop.get("drivingTime"))
        if driving_time is not None and driving_time > 0:
            active_seconds += float(driving_time)

        if _stop_type(stop) == "charging":
            active_seconds += _stop_duration_seconds(stop)

    operation_seconds = sched_end - sched_start
    if operation_seconds <= 0:
        return None

    util = active_seconds / operation_seconds
    return max(0.0, min(1.0, util))


def _compute_average_operations_utilization_from_raw(raw: Any) -> float | None:
    """Compute the mean active-time utilization across all CAB entries in a raw solver output."""
    entries: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]
    elif isinstance(raw, dict):
        if _extract_trip_stops_from_cab_entry(raw):
            entries = [raw]
        else:
            for key in ("vehicles", "cabs", "entries", "results"):
                maybe_list = raw.get(key)
                if isinstance(maybe_list, list):
                    entries = [e for e in maybe_list if isinstance(e, dict)]
                    if entries:
                        break

    if not entries:
        return None

    entries = [e for e in entries if _is_cab_entry(e)]
    if not entries:
        return None

    util_values: List[float] = []
    for entry in entries:
        u = _compute_operations_utilization_for_cab_entry(entry)
        if u is not None:
            util_values.append(u)

    if not util_values:
        return None
    return sum(util_values) / len(util_values)


def _compute_customer_trip_ratio_from_raw(raw: Any) -> float | None:
    """Estimate how much CAB driving time was spent carrying customers rather than running empty."""
    if not raw:
        return None

    total_driving_time = 0.0
    passenger_time = 0.0
    entries = raw if isinstance(raw, list) else [raw]

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        trip_stops = entry.get("tripStops") or entry.get("stops") or entry.get("events") or []
        if not isinstance(trip_stops, list):
            trip_stops = []

        for stop in trip_stops:
            if not isinstance(stop, dict):
                continue
            dt = _coerce_number_or_none(stop.get("drivingTime"))
            if dt is not None:
                total_driving_time += dt

        trips: Dict[str, Dict[str, Any]] = {}
        for stop in trip_stops:
            if not isinstance(stop, dict):
                continue
            trip_guid = stop.get("tripGuid") or ""
            if not trip_guid:
                continue
            rec = trips.setdefault(trip_guid, {"pickup_departure": None, "dropoff_arrival": None})
            stype = _stop_type(stop)
            dep = _parse_iso_dt(stop.get("departure"))
            arr = _parse_iso_dt(stop.get("arrival"))
            if stype == "pickup":
                rec["pickup_departure"] = dep if dep is not None else arr
            elif stype == "dropoff":
                rec["dropoff_arrival"] = arr if arr is not None else dep

        for trip_guid, rec in trips.items():
            start = rec.get("pickup_departure")
            end = rec.get("dropoff_arrival")
            if start is not None and end is not None and end >= start:
                passenger_time += (end - start)
            else:
                pickup_idx = None
                drop_idx = None
                for idx, stop in enumerate(trip_stops):
                    if not isinstance(stop, dict):
                        continue
                    if stop.get("tripGuid") != trip_guid:
                        continue
                    stype = _stop_type(stop)
                    if stype == "pickup" and pickup_idx is None:
                        pickup_idx = idx
                    if stype == "dropoff":
                        drop_idx = idx
                if pickup_idx is not None and drop_idx is not None and drop_idx >= pickup_idx:
                    seg_drive = 0.0
                    for j in range(pickup_idx + 1, drop_idx + 1):
                        s = trip_stops[j]
                        if not isinstance(s, dict):
                            continue
                        dt = _coerce_number_or_none(s.get("drivingTime"))
                        if dt is not None:
                            seg_drive += dt
                    if seg_drive > 0:
                        passenger_time += seg_drive

    if total_driving_time > 0:
        return passenger_time / total_driving_time
    return None


def _extract_legacy_cab_rejects(raw: Dict[str, Any] | List[Any]) -> float | None:
    """Read legacy CAB rejection fields that may still appear in older output payloads."""
    base_keys = ["numberOfRejects", "rejects", "rejectCount"]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = _coerce_number_or_none(_pick_ci(item, base_keys))
            if value is not None:
                return float(value)
        return None
    if isinstance(raw, dict):
        value = _coerce_number_or_none(_pick_ci(raw, base_keys))
        if value is not None:
            return float(value)
    return None


def _compute_cab_detailed_metrics(raw: Dict[str, Any] | List[Any]) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Aggregate detailed CAB KPIs from trip stops, charging events, and customer trip pairs."""
    entries = _extract_cab_entries(raw)
    metrics: Dict[str, float] = {
        "number_of_cabs": float(len(entries)),
        "number_of_used_cabs": 0.0,
        "total_customer_trips": 0.0,
        "short_customer_trips": 0.0,
        "total_customer_distance_m": 0.0,
        "total_customer_distance_with_pro_m": 0.0,
        "total_customer_distance_without_pro_m": 0.0,
        "total_customer_in_vehicle_time_s": 0.0,
        "total_customer_consumed_energy_wh": 0.0,
        "total_customer_service_time_s": 0.0,
        "total_empty_pickup_distance_m": 0.0,
        "total_empty_reposition_distance_m": 0.0,
        "total_empty_service_time_s": 0.0,
        "total_empty_pickup_time_s": 0.0,
        "total_empty_reposition_time_s": 0.0,
        "total_empty_reposition_trips": 0.0,
        "total_charging_access_distance_m": 0.0,
        "total_charging_access_time_s": 0.0,
        "total_charging_access_consumed_energy_wh": 0.0,
        "total_charging_stationary_time_s": 0.0,
        "total_charging_events": 0.0,
        "total_stationary_charging_energy_wh": 0.0,
        "total_pro_charging_energy_wh": 0.0,
        "total_consumed_energy_wh": 0.0,
        "total_vehicle_distance_m": 0.0,
        "total_vehicle_driving_time_s": 0.0,
        "total_vehicle_service_time_s": 0.0,
        "total_vehicle_operating_time_s": 0.0,
        "total_charging_energy_wh": 0.0,
        "longest_customer_trip_distance_m": 0.0,
        "average_utilization": 0.0,
        "customer_vs_empty_ratio": 0.0,
    }

    longest_customer_trip: Dict[str, Any] | None = None
    used_cabs = 0

    for entry_idx, entry in enumerate(entries):
        stops = _extract_trip_stops_from_cab_entry(entry)
        cab_id = _cab_entry_id(entry, entry_idx)
        trip_pairs = _collect_customer_trip_pairs(stops)
        if trip_pairs:
            used_cabs += 1

        for trip in trip_pairs:
            metrics["total_customer_trips"] += 1.0
            dist_m = float(trip.get("distance_m") or 0.0)
            if dist_m < SHORT_CUSTOMER_TRIP_THRESHOLD_M:
                metrics["short_customer_trips"] += 1.0

            metrics["total_customer_distance_m"] += dist_m
            metrics["total_customer_in_vehicle_time_s"] += float(trip.get("in_vehicle_time_s") or 0.0)
            metrics["total_customer_consumed_energy_wh"] += float(trip.get("consumed_energy_wh") or 0.0)

            if trip.get("with_pro"):
                metrics["total_customer_distance_with_pro_m"] += dist_m
            else:
                metrics["total_customer_distance_without_pro_m"] += dist_m

            if dist_m > metrics["longest_customer_trip_distance_m"]:
                metrics["longest_customer_trip_distance_m"] = dist_m
                pickup = trip.get("pickup") if isinstance(trip.get("pickup"), dict) else {}
                dropoff = trip.get("dropoff") if isinstance(trip.get("dropoff"), dict) else {}
                longest_customer_trip = {
                    "cab_id": cab_id,
                    "trip_guid": str(dropoff.get("tripGuid") or pickup.get("tripGuid") or ""),
                    "user_guid": str(dropoff.get("userGuid") or pickup.get("userGuid") or ""),
                    "distance_m": dist_m,
                }

        for i, stop in enumerate(stops):
            stype = _stop_type(stop)
            distance_m = _stop_num(stop, "distance")
            driving_time_s = _stop_num(stop, "drivingTime")
            duration_s = _stop_num(stop, "duration")
            consumed_energy_wh = _stop_num(stop, "consumedEnergy")

            metrics["total_vehicle_distance_m"] += distance_m
            metrics["total_vehicle_driving_time_s"] += driving_time_s
            metrics["total_consumed_energy_wh"] += consumed_energy_wh

            if stype == "pickup":
                metrics["total_empty_pickup_distance_m"] += distance_m
                metrics["total_empty_pickup_time_s"] += driving_time_s
                metrics["total_customer_service_time_s"] += duration_s
            elif stype == "dropoff":
                metrics["total_customer_service_time_s"] += duration_s
            elif stype == "charging":
                metrics["total_charging_access_distance_m"] += distance_m
                metrics["total_charging_access_time_s"] += driving_time_s
                metrics["total_charging_access_consumed_energy_wh"] += consumed_energy_wh
                metrics["total_charging_stationary_time_s"] += _stop_duration_seconds(stop)
                metrics["total_charging_events"] += 1.0
                if i > 0:
                    delta = _estimated_transition_energy_wh(stops[i - 1], stop)
                    if delta > 0:
                        metrics["total_stationary_charging_energy_wh"] += delta
            elif stype in RELOCATION_STOP_TYPES:
                metrics["total_empty_reposition_distance_m"] += distance_m
                metrics["total_empty_reposition_time_s"] += driving_time_s
                metrics["total_empty_service_time_s"] += duration_s
                metrics["total_empty_reposition_trips"] += 1.0
            elif stype not in ("depot", ""):
                metrics["total_empty_service_time_s"] += duration_s

            if i + 1 < len(stops):
                delta = _estimated_transition_energy_wh(stop, stops[i + 1])
                if delta > 0 and stype != "charging":
                    nxt_type = _stop_type(stops[i + 1])
                    if stype in RELOCATION_STOP_TYPES or nxt_type in RELOCATION_STOP_TYPES:
                        metrics["total_pro_charging_energy_wh"] += delta

    metrics["number_of_used_cabs"] = float(used_cabs)
    metrics["total_vehicle_service_time_s"] = (
        metrics["total_empty_service_time_s"] + metrics["total_customer_service_time_s"]
    )
    metrics["total_vehicle_operating_time_s"] = (
        metrics["total_vehicle_service_time_s"] + metrics["total_charging_stationary_time_s"]
    )
    metrics["total_charging_energy_wh"] = (
        metrics["total_stationary_charging_energy_wh"] + metrics["total_pro_charging_energy_wh"]
    )

    if metrics["total_charging_energy_wh"] > 0:
        metrics["pro_charging_ratio"] = round(
            metrics["total_pro_charging_energy_wh"] / metrics["total_charging_energy_wh"], 4
        )

    util = _compute_average_operations_utilization_from_raw(raw)
    if util is None:
        util = _compute_customer_trip_ratio_from_raw(raw)
    metrics["average_utilization"] = float(util) if util is not None else 0.0

    if metrics["total_vehicle_distance_m"] > 0:
        metrics["customer_vs_empty_ratio"] = (
            metrics["total_customer_distance_m"] / metrics["total_vehicle_distance_m"]
        )
    else:
        metrics["customer_vs_empty_ratio"] = 0.0

    meta = {
        "short_trip_threshold_m": SHORT_CUSTOMER_TRIP_THRESHOLD_M,
        "longest_customer_trip": longest_customer_trip or {},
    }
    return metrics, meta


def parse_output_cab_json(raw: Dict[str, Any] | List[Any], run_label: str) -> Dict[str, Any]:
    """Parse CAB output into normalized KPI, metric, and metadata structures for the dashboard."""
    metrics, meta = _compute_cab_detailed_metrics(raw)

    numeric_kpis: Dict[str, float] = dict(metrics)

    numeric_kpis["averageUtilization"] = metrics.get("average_utilization", 0.0)

    legacy_rejects = _extract_legacy_cab_rejects(raw)
    if legacy_rejects is not None:
        numeric_kpis["numberOfRejects"] = legacy_rejects

    return {
        "run": run_label,
        "raw": raw or {},
        "kpis": numeric_kpis,
        "metrics": metrics,
        "meta": meta,
    }


# --------------------------------------------------------------------------
# PRO KPI Extraction
# --------------------------------------------------------------------------
def _extract_pro_entries(raw: Dict[str, Any] | List[Any]) -> List[Dict[str, Any]]:
    """Filter raw output entries down to PRO vehicles or entries with chaining stops."""
    entries = _iter_json_entries(raw)
    result: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if _entry_vehicle_type(entry) == "pro":
            result.append(entry)
            continue
        chaining = entry.get("chainingStops") or entry.get("ChainingStops")
        if isinstance(chaining, list) and chaining:
            result.append(entry)
    return result


def _get_pro_chaining_stops(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return chaining stops from a PRO entry using supported key variants."""
    for key in ("chainingStops", "ChainingStops", "stops", "Stops"):
        value = entry.get(key)
        if isinstance(value, list):
            return [s for s in value if isinstance(s, dict)]
    return []


def _compute_pro_detailed_metrics(raw: Dict[str, Any] | List[Any]) -> Dict[str, float]:
    """Aggregate PRO chaining counts, distances, driving time, and service time from output entries.

    Each realized towing trip is represented by a Start stop (distance=0) and an End stop
    (distance>0), both carrying the same chainedCabs count. Only the End stop is counted for
    chainedCabs/distance/drivingTime, to avoid double-counting and to exclude empty repositioning
    runs; total_chaining_service_time_s is the exception, it sums both stops' own duration (the
    fixed chain/unchain handshake time at each end of the leg).
    """
    entries = _extract_pro_entries(raw)
    metrics: Dict[str, float] = {
        "number_of_pros": float(len(entries)),
        "number_of_active_pros": 0.0,
        "total_chaining_events": 0.0,
        "total_chained_cabs": 0.0,
        "total_chaining_distance_m": 0.0,
        "total_chaining_driving_time_s": 0.0,
        "total_chaining_service_time_s": 0.0,
        "total_charged_energy_wh": 0.0,
    }

    for entry in entries:
        stops = _get_pro_chaining_stops(entry)
        pro_active = False
        for i, stop in enumerate(stops):
            chained = int(_stop_num(stop, "chainedCabs"))
            if chained <= 0:
                continue
            dist = _stop_num(stop, "distance")
            if dist <= 0:
                # Start stop of the pair — skip, the other metrics are on the End stop
                continue
            pro_active = True
            metrics["total_chaining_events"] += 1.0
            metrics["total_chained_cabs"] += float(chained)
            metrics["total_chaining_distance_m"] += dist
            metrics["total_chaining_driving_time_s"] += _stop_num(stop, "drivingTime")
            metrics["total_chaining_service_time_s"] += _stop_duration_seconds(stop)
            metrics["total_charged_energy_wh"] += _stop_num(stop, "chargedEnergy")
            if i > 0:
                metrics["total_chaining_service_time_s"] += _stop_duration_seconds(stops[i - 1])
        if pro_active:
            metrics["number_of_active_pros"] += 1.0

    return metrics


def parse_output_pro_json(raw: Dict[str, Any] | List[Any], run_label: str) -> Dict[str, Any]:
    """Parse PRO output and keep both detailed chaining metrics and legacy numeric KPI fields."""
    metrics = _compute_pro_detailed_metrics(raw)
    numeric_kpis: Dict[str, float] = dict(metrics)

    # Keep old behavior: include direct numeric values from top-level PRO objects.
    if isinstance(raw, list):
        sums: Dict[str, float] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                val = _coerce_number_or_none(v)
                if val is not None:
                    sums[k] = sums.get(k, 0.0) + val
        numeric_kpis.update(sums)
    elif isinstance(raw, dict):
        for k, v in raw.items():
            val = _coerce_number_or_none(v)
            if val is not None:
                numeric_kpis[k] = val

    return {
        "run": run_label,
        "raw": raw or {},
        "kpis": numeric_kpis,
        "metrics": metrics,
        "meta": {"metric_source": "pro_chaining_stops"},
    }


# --------------------------------------------------------------------------
# Paired Aggregation
# --------------------------------------------------------------------------
def _extract_num_cabs(cab_run: Dict[str, Any], sim_run: Dict[str, Any]) -> float | None:
    """Infer the fleet size from CAB/SIM metrics or the output filename iteration."""
    for src in (
        cab_run or {},
        sim_run or {},
        (cab_run or {}).get("kpis", {}),
        (sim_run or {}).get("kpis", {}),
        (cab_run or {}).get("metrics", {}),
        (sim_run or {}).get("metrics", {}),
    ):
        if not isinstance(src, dict):
            continue
        for key in ("numCabs", "numberOfCabs", "fleetSize", "fleet_size", "vehicleCount", "number_of_cabs"):
            v = _coerce_number_or_none(src.get(key))
            if v is not None:
                return float(v)

    idx_cab = _extract_output_idx_from_run_label((cab_run or {}).get("run"))
    idx_sim = _extract_output_idx_from_run_label((sim_run or {}).get("run"))
    idx = idx_cab if idx_cab is not None else idx_sim
    if idx is not None:
        return float(idx + 1)
    return None


def _sim_metrics_from_run(sim_run: Dict[str, Any]) -> Dict[str, float]:
    """Return normalized SIM metrics, rebuilding them from raw data if parsed metrics are missing."""
    metrics = (sim_run or {}).get("metrics")
    if isinstance(metrics, dict) and metrics:
        return {k: float(v) for k, v in metrics.items() if _coerce_number_or_none(v) is not None}

    raw = (sim_run or {}).get("raw", {}) or {}
    kpis = (sim_run or {}).get("kpis", {}) or {}
    fallback: Dict[str, float] = {}
    for key in SIM_REPORT_KEYS:
        fallback[key] = _final_or_last_series_value(raw, kpis, key)

    derived_reject_count = _derive_sim_reject_count(raw)
    reject_count = int(derived_reject_count) if derived_reject_count is not None else _count_sim_rejects_from_steps(raw.get("stepResults", []) or [])
    total_requests = len(raw.get("stepResults", []) or [])
    fallback.update(
        {
            "total_requests": float(total_requests),
            "accepted_requests": float(max(total_requests - reject_count, 0)),
            "rejected_requests": float(reject_count),
            "rejects": float(reject_count),
            "rejection_rate": float((reject_count / total_requests) if total_requests else 0.0),
        }
    )
    return fallback


def _summarize_sim_run(sim_run: Dict[str, Any]) -> Dict[str, float]:
    """Build the compact SIM metric subset used in paired output rows."""
    metrics = _sim_metrics_from_run(sim_run)
    return {
        "sim_sumConsumedEnergy": float(metrics.get("sumConsumedEnergy", 0.0)),
        "sim_sumDistance": float(metrics.get("sumDistance", 0.0)),
        "sim_sumDrivingTime": float(metrics.get("sumDrivingTime", 0.0)),
        "sim_itemCount": float(metrics.get("itemCount", 0.0)),
        "sim_sumCustomerDistance": float(metrics.get("sumCustomerDistance", 0.0)),
        "sim_sumCustomerDrivingTime": float(metrics.get("sumCustomerDrivingTime", 0.0)),
        "sim_sumCustomerConsumedEnergy": float(metrics.get("sumCustomerConsumedEnergy", 0.0)),
        "sim_chargingTime": float(metrics.get("chargingTime", 0.0)),
        "sim_sumChargedEnergy": float(metrics.get("sumChargedEnergy", 0.0)),
        "sim_rejects": float(metrics.get("rejects", metrics.get("rejected_requests", 0.0))),
    }


def _output_cost_model(state: Dict[str, Any]) -> CostModel | None:
    """Build the CostModel used for output cost/revenue KPIs, only when a metadata file-backed payload is available."""
    meta = (state or {}).get("_outputSimulationMetadata")
    if not isinstance(meta, dict):
        return None

    defaults = CostModel()

    def _num(key: str, fallback: float) -> float:
        value = _coerce_number_or_none(meta.get(key))
        return float(value) if value is not None else fallback

    return CostModel(
        _cab_price=_num("cabPrice", defaults.cab_price),
        _pro_price=_num("proPrice", defaults.pro_price),
        _stationary_kwh_price=_num("stationaryKwhPrice", defaults.stationary_kwh_price),
        _pro_kwh_price=_num("proKwhPrice", defaults.pro_kwh_price),
        _cab_lifetime_years=_num("cabLifetimeYears", defaults.cab_lifetime_years),
        _pro_lifetime_years=_num("proLifetimeYears", defaults.pro_lifetime_years),
        _interest_rate_percent=_num("interestRatePercent", defaults.interest_rate_percent),
        _operating_days_per_year=_num("operatingDaysPerYear", defaults.operating_days_per_year),
        _fare_base_eur=_num("fareBaseEur", defaults.fare_base_eur),
        _fare_distance_eur_per_m=_num("fareDistanceEurPerM", defaults.fare_distance_eur_per_m),
        _fare_time_eur_per_hour=_num("fareTimeEurPerHour", defaults.fare_time_eur_per_hour),
        _fare_min_per_trip_eur=_num("fareMinPerTripEur", defaults.fare_min_per_trip_eur),
        _fare_daily_subscription_eur=_num("fareDailySubscriptionEur", defaults.fare_daily_subscription_eur),
    )


def _first_numeric(*values: Any) -> float | None:
    """Return the first value that can be interpreted as a number."""
    for value in values:
        number = _coerce_number_or_none(value)
        if number is not None:
            return float(number)
    return None


def _energy_kwh_breakdown(row: Dict[str, Any],
                          cab_run: Dict[str, Any],
                          pro_run: Dict[str, Any]) -> Tuple[float, float]:
    """Split a day's Cab energy into (grid_energy_kwh, pro_energy_kwh) for separate pricing.

    The actual grid/Pro deficit-assumption split lives in CostModel.split_grid_and_pro_energy_kwh
    (models.py) - the fleet-planning search objective calls the same method, so there's one
    source of truth for the assumption instead of two copies that could drift apart. This
    function's job is just extracting (consumed_wh, stationary_wh, pro_wh) from whatever shape
    of run data is available and converting to kWh for that call, including the degraded
    SIM-level fallback below.

    Falls back to the SIM-level energy totals (treated entirely as grid energy, since no
    stationary/Pro split exists at that level) when detailed Cab metrics aren't available.
    """
    cab_metrics = (cab_run or {}).get("metrics", {}) if isinstance(cab_run, dict) else {}
    cab_kpis = (cab_run or {}).get("kpis", {}) if isinstance(cab_run, dict) else {}

    consumed_wh = _first_numeric(
        cab_metrics.get("total_consumed_energy_wh") if isinstance(cab_metrics, dict) else None,
        cab_kpis.get("total_consumed_energy_wh") if isinstance(cab_kpis, dict) else None,
    )
    stationary_wh = _first_numeric(
        cab_metrics.get("total_stationary_charging_energy_wh") if isinstance(cab_metrics, dict) else None,
        cab_kpis.get("total_stationary_charging_energy_wh") if isinstance(cab_kpis, dict) else None,
    )
    pro_wh = _first_numeric(
        cab_metrics.get("total_pro_charging_energy_wh") if isinstance(cab_metrics, dict) else None,
        cab_kpis.get("total_pro_charging_energy_wh") if isinstance(cab_kpis, dict) else None,
    )

    if consumed_wh is not None and stationary_wh is not None and pro_wh is not None:
        return CostModel.split_grid_and_pro_energy_kwh(consumed_wh / 1000.0, stationary_wh / 1000.0, pro_wh / 1000.0)

    # Degraded fallback: no detailed breakdown available (e.g. a backend that doesn't emit
    # per-stop energy detail). Use whichever SIM-level total exists and treat it entirely as
    # grid energy - there's no way to isolate a Pro/hydrogen share at that level.
    sim_consumed_wh = _coerce_number_or_none(row.get("sim_sumConsumedEnergy"))
    sim_charged_wh = _coerce_number_or_none(row.get("sim_sumChargedEnergy") or row.get("sim_sumCharged_energy"))
    sim_consumed = float(sim_consumed_wh) / 1000.0 if sim_consumed_wh is not None else None
    sim_charged = float(sim_charged_wh) / 1000.0 if sim_charged_wh is not None else None
    grid_energy_kwh = sim_consumed if sim_consumed is not None else (sim_charged or 0.0)
    return (grid_energy_kwh, 0.0)


def _resolve_fleet_size(row: Dict[str, Any],
                        cab_run: Dict[str, Any],
                        pro_run: Dict[str, Any],
                        fallback_num_pros: float | None = None) -> Tuple[float, float]:
    """Resolve (num_cabs, num_pros) for a paired row from whichever source has them."""
    cab_metrics = (cab_run or {}).get("metrics", {}) if isinstance(cab_run, dict) else {}
    cab_kpis = (cab_run or {}).get("kpis", {}) if isinstance(cab_run, dict) else {}
    pro_metrics = (pro_run or {}).get("metrics", {}) if isinstance(pro_run, dict) else {}
    pro_kpis = (pro_run or {}).get("kpis", {}) if isinstance(pro_run, dict) else {}

    num_cabs = _first_numeric(
        row.get("numCabs"),
        cab_metrics.get("number_of_cabs") if isinstance(cab_metrics, dict) else None,
        cab_kpis.get("number_of_cabs") if isinstance(cab_kpis, dict) else None,
    )
    num_pros = _first_numeric(
        pro_metrics.get("number_of_pros") if isinstance(pro_metrics, dict) else None,
        pro_kpis.get("number_of_pros") if isinstance(pro_kpis, dict) else None,
        pro_kpis.get("numPros") if isinstance(pro_kpis, dict) else None,
        fallback_num_pros,
    )
    return (num_cabs if num_cabs is not None else 0.0, num_pros if num_pros is not None else 0.0)


def _add_average_customer_trip_cost(row: Dict[str, Any],
                                    cost_model: CostModel | None,
                                    cab_run: Dict[str, Any],
                                    pro_run: Dict[str, Any]) -> None:
    """Add per-trip cost and profit KPIs (amortized daily fleet cost and profit, each divided
    by trips served). Must run after _add_fleet_economics has populated row['dailyFleetCostEur']
    and row['profitEur'] for this row."""
    if not cost_model or "dailyFleetCostEur" not in row:
        return

    cab_metrics = (cab_run or {}).get("metrics", {}) if isinstance(cab_run, dict) else {}
    cab_kpis = (cab_run or {}).get("kpis", {}) if isinstance(cab_run, dict) else {}

    customer_trips = _first_numeric(
        cab_metrics.get("total_customer_trips") if isinstance(cab_metrics, dict) else None,
        cab_kpis.get("total_customer_trips") if isinstance(cab_kpis, dict) else None,
    )
    if customer_trips is None or customer_trips <= 0:
        return

    row["averageCustomerTripCostEur"] = round(row["dailyFleetCostEur"] / customer_trips, 4)
    if "profitEur" in row:
        row["averageProfitPerTripEur"] = round(row["profitEur"] / customer_trips, 4)


def _compute_total_trip_revenue_eur(cab_run: Dict[str, Any], cost_model: CostModel) -> float:
    """Sum a MATSim DRT-style fare over every customer trip pair found in a CAB run's raw output.

    Priced on transit_time_s, not in_vehicle_time_s: the meter runs from departure to arrival,
    not from boarding to alighting.
    """
    raw = (cab_run or {}).get("raw") if isinstance(cab_run, dict) else None
    if not raw:
        return 0.0

    total = 0.0
    for entry in _extract_cab_entries(raw):
        stops = _extract_trip_stops_from_cab_entry(entry)
        for trip in _collect_customer_trip_pairs(stops):
            distance_m = float(trip.get("distance_m") or 0.0)
            transit_time_s = float(trip.get("transit_time_s") or 0.0)
            total += cost_model.trip_fare(distance_m, transit_time_s)
    return total


def _add_fleet_economics(row: Dict[str, Any],
                         cost_model: CostModel | None,
                         cab_run: Dict[str, Any],
                         pro_run: Dict[str, Any],
                         fallback_num_pros: float | None = None) -> None:
    """Add daily fleet cost (split into its amortized-vehicle and same-day-energy components),
    trip revenue, and profit KPIs to a paired row."""
    if not cost_model:
        return

    num_cabs, num_pros = _resolve_fleet_size(row, cab_run, pro_run, fallback_num_pros)

    # The only figure in this whole family that is purely amortized (a one-time purchase price
    # spread over the vehicle's assumed lifetime) - everything downstream of it blends this with
    # same-day totals (energy, revenue), so only this one KPI carries an "(amortized)" label.
    daily_vehicle_cost_eur = num_cabs * cost_model.daily_cab_cost() + num_pros * cost_model.daily_pro_cost()

    grid_energy_kwh, pro_energy_kwh = _energy_kwh_breakdown(row, cab_run, pro_run)
    total_energy_cost_eur = (
        grid_energy_kwh * cost_model.stationary_kwh_price +
        pro_energy_kwh * cost_model.pro_kwh_price
    )
    # Same total as cost_model.daily_fleet_cost(num_cabs, num_pros, grid_energy_kwh,
    # pro_energy_kwh) - computed as the sum of the two rows above instead, since those are
    # separately reported KPIs (dailyVehicleCostEur, totalEnergyCostEur) here.
    daily_fleet_cost_eur = daily_vehicle_cost_eur + total_energy_cost_eur
    total_revenue_eur = _compute_total_trip_revenue_eur(cab_run, cost_model)

    row["dailyVehicleCostEur"] = round(daily_vehicle_cost_eur, 2)
    row["totalEnergyCostEur"] = round(total_energy_cost_eur, 2)
    row["dailyFleetCostEur"] = round(daily_fleet_cost_eur, 2)
    row["totalRevenueEur"] = round(total_revenue_eur, 2)
    row["profitEur"] = round(total_revenue_eur - daily_fleet_cost_eur, 2)


def _add_request_outcome_kpis(row: Dict[str, Any]) -> None:
    """Add reject count and rate to a paired row. Every request that no Cab serves is a reject, so
    rejects are the simulated request total minus the customer trips found in the CAB output. The
    simulator's own reject count is used when there is no CAB output to count trips from."""
    total = _coerce_number_or_none(row.get("sim_total_requests"))
    served = _coerce_number_or_none(row.get("cab_total_customer_trips"))
    if total is not None and served is not None:
        rejects = max(float(total) - float(served), 0.0)
    else:
        rejects = _coerce_number_or_none(row.get("sim_rejects"))
        if rejects is None:
            return
    row["rejects"] = float(rejects)
    row["rejectionRate"] = float(rejects) / float(total) if total else 0.0


def build_paired_runs(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Align SIM, CAB, and PRO runs by output iteration and combine them into comparable dashboard rows."""
    sim_runs = state.get("outputSimRuns", []) or []
    cab_runs = state.get("outputCabRuns", []) or []
    pro_runs = state.get("outputProRuns", []) or []
    cost_model = _output_cost_model(state)
    fallback_num_pros = float(len(state.get("proSchedules", []) or [])) if isinstance(state.get("proSchedules", []), list) else None

    sim_by_idx: Dict[int, Dict[str, Any]] = {}
    cab_by_idx: Dict[int, Dict[str, Any]] = {}
    pro_by_idx: Dict[int, Dict[str, Any]] = {}

    for r in sim_runs:
        i = _extract_output_idx_from_run_label(r.get("run"))
        if i is not None:
            sim_by_idx[i] = r
    for r in cab_runs:
        i = _extract_output_idx_from_run_label(r.get("run"))
        if i is not None:
            cab_by_idx[i] = r
    for r in pro_runs:
        i = _extract_output_idx_from_run_label(r.get("run"))
        if i is not None:
            pro_by_idx[i] = r

    all_idx = sorted(set(sim_by_idx.keys()) | set(cab_by_idx.keys()) | set(pro_by_idx.keys()))
    if not all_idx:
        n = max(len(sim_runs), len(cab_runs), len(pro_runs))
        all_idx = list(range(n))
        for i in range(n):
            if i < len(sim_runs):
                sim_by_idx[i] = sim_runs[i]
            if i < len(cab_runs):
                cab_by_idx[i] = cab_runs[i]
            if i < len(pro_runs):
                pro_by_idx[i] = pro_runs[i]

    paired: List[Dict[str, Any]] = []

    for idx in all_idx:
        sim_run = sim_by_idx.get(idx, {})
        cab_run = cab_by_idx.get(idx, {})
        pro_run = pro_by_idx.get(idx, {})

        row: Dict[str, Any] = {"numCabs": float(idx + 1), "detailIdx": idx}
        row.update(_summarize_sim_run(sim_run))

        sim_metrics = _sim_metrics_from_run(sim_run)
        for k, v in sim_metrics.items():
            n = _coerce_number_or_none(v)
            if n is not None:
                row[f"sim_{k}"] = float(n)

        cab_metrics = (cab_run or {}).get("metrics", {})
        if isinstance(cab_metrics, dict):
            for k, v in cab_metrics.items():
                n = _coerce_number_or_none(v)
                if n is not None:
                    row[f"cab_{k}"] = float(n)

            row["averageUtilization"] = float(cab_metrics.get("average_utilization", 0.0))

        # Legacy fallback from existing kpi fields.
        cab_kpis = (cab_run or {}).get("kpis", {})
        if isinstance(cab_kpis, dict) and "averageUtilization" not in row:
            n = _coerce_number_or_none(cab_kpis.get("averageUtilization"))
            if n is not None:
                row["averageUtilization"] = float(n)

        pro_metrics = (pro_run or {}).get("metrics", {})
        if isinstance(pro_metrics, dict):
            for k, v in pro_metrics.items():
                n = _coerce_number_or_none(v)
                if n is not None:
                    row[f"pro_{k}"] = float(n)

        nc = _extract_num_cabs(cab_run, sim_run)
        if nc is not None:
            row["numCabs"] = nc

        _add_request_outcome_kpis(row)
        _add_fleet_economics(row, cost_model, cab_run, pro_run, fallback_num_pros)
        _add_average_customer_trip_cost(row, cost_model, cab_run, pro_run)
        paired.append(row)

    paired.sort(key=lambda r: (r.get("numCabs") is None, r.get("numCabs")))
    return paired


def _build_paired_labels(include_cost_labels: bool = False) -> Dict[str, str]:
    """Build human-readable labels for all paired dashboard metrics."""
    labels: Dict[str, str] = dict(PAIRED_BASE_LABELS)

    for k, label in CAB_METRIC_LABELS.items():
        if k not in PAIRED_OMITTED_CAB_METRICS:
            labels.setdefault(f"cab_{k}", label)
    for k, label in PRO_METRIC_LABELS.items():
        labels.setdefault(f"pro_{k}", label)

    labels.setdefault("averageUtilization", "Average Utilization")
    if include_cost_labels:
        # Ordered so the table reads as the arithmetic it represents: vehicle investment +
        # energy = fleet cost; revenue - fleet cost = profit; fleet cost/profit per trip last.
        # Only dailyVehicleCostEur is purely amortized (a one-time price spread over years) -
        # every KPI below it blends that with same-day totals, so none of them carry the
        # "(amortized)" tag themselves; the breakdown row is the explanation, not a label suffix.
        labels.setdefault("dailyVehicleCostEur", "Daily Vehicle Cost (EUR, amortized)")
        labels.setdefault("totalEnergyCostEur", "Total Energy Cost (EUR)")
        labels.setdefault("dailyFleetCostEur", "Daily Fleet Cost (EUR)")
        labels.setdefault("totalRevenueEur", "Total Trip Revenue (EUR)")
        labels.setdefault("profitEur", "Daily Profit (EUR)")
        labels.setdefault("averageCustomerTripCostEur", "Average Customer Trip Cost (EUR)")
        labels.setdefault("averageProfitPerTripEur", "Average Profit per Trip (EUR)")
    return labels


# --------------------------------------------------------------------------
# Public Payload Builders
# --------------------------------------------------------------------------
def build_output_payload(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the full output payload consumed by charts, tables, and detail navigation."""
    paired = build_paired_runs(state)
    include_cost_labels = any(
        "averageCustomerTripCostEur" in row or "dailyFleetCostEur" in row for row in paired
    )
    return {
        "runs": {
            "sim": state.get("outputSimRuns", []),
            "cab": state.get("outputCabRuns", []),
            "pro": state.get("outputProRuns", []),
        },
        "labels": {
            "sim": SIM_LABELS,
            "cab": CAB_METRIC_LABELS,
            "pro": PRO_METRIC_LABELS,
        },
        "paired": {
            "runs": paired,
            "labels": _build_paired_labels(include_cost_labels),
            "higherIsBetter": sorted(HIGHER_IS_BETTER),
        },
    }


def build_chart_payload(state: Dict[str, Any], dataset: str, y1: str, y2: str, view: str = "progression") -> Dict[str, Any]:
    """Build a Plotly-ready chart payload for the selected dataset, KPI axes, and chart view."""
    payload = build_output_payload(state)

    if dataset == "paired":
        pairs = payload["paired"]["runs"]
        labels = payload["paired"]["labels"]
        x = [p.get("numCabs") for p in pairs]
        detail_idx = [p.get("detailIdx") for p in pairs]
        if any(d is None for d in detail_idx):
            detail_idx = list(range(len(pairs)))

        def get_val(p: Dict[str, Any], key: str):
            """Read one KPI value for charting while respecting disabled secondary-axis selections."""
            if not key or key == "(none)":
                return None
            return p.get(key)

        util_y = [p.get("averageUtilization") for p in pairs]
        traces_util = [
            {
                "x": x,
                "y": util_y,
                "type": "bar",
                "name": "Utilization",
                "customdata": detail_idx,
                "marker": {"color": "mediumorchid"},
                "hovertemplate": "<b>%{x}</b><br>Utilization: %{y:.2%}<extra></extra>",
            }
        ]

        layout_util = {
            "margin": {"l": 70, "r": 40, "t": 30, "b": 60},
            "xaxis": {"title": "Flottenkonfiguration"},
            "yaxis": {"title": "Utilization", "tickformat": ".0%"},
        }

        if view == "pareto" and y2 and y2 != "(none)":
            rows = [p for p in pairs if get_val(p, y1) is not None and get_val(p, y2) is not None]
            row_idx = [p.get("detailIdx") for p in rows]
            if any(d is None for d in row_idx):
                row_idx = list(range(len(rows)))
            raw_x = [get_val(p, y1) for p in rows]
            raw_y = [get_val(p, y2) for p in rows]

            x_higher_better = _metric_prefers_higher(y1)
            y_higher_better = _metric_prefers_higher(y2)

            # Plot each axis as "gap to the best value observed" (0 = best), regardless of
            # whether the raw metric is better lower or higher, so (0, 0) is always the ideal
            # (utopia) point the frontier bends around.
            x_ideal = (max(raw_x) if x_higher_better else min(raw_x)) if raw_x else 0.0
            y_ideal = (max(raw_y) if y_higher_better else min(raw_y)) if raw_y else 0.0
            xs = [(x_ideal - v) if x_higher_better else (v - x_ideal) for v in raw_x]
            ys = [(y_ideal - v) if y_higher_better else (v - y_ideal) for v in raw_y]

            def hover_text(p: Dict[str, Any], vx: float, vy: float) -> str:
                """Build a hover label with fleet composition and the raw (untransformed) KPI values."""
                cabs = p.get("numCabs")
                pros = p.get("pro_number_of_pros")
                cabs_str = f"{cabs:.0f}" if cabs is not None else "N/A"
                pros_str = f"{pros:.0f}" if pros is not None else "N/A"
                return (
                    f"Cabs: {cabs_str} · Pros: {pros_str}"
                    f"<br>{labels.get(y1, y1)}: {vx:g}"
                    f"<br>{labels.get(y2, y2)}: {vy:g}"
                )

            texts = [hover_text(p, raw_x[i], raw_y[i]) for i, p in enumerate(rows)]
            # xs/ys are already normalized to lower-is-better ("gap to best"), so dominance is
            # a plain min/min comparison in this space.
            mask = _pareto_frontier_mask(xs, ys, False, False)

            dominated = [i for i, m in enumerate(mask) if not m]
            frontier = sorted((i for i, m in enumerate(mask) if m), key=lambda i: xs[i])

            hovertemplate = "%{text}<extra></extra>"

            traces_main = [
                {
                    "x": [xs[i] for i in dominated],
                    "y": [ys[i] for i in dominated],
                    "text": [texts[i] for i in dominated],
                    "customdata": [row_idx[i] for i in dominated],
                    "mode": "markers",
                    "type": "scatter",
                    "name": "Dominated",
                    "marker": {"size": 8, "color": "lightgray"},
                    "hovertemplate": hovertemplate,
                },
                {
                    "x": [xs[i] for i in frontier],
                    "y": [ys[i] for i in frontier],
                    "mode": "lines",
                    "type": "scatter",
                    "name": "Pareto frontier",
                    "line": {"color": "seagreen", "width": 2, "dash": "dot", "shape": "hv"},
                    "hoverinfo": "skip",
                },
                {
                    "x": [xs[i] for i in frontier],
                    "y": [ys[i] for i in frontier],
                    "text": [texts[i] for i in frontier],
                    "customdata": [row_idx[i] for i in frontier],
                    "mode": "markers",
                    "type": "scatter",
                    "name": "Non-dominated",
                    "marker": {"size": 10, "color": "seagreen"},
                    "hovertemplate": hovertemplate,
                },
            ]

            layout_main = {
                "margin": {"l": 70, "r": 40, "t": 50, "b": 60},
                "hovermode": "closest",
                "xaxis": {"title": f"{labels.get(y1, y1)} (gap to best)"},
                "yaxis": {"title": f"{labels.get(y2, y2)} (gap to best)"},
                "legend": {"orientation": "h", "y": -0.25},
            }

            return {
                "chartMain": {"traces": traces_main, "layout": layout_main},
                "chartUtil": {"traces": traces_util, "layout": layout_util},
            }

        y1_vals = [get_val(p, y1) for p in pairs]
        y2_vals = [get_val(p, y2) for p in pairs]

        traces_main = [
            {
                "x": x,
                "y": y1_vals,
                "customdata": detail_idx,
                "mode": "lines+markers",
                "type": "scatter",
                "name": labels.get(y1, y1),
                "line": {"color": "royalblue", "width": 3},
                "marker": {"size": 8},
                "yaxis": "y",
            }
        ]

        layout_main = {
            "margin": {"l": 70, "r": 70, "t": 50, "b": 40},
            "hovermode": "closest",
            "xaxis": {"title": "Flottenkonfiguration"},
            "yaxis": {"title": labels.get(y1, y1)},
            "legend": {"orientation": "h", "y": -0.25},
        }

        if y2 and y2 != "(none)":
            traces_main.append(
                {
                    "x": x,
                    "y": y2_vals,
                    "customdata": detail_idx,
                    "mode": "lines+markers",
                    "type": "scatter",
                    "name": labels.get(y2, y2),
                    "line": {"color": "orangered", "width": 3},
                    "marker": {"size": 8},
                    "yaxis": "y2",
                }
            )
            layout_main["yaxis2"] = {
                "title": labels.get(y2, y2),
                "overlaying": "y",
                "side": "right",
                "showgrid": False,
            }

        return {
            "chartMain": {"traces": traces_main, "layout": layout_main},
            "chartUtil": {"traces": traces_util, "layout": layout_util},
        }

    runs = payload["runs"].get(dataset, [])
    labels = payload["labels"].get(dataset, {})
    traces = []
    for i, r in enumerate(runs):
        x = (r.get("kpis") or {}).get("index") or list(range(len((r.get("kpis") or {}).get(y1, []))))
        y = (r.get("kpis") or {}).get(y1)
        traces.append(
            {
                "x": x,
                "y": y,
                "mode": "lines+markers",
                "type": "scatter",
                "name": r.get("run") or f"Run {i + 1}",
            }
        )

    layout = {
        "margin": {"l": 60, "r": 30, "t": 30, "b": 50},
        "hovermode": "closest",
        "xaxis": {"title": labels.get("index", "Index")},
        "yaxis": {"title": labels.get(y1, y1)},
    }

    return {
        "chartMain": {"traces": traces, "layout": layout},
        "chartUtil": None,
    }
