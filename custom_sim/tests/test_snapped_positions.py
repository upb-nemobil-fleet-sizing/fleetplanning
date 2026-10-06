from types import SimpleNamespace

from models import ChargingStation
from custom_sim.custom_simulation import CustomSimulation
from custom_sim.models_cs import CabEntryType, SimChargingStation


STATION_POINT = (51.2001, 8.2002)


class _StubRouter:
	"""
	Answers like the real router for the two cases that matter here: identical
	coordinates give the zero route, any other pair gives a detour.
	"""

	def __init__(self):
		self.queries = []

	def shortest_path(self, orig, dest):
		self.queries.append((orig, dest))
		same = tuple(orig) == tuple(dest)
		return {
			"time_s": 0 if same else 7,
			"distance_m": 0.0 if same else 46.0,
			"energy_wh": 0.0 if same else 10.0,
			"proj_orig": tuple(orig),
			"proj_dest": tuple(dest),
			"query_time_s": 0.0,
		}


def _simulation_with_cab_at_station(router):
	sim = CustomSimulation.__new__(CustomSimulation)
	sim.router_cab = router
	sim.runtime_diagnostics = SimpleNamespace(router_calls={"cab": 0}, router_time_s={"cab": 0.0})
	sim.parameters = {"algorithm": {}, "problem": {"cs_connect_time": 60}}
	sim.horizon_start = 0
	sim.horizon_end = 8 * 3600

	# the station keeps its raw input coordinate, the wrapper holds the snapped point
	station = ChargingStation("CS_A", 51.2, 8.2)
	sim.sim_charging_stations = [SimChargingStation(station, 0, *STATION_POINT)]

	cab = SimpleNamespace(
		cab=SimpleNamespace(id=1),
		schedule=[],
		start_schedule=0,
		end_schedule=8 * 3600,
		charge_init=10000.0,
		charge_lb=0.0,
		charge_ub=10000.0,
		speed=8.33,
		consumption=0.1,
		current_lat=STATION_POINT[0],
		current_lon=STATION_POINT[1],
	)
	sim.cab_fleet = [cab]
	return sim, cab


def test_cab_standing_at_its_station_gets_a_zero_route_to_that_station():
	router = _StubRouter()
	sim, cab = _simulation_with_cab_at_station(router)

	sim._initialize_charging()

	first_approach = cab.schedule[0]
	assert first_approach.type == CabEntryType.ChA
	assert first_approach.distance_m == 0.0
	assert first_approach.end_time == first_approach.start_time
	assert (first_approach.start_lat, first_approach.start_lon) == STATION_POINT
	assert (first_approach.end_lat, first_approach.end_lon) == STATION_POINT


def test_every_charging_approach_of_a_cab_at_its_station_routes_between_identical_coordinates():
	router = _StubRouter()
	sim, cab = _simulation_with_cab_at_station(router)

	sim._initialize_charging()

	assert router.queries
	assert all(tuple(orig) == tuple(dest) == STATION_POINT for orig, dest in router.queries)
	approaches = [entry for entry in cab.schedule if entry.type == CabEntryType.ChA]
	assert all(entry.distance_m == 0.0 for entry in approaches)
