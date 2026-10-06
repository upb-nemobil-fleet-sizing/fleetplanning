#!/usr/bin/python

import sys
import requests
import base64
import json
import time
from datetime import datetime
# for import
from pathlib import Path
# Get the parent directory
parent_dir = Path(__file__).resolve().parent.parent

# Add parent directory to sys.path
sys.path.append(str(parent_dir))

from models import Request, Cab, Pro, DemandScenario, VehicleFleet, OperationalArea, FleetAndRequests


class RWAPIError(RuntimeError):
	"""Raised when a Reisewitz API iteration cannot be completed."""


def compute_run_scenario_prefix(operational_area: OperationalArea, base_cab: Cab, base_pro: Pro | None,
								 demand_scenario: DemandScenario) -> str:
	"""Schema prefix (area_NcsNlinesNbcNcpNrqNtw) for a run's output files. Reimplements
	fleet_planning.py's function of the same name (itself reimplementing
	custom_sim/utils_cs.py's build_prefix()) on the run's initial config, so this module
	doesn't import fleet_planning."""
	raw_area = str(getattr(operational_area.operational_area, "description", "") or "").strip().lower()
	area = raw_area.split("_", 1)[0].split("-", 1)[0].split(" ", 1)[0] or "pb"
	area = {"paderborn": "pb", "hoexter": "hx"}.get(area, area)
	if area in ("default", "unknown"):
		plate = str(getattr(base_cab, "licensePlate", "") or "")
		if isinstance(plate, (list, tuple)):
			plate = plate[0] if plate else ""
		plate_prefix = plate.split("-", 1)[0].lower() if "-" in plate else ""
		area = {"pb": "pb", "hx": "hx"}.get(plate_prefix, area)
	area = "".join(ch for ch in area if ch.isalnum()) or "pb"

	cab_energy = float(getattr(base_cab, "total_energy_capacity", 0) or 0)
	cp_power = float(getattr(base_pro, "max_power_supply", 0) or 0) if base_pro is not None else 0.0
	energy = int(round(cab_energy / 1000.0)) if cab_energy >= 1000 else int(round(cab_energy))
	power = int(round(cp_power / 1000.0)) if cp_power >= 1000 else int(round(cp_power))

	routes = getattr(operational_area.pro_routes_and_trips, "chain_routes", []) or []
	windows = set()
	for req in demand_scenario.requests:
		lower, upper = req.tw_lower, req.tw_upper
		if isinstance(lower, str):
			lower = datetime.fromisoformat(lower.replace("Z", "+00:00"))
		if isinstance(upper, str):
			upper = datetime.fromisoformat(upper.replace("Z", "+00:00"))
		if isinstance(lower, datetime) and isinstance(upper, datetime) and upper > lower:
			windows.add(int(round((upper - lower).total_seconds() / 60.0)))
	timewindow = next(iter(windows)) if len(windows) == 1 else 0

	return (
		f"{area}_{len(operational_area.charging_stations)}cs_{int(len(routes) / 2)}lines_"
		f"{energy}bc_{power}cp_{demand_scenario.num_requests}rq_{timewindow}tw"
	)


class rwAPI:
	'''
	Reisewitz Operational Planning 
	'''
	
	def __init__(self, _operations_area: OperationalArea=None,
					_base_url: str="https://nemo.reisewitz.io", _token_url: str="https://idp.reisewitz.io",
					_credentials_file: str="./rw_credentials.txt", _verbose: int=1, _timeout: int=7200, _poll: float=5.0,
					_output_folder: str=None):

		'''
		Constructor
		'''
		# API Parameters
		self.base_url 			= _base_url 			# url for API calls
		self.token_url 			= _token_url 			# url for Token access
		self.credentials_file 	= _credentials_file 	# path to file with credentials
		self.cred_id 			= None 					# credentials ID
		self.cred_sc 			= None 					# credentials secret
		self.token 				= "" 					# bearer token
		self.timeout 			= _timeout				# value to wait for connection timeout
		self.request_timeout	= (30, self.timeout)	# connect/read timeout for HTTP requests
		self.polling_rate 		= _poll					# Time between two completion checks for simulation

		# Instance parameters
		self.operations_data 	= _operations_area 	# OperationArea object (should be static)
		self.output_folder		= _output_folder 	# output subfolder name under log_path

		# Additional Parameters
		self.verbose            = _verbose
	
	# --------------------------------------------------------------------------------------
	
	def json_dumps_datetime_safe(self,obj, **kwargs):
		def convert(o):
			if isinstance(o, datetime):
				return o.isoformat(sep='T')
			raise TypeError(f"Type {type(o)} not serializable")
		
		return json.dumps(obj, default=convert, **kwargs)
	
	# --------------------------------------------------------------------------------------
	
	def set_operations_data(self, input_od: OperationalArea):
		self.operations_data = input_od
		return
	
	# --------------------------------------------------------------------------------------
	
	# Step 1: Read Client ID and Secret from a File
	def read_credentials(self):
		# If credentials have already been provided, return them
		if self.cred_id and self.cred_sc:
			return self.cred_id, self.cred_sc
		
		client_id = None
		client_secret = None
		# Try opening the file to read credentials
		try:
			with open(self.credentials_file, 'r') as file:
				for line in file:
					if line.startswith("Client Id:"):
						client_id = line.split(":", 1)[1].strip()
					elif line.startswith("Client Secret:"):
						client_secret = line.split(":", 1)[1].strip()
			if not client_id or not client_secret:
				raise ValueError("Failed to retrieve Client Id or Client Secret from the file.")
		except FileNotFoundError:
			import getpass
			# If the file doesn't exist, prompt the user for credentials
			print("Credentials could not be retreived. Please enter them manually.")
			client_id = getpass.getpass("Enter Client Id: ")
			client_secret = getpass.getpass("Enter Client Secret: ")
			# store in member variables to not promt again
			self.cred_id = client_id
			self.cred_sc = client_secret
		
		return client_id, client_secret
	
	# --------------------------------------------------------------------------------------
	
	# Step 2: Get Token
	def get_token(self):
		client_id, client_secret = self.read_credentials()
		token_url = f"{self.token_url}/realms/nemobil/protocol/openid-connect/token"  # Token endpoint
		
		# Prepare Basic Authorization header
		api_user = f"{client_id}:{client_secret}"
		api_user_encoded = base64.b64encode(api_user.encode()).decode()  # Encode as Base64
		
		headers = {
			"Content-Type": "application/x-www-form-urlencoded",
			"Authorization": f"Basic {api_user_encoded}"
		}

		# Prepare form data
		data = {
			"grant_type": "client_credentials",
			"scope": "simulation"
		}
		
		# Make the POST request
		response = requests.post(token_url, headers=headers, data=data, timeout=self.request_timeout)
		response.raise_for_status()  # Raise an error for HTTP issues

		# Extract and return the access token
		self.token = response.json().get("access_token")
		if not self.token:
			raise ValueError("Failed to retrieve access token.")
		return 
	
	# --------------------------------------------------------------------------------------
	
	# Step 3: Reset API
	def reset_api(self):
		url = f"{self.base_url}/simulations/v1/reset"  # Reset endpoint
		headers = {"Authorization": f"Bearer {self.token}"}
		response = requests.get(url, headers=headers, timeout=self.request_timeout)
		response.raise_for_status()
		return
	
	# --------------------------------------------------------------------------------------
	
	# Step 4a: Feed JSON Files
	def feed_init_json(self, payload):
		url = f"{self.base_url}/simulations/v1/init-simulation-basedata"  # Feed endpoint
		# Prepare headers
		headers = {
			"Authorization": f"Bearer {self.token}",
			"Content-Type": "application/json"
		}
		
		if self.verbose:
			print(f"Sending payload to API (init with timeout {self.timeout}):")
		# Send the POST request with the JSON payload
		#response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=self.timeout)
		response = requests.post(url, headers=headers, data=self.json_dumps_datetime_safe(payload), timeout=self.request_timeout)
		
		# Check for errors
		response.raise_for_status()
		return
	
	# --------------------------------------------------------------------------------------
	
	# Step 4b: Feed JSON Files
	def feed_request_json(self, payload):
		url = f"{self.base_url}/simulations/v1/simulate"  # Feed endpoint
		# Prepare headers
		headers = {
			"Authorization": f"Bearer {self.token}",
			"Content-Type": "application/json"
		}
		
		
		if self.verbose:
			print(f"Sending payload to API (req with timeout {self.timeout}):")
		# Send the POST request with the JSON payload
		#response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=self.timeout)
		response = requests.post(url, headers=headers, data=self.json_dumps_datetime_safe(payload), timeout=self.request_timeout)
		
		# Check for errors
		response.raise_for_status()
		return response.json()
	
	# --------------------------------------------------------------------------------------
	
	# Step 5a: Retrieve Output
	def check_status(self, uuid):
		url = f"{self.base_url}/simulations/v1/status/{uuid}"
		headers = {"Authorization": f"Bearer {self.token}"}
		response = requests.get(url, headers=headers, timeout=self.request_timeout)
		
		if response.status_code == 401:  # Token expired
			print("Token expired. Fetching a new one...")
			self.get_token()  # Refresh token
			headers["Authorization"] = f"Bearer {self.token}"
			response = requests.get(url, headers=headers, timeout=self.request_timeout)  # Retry request
        
		response.raise_for_status()
		return response.json()
	
	# --------------------------------------------------------------------------------------
	
	# Step 5b: Retrieve Output
	def retrieve_req_output(self, uuid):
		url = f"{self.base_url}/simulations/v1/result/{uuid}"  # Output endpoint
		headers = {"Authorization": f"Bearer {self.token}"}
		response = requests.get(url, headers=headers, timeout=self.request_timeout)
		response.raise_for_status()
		return response.json()
	
	# --------------------------------------------------------------------------------------
	
	# Step 5c: Retrieve Output
	def retrieve_cab_output(self):
		url = f"{self.base_url}/schedules/v1/cabs"  # Output endpoint
		headers = {"Authorization": f"Bearer {self.token}"}
		response = requests.get(url, headers=headers, timeout=self.request_timeout)
		response.raise_for_status()
		return response.json()
		
	# --------------------------------------------------------------------------------------
	
	# Step 5d: Retrieve Output
	def retrieve_pro_output(self):
		url = f"{self.base_url}/schedules/v1/pros"  # Output endpoint
		headers = {"Authorization": f"Bearer {self.token}"}
		response = requests.get(url, headers=headers, timeout=self.request_timeout)
		response.raise_for_status()
		return response.json()
	
	# --------------------------------------------------------------------------------------

	def _get_debug_paths(self, far: FleetAndRequests, log_id: int, log_path: str):
		base_cab = far.vehicle_fleet.cabs[0] if far.vehicle_fleet.cabs else None
		base_pro = far.vehicle_fleet.pros[0] if far.vehicle_fleet.pros else None
		prefix = compute_run_scenario_prefix(self.operations_data, base_cab, base_pro, far.demand_scenario)
		output_dir = Path(log_path)
		if self.output_folder:
			output_dir = output_dir / self.output_folder

		return output_dir, {
			"input_req": output_dir / f"{prefix}_input_req_file_{log_id}.json",
			"input_base": output_dir / f"{prefix}_input_base_file_{log_id}.json",
			"output_cab": output_dir / f"{prefix}_output_cab_{log_id}.json",
			"output_pro": output_dir / f"{prefix}_output_pro_{log_id}.json",
			"output_sim": output_dir / f"{prefix}_output_sim_{log_id}.json",
		}

	# --------------------------------------------------------------------------------------

	def _normalize_json_data(self, data, convert):
		return json.loads(json.dumps(data, default=convert, sort_keys=True))

	# --------------------------------------------------------------------------------------

	def _load_json_file(self, path: Path):
		with open(path, "r") as file:
			return json.load(file)

	# --------------------------------------------------------------------------------------

	def _cache_inputs_match(self, paths, request_data, base_data, convert):
		if not paths["input_req"].exists() or not paths["input_base"].exists():
			return False

		cached_request = self._load_json_file(paths["input_req"])
		cached_base = self._load_json_file(paths["input_base"])

		return (
			cached_request == self._normalize_json_data(request_data, convert)
			and cached_base == self._normalize_json_data(base_data, convert)
		)

	# --------------------------------------------------------------------------------------

	def _load_cached_outputs(self, paths, request_data, base_data, convert, log_id: int, cache_required: bool=False):
		output_keys = ("output_cab", "output_pro", "output_sim")
		missing = [paths[key].name for key in output_keys if not paths[key].exists()]
		if missing:
			message = f"RW cache incomplete for iteration {log_id}; missing: {', '.join(missing)}"
			if cache_required:
				raise RWAPIError(message)
			if self.verbose:
				print(message)
			return None

		try:
			if not self._cache_inputs_match(paths, request_data, base_data, convert):
				message = f"RW cache ignored for iteration {log_id}; cached input files do not match generated input."
				if cache_required:
					raise RWAPIError(message)
				if self.verbose:
					print(message)
				return None

			cab_output = self._load_json_file(paths["output_cab"])
			pro_output = self._load_json_file(paths["output_pro"])
			sim_output = self._load_json_file(paths["output_sim"])
		except (OSError, json.JSONDecodeError) as e:
			message = f"RW cache unreadable for iteration {log_id}: {e}"
			if cache_required:
				raise RWAPIError(message) from e
			if self.verbose:
				print(message)
			return None

		if self.verbose:
			print(f"Using cached RW outputs for iteration {log_id}.")
		return cab_output, pro_output, sim_output

	# --------------------------------------------------------------------------------------

	def _write_debug_inputs(self, paths, request_data, base_data, convert):
		paths["input_req"].parent.mkdir(parents=True, exist_ok=True)
		with open(paths["input_req"], "w") as file:
			json.dump(request_data, file, indent='\t', default=convert)
		with open(paths["input_base"], "w") as file:
			json.dump(base_data, file, indent='\t', default=convert)

	# --------------------------------------------------------------------------------------

	def _write_debug_outputs(self, paths, cab_output, pro_output, sim_output, convert):
		with open(paths["output_cab"], "w") as file:
			json.dump(cab_output, file, indent='\t', default=convert)
		with open(paths["output_pro"], "w") as file:
			json.dump(pro_output, file, indent='\t', default=convert)
		with open(paths["output_sim"], "w") as file:
			json.dump(sim_output, file, indent='\t')

	# --------------------------------------------------------------------------------------

	def _run_api_simulation(self, request_data, base_data):
		self.get_token()
		if ( self.verbose ):
			print("Token obtained successfully.")

		self.reset_api()
		if ( self.verbose ):
			print("API reset successfully.")

		self.feed_init_json(base_data)
		if ( self.verbose ):
			print("First JSON file fed successfully.")

		sim_id = self.feed_request_json(request_data)
		if ( self.verbose ):
			print("Second JSON file fed successfully. Started simulating")

		status = False
		time_elapsed = 0
		while ( not status ):
			curr_time = -time.time()
			status_output = self.check_status(sim_id)
			if status_output["state"] == "Completed":
				status = True
				sim_output = self.retrieve_req_output(sim_id)
				if ( self.verbose ):
					print("Simulating completed ")
			elif ( time_elapsed < self.timeout ):
				if ( self.verbose ):
					print("Completion: {:3.2f} %".format(status_output["progress"]))
				time.sleep(self.polling_rate)
				time_elapsed += curr_time + time.time()
			else:
				raise RWAPIError("Timelimit for Simulation reached")

		cab_output = self.retrieve_cab_output()
		pro_output = self.retrieve_pro_output()
		if ( self.verbose ):
			print("Output retrieved successfully.")

		return cab_output, pro_output, sim_output

	# --------------------------------------------------------------------------------------

	def optimize(self, far: FleetAndRequests, log_id: int=-1, log_path: str="./rw/output", debug: bool=True,
				 rw_cache: str="auto", cache_required: bool=False, max_retries: int=1):

		# convert data to json "format" (needs to be "json.dumps()"'d for api)
		request_data = far.write_api_req_file()
		base_data = far.write_api_base_file(self.operations_data)

		def convert(obj):
			if isinstance(obj, datetime):
				return obj.isoformat(sep='T')
			raise TypeError(f"Type {type(obj)} not serializable")

		if rw_cache not in ("auto", "force", "off"):
			raise ValueError("rw_cache must be one of: auto, force, off")
		max_retries = max(0, int(max_retries))

		paths = None
		if ( log_id > -1 and debug ):
			_, paths = self._get_debug_paths(far, log_id, log_path)
			if rw_cache == "auto":
				cached_outputs = self._load_cached_outputs(
					paths, request_data, base_data, convert, log_id, cache_required=cache_required
				)
				if cached_outputs is not None:
					cab_output, pro_output, sim_output = cached_outputs
					status = self.get_all_kpis(far, cab_output, pro_output, sim_output)
					return status
			elif cache_required:
				raise RWAPIError(f"RW cache is required for iteration {log_id}, but rw_cache={rw_cache}.")

			self._write_debug_inputs(paths, request_data, base_data, convert)
		elif cache_required:
			raise RWAPIError("RW cache cannot be required when debug output is disabled or log_id is missing.")

		attempts = max_retries + 1
		for attempt in range(1, attempts + 1):
			try:
				cab_output, pro_output, sim_output = self._run_api_simulation(request_data, base_data)
				break
			except requests.exceptions.RequestException as e:
				if attempt >= attempts:
					raise RWAPIError(f"RW API request failed in iteration {log_id}: {e}") from e
				print(f"RW API request failed in iteration {log_id} (attempt {attempt}/{attempts}): {e}")
				time.sleep(self.polling_rate)
			except RWAPIError:
				raise
			except Exception as e:
				raise RWAPIError(f"Unexpected RW API error in iteration {log_id}: {e}") from e

		if ( log_id > -1 and debug and paths is not None ):
			self._write_debug_outputs(paths, cab_output, pro_output, sim_output, convert)

		status = self.get_all_kpis(far, cab_output, pro_output, sim_output)
		return status
	
	# --------------------------------------------------------------------------------------
	
	def get_req_kpis(self, request: Request, sim_data):
		
		# helper function ---------------
		def iso_to_dt(s):
			if not s:
				return None
			s = s.strip()

			if s.endswith("Z"):
				time_part = s[:-1]
				timezone_part = "+00:00"
			else:
				timezone_index = -1
				for sign in ("+", "-"):
					index = s.rfind(sign)
					if index > 10:
						timezone_index = max(timezone_index, index)

				if timezone_index > -1:
					time_part = s[:timezone_index]
					timezone_part = s[timezone_index:]
				else:
					time_part = s
					timezone_part = "+00:00"

			if "." in time_part:
				prefix, fraction = time_part.split(".", 1)
				time_part = f"{prefix}.{fraction[:6].ljust(6, '0')}"

			try:
				return datetime.fromisoformat(time_part + timezone_part)
			except ValueError:
				print(f"[rw-api] Unexpected datetime format: '{s}'")
				return None
		# ------------------------------
		
		# look for a matching proposal
		user_guid = str(request.id)
		matches = [
			step for step in sim_data["stepResults"]
			if str(step.get("tripRequestResponse", {}).get("userGuid")) == user_guid
		]
		#if len(matches) != 1:
			## can't safely decide which entry belongs to this request
			#request.sim_invalid = True
			#print("Error: Multiple or no matching proposals found in simulation output for UserGuid", user_guid)
			#import sys
			#sys.exit(-1)
			#return
		
		if len(matches) == 0:
			request.sim_invalid = True
			print("[rw-api] Warning: No matching stepResults found for UserGuid", user_guid)
			return

		if len(matches) > 1:
			request.sim_invalid = True
			print("[rw-api] Warning: Ambiguous mapping (multiple stepResults) for UserGuid", user_guid, "count=", len(matches))
			return
		
		## NOTE: this is based on the assumption, that request.ids equal to indices in stepResults
		#data_entry = sim_data["stepResults"][request.id]
		## NOTE: hopefully not anymore with the proposal lookup block above???
		data_entry = matches[0]
		
		request.sim_invalid = bool(data_entry.get("invalidRequest", False))
		if request.sim_invalid:
			print("[rw-api] Warning: invalid request in simulation output for ID:", request.id)
			return

		# --- NEW: handle empty proposal / empty proposals list safely ---
		trr = data_entry.get("tripRequestResponse", {})
		proposals = trr.get("proposals", [])
		chosen_proposal_guid = (data_entry.get("proposal", "")).strip()

		has_chosen_proposal = bool(chosen_proposal_guid) and any(
			prop.get("guid") == chosen_proposal_guid for prop in proposals
		)

		# system reject if black-box says unsuccessful OR there is no usable chosen proposal
		request.sim_system_reject = (not trr.get("successful", False)) or (not has_chosen_proposal)
		# ---------------------------------------------------------------
		
		# custom reject (your existing logic)
		if data_entry["bookTripResponse"] is not None:
			request.sim_custom_reject = not data_entry["bookTripResponse"]["successful"]
		else:
			request.sim_custom_reject = True
		
		# --- NEW: also require trip to exist before reading it ---
		if (
			data_entry["bookTripResponse"] is not None
			and data_entry["bookTripResponse"]["successful"]
			and data_entry.get("trip") is not None
		):
			trip = data_entry["trip"]
			request.vehicle              = trip.get("vehicleLabel")
			request.sim_prop_pu_time     = iso_to_dt(trip.get("estimatedPickupTime"))
			request.sim_pu_time          = iso_to_dt(trip.get("estimatedPickupTime"))
			request.sim_prop_do_time     = iso_to_dt(trip.get("estimatedDropoffTime"))
			request.sim_do_time          = iso_to_dt(trip.get("estimatedDropoffTime"))
			if request.tw_type:
				request.sim_wait_time_prop = request.sim_prop_pu_time - request.tw_lower
			else:
				request.sim_wait_time_prop = request.sim_prop_do_time - request.tw_lower
		else:
			# Normal case if request wasn't served (system reject or customer reject)
			if request.sim_system_reject or request.sim_custom_reject:
				# no log needed, this is expected
				return
			# Anything else reaching here is inconsistent -> warn
			print("[rw-api] Warning: unexpected unbooked state for request ID:", request.id)
		
		return
	
	# --------------------------------------------------------------------------------------
	
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
		
		# Validation
		if len(matches) == 0:
			print("[rw-api] Warning: no matching cab found in output_cab.json for", cab_id)
			return

		if len(matches) > 1:
			print("[rw-api] Warning: multiple matching cabs found in output_cab.json for", cab_id, "count=", len(matches))
			return

		cab_entry = matches[0] # Retrieve matched cab entry

		# Total operation KPIs
		total_distance_m = 0.0
		total_driving_s = 0.0
		total_service_s = 0.0
		total_energy_wh = 0.0

		# Empty total KPIs
		empty_distance_m = 0.0
		empty_time_s = 0.0

		# Customer KPIs
		customer_distance_m = 0.0
		customer_distance_with_pro_m = 0.0
		customer_distance_without_pro_m = 0.0
		customer_driving_time_s = 0.0
		customer_driving_time_with_pro_s = 0.0
		customer_driving_time_without_pro_s = 0.0
		customer_service_time_s = 0.0
		customer_trip_count = 0

		# Empty pickup operation KPIs
		empty_pickup_distance_m = 0.0
		empty_pickup_time_s = 0.0
		empty_pickup_service_time_s = 0.0
		empty_pro_reposition_distance_m = 0.0
		empty_pro_reposition_time_s = 0.0
		empty_pro_reposition_service_time_s = 0.0
		empty_pro_reposition_count = 0

		# Charging access to stationary charging KPIs, always without customer on board
		charging_stationary_access_distance_m = 0.0
		charging_stationary_access_time_s = 0.0
		charging_stationary_time_s = 0.0
		charging_stationary_energy_wh = 0.0
		charging_stationary_event_count = 0

		# Pro charging support KPIs
		charging_pro_energy_wh = 0.0

		# Internal helper states
		in_empty_pro_reposition_sequence = False

		# Process RW trip stops
		for stop in cab_entry.get("tripStops", []):
			stop_type = stop.get("stopType", "")
			distance_m = float(stop.get("distance", 0) or 0)
			driving_time_s = float(stop.get("drivingTime", 0) or 0)
			service_time_s = float(stop.get("duration", 0) or 0)
			consumed_energy_wh = float(stop.get("consumedEnergy", 0) or 0)
			charging_energy_wh = float(stop.get("chargedEnergy", 0) or 0) # energy gain during charging
			trip_guid = stop.get("tripGuid", "")

			# Total KPIs
			total_distance_m += distance_m
			total_driving_s += driving_time_s
			total_service_s += service_time_s
			total_energy_wh += consumed_energy_wh

			# Pickup = empty approach to customer
			if stop_type == "Pickup":
				in_empty_pro_reposition_sequence = False

				empty_distance_m += distance_m
				empty_time_s += driving_time_s

				empty_pickup_distance_m += distance_m
				empty_pickup_time_s += driving_time_s
				empty_pickup_service_time_s += service_time_s

			# Dropoff = customer transport
			elif stop_type == "Dropoff":
				customer_distance_m += distance_m
				customer_driving_time_s += driving_time_s
				customer_service_time_s += service_time_s

				customer_distance_without_pro_m += distance_m
				customer_driving_time_without_pro_s += driving_time_s

				customer_trip_count += 1

				in_empty_pro_reposition_sequence = False

			# Chaining / Unchaining
			elif stop_type in ("Chaining", "Unchaining"):
				# Customer attached to Pro convoy
				if trip_guid:
					customer_distance_m += distance_m
					customer_driving_time_s += driving_time_s
					customer_service_time_s += service_time_s

					customer_distance_with_pro_m += distance_m
					customer_driving_time_with_pro_s += driving_time_s

					if charging_energy_wh > 0:
						charging_pro_energy_wh += charging_energy_wh

					in_empty_pro_reposition_sequence = False

				# Empty repositioning movement
				else:
					empty_distance_m += distance_m
					empty_time_s += driving_time_s

					empty_pro_reposition_distance_m += distance_m
					empty_pro_reposition_time_s += driving_time_s
					empty_pro_reposition_service_time_s += service_time_s

					if charging_energy_wh > 0:
						charging_pro_energy_wh += charging_energy_wh

					if not in_empty_pro_reposition_sequence:
						empty_pro_reposition_count += 1
						in_empty_pro_reposition_sequence = True

			# Charging
			elif stop_type == "Charging":
				# Access drive to charging station
				empty_distance_m += distance_m
				empty_time_s += driving_time_s

				charging_stationary_access_distance_m += distance_m
				charging_stationary_access_time_s += driving_time_s

				if charging_energy_wh > 0:
					charging_stationary_energy_wh += charging_energy_wh

				# Stationary charging process
				charging_stationary_time_s += service_time_s
				charging_stationary_event_count += 1

		## Write KPIs to Cab model ##
		# Total operation
		cab.sim_cum_distance = total_distance_m
		cab.sim_cum_driving_time = total_driving_s
		cab.sim_cum_service_time = total_service_s
		cab.sim_cum_energy_cons = total_energy_wh

		# Customer
		cab.sim_customer_distance = customer_distance_m
		cab.sim_customer_distance_with_pro = customer_distance_with_pro_m
		cab.sim_customer_distance_without_pro = customer_distance_without_pro_m
		cab.sim_customer_driving_time = customer_driving_time_s
		cab.sim_customer_driving_time_with_pro = customer_driving_time_with_pro_s
		cab.sim_customer_driving_time_without_pro = customer_driving_time_without_pro_s
		cab.sim_customer_service_time = customer_service_time_s
		cab.sim_customer_trip_count = customer_trip_count

		# Legacy compatibility
		cab.sim_num_requests = customer_trip_count

		# Empty
		cab.sim_empty_distance = empty_distance_m
		cab.sim_empty_time = empty_time_s
		cab.sim_empty_pickup_distance = empty_pickup_distance_m
		cab.sim_empty_pickup_time = empty_pickup_time_s
		cab.sim_empty_pickup_service_time = empty_pickup_service_time_s
		cab.sim_empty_pro_reposition_distance = empty_pro_reposition_distance_m
		cab.sim_empty_pro_reposition_time = empty_pro_reposition_time_s
		cab.sim_empty_pro_reposition_service_time = empty_pro_reposition_service_time_s
		cab.sim_empty_pro_reposition_count = empty_pro_reposition_count

		# Charging
		cab.sim_charging_stationary_access_distance = charging_stationary_access_distance_m
		cab.sim_charging_stationary_access_time = charging_stationary_access_time_s
		cab.sim_charging_stationary_time = charging_stationary_time_s
		cab.sim_charging_stationary_energy = charging_stationary_energy_wh
		cab.sim_charging_stationary_event_count = charging_stationary_event_count

		# Pro charging support
		cab.sim_charging_pro_energy = charging_pro_energy_wh

		##  KPI consistency checks ##
		# Customer distance consistency
		customer_split_distance = customer_distance_with_pro_m + customer_distance_without_pro_m
		if abs(customer_distance_m - customer_split_distance) > 1e-6:
			print("[rw-api KPI WARNING] Customer distance mismatch for cab {}: total {} vs split {} + {}".format(
				cab.id,
				customer_distance_m,
				customer_distance_with_pro_m,
				customer_distance_without_pro_m,
			))

		# Customer driving time consistency
		customer_split_time = customer_driving_time_with_pro_s + customer_driving_time_without_pro_s
		if abs(customer_driving_time_s - customer_split_time) > 1e-6:
			print("[rw-api KPI WARNING] Customer driving time mismatch for cab {}: total {} vs split {} + {}".format(
				cab.id,
				customer_driving_time_s,
				customer_driving_time_with_pro_s,
				customer_driving_time_without_pro_s,
			))

		# Total distance decomposition consistency
		classified_distance = customer_distance_m + empty_pickup_distance_m + empty_pro_reposition_distance_m + charging_stationary_access_distance_m
		if abs(total_distance_m - classified_distance) > 1e-6:
			print("[rw-api KPI WARNING] Total distance decomposition mismatch for cab {}: total {} vs classified {}".format(
				cab.id,
				total_distance_m,
				classified_distance,
			))

		# Total driving time decomposition consistency
		classified_driving_time = customer_driving_time_s + empty_pickup_time_s + empty_pro_reposition_time_s + charging_stationary_access_time_s
		if abs(total_driving_s - classified_driving_time) > 1e-6:
			print("[rw-api KPI WARNING] Total driving time decomposition mismatch for cab {}: total {} vs classified {}".format(
				cab.id,
				total_driving_s,
				classified_driving_time,
			))

		# Total service time decomposition consistency
		classified_service_time = customer_service_time_s + empty_pickup_service_time_s + empty_pro_reposition_service_time_s + charging_stationary_time_s
		if abs(total_service_s - classified_service_time) > 1e-6:
			print("[rw-api KPI WARNING] Total service time decomposition mismatch for cab {}: total {} vs classified {}".format(
				cab.id,
				total_service_s,
				classified_service_time,
			))

		## DEBUG OUTPUT ##
		"""
		print("---------------DEBUG OUTPUT: KPIs RW-API------------------------------")
		print(f"Number of Cabs in Fleet: {len(self.cab_fleet)}")
		print(f"Information for Cab {cab.id}")

		print("REQUESTS")
		print(f"Requests served: {cab.sim_customer_trip_count}")

		print("DISTANCE")
		print(f"Total distance: {cab.sim_cum_distance:.2f} m")
		print(f"Total driving time: {cab.sim_cum_driving_time:.2f} s")
		print(f"Total service time: {cab.sim_cum_service_time:.2f} s")
		print(f"Total consumed energy: {cab.sim_cum_energy_cons:.2f} Wh")

		print(f"Customer total distance: {cab.sim_customer_distance:.2f} m")
		print(f"Customer distance with Pro: {cab.sim_customer_distance_with_pro:.2f} m")
		print(f"Customer distance without Pro: {cab.sim_customer_distance_without_pro:.2f} m")
		print(f"Empty total distance: {cab.sim_empty_distance:.2f} m")
		print(f"Empty pickup distance: {cab.sim_empty_pickup_distance:.2f} m")
		print(f"Empty Pro reposition distance: {cab.sim_empty_pro_reposition_distance:.2f} m")
		print(f"Charging access distance: {cab.sim_charging_stationary_access_distance:.2f} m")

		print("TIME")
		print(f"Customer total driving time: {cab.sim_customer_driving_time:.2f} s")
		print(f"Customer driving time with Pro: {cab.sim_customer_driving_time_with_pro:.2f} s")
		print(f"Customer driving time without Pro: {cab.sim_customer_driving_time_without_pro:.2f} s")
		print(f"Customer service time: {cab.sim_customer_service_time:.2f} s")
		print(f"Empty total time: {cab.sim_empty_time:.2f} s")
		print(f"Empty pickup time: {cab.sim_empty_pickup_time:.2f} s")
		print(f"Empty Pro reposition time: {cab.sim_empty_pro_reposition_time:.2f} s")
		print(f"Charging access time: {cab.sim_charging_stationary_access_time:.2f} s")
		print(f"Charging stationary time: {cab.sim_charging_stationary_time:.2f} s")

		print("COUNTs")
		print(f"Customer trip count: {cab.sim_customer_trip_count}")
		print(f"Empty Pro reposition count: {cab.sim_empty_pro_reposition_count}")
		print(f"Charging stationary event count: {cab.sim_charging_stationary_event_count}")

		print("ENERGY")
		print(f"Charging stationary energy: {cab.sim_charging_stationary_energy:.2f} Wh")
		print(f"Charging Pro energy: {cab.sim_charging_pro_energy:.2f} Wh")

		"""
		return

	# --------------------------------------------------------------------------------------


	def get_pro_kpis(self, pro, pro_data):


		#self.reset_cumulative_values()
		cum_dist = 0
		cum_driving = 0
		cum_service = 0
		cum_chained_cabs = 0

		# match cab entry by vehicle id from cab object
		pro_id = int(pro.id)

		matches = []
		for entry in pro_data:
			vid = entry.get("vehicle", {}).get("id", "")
			# extract trailing number from strings like "Cab1", "Cab01", "CAB-12"
			num = ""
			for ch in reversed(str(vid)):
				if ch.isdigit():
					num = ch + num
				else:
					break
			if num and int(num) == pro_id:
				matches.append(entry)

		if len(matches) == 0:
			print("[rw-api] Warning: no matching pro found in output_pro.json for", pro_id)
			return

		if len(matches) > 1:
			print("[rw-api] Warning: multiple matching pros found in output_pro.json for", pro_id, "count=", len(matches))
			return

		pro_entry = matches[0]

		## NOTE: this is based on the assumption, that cab.ids equal to indices in output_cab.json
		#for stop in pro_data[pro.id-1]["chainingStops"]:
		for stop in pro_entry["chainingStops"]:
			#if pro.id != int(pro_data[pro.id-1]["vehicle"]["id"].partition("Pro")[-1]):
				#print("WARNING: ProID is not matching to position in RW Output")
				##sys.exit(-1)
			cum_dist += stop["distance"]
			cum_driving += stop["drivingTime"]
			cum_service += stop["duration"]
			cum_chained_cabs += stop["chainedCabs"]
		pro.sim_cum_distance = cum_dist
		pro.sim_cum_driving_time = cum_driving
		pro.sim_cum_service_time = cum_service
		pro.sim_cum_chained_cabs = cum_chained_cabs

		return


	# --------------------------------------------------------------------------------------

	def get_all_kpis(self,far, cab_data, pro_data, sim_data):
		# writes kpis into the far object
			
		for cab in far.vehicle_fleet.cabs:
			self.get_cab_kpis(cab, cab_data)

		for pro in far.vehicle_fleet.pros:
			self.get_pro_kpis(pro, pro_data)
			
		for req in far.demand_scenario.requests:
			self.get_req_kpis(req, sim_data)

		far.sim_aggregates = sim_data["finalReport"]
		far.sim_aggregates.update(sim_data["overview"])
		
		return 0


# ======================================================================================
# ======================================================================================


if __name__ == "__main__":
	"""Small local smoke test: builds example models and exercises the parts of rwAPI that
	don't need network access or credentials (prefix/path construction). Does not call the
	real API; run fleet_planning.py --use_sim rw for that."""
	import models

	now = datetime.now()
	reg_time = datetime.fromisoformat("2025-10-16T19:04:00")
	tw_low = datetime.fromisoformat("2025-10-18T06:48:21+02:00")
	tw_upp = datetime.fromisoformat("2025-10-18T06:59:00+02:00")
	sched_start = datetime.fromisoformat("2025-10-18T05:00:00+02:00")
	sched_end = datetime.fromisoformat("2025-10-18T23:00:00+02:00")

	exmpl_req = models.Request(0, 0.0, 0.0, 0.1, 0.1, reg_time, tw_low, tw_upp, False)
	exmpl_cab = models.Cab(0, 0.0, 0.0, sched_start, sched_end, 10.0, 0.2, 50000.0, 50000.0)
	exmpl_pro = models.Pro(0, 0.0, 0.0, now, now, 10.0, 5000.0, 0.2, 100000.0, 100000.0)
	demand = models.DemandScenario([exmpl_req])
	vf = models.VehicleFleet([exmpl_cab], [exmpl_pro],
							(exmpl_cab.init_location_lat, exmpl_cab.init_location_lon),
							(exmpl_pro.init_location_lat, exmpl_pro.init_location_lon))
	far = models.FleetAndRequests(0, demand, vf)

	op_vertices = models.OperationalVertices([(0.0, 0.0), (0.1, 0.0), (0.1, 0.1)], _desc="pb_test")
	op_area = models.OperationalArea(op_vertices, [], [], [], models.ProRoutesAndTrips([], []))

	api = rwAPI(op_area)
	prefix = compute_run_scenario_prefix(op_area, exmpl_cab, exmpl_pro, demand)
	print("prefix:", prefix)

	output_dir, paths = api._get_debug_paths(far, 0, "./rw/output")
	print("output_dir:", output_dir)
	for key, path in paths.items():
		print(f"  {key}: {path}")

	print("OK: rwAPI construction and path resolution work without network access.")
	
