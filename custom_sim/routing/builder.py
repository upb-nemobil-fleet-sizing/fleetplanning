
from __future__ import annotations

import json
import math
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from typing import Dict, Any, Optional

from shapely.geometry import Polygon

import osmium
import osmnx as ox
import osmnx._http as ox_http
import osmnx._overpass as ox_overpass
from osmnx import projection, simplification, stats, truncate
import networkit as nk
import numpy as np

from osmnx.truncate import largest_component

from .persistence import create_numpy_matrix

# --- OSMnx global configuration ---
ox.settings.use_cache = True
ox.settings.cache_folder = Path(__file__).resolve().parent / "data" / "osm_cache"
ox.settings.logs_folder = Path(__file__).resolve().parent / "data" / "osm_logs"
ox.settings.log_console = False  # (optional) suppress verbose OSMnx logging
_EXTRA_USEFUL_WAY_TAGS = [
	"maxspeed:forward",
	"maxspeed:backward",
	"maxspeed:type",
	"zone:maxspeed",
	"maxspeed:conditional",
	"maxspeed:advisory",
	"motor_vehicle",
	"vehicle",
	"foot",
	"bicycle",
	"surface",
	"smoothness",
	"tracktype",
]
ox.settings.useful_tags_way = ox.settings.useful_tags_way + [
	tag for tag in _EXTRA_USEFUL_WAY_TAGS if tag not in ox.settings.useful_tags_way
]

# Mappings for symbolic OSM speed tags such as "DE:urban".
# Based on OSM/OSRM default-speed conventions for Germany.
_SPEED_TAG_DEFAULTS_KPH = {
	"de:urban": 50.0,
	"de:rural": 100.0,
	"de:zone30": 30.0,
	"de:living_street": 7.0,
}

# Practical fallback speeds/caps are primarily based on OSRM's car profile:
# https://github.com/Project-OSRM/osrm-backend/blob/master/profiles/car.lua
# https://github.com/Project-OSRM/osrm-backend/blob/master/profiles/lib/maxspeed.lua
# Adapted here on 2026-05-05 from the then-current OSRM source tree
# (repository latest release: 26.4.1 on 2026-04-18). The OSRM license is in OSRM_LICENSE.txt.
_FALLBACK_BASE_SPEEDS_KPH = {
	"motorway": 90.0,
	"motorway_link": 45.0,
	"trunk": 85.0,
	"trunk_link": 40.0,
	"primary": 65.0,
	"primary_link": 30.0,
	"secondary": 55.0,
	"secondary_link": 25.0,
	"tertiary": 40.0,
	"tertiary_link": 20.0,
	"unclassified": 25.0,
	"residential": 25.0,
	"living_street": 10.0,
	"service": 15.0,
	"emergency_bay": 10.0,
}

# OSRM models these as service-road penalties rather than hard caps.
_SERVICE_SPEED_FACTORS = {
	"alley": 0.5,
	"parking": 0.5,
	"parking_aisle": 0.5,
	"driveway": 0.5,
	"drive-through": 0.5,
	"drive-thru": 0.5,
}

_SURFACE_SPEED_CAPS_KPH = {
	"concrete": None,
	"concrete:plates": None,
	"concrete:lanes": None,
	"paved": None,
	"asphalt": None,
	"cement": 80.0,
	"compacted": 80.0,
	"fine_gravel": 80.0,
	"paving_stones": 60.0,
	"metal": 60.0,
	"bricks": 60.0,
	"grass": 40.0,
	"wood": 40.0,
	"sett": 40.0,
	"grass_paver": 40.0,
	"gravel": 40.0,
	"unpaved": 40.0,
	"ground": 40.0,
	"dirt": 40.0,
	"pebblestone": 40.0,
	"tartan": 40.0,
	"cobblestone": 30.0,
	"earth": 20.0,
	"stone": 20.0,
	"rocky": 20.0,
	"sand": 20.0,
	"mud": 10.0,
}

_TRACKTYPE_SPEED_CAPS_KPH = {
	"grade1": 60.0,
	"grade2": 40.0,
	"grade3": 30.0,
	"grade4": 25.0,
	"grade5": 20.0,
}

_SMOOTHNESS_SPEED_CAPS_KPH = {
	"intermediate": 80.0,
	"bad": 40.0,
	"very_bad": 20.0,
	"horrible": 10.0,
	"very_horrible": 5.0,
	"impassable": 0.0,
}

# Policy switch: if True, explicit legal speed can still be capped by a
# small set of strong practical tags such as driveway/rough track surfaces.
_CAP_EXPLICIT_SPEEDS_WITH_STRONG_PRACTICAL_TAGS = True

# Policy switch: if False, explicit legal speed wins whenever present.
# If True, explicit legal speed is capped by the practical-speed model.
_CAP_EXPLICIT_SPEEDS_WITH_PRACTICAL_MODEL = False

_STRONG_SURFACE_SPEED_CAPS_KPH = {
	"gravel": 40.0,
	"unpaved": 40.0,
	"ground": 40.0,
	"earth": 20.0,
	"sand": 20.0,
	"mud": 10.0,
}

_SPEED_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)")


# ----------------------------------


def stable_value_key(value: Any) -> tuple[str, str]:
	"""
	Build a deterministic sort key for heterogeneous node/edge identifiers.
	"""
	return (type(value).__name__, str(value))


@contextmanager
def _track_overpass_request_stats():
	"""
	Track whether OSMnx served Overpass requests from cache or from the network.
	"""
	stats = {
		"cache_hits": 0,
		"downloads": 0,
	}

	orig_retrieve_from_cache = ox_http._retrieve_from_cache
	orig_requests_post = ox_overpass.requests.post

	def tracked_retrieve_from_cache(url: str):
		response_json = orig_retrieve_from_cache(url)
		if response_json is not None:
			stats["cache_hits"] += 1
		return response_json

	def tracked_requests_post(*args, **kwargs):
		stats["downloads"] += 1
		return orig_requests_post(*args, **kwargs)

	ox_http._retrieve_from_cache = tracked_retrieve_from_cache
	ox_overpass.requests.post = tracked_requests_post
	try:
		yield stats
	finally:
		ox_http._retrieve_from_cache = orig_retrieve_from_cache
		ox_overpass.requests.post = orig_requests_post


# ----------------------------------


def select_best_edge_for_weight(edge_variants: Dict[Any, Dict[str, Any]],
								weight_attr: str):
	"""
	Pick a canonical edge variant for one directed (u, v) pair.

	Selection is deterministic:
	1. smallest routing weight
	2. smallest edge length
	3. smallest edge key
	"""
	best = None

	for key, data in sorted(edge_variants.items(), key=lambda item: stable_value_key(item[0])):
		w = data.get(weight_attr, None)
		if w is None:
			continue

		try:
			weight = float(w)
		except (TypeError, ValueError):
			continue

		try:
			length = float(data.get("length", float("inf")))
		except (TypeError, ValueError):
			length = float("inf")

		rank = (weight, length, stable_value_key(key))
		if best is None or rank < best[0]:
			best = (rank, key, data, weight)

	if best is None:
		return None, None, None

	_, key, data, weight = best
	return key, data, weight


# ----------------------------------


def _tag_values(value: Any) -> list[Any]:
	"""Flatten a scalar or small list-like tag value into a plain list."""
	if value is None:
		return []

	values = []
	stack = [value]
	while stack:
		item = stack.pop()
		if item is None:
			continue
		# GraphML roundtrips can turn tags into tiny list-like containers.
		if isinstance(item, (list, tuple, set)):
			for nested in reversed(list(item)):
				stack.append(nested)
			continue
		values.append(item)

	return values

# ----------------------------------

def _normalized_tag_value(value: Any) -> Optional[str]:
	"""Return the first flattened tag value as a stripped string."""
	values = _tag_values(value)
	if not values:
		return None
	text = str(values[0]).strip()
	return text or None

# ----------------------------------

def _normalized_tag_values(value: Any) -> list[str]:
	"""Return all flattened tag values as stripped strings."""
	values = []
	seen = set()

	for item in _tag_values(value):
		text = str(item).strip()
		if not text or text in seen:
			continue
		values.append(text)
		seen.add(text)

	return values

# ----------------------------------

def _parse_speed_value_kph(value: Any,
								speed_tag_defaults: Optional[Dict[str, float]] = None) -> Optional[float]:
	"""Parse a numeric or symbolic OSM speed tag into km/h."""
	values = []

	for item in _tag_values(value):
		if isinstance(item, (int, float)):
			if item > 0:
				values.append(float(item))
			continue

		text = str(item).strip()
		if not text:
			continue

		parts = re.split(r"[;|]", text)
		for part in parts:
			part = part.strip()
			if not part:
				continue

			part_key = part.lower()
			if speed_tag_defaults and part_key in speed_tag_defaults:
				values.append(speed_tag_defaults[part_key])
				continue

			match = _SPEED_RE.search(part_key)
			if match is None:
				continue

			speed = float(match.group(1).replace(",", "."))
			if "mph" in part_key:
				speed *= 1.60934
			if speed > 0:
				values.append(speed)

	if not values:
		return None

	return sum(values) / len(values)

# ----------------------------------

def _edge_matches_local_source_network_type(data: Dict[str, Any],
											 network_type: str) -> bool:
	"""
	Approximate OSMnx's built-in network_type filtering for local XML input.
	Currently supports the router's driving modes.
	"""
	if network_type not in {"drive", "drive_service"}:
		raise NotImplementedError(
			f"Local OSM source mode does not support network_type='{network_type}' yet."
		)

	highway_values = set(_normalized_tag_values(data.get("highway")))
	if not highway_values:
		return False

	if "yes" in _normalized_tag_values(data.get("area")):
		return False
	if "private" in _normalized_tag_values(data.get("access")):
		return False
	if "no" in _normalized_tag_values(data.get("motor_vehicle")):
		return False
	if "no" in _normalized_tag_values(data.get("motorcar")):
		return False

	excluded_highways = {
		"abandoned",
		"bridleway",
		"bus_guideway",
		"construction",
		"corridor",
		"cycleway",
		"elevator",
		"escalator",
		"footway",
		"no",
		"path",
		"pedestrian",
		"planned",
		"platform",
		"proposed",
		"raceway",
		"razed",
		"rest_area",
		"steps",
		"track",
	}
	if network_type == "drive":
		excluded_highways.update({"service", "services"})
	else:
		excluded_highways.add("services")

	if any(highway in excluded_highways for highway in highway_values):
		return False

	service_values = set(_normalized_tag_values(data.get("service")))
	if network_type == "drive":
		excluded_services = {"alley", "driveway", "emergency_access", "parking", "parking_aisle", "private"}
	else:
		excluded_services = {"emergency_access", "parking", "parking_aisle", "private"}

	if any(service in excluded_services for service in service_values):
		return False

	return True

# ----------------------------------

def _filter_local_source_graph_by_network_type(G, network_type: str):
	"""Remove raw edges that do not belong to the requested network type."""
	# Mirror the coarse network-type filtering that Overpass would otherwise do
	# before the graph ever reaches our local postprocessing pipeline.
	kept_edges = [
		(u, v, k)
		for u, v, k, data in G.edges(keys=True, data=True)
		if _edge_matches_local_source_network_type(data, network_type)
	]
	return G.edge_subgraph(kept_edges).copy()

# ----------------------------------

def _raw_way_matches_local_source_network_type(way, network_type: str) -> bool:
	"""Apply the same coarse driving-network filter directly to raw OSM way tags."""
	tag_data = {tag.k: tag.v for tag in way.tags}
	return _edge_matches_local_source_network_type(tag_data, network_type)

# ----------------------------------

def _reduce_local_source_to_buffered_bbox(source_path: Path, bbox, network_type: str) -> Path:
	"""
	Reduce a local OSM source to the buffered bbox before importing it with OSMnx.
	The reducer is format-agnostic on the input side and always writes a temporary
	XML-like `.osm.gz` file so the rest of the pipeline can stay unchanged.
	"""
	start_time = time.perf_counter()
	min_lon, min_lat, max_lon, max_lat = bbox
	source_size_bytes = source_path.stat().st_size
	tmp = tempfile.NamedTemporaryFile(
		prefix="routing_source_clip_",
		suffix=".osm.gz",
		delete=False,
	)
	tmp_path = Path(tmp.name)
	tmp.close()
	idx_tmp = tempfile.NamedTemporaryFile(
		prefix="routing_source_idx_",
		delete=False,
	)
	idx_path = Path(idx_tmp.name)
	idx_tmp.close()

	tracker = osmium.IdTracker()
	# Keep the reduction single-threaded: it is easier on memory and gives
	# more predictable behavior for one-off preprocessing runs.
	thread_pool = osmium.io.ThreadPool(1)
	# Store node locations on disk so ways can access coordinates without
	# keeping a full in-memory node table for the whole source file.
	idx_spec = f"sparse_file_array,{idx_path}"
	scanned_way_count = 0
	routable_way_count = 0

	try:
		# First pass: inspect only ways, but let libosmium attach node locations
		# from a disk-backed index so we never materialize all bbox nodes in Python.
		way_processor = (
			osmium.FileProcessor(source_path, thread_pool=thread_pool)
			.with_locations(idx_spec)
			.with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
		)

		for way in way_processor:
			scanned_way_count += 1
			if not _raw_way_matches_local_source_network_type(way, network_type):
				continue
			routable_way_count += 1

			if any(
				node.location.valid()
				and min_lat <= node.location.lat <= max_lat
				and min_lon <= node.location.lon <= max_lon
				for node in way.nodes
				):
					tracker.add_way(way.id)
					tracker.add_references(way)

		kept_way_count = len(tracker.way_ids())
		kept_node_count = len(tracker.node_ids())

		# Second pass: write only the tracked nodes and ways back out. OSMnx's
		# XML importer builds the graph from nodes and ways only, so relations
		# do not need to survive this reduced intermediate file.
		write_processor = (
			osmium.FileProcessor(
				source_path,
				osmium.osm.NODE | osmium.osm.WAY,
				thread_pool=thread_pool,
			)
			.with_filter(tracker.id_filter())
		)
		written_node_count = 0
		written_way_count = 0
		with osmium.SimpleWriter(tmp_path, overwrite=True) as writer:
			for obj in write_processor:
				writer.add(obj)
				if obj.is_node():
					written_node_count += 1
				else:
					written_way_count += 1
	finally:
		try:
			idx_path.unlink(missing_ok=True)
		except OSError:
			pass

	reduced_size_bytes = tmp_path.stat().st_size
	elapsed_s = time.perf_counter() - start_time
	print(
		"[builder] reduction finished in "
		f"{elapsed_s:.1f}s: {routable_way_count}/{scanned_way_count} routable ways "
		f"kept {kept_way_count} ways and {kept_node_count} nodes, wrote "
		f"{written_way_count} ways and {written_node_count} nodes "
		f"({source_size_bytes / (1024 * 1024):.1f} MB -> "
		f"{reduced_size_bytes / (1024 * 1024):.1f} MB)"
	)

	return tmp_path

# ----------------------------------

def _build_osm_graph_from_source_file(polygon: Polygon,
										 source_path: Path,
										 network_type: str):
	"""
	Load raw OSM data from a local file, then apply the same polygon-based
	postprocessing structure as OSMnx's graph_from_polygon().
	"""
	source_path = Path(source_path)
	suffixes = {suffix.lower() for suffix in source_path.suffixes}
	start_time = time.perf_counter()

	if not source_path.exists():
		raise FileNotFoundError(f"OSM source file not found: {source_path}")
	if not ({".pbf", ".osm", ".xml"} & suffixes):
		raise NotImplementedError(
			f"Unsupported OSM source format for '{source_path.name}'."
		)

	poly_proj, crs_utm = projection.project_geometry(polygon)
	# Keep a small outside context so border cuts do not distort simplification.
	poly_proj_buff = poly_proj.buffer(500)
	poly_buff, _ = projection.project_geometry(
		poly_proj_buff,
		crs=crs_utm,
		to_latlong=True,
	)

	print("[builder] reducing local source to buffered bbox before graph import...")
	reduced_source_path = _reduce_local_source_to_buffered_bbox(
		source_path,
		poly_buff.bounds,
		network_type,
	)

	bidirectional = network_type in ox.settings.bidirectional_network_types
	print(f"[builder] importing reduced source with OSMnx from '{reduced_source_path.name}'...")
	import_start = time.perf_counter()
	try:
		G_raw = ox.graph_from_xml(
			reduced_source_path,
			bidirectional=bidirectional,
			simplify=False,
			retain_all=True,
		)
	finally:
		reduced_source_path.unlink(missing_ok=True)
	print(
		f"[builder] reduced source import finished in {time.perf_counter() - import_start:.1f}s: "
		f"{len(G_raw.nodes())} nodes, {len(G_raw.edges())} edges"
	)
	# Even after raw-source reduction, the imported graph may still contain
	# edges that the driving-network filter should drop.
	G_raw = _filter_local_source_graph_by_network_type(G_raw, network_type)

	G_buff = truncate.truncate_graph_polygon(
		G_raw,
		poly_buff,
		truncate_by_edge=False,
	)
	# Match OSMnx's polygon workflow: clean connectivity before simplifying.
	G_buff = truncate.largest_component(G_buff, strongly=False)
	G_buff = simplification.simplify_graph(G_buff)

	# After simplifying with buffered context, cut back to the true target area.
	G = truncate.truncate_graph_polygon(
		G_buff,
		polygon,
		truncate_by_edge=False,
	)
	# The final cut can disconnect pieces again near the boundary.
	G = truncate.largest_component(G, strongly=False)

	# Preserve street counts from the buffered graph for border nodes.
	street_counts = stats.count_streets_per_node(G_buff, nodes=G.nodes)
	for node, count in street_counts.items():
		if node in G.nodes:
			G.nodes[node]["street_count"] = count

	print(
		f"[builder] local-source postprocessing finished in {time.perf_counter() - start_time:.1f}s: "
		f"{len(G.nodes())} nodes, {len(G.edges())} edges"
	)

	return G

# ----------------------------------

def _edge_explicit_speed_kph(data: Dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
	"""Resolve the best explicit legal speed tag for one directed edge."""
	# Prefer the most specific legal speed source available for this directed edge.
	reversed_edge = bool(data.get("reversed", False))
	if reversed_edge:
		directional_key = "maxspeed:backward"
	else:
		directional_key = "maxspeed:forward"

	directional_speed = _parse_speed_value_kph(data.get(directional_key))
	if directional_speed is not None:
		return directional_speed, directional_key

	maxspeed_speed = _parse_speed_value_kph(data.get("maxspeed"))
	if maxspeed_speed is not None:
		return maxspeed_speed, "maxspeed"

	zone_speed = _parse_speed_value_kph(
		data.get("zone:maxspeed"),
		speed_tag_defaults=_SPEED_TAG_DEFAULTS_KPH,
	)
	if zone_speed is not None:
		return zone_speed, "zone:maxspeed"

	type_speed = _parse_speed_value_kph(
		data.get("maxspeed:type"),
		speed_tag_defaults=_SPEED_TAG_DEFAULTS_KPH,
	)
	if type_speed is not None:
		return type_speed, "maxspeed:type"

	return None, None

# ----------------------------------

def _apply_speed_cap(current_speed_kph: float,
							value: Any,
							speed_caps: Dict[str, float]) -> float:
	"""Lower a speed if any flattened tag value has a configured cap."""
	capped_speed_kph = current_speed_kph

	for normalized in _normalized_tag_values(value):
		cap = speed_caps.get(normalized, current_speed_kph)
		if cap is None:
			continue
		capped_speed_kph = min(capped_speed_kph, cap)

	return capped_speed_kph

# ----------------------------------

def _apply_speed_factor(current_speed_kph: float,
							 value: Any,
							 speed_factors: Dict[str, float]) -> float:
	"""Scale a speed if any flattened tag value has a configured factor."""
	scaled_speed_kph = current_speed_kph

	for normalized in _normalized_tag_values(value):
		factor = speed_factors.get(normalized)
		if factor is None:
			continue
		scaled_speed_kph = min(scaled_speed_kph, current_speed_kph * factor)

	return scaled_speed_kph

# ----------------------------------

def _edge_practical_speed_kph(data: Dict[str, Any]) -> float:
	"""Estimate a practical travel speed from road-class fallback rules."""
	# Fallback / practical speed model for edges with no usable legal-speed tag.
	highway_values = _normalized_tag_values(data.get("highway"))
	if highway_values:
		speed_kph = min(
			_FALLBACK_BASE_SPEEDS_KPH.get(highway, 50.0)
			for highway in highway_values
		)
	else:
		speed_kph = 50.0

	# These caps model road characteristics that typically reduce travel speed.
	speed_kph = _apply_speed_factor(speed_kph, data.get("service"), _SERVICE_SPEED_FACTORS)
	speed_kph = _apply_speed_cap(speed_kph, data.get("surface"), _SURFACE_SPEED_CAPS_KPH)
	speed_kph = _apply_speed_cap(speed_kph, data.get("tracktype"), _TRACKTYPE_SPEED_CAPS_KPH)
	speed_kph = _apply_speed_cap(speed_kph, data.get("smoothness"), _SMOOTHNESS_SPEED_CAPS_KPH)

	return speed_kph

# ----------------------------------

def _edge_strong_practical_speed_cap_kph(data: Dict[str, Any]) -> Optional[float]:
	"""Return a strong practical cap for explicit speeds, if any applies."""
	highway_values = _normalized_tag_values(data.get("highway"))
	if highway_values:
		base_speed_kph = min(
			_FALLBACK_BASE_SPEEDS_KPH.get(highway, 50.0)
			for highway in highway_values
		)
	else:
		base_speed_kph = 50.0
	caps = []

	for service in _normalized_tag_values(data.get("service")):
		service_factor = _SERVICE_SPEED_FACTORS.get(service)
		if service_factor is not None:
			caps.append(base_speed_kph * service_factor)

	if "living_street" in highway_values:
		caps.append(_FALLBACK_BASE_SPEEDS_KPH["living_street"])

	for surface in _normalized_tag_values(data.get("surface")):
		if surface in _STRONG_SURFACE_SPEED_CAPS_KPH:
			caps.append(_STRONG_SURFACE_SPEED_CAPS_KPH[surface])

	for tracktype in _normalized_tag_values(data.get("tracktype")):
		if tracktype in _TRACKTYPE_SPEED_CAPS_KPH:
			caps.append(_TRACKTYPE_SPEED_CAPS_KPH[tracktype])

	if not caps:
		return None

	return min(caps)

# ----------------------------------

def add_edge_speeds_from_tags(
	G,
	cap_explicit_speeds_with_practical_model=None,
	cap_explicit_speeds_with_strong_practical_tags=None,
):
	"""
	Assign deterministic edge speeds in km/h using explicit speed tags first
	and a small local fallback model for the remaining edges.
	"""
	if cap_explicit_speeds_with_practical_model is None:
		cap_explicit_speeds_with_practical_model = _CAP_EXPLICIT_SPEEDS_WITH_PRACTICAL_MODEL
	if cap_explicit_speeds_with_strong_practical_tags is None:
		cap_explicit_speeds_with_strong_practical_tags = (
			_CAP_EXPLICIT_SPEEDS_WITH_STRONG_PRACTICAL_TAGS
		)

	for _, _, _, data in G.edges(keys=True, data=True):
		legal_speed_kph, legal_speed_source = _edge_explicit_speed_kph(data)
		practical_speed_kph = _edge_practical_speed_kph(data)
		strong_practical_speed_cap_kph = _edge_strong_practical_speed_cap_kph(data)

		if legal_speed_kph is None:
			# No explicit legal speed: rely entirely on the practical fallback model.
			speed_kph = practical_speed_kph
			speed_source = "fallback"
		elif cap_explicit_speeds_with_practical_model:
			# Optional mode: explicit legal speed can still be capped by
			# practical road characteristics such as driveway/surface.
			speed_kph = min(legal_speed_kph, practical_speed_kph)
			speed_source = legal_speed_source
		elif (
			cap_explicit_speeds_with_strong_practical_tags
			and strong_practical_speed_cap_kph is not None
		):
			# Default mode: only a small set of strong practical tags can cap
			# an explicit legal speed.
			speed_kph = min(legal_speed_kph, strong_practical_speed_cap_kph)
			speed_source = legal_speed_source
		else:
			# Default mode: explicit legal speed wins whenever it is present.
			speed_kph = legal_speed_kph
			speed_source = legal_speed_source

		data["legal_speed_kph"] = legal_speed_kph
		data["legal_speed_source"] = legal_speed_source
		data["practical_speed_kph"] = practical_speed_kph
		data["strong_practical_speed_cap_kph"] = strong_practical_speed_cap_kph
		data["speed_kph"] = speed_kph
		data["speed_kph_source"] = speed_source

	return G


# ----------------------------------


def build_osm_graph_from_polygon(
	polygon: Polygon,
	network_type="drive_service",
	keep_only_largest_strong_component=True,
	cap_explicit_speeds_with_practical_model=None,
	cap_explicit_speeds_with_strong_practical_tags=None,
	osm_source_path: str | Path | None = None,
):
	"""
	Download/build OSM graph clipped to polygon and add travel_time field.
	Returns an osmnx graph G.
	"""
	build_start = time.perf_counter()
	source_start = time.perf_counter()
	if osm_source_path is not None and not Path(osm_source_path).exists():
		print(f"[builder] configured OSM source '{osm_source_path}' not found, falling back to Overpass download")
		osm_source_path = None
	if osm_source_path is None:
		with _track_overpass_request_stats() as overpass_stats:
			G = ox.graph_from_polygon(polygon, network_type=network_type)
		print(
			"[builder] overpass request sources: "
			f"{overpass_stats['cache_hits']} cache hit(s), "
			f"{overpass_stats['downloads']} download(s)"
		)
	else:
		print(f"[builder] loading raw OSM source from '{Path(osm_source_path).name}'...")
		G = _build_osm_graph_from_source_file(
			polygon,
			Path(osm_source_path),
			network_type,
		)
	print(
		f"[builder] raw graph acquisition finished in {time.perf_counter() - source_start:.1f}s: "
		f"{len(G.nodes())} nodes, {len(G.edges())} edges"
	)

	if keep_only_largest_strong_component:
		print("[builder] keeping only largest strongly-connected component")
		G = largest_component(G, strongly=True)
	
	enrich_start = time.perf_counter()
	G = add_edge_speeds_from_tags(
		G,
		cap_explicit_speeds_with_practical_model=cap_explicit_speeds_with_practical_model,
		cap_explicit_speeds_with_strong_practical_tags=cap_explicit_speeds_with_strong_practical_tags,
	) # adds deterministic 'speed_kph'
	G = ox.add_edge_travel_times(G) # adds 'travel_time' (seconds)
	print(
		f"[builder] edge speed/travel-time enrichment finished in {time.perf_counter() - enrich_start:.1f}s"
	)
	print(
		f"[builder] final graph build finished in {time.perf_counter() - build_start:.1f}s: "
		f"{len(G.nodes())} nodes, {len(G.edges())} edges"
	)
	return G

# ----------------------------------

def load_profiles(config_path: Path = None):
	if config_path is None:
		config_path = Path(__file__).resolve().parent / "vehicle_profiles.json"
	with open(config_path, "r", encoding="utf-8") as f:
		return json.load(f)

# ----------------------------------

def load_routing_config(config_path: Path = None):
	if config_path is None:
		config_path = Path(__file__).resolve().parent / "routing_config.json"
	with open(config_path, "r", encoding="utf-8") as f:
		config = json.load(f)
	
	source_path = config.get("build", {}).get("source", {}).get("osm_source_path")
	if source_path:
		source_path = Path(source_path)
		if not source_path.is_absolute():
			source_path = (config_path.parent / source_path).resolve()
		config["build"]["source"]["osm_source_path"] = str(source_path)
	
	return config

# ----------------------------------

def compute_edge_time_for_profile(data: Dict[str, Any],
									profile_rules: Dict[str, Any],
									record_debug: bool = True,
								profile_name: Optional[str] = None) -> Optional[float]:
	"""
	Returns travel time in seconds for this edge under the given profile rules,
	or None if the edge should be excluded for this profile.
	profile_rules:
		{
		"max_speed_mps": 30,
		"forbid_highways": {"motorway_link"},               # exclude these
		"min_allowed_osm_speed_mps": None,                  # exclude if osm < this
		"avoid_highways": {"residential": 1.2},             # penalty factors
		"global_speed_factor": 1.0,                         # scale whole profile (e.g. congestion)
		}
	"""
	length_m = data.get("length", None)
	if length_m <= 0 or length_m is None:
		return None
	
	highway_values = _normalized_tag_values(data.get("highway"))
	
	forbidden_highways = set(profile_rules.get("forbid_highways", ()))
	if any(highway in forbidden_highways for highway in highway_values):
		return None
	
	osm_speed = data.get("speed_kph", None) / 3.6
	
	# Optional hard exclusion for too-slow roads under this profile
	min_osm = profile_rules.get("min_allowed_osm_speed_mps")
	if min_osm is not None and osm_speed < min_osm:
		return None
	
	# Vehicle speed cap
	vmax = profile_rules.get("max_speed_mps", osm_speed)
	eff_speed_mps = min(osm_speed, vmax)
	
	# Optional global profile scaling (e.g., congestion or sluggish vehicle)
	eff_speed_mps *= profile_rules.get("global_speed_factor", 1.0)
	
	# Optional highway-specific penalties (slow down on certain classes)
	penalty = 1.0
	avoid_map = profile_rules.get("avoid_highways", {})
	if highway_values:
		penalty_factors = [
			avoid_map[highway]
			for highway in highway_values
			if highway in avoid_map
		]
		if penalty_factors:
			penalty *= max(penalty_factors)  # >1.0 means slower/longer time
	
	# Convert to time (seconds). Penalty multiplies time (equivalently divides speed).
	if eff_speed_mps <= 0:
		return None
	
	time_s = (length_m / eff_speed_mps) * penalty
	
	if record_debug:
		# Store all speed references for visualization / debugging
		if "maxspeed" in data:
			data["osm_maxspeed_raw"] = data["maxspeed"]         # raw OSM string
		
		if profile_name:
			data[f"osm_speed_mps_used_{profile_name}"] = osm_speed
			data[f"eff_speed_mps_used_{profile_name}"] = eff_speed_mps
		else:
			# fallback for single-profile use
			data["osm_speed_mps_used"] = osm_speed
			data["eff_speed_mps_used"] = eff_speed_mps
	
	return time_s

# ----------------------------------

def add_vehicle_profile_times(G, profiles: Dict[str, Dict[str, Any]]):
	"""
	For each profile (e.g., "cab", "pro"), write an edge attribute f"{profile}_time".
	If an edge should be excluded for a profile, set the time to None (we'll skip it
	when converting to Networkit).
	"""
	start_time = time.perf_counter()
	edge_count = G.number_of_edges()
	for profile_name, rules in profiles.items():
		for _, _, _, data in G.edges(keys=True, data=True):
			t = compute_edge_time_for_profile(
				data,
				rules,
				record_debug=True,
				profile_name=profile_name,
			)
			e = compute_edge_energy_for_profile(
				data,
				rules,
				record_debug=True,
				profile_name=profile_name,
			)
			if t is not None:
				data[f"{profile_name}_time"] = t
			if e is not None:
				data[f"{profile_name}_energy_wh"] = e
	print(
		f"[builder] profile edge weights finished in {time.perf_counter() - start_time:.1f}s: "
		f"{len(profiles)} profiles on {edge_count} edges"
	)
	return G

# ----------------------------------

def compute_edge_energy_for_profile(data: Dict[str, Any],
									profile_rules: Dict[str, Any],
									record_debug: bool = True,
									profile_name: Optional[str] = None) -> Optional[float]:
	"""
	Compute edge energy consumption (kWh) for this vehicle profile.
	Current model: constant
	(proportional to distance * (1 + speed_factor^2) commented out)
	"""
	length_m = data.get("length", 0)
	if length_m <= 0 or length_m is None:
		return None
	speed_mps = data.get("speed_kph", 0) / 3.6
	# Vehicle speed cap
	vmax = profile_rules.get("max_speed_mps", speed_mps)
	eff_speed_mps = min(speed_mps, vmax)
	if eff_speed_mps is None or eff_speed_mps <= 0:
		return None
	
	''' # proportional
	# base Wh/m from profile or default
	base_wh_per_m = profile_rules.get("energy_per_wh_m", 150)
	# simple aerodynamic correction (optional)
	speed_factor = (speed_mps / 100.0) ** 2
	wh_per_m = base_wh_per_m * (1.0 + 0.2 * speed_factor)

	energy_wh = (length_m / 1000.0) * wh_per_m / 1000.0
	'''
	
	# constant formula
	# Energy per m (Wh/m)
	energy_per_wh_m = profile_rules.get("energy_per_wh_m", 180)
	energy_wh = (energy_per_wh_m * length_m)
	
	if record_debug:
		if profile_name:
			data[f"energy_wh_used_{profile_name}"] = energy_wh
		else:
			data["energy_wh_used"] = energy_wh
		
	return energy_wh

# ----------------------------------

def to_networkit_with_attr(G, weight_attr: str):
	"""
	Create a directed Networkit graph from OSMnx graph using a given edge-time attribute.
	"""
	print("[builder] converting to networkit graph...")
	nodes_sorted = sorted(G.nodes(), key=stable_value_key)
	node_map = {node_id: idx for idx, node_id in enumerate(nodes_sorted)}
	nkG = nk.graph.Graph(n=len(node_map), weighted=True, directed=True)
	edge_choice = {}
	edge_groups = {}

	for u, v, k, data in G.edges(keys=True, data=True):
		if (u, v) not in edge_groups:
			edge_groups[(u, v)] = {}
		edge_groups[(u, v)][k] = data

	for (u, v) in sorted(edge_groups.keys(),
						 key=lambda pair: (stable_value_key(pair[0]), stable_value_key(pair[1]))):
		key, _, weight = select_best_edge_for_weight(edge_groups[(u, v)], weight_attr)
		if key is None:
			continue

		uu = node_map[u]
		vv = node_map[v]
		nkG.addEdge(uu, vv, weight)
		edge_choice[(u, v)] = key

	return nkG, node_map, edge_choice

# ----------------------------------

def build_ch(nkG):
	"""
	Build Contraction Hierarchy on a networkit graph (nkG).
	Returns the CH object (networkit.distance.Ch).
	"""
	print("[builder] preprocessing CH...")
	ch = nk.distance.Ch(G=nkG, weight=True)
	ch.preprocess()
	return ch

# ----------------------------------

def build_od_matrix(nkG):
	"""Build dense distance and predecessor matrices for all Networkit nodes."""
	n = nkG.numberOfNodes()

	od_matrix = [[math.inf] * n for _ in range(n)]
	pred_matrix = [[-1] * n for _ in range(n)]

	for source in range(n):
		dj = nk.distance.Dijkstra(nkG, source)
		dj.run()
		dist = dj.getDistances()

		for target in range(n):
			if source != target:
				od_matrix[source][target] = dist[target]
				predecessors = dj.getPredecessors(target)
				if predecessors:
					pred_matrix[source][target] = predecessors[0]

	return od_matrix, pred_matrix


def build_od_numpy_matrices(nkG, pred_path: Path, dist_path: Path):
	"""Build dense OD caches directly into NumPy `.npy` files."""
	n = nkG.numberOfNodes()
	pred_matrix = create_numpy_matrix(pred_path, np.int32, (n, n))
	pred_matrix.fill(-1)

	dist_matrix = create_numpy_matrix(dist_path, np.float64, (n, n))
	dist_matrix[:] = np.inf

	for source in range(n):
		dj = nk.distance.Dijkstra(nkG, source)
		dj.run()
		dist = dj.getDistances()

		dist_matrix[source, :] = np.asarray(dist, dtype=np.float64)

		pred_row = np.full(n, -1, dtype=np.int32)
		for target in range(n):
			if source != target:
				predecessors = dj.getPredecessors(target)
				if predecessors:
					pred_row[target] = predecessors[0]
		pred_matrix[source, :] = pred_row

	dist_matrix.flush()
	pred_matrix.flush()
