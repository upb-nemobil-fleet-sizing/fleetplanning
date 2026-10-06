import pandas as pd
import argparse
import os

parser = argparse.ArgumentParser()
parser.add_argument('directory')  
args = parser.parse_args()
base_data = args.directory.split("/")[1]
instances_with_shift = []

for file_name in os.listdir(args.directory):
	if file_name.startswith("event"):
		file_path = os.path.join(args.directory, file_name)
		with open(file_path, 'r') as file:
		    lines = file.readlines()

		for line in lines:
		    parts = line.strip().split()

		    if parts[0] == "shift":#
		    	if file_name not in instances_with_shift:
		    		instances_with_shift.append(file_name)

print(f"Analysis regarding violations of schedules for {base_data}\n")
if instances_with_shift:
	print("instances with shift")
	for instance in instances_with_shift:
		print(instance)
else:
	print("No shifts in any instance\n")
