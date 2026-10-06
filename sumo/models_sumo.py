from __future__ import absolute_import
from __future__ import print_function
import os
import sys
import optparse
from . import sumo_utilities
import numpy as np
import math
from . import utilities
from pathlib import Path

# we need to import python modules from the $SUMO_HOME/tools directory
if 'SUMO_HOME' in os.environ:
    tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
    sys.path.append(tools)
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")

from sumolib import checkBinary  # noqa
import traci  # noqa
import sumolib  # noqa

class Reservation:
    """
    reservation class bundles all information which is specified by the customer
    """

    def __init__(self, id,startPos, endPos, registerTime, twType, lb, ub, tol_lower, tol_upper, numPersons, needRamp, 
                 rideSharing):

        self.id = id  
        self.puEdge = None
        self.doEdge = None
        self.startPos = startPos
        self.endPos = endPos
        self.registerTime = registerTime
        self.promisedPuTime = None
        self.promisedDoTime = None
        self.actualPuTime = None
        self.actualDoTime = None
        self.waitingTime = 0
        self.delayedDoTime = 0
        self.twType = twType
        self.numPersons = numPersons
        self.needRamp = needRamp
        self.rideSharing = rideSharing
        self.lb = lb
        self.ub = ub
        self.tol_lower = tol_lower # (not needed right now)
        self.tol_upper = tol_upper # (not needed right now)
        self.customReject = False
        self.vehicle = None

    

    def calculate_edges_and_offset(self, road_network):
        puEdge, doEdge = sumo_utilities.get_feasible_edges(self.startPos, self.endPos, road_network)

        if puEdge != None:
            self.puEdge, self.puOffset = puEdge[0].getID()[:-2], puEdge[1]
            self.doEdge, self.doOffset = doEdge[0].getID()[:-2], doEdge[1]
            return True
        else: 
            return False

    def set_actual_pu_time(self, actualPuTime):
        self.actualPuTime = actualPuTime
        self.waitingTime = utilities.relu(self.actualPuTime - self.promisedPuTime)

    def set_actual_do_time(self, actualDoTime):
        self.actualDoTime = actualDoTime
        self.delayedDoTime = utilities.relu(self.actualDoTime - self.promisedDoTime)



class ScheduleEntry: 
    """
    the schedule entry class represents the perspective of the taxi service of the reservations made by the customer (customerTransport)
    additional types of ScheduleEntries are: customerAppraoch, chargingAppraoach, chargingStop
    """
    buffer = 120
    dropOffTime = 30
    pickUpTime = 30 

    def __init__(self, id, reservationID, typeSchedule, 
                startPos, endPos, puEdge, doEdge, 
                startT, startEnergy, vehicle = None, chargingStation = None, parkingID = "", 
                endEnergy = None, platoonOffset = None, platoonEntryID = None):


        self.id = id 
        self.typeSchedule = typeSchedule 
        if self.typeSchedule == "chargingStop":
            self.reservationID = [int(reservationID)]
        else:
            self.reservationID = reservationID
        self.startPos = startPos
        self.endPos = endPos
        self.puEdge = puEdge
        self.doEdge = doEdge
        self.startT = math.ceil(startT)
        self.startEnergy = float(startEnergy)
        self.vehicle = vehicle

        self.chargingStation = chargingStation

        if self.typeSchedule != "chargingStop":
            # self.distance = traci.simulation.getDistanceRoad(self.puEdge, self.puOffset, self.doEdge, self.doOffset, isDriving = True)
            self.distance = sumo_utilities.route_length([self.puEdge, self.doEdge])
        else: 
            self.distance = 5

        if typeSchedule != "chargingStop":
            self.endEnergy = self.startEnergy - self.delta_energy()
        else: 
            # sanity check for Energy Level at the end of CS
            if endEnergy > self.vehicle.totalEnergyCapacity + 0.05:
                raise ValueError(f"""endEnergy: {endEnergy} > maxCapacity: {self.vehicle.totalEnergyCapacity} 
                    in chargingStop starting {self.startT} for reservation {self.reservationID} and {self.vehicle.id}""")
            self.endEnergy = endEnergy

        self.feasible = True 
        if typeSchedule == "chargingApproach" or typeSchedule == "chargingStop":
            if parkingID[0:3] != "pa_":
                self.parkingID = f"pa_{parkingID}"
            else: 
                self.parkingID = parkingID

        if typeSchedule == "platoonApproach":
            if platoonOffset:
                self.platoonOffset = platoonOffset
            self.parkingID = parkingID
        if typeSchedule == "platoonTransport":
            if platoonOffset:
                self.platoonOffset = platoonOffset 
            self.parkingID = parkingID
            self.platoonEntryID = platoonEntryID
        if typeSchedule == "attachApproach":
            if platoonOffset:
                self.platoonOffset = platoonOffset
            self.parkingID = parkingID
        if typeSchedule == "detachApproach":
            if platoonOffset:
                self.platoonOffset = platoonOffset
            self.parkingID = parkingID
        if self.endEnergy <= 0: 
            self.feasible = False

        self.endT = math.ceil(self.startT + self.delta_t())
        self.energyConsumption = self.startEnergy - self.endEnergy
        self.duration = self.endT - self.startT


    def set_offset(self, simulationObject):

        self.puOffset = simulationObject.road_network.getEdge(self.puEdge).getClosestLanePosDist(self.startPos)[1]
        self.doOffset = simulationObject.road_network.getEdge(self.doEdge).getClosestLanePosDist(self.endPos)[1]
        # self.distance = traci.simulation.getDistanceRoad(self.puEdge, int(self.puOffset), self.doEdge, int(self.doOffset), isDriving = True)


    def delta_energy(self):
        if self.typeSchedule != "chargingStop":
            if not (self.typeSchedule == "platoonTransport" or self.typeSchedule == "decouplingCab") :
                return sumo_utilities.energy_for_route([self.puEdge, self.doEdge], self.vehicle.energyConsumption)
            else:
                return 0
    
    def set_platoon_charging_time(self, charging_time, pro):
        charging_amount = charging_time * pro.maxPowerSupplyPerCab
        self.endEnergy = self.startEnergy + charging_amount
        self.energyConsumption = self.startEnergy - self.endEnergy
        self.maximum_charging = self.duration * pro.maxPowerSupplyPerCab 

    def set_platoon_energy_consumption(self, energyConsumption):
        self.energyConsumption = energyConsumption
        self.endEnergy = self.startEnergy - self.energyConsumption

    def set_custom_travel_time(self, simulationObject):
        net = simulationObject.road_network
        self.duration = int(sumo_utilities.custom_travel_time(self.puEdge, self.doEdge, self.vehicle, net))
        self.endT = self.startT + self.duration

    def delta_t(self):
        platoonEntries = ["detachApproach", "platoonTransport"]
        approachEntries = ["customerApproach", "platoonApproach"]

        if self.typeSchedule == "attachApproach":
            return sumo_utilities.travel_time([self.puEdge, self.doEdge], averageSpeed = self.vehicle.averageSpeed) + 60
        elif self.typeSchedule == "decouplingCab":
            return 15
        elif self.typeSchedule == "chargingStop":
            diffEnergy = self.endEnergy - self.startEnergy
            # Sanity check that endEnergy > startEnergy
            if diffEnergy < 0:
                raise ValueError(f"endEnergy ({self.endEnergy}) < startEnergy ({self.startEnergy}) in chargingStop starting {self.startT} for reservation {self.reservationID} and taxi {self.vehicle.id}")
            # calculate needed time in hours 
            neededTime = diffEnergy/(0.9*self.chargingStation.max_supply)
            # return needed time in seconds, charging stop needs to last atleast 20 seconds
            if neededTime * 3600 > 20: 
                return neededTime * 3600
            else: 
                return 20
        else:
            if self.vehicle.id.startswith("Pro"):
                return sumo_utilities.travel_time([self.puEdge, self.doEdge], averageSpeed = self.vehicle.averageSpeed)
            else: 
                return sumo_utilities.travel_time([self.puEdge, self.doEdge], averageSpeed = self.vehicle.cabSpeedModel(self.distance))
    
    def reset_startT(self, newStart):
        self.startT = math.ceil(newStart)
        self.endT = math.ceil(self.startT + self.delta_t()) 

    def reset_endT(self, newEnd):
        self.startT = newEnd - self.duration
        self.endT = newEnd

    def update_start_energy(self, newStartEnergy):
        self.startEnergy = newStartEnergy
        self.endEnergy   = self.startEnergy - self.energyConsumption

    def reset_endEnergy(self, newEndEnergy):
        self.endEnergy = newEndEnergy
        self.energyConsumption = self.startEnergy - self.endEnergy
        self.endT = math.ceil(self.startT + self.delta_t())
        self.duration = self.endT - self.startT



    def communicate_entry_start(self, vehicle, simulationObject):
        net = simulationObject.road_network

        self.start_distance = traci.vehicle.getDistance(vehicle.id)
        self.start_energy   = float(traci.vehicle.getParameter(vehicle.id, "device.battery.actualBatteryCapacity"))

        if self.typeSchedule == "customerApproach":
            if self.puEdge != self.doEdge:
                vehicle.state = "customerApproach"
                traci.vehicle.resume(vehicle.id)

                edge = simulationObject.road_network.getEdge(self.doEdge)
                length = edge.getLength()


                reservation = next((x for x in simulationObject.reservation_history if x.id == self.reservationID), None)#
                sumo_utilities.set_route_by_edges(vehicle.id, self.puEdge, self.doEdge, net)

                if self.doOffset > length - 0.1:
                    traci.vehicle.setStop(vehicle.id, self.doEdge, length - 0.1, flags = 1)
                elif self.doOffset <= 0.1: 
                    traci.vehicle.setStop(vehicle.id, self.doEdge, 0.1, flags = 1)
                else:
                    traci.vehicle.setStop(vehicle.id, self.doEdge, self.doOffset, flags = 1)


        elif self.typeSchedule == "customerTransport":
            
            traci.vehicle.resume(vehicle.id)
            sumo_utilities.set_route_by_edges(vehicle.id, self.puEdge, self.doEdge, net)
            reservation = next((x for x in simulationObject.reservation_history if x.id == self.reservationID), None)
            edge = simulationObject.road_network.getEdge(self.doEdge)
            length = edge.getLength()

            if self.doOffset > length - 0.1:
                traci.vehicle.setStop(vehicle.id, self.doEdge, length - 0.1, flags = 1)
            elif self.doOffset <= 0.1:
                traci.vehicle.setStop(vehicle.id, self.doEdge, 0.1, flags = 1)
            else:
                traci.vehicle.setStop(vehicle.id, self.doEdge, self.doOffset, flags = 1)

        elif self.typeSchedule == "chargingApproach":

            traci.vehicle.resume(vehicle.id)
            vehicle.state = "chargingApproach"
            offset = traci.parkingarea.getStartPos(self.parkingID)
            if vehicle.edge == self.doEdge: 
                edge = net.getEdge(vehicle.edge)
                intermediateEdge = list(edge.getAllowedOutgoing("taxi").keys())[0]._id
                route1 = traci.simulation.findRoute(vehicle.edge, intermediateEdge, vType = "taxi").edges[:-1]
                route2 = traci.simulation.findRoute(intermediateEdge, self.doEdge, vType = "taxi").edges
                totalRoute = route1 + route2
                traci.vehicle.setRoute(vehicle.id, totalRoute)
                traci.vehicle.setStop(vehicle.id, self.doEdge, offset - 5, flags = 1)
            else:
                sumo_utilities.set_route_by_edges(vehicle.id, self.puEdge, self.doEdge, net)
                traci.vehicle.setStop(vehicle.id, self.doEdge, offset - 5, flags = 1)

        elif self.typeSchedule == "chargingStop":
            # update energy level
            traci.vehicle.resume(vehicle.id)
            vehicle.start_charging(self.duration)
            traci.vehicle.setParkingAreaStop(vehicle.id, self.parkingID, self.duration)


        elif self.typeSchedule == "platoonApproach":
            traci.vehicle.resume(vehicle.id)
            sumo_utilities.set_route_by_edges(vehicle.id, self.puEdge, self.doEdge, net)
            traci.vehicle.setParkingAreaStop(vehicle.id, self.parkingID)

        elif self.typeSchedule == "platoonTransport":
            # traci.vehicle.resume(self.id)
            pass 


        elif self.typeSchedule == "attachApproach":
            # make pro-cab go to location to allow vehicles to attach
            vehicle.state == "attachApproach"
            traci.vehicle.resume(vehicle.id)
            sumo_utilities.set_route_by_edges(vehicle.id, self.puEdge, self.doEdge, net)
            traci.vehicle.setParkingAreaStop(vehicle.id, self.parkingID)

                                
        elif self.typeSchedule == "detachApproach":
            attachedTaxiIDs = vehicle.platoonCabs.keys()

            if len(attachedTaxiIDs) == 0: 
                simulationObject.empty_pro_tours = simulationObject.empty_pro_tours + 1  
            else:
                simulationObject.platoon_lengths.append(len(attachedTaxiIDs))
            self.numCabs = len(attachedTaxiIDs)
            for attachedTaxiID in attachedTaxiIDs:
                attachedTaxi = next((x for x in simulationObject.taxis if x.id == attachedTaxiID), None)
                currentTaxiEntry = attachedTaxi.schedule[0]
                if 'platoonApproach' == currentTaxiEntry.typeSchedule:
                    with open(simulationObject.event_based_log_file, "a") as f:
                        endPos = simulationObject.road_network.convertXY2LonLat(currentTaxiEntry.endPos[0], currentTaxiEntry.endPos[1])
                        f.write((f"{attachedTaxi.id} completes {currentTaxiEntry.typeSchedule} for Reservation {currentTaxiEntry.reservationID} at step {currentTaxiEntry.endT} "
                    f"with position {endPos} and energy level {currentTaxiEntry.endEnergy} using {currentTaxiEntry.platoonEntryID} "
                    f"and realised time {currentTaxiEntry.realised_time} and distance {currentTaxiEntry.realised_distance} and energy consumption {currentTaxiEntry.realised_energy_consumption}\n"))
                    attachedTaxi.schedule.remove(currentTaxiEntry)
                    simulationObject.entry_history.append(currentTaxiEntry)

            traci.vehicle.resume(vehicle.id)
            sumo_utilities.set_route_by_edges(vehicle.id, self.puEdge, self.doEdge, net)
            traci.vehicle.setParkingAreaStop(vehicle.id, self.parkingID)
            attachedTaxiIDs = vehicle.platoonCabs.keys()

    def log_entry_start(self, vehicle, simulationObject):
        with open(simulationObject.event_based_log_file, "a") as f:
            startPos = simulationObject.road_network.convertXY2LonLat(self.startPos[0], self.startPos[1])
            pureCabEntries = ["customerApproach", "customerTransport", "chargingApproach", "chargingStop"]
            
            if self.typeSchedule in pureCabEntries:
                f.write((f"{vehicle.id} starts {self.typeSchedule} for Reservation {self.reservationID} at step {self.startT} "
                    f"with position {startPos} and energy level {self.startEnergy} "
                    f"and approximated time {self.duration} and distance {self.distance} and energy consumption {self.energyConsumption}\n"))

            elif self.typeSchedule == "detachApproach": 
                f.write((f"{vehicle.id} starts {self.typeSchedule} at step {self.startT} with {self.numCabs} cabs attached "
                    f"with position {startPos} and energy level {self.startEnergy} for {self.id} "
                    f"and approximated time {self.duration} and distance {self.distance} and energy consumption {self.energyConsumption}\n"))

            elif self.typeSchedule == "attachApproach":
                f.write((f"{vehicle.id} starts {self.typeSchedule} at step {self.startT} "
                    f"with position {startPos} and energy level {self.startEnergy} for {self.id} "
                    f"and approximated time {self.duration} and distance {self.distance} and energy consumption {self.energyConsumption}\n"))
            else:
                f.write((f"{vehicle.id} starts {self.typeSchedule} for Reservation {self.reservationID} at step {self.startT} "
                    f"with position {startPos} and energy level {self.startEnergy} using {self.platoonEntryID} "
                    f"and approximated time {self.duration} and distance {self.distance} and energy consumption {self.energyConsumption}\n"))

    def communicate_entry_end(self, index, step, vehicle, simulationObject):
        net = simulationObject.road_network

        if self.typeSchedule != "chargingStop":
            self.realised_time = self.duration - vehicle.stop_time
        else:
            self.realised_time = vehicle.stop_time

        if self.typeSchedule != "platoonTransport" and self.typeSchedule != "decouplingCab":
            self.realised_distance = (traci.vehicle.getDistance(vehicle.id) -  self.start_distance)
            self.realised_energy_consumption = (self.start_energy
                                          - float(traci.vehicle.getParameter(vehicle.id, "device.battery.actualBatteryCapacity")))
        if self.typeSchedule == "customerApproach":
            for x in simulationObject.reservation_history:
                if x.id == self.reservationID:
                    x.set_actual_pu_time(step)#
            vehicle.start_pickup()
        elif self.typeSchedule == "customerTransport":
            for x in simulationObject.reservation_history:
                if x.id == self.reservationID:
                    x.set_actual_do_time(step)
            vehicle.start_dropoff()
        elif self.typeSchedule == "decouplingCab":
            vehicle.state = "idle"
        elif self.typeSchedule == "chargingApproach": 
            vehicle.state = "idle"
        elif self.typeSchedule == "attachApproach":
            vehicle.state = "attachingStop"
        elif self.typeSchedule == "detachApproach":
            vehicle.compute_trip_energy()
            # completedPlatoonTripInfoDict['trips'].append(vehicle.generate_trip_info())
            
            # update taxi schedules so that they drive off 
            attachedTaxiIDs = vehicle.platoonCabs.keys()
            for attachedTaxiID in attachedTaxiIDs:
                attachedTaxi = next((x for x in simulationObject.taxis if x.id == attachedTaxiID), None)
                currentTaxiEntry = attachedTaxi.schedule[0]

                # remove plaroonTransport entry from their schedules
                if 'platoonTransport' == currentTaxiEntry.typeSchedule:
                    with open(simulationObject.event_based_log_file, "a") as f:
                        endPos = simulationObject.road_network.convertXY2LonLat(currentTaxiEntry.endPos[0], currentTaxiEntry.endPos[1])
                        currentTaxiEntry.realised_time = self.realised_time
                        currentTaxiEntry.realised_distance = self.realised_distance
                        attachedTaxi.platooningDistance = attachedTaxi.platooningDistance + self.realised_distance
                        f.write((f"{attachedTaxi.id} completes {currentTaxiEntry.typeSchedule} for Reservation {currentTaxiEntry.reservationID} at step {currentTaxiEntry.endT} "
                    f"with position {endPos} and energy level {currentTaxiEntry.endEnergy} using {currentTaxiEntry.platoonEntryID} "
                    f"and realised time {currentTaxiEntry.realised_time} and distance {currentTaxiEntry.realised_distance} and energy consumption {currentTaxiEntry.energyConsumption}\n"))
                        
                    attachedTaxi.lastEntry = attachedTaxi.schedule[0]
                    decouplingEntry =  ScheduleEntry(None, None, "decouplingCab", 
                                attachedTaxi.schedule[0].endPos, attachedTaxi.schedule[0].endPos, attachedTaxi.schedule[0].doEdge, attachedTaxi.schedule[0].doEdge, 
                                step, attachedTaxi.schedule[0].endEnergy)
                    simulationObject.entry_history.append(attachedTaxi.schedule[0])
                    attachedTaxi.schedule.remove(attachedTaxi.schedule[0])
                    attachedTaxi.schedule.insert(0, decouplingEntry)
                    
                                     
            vehicle.detach_cabs(self.parkingID, step, self.doEdge, self.realised_time, 0)

        if self.typeSchedule not in ["platoonApproach", "platoonTransport"]:
            vehicle.schedule.remove(self)
            if vehicle.id[0] == "t":
                simulationObject.entry_history.append(self)
        vehicle.lastEntry = self

    def log_entry_end(self, vehicle, simulationObject):
        with open(simulationObject.event_based_log_file, "a") as f:
            endPos = simulationObject.road_network.convertXY2LonLat(self.endPos[0], self.endPos[1])
            pureCabEntries = ["customerApproach", "customerTransport", "chargingApproach", "chargingStop"]

            if self.typeSchedule in pureCabEntries:
                f.write((f"{vehicle.id} completes {self.typeSchedule} for Reservation {self.reservationID} at step {self.endT} "
                    f"with position {endPos} and energy level {self.endEnergy} "
                    f"and realised time {self.realised_time} and distance {self.realised_distance} and energy consumption {self.realised_energy_consumption}\n"))

            elif self.typeSchedule == "detachApproach":
                f.write((f"{vehicle.id} completes {self.typeSchedule} at step {self.endT} with {self.numCabs} cabs attached "
                    f"with position {endPos} and energy level {self.endEnergy} for {self.id} "
                    f"and realised time {self.realised_time} and distance {self.realised_distance} and energy consumption {self.realised_energy_consumption}\n"))
            elif self.typeSchedule == "attachApproach":
                f.write((f"{vehicle.id} completes {self.typeSchedule} at step {self.endT} "
                    f"with position {endPos} and energy level {self.endEnergy} for {self.id} "
                    f"and realised time {self.realised_time} and distance {self.realised_distance} and energy consumption {self.realised_energy_consumption}\n"))

class CandidateSolution: 

    def __init__(self, taxi, entries, oldApproach, pro = None): 

        self.taxi = taxi
        self.pro = pro
        self.entries = entries 
        self.entry_dict = {f"{entry.typeSchedule}": entry for entry in self.entries}
        self.reservationID = entries[0].reservationID
        self.isolated_entries, self.new_approach = self.return_isolated_entries()
        self.old_approach = oldApproach
        self.total_driving_time = self.return_canditate_driving_time()
        self.isolated_driving_time = self.return_isolated_canditate_driving_time()
        self.deleted_cs = False
        self.set_total_time()
        # if self.pro != None:
        #     self.total_waiting_time = self.return_candidate_waiting_time()
        #     self.total_time = self.total_driving_time + self.total_waiting_time
        #     self.isolated_total_time = self.isolated_driving_time + self.total_waiting_time
        # else:
        #     self.total_time = self.total_driving_time 
        #    self.isolated_total_time = self.isolated_driving_time

        self.total_energy = self.return_candidate_energy()
        self.isolated_total_energy = self.return_isolated_total_energy()
        self.total_distance = self.return_candidate_distance()
        self.isolated_total_distance = self.return_isolated_total_distance()

    def check_cs_necessity(self):
        if "chargingStop" in self.entry_dict.keys():
            if self.entries[-1].typeSchedule == "customerApproach":
                offset = -1
                self.new_approach = self.entries[-1] 
                new_approach_energy = self.new_approach.energyConsumption 
            elif self.entries[-1].typeSchedule == "chargingStop":
                offset = 0
                new_approach_energy = 0
            self.charging_stop_m = self.entries[-1 + offset]
            if self.charging_stop_m.energyConsumption == 0:
                self.charging_approach_m = self.entries[-2 + offset]
                self.customer_transport = self.entries[-3 + offset]
                self.platoon_transport = self.entries[-4 + offset]

                saved_energy = (self.charging_approach_m.energyConsumption 
                            + new_approach_energy)
                needed_energy = sumo_utilities.energy_for_route([self.customer_transport.doEdge, 
                                                                self.new_approach.doEdge], 
                                                                energyConsumption = self.taxi.energyConsumption)
                feasible = False
                energy_diff = saved_energy - needed_energy
                if energy_diff > 0: 
                    # -> charge less in platoon
                    energy_balance = energy_diff + self.platoon_transport.energyConsumption
                    if energy_balance < 0:
                        self.platoon_transport.set_platoon_energy_consumption(energy_balance)
                        feasible = True
                else: 
                    # cant be the case right now
                    print("Case that we need to charge more should not exist")
                    exit()
                    max_charging = min(self.platoon_transport.maximum_charging, 
                                       self.taxi.totalEnergyCapacity - self.platoon_transport.startEnergy)
                    if energy_diff < max_charging:
                        self.platoon_transport.set_platoon_energy_consumption(energy_diff)
                        feasible = True
                
                if feasible:
                    # update customer transport start energy as per new platoon charging
                    self.customer_transport.update_start_energy(self.platoon_transport.endEnergy)
                    # delete chargingApproach, chargingStop, newApproach
                    to_remove = [self.charging_approach_m, self.charging_stop_m, self.new_approach]
                    self.entries = [entry for entry in self.entries if entry not in to_remove]
                    # create new approach after customer transport 
                    new_approach = ScheduleEntry(self.customer_transport.id + 1, self.new_approach.reservationID, "customerApproach", 
                    self.customer_transport.endPos, self.new_approach.endPos, 
                    self.customer_transport.doEdge, self.new_approach.doEdge, 
                    self.customer_transport.endT + ScheduleEntry.dropOffTime + 1, 
                    self.customer_transport.endEnergy, vehicle = self.taxi)
                    self.entries.append(new_approach)
                    # set total time with new entries
                    self.deleted_cs = True
                    self.set_total_time()
                    return False

    def set_total_time(self):
        if self.new_approach == None: 
            new_approach_duration = 0 
        else: 
            new_approach_duration = self.new_approach.duration
        self.total_time = self.isolated_entries[-1].endT - self.isolated_entries[0].startT  + new_approach_duration
  
    def return_candidate_energy(self):

        return sum([entry.energyConsumption for entry in self.entries if entry != None and entry.typeSchedule != "chargingStop"])

    def return_isolated_total_energy(self):

        return sum([entry.energyConsumption for entry in self.isolated_entries])

    def return_isolated_total_distance(self):

        return sum([entry.distance for entry in self.isolated_entries])

    def return_candidate_distance(self):
        
        return sum([entry.distance for entry in self.entries if entry != None])

    def return_candidate_waiting_time(self):

        for entry in self.entries:
            if entry.typeSchedule == "platoonApproach":
                platoonApproach = entry
            elif entry.typeSchedule == "platoonTransport":
                platoonTransport = entry
            elif entry.typeSchedule == "customerTransport":
                customerTransport = entry 

        return (platoonTransport.startT - platoonApproach.endT) + (customerTransport.startT - platoonTransport.endT)

    def return_isolated_entries(self):
        isolated_entries = [entry for entry in self.entries if entry != None and entry.typeSchedule != "chargingStop" and entry.typeSchedule != "chargingApproach"]
        if isolated_entries[-1].typeSchedule == "customerApproach": 
            new_approach = isolated_entries.pop(-1)
            return isolated_entries, new_approach
        else:
            return isolated_entries, None

    def return_isolated_canditate_driving_time(self): 

        return sum([entry.duration for entry in self.isolated_entries])


    def return_canditate_driving_time(self):

        return sum([entry.duration for entry in self.entries if entry != None and entry.typeSchedule != "chargingStop"])


class Taxi: 
    """
    taxi class which mirrors the taxis in the sumo simulation
    """
    threshold = 500
    dropOffTime = 30
    pickUpTime = 30
    def __init__(self, id, state, position, initialEnergyCapacity, 
                 totalEnergyCapacity, energyConsumption, maxSpeed, hasRamp, seats, maxToAverage = 2/3, cabSpeedModel = None):
        self.id = id
        self.state = state
        self.empty = True
        self.position = position
        self.edge = None
        self.energy = initialEnergyCapacity
        self.initialEnergyCapacity = initialEnergyCapacity
        self.totalEnergyCapacity = totalEnergyCapacity
        self.energyConsumption = energyConsumption
        self.maxSpeed = maxSpeed
        self.averageSpeed = self.maxSpeed * maxToAverage
        self.cabSpeedModel = cabSpeedModel
        self.platooningDistance = 0
        self.hasRamp = hasRamp
        self.seats = seats
        self.schedule = []
        self.lastEntry = None
        self.remainingStop = 1000000000
        self.allDeltas = []
        self.highestViolation = None
        self.num_requests = 0
        self.stop_time = 0

    def calculate_edge(self, net):

        self.edge = sumo_utilities.get_lane_and_offset(self.position, net, lat_lon = False)[0].getID()[:-2]
    
    def start_pickup(self): 
        self.state = "pickingUp"
        self.remainingStop = ScheduleEntry.pickUpTime 

    def start_dropoff(self): 
        self.state = "droppingOff"
        self.remainingStop = ScheduleEntry.dropOffTime 

    def start_charging(self, duration):
        self.state = "chargingStop"
        self.remainingStop = duration 

    def check_if_in_sim(self):
        """
        function which checks to see if the taxi is in the simulation
        """
        allVehicles = traci.vehicle.getLoadedIDList()
        return self.id in allVehicles

    def update_state(self, step, net):
        """
        function which updates the state of the taxi based on state it is in and the event occuring
        """
        # check if taxi is in the sim

        if not self.check_if_in_sim():
            self.state = 'notInSim'
            self.lastChange = step
            return
        else:
            if traci.vehicle.getSpeed(self.id) == 0: 
                self.stop_time = self.stop_time + 1
            else: 
                self.stop_time = 0

        if self.state == "customerApproach":
            pass

        elif self.state == "pickingUp":
            self.remainingStop = self.remainingStop - 1
            if self.remainingStop == 0: 
                self.state = "idle"

        elif self.state == "customerTransport":
            self.remainingStop = self.remainingStop - 1
            if self.remainingStop == 0: 
                self.state = "idle"

        elif self.state == "droppingOff":
            self.remainingStop = self.remainingStop - 1
            if self.remainingStop == 0: 
                self.state = "idle"
        elif self.state == "chargingApproach":
            pass

        elif self.state == "chargingStop":
            self.remainingStop = self.remainingStop - 1 
            if self.remainingStop == 0: 
                self.state = "idle"
                for index, entry in enumerate(self.schedule): 
                    if entry.endT == step: 
                        if self.schedule[index + 1].startT - entry.endT > 1: 
                            offset = traci.parkingarea.getStartPos(entry.parkingID)
                            traci.vehicle.setStop(self.id, entry.doEdge, offset + 15, flags = 1)


        elif self.state == "idle":
            pass


    def synchronize_state(self, net, edgeIDs):
        """
        function which is called each step and syncronizes the state of the taxi object with the taxis in the sumo simulation
        """
        if self.state != "notInSim" and self.state != "chargingStop":
            self.edge = sumo_utilities.get_edge_of_car(self, edgeIDs, net)
            self.position = traci.vehicle.getPosition(self.id)
            # we do not use sumos energy model anymore 
            # self.energy = float(traci.vehicle.getParameter(self.id, "device.battery.actualBatteryCapacity"))


    
    def return_all_candidates(self, step, reservation, simulationObject):
        """
        function which iterates through the schedule of the taxi and returns the idle time window best suitable to insert the reservation
        """
        candidates_without_platooning = []
        candidates_with_platooning = []
        if reservation.lb > simulationObject.simulation_end or reservation.ub < simulationObject.ramp_up_time:
            return [], [], False
            
        if self.schedule: 
            for index in range(0, len(self.schedule) + 1): 
                checkTW = True
                if index == 0:
                    entry = self.schedule[index]
                    if entry.typeSchedule == "customerApproach" or entry.typeSchedule == "chargingApproach":
                        first = None 
                        approach = entry
                        third = self.schedule[index + 1]    
                    else: 
                        checkTW = False

                elif index == (len(self.schedule)):
                    first = self.schedule[index - 1]  
                    approach = None 
                    third = None

                else: 
                    entry = self.schedule[index]
                    if entry.typeSchedule == "customerApproach" or entry.typeSchedule == "chargingApproach":
                        first = self.schedule[index - 1]   
                        approach = entry  
                        third = self.schedule[index + 1]  

                    else: 
                        checkTW = False

           
                if checkTW:

                    if not simulationObject.only_platoon:
                        candidate_without_platooning = self.return_canditate_without_platooning(step, reservation, simulationObject, first, approach, third)
                        if candidate_without_platooning != False: 
                           candidate = CandidateSolution(self, candidate_without_platooning, approach)
                           candidates_without_platooning.append(candidate)

                    if not simulationObject.only_cabs:
                        for pro in simulationObject.pros:
                            candidate_with_platooning = self.return_canditate_with_platooning(step, reservation, simulationObject, first, approach, third, pro)
                            if candidate_with_platooning != False:
                                candidate = CandidateSolution(self, candidate_with_platooning, approach, pro)
                                candidates_with_platooning.append(candidate)


        else:
            first = None 
            approach = None 
            third = None
            if not simulationObject.only_platoon:
                candidate_without_platooning = self.return_canditate_without_platooning(step, reservation, simulationObject, first, approach, third)
                if candidate_without_platooning != False: 
                   candidate = CandidateSolution(self, candidate_without_platooning, approach)
                   candidates_without_platooning.append(candidate)

            if not simulationObject.only_cabs:
                for pro in simulationObject.pros:
                    candidate_with_platooning = self.return_canditate_with_platooning(step, reservation, simulationObject, first, approach, third, pro)
                    if candidate_with_platooning != False:
                        candidate = CandidateSolution(self, candidate_with_platooning, approach, pro)
                        candidates_with_platooning.append(candidate)

        for candidate in candidates_with_platooning:
            needed = candidate.check_cs_necessity()

        return candidates_without_platooning, candidates_with_platooning, True


    def return_canditate_without_platooning(self, step, reservation, simulationObject, first, approach, third):

        (
        feasible, timeLeft,
        chargingApproach_n, chargingStop_n,
        customerApproach, customerTransport,
        chargingApproach_m, chargingStop_m,
        newApproach 
        ) = self.check_feasibility_without_platooning(step, reservation, simulationObject, first, approach, third)
        if feasible: 
            # decide where to place the entries within the available time frame:
            (
            delta, feasible,
            chargingApproach_n, chargingStop_n, 
            customerApproach, customerTransport, 
            chargingApproach_m, chargingStop_m, 
            newApproach 
            ) = self.finalize_entries_without_platooning(step, simulationObject, first, third,
                                    reservation, feasible, timeLeft, 
                                    chargingApproach_n, chargingStop_n, 
                                    customerApproach, customerTransport, 
                                    chargingApproach_m, chargingStop_m, 
                                    newApproach)

            if feasible and delta == 0: 
                thirdBest = third
                toSchedule = [chargingApproach_n, chargingStop_n, 
                              customerApproach, customerTransport,
                              chargingApproach_m, chargingStop_m, 
                              newApproach]
                toScheduleReturn = [entry for entry in toSchedule if entry != None]
            else:
                toScheduleReturn = False

            
            return toScheduleReturn
        
        else:
            return False


    def return_canditate_with_platooning(self, step, reservation, simulationObject, first, approach, third, pro):
        (
        feasible, timeLeft,
        chargingApproach_n, chargingStop_n,
        customerApproach, 
        platoonApproach, platoonTransport,
        customerTransport,
        chargingApproach_m, chargingStop_m,
        newApproach 
        ) = self.check_feasibility_with_platooning(step, reservation, simulationObject, first, approach, third, pro)
        if feasible: 
            # decide where to place the entries within the available time frame:
            (
            delta, feasible,
            chargingApproach_n, chargingStop_n,
            customerApproach, 
            platoonApproach, platoonTransport,
            customerTransport,
            chargingApproach_m, chargingStop_m,
            newApproach 
            ) = self.finalize_entries_with_platooning(step, simulationObject, first, third,
                                    reservation, 
                                    chargingApproach_n, chargingStop_n, 
                                    customerApproach, 
                                    platoonApproach, platoonTransport,
                                    customerTransport, 
                                    chargingApproach_m, chargingStop_m, 
                                    newApproach, pro)
            if feasible and delta == 0: 
                thirdBest = third
                toSchedule = [chargingApproach_n, chargingStop_n, 
                          customerApproach, 
                          platoonApproach, platoonTransport,
                          customerTransport,
                          chargingApproach_m, chargingStop_m, 
                          newApproach]
                toScheduleReturn = [entry for entry in toSchedule if entry != None] 
            else:

                toScheduleReturn = False

            
            return toScheduleReturn
        
        else:
            return False



    def check_feasibility_without_platooning(self, step, reservation, simulationObject, firstEntry, approach, thirdEntry):
         # schedules the reservation into the intervall defined by startIntervall and endIntervall, returns if the intervall is feasible and the violation of the tw

        energyFeasible, missingEnergy = self.check_energy_feasibility_without_platooning(reservation, simulationObject, firstEntry, approach)
        if energyFeasible:
            # ZWEIG A
            (
            feasible, timeLeft, 
            customerApproach, customerTransport, 
            chargingApproach_m, chargingStop_m, 
            newApproach 
            ) = self.check_time_feasibility_without_platooning(step, simulationObject, reservation, 
                                                      firstEntry, approach, thirdEntry)

            chargingApproach_n = None 
            chargingStop_n = None       
        else: 
            # ZWEIG B
            (
            feasible, timeLeft, 
            chargingApproach_n, chargingStop_n, 
            customerApproach, customerTransport, 
            chargingApproach_m, chargingStop_m, 
            newApproach 
            ) = self.charge_and_check_time_feasibility_without_platooning(step, simulationObject, reservation, missingEnergy, 
                                                                firstEntry, approach, thirdEntry)

        return (feasible, timeLeft, 
        chargingApproach_n, chargingStop_n, 
        customerApproach, customerTransport, 
        chargingApproach_m, chargingStop_m, 
        newApproach)


    def check_feasibility_with_platooning(self, step, reservation, simulationObject, firstEntry, approach, thirdEntry, pro):
        energyFeasible, missingEnergy, platoon_charging_duration = self.check_energy_feasibility_with_platooning(reservation, simulationObject, firstEntry, approach, pro)
        if energyFeasible:
            # ZWEIG A
            (
            feasible, timeLeft,
            customerApproach, 
            platoonApproach, platoonTransport,
            customerTransport,
            chargingApproach_m, chargingStop_m,
            newApproach 
            ) = self.check_time_feasibility_with_platooning(step, simulationObject, reservation, 
                                                      firstEntry, approach, thirdEntry, pro, platoon_charging_duration)
            chargingApproach_n = None 
            chargingStop_n = None       
        else: 
            # ZWEIG B
            (
            feasible, timeLeft,
            chargingApproach_n, chargingStop_n,
            customerApproach, 
            platoonApproach, platoonTransport,
            customerTransport,
            chargingApproach_m, chargingStop_m,
            newApproach 
            ) = self.charge_and_check_time_feasibility_with_platooning(step, simulationObject, reservation, missingEnergy, 
                                                                firstEntry, approach, thirdEntry, pro, platoon_charging_duration)

        return (feasible, timeLeft,
            chargingApproach_n, chargingStop_n,
            customerApproach, 
            platoonApproach, platoonTransport,
            customerTransport,
            chargingApproach_m, chargingStop_m,
            newApproach)


    def check_energy_feasibility_without_platooning(self, reservation, simulationObject, firstEntry, approach):

        if firstEntry != None: 

            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy

        else: 
            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy


        pickUp = reservation.puEdge
        dropOff = reservation.doEdge
        # Compute the charging station which is closest to the dropoff point
        nearestCharging = sumo_utilities.nearest_station(reservation.doEdge, simulationObject)

        chargingEdge = nearestCharging.lane.getID()[:-2]

        complete_trip = [initialEdge, pickUp, dropOff, chargingEdge]
        remainingEnergy = endEnergy - sumo_utilities.energy_for_route(complete_trip, self.energyConsumption) - Taxi.threshold

        if remainingEnergy >= 0:
            return True, 0
        else: 
            return False, remainingEnergy



    def check_energy_feasibility_with_platooning(self, reservation, simulationObject, firstEntry, approach, pro):

        if firstEntry != None: 

            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy

        else: 
            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy

        pickUp = reservation.puEdge
        dropOff = reservation.doEdge
        # Pick appropriate platoon
        pro.chain_routes.sort(key = lambda chain_route: 
                                        sumo_utilities.travel_time([pickUp, chain_route.start_location.lane.getID()[:-2]]))

        chain_route = pro.chain_routes[0]
        pro.chain_points = [chain_route.start_location, chain_route.end_location] 

        # Compute the charging station which is closest to the dropoff point
        nearestCharging = sumo_utilities.nearest_station(dropOff, simulationObject)
        
        platoon_duration_hours = None
        for entry in pro.schedule: 
            if entry.id.split("_")[0] + "_" + entry.id.split("_")[1] == chain_route.id: 
                platoon_duration_hours = entry.duration / 3600
                break


        chargingEdge = nearestCharging.lane.getID()[:-2]
        startPlatoonEdge = pro.chain_points[0].lane.getID()[:-2]
        endPlatoonEdge = pro.chain_points[1].lane.getID()[:-2]
        tripToPlatoon = [initialEdge, pickUp, startPlatoonEdge]
        tripFromPlatoon = [endPlatoonEdge, dropOff, chargingEdge]

        max_charging = (self.totalEnergyCapacity 
                       - (endEnergy 
                       - sumo_utilities.energy_for_route(tripToPlatoon, self.energyConsumption))) 


        if pro.maxPowerSupplyPerCab != 0:
            max_charging_duration = (max_charging / pro.maxPowerSupplyPerCab)
        else:
            max_charging_duration = platoon_duration_hours

        if platoon_duration_hours != None:
            charging_duration = min(platoon_duration_hours, max_charging_duration) 


            remainingEnergy = (endEnergy - sumo_utilities.energy_for_route(tripToPlatoon, self.energyConsumption) 
                                         + charging_duration * pro.maxPowerSupplyPerCab
                                         - sumo_utilities.energy_for_route(tripFromPlatoon, self.energyConsumption) 
                                         - Taxi.threshold)
        else: 
            remainingEnergy = -1
        if remainingEnergy >= 0 and platoon_duration_hours != None:
            return True, 0, charging_duration
        else: 
            return False, remainingEnergy, 0


    def check_time_feasibility_without_platooning(self, step, simulationObject, reservation, firstEntry, approach, thirdEntry):

        customerApproach = self.create_customer_approach(step, firstEntry, approach, reservation)

        customerTransport = ScheduleEntry(len(self.schedule) + 1, reservation.id, "customerTransport", 
                            customerApproach.endPos, reservation.endPos, reservation.puEdge, reservation.doEdge, 
                            customerApproach.endT + ScheduleEntry.pickUpTime + 1, customerApproach.endEnergy, vehicle = self)

        # Disregard new_platoon_charging_duration
        (energyFeasible, 
        chargingApproach_m, 
        chargingStop_m, 
        newApproach) = self.create_charging_stop_m(firstEntry, reservation, customerApproach, customerTransport, approach, thirdEntry, simulationObject)


        availableTime = self.compute_available_time(step, firstEntry, thirdEntry, simulationObject)
        
        if energyFeasible:
            completeTrip = [customerApproach, customerTransport, chargingApproach_m, chargingStop_m, newApproach]
            feasible, timeLeft = self.check_available_time(availableTime, completeTrip)

        else: 
            feasible = energyFeasible
            timeLeft = 0
            chargingApproach_m = None
            chargingStop_m = None 
            newApproach = None

        return (feasible, timeLeft, 
        customerApproach, customerTransport, 
        chargingApproach_m, chargingStop_m, 
        newApproach)

    def check_time_feasibility_with_platooning(self, step, simulationObject, reservation, 
                                                firstEntry, approach, thirdEntry, 
                                                pro, platoon_charging_duration):
        net = simulationObject.road_network
        chargingStations = simulationObject.operations_area.charging_stations

        customerApproach = self.create_customer_approach(step, firstEntry, approach, reservation)


        platoonApproach, platoonTransport, feasible = self.create_platoon_entries(customerApproach, reservation, pro)

        if feasible:
            # set the charging for the platoontransport for now
            platoonTransport.set_platoon_charging_time(platoon_charging_duration, pro)
            customerTransport = ScheduleEntry(platoonTransport.id + 1, reservation.id, "customerTransport", 
                                platoonTransport.endPos, reservation.endPos, platoonTransport.doEdge, reservation.doEdge, 
                                platoonTransport.endT + 45, platoonTransport.endEnergy, vehicle = self)

            (energyFeasible, 
            chargingApproach_m, 
            chargingStop_m, 
            newApproach) = self.create_charging_stop_m(firstEntry, reservation, 
                                                      customerApproach, customerTransport, 
                                                      approach, thirdEntry, simulationObject, 
                                                      pro, platoonApproach, platoonTransport, platoon_charging_duration)

            availableTime = self.compute_available_time(step, firstEntry, thirdEntry, simulationObject)
            
            if energyFeasible:
                completeTrip = [customerApproach, platoonApproach, platoonTransport, customerTransport, chargingApproach_m, chargingStop_m, newApproach]
                feasible, timeLeft = self.check_available_time(availableTime, completeTrip)

            else: 
                feasible = energyFeasible
                timeLeft = 0
                chargingApproach_m = None
                chargingStop_m = None 
                newApproach = None
        else: 
            feasible = False
            timeLeft = 0
            customerTransport = False
            chargingApproach_m = None
            chargingStop_m = None 
            newApproach = None


        return (feasible, timeLeft, 
        customerApproach, 
        platoonApproach, platoonTransport,
        customerTransport, 
        chargingApproach_m, chargingStop_m, 
        newApproach)

    def charge_and_check_time_feasibility_without_platooning(self, step, simulationObject, reservation, missingEnergy, firstEntry, approach, thirdEntry):
        # schedule charging to realize energy feasibility 
        # Compute the charging station which is closest to the dropoff point
        net = simulationObject.road_network
        chargingStations = simulationObject.operations_area.charging_stations

        if firstEntry != None: 

            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy

        else: 
            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy


        chargingApproach_n, nearestCharging_n = self.create_charging_approach_n(step, firstEntry, approach, reservation, simulationObject)

       
        energyFeasible = False
         # determine where to charge after the customer transport
        (
        energyFeasible, 
        nearestCharging, 
        energyToCharge_m, 
        energyToCharge_n,
        _
        ) = sumo_utilities.nearest_feasible_station(chargingApproach_n, 
                                                    reservation,initialEdge, 
                                                    endEnergy, 
                                                    approach, thirdEntry, 
                                                    simulationObject)
        if energyFeasible:

            chargingStop_n = self.create_charging_stop_n(step, chargingApproach_n, reservation, energyToCharge_n, nearestCharging_n,simulationObject)


            customerApproach = ScheduleEntry(chargingStop_n.id + 1, reservation.id, "customerApproach", 
                    chargingStop_n.endPos, reservation.startPos, chargingStop_n.doEdge, reservation.puEdge, chargingStop_n.endT + 30, chargingStop_n.endEnergy, vehicle = self)


            customerTransport = ScheduleEntry(customerApproach.id + 1, reservation.id, "customerTransport", 
                                customerApproach.endPos, reservation.endPos, reservation.puEdge, reservation.doEdge, 
                                customerApproach.endT + ScheduleEntry.pickUpTime + 1, customerApproach.endEnergy, vehicle = self)

            (energyFlagM, 
            chargingApproach_m, chargingStop_m, 
            newApproach) = self.create_charging_stop_m(firstEntry, reservation, customerApproach, customerTransport, approach, thirdEntry, simulationObject, charging_approach_n =  chargingApproach_n)

            if energyFlagM:
                availableTime = self.compute_available_time(step, firstEntry, thirdEntry, simulationObject)

                completeTrip = [chargingApproach_n, chargingStop_n, 
                                customerApproach, customerTransport, 
                                chargingApproach_m, chargingStop_m, 
                                newApproach]
                feasible, timeLeft = self.check_available_time(availableTime, completeTrip)
            else: 
                feasible = energyFlagM
                timeLeft = 0
                chargingApproach_n, chargingStop_n = None, None
                customerApproach, customerTransport= None, None
                chargingApproach_m, chargingStop_m = None, None 
                newApproach  = None
        else:
            feasible = energyFeasible
            timeLeft = 0
            chargingApproach_n, chargingStop_n = None, None
            customerApproach, customerTransport = None, None
            chargingApproach_m, chargingStop_m = None, None 
            newApproach  = None


        return (feasible, timeLeft, 
        chargingApproach_n, chargingStop_n, 
        customerApproach, customerTransport, 
        chargingApproach_m, chargingStop_m, 
        newApproach)


    def charge_and_check_time_feasibility_with_platooning(self, step, simulationObject, reservation, missingEnergy, 
                                                        firstEntry, approach, thirdEntry, pro, 
                                                        platoon_charging_duration):
        # schedule charging to realize energy feasibility 
        # Compute the charging station which is closest to the dropoff point
        net = simulationObject.road_network
        chargingStations = simulationObject.operations_area.charging_stations

        if firstEntry != None: 

            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy

        else: 
            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy


        chargingApproach_n, nearestCharging_n = self.create_charging_approach_n(step, firstEntry, approach, reservation, simulationObject)
       
        energyFeasible = False
         # determine where to charge after the customer transport
        (
        energyFeasible, 
        nearestCharging, 
        energyToCharge_m, 
        energyToCharge_n, 
        _
        ) = sumo_utilities.nearest_feasible_station(chargingApproach_n, 
                                                    reservation,initialEdge, 
                                                    endEnergy, 
                                                    approach, thirdEntry, 
                                                    simulationObject, pro = pro, platoon_charging_duration = platoon_charging_duration)


        if energyFeasible: 
            chargingStop_n = self.create_charging_stop_n(step, chargingApproach_n, reservation, energyToCharge_n, nearestCharging_n, simulationObject)


            customerApproach = ScheduleEntry(chargingStop_n.id + 1, reservation.id, "customerApproach", 
                    chargingStop_n.endPos, reservation.startPos, chargingStop_n.doEdge, reservation.puEdge, chargingStop_n.endT + 30, chargingStop_n.endEnergy, self)

            platoonApproach, platoonTransport, feasible = self.create_platoon_entries(customerApproach, reservation, pro)

            if feasible:
                # set the charging for the platoontransport for now
                platoonTransport.set_platoon_charging_time(platoon_charging_duration, pro)
                customerTransport = ScheduleEntry(platoonTransport.id + 1, reservation.id, "customerTransport", 
                                    platoonTransport.endPos, reservation.endPos, platoonTransport.doEdge, reservation.doEdge, 
                                    platoonTransport.endT + 45, platoonTransport.endEnergy, self)

                (energyFlagM, 
                chargingApproach_m, 
                chargingStop_m, 
                newApproach) = self.create_charging_stop_m(firstEntry, reservation, customerApproach, customerTransport, 
                                                           approach, thirdEntry, simulationObject, 
                                                           pro, platoonApproach, platoonTransport, platoon_charging_duration, 
                                                           charging_approach_n = chargingApproach_n)

                
                availableTime = self.compute_available_time(step, firstEntry, thirdEntry, simulationObject)

                completeTrip = [chargingApproach_n, chargingStop_n, 
                                customerApproach, customerTransport, 
                                chargingApproach_m, chargingStop_m, 
                                newApproach]

                if energyFlagM:
                    feasible, timeLeft = self.check_available_time(availableTime, completeTrip)
                else: 
                    feasible = energyFlagM
                    timeLeft = 0
            else: 
                feasible = False
                timeLeft = 0
                chargingApproach_n, chargingStop_n = None, None
                customerApproach, customerTransport = None, None
                platoonApproach, platoonTransport = None, None
                chargingApproach_m, chargingStop_m = None, None 
                newApproach  = None

        else: 
            feasible = False
            timeLeft = 0
            chargingApproach_n, chargingStop_n = None, None
            customerApproach, customerTransport = None, None
            platoonApproach, platoonTransport = None, None
            chargingApproach_m, chargingStop_m = None, None 
            newApproach  = None


        return (feasible, timeLeft, 
        chargingApproach_n, chargingStop_n, 
        customerApproach, 
        platoonApproach, platoonTransport,
        customerTransport, 
        chargingApproach_m, chargingStop_m, 
        newApproach)




    def finalize_entries_without_platooning(self, step, simulationObject, first, third, reservation, timeLeft, feasible, chargingApproach_n, chargingStop_n, 
                            customerApproach, customerTransport, chargingApproach_m, chargingStop_m, newApproach):

        # intervall defined by startIntervall and endIntervall is feasible, now concretize where to put each entry 

            startIntervall, endIntervall = self.define_Intervall(step,first, third, simulationObject)


            if reservation.twType == "PU":
                arrival, delta, feasible = self.finalize_entries_without_platooning_for_pu(step, simulationObject, first, third, reservation, timeLeft, chargingApproach_n, chargingStop_n, 
                            customerApproach, customerTransport, chargingApproach_m, chargingStop_m, newApproach, startIntervall, endIntervall)


            else:
                arrival, delta, feasible = self.finalize_entries_without_platooning_for_do(step, simulationObject, first, third, reservation, timeLeft, chargingApproach_n, chargingStop_n, 
                            customerApproach, customerTransport, chargingApproach_m, chargingStop_m, newApproach, startIntervall, endIntervall)


            return (delta, feasible,
            chargingApproach_n, chargingStop_n, 
            customerApproach, customerTransport, 
            chargingApproach_m, chargingStop_m, 
            newApproach)

    def finalize_entries_with_platooning(self, step, simulationObject, first, third, reservation,  chargingApproach_n, chargingStop_n, 
                                    customerApproach, platoonApproach, platoonTransport, customerTransport, chargingApproach_m, chargingStop_m, newApproach, pro):

        startIntervall, endIntervall = self.define_Intervall(step,first, third, simulationObject)

        if reservation.twType == "PU":
            arrival, delta, feasible = self.finalize_entries_with_platooning_for_pu(reservation, chargingApproach_n, chargingStop_n, 
                                                                                    customerApproach, platoonApproach, platoonTransport, customerTransport, 
                                                                                    chargingStop_m, newApproach, startIntervall, endIntervall)


        else:
            arrival, delta, feasible = self.finalize_entries_with_platooning_for_do(reservation, chargingApproach_n, chargingStop_n, 
                                                                                    customerApproach, platoonApproach, platoonTransport, customerTransport, 
                                                                                    chargingApproach_m, chargingStop_m, newApproach, startIntervall, endIntervall)

        return  (delta, feasible,
            chargingApproach_n, chargingStop_n,
            customerApproach, 
            platoonApproach, platoonTransport,
            customerTransport,
            chargingApproach_m, chargingStop_m,
            newApproach)



    def finalize_entries_without_platooning_for_pu(self, step, simulationObject, first, third, reservation, timeLeft, chargingApproach_n, chargingStop_n, 
                            customerApproach, customerTransport, chargingApproach_m, chargingStop_m, newApproach, startIntervall, endIntervall):

        arrival = customerApproach.endT
        delta = arrival - reservation.lb
        feasible = True
        if delta < 0: 

            newStartT = reservation.lb - customerApproach.duration 
            customerApproach.reset_startT(max(newStartT, startIntervall + 1))
            customerTransport.reset_startT(customerApproach.endT + ScheduleEntry.pickUpTime + 1)

        if chargingApproach_m  != None: 
            chargingApproach_m.reset_startT(customerTransport.endT + ScheduleEntry.dropOffTime + 1)
            chargingStop_m.reset_startT(chargingApproach_m.endT + 1)
            newApproach.reset_startT(chargingStop_m.endT + 30)

            timeViolation = newApproach.endT - endIntervall

            if timeViolation > 0: 
                newApproach.reset_endT(newApproach.endT - timeViolation - 1)
                chargingStop_m.reset_endT(newApproach.startT - 30)
                chargingApproach_m.reset_endT(chargingStop_m.startT - 1)
                customerTransport.reset_endT(chargingApproach_m.startT - ScheduleEntry.dropOffTime - 1)
                customerApproach.reset_endT(customerTransport.startT - ScheduleEntry.pickUpTime - 1)
                if chargingStop_n != None: 
                    if customerApproach.startT < chargingStop_n.endT + 30:
                        feasible = False
                else:
                    if customerApproach.startT < startIntervall + 1: 
                        feasible = False
            else: 
                newApproach.reset_startT(newApproach.startT - timeViolation - 1)
        else: 
            if customerTransport.endT > endIntervall: 
                feasible = False


        arrival = customerApproach.endT
        delta = utilities.delta(arrival, reservation.lb, reservation.ub)

        return arrival, delta, feasible


    def finalize_entries_with_platooning_for_pu(self, reservation, chargingApproach_n, chargingStop_n,
                                                customerApproach, platoonApproach, platoonTransport, customerTransport, 
                                                chargingStop_m, newApproach, startIntervall, endIntervall):

        arrival = customerApproach.endT
        delta = arrival - reservation.lb
        feasible = True

        if delta < 0: 

            newStartT = reservation.lb - customerApproach.duration 
            customerApproach.reset_startT(max(newStartT, startIntervall + 1))
            platoonApproach.reset_startT(customerApproach.endT + ScheduleEntry.pickUpTime + 1) 


        timeViolation = platoonApproach.endT - platoonTransport.startT

        if timeViolation > 0: 
            platoonApproach.reset_endT(platoonApproach.endT - timeViolation - 45)
            customerApproach.reset_endT(platoonApproach.startT - ScheduleEntry.pickUpTime - 1)

        else:
            newEndT = min(reservation.ub + platoonApproach.duration, platoonTransport.startT - 45)
            platoonApproach.reset_endT(newEndT)
            customerApproach.reset_endT(platoonApproach.startT - ScheduleEntry.pickUpTime - 1)
            
        if chargingStop_n != None: 
            if customerApproach.startT < chargingStop_n.endT + 30:
                feasible = False
        else:
            if customerApproach.startT < startIntervall + 1: 
                feasible = False

        if newApproach != None:
            timeViolation = newApproach.endT - endIntervall

            if timeViolation > 0: 
                newApproach.reset_endT(newApproach.endT - timeViolation - 1)
                if chargingStop_m != None:
                    if newApproach.startT < chargingStop_m.endT + 30:
                        feasible = False
                else:
                    if newApproach.startT < customerTransport.endT + customerTransport.dropOffTime:
                        feasible = False
            else:
                newApproach.reset_endT(newApproach.endT - timeViolation - 1)#
        else:
            if customerTransport.endT > endIntervall: 
                feasible = False
                    

        arrival = customerApproach.endT
        delta = utilities.delta(arrival, reservation.lb, reservation.ub)

        return arrival, delta, feasible



    def finalize_entries_without_platooning_for_do(self, step, simulationObject, first, third, reservation, timeLeft, chargingApproach_n, chargingStop_n, 
                            customerApproach, customerTransport, chargingApproach_m, chargingStop_m, newApproach, startIntervall, endIntervall):

        arrival = customerTransport.endT
        delta = arrival - reservation.lb
        feasible = True
        if delta < 0: 

            newStartT = reservation.lb - customerTransport.duration - customerApproach.duration 
            customerApproach.reset_startT(max(newStartT, startIntervall + 1))
            customerTransport.reset_startT(customerApproach.endT + ScheduleEntry.pickUpTime + 1)

        if chargingApproach_m  != None: 
            chargingApproach_m.reset_startT(customerTransport.endT + ScheduleEntry.dropOffTime + 1)
            chargingStop_m.reset_startT(chargingApproach_m.endT + 1)
            newApproach.reset_startT(chargingStop_m.endT + 30)

            timeViolation = newApproach.endT - endIntervall

            if timeViolation > 0: 
                newApproach.reset_endT(newApproach.endT - timeViolation - 1)
                chargingStop_m.reset_endT(newApproach.startT - 30)
                chargingApproach_m.reset_endT(chargingStop_m.startT - 1)
                customerTransport.reset_endT(chargingApproach_m.startT - ScheduleEntry.dropOffTime - 1)
                customerApproach.reset_endT(customerTransport.startT - ScheduleEntry.pickUpTime - 1)
                if chargingStop_n != None: 
                    if customerApproach.startT < chargingStop_n.endT + 30:
                        feasible = False
                else:
                    if customerApproach.startT < startIntervall + 1: 
                        feasible = False
            else: 
                newApproach.reset_startT(newApproach.startT - timeViolation - 1)
        else:
            if customerTransport.endT > endIntervall: 
                feasible = False
                    

        arrival = customerTransport.endT
        delta = utilities.delta(arrival, reservation.lb, reservation.ub)

        return arrival, delta, feasible

    def finalize_entries_with_platooning_for_do(self, reservation, chargingApproach_n, chargingStop_n, customerApproach, platoonApproach, platoonTransport,customerTransport, 
                                                chargingApproach_m, chargingStop_m,newApproach, startIntervall, endIntervall):
        feasible = True
        arrival = customerTransport.endT
        delta = arrival - reservation.lb

        if delta < 0: 
            newStartT = reservation.lb - customerTransport.duration
            customerTransport.reset_startT(max(newStartT, platoonTransport.endT + 20))
            if chargingApproach_m != None:
                chargingApproach_m.reset_startT(customerTransport.endT + ScheduleEntry.dropOffTime + 1)
                chargingStop_m.reset_startT(chargingApproach_m.endT + 1)
                newApproach.reset_startT(chargingStop_m.endT + 30)
        if newApproach != None:
            timeViolation = newApproach.endT - endIntervall

            if timeViolation > 0: 
                newApproach.reset_endT(newApproach.endT - timeViolation - 1)
            else:
                newApproach.reset_endT(newApproach.endT - timeViolation - 1)

            if newApproach.startT < chargingStop_m.endT + 30:
                feasible = False
        else:
            if customerTransport.endT > endIntervall: 
                feasible = False
                    

        timeViolation = platoonApproach.endT - platoonTransport.startT
        if timeViolation > 0: 
            platoonApproach.reset_endT(platoonApproach.endT - timeViolation - 45)
            customerApproach.reset_endT(platoonApproach.startT - ScheduleEntry.pickUpTime - 1)
  
        else:
            newEndT = min(reservation.ub + platoonApproach.duration, platoonTransport.startT - 45)
            platoonApproach.reset_endT(newEndT)
            customerApproach.reset_endT(platoonApproach.startT - ScheduleEntry.pickUpTime - 1)

        if chargingStop_n != None: 
            if customerApproach.startT < chargingStop_n.endT + 30:
                feasible = False
        else:
            if customerApproach.startT < startIntervall + 1: 
                feasible = False

        arrival = customerTransport.endT
        delta = utilities.delta(arrival, reservation.lb, reservation.ub)

        return arrival, delta, feasible
        
    def define_Intervall(self, step, first, third, simulationObject):

        if first == None and third == None: 
            startIntervall = step 
            endIntervall = simulationObject.simulation_end
        elif first == None and third != None: 
            startIntervall = step 

            if third.typeSchedule == "customerTransport" or third.typeSchedule == "platoonApproach":
                endIntervall = third.startT - ScheduleEntry.pickUpTime
            else:
                endIntervall = third.startT 


        elif first != None and third == None:
            if first.typeSchedule == "customerTransport":
                startIntervall = first.endT + ScheduleEntry.dropOffTime
            elif first.typeSchedule == "chargingStop":
                startIntervall = first.endT + 30
            else:
                startIntervall = first.endT 

            endIntervall = simulationObject.simulation_end
        else: 
            if first.typeSchedule == "customerTransport":
                startIntervall = first.endT + ScheduleEntry.dropOffTime
            elif first.typeSchedule == "chargingStop":
                startIntervall = first.endT + 30
            else:
                startIntervall = first.endT  

            if third.typeSchedule == "customerTransport" or third.typeSchedule == "platoonApproach":
                endIntervall = third.startT - ScheduleEntry.pickUpTime
            else:
                endIntervall = third.startT 
                
        return startIntervall, endIntervall

    def time_feasibility_platooning(self, lastEntry, endIntervall):
        if lastEntry.endT > endIntervall: 
            return False 
        else:
            return True

    def create_customer_approach(self, step, firstEntry, approach, reservation):
        if firstEntry != None:  
            # first entry is not none so reservation gets scheduled not as first
            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy
            if not (firstEntry.typeSchedule == "chargingStop" or firstEntry.typeSchedule == "customerTransport"):
                customerApproach = ScheduleEntry(len(self.schedule), reservation.id, "customerApproach", 
                    firstEntry.endPos, reservation.startPos, initialEdge, reservation.puEdge, firstEntry.endT + 1, firstEntry.endEnergy, vehicle = self)
            elif firstEntry.typeSchedule == "chargingStop":
                customerApproach = ScheduleEntry(len(self.schedule), reservation.id, "customerApproach", 
                    firstEntry.endPos, reservation.startPos, initialEdge, reservation.puEdge, firstEntry.endT + 30, firstEntry.endEnergy, vehicle = self)
            elif firstEntry.typeSchedule == "customerTransport":
                customerApproach = ScheduleEntry(len(self.schedule), reservation.id, "customerApproach", 
                    firstEntry.endPos, reservation.startPos, initialEdge, reservation.puEdge, firstEntry.endT + ScheduleEntry.dropOffTime + 1, firstEntry.endEnergy, vehicle = self)

        else: 
            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy

            if self.lastEntry != None and self.lastEntry.typeSchedule == "chargingStop":
                customerApproach = ScheduleEntry(len(self.schedule), reservation.id, "customerApproach", 
                    self.position, reservation.startPos, initialEdge, reservation.puEdge, step + 30, endEnergy, vehicle = self)
            elif self.lastEntry != None and self.lastEntry.typeSchedule == "customerTransport":
                customerApproach = ScheduleEntry(len(self.schedule), reservation.id, "customerApproach", 
                    self.position, reservation.startPos, initialEdge, reservation.puEdge, step + ScheduleEntry.dropOffTime + 1, endEnergy, vehicle = self)
            else:
                customerApproach = ScheduleEntry(len(self.schedule), reservation.id, "customerApproach", 
                    self.position, reservation.startPos, initialEdge, reservation.puEdge, step + 1, endEnergy, vehicle = self)

        return customerApproach


    def compute_available_time(self, step, firstEntry, thirdEntry, simulationObject):

        if firstEntry == None and thirdEntry != None:
            availableTime = thirdEntry.startT - step

        elif firstEntry != None and thirdEntry != None: 
            availableTime = thirdEntry.startT - firstEntry.endT

        elif firstEntry != None and thirdEntry != None: 
            availableTime = simulationObject.simulation_end - firstEntry.endT

        else: 
            availableTime = simulationObject.simulation_end - step

        return availableTime


    def create_charging_stop_m(self, firstEntry, reservation, customerApproach, customerTransport, approach, thirdEntry, 
                               simulationObject, pro = None, platoonApproach = None, platoonTransport = None, platoon_charging_duration = None, charging_approach_n = None):
        energyFlagM = True
        if firstEntry != None: 

            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy

        else: 
            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy



        if thirdEntry != None:
            simulationObject.total_cs = simulationObject.total_cs + 1
             # determine where to charge after the customer transport
            (
            energyFlagM, 
            nearestCharging, 
            energyToCharge_m, 
            energyToCharge_n,
            new_platoon_charging_duration
            ) = sumo_utilities.nearest_feasible_station(charging_approach_n , reservation, initialEdge, 
                                                        endEnergy, approach, thirdEntry, simulationObject, 
                                                        pro, platoonApproach, platoonTransport, platoon_charging_duration = platoon_charging_duration)
            
            if energyFlagM:
                if energyToCharge_m >= 0:
                    # adapt charging in platoon and energy levels of customer transport
                    if pro != None: 
                        platoonTransport.set_platoon_charging_time(new_platoon_charging_duration, pro)
                        customerTransport.update_start_energy(platoonTransport.endEnergy)

                    laneID = nearestCharging.lane.getID()
                    chargingEdge = laneID[:-2]
                    lane = simulationObject.road_network.getLane(laneID)

                    chargingPos = (nearestCharging.x, nearestCharging.y)
                    chargingApproach_m = ScheduleEntry(customerTransport.id + 1, reservation.id, "chargingApproach", 
                                customerTransport.endPos, chargingPos, customerTransport.doEdge, chargingEdge, customerTransport.endT + ScheduleEntry.dropOffTime + 1, 
                                customerTransport.endEnergy, vehicle = self, parkingID = nearestCharging.id)
                    chargingStop_m = ScheduleEntry(chargingApproach_m.id + 1, reservation.id, "chargingStop", 
                                chargingApproach_m.endPos, chargingPos, chargingApproach_m.doEdge, chargingEdge, chargingApproach_m.endT + 1, 
                                chargingApproach_m.endEnergy, vehicle = self, chargingStation = nearestCharging, parkingID = nearestCharging.id, endEnergy = chargingApproach_m.endEnergy + energyToCharge_m)

                    newApproach = ScheduleEntry(chargingStop_m.id + 1, approach.reservationID, "customerApproach", 
                                chargingStop_m.endPos, thirdEntry.startPos , chargingStop_m.doEdge, thirdEntry.puEdge, chargingStop_m.endT + 30, chargingStop_m.endEnergy, vehicle = self)
                else:
                    simulationObject.invalid_cs = simulationObject.invalid_cs + 1 
                    chargingApproach_m = None
                    chargingStop_m = None 
                    newApproach = None
                    energyFlagM = False
            else: 
                chargingApproach_m = None
                chargingStop_m = None 
                newApproach = None

        else: 
            if pro != None:
                platoonTransport.set_platoon_charging_time(platoon_charging_duration, pro)
                customerTransport.update_start_energy(platoonTransport.endEnergy)
            chargingApproach_m = None
            chargingStop_m = None 
            newApproach = None

        return energyFlagM, chargingApproach_m, chargingStop_m, newApproach


    def create_charging_approach_n(self, step, firstEntry, approach, reservation, simulationObject):

        net = simulationObject.road_network

        if firstEntry == None: 

            if approach != None:
                initialEdge = approach.puEdge
                endEnergy   = approach.startEnergy
            else: 
                if self.lastEntry != None:
                    initialEdge = self.lastEntry.doEdge
                    endEnergy   = self.lastEntry.endEnergy
                else:
                    initialEdge = self.edge
                    endEnergy   = self.energy

            position = self.position

            nearestCharging_n = sumo_utilities.nearest_station(initialEdge, simulationObject)
            laneID = nearestCharging_n.lane.getID()
            chargingEdge = laneID[:-2]
            lane = net.getLane(laneID)
            chargingPos = (nearestCharging_n.x, nearestCharging_n.y)

            if self.lastEntry != None and self.lastEntry.typeSchedule == "chargingStop":
                chargingApproach_n = ScheduleEntry(len(self.schedule), reservation.id, "chargingApproach", 
                        position, chargingPos, initialEdge, chargingEdge, step + 30, 
                        endEnergy, vehicle = self, parkingID = nearestCharging_n.id)  
            elif self.lastEntry != None and self.lastEntry.typeSchedule == "customerTransport":
                chargingApproach_n = ScheduleEntry(len(self.schedule), reservation.id, "chargingApproach", 
                        position, chargingPos, initialEdge, chargingEdge, step + ScheduleEntry.dropOffTime + 1, 
                        endEnergy, vehicle = self, parkingID = nearestCharging_n.id)  

            else:
                chargingApproach_n = ScheduleEntry(len(self.schedule), reservation.id, "chargingApproach", 
                        position, chargingPos, initialEdge, chargingEdge, step + 1, 
                        endEnergy, vehicle = self, parkingID = nearestCharging_n.id) 

        else: 
            initialEdge = firstEntry.doEdge
            endEnergy = firstEntry.endEnergy
            position = firstEntry.endPos
            nearestCharging_n = sumo_utilities.nearest_station(initialEdge, simulationObject)

            laneID = nearestCharging_n.lane.getID()
            chargingEdge = laneID[:-2]
            lane = net.getLane(laneID)
            chargingPos = (nearestCharging_n.x, nearestCharging_n.y)

            if firstEntry.typeSchedule == "chargingStop":
                chargingApproach_n = ScheduleEntry(len(self.schedule), reservation.id, "chargingApproach", 
                        firstEntry.endPos, chargingPos, firstEntry.doEdge, chargingEdge, firstEntry.endT + 30, 
                        firstEntry.endEnergy, vehicle = self, parkingID = nearestCharging_n.id)  
            elif firstEntry.typeSchedule == "customerTransport":
                chargingApproach_n = ScheduleEntry(len(self.schedule), reservation.id, "chargingApproach", 
                        firstEntry.endPos, chargingPos, firstEntry.doEdge, chargingEdge, firstEntry.endT + ScheduleEntry.dropOffTime + 1, 
                        firstEntry.endEnergy, vehicle = self, parkingID = nearestCharging_n.id)  
            else:
                chargingApproach_n = ScheduleEntry(len(self.schedule), reservation.id, "chargingApproach", 
                        firstEntry.endPos, chargingPos, firstEntry.doEdge, chargingEdge, firstEntry.endT + 1, 
                        firstEntry.endEnergy, vehicle = self, parkingID = nearestCharging_n.id)  



        return chargingApproach_n, nearestCharging_n



    def create_charging_stop_n(self, step, chargingApproach_n, reservation, energyToCharge_n, chargingStation, simulationObject):

        chargingStop_n = ScheduleEntry(len(self.schedule) + 1, reservation.id, "chargingStop", 
                chargingApproach_n.endPos, chargingApproach_n.endPos, chargingApproach_n.doEdge, chargingApproach_n.doEdge, chargingApproach_n.endT + 1, chargingApproach_n.endEnergy, 
                vehicle = self, chargingStation = chargingStation, parkingID = chargingApproach_n.parkingID, endEnergy = chargingApproach_n.endEnergy + energyToCharge_n)

        return chargingStop_n


    def check_available_time(self, availableTime, completeTrip):

        neededTime = sum([entry.duration for entry in completeTrip if entry != None])

        timeLeft = availableTime - neededTime

        if timeLeft >= ScheduleEntry.buffer:
            feasible = True
        else: 
            feasible = False

        return feasible, timeLeft

    def create_platoon_entries(self, customerApproach, reservation, pro):

        feasible = True 
        platoonStart = pro.chain_points[0]
        platoonEnd = pro.chain_points[1]

        platoonApproach = ScheduleEntry(customerApproach.id + 1, reservation.id, "platoonApproach", 
                            customerApproach.endPos, (platoonStart.x_start, platoonStart.y_start), customerApproach.doEdge, platoonStart.lane.getID()[:-2], 
                            customerApproach.endT + ScheduleEntry.pickUpTime + 1, customerApproach.endEnergy, vehicle = self, parkingID = platoonStart.id , platoonOffset = platoonStart.offset)

        choosenEntry = self.choose_platoon(customerApproach, platoonApproach, reservation, pro)

        if choosenEntry != None:
            platoonStartTime = choosenEntry.startT

            platoonTransport = ScheduleEntry(platoonApproach.id + 1, reservation.id, "platoonTransport", 
                                platoonApproach.endPos, (platoonEnd.x_start, platoonEnd.y_start), platoonApproach.doEdge, platoonEnd.lane.getID()[:-2], 
                                platoonStartTime, platoonApproach.endEnergy, vehicle = choosenEntry.vehicle, parkingID = choosenEntry.parkingID, platoonOffset = platoonEnd.offset, platoonEntryID = choosenEntry.id)
            platoonTransport.endT = choosenEntry.endT
            platoonTransport.duration = platoonTransport.endT - platoonTransport.startT

        else: 
            platoonApproach, platoonTransport, feasible = None, None, False


        return platoonApproach, platoonTransport, feasible


    def choose_platoon(self, customerApproach, platoonApproach, reservation, pro):

        platoonStart = pro.chain_points[0]
        platoonEnd = pro.chain_points[1]
        
        choosenEntry = None
        for scheduleEntry in pro.schedule:    
            if scheduleEntry.typeSchedule == "detachApproach": 
                if reservation.twType == "PU":   
                    if customerApproach.endT > reservation.ub:
                        break
                    else:
                        if (scheduleEntry.puEdge == platoonStart.lane.getID()[:-2] and 
                        (scheduleEntry.startT > (max(customerApproach.endT, reservation.lb) + sumo_utilities.travel_time([platoonApproach.puEdge, platoonApproach.doEdge], averageSpeed = self.cabSpeedModel(platoonApproach.distance)) + 45))):
                            choosenEntry = scheduleEntry
                            break
                else:

                    if scheduleEntry.puEdge == platoonStart.lane.getID()[:-2]: 
                        distance = sumo_utilities.route_length([platoonEnd.lane.getID()[:-2], reservation.doEdge])
                        if (scheduleEntry.endT + sumo_utilities.travel_time([platoonEnd.lane.getID()[:-2], reservation.doEdge], averageSpeed = self.cabSpeedModel(distance)) + 45 >= reservation.lb):
                            choosenEntry = scheduleEntry
                            break

        return choosenEntry

    def shift_schedule(self, delta, entry, type):

        previousEnd = entry.endT

        if type == "shiftStart":
            entry.startT = math.ceil(entry.startT + delta)
            entry.endT = math.ceil(entry.endT + delta)
            for index, updateEntry in enumerate(self.schedule):
                    if updateEntry.startT > previousEnd:
                        previousEntry = self.schedule[index - 1]

                        if updateEntry.startT <= previousEntry.endT:
                            delta = previousEntry.endT - updateEntry.startT
                            if previousEntry.typeSchedule == "chargingStop":
                                updateEntry.startT = math.ceil(updateEntry.startT + delta) + 20
                                updateEntry.endT = math.ceil(updateEntry.endT + delta) + 20
                            else:
                                updateEntry.startT = math.ceil(updateEntry.startT + delta) + 1
                                updateEntry.endT = math.ceil(updateEntry.endT + delta) + 1
                        elif updateEntry.startT <= previousEntry.endT + 20: 
                            if previousEntry.typeSchedule == "chargingStop":
                                updateEntry.startT = math.ceil(updateEntry.startT + delta) + 20
                                updateEntry.endT = math.ceil(updateEntry.endT + delta) + 20



        elif type == "shiftEnd":
            entry.endT = math.ceil(entry.endT + delta)
            for index, updateEntry in enumerate(self.schedule):
                    if updateEntry.startT > previousEnd:
                        previousEntry = self.schedule[index - 1]
                        if updateEntry.startT <= previousEntry.endT:
                            delta = previousEntry.endT - updateEntry.startT
                            if previousEntry.typeSchedule == "chargingStop":
                                updateEntry.startT = math.ceil(updateEntry.startT + delta) + 20
                                updateEntry.endT = math.ceil(updateEntry.endT + delta) + 20
                            else:
                                updateEntry.startT = math.ceil(updateEntry.startT + delta) + 1
                                updateEntry.endT = math.ceil(updateEntry.endT + delta) + 1
                        elif updateEntry.startT <= previousEntry.endT + 20: 
                            if previousEntry.typeSchedule == "chargingStop":
                                updateEntry.startT = math.ceil(updateEntry.startT + delta) + 20
                                updateEntry.endT = math.ceil(updateEntry.endT + delta) + 20


    def consolidate_schedule(self, step):
        toRemove = []
        length = len(self.schedule)
        if length >= 8:
            for index, entry in enumerate(self.schedule): 
                if index < length - 2:
                    firstEntry = entry 
                    secondEntry = self.schedule[index + 1]
                    if firstEntry.typeSchedule == "chargingStop" and secondEntry.typeSchedule == "chargingApproach": 
                        if firstEntry.parkingID == secondEntry.parkingID:
                            thirdEntry = self.schedule[index + 2]
                            toRemove = toRemove + [secondEntry, thirdEntry]
                            previousEnd = firstEntry.endT
                            firstEntry.reset_endEnergy(thirdEntry.endEnergy + secondEntry.energyConsumption)
                            firstEntry.reservationID.extend(thirdEntry.reservationID)
                            if self.state == "chargingStop" and self.edge == thirdEntry.puEdge and firstEntry.startT < step < firstEntry.endT:
                                timeToAdd = firstEntry.endT - previousEnd
                                self.remainingStop = self.remainingStop + timeToAdd
                                traci.vehicle.setParkingAreaStop(self.id, entry.parkingID, self.remainingStop)

        for entry in toRemove:
            self.schedule.remove(entry)



class PlatoonTaxi():
    """
    A Class to help with keeping track of taxis that have been removed from the sim, because they are in a platoon.
    """
    def __init__(self, cabID, position_in_platoon):
        self.cabID = cabID
        self.battery_level_before_trip = float(traci.vehicle.getParameter(self.cabID, "device.battery.chargeLevel"))
        self.passengers = traci.vehicle.getPersonIDList(self.cabID)
        self.position_in_platoon = position_in_platoon



   
class ProCab():
    """
    A class to help track and control pro-cabs in a SUMO sim.
    """
    def __init__(self, proCabID, state, position, maxSpeed, maxCabs, maxPowerSupply, energyConsumption, energy,max_capacity, maxToAverage = 2/3,):
        self.id = proCabID                                                                                      # the vehicle ID of the pro-cab functions as the platoon ID
        self.state = state                                                                                      # what the pro-cab is currently doing
        self.lastChange = 0                                                                                     # step of last state update
        self.position = position                                                                                # position of pro-cab (x, y)
        self.attachOffset = None                                                                                # offset in meters along edge where "attaching" takes place
        self.edge = None                                                                                        # current energy of pro-cab 
        self.energy = energy       
        self.energyConsumption = energyConsumption                         
        self.maxSpeed = maxSpeed 
        self.maxCabs = maxCabs
        self.maxPowerSupply = maxPowerSupply
        self.maxPowerSupplyPerCab = self.maxPowerSupply / self.maxCabs      
        self.averageSpeed = self.maxSpeed * maxToAverage                                             
        self.schedule = []                                                                                      # list of ScheduleEntries / what the pro plans on doing
        self.chain_routes = []
        self.lastEntry = []                                                                                     # last ScheduleEntry Completed
        self.reservedCabIDs = {}                                                                                # taxis that have reserved to attach to the pro-cab                                              
        self.platoonCabs: Dict[str, PlatoonTaxi] = {}                                                           # currently attached taxis
        self.passengerIDS = []                                                                                  # passengers in attached taxis 
        self.totalConvoyTrips = 0                                                                                 # number of convoy trips completed
        self.remainingStop = 1_000_000                                                                          # how long pro-cab should remain stopped
        self.maxBatterLevel = max_capacity        # maximum battery capacity of vehicle kwH
        self.battery_level_before_trip = 0 
        self.last_trip_energy = 0
        self.lastTripEnergy = 0
        self.batteryLevelBeforeTrip = 0 
        self.platoonTripLength = 0
        self.stop_time = 0

        
    def __str__(self):
        stop_state = traci.vehicle.getStopState(self.id)
        wait_time = 0
        if traci.vehicle.getStopState(self.id) > 0:
            wait_time = traci.vehicle.getWaitingTime(self.id)
        platoon_string = " ".join(map(str, self.platoonCabs.keys()))
        reserve_string = " ".join(map(str, self.reservedCabIDs))
        info = f"""PLATOON {self.id}:
        \tstate: {self.state}
        \tcabs: {platoon_string}
        \treserved cabs: {reserve_string}
        \twaiting time: {wait_time}
        \tstop state: {stop_state}"""
        return info

    def calculate_edge(self, net):

        self.edge = sumo_utilities.get_lane_and_offset(self.position, net, lat_lon = False)[0].getID()[:-2]

    def synchronize_state(self, simulationObject):
        """
        function which is called each step and syncronizes the state of the pro-cab object with the taxis in the sumo simulation.
        Copied from model.Taxi
        """
        net, edgeIDs = simulationObject.road_network, simulationObject.edge_ids
        self.edge = sumo_utilities.get_edge_of_car(self, edgeIDs, net)
        self.position = traci.vehicle.getPosition(self.id)
        self.energy = float(traci.vehicle.getParameter(self.id, "device.battery.actualBatteryCapacity"))

        self.currentParkingLocation = sumo_utilities.nearest_parking_station(self.position, simulationObject)

        if self.state == "attachingStop":
            self.check_for_cabs_at_station(self.currentParkingLocation.id, simulationObject)
        
    def update_state(self, step):
        """
        function which updates the state of the taxi based on state it is in and the event occuring
        """

        if traci.vehicle.getSpeed(self.id) == 0: 
            self.stop_time = self.stop_time + 1
        else: 
            self.stop_time = 0

        
        if self.state == "attachAproach":
            pass
        
        elif self.state == "detachApproach":
            pass
        
        # elif self.state == "attachingStop":
        #     if self.check_if_all_attached(platoonEntryID):
        #        self.state = 'idle' 
            
        # elif self.state == "detachingStop":
        #     self.state = 'idle'
        #    pass
        
        # elif self.state == 'idle':
        #     if platoonEntryID != 0:
        #         if not self.check_if_all_attached(platoonEntryID):
        #            self.state = "attachingStop"
            
        self.lastChange = step
        
    def shift_schedule(self, delta, entry, type):

        previousEnd = entry.endT

        if type == "shiftStart":

            entry.startT = math.ceil(entry.startT + delta)
            entry.endT = math.ceil(entry.endT + delta)
            for index, updateEntry in enumerate(self.schedule):
                    if updateEntry.startT > previousEnd:
                        previousEntry = self.schedule[index - 1]
                        if updateEntry.startT <= previousEntry.endT:
                            delta = previousEntry.endT - updateEntry.startT
                            updateEntry.startT = math.ceil(updateEntry.startT + delta) + 1
                            updateEntry.endT = math.ceil(updateEntry.endT + delta) + 1
      
        elif type == "shiftEnd":

            entry.endT = math.ceil(entry.endT + delta)
            for index, updateEntry in enumerate(self.schedule):
                    if updateEntry.startT > previousEnd:
                        previousEntry = self.schedule[index - 1]
                        if updateEntry.startT <= previousEntry.endT:
                            delta = previousEntry.endT - updateEntry.startT
                            updateEntry.startT = math.ceil(updateEntry.startT + delta) + 1
                            updateEntry.endT = math.ceil(updateEntry.endT + delta) + 1

    def start_platoon_trip(self):
        """
        Save some useful information for later.
        """ 
        self.totalConvoyTrips +=1
        self.batteryLevelBeforeTrip =  float(traci.vehicle.getParameter(self.id, "device.battery.chargeLevel")) # current battery capacity of vehicle kWh
        
        wholeRoute = list(traci.vehicle.getRoute(self.id))
        platoonRoute = []
        
        for edge in reversed(wholeRoute):
            platoonRoute.append(edge)
            if edge == self.schedule[0].puEdge:
                break
            
        platoonRoute.reverse()
        self.platoonTripLength = sumo_utilities.route_length(platoonRoute)

        
    def start_detach_stop(self): 
        self.state = "detachingStop"
        # self.remainingStop = 300 
                  
    
    def reserve_platoon_spot(self, cabID, platoonEntryID):
        """
        Protocol for how pro-cab should deal with passenger-cabs wanting to attach at some point.
        """
        if cabID not in self.reservedCabIDs[platoonEntryID]:
            self.reservedCabIDs[platoonEntryID].append(cabID)

    def detach_cabs(self, parkingID, step, detach_edge, platoon_duration, detach_pos = 0):
        """
        Detach all the cabs attached in the platoon.
        """
        # print(f"PLATOON {self.id}| [{traci.simulation.getTime()*2}]: removing cabs from platoon\n")
        for postion_in_platoon, cabID in enumerate(self.platoonCabs, step):
            self.detach_cab_from_platoon(cabID, parkingID, step, detach_edge, detach_pos, platoon_duration)
        self.platoonCabs = {}
    
    def detach_cab_from_platoon(self, cabID, parkingID, sim_time, detach_edge, detach_pos, platoon_duration):
        """
        Insert cab back into sim and make them stop. 
        """
        # Add dummy route for taxi to stop at detach edge
        route_id = f"detach_route_{cabID}_{sim_time}"
        traci.route.add(routeID=route_id, edges=[detach_edge])
        
        # Add the taxi back into simulation
        traci.vehicle.add(
            vehID=cabID,                  
            routeID=route_id,                    
            typeID="taxi", 
            departPos =  detach_pos      
        )
        # Insert a stop for the taxi 
        traci.vehicle.setParkingAreaStop(cabID, parkingID)
        platoon_duration_hours = platoon_duration / 3600
        battery_level_after_trip = self.platoonCabs[cabID].battery_level_before_trip + platoon_duration_hours * self.maxPowerSupplyPerCab
        # TODO use platoon_duration and pro.maximumPowerSupply to model charing in platoon
        traci.vehicle.setParameter(cabID, "device.battery.chargeLevel", battery_level_after_trip)
        
        # print(f"Cab \'{cabID}\' info after platoon trip\n\tBattery Level: {str(self.platoonCabs[cabID].battery_level)}\n\tPassengers:{', '.join(self.platoonCabs[cabID].passengers)}")
                  
                
    def check_if_all_attached(self, platoonEntryID):
        """
        Checks to see if all the reserved taxis are attached to the pro-cab/platoon. 
        """
        if self.reservedCabIDs[platoonEntryID]:
            if set(self.reservedCabIDs[platoonEntryID]) == set(self.platoonCabs.keys()):
                self.state = "idle" 
        else:
            self.state = "idle"

    def attach_cab_to_platoon(self, cabID):
        """
        Control the process of taxi joining platoon.
        """
        # print(f"PLATOON {self.id} | [{traci.simulation.getTime()*2}]: {cabID} attaching to platoon / removing from sim.\n")
        self.platoonCabs[cabID] = PlatoonTaxi(cabID, len(self.platoonCabs))
        traci.vehicle.remove(cabID)



    def check_for_cabs_at_station(self, currentParkingID, simulationObject):
        """
        Gets the vehicle ids of the reserved taxi that are stopped at the correct attach location. 
        If taxis are at stopped correctly to join the platoon, they are attached to the platoon / removed from the sim.
        """
        arrived_taxi_ids = traci.parkingarea.getVehicleIDs(currentParkingID)
        arrived_taxi_ids = [id for id in arrived_taxi_ids if id[0] == "t"]
        for id in arrived_taxi_ids: 
            if id in self.reservedCabIDs[self.schedule[0].id]:
                taxi = next((x for x in simulationObject.taxis if x.id == id), None)
                self.attach_cab_to_platoon(id)
        return arrived_taxi_ids  

        
         

    def generate_trip_info(self):  
        platoon_trip_info = {
            'pro-cabID': self.id,
            'tripNumber': self.totalConvoyTrips,
            'taxiIDS': "|".join(self.platoonCabs.keys()),
            'departure': self.schedule[0].startT,
            'arrivalTime': self.schedule[0].endT,
            'routelength': self.platoonTripLength,
            'energyConsumed': self.lastTripEnergy,
            'fromEdge': self.schedule[0].puEdge,
            'toEdge': self.schedule[0].doEdge}
        return platoon_trip_info

    
    def compute_trip_energy(self):
        """
        Use the energy consumed by procab that completed the platoon trip/route to calculate the total enrgy consumed for the platoon. 
        Also update the pro-cab's battery level, accordingly 
        """
        num_taxis = len(self.platoonCabs)
        aero = 0.85
        current_battery_level = float(traci.vehicle.getParameter(self.id, "device.battery.chargeLevel")) # current battery capacity of vehicle kWh
        # print(f"PLATOON {self.id} | [{traci.simulation.getTime()*2}]: battery level after trip {round(current_battery_level, 2)}\n")
        taxi_energy = self.batteryLevelBeforeTrip - current_battery_level # the assumed energy consumed by one taxi doing this trip
        self.lastTripEnergy = taxi_energy + (num_taxis*taxi_energy*aero)
        new_battery_level = self.batteryLevelBeforeTrip - self.lastTripEnergy
        traci.vehicle.setParameter(self.id, "device.battery.chargeLevel", str(new_battery_level))
        # print(f"PLATOON {self.id} | [{traci.simulation.getTime()*2}]: corrected battery level after trip {round(new_battery_level,2)} kWh\n")
