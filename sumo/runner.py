#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Eclipse SUMO, Simulation of Urban MObility; see https://eclipse.dev/sumo
# Copyright (C) 2008-2023 German Aerospace Center (DLR) and others.
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# https://www.eclipse.org/legal/epl-2.0/
# This Source Code may also be made available under the following Secondary
# Licenses when the conditions for such availability set forth in the Eclipse
# Public License 2.0 are satisfied: GNU General Public License, version 2
# or later which is available at
# https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html
# SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later

# @file    runner.py
# @author  Michael Behrisch
# @author  Daniel Krajzewicz
# @author  Melanie Weber
# @author  Lara Kaiser
# @date    2021-03-29


from __future__ import absolute_import
from __future__ import print_function
import os
import sys
from . import dispatcher
from . import sumo_utilities
from . import sumo_simulation
from sumolib import checkBinary  # noqaa
import traci
import timeit


# we need to import python modules from the $SUMO_HOME/tools directory
if 'SUMO_HOME' in os.environ:
    tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
    sys.path.append(tools)
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")
                    
def run(simulationObject):
    """execute the TraCI control loop"""
    step = 0
    sumoBinary = checkBinary('sumo')
    traci.start([sumoBinary, "-c", simulationObject.NETWORK_DATA / "sumo.sumocfg", "--no-step-log", "--no-warnings", "--default.speeddev", "0",
        "--tripinfo-output", f"{simulationObject.OUTPUT_DIR}/tripinfos/tripinfo_{simulationObject.run_name}_iter_{simulationObject.log_id}.xml", "--tripinfo-output.write-unfinished", "True", "--lateral-resolution", "0.1"])
    if simulationObject.disable_tls:
        trafficLights = simulationObject.road_network.getTrafficLights()#
        # remove all traffic lights
        for tls in trafficLights: 
            traci.trafficlight.setProgram(tls.getID(), "off")
    if not simulationObject.only_cabs:
        simulationObject.parse_chain_route_trips()
        sumo_utilities.create_pros(simulationObject.pros)
    
    num_taxis = len(simulationObject.taxis)
    sumo_utilities.create_taxis(num_taxis, simulationObject.initial_station)

    # the simulation needs a ramp up time to insert the cars
    while step < simulationObject.simulation_end:
        if step >= simulationObject.ramp_up_time:
            # Reservation to register with the dispatcher
            registerReservation = []
            for reservation in simulationObject.reservations: 
                if reservation.registerTime == step: 
                    registerReservation.append(reservation)
                if step > reservation.registerTime: 
                    break
            for reservation in registerReservation:
                simulationObject.reservations.remove(reservation)
            # Assignment Policy is executed in every step
            dispatcher.policy(step, registerReservation, simulationObject)
        traci.simulationStep()
        step += 1
    traci.close()
    sys.stdout.flush()

def get_options():
    optParser = optparse.OptionParser()
    optParser.add_option("--nogui", action="store_true",
                         default=False, help="run the commandline version of sumo")
    options, args = optParser.parse_args()
    return options
