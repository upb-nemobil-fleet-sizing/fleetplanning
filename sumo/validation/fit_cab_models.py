import pandas as pd
import argparse
import seaborn as sns
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.optimize import minimize
import numpy as np
import os
from scipy.optimize import curve_fit
import json

parser = argparse.ArgumentParser()
parser.add_argument('directory')  
args = parser.parse_args()

data = []
base_data_name = args.directory.split("/")[0].split("_")[1:]
base_data_name = "_".join(base_data_name)
for file_name in os.listdir(args.directory):
    file_path = os.path.join(args.directory, file_name)
    with open(file_path, 'r') as file:
        lines = file.readlines()
    for line in lines:
        parts = line.strip().split()
        if parts[0] == "taxi":
            vehicle = f"{parts[0]} {parts[1]}"
            start_or_end = parts[2]
            type_entry = parts[3]
            if type_entry == "chargingStop":
                if len(parts) == 25:
                    reservation = f"{parts[5]} {parts[6][1:-1]}"
                    step = int(parts[9])
                elif len(parts) == 26:
                    reservation = f"{parts[5]} {parts[7][1:-1]}"
                    step = int(parts[10])
            elif type_entry != "platoonTransport":
                reservation = f"{parts[5]} {parts[6]}"
                step = int(parts[9])
                time = int(parts[-8])
                distance = float(parts[-5])
                energy = float(parts[-1])


            data.append([vehicle, start_or_end, type_entry, reservation, step, distance, time, energy])


# Convert from List to DataFrame 
df = pd.DataFrame(data, columns=['vehicle', 'start_or_end', 'type_entry', 'id', 'step', 'distance', 'duration', 'energy'])
# restrict analysis to taxis
df = df[df["vehicle"].str.startswith("taxi")]
# choose completion of events as those contain the realised values in the event_log
df = df[df["start_or_end"] == "completes"]
# filter out platoon transport and charging stop
chargingApproaches = df[(df["type_entry"] == "chargingApproach")] 
df = df[(df["type_entry"] != "chargingApproach") & (df["type_entry"] != "chargingStop") & (df["type_entry"] != "platoonTransport")]
# filter out extreme low distance values as they skew the average speed
df = df[df["distance"] > 1]
# compute average speed
df["avg_real_speed"] = df["distance"] / df["duration"]

# Hill / Michaelis–Menten with exponent:
# y(x) = d + A * x^n / (K^n + x^n)
def hill(x, A, K, n, d, threshold1, threshold2, constant_avg_speed, constant_avg_speed2):
    y = np.empty_like(x)
    # Case: below threshold
    mask = x < threshold1
    mask2 = (x < threshold2) & (threshold1 <= x)
    mask3 = x >= threshold2
    y[mask] = constant_avg_speed
    y[mask2] = constant_avg_speed2
    y[mask3] = d + A * (x[mask3]**n) / (K**n + x[mask3]**n)

    return y

def linear_model(x, a, b):
    return a * x + b

# One-sided, x-weighted objective
def objective(theta, X, Y, threshold1, threshold2, constant_avg_speed, constant_avg_speed2):
    A, K, n, d = theta
    yhat = hill(X, A, K, n, d, threshold1, threshold2, constant_avg_speed, constant_avg_speed2)

    # forbid overshooting
    viol = yhat - Y
    overshoot_penalty = np.sum((viol[viol > 0])**2) * 1e10

    # emphasize large distances
    w = (X - X.min()) / (X.max() - X.min() + 1e-9)
    w = 0.2 + 0.8 * w

    return np.sum((Y - yhat)**2) + overshoot_penalty


def fit_avg_speed_model(df, axes):
    # Extract columns from DataFrame
    X = df["distance"].to_numpy(dtype=float)
    Y = df["avg_real_speed"].to_numpy(dtype=float)
    # Initial guess
    upper = np.percentile(Y, 90)
    lower = np.percentile(Y, 5)
    A0 = max(upper - lower, 1e-3)
    K0 = np.percentile(X[X > 0], 40) if np.any(X > 0) else 1.0
    n0 = 1.0
    d0 = lower - 0.05
    theta0 = np.array([A0, K0, n0, d0], dtype=float)

    # Bounds
    bounds = [(1e-9, None), (1e-9, None), (1e-3, 8.0), (None, None)]
    threshold1 = 0
    threshold2 = 0
    constant_avg_speed = 4
    constant_avg_speed2 = 6
    # Fit
    res = minimize(objective, theta0, args = (X,Y, threshold1, threshold2, constant_avg_speed, constant_avg_speed2), method="L-BFGS-B", bounds=bounds)
    theta_opt = res.x
    params = {}
    params["A"] = theta_opt[0]
    params["K"] = theta_opt[1]
    params["n"] = theta_opt[2]
    params["d"] = theta_opt[3]
    params["threshold1"] = threshold1
    params["threshold2"] = threshold2
    params["constant_avg_speed"]  = constant_avg_speed
    params["constant_avg_speed2"] = constant_avg_speed2


    
    # with open("hill_model_params_new.json", "w") as file:
    #      json.dump(params, file, indent=4)

    with open("hill_model_params_new.json", "r") as f:
         params_new = json.load(f)

    A_new = params_new["A"]
    K_new = params_new["K"]
    n_new = params_new["n"]
    d_new = params_new["d"]
    threshold1 = params_new["threshold1"]
    threshold2 = params_new["threshold2"]
    constant_avg_speed  = params_new["constant_avg_speed"]
    constant_avg_speed2 = params_new["constant_avg_speed2"]

    # Predictions for plotting
    xgrid = np.linspace(X.min(), X.max(), 500)
    ygrid_new = hill(xgrid, A_new, K_new, n_new, d_new, threshold1, threshold2, constant_avg_speed, constant_avg_speed2)
    # ygrid_new = hill(xgrid, A_new, K_new, n_new, d_new, threshold1, threshold2, constant_avg_speed, constant_avg_speed2)

    # Check violations
    violations = np.sum(hill(X, A_new, K_new, n_new, d_new, threshold1, threshold2, constant_avg_speed, constant_avg_speed2) > Y)
    print(violations)

    # Plot
    axes[0].scatter(X, Y, s=12, alpha=0.8)
    # axes[0].plot(xgrid, ygrid_new, linewidth=2, color='green')
    axes[0].plot(xgrid, ygrid_new, linewidth=2, color='red')
    axes[0].set_xlabel("distance")
    axes[0].set_ylabel("avg_real_speed")
    axes[0].set_title("Lower-envelope Hill fit (one-sided, x-weighted)")

def fit_avg_speed_model_linear(df, axes):
    X = df["distance"].to_numpy(dtype=float)
    Y = df["avg_real_speed"].to_numpy(dtype=float)
    params, covariance = curve_fit(linear_model, X, Y)
    # compare against approximated fit
    a_fit, b_fit = 1/5000, 2.333333
    xgrid = np.linspace(X.min(), X.max(), 500)
    ygrid = linear_model(xgrid, a_fit, b_fit)
    # Plot
    axes[0].scatter(X, Y, s=12, alpha=0.8)
    axes[0].plot(xgrid, ygrid, linewidth=2, color='red')
    axes[0].set_xlabel("distance")
    axes[0].set_ylabel("avg_real_speed")
    axes[0].set_title("Lower-envelope Hill fit (one-sided, x-weighted)")

def fit_energy_consumption_model(df, axes):
    X = df["distance"].to_numpy(dtype=float)
    Y = df["energy"].to_numpy(dtype=float)

    params, covariance = curve_fit(linear_model, X, Y)
    # compare against approximated fit
    a_fit, b_fit = 0.1, 0
    xgrid = np.linspace(X.min(), X.max(), 500)
    ygrid = linear_model(xgrid, a_fit, b_fit)
    axes[1].scatter(X, Y, s=12, alpha=0.8)
    axes[1].plot(xgrid, ygrid, linewidth=2, color='red')
    axes[1].set_xlabel("distance")
    axes[1].set_ylabel("energy consumption (Wh)")
    axes[1].set_title("distance (in m)")


# create subplot object
fig, axes = plt.subplots(1, 2, figsize=(10, 4))
fit_energy_consumption_model(df, axes)
fit_avg_speed_model(df, axes)
plt.savefig(f"real_vs_approx_{base_data_name}.png")