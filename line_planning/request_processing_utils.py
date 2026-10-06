#!/usr/bin/python3
import sys
import os
import json
import math
import requests
import copy
from collections import deque, Counter
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time
import numpy as np
import pandas as pd
import networkx as nx
import osmnx as ox
import polyline
import pyproj
from shapely.geometry import LineString, Point
from shapely.ops import transform, substring
from geopy.distance import geodesic
from sklearn.cluster import DBSCAN, HDBSCAN
from sklearn.neighbors import KernelDensity, sort_graph_by_row_values
from scipy.sparse import csr_matrix
from scipy.spatial import KDTree
import matplotlib.pyplot as plt
import plotly.express as px
import plotly.colors as pc
import plotly.graph_objects as go

from operational_vertices import OperationalVertices

# # we need to import python modules from the $SUMO_HOME/tools directory
# if 'SUMO_HOME' in os.environ:
#     tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
#     sys.path.append(tools)
# else:
#     sys.exit("please declare environment variable 'SUMO_HOME'")
    
# import sumolib
# from sumolib import checkBinary  # noqaa
# import traci

AVG_PRO_SPEED = 15  # Expected Pro speed for estimating OSMnx route duration, in m/s.
AVG_PASSENGER_CAB_SPEED = 7  # Expected Cab speed for estimating OSMnx route duration, in m/s.
CLOSE_CLUSTER_DISTANCE_THRESHOLD = 1000  # Distance below which clusters are considered close, in meters.
MINIMUM_CLUSTER_DISTANCE_FOR_LINE = 2000  # Minimum cluster separation for a candidate line, in meters.
MINIMUM_LINE_DISTANCE = 1500  # Minimum route length for a line, in meters.
LINE_USAGE_THRESHOLD = 2000  # Minimum direct-trip distance for considering a line, in meters.
MIN_CL_EDGE_LENGTH = 20  # Minimum edge length for placing a chaining location away from junctions.
OPERATIONAL_START_TIME = time(hour=6, minute=0)
OPERATIONAL_END_TIME = time(hour=22, minute=0)

###########################################################################


def _to_minute_of_day(value):
    """
    Accepts:
        - full datetime string (with or without timezone)
        - 'HH:MM' string
        - pandas Timestamp
    Returns:
        minute-of-day (int)
    """
    ts = pd.to_datetime(value)
    return int(ts.hour) * 60 + int(ts.minute)

###########################################################################
###########################################################################
###########################################################################

def find_nearest_free_node(graph, start_node, occupied_nodes):
    """
    Performs a breadth-first search (BFS) on the graph starting from `start_node` to find
    the nearest unoccupied node (i.e., not in `occupied_nodes`).

    Parameters:
        graph: networkx-like graph object.
        start_node: node id from which to start the search.
        occupied_nodes: set of node ids

    Returns:
        start_node: The nearest unoccupied node if found, otherwise returns the start_node as fallback.
    """
    visited = set()
    queue = deque([start_node])
    
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        
        # Check if current node is valid
        if current not in occupied_nodes:
            return current
        # Add neighbors to queue
        neighbors = list(graph.neighbors(current))  # or use successors if directed
        queue.extend(neighbors)

        return start_node  # fallback if none found
    
def find_nearest_node_at_least_x_away(graph, start_node, occupied_nodes, x):
    """
    Finds the nearest node to `start_node` that is at least `x` steps away from all nodes in `occupied_nodes`.

    This function performs two passes:
    1. It computes the shortest distance from each node in the graph to any occupied node.
    2. Then it performs a BFS from `start_node` to find the nearest node whose distance to all occupied nodes
       is at least `x`.

    Parameters:
        graph: A graph object supporting the `.neighbors(node)` method.
        start_node: The node from which to start the search.
        occupied_nodes: A collection of nodes considered "occupied."
        x: Minimum required distance (in graph steps) between the returned node and any occupied node.

    Returns:
        The nearest node to `start_node` that is at least `x` steps away from all occupied nodes.
        Returns `start_node` as a fallback if no such node is found.
    """
    min_distances = {}

    for node in occupied_nodes:
        visited = set()
        queue = deque([(node, 0)])

        while queue:
            current, dist = queue.popleft()
            if current in visited:
                continue
            visited.add(current)

            if current not in min_distances:
                min_distances[current] = dist
            else:
                min_distances[current] = min(min_distances[current], dist)

            if dist < x:
                for neighbor in graph.neighbors(current):
                    queue.append((neighbor, dist + 1))

    visited = set()
    queue = deque([start_node])

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)

        if min_distances.get(current, float('inf')) >= x:
            return current

        for neighbor in graph.neighbors(current):
            queue.append(neighbor)

    return start_node  # fallback if none found
    
def expand_chain_route_schedule(input_path, output_path=None):
    """
    Expands chain route schedules of a basdata file into individual departure and arrival entries.
    """
    
    with open(input_path, 'r') as f:
        json_data = json.load(f)
    
    schedules = json_data.get("ChainRouteSchedules", [])
    chain_routes_list = json_data.get("ChainRoutes", [])
    chain_routes_dict = {route["Guid"]: route for route in chain_routes_list if "Guid" in route}

    expanded = []

    end_time = datetime.strptime("23:59:59", "%H:%M:%S")

    for entry in schedules:
        start_time = datetime.strptime(entry["StartTime"], "%H:%M:%S")
        frequency = int(entry["Frequency"])
        route_key = entry["ChainRoute"]

        # Get duration in seconds
        duration_seconds = chain_routes_dict[route_key]["Duration"]
        delta = timedelta(seconds=frequency)
        duration_delta = timedelta(seconds=duration_seconds)

        # Create new entries with Departure and Arrival
        current_time = start_time
        
        trip_number = 1
        
        while current_time <= end_time:
            new_entry = {
                "Guid": f"{entry['Guid']}_{trip_number}",
                "ChainRoute": entry['ChainRoute'],
                "ProSchedule": entry['ProSchedule'],
                "Departure": current_time.strftime("%H:%M:%S"),
                "Arrival": (current_time + duration_delta).strftime("%H:%M:%S")
            }
            expanded.append(new_entry)
            current_time += delta
            trip_number += 1

    json_data["ChainRouteSchedules"] = expanded
    
    # Write to output file
    if not output_path:
        output_path = input_path
        
    with open(output_path, 'w') as f:
        json.dump(json_data, f, indent=4)

def remove_chain_suffix(s: str)-> str: 
    """
    Removes the suffix '_Chain' or '_Unchain' from the given string, if present.
    """
    if s.endswith("_Chain"):
        return s[:-6]
    elif s.endswith("_Unchain"):
        return s[:-8]
    else:
        return s 

def get_osmnx_route(start, end, graph, avg_speed, weight="length", via_nodes=None):
    """
    Get the route between start and end (coordinates or node IDs) using an OSMnx graph, optionally passing through intermediate waypoints.

    Parameters:
        start (tuple or int): Starting point as (lon, lat) coordinates or a node ID.
        end (tuple or int): Ending point as (lon, lat) coordinates or a node ID.
        graph (networkx.MultiDiGraph): The OSMnx graph to route on.
        avg_speed (float): Average travel speed in m/s, used to estimate travel time.
        weight (str, optional): Edge attribute to minimize (default is "length").
        via_nodes (list of int): Waypoints to pass through, as node ids.

    Returns:
        route (list of tuple): List of (lon, lat) coordinates representing the route.
        node_route (list of int): List of node IDs representing the route.
        distance (float): Total route distance in meters.
        duration (float): Estimated route duration in seconds (approximate).
    """
    if isinstance(start, tuple) :
        # if coordinate was given, map to node id
        orig_node = ox.distance.nearest_nodes(graph, X=start[0], Y=start[1])
    else:
        # node id was given
        orig_node = start
    
    if isinstance(end, tuple) :
        # if coordinate was given, map to node id
        dest_node = ox.distance.nearest_nodes(graph, X=end[0], Y=end[1])
    else:
        # node id was given
        dest_node = end 
    
    node_route = []
    
    if via_nodes is not None:
        
        via_nodes.insert(0, orig_node)
        via_nodes.append(dest_node)
        for i in range(len(via_nodes) - 1):
            route_segment = nx.shortest_path(graph, 
                                             via_nodes[i], 
                                             via_nodes[i+1], 
                                             weight=weight)
            if i > 0:
                route_segment = route_segment[1:]  # avoid duplicate nodes
            node_route.extend(route_segment)
        
    else:    
        node_route = ox.routing.shortest_path(graph, orig_node, dest_node, weight=weight) #  weight="travel_time" or "length"
    
    if len(node_route) < 2:
        # origin is destination
        return [start], [orig_node], 1, 1
    
    
    gdf = ox.routing.route_to_gdf(graph, node_route, weight=weight)
    lengths = list(gdf["length"])
    route = [(graph.nodes[n]['x'], graph.nodes[n]['y']) for n in node_route]
    distance = int(sum(lengths))
    duration = distance/avg_speed # list(gdf["travel_time"])
    
    return route, node_route, distance, duration

def get_node_coords(node, graph):
    """Rturns the coords (lon, lat) of a Node"""
    lon = graph.nodes[node]['x']
    lat = graph.nodes[node]['y']
    return (lon, lat)

def get_edge_route_from_node_route(graph, node_route):
    """Returns a list of OSMNX edge ids that connect sequence nodes in a graph."""
    edge_route = []
    for i in range(len(node_route)-1):
        from_node = node_route[i]
        to_node = node_route[i+1]
        edge_data: dict = graph[from_node][to_node][0]
        edge_route.append(edge_data.get("osmid", None))
    return edge_route

def get_route(start, end, server_ip='131.234.22.55', vehicle='car', points_encoded=False):
    """
    Calculate the route from start to end using a Graphhopper server.

    Parameters:
    - start: Tuple of (longitude, latitude) for the start point.
    - end: Tuple of (longitude, latitude) for the end point.
    - server_ip: The IP address where the Graphhopper server is running.
    - vehicle: The vehicle type for routing (default is 'car').

    Returns:
    - route: A list of (lat, lon) tuples representing the route.
    - route_distance (float): distance of route in meters.
    - route_time (int): duration of trip in milliseconds.
    """
    
    # Construct the URL using the server IP
    url = f"http://{server_ip}:8989/route"

    # Create the request body as a JSON payload
    params = {
        "point": [f"{start[1]},{start[0]}", f"{end[1]},{end[0]}"],
        "profile": vehicle,
        'locale': 'en',
        'calc_points': True,  # No need for detailed route points
        'instructions': False,  # No need for turn-by-turn instructions
        "points_encoded": points_encoded,  # We don't need the encoded polyline,
        "simplify_response": False,
        "way_point_max_distance ": 1.0,
        "do_simplify": False, 
    }

    # Send the POST request to the Graphhopper server
    response = requests.get(url, params=params)
    #response = requests.post(url, json=params)

    # Check if the request was successful
    if response.status_code == 200:
        data = response.json()

        # Extract the route geometry (lat/lon points)
        if 'paths' in data and len(data['paths']) > 0:
            if not points_encoded:
                route_geometry = data['paths'][0]['points']['coordinates']
                route_list = [(lon, lat) for lon, lat in route_geometry]
            else: 
                encoded_polyline = data['paths'][0]['points']  # The encoded polyline
                # Decode the polyline to get the list of coordinates
                # route_geometry = [(0,0)] 
                route_geometry = polyline.decode(encoded_polyline)
                route_list = [(lon, lat) for lat, lon  in route_geometry]

            # get route distance in m
            route_distance =  data['paths'][0]['distance']
            # get route time in ms
            route_time = data['paths'][0]['time']
            # Return the list of (lat, lon) tuples
            return route_list, route_distance, route_time
        else:
            print("No routes found.")
            return None, None, None
    else:
        print(f"Error: {response.status_code} - {response.text}")
        return None, None, None

def get_request_data(fname=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "input", "example_demand.json")):
    """
    Generate Dataframe form demand/request file in.

    Parameters:
    - fname: file path to json file specifying requests

    Returns:
    - df: a dataframe of the requests
    """
    
    with open(fname, 'r') as file:
        data = json.load(file)
    
    columns = ["UserGuid", "Instance", "PickUpLatitude", "PickUpLongitude", "DropOffLatitude", 
               "DropOffLongitude","SimulatedTime", "PickUpStartTime", "PickUpEndTime", 
               "DropOffStartTime", "DropOffEndTime","TimeWindowType","RequestedAdults",
               "RequestedChilds","NeedRamp", "AllowCarpooling","ToleratedDelayBefore", 
               "ToleratedDelayAfter"]
    
    # get request entries
    simulation_steps = data.get("simulationSteps", [])
    
    entries = []
    for step in simulation_steps:
        new_entry = {}
        request_params = step.get("requestParameter", {})
        new_entry["UserGuid"] = int(request_params.get("UserGuid", None))
        new_entry['Instance'] = os.path.splitext(os.path.basename(fname))[0]
        pu_location = request_params.get("CurrentLocation",{})
        new_entry["PickUpLatitude"] = pu_location.get("Latitude", None)
        new_entry["PickUpLongitude"] = pu_location.get("Longitude", None)
        do_location = request_params.get("TargetLocation",{})
        new_entry["DropOffLatitude"] = do_location.get("Latitude", None)
        new_entry["DropOffLongitude"] = do_location.get("Longitude", None)
        new_entry["SimulatedTime"] = datetime.fromisoformat(step.get("SimulatedTime", None))
        pu_window = request_params.get("pickupTime",{})
        do_window = request_params.get("targetTime",{})
        if pu_window == None:
            new_entry["TimeWindowType"] = False
            new_entry["PickUpStartTime"] = datetime.fromisoformat(do_window.get("StartTime", None))
            new_entry["PickUpEndTime"] = datetime.fromisoformat(do_window.get("EndTime", None))
        elif do_window == None:
            new_entry["TimeWindowType"] = True
            new_entry["DropOffStartTime"] = datetime.fromisoformat(pu_window.get("StartTime", None))
            new_entry["DropOffEndTime"] = datetime.fromisoformat(pu_window.get("EndTime", None))
        else:
            print("Error, both TWs specified")
            sys.exit(-1)
        new_entry["RequestedAdults"] = request_params.get("requestedAdults", None)
        new_entry["RequestedChilds"] = request_params.get("requestedChilds", None)
        new_entry["NeedRamp"] = request_params.get("needRamp", None)
        preferences = request_params.get("personalPreferences",{})
        new_entry["AllowCarpooling"] = preferences.get("allowCarpooling", None)
        new_entry["ToleratedDelayBefore"] = preferences.get("toleratedDelayBefore", None)
        new_entry["ToleratedDelayAfter"] = preferences.get("toleratedDelayAfter", None)
        entries.append(new_entry)
    
    df = pd.DataFrame(entries, columns=columns)
    
    # ensure correct data types for columns
    df["PickUpStartTime"] = pd.to_datetime(df["PickUpStartTime"], errors="coerce")
    df["PickUpEndTime"] = pd.to_datetime(df["PickUpEndTime"], errors="coerce")
    df["DropOffStartTime"] = pd.to_datetime(df["DropOffStartTime"], errors="coerce")
    df["DropOffEndTime"] = pd.to_datetime(df["DropOffEndTime"], errors="coerce")
    df["SimulatedTime"] = pd.to_datetime(df["SimulatedTime"], errors="coerce")
    
    # determine week day of trip
    try:
        df["TripWeekday"] = df["PickUpStartTime"].dt.day_name().fillna(
            df["DropOffStartTime"].dt.day_name()
        )
    except Exception as e:
        print(f"Error while creating 'TripWeekday': {e}")
    
    # determine total passengers
    df["TotalPassengers"] = df["RequestedAdults"] + df["RequestedChilds"]
    
    # determine trip time window
    # df["EarliestTripStart"] = df["PickUpStartTime"].dt.time
    # df["LatestTripEnd"] = (df["DropOffEndTime"] + pd.to_timedelta(df["ToleratedDelayAfter"], unit="s")).dt.time
    df.set_index("UserGuid", inplace=True)
    
    return df

def plot_edges_on_map(edge_ids, graph):
    
    all_lats = []
    all_lons = []
    id_list = []
        
    fig = go.Figure()


    for edge_id in edge_ids:
        from_node, to_node = edge_id
        edge_data = graph[from_node][to_node][0]  # key is usually 0 unless you have parallel edges
        shape = edge_data.get("geometry", None)
        
        if shape is not None:
            lons, lats = zip(*[(point[0], point[1]) for point in shape.coords])
        else:
            # Fall back to straight line between the nodes
            point_from = get_node_coords(from_node, graph)
            point_to = get_node_coords(to_node, graph)
            lons, lats = zip(point_from, point_to)
        
        all_lons.extend(lons)
        all_lats.extend(lats)  
        id_list.extend([f"Edge {edge_id}"] * len(lons)) 
        
        all_lons.append(None)  # None will break the line
        all_lats.append(None)  # None will break the line
        id_list.append(None)  # None will break the ID for that edge
        
        
    fig.add_trace(
        go.Scattermap(lon=all_lons,
                      lat=all_lats,
                      mode='lines+markers',
                      marker=dict(size=5, color="black", opacity=0.66),
                      line=dict(width=3, color="red"),
                      opacity=0.33,
                      name="Edges",
                      showlegend=True,
                      hovertemplate="%{customdata}<extra></extra>", # Show the custom label for each edge
                      customdata=id_list
                      )
        )

    all_lons_np = np.array(all_lons, dtype=np.float64)
    all_lats_np = np.array(all_lats, dtype=np.float64)

    # Filter out nan (convert None to nan automatically with float dtype)
    filtered_lons = all_lons_np[~np.isnan(all_lons_np)]
    filtered_lats = all_lats_np[~np.isnan(all_lats_np)]
    
    # Update layout - center, and zoom level
    fig.update_layout(
        title = "Edges on Map",
        template = "plotly",  # Use plotly template for styling
        showlegend = True,  # Enable legend to toggle visibility of pickups and drop-offs
        map_center_lon = filtered_lons.mean(),
        map_center_lat = filtered_lats.mean(),
        width = 800,  # Adjust width of the figure
        height = 600,  # Adjust height of the figure
        map_zoom = 12
    )

    fig.update_layout(map_style="carto-positron")
    fig.show()

def plot_nodes_on_map(node_ids, graph):
    fig = go.Figure()

    lats = []
    lons = []
    
    for node_id in node_ids:
        lon, lat = get_node_coords(node_id, graph)
        lats.append(lat)
        lons.append(lon) 
    
    fig.add_trace(
        go.Scattermap(
            lat= lats,
            lon= lons,
            mode="markers",
            marker=dict(
                size=(10),  # Marker size based on passenger count
                opacity=0.66,
                color="Green",  # this enables colormap
            ),
            text=node_ids,  # This is where the hover text is added
            hoverinfo="text",
            name="Nodes"  # Legend entry for pickups
        )
    )


    # Update layout - center, and zoom level
    fig.update_layout(
        title = "Nodes of Graph on Map",
        template = "plotly",  # Use plotly template for styling
        showlegend = True,  # Enable legend to toggle visibility of pickups and drop-offs
        map_center_lon = np.mean(lons),
        map_center_lat = np.mean(lats),
        width = 800,  # Adjust width of the figure
        height = 600,  # Adjust height of the figure
        map_zoom = 12
    )

    fig.update_layout(map_style="carto-positron")
    fig.show()

def plot_used_junctions(junctions_freq_df, graph, min_freq=1):
    """ Plots junctions with a 'Frequency' higher than 'min_freq', given dataframe with 'JunctionID' and 'Frequency' columns"""
    # merge data from self.junctions_df into junctions_freq_df
    junctions_freq_df = junctions_freq_df[junctions_freq_df['Frequency'] > min_freq]
    
    if len(junctions_freq_df) == 0:
        print("No junctions plot", flush=True)
        return
    
    max_freq = junctions_freq_df['Frequency'].max()
    
    lats = []
    lons = []
    freqs = junctions_freq_df['Frequency'].tolist()
    
    for node_id in junctions_freq_df.index.tolist():
        lon, lat = get_node_coords(node_id, graph)
        lats.append(lat)
        lons.append(lon) 
    
    fig = go.Figure()
    
    # add trace for junction frequencies
    fig.add_trace(
        go.Scattermap(
            lon=lons,
            lat=lats,
            mode='markers',
            marker=dict(
                size= 5 + 15 * (freqs / max_freq), 
                color=freqs,  
                colorscale="Sunsetdark",  
                opacity = 0.75, #(travelled_nodes_exploded_df['Frequency'] / max_freq),  
                colorbar=dict(
                    title="Frequency",  
                    x=1.15,  # Move to the right
                    y=0.35,  # Center vertically
                    len=0.75  # Adjust height
            ),
            ),
            name="Junctions",
            showlegend=False,
            hovertemplate=(
                "Node ID: %{customdata}<br>"  # Show Node ID
                "Lat: %{lat}<br>"  # Show Latitude
                "Lon: %{lon}<br>"  # Show Longitude
                "Frequency: %{marker.color}"  # Show Frequency (color value)
                "<extra></extra>"  # Removes extra hover box
            ),
            customdata=junctions_freq_df.index.tolist(),
            )
        )
    
    fig.update_layout(
        title="Junction Usage Over Time",
        template="plotly", 
        showlegend=True,
        map_center_lon=np.mean(lons),
        map_center_lat=np.mean(lats),
        width=1200,
        height=900,
        map_zoom=12,
    )
    fig.update_layout(map_style="carto-positron")
    fig.show()


def plot_od_corridor_map(od_table, bin_size_min, horizon_start, horizon_end):

    title = (
        f"OD Structural Demand "
        f"(bin={bin_size_min}min, "
        f"horizon={horizon_start}–{horizon_end})"
    )

    fig = px.scatter(
        od_table,
        x="UniqueTrips",
        y="ShareActiveBins",
        size="MaxFeasibleTripsInBin",
        color="Imbalance",
        hover_data=["PU", "DO", "MeanFeasibleTripsPerBin", "P90FeasibleTripsPerBin"],
        title=title,
    )

    fig.update_layout(
        xaxis_title="Total Unique Trips",
        yaxis_title="Share of Active Time Bins",
        height=600,
        width=900,
    )

    fig.show()
###########################################################################

@dataclass
class PointCluster:
    center: tuple[float, float]  # coord (lon, lat) of cluster center 
    size: int  # numper of points (PU or DO) in cluster

class ChainingLocation():
    '''
	Chaining Location class	
	'''
 
    def __init__(self, id: int, location_name: str, start_node, end_node, graph = None, chain_type:str = 'Both'):
        self.id = id
        self.location_name = location_name 
        self.start_node = start_node  # id of node in the gaph that specifies the start of the lane on which this CL will be placed                                    
        self.end_node = end_node  # id of node in the gaph that specifies the end of the lane on which this CL will be placed
        self.chain_type = chain_type  # Chain, Unchain or Both
        self.center = None  # coords (lon, lat) that is the center of the CL
        self.length = None  # length of CL
        self.location_start = {"Longitude": None, "Latitude": None}  # start coords of the CL
        self.location_end = {"Longitude": None, "Latitude": None}   # end coords of the CL
        if graph:
            self.set_location(start_node, end_node, graph)

    def get_guid(self):
        return f"{self.location_name}_{self.chain_type}" 
        
    def set_location(self, start_node, end_node, graph):
        """
        Sets the self.location_start and self.location_end of the instance. And then sets the self.center and self.length accordingly.

        Parameters:
            start_node: OSMNX node ID at one end of the edge
            end_node: OSMNX node ID at the other end of the edge
            graph: OSMNX MultiDiGraph
        """
        desired_length = 10 # desired length of the sub-segment (in meters) used for the CL
        
        edge_data = graph.get_edge_data(start_node, end_node)
        if edge_data is None:
            print(f"[Warning] No edge found between {start_node} and {end_node}. ",
                  f"Location of CL {self.location_name} could not be determined")
            return

        if isinstance(edge_data, dict):
            edge_data = next(iter(edge_data.values()))

        start_point = (graph.nodes[start_node]['x'], graph.nodes[start_node]['y'])
        end_point = (graph.nodes[end_node]['x'], graph.nodes[end_node]['y'])

        # Get geometry or fallback to straight line
        if 'geometry' in edge_data:
            geom = edge_data['geometry']
        else:
            geom = LineString([start_point, end_point])

        # Project geometry to meters using EPSG:3857 (Web Mercator)        
        project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
        geom_projected = transform(project, geom)
        
        edge_length = geom_projected.length

        if edge_length < MIN_CL_EDGE_LENGTH:
            print(f"[Warning] ChainingLocation Placement: Placing CL {self.id} on short edge Kof length ({edge_length:.2f} m).")
            end_dist = edge_length
            start_dist = end_dist - desired_length
    
        # Extract centered subsegment from projected geom
        mid_dist = edge_length // 2
        end_dist = mid_dist + desired_length // 2
        start_dist = mid_dist - desired_length // 2
        center_line_projected = substring(geom_projected, start_dist, end_dist, normalized=False)
        
        # find coords paralel to the road
        offset_distance = 3.5  # Offset distance in meters (right-hand side)
        coords_proj = list(center_line_projected.coords) # Get projected coordinates of the subsegment

        def offset_point_right(p1, p2, offset):
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]  # Vector from p1 to p2
            length = math.hypot(dx, dy)
            if length == 0:
                return p1  # prevent division by zero

            # Normalize and rotate 90° clockwise for right-hand side
            offset_dx = offset * dy / length
            offset_dy = -offset * dx / length

            # Offset both points
            p1_off = (p1[0] + offset_dx, p1[1] + offset_dy)
            p2_off = (p2[0] + offset_dx, p2[1] + offset_dy)
            return p1_off, p2_off
        
        p1_off_proj, p2_off_proj = offset_point_right(coords_proj[0], coords_proj[-1], offset_distance) # got coords paralel to the road

        # Reproject back to lon/lat
        unproject = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform 
        p1_off = transform(unproject, Point(p1_off_proj)).coords[0]
        p2_off = transform(unproject, Point(p2_off_proj)).coords[0]

        # Save offset coordinates
        self.location_start = {"Longitude": p1_off[0], "Latitude": p1_off[1]}
        self.location_end = {"Longitude": p2_off[0], "Latitude": p2_off[1]}
        center = np.mean(np.array([p1_off, p2_off]), axis=0)
        self.center = (center[0], center[1])
        self.length = desired_length
    
    def to_dictionary(self) -> dict:
       
        additional_time = 60

        dictionary = {"Guid":f"{self.get_guid()}",
                      "LocationStart": self.location_start,
                      "LocationEnd": self.location_end,
                      "Type": self.chain_type,
                      "AdditionalTime": additional_time
                    }
         
        return dictionary

    def __str__(self):
        return self.get_guid()
        
class ChainRoute():
    """
    Chain Route Class
    """
    def __init__(self, cl_id1, cl_id2, orig_node: int, dest_node: int, graph, occupied_nodes=None, via_nodes=None):
        """
        Parameters:
            cl_id1 (Any): Identifier for the starting chaining location (can be any type depending on context).
            cl_id2 (Any): Identifier for the ending chaining location.
            orig_node (int): The OSMnx node ID corresponding to the start of the route.
            dest_node (int): The OSMnx node ID corresponding to the end of the route.
            graph (networkx.MultiDiGraph): The OSMnx graph used for routing.
            occupied_nodes (list[int], optional): List of node IDs considered occupied or unavailable for placing Chaining Locations.
            via_nodes (list[int], optional): List of intermediate node IDs that the route must pass through.
        """
        self.cl_id1 = cl_id1  # id of CL, used for chaining
        self.cl_id2 = cl_id2  # id of CL, used for unchaining
        self.valid: bool = False
        self.intermediate = [] 
        self.duration = None  # Duration
        self.distance = None  # Distance
        self.node_route = None  # Route represented by list of OSMNX node ids
        self.route = None  # Route represented by list of coords (lon, lat)
        self.CL1: ChainingLocation
        self.UCL1: ChainingLocation
        self.CL2: ChainingLocation
        self.UCL2: ChainingLocation
        self.distance_ucl1_to_cl1 = None
        self.distance_ucl2_to_cl2 = None
        self.duration_ucl1_to_cl1 = None
        self.duration_ucl2_to_cl2 = None
        self.via_nodes = via_nodes
        self.determine_route(orig_node, dest_node, graph, occupied_nodes)
    
    def get_guid(self, cl1: ChainingLocation, cl2: ChainingLocation)-> str:
        """Returns the GUID of this ChainRoute"""
        return f"Station{cl1.id}_Station{cl2.id}" # Guid # route ID 
        
    def get_start(self, cl: ChainingLocation)-> str:
        """Returns the GUID of the start ChainingLoaction of the route"""
        return f"Station{cl.id}_{cl.chain_type}" #StartLocation
    
    def get_end(self, cl: ChainingLocation)-> str:
        """Returns the GUID of end ChainingLoaction of the route"""
        return  f"Station{cl.id}_{cl.chain_type}" #EndLocation
        
    def determine_route(self, orig_node: int, dest_node: int, graph, occupied_nodes=None):
        """
        Determines a valid route between two nodes in a road network graph and identifies 
        appropriate segments for chaining and unchaining operations. The method initializes 
        ChainingLocation objects such that they are located at the beginning and end of the route 
        by identifying edges of sufficient length that are not already occupied (by other ChainingLocation) and not one-way restricted.
        
        Parameters:
            orig_node (int): The origin node ID in the OSMNX graph.
            dest_node (int): The destination node ID in the OSMNX graph.
            graph: A graph object (e.g., OSMNX graph) used for routing.
            occupied_nodes (list[int], optional): List of node IDs that are already occupied by other lines 
                                                    and should be avoided for chaining. Defaults to an empty list.
        """
        if occupied_nodes is None:
            occupied_nodes = []
        
        # TODO deal with large edegs
        # TODO add GH routing as option
        
        route, node_route, distance, _ = get_osmnx_route(orig_node, dest_node, graph, AVG_PRO_SPEED, "travel_time", via_nodes=self.via_nodes)

        if ((len(node_route) < 4)
            and (distance < MINIMUM_LINE_DISTANCE)):
            # a valid line, must conists of at least 4 edges and exceede a minimum distance 
            self.valid = False
            return
        
        gdf = ox.routing.route_to_gdf(graph, node_route, weight="travel_time")
        lengths = list(gdf["length"])

        # find valid edge/node pairs for chaining locations of route
        # default values for start chaining locations
        chaining_location_start, chaining_location_end = node_route[0], node_route[1]
        unchaining_location_start, unchaining_location_end = node_route[1], node_route[0]
        
        # find the 1st valid edge
        for i, length in enumerate(lengths):
            if (length >= MIN_CL_EDGE_LENGTH):
                # if long enough edge found
                node_1, node_2 = node_route[i:i+2] # get ids of the nodes connected by this length 
                if ((node_1 not in occupied_nodes) 
                    and (node_2 not in occupied_nodes)):
                    # if edge is not already used by another line
                    edge = graph.get_edge_data(node_1, node_2)[0] # get edge between nodes
                    if (edge['oneway'] is False):
                        # check for oneways
                        chaining_location_start, chaining_location_end = node_route[i], node_route[i+1]
                        unchaining_location_start, unchaining_location_end = node_route[i+1], node_route[i]
                        # update route and lengths
                        node_route = node_route[i:]
                        route = route[i:]
                        lengths = lengths[i:]
                        # stop search once first valid edge is found
                        break
        else:
            # reached the end, and could not fine valid edges    
            self.valid = False
            return
        
        
        # init the CL for un/chaining for route
        self.UCL1 = ChainingLocation(self.cl_id1, 
                                    f"Station{self.cl_id1}", 
                                    unchaining_location_start, 
                                    unchaining_location_end, 
                                    graph, 
                                    'Unchain')
        
        self.CL1 = ChainingLocation(self.cl_id1, 
                                    f"Station{self.cl_id1}", 
                                    chaining_location_start, 
                                    chaining_location_end, 
                                    graph, 
                                    'Chain')
        
        # find valid edge/node pairs for chaining locations of route
        # default values for end chaining locations
        unchaining_location_start, unchaining_location_end = node_route[-2], node_route[-1]
        chaining_location_start, chaining_location_end = node_route[-1], node_route[-2]
        
        # find the last valid edge
        for i in range(len(lengths) - 1, -1, -1):
            length = lengths[i]
            if (length >= MIN_CL_EDGE_LENGTH):
                # if long enough edge found
                node_1, node_2 = node_route[i:i+2] # get ids of the nodes connected by this length 
                if ((node_1 not in occupied_nodes) 
                    and (node_2 not in occupied_nodes)):
                    # if edge is not already used by another line
                    edge = graph.get_edge_data(node_1, node_2)[0] # get edge between nodes
                    if (edge['oneway'] is False):
                        # check for oneways
                        unchaining_location_start, unchaining_location_end = node_route[i], node_route[i+1]
                        chaining_location_start, chaining_location_end = node_route[i+1], node_route[i]
                        # update route and lengths
                        node_route = node_route[:i+2]
                        route = route[:i+2]
                        lengths = lengths[:i+1]
                        # stop search once first valid edge is found
                        break
        else:
            # reached the end, and could not fine valid edges    
            self.valid = False
            return
        
        # init the CL for un/chaining for route
        self.UCL2 = ChainingLocation(self.cl_id2,
                                    f"Station{self.cl_id2}",
                                     unchaining_location_start,
                                     unchaining_location_end,
                                     graph,
                                     'Unchain')
        
        self.CL2 = ChainingLocation(self.cl_id2,
                                    f"Station{self.cl_id2}",
                                     chaining_location_start,
                                     chaining_location_end,
                                     graph,
                                     'Chain')
        
        final_distance = int(sum(lengths))
        if (final_distance < MINIMUM_LINE_DISTANCE): 
            self.valid = False
            return
        
        # successfully created route and Cls
        self.valid = True
        self.route = route
        self.node_route = node_route
        self.distance = final_distance
        self.duration = self.distance / AVG_PRO_SPEED
        self.distance_ucl1_to_cl1 = lengths[0]
        self.distance_ucl2_to_cl2 = lengths[-1]
        self.duration_ucl1_to_cl1 = self.distance_ucl1_to_cl1 / AVG_PRO_SPEED
        self.duration_ucl2_to_cl2 = self.distance_ucl2_to_cl2 / AVG_PRO_SPEED
    
    def reverse_route(self, graph):
        """
        Reverses the route by swapping the chaining and unchaining locations.
        Also updates related attributes accordingly.
        
        Parameters:
            graph: An OSMNX graph object used for routing.
        """
        
        self.cl_id1, self.cl_id2 = self.cl_id2, self.cl_id1
        self.CL1, self.CL2 = self.CL2, self.CL1
        self.UCL1, self.UCL2 = self.UCL2, self.UCL1        
        orig_node, dest_node = self.CL1.end_node, self.CL2.start_node
        if self.via_nodes is not None:
            self.via_nodes.reverse()
        self.route, self.node_route, self.distance, self.duration = get_osmnx_route(orig_node, dest_node, graph, AVG_PRO_SPEED, "travel_time", self.via_nodes)
        
    def to_dictionary(self) -> dict:
        """Returns a dictionary that can be used to create a ChainRoute entry in base data files."""
        out = {"Guid": self.get_guid(self.CL1, self.UCL2),
               "StartLocation": self.get_start(self.CL1),
               "IntermediateChainingLocations": self.intermediate,
               "EndLocation": self.get_end(self.UCL2),
               "Duration": self.duration,
               "Distance": self.distance
        }
        return out
    
    def get_mini_trip_dictionaries(self):
        
        out1 = {"Guid": self.get_guid(self.UCL1, self.CL1),
               "StartLocation": self.get_start(self.UCL1),
               "IntermediateChainingLocations": self.intermediate,
               "EndLocation": self.get_end(self.CL1),
               "Duration": self.duration_ucl1_to_cl1,
               "Distance": self.distance_ucl1_to_cl1
        }
        out2 = {"Guid": self.get_guid(self.UCL2, self.CL2),
               "StartLocation": self.get_start(self.UCL2),
               "IntermediateChainingLocations": self.intermediate,
               "EndLocation": self.get_end(self.CL2),
               "Duration": self.duration_ucl2_to_cl2,
               "Distance": self.distance_ucl2_to_cl2
        }

        return [out1, out2]

    def __str__(self):
        out = (f"From {self.CL1} to {self.UCL2}")
        return out
        
class ProTimeTable():
    """
    A class for pro time tables. An itterable that stores departure/arrival times and un/chaining locations ids from/to pros are travelling. 
    """
    def __init__(self, id, start_time: datetime, start_chaining_location_id, arrival_time: datetime, end_chaining_location_id):
        self.id = id
        self.depart_schedule = [start_time]  # time stamps of departures from the chainion locations in self.chaining_schedule
        self.chaining_schedule = [start_chaining_location_id]  # ids the chaining locations from whcih the pro will depart
        self.arrival_schedule = [arrival_time]  # time stamps of arivals at the chainion locations in self.unchaining_schedule
        self.unchaining_schedule = [end_chaining_location_id]  # ids of the unchaining locations where pro will arrive
    
    def append_entries_to_schedules(self, depart_time: datetime, chaining_location_id, arrival_time: datetime, unchaining_location_id):
        self.depart_schedule.append(depart_time)
        self.chaining_schedule.append(chaining_location_id)
        self.arrival_schedule.append(arrival_time)
        self.unchaining_schedule.append(unchaining_location_id)
        
    def __getitem__(self, index):
        return (
            self.depart_schedule[index],
            self.chaining_schedule[index],
            self.arrival_schedule[index],
            self.unchaining_schedule[index]
        )
    
    def __iter__(self):
        return iter(zip(self.depart_schedule, self.chaining_schedule, self.arrival_schedule, self.unchaining_schedule))
      
class PlatoonLine():
    """
    Platoon line Class
    """
    def __init__(self, id):
        self.id = id  # PlatoonLine id 
        self.cluster_1 = []  # clusters used to determine 1st CL of PlatoonLine
        self.cluster_2 = []  # clusters used to determine 2nd CL of PlatoonLine
        self.trip_count = []  # number of trips travelling between clusters in self.cluster_1 and self.cluster_2
        self.chaining_location_ids = [-1, -1]  # id of CLs
        self.chain_route_1: ChainRoute = None # ChainRoute obj
        self.chain_route_2: ChainRoute = None # ChainRoute obj, the return trip
        
    def has_clusters(self)-> bool:
        """Return true if this instance has values in both self.cluster_1 and self.cluster_2. Else Fasle is returned"""
        return bool(self.cluster_1 and self.cluster_2)
    
    def total_trips(self)-> int:
        """Returns the total trips used to create this line"""
        return sum(self.trip_count)
    
    def get_destination_chaining_location_id(self, start_chaining_location_ids):
        """
        Given a starting chaining location ID, returns the corresponding destination 
        chaining location ID from a pair of known chaining location IDs.

        Parameters:
            start_chaining_location_ids: The ID of the starting chaining location.

        Returns:
            The destination chaining location ID if the start ID is found; otherwise, None.
        """
        try:
            index = self.chaining_location_ids.index(start_chaining_location_ids)
        except ValueError:
            return None

        if index == 0:
            return self.chaining_location_ids[1]
        else:
            return self.chaining_location_ids[0]
        
    def get_arrival_time(self, depart_time, start_chaining_location_id, end_chaining_location_id)-> datetime:
        """
        Calculates the expected arrival time at the destination chaining location, given a departure time and a start location.

        Parameters:
            depart_time (datetime): The departure time from the start chaining location.
            start_chaining_location_id: The ID of the starting chaining location.
            end_chaining_location_id: The ID of the destination chaining location.

        Returns:
            depart_time (datetime): The estimated arrival time at the destination chaining location.
        """
        try:
            start_index = self.chaining_location_ids.index(start_chaining_location_id)
        except ValueError:
            start_index = None

        try:
            end_index = self.chaining_location_ids.index(end_chaining_location_id)
        except ValueError:
            end_index = None
         
        max_delay = 3*60 # maximum duration of delay caused by unchaining
         
        if ((start_index is not None) and (end_index is not None) and (start_index == end_index)):
            # duration for pro to get back to Cl 1 from CL 1 after going by CL 2
            # or duration for pro to get back to CL 2 from CL 2 after going by CL 1
            duration = self.chain_route_1.duration + self.chain_route_2.duration + max_delay
        elif start_index == 0:
            # duration for pro to get to CL 1 from CL 2
            duration = self.chain_route_1.duration
        elif start_index == 1:
            # duration for pro to get to CL 2 from CL 1
            duration = self.chain_route_2.duration      
        
        return depart_time + timedelta(seconds=duration)
    
    def __str__(self) -> str:
        
        distances = [self.chain_route_1.distance, self.chain_route_2.distance]
        durations = [self.chain_route_1.duration, self.chain_route_2.duration]
        
        return (
            f"Line {self.id}:\n"
            f"Cluster 1: {self.cluster_1}\n"
            f"Cluster 2: {self.cluster_2}\n"
            f"Trip Count: {self.trip_count} = {self.total_trips()}\n"
            f"Route 1: {self.chain_route_1}\n"
            f"Route 2: {self.chain_route_2}\n"
            f"Travel Distances between Chaining_Locations: {distances} (m)\n"
            f"Travel Durations between Chaining_Locations: {durations} (sec)"
        )

class RoadNetworkData():
    """
    A class for managing a dataframes for nodes and edges of SUMO road Network.
    """
    
    def __init__(self, netfile_path):
        self.net = sumolib.net.readNet(netfile_path)
        self.edge_df = self.generate_edge_df()
        self.junctions_df = self.generate_junction_df()
        self.G = self.generate_graph()  
        
        # setup kd_tree to find closest junctions
        junction_coordinates = self.junctions_df[['Lon', 'Lat']].to_numpy()
        self.junction_kdtree = KDTree(junction_coordinates)   
    
    def generate_edge_df(self):
        """
        Create edge_df with the following columns
        EdgeID: SUMO edge ID 
        FromJunction: Junction from which edge is coming from
        ToJunction: Junction to which edge is going to
        Shape: Coordinates specifiyingt the shape of the edge in format[(long, lat),....]
        Length: Length of edge in meters
        TravelTime: Estimated travel time (Length / Maximum Allowed Speed) in seconds
        Type: SUMO edge type
        """
        # specify edges types typically not allowing cars to drive on them
        dont_inlcude = ['highway.path', 'highway.cycleway','highway.steps', 'highway.footway', 'highway.pedestrian', 'railway.rail'] 

        edge_df = pd.DataFrame(columns=["EdgeID", "FromJunction", "ToJunction", "Shape", "Length", "TravelTime", "Type"])

        for edge in self.net.getEdges():
            
            edge_type_tuple = edge.getType(),
            edge_type = edge_type_tuple[0] # I never saw a type_tuple contain more than one value
            
            # skip edges with "wrong" types
            if edge_type in dont_inlcude:
                continue
            
            id_tuple = edge.getID(),
            id = id_tuple[0]
            
            shape_tuple = edge.getShape(),
            shape = shape_tuple[0]
            new_shape = [(self.net.convertXY2LonLat(x, y)) for x, y in shape]
            
            from_junc = edge._from.getID()
            to_junc = edge._to.getID()
            
            length = edge.getLength()
            
            travel_time = length / edge.getSpeed() # seconds
            
            entry = {"EdgeID": id, "FromJunction": from_junc, "ToJunction": to_junc, "Shape": new_shape, "Length":length ,"TravelTime": travel_time, "Type": edge_type}
            edge_df.loc[len(edge_df)] = entry

        # set the index to be the names column
        edge_df.set_index('EdgeID', inplace=True)
        return edge_df
    
    def generate_junction_df(self):
        """
        Create junction_df with the following columns
        JunctionID: SUMO junction ID 
        Lon: Longitude of junction
        Lat: Latitude of junction
        Type: SUMO junction type
        """
        # create a dataframe of junctions
        junctions_df = pd.DataFrame(columns=["JunctionID", "Lon", "Lat", "Type"])
        
        for node in self.net.getNodes():
            
            edge_list = node._incLanes
            edge_list = [edge_id[0:-2] for edge_id in edge_list]
            
            # only use junctions that are attached to edges in self.edge_df
            if not set(edge_list) & set(self.edge_df.index.values):
                continue
            
            id_tuple = node.getID(),
            id = id_tuple[0]
            type_tuple = node.getType(),
            x, y = node._coord[0], node._coord[1]
            lon, lat = self.net.convertXY2LonLat(x, y)
            entry = {"JunctionID": id, "Lon": lon, "Lat": lat, "Type": type_tuple}
            junctions_df.loc[len(junctions_df)] = entry
            
        # set the index to be the names column
        junctions_df.set_index('JunctionID', inplace=True)
        
        return junctions_df
    
    def generate_graph(self):
        G = nx.DiGraph()
        
        # Add nodes to Graph (junctions)
        for index, row in self.junctions_df.iterrows():
            G.add_node(index, pos=(row['Lon'], row['Lat']))

        # Add edges to graph (streets/lanes)
        for index, row in self.edge_df.iterrows():
            edge_id = index
            from_node = row["FromJunction"]
            to_node = row["ToJunction"]
            length = row["Length"]
            travel_time = row["TravelTime"] # seconds
            
            # some edges ad nodes for junctions that are not in the junctions_df, 
            # because of some strange/dead end behaviour
            G.add_edge(from_node, to_node, weight=travel_time, id=edge_id, length=length) # weight is the minimum time it would take to drive along the edge
        
        return G
    
    def find_nearest_junction(self, lon, lat, distance_threshold=0.001):
       """
       Returns the ID of the closest junction to the given coords (lon, lat)
       """
       distance, idx = self.junction_kdtree.query((lon, lat), distance_upper_bound=distance_threshold)
       
       if idx < len(self.junctions_df):
           return self.junctions_df.index[idx]  # Return the node ID from the DataFrame's index
       else:
           # if no junction was close enough find the closest junctions from the closest edge
           edge = self.find_nearest_edge(lon, lat)    
           from_node_id = edge._from.getID()
           to_node_id = edge._to.getID()
           
           from_node_coords = tuple(self.junctions_df.loc[from_node_id][['Lat', 'Lon']])
           to_node_coords = tuple(self.junctions_df.loc[to_node_id][['Lat', 'Lon']])
           
           from_distance = geodesic((lat, lon), from_node_coords).meters
           to_distance = geodesic((lat, lon), to_node_coords).meters 
       
           junction_id = to_node_id if to_distance < from_distance else from_node_id
           return junction_id
        
    def find_nearest_edge(self, lon, lat):
        """
        Returns the SUMO edge object of the closest edge to the given coords (lon, lat)
        """
        x,y = self.net.convertLonLat2XY(lon, lat)
        radius = 10
        i=0
        
        edges = self.net.getNeighboringEdges(x, y, radius)
        distances_and_edges = sorted([(dist, edge) for edge, dist in edges], key=lambda x:x[0])
        dist, closest_edge = distances_and_edges[0]
        

        while (closest_edge.allows('passenger') == False) and (closest_edge.allows('delivery') == False):
            i = i + 1
            
            if i < len(edges):
                dist, closest_edge = distances_and_edges[i]
            else: 
                radius = radius * 1.5
                edges = self.net.getNeighboringEdges(x, y, radius)
                distances_and_edges = sorted([(dist, edge) for edge, dist in edges], key=lambda x:x[0])
                
        return closest_edge
    
    def verify_junction_route(self, route_junctions):
        """
        Completes a route defined by nodes, by adding intermediate nodes. Assumes that a junction cant move to and from the same junction.
        """
        updated_junctions = [route_junctions[0]]  
        
        for i in range(len(route_junctions) - 1):
            
            start_node = route_junctions[i] # get current node
            end_node = route_junctions[i + 1] # get next node
            
            # cant go from the same junction to the same junction
            if start_node == end_node:
                continue
            
            shortest_path = [start_node, end_node]
            
            if not self.G.has_edge(start_node, end_node):
                shortest_path = nx.shortest_path(self.G, source=start_node, target=end_node, weight='length') # get path from current to next
                
            updated_junctions.extend(shortest_path[1:]) # add path from current to to next to build 
        
        return updated_junctions
    
    def convert_node_route_to_edge_route(self, route_id, node_sequence):
        """
        Returns a list of edges in that connect the nodes/junction in the node_sequence
        """
        edge_sequence = []
        for i in range(len(node_sequence)-2):
            edge = self.G.get_edge_data(node_sequence[i], node_sequence[i+1])
            if not edge:
                print(f"No Edge between {node_sequence[i]} - {node_sequence[i+1]} for route of {route_id}")
                continue
            edge_id = edge['id']
            edge_sequence.append(edge_id) 
        return edge_sequence
    
    def get_junction_coords(self, junction_ids=None):
        """
        Returns list of latitudes and a list of longitudes, for the corresponding junction_ids 
        """
        if junction_ids is not None:
            subset = self.junctions_df.loc[junction_ids]
        else:
            subset = self.junctions_df

        lats = subset['Lat'].tolist()
        lons = subset['Lon'].tolist()
        return lats, lons
     
    def get_edge_scatter_map(self, edge_ids=None, mode='map'):
        """ Returns go.Scattermap of selecetd edges """
        all_lats = []
        all_lons = []
        id_list = []
        # reduce dataframe if appropriate
        if edge_ids is None:
            filtered_edge_df = self.edge_df
        else:
            filtered_edge_df = self.edge_df.loc[edge_ids]
            
        for index, row in filtered_edge_df.iterrows(): 
            route = row['Shape'] 
            lons, lats = zip(*route)  # Unpack list of (lat, lon) tuples
            
            all_lons.extend(lons)
            all_lats.extend(lats)
            
            # Assign an ID for each edge
            id_list.extend([f"Edge {index}"] * len(lons)) 
            
            all_lons.append(None)  # None will break the line
            all_lats.append(None)  # None will break the line
            id_list.append(None)  # None will break the ID for that edge

        if mode=='scatter':
                trace = go.Scatter(
                x=all_lons,
                y=all_lats,
                mode='lines+markers',
                marker=dict(size=5, color="black", opacity=0.66),
                line=dict(width=3, color="red"),
                opacity=0.33,
                name="Edges",
                showlegend=True,
                customdata=id_list,
                hovertemplate="%{customdata}<extra></extra>",
            )
        else:
            trace = go.Scattermap(lon=all_lons,
                                lat=all_lats,
                                mode='lines+markers',
                                marker=dict(size=5, color="black", opacity=0.66),
                                line=dict(width=3, color="red"),
                                opacity=0.33,
                                name="Edges",
                                showlegend=True,
                                hovertemplate="%{customdata}<extra></extra>",  # Show the custom label for each edge
                                customdata=id_list
                                )
            
        return trace
    
    def get_jucntion_scatter_map(self, junction_ids=None):
        """ Returns go.Scattermap of selecetd junctions """
        
        # reduce dataframe if appropriate
        if junction_ids is None:
            filtered_junctions_df = self.junctions_df
            junction_ids = filtered_junctions_df.index.values
            
        else:
            filtered_junctions_df = self.junctions_df.loc[junction_ids]
        
        lats = filtered_junctions_df['Lat']
        lons = filtered_junctions_df['Lon']
        
        trace = go.Scattermap(
                lat= lats,
                lon= lons,
                mode="markers",
                marker=dict(
                    size=(10),
                    opacity=0.66,
                    color='Green'
                ),
                text=junction_ids,  # text for hover
                hoverinfo="text",
                name="Nodes"  # Legend entry for pickups
            )
        
        return trace

    def plot_used_edges(self, edge_freq_df, min_freq=1, pu_trace=None, do_trace=None):
        """ Plots edges with a 'Frequency' higher than 'min_freq', given dataframe with 'EdgeID' and 'Frequency' columns """
        
        # merge data from self.junctions_df into junctions_freq_df
        edge_freq_df = edge_freq_df.merge(self.edge_df, left_index=True, right_index=True, how="left")
        
        edge_freq_df = edge_freq_df[edge_freq_df['Frequency']>=min_freq]
        
        max_freq = edge_freq_df['Frequency'].max()

        color_values = np.linspace(0, 1, int(max_freq + 1))  # Normalized for color mapping
        # color maps: 
        # Sample colors from Plotly colormap
        colors1 = pc.sample_colorscale('darkmint', color_values) 
        colors2 = pc.sample_colorscale('burgyl', color_values)
        fig = go.Figure()

        # Add each edge as a line
        for index, row in edge_freq_df.iterrows():
            freq =  int(row['Frequency'])
            color = colors2[freq] if index[0] == '-' else colors1[freq]
            lon, lat = zip(*row['Shape'])  # Unpack coordinates 
            fig.add_trace(go.Scatter(
                x=lon, y=lat,
                mode='markers+lines',
                line=dict(color=color, width=2),
                marker=dict(symbol="arrow", size=5*(freq/max_freq), angleref="previous"),
                name=index,
                hovertemplate=f"ID:{index}<br>Frequency: {freq}<extra></extra>", # Display data on hover frequency
                opacity=0.9,
                showlegend=False,
            ))

        # Add color bars
        # First color bar (left)
        fig.add_trace(go.Scatter(
            x=[None], y=[None],  # Invisible point
            mode='markers',
            marker=dict(
                colorscale='Tealgrn',
                cmin=0, cmax=max_freq,  
                colorbar=dict(
                    title="Frequency", 
                    x=1.05,  # Move a bit to the left
                    y=0.5,  # Center vertically
                    len=0.75  # Adjust height
                ),
                showscale=True,
            ),
            showlegend=False
        ))

        # Second color bar (right)
        fig.add_trace(go.Scatter(
            x=[None], y=[None],  # Invisible point
            mode='markers',
            marker=dict(
                colorscale='pinkyl',  # Second colormap
                cmin=0, cmax=max_freq,  
                colorbar=dict(
                    title="Frequency",  
                    x=1.15,  # Move to the right
                    y=0.5,  # Center vertically
                    len=0.75  # Adjust height
                ),
                showscale=True
            ),
            showlegend=False
        ))

        # add  pu and do traces if they exsist
        if pu_trace:
            fig.add_trace(pu_trace)
            
        if do_trace:
            fig.add_trace(do_trace)
        
        # Update layout
        fig.update_layout(
            title="Road/Edge Usage",
            xaxis_title="Longitude",
            yaxis_title="Latitude",
            width=1200,
            height=900, 
            showlegend=True,
        )

        fig.show()
        
    def plot_used_junctions(self, junctions_freq_df, min_freq=1):
        """ Plots junctions with a 'Frequency' higher than 'min_freq', given dataframe with 'JunctionID' and 'Frequency' columns """
        # merge data from self.junctions_df into junctions_freq_df
        junctions_freq_df = junctions_freq_df.merge(self.junctions_df, left_index=True, right_index=True, how="left")
        junctions_freq_df = junctions_freq_df[junctions_freq_df['Frequency'] > min_freq]
        max_freq = junctions_freq_df['Frequency'].max()
        
        fig = go.Figure()
        
        # add trace for junction frequencies
        fig.add_trace(
            go.Scattermap(
                lon=junctions_freq_df['Lon'],
                lat=junctions_freq_df['Lat'],
                mode='markers',
                marker=dict(
                    size= 5 + 15 * (junctions_freq_df['Frequency'] / max_freq), 
                    color=junctions_freq_df['Frequency'],  
                    colorscale="Sunsetdark",  
                    opacity = 0.75, #(travelled_nodes_exploded_df['Frequency'] / max_freq),  
                    colorbar=dict(
                        title="Frequency",  
                        x=1.15,  # Move to the right
                        y=0.35,  # Center vertically
                        len=0.75  # Adjust height
                ),
                ),
                name="Junctions",
                showlegend=False,
                hovertemplate=(
                    "Node ID: %{customdata}<br>"  # Show Node ID
                    "Lat: %{lat}<br>"  # Show Latitude
                    "Lon: %{lon}<br>"  # Show Longitude
                    "Frequency: %{marker.color}"  # Show Frequency (color value)
                    "<extra></extra>"  # Removes extra hover box
                ),
                customdata=junctions_freq_df.index.values,
                )
            )
        
        fig.update_layout(
            title="Junction Usage with Used Egdes",
            template="plotly", 
            showlegend=True,
            map_center_lon=self.junctions_df["Lon"].mean(),
            map_center_lat=self.junctions_df["Lat"].mean(),
            width=1200,
            height=900,
            map_zoom=12,
        )

        fig.update_layout(map_style="carto-positron")
        fig.show()
       
    def plot_used_edges_and_junctions(self, edge_freq_df, junctions_freq_df, min_edge_freq=1, min_junction_freq=1, pu_trace=None, do_trace=None):
        
        # merge data from self.junctions_df into junctions_freq_df
        edge_freq_df = edge_freq_df.merge(self.edge_df, left_index=True, right_index=True, how="left")
        edge_freq_df = edge_freq_df[edge_freq_df['Frequency']>= min_edge_freq]
        max_edge_freq = edge_freq_df['Frequency'].max()
        
        # merge data from self.junctions_df into junctions_freq_df
        junctions_freq_df = junctions_freq_df.merge(self.junctions_df, left_index=True, right_index=True, how="left")
        junctions_freq_df = junctions_freq_df[junctions_freq_df['Frequency'] >= min_junction_freq]
        max_junction_freq = junctions_freq_df['Frequency'].max()
        
        fig = go.Figure()
        
        # add tracce for used edges
        fig.add_trace(self.get_edge_scatter_map(edge_freq_df.index.values))
        
        # add  pu and do traces if they exsist
        if pu_trace:
            fig.add_trace(pu_trace)
            
        if do_trace:
            fig.add_trace(do_trace)
        
        # add trace for junction frequencies
        fig.add_trace(
            go.Scattermap(
                lon=junctions_freq_df['Lon'],
                lat=junctions_freq_df['Lat'],
                mode='markers',
                marker=dict(
                    size= 5 + 15 * (junctions_freq_df['Frequency'] / max_junction_freq), 
                    color=junctions_freq_df['Frequency'],  
                    colorscale="Sunsetdark",  
                    opacity = 0.75, #(travelled_nodes_exploded_df['Frequency'] / max_freq),  
                    colorbar=dict(
                        title="Frequency",  
                        x=1.15,  # Move to the right
                        y=0.35,  # Center vertically
                        len=0.75  # Adjust height
                ),
                ),
                name="Junctions",
                showlegend=True,
                hovertemplate=(
                    "Node ID: %{customdata}<br>"  # Show Node ID
                    "Lat: %{lat}<br>"  # Show Latitude
                    "Lon: %{lon}<br>"  # Show Longitude
                    "Frequency: %{marker.color}"  # Show Frequency (color value)
                    "<extra></extra>"  # Removes extra hover box
                ),
                customdata=junctions_freq_df.index.values,
                )
            )
        
        fig.update_layout(
        title="Junctions and Edges of Road Network",
        template="plotly", 
        showlegend=True,
        map_center_lon=self.junctions_df["Lon"].mean(),
        map_center_lat=self.junctions_df["Lat"].mean(),
        width=1200,
        height=900, 
        map_zoom=12,
        )

        fig.update_layout(map_style="carto-positron")
        
        fig.show()
             
    def plot_road_network_map(self, edge_ids=None, junction_ids=None):
        """ Plots the junctions and egdes of the road network / graph on a map"""
        fig = go.Figure()
        
        # Add trace for edges
        fig.add_trace(self.get_edge_scatter_map(edge_ids))
        
        # Add trace for junctions
        fig.add_trace(self.get_jucntion_scatter_map(junction_ids))

        fig.update_layout(
            title="Junctions and Edges of Road Network",
            template="plotly", 
            showlegend=True,
            map_center_lon=self.junctions_df["Lon"].mean(),
            map_center_lat=self.junctions_df["Lat"].mean(),
            width=800,
            height=600, 
            map_zoom=12,
        )

        fig.update_layout(map_style="carto-positron")
        fig.show()

class RequestDataframe():
    """
    A class for creating, manipulation dataframes from request/demand files to determine possible lines and pro-schedules.
    """

    def __init__(self, path: str, op_verts: OperationalVertices):
        
        # create graph/road network of area
        center = op_verts.get_center()
        # calculate radius
        corner = (op_verts.ne_lat, op_verts.ne_lon)
        radius = geodesic(center, corner).meters 
        # generate road network Graph
        self.G = ox.graph.graph_from_point(center, dist=radius, network_type="drive")
        self.G  = ox.routing.add_edge_speeds(self.G) # impute speed on all edges missing data
        self.G  = ox.routing.add_edge_travel_times(self.G) # calculate travel time (seconds) for all edge
        
        # create df from instance file(s)
        self.df: pd.DataFrame = None
        if os.path.isfile(path):
            if path.endswith(".json"):
                self.generate_dataframe_from_file(path)
            if path.endswith(".csv"):
                self.load_df_from_csv(path)
            else:
                raise ValueError("File type not supported.")
        elif os.path.isdir(path):
            self.generate_dataframe_from_dir(path)
        else:
            raise FileNotFoundError(f"file/dir not found: {path}")
        
        self.point_clusters: dict[int, PointCluster]
        self.cluster_travel_times: np.ndarray  # a matrix with travel times between clusters
        self.cluster_travel_distances: np.ndarray # a matrix with distances (via road network) between clusters
        self.chain_routes: dict[int, ChainRoute] = {} # ID, route object
        self.lines: dict[int, PlatoonLine] = {}  # ID, line object
        self.chaining_locations: dict[str, ChainingLocation] = {}
        self.unchaining_locations: dict[str, ChainingLocation] = {}

    def save_df_to_csv(self, fpath):
        """Save the self.df to a csv file."""
        self.df.to_csv(fpath)
        
    def load_df_from_csv(self, fpath):
        """
        Load the self.df from a csv file.
        Unable to convert columns with strings to datatime objetcs, automatically.  
        """
        
        self.df = pd.read_csv(fpath, index_col=0)
        
        # Code below not working
        
        for col in self.df.select_dtypes(include='object'):
            # Drop NA and strip whitespace
            non_null_series = self.df[col].dropna().astype(str).str.strip()

            if non_null_series.empty:
                continue

            sample_val = non_null_series.iloc[0]
            try:
                # Attempt to parse one sample value using fromisoformat fallback
                pd.to_datetime(sample_val, utc=True)  # works for ISO strings like "2025-10-18T18:17:38+02:00"
                
                # If parse succeeds, convert whole column
                self.df[col] = pd.to_datetime(self.df[col], errors='coerce', utc=True)
            except Exception:
                continue
    
    def generate_dataframe_from_file(self, fname):
        """ Sets the self.df based on a single request file. """
        self.df = get_request_data(fname)
        # drop unneeded columns
        self.df = self.df.drop(columns=["RequestedAdults", "RequestedChilds", "NeedRamp", "AllowCarpooling", "ToleratedDelayBefore", "ToleratedDelayAfter"], errors="ignore")
        self.add_start_and_end_node_columns()
        
    def generate_dataframe_from_dir(self, dir):
        """ Sets the self.df based on all request files inside a directory. """
        demand_files = [os.path.join(dir, f) for f in os.listdir(dir) if os.path.isfile(os.path.join(dir, f))]
        dfs = []
        for demand_file in demand_files:
            df = get_request_data(demand_file)
            if not df.empty and not df.isna().all().all():
                # Only add if not empty and not all-NaN
                dfs.append(df)
            
        self.df = pd.concat(dfs, ignore_index=True) if dfs else None
        
        # drop unneeded columns
        self.df = self.df.drop(columns=["RequestedAdults", "RequestedChilds", "NeedRamp", "AllowCarpooling", "ToleratedDelayBefore", "ToleratedDelayAfter"], errors="ignore")
        
        self.add_start_and_end_node_columns() 
        
    def add_start_and_end_node_columns(self):
        """
        Add the 'PickUpNodeOSMNX' and 'DropOffNodeOSMNX' columns to self.df. 
        
        The node ids in these columns are those of the PU and DO coordinatsed mapped to the OSMNX nodes.
        """
        # Combine all coordinates into one list
        pu_coords = list(zip(self.df["PickUpLongitude"], self.df["PickUpLatitude"]))
        do_coords = list(zip(self.df["DropOffLongitude"], self.df["DropOffLatitude"]))
        combined_coords = pu_coords + do_coords
        # Extract separate lon/lat lists
        lon_all, lat_all = zip(*combined_coords)
        # Find nearest nodes in the graph for all coordinates
        all_nodes = ox.distance.nearest_nodes(self.G, X=lon_all, Y=lat_all)
        # Save PU and DO nodes directly to self.df
        num_pu = len(self.df)
        self.df["PickUpNodeOSMNX"] = all_nodes[:num_pu]
        self.df["DropOffNodeOSMNX"] = all_nodes[num_pu:]
           
    def load_lines_from_ini_data(self, fpath:str):
        """    
        Loads line and chaining location data from an InitBaseData file.

        Sets the `self.lines` and `self.chaining_locations` dictionaries according to the
        data in the InitBaseData file provided at `fpath`.

        Parameters:
            fpath (str): Path to a InitBaseData file.
            
        Notes:
            Not wroking.
        """
        with open(fpath, 'r') as file:
            data = json.load(file)
    
        # get request entries
        chain_routes = data.get("ChainRoutes", []) # list of lines 
        chaining_locations = data.get("ChainingLocations", []) # list of lines 
        
        pass 
        self.lines = {}
        self.chaining_locations = {}
        cl_name_to_id = {}
        cl_pair_to_line = {}
        # cr_name_to_id_mapping = {}
        
        for cl_dict in chaining_locations:
            guid = cl_dict['Guid']
            cl_name = remove_chain_suffix(guid)
            
            start_coords = (cl_dict['LocationStart']['Longitude'], cl_dict['LocationStart']['Latitude'])
            end_coords = (cl_dict['LocationEnd']['Longitude'], cl_dict['LocationEnd']['Latitude'])
            
            if cl_name not in cl_name_to_id:
                cl_id = len(self.chaining_locations)+1
                cl_name_to_id[cl_name] = cl_id
                cl = ChainingLocation(cl_id, cl_name, start_coords, end_coords, 0, self.G)
                self.chaining_locations[cl_id] = cl
        
        for cr_dict in chain_routes:
            chain_route = ChainRoute(**cr_dict)
            cl_start_name, cl_end_name = remove_chain_suffix(chain_route.start), remove_chain_suffix(chain_route.end)
            cl_id_pair = sorted([cl_name_to_id[cl_start_name], cl_name_to_id[cl_end_name]])
            
            
            if (tuple(cl_id_pair)) not in cl_pair_to_line:
                line_id = len(cl_pair_to_line)+1
                line = PlatoonLine(line_id)
                
                line.chaining_location_ids = cl_id_pair
                line.chain_route_1 = chain_route
                
                chaining_locations[cl_start_name]
                chaining_locations[cl_start_name]
                
                # TODO correct add routes correctly 
                # orig_node, dest_node = ox.distance.nearest_nodes(self.G, X=[start_coords[0], end_coords[0]], Y=[start_coords[1],end_coords[1]])
                # node_route = ox.routing.shortest_path(self.G, orig_node, dest_node, weight="travel_time") #  weight="travel_time" or "length"
                # # line.route_1 = [(self.G.nodes[n]['y'], self.G.nodes[n]['x']) for n in node_route]
                # node_route = ox.routing.shortest_path(self.G, dest_node, orig_node, weight="travel_time") #  weight="travel_time" or "length"
                # # line.route_2 = [(self.G.nodes[n]['y'], self.G.nodes[n]['x']) for n in node_route]
                
                self.lines[line_id] = line
    
                cl_pair_to_line[tuple(cl_id_pair)] = line_id
                
            else:
                line_id = cl_pair_to_line[tuple(cl_id_pair)]
                self.lines[line_id].chain_route_2 = chain_route
        
    def get_grahphopper_route_data_for_entry(self, index):
        """
        Determine the route (represented by geo coordinates (lon,lat)) for a request in the dataframe.
        Route is calculate the using a GraphHopper server.
        
        Parameters:
            index (int): to specify entry in the dataframe
        
        Returns:
            route: A list of (lon, lat) tuples representing the route.
            distance (float): distance of route in meters
            duration (int): duratiuon of route in seconds.
        """
        entry = self.df.iloc[index]
        start = (entry.PickUpLongitude, entry.PickUpLatitude)
        end = (entry.DropOffLongitude, entry.DropOffLatitude)
        route, distance, duration_ms = get_route(start, end, server_ip='131.234.22.55', vehicle='car', points_encoded=True)
        duration = round(duration_ms/1000)
        return route, distance, duration
        
    def get_osmnx_route_data_for_entry(self, index):
        """
        Determine the route (represented by geo coordinates (lon,lat)) for a request in the dataframe.
        Route is determined using a OSMNX. 
        
        Parameters:
            index (int): to specify entry in the dataframe
        
        Returns:
            route: A list of (lon, lat) tuples representing the route.
            distance (float): distance of route in meters
            duration (int): duratiuon of route in seconds.
            node_route: A list of node ids representing the route.
            edge_route: A list of edge ids representing the route.
        """
        entry = self.df.iloc[index]
        start = entry["PickUpNodeOSMNX"]
        end = entry["DropOffNodeOSMNX"]
        route, node_route, distance, duration = get_osmnx_route(start, end, self.G, avg_speed=AVG_PASSENGER_CAB_SPEED, weight="travel_time")
        return route, distance, duration, node_route
        
    def calculate_metrics_to_start_chaining_from_pickup(self, index):
        """
        Determine the duration for a trip entry in the dataframe from PickUp to StartChainingLocation.
        Route is calculated the using a OSMNX.
        
        Parameters:
            index (int): to specify entry in the dataframe
        
        Returns:
            duration (int): duratiuon of route in seconds.
            distance (int): distance of route in meters.
        """
        entry = self.df.iloc[index]
        start = entry["PickUpNodeOSMNX"]
        # start = (entry["PickUpLongitude"], entry["PickUpLatitude"])
        chaining_location_id = entry['StartChainingLocation']
        if chaining_location_id == -1:
            return np.nan, np.nan
        chaining_location = self.chaining_locations[chaining_location_id]
        end = chaining_location.center        
        _, _, distance, duration = get_osmnx_route(start, end, self.G, AVG_PASSENGER_CAB_SPEED, weight="travel_time")
        return duration, distance
    
    def calculate_metrics_to_dropoff_from_end_chaining(self, index):
        """
        Determine the duration for a trip entry in the dataframe from EndChainingLocation to DropOff.
        Route is calculated the using a OSMNX.
        
        Parameters:
            index (int): to specify entry in the dataframe
        
        Returns:
            duration (int): duratiuon of route in seconds.
            distance (int): distance of route in meters.
        """
        entry = self.df.iloc[index]
        start = entry["DropOffNodeOSMNX"]
        # start = (entry["DropOffLongitude"], entry["DropOffLatitude"])
        chaining_location_id = entry['EndChainingLocation']
        if chaining_location_id == -1:
            return np.nan, np.nan
        chaining_location = self.chaining_locations[chaining_location_id]
        end = chaining_location.center
        
        _, _, distance, duration = get_osmnx_route(start, end, self.G, AVG_PASSENGER_CAB_SPEED, weight="travel_time")
        
        return duration, distance
            
    def add_graph_hopper_route_columns(self):        
        """
        Adds the following columns to self.df: "GraphHopperRoute", "GHEstimatedTripDistance", "GHEstimatedDuration".
        Also estimates the missing values of PickUpStartTime, PickUpEndTime, DropOffStartTime, DropOffEndTime.
        """
        results = self.df.index.to_series().apply(lambda idx: self.get_grahphopper_route_data_for_entry(idx))
        # Unpack results from geo_results and shortest_results into new DataFrame columns
        self.df[["GraphHopperRoute", "GHEstimatedTripDistance", "GHEstimatedDuration"]] = pd.DataFrame(results.tolist(), index=self.df.index)
            
        # Convert GHEstimatedDuration (seconds) to timedelta
        self.df['GHEstimatedDuration'] = pd.to_timedelta(self.df['GHEstimatedDuration'], unit='s')
        
        missing_pickup_times = self.df['PickUpStartTime'].isna()  # Mask for rows where DropOffStartTime is NaT
        
        missing_drop_off_times = self.df['DropOffStartTime'].isna()
        
        # Update PickpUp Times where it is NaT
        self.df.loc[missing_pickup_times, 'PickUpStartTime'] = self.df.loc[missing_pickup_times, 'DropOffStartTime'] - self.df.loc[missing_pickup_times, 'GHEstimatedDuration']
        self.df.loc[missing_pickup_times, 'PickUpEndTime'] = self.df.loc[missing_pickup_times, 'DropOffEndTime'] - self.df.loc[missing_pickup_times, 'GHEstimatedDuration']
        
        # Update LatestTripEnd
        self.df.loc[missing_drop_off_times, 'DropOffStartTime'] = self.df.loc[missing_drop_off_times, 'PickUpStartTime'] + self.df.loc[missing_drop_off_times, 'GHEstimatedDuration']
        self.df.loc[missing_drop_off_times, 'DropOffEndTime'] = self.df.loc[missing_drop_off_times, 'PickUpEndTime'] + self.df.loc[missing_drop_off_times, 'GHEstimatedDuration']
        
    def add_osmnx_route_columns(self, map_from_gh=False):        
        """
        Adds the following columns to self.df: "OSMNXRoute", "OSMNXEstimatedTripDistance", "OSMNXEstimatedDuration", "OSMNXNodeRoute".
        Also estimates the missing values of PickUpStartTime, PickUpEndTime, DropOffStartTime, DropOffEndTime.
        .
        
        Parameters:
            map_from_gh (bool): If True, the route established through GraphHopper is mapped to the graph. 
        """
        
        if map_from_gh == True:
            results = self.map_graphopper_to_osmnx()
        else:
            results = self.df.index.to_series().apply(lambda idx: self.get_osmnx_route_data_for_entry(idx))
        
        # Columns to be added
        columns = [
            "OSMNXRoute",
            "OSMNXEstimatedTripDistance",
            "OSMNXEstimatedDuration",
            "OSMNXNodeRoute"
        ]
        # Convert results to a DataFrame
        processed_results = results.tolist()

        # Assign to DataFrame
        self.df[columns] = pd.DataFrame(processed_results, index=self.df.index) 

        # Fill in missing time data
        # Convert EstimatedDuration (seconds) to timedelta
        self.df['OSMNXEstimatedDuration'] = pd.to_timedelta(self.df['OSMNXEstimatedDuration'], unit='s')
        
        missing_pickup_times = self.df['PickUpStartTime'].isna()  # Mask for rows where DropOffStartTime is NaT
        
        missing_drop_off_times = self.df['DropOffStartTime'].isna()
        
        # Update PickpUp Times where it is NaT
        self.df.loc[missing_pickup_times, 'PickUpStartTime'] = self.df.loc[missing_pickup_times, 'DropOffStartTime'] - self.df.loc[missing_pickup_times, 'OSMNXEstimatedDuration']
        self.df.loc[missing_pickup_times, 'PickUpEndTime'] = self.df.loc[missing_pickup_times, 'DropOffEndTime'] - self.df.loc[missing_pickup_times, 'OSMNXEstimatedDuration']
        
        # Update LatestTripEnd where it is NaT
        self.df.loc[missing_drop_off_times, 'DropOffStartTime'] = self.df.loc[missing_drop_off_times, 'PickUpStartTime'] + self.df.loc[missing_drop_off_times, 'OSMNXEstimatedDuration']
        self.df.loc[missing_drop_off_times, 'DropOffEndTime'] = self.df.loc[missing_drop_off_times, 'PickUpEndTime'] + self.df.loc[missing_drop_off_times, 'OSMNXEstimatedDuration']
    
    def get_difference_between_gh_and_osmnx(self)->pd.DataFrame:
        """
        Computes and returns summary statistics of geodesic distance differences between 
        tart and end points of routes obtained from GraphHopper and OSMNX.
        """
        start_difs = []
        end_difs = []

        if (("GraphHopperRoute" not in self.df.columns) 
            and ("OSMNXRoute" not in self.df.columns)):
            print("Either GraphHopperRoute or the OSMNXRoute column has not been added"
                  "\nTry running add_graph_hopper_route_columns() or add_osmnx_route_columns() first.")
            return
        
        for index, entry in self.df.iterrows():
            gh_start_coord = entry["GraphHopperRoute"][0]  # (lon, lat)
            gh_end_coord = entry["GraphHopperRoute"][-1]  # (lon, lat)
            
            osmnx_start = entry['OSMNXRoute'][0]
            osmnx_end = entry['OSMNXRoute'][-1]
            
            start_difs.append(geodesic(gh_start_coord[::-1], osmnx_start[::-1]).meters)
            end_difs.append(geodesic(gh_end_coord[::-1], osmnx_end[::-1]).meters)
            
        difference_df = pd.DataFrame(list(zip(start_difs, end_difs)), columns=['StartDif', 'EndDif'])
        difference_df.describe()
        
        return difference_df.describe()
        
    def map_graphopper_to_osmnx(self):
        """ 
        Generates the OSMNX columns (-Route, -NodeRoute, -EdgeRoute, -Duration and -Distance) 
        
        Notes:
            Not working
        """
        
        # make sure GraphHopeprRoute column exists
        if not ('GraphHopperRoute' in self.df.columns):
            self.add_graph_hopper_route_column()
        
        node_route_list = []
        egde_route_list = []
        route_list = []
        
        # iterate over GraphHopper routes for trips
        for route_num, coord_list in enumerate(self.df["GraphHopperRoute"]):
            lons = [pt[0] for pt in coord_list]
            lats = [pt[1] for pt in coord_list]
            # map GraphHopperRoute to self.G graph
            node_route, distances = ox.distance.nearest_nodes(self.G, X=lons, Y=lats, return_dist=True) 
            # remove nodes that are too far away from the GH coords
            updated_node_route = [int(node) for node, dist in zip(node_route, distances) if dist <= 25]
            updated_node_route = self.verify_mapped_route(updated_node_route)
            edge_route = get_edge_route_from_node_route(self.G, updated_node_route)              
            node_route_list.append(updated_node_route)
            egde_route_list.append(edge_route)
            # get route 
            route = []
            # get coords of nodes 
            for i, node_id in enumerate(updated_node_route):
                lon, lat = get_node_coords(node_id, self.G)
                route.append((lon, lat))
            route_list.append(route)
        
        return (route_list, 
                self.df['GHEstimatedTripDistance'].tolist(), 
                self.df['GHEstimatedDuration'].tolist(),
                node_route_list,
                egde_route_list)
    
    def verify_mapped_route(self, node_route):
        """
        Returns a valid and complete node route by adding intermediate nodes to node_route.
        
        Parameters:
            node_route: A list of node ids representing the route.
        
        Returns:
            updated_node_route: A list of node ids representing the route.
        
        Notes:
            Not working.
        """
        
        updated_node_route = [node_route[0]]  
        
        for i in range(len(node_route) - 1):
            
            start_node = node_route[i] # get current node
            end_node = node_route[i + 1] # get next node
            
            # cant go from the same junction to the same junction
            if start_node == end_node:
                continue
            
            shortest_path = [start_node, end_node]
            
            if not self.G.has_edge(start_node, end_node):
                shortest_path = nx.shortest_path(self.G, source=start_node, target=end_node, weight='length') # get path from current to next
                
            updated_node_route.extend(shortest_path[1:]) # add path from current to to next to build 
        
        return updated_node_route
        
    def filter_df_by_time(self, min_time, max_time)-> pd.DataFrame:
        """
        Filters self.df to include only trips that occur within a specified time window.

        Parameters:
            min_time (datetime.time or None): The lower bound of the time window (inclusive). 
                                            If None, no lower bound is applied.
            max_time (datetime.time or None): The upper bound of the time window (inclusive). 
                                            If None, no upper bound is applied.

        Returns:
            pandas.DataFrame: A filtered copy of self.df containing only trips whose pickup or dropoff times fall 
                            within the specified window.
        """                    
        if (min_time==None) and (max_time==None):
            return self.df.copy()
        
       
        elif (min_time==None) and (max_time!=None):
            # any trips happening after min_time
            filtered_df = self.df[(self.df["PickUpStartTime"].dt.time >= min_time) | (self.df["DropOffEndTime"].dt.time  >= min_time)]
            
        elif (min_time==None) and (max_time!=None):
            # any trips happening before max_time
            filtered_df = self.df[(self.df["PickUpStartTime"].dt.time <= max_time) | (self.df["DropOffEndTime"].dt.time <= max_time)]
            
        else:
            # trips starting in time window
            filtered_df = self.df[(self.df["PickUpStartTime"].dt.time >= min_time) & (self.df["PickUpStartTime"].dt.time <= max_time)]
        
        return filtered_df.copy()
        
    def get_edge_frequencies(self, min_time=None, max_time=None)-> pd.DataFrame:
        """
        Computes and returns the frequency of used edges by requests from the 'OSMNXNodeRoute' column.

        Parameters:
            min_time (datetime.time, optional): Lower bound of the trip time window. Trips that start before this time are excluded.
            max_time (datetime.time, optional): Upper bound of the trip time window. Trips that start after this time are excluded.

        Returns:
            pd.DataFrame: A DataFrame indexed by OSMNX edge IDs with the following columns:
                - 'EdgeID' (tuple): a tuple with node ids (from_node, to_node)
                - 'Frequency': The number of trips that traversed each edge.
        """
        # Ensure OSMNXNodeRoute column exists
        if 'OSMNXNodeRoute' not in self.df.columns:
            self.add_osmnx_route_columns()

        # Filter trips by time window
        filtered_df = self.filter_df_by_time(min_time, max_time)

        # Count edge frequencies
        edge_counter = Counter()

        for route in filtered_df['OSMNXNodeRoute'].dropna():
            edge_counter.update(zip(route[:-1], route[1:]))

        # Convert to DataFrame
        freq_df = pd.DataFrame.from_records(
            list(edge_counter.items()),  # Wrap in list to avoid 'not subscriptable' error
            columns=['EdgeID', 'Frequency']
        )

        # Sort
        freq_df.sort_values(by='Frequency', ascending=False).reset_index(drop=True)
        freq_df = freq_df.set_index('EdgeID')
        
        return freq_df
    
    def get_node_frequencies(self, min_time=None, max_time=None)-> pd.DataFrame:
        """
        Computes and returns the frequency of requests ar routed through a node (junction), by using the 'OSMNXNodeRoute' column.

        Parameters:
            min_time (datetime.time, optional): Lower bound of the trip time window. 
                                                Only trips starting at or after this time are considered.
            max_time (datetime.time, optional): Upper bound of the trip time window. 
                                                Only trips starting at or before this time are considered.

        Returns:
            pd.DataFrame: A DataFrame indexed by OSMNX node IDs with the following columns:
                - 'NodeID': The unique identifier of an OSMNX node (junction).
                - 'Frequency': The number of trips that include this node in their route.
        """
        if not ('OSMNXNodeRoute' in self.df.columns):
            self.add_osmnx_route_columns()
             
        # Explode node routes to help anaylyze routes
        filtered_df = self.filter_df_by_time(min_time, max_time)
        junction_freq_df  = filtered_df['OSMNXNodeRoute'].explode("OSMNXNodeRoute")
        # Get the frequency count of each unique junction
        junction_freq_df = junction_freq_df.value_counts().reset_index()
        junction_freq_df.columns = ['NodeID', 'Frequency']
        junction_freq_df = junction_freq_df.set_index('NodeID')
        return junction_freq_df
        
    def determine_lines_with_clustering(self, cluster_method: str = "DBSCAN", 
                                        eps = 250, 
                                        minpts: int = 20, 
                                        maxpts: int | None = None, 
                                        routing_method: str = "OSMNX") -> None:
        """
        Use road network constrained clustering of PU/DO points to determine the popular lines for the trips in self.df.
        
        Parameters:
            cluster_method (str): The clustering algorithm to use. Currently supports 'DBSCAN' (default).
            eps (int or float): The maximum distance (meters) between two samples for one to be considered as in the neighborhood of the other. 
            minpts (int): The number of samples in a neighborhood for a point to be considered as a core point.
            maxpts (int, optional): Maximum number of points allowed in a cluster. If None, no upper limit is applied. Only relevant for HDBSCAN.
            routing_method (str): Routing method to use when building routes between cluster centers. Default is "OSMNX".      
        """
        
        self.point_clusters: dict[int, PointCluster] = {}
        
        # get all the nodes ofs PUs and DOs
        all_nodes = self.df["PickUpNodeOSMNX"].to_list() + self.df["DropOffNodeOSMNX"].to_list()

        # Prepare unique nodes for distance matrix
        nodes_unique = pd.Series(np.unique(all_nodes))
        nodes_unique.index = nodes_unique.values

        def network_distance_matrix(u, D, vs=nodes_unique):
            dists = (nx.dijkstra_path_length(D, source=u, target=v, weight="length") for v in vs)
            return pd.Series(dists, index=vs)

        # Build distance matrix
        D = ox.convert.to_digraph(self.G, weight="length")
        node_dm = nodes_unique.apply(network_distance_matrix, D=D).astype(int)
        
        # newtwork constrained clustering with HDBSCAN does not seem to work
        if cluster_method == "HDBSCAN":
            # Keep actual distances
            dense_dm = node_dm.reindex(index=all_nodes, columns=all_nodes).values

            # Replace infinities or NaNs
            dense_dm[np.isinf(dense_dm)] = 1e6
            dense_dm[np.isnan(dense_dm)] = 1e6

            # Symmetrize if needed
            if not np.allclose(dense_dm, dense_dm.T, atol=1e-8):
                dense_dm = 0.5 * (dense_dm + dense_dm.T)
    
            params = {'min_cluster_size': minpts,
                      'cluster_selection_epsilon': eps,
                      'max_cluster_size': maxpts,
                      'metric': 'precomputed'
                      }
            
            # Perform clustering
            hdb = HDBSCAN(**params)
            cluster_labels = hdb.fit_predict(dense_dm)
        
        else:
            node_dm[node_dm == 0] = 1
            node_dm[node_dm > eps] = 0

            # Reindex to match all_nodes order
            net_dm = node_dm.reindex(index=all_nodes, columns=all_nodes)
            
            # convert network-based distance matrix to a sparse matrix
            net_dm_sparse = sort_graph_by_row_values(csr_matrix(net_dm), warn_when_not_sorted=False)
            
            # Perform clustering
            db = DBSCAN(eps=eps, min_samples=minpts, metric="precomputed")
            cluster_labels = db.fit_predict(net_dm_sparse)
            
        # Assign clusters back to self.df
        num_pu = len(self.df)
        self.df["PickUpCluster"] = cluster_labels[:num_pu]
        self.df["DropOffCluster"] = cluster_labels[num_pu:]
            
        # Get centers of clusters
        # Combine all coordinates and labels into one DataFrame
        lon_all = list(self.df["PickUpLongitude"]) + list(self.df["DropOffLongitude"])
        lat_all = list(self.df["PickUpLatitude"]) + list(self.df["DropOffLatitude"])
        clusters_all = list(self.df["PickUpCluster"]) + list(self.df["DropOffCluster"])
        
        temp_df = pd.DataFrame({
            "lon": lon_all,
            "lat": lat_all,
            "cluster": clusters_all
        })

        # Exclude noise (-1)
        temp_df = temp_df[temp_df["cluster"] != -1]

        # group by cluster and calculate mean coordinates and size 
        grouped = temp_df.groupby("cluster")

        for cluster_id, group in grouped:
            new_PC = PointCluster(center=(group["lon"].mean(), group["lat"].mean()),
                                  size=len(group)
                                  )
            self.point_clusters[cluster_id] = new_PC
        
        self.determine_cluster_travel_time_and_distances(routing_method=routing_method)
        self.determine_lines_from_cluster_info(min_distance=MINIMUM_CLUSTER_DISTANCE_FOR_LINE, routing_method=routing_method)
        self.assign_trips_to_lines_from_cluster_info()
        # these 2 functions are really slow 
        # self.add_osmnx_route_columns()   
        self.add_estimated_time_at_start_chaining_location_column()
        
    def determine_lines_with_clustering_2(self, min_cluster_size=25, max_cluster_size=100, cluster_selection_epsilon=25, routing_method="OSMNX"):
        """
        Use HDBSCAN clustering of PU/DO points to determine the popular lines for the trips in self.df. 
        Its is recommmended to rather use determine_lines_with_clustering(), which uses network contrained clustering with DBSCAN.
        
        Parameters:
        min_cluster_size (int): Minimum number of points required to form a cluster.
        max_cluster_size (int): Maximum allowable number of points per cluster.
        cluster_selection_epsilon (int): Distance threshold for merging nearby clusters (in meters).
        routing_method (str): Method used to compute routes between clusters, e.g., 'OSMNX'.

        Returns:
            None: The method updates internal state including clusters, lines, and un/chaining locations etc..
        """
                
        self.cluster_pus_and_dos_with_hdbscan(min_cluster_size=min_cluster_size, max_cluster_size=max_cluster_size, cluster_selection_epsilon=cluster_selection_epsilon)
        # Calculate the distances and travel Times between cluster centers
        self.determine_cluster_travel_time_and_distances(routing_method=routing_method)
        self.determine_lines_from_cluster_info()
        self.assign_trips_to_lines_from_cluster_info()
    
    def determine_cluster_travel_time_and_distances(self, routing_method: str="OSMNX"):
        """
        Generates Travel Time Matrix (self.cluster_travel_times) and Travel Distance Matrix (self.cluster_travel_distances) for the cluster centers in self.point_clusters. 
        Can determine the matrices with GraphHopper or the OSMNX graph.
        
        Parameters:
            routing_method (str): Routing method to use when building routes between cluster centers. Default is "OSMNX".
        """
        # Calculate the distances and travel Times between cluster centers
        
        self.cluster_travel_times = np.zeros((len(self.point_clusters), len(self.point_clusters)))
        self.cluster_travel_distances = np.zeros((len(self.point_clusters), len(self.point_clusters)))
        
        for start_cluster_id, start_cluster_data in self.point_clusters.items():
            
            start_coords = start_cluster_data.center
            
            for end_cluster_id, end_cluster_data in self.point_clusters.items():
                
                if end_cluster_id == start_cluster_id:
                    continue
                
                end_coords = end_cluster_data.center
                
                
                if routing_method == 'GraphHopper':
                    # calculate mnetrics between cluster centers using GraphHopper 
                    _, distance, duration_ms = get_route(start=start_coords, end=end_coords)
                    duration = round(duration_ms/1000)
                
                else: 
                    # Default use the OSMNX graph
                    _, _, distance, duration = get_osmnx_route(start_coords, end_coords, self.G, avg_speed=AVG_PRO_SPEED, weight="travel_time")
                    
                self.cluster_travel_times[start_cluster_id][end_cluster_id] = duration
                self.cluster_travel_distances[start_cluster_id][end_cluster_id] = round(distance)

    def assign_trips_to_lines_from_cluster_info(self):
        """
        Adds pro line related columns (Line, StartChainingLocation and EndChainingLocation) to the dataframe after creating running self.determine_lines_with_clustering()
        """
        # Create mapping for quick lookup from cluster pair to line
        pair_to_line = {}

        for line_id, line in self.lines.items():
            
            for cluster_1, cluster_2 in zip(line.cluster_1, line.cluster_2):
                tuple_id = tuple(sorted([cluster_1, cluster_2]))
                
                if tuple_id not in pair_to_line:
                    pair_to_line[tuple_id] = line.id
                
                
        # Create 'ClusterPair' column with sorted clusters for both PickUpCluster and DropOffCluster
        self.df['ClusterPair'] = list(map(lambda x, y: tuple(sorted([x, y])), self.df['PickUpCluster'], self.df['DropOffCluster']))

        self.df['Line'] = self.df['ClusterPair'].map(pair_to_line).fillna(-1).astype(int)

        # Vectorized assignment of Start and End Chaining_Locations
        def assign_chaining_locations(entry):
            if entry['Line'] == -1:
                return -1, -1  # Default values for no line assignment
            
            entry_line = self.lines[entry['Line']]
            
            # Check for cluster assignment and assign the correct chaining_locations
            if int(entry['PickUpCluster']) in entry_line.cluster_1:
                return entry_line.chaining_location_ids[0], entry_line.chaining_location_ids[1]
            else:
                return entry_line.chaining_location_ids[1], entry_line.chaining_location_ids[0]
        
        # Generate 'StartChainingLocation', 'EndChainingLocation' columns
        self.df[['StartChainingLocation', 'EndChainingLocation']] = self.df.apply(assign_chaining_locations, axis=1, result_type='expand')
        
        # Convert columns to categorical
        self.df["Line"] = self.df["Line"].astype('category')
        self.df["StartChainingLocation"] = self.df["StartChainingLocation"].astype('category')
        self.df["EndChainingLocation"] = self.df["EndChainingLocation"].astype('category')

        # drop redundant 'ClusterPair' column
        self.df.drop(columns=['ClusterPair'], inplace=True)

    def reassign_trips_to_lines(self, line_ids=None, min_distance: int=LINE_USAGE_THRESHOLD):
        """
        Adjusts line related columns (Line, StartChainingLocation and EndChainingLocation) of self.df,
        such that trips can only be assigned to line with ids contained in line_ids.
        
        Parameters:
            line_ids (list): ids of lines to consider.
            min_distance (int): minimum distance a trip need to drive to consider the use of a line.
        """
        #  TODO check if speedup is possible.
        
        # need to add at least one trip distance estimation in self.df
        if (("GHEstimatedTripDistance" not in self.df.columns)
            and ("OSMNXEstimatedTripDistance" not in self.df.columns)):
                self.add_osmnx_route_columns()
        
        results = self.df.index.to_series().apply(lambda idx: self.assign_trip_to_line(idx, line_ids, min_distance))
        line_results, start_cl_results, end_cl_results, distance_results, eta_results = zip(*results)
        self.df["Line"] = line_results
        self.df["StartChainingLocation"] = start_cl_results
        self.df["EndChainingLocation"] = end_cl_results
        self.df["EstimatedAdditionalDistance"] = distance_results
        self.df["EstimatedTimeAtStartChainingLocation"] = eta_results
    
    def assign_trip_to_line(self, index, line_ids=None, min_distance: int=LINE_USAGE_THRESHOLD):
        """
        Determines the optimal line assignment for a single trip in `self.df` based on travel cost 
        (distance) required to use a line.

        Parameters:
            index (int): Index of the trip in `self.df`.
            line_ids (list): List of line IDs to consider when selecting the best line.
            min_distance (int): Minimum travel distance (in meters) required for a trip to be eligible for line assignment.

        Returns:
            best_line (int or str): ID of the most suitable line for the trip.
            best_start_cl (int or str): ID of the start chaining location for the line.
            best_end_cl (int or str): ID of the end chaining location for the line.
            best_distance (float): Distance (in meters) the trip would travel to use the selected line.
            best_eta_start_cl (datetime): Estimated time of arrival at the start chaining location.
        """
        # TODO check if speedup is possible.    
        entry = self.df.iloc[index]
        initial_line_id = entry["Line"]
        original_distance = entry['GHEstimatedTripDistance'] if 'GHEstimatedTripDistance' in self.df.columns else entry['OSMNXEstimatedTripDistance']
        best_distance = original_distance
        best_line = -1
        best_start_cl = -1
        best_end_cl = -1
        best_eta_start_cl = None
        
        if line_ids == None:
            line_ids = self.lines
        
        min_saved = 1 # minimum distance needed to be saved, to consider using a line 
        
        if best_distance < min_distance:
            # if the direct trip is below the threshold, dont assign a line to this trip
            return best_line, best_start_cl, best_end_cl, 0, best_eta_start_cl
        
        if initial_line_id in line_ids:
            # trip is already assigned to its optimal line
            best_line = initial_line_id
            best_start_cl = entry["StartChainingLocation"]
            best_end_cl = entry["EndChainingLocation"]
            best_distance, _, _, best_eta_start_cl = self.calculate_cost_of_line_usage(index=index, line_id=best_line, cost='length', cl_id_1=best_start_cl, cl_id_2=best_end_cl)
        else:          
            for line_id in line_ids:
                distance, start_cl, end_cl, eta_at_start_cl = self.calculate_cost_of_line_usage(index=index, line_id=line_id, cost='length')
                
                distance_saved = (original_distance - distance)
                
                if ((distance_saved >= min_saved) and (distance < best_distance)):
                    best_line = line_id
                    best_start_cl = start_cl
                    best_end_cl = end_cl
                    best_distance = distance
                    best_eta_start_cl = eta_at_start_cl
        
        best_distance = 0 if best_line == -1 else best_distance
        
        return best_line, best_start_cl, best_end_cl, best_distance, best_eta_start_cl
              
    def calculate_cost_of_line_usage(self, index, line_id: int, cost: str='length', cl_id_1=None, cl_id_2=None):
        """
        Estimate the cost (additional duration or distance) involved for using a specific platoon line for a trip entry in the self.df. 
        
        Parameters:
            index (int): to specify entry in the dataframe
            line_id (int): id of PlatoonLine used to calculate detour cost for
            cost (str): specify the cost metric (tavel_time or length)
            
        Returns:
            cost (int): cost (duration or distance) of taking the line. seconds if weight='tavel_time'. meters if weight='length'
            cl_id (int): id of starting ChainLocation
            cl_id (int): id of ending ChainLocation
            eta: estimated arrival time at starting ChainLocation
        """
        if line_id == -1:
            # in case line_id was -1, which means no line is being used
            return 0, -1, -1, None
        
        line = self.lines[line_id]
        
        
        if (cl_id_1 is not None) and (cl_id_2 is not None):
            cl_ids_provided = True
            cl_1_node = self.chaining_locations[cl_id_1].start_node
            cl_2_node = self.chaining_locations[cl_id_2].start_node
        else:
            cl_ids_provided = False 
            cl_id_1, cl_id_2 = line.chaining_location_ids
            cl_1_node = self.chaining_locations[cl_id_1].start_node
            cl_2_node = self.chaining_locations[cl_id_2].start_node
        
        entry = self.df.iloc[index]
        start_node = entry['PickUpNodeOSMNX']
        end_node = entry['DropOffNodeOSMNX'] 
        
        # 1st way with line
        _, _, distance1, duration1 = get_osmnx_route(start_node, cl_1_node, self.G, AVG_PASSENGER_CAB_SPEED, cost)  # distance and duration from PU to Chaining Location
        _, _, distance2, duration2 = get_osmnx_route(end_node, cl_2_node, self.G, AVG_PASSENGER_CAB_SPEED, cost)  # distance and duration from Unchaining Location to DO
        if cost == "length":        
            total_cost_1 = int(distance1 + distance2)
        if cost == "tavel_time":        
            total_cost_1 = int(duration1 + duration2)
        eta_1 = entry["PickUpStartTime"] + timedelta(seconds=duration1)
        
        total_cost_2 = 10e15
        # if the 2nd way with line still need to be checked       
        if cl_ids_provided == False:
            _, _, distance1, duration1 = get_osmnx_route(start_node, cl_2_node, self.G, AVG_PASSENGER_CAB_SPEED, cost)
            _, _, distance2, duration2 = get_osmnx_route(end_node, cl_1_node, self.G, AVG_PASSENGER_CAB_SPEED, cost)
            if cost == "length":        
                total_cost_2 = int(distance1 + distance2)
            if cost == "tavel_time":        
                total_cost_2 = int(duration1 + duration2)
            eta_2 = entry["PickUpStartTime"] + timedelta(seconds=duration1)
        
        # determine output, based on which line direction is best 
        if total_cost_1 < total_cost_2:
            return total_cost_1, cl_id_1, cl_id_2, eta_1
        else:
            return total_cost_2, cl_id_2, cl_id_1, eta_2
   
    def add_line_between_coords(self, start, end, via=None, verbose: bool=False):
        """
        Adds a PlatoonLine Object to the self.lines 
        
        Parameterss:
        start (tuple): Starting point as (lon, lat) coordinates.
        end (tuple): Ending point as (lon, lat) coordinates.
        via (list of tuple): Waypoints to pass through, as coordinates
        """
        if via is None:
            via = []
        
        # get ids for new objects
        # Safe default ID creation
        line_id = max(self.lines.keys(), default=0) + 1
        cl_1_id = max(self.chaining_locations.keys(), default=0) + 1
        cl_2_id = cl_1_id + 1
        
        combined_coords: list = [start] + via + [end]
        lons, lats = zip(*combined_coords)
        
        combined_nodes = ox.distance.nearest_nodes(self.G, X=lons, Y=lats)
        orig_node, *via_nodes, dest_node = combined_nodes

        chain_route_1 = ChainRoute(cl_1_id, cl_2_id, orig_node, dest_node, self.G, via_nodes=via_nodes)
        # Make a chain route using the exact same CLs, but different types
        chain_route_2 = copy.copy(chain_route_1)
        chain_route_2.reverse_route(self.G)

        self.chaining_locations[chain_route_1.CL1.id] = chain_route_1.CL1
        self.unchaining_locations[chain_route_1.UCL1.id] = chain_route_1.UCL1
        self.chaining_locations[chain_route_1.CL2.id] = chain_route_1.CL2
        self.unchaining_locations[chain_route_1.UCL2.id] = chain_route_1.UCL2
        
        line = PlatoonLine(line_id)
        line.chain_route_1, line.chain_route_2 = chain_route_1, chain_route_2 
        line.chaining_location_ids = [cl_1_id, cl_2_id]
        self.lines[line_id] = line
        if verbose is True:
            print(f"Added manual line between {start} and {end} with ID: {line.id}.")
            
    def determine_lines_from_cluster_info(self, min_distance: int, routing_method: str="OSMNX"):
        """
        Uses the biderectional flow (essentially and OD matrix) of trips between clusters to determine lines.
        Lines then have the locations of their chaining_locations calculated based on the cluster centers.
        
        Parameters:
            min_distance (int): minimum distance (meters) between cluster to be used for a line
            routing_method (str): Routing method to use when building routes between cluster centers. Default is "OSMNX".
        """
                
        # clear existing lines
        self.lines: dict[int, PlatoonLine] = {}
        
        # determine cluster centers that can easily be travelled between
        close_cluster_dict = self.find_close_clusters(metric='distance', threshold=CLOSE_CLUSTER_DISTANCE_THRESHOLD)
        
        # get a dataframe of bidirectional flow
        trip_freq_df = self.df.groupby(['PickUpCluster','DropOffCluster'],  observed=True).size().reset_index(name="TripCount")
        trip_freq_df.sort_values('TripCount', ascending=False)
        
        trip_freq_df["ClusterPair"] = trip_freq_df.apply(
            lambda row: tuple(sorted((row["PickUpCluster"], row["DropOffCluster"]))), axis=1
        )
        
        trip_freq_df = trip_freq_df[
            (trip_freq_df["PickUpCluster"] != -1) & (trip_freq_df["DropOffCluster"] != -1)
        ]
        
        bidirectional_flows = trip_freq_df.groupby("ClusterPair")["TripCount"].sum().reset_index()
        bidirectional_flows = bidirectional_flows.sort_values("TripCount", ascending=False).reset_index(drop=True)
        bidirectional_flows[["ClusterA", "ClusterB"]] = pd.DataFrame(bidirectional_flows["ClusterPair"].tolist(), index=bidirectional_flows.index)
        bidirectional_flows = bidirectional_flows[["ClusterA", "ClusterB", "TripCount"]]
         
        checked_cluster_pairs = set()
        
        # use bidirectional flow between cluster pairs to build lines 
        
        for _, entry in bidirectional_flows.iterrows():
            
            # get initial cluster pair
            cluster_a = entry['ClusterA']
            cluster_b = entry['ClusterB']
            
            # skip if going to/from the same cluster
            if cluster_a == cluster_b:
                continue
            
            # skip if this cluster pair have already been added to a line 
            sorted_cluster_pair = tuple(sorted((cluster_a, cluster_b)))
            if sorted_cluster_pair in checked_cluster_pairs:
                continue
            
            # get close clusters
            cluster_1 = close_cluster_dict[cluster_a]
            cluster_2 = close_cluster_dict[cluster_b]
            
            # generate all combinations of cluster pairs that are similar to the initial combination
            combinations = []
            for a in cluster_1:
                for b in cluster_2:
                    new_combi = tuple(sorted((a,b)))
                    if new_combi not in checked_cluster_pairs:
                    # only add if this cluster pair has not already been addded to a line 
                        combinations.append(new_combi)
            
            # Generate a Line 
            new_line = PlatoonLine(id = (len(self.lines)+1))
              
            for combi in combinations:
                # add to checked pairs
                checked_cluster_pairs.add(combi)          
                
                # get amount of trips between these clusters
                result = bidirectional_flows[(bidirectional_flows['ClusterA'] == combi[0]) & (bidirectional_flows['ClusterB'] == combi[1])]
                
                #  skip if there are no trips between these clusters
                if result.empty:
                    continue
                
                # skip if the distance between the clusters are too small, dont consider it for a line
                if min(self.cluster_travel_distances[combi[0]][combi[1]], self.cluster_travel_distances[combi[1]][combi[0]]) < min_distance:
                    continue
                
                new_line.cluster_1.append(combi[0])
                new_line.cluster_2.append(combi[1])
                new_line.trip_count.append(int(result.iloc[0]['TripCount']))

            # keep 'valid' lines
            if new_line.has_clusters():
                self.lines[new_line.id] = new_line

        # determine the CLs for lines
        self.determine_chaining_locations_from_line_info(routing_method=routing_method)     
    
    def determine_chaining_locations_from_line_info(self, routing_method: str="OSMNX"):
        """
        Create ChainingLocation Objects (to store in self.chaining_location and self.unchaining_locations) by creating CahinRoute Objetcs (determining the line routes) for the created lines (self.lines).
        
        Parameters:
            routing_method (str): Routing method to use when building routes between cluster centers. Default is "OSMNX".
        """
        
        # reset the variables
        self.chaining_locations: dict[str, ChainingLocation] = {}
        self.unchaining_locations: dict[str, ChainingLocation] = {}
        
        # list of node ids already used for chaining locations, to prevent CLs being placed on the same egde
        occupied_nodes = []
        
        failed_line_ids = [] 
        for line_id, line in self.lines.items():
            
            # get center between starting clusters of line, the weighted average
            coord_sum = (0, 0)
            total_size = 0
            for cluster_id in set(line.cluster_1):
                cluster_coords = self.point_clusters[cluster_id].center
                cluster_size = self.point_clusters[cluster_id].size
                total_size += cluster_size
                coord_sum = (coord_sum[0] + cluster_coords[0]*cluster_size, coord_sum[1] + cluster_coords[1]*cluster_size) 
        
            start_lon_lat = (coord_sum[0] / total_size, coord_sum[1] / total_size) # line start end coords
            
            # get center between ending clusters of line, the weighted average
            coord_sum = (0, 0)
            total_size = 0
            for cluster_id in set(line.cluster_2):
                cluster_coords = self.point_clusters[cluster_id].center
                cluster_size = self.point_clusters[cluster_id].size
                total_size += cluster_size
                coord_sum = (coord_sum[0] + cluster_coords[0]*cluster_size, coord_sum[1] + cluster_coords[1]*cluster_size) 
        
            end_lon_lat = (coord_sum[0] / total_size, coord_sum[1] / total_size) # line end coords
            
            start_lon, start_lat = start_lon_lat
            end_lon, end_lat = end_lon_lat
            
            orig_node, dest_node = ox.distance.nearest_nodes(self.G, X=[start_lon, end_lon], Y=[start_lat,end_lat]) # get node ids of coords
            
            # ensure start and end nodes are not used by other line for CLs
            if orig_node in occupied_nodes:
                orig_node = find_nearest_node_at_least_x_away(self.G, orig_node, occupied_nodes, 2)
            
            if dest_node in occupied_nodes:
                dest_node = find_nearest_node_at_least_x_away(self.G, dest_node, occupied_nodes, 2)
            
            cl_id1 = len(self.chaining_locations)+1
            cl_id2 = cl_id1 + 1
            
            # Determie routes between chaining locations
            chain_route_1 = ChainRoute(cl_id1, cl_id2, orig_node, dest_node, self.G, occupied_nodes)
            
            if chain_route_1.valid is False:
                # failed to find a valid route
                failed_line_ids.append(line_id)
                continue
            
            # Make a chain route using the exact same CLs, but different types (Unchain, becomes Chain and vice versa)
            chain_route_2 = copy.copy(chain_route_1)
            chain_route_2.reverse_route(self.G)
            
            # save un/chaining locations
            self.chaining_locations[chain_route_1.CL1.id] = chain_route_1.CL1
            self.unchaining_locations[chain_route_1.UCL1.id] = chain_route_1.UCL1
            
            self.chaining_locations[chain_route_1.CL2.id] = chain_route_1.CL2
            self.unchaining_locations[chain_route_1.UCL2.id] = chain_route_1.UCL2
            
            # update occupied_nodes
            occupied_nodes.extend([chain_route_1.CL1.start_node,
                                   chain_route_1.CL1.end_node,
                                   chain_route_1.CL2.start_node,
                                   chain_route_1.CL2.end_node])
                   
            # add CR objects to line 
            line.chain_route_1 = chain_route_1
            line.chain_route_2 = chain_route_2
            # add CL ids to line
            line.chaining_location_ids = [cl_id1, cl_id2]
            
        # remove invalid lines
        for line_id in failed_line_ids:
            # remove line from self.lines
            self.lines.pop(line_id, None) 
            
    def find_close_clusters(self, metric='distance', threshold=1000) -> dict:
        """
        Identify clusters that are within a specified threshold of travel distance or duration 
        from each other, based on pairwise travel metrics between cluster centers.

        Parameters:
            metric (str): The travel metric to use when comparing clusters. 
                        Can be either 'distance' (default) or 'duration'.
            threshold (int): The maximum allowed travel distance or duration (depending on metric) 
                            between clusters for them to be considered "close".

        Returns:
            dict: A dictionary where each key is a cluster ID and the value is a list of cluster IDs 
                that are within the threshold from it, including itself.
        """
        
        similar_dic = {key: [key] for key in self.point_clusters}
        
        for query_cluster_id in similar_dic:
            
            for candidate_cluster_id in range(query_cluster_id+1, len(similar_dic)):
                
                if metric == 'duration':
                    to_d = self.cluster_travel_times[query_cluster_id][candidate_cluster_id]
                    from_d = self.cluster_travel_times[candidate_cluster_id][query_cluster_id]
                else: # default 
                    to_d = self.cluster_travel_distances[query_cluster_id][candidate_cluster_id]
                    from_d = self.cluster_travel_distances[candidate_cluster_id][query_cluster_id]

                if max(to_d, from_d) <= threshold:
                    similar_dic[query_cluster_id].append(candidate_cluster_id)
                    similar_dic[candidate_cluster_id].append(query_cluster_id)

        return similar_dic
     
    def cluster_pus_and_dos_with_hdbscan(self, min_cluster_size=25, max_cluster_size=100, cluster_selection_epsilon=25):
        """
        Clusters all PU and DO locations using HDBSCAN and assigns each trip a corresponding pickup and dropoff cluster.

        This method applies HDBSCAN to spatially cluster both pickup and dropoff points from the dataset.
        Trips that fall outside any cluster (i.e. labeled as noise) are assigned to the nearest valid cluster.
        Each trip is annotated with a `PickUpCluster` and `DropOffCluster`.

        Parameters:
            min_cluster_size (int): Minimum number of points required to form a cluster.
            max_cluster_size (int): Maximum allowable size of a cluster to avoid overly large regions.
            cluster_selection_epsilon (float): Distance threshold (in meters) to merge nearby clusters 
                                            and assign noise points to the nearest cluster.

        Returns:
            None: Updates the internal dataframe with `PickUpCluster` and `DropOffCluster` columns.
        """
        
        # reset the variables
        self.point_clusters = {} # key = cluster label, value = (lont, lat)
        
        # Extract coordinates
        transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        pu_coords = list(zip(self.df["PickUpLongitude"], self.df["PickUpLatitude"]))
        do_coords = list(zip(self.df["DropOffLongitude"], self.df["DropOffLatitude"]))
        projected_pu_coords = [transformer.transform(lon, lat) for lon, lat in pu_coords]
        projected_do_coords = [transformer.transform(lon, lat) for lon, lat in do_coords]
             
        params = {'min_cluster_size': min_cluster_size,
                  'max_cluster_size': max_cluster_size,
                  'cluster_selection_epsilon': cluster_selection_epsilon,
                  'store_centers': 'centroid',
                  }
        all_coords = projected_pu_coords + projected_do_coords
        hdb = HDBSCAN(**params).fit(all_coords)
        all_labels = hdb.labels_
        pu_labels = all_labels[:len(projected_pu_coords)]
        do_labels = all_labels[len(projected_pu_coords):]
        
        self.df["PickUpCluster"] = pu_labels
        self.df["DropOffCluster"] = do_labels
        
        # Assign noisy PU points to nearest cluster
        for idx, label in enumerate(pu_labels):
            if label != -1:
                continue
            point = projected_pu_coords[idx]
            min_dist = 1000 # point must be within 'min_dist' m from cluster center
            nearest_cluster_id = -1
            for cluster_id, cluster_data in self.point_clusters.items():
                centroid = cluster_data.center
                dist = np.linalg.norm(np.array(point) - np.array(centroid))
                if dist < min_dist:
                    min_dist = dist
                    nearest_cluster_id = cluster_id
            self.df.at[idx, "PickUpCluster"] = nearest_cluster_id
            
        # Assign noisy DO points to nearest cluster
        for idx, label in enumerate(do_labels):
            if label != -1:
                continue
            point = projected_do_coords[idx]
            min_dist = 1000 # point must be within 'min_dist' m from cluster center
            nearest_cluster_id = -1
            for cluster_id, cluster_data in self.point_clusters.items():
                centroid = cluster_data.center
                dist = np.linalg.norm(np.array(point) - np.array(centroid))
                if dist < min_dist:
                    min_dist = dist
                    nearest_cluster_id = cluster_id
            self.df.at[idx, "DropOffCluster"] = nearest_cluster_id
                
        self.df["PickUpCluster"] = self.df["PickUpCluster"].astype('category')
        self.df["DropOffCluster"] = self.df["DropOffCluster"].astype('category')
        
        # Combine all coordinates and labels into one DataFrame
        lon_all = list(self.df["PickUpLongitude"]) + list(self.df["DropOffLongitude"])
        lat_all = list(self.df["PickUpLatitude"]) + list(self.df["DropOffLatitude"])
        clusters_all = list(self.df["PickUpCluster"]) + list(self.df["DropOffCluster"])
        
        temp_df = pd.DataFrame({
            "lon": lon_all,
            "lat": lat_all,
            "cluster": clusters_all
        })

        # Exclude noise (-1)
        temp_df = temp_df[temp_df["cluster"] != -1]

        # group by cluster and calculate mean coordinates and size 
        grouped = temp_df.groupby("cluster")

        # Save cluster info
        for cluster_id, group in grouped:
            new_PC = PointCluster(center=(group["lon"].mean(), group["lat"].mean()),
                                  size=len(group)
                                  )
            self.point_clusters[cluster_id] = new_PC
            
    def generate_line_schedule_with_max_frequency(self, line_id=1, acceptable_wait=300, max_chain=3, service_start=None, service_end=None)->tuple[ProTimeTable, float]:
        """
        Determines the schedule of Pro-cab trips between ChainingLocations of a line.
        
        Parameters:
        - line_id: id of line to determine schedule fos
        - acceptable_wait (int): seconds that cabs can wait at a chaining_location
        - max_chain (int): maximum number of cabs that can chain to a convoy 
        - service_start (str|time|None): optional lower bound for departure times
        - service_end (str|time|None): optional upper bound for departure times
                
        Returns:
        - pro_time_table (ProTimeTable)
        - max_frequency (int): seconds between departures from the same CL
        """
        
        line_trips = self.df[self.df['Line'] == line_id]
        
        if line_trips.empty:
            return None,None
        
        line_obj = self.lines[line_id]
        chaining_location1, chaining_location2 = line_obj.chaining_location_ids
        trip_duration1 = line_obj.chain_route_1.duration # seconds
        trip_duration2 = line_obj.chain_route_2.duration # seconds
        
        # calculate duration of round-trip constraint 
        turnaround_time = (trip_duration1 + trip_duration2 + 2*acceptable_wait) # seconds
        min_headway = max(4*acceptable_wait, int(turnaround_time)) # min heady should at least be 4*acceptable_wait
        
        trips = line_trips[line_trips['StartChainingLocation'].isin([chaining_location1, chaining_location2])]
        trips = trips.sort_values(by='EstimatedTimeAtStartChainingLocation')

        service_start_t = None
        service_end_t = None
        def normalize_service_time(value):
            if isinstance(value, time):
                return value
            return pd.to_datetime(value).time()
        if (service_start is None) != (service_end is None):
            raise ValueError("Both 'service_start' and 'service_end' must be provided together.")

        if service_start is not None and service_end is not None:
            service_start_t = normalize_service_time(service_start)
            service_end_t = normalize_service_time(service_end)
            if service_start_t > service_end_t:
                raise ValueError("'service_start' must be earlier than or equal to 'service_end'.")
            trips = trips[
                trips["EstimatedTimeAtStartChainingLocation"].dt.time.between(service_start_t, service_end_t)
            ]
            if trips.empty:
                return None, None
        
        earliest_trip = trips.loc[trips['EstimatedTimeAtStartChainingLocation'].idxmin()]
        latest_trip   = trips.loc[trips['EstimatedTimeAtStartChainingLocation'].idxmax()]
        
        def round_up_to_next_interval(dt, interval=5) -> datetime:
                discard = timedelta(minutes=dt.minute % interval,
                                    seconds=dt.second,
                                    microseconds=dt.microsecond)
                return dt + (timedelta(minutes=interval) - discard) if discard != timedelta(0) else dt
                
        # create ptt / 1st departure
        depart_time = round_up_to_next_interval(earliest_trip['EstimatedTimeAtStartChainingLocation'], 5)
        if service_start_t is not None and depart_time.time() < service_start_t:
            depart_time = depart_time.replace(
                hour=service_start_t.hour,
                minute=service_start_t.minute,
                second=service_start_t.second,
                microsecond=0
            )
        if service_end_t is not None and depart_time.time() > service_end_t:
            return None, None
        start_cl = earliest_trip['StartChainingLocation']
        end_cl = earliest_trip['EndChainingLocation']
        trip_duration = trip_duration1 if start_cl == chaining_location1 else trip_duration2
        arrival_time = depart_time + (timedelta(seconds=trip_duration))
        if service_end_t is not None and arrival_time.time() > service_end_t:
            return None, None
        ptt = ProTimeTable(1, depart_time, start_cl, arrival_time, end_cl)
        
        last_trip_served = False
        while last_trip_served is False:
            depart_time = arrival_time + (timedelta(seconds=acceptable_wait))
            start_cl, end_cl = end_cl, start_cl
            trip_duration = trip_duration1 if start_cl == chaining_location1 else trip_duration2
            arrival_time = depart_time + (timedelta(seconds=trip_duration))
            if service_end_t is not None and (depart_time.time() > service_end_t or arrival_time.time() > service_end_t):
                break
            ptt.append_entries_to_schedules(depart_time, start_cl, arrival_time, end_cl)
            if depart_time >= round_up_to_next_interval(latest_trip['EstimatedTimeAtStartChainingLocation'], 5) and start_cl == latest_trip['StartChainingLocation']:
                # stop adding to the schedule after the last trip is served
                last_trip_served = True
        
        # Return only time portion
        return ptt, min_headway

    def generate_line_schedules_with_max_frequency(self, line_id=1, acceptable_wait=300, max_chain=3, available_pros: int=1, service_start=None, service_end=None)->tuple[list[ProTimeTable], float]:
        """
        Determines fixed-frequency schedules for one or multiple Pro-cabs on a line.

        For `available_pros > 1`, additional schedules are phase-shifted versions of the base
        schedule, with offset `round_trip_duration / available_pros`.
        """
        if available_pros is None:
            available_pros = 1

        available_pros = int(available_pros)
        if available_pros < 1:
            raise ValueError("'available_pros' must be at least 1.")

        base_pro_time_table, min_headway = self.generate_line_schedule_with_max_frequency(
            line_id=line_id,
            acceptable_wait=acceptable_wait,
            max_chain=max_chain,
            service_start=service_start,
            service_end=service_end
        )

        if base_pro_time_table is None:
            return [], min_headway

        line_obj = self.lines[line_id]
        round_trip_duration = line_obj.chain_route_1.duration + line_obj.chain_route_2.duration + 2 * acceptable_wait
        phase_offset_seconds = round_trip_duration / available_pros

        def normalize_service_time(value):
            if value is None:
                return None
            if isinstance(value, time):
                return value
            return pd.to_datetime(value).time()

        service_start_t = normalize_service_time(service_start)
        service_end_t = normalize_service_time(service_end)

        pro_time_tables = []
        for i in range(available_pros):
            offset = timedelta(seconds=phase_offset_seconds * i)
            shifted_entries = []

            for depart_time, chain_id, arrival_time, unchain_id in base_pro_time_table:
                shifted_depart = depart_time + offset
                shifted_arrival = arrival_time + offset
                if service_start_t is not None and service_end_t is not None:
                    if not (
                        service_start_t <= shifted_depart.time() <= service_end_t
                        and service_start_t <= shifted_arrival.time() <= service_end_t
                    ):
                        continue
                shifted_entries.append((shifted_depart, chain_id, shifted_arrival, unchain_id))

            if not shifted_entries:
                continue

            first_depart, first_chain, first_arrival, first_unchain = shifted_entries[0]
            ptt = ProTimeTable((i + 1), first_depart, first_chain, first_arrival, first_unchain)
            for depart_time, chain_id, arrival_time, unchain_id in shifted_entries[1:]:
                ptt.append_entries_to_schedules(depart_time, chain_id, arrival_time, unchain_id)

            pro_time_tables.append(ptt)

        return pro_time_tables, min_headway

    def add_line_related_timing_columns(self) -> None:
        """
        Adds columns to self.df that specify when a trip entry is epexcted to arrive at its assigned StartChainingLocation, EndChainingLocation and the DropOff when using its assigned line.
        """
        self.add_estimated_time_at_start_chaining_location_column()
        self.add_estimated_time_at_end_chaining_location_column()
          
    def add_estimated_time_at_start_chaining_location_column(self, routing_method="OSMNX") -> None:
        """
        Adds a column to self.df that specifies when a trip entry is estimated to arrive at its assigned start chaining location
        
        ## See if speedup is possible
        """
        if routing_method == "GraphHopper":
            # TODO
            pass
        else:
            results = self.df.index.to_series().apply(lambda idx: self.calculate_metrics_to_start_chaining_from_pickup(idx))
            duration_results, _ = zip(*results)
        
        # self.df['DistanceToStartChainingLocationFromPickUp'] = distance_results
        estimated_pickup_time = self.df["PickUpStartTime"] + (self.df["PickUpEndTime"] - self.df["PickUpStartTime"]) / 2
        self.df["EstimatedTimeAtStartChainingLocation"] = estimated_pickup_time + pd.to_timedelta(duration_results, unit='s')
    
    def add_estimated_time_at_end_chaining_location_column(self, routing_method="OSMNX") -> None:
        """
        Adds a column to self.df that specifies when a trip entry is estimated to arrive at its assigned end chaining location
        """
        convoy_delay = 60 # time to detach
        
        time_results = self.df.index.to_series().apply(lambda idx: self.get_earliest_arrival_at_end_chaining_location(idx))
        
        # TODO
        if routing_method == "GraphHopper":
            # TODO
            pass
        else:
            results = self.df.index.to_series().apply(lambda idx: self.calculate_metrics_to_dropoff_from_end_chaining(idx))
            duration_results, _ = zip(*results)
        
        # self.df['DistanceToDropOffFromEndChainingLocation'] = distance_results
        self.df["EarliestDropOffTimeWithLine"] = time_results + pd.to_timedelta(duration_results, unit='s') + timedelta(seconds=(convoy_delay))
    
    def get_earliest_arrival_at_end_chaining_location(self, index):
        """
        Determine the earliest arrival of a trip entry at its end chaining_location. 
        
        Parameters:
        - index (int): to specify entry in the dataframe
        
        Returns:
        - time (datetime): earliest possible time the request can get to the end chaining_location
        """
        
        convoy_delay = 60 # delay to attach
        entry = self.df.iloc[index]
        line_id = entry['Line']
        if line_id == -1:
            return np.nan
        line = self.lines[line_id]
        start_chaining_location_id = entry['StartChainingLocation']
        index = line.chaining_location_ids.index(start_chaining_location_id)
        if index == 0:
            duration = line.chain_route_1.duration
        else:
            duration = line.chain_route_2.duration
        time = entry['EstimatedTimeAtStartChainingLocation'] + timedelta(seconds=(duration + convoy_delay)) 
        return time
    
    def print_line_info(self, line_ids=None):
        """
        Prints the string representation of the selected ProLine Objects.
        """
        if line_ids == None:
            line_ids = self.lines.keys()
            
        for id in line_ids:
            print(self.lines.get(id, ""), "\n", "-"*150)
            
    def generate_chain_route_schedules2(self, line_ids=None, service_start=None, service_end=None, pros_per_line=None):
        """
        Generate the ChainRouteSchedules list for a BaseData JSON file using fixed-frequency schedules.
        """
        if not line_ids:
            line_ids = list(self.lines.keys())

        def normalize_service_time(value):
            if isinstance(value, time):
                return value
            return pd.to_datetime(value).time()

        if (service_start is None) != (service_end is None):
            raise ValueError("Both 'service_start' and 'service_end' must be provided together.")

        service_start_t = normalize_service_time(service_start) if service_start is not None else None
        service_end_t = normalize_service_time(service_end) if service_end is not None else None
        if service_start_t is not None and service_start_t > service_end_t:
            raise ValueError("'service_start' must be earlier than or equal to 'service_end'.")

        if pros_per_line is not None:
            if len(pros_per_line) != len(line_ids):
                raise ValueError("'pros_per_line' length must match the number of selected lines.")
            for count in pros_per_line:
                if int(count) < 1:
                    raise ValueError("Each value in 'pros_per_line' must be at least 1.")
        
        # track number of pros used 
        num_pros = 0
        
        chain_route_schedules = []
        
        # count number of time each chain route is used, such that GUID can be made
        chain_route_count = {}
        
        for index, id in enumerate(line_ids):
            pros_available = 1
            if pros_per_line is not None:
                pros_available = int(pros_per_line[index])

            pro_time_tables, _ = self.generate_line_schedules_with_max_frequency(
                line_id=id,
                acceptable_wait=300,
                max_chain=3,
                available_pros=pros_available,
                service_start=service_start_t,
                service_end=service_end_t
            )

            if not pro_time_tables:
                continue

            for pro_time_table in pro_time_tables:
                pro_entries = []
                for depart_time, chain_id, arrival_time, unchain_id in pro_time_table:
                    if service_start_t is not None and not (
                        service_start_t <= depart_time.time() <= service_end_t
                        and service_start_t <= arrival_time.time() <= service_end_t
                    ):
                        continue
                    chain_route_name = f"Station{chain_id}_Station{unchain_id}"

                    if chain_route_name not in chain_route_count:
                        chain_route_count[chain_route_name] = 1
                    else:
                        chain_route_count[chain_route_name] += 1

                    pro_entries.append({
                        "Guid": f"{chain_route_name}_{chain_route_count[chain_route_name]}",
                        "ChainRoute": chain_route_name,
                        "Departure": depart_time,
                        "Arrival": arrival_time
                    })

                if not pro_entries:
                    continue

                num_pros += 1
                for entry in pro_entries:
                    entry["ProSchedule"] = f"Pro{num_pros}"
                    chain_route_schedules.append(entry)
        
        # create a dataframe of the schedules to sort them chronologically
        if not chain_route_schedules:
            # no schedules for lines exists
            print("ChainRouteSchedules is empty. Likely due to no requests being assigned to the selected lines.")
            return chain_route_schedules
        
        chain_route_schedules_df = pd.DataFrame(chain_route_schedules)   
        chain_route_schedules_df = chain_route_schedules_df.sort_values(by="Departure")
        # format timestamps
        chain_route_schedules_df['Departure'] = chain_route_schedules_df['Departure'].apply(lambda x: x.isoformat(timespec='seconds'))
        chain_route_schedules_df['Arrival'] = chain_route_schedules_df['Arrival'].apply(lambda x: x.isoformat(timespec='seconds'))
        
        # convert sorted dataframe back to list of dictionaries
        chain_route_schedules = chain_route_schedules_df.to_dict(orient='records')
        
        return chain_route_schedules #, chain_rout_schedules_df
    
    def generate_chain_route_schedules(self, line_ids=None, avg_cutoff: float=0.1, pros_per_line=None):
        """
        Generate the ChainRouteSchedules list for a BaseData JSON file using generate_line_schedule()
        """
        if not line_ids:
            line_ids = list(self.lines.keys())
            
        num_pros = 0
        
        
        chain_route_schedules = []
        
        # count number of time each chain route is used, such that GUID can be made
        chain_route_count = {}
        
        for index, id in enumerate(line_ids):
            pros_available = 1
            if pros_per_line:
                pros_available = pros_per_line[index]
                
            pro_time_tables = self.generate_line_schedule(id, avg_cutoff, pros_available)
            
            # current_line = self.lines[id]
            
            for protime_table in pro_time_tables:
                num_pros += 1
                
                for depart_time, chain_id, arrival_time, unchain_id in protime_table:
                    chain_route_name = f"Station{chain_id}_Station{unchain_id}"
                    
                    if chain_route_name not in chain_route_count:
                        chain_route_count[chain_route_name] = 1
                    else:
                        chain_route_count[chain_route_name] += 1
                    
                    entry={
                        "Guid": f"{chain_route_name}_{chain_route_count[chain_route_name]}",
                        "ChainRoute": chain_route_name,
                        "ProSchedule": f"Pro{num_pros}",
                        "Departure": depart_time,
                        "Arrival": arrival_time
                    }
                    
                    chain_route_schedules.append(entry)
        
        # create a dataframe of the schedules to sort them chronologically
        chain_rout_schedules_df = pd.DataFrame(chain_route_schedules)   
        chain_rout_schedules_df = chain_rout_schedules_df.sort_values(by="Departure")
        # format timestamps
        chain_rout_schedules_df['Departure'] = chain_rout_schedules_df['Departure'].apply(lambda x: x.isoformat(timespec='seconds'))
        chain_rout_schedules_df['Arrival'] = chain_rout_schedules_df['Arrival'].apply(lambda x: x.isoformat(timespec='seconds'))
        
        # convert sorted dataframe back to list of dictionaries
        chain_route_schedules = chain_rout_schedules_df.to_dict(orient='records')
        
        return chain_route_schedules #, chain_rout_schedules_df
                
    def generate_line_schedule(self, line_id, avg_cutoff: float=0.1, available_pros: int=1)->list[ProTimeTable]:
        """
        Returns a schedule of platoon trips for a given line based on request density and vehicle availability.

        A departure time (platoon trip) is added to the schedule whenever the average number of requests within 
        a time window exceeds the specified threshold (`avg_cutoff`). The function ensures that no more than 
        `available_pros` vehicles are scheduled simultaneously for this line.

        Parameters:
            line_id: The identifier of the line for which the schedule is generated.
            avg_cutoff (float, optional): The average requests threshold for triggering a departure. Default is 0.1.
            available_pros (int, optional): The number of ProVehicles available for this line. Default is 1.
        
        Returns:
            pro_time_tables (list): A list of ProTimeTable objetcs. Each entry in the list represents the generated schedule of a single pro for the line.
        """

        max_covoy = 10e10           # maximum number of cabs that can in a convoy
        min_gap = max_covoy*60+60   # duration (sec) needed between departures from the same ChainingLocation
        acceptable_wait = 5         # duration (minutes) 
        
        if available_pros is None:
            available_pros=1
        
        line = self.lines[line_id]
        avg_arrivals_cl1 = self.generate_time_table_for_chaining_location(line.chaining_location_ids[0], line.chaining_location_ids[1], acceptable_wait)
        avg_arrivals_cl2 = self.generate_time_table_for_chaining_location(line.chaining_location_ids[1], line.chaining_location_ids[0], acceptable_wait)
        combined_avg_arrivals = pd.concat([avg_arrivals_cl1, avg_arrivals_cl2], ignore_index=True)
        combined_avg_arrivals = combined_avg_arrivals.sort_values(by="TimeBinStart", ascending=True)
        
        chaining_location_1_last_departure_time = None
        chaining_location_2_last_departure_time = None
        
        complete_table = []
        
        pro_time_tables = []
        
        for i, entry in combined_avg_arrivals.iterrows():
            entry_avg_arrival = entry['AvgArrivals']
            entry_start_time = entry['TimeBinStart']
            entry_start_chaining_location_id = int(entry['StartChainingLocation'])
            entry_end_chaining_location_id = int(entry['EndChainingLocation'])
            
            if entry_avg_arrival < avg_cutoff:
                # Not enough demand to justify having a departure time
                continue
            
            if i == 0:
                # deal with 1st case, at least 1 pro is needed
                if entry_avg_arrival > max_covoy:
                    # how to deal with times where demand exceeds max convoy size
                    pass
                else:
                    if len(pro_time_tables) < available_pros:
                        depart_time = entry_start_time + timedelta(minutes=acceptable_wait)
                        
                        earliest_arrival_at_destination = line.get_arrival_time(depart_time, entry_start_chaining_location_id, entry_end_chaining_location_id)
                        
                        ptt = ProTimeTable((len(pro_time_tables)+1), depart_time, entry_start_chaining_location_id, earliest_arrival_at_destination, entry_end_chaining_location_id)
                        # complete_table.append([1, depart_time, entry_chaining_location_id])
                        pro_time_tables.append(ptt)
                        continue
                        
            else:
                # deal with following cases
                if entry_avg_arrival > max_covoy:
                    # how to deal with times where demand exceeds max convoy size
                    pass
                else: 
                    depart_time = entry_start_time + timedelta(minutes=acceptable_wait)
                    
                    ptt_found = False
                    
                    for ptt_index, ptt in enumerate(pro_time_tables):
                        
                        # get when and where the pro departed from
                        last_depart_time, last_chaining_location_id, last_arrival_time, last_unchaining_location_id = ptt[-1]
                        
                        # get the time at which this pro can reach the station
                        earliest_arrival_for_depart = line.get_arrival_time(last_depart_time, last_chaining_location_id, entry_start_chaining_location_id)
                        
                        if last_chaining_location_id != entry_start_chaining_location_id:
                        # prioritise pros that are on their way to CL with id equal to entry_chaining_location_id
                            if earliest_arrival_for_depart < depart_time:
                                # this pro can depart in time
                                # get time when pro gets to destination
                                earliest_arrival_at_destination = line.get_arrival_time(depart_time, last_chaining_location_id, entry_start_chaining_location_id)
                                ptt.append_entries_to_schedules(depart_time, entry_start_chaining_location_id, earliest_arrival_at_destination, entry_end_chaining_location_id)
                                # complete_table.append([ptt_index+1, depart_time, entry_chaining_location_id])
                                ptt_found = True
                                # a suitable pro has been found, no need to continue
                                break
                        else:
                            # best option is having a pro come back immediatly
                            if earliest_arrival_for_depart < depart_time and not ptt_found:
                                
                                # add schedule for quick return trip
                                intermediate_depart_time = last_arrival_time + timedelta(minutes=1)
                                earliest_arrival_for_return_trip = line.get_arrival_time(intermediate_depart_time, last_unchaining_location_id, None)
                                ptt.append_entries_to_schedules(intermediate_depart_time, last_unchaining_location_id, earliest_arrival_for_return_trip, entry_start_chaining_location_id)
                                
                                # add schedule to serve cabs
                                # get time when pro gets to destination
                                earliest_arrival_at_destination = line.get_arrival_time(depart_time, entry_start_chaining_location_id, None)
                                ptt.append_entries_to_schedules(depart_time, entry_start_chaining_location_id, earliest_arrival_at_destination, entry_end_chaining_location_id)
                                # complete_table.append([ptt_index+1, depart_time, entry_chaining_location_id])
                                ptt_found = True
                                # a suitable pro has been found, no need to continue
                                break
                        
                    if not ptt_found:
                        # another pro is needed to deal with the demand
                        if len(pro_time_tables) < available_pros:
                            # another pro can be added to the line
                            earliest_arrival_at_destination = line.get_arrival_time(depart_time, entry_start_chaining_location_id, entry_end_chaining_location_id)
                            ptt = ProTimeTable((len(pro_time_tables)+1), depart_time, entry_start_chaining_location_id, earliest_arrival_at_destination, entry_end_chaining_location_id)
                            # complete_table.append([(len(pro_time_tables)+1), depart_time, entry_start_chaining_location_id])
                            pro_time_tables.append(ptt)
                        else:
                            pass # print(f"[DEBUG] Limit reached: {len(pro_time_tables)} >= {available_pros}, cannot add more pros.")

        
        return pro_time_tables #, combined_avg_arrivals
        
    def generate_time_table_for_chaining_location(self, start_chaining_location_id: int,  end_chaining_location_id: int, window_duration: int):
        """
        Get the average arrival at a given StartChainingLocation witin time windows across instances. 
        
        Parameters:
            start_chaining_location_id (int): identifier of ChainingLocation
            window_duration (int): duration in minutes, used to bin arrival
        
        Returns:
            avg_arrivals (dataframe): A dataframe specifiying the average arrivals withing time windows at the ChainingLocation
        """
        
        num_instances = self.df["Instance"].nunique()
        
         # reduce dataframe to just the single StartChainingLocation
        filtered_df = self.df[self.df['StartChainingLocation'] == start_chaining_location_id].copy()
        filtered_df["TimeBinStart"] = filtered_df["EstimatedTimeAtStartChainingLocation"].dt.floor(f"{window_duration}min")

        # count arrivals per bin per instance
        grouped = filtered_df.groupby(["Instance", "TimeBinStart"]).size().reset_index(name="Arrivals")

        # compute the average arrivals per bin across all instances
        avg_arrivals = grouped.groupby("TimeBinStart")["Arrivals"].sum().reset_index(name="AvgArrivals")

        avg_arrivals['AvgArrivals'] = avg_arrivals['AvgArrivals'] / num_instances
        
        avg_arrivals["StartChainingLocation"] = start_chaining_location_id
        avg_arrivals["EndChainingLocation"] = end_chaining_location_id
        
        return avg_arrivals
        
    def generate_ini_base_data(self, fpath, line_ids=None, pros_per_line=None, service_start=None, service_end=None):
        """
        Generate and export data to a JSON 'base data' file.

        This method collects chaining location data, chain routes, and chain route schedules
        for the specified line IDs (or all lines if none are provided), and exports the result
        as a structured JSON file.

        Parameters:
            fpath (str): Path to the output JSON file where the base data will be written.
            line_ids (list): List of line IDs to include in the export. If None, all available lines are used.
            pros_per_line (list): Optional number of pros per selected line for fixed-frequency scheduling.
            service_start (str, optional): Earliest allowed departure time (e.g. "05:00") for schedule generation.
            service_end (str, optional): Latest allowed departure time (e.g. "23:00") for schedule generation.

        Notes:
            This method uses `generate_chain_route_schedules2()` to create the schedule block, with a fixed frequency.
        """
        ini_base_data = {}
        
        filtered_lines = []
        
        if line_ids:
            filtered_lines = [self.lines[id] for id in line_ids]
        else:
            filtered_lines = list(self.lines.values())
        
        # add ChainingLocations, ChainRoutes
        chaining_locations_dicts = []
        chain_routes_dicts = []
        
        for i, line in enumerate(filtered_lines):
            
            # add chaining routes
            chain_routes_dicts.append(line.chain_route_1.to_dictionary())
            chain_routes_dicts.append(line.chain_route_2.to_dictionary())
            
            cl1_chain = line.chain_route_1.CL1
            cl1_unchain = line.chain_route_1.UCL1
            cl2_chain = line.chain_route_1.CL2
            cl2_unchain = line.chain_route_1.UCL2
            
            chaining_locations_dicts.extend([cl1_chain.to_dictionary(),
                                             cl1_unchain.to_dictionary(),
                                             cl2_chain.to_dictionary(),
                                             cl2_unchain.to_dictionary()
                                             ])
            
        ini_base_data["ChainingLocations"] =  chaining_locations_dicts
        ini_base_data["ChainRoutes"] = chain_routes_dicts
        # fixed frequency schedule
        original_df = self.df
        try:
            if (service_start is None) != (service_end is None):
                raise ValueError("Both 'service_start' and 'service_end' must be provided together.")

            if service_start is not None and service_end is not None:
                # Ensure timing column exists for consistent filtering
                if "EstimatedTimeAtStartChainingLocation" not in self.df.columns:
                    self.add_estimated_time_at_start_chaining_location_column()

                start_t = pd.to_datetime(service_start).time()
                end_t = pd.to_datetime(service_end).time()

                self.df = self.df[
                    self.df["EstimatedTimeAtStartChainingLocation"].dt.time.between(start_t, end_t)
                ].copy()

            ini_base_data["ChainRouteSchedules"] = self.generate_chain_route_schedules2(
                line_ids,
                service_start=service_start,
                service_end=service_end,
                pros_per_line=pros_per_line
            )
        finally:
            self.df = original_df
        # demand driven scheduling
        # ini_base_data["ChainRouteSchedules"] = self.generate_chain_route_schedules(line_ids=line_ids, avg_cutoff=avg_cut_off, pros_per_line=pros_per_line)
        # save to json
        with open(fpath, "w") as f:
            json.dump(ini_base_data, f, indent=4)
    
    ###########################################
    # Plotting methods 
    ###########################################
    
    def plot_pu_and_do_cluster(self):
        
        if "DropOffCluster" not in self.df.columns or "PickUpCluster" not in self.df.columns:
            self.cluster_pus_and_dos_with_hdbscan()
        
        # define color maps
        colors = px.colors.qualitative.Dark24

        cluster_labels = [key for key in self.point_clusters]
        cluster_labels.insert(0, -1)
        color_map = {label: colors[i % len(colors)] for i, label in enumerate(cluster_labels)}

        # strat plotting
        fig = go.Figure()

        # add PU trace
        for label in cluster_labels:
            df_pickup = self.df[self.df["PickUpCluster"] == label]
            fig.add_trace(go.Scattermap(
                lat=df_pickup["PickUpLatitude"],
                lon=df_pickup["PickUpLongitude"],
                mode="markers",
                marker=dict(size=10, color=color_map[label]),
                name=f"PickUp Cluster {label}",
                hoverinfo="text",
                hovertext=[f"Cluster {label} PickUps" for _ in range(len(df_pickup))]
            ))

        # add DO trace
        for label in cluster_labels:
            df_dropoff = self.df[self.df["DropOffCluster"] == label]
            fig.add_trace(go.Scattermap(
                lat=df_dropoff["DropOffLatitude"],
                lon=df_dropoff["DropOffLongitude"],
                mode="markers",
                marker=dict(size=10, color=color_map[label]),
                name=f"DropOff Cluster {label}",
                hoverinfo="text",
                hovertext=[f"Cluster {label} DropOffs" for _ in range(len(df_dropoff))]
            ))

        # update layout
        fig.update_layout(
            title=f"Clustered PU and DO points",
            template="plotly",  # Use plotly template for styling
            showlegend=True,  # Enable legend to toggle visibility of pickups and drop-offs
            xaxis_title="Longitude",
            yaxis_title="Latitude",
            width=900,
            height=650,
            map_center_lon=pd.concat([self.df["PickUpLongitude"], self.df["DropOffLongitude"]]).mean(),
            map_center_lat=pd.concat([self.df["PickUpLatitude"], self.df["DropOffLatitude"]]).mean(),
            map_zoom=12
        )

        fig.show()
    
    def plot_travel_time_between_clusters(self):
        # travel time between junctions
                
        fig = px.imshow(
            self.cluster_travel_times,
            labels=dict(x="To Cluster", y="From Cluster", color="Seconds"),
            x=list(self.point_clusters.keys()),  # columns
            y=list(self.point_clusters.keys()),   # rows
            color_continuous_scale='OrRd',
            text_auto=True
        )

        fig.update_layout(
            title="Trip Duration Between Clusters",
            xaxis_title="To Cluster",
            yaxis_title="From Cluster",
            height=600,
            width=600,
            xaxis=dict(
                tickmode='array',
                tickvals=list(self.point_clusters.keys()),
                ticktext=list(self.point_clusters.keys())
            ),
            yaxis=dict(
                tickmode='array',
                tickvals=list(self.point_clusters.keys()),
                ticktext=list(self.point_clusters.keys())
            ),
        )
        
        fig.show()
        
        fig = px.imshow(
            self.cluster_travel_distances,
            labels=dict(x="To Cluster", y="From Cluster", color="Distance (m)"),
            x=list(self.point_clusters.keys()),  # columns
            y=list(self.point_clusters.keys()),   # rows
            color_continuous_scale='OrRd',
            text_auto=True
        )

        fig.update_layout(
            title="Distances Between Clusters",
            xaxis_title="To Cluster",
            yaxis_title="From Cluster",
            height=600,
            width=600,
            xaxis=dict(
                tickmode='array',
                tickvals=list(self.point_clusters.keys()),
                ticktext=list(self.point_clusters.keys())
            ),
            yaxis=dict(
                tickmode='array',
                tickvals=list(self.point_clusters.keys()),
                ticktext=list(self.point_clusters.keys())
            ),
        )
        
        fig.show()
        
    def plot_cluster_centers(self):
        cluster_labels = sorted(self.df["DropOffCluster"].unique())
        colors = px.colors.qualitative.Dark24 
        color_map = {label: colors[i % len(colors)] for i, label in enumerate(cluster_labels)}
        cluster_data_list = []
        
        for cluster_label, cluster_data in self.point_clusters.items():
            lon, lat = cluster_data.center
            cluster_data_list.append({
                "Cluster": cluster_label,
                "Longitude": lon,
                "Latitude": lat
            })

        centers_df = pd.DataFrame(cluster_data_list)
        centers_df["Cluster"] = centers_df["Cluster"].astype('category')
        centers_df["MarkerSize"] = 5
        
        fig = px.scatter_map(
            centers_df,
            lat="Latitude",
            lon="Longitude",
            hover_name="Cluster",
            color="Cluster",
            size='MarkerSize',
            size_max=10,
            color_discrete_map=color_map
        )

        fig.update_layout(
            title=f"Cluster Centers",
            template="plotly",
            width=900,
            height=650,
            map_center_lon=pd.concat([self.df["PickUpLongitude"], self.df["DropOffLongitude"]]).mean(),
            map_center_lat=pd.concat([self.df["PickUpLatitude"], self.df["DropOffLatitude"]]).mean(),
            map_zoom=12
        )
        
        fig.show()
        
    def plot_trip_frequency_between_clusters(self, verbose=False):
        """ 
        Plots OD matrix based on the folw of traffica beteen Clusters. 
        Set verbose "True" to get the total bidirectional flow between clusters in descending order.
        """
        
        trip_freq_df = self.df.groupby(['PickUpCluster','DropOffCluster'],  observed=True).size().reset_index(name="TripCount")
        trip_freq_df.sort_values('TripCount', ascending=False)
    
        if verbose == True:
            # For Bidiredctional FLow 
            trip_freq_df["ClusterPair"] = trip_freq_df.apply(
                lambda row: tuple(sorted((row["PickUpCluster"], row["DropOffCluster"]))), axis=1
            )
            
            trip_freq_df = trip_freq_df[
                (trip_freq_df["PickUpCluster"] != -1) & (trip_freq_df["DropOffCluster"] != -1)
            ]
            
            bidirectional_flows = trip_freq_df.groupby("ClusterPair")["TripCount"].sum().reset_index()
            bidirectional_flows = bidirectional_flows.sort_values("TripCount", ascending=False).reset_index(drop=True)
            bidirectional_flows[["ClusterA", "ClusterB"]] = pd.DataFrame(bidirectional_flows["ClusterPair"].tolist(), index=bidirectional_flows.index)
            bidirectional_flows = bidirectional_flows[["ClusterA", "ClusterB", "TripCount"]]
    
            print("Total Trips between Clusters (Bidirectional Flow): \n", bidirectional_flows)
            
            print(f"Total clustered trips: {bidirectional_flows['TripCount'].sum()} from {len(self.df)}")
        
        # For OD Matrix 
        cluster_keys = list(list(self.point_clusters.keys()))

        od_matrix = np.zeros((len(cluster_keys), len(cluster_keys)), dtype=int)
        
        for _, row in trip_freq_df.iterrows():
            pu = row['PickUpCluster']
            do = row['DropOffCluster']
            count = row['TripCount']
            od_matrix[pu, do] = count
            
        fig = px.imshow(
            od_matrix,
            labels=dict(x="DropOff Cluster", y="PickUp Cluster", color="Trip Count"),
            x=cluster_keys,  # columns
            y=cluster_keys,   # rows
            color_continuous_scale='OrRd',
            text_auto=True
        )

        fig.update_layout(
            title="Trip Count Between Clusters (OD Matrix)",
            xaxis_title="To Cluster",
            yaxis_title="From Cluster",
            height=600,
            width=600,
            xaxis=dict(
                tickmode='array',
                tickvals=cluster_keys,
                ticktext=cluster_keys
            ),
            yaxis=dict(
                tickmode='array',
                tickvals=cluster_keys,
                ticktext=cluster_keys
            ),
        )
        
        fig.show()

    def plot_trip_frequency_between_clusters_interactive(
        self,
        start_time=None,
        end_time=None,
        time_col="PickUpStartTime",
        time_of_day_start=None,
        time_of_day_end=None,
        exclude_diagonal=False,
        verbose=False,
    ):
        import numpy as np
        import pandas as pd
        import plotly.express as px

        df = self.df.copy()

        # Ensure datetime
        if not pd.api.types.is_datetime64_any_dtype(df[time_col]):
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce")

        # Only drop NaT if we actually apply any time filtering
        if (
            start_time is not None
            or end_time is not None
            or (time_of_day_start is not None and time_of_day_end is not None)
        ):
            df = df.dropna(subset=[time_col])

        # Absolute window filter
        if start_time is not None:
            start_ts = pd.to_datetime(start_time)
            df = df[df[time_col] >= start_ts]

        if end_time is not None:
            end_ts = pd.to_datetime(end_time)
            df = df[df[time_col] <= end_ts]

        # Time-of-day filter using minute-of-day (robust for tz-aware datetimes)
        if time_of_day_start is not None and time_of_day_end is not None:
            ts = pd.to_datetime(time_of_day_start)
            te = pd.to_datetime(time_of_day_end)
            tod_start = int(ts.hour) * 60 + int(ts.minute)
            tod_end = int(te.hour) * 60 + int(te.minute)

            mins = df[time_col].dt.hour * 60 + df[time_col].dt.minute

            if tod_start <= tod_end:
                df = df[(mins >= tod_start) & (mins <= tod_end)]
            else:
                # crosses midnight
                df = df[(mins >= tod_start) | (mins <= tod_end)]
        
        df = df[
            (df["PickUpCluster"] != -1) &
            (df["DropOffCluster"] != -1)
        ]
        
        # Require both pickup window endpoints (drop rows missing either)
        df = df.dropna(subset=["PickUpStartTime", "PickUpEndTime"])

        # Convert each request to minute-of-day interval
        pu_start_m = df["PickUpStartTime"].dt.hour * 60 + df["PickUpStartTime"].dt.minute
        pu_end_m   = df["PickUpEndTime"].dt.hour   * 60 + df["PickUpEndTime"].dt.minute

        # Slider window to minutes
        ts = pd.to_datetime(time_of_day_start)
        te = pd.to_datetime(time_of_day_end)
        w_start = int(ts.hour) * 60 + int(ts.minute)
        w_end   = int(te.hour) * 60 + int(te.minute)

        if w_start <= w_end:
            # overlap on same day
            df = df[(pu_start_m <= w_end) & (pu_end_m >= w_start)]
        else:
            # window crosses midnight: treat as union [w_start..1439] U [0..w_end]
            in_late = (pu_start_m <= 1439) & (pu_end_m >= w_start)
            in_early = (pu_start_m <= w_end) & (pu_end_m >= 0)
            df = df[in_late | in_early]

        trip_freq_df = (
            df.groupby(["PickUpCluster", "DropOffCluster"], observed=True)
            .size()
            .reset_index(name="TripCount")
            .sort_values("TripCount", ascending=False)
        )

        if verbose:
            trip_freq_df["ClusterPair"] = trip_freq_df.apply(
                lambda row: tuple(sorted((row["PickUpCluster"], row["DropOffCluster"]))),
                axis=1,
            )

            bidirectional_flows = (
                trip_freq_df.groupby("ClusterPair")["TripCount"]
                .sum()
                .reset_index()
                .sort_values("TripCount", ascending=False)
                .reset_index(drop=True)
            )

            bidirectional_flows[["ClusterA", "ClusterB"]] = pd.DataFrame(
                bidirectional_flows["ClusterPair"].tolist(),
                index=bidirectional_flows.index,
            )
            bidirectional_flows = bidirectional_flows[["ClusterA", "ClusterB", "TripCount"]]

            print("Total Trips between Clusters (Bidirectional Flow):\n", bidirectional_flows)
            print(f"Total clustered trips: {int(bidirectional_flows['TripCount'].sum())} from {len(df)} (filtered)")

        cluster_keys = list(self.point_clusters.keys())
        n = len(cluster_keys)
        od_matrix = np.zeros((n, n), dtype=int)

        # Fill matrix (accumulate to be robust)
        for _, row in trip_freq_df.iterrows():
            pu = int(row["PickUpCluster"])
            do = int(row["DropOffCluster"])
            cnt = int(row["TripCount"])
            if 0 <= pu < n and 0 <= do < n:
                od_matrix[pu, do] += cnt
        
        # after filling od_matrix
        if exclude_diagonal:
            np.fill_diagonal(od_matrix, 0)

        fig = px.imshow(
            od_matrix,
            labels=dict(x="DropOff Cluster", y="PickUp Cluster", color="Trip Count"),
            x=cluster_keys,
            y=cluster_keys,
            color_continuous_scale="OrRd",
            text_auto=True,
        )

        title_bits = []
        if start_time is not None or end_time is not None:
            title_bits.append(f"{start_time or '-inf'} to {end_time or '+inf'}")
        if time_of_day_start is not None and time_of_day_end is not None:
            title_bits.append(f"{time_of_day_start} to {time_of_day_end}")

        title_suffix = f" (window: {', '.join(title_bits)})" if title_bits else ""

        fig.update_layout(
            title="Trip Count Between Clusters (OD Matrix)" + title_suffix,
            xaxis_title="To Cluster",
            yaxis_title="From Cluster",
            height=600,
            width=600,
            xaxis=dict(tickmode="array", tickvals=cluster_keys, ticktext=cluster_keys),
            yaxis=dict(tickmode="array", tickvals=cluster_keys, ticktext=cluster_keys),
        )

        fig.show()
        return fig
    
    
    def analyze_od_stability(
        self,
        bin_size_min=30,
        exclude_diagonal=True,
        horizon_start=None,
        horizon_end=None,
    ):
        import numpy as np
        import pandas as pd

        df = self.df.copy()

        # Remove unclustered
        df = df[(df["PickUpCluster"] != -1) & (df["DropOffCluster"] != -1)]

        # Require pickup window
        df = df.dropna(subset=["PickUpStartTime", "PickUpEndTime"])

        # Optional: filter by horizon using window overlap (minute-of-day)
        if horizon_start is not None or horizon_end is not None:

            hs_m = _to_minute_of_day(horizon_start) if horizon_start is not None else 0
            he_m = _to_minute_of_day(horizon_end) if horizon_end is not None else 24 * 60

            pu_start_m = df["PickUpStartTime"].dt.hour * 60 + df["PickUpStartTime"].dt.minute
            pu_end_m = df["PickUpEndTime"].dt.hour * 60 + df["PickUpEndTime"].dt.minute

            overlap = (pu_start_m < he_m) & (pu_end_m > hs_m)
            df = df[overlap]

        # Minute-of-day windows
        pu_start = df["PickUpStartTime"].dt.hour * 60 + df["PickUpStartTime"].dt.minute
        pu_end = df["PickUpEndTime"].dt.hour * 60 + df["PickUpEndTime"].dt.minute
        df = df.assign(PU_start_min=pu_start, PU_end_min=pu_end)

        # Build bins within horizon (minute-of-day)
        hs_m = 0
        he_m = 24 * 60

        if horizon_start is not None:
            hs_m = _to_minute_of_day(horizon_start)

        if horizon_end is not None:
            he_m = _to_minute_of_day(horizon_end)

        bins = np.arange(hs_m, he_m + bin_size_min, bin_size_min)
        num_bins = len(bins) - 1

        results = []

        for (pu, do), group in df.groupby(["PickUpCluster", "DropOffCluster"], observed=True):
            if exclude_diagonal and pu == do:
                continue

            bin_counts = np.zeros(num_bins, dtype=int)

            for i in range(num_bins):
                b_start = int(bins[i])
                b_end = int(bins[i + 1])
                
                # Half-open bin overlap: [b_start, b_end)
                overlap = (group["PU_start_min"] < b_end) & (group["PU_end_min"] > b_start)
                bin_counts[i] = int(overlap.sum())

            unique_trips = int(len(group))
            bin_overlap_count = int(bin_counts.sum())
            active_bins = int((bin_counts > 0).sum())
            share_active = active_bins / float(num_bins)

            mean_per_bin = float(bin_counts.mean())
            std_per_bin = float(bin_counts.std())
            cv = (std_per_bin / mean_per_bin) if mean_per_bin > 0 else np.nan

            avg_bins_per_trip = (bin_overlap_count / float(unique_trips)) if unique_trips > 0 else np.nan
            
            max_feasible = int(bin_counts.max())
            p90_feasible = float(np.quantile(bin_counts, 0.90))

            results.append({
                "PU": int(pu),
                "DO": int(do),
                "UniqueTrips": unique_trips,
                "BinOverlapCount": bin_overlap_count,
                "AvgBinsPerTrip": avg_bins_per_trip,
                "ActiveBins": active_bins,
                "ShareActiveBins": share_active,
                "MeanFeasibleTripsPerBin": mean_per_bin,
                "StdFeasibleTripsPerBin": std_per_bin,
                "CV": cv,
                "MaxFeasibleTripsInBin": max_feasible,
                "P90FeasibleTripsPerBin": p90_feasible,
            })

        result_df = pd.DataFrame(results)
        if result_df.empty:
            return result_df

        # Directional indicator (tie-breaker)
        rev = result_df.rename(columns={
            "PU": "DO",
            "DO": "PU",
            "UniqueTrips": "ReverseUniqueTrips",
        })[["PU", "DO", "ReverseUniqueTrips"]]

        result_df = result_df.merge(rev, on=["PU", "DO"], how="left")
        result_df["ReverseUniqueTrips"] = result_df["ReverseUniqueTrips"].fillna(0).astype(int)

        denom = (result_df["UniqueTrips"] + result_df["ReverseUniqueTrips"]).replace(0, np.nan)
        result_df["Imbalance"] = (result_df["UniqueTrips"] - result_df["ReverseUniqueTrips"]).abs() / denom

        result_df = result_df.sort_values(
            ["UniqueTrips", "ShareActiveBins", "AvgBinsPerTrip"],
            ascending=[False, False, False],
        ).reset_index(drop=True)

        return result_df


    def plot_od_time_profile(
        self,
        pu,
        do,
        bin_size_min=30,
        horizon_start="05:00",
        horizon_end="23:00",
    ):
        df = self.df.copy()

        # Remove unclustered and require pickup window
        df = df[
            (df["PickUpCluster"] != -1) &
            (df["DropOffCluster"] != -1)
        ].dropna(subset=["PickUpStartTime", "PickUpEndTime"])

        # Filter OD pair (both directions) and force copies to avoid SettingWithCopyWarning
        df_ab = df.loc[(df["PickUpCluster"] == pu) & (df["DropOffCluster"] == do)].copy()
        df_ba = df.loc[(df["PickUpCluster"] == do) & (df["DropOffCluster"] == pu)].copy()

        # Convert pickup window to minute-of-day
        df_ab.loc[:, "start"] = df_ab["PickUpStartTime"].dt.hour * 60 + df_ab["PickUpStartTime"].dt.minute
        df_ab.loc[:, "end"] = df_ab["PickUpEndTime"].dt.hour * 60 + df_ab["PickUpEndTime"].dt.minute

        df_ba.loc[:, "start"] = df_ba["PickUpStartTime"].dt.hour * 60 + df_ba["PickUpStartTime"].dt.minute
        df_ba.loc[:, "end"] = df_ba["PickUpEndTime"].dt.hour * 60 + df_ba["PickUpEndTime"].dt.minute

        # Horizon to minute-of-day
        hs_m = _to_minute_of_day(horizon_start)
        he_m = _to_minute_of_day(horizon_end)

        bins = np.arange(hs_m, he_m + bin_size_min, bin_size_min)
        num_bins = len(bins) - 1

        counts_ab = np.zeros(num_bins, dtype=int)
        counts_ba = np.zeros(num_bins, dtype=int)

        for i in range(num_bins):
            b_start = int(bins[i])
            b_end = int(bins[i + 1])

            # Half-open overlap: [b_start, b_end)
            counts_ab[i] = int(((df_ab["start"] < b_end) & (df_ab["end"] > b_start)).sum())
            counts_ba[i] = int(((df_ba["start"] < b_end) & (df_ba["end"] > b_start)).sum())

        # Time labels for bin starts
        times = [f"{int(b // 60):02d}:{int(b % 60):02d}" for b in bins[:-1]]

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=times, y=counts_ab, mode="lines+markers", name=f"{pu} -> {do}"))
        fig.add_trace(go.Scatter(x=times, y=counts_ba, mode="lines+markers", name=f"{do} -> {pu}"))

        fig.update_layout(
            title=f"Temporal Demand Profile ({pu} <-> {do}) | bin={bin_size_min}min | {horizon_start}-{horizon_end}",
            xaxis_title="Time of Day (bin start)",
            yaxis_title="Feasible Trips per Bin",
            height=500,
            width=1000,
        )

        fig.show()

               
    def plot_chaining_location(self, id):
        
        ucl = self.unchaining_locations[id]
        cl  = self.chaining_locations[id]
        
        cl_start_coords = cl.location_start['Longitude'], cl.location_start['Latitude']
        cl_end_coords = cl.location_end['Longitude'], cl.location_end['Latitude']
        ucl_start_coords = ucl.location_start['Longitude'], ucl.location_start['Latitude']
        ucl_end_coords = ucl.location_end['Longitude'], ucl.location_end['Latitude']
         
        fig = go.Figure()
        
        # Combine with None in between
        combined_lons = [cl_start_coords[0], cl_end_coords[0], None, ucl_start_coords[0], ucl_end_coords[0]]
        combined_lats =  [cl_start_coords[1], cl_end_coords[1], None, ucl_start_coords[1], ucl_end_coords[1]]
                
        text1 = [f"Chaining"] * 2
        text2 = [f"Unchaining"] * 2
        combined_text = text1 + [None] + text2
        
        fig.add_trace(go.Scattermap(
            mode="markers+lines",
            lon=combined_lons,
            lat=combined_lats,
            marker=dict(size=15),
            text=combined_text
        ))
        
        fig.update_layout(
            title=f"Un/Chain Location {id}",
            template="plotly",  # Use plotly template for styling
            xaxis_title="Longitude",
            yaxis_title="Latitude",
            width=900,
            height=650,
            map_center_lon=pd.concat([self.df["PickUpLongitude"], self.df["DropOffLongitude"]]).mean(),
            map_center_lat=pd.concat([self.df["PickUpLatitude"], self.df["DropOffLatitude"]]).mean(),
            map_zoom=12
        )
        
        fig.show()
        
    def plot_lines_with_chaining_locations(self, line_ids=None, fixed_cl_size=None, save_as=None):
        
        filtered_lines = []
        line_trace_indices = []
        
        if line_ids is None:
            filtered_lines = list(self.lines.values())
        else:
            filtered_lines = [self.lines[id] for id in line_ids]
        
        # Get line usage counts
        if "Line" in self.df.columns:
            line_trip_counts = self.df["Line"].value_counts().to_dict() 
        else:
            self.df["Line"] = None
            line_trip_counts = {} 
                
        # Sort lines by count from the dataframe, defaulting to 0 if line not found
        sorted_lines_desc = sorted(
            filtered_lines,
            key=lambda line: line_trip_counts.get(line.id, 0),
            reverse=True
        )
            
        # create color data
        unique_lines = [line_id for line_id in self.lines]
        colors = px.colors.qualitative.Dark24
        line_id_to_color = {line_id: colors[i % len(colors)] for i, line_id in enumerate(sorted(unique_lines))}

        max_trips = line_trip_counts.get(sorted_lines_desc[0].id, 0)
        min_trips = line_trip_counts.get(sorted_lines_desc[-1].id, 0)
        range_trips = max_trips - min_trips

        fig = go.Figure()
        
        for line in sorted_lines_desc:
            
            unchaining_location_1 = line.chain_route_1.UCL1 
            chaining_location_1 = line.chain_route_1.CL1
            unchaining_location_2 = line.chain_route_1.UCL2 
            chaining_location_2 = line.chain_route_1.CL2
            
            cl_centers = [
                unchaining_location_1.center,
                chaining_location_1.center,
                unchaining_location_2.center,
                chaining_location_2.center,
            ]

            # Split into separate longitude and latitude lists
            cl_longitudes = [coord[0] for coord in cl_centers]
            cl_latitudes = [coord[1] for coord in cl_centers]
            
            # add trace for the chaining locations 
            total_trips = line_trip_counts.get(line.id, 0)
            
            if fixed_cl_size is None:
                if range_trips == 0:
                    size_norm = 20
                else:
                    size_norm = max(20, 50 * (total_trips) / range_trips)
            else:
                size_norm = fixed_cl_size
            
            fig.add_trace(go.Scattermap(
                mode="markers",
                lon=cl_longitudes,
                lat=cl_latitudes,
                marker=dict(size=size_norm,
                            opacity=0.5,
                            color=line_id_to_color[line.id]),
                name=f'(Un)Chaining Locations of Line {line.id}',
                text=[
                    f"LINE: {line.id}<br>CL ID: {unchaining_location_1.id} Unchain<br>Total Requests Assigned to Line: {total_trips}",
                    f"LINE: {line.id}<br>CL ID: {chaining_location_1.id} Chain<br>Total Requests Assigned to Line: {total_trips}",
                    f"LINE: {line.id}<br>CL ID: {unchaining_location_2.id} Unchain<br>Total Requests Assigned to Line: {total_trips}",
                    f"LINE: {line.id}<br>CL ID: {chaining_location_2.id} Chain<br>Total Requests Assigned to Line: {total_trips}"
                ],
                hoverinfo="text"
            ))
            line_trace_indices.append(len(fig.data) - 1)
            
            # add trace for line route
            if line.chain_route_1 and line.chain_route_2:
                
                lons1, lats1 = zip(*line.chain_route_1.route)
                lons2, lats2 = zip(*line.chain_route_2.route)

                # Combine with None in between
                combined_lons = list(lons1) + [None] + list(lons2)
                combined_lats = list(lats1) + [None] + list(lats2)
                
                text1 = [f"LINE: {line.id}<br>From CL: {chaining_location_1.id}<br>To UnCL: {unchaining_location_2.id}"] * len(lons1)
                text2 = [f"LINE: {line.id}<br>From CL: {chaining_location_2.id}<br>To UnCL: {unchaining_location_1.id}"] * len(lons2)
                combined_text = text1 + [None] + text2
                
                fig.add_trace(go.Scattermap(
                    mode="markers+lines",
                    lon=combined_lons,
                    lat=combined_lats,
                    marker=dict(size=10,
                                opacity=0.5,
                                color=line_id_to_color[line.id]),
                    name=f'Route of Line {line.id}',
                    text=combined_text,
                    hoverinfo="text"
                ))
                line_trace_indices.append(len(fig.data) - 1)

            # add trace for trips PU/DOs
            color = line_id_to_color.get(line.id, "lightgray")

            # Filter PU and DO by line
            line_df = self.df[self.df["Line"] == line.id]
            lon = line_df["PickUpLongitude"].tolist() + line_df["DropOffLongitude"].tolist()
            lat = line_df["PickUpLatitude"].tolist() + line_df["DropOffLatitude"].tolist()
            user_guids = line_df.index.tolist() * 2 
            
            fig.add_trace(go.Scattermap(
                mode="markers",
                lon=lon,
                lat=lat,
                marker=dict(size=10, opacity=0.8, color=color),
                name=f"PU/DO of requests that could use Line {line.id}",
                text=[f"UserGuid: {uid}" for uid in user_guids],  # Add hover text
                hoverinfo='text'
            ))
            line_trace_indices.append(len(fig.data) - 1)

        n_traces = len(fig.data)
        
        # Start with everything visible
        all_visible = [True] * n_traces
        # Hide only line-related traces
        lines_hidden = all_visible.copy()
        for idx in line_trace_indices:
            lines_hidden[idx] = "legendonly"
            
        # Add trace for trips with no line
        no_line_df = self.df[(self.df["Line"].isna()) | (self.df["Line"] == -1)]
        no_line_lon = no_line_df["PickUpLongitude"].tolist() + no_line_df["DropOffLongitude"].tolist()
        no_line_lat = no_line_df["PickUpLatitude"].tolist() + no_line_df["DropOffLatitude"].tolist()
        
        fig.add_trace(go.Scattermap(
            mode="markers",
            lon=no_line_lon,
            lat=no_line_lat,
            marker=dict(size=8, color="gray", opacity=0.5),
            name="PU/DO of requests that dont use a line",
            text=no_line_df.index.tolist()
        ))
            
                   
        fig.update_layout(
            title=f"Platoon Lines",
            template="plotly",  # Use plotly template for styling
            showlegend=True,  # Enable legend to toggle visibility of pickups and drop-offs
            xaxis_title="Longitude",
            yaxis_title="Latitude",
            width=900,
            height=650,
            map_center_lon=pd.concat([self.df["PickUpLongitude"], self.df["DropOffLongitude"]]).mean(),
            map_center_lat=pd.concat([self.df["PickUpLatitude"], self.df["DropOffLatitude"]]).mean(),
            map_zoom=12,
            updatemenus=[
                dict(
                    type="buttons",
                    direction="right",
                    x=1.00,
                    xanchor="left",
                    y=1.12,
                    yanchor="top",
                    showactive=False,
                    buttons=[
                        dict(
                            label="Unselect all lines",
                            method="update",
                            args=[{"visible": lines_hidden}],
                        ),
                        dict(
                            label="Show all lines",
                            method="update",
                            args=[{"visible": all_visible}],
                        ),
                    ],
                )
    ]
        )
        
        if save_as:
            if save_as.endswith(".html"):
                try:
                    # if browser is available
                    fig.write_html(save_as, auto_open=False)
                except Exception as e:
                    print("Error opening plot automatically in the browser:\n{e}")
            else:
                fig.write_image(save_as)
        else:
            fig.show()
    
    def plot_line_usage(self):
        """
        
        """
        x_axis_locations = [-1] + list(self.chaining_locations.keys())

        # Step 2: Group by StartChainingLocation and Line
        df_counts = self.df.groupby(['StartChainingLocation', 'Line']).size().reset_index(name='Count')

        # Step 3: Filter to only include StartChainingLocations in x_axis_locations
        df_counts = df_counts[df_counts['StartChainingLocation'].isin(x_axis_locations)]

        # Step 4: Ensure all (location, line) combinations are present
        all_lines = df_counts['Line'].unique()
        all_combinations = pd.MultiIndex.from_product(
            [x_axis_locations, all_lines],
            names=['StartChainingLocation', 'Line']
        )
        df_counts = df_counts.set_index(['StartChainingLocation', 'Line']).reindex(all_combinations, fill_value=0).reset_index()

        # Step 5: Set up color mapping with gray fallback
        unique_lines = sorted(self.lines.keys())
        colors = px.colors.qualitative.Dark24
        line_id_to_color = {line_id: colors[i % len(colors)] for i, line_id in enumerate(unique_lines)}
        default_color = "#999999"

        df_counts['LineColor'] = df_counts['Line'].apply(lambda line: line if line in line_id_to_color else 'Other')
        color_map_with_gray = {**line_id_to_color, 'Other': default_color}

        fig = px.bar(
            df_counts,
            x='StartChainingLocation',
            y='Count',
            color='LineColor',
            color_discrete_map=color_map_with_gray,
            category_orders={'StartChainingLocation': x_axis_locations},
            title='Start Chaining Location Frequency by Line',
            hover_data={'Count': True, 'StartChainingLocation': False, 'Line': False, 'LineColor': False}
        )

        fig.update_traces(hovertemplate='Count: %{y}<extra></extra>')

        fig.update_layout(
            xaxis_title="Start Chaining Location",
            yaxis_title="Trip Count",
            legend_title="Line",
            barmode="stack"
        )

        fig.show()
               
    def get_pu_scatter_map(self, min_time=None, max_time=None, size=10, mode='map'):
        """
        Create trace for PU spots in certain time window
        """
        filtered_df = self.filter_df_by_time(min_time, max_time)
        
        if filtered_df.empty:
            print(f"No PickUps Between {min_time} and {max_time}")
            return None
        
        # random noise scale to prevent points appearing over eachother
        noise_scale = 0.0005  
        
        # Add noise for display
        pickup_x_noisy = filtered_df["PickUpLongitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))
        pickup_y_noisy = filtered_df["PickUpLatitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))

        if mode == 'scatter':
            # Create color mapping based on a colormap (Rainbow)
            colorscale = "Rainbow" 
            unique_indices = sorted(filtered_df.index.unique())
            index_to_color_value = {idx: i / (len(unique_indices) - 1) if len(unique_indices) > 1 else 0.5 for i, idx in enumerate(unique_indices)}
            color_values = filtered_df.index.map(index_to_color_value)
            
            trace = go.Scatter(
                    x=pickup_x_noisy,
                    y=pickup_y_noisy,
                    mode="markers+text",  # Show markers and text
                    marker=dict(
                        size=12,
                        color=color_values,  # Color according to UserGuid
                        colorscale=colorscale,
                        showscale=False, 
                        opacity=0.5
                    ),
                    text=filtered_df.index.values,  
                    textposition="top center",
                    textfont=dict(size=10, color="black"),
                    name="Pickups",
                    customdata=filtered_df[["PickUpLongitude", "PickUpLatitude", "DropOffLongitude", "DropOffLatitude"]],
                    hovertemplate="<b>Pickup: %{text}</b><br>Longitude: %{customdata[0]}<br>Latitude: %{customdata[1]}<br><b>Corresponding Drop-off:</b><br>Longitude: %{customdata[2]}<br>Latitude: %{customdata[3]}<extra></extra>",       
                )
            
        else:
            trace = go.Scattermap(
                lat=pickup_y_noisy,
                lon=pickup_x_noisy,
                mode="markers",
                marker=dict(
                    size=(size + 2 * filtered_df["TotalPassengers"]),  # Marker size based on passenger count
                    color="blue",
                    opacity=0.66,  
                ),
                name="Pickups",  # Legend entry for pickups
                text=[f"Trip: {uid}<br>Lat: {lat:.5f}<br>Lon: {lon:.5f}" for uid, lat, lon in zip(filtered_df.index.values, filtered_df["PickUpLatitude"], filtered_df["PickUpLongitude"])],
                hoverinfo="text",
            )
        
        return trace
               
    def get_do_scatter_map(self, min_time=None, max_time=None, size=10, mode='map'):
        """
        Create trace for DO spots in certain time window
        """
        filtered_df = self.filter_df_by_time(min_time, max_time)
        
        if filtered_df.empty:
            print(f"No DropOffs Between {min_time} and {max_time}")
            return None
        
        # random noise scale to prevent points appearing over eachother
        noise_scale = 0.0005  
        
        # Add noise for display
        dropoff_x_noisy = filtered_df["DropOffLongitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))
        dropoff_y_noisy = filtered_df["DropOffLatitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))
            
        if mode == 'scatter':
             # Create color mapping based on a colormap (Rainbow)
            colorscale = "Rainbow" 
            unique_indices = sorted(filtered_df.index.unique())
            index_to_color_value = {idx: i / (len(unique_indices) - 1) if len(unique_indices) > 1 else 0.5 for i, idx in enumerate(unique_indices)}
            color_values = filtered_df.index.map(index_to_color_value)


            
            trace = go.Scatter(
                    x=dropoff_x_noisy,
                    y=dropoff_y_noisy,
                    mode="markers+text",
                    marker=dict(
                        size=12,
                        color=color_values,  # Same color as pickup
                        colorscale=colorscale,
                        opacity=0.5,
                        showscale=False,
                        symbol='x'
                    ),
                    text=filtered_df.index,
                    textposition="bottom center",
                    textfont=dict(size=10, color="black"),
                    name="Drop-offs",
                    customdata=filtered_df[["DropOffLongitude", "DropOffLatitude", "PickUpLongitude", "PickUpLatitude"]],
                    hovertemplate="<b>Drop-off: %{text}</b><br>Longitude: %{customdata[0]}<br>Latitude: %{customdata[1]}<br><b>Corresponding Pickup:</b><br>Longitude: %{customdata[2]}<br>Latitude: %{customdata[3]}<extra></extra>",
                )
        
        else:
            trace = go.Scattermap(
                lat=dropoff_y_noisy,
                lon=dropoff_x_noisy,
                mode="markers",
                marker=dict(
                    size=(size + 2 * filtered_df["TotalPassengers"]),  # Marker size based on passenger count
                    color="red",
                    opacity=0.66, 
                    symbol='circle' 
                ),
                name="DropOffs",  # Legend entry for pickups
                text=[f"Trip: {uid}<br>Lat: {lat:.5f}<br>Lon: {lon:.5f}" for uid, lat, lon in zip(filtered_df.index.values, filtered_df["DropOffLatitude"], filtered_df["DropOffLongitude"])],
                hoverinfo="text",
            )
        
        return trace
    
    def plot_routes_over_time(self, min_time, max_time, route_mode='lines'):
        """
        Plots the predicted GraphHopper routes of trh trips within a given a time window specified bt min time and max time. 
        route_mode: "lines" or 'markes'
        """
        filtered_df = self.filter_df_by_time(min_time, max_time)
        
        if filtered_df.empty:
            print(f"No Trips Between {min_time} and {max_time}")
            return

        fig = go.Figure()
        
        # Add pickup points
        fig.add_trace(self.get_pu_scatter_map(min_time, max_time))
        
        # Add drop-off points
        fig.add_trace(self.get_do_scatter_map(min_time, max_time))
        
        using_gh = False 
        # add GH routes as traces
        if 'GraphHopperRoute' in self.df.columns:
            for index, entry in filtered_df.iterrows():
                lons, lats  = zip(*entry['GraphHopperRoute'])  # Unpack list of (lat, lon) tuples
                
                fig.add_trace(
                    go.Scattermap(
                        lon=lons,  
                        lat=lats,  
                        mode=route_mode,
                        line=dict(width=3),
                        opacity=0.6,
                        name=f"Trip {index}", # Legend entry for route lines
                        showlegend=False,
                    )
                    
                )
            using_gh = True
        # add OSMNX routes as traces        
        elif 'OSMNXRoute' in self.df.columns:
            for index, entry in filtered_df.iterrows():
                lons, lats = zip(*entry['OSMNXRoute'])  # Unpack list of (lat, lon) tuples
                fig.add_trace(
                    go.Scattermap(
                        lon=lons,  
                        lat=lats,  
                        mode=route_mode,
                        line=dict(width=3),
                        opacity=0.6,
                        name=f"Trip {index}", # Legend entry for route lines
                        showlegend=False,
                    )
                    
                )
        
        # Update layout - center, and zoom level
        routing_text = "GraphHopper used for routing" if using_gh == True else "OSMNX used for routing"
        fig.update_layout(
            title="Pickup, Dropoff, Trip Routes Over Time | " + routing_text,
            template="plotly",  # Use plotly template for styling
            showlegend=True,  # Enable legend to toggle visibility of pickups and drop-offs
            map_center_lon=pd.concat([filtered_df["PickUpLongitude"], filtered_df["DropOffLongitude"]]).mean(),
            map_center_lat=pd.concat([filtered_df["PickUpLatitude"], filtered_df["DropOffLatitude"]]).mean(),
            width=800,  # Adjust width of the figure
            height=600,  # Adjust height of the figure
            map_zoom=12
        )
        
        fig.update_layout(map_style="carto-positron")

        fig.show()
    
    def plot_route_heat_map(self, min_time=None, max_time=None, cluster_range=10, min_frequency=1):
        filtered_df = self.filter_df_by_time(min_time, max_time)
        
        if filtered_df.empty:
            print(f"No Trips Between {min_time} and {max_time}")
            return
        
        all_points = []

        for user_guid, route in filtered_df["GraphHopperRoute"].items():
            for coord in route:
                all_points.append({
                    "UserGUID": user_guid,
                    "lat": coord[1],
                    "lon": coord[0]
                })

        points_df = pd.DataFrame(all_points)
        
        # Convert lat/lon to radians for haversine
        coords_rad = np.radians(points_df[["lat", "lon"]])

        # Cluster
        kms_per_radian = 6371.0088
        epsilon = (cluster_range / 1000) / kms_per_radian  # e.g., 10 m radius

        db = DBSCAN(eps=epsilon, min_samples=3, algorithm='ball_tree', metric='haversine')
        cluster_labels = db.fit_predict(coords_rad)

        points_df["cluster"] = cluster_labels

        # Filter out noise points if needed (cluster = -1)
        clustered = points_df[points_df["cluster"] != -1]

        # Group by cluster and calculate centroid + size
        cluster_summary = (
            clustered
            .groupby("cluster")
            .agg(
                ClusterLat=("lat", "mean"),
                ClusterLon=("lon", "mean"),
                Size=("cluster", "count")
            )
            .reset_index()
        )
        
        cluster_summary = cluster_summary[cluster_summary['Size'] >= min_frequency]
        
        
        fig = go.Figure()
        
        # add trace for junction frequencies
        fig.add_trace(
            go.Scattermap(
                lon=cluster_summary['ClusterLon'],
                lat=cluster_summary['ClusterLat'],
                mode='markers',
                marker=dict(
                    size= 5 + 15 * (cluster_summary['Size'] / cluster_summary['Size'].max()), 
                    color=cluster_summary['Size'],  
                    colorscale="Bluered",  
                    opacity = 0.75, #(travelled_nodes_exploded_df['Frequency'] / max_freq),  
                    colorbar=dict(
                        title="Frequency",  
                        x=1.15,  # Move to the right
                        y=0.35,  # Center vertically
                        len=0.75  # Adjust height
                ),
                ),
                name="HeatMap",
                showlegend=True,
                hovertemplate=(
                    "Lat: %{lat}<br>"  # Show Latitude
                    "Lon: %{lon}<br>"  # Show Longitude
                    "Frequency: %{marker.color}"  # Show Frequency (color value)
                    "<extra></extra>"  # Removes extra hover box
                )
                )
            )

        # Update layout to set the map style and zoom level
        fig.update_layout(
            title="Density Map of Coordinates Along GraphHopper Routes",
            mapbox_style="carto-positron",
            width=800,  # Adjust width of the figure
            height=600,  # Adjust height of the figure
            map_center_lon=points_df["lon"].mean(),
            map_center_lat=points_df["lat"].mean(),
            map_zoom=10
        )

        # Show the plot
        fig.show()
    
    def plot_pu_heat_map(self, min_time=None, max_time=None, cluster_range=10, min_frequency=1):
        filtered_df = self.filter_df_by_time(min_time, max_time)
        
        if filtered_df.empty:
            print(f"No Trips Between {min_time} and {max_time}")
            return

        points_df = filtered_df[["PickUpLatitude","PickUpLongitude"]]
        
        # Convert lat/lon to radians for haversine
        coords_rad = np.radians(points_df[["PickUpLatitude", "PickUpLongitude"]])

        # TODO Change clustering
        # Cluster
        kms_per_radian = 6371.0088
        epsilon = (cluster_range / 1000) / kms_per_radian  # e.g., 10 m radius

        db = DBSCAN(eps=epsilon, min_samples=3, algorithm='ball_tree', metric='haversine')
        cluster_labels = db.fit_predict(coords_rad)

        points_df["Cluster"] = cluster_labels

        # Filter out noise points if needed (cluster = -1)
        clustered = points_df[points_df["Cluster"] != -1]

        # Group by cluster and calculate centroid + size
        cluster_summary = (
            clustered
            .groupby("Cluster")
            .agg(
                ClusterLat=("PickUpLatitude", "mean"),
                ClusterLon=("PickUpLongitude", "mean"),
                Size=("Cluster", "count")
            )
            .reset_index()
        )
        
        cluster_summary = cluster_summary[cluster_summary['Size'] >= min_frequency]
        
        
        fig = go.Figure()
        
        # add trace for junction frequencies
        fig.add_trace(
            go.Scattermap(
                lon=cluster_summary['ClusterLon'],
                lat=cluster_summary['ClusterLat'],
                mode='markers',
                marker=dict(
                    size= 15 + 50 * (cluster_summary['Size'] / cluster_summary['Size'].max()), 
                    color=cluster_summary['Size'],  
                    colorscale="Inferno",  
                    opacity = 0.75, #(travelled_nodes_exploded_df['Frequency'] / max_freq),  
                    colorbar=dict(
                        title="Frequency",  
                        x=1.15,  # Move to the right
                        y=0.35,  # Center vertically
                        len=0.75  # Adjust height
                ),
                ),
                name="HeatMap",
                showlegend=True,
                hovertemplate=(
                    "Lat: %{lat}<br>"  # Show Latitude
                    "Lon: %{lon}<br>"  # Show Longitude
                    "Frequency: %{marker.color}"  # Show Frequency (color value)
                    "<extra></extra>"  # Removes extra hover box
                )
                )
            )
        
        # Add PU trace
        fig.add_trace(self.get_pu_scatter_map(min_time=min_time, max_time=max_time, size=5))
        
        # Update layout to set the map style and zoom level
        fig.update_layout(
            title="Density Map of Coordinates Along GraphHopper Routes",
            mapbox_style="carto-positron",
            width=800,  # Adjust width of the figure
            height=600,  # Adjust height of the figure
            map_center_lon=points_df["PickUpLongitude"].mean(),
            map_center_lat=points_df["PickUpLatitude"].mean(),
            map_zoom=10
        )

        # Show the plot
        fig.show()
    
    def plot_pu_do_combined_scatter(self, min_time=None, max_time=None):
        
        filtered_df = self.filter_df_by_time(min_time, max_time)
        
        if filtered_df.empty:
            print(f"No Trips Between {min_time} and {max_time}")
            return

        # Create color mapping based on a colormap (Rainbow)
        colorscale = "Rainbow" 
        unique_indices = sorted(filtered_df.index.unique())
        index_to_color_value = {idx: i / (len(unique_indices) - 1) if len(unique_indices) > 1 else 0.5 for i, idx in enumerate(unique_indices)}
        color_values = filtered_df.index.map(index_to_color_value)

        # random noise scale to prevent points appearing over eachother
        noise_scale = 0.0005  
        
        # Add noise for display
        pickup_x_noisy = filtered_df["PickUpLongitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))
        pickup_y_noisy = filtered_df["PickUpLatitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))

        dropoff_x_noisy = filtered_df["DropOffLongitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))
        dropoff_y_noisy = filtered_df["DropOffLatitude"] + np.random.normal(0, noise_scale, size=len(filtered_df))
        
        fig = go.Figure()

        # Add Pickup Points with colors based on UserGuid
        fig.add_trace(
                go.Scatter(
                    x=pickup_x_noisy,
                    y=pickup_y_noisy,
                    mode="markers+text",  # Show markers and text
                    marker=dict(
                        size=12,
                        color=color_values,  # Color according to UserGuid
                        colorscale=colorscale,
                        showscale=False, 
                        opacity=0.5
                    ),
                    text=filtered_df.index.values,  
                    textposition="top center",
                    textfont=dict(size=10, color="black"),
                    name="Pickups",
                    customdata=filtered_df[["PickUpLongitude", "PickUpLatitude", "DropOffLongitude", "DropOffLatitude"]],
                    hovertemplate="<b>Pickup: %{text}</b><br>Longitude: %{customdata[0]}<br>Latitude: %{customdata[1]}<br><b>Corresponding Drop-off:</b><br>Longitude: %{customdata[2]}<br>Latitude: %{customdata[3]}<extra></extra>",       
                )
            )

            # Add Drop-off Points with colors based on UserGuid
        fig.add_trace(
            go.Scatter(
                x=dropoff_x_noisy,
                y=dropoff_y_noisy,
                mode="markers+text",
                marker=dict(
                    size=12,
                    color=color_values,  # Same color as pickup
                    colorscale=colorscale,
                    opacity=0.5,
                    showscale=False,
                    symbol='x'
                ),
                text=filtered_df.index,
                textposition="bottom center",
                textfont=dict(size=10, color="black"),
                name="Drop-offs",
                customdata=filtered_df[["DropOffLongitude", "DropOffLatitude", "PickUpLongitude", "PickUpLatitude"]],
                hovertemplate="<b>Drop-off: %{text}</b><br>Longitude: %{customdata[0]}<br>Latitude: %{customdata[1]}<br><b>Corresponding Pickup:</b><br>Longitude: %{customdata[2]}<br>Latitude: %{customdata[3]}<extra></extra>",
            )
        )
        # Update layout
        fig.update_layout(
            title="Pickup & Drop-off Locations",
            xaxis_title="Longitude",
            yaxis_title="Latitude",
            width=900,
            height=650,
            map_center_lon=pd.concat([filtered_df["PickUpLongitude"], filtered_df["DropOffLongitude"]]).mean(),
            map_center_lat=pd.concat([filtered_df["PickUpLatitude"], filtered_df["DropOffLatitude"]]).mean(),
            showlegend=False
        )
        fig.show()
    
    def plot_single_trip_route(self, user_guid):
        """
        Plots the Route of a single trip.
        """
        trip_entry = self.df.iloc[user_guid]
        using_gh  = False
        if "GraphHopperRoute" in self.df.columns:
            lons, lats = zip(*trip_entry["GraphHopperRoute"]) 
            using_gh = True
        elif "OSMNXRoute" in self.df.columns:
            lons, lats = zip(*trip_entry["OSMNXRoute"]) 
        else:
            print("Neither GraphHopperRoute or OSMNXRoute columns exist yet")
        
        
        fig = go.Figure()
        
        # Add pickup points
        fig.add_trace(
            go.Scattermap(
                lat=[trip_entry["PickUpLatitude"]],
                lon=[trip_entry["PickUpLongitude"]],
                mode="markers",
                marker=dict(
                    size=(10),  # Marker size based on passenger count
                    color="blue",
                    opacity=0.66,  
                ),
                name="Pickups"  # Legend entry for pickups
            )
        )
        
        # Add drop-off points
        fig.add_trace(
            go.Scattermap(
                lat=[trip_entry["DropOffLatitude"]],
                lon=[trip_entry["DropOffLongitude"]],
                mode="markers",
                marker=dict(
                    size=(10),  # Marker size based on passenger count
                    color="red",
                    opacity=0.66,
                ),
                name="Drop-off"  # Legend entry for drop-offs
            )
        )

        # draw route
        fig.add_trace(
            go.Scattermap(
                lon=lons,  
                lat=lats,  
                mode="lines",
                line=dict(width=3),
                opacity=0.6,
                showlegend=False,
                name="Route"
            )
        )
                
        # Update layout - center, and zoom level
        routing_text = "GraphHopper used for routing" if using_gh == True else "OSMNX used for routing"
        fig.update_layout(
            title = (
                f"Trip {user_guid} - between {trip_entry['PickUpStartTime'].strftime('%H:%M')} "
                f"and {trip_entry['DropOffEndTime'].strftime('%H:%M')} | "
                f"{routing_text}"
            ),
            template="plotly",  # Use plotly template for styling
            showlegend=True,  # Enable legend to toggle visibility of pickups and drop-offs
            map_center_lon=sum(lons)/len(lons),
            map_center_lat=sum(lats)/len(lats),
            width=800,  # Adjust width of the figure
            height=600,  # Adjust height of the figure
            map_zoom=12
        )
        
        fig.update_layout(map_style="carto-positron")
        fig.show()
        
    def plot_reservation_timings(self):
        """
        Plots a histogram plot indicating the frequency of trips reserved within the hour of day
        """
        hours_df =  self.df["SimulatedTime"].dt.hour
        hour_counts = hours_df.value_counts().sort_index()

        # Create the histogram
        fig = px.histogram(
            self.df,
            x=self.df["SimulatedTime"].dt.hour, 
            nbins=24,  # 24 bins for 24 hours
            title="Distribution of Bookings Over the Time of Day",
            labels={"Hour (Selected)": "Hour of Day"}, 
            color=self.df["SimulatedTime"].dt.hour
        )

        fig.update_layout(
            bargap=0.2,
            xaxis=dict(
                title="Hour of Day",
                tickmode="linear", 
                tick0=0, 
                dtick=1, 
                range=[0, 23]  # Ensure x-axis only shows 0-23
            ),
            
            yaxis=dict(
                title="Number of Bookings",
                tickmode="linear",
                tick0=0,
                dtick=max(1, int(hour_counts.max()/10))
            ),
            showlegend = False
        )

        fig.show()
        
    def plot_days_of_trips(self):
        """
        Plots a histogram plot indicating the frequency of trips on weekdays 
        """   
        # Define all weekdays
        days_of_week_array = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        # Count occurrences of each weekday
        weekday_counts = self.df["TripWeekday"].value_counts().reindex(days_of_week_array, fill_value=0)

        tick_setp = weekday_counts.values.max() // 10

        # Convert to DataFrame
        weekday_df = pd.DataFrame({"TripWeekday": weekday_counts.index, "Count": weekday_counts.values})

        # Create histogram (bar chart) with all days present
        fig = px.bar(
            weekday_df,
            x="TripWeekday",
            y="Count",
            title="Distribution of Trips Over Weekdays",
            color="TripWeekday"
        )

        # Adjust layout
        fig.update_layout(
            bargap=0.5,
            xaxis=dict(title="Weekday"),
            yaxis=dict(
                title="Number of Trips",
                tickmode="linear",  # Ensures regular spacing
                tick0=0,  # Starts at 0
                dtick=tick_setp,
                tickformat=".0f"  # Ensures integer formatting
            ),
            showlegend = False
        )

        fig.show() 
    
    def plot_trip_timings(self, interval_mins=15):
        """
        
        """
        # Convert to decimal hour
        self.df["TripHourDecimal"] = (
            self.df["PickUpStartTime"].dt.hour +
            self.df["PickUpStartTime"].dt.minute / 60
        )
        
        n_bins = int((24 * 60) / interval_mins)
        fig = px.histogram(
            self.df,
            x="TripHourDecimal",
            nbins=n_bins,
            labels={"TripHourDecimal": "Time of Day"},
            title="Number of Trips Starting Throughout the Day",
        )

        fig.update_layout(
            xaxis=dict(
                tickmode="array",
                tickvals=list(range(0, 25, 2)),
                ticktext=[f"{h:02d}:00" for h in range(0, 25, 2)],
                range=[0, 24]
            ),
            yaxis_title="Number of Trips",
            bargap=0.01,
        )
        fig.show()
    
    def plot_distance_and_duration_boxplots(self, routing_method="OSMNX"):
        
        df = self.df.copy()
        
        if routing_method == "GraphHopper":
            duration_column = 'GHEstimatedDuration'
            distance_column = 'GHEstimatedTripDistance'
            
            if "GHEstimatedDuration" not in self.df.columns:
                print("GraphHopper Related Columns are not yet added to dataframe")
                print("Try Running 'add_graph_hopper_route_columns() first'")
                return
        else:
            duration_column = 'OSMNXEstimatedDuration'
            distance_column = 'OSMNXEstimatedTripDistance'
            if "OSMNXEstimatedDuration" not in self.df.columns:
                print("Neither GraphHopperRoute or OSMNXRoute columns exist yet")
                print("Try Running 'add_osmnx_route_columns() first'")
                return
        
        df['DurationMinutes'] = df[duration_column].dt.total_seconds() / 60

        fig = go.Figure()

        fig.add_trace(go.Box(
            y=df[distance_column],
            name='Distance',
            boxmean='sd',
            marker_color='blue',
            yaxis='y1'
        ))

        fig.add_trace(go.Box(
            y=df['DurationMinutes'],
            name='Duration',
            boxmean='sd',
            marker_color='green',
            yaxis='y2'
        ))

        fig.update_layout(
            title='Box Plots: Distance and Duration (Estimated With OSMNX)',
            width=800,         
            height=800,        
            xaxis=dict(showticklabels=False), # no x-tick labels
            yaxis=dict(
                title='Estimated Trip Distance (m)',
            ),
            yaxis2=dict(
                title='Estimated Duration (min)',
                overlaying='y',
                side='right'
            ),
            showlegend=False
        )

        fig.show()
    
    def plot_trip_timings_for_lines(self, min_time=None, max_time=None, line_ids=None, marker_size=10):
        """
        Plots the 'PickUpStartTime' of trip entries. Entries assigned to the same line have the same y value. Entries with the same symbol and y line start from the same ChainingLocation
        """
        filtered_df = self.filter_df_by_time(min_time, max_time)    
        
        # convert to categorical
        filtered_df["Line"] = filtered_df["Line"].astype(str)
        filtered_df["StartChainingLocation"] = filtered_df["StartChainingLocation"].astype(str)
        
        filtered_df['TimeOfDay'] = filtered_df['PickUpStartTime'].dt.floor('min')  # Floors to nearest minute
        
        if filtered_df.empty:
            print(f"No Trips Between {min_time} and {max_time}")
            return
        
        
        if line_ids is None:
            unique_lines = sorted(filtered_df["Line"].unique())
        else:
            line_ids = [str(id) for id in line_ids]
            filtered_df = filtered_df[filtered_df['Line'].isin(line_ids)]
        
        # dont = include trips not in line
        filtered_df = filtered_df[filtered_df['Line'] != -1]
        
        # create color mapping
        unique_lines =  [line_id for line_id in self.lines]
        colors = px.colors.qualitative.Dark24
        id_to_color = {line_id: colors[i % len(colors)] for i, line_id in enumerate(sorted(unique_lines))}
          
        # create symbol mapping
        id_to_symbol = {
            chaining_location_id: 'diamond' if int(chaining_location_id) % 2 else 'cross'
            for chaining_location_id in filtered_df['StartChainingLocation'].unique()
        }  
           
        # create plot
        fig = px.scatter(
            filtered_df,
            x='TimeOfDay',
            y='Line',  # Keep y constant to place all dots on the same line
            color="Line",  # Use 'Line' to color the dots by LineID
            color_discrete_map=id_to_color,  # Apply custom color mapping here
            title="Start Time of Trips Over Time by Line/Chaining_Location",
            labels={'TimeOfDay': 'Time of Day', 'y': 'Trips', 'Line': 'Line'},
            symbol='StartChainingLocation',
            symbol_map=id_to_symbol,
            opacity=0.5
        )
        
        # update layout
        fig.update_traces(
            hovertemplate="<b>Time:</b> %{x|%H:%M}",
            marker=dict(size=marker_size)
        )

        # Round min and max to the nearest hour
        min_time = self.df['PickUpStartTime'].min().replace(minute=0, second=0, microsecond=0)
        max_time = self.df['PickUpStartTime'].max().replace(minute=0, second=0, microsecond=0)

        # If the minute is greater than or equal to 30, round up to the next hour
        min_time = min_time - pd.Timedelta(hours=1)
        max_time = max_time + pd.Timedelta(hours=1)

        # Generate tick positions (every 30 minutes) between rounded min and max times
        tickvals = pd.date_range(start=min_time, end=max_time, freq='30min')

        # Generate tick labels (formatted as HH:MM)
        ticktext = tickvals.strftime('%H:%M')
        
        fig.update_layout(
            xaxis=dict(
                tickformat='%H:%M',
                tickvals=tickvals,
                ticktext=ticktext,
                dtick=60000 * 1,
                title='Time of Day',
                showgrid=True,
                showticklabels=True,
            ),
            xaxis_title_standoff=30,
            yaxis=dict(
                showgrid=False,
                showticklabels=True,
                tickmode="array",
                tickvals=line_ids
            ),
            showlegend=True,
            height=400
        )

        # Show the plot
        fig.show()
              
    def plot_estimated_arrivals_for_chaining_location(self, start_chaining_location_id, min_time=None, max_time=None, marker_size=10):
        """
        Plots the 'EstimatedTimeAtStartChainingLocation' of trip entries at the given StartChainingLocation
        """
        filtered_df = self.filter_df_by_time(min_time, max_time)    
        
        filtered_df['TimeOfDay'] = filtered_df['EstimatedTimeAtStartChainingLocation'].dt.floor('min')  # Floors to nearest minute
        
        if filtered_df.empty:
            print(f"No Trips Between {min_time} and {max_time}")
            return
        
        # reduce dataframe to just the single StartChainingLocation
        filtered_df = filtered_df[filtered_df['StartChainingLocation'] == start_chaining_location_id]

        # create plot
        fig = px.scatter(
            filtered_df,
            x='EstimatedTimeAtStartChainingLocation',
            y='Instance',  # Keep y constant to place all dots on the same line
            color='Instance',  # Use 'Line' to color the dots by LineID
            title= f"Estimated Arrival Time of Trips at ChainingLocation {start_chaining_location_id}",
            labels={'TimeOfDay': 'Time of Day', 'y': 'Trips', 'Line': 'Line'},
            symbol='StartChainingLocation',
            opacity=0.5
        )
        
        # update layout
        fig.update_traces(
            hovertemplate="<b>Time:</b> %{x|%H:%M}",
            marker=dict(size=marker_size)
        )

        # Round min and max to the nearest hour
        min_time = self.df['PickUpStartTime'].min().replace(minute=0, second=0, microsecond=0)
        max_time = self.df['PickUpStartTime'].max().replace(minute=0, second=0, microsecond=0)

        # If the minute is greater than or equal to 30, round up to the next hour
        min_time = min_time - pd.Timedelta(hours=1)
        max_time = max_time + pd.Timedelta(hours=1)

        # Generate tick positions (every 30 minutes) between rounded min and max times
        tickvals = pd.date_range(start=min_time, end=max_time, freq='30min')

        # Generate tick labels (formatted as HH:MM)
        ticktext = tickvals.strftime('%H:%M')

        fig.update_layout(
            xaxis=dict(
                tickformat='%H:%M',  # Hour:Minute format for time
                 tickvals=tickvals,  # Set custom tick positions
                ticktext=ticktext,  # Set custom tick labels
                dtick=60000 * 1,  # Every 5 minutes (60000 ms * 5)
                title='Time of Day',
                showgrid=True,
                showticklabels=True,
            ),
            xaxis_title_standoff=30 ,
            showlegend=True,  # Show legend for different lines
            height=400
        )

        # Show the plot
        fig.show()    
