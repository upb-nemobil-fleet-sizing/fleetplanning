from __future__ import absolute_import
from __future__ import print_function
import os
import sys
from datetime import datetime, timezone, timedelta
import json

def relu(x): 
    """
    function which returns the identity for positive x, else returns 0
    """
    if x < 0: 
        return 0 
    else: 
        return x 

def delta(arrival, lowerBound, upperBound):

    if arrival < lowerBound: 
        return lowerBound - arrival
    elif arrival > upperBound:
        return arrival - upperBound
    else: 
        return 0

def convert_to_simulation_time(time, today, offset, typeOfFile):
    """
    takes a time as string: yyyy-dd-mn hh:mm:ss and what day (today) gets simulated, the starting time of the simulation
    gets passed an offset
    """
    if typeOfFile == "xml":
        day = time[:11]
        hours = int(time[11:13])
        minutes = int(time[14:16])
        rampUp = 30
        if day != today: 
            simulationTime = 0 + rampUp
        elif hours < offset:
            simulationTime = 0 + rampUp
        else:
            simulationTime = (hours - offset) * 60 * 60 + minutes * 60 + rampUp
        
        return simulationTime
    elif typeOfFile == "json":
        day = time[:10]
        hours = int(time[11:13])
        minutes = int(time[14:16])
        rampUp = 30
        if day != today: 
            simulationTime = 0 + rampUp
        elif hours < offset:
            simulationTime = 0 + rampUp
        else:
            simulationTime = (hours - offset) * 60 * 60 + minutes * 60 + rampUp
        
        return int(simulationTime)


def convert_datetime_to_simulation_time(time:datetime, today:datetime, serviceStart:datetime, ramp_up = 0, cut_at_ramp_up = True, simulation_end = None) -> int:
    
    if (time.day < today.day or time.month < today.month) or (time.day == today.day and cut_at_ramp_up and time.hour < serviceStart.hour):
        return ramp_up
    elif time.day > today.day:
        return simulation_end
    else:
        startInSeconds = serviceStart.hour * 3600 + serviceStart.minute * 60 + serviceStart.second 
        timeInSeconds = time.hour * 3600 + time.minute * 60 + time.second

    if simulation_end != None:
        return min(simulation_end, timeInSeconds - startInSeconds + ramp_up)
    else:
        return timeInSeconds - startInSeconds + ramp_up



def convert_simulation_time_to_datetime(
    simulation_time: int,
    service_start: datetime,
    ramp_up: int = 0, 
    return_datetime = False,
) -> str:
    # If simulation time is within ramp-up, clamp to service_start
    if simulation_time <= ramp_up:
        result = service_start
    else:
        seconds_after_start = int(simulation_time - ramp_up)
        result = service_start + timedelta(seconds=seconds_after_start)
    if return_datetime:
        return result
    else:
        return result.strftime("%Y-%m-%d %H:%M:%S")

def load_json_file(path):
        with open(path, "r") as file:
            return json.load(file)

