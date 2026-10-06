from datetime import datetime, timedelta

from models import (Cab, DemandScenario, FleetAndRequests, OperationalArea,
					OperationalVertices, Pro, ProRoutesAndTrips, VehicleFleet)


def test_objects_of_an_empty_scenario_build():
	start = datetime.now()
	end = start + timedelta(hours=1)

	cab = Cab(0, 51.7185, 8.7555, start, end, 30.0, 0.1, 10, 10)
	pro = Pro(0, 51.7185, 8.7555, start, end, 70.0, 0.1, 10, 10, 10)
	vehicle_fleet = VehicleFleet([cab], [pro], None, None)
	solution = FleetAndRequests(0, DemandScenario([]), vehicle_fleet)

	vertices = [(8.7534, 51.71675), (8.7606, 51.71675), (8.7606, 51.72125), (8.7534, 51.72125)]
	area = OperationalArea(OperationalVertices(vertices), [], [], [], ProRoutesAndTrips([], []))

	assert len(solution.vehicle_fleet.cabs) == 1
	assert len(solution.vehicle_fleet.pros) == 1
	assert solution.demand_scenario.requests == []
	assert len(area.operational_area.vertices) == 4
