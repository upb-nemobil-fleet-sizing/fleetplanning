import math
import pathlib

import numpy as np
from shapely.geometry import Point


def point_in_polygon(lon, lat, polygon, allow_boundary=True):
	"""
	Check whether a single point is inside (or on the boundary of) a polygon.
	"""
	p = Point(lon, lat)
	if allow_boundary:
		return polygon.contains(p) or polygon.touches(p)
	return polygon.contains(p)


def file_stamp(path):
	"""
	Stable file stamp used for cache invalidation.
	"""
	path = pathlib.Path(path)
	if not path.exists():
		return None
	st = path.stat()
	return f"{st.st_size}:{st.st_mtime_ns}"


def haversine_distance(lat1, lon1, lat2, lon2):
	"""
	Compute great-circle distance between two points on Earth (in meters).
	"""
	R = 6_371_000.0
	dlat = math.radians(lat2 - lat1)
	dlon = math.radians(lon2 - lon1)
	a = (
		math.sin(dlat / 2) ** 2
		+ math.cos(math.radians(lat1))
		* math.cos(math.radians(lat2))
		* math.sin(dlon / 2) ** 2
	)
	c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
	return R * c


def pick_closest_endpoint(u, v, proj_lat, proj_lon, u_lat, u_lon, v_lat, v_lon):
	"""
	Return whichever endpoint is closer to the projected point.
	"""
	# Old Shapely-based variant kept here for reference:
	# from shapely.geometry import Point
	# pu = Point(u_lon, u_lat)
	# pv = Point(v_lon, v_lat)
	# pp = Point(proj_lon, proj_lat)
	# du, dv = pu.distance(pp), pv.distance(pp)
	du = haversine_distance(proj_lat, proj_lon, u_lat, u_lon)
	dv = haversine_distance(proj_lat, proj_lon, v_lat, v_lon)
	return u if du <= dv else v


def normalize_bidirectional_path(path_idx, src_idx, dst_idx):
	"""
	Restore missing endpoints in Networkit's bidirectional path output.
	"""
	path_idx = list(path_idx or [])
	if not path_idx:
		return []
	if path_idx[0] != src_idx:
		path_idx.insert(0, src_idx)
	if path_idx[-1] != dst_idx:
		path_idx.append(dst_idx)
	return path_idx


def normalize_numpy_scalars(mapping):
	"""
	Convert numpy scalar-like values in a result mapping to plain Python floats.
	"""
	for key, value in list(mapping.items()):
		if isinstance(value, (np.generic, np.ndarray)):
			mapping[key] = float(value)
	return mapping
