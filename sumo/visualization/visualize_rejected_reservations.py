import pandas as pd
import argparse
import seaborn as sns
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.optimize import minimize
import numpy as np
import os
import json
from pathlib import Path
import os
import sys
import random
from collections import defaultdict
sys.path.append(os.path.join(os.environ["SUMO_HOME"], 'tools'))
import sumolib  # noqa
import traci  # noqa
import random
import plotly.graph_objects as go


parser = argparse.ArgumentParser()
parser.add_argument("--instance", default=None)
parser.add_argument("--eventlog", default=None)
parser.add_argument("--config", default=None)
args = parser.parse_args()
rejected_reservations = []
data = []
file_path = args.eventlog
instance_file = args.instance
with open(file_path, 'r') as file:
    lines = file.readlines()
for line in lines:
    parts = line.strip().split()
    if parts[0] == "Reservation" and parts[2] == "rejected":
        rejected_reservations.append(parts[1])

with open(instance_file, "r") as f:
    data = json.load(f)

fig = go.Figure()

sumoBinary = sumolib.checkBinary("sumo-gui")
traci.start([sumoBinary, "-c", args.config])
t = 0 
end = 1000
while t <= end:
    if t == 0:
        # Formatting
        for reservation in data["simulationSteps"]:
            if reservation["requestParameter"]["UserGuid"] in rejected_reservations:
                (xPU, yPU) = traci.simulation.convertGeo(reservation["requestParameter"]["CurrentLocation"]["Longitude"], reservation["requestParameter"]["CurrentLocation"]["Latitude"], fromGeo=True)
                (xDO, yDO) = traci.simulation.convertGeo(reservation["requestParameter"]["TargetLocation"]["Longitude"], reservation["requestParameter"]["TargetLocation"]["Latitude"], fromGeo=True)
                random_rgb = (random.randint(0, 255),
                              random.randint(0, 255),
                              random.randint(0, 255))
                traci.poi.add(f"{reservation['requestParameter']['UserGuid']}_PU", xPU, yPU, random_rgb)
                traci.poi.add(f"{reservation['requestParameter']['UserGuid']}_DO", xDO, yDO, random_rgb)

                res_id = reservation["requestParameter"]["UserGuid"]

                # Register time (point)
                fig.add_trace(go.Scatter(
                    x=[reservation["SimulatedTime"]], y=[0],
                    mode="markers",
                    marker=dict(size=10, symbol="circle", color = f"rgb{random_rgb}"),
                    name=res_id + " register",
                    legendgroup=res_id,
                    showlegend=True
                ))

                # Pickup window (solid line)
                if reservation["requestParameter"]["pickupTime"] != None:
                    fig.add_trace(go.Scatter(
                        x=[reservation["requestParameter"]["pickupTime"]["StartTime"], reservation["requestParameter"]["pickupTime"]["EndTime"]], y=[0, 0],
                        mode="markers",
                        marker=dict(size=10, symbol="diamond", color = f"rgb{random_rgb}"),
                        name=res_id + " pickup",
                        legendgroup=res_id,
                        showlegend=True
                    ))

                else:
                    # Dropoff window (dashed line)
                    fig.add_trace(go.Scatter(
                        x=[reservation["requestParameter"]["targetTime"]["StartTime"], reservation["requestParameter"]["targetTime"]["EndTime"]], y=[0, 0],
                        mode="markers",
                        marker=dict(size=10, symbol="square", color = f"rgb{random_rgb}"),
                        name=res_id + " dropoff",
                        legendgroup=res_id,
                        showlegend=True
                    ))


        # Layout
        fig.update_layout(
            title="Rejected Reservations",
            xaxis=dict(title="Time"),
            yaxis=dict(visible=False),
            legend=dict(title="Reservations", itemsizing="constant"),
            hovermode="x unified"
        )


        fig.show()

    
    traci.simulationStep()
    t = traci.simulation.getTime()

traci.close()