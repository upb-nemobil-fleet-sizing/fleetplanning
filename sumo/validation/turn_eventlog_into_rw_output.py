"""
Turns a SUMO event log into the output files the dashboard reads:
<prefix>_output_cab_<iter>.json, <prefix>_output_pro_<iter>.json and <prefix>_output_sim_<iter>.json.

SumoSimulation calls build_rw_outputs() after every iteration with the vehicles it simulated.
Run as a script, the vehicles are taken from the BaseData file the simulation ran with:

    python validation/turn_eventlog_into_rw_output.py <event_log> <base_data> --output_dir <dir> --prefix <prefix> --iter <n>
"""
import argparse
import copy
import json 
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    from ..utilities import convert_simulation_time_to_datetime
except ImportError:
    # run as a script
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from utilities import convert_simulation_time_to_datetime


def parse_eventlog_to_dfs(file_name):
    with open(file_name, 'r') as file:
        lines = file.readlines()

    event_data = []
    reservation_data = []
    num_cabs = None
    num_pros = None

    for line in lines:
        parts = line.strip().split()
        if not parts:
            continue

        if parts[0] == "taxi":
            vehicle = f"{parts[0]} {parts[1]}"
            start_or_end = parts[2]
            type_entry = parts[3]

            if type_entry == "chargingStop":
                if len(parts) == 29:
                    reservation = f"{parts[5]} {parts[6][1:-1]}"
                    step = int(parts[9])
                    position = (float(parts[12][1:-1]), float(parts[13][:-1])) 
                    time = int(parts[-8])
                    distance = float(parts[-5])
                    energy = float(parts[-1])
                elif len(parts) == 30:
                    reservation = f"{parts[5]} {parts[7][1:-1]}"
                    step = int(parts[10])
                    position = (float(parts[13][1:-1]), float(parts[14][:-1])) 
                    time = int(parts[-8])
                    distance = float(parts[-5])
                    energy = float(parts[-1])

            elif type_entry in ["platoonApproach", "platoonTransport"]:
                reservation = [f"{parts[5]} {parts[6]}", parts[-12]]
                position = (float(parts[12][1:-1]), float(parts[13][:-1])) 
                step = int(parts[9])
                time = int(parts[-8])
                distance = float(parts[-5])
                energy = float(parts[-1])

            else:
                reservation = f"{parts[5]} {parts[6]}"
                position = (float(parts[12][1:-1]), float(parts[13][:-1])) 
                step = int(parts[9])
                time = int(parts[-8])
                distance = float(parts[-5])
                energy = float(parts[-1])

            event_data.append([vehicle, start_or_end, position, type_entry, reservation, step, distance, time, energy])

        elif parts[0] == "Reservation":
            if parts[2] == "registered":
                status = "registered"
                vehicle = "not assigned"
                step = int(parts[-1])
                reservation_id = f"{parts[0]} {parts[1]}"
            elif parts[2] == "assigned":
                status = "assigned"
                vehicle = f"{parts[4]} {parts[5]}"
                reservation_id = f"{parts[0]} {parts[1]}"
                step = ""
            elif parts[2] == "rejected":
                if len(parts) == 3:
                    status = "rejected"
                else:
                    status = "infeasible_tw"
                vehicle = None
                reservation_id = f"{parts[0]} {parts[1]}"
                step = ""

            reservation_data.append([reservation_id, status, step, vehicle])

        elif parts[0].startswith("Pro"):
            vehicle = parts[0]
            start_or_end = parts[1]
            type_entry = parts[2]

            if type_entry == "detachApproach":
                step = int(parts[5])
                reservation = [parts[-12], int(parts[7])]
                position = (float(parts[12][1:-1]), float(parts[13][:-1])) 
                time = int(parts[-8])
                distance = float(parts[-5])
                energy = float(parts[-1])

            elif type_entry == "attachApproach":
                step = int(parts[5])
                reservation = parts[-12]
                position = (float(parts[8][1:-1]), float(parts[9][:-1])) 
                time = int(parts[-8])
                distance = float(parts[-5])
                energy = float(parts[-1])

            event_data.append([vehicle, start_or_end, position, type_entry, reservation, step, distance, time, energy])
        elif parts[0].startswith("Initializing"):
            num_cabs = int(parts[3])
            num_pros = int(parts[6])

    event_df = pd.DataFrame(event_data, columns=['vehicle', 'start_or_end', 'position', 'type_entry', 'id', 'step', 'distance', 'duration', 'energy'])
    reservation_df = pd.DataFrame(reservation_data, columns=["id", "status", "step", "vehicle"])

    return event_df, reservation_df, num_cabs, num_pros


def read_fleet_ids(file_name):
    """
    Cab and Pro ids of the simulated fleet from the "Fleet cabs 1 2 4 pros 1 3" line SumoSimulation
    writes below the "Initializing" line, or (None, None) for older event logs without it.
    """
    with open(file_name, 'r') as file:
        for line in file:
            parts = line.split()
            if parts and parts[0] == "Fleet":
                split = parts.index("pros")
                return [int(i) for i in parts[2:split]], [int(i) for i in parts[split + 1:]]
    return None, None


# ------------------------------------------------------------------------------
# vehicle entries, same shape as the custom simulation writes them (custom_sim/utils_cs.py)

def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _location(lat, lon):
    return {"longitude": float(lon), "latitude": float(lat)}


def _license_plate(vehicle, fallback):
    plate = getattr(vehicle, "licensePlate", fallback)
    # models.Cab/Pro set it as a one-element tuple until set_id() is called
    if isinstance(plate, (list, tuple)):
        plate = plate[0] if plate else fallback
    return str(plate)


def _power_consumption(vehicle):
    return {
        "speed": getattr(vehicle, "pc_speed", 0),
        "wind": getattr(vehicle, "pc_wind", 0),
        "loadCapacity": getattr(vehicle, "pc_load_capacity", 0),
        "tireTraction": getattr(vehicle, "pc_tire_traction", 0),
        "weatherConditions": getattr(vehicle, "pc_weather_condition", 0),
        "climatronic": getattr(vehicle, "pc_climatronic", 0),
        "regenerativeBraking": getattr(vehicle, "pc_regenerative_breaking", 0),
    }


def cab_vehicle_payload(cab):
    """Output "vehicle" entry for a models.Cab."""
    return {
        "hasRamp": bool(getattr(cab, "has_ramp", False)),
        "seats": int(getattr(cab, "seats", 0) or 0),
        "childSeats": int(getattr(cab, "child_seats", 0) or 0),
        "luggage": int(getattr(cab, "luggage", 0) or 0),
        "chargingCurve": list(getattr(cab, "charging_curve", [0]) or [0]),
        "maxSpeedAutonomous": getattr(cab, "max_speed_autonomous", 0),
        "id": f"Cab{cab.id}",
        "schedule": {"startTime": _iso(cab.schedule_start_time), "endTime": _iso(cab.schedule_end_time)},
        "label": f"Cab{cab.id}",
        "licensePlate": _license_plate(cab, f"PB-NE-{cab.id}"),
        "powerConsumption": _power_consumption(cab),
        "flatratEnergyConsumption": getattr(cab, "flatrate_power_consumtion", 0),
        "risk": getattr(cab, "risc", 0),
        "totalEnergyCapacity": cab.total_energy_capacity,
        "initialEnergyCapacity": cab.init_energy_capacity,
        "initialLocation": _location(cab.init_location_lat, cab.init_location_lon),
        "maxSpeed": getattr(cab, "max_speed", 0),
        "regenerativePower": getattr(cab, "regenerative_power", 0),
        "operatingCompany": getattr(cab, "operating_company", ""),
        "maximumRegenerativePower": getattr(cab, "maximum_regenerative_power", 0),
        "defaultEnergyConsuptionPerM": getattr(cab, "default_energy_consumption_per_m", 0),
        "vehicleType": "cab",
        "stateOfSchedule": "Active",
    }


def pro_vehicle_payload(pro):
    """Output "vehicle" entry for a models.Pro."""
    payload = {
        "maxCabChain": getattr(pro, "max_cabs", 0),
        "additionalEnergyConsumptionPerCab": getattr(pro, "additional_consumption_per_cab", [0]),
        "id": f"Pro{pro.id}",
        "schedule": {"startTime": _iso(pro.schedule_start), "endTime": _iso(pro.schedule_end)},
        "label": f"Pro{pro.id}",
        "licensePlate": _license_plate(pro, f"PB-PR-{pro.id}"),
        "powerConsumption": _power_consumption(pro),
        "flatratEnergyConsumption": getattr(pro, "flatrate_power_consumtion", 0),
        "risk": getattr(pro, "risc", 0),
        "totalEnergyCapacity": pro.total_energy_capacity,
        "initialEnergyCapacity": pro.init_energy_capacity,
        "initialLocation": _location(pro.init_location_lat, pro.init_location_lon),
        "maxSpeed": getattr(pro, "max_speed", 0),
        "regenerativePower": getattr(pro, "regenerative_power", 0),
        "operatingCompany": getattr(pro, "operating_company", ""),
        "maximumRegenerativePower": getattr(pro, "maximum_regenerative_power", 0),
        "defaultEnergyConsuptionPerM": getattr(pro, "default_energy_consumption_per_m", 0),
        "vehicleType": "pro",
        "stateOfSchedule": "Active",
    }
    if getattr(pro, "max_power_supply", None) is not None:
        payload["maxCabChargingPower"] = pro.max_power_supply
    return payload


# ------------------------------------------------------------------------------
# output files

def sim_time_to_iso(step, service_start, ramp_up):
    return convert_simulation_time_to_datetime(simulation_time = step,
                                               service_start = service_start,
                                               ramp_up = ramp_up,
                                               return_datetime = True).isoformat()


def build_cab_output(df, cabs, service_start, ramp_up):
    result = []

    for cab in sorted(cabs, key=lambda cab: cab.id):
        cab_dict = {"vehicle":{}, "currentLocation": {}, "tripStops":{}}
        cab_dict["vehicle"] = cab_vehicle_payload(cab)
        cab_dict["estimatedTimeAtStop"] = "0001-01-01T00:00:00"
        cab_dict["estimatedEnergyAtStop"] = "0001-01-01T00:00:00"
        cab_dict["currentLocation"] = cab_dict["vehicle"]["initialLocation"]
        cab_dict["tripStops"] = turn_df_into_rw_trips(df[df["vehicle"] == f"taxi {cab.id}"], cab_dict, service_start, ramp_up)

        result.append(cab_dict)

    return result

def turn_df_into_rw_trips(df, cab_dict, service_start, ramp_up):
    cab_id = cab_dict["vehicle"]["id"]
    # initialize "depot" entry 
    depot = {}
    depot["vehicleSchedule"] = cab_id
    depot["tripGuid"] = ""
    depot["userGuid"] = ""
    depot["stopType"] = "Depot"
    depot["consumedEnergy"] = 0
    depot["remainingEnergy"] = cab_dict["vehicle"]["initialEnergyCapacity"]
    depot["currentQuantitites"] = 1
    depot["appointment"] = None
    depot["guidChainRouteSegment"] = None
    depot["key"] = f"{cab_id}_Start"
    depot["location"] = cab_dict["vehicle"]["initialLocation"]
    depot["latestStartDrivingToStop"] = _iso(service_start)
    depot["arrival"] = _iso(service_start)
    depot["departure"] = _iso(service_start)
    depot["duration"] = 0
    depot["drivingTime"] = 0
    depot["distance"] = 0

    results = [depot]
    # store remaining energy for updating
    remaining_energy = depot["remainingEnergy"]

    # events come in start/end pairs; a last unpaired start (simulation ended mid-trip) is skipped
    for i in range(0, len(df) - 1, 2):
        block = df.iloc[i:i+2]
        start = block.iloc[0]
        end = block.iloc[1]


        # skip charging appraoches as they are collapsed into one stop with charging stops
        if end["type_entry"] != "chargingApproach":
            next_entry = df.iloc[i+2] if i + 2 < len(df) else None

            stop = {}
            stop["vehicleSchedule"] = cab_id
            stop["tripGuid"] = ""
            stop["appointment"] = {"startTime": "", "endTime": ""}
            if end["type_entry"] == "platoonApproach" or end["type_entry"] == "platoonTransport":
                stop["userGuid"] = int(end["id"][0].split(" ")[-1])
            else:
                stop["userGuid"] = int(end["id"].split(" ")[-1])

            stop["location"] = {"longitude": float(end["position"][0]) , "latitude": float(end["position"][1])}
            stop["currentQuantitites"] = 1
            stop["guidChainRouteSegment"] = None
            service_time = 60
            if end["type_entry"] == "customerApproach":
                stop["stopType"] = "Pickup"
                stop["consumedEnergy"] = start["energy"]
                stop["remainingEnergy"] = remaining_energy - start["energy"]
                stop["key"] = f"{end['id'].split()[-1]}_Pickup"
                stop["drivingTime"] = float(start["duration"])
                stop["distance"] = float(start["distance"])
                remaining_energy = stop["remainingEnergy"]
                if next_entry is not None:
                    stop["duration"] =  float(next_entry["step"] - end["step"])
                else:
                    stop["duration"] = float(service_time)
                service_time = stop["duration"]

            elif end["type_entry"] == "customerTransport":
                stop["stopType"] = "Dropoff"
                stop["consumedEnergy"] = start["energy"]
                stop["remainingEnergy"] = remaining_energy - start["energy"]
                stop["key"] = f"{end['id'].split()[-1]}_Dropoff"
                stop["drivingTime"] = float(start["duration"])
                stop["distance"] = float(start["distance"])
                remaining_energy = stop["remainingEnergy"]
                stop["duration"] =  service_time

            elif end["type_entry"] == "platoonApproach":
                reservation_id = end["id"][0]
                pro_id = end["id"][1]
                stop["stopType"] = "Chaining"
                stop["consumedEnergy"] = start["energy"]
                stop["remainingEnergy"] = remaining_energy - start["energy"]
                stop["key"] = f"Chain_{pro_id}"
                stop["guidChainRouteSegment"] = pro_id
                stop["drivingTime"] = float(start["duration"])
                stop["distance"] = float(start["distance"])
                remaining_energy = stop["remainingEnergy"]
                stop["duration"] =  service_time


            elif end["type_entry"] == "platoonTransport":
                reservation_id = end["id"][0]
                pro_id = end["id"][1]
                stop["stopType"] = "Unchaining"
                stop["consumedEnergy"] = start["energy"]
                stop["remainingEnergy"] = remaining_energy - start["energy"]
                stop["key"] = f"Unchain_{pro_id}"
                stop["guidChainRouteSegment"] = pro_id
                stop["drivingTime"] = float(start["duration"])
                stop["distance"] = float(start["distance"])
                remaining_energy = stop["remainingEnergy"]
                stop["duration"] =  service_time


            elif end["type_entry"] == "chargingStop":
                start_approach = df.iloc[i-2]
                end_approach = df.iloc[i-1]

                stop["stopType"] = "Charging"
                stop["consumedEnergy"] = start_approach["energy"]
                stop["remainingEnergy"] = remaining_energy - start_approach["energy"]
                stop["key"] = "ChargeCharging"
                stop["drivingTime"] = float(start_approach["duration"])
                stop["distance"] = float(start_approach["distance"])
                remaining_energy = stop["remainingEnergy"]
                # add the charged energy onto the remaining energy for the next stop
                remaining_energy = remaining_energy - start["energy"]
                stop["duration"] =  float(start["duration"])

            if end["type_entry"] != "chargingStop":
                stop["latestStartDrivingToStop"] = sim_time_to_iso(start["step"], service_start, ramp_up)
                stop["arrival"] = sim_time_to_iso(end["step"], service_start, ramp_up)
            else:
                stop["latestStartDrivingToStop"] = sim_time_to_iso(start_approach["step"], service_start, ramp_up)
                stop["arrival"] = sim_time_to_iso(end_approach["step"], service_start, ramp_up)
            stop["departure"] = (datetime.fromisoformat(stop["arrival"]) + timedelta(seconds = int(stop["duration"]))).isoformat()
            results.append(stop)
    return results 



def build_pro_output(df, pros, service_start, ramp_up):
    result = []

    for pro in sorted(pros, key=lambda pro: pro.id):
        pro_dict = {}
        pro_dict["vehicle"] = pro_vehicle_payload(pro)
        pro_dict["currentLocation"] = pro_dict["vehicle"]["initialLocation"]
        pro_dict["chainingStops"] = turn_df_into_chain_stops(df[df["vehicle"] == f"Pro{pro.id}"], pro_dict, service_start, ramp_up)

        result.append(pro_dict)

    return result


def turn_df_into_chain_stops(df, pro_dict, service_start, ramp_up):
    pro_id = pro_dict["vehicle"]["id"]
    results = []
    # events come in start/end pairs; a last unpaired start (simulation ended mid-trip) is skipped
    for i in range(0, len(df) - 1, 2):
        block = df.iloc[i:i+2]
        start = block.iloc[0]
        end = block.iloc[1]
        # only the convoy legs (detach approaches) become chaining stops
        if end["type_entry"] == "detachApproach":
            start_entry = {}
            start_entry["vehicleSchedule"] = pro_id
            start_entry["chainedCabs"] = start["id"][1]
            start_entry["location"] = {"longitude": float(start["position"][0]) , "latitude": float(start["position"][1])}
            start_entry["key"] = f"{start['id'][0]}_Start"
            start_entry["drivingTime"] = 0
            start_entry["distance"] = 0
            start_entry["duration"] =  0
            start_entry["latestStartDrivingToStop"] = sim_time_to_iso(start["step"], service_start, ramp_up)
            start_entry["arrival"] = sim_time_to_iso(start["step"], service_start, ramp_up)
            start_entry["departure"] = start_entry["arrival"]

            stop_entry = {}
            stop_entry["vehicleSchedule"] = pro_id
            stop_entry["chainedCabs"] = start["id"][1]
            stop_entry["location"] = {"longitude": float(end["position"][0]) , "latitude": float(end["position"][1])}

            stop_entry["key"] = f"{start['id'][0]}_End"
            stop_entry["drivingTime"] = float(start["duration"])
            stop_entry["distance"] = float(start["distance"])
            stop_entry["duration"] =  0

            stop_entry["latestStartDrivingToStop"] = sim_time_to_iso(start["step"], service_start, ramp_up)
            stop_entry["arrival"] = sim_time_to_iso(end["step"], service_start, ramp_up)

            stop_entry["departure"] = (datetime.fromisoformat(stop_entry["arrival"]) + timedelta(seconds = int(stop_entry["duration"]))).isoformat()
            results.append(start_entry)
            results.append(stop_entry)
    return results 

def build_step_results(reservation_df, final_report, request_ids=None, invalid_request_ids=()):
    """
    One entry per request, the dashboard takes the number of requests (and with it the rejects)
    from the number of entries.

    request_ids:          all requests of the scenario, without them only the requests registered
                          in the event log are known
    invalid_request_ids:  requests SUMO could not place on the road network, never registered
    """
    statuses = {}
    vehicles = {}
    for row in reservation_df.itertuples(index=False):
        request_id = int(row.id.split()[-1])
        statuses.setdefault(request_id, set()).add(row.status)
        if row.status == "assigned":
            vehicles[request_id] = row.vehicle
    request_ids = sorted(statuses) if request_ids is None else [int(request_id) for request_id in request_ids]
    invalid_request_ids = {int(request_id) for request_id in invalid_request_ids}
    zero_report = {key: 0 for key in final_report}

    result_list = []
    for index, request_id in enumerate(request_ids):
        booked = "assigned" in statuses.get(request_id, set())
        entry = {}
        entry["proposal"] = ""
        entry["invalidRequest"] = request_id in invalid_request_ids
        entry["tripRequestResponse"] = {"bookingTransaction": "", 
                                        "userGuid": request_id, 
                                        "successful": booked,
                                        "possibleStartLocation": None, 
                                        "possibleTargetLocation": None, 
                                        "earliestPossibleStart": None, 
                                        "proposals": []
                                        }
        entry["bookTripResponse"] = {"successful": True, 
                                     "statusCode": "Successful",
                                     "createdTripGuid": "",
                                    } if booked else None
        entry["exception"] = None
        # as in the custom simulation: the final report on the last request, the others empty
        entry["report"] = final_report if index == len(request_ids) - 1 else zero_report
        entry["trip"] = {"tripGuid": "",
                         "userGuid": request_id,
                         "vehicleLabel": f"Cab{vehicles[request_id].split()[-1]}",
                        } if booked else None
        result_list.append(entry)

    return result_list



def build_sim_output(event_df, reservation_df, request_ids=None, invalid_request_ids=()):
    sim_dict = {"stepResults": [], "startReport": {}, "finalReport": {}, "overview": {}}
    sim_dict["startReport"] = {"sumConsumedEnergy": 0, 
                               "sumDistance": 0, 
                               "sumDrivingTime": 0, 
                               "itemCount": 0, 
                               "sumServiceTime": 0, 
                               "counterToursWithServices":0, 
                               "itemsPerTour":0, 
                               "kmPerItems":0, 
                               "sumCustomerDistance":0, 
                               "sumChargingDistance":0, 
                               "sumChargingConsumedEnergy":0, 
                               "sumChargedEnergy":0
                              }

    sim_dict["finalReport"] = {"sumConsumedEnergy": round(event_df[(event_df["start_or_end"] == "starts") & (event_df["type_entry"] != "chargingStop")]["energy"].sum(), 2), 
                               "sumDistance": round(event_df[(event_df["start_or_end"] == "starts") & (event_df["type_entry"] != "chargingStop")]["distance"].sum(), 2), 
                               "sumDrivingTime": round(float(event_df[(event_df["start_or_end"] == "starts") & (event_df["type_entry"] != "chargingStop")]["duration"].sum()), 2), 
                               "itemCount": 0, 
                               "sumServiceTime": len(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "customerApproach") | (event_df["type_entry"] == "customerTransport"))]) * 30, 
                               "counterToursWithServices":0, 
                               "itemsPerTour":0, 
                               "kmPerItems":0, 
                               "sumCustomerDistance":round(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "customerApproach") | (event_df["type_entry"] == "customerTransport"))]["distance"].sum(), 2), 
                               "sumCustomerDrivingTime":float(round(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "customerApproach") | (event_df["type_entry"] == "customerTransport"))]["duration"].sum(), 2)), 
                               "sumCustomerConsumedEnergy":float(round(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "customerApproach") | (event_df["type_entry"] == "customerTransport"))]["energy"].sum(), 2)), 
                               "sumChargingDistance":round(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "chargingApproach"))]["distance"].sum(), 2), 
                               "sumChargingDrivingTime":float(round(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "chargingApproach"))]["duration"].sum(), 2)), 
                               "sumChargingConsumedEnergy":float(-round(event_df[(event_df["start_or_end"] == "starts") & ((event_df["type_entry"] == "chargingApproach"))]["energy"].sum(), 2)), 
                               "chargingTime":float(abs(round(event_df[(event_df["start_or_end"] == "starts") & (event_df["type_entry"] == "chargingStop")]["duration"].sum(), 2))),
                               "sumChargedEnergy":abs(round(event_df[(event_df["start_or_end"] == "starts") & (event_df["type_entry"] == "chargingStop")]["energy"].sum(), 2)),
                              }


    sim_dict["overview"] = {
        "successfulBookings": float(len(reservation_df[reservation_df["status"] == "assigned"])), 
        "requestsWithNoProposal": float(len(reservation_df[reservation_df["status"] == "rejected"])),
        "requestsWithProposal": float(len(reservation_df[reservation_df["status"] == "assigned"]))
    }
    sim_dict["stepResults"] = build_step_results(reservation_df, sim_dict["finalReport"], request_ids, invalid_request_ids)

    return sim_dict


def build_rw_outputs(event_log_file, cabs, pros, service_start, ramp_up, request_ids=None, invalid_request_ids=()):
    """
    Build the cab, pro and sim output of one simulation run.

    cabs, pros:           the simulated models.Cab / models.Pro objects (vehicle ids as in the event log)
    service_start:        the datetime SUMO's simulation time counts from (after ramp_up)
    ramp_up:              the ramp-up time in seconds the simulation started with
    request_ids:          ids of all requests of the scenario, see build_step_results()
    invalid_request_ids:  ids of the requests SUMO could not place on the road network
    """
    event_df, reservation_df, _, _ = parse_eventlog_to_dfs(file_name=event_log_file)
    # stable, so events logged in the same step keep their logged order
    event_df = event_df.sort_values(by="step", kind="stable")

    event_df_pros = event_df[event_df["vehicle"].str.startswith("Pro")]
    event_df_cabs = event_df[event_df["vehicle"].str.startswith("taxi")]

    cab_output = build_cab_output(event_df_cabs, cabs, service_start, ramp_up)
    pro_output = build_pro_output(event_df_pros, pros, service_start, ramp_up)
    sim_output = build_sim_output(event_df, reservation_df, request_ids, invalid_request_ids)
    return cab_output, pro_output, sim_output


# ------------------------------------------------------------------------------
# command line use: vehicles from a BaseData file

def vehicles_from_base_data(base_data_file, event_log_file, num_cabs, num_pros):
    """
    Cab and Pro objects for the vehicles of the event log, built from the BaseData file the
    simulation ran with. Vehicles the BaseData file has no entry for (fleet planning adds
    copies of the first cab) are built from its first entry, the same way fleet planning does.
    """
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from models import Cab, Pro
    from utils import read_base_json

    base = read_base_json(base_data_file)
    cab_ids, pro_ids = read_fleet_ids(event_log_file)
    if cab_ids is None:
        # older event logs only carry the fleet size
        event_df, _, _, _ = parse_eventlog_to_dfs(event_log_file)
        logged = set(event_df["vehicle"])
        cab_ids = sorted(set(range(1, (num_cabs or 0) + 1)) | {int(v.split()[-1]) for v in logged if v.startswith("taxi")})
        pro_ids = sorted(set(range(1, (num_pros or 0) + 1)) | {int(v[3:]) for v in logged if v.startswith("Pro")})

    def build(vehicle_class, entries, vehicle_id):
        entry = next((e for e in entries if e["_id"] == vehicle_id), None)
        if entry is None:
            entry = copy.deepcopy(entries[0])
            entry["_id"] = vehicle_id
        return vehicle_class(**entry)

    cabs = [build(Cab, base["cab_schedules"], i) for i in cab_ids]
    pros = [build(Pro, base["pro_schedules"], i) for i in pro_ids] if base["pro_schedules"] else []
    service_start = Cab(**base["cab_schedules"][0]).schedule_start_time
    return cabs, pros, service_start


def main():
    parser = argparse.ArgumentParser(description="Turn a SUMO event log into the output files the dashboard reads.")
    parser.add_argument('event_log_file')
    parser.add_argument('base_data', help='BaseData file the simulation ran with, supplies the vehicle data')
    parser.add_argument('--output_dir', default='.', help='Directory to save output JSON files')
    parser.add_argument('--prefix', default='', help='Prefix for output file names')
    parser.add_argument('--iter', default='', help='specfies the iteratation corresponding to the results')
    parser.add_argument('--request_data', default=None,
                        help='request file the simulation ran with, so requests SUMO never registered count as rejects too')
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    _, reservation_df, num_cabs, num_pros = parse_eventlog_to_dfs(args.event_log_file)
    registered = reservation_df[reservation_df["status"] == "registered"]["id"]
    if registered.duplicated().any():
        print(f"Warning: {args.event_log_file} registers the same reservation more than once, "
              f"it probably contains several simulation runs appended to each other", file=sys.stderr)

    cabs, pros, service_start = vehicles_from_base_data(args.base_data, args.event_log_file, num_cabs, num_pros)
    # same ramp-up as SumoSimulation.initialize_iteration(), from the fleet size in the log header
    ramp_up = ((num_cabs if num_cabs is not None else len(cabs)) + (num_pros if num_pros is not None else len(pros))) * 5 + 20
    request_ids = None
    if args.request_data:
        from utils import read_request_json
        request_ids = [entry["_id"] for entry in read_request_json(args.request_data)]
    outputs = build_rw_outputs(args.event_log_file, cabs, pros, service_start, ramp_up, request_ids)

    for name, output in zip(("output_cab", "output_pro", "output_sim"), outputs):
        with open(os.path.join(args.output_dir, f"{args.prefix}_{name}_{args.iter}.json"), "w") as f:
            json.dump(output, f, indent=2)


if __name__ == "__main__":
    main()
