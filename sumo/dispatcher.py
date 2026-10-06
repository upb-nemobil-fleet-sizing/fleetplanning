from __future__ import absolute_import
from __future__ import print_function
import os
import sys
import optparse
from . import models_sumo
from . import sumo_utilities
import bisect
import math
import numpy as np
import timeit



# we need to import python modules from the $SUMO_HOME/tools directory
if 'SUMO_HOME' in os.environ:
    tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
    sys.path.append(tools)
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")

from sumolib import checkBinary  # noqa
import traci  # noqa
import sumolib  # noqa


# assign idle taxis to reservations and computes idle vehicle management
def policy(step, registerReservation, simulationObject): 

    simulationObject.reservation_history.extend(registerReservation)
    # Assign Vehicles 
    start = timeit.default_timer()
    cab_schedule_management(step, registerReservation,simulationObject)
    stop = timeit.default_timer()
    simulationObject.run_time_assignment = simulationObject.run_time_assignment + (stop - start) 

    # Communicate Schedule entries through the TraCI API
    start = timeit.default_timer()
    schedule_communication(step,simulationObject)
    stop = timeit.default_timer()
    simulationObject.run_time_traci = simulationObject.run_time_traci + (stop - start) 

    if step == simulationObject.simulation_end - 1:

            print("\n")
            # for taxi in simulationObject.taxis: 
            #     print(f"NUMBER OF RESERVATIONS SERVED BY {taxi.id}: {len(taxi.allDeltas)}")
            #     if len(taxi.allDeltas) > 0:
            #         print(f"AVERAGE VIOLATION OF TW OF {taxi.id}: ", round(sum(taxi.allDeltas)/len(taxi.allDeltas)))
            #         print(f"MAXIMUM VIOLATION OF {taxi.id}: {round(max(taxi.allDeltas))}")
            #         print(f"MINIMUM VIOLATION OF {taxi.id}: {round(min(taxi.allDeltas))}")
            #         print(f"NUMBER OF NO VIOLATIONS OF {taxi.id}: {taxi.allDeltas.count(0)}")
            #     print("") 

            reservationServiced = []
            reservationsNotServiced = []
            for reservation in simulationObject.reservation_history: 
                if reservation.actualDoTime == None: 
                    reservationsNotServiced.append(reservation)
                else:
                    reservationServiced.append(reservation)

            print("RESERVATIONS NOT SERVICED: ", len(reservationsNotServiced))
            print([reservation.id for reservation in reservationsNotServiced])
            totalPlatoonTrips = sum([simulationObject.num_platoon_trips[chain_route.id] for chain_route in simulationObject.operations_area.pro_routes_and_trips.chain_routes])
            simulationObject.total_num_platoons = totalPlatoonTrips
            simulationObject.platoon_share = round(totalPlatoonTrips / len(reservationServiced), 2)
            print("Total number of CS created in the Candidate Generation: ", simulationObject.total_cs)
            print("Invalid Charging Processes disgarded in the Candidate Generation: ", simulationObject.invalid_cs)


def cost_function(candidate, simulationObject, alpha = 0):
    return candidate.total_time

def cab_schedule_management(step, registerReservation,simulationObject):
    """
    function which inserts reservation which were registered in the current time step into the schedules 
    of the taxis

    """
    for reservation in registerReservation:
        if simulationObject.verbose:
            print(f"Reservation {reservation.id} registered with the System\n")
            print(f"specified time window is {reservation.twType} with [{reservation.lb}, {reservation.ub}]")
            print("Number of Passengers :", reservation.numPersons)
            print("Coordinates of Pickup: ", reservation.startPos)
            print("Coordinates of Dropoff: ", reservation.endPos, "\n")
        with open(simulationObject.event_based_log_file, "a") as f:
            f.write(f"Reservation {reservation.id} registered with the System at step {step}\n")

        all_candidates_without_platooning = []
        all_candidates_with_platooning = []
        for taxi in simulationObject.taxis:
            taxi.synchronize_state(simulationObject.road_network, simulationObject.edge_ids)
            candidates_without_platooning, candidates_with_platooning, tw_feasible = taxi.return_all_candidates(step, reservation, simulationObject)

            if not tw_feasible:
                with open(simulationObject.event_based_log_file, "a") as f:
                    f.write(f"Reservation {reservation.id} rejected due to TW being outside of service times\n")
                    reservation.customReject = True
                    break
            else:
                if candidates_without_platooning:
                    all_candidates_without_platooning.extend(candidates_without_platooning)
                if candidates_with_platooning:
                    all_candidates_with_platooning.extend(candidates_with_platooning)
            
                
        if all_candidates_without_platooning:
            all_candidates_without_platooning.sort(key = lambda candidate: cost_function(candidate, simulationObject))
            bestCabCandidate = all_candidates_without_platooning[0]
        if all_candidates_with_platooning:
            all_candidates_with_platooning.sort(key = lambda candidate: cost_function(candidate, simulationObject))
            bestPlatoonCandiate = all_candidates_with_platooning[0]
        if (not all_candidates_without_platooning) and (not all_candidates_with_platooning): 
            reservation.customReject = True
            with open(simulationObject.event_based_log_file, "a") as f:
                f.write(f"Reservation {reservation.id} rejected\n")
        else:
            if not all_candidates_with_platooning:
                bestGlobalCandidate = bestCabCandidate
            elif not all_candidates_without_platooning:
                bestGlobalCandidate = bestPlatoonCandiate
            else:
                if cost_function(bestCabCandidate, simulationObject) < cost_function(bestPlatoonCandiate, simulationObject):
                    bestGlobalCandidate = bestCabCandidate
                else:
                    bestGlobalCandidate = bestPlatoonCandiate

            reservation.vehicle = bestGlobalCandidate.taxi

            if all_candidates_without_platooning:
                if bestGlobalCandidate != bestCabCandidate:
                    detour_energy = bestGlobalCandidate.total_energy - bestCabCandidate.total_energy
                    detour_distance = bestGlobalCandidate.total_distance - bestCabCandidate.total_distance
                    detour_duration = bestGlobalCandidate.total_time - bestCabCandidate.total_time

                    simulationObject.saved_energies.append(detour_energy)
                    simulationObject.relative_saved_energies.append(detour_energy/bestCabCandidate.total_energy)
                    simulationObject.detour_distances.append(detour_distance)
                    simulationObject.detour_durations.append(detour_duration)
            else:
                pass
                # detour_energy = bestGlobalCandidate.total_energy
                # detour_distance = bestGlobalCandidate.total_distance 
                # detour_duration = bestGlobalCandidate.total_time 

                # simulationObject.saved_energies.append(detour_energy)
                # simulationObject.relative_saved_energies.append(detour_energy/bestCabCandidate.total_energy)
                # simulationObject.detour_distances.append(detour_distance)
                # simulationObject.detour_durations.append(detour_duration)

            bestTaxi = bestGlobalCandidate.taxi

            if bestGlobalCandidate.old_approach != None: 
                bestTaxi.schedule.remove(bestGlobalCandidate.old_approach)

            bestTaxi.num_requests = bestTaxi.num_requests + 1
            bestTaxi.empty = False
            if bestGlobalCandidate.pro != None: 
                bestPro = bestGlobalCandidate.pro

            for entry in bestGlobalCandidate.entries:
                entry.set_offset(simulationObject)
                bisect.insort(bestTaxi.schedule, entry, key = lambda x: x.startT)
                if entry.typeSchedule == "customerTransport": 
                    reservation.promisedPuTime = entry.startT
                    reservation.promisedDoTime = entry.endT
                elif entry.typeSchedule == "platoonTransport":
                    platoonTransport = entry
                    chainRouteID = entry.platoonEntryID.split("_")[0] + "_" + entry.platoonEntryID.split("_")[1]
                    simulationObject.num_platoon_trips[chainRouteID] =  simulationObject.num_platoon_trips[chainRouteID] + 1
                    # entry.duration = entry.duration + bestGlobalCandidate.return_candidate_waiting_time()
                    # bestTaxi.platooningDistance = bestTaxi.platooningDistance + entry.distance -> now using realised distance
                    bestPro.reserve_platoon_spot(bestTaxi.id, entry.platoonEntryID)
                    # entry.set_platoon_energy(bestPro)
                    simulationObject.platoon_reservations.append(entry.reservationID)
                    simulationObject.pro_prebooking_times.append(entry.startT - step)
                    platoonApproach.platoonEntryID = platoonTransport.platoonEntryID
                elif entry.typeSchedule == "platoonApproach":
                    platoonApproach = entry

            if bestGlobalCandidate.deleted_cs: 
                simulationObject.deleted_cs.append(bestGlobalCandidate.reservationID)

            bestTaxi.consolidate_schedule(step)

            with open(simulationObject.event_based_log_file, "a") as f:
                f.write(f"Reservation {reservation.id} assigned to {bestTaxi.id}\n")

            if simulationObject.verbose:
                print(f"Reservation {reservation.id} assigned to {bestTaxi.id} \n \n")
                for taxi in simulationObject.taxis: 
                    print(f"Schedule of {taxi.id} after insertion\n")
                    toConsole = ""
                    for entry in taxi.schedule: 
                        if entry.typeSchedule == "customerApproach":
                            toConsole = toConsole + f"cuA[{entry.reservationID}]({entry.startT}, {entry.endT})({round(entry.startEnergy, 2)}, {round(entry.endEnergy, 2)}) "

                        elif entry.typeSchedule == "customerTransport":
                            toConsole = toConsole + f"cT[{entry.reservationID}]({entry.startT}, {entry.endT})({round(entry.startEnergy, 2)}, {round(entry.endEnergy, 2)}) "

                        elif entry.typeSchedule == "chargingApproach":
                            toConsole = toConsole + f"chA[{entry.parkingID}, {entry.reservationID}]({entry.startT}, {entry.endT})({round(entry.startEnergy, 2)}, {round(entry.endEnergy, 2)}) "
                        elif entry.typeSchedule == "chargingStop":
                            toConsole = toConsole + f"cS[{entry.parkingID}, {entry.reservationID}]({entry.startT}, {entry.endT})({round(entry.startEnergy, 2)}, {round(entry.endEnergy, 2)}) "
                        elif entry.typeSchedule == "platoonApproach":
                            toConsole = toConsole + f"pA[{entry.reservationID}]({entry.startT}, {entry.endT})({round(entry.startEnergy, 2)}, {round(entry.endEnergy, 2)}) "
                        elif entry.typeSchedule == "platoonTransport":
                            toConsole = toConsole + f"pT[{entry.reservationID}, {entry.platoonEntryID}]({entry.startT}, {entry.endT})({round(entry.startEnergy, 2)}, {round(entry.endEnergy, 2)}) "
                    print(toConsole, "\n")

def schedule_communication(step, simulationObject):
    """
    function which iterates through the schedules of the taxis and dispatches the taxis as needed
    through the TraCI API
    """

    for taxi in simulationObject.taxis:
        taxi.update_state(step, simulationObject.road_network)
        for index, entry in enumerate(taxi.schedule):
            # check if entry is scheduled for this step

            if entry.startT == step: 
                taxi.synchronize_state(simulationObject.road_network, simulationObject.edge_ids)
    
                # check snapshot of system
                if taxi.state != "notInSim" and traci.vehicle.isStopped(taxi.id) and (taxi.edge == entry.puEdge or math.dist(taxi.position, entry.startPos) < 25):                    
                    if taxi.state == "idle":
                        entry.communicate_entry_start(taxi, simulationObject)
                        entry.log_entry_start(taxi, simulationObject)
                    else:
                        # car is  where it is approximated to be but is still stopped due to pu/do/charging
                        # update every subsequent entry
                        neededTime = taxi.remainingStop
                        taxi.shift_schedule(neededTime, entry, "shiftStart")
                        with open(simulationObject.event_based_log_file, "a") as f:
                            f.write(f"shift by start {taxi.id} in {step} for {entry.typeSchedule}\n")

                

            elif entry.endT == step:
                # simulationObject.distance_errors.append((entry.reservationID, entry.typeSchedule, realised_distance, entry.distance, abs(realised_distance - entry.distance)))

                if entry.typeSchedule == "decouplingCab":
                    entry.communicate_entry_end(index, step, taxi, simulationObject)
                else:
                    taxi.synchronize_state(simulationObject.road_network, simulationObject.edge_ids)
                    if taxi.state != "notInSim" and traci.vehicle.isStopped(taxi.id) and (taxi.edge == entry.doEdge or math.dist(taxi.position, entry.endPos) < 25):
                        entry.communicate_entry_end(index, step, taxi, simulationObject)
                        entry.log_entry_end(taxi, simulationObject)
                    else:
                        neededTime = sumo_utilities.travel_time([taxi.edge, entry.doEdge])
                        # update every subsequent entry
                        if entry.typeSchedule != "platoonTransport":
                            taxi.shift_schedule(neededTime, entry, "shiftEnd")
                            with open(simulationObject.event_based_log_file, "a") as f:
                                f.write(f"shift by end for {taxi.id} in {step} for {entry.typeSchedule}\n")

    for pro in simulationObject.pros:
        pro.update_state(step)
        for index, entry in enumerate(pro.schedule):
            # check if schedule entry should start this step
            if entry.startT == step:
                pro.synchronize_state(simulationObject)
                if pro.state == "attachingStop":
                    pro.check_if_all_attached(entry.id)
                # is the pro at the edge for entry to start and is the vehicle stopped
                if pro.edge == entry.puEdge and traci.vehicle.isStopped(pro.id) or math.dist(pro.position, entry.startPos):              
                    # is the pro idle
                    if pro.state == "idle":
                        entry.communicate_entry_start(pro, simulationObject)
                        entry.log_entry_start(pro, simulationObject)
                    else:
                        pass
                        # TODO 
                        # pro-cab is where it is approximated to be but is not idle
                        neededTime = 100 # in steps
                        # update every subsequent entr
                        pro.shift_schedule(neededTime, entry, "shiftStart") 
                        with open(simulationObject.event_based_log_file, "a") as f:
                            f.write(f"shift by start {pro.id} in {step} for {entry.typeSchedule}\n")

            elif entry.endT == step:
                pro.synchronize_state(simulationObject)
                if pro.edge == entry.doEdge and traci.vehicle.isStopped(pro.id) or math.dist(pro.position, entry.endPos):
                    entry.communicate_entry_end(index, step, pro, simulationObject)
                    entry.log_entry_end(pro, simulationObject)
                else:
                    # update the entries of the pro-cab if there has been a delay to get to doEdge
                    neededTime = sumo_utilities.travel_time([pro.edge, entry.doEdge])
                    # update every subsequent entry
                    pro.shift_schedule(neededTime, entry, "shiftEnd")
                    with open(simulationObject.event_based_log_file, "a") as f:
                        f.write(f"shift by end for {pro.id} in {step} for {entry.typeSchedule}\n")


