import os
import sys
import hashlib
import traceback
import re
import timeit
import base64
import math
import time
from pathlib import Path
from . import sumo_utilities, utilities
from .runner import run
from .validation.turn_eventlog_into_rw_output import build_rw_outputs, parse_eventlog_to_dfs
import xml.etree.ElementTree as ET
import xml.dom.minidom
from datetime import datetime, timezone, timedelta
import json
import csv
import numpy
import bisect
import random
import shutil
import subprocess
import requests
import networkx as nx
import pandas as pd
import copy
# Get the parent directory
parent_dir = Path(__file__).resolve().parent.parent

# Add parent directory to sys.path
sys.path.append(str(parent_dir))

from models import (Request, DemandScenario, Cab, Pro, VehicleFleet, ChargingStation, ChainingLocation, 
ParkingLocation, OperationalVertices, OperationalArea, FleetAndRequests, SearchParameters)

from .models_sumo import Reservation, Taxi, ProCab, ScheduleEntry
from .utilities import load_json_file

# we need to import python modules from the $SUMO_HOME/tools directory
if 'SUMO_HOME' in os.environ:
    tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
    sys.path.append(tools)
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")
import sumolib  # noqa
import traci

random.seed(42)
class SumoSimulation: 


    def __init__(self, _operations_area: OperationalArea, _cab : Cab, _pro : Pro, _input_request_file: str,
                 _output_folder: str = None):

        self.output_folder = _output_folder  # output subfolder name under BASE_DIR/output, see set_output_names()
        self.operations_area = copy.deepcopy(_operations_area)
        self.operations_area_unchanged = _operations_area
        self.polygon_string = self.parse_to_poly_string()
        # I/O path definitions
        self.base_data_file = f"{self.operations_area.operational_area.description}"
        net_file_id = "_".join(self.operations_area.operational_area.description.split("_")[:2])
        self.net_file_name = f"{net_file_id}.net.xml.gz"
        self.BASE_DIR       = Path(__file__).resolve().parent
        self.ROAD_NETWORK_DATA = self.BASE_DIR / "road_network_data"
        self.NETWORK_DATA       = self.BASE_DIR / f"network_data_{self.base_data_file}"
        self.NETWORK_DATA.mkdir(parents = True, exist_ok = True)
        self.INPUT_DIR      = self.BASE_DIR / "input"
        self.OUTPUT_DIR     = self.BASE_DIR / f"output_{self.base_data_file}"
        self.OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
        self.create_output_subdir()

        # create and preprocess network file is not run every run, therefore commented out
        self.create_and_preprocess_net_file(poly = self.polygon_string)
        # Copy needed files into subdirectory of current basedata run
        for file_path in self.ROAD_NETWORK_DATA.iterdir():
            if file_path.is_file():
                shutil.copy2(file_path, self.NETWORK_DATA / file_path.name)
        self.road_network_file = self.ROAD_NETWORK_DATA / self.net_file_name
        self.log_id = 0
        self.road_network = sumolib.net.readNet(self.road_network_file)
        self.edge_ids = [edge._id for edge in self.road_network.getEdges()]
        self.input_request_file = _input_request_file
        # output folders and file names, set on the first iteration (see set_output_names())
        self.file_prefix = None
        self.run_name = None
        self.rw_output_dir = None
        self.input_copy_dir = None
        self.iteration_inputs = None
        self.iteration_inputs_hash = None

        self.verbose = True
        
        self.set_attributes_stations()

        self.cab = _cab # define basic paramaters of the cab vehicle
        self.cab_speed_model = self.parse_cab_speed_approx_model()
        self.charging_threshold = 500
        self.pro = _pro
        self.only_cabs = False
        if self.pro == None: 
            self.only_cabs = True
        self.only_platoon = False
        self.disable_tls = True
        self.initial_station = "pa_" + sorted(self.operations_area.charging_stations, 
                                        key = lambda station: 
                                        math.dist(
                                            [self.cab.init_location_lat, self.cab.init_location_lon],
                                            [station.location_lat, station.location_lon])
                                        )[0].id
        # convert cab values (utils.read_base_json already parses them into datetimes)
        if isinstance(self.cab.schedule_start_time, str):
            self.cab.schedule_start_time = datetime.fromisoformat(self.cab.schedule_start_time)
        if isinstance(self.cab.schedule_end_time, str):
            self.cab.schedule_end_time = datetime.fromisoformat(self.cab.schedule_end_time)
        # get todays date
        self.todays_date = self.cab.schedule_start_time.date()
        self.simlation_start = 0
        self.simulation_end = int(utilities.convert_datetime_to_simulation_time(self.cab.schedule_end_time, 
                                                                            self.todays_date,
                                                                            self.cab.schedule_start_time))

        self.vehicle_fleet = None
        self.demand_scenario = None
        self.taxis = None
        self.pros = None 
        self.reservations = None
        self.reservation_history = []
        self.ramp_up_time = None
        self.run_time_assignment = None
        self.run_time_traci = None
        self.run_time_dict = {}

        self.create_net_files()



    def parse_to_poly_string(self):
        string = ""
        for vertix in self.operations_area.operational_area.vertices:
            string = string + f" {vertix[1]} {vertix[0]}"
        return string


    def create_output_subdir(self): 
        sub_dirs = ["console_output", "event_log", "general_kpi", "platoon_results", "runtime", "tripinfos"]

        for sub in sub_dirs:
            (self.OUTPUT_DIR / sub).mkdir(exist_ok=True)

    def create_and_preprocess_net_file(self, poly):
        """
        Takes poly path as an argument in the form:
       "min_lat, min_lon
        min_lat, max_lon
        max_lat, max_lon
        max_lat, min_lon
        min_lat, min_lon"
        """

        # check if file already exists with the identifier
        if not (self.ROAD_NETWORK_DATA / self.net_file_name).is_file():
            print("Creating Net file...")
            overpass_url = "https://overpass-api.de/api/interpreter"
            query = f"""
            [out:xml][timeout:25];
            (
              way(poly:"{poly}");
              >;
            );
            out body;
            """

            response = requests.post(overpass_url, data={"data": query})
            # Overpass returning an error page (rate limit, rejected query, outage, ...) is not
            # XML; writing it to the .osm.xml file unchecked turns into a netconvert parse error,
            # then a misleading FileNotFoundError three steps downstream once the net file this
            # code expects netconvert to have produced never appears.
            response.raise_for_status()
            if not response.content.lstrip().startswith(b"<"):
                raise RuntimeError(
                    f"Overpass did not return XML for {overpass_url}: {response.content[:200]!r}"
                )

            osm_file = self.ROAD_NETWORK_DATA / f"{self.operations_area.operational_area.description}.osm.xml"
            with open(osm_file, "wb") as f:
                f.write(response.content)
            subprocess.run([sys.executable, self.ROAD_NETWORK_DATA / "osmBuild.py",
                            "--osm-file", osm_file,
                            "-d", self.ROAD_NETWORK_DATA,
                            "--vehicle-classes", "nemo",
                            "-z"])

            built_net_file = self.ROAD_NETWORK_DATA / "osm.net.xml.gz"
            if not built_net_file.is_file():
                raise RuntimeError(
                    f"osmBuild.py did not produce {built_net_file}; see its own console output "
                    "above for the underlying netconvert/polyconvert failure"
                )
            # make the net file identifiable
            os.rename(built_net_file, self.ROAD_NETWORK_DATA / self.net_file_name)
            self.compute_strongly_connected()
        else:
            print("Net File already exists for given identifier. Skipping net file creation...")

        # change the value in the sumo cfg
        cfg_path = self.ROAD_NETWORK_DATA / "sumo.sumocfg"

        # Parse XML
        tree = ET.parse(cfg_path)
        root = tree.getroot()

        # Find the net-file element and update its value
        for net in root.iter("net-file"):
            net.set("value", self.net_file_name)

        # Write back to file
        tree.write(cfg_path, encoding="utf-8", xml_declaration=True)

    def compute_strongly_connected(self):

        # --- CONFIGURATION ---
        input_net = self.ROAD_NETWORK_DATA / self.net_file_name  
        output_net = self.ROAD_NETWORK_DATA / self.net_file_name 

        # --- STEP 1: Load network ---
        print("Loading network...")
        net = sumolib.net.readNet(input_net)

        # --- STEP 2: Build directed graph ---
        print("Building directed graph...")
        G = nx.DiGraph()
        for edge in net.getEdges():
            if edge.isSpecial():  # skip internal, walking, etc., if desired
                continue
            from_id = edge.getFromNode().getID()
            to_id = edge.getToNode().getID()
            G.add_edge(from_id, to_id)

        # --- STEP 3: Find largest strongly connected component ---
        print("Finding largest strongly connected component...")
        largest_scc_nodes = max(nx.strongly_connected_components(G), key=len)
        largest_scc_nodes = set(largest_scc_nodes)
        print(f"Largest SCC has {len(largest_scc_nodes)} nodes")

        # --- STEP 4: Keep only edges in SCC ---
        edges_to_keep = []
        for edge in net.getEdges():
            from_id = edge.getFromNode().getID()
            to_id = edge.getToNode().getID()
            if from_id in largest_scc_nodes and to_id in largest_scc_nodes:
                edges_to_keep.append(edge.getID())

        print(f"Keeping {len(edges_to_keep)} edges out of {len(net.getEdges())}")

        # --- STEP 5: Export reduced network ---
        print("Exporting reduced network...")
        edges_str = ",".join(edges_to_keep)
        import subprocess

        subprocess.run([
            "netconvert",
            "-s", input_net,  # optionally replace with a config, or use -n input_net
            "--keep-edges.explicit", edges_str,
            "-o", output_net
        ])

        print(f"Reduced network saved to {output_net}")

    def set_attributes_stations(self):
        # set location attributes for stations for sumo
        for ps in self.operations_area.parking_locations:
            ps.x, ps.y = self.road_network.convertLonLat2XY(ps.location_lon, ps.location_lat)
            ps.lane, ps.offset = sumo_utilities.get_lane_and_offset((ps.x, ps.y), 
                                                                     self.road_network, #
                                                                         lat_lon = False)

        for cs in self.operations_area.charging_stations:
            cs.x, cs.y = self.road_network.convertLonLat2XY(cs.location_lon, cs.location_lat)
            cs.lane, cs.offset = sumo_utilities.get_lane_and_offset((cs.x, cs.y), 
                                                                     self.road_network, 
                                                                     lat_lon = False)
        for chain_loc in self.operations_area.chaining_locations:
            chain_loc.x_start, chain_loc.y_start = self.road_network.convertLonLat2XY(chain_loc.start_location_lon, chain_loc.start_location_lat)
            chain_loc.x_end, chain_loc.y_end = self.road_network.convertLonLat2XY(chain_loc.end_location_lon, chain_loc.end_location_lat)
            chain_loc.lane, chain_loc.offset = sumo_utilities.get_car_lane_and_offset((chain_loc.x_start, chain_loc.y_start), 
                                                                     self.road_network, 
                                                                     lat_lon = False)
            chain_loc.length = numpy.linalg.norm([chain_loc.x_start - chain_loc.x_end, chain_loc.y_start - chain_loc.y_end])

    def create_net_files(self):
        # use operations_area to create needed files for sumo _simulation
        operational_vertices = self.operations_area.operational_area
        charging_stations = self.operations_area.charging_stations
        parking_locations = self.operations_area.parking_locations
        chaining_locations = self.operations_area.chaining_locations

        # add chaining locations in the future
        self.create_stations_file(charging_stations, parking_locations, chaining_locations)
        self.create_route_file(self.cab, self.pro)


    def create_stations_file(self, charging_stations: list[ChargingStation], 
                                   parking_locations: list[ParkingLocation], 
                                   chaining_locations:list[ChainingLocation]):
        root = ET.Element("additional")
        parking_station_capacity = 200
        for ps in parking_locations:
            parking_station = ET.SubElement(root, "parkingArea")
            parking_station.set("id", ps.id)
            lane = ps.lane
            edgeID = lane.getID()[:-2]
            edge = self.road_network.getEdge(edgeID)
            length = edge.getLength()
            start_pos = length/2
            parking_station_length = 10
            end_pos = start_pos + parking_station_length  
            parking_station.set("lane", str(lane.getID())) 
            parking_station.set("startPos", str(start_pos))
            parking_station.set("endPos", str(end_pos)) 
            parking_station.set("roadsideCapacity", str(parking_station_capacity))


        for cs in charging_stations: 

            position = (cs.location_lon, cs.location_lat)
            lane = cs.lane
            edgeID = lane.getID()[:-2]
            edge = self.road_network.getEdge(edgeID)
            length = edge.getLength()
            start_pos = length/2
            charging_station_length = 10
            end_pos = start_pos + charging_station_length
            # Create the parking station which belongs to the charging station
            parking_station = ET.SubElement(root, "parkingArea")
            parking_station.set("id", "pa_" + str(cs.id))
            parking_station.set("lane", str(lane.getID()))
            parking_station.set("startPos", str(start_pos))
            parking_station.set("endPos", str(end_pos))
            parking_station.set("roadsideCapacity", str(parking_station_capacity))


            charging_station = ET.SubElement(root, "chargingStation")
            # values which are shared across all charging stations
            charging_station.set("chargeDelay", "0")
            charging_station.set("chargeInTransit", "0")
            charging_station.set("efficiency", "0.95")
            charging_station.set("chargeType", "electric")

            # Individual Charging Station Parameters
            charging_station.set("id", str(cs.id))
            charging_station.set("parkingArea", "pa_" + str(cs.id))
            charging_station.set("power", str(cs.max_supply))
            charging_station.set("lane", str(lane.getID()))
            charging_station.set("startPos", str(start_pos))
            charging_station.set("endPos", str(end_pos))

        for chain_loc in chaining_locations:
            parking_station = ET.SubElement(root, "parkingArea")
            parking_station.set("id", chain_loc.id)
            lane = chain_loc.lane
            start_pos = chain_loc.offset
            end_pos = start_pos + chain_loc.length
            parking_station.set("lane", str(lane.getID())) 
            parking_station.set("startPos", str(start_pos))
            parking_station.set("endPos", str(end_pos)) 
            parking_station.set("roadsideCapacity", str(parking_station_capacity))

        # Convert the tree to a string
        xml_string = ET.tostring(root, encoding="utf-8").decode()

        # Pretty-print the XML
        dom = xml.dom.minidom.parseString(xml_string)
        pretty_xml = dom.toprettyxml(indent="  ")

        # Save to a file
        with open(self.NETWORK_DATA / "charging_stations.add.xml", "w", encoding="utf-8") as f:
            f.write(pretty_xml)


    def create_route_file(self, cab: Cab, pro: Pro):
        root = ET.Element("routes")
        root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
        root.set("xsi:noNamespaceSchemaLocation", "http://sumo.dlr.de/xsd/routes_file.xsd")
        v_type_cab = ET.SubElement(root, "vType")

        # create the vtype for the cabs
        v_type_cab.set("id", "taxi")
        v_type_cab.set("vClass", "taxi")

        # adjust the max speed in the future
        v_type_cab.set("maxSpeed", str(cab.max_speed))

        # sublane stuff
        v_type_cab.set("width", "1.0")
        v_type_cab.set("latAlignment", "right")
        v_type_cab.set("lcSublane", "1")
        v_type_cab.set("lcStrategic", "0")
        v_type_cab.set("lcCooperative", "0")
        v_type_cab.set("lcSpeedGain", "0")
        v_type_cab.set("lcKeepRight", "0.5")
        v_type_cab.set("minGapLat", "0.1")
        params_cab = {"mass": "800", 
                  "device.battery.capacity": cab.total_energy_capacity, 
                  "has.battery.device": "true", 
                  "device.battery.chargeLevel": cab.init_energy_capacity, 
                  "recuperationEfficiency" : 0,
                  "maximumPower": 17000,
                  }

        for param in params_cab:
            childElement = ET.SubElement(v_type_cab, "param")
            childElement.set("key", param)
            childElement.set("value", str(params_cab[param]))

        route = ET.SubElement(root, "route")
        route.set("id", "route_taxi")
        position = (cab.init_location_lon, cab.init_location_lat)
        edge = sumo_utilities.get_lane_and_offset(position, self.road_network)[0].getID()[:-2]
        route.set("edges", str(edge))

        if not self.only_cabs:
            # create the vtype for the pro
            v_type_pro = ET.SubElement(root, "vType")

            v_type_pro.set("id", "pro")
            v_type_pro.set("vClass", "bus")

            # adjust the max speed in the future
            v_type_pro.set("maxSpeed", str(pro.max_speed))

            # sublane stuff
            v_type_pro.set("length", "6")
            v_type_pro.set("color", "1,0,0")
            v_type_pro.set("width", "1.0")
            v_type_pro.set("latAlignment", "left")
            v_type_pro.set("lcSublane", "1")
            v_type_pro.set("lcStrategic", "255")
            v_type_pro.set("lcCooperative", "0.5")
            v_type_pro.set("lcSpeedGain", "1.0")
            v_type_pro.set("lcKeepRight", "0.2")
            v_type_pro.set("minGapLat", "0.2")
            v_type_pro.set("lcPushy", "1")
            v_type_pro.set("lcAssertive", "1.0")

            params_pro = {"mass": "3000", 
                      "device.battery.capacity": pro.total_energy_capacity, 
                      "has.battery.device": "true", 
                      "device.battery.chargeLevel": pro.init_energy_capacity, 
                      "recuperationEfficiency" : 0,
                      "maximumPower": 17000,
                      }
            for param in params_pro:
                childElement = ET.SubElement(v_type_pro, "param")
                childElement.set("key", param)
                childElement.set("value", str(params_pro[param]))



            route = ET.SubElement(root, "route")
            route.set("id", "route_pro")
            position_pro = (pro.init_location_lon, pro.init_location_lat)
            edge_pro = sumo_utilities.get_lane_and_offset(position_pro, self.road_network)[0].getID()[:-2]
            route.set("edges", str(edge_pro))

        # Convert the tree to a string
        xml_string = ET.tostring(root, encoding="utf-8").decode()

        # Pretty-print the XML
        dom = xml.dom.minidom.parseString(xml_string)
        pretty_xml = dom.toprettyxml(indent="  ")

        # Save to a file
        with open(self.NETWORK_DATA / "instance.rou.xml", "w", encoding="utf-8") as f:
            f.write(pretty_xml)

    def parse_cab_speed_approx_model(self):
        with open(self.BASE_DIR / "hill_model_params.json", "r") as f:
            params = json.load(f)

        A = params["A"]
        K = params["K"]
        n = params["n"]
        d = params["d"]
        threshold1 = params["threshold1"]
        threshold2 = params["threshold2"]
        constant_avg_speed  = params["constant_avg_speed"]
        constant_avg_speed2 = params["constant_avg_speed2"]

        return lambda x: d + A * (x**n) / (K**n + x**n) if x >= threshold2 else constant_avg_speed2 if x >= threshold1 else constant_avg_speed 

    def linear_model(x, a = 1/5000, b = 2.33333):
        return lambda x : a * x + b

    def parse_requests_to_reservations(self, request_list : list[Request]) -> list[Reservation]: 
        reservation_list = []
        for request in request_list:
            converted_register_time = utilities.convert_datetime_to_simulation_time(request.register_time, 
                                                                                    self.todays_date,
                                                                                    self.cab.schedule_start_time, 
                                                                                    self.ramp_up_time, 
                                                                                    simulation_end = self.simulation_end)
            
            converted_tw_type = "PU" if request.tw_type else "DO"

            converted_tw_lower = utilities.convert_datetime_to_simulation_time(request.tw_lower, 
                                                                                self.todays_date,
                                                                                self.cab.schedule_start_time, 
                                                                                self.ramp_up_time, 
                                                                                cut_at_ramp_up = False, 
                                                                                simulation_end = self.simulation_end)

     
            converted_tw_upper = utilities.convert_datetime_to_simulation_time(request.tw_upper, 
                                                                            self.todays_date,
                                                                            self.cab.schedule_start_time, 
                                                                            self.ramp_up_time, 
                                                                            cut_at_ramp_up = False, 
                                                                            simulation_end = self.simulation_end)
            
            converted_start_pos = self.road_network.convertLonLat2XY(request.pu_lon, request.pu_lat)
            converted_end_pos = self.road_network.convertLonLat2XY(request.do_lon, request.do_lat)#


            reservation_object = Reservation(request.id, converted_start_pos, converted_end_pos, 
                converted_register_time, converted_tw_type, converted_tw_lower, converted_tw_upper, request.tol_lower, 
                request.tol_upper, request.num_persons, request.need_ramp, request.ride_sharing)

            valid = reservation_object.calculate_edges_and_offset(self.road_network)

            if converted_register_time >= self.simulation_end:
                reservation_object.customReject = True
                self.reservation_history.append(reservation_object)
            else:

                if valid:
                    reservation_list.append(reservation_object)
                else: 
                    self.reject_invalid_location.append(reservation_object)

        return reservation_list

    def parse_cabs_to_taxis(self, cab_list : list[Cab]) -> list[Taxi]:
        taxi_list = []

        for cab in cab_list:
            converted_position = self.road_network.convertLonLat2XY(cab.init_location_lon, cab.init_location_lat)
            taxi_object = Taxi(f"taxi {cab.id}", "idle", converted_position, cab.init_energy_capacity, cab.total_energy_capacity,
                              cab.default_energy_consumption_per_m, cab.max_speed, cab.has_ramp, cab.seats, maxToAverage = 2/3, cabSpeedModel = self.cab_speed_model)
            taxi_object.calculate_edge(self.road_network)
            taxi_list.append(taxi_object)

        return taxi_list

    def parse_pros_to_procabs(self, pro_list : list[Pro]) -> list[ProCab]:
        procab_list = []

        for index, pro in enumerate(pro_list):
            maxToAverage = 0.6
            pro_object = ProCab(f'Pro{pro.id}', "idle", "tbd", pro.max_speed, pro.max_cabs,  
                                pro.max_power_supply, pro.default_energy_consumption_per_m, pro.init_energy_capacity, 
                                pro.total_energy_capacity, maxToAverage = maxToAverage)
            pro_object.state = "attachingStop"
            procab_list.append(pro_object)

        return procab_list


    def parse_chain_routes(self):
        for chain_route in self.operations_area.pro_routes_and_trips.chain_routes: 
            # reset ChainingLocation attribute from string to object
            chain_route.start_location = next((x for x in self.operations_area.chaining_locations if x.id == chain_route.start_location), None)
            chain_route.end_location   = next((x for x in self.operations_area.chaining_locations  if x.id == chain_route.end_location), None)

    def parse_chain_route_trips(self):

        for chain_route_trip in self.operations_area.pro_routes_and_trips.chain_route_trips:
            # reset ChainRoute attribute from string to object
            if self.log_id == 0:
                chain_route_trip.chain_route = next((x for x in self.operations_area.pro_routes_and_trips.chain_routes if x.id == chain_route_trip.chain_route), None)

            # the full generated timetable covers every Pro slot a line could need; this
            # iteration's fleet usually has fewer Pros active than that, so a trip whose
            # Pro is not part of self.pros right now is not scheduled this iteration.
            pro_schedule_id = chain_route_trip.pro_schedule if isinstance(chain_route_trip.pro_schedule, str) else chain_route_trip.pro_schedule.id
            pro = next((x for x in self.pros if x.id == pro_schedule_id), None)
            if pro is None:
                continue
            chain_route_trip.pro_schedule = pro

            start_location = chain_route_trip.chain_route.start_location
            end_location = chain_route_trip.chain_route.end_location
            if chain_route_trip.chain_route not in pro.chain_routes:
                pro.chain_routes.append(chain_route_trip.chain_route)
            start_time = utilities.convert_datetime_to_simulation_time(datetime.fromisoformat(chain_route_trip.departure), 
                                                                            self.todays_date,
                                                                            self.cab.schedule_start_time, 
                                                                            self.ramp_up_time)
                
            if len(pro.schedule) == 0: 
                detachApproach = ScheduleEntry(chain_route_trip.id, None, 'detachApproach', 
                                           (start_location.x_start, start_location.y_start), 
                                           (end_location.x_start, end_location.y_start), 
                                           start_location.lane.getID()[:-2], end_location.lane.getID()[:-2],
                                           start_time, pro.energy, vehicle = pro, parkingID = end_location.id,
                                           platoonOffset = end_location.offset)
                pro.position = detachApproach.startPos
                pro.calculate_edge(self.road_network)
                pro.start_location = start_location.id
            else:
                detachApproach = ScheduleEntry(chain_route_trip.id, None, 'detachApproach', 
                                           (start_location.x_start, start_location.y_start), 
                                           (end_location.x_start, end_location.y_start), 
                                           start_location.lane.getID()[:-2], end_location.lane.getID()[:-2],
                                           start_time, attachApproach.endEnergy, vehicle = pro, parkingID = end_location.id,
                                           platoonOffset = end_location.offset)

            detachApproach.set_custom_travel_time(self)

            nextChaining = next((x for x in self.operations_area.chaining_locations if x.id == end_location.id.split("_")[0]+ "_Chain"), None)
            attachApproach = ScheduleEntry(f"relocate_to_{nextChaining.id}", None, 'attachApproach', 
                                       (end_location.x_start, end_location.y_start), 
                                       (nextChaining.x_start, nextChaining.y_start), 
                                       end_location.lane.getID()[:-2], nextChaining.lane.getID()[:-2],
                                       detachApproach.endT + 1, detachApproach.endEnergy, vehicle = pro, parkingID = nextChaining.id,
                                       platoonOffset = nextChaining.offset)
            pro.reservedCabIDs[detachApproach.id]  = []

            bisect.insort(pro.schedule, detachApproach, key = lambda x: x.startT)
            if attachApproach.endT < self.simulation_end:   
                bisect.insort(pro.schedule, attachApproach, key = lambda x: x.startT)

    def optimize(self, far: FleetAndRequests, log_id: int=-1, log_path: str=".", debug: bool=False):
        # Simulate the Scenario which is defined by the FleetAndRequests object
        self.initialize_iteration(far, log_id)
        if self.check_warm_start():
            print("[SUMO] Loading existing results for given configuration")
            # rebuilt from the stored event log, so they always have the current output format
            self.create_rw_output_files()
            self.get_all_kpis()
        else:
            # the stored inputs of this iteration no longer describe its output files once those are rewritten
            paths = self.rw_paths(self.log_id)
            paths["input_base"].unlink(missing_ok=True)
            paths["input_req"].unlink(missing_ok=True)
            self.start_event_log()
            start = timeit.default_timer()
            simulation_succeeded = False
            try: 
                run(self)
                simulation_succeeded = True
                with open(self.OUTPUT_DIR / "console_output" /f"{self.run_name}_iter_{self.log_id}_SUCCESS.txt", 'w') as file:
                     file.write("Simulation ran successfully\n")

            except Exception as e:
                with open(self.OUTPUT_DIR / "console_output" /f"{self.run_name}_iter_{self.log_id}_FAIL.txt", 'w') as file:
                    file.write(f"Simulation failed with the following exception:\n{e}\n\n{traceback.format_exc()}")

            stop = timeit.default_timer()
            self.track_run_time(stop - start)
            # a failed run has no complete SUMO output to aggregate; aggregating anyway replaces
            # this exception with a second, unrelated one from reading partial or missing output
            # files, which hides what actually failed
            if simulation_succeeded:
                self.result_aggregation()
                # reset reservation history after completion
                self.reservation_history = []
                # create rw_output files
                self.create_rw_output_files()
                # stored last, so a warm start only finds inputs next to complete results of a successful run
                self.write_input_files()

    def check_warm_start(self):
        """
        True if an earlier run simulated this iteration with exactly the same inputs (fleet, requests,
        operational area) and left its event log, so the simulation itself can be skipped.
        """
        paths = self.rw_paths(self.log_id)
        if not (paths["input_base"].is_file() and paths["input_req"].is_file() and self.event_based_log_file.is_file()):
            return False
        # the event log is named after the request file only, a run with other BaseData of the same area can have replaced it
        with open(self.event_based_log_file, "r") as f:
            header = [f.readline() for _ in range(3)]
        if f"Inputs {self.iteration_inputs_hash}\n" not in header:
            return False
        request_data, base_data = self.iteration_inputs
        return (load_json_file(paths["input_req"]) == json.loads(json.dumps(request_data))
                and load_json_file(paths["input_base"]) == json.loads(json.dumps(base_data)))

    def get_all_kpis(self):
        far = self.far
        cab_data = load_json_file(self.rw_paths(self.log_id)["output_cab"])
        event_df, reservation_df, num_cabs, num_pros = parse_eventlog_to_dfs(file_name=self.event_based_log_file)
        event_df_cabs = event_df[event_df["vehicle"].str.startswith("taxi")]
        for cab in far.vehicle_fleet.cabs:
            self.get_cab_kpis(cab, cab_data)
        for req in far.demand_scenario.requests:
            self.get_req_kpis(req, event_df_cabs, reservation_df)
        
        return 0

    def rw_paths(self, log_id):
        """Output files of iteration log_id, and SUMO's copies of the inputs it was simulated with."""
        return {
            "output_cab": self.rw_output_dir / f"{self.file_prefix}_output_cab_{log_id}.json",
            "output_pro": self.rw_output_dir / f"{self.file_prefix}_output_pro_{log_id}.json",
            "output_sim": self.rw_output_dir / f"{self.file_prefix}_output_sim_{log_id}.json",
            "input_base": self.input_copy_dir / f"{self.file_prefix}_input_base_file_{log_id}.json",
            "input_req": self.input_copy_dir / f"{self.file_prefix}_input_req_file_{log_id}.json",
        }

    def set_output_names(self, demand_scenario):
        """
        Decide where this run's output files go and how they are named.

        The rw-style interface files (what the dashboard reads) go under BASE_DIR/output, in
        self.output_folder if the caller gave one (a dashboard job), otherwise in a folder named
        after the request file. SUMO's own native per-run artifacts (event logs, tripinfos, ...)
        stay under OUTPUT_DIR (output_<area>), unrelated to that override.
        """
        self.run_name = self.output_folder or self.input_request_file
        self.file_prefix = self.scenario_prefix(demand_scenario)
        self.rw_output_dir = self.BASE_DIR / "output" / self.run_name
        self.input_copy_dir = self.OUTPUT_DIR / "rw_output" / self.run_name
        self.rw_output_dir.mkdir(parents=True, exist_ok=True)
        self.input_copy_dir.mkdir(parents=True, exist_ok=True)
        print(f"[SUMO] output files: {self.rw_output_dir}/{self.file_prefix}_output_<cab|pro|sim>_<iteration>.json")

    def scenario_prefix(self, demand_scenario):
        """
        <area>_<N>cs_<N>lines_<N>bc_<N>cp_<N>rq_<N>tw, by the rules the dashboard names its job files
        with (frontend/app.py _build_auto_naming_base()).
        """
        def kilo(value):
            return int(round(value / 1000)) if value >= 1000 else int(round(value))

        def to_datetime(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value

        short_handle = str(self.operations_area.operational_area.description or "").strip().lower()
        area = re.sub(r"[^a-z0-9]+", "", re.split(r"[_\-\s]+", short_handle, maxsplit=1)[0])
        if not area:
            plate = self.cab.licensePlate
            plate = str(plate[0] if isinstance(plate, (list, tuple)) and plate else plate)
            match = re.match(r"^([A-Za-z]+)-", plate)
            area = match.group(1).lower() if match else "pb"
        area = {"hoexter": "hx", "paderborn": "pb"}.get(area, area)

        stations = len(self.operations_area.charging_stations)
        lines = int(len(self.operations_area.pro_routes_and_trips.chain_routes or []) / 2)
        battery = kilo(float(self.cab.total_energy_capacity or 0))
        power = kilo(max((float(station.max_supply) for station in self.operations_area.charging_stations), default=0))
        windows = set()
        for request in demand_scenario.requests:
            lower, upper = to_datetime(request.tw_lower), to_datetime(request.tw_upper)
            if isinstance(lower, datetime) and isinstance(upper, datetime) and upper > lower:
                windows.add(int(round((upper - lower).total_seconds() / 60)))
        time_window = next(iter(windows)) if len(windows) == 1 else 0

        return f"{area}_{stations}cs_{lines}lines_{battery}bc_{power}cp_{len(demand_scenario.requests)}rq_{time_window}tw"


    def get_cab_kpis(self, cab, cab_data):
        
        #self.reset_cumulative_values()
        cum_dist = 0
        cum_driving = 0
        cum_service = 0
        cum_energy = 0
        num_pu = 0
        num_do = 0

        # match cab entry by vehicle id from cab object
        #cab_id = str(cab.id)  # <-- change to cab.label or cab.id if that's what you store
        cab_id = int(cab.id)
        
        #matches = [
            #entry for entry in cab_data
            #if str(entry.get("vehicle", {}).get("id")) == cab_id
        #]
        matches = []
        for entry in cab_data:
            vid = entry.get("vehicle", {}).get("id", "")
            # extract trailing number from strings like "Cab1", "Cab01", "CAB-12"
            num = ""
            for ch in reversed(str(vid)):
                if ch.isdigit():
                    num = ch + num
                else:
                    break
            if num and int(num) == cab_id:
                matches.append(entry)
        
        #if len(matches) != 1:
            #print("[rw-api] Error: Multiple or no matching cab found in output_cab.json for", cab_id)
            #import sys
            #sys.exit(-1)
            #return
        
        if len(matches) == 0:
            print("[SUMO] Warning: no matching cab found in output_cab.json for", cab_id)
            return

        if len(matches) > 1:
            print("[SUMO] Warning: multiple matching cabs found in output_cab.json for", cab_id, "count=", len(matches))
            return

        cab_entry = matches[0]

        for stop in cab_entry.get("tripStops", []):
            cum_dist += stop.get("distance", 0)
            cum_driving += stop.get("drivingTime", 0)
            cum_service += stop.get("duration", 0)
            cum_energy += stop.get("consumedEnergy", 0)

            if stop.get("stopType") == "Pickup":
                num_pu += 1
            elif stop.get("stopType") == "Dropoff":
                num_do += 1
            #if stop["stopType"] == "Charging":
                #print("Charging")

        cab.sim_cum_distance = cum_dist

        cab.sim_cum_driving_time = cum_driving
        cab.sim_cum_service_time = cum_service
        cab.sim_cum_energy_cons = cum_energy
        if num_pu == num_do:
            cab.sim_num_requests = num_pu

        return

    def get_req_kpis(self, request: Request, event_df, reservation_df):
        
        reservation = reservation_df[reservation_df["id"] == f"Reservation {request.id}"]
        events = event_df[event_df["id"] == f"Reservation {request.id}"]
        request.sim_invalid = 0
        if "assigned" in reservation["status"].unique():
            request.sim_custom_reject = False
            request.vehicle           = f"Cab {reservation[reservation['status'] == 'assigned']['vehicle'].iloc[0].split()[1]}"
            # an assigned request can still be on its way when the simulation ends
            pickup_steps = events[(events["type_entry"] == "customerApproach") & (events["start_or_end"] == "completes")]["step"]
            dropoff_steps = events[(events["type_entry"] == "customerTransport") & (events["start_or_end"] == "completes")]["step"]
            request.sim_prop_pu_time     = utilities.convert_simulation_time_to_datetime(int(pickup_steps.iloc[0]),
                                                                                         self.cab.schedule_start_time, 
                                                                                         self.ramp_up_time, 
                                                                                         return_datetime = True
                                                                                        ) if len(pickup_steps) else None

            request.sim_pu_time          = request.sim_prop_pu_time  
            request.sim_prop_do_time     = utilities.convert_simulation_time_to_datetime(int(dropoff_steps.iloc[0]),
                                                                                         self.cab.schedule_start_time, 
                                                                                         self.ramp_up_time, 
                                                                                         return_datetime = True
                                                                                        ) if len(dropoff_steps) else None
            request.sim_do_time          = request.sim_prop_do_time
            served_time = request.sim_prop_pu_time if request.tw_type else request.sim_prop_do_time
            # 0 like Reservation.waitingTime of a reservation not picked up yet
            request.sim_wait_time_prop = served_time - request.tw_lower if served_time is not None else 0
        else:
            request.sim_custom_reject = True
            request.vehicle           = None
            request.sim_prop_pu_time     = None
            request.sim_pu_time          = None
            request.sim_prop_do_time     = None
            request.sim_do_time          = None
            request.sim_wait_time_prop = None

    def track_run_time(self, total_time):
        print(f'Run Time for iteration  {self.log_id}: ', int(total_time)) 
        print("Run Time needed for assignment: ", int(self.run_time_assignment))
        print("Run Time needed for traci: ", int(self.run_time_traci))#
        print("Number of Platoon Trips: ", self.num_platoon_trips)
        self.run_time_dict[self.log_id] = [total_time, self.run_time_assignment, self.run_time_traci]

    def initialize_iteration(self, far, log_id):
        start = timeit.default_timer()

        if self.run_name is None:
            self.set_output_names(far.demand_scenario)
        # written from scratch when this iteration is simulated, see start_event_log()
        self.event_based_log_file = self.OUTPUT_DIR / "event_log" /f"eventlog_{self.run_name}_iter_{log_id}.txt"

        self.log_id = log_id
        self.total_cs = 0
        self.invalid_cs = 0
        self.far = far 
        self.vehicle_fleet = far.vehicle_fleet
        self.demand_scenario = far.demand_scenario
        self.ramp_up_time = (len(self.vehicle_fleet.cabs) + len(self.vehicle_fleet.pros)) * 5 + 20
        self.simulation_start = self.simlation_start + self.ramp_up_time 
        self.simulation_end = self.simulation_end + self.ramp_up_time
        self.taxis = self.parse_cabs_to_taxis(self.vehicle_fleet.cabs)
        if not self.only_cabs:
            self.pros = self.parse_pros_to_procabs(self.vehicle_fleet.pros)
        else: 
            self.pros = []
        if self.log_id == 0 and not self.only_cabs: 
            self.parse_chain_routes()
        self.reject_invalid_location = []
        self.reservations = self.parse_requests_to_reservations(self.demand_scenario.requests)
        self.initialize_result_metrics()
        # the inputs of this iteration, compared with the stored ones for a warm start
        self.iteration_inputs = (far.write_api_req_file(), far.write_api_base_file(self.operations_area_unchanged))
        self.iteration_inputs_hash = hashlib.sha1(json.dumps(self.iteration_inputs).encode()).hexdigest()
        stop = timeit.default_timer()
        print('Time needed for Initialisation', int(stop - start)) 

    def start_event_log(self):
        """Start this iteration's event log from scratch, so a rerun never appends to an earlier run's log."""
        cabs = self.vehicle_fleet.cabs
        pros = self.vehicle_fleet.pros
        with open(self.event_based_log_file, "w") as f:
            f.write(f"Initializing Simulation with {len(cabs)} cabs and {len(pros)} pros \n")
            # the vehicle ids, which have gaps once fleet planning removed vehicles (see validation/turn_eventlog_into_rw_output.py)
            f.write(f"Fleet cabs {' '.join(str(cab.id) for cab in cabs)} pros {' '.join(str(pro.id) for pro in pros)}\n")
            # which inputs this log belongs to, checked by check_warm_start()
            f.write(f"Inputs {self.iteration_inputs_hash}\n")

    def write_input_files(self):
        """Store the inputs this iteration was simulated with next to its results, see check_warm_start()."""
        request_data, base_data = self.iteration_inputs
        paths = self.rw_paths(self.log_id)

        with open(paths["input_base"], "w", encoding="utf-8") as f:
            json.dump(base_data, f, indent=4)

        with open(paths["input_req"], "w", encoding="utf-8") as f:
            json.dump(request_data, f, indent=4)

    def initialize_result_metrics(self):

        self.entry_history = []
        self.run_time_assignment = 0
        self.run_time_traci = 0 
        self.num_platoon_trips = {chain_route.id:0 for chain_route in self.operations_area.pro_routes_and_trips.chain_routes}
        self.platoon_share = 0.0
        self.empty_pro_tours = 0
        self.deleted_cs = []
        self.pro_prebooking_times = []
        self.platoon_reservations = []
        self.platoon_lengths = []
        self.detour_distances = [] 
        self.saved_energies = []
        self.relative_saved_energies = []
        self.detour_durations = []


    def result_aggregation(self): 
        file_name_trip = self.OUTPUT_DIR / "tripinfos" /f"tripinfo_{self.run_name}_iter_{self.log_id}.xml"
        cab_file_name = self.OUTPUT_DIR / "cab_results" / f"cab_info_{self.run_name}_iter_{self.log_id}.json"
        run_time_file_name = self.OUTPUT_DIR / "runtime" / f"run_time_{self.run_name}.csv"
        # distance_error_file_name = self.OUTPUT_DIR / f"distance_error_{self.run_name}.csv"
        request_file_name = self.OUTPUT_DIR / "request_results" /f"request_info_{self.run_name}_iter_{self.log_id}.json"
        platoon_file_name = self.OUTPUT_DIR / "platoon_results" /f"platoon_info_{self.run_name}_iter_{self.log_id}.json"
        kpi_file_name = self.OUTPUT_DIR / "general_kpi" /f"kpi_{self.run_name}_iter_{self.log_id}.json"

        self.log_run_time(run_time_file_name)
        if not self.only_cabs:
            self.log_platoon_kpis(platoon_file_name)
        total_distance, total_energy_consumption = self.pass_cab_kpis_to_fleetplanning(file_name_trip)
        num_rejects = self.pass_request_kpis_to_fleetplanning()
        total_distance_archive = sum([entry.distance for entry in self.entry_history if entry.typeSchedule != "chargingStop"])
        total_travel_time = sum([entry.duration for entry in self.entry_history if entry.typeSchedule != "chargingStop"])
        passenger_entry_types = ["customerTransport", "platoonApproach", "platoonTransport"]
        passenger_travel_time = sum([entry.duration for entry in self.entry_history if entry.typeSchedule in passenger_entry_types])
        print("total archive distance :", total_distance_archive)
        # with open(distance_error_file_name, 'w') as csv_file:  
        #     writer = csv.writer(csv_file)
        #     header = ['Reservation', 'type_schedule', 'realised_distance', 'approx_distance', 'error']
        #     writer.writerow(header)
        #     for value in self.distance_errors:
        #        writer.writerow([value[0], value[1], value[2], value[3], value[4]])

        # log most important metrics
        with open(kpi_file_name, 'w') as f:
            json.dump(
                {
                "total_distance": total_distance,
                'total_energy_consumption': total_energy_consumption,
                "num_rejects": num_rejects,
                "total_travel_time": total_travel_time, 
                "passenger_travel_time": passenger_travel_time
                }, 
                f)

    def log_run_time(self, run_time_file_name):
        with open(run_time_file_name, 'w') as csv_file:  
            writer = csv.writer(csv_file)
            header = ['iteration', 'total_time', 'assignment_run_time', 'traci_run_time', 'sumo_remaining']
            writer.writerow(header)
            for key, value in self.run_time_dict.items():
               writer.writerow([key, value[0], value[1], value[2], value[0] - value[1] - value[2]])

    def log_platoon_kpis(self, platoon_file_name):

        with open(platoon_file_name, 'w') as f:
            json.dump(self.num_platoon_trips |
                {
                "deleted_cs": self.deleted_cs,
                "total_num_platoons": sum(self.num_platoon_trips.values()),
                "platoon_reservations": self.platoon_reservations,
                "platoon_share": self.platoon_share,
                "empty_pro_tours": self.empty_pro_tours,
                "average_platoon_length": round(sum(self.platoon_lengths) / len(self.platoon_lengths), 2) if self.platoon_lengths else 0, 
                "average_pro_prebooking_time":round(sum(self.pro_prebooking_times) / len(self.pro_prebooking_times) if self.pro_prebooking_times else 0, 2), 
                "average_energy_save":round(sum(self.saved_energies) / len(self.saved_energies) if self.saved_energies else 0, 2),
                "relative_saved_energies":round(sum(self.relative_saved_energies) / len(self.relative_saved_energies) if self.relative_saved_energies else 0, 2),
                "average_detour_distance":round(sum(self.detour_distances) / len(self.detour_distances) if self.detour_distances else 0, 2),
                "average_detour_duration":round(sum(self.detour_durations) / len(self.detour_durations) if self.detour_durations else 0, 2),  
                }, 
                f)

    def pass_cab_kpis_to_fleetplanning(self, file_name_trip):
        tripInfo = ET.parse(file_name_trip).getroot()
        vehicles = tripInfo.findall('tripinfo')

        for vehicle in vehicles: 
            taxi_id = vehicle.attrib['id'].split()
            if taxi_id[0] == "taxi":
                emissions = vehicle.find("emissions")
                cab = next((x for x in self.vehicle_fleet.cabs if x.id == int(taxi_id[1])), None)
                if not self.only_cabs:
                    cab.sim_cum_distance = cab.sim_cum_distance + float(vehicle.attrib['routeLength'])
                    cab.sim_cum_driving_time = cab.sim_cum_driving_time + (float(vehicle.attrib['duration']) - float(vehicle.attrib['stopTime']))
                    cab.sim_cum_energy_cons = float(emissions.attrib['electricity_abs']) + cab.sim_cum_energy_cons
                else:
                    cab.sim_cum_distance = float(vehicle.attrib['routeLength'])
                    cab.sim_cum_driving_time = (float(vehicle.attrib['duration']) - float(vehicle.attrib['stopTime']))
                    cab.sim_cum_energy_cons = float(emissions.attrib['electricity_abs'])
        
        total_distance = 0 
        total_energy_consumption = 0

        for cab in self.vehicle_fleet.cabs: 
            taxi = next((x for x in self.taxis if x.id == f"taxi {cab.id}"), None)
            cab.sim_num_requests = taxi.num_requests
            cab.sim_cum_service_time = cab.sim_num_requests * (taxi.pickUpTime + taxi.dropOffTime)
            if not taxi.empty:
                cab.sim_cum_distance = cab.sim_cum_distance + taxi.platooningDistance
            else:
                cab.sim_cum_distance = 0
            total_distance = total_distance + cab.sim_cum_distance
            total_energy_consumption = total_energy_consumption + cab.sim_cum_energy_cons

        return total_distance, total_energy_consumption

    def pass_request_kpis_to_fleetplanning(self): 
        num_rejects = 0 
        for request in self.demand_scenario.requests:
            if request.id not in [reservation.id for reservation in self.reject_invalid_location]:
                reservation = next((x for x in self.reservation_history if int(x.id) == request.id), None)
                request.sim_invalid = 0 
                request.sim_custom_reject = reservation.customReject
                if reservation.customReject: 
                    num_rejects = num_rejects + 1
                request.sim_wait_time_prop = reservation.waitingTime 
                if reservation.vehicle != None:
                    request.vehicle = int(reservation.vehicle.id[-1])
                else:
                    request.vehicle = None
        return num_rejects

    def create_rw_output_files(self):
        """
        Write this iteration's <prefix>_output_{cab,pro,sim}_<iteration>.json, built from the event log
        and the simulated fleet (validation/turn_eventlog_into_rw_output.py).
        """
        cab_output, pro_output, sim_output = build_rw_outputs(self.event_based_log_file,
                                                              self.vehicle_fleet.cabs,
                                                              [] if self.only_cabs else self.vehicle_fleet.pros,
                                                              self.cab.schedule_start_time,
                                                              self.ramp_up_time,
                                                              [request.id for request in self.demand_scenario.requests],
                                                              [reservation.id for reservation in self.reject_invalid_location])
        paths = self.rw_paths(self.log_id)
        for key, output in (("output_cab", cab_output), ("output_pro", pro_output), ("output_sim", sim_output)):
            with open(paths[key], "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2)
