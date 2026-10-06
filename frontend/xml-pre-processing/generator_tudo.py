#!/usr/bin/python3

import argparse
import sys
import random
from datetime import datetime, time
from pathlib import Path

import pandas as pd
import json
from shapely.geometry import shape, Point, box

# Depot / first charging station for the Paderborn (SICP) scenario, taken from
# "CS_Salzkotten" in this repo's InitSimulationBaseData_pb_*.json files (the
# same coordinates appear in every pb_* fleet-instance file). Used only when
# --guarantee-feasible is set; override with --depot-lat/--depot-lon for a
# different area or depot.
DEFAULT_DEPOT_LAT = 51.670479
DEFAULT_DEPOT_LON = 8.604126

# On-/off-boarding time: a vehicle can't complete a pickup or drop-off the
# instant it arrives. Matches Customer._entry_time's default in
# custom_sim/models_cs.py (Custom Simulation's own on-/off-boarding constant,
# used identically for both sides of a trip - see custom_simulation.py's
# `2 * service_time` usage). Used only when --guarantee-feasible is set;
# override with --service-seconds.
DEFAULT_SERVICE_SECONDS = 60

# Fleet operating hours: a vehicle can't start before the fleet's horizon
# begins, no matter how early the booking was placed. Matches
# custom_simulation.py's `self.horizon_start = min(cab.start_schedule for cab
# in self.cab_fleet)` - confirmed 05:00-23:00 for every cab in every
# InitSimulationBaseData_pb_*.json file (same family as the depot coords).
# Used only when --guarantee-feasible is set; override with
# --horizon-start/--horizon-end.
DEFAULT_HORIZON_START = [5, 0]
DEFAULT_HORIZON_END = [23, 0]

# All 3 charging stations for the Paderborn (SICP) scenario, confirmed
# identical across every InitSimulationBaseData_pb_*.json file. Used only
# when --guarantee-feasible is set and no --base-data file is given.
DEFAULT_CHARGING_STATIONS = [
	(51.670479, 8.604126),   # CS_Salzkotten (== DEFAULT_DEPOT_LAT/LON)
	(51.683923, 8.721561),   # CS_Moenkeloh
	(51.604758, 8.650555),   # CS_Wewelsburg
]


#=======================================================================
#=======================================================================
#=======================================================================



def format_time_with_timezone(dt, offset="+02:00"):
	"""
	Formats the datetime object to include the timezone offset.
	Example: "2025-10-18T14:00:00+02:00"
	"""
	return dt.strftime("%Y-%m-%dT%H:%M:%S") + offset


#=======================================================================


def read_from_tudo_json(_file_name, _date_time_cols=["SimulatedTime","start_time","end_time"]):
	
	try:
		# Try to read the CSV file
		df = pd.read_json(_file_name)
	except FileNotFoundError:
		# If the file is not found, return no data frame
		return None
	
	for col in _date_time_cols:
		df[col] = pd.to_datetime(df[col]) 
	
	return df

#=======================================================================


def read_polygon_from_geojson(file_name="sicpArea_merged.geojson"):
	# read geojson
	with open(file_name) as f:
		data = json.load(f)
    
	polygon = shape(data["features"][0]["geometry"])

	return polygon


#=======================================================================


def read_base_data_essentials(filepath):
	"""
	Minimal, self-contained extract of just what --guarantee-feasible needs
	from a BaseData file (chargingPoints, cabSchedules): depot, the full
	charging-station list, and fleet operating hours.

	Deliberately not importing utils.py::read_base_json, which already
	parses BaseData files more completely elsewhere in this repo: that
	function also parses chain routes, chaining locations, pro schedules,
	and parking locations, none of which this pipeline uses, and importing
	it would make this file depend on the fleetplanning repo root for a
	second, avoidable thing (on top of custom_sim.routing, which is
	unavoidable). See DEFAULT_DEPOT_LAT/LON, DEFAULT_HORIZON_START/END, and
	DEFAULT_CHARGING_STATIONS for the fallback values used when no
	--base-data file is given.

	Returns a dict with:
		'charging_stations': list of (lat, lon) tuples
		'depot': (lat, lon) - the first charging station
		'horizon_start', 'horizon_end': [hour, minute] - min/max across all
			cabSchedules, matching custom_simulation.py's
			horizon_start = min(cab.start_schedule for cab in cab_fleet)
	"""
	with open(filepath) as f:
		data = json.load(f)

	charging_points = data.get("chargingPoints", [])
	if not charging_points:
		raise ValueError(f"No chargingPoints found in --base-data file '{filepath}'.")
	charging_stations = [
		(cp["Location"]["Latitude"], cp["Location"]["Longitude"])
		for cp in charging_points
	]

	cab_schedules = data.get("cabSchedules", [])
	if not cab_schedules:
		raise ValueError(f"No cabSchedules found in --base-data file '{filepath}'.")
	starts = [datetime.fromisoformat(cab["schedule"]["StartTime"]) for cab in cab_schedules]
	ends = [datetime.fromisoformat(cab["schedule"]["EndTime"]) for cab in cab_schedules]
	horizon_start_dt = min(starts)
	horizon_end_dt = max(ends)

	return {
		"charging_stations": charging_stations,
		"depot": charging_stations[0],
		"horizon_start": [horizon_start_dt.hour, horizon_start_dt.minute],
		"horizon_end": [horizon_end_dt.hour, horizon_end_dt.minute],
	}


#=======================================================================


def build_feasibility_router(area_polygon, area_id):
	"""
	Build a custom_sim.routing.Router for real drive-time queries (used by
	--guarantee-feasible).

	The import is deliberately deferred to inside this function, not the
	module top, so this script still loads (and runs, for every case that
	doesn't need routing) in copies of this pipeline that don't have
	custom_sim available - only the --guarantee-feasible code path fails,
	with a clear message, instead of every invocation failing at import time.
	"""
	try:
		# custom_sim lives at the fleetplanning repo root, three directories
		# above this file (frontend/xml-pre-processing/generator_tudo.py).
		# Python only puts this script's own directory on sys.path by
		# default, so make the repo root importable too, same as any script
		# reaching for a sibling package it isn't installed alongside.
		fleetplanning_root = Path(__file__).resolve().parents[2]
		if str(fleetplanning_root) not in sys.path:
			sys.path.insert(0, str(fleetplanning_root))
		from custom_sim.routing.router import Router
	except ImportError as exc:
		raise RuntimeError(
			"--guarantee-feasible requires custom_sim.routing, which is only "
			"available when this script runs from inside the fleetplanning "
			"repo (not this standalone copy)."
		) from exc

	return Router(area_polygon, profile="cab", area_id=area_id)


#=======================================================================


def add_route_leg_seconds(df, router, depot_coord, charging_stations):
	"""
	Add the three router drive times (seconds, kept as router returns them -
	no /60 float conversion, see calculate_bounds_centered) that
	--guarantee-feasible needs: depot->pickup, pickup->dropoff,
	dropoff->nearest charging station. Computed for every row regardless of
	choice_dropoff.

	'time_DO_nCS_s' is the minimum over all charging_stations - nearest to
	the DROPOFF, not the depot (matches custom_simulation.py's
	trailing-insertion charging station choice).

	Adds 'time_depot_PU_s', 'time_PU_DO_s', 'time_DO_nCS_s' to a copy of df.
	"""
	df = df.copy()

	df['time_depot_PU_s'] = df.apply(
		lambda row: router.shortest_path(depot_coord, (row['origin_lat'], row['origin_lon']))['time_s'],
		axis=1,
	)
	df['time_PU_DO_s'] = df.apply(
		lambda row: router.shortest_path(
			(row['origin_lat'], row['origin_lon']),
			(row['destination_lat'], row['destination_lon']),
		)['time_s'],
		axis=1,
	)
	df['time_DO_nCS_s'] = df.apply(
		lambda row: min(
			router.shortest_path((row['destination_lat'], row['destination_lon']), cs)['time_s']
			for cs in charging_stations
		),
		axis=1,
	)

	return df


#=======================================================================


def write_json_to_file(data_frame: pd.DataFrame, filename: str, n_entries: int=5, debug: bool=False) -> None:
	"""
	Write an XML element tree to a file.

	:param data_frame: The data frame to be written to the file.
	:param filename: The name of the file to write the json to.
	"""
	
	#data_frame.to_json(filename, orient='records', date_format='iso', indent=4)
	simulation_steps = []
	
	for idx, (i, row) in enumerate(data_frame.iterrows()):
		if idx >= n_entries:
			break
		
		if row["choice_dropoff"]:
			target_time = {
				"StartTime": format_time_with_timezone(row["tw_lower"]),
				"EndTime": format_time_with_timezone(row["tw_upper"])
			}
			pickup_time = None
		else:
			target_time = None
			pickup_time = {
				"StartTime": format_time_with_timezone(row["tw_lower"]),
				"EndTime": format_time_with_timezone(row["tw_upper"])
			}
		
		step = {
			"requestParameter": {
				"UserGuid": str(row["ID"]),
				"CurrentLocation": {
					"Longitude": row["origin_lon"],
					"Latitude": row["origin_lat"]
				},
				"TargetLocation": {
					"Longitude": row["destination_lon"],
					"Latitude": row["destination_lat"]
				},
				"targetTime": target_time,
				"pickupTime": pickup_time,
				"needRamp": False,
				"requestedAdults": 1,
				"requestedChilds": 0,  # Assuming no children as data is not in DataFrame
				"luggage": 0,  # Assuming no luggage as data is not in DataFrame
				"personalPreferences": {
					"allowCarpooling": False,
					"toleratedDelayBefore": 300,
					"toleratedDelayAfter": 300
				}
			},
			"SimulatedTime": format_time_with_timezone(row["SimulatedTime"]),
			"bookProposal": True,
			"createReport": True			
		}
		if ( debug == True ):
			step["Booking_ID"] = row['unique_trip_id']
			
		simulation_steps.append(step)
	
	# Final JSON structure
	final_structure = {
		"simulationSteps": simulation_steps,
		"createInitialReport": True
	}
	
	with open(filename, "w") as f:
		json.dump(final_structure, f, indent='\t')
	
	return


#=======================================================================
#=======================================================================
#=======================================================================


def sample_data(df, sample_size=None, random_seed=None):
	"""
	Selects a uniform random sample of rows from the DataFrame. Uses the global random state if no seed is provided;
	otherwise, uses a local random number generator with the specified seed.
	If no sample_size is provided or if sample_size exceeds the size of the DataFrame, returns the original DataFrame.

	Parameters:
		df (pd.DataFrame): The original DataFrame.
		sample_size (int, optional): The number of rows to sample. If None or greater than the DataFrame size, returns the original DataFrame.
		random_seed (int, optional): A random seed for reproducibility. If None, uses the global random state.

	Returns:
		pd.DataFrame: A DataFrame containing the sampled rows or the original DataFrame if no sample_size is provided or it exceeds DataFrame size.
	"""
	
	# If sample_size >= len(df), or <= 0, the function returns the full dataset.
	
	# Handle cases where sample_size is None or invalid
	if sample_size is None or sample_size <= 0 or sample_size >= len(df):
		#print("STRANGE CASE")
		if sample_size >= len(df):
			print(f"# Sample size {sample_size} exceeds the DataFrame size {len(df)}. Returning the original DataFrame.")
		return df.copy()  # Return a copy of the original DataFrame
	
	# Create a copy of the original DataFrame to avoid modifying it
	df_copy = df.copy()

	# Generate a list of row indices
	all_indices = list(df_copy.index)

	# Sample indices using global or local random number generator
	if random_seed is None:
		sampled_indices = random.sample(all_indices, sample_size)
	else:
		local_random = random.Random()
		local_random.seed(random_seed)
		sampled_indices = local_random.sample(all_indices, sample_size)
	
	# Select rows based on sampled indices
	sampled_df = df_copy.loc[sampled_indices].reset_index(drop=True)

	return sampled_df


#=======================================================================
#=======================================================================
#=======================================================================


def filter_by_time_range(df, start_time, end_time):
	"""
	Filters the DataFrame to include only rows where 'start_time' is between the
	specified start and end times (hours and minutes). The returned DataFrame is
	a copy of the filtered data.

	Parameters:
		df (pd.DataFrame): The DataFrame containing the 'start_time' column.
		start_time (list): A list representing the start time in the format [hh, mm].
		end_time (list): A list representing the end time in the format [hh, mm].

	Returns:
		pd.DataFrame: A copy of the filtered DataFrame.
	"""

	if df.empty or df is None:
		return df

	# Convert start_time and end_time lists to datetime.time objects
	start_time_obj = time(hour=start_time[0], minute=start_time[1])
	end_time_obj = time(hour=end_time[0], minute=end_time[1])

	# Extract the time part from 'start_time'
	pickup_times = df["start_time"].dt.time
	
	# Filter rows based on the time range
	filtered_df = df[(pickup_times >= start_time_obj) & (pickup_times <= end_time_obj)]
	
	# Return a copy of the filtered DataFrame to avoid modifying the original DataFrame
	return filtered_df.copy()


#=======================================================================
#=======================================================================
#=======================================================================


def point_in_polygon(lon, lat, polygon, allow_boundary=True):
	"""
	Check whether a single point is inside (or on the boundary of) a polygon.
	"""
	p = Point(lon, lat)

	if allow_boundary:
		return polygon.contains(p) or polygon.touches(p)
	else:
		return polygon.contains(p)

#=======================================================================

def filter_polygon(df,
	polygon,
	lon1="origin_lon", lat1="origin_lat",
	lon2="destination_lon", lat2="destination_lat",
	allow_boundary=True):
	"""
	Filter rows where BOTH coordinate pairs lie within the polygon.
	"""
	
	def both_inside(row):
		inside1 = point_in_polygon(row[lon1], row[lat1], polygon, allow_boundary)
		inside2 = point_in_polygon(row[lon2], row[lat2], polygon, allow_boundary)
		return ( inside1 and inside2 )
	
	mask = df.apply(both_inside, axis=1)
	return df[mask].copy()

#=======================================================================
#=======================================================================
#=======================================================================


def process_booking_data(df, new_date=None, tw_dict=None, seed=None,
							guarantee_feasible=False, router=None, depot_coord=None,
							charging_stations=None,
							service_seconds=DEFAULT_SERVICE_SECONDS,
							horizon_start=DEFAULT_HORIZON_START, horizon_end=DEFAULT_HORIZON_END,
							max_shift_minutes=None):
	"""
	Processes booking data by cleaning and transforming the DataFrame.

	The function:
	- Removes unneeded columns.
	- Adds an 'ID' column based on 'SimulatedTime' timestamps.
	- Computes lower and upper bounds for pickup and dropoff times.

	Parameters:
		df (pd.DataFrame): The original DataFrame with booking data.
		new_date (List): Artificial date to convert all entries to.
		tw_dict (List): Optional list with tw lengths (10 minute default)
		guarantee_feasible (bool): If True, in addition to the flat 5-minute
			sanity buffer (tw_lower must stay after SimulatedTime, always
			active), also drop requests that aren't servicable by a vehicle
			starting cold at the depot (unlimited fleet) and shift the rest
			forward only as far as needed to become servicable - see
			calculate_bounds_centered for the full logic. Requires router
			and depot_coord.
		router: A custom_sim.routing.Router instance (see
			build_feasibility_router). Only used when guarantee_feasible.
		depot_coord (tuple): (lat, lon) of the depot. Only used when
			guarantee_feasible.
		charging_stations (list): [(lat, lon), ...] of all charging
			stations, for the return-to-charging feasibility check. Only
			used when guarantee_feasible.
		service_seconds (int): On-/off-boarding time. Only used when
			guarantee_feasible.
		horizon_start, horizon_end (list): [hour, minute] fleet operating
			hours. Only used when guarantee_feasible.
		max_shift_minutes (int): Cap on how far a window may be shifted
			forward to become feasible; None = unlimited. Only used when
			guarantee_feasible.

	Returns:
		df.copy (pd.DataFrame): The processed DataFrame.
	"""

	if df.empty or df is None:
		print("Warning: No input data provided. No file is created")
		return df
	
	# Step 1: Make a copy of the original DataFrame
	df_copy = df.copy()

	# Step 2: Keep only the needed columns with updated names
	needed_columns = [
		'person_id', 'trip_number', 
		'origin_lat', 'origin_lon', 
		'destination_lat', 'destination_lon',
		'start_time', 'end_time',
		'SimulatedTime',
	]
	df_copy = df_copy[needed_columns]
	df_copy['unique_trip_id'] = df_copy['person_id'].astype(str) + '_' + df_copy['trip_number'].astype(str)
	
	#print(df_copy['unique_trip_id'].is_unique)
	#sys.exit(-1)
	
	# time window lengths
	if tw_dict is None:
		# Default: all requests get a time window of 10
		tws = {rid: 10 for rid in df_copy["unique_trip_id"]}
	else:
		# Use the provided dictionary, falling back to 10 if ID is missing
		tws = {rid: tw_dict.get(rid, 10) for rid in df_copy["unique_trip_id"]}

	# hardcoded random choice for customers preference for pick-up/drop-off - runs for every
	# request regardless of --sample-size, so --seed matters even with no sampling
	if seed is not None:
		random.seed(seed)
		
	df_copy["choice_dropoff"] = [random.random() > 0.5 for _ in range(len(df_copy))]

	# router leg times, only needed under guarantee_feasible
	if guarantee_feasible:
		df_copy = add_route_leg_seconds(df_copy, router, depot_coord, charging_stations)

	# under guarantee_feasible, also decides whether the row stays at all
	# (the 'keep' column) - see calculate_bounds_centered
	init_size = len(df_copy)
	df_copy[['SimulatedTime', 'tw_lower', 'tw_upper', 'keep']] = df_copy.apply(
		lambda row: calculate_bounds_centered(
			row,
			tws=tws,
			buffer_minutes=5,
			new_date=new_date,
			guarantee_feasible=guarantee_feasible,
			service_seconds=service_seconds,
			horizon_start=horizon_start if guarantee_feasible else None,
			horizon_end=horizon_end if guarantee_feasible else None,
			max_shift_minutes=max_shift_minutes,
		),
		axis=1,
	)

	if guarantee_feasible:
		df_copy = df_copy[df_copy['keep']].drop(columns=['keep'])
		print(
			f"# Feasibility filter: dropped {init_size - len(df_copy)} requests not servicable by "
			f"an unlimited fleet ({init_size} -> {len(df_copy)})"
		)
	else:
		df_copy = df_copy.drop(columns=['keep'])

	# Step 3: Sort by 'SimulatedTime' (time the booking was placed)
	df_copy = df_copy.sort_values(by='SimulatedTime').reset_index(drop=True)

	# Step 4: Add an ID column in ascending order
	df_copy['ID'] = df_copy.index + 0
	
	
	# Step 6: Drop columns no longer needed for output
	df_copy = df_copy.drop(columns=['start_time', 'end_time'])
	
	# Step 7: Rearrange the columns to have 'ID' first and 'SimulatedTime' second
	column_order = ['ID', 'SimulatedTime'] + [col for col in df_copy.columns if col not in ['ID', 'SimulatedTime']]
	df_copy = df_copy[column_order]
	
	# Rename columns to be valid XML tags
	#df_copy.columns = [col.replace(' ', '_').replace('(', '').replace(')', '') for col in df_copy.columns]
	
	# tw_lower/tw_upper carry a "must be at least this late" guarantee -
	# floor() could round below it on sub-second float noise, so round up.
	# SimulatedTime has no such guarantee, floor is fine.
	for col in ['tw_lower', 'tw_upper']:
		if pd.api.types.is_datetime64_any_dtype(df_copy[col]):
			df_copy[col] = df_copy[col].dt.ceil('s')
	if pd.api.types.is_datetime64_any_dtype(df_copy['SimulatedTime']):
		df_copy['SimulatedTime'] = df_copy['SimulatedTime'].dt.floor('s')
	
	#print(df_copy)
	#sys.exit(-1)
	
	return df_copy

#=======================================================================

def calculate_bounds_centered(row, tws, buffer_minutes=5, new_date=None,
								guarantee_feasible=False, service_seconds=None,
								horizon_start=None, horizon_end=None,
								max_shift_minutes=None):
	"""
	Window is centered on the natural pickup/dropoff time. Sanity buffer
	(always active): tw_lower >= created_at + buffer_minutes.

	Under guarantee_feasible, also decides whether the row is servicable by
	a vehicle starting cold at the depot (unlimited fleet): drops it if
	not, else shifts forward only as far as needed. Formulas match
	custom_sim/custom_simulation.py's
	_find_time_feasible_candidates_pure_cab for a fresh vehicle / empty
	schedule:
	- time_left = max(created_at, horizon_start)
	- time_right = horizon_end
	- pos_left = depot, pos_right = nearest CS to the dropoff

	Drop conditions (permanent - no shift can fix these):
	- whole TW outside [horizon_start, horizon_end]
	- at tw_lower (earliest the window allows boarding/dropoff), the
	  remaining leg(s) to the nearest CS don't fit before horizon_end
	- from time_left with no shift at all, the whole trip doesn't fit
	  before horizon_end

	Otherwise shift tw_lower/tw_upper forward by max(buffer shift, deadline
	shift) if needed, then drop if that exceeds max_shift_minutes or
	breaks the tw_lower-anchored check above.

	Requires row['time_depot_PU_s'], row['time_PU_DO_s'],
	row['time_DO_nCS_s'] (see add_route_leg_seconds) when guarantee_feasible.

	Parameters:
		row: DataFrame row
		tws: unique_trip_id -> TW length in minutes
		buffer_minutes: sanity buffer
		new_date: optional artificial target date
		guarantee_feasible: if False, only the sanity buffer applies
		service_seconds: on-/off-boarding time; only used if guarantee_feasible
		horizon_start, horizon_end: [hour, minute]; only used if guarantee_feasible
		max_shift_minutes: cap on the forward shift; None = unlimited

	Returns:
		pd.Series([created_at, tw_lower, tw_upper, keep])
	"""

	tw_len = tws.get(row['unique_trip_id'], 10)
	half_window = pd.to_timedelta(tw_len / 2, unit='minutes')
	buffer = pd.to_timedelta(buffer_minutes, unit='minutes')

	center = row['end_time'] if row['choice_dropoff'] else row['start_time']

	if new_date:
		y, m, d = new_date

		def shift_date(ts):
			return ts.replace(year=y, month=m, day=d)

		start = shift_date(row["start_time"])
		center = shift_date(center)
		offset = row["start_time"] - row["SimulatedTime"]
		created_at = start - offset
	else:
		created_at = row["SimulatedTime"]

	tw_lower = center - half_window
	tw_upper = center + half_window

	if not guarantee_feasible:
		min_lower = created_at + buffer
		if tw_lower < min_lower:
			delta = min_lower - tw_lower
			tw_lower += delta
			tw_upper += delta
		return pd.Series([created_at, tw_lower, tw_upper, True])

	hs_h, hs_m = horizon_start
	he_h, he_m = horizon_end
	horizon_start_dt = center.replace(hour=hs_h, minute=hs_m, second=0, microsecond=0)
	horizon_end_dt = center.replace(hour=he_h, minute=he_m, second=0, microsecond=0)

	# whole TW outside horizon, unfixable
	if tw_upper < horizon_start_dt or tw_lower > horizon_end_dt:
		return pd.Series([created_at, tw_lower, tw_upper, False])

	time_left = max(horizon_start_dt, created_at)
	# seconds throughout, no float unit conversion - avoids losing sub-second
	# precision that a later .dt.ceil('s') could otherwise round the wrong way
	service = pd.to_timedelta(service_seconds, unit='s')
	t_depot_pu = pd.to_timedelta(row['time_depot_PU_s'], unit='s')
	t_pu_do = pd.to_timedelta(row['time_PU_DO_s'], unit='s')
	t_do_ncs = pd.to_timedelta(row['time_DO_nCS_s'], unit='s')

	if row['choice_dropoff']:
		# tw_lower = earliest arrival at dropoff; only deboarding still ahead
		min_upper = time_left + t_depot_pu + t_pu_do + 2 * service
		lower_fits = (tw_lower + service + t_do_ncs) <= horizon_end_dt
		departure_fits = (time_left + t_depot_pu + t_pu_do + t_do_ncs + 2 * service) <= horizon_end_dt
	else:
		# both boarding and deboarding still ahead of tw_lower
		min_upper = time_left + t_depot_pu + service
		lower_fits = (tw_lower + t_pu_do + 2 * service + t_do_ncs) <= horizon_end_dt
		departure_fits = (time_left + t_depot_pu + t_pu_do + 2 * service + t_do_ncs) <= horizon_end_dt

	# neither is shift-fixable: shifting only increases tw_lower/time_left
	if not lower_fits or not departure_fits:
		return pd.Series([created_at, tw_lower, tw_upper, False])

	# sanity buffer and deadline are independent lower bounds on the same
	# shift, so the combined minimum is just their max
	shift1 = max(pd.Timedelta(0), (created_at + buffer) - tw_lower)
	shift2 = max(pd.Timedelta(0), min_upper - tw_upper)
	shift_required = max(shift1, shift2)

	if shift_required > pd.Timedelta(0):
		if max_shift_minutes is not None and shift_required > pd.to_timedelta(max_shift_minutes, unit='minutes'):
			return pd.Series([created_at, tw_lower, tw_upper, False])

		# shift also pushes tw_lower forward, which can break lower_fits
		# even though it held naturally; departure_fits is shift-invariant
		shifted_lower = tw_lower + shift_required
		if row['choice_dropoff']:
			still_fits = (shifted_lower + service + t_do_ncs) <= horizon_end_dt
		else:
			still_fits = (shifted_lower + t_pu_do + 2 * service + t_do_ncs) <= horizon_end_dt

		if not still_fits:
			return pd.Series([created_at, tw_lower, tw_upper, False])

		tw_lower += shift_required
		tw_upper += shift_required

	return pd.Series([created_at, tw_lower, tw_upper, True])


#=======================================================================
#=======================================================================
#=======================================================================


def generate(file_name=None,
				geojson_name=None,
				start_time=[0,0],
				end_time=[23,59],
				sample_size=-1,
				new_date=[2024,8,1],
				drop_prebooking=False,
				tw_minutes=10,
				guarantee_feasible=False,
				depot_lat=None,
				depot_lon=None,
				service_seconds=DEFAULT_SERVICE_SECONDS,
				horizon_start=None,
				horizon_end=None,
				base_data_file=None,
				max_shift_minutes=None):
	"""
	Generates an instance.

	The function:
	- reads requests from json file
	- Removes entries outside a time range
	- Removes entries outside a boundary range
	- Takes a sample of the remaining entries
	- Calculates the remaining instance parameters

	Parameters:
		start_time (List): Earliest PU time to include from booking data (default: 00:00).
		end_time (List): Latest PU time to include from booking data (default: 23:59).
		sample_size (int): Max. Number of entries to return (default: -1 [return original df]).
		new_date (List): Artificial date to convert all remaining entries to (default: 2024-08-01).
		sumo_map (str): Path to SUMO xml file for extractiong boundary information from (default: None [no filter]).
		tw_minutes (int): Time window length (in minutes) applied to every request (default: 10).
		guarantee_feasible (bool): If True, in addition to the flat 5-minute
			sanity buffer (always active), also enforce a deadline on every
			request's tw_upper derived from real depot/trip drive times, so
			it stays servicable by a vehicle starting cold at the depot
			(given an unlimited fleet). Requires custom_sim.routing (see
			build_feasibility_router).
		depot_lat, depot_lon (float): Depot / first charging station location,
			only used when guarantee_feasible. Precedence: this override, then
			--base-data (if given), then DEFAULT_DEPOT_LAT/LON.
		service_seconds (int): On-/off-boarding time added to the tw_upper
			deadline, only used when guarantee_feasible (default: 60,
			matching Custom Simulation's own Customer._entry_time default).
			Not sourced from --base-data - it's a Customer/simulation-wide
			default (models_cs.py), not per-area fleet infrastructure.
		horizon_start, horizon_end (list): [hour, minute] fleet operating
			hours, only used when guarantee_feasible. Same precedence as
			depot_lat/depot_lon (default: 05:00/23:00).
		base_data_file (str): Optional path to a BaseData JSON file
			(chargingPoints, cabSchedules). When given, supplies depot,
			the full charging-station list, and horizon in one go - see
			read_base_data_essentials. Only used when guarantee_feasible.
		max_shift_minutes (int): Cap on how far a window may be shifted
			forward to become feasible; None = unlimited. Only used when
			guarantee_feasible.

	Returns:
		pd.DataFrame: The processed DataFrame.
	"""
	if file_name is None:
		raise ValueError("file_name must be provided.")
	
	# --- Load and combine data ---
	df_in = read_from_tudo_json(file_name)
	#print(df_in)
	#sys.exit(-1)
	
	# --- Time filtering ---
	# filter out requests outside time range
	df_proc = filter_by_time_range(df_in,start_time,end_time)

	if drop_prebooking:
		df_proc = df_proc[df_proc['SimulatedTime'].dt.date == df_proc['start_time'].dt.date]
	# --- Spatial filtering ---
	# No geojson given: fall back to a rectangle over the data's own extent (i.e. no real
	# filtering) instead of requiring a boundary file to exist.
	if geojson_name is not None:
		poly = read_polygon_from_geojson(geojson_name)
	elif df_proc.empty:
		poly = None
	else:
		lons = pd.concat([df_proc["origin_lon"], df_proc["destination_lon"]])
		lats = pd.concat([df_proc["origin_lat"], df_proc["destination_lat"]])
		poly = box(lons.min(), lats.min(), lons.max(), lats.max())

	df_proc_sumo = filter_polygon(df_proc.copy(), poly) if poly is not None else df_proc.copy()
	print(f"# Polygon filter: #rows_orig {len(df_proc)}, #rows_left {len(df_proc_sumo)})")

	df_proc = df_proc_sumo
	
	print("# usable bookings after filtering:", len(df_proc))
	
	
	
	# --- Sampling ---
	# sample entries if not all are needed
	df_proc_sample = sample_data(df_proc,sample_size)

	# --- Time windows ---
	# every request gets the same tw_minutes-long window (see calculate_bounds_centered)
	df_tw_tmp = pd.DataFrame()
	df_tw_tmp['unique_trip_id'] = df_proc_sample['person_id'].astype(str) + '_' + df_proc_sample['trip_number'].astype(str)
	tw_dict = { uid: tw_minutes for uid in df_tw_tmp["unique_trip_id"] }

	# --- Feasibility buffer router (only built if actually needed) ---
	router = None
	depot_coord = None
	charging_stations = None
	if guarantee_feasible:
		# precedence: explicit override > --base-data > hardcoded default
		base_data = read_base_data_essentials(base_data_file) if base_data_file else None

		bd_depot_lat, bd_depot_lon = base_data["depot"] if base_data else (DEFAULT_DEPOT_LAT, DEFAULT_DEPOT_LON)
		bd_horizon_start = base_data["horizon_start"] if base_data else DEFAULT_HORIZON_START
		bd_horizon_end = base_data["horizon_end"] if base_data else DEFAULT_HORIZON_END
		charging_stations = base_data["charging_stations"] if base_data else DEFAULT_CHARGING_STATIONS

		resolved_depot_lat = depot_lat if depot_lat is not None else bd_depot_lat
		resolved_depot_lon = depot_lon if depot_lon is not None else bd_depot_lon
		horizon_start = horizon_start if horizon_start is not None else bd_horizon_start
		horizon_end = horizon_end if horizon_end is not None else bd_horizon_end

		depot_coord = (resolved_depot_lat, resolved_depot_lon)
		if geojson_name is not None:
			area_id = Path(geojson_name).stem
		else:
			area_id = "bbox_{:.4f}_{:.4f}_{:.4f}_{:.4f}".format(*poly.bounds)
		print(f"# Building router for feasibility buffer (area_id={area_id})...")
		router = build_feasibility_router(poly, area_id)

	# --- Postprocessing ---
	# process data for output instance and write out files
	df_out = process_booking_data(
		df_proc_sample, new_date, tw_dict,
		guarantee_feasible=guarantee_feasible,
		router=router,
		depot_coord=depot_coord,
		charging_stations=charging_stations,
		service_seconds=service_seconds,
		horizon_start=horizon_start,
		horizon_end=horizon_end,
		max_shift_minutes=max_shift_minutes,
	)
	
	#print(df_proc)
	#sys.exit(-1)

	return df_out




#=======================================================================
#=======================================================================
#=======================================================================
#=======================================================================




def parse_hhmm(value):
	try:
		hour, minute = map(int, value.split(":"))
	except ValueError as exc:
		raise argparse.ArgumentTypeError("Time must be in HH:MM format.") from exc

	if not (0 <= hour <= 23 and 0 <= minute <= 59):
		raise argparse.ArgumentTypeError("Time must be in range 00:00 to 23:59.")

	return [hour, minute]


def parse_new_date(value):
	try:
		year, month, day = map(int, value.split("-"))
	except ValueError as exc:
		raise argparse.ArgumentTypeError("Date must be in YYYY-MM-DD format.") from exc

	return [year, month, day]


def parse_positive_int(value):
	try:
		parsed = int(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError("Must be an integer.") from exc

	if parsed <= 0:
		raise argparse.ArgumentTypeError("Must be a positive integer.")

	return parsed


def parse_args():
	parser = argparse.ArgumentParser(
		description="Generate final booking-request JSON from simulated TUDO requests."
	)
	parser.add_argument(
		"input_file",
		help="Input JSON produced by create_json_with_simulated_time.py",
	)
	parser.add_argument(
		"-o",
		"--output",
		default="",
		help="Output JSON path. Defaults to pb_<size>_<drop_prebooking>_<seed>.json",
	)
	parser.add_argument(
		"--geojson",
		default=None,
		help="GeoJSON polygon for spatial filtering. Default: a rectangle over the data's own extent.",
	)
	parser.add_argument(
		"--new-date",
		type=parse_new_date,
		default=[2025, 10, 18],
		help="Target request day in YYYY-MM-DD format.",
	)
	parser.add_argument(
		"--start-time",
		type=parse_hhmm,
		default=[0, 0],
		help="Earliest pickup time in HH:MM.",
	)
	parser.add_argument(
		"--end-time",
		type=parse_hhmm,
		default=[23, 59],
		help="Latest pickup time in HH:MM.",
	)
	parser.add_argument(
		"--sample-size",
		type=int,
		default=-1,
		help="Number of requests to sample. Use -1 for all requests.",
	)
	parser.add_argument(
		"--seed",
		type=int,
		default=0,
		help="Random seed for sampling and pickup/dropoff preference.",
	)
	parser.add_argument(
		"--drop-prebooking",
		action="store_true",
		help="Drop prebookings (keep only same-day requests).",
	)
	parser.add_argument(
		"--tw-minutes",
		type=parse_positive_int,
		default=10,
		help="Time window length in minutes, applied to every request (default: 10).",
	)
	parser.add_argument(
		"--guarantee-feasible",
		action="store_true",
		help=(
			"In addition to the flat 5-minute sanity buffer (always active), enforce a "
			"per-request deadline on tw_upper derived from real depot/trip drive times, "
			"so every request stays servicable by a vehicle starting cold at the depot "
			"(unlimited fleet). Requires custom_sim.routing (only available running "
			"from the fleetplanning repo)."
		),
	)
	parser.add_argument(
		"--base-data",
		default=None,
		help=(
			"Optional BaseData JSON file (chargingPoints, cabSchedules), only used with "
			"--guarantee-feasible. Supplies depot, the full charging-station list, and "
			"horizon in one go; --depot-lat/--depot-lon/--horizon-start/--horizon-end still "
			"override it if also given. Falls back to hardcoded defaults if omitted."
		),
	)
	parser.add_argument(
		"--depot-lat",
		type=float,
		default=None,
		help=(
			f"Depot latitude, only used with --guarantee-feasible. Overrides --base-data if "
			f"both given (default: {DEFAULT_DEPOT_LAT}, CS_Salzkotten, if neither given)."
		),
	)
	parser.add_argument(
		"--depot-lon",
		type=float,
		default=None,
		help=(
			f"Depot longitude, only used with --guarantee-feasible. Overrides --base-data if "
			f"both given (default: {DEFAULT_DEPOT_LON}, CS_Salzkotten, if neither given)."
		),
	)
	parser.add_argument(
		"--service-seconds",
		type=int,
		default=DEFAULT_SERVICE_SECONDS,
		help=(
			f"On-/off-boarding time added to the tw_upper deadline, only used with "
			f"--guarantee-feasible (default: {DEFAULT_SERVICE_SECONDS}, matching Custom "
			f"Simulation's own Customer._entry_time default). Not sourced from --base-data - "
			f"a Customer/simulation-wide default, not per-area fleet infrastructure."
		),
	)
	parser.add_argument(
		"--horizon-start",
		type=parse_hhmm,
		default=None,
		help=(
			f"Fleet operating hours start, only used with --guarantee-feasible. Overrides "
			f"--base-data if both given (default: "
			f"{DEFAULT_HORIZON_START[0]:02d}:{DEFAULT_HORIZON_START[1]:02d} if neither given, "
			f"matching Custom Simulation's own horizon_start)."
		),
	)
	parser.add_argument(
		"--horizon-end",
		type=parse_hhmm,
		default=None,
		help=(
			f"Fleet operating hours end, only used with --guarantee-feasible. Overrides "
			f"--base-data if both given (default: "
			f"{DEFAULT_HORIZON_END[0]:02d}:{DEFAULT_HORIZON_END[1]:02d} if neither given)."
		),
	)
	parser.add_argument(
		"--max-shift-minutes",
		type=int,
		default=None,
		help=(
			"Cap on how far --guarantee-feasible may shift a window forward to make it "
			"servicable, in minutes. 0 = no shift allowed at all (only requests already "
			"servicable as-is survive). Default: unlimited (shift as far as physically "
			"necessary). Only used with --guarantee-feasible."
		),
	)
	parser.add_argument(
		"--debug-output",
		default="",
		help="Optional debug JSON path with Booking_ID fields.",
	)
	return parser.parse_args()


if __name__ == '__main__':
	args = parse_args()

	random.seed(args.seed)
	df_out = generate(
		args.input_file,
		args.geojson,
		args.start_time,
		args.end_time,
		args.sample_size,
		args.new_date,
		args.drop_prebooking,
		args.tw_minutes,
		args.guarantee_feasible,
		args.depot_lat,
		args.depot_lon,
		args.service_seconds,
		args.horizon_start,
		args.horizon_end,
		args.base_data,
		args.max_shift_minutes,
	)

	real_size = len(df_out.index) if args.sample_size <= 0 else min(args.sample_size, len(df_out.index))
	file_json = args.output or f"pb_{real_size}_{int(args.drop_prebooking)}_{args.seed}_{args.tw_minutes}.json"
	write_json_to_file(df_out, file_json, len(df_out.index))
	print(f"Data successfully exported to {file_json}")

	if args.debug_output:
		write_json_to_file(df_out, args.debug_output, len(df_out.index), True)
		print(f"Data successfully exported to {args.debug_output}")
