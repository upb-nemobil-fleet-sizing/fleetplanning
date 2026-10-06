#!/usr/bin/python3

import argparse
import subprocess
import sys
import os

from collections import defaultdict

import json
import pyproj

import xml.etree.ElementTree as ET


#=======================================================================


def clean_xml_file(file_path, output_path, tail=0):
	with open(file_path, 'r', encoding='utf-8') as file:
		xml_content = file.read()

	# Create a cleaned version of the XML file
	# Keep printable characters, retain newlines and tabs, remove non-printable characters
	cleaned_xml = ''.join([c for c in xml_content if c.isprintable() or c in ['\n', '\r', '\t']])
	
	# Append </population> if not present
	if not cleaned_xml.strip().endswith('</population>'):
		cleaned_xml += '\n</population>'

	## Manually ensure the closing tag exists if it's missing
	#with open(cleaned_file_path, 'a', encoding='utf-8') as file:
		#file.write('</population>\n')  # Ensure the closing </population> is at the end of the file
		
	# Save cleaned file
	with open(output_path, 'w', encoding='utf-8') as file:
		file.write(cleaned_xml)
	
	# Check the last lines of the file to make sure it's correct
	if ( tail > 0 ):
		with open(output_path, 'r', encoding='utf-8') as file:
			lines = file.readlines()
			print("Last 10 lines of cleaned XML:")
			for line in lines[-10:]:
				print(line.strip())
	
	return

#=======================================================================

''' WARNING:
	Removed this bit since *xmllint* (`libxml2-utils`) needs to be installed
	- Do this in the terminal with 
		xmllint --noout ./data/plans-subset-3Agents.xml
	or
		xmllint --noout --dtdvalid http://www.matsim.org/files/dtd/population_v6.dtd ./data/plans-subset-3Agents.xml
'''
#def check_for_hidden_characters(file_path, cleaned="cleaned.xml"):
	#cleaned_file_path = cleaned

	## Clean the XML file, preserving the formatting
	#clean_xml_file(file_path, cleaned_file_path)
	
	## Run xmllint to validate XML
	#print("Attempting to validate cleaned XML with xmllint...")

	#try:
		#result = subprocess.run(["xmllint", "--noout", cleaned_file_path], check=True, capture_output=True, text=True)
		#print("XML validation successful!")
	#except subprocess.CalledProcessError as e:
		#print(f"xmllint error: {e.stderr}")  # Print the stderr message from xmllint
		#sys.exit(-1)  # Exit the script with failure
	
	#print("Exiting script after XML validation.")
	##sys.exit(0)
	
	#return

#=======================================================================

from datetime import datetime, timedelta

#def parse_time(timestr):
	#"""Parses HH:MM:SS into timedelta."""
	#try:
		#h, m, s = map(int, timestr.split(":"))
		#return timedelta(hours=h, minutes=m, seconds=s)
	#except:
		#return None

#def add_times(dep, dur):
	#"""Adds dep_time and trav_time as HH:MM:SS strings."""
	#try:
		#base = datetime.strptime(dep, "%H:%M:%S")
		#delta = parse_time(dur)
		#if delta:
			#return (base + delta).time().isoformat()
	#except:
		#return None

def parse_overflow_time(timestr):
	try:
		h, m, s = map(int, timestr.split(":"))
		return timedelta(hours=h, minutes=m, seconds=s)
	except Exception:
		return None

def add_times(dep, dur):
	try:
		dep_delta = parse_overflow_time(dep)
		dur_delta = parse_overflow_time(dur)
		if dep_delta and dur_delta:
			end_delta = dep_delta + dur_delta
			total_seconds = int(end_delta.total_seconds())
			hours = total_seconds // 3600
			minutes = (total_seconds % 3600) // 60
			seconds = total_seconds % 60
			return f"{hours:02}:{minutes:02}:{seconds:02}"
	except Exception:
		return None

def parse_xml(file_path):
	"""
	Parses the cleaned XML file and returns the root element.
	"""
	try:
		tree = ET.parse(file_path)
		root = tree.getroot()
		print("XML parsed successfully!")
		return root
	#except ET.XMLSyntaxError as e:
	except ET.ParseError as e:
		print(f"Error parsing the XML file: {e}")
		return None
	except FileNotFoundError:
		print("The XML file was not found. Please check the file path.")

#=======================================================================

def process_person_data(root):
	"""
	Example function that processes 'person' data from the XML tree.

	:param root:    root of XML tree
	:return:        void
	"""
	#for person in root.findall(".//person"):
	# Iterate over each "person" element
	for person in root.findall('person'):
		person_id = person.get('id')  # Get the person's ID
		print(f"Person ID: {person_id}")
		
		# Iterate through the person's plans
		for plan in person.findall('plan'):
			plan_score = plan.get('score')
			plan_selected = plan.get('selected')
			print(f"  Plan - Score: {plan_score}, Selected: {plan_selected}")
			
			# Process each activity in the plan
			for activity in plan.findall('activity'):
				activity_type = activity.get('type')
				activity_end_time = activity.get('end_time')
				activity_x = activity.get('x')
				activity_y = activity.get('y')
				
				print(f"    Activity - Type: {activity_type}, End Time: {activity_end_time}, Location: ({activity_x}, {activity_y})")
				
				# Extract attributes inside <activity>
				activity_attributes = activity.find('attributes')
				if activity_attributes is not None:
					for attribute in activity_attributes.findall('attribute'):
						attr_name = attribute.get('name')
						attr_value = attribute.text  # The text value of <attribute> tag
						print(f"      Activity Attribute - Name: {attr_name}, Value: {attr_value}")

			# Process each leg in the plan
			for leg in plan.findall('leg'):
				leg_mode = leg.get('mode')
				leg_dep_time = leg.get('dep_time')
				leg_trav_time = leg.get('trav_time')
				
				print(f"    Leg - Mode: {leg_mode}, Departure Time: {leg_dep_time}, Travel Time: {leg_trav_time}")
				
				# Process the route inside each leg
				route = leg.find('route')
				if route is not None:
					route_type = route.get('type')
					route_start_link = route.get('start_link')
					route_end_link = route.get('end_link')
					route_distance = route.get('distance')
					
					print(f"      Route - Type: {route_type}, Start Link: {route_start_link}, End Link: {route_end_link}, Distance: {route_distance}")

				# Extract attributes inside <leg>
				leg_attributes = leg.find('attributes')
				if leg_attributes is not None:
					for attribute in leg_attributes.findall('attribute'):
						attr_name = attribute.get('name')
						attr_value = attribute.text
						print(f"      Leg Attribute - Name: {attr_name}, Value: {attr_value}")


def process_person_data_two(root):
#def process_person_data(root):
	"""
	Extracts origin-destination pairs for DRT rides from the selected plan.
	Each DRT ride includes:
		- a <drt interaction> activity,
		- a <leg mode="drt">,
		- and the following destination <activity>.
	"""
	processed_data = []

	for person in root.findall('person'):
		person_id = person.get('id')

		selected_plan = next(
			(plan for plan in person.findall('plan') if plan.get('selected') == 'yes'), None
		)
		if selected_plan is None:
			continue

		plan_elements = list(selected_plan)
		drt_trips = []
		i = 0

		while i < len(plan_elements) - 2:
			act = plan_elements[i]
			leg = plan_elements[i + 1]
			dest = plan_elements[i + 2]

			# Check for DRT interaction + DRT leg + next activity
			if (
				act.tag == 'activity' and act.get('type') == 'drt interaction'
				and leg.tag == 'leg' and leg.get('mode') == 'drt'
				and dest.tag == 'activity'
			):
				trip = {
					'origin_activity': dict(act.attrib),
					'leg': {
						'attributes': dict(leg.attrib),
						'children': [
							{
								'tag': child.tag,
								'attributes': dict(child.attrib),
								'text': child.text.strip() if child.text else ''
							}
							for child in leg
						]
					},
					'destination_activity': dict(dest.attrib)
				}
				drt_trips.append(trip)
				i += 2  # move forward by 2 to land on dest, and +1 below makes it 3

			i += 1

		if drt_trips:
			processed_data.append({
				'id': person_id,
				'drt_trips': drt_trips
			})

	return processed_data

def process_person_data_three(root):
	"""
	Extracts flat DRT trip data:
	- origin/destination coords
	- start/end time
	- distance and travel_time
	"""
	drt_trip_records = []

	for person in root.findall('person'):
		person_id = person.get('id')
		trip_count = 0

		selected_plan = next(
			(plan for plan in person.findall('plan') if plan.get('selected') == 'yes'), None
		)
		if selected_plan is None:
			continue

		plan_elements = list(selected_plan)
		i = 0

		while i < len(plan_elements) - 2:
			act = plan_elements[i]
			leg = plan_elements[i + 1]
			dest = plan_elements[i + 2]

			if (
				act.tag == 'activity' and 'drt' in act.get('type', '').lower()
				and leg.tag == 'leg' and leg.get('mode') == 'drt'
				and dest.tag == 'activity'
			):
				trip_count += 1  # increment trip counter
				
				origin_x = act.get('x')
				origin_y = act.get('y')
				dest_x = dest.get('x')
				dest_y = dest.get('y')

				dep_time = leg.get('dep_time') or act.get('end_time')
				trav_time = leg.get('trav_time')
				end_time = add_times(dep_time, trav_time) if dep_time and trav_time else dest.get('start_time')

				# Extract route distance if available
				distance = None
				for child in leg:
					if child.tag == 'route':
						distance = child.get('distance')
						break
						
				orig_lon, orig_lat = (convert_xy_to_latlon(float(origin_x),float(origin_y))) if origin_x and origin_y else (None,None)
				dest_lon, dest_lat = (convert_xy_to_latlon(float(dest_x),float(dest_y))) if dest_x and dest_y else (None,None)
				
				drt_trip_records.append({
					'person_id': person_id,
					'trip_number': trip_count,
					'origin_x': float(origin_x) if origin_x else None,
					'origin_y': float(origin_y) if origin_y else None,
					'origin_lat': float(orig_lat) if orig_lat else None,
					'origin_lon': float(orig_lon) if orig_lon else None,
					'destination_x': float(dest_x) if dest_x else None,
					'destination_y': float(dest_y) if dest_y else None,
					'destination_lat': float(dest_lat) if dest_lat else None,
					'destination_lon': float(dest_lon) if dest_lon else None,
					'start_time': dep_time,
					'end_time': end_time,
					'travel_time': trav_time,
					'distance': float(distance) if distance else None
				})

				i += 2  # skip over the leg and activity

			i += 1

	return drt_trip_records

def export_to_json(data, output_path):
	"""
	Exports the processed person data to a JSON file.
	"""
	try:
		with open(output_path, 'w', encoding='utf-8') as f:
			json.dump(data, f, ensure_ascii=False, indent="\t")
		print(f"Data successfully written to {output_path}")
	except Exception as e:
		print(f"Failed to write JSON file: {e}")



def convert_xy_to_latlon(x, y, net_offset_x=0, net_offset_y=0, proj_parameter=None):
	"""
	Convert UTM coordinates to latitude/longitude after applying netOffset.
	
	:param x (float): X coordinate (Cartesian, UTM Eastings)
	:param y (float): Y coordinate (Cartesian, UTM Northings)
	:param net_offset_x (float): Offset to be subtracted from the X coordinate (default: 0)
	:param net_offset_y (float): Offset to be subtracted from the Y coordinate (default: 0)
	:param proj_paramete (str): The projection parameters from the SUMO file (default None: UTM Zone 32)
	:return: Converted latitude, longitude
	"""
	
	# Default proj_parameter if not provided
	if proj_parameter is None:
		# UTM Zone 32 with WGS84 as the default
		proj_parameter = "EPSG:25832"
	
	# Create the projection from the projParameter
	utm_proj = pyproj.CRS(proj_parameter)
	wgs84_proj = pyproj.CRS("EPSG:4326")  # WGS84 for lat/lon

	# Create a transformer for UTM -> WGS84
	transformer = pyproj.Transformer.from_crs(utm_proj, wgs84_proj, always_xy=True)
	transformer = pyproj.Transformer.from_crs(utm_proj, wgs84_proj, always_xy=True)
	
	# Apply the netOffset to adjust the coordinates
	adjusted_x = x - net_offset_x
	adjusted_y = y - net_offset_y
	
	#print(f"Original X:\t {x},\t Original Y:\t {y}")
	#print(f"Net Offset X:\t {net_offset_x},\t Net Offset Y:\t {net_offset_y}")
	#print(f"Adjusted X:\t {adjusted_x},\t Adjusted Y:\t {adjusted_y}")
	
	# Convert to lat/lon
	lat, lon = transformer.transform(adjusted_x, adjusted_y)
	
	return lat, lon

#=======================================================================

def parse_args():
	parser = argparse.ArgumentParser(
		description="Extract DRT trips from a MATSim population XML and write JSON."
	)
	parser.add_argument(
		"xml_file",
		help="Input population XML file.",
	)
	parser.add_argument(
		"-o",
		"--output",
		default=None,
		help="Output JSON path. Defaults to <xml_file_without_ext>.json",
	)
	parser.add_argument(
		"--reuse-existing",
		action="store_true",
		help="Reuse existing output JSON if present instead of reparsing XML.",
	)
	parser.add_argument(
		"--check-overlap",
		action="store_true",
		help="Run overlap checks on extracted trips.",
	)
	return parser.parse_args()


#=======================================================================

if __name__ == "__main__":
	args = parse_args()

	original_xml = args.xml_file
	cleaned_xml = args.xml_file
	output_path = args.output or (os.path.splitext(args.xml_file)[0] + ".json")

	check_overlap = args.check_overlap
	force_read = not args.reuse_existing

	# Clean the XML file
	#clean_xml_file(original_xml, cleaned_xml, 10)

	## Run the check
	#check_for_hidden_characters(orig_file, cleaned_file)
	
	#-----------------------------------------------------------------------
	
	if os.path.exists(output_path) and not force_read:
		print(f"JSON already exists: {output_path}")
		# Load existing JSON instead of parsing XML
		with open(output_path, 'r', encoding='utf-8') as f:
			person_data = json.load(f)
		print(f"Loaded {len(person_data)} records from existing JSON.")
	else:
		# Parse the cleaned XML file
		root = parse_xml(cleaned_xml)

		# If parsing was successful, process the data
		if root is not None:
			#person_data = process_person_data_two(root)
			person_data = process_person_data_three(root)
			export_to_json(person_data, output_path)
		else:
			print("Failed to parse XML file.")
			person_data = []
	
	#-----------------------------------------------------------------------
	
	if check_overlap:
		
		def time_to_seconds(t):
			h, m, s = map(int, t.split(':'))
			return h * 3600 + m * 60 + s
		
		# group by person
		person_data = defaultdict(list)
		for trip in person_data:
			person_data[trip['person_id']].append(trip)
		
		overlaps = []
		
		for pid, trips in person_data.items():
			# sort trips by start time
			trips.sort(key=lambda x: time_to_seconds(x['start_time']))
			
			for i in range(1, len(trips)):
				prev = trips[i - 1]
				curr = trips[i]
				
				prev_end = time_to_seconds(prev['end_time'])
				curr_start = time_to_seconds(curr['start_time'])
				
				if curr_start < prev_end:
					overlaps.append({
						'person_id': pid,
						'prev_trip_start': prev['start_time'],
						'prev_trip_end': prev['end_time'],
						'curr_trip_start': curr['start_time'],
						'curr_trip_end': curr['end_time'],
					})
		
		if overlaps:
			print("Overlapping DRT trips found!")
			for o in overlaps:
				print(
					f"Person {o['person_id']} has overlap: "
					f"{o['prev_trip_start']}–{o['prev_trip_end']} "
					f"overlaps with {o['curr_trip_start']}–{o['curr_trip_end']}"
				)
		else:
			print("All DRT trips are non-overlapping.")
