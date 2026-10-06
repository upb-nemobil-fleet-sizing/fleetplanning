from __future__ import absolute_import
from __future__ import print_function
import os
import sys
import optparse
import numpy as np
import math
from . import models_sumo
# import model

# we need to import python modules from the $SUMO_HOME/tools directory
if 'SUMO_HOME' in os.environ:
    tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
    sys.path.append(tools)
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")

import traci  # noqa
import sumolib  # noqa

# Creating taxis
def create_taxis(numTaxis, initial_parking_station):
    for i in range(0,numTaxis): 
        taxiID = f'taxi {i+1}'
        traci.vehicle.add(taxiID, 'route_taxi',
                          'taxi', depart=0)
        traci.vehicle.setParkingAreaStop(taxiID, initial_parking_station)

# Creating taxis
def create_pros(procab_list):
    for procab in procab_list:
        procabID = f'{procab.id}'
        traci.route.add(routeID=f"route_{procabID}", edges=[procab.edge])
        traci.vehicle.add(procabID, f"route_{procabID}",
                          'pro', depart=0, departPos = 0)
        traci.vehicle.setParkingAreaStop(procabID, procab.start_location)

def nearest_station(edge, simulationObject):
    """
    Takes a position and a set of charging OR parking stations and then returns the closest one
    """
    sortedStations = sorted(simulationObject.operations_area.charging_stations, 
                                        key = lambda station: 
                                        travel_time([edge, station.lane.getID()[:-2]]))
    return sortedStations[0]

def nearest_parking_station(position, simulationObject):
    """
    Takes a position and a set of charging OR parking stations and then returns the closest one
    """

    sortedStations = sorted(simulationObject.operations_area.chaining_locations, 
                                        key = lambda station: 
                                        traci.simulation.getDistance2D(position[0], position[1], station.x_start, station.y_start))
    return sortedStations[0]

def nearest_feasible_station(chargingApproach_n, reservation, initialEdge, endEnergy, 
                            approach, thirdEntry, simulationObject, 
                            pro = None, platoonApproach = None, platoonTransport = None, platoon_charging_duration = None):
    """
    Takes a position and a set of charging OR parking stations and then returns the closest feasible one
    """
    if pro != None: 
        platoonStart = pro.chain_points[0]
        platoonEnd = pro.chain_points[1]

    position = reservation.endPos
    energyConsumption = simulationObject.taxis[0].energyConsumption
    sortedStations = sorted(simulationObject.operations_area.charging_stations, 
                                        key = lambda station: 
                                        travel_time([reservation.doEdge, station.lane.getID()[:-2]]))

    bestStation_m = ""
    energyFlagN = True
    energyFlagM = True
    feasible = True
    for station in sortedStations: 
        lane_m = station.lane
        laneID_m = lane_m.getID()
        chargingEdge_m = laneID_m[:-2]

        if chargingApproach_n != None:
            if pro == None:
                energyChargingRoute_n = energy_for_route([initialEdge, 
                                                      chargingApproach_n.doEdge, 
                                                      reservation.puEdge, 
                                                      reservation.doEdge, 
                                                      chargingEdge_m], energyConsumption = energyConsumption)
            else:
                energyChargingRoute_n = (energy_for_route([initialEdge, 
                                                      chargingApproach_n.doEdge, 
                                                      reservation.puEdge, platoonStart.lane.getID()[:-2]], energyConsumption = energyConsumption) 
                                      - (platoon_charging_duration * pro.maxPowerSupplyPerCab)
                                      + energy_for_route([platoonEnd.lane.getID()[:-2],
                                                      reservation.doEdge, 
                                                      chargingEdge_m], energyConsumption = energyConsumption))

            energyToCharge_n = (- endEnergy
                               + energyChargingRoute_n + simulationObject.charging_threshold)

            if (chargingApproach_n.endEnergy + energyToCharge_n > simulationObject.taxis[0].totalEnergyCapacity) or energyToCharge_n <= 0: 
                energyFlagN = False

            if thirdEntry != None:
                if pro == None: 
                    energyChargingRoute_m = energy_for_route([initialEdge, chargingApproach_n.doEdge, 
                                                              reservation.puEdge, reservation.doEdge, 
                                                              chargingEdge_m, thirdEntry.puEdge], energyConsumption = energyConsumption)
                else: 
                    energyChargingRoute_m = (energy_for_route([initialEdge, 
                                                          chargingApproach_n.doEdge, 
                                                          reservation.puEdge, 
                                                          platoonStart.lane.getID()[:-2]], energyConsumption = energyConsumption) 
                                          - (platoon_charging_duration *  pro.maxPowerSupplyPerCab)
                                          + energy_for_route([platoonEnd.lane.getID()[:-2],
                                                          reservation.doEdge, 
                                                          chargingEdge_m, thirdEntry.puEdge], energyConsumption = energyConsumption))

                energyToCharge_m = (thirdEntry.startEnergy
                           - endEnergy
                           - energyToCharge_n
                           + energyChargingRoute_m)
        
                chargingStop_m_StartEnergy = (endEnergy 
                                             - energyChargingRoute_m 
                                             + energy_for_route([chargingEdge_m, thirdEntry.puEdge], energyConsumption = energyConsumption) 
                                             + energyToCharge_n)
            else:
                energyToCharge_m = 0
                chargingStop_m_StartEnergy = 0


        else: 
            energyToCharge_n = 0
            finalChargingStop_n = None
            energyFlagN = True
            if thirdEntry != None:
                if pro == None: 
                    energyChargingRoute_m = energy_for_route([initialEdge, reservation.puEdge,
                                                         reservation.doEdge, chargingEdge_m, 
                                                         thirdEntry.puEdge], energyConsumption = energyConsumption)
                else:

                    energyChargingRoute_m = (energy_for_route([initialEdge,
                                                            reservation.puEdge, 
                                                            platoonStart.lane.getID()[:-2]], energyConsumption = energyConsumption) 
                                             - (platoon_charging_duration *  pro.maxPowerSupplyPerCab)
                                             +  energy_for_route([platoonEnd.lane.getID()[:-2],
                                                            reservation.doEdge, 
                                                            chargingEdge_m, thirdEntry.puEdge], energyConsumption = energyConsumption))

                                                    
                energyToCharge_m = (thirdEntry.startEnergy
                           - endEnergy
                           - energyToCharge_n
                           + energyChargingRoute_m)
        
                chargingStop_m_StartEnergy = (endEnergy 
                                             - energyChargingRoute_m 
                                             + energy_for_route([chargingEdge_m, thirdEntry.puEdge], energyConsumption = energyConsumption) 
                                             + energyToCharge_n)
            else:
                energyToCharge_m = 0
                chargingStop_m_StartEnergy = 0

        if chargingStop_m_StartEnergy + energyToCharge_m > simulationObject.taxis[0].totalEnergyCapacity: 
            energyFlagM = False

        if energyFlagM and energyFlagN:
            finalEnergytoCharge_n = energyToCharge_n
            finalEnergytoCharge_m = energyToCharge_m   
            bestStation_m = station
            if finalEnergytoCharge_m <= 0 and pro != None:
                if pro.maxPowerSupplyPerCab != 0:
                    charge_in_platoon = (platoon_charging_duration *  pro.maxPowerSupplyPerCab 
                                        + finalEnergytoCharge_m)
                    finalEnergytoCharge_m = 0
                    new_charge_in_platoon_duration = charge_in_platoon / pro.maxPowerSupplyPerCab
                    if chargingApproach_n != None:
                        new_energy_at_cs =  (chargingApproach_n.endEnergy 
                                                    - energy_for_route([
                                                              chargingApproach_n.doEdge, 
                                                              reservation.puEdge, 
                                                              platoonStart.lane.getID()[:-2]], energyConsumption = energyConsumption) 
                                                    + charge_in_platoon
                                                    - energy_for_route([platoonEnd.lane.getID()[:-2],
                                                          reservation.doEdge, 
                                                          chargingEdge_m], energyConsumption = energyConsumption))
                        if new_energy_at_cs < simulationObject.charging_threshold: 
                            feasible = False
                        else:
                            break
                    else:
                        break
                else:
                    feasible = False
            elif finalEnergytoCharge_m > 0 and pro != None:
                new_charge_in_platoon_duration = platoon_charging_duration
                break
            elif pro == None:
                break
        else:
            feasible = False

    if feasible and pro != None:
        return (feasible, bestStation_m, 
                finalEnergytoCharge_m, finalEnergytoCharge_n, 
                new_charge_in_platoon_duration)
    elif feasible: 
        return (feasible, bestStation_m, 
                finalEnergytoCharge_m, finalEnergytoCharge_n, None)
    else: 
        return feasible, None, None, None, None


def get_edge_of_car(taxi, edgeIDs, net):
    """
    takes a taxi id and returns the id of the edge the taxi is on or closest too 
    """
    edgeID = traci.vehicle.getLaneID(taxi.id)[:-2]
    radius = 25
    if edgeID == "":
        # car is dropping off previous passenger or on parking lot/charging station and therefore not on any lane
        x,y = traci.vehicle.getPosition(taxi.id)
        neighboringLanes = net.getNeighboringLanes(x, y, radius)
        neighboringLanes = sorted(neighboringLanes, key = lambda lane: lane[1])
        i = 0
        try:
            lane = neighboringLanes[i][0].getID()
            edgeID = lane[:-2]
            edge = net.getEdge(edgeID)
            while edge.allows("taxi") == False: 
                i = i + 1 
                lane = neighboringLanes[i][0].getID()
                edgeID = lane[:-2]
                edge = net.getEdge(edgeID)
        except: 
            edgeID = taxi.edge
        return edgeID
    return edgeID


def route_length(edgesList):
    """
    takes a list of edges as an argument and return the length of the route which connects the edges as a shortest path
    """

    totalLength = 0
    for i in range(0, len(edgesList) - 1):
        lengthRoute = traci.simulation.findRoute(edgesList[i], edgesList[i+1], vType = "taxi").length
        totalLength = totalLength + lengthRoute
            
    return totalLength


def travel_time(edgesList, averageSpeed = 5):
    """
    takes a list of edges as an argument and return the time needed of the route which connects the edges as a shortest path
    """

    return route_length(edgesList)/averageSpeed

def custom_travel_time(origin, destination, vehicle, net):
    """
    takes a list of edges as an argument and return the time needed of the route which connects the edges as a shortest path
    """

    if vehicle.id.startswith("Pro"):
        vtype = "pro"
        vclass = "bus"
    else:
        vtype = "taxi"
        vclass = "taxi"
    route = traci.simulation.findRoute(origin, destination, vType = vtype).edges
    total_minimum_travel_time = 0
    for edgeID in route:
        edge = net.getEdge(edgeID)
        max_avg_speed = min(vehicle.maxSpeed, edge.getSpeed())
        minimum_travel_time = edge.getLength() / max_avg_speed
        total_minimum_travel_time = total_minimum_travel_time + minimum_travel_time

    return total_minimum_travel_time * 1.4
def set_route_by_edges(vehicle, origin, destination, net):
    """
    takes a two edges and a vehicle as an argument and set the route of vehicle according to the edges
    """
    if vehicle.startswith("Pro"):
        vtype = "pro"
        vclass = "bus"
    else:
        vtype = "taxi"
        vclass = "taxi"
    route = traci.simulation.findRoute(origin, destination, vType = vtype).edges
    # for edgeID in route:
    #     edge = net.getEdge(edgeID)
    #    assert edge.allows(vclass), f"Edge {edgeID} in Route does not allow given vtype {vclass}"

    traci.vehicle.setRoute(vehicle, route)

def energy_for_route(edgesList, energyConsumption):
    """
    takes a list of edges as an argument and computes the needed energy for the route
    """
    routeLength = route_length(edgesList)
    
    return routeLength * energyConsumption


def get_lane_and_offset(pos, net, lat_lon = True):
    """
    takes a position in LON LAT coordinates and projects it onto a lane + offset
    """
    radius = 25
    if lat_lon:
        x,y = net.convertLonLat2XY(pos[0], pos[1])
        neighboringLanes = net.getNeighboringLanes(x, y, radius)
        neighboringLanes = sorted(neighboringLanes,
                                  key = lambda lane:
                                  lane[1])
        lane, d = neighboringLanes[0] 
        lanePos, dist = sumolib.geomhelper.polygonOffsetAndDistanceToPoint((x,y), lane.getShape())
        return lane, int(lanePos)
    else:
        x,y = pos
        neighboringLanes = net.getNeighboringLanes(x, y, radius)
        neighboringLanes = sorted(neighboringLanes,
                                  key = lambda lane:
                                  lane[1])
        lane, d = neighboringLanes[0] 
        lanePos, dist = sumolib.geomhelper.polygonOffsetAndDistanceToPoint((x,y), lane.getShape())
        return lane, lanePos

def get_car_lane_and_offset(pos, net, lat_lon = True):
    """
    takes a position in LON LAT coordinates and projects it onto a lane + offset
    """
    if lat_lon:
        x,y = net.convertLonLat2XY(pos[0], pos[1])
        neighboringLanes = net.getNeighboringLanes(x, y, 5)
        neighboringLanes = sorted(neighboringLanes,
                                  key = lambda lane:
                                  lane[1])
        for lane, dist in neighboringLanes: 
            edgeID = lane.getID()[:-2]
            edge = net.getEdge(edgeID)
            if edge.allows("passenger"):
                choosenLane = lane
                break
        lanePos, dist = sumolib.geomhelper.polygonOffsetAndDistanceToPoint((x,y), choosenLane.getShape())
        return choosenLane, int(lanePos)

    else:
        x,y = pos
        neighboringLanes = net.getNeighboringLanes(x, y, 5)
        neighboringLanes = sorted(neighboringLanes,
                                  key = lambda lane:
                                  lane[1])
        for lane, dist in neighboringLanes: 
            edgeID = lane.getID()[:-2]
            edge = net.getEdge(edgeID)
            if edge.allows("passenger"):
                choosenLane = lane
                break
        lanePos, dist = sumolib.geomhelper.polygonOffsetAndDistanceToPoint((x,y), choosenLane.getShape())
        return choosenLane, int(lanePos)

def get_feasible_edges(startPos, endPos,net):
    """
    gets a startPos and endPos and computes feasible edges for each position
    """
    xFrom, yFrom = startPos
    xTo, yTo = endPos
    radius = 100
    i = 0
    changedPuPosition = False
    sortedLanes = sorted(net.getNeighboringLanes(xFrom, yFrom, radius), 
                                        key = lambda lane: 
                                        lane[1])
    try:
        puEdge,_ = sortedLanes[i]
    except: 
        return None, None
    while puEdge.allows('taxi') == False:
        i = i + 1
        try:
            puEdge,_ = sortedLanes[i]
        except: 
            return None, None

    i = 0
    sortedLanes = sorted(net.getNeighboringLanes(xTo, yTo, radius), 
                                        key = lambda lane: 
                                        lane[1])
    try: 
        doEdge,_ = sortedLanes[i]
    except: 
        return None, None
    while doEdge.allows('taxi') == False:
        i = i + 1
        try: 
            doEdge,_ = sortedLanes[i]
        except: 
            return None, None

    puOffset, _ = sumolib.geomhelper.polygonOffsetAndDistanceToPoint(startPos, puEdge.getShape())
    doOffset, _ = sumolib.geomhelper.polygonOffsetAndDistanceToPoint(endPos, doEdge.getShape())


    return (puEdge, puOffset), (doEdge, doOffset)


def get_vehicle_ahead(vehicleID):
    """
    Gets the vehicle ID, type and distance between of the vehicle ahead of the given vehicleID  
    """
    minDistance = 50
    leaderID, distanceToLeader  = traci.vehicle.getLeader(vehicleID, dist=minDistance)
    leaderType = None
    
    if (leaderID and distanceToLeader):
        leaderType = traci.vehicle.getTypeID(leaderID)

    return leaderID, leaderType, distanceToLeader

def stop_behind_vehicle(followerID, leaderID, safe_distance=10, duration=600):
    """
    Commands the follower vehicle to stop directly behind the leader vehicle.
    Parameters:
        followerID (str): The ID of the vehicle that will stop (follower).
        leaderID (str): The ID of the vehicle to stop behind (leader).
        safe_distance (float): The distance between the front of leader and front follower (in meters). Minimum should be 1m+length of leader.
        duration (int): time in seconds
    """
    try:
        leader_edge = traci.vehicle.getRoadID(leaderID)
        leader_lane = traci.vehicle.getLaneIndex(leaderID)
        leader_lane_pos = traci.vehicle.getLanePosition(leaderID)
        stop_position = max(0, leader_lane_pos - safe_distance)
        traci.vehicle.insertStop(followerID, nextStopIndex=0, edgeID=leader_edge, pos=stop_position, duration=duration)
    except traci.TraCIException as e:
        print(f"Error: Unable to stop vehicle {followerID} behind {leaderID}. Details: {e}")

def get_route_for_start_to_end_edges(vehicle_id, start_edge, end_edge):
    """
    Get the route (edges) to get from start_edge to end_edge for a specific vehicle.
    """
    v_type = traci.vehicle.getTypeID(vehicle_id)
    stage = traci.simulation.findRoute(fromEdge=start_edge, toEdge=end_edge, vType=v_type)
    route, travel_time = stage.edges, stage.travelTime
    return route

    
def get_vehicles_by_type(vehicleType, as_substring=True):
    """
    Gets the vehicle IDs of all vehicles of specific type. 
    """
    allVehicleIds = traci.vehicle.getIDList()
    filteredVehicleIDs = []
    for vehicleId in allVehicleIds:
        if traci.vehicle.getTypeID(vehicleId) == vehicleType:
            filteredVehicleIDs.append(vehicleId)
        elif as_substring:
            if vehicleType in traci.vehicle.getTypeID(vehicleId):
                filteredVehicleIDs.append(vehicleId)
                
    return filteredVehicleIDs
      
def get_route_for_start_to_end_edges(vehicle_id, start_edge, end_edge):
    """
    Get the route (edges) to get from start_edge to end_edge for a specific vehicle.
    """
    v_type = traci.vehicle.getTypeID(vehicle_id)
    stage = traci.simulation.findRoute(fromEdge=start_edge, toEdge=end_edge, vType=v_type)
    route, travel_time = stage.edges, stage.travelTime
    return route
    
def insert_trip_into_sim(vehicleID, vClass, startEdge, destinationEdge, route_id=None, position='base', time_delay=10):
    """
    Dynamically adds a vehicle to the sim given the arguments. 
    """
    if route_id == None:
        route_id = f"route_{vehicleID}"
    
    try:
        route = traci.simulation.findRoute(fromEdge=startEdge, toEdge=destinationEdge, vType=vClass).edges
        if not route:
            raise ValueError(f"Unable to find a route from {startEdge} to {destinationEdge}")
    except Exception as e:
        print(f"Route calculation failed: {e}")
        return
    
    try:
        traci.route.add(route_id, route)
    except Exception as e:
        print(f"Could not add route '{route_id}': {e}")
        return
    
    traci.vehicle.add(
        vehID=vehicleID,
        routeID=route_id,
        typeID=vClass, 
        departPos=position,
        depart=traci.simulation.getTime() + time_delay  
    )

def get_vehicles_behind(vehicle_id, threshold):
    """
    Gets a list of vehicles that are behind a given vehicle. 
    Only gets vehicles within a given threshold, which is the leghth of the attach station.
    """
    # Get the current lane of the target vehicle
    lane_id = traci.vehicle.getLaneID(vehicle_id)
    if not lane_id:
        return []  # Vehicle might be off the road
    target_position = traci.vehicle.getLanePosition(vehicle_id)    
    vehicles_on_lane = traci.lane.getLastStepVehicleIDs(lane_id)
    vehicles_behind = []
    for veh in vehicles_on_lane:
        if veh != vehicle_id:
            veh_position = traci.vehicle.getLanePosition(veh)
            position_dif = target_position - veh_position
            if position_dif > 0 and position_dif < threshold:
                vehicles_behind.append((veh, position_dif))
    vehicles_behind.sort(key=lambda x: x[1]) # order list based on distance, closest firts
    return vehicles_behind