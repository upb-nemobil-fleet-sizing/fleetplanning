import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from custom_sim.custom_simulation import CustomSimulation
from models import (Cab, ChargingStation, DemandScenario, FleetAndRequests, OperationalArea,
					OperationalVertices, Pro, ProRoutesAndTrips, Request, VehicleFleet)


ROUTING_CONFIG = Path(__file__).resolve().parents[1] / "routing" / "routing_config.json"


def _extract_present():
	config = json.loads(ROUTING_CONFIG.read_text(encoding="utf-8"))
	source = config["build"]["source"]["osm_source_path"]
	return (ROUTING_CONFIG.parent / source).is_file()


def _overpass_reachable(host="overpass-api.de", port=443, timeout=3):
	try:
		socket.create_connection((host, port), timeout=timeout).close()
		return True
	except OSError:
		return False


@pytest.mark.integration
def test_minimal_scenario_serves_the_request_with_the_cab():
	if not (_extract_present() or _overpass_reachable()):
		pytest.fail("no road network: the local OSM extract is missing and Overpass is unreachable")

	start = datetime.now()
	end = start + timedelta(hours=1)

	request = Request(0, 51.7180, 8.7550, 51.7200, 8.7590,
					  start, start + timedelta(minutes=5), start + timedelta(minutes=30))
	cab = Cab(0, 51.7185, 8.7555, start, end, 8.333333, 0.1, 10000, 10000)
	pro = Pro(0, 51.7185, 8.7555, start, end, 19.444444, 11000, 0.18, 10000, 10000, 3, [0.1])
	station = ChargingStation(0, 51.7185, 8.7555)

	vehicle_fleet = VehicleFleet([cab], [pro], None, None)
	solution = FleetAndRequests(0, DemandScenario([request]), vehicle_fleet)

	vertices = [(8.7534, 51.71675), (8.7606, 51.71675), (8.7606, 51.72125), (8.7534, 51.72125)]
	area = OperationalArea(OperationalVertices(vertices), [station], [], [], ProRoutesAndTrips([], []))

	simulation = CustomSimulation(area)
	simulation.optimize(solution)

	state = simulation.demand_scenario[0].sim_state
	assert state.assigned is True
	assert state.assigned_cab_id == 0
	assert state.assigned_mode == "pure"
	assert state.dropoff_ts > state.pickup_ts
