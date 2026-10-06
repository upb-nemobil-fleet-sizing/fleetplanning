import copy
import os
import time
import pathlib
import math
import pickle
from collections import OrderedDict
from typing import Optional

import osmnx as ox
import networkit as nk
import numpy as np

from shapely.geometry import Polygon, Point, LineString
from shapely.strtree import STRtree

from .builder import (
	build_osm_graph_from_polygon,
	add_vehicle_profile_times,
	to_networkit_with_attr,
	build_ch,
	load_profiles,
	load_routing_config,
	compute_edge_time_for_profile,
	compute_edge_energy_for_profile,
	select_best_edge_for_weight,
	stable_value_key,
	build_od_matrix,
	build_od_numpy_matrices,
)

from .persistence import (
	save_graphml_metadata,
	load_graphml_metadata,
	save_nk_graph,
	load_nk_graph,
	save_ch,
	load_ch,
	save_matrix,
	load_matrix,
	load_numpy_matrix,
)
from .utils_rt import (
	file_stamp,
	haversine_distance,
	normalize_bidirectional_path,
	normalize_numpy_scalars,
	pick_closest_endpoint,
)

class Router:
	"""
	Unified Router interface.
	- area_polygon: shapely.geometry.Polygon
	- profile: string, e.g. "car" (used to pick edge weight attribute "<profile>_time")
	- use_ch: bool toggles Contraction Hierarchy usage globally
	- storage_dir: where to save/load preprocessed artifacts
	"""
	def __init__(
			self,
			area_polygon,
			profile: str = "cab",
			use_ch: bool = False,
			storage_dir: str = None,
			profiles: dict = None,
			profiles_path: str = None,
			routing_config: dict = None,
			routing_config_path: str = None,
			area_id: str = None,
			force_rebuild: bool=False
	):
		# area_polygon: a shapely Polygon, a raw (lon, lat) vertex sequence to build one from,
		# or None (only valid when a matching GraphML cache already exists - see _prepare_graph_and_index)
		if area_polygon is None or isinstance(area_polygon, Polygon):
			self.area_polygon = area_polygon
		else:
			self.area_polygon = Polygon(area_polygon)
		self.profile = profile
		self.use_ch = use_ch
		
		# rebuild graphs, even if cached files exist
		self.force_rebuild = force_rebuild
		
		# count number of calls and fallback Dijkstra calls during routing
		self.num_calls = 0
		self.num_calls_fallback = 0
		
		# Load profile definitions
		if profiles is not None:
			self.profiles = profiles
		else:
			self.profiles = load_profiles(profiles_path)

		if routing_config is not None:
			self.routing_config = routing_config
		else:
			self.routing_config = load_routing_config(routing_config_path)
		
		source_path = self.routing_config["build"]["source"]["osm_source_path"]
		self.osm_source_path = pathlib.Path(source_path) if source_path else None
		
		self.router_setting = {
			"map": area_id,
			"max_speed_mps": self.profiles[profile]['max_speed_mps'],
			"energy_per_wh_m": self.profiles[profile]['energy_per_wh_m']
		}
		
		# Default storage inside custom_sim/routing/data
		base_dir = pathlib.Path(__file__).resolve().parent  # custom_sim/routing/
		self.storage_dir = pathlib.Path(storage_dir) if storage_dir else base_dir / "data"
		self.storage_dir.mkdir(parents=True, exist_ok=True)

		self.graphml_path = self.storage_dir / (
			f"{self.router_setting['map']}"
			"_graph_area.graphml"
		)
		self.nk_path = self.storage_dir / (
			f"{self.router_setting['map']}"
			f"_{self.router_setting['max_speed_mps']:.2f}"
			f"_graph_area_{profile}_nk.pkl"
		)
		self.ch_path = self.storage_dir / (
			f"{self.router_setting['map']}"
			f"_{self.router_setting['max_speed_mps']:.2f}"
			f"_{self.router_setting['energy_per_wh_m']:.2f}"
			f"graph_area_{profile}_ch.bin"
		)

		od_cache_backend = self._od_cache_mode()
		if od_cache_backend == "pickle":
			self.od_matrix_path = self.storage_dir / (
				f"{self.router_setting['map']}"
				f"_{self.router_setting['max_speed_mps']:.2f}"
				f"_{self.router_setting['energy_per_wh_m']:.2f}"
				f"_od_matrix_{profile}.pkl"
			)
			self.pred_matrix_path = self.storage_dir / (
				f"{self.router_setting['map']}"
				f"_{self.router_setting['max_speed_mps']:.2f}"
				f"_{self.router_setting['energy_per_wh_m']:.2f}"
				f"_pred_matrix_{profile}.pkl"
			)
		else:
			self.od_matrix_path = self.storage_dir / (
				f"{self.router_setting['map']}"
				f"_{self.router_setting['max_speed_mps']:.2f}"
				f"_{self.router_setting['energy_per_wh_m']:.2f}"
				f"_od_numpy_dist_{profile}.npy"
			)
			self.pred_matrix_path = self.storage_dir / (
				f"{self.router_setting['map']}"
				f"_{self.router_setting['max_speed_mps']:.2f}"
				f"_{self.router_setting['energy_per_wh_m']:.2f}"
				f"_od_numpy_pred_{profile}.npy"
			)
		
		self.weight_attr  = f"{self.profile}_time"
		self.od_matrix = None
		self.pred_matrix = None
		self.route_result_cache = OrderedDict()
		self.route_result_cache_hits = 0
		self.route_result_cache_misses = 0
		
		# load or build
		self._prepare_graph_and_index()

	# ----------------------------------------------------------------------

	def _runtime_cfg(self) -> dict:
		return getattr(self, "routing_config", {}).get("runtime", {})

	# ----------------------------------------------------------------------

	def _routing_runtime_cfg(self) -> dict:
		return self._runtime_cfg().get("routing", {})

	# ----------------------------------------------------------------------

	def _od_runtime_cfg(self) -> dict:
		return self._runtime_cfg().get("od", {})

	# ----------------------------------------------------------------------

	def _od_cache_cfg(self) -> dict:
		return self._od_runtime_cfg().get("cache", {})

	# ----------------------------------------------------------------------

	def _route_result_cache_cfg(self) -> dict:
		return self._runtime_cfg().get("route_result_cache", {})

	# ----------------------------------------------------------------------

	def _route_result_cache_max_entries(self) -> int:
		cache_cfg = self._route_result_cache_cfg()
		value = cache_cfg.get("max_entries", 0)
		if value is None:
			return 0
		try:
			return max(0, int(value))
		except Exception:
			return 0

	# ----------------------------------------------------------------------

	def _use_route_result_cache(self) -> bool:
		max_entries = self._route_result_cache_max_entries()
		if max_entries <= 0:
			return False

		cache_cfg = self._route_result_cache_cfg()
		profile_overrides = cache_cfg.get("enabled_for", {})
		if isinstance(profile_overrides, dict) and self.profile in profile_overrides:
			return bool(profile_overrides[self.profile])

		return bool(cache_cfg.get("enabled", False))

	# ----------------------------------------------------------------------

	def _route_result_cache_key(self, orig, dest, precise_mode, use_od):
		return (
			float(orig[0]),
			float(orig[1]),
			float(dest[0]),
			float(dest[1]),
			precise_mode,
			bool(use_od),
		)

	# ----------------------------------------------------------------------

	def _get_cached_route_result(self, cache_key):
		cached = self.route_result_cache.get(cache_key)
		if cached is None:
			self.route_result_cache_misses += 1
			return None

		self.route_result_cache_hits += 1
		self.route_result_cache.move_to_end(cache_key)
		return copy.deepcopy(cached)

	# ----------------------------------------------------------------------

	def _store_cached_route_result(self, cache_key, result):
		cached_result = copy.deepcopy(result)
		cached_result.pop("query_time_s", None)
		self.route_result_cache[cache_key] = cached_result
		self.route_result_cache.move_to_end(cache_key)

		max_entries = self._route_result_cache_max_entries()
		while len(self.route_result_cache) > max_entries:
			self.route_result_cache.popitem(last=False)

	# ----------------------------------------------------------------------
	
	def _save_cache(self, cache_path, graphml_stamp):
		"""
		Save preprocessed graph and spatial index metadata.
		"""
		try:
			with open(cache_path, "wb") as f:
				pickle.dump({
					"G": self.G,
					"graphml_stamp": graphml_stamp,
				}, f, protocol=pickle.HIGHEST_PROTOCOL)
			print(f"[Router] Save cached graph topology -> '{cache_path.name}'")
		except Exception as e:
			print(f"[Router] Cache save failed: {e}")
	
	# ----------------------------------------------------------------------
	
	def _load_cache(self, cache_path, graphml_stamp):
		"""
		Load preprocessed graph and rebuild spatial index.
		"""
		if not os.path.exists(cache_path):
			return False
		try:
			with open(cache_path, "rb") as f:
				data = pickle.load(f)
			if (
				not isinstance(data, dict)
				or data.get("graphml_stamp") != graphml_stamp
				or "G" not in data
			):
				print(f"[Router] Skip stale topology cache '{cache_path.name}'")
				return False
			self.G = data["G"]
			print(f"[Router] loading cached graph topology from '{cache_path.name}'...")
			self._build_edge_spatial_index()
			return True
		except Exception as e:
			print(f"[Router] Cache load failed: {e}")
			return False
	
	# ----------------------------------------------------------------------
	
	def _build_edge_spatial_index(self):
		"""
		Build a Shapely STRtree for fast nearest-edge queries.
		Called once during initialization.
		"""
		self.edge_geoms = []
		self.edge_uvk = []

		# Keep the geometry/index arrays in a stable order so repeated rebuilds
		# of the same graph produce the same STRtree backing data.
		edge_items = sorted(
			self.G.edges(keys=True, data=True),
			key=lambda item: (
				stable_value_key(item[0]),
				stable_value_key(item[1]),
				stable_value_key(item[2]),
			),
		)

		for u, v, k, data in edge_items:
			geom = data.get("geometry")
			if geom is None:
				# Construct simple straight segment
				x1, y1 = self.G.nodes[u]["x"], self.G.nodes[u]["y"]
				x2, y2 = self.G.nodes[v]["x"], self.G.nodes[v]["y"]
				geom = LineString([(x1, y1), (x2, y2)])
			self.edge_geoms.append(geom)
			self.edge_uvk.append((u, v, k))

		# Build the spatial index
		self.edge_tree = STRtree(self.edge_geoms)
		print(f"[Router] Built STRtree with {len(self.edge_geoms)} edges.")

	# ----------------------------------------------------------------------

	def _build_edge_length_lookup(self):
		"""
		Precompute each multi-point edge's own real (geodesic) cumulative
		distance at every shape point, once per Router instance - in-memory
		only, not part of the persisted topology/networkit/OD caches, so it
		doesn't touch any of the cache-invalidation rules those rely on.

		_calc_proj_data needs "how far along this edge is the projected point"
		in real meters. Shapely's geom.project() measures distance over the
		geometry's raw (lon, lat) coordinates - i.e. in degrees, since this
		graph's CRS is unprojected WGS84 (confirmed via
		self.G.graph["crs"] == "epsg:4326") - so that value can't be used
		directly as meters (a real partial-edge distance of e.g. 45m would come
		out as ~0.0004). A single per-edge scaling factor (edge_length_m /
		geom.length) is exact for a straight edge, but assumes one uniform
		lon/lat mix for the whole edge, which is a poor approximation on real,
		strongly curved multi-point edges in this graph (measured up to ~259m
		off). This lookup instead makes the same real-meters number available
		for every
		edge exactly, via one haversine call per query on top of a per-edge
		precomputation that costs a few edges to run.

		Only covers edges with a real multi-point geometry (>2 coordinates) -
		for a straight two-point edge (or a synthetic one built for an edge
		with no stored geometry), a single line has one direction throughout,
		so degrees-to-meters scaling by the edge's own length_full/geom.length
		ratio is exact there, not an approximation, and is used directly in
		_edge_partial_length_m instead of needing an entry here.
		"""
		self.edge_cum_length_m = {}
		for u, v, k, data in self.G.edges(keys=True, data=True):
			geom = data.get("geometry")
			if geom is None:
				continue
			coords = list(geom.coords)
			if len(coords) <= 2:
				continue
			cum = [0.0]
			for i in range(len(coords) - 1):
				lon1, lat1 = coords[i]
				lon2, lat2 = coords[i + 1]
				cum.append(cum[-1] + haversine_distance(lat1, lon1, lat2, lon2))
			self.edge_cum_length_m[(u, v, k)] = (coords, cum)

	# ----------------------------------------------------------------------

	def _edge_partial_length_m(self, u, v, key, data, geom, proj):
		"""
		Real-world distance (meters) from this edge's own start (index 0 of
		its stored geometry) to the given projected point - see
		_build_edge_length_lookup for why this can't just be geom.project().
		"""
		cached = self.edge_cum_length_m.get((u, v, key))
		if cached is None:
			# Straight (<=2 point, or synthetic) edge: exact via a single
			# edge-wide degrees-to-meters ratio, since there's only one
			# direction over the whole edge to account for.
			geom_len = geom.length
			if geom_len <= 0:
				return 0.0
			return geom.project(proj) * (float(data["length"]) / geom_len)

		coords, cum = cached
		proj_lon, proj_lat = proj.x, proj.y

		# Find which segment the projection falls closest to - few segments
		# per real edge, so a linear scan is cheap (measured: ~4 microseconds
		# per lookup, ~0.2% of a real shortest_path() call).
		best_i, best_d2 = 0, None
		for i in range(len(coords) - 1):
			x1, y1 = coords[i]
			x2, y2 = coords[i + 1]
			dx, dy = x2 - x1, y2 - y1
			seg_len2 = dx * dx + dy * dy
			t = 0.0 if seg_len2 == 0 else max(0.0, min(1.0, ((proj_lon - x1) * dx + (proj_lat - y1) * dy) / seg_len2))
			px, py = x1 + t * dx, y1 + t * dy
			d2 = (proj_lon - px) ** 2 + (proj_lat - py) ** 2
			if best_d2 is None or d2 < best_d2:
				best_d2, best_i = d2, i

		seg_lon, seg_lat = coords[best_i]
		return cum[best_i] + haversine_distance(seg_lat, seg_lon, proj_lat, proj_lon)

	# ----------------------------------------------------------------------

	def _od_cache_mode(self) -> str:
		cache_cfg = self._od_cache_cfg()
		backend = cache_cfg.get("backend", "numpy")

		if backend not in {"pickle", "numpy"}:
			raise ValueError(f"Unsupported OD cache backend '{backend}'.")
		return backend

	# ----------------------------------------------------------------------

	def _od_cache_meta_path(self):
		return pathlib.Path(f"{self.pred_matrix_path}.meta.json")

	# ----------------------------------------------------------------------

	def _current_graphml_stamp(self):
		graphml_path = getattr(self, "graphml_path", None)
		if graphml_path is None or not os.path.exists(graphml_path):
			return None
		return file_stamp(graphml_path)

	# ----------------------------------------------------------------------

	def _od_cache_is_current(self, graphml_stamp):
		if graphml_stamp is None:
			return False
		metadata = load_graphml_metadata(self._od_cache_meta_path())
		return (
			isinstance(metadata, dict)
			and metadata.get("graphml_stamp") == graphml_stamp
		)

	# ----------------------------------------------------------------------

	def _ensure_od_matrix(self):
		"""Load or build the OD matrix on demand, with a simple size guard."""
		od_cache_backend = self._od_cache_mode()
		graphml_stamp = self._current_graphml_stamp()
		if self.pred_matrix is not None and self.od_matrix is not None:
			return

		node_count = self.nkG.numberOfNodes()
		max_od_nodes = self._od_runtime_cfg().get("max_nodes", 10000)
		if max_od_nodes is not None and node_count > max_od_nodes:
			raise RuntimeError(
				f"OD matrix requested for {node_count} nodes, which exceeds "
				f"runtime.od.max_nodes={max_od_nodes}."
			)

		dist_cache_exists = self.od_matrix_path is None or os.path.exists(self.od_matrix_path)
		pred_cache_exists = os.path.exists(self.pred_matrix_path)
		meta_current = self._od_cache_is_current(graphml_stamp)
		if not self.force_rebuild and dist_cache_exists and pred_cache_exists and meta_current:
			print(f"[Router] loading od matrix for profile '{self.profile}'...")
			if od_cache_backend == "pickle":
				self.od_matrix = load_matrix(self.od_matrix_path)
				self.pred_matrix = load_matrix(self.pred_matrix_path)
			else:
				self.od_matrix = load_numpy_matrix(self.od_matrix_path)
				self.pred_matrix = load_numpy_matrix(self.pred_matrix_path)
			if self.pred_matrix is not None and self.od_matrix is not None:
				return
			print(f"[Router] Skip incomplete OD cache for profile '{self.profile}'")
			self.od_matrix = None
			self.pred_matrix = None
		elif not self.force_rebuild and (dist_cache_exists or pred_cache_exists):
			print(f"[Router] Skip stale OD cache for profile '{self.profile}'")

		print(f"[Router] creating od matrix for profile '{self.profile}'...")
		od_start_time = time.perf_counter()
		if od_cache_backend == "pickle":
			self.od_matrix, self.pred_matrix = build_od_matrix(self.nkG)
			save_matrix(self.od_matrix, self.od_matrix_path)
			save_matrix(self.pred_matrix, self.pred_matrix_path)
		else:
			build_od_numpy_matrices(
				self.nkG,
				pred_path=self.pred_matrix_path,
				dist_path=self.od_matrix_path,
			)
			self.od_matrix = load_numpy_matrix(self.od_matrix_path)
			self.pred_matrix = load_numpy_matrix(self.pred_matrix_path)
		save_graphml_metadata(self._od_cache_meta_path(), {"graphml_stamp": graphml_stamp})
		print(f"[Router] od matrix build finished in {time.perf_counter() - od_start_time:.1f}s")
	
	# ----------------------------------------------------------------------

	def _use_od_by_default(self) -> bool:
		"""Resolve OD default from runtime config, with optional per-profile override."""
		od_cfg = self._od_runtime_cfg()
		profile_overrides = od_cfg.get("enabled_for", {})
		if isinstance(profile_overrides, dict) and self.profile in profile_overrides:
			return bool(profile_overrides[self.profile])
		return bool(od_cfg.get("enabled_by_default", False))
	
	# ----------------------------------------------------------------------
	
	def _prepare_graph_and_index(self):
		graphml_meta_path = self.storage_dir / (
			f"{self.router_setting['map']}"
			"_graph_area.graphml.meta.json"
		)
		graphml_cache_settings = {
			"build": self.routing_config["build"],
			"source_file_stamp": None,
		}
		if self.osm_source_path is not None:
			graphml_cache_settings["source_file_stamp"] = file_stamp(self.osm_source_path)
		graph_cfg = graphml_cache_settings["build"]["graph"]
		speed_model_cfg = graphml_cache_settings["build"]["speed_model"]
		profile_rules = self.profiles[self.profile]
		cache_path = self.storage_dir / (
			f"{self.router_setting['map']}"
			"_router_topology_cache.pkl"
		)
		graphml_meta = load_graphml_metadata(graphml_meta_path)

		# 1) Obtain an unprofiled graph plus its spatial index.
		# Reuse the existing GraphML only if it matches the current build settings.
		graphml_rebuilt = False
		if (
			not self.force_rebuild
			and os.path.exists(self.graphml_path)
			and graphml_meta == graphml_cache_settings
		):
			graphml_stamp = file_stamp(self.graphml_path)

			# Try the derived topology cache first. If it is stale or missing,
			# fall back to loading the current GraphML and rebuilding the cache.
			if not self._load_cache(cache_path, graphml_stamp):
				print(f"[Router] loading saved osmnx graph for map '{self.router_setting['map']}'...")
				self.G = ox.load_graphml(self.graphml_path)
				self._build_edge_spatial_index()
				self._save_cache(cache_path, graphml_stamp)
		else:
			if self.force_rebuild:
				print(f"[Router] building osmnx graph from polygon for map '{self.router_setting['map']}'...")
			elif os.path.exists(self.graphml_path):
				print(
					f"[Router] graphml '{self.graphml_path.name}' settings changed for map "
					f"'{self.router_setting['map']}'; rebuilding osmnx graph from polygon..."
				)
			else:
				print(
					f"[Router] graphml '{self.graphml_path.name}' missing for map "
					f"'{self.router_setting['map']}'; rebuilding osmnx graph from polygon..."
				)

			self.G = build_osm_graph_from_polygon(
				self.area_polygon,
				network_type=graph_cfg["network_type"],
				keep_only_largest_strong_component=graph_cfg["keep_only_largest_strong_component"],
				cap_explicit_speeds_with_practical_model=speed_model_cfg["cap_explicit_speeds_with_practical_model"],
				cap_explicit_speeds_with_strong_practical_tags=speed_model_cfg["cap_explicit_speeds_with_strong_practical_tags"],
				osm_source_path=self.osm_source_path,
			)
			ox.save_graphml(self.G, self.graphml_path)
			save_graphml_metadata(graphml_meta_path, graphml_cache_settings)
			graphml_stamp = file_stamp(self.graphml_path)
			self._build_edge_spatial_index()
			self._save_cache(cache_path, graphml_stamp)
			graphml_rebuilt = True

		# 2) Add profile-specific edge attributes on top of the unprofiled graph.
		self.G = add_vehicle_profile_times(self.G, self.profiles)
		
		# 3) Reuse or rebuild the profile-specific networkit graph.
		self.nkG = None
		nk_loaded_from_cache = False
		if ( not self.force_rebuild and not graphml_rebuilt and os.path.exists(self.nk_path) ):
			print(f"[Router] loading networkit graph for profile '{self.profile}'...")
			(
				self.nkG,
				self.node_map,
				self.nk_edge_choice,
				nk_graphml_stamp,
				nk_profile_name,
				nk_profile_rules,
			) = load_nk_graph(self.nk_path)
			if nk_graphml_stamp != graphml_stamp:
				print(f"[Router] Skip stale networkit cache '{self.nk_path.name}'")
				self.nkG = None
			elif nk_profile_name != self.profile or nk_profile_rules != profile_rules:
				print(f"[Router] Skip stale networkit profile cache '{self.nk_path.name}'")
				self.nkG = None
			elif self.nkG is None or self.node_map is None or self.nk_edge_choice is None:
				print(f"[Router] Skip incomplete networkit cache '{self.nk_path.name}'")
				self.nkG = None
			else:
				nk_loaded_from_cache = True

		if not nk_loaded_from_cache:
			print(f"[Router] converting to networkit graph for '{self.profile}'...")
			nk_start_time = time.perf_counter()
			self.nkG, self.node_map, self.nk_edge_choice = to_networkit_with_attr(self.G, self.weight_attr)
			save_nk_graph(
				self.nkG,
				self.node_map,
				self.nk_path,
				edge_choice=self.nk_edge_choice,
				graphml_stamp=graphml_stamp,
				profile_name=self.profile,
				profile_rules=profile_rules,
			)
			print(f"[Router] networkit conversion finished in {time.perf_counter() - nk_start_time:.1f}s")
		
		self.rev_node_map = {v: k for k, v in self.node_map.items()}
		
		if self._use_od_by_default():
			self._ensure_od_matrix()
		
		# 4) Legacy CH support. Current routing uses Networkit directly.
		if ( self.use_ch ):
			if ( not self.force_rebuild and nk_loaded_from_cache and os.path.exists(self.ch_path) ):
				print(f"[Router] loading CH for profile '{self.profile}'...")
				self.ch = load_ch(self.ch_path, self.nkG)
			else:
				print(f"[Router] building CH for '{self.profile}' (may take time)...")
				self.ch = build_ch(self.nkG)
				save_ch(self.ch, self.ch_path)
		else:
			self.ch = None

		# In-memory only (not part of any persisted cache) - geometry never
		# changes across the branches above, so one build covers all of them.
		self._build_edge_length_lookup()

		return
	
	# ----------------------------------------------------------------------
	
	def nearest_node(self, lat: float, lon: float):
		"""
		Snap a lat/lon to the nearest osmnx node id.
		The router's own queries do not call it. Each call builds a spatial index over
		all graph nodes, which takes tens of milliseconds on a city-sized graph.
		"""
		return ox.distance.nearest_nodes(self.G, X=lon, Y=lat)

	def snap(self, lat: float, lon: float) -> tuple:
		"""
		Project a coordinate onto the nearest edge.
		Returns ((proj_lat, proj_lon), offset_m): the point on the edge and its distance
		in meters from the coordinate.
		"""
		_, _, _, _, _, proj, _, _, offset_m = self._calc_proj_data(lat, lon)
		return (float(proj.y), float(proj.x)), offset_m

	# ----------------------------------------------------------------------
	
	def _calc_proj_data(self, la, lo):
		"""
		Calculate closest edge to coordinates and the projected point on the edge.
		The returned edge data is later reused for entry/exit partial time and
		energy, so it must come from the same concrete (u, v, k) edge we snapped to.
		"""
		# 1. Fast nearest-edge lookup via the prebuilt STRtree.
		u, v, key = self._nearest_edge(lo, la)
		data_all = self.G.get_edge_data(u, v)
		data = data_all.get(key, next(iter(data_all.values())))

		# 2. Project the query point onto that same concrete edge geometry.
		geom = data.get("geometry", LineString([
			(self.G.nodes[u]["x"], self.G.nodes[u]["y"]),
			(self.G.nodes[v]["x"], self.G.nodes[v]["y"]),
		]))
		pt = Point(lo, la)
		proj = geom.interpolate(geom.project(pt))
		# Distances along edge (m). geom.project(proj) directly would be in
		# degrees (Shapely has no notion of this graph's WGS84 CRS), not
		# meters - see _build_edge_length_lookup's docstring for why this
		# needs its own real-meters computation instead.
		length_full = data["length"]
		length_to_proj = self._edge_partial_length_m(u, v, key, data, geom, proj)
		# For visualization/debug, and for proj_offsets_m in the shortest_path()
		# result. Same degrees-vs-meters distinction as length_to_proj above:
		# pt.distance(proj) is a raw Shapely distance over this graph's
		# (lon, lat) degree coordinates, not meters, so it's computed via
		# haversine instead.
		off_dist = haversine_distance(pt.y, pt.x, proj.y, proj.x)

		return u, v, data, geom, pt, proj, length_full, length_to_proj, off_dist
	
	# ----------------------------------------------------------------------
	
	def _nearest_edge(self, lon, lat):
		"""
		Fast nearest-edge query using the prebuilt STRtree.
		Returns (u, v, key) for the edge whose geometry is nearest to (lon, lat).
		Compatible with both Shapely 1.x and 2.x APIs.
		"""
		if not hasattr(self, "edge_tree"):
			raise RuntimeError("Edge spatial index not built. Call _build_edge_spatial_index() first.")

		pt = Point(lon, lat)
		if hasattr(self.edge_tree, "query_nearest"):
			idx_raw, dist_raw = self.edge_tree.query_nearest(pt, return_distance=True)
			idxs = idx_raw.tolist()
			dists = dist_raw.tolist()

			candidates = []
			for raw_idx, raw_dist in zip(idxs, dists):
				idx = int(raw_idx)
				u, v, k = self.edge_uvk[idx]
				candidates.append((
					float(raw_dist),
					stable_value_key(u),
					stable_value_key(v),
					stable_value_key(k),
					idx,
				))

			*_, idx = min(candidates)
		else:
			nearest_geom = self.edge_tree.nearest(pt)

			# Shapely 2.x returns an integer index; Shapely 1.x returns the geometry object
			if isinstance(nearest_geom, (int, np.integer, np.int64, np.int32)):
				idx = int(nearest_geom)
			else:
				idx = self.edge_geoms.index(nearest_geom)

		u, v, k = self.edge_uvk[idx]
		return u, v, k
	
	# ----------------------------------------------------------------------

	def _partial_time(self, edge_data, partial_len_m: float) -> float:
		"""
		Compute time for a partial edge using the same logic as in builder.py,
		scaled by the partial length. This preserves the same profile-specific
		speed cap logic as the main route weights. No debug attributes are written.
		"""
		if partial_len_m <= 0:
			return 0.0
		rules = self.profiles.get(self.profile, {})
		full_time = compute_edge_time_for_profile(edge_data, rules, record_debug=False)
		if full_time is None:
			return math.inf

		full_len = edge_data.get("length", None)
		if not full_len or full_len <= 0:
			return math.inf

		return full_time * (partial_len_m / full_len)
	
	# ----------------------------------------------------------------------
	
	def _edge_length_m(self, u_osm, v_osm):
		"""
		Length of the canonical OSM edge for this directed pair.
		"""
		ed, d = self._resolved_edge_data(u_osm, v_osm)
		if d is None:
			return 0.0
		return self._edge_length_from_data(u_osm, v_osm, d)
	
	# ----------------------------------------------------------------------
	
	def _edge_energy_wh(self, u, v) -> float:
		"""
		Compute precomputed or approximate energy use for an edge between a,b.
		"""
		ed, d = self._resolved_edge_data(u, v)
		return self._edge_energy_wh_from_data(d, ed)

	# ----------------------------------------------------------------------

	def _edge_length_m_and_energy_wh(self, u_osm, v_osm):
		"""
		Resolve the selected OSM edge once and reuse it for both metrics.
		"""
		ed, d = self._resolved_edge_data(u_osm, v_osm)
		if d is None:
			return 0.0, 0.0
		return (
			self._edge_length_from_data(u_osm, v_osm, d),
			self._edge_energy_wh_from_data(d, ed),
		)

	# ----------------------------------------------------------------------

	def _resolved_edge_data(self, u_osm, v_osm, edge_data=None):
		"""
		Resolve the concrete OSM edge record that should represent this directed
		pair for distance/energy reconstruction.
		"""
		if edge_data is None:
			edge_data = self.G.get_edge_data(u_osm, v_osm, default=None)
		if not edge_data:
			return None, None

		_, selected_data = self._selected_edge_data(u_osm, v_osm, edge_data)
		if selected_data is None:
			selected_data = next(iter(edge_data.values()))

		return edge_data, selected_data

	# ----------------------------------------------------------------------

	def _edge_length_from_data(self, u_osm, v_osm, edge_data):
		if "length" in edge_data:  # usual case
			try:
				return float(edge_data["length"])
			except Exception:
				pass
		if "geometry" in edge_data:
			return float(edge_data["geometry"].length)
		# crude fallback
		x1, y1 = self.G.nodes[u_osm]["x"], self.G.nodes[u_osm]["y"]
		x2, y2 = self.G.nodes[v_osm]["x"], self.G.nodes[v_osm]["y"]
		return ((x2-x1)**2 + (y2-y1)**2)**0.5 * 111_000.0

	# ----------------------------------------------------------------------

	def _edge_energy_wh_from_data(self, selected_data, edge_data) -> float:
		if selected_data is not None:
			val = selected_data.get(f"{self.profile}_energy_wh")
			if val is not None:
				return float(val)

		if edge_data:
			for data in edge_data.values():
				val = data.get(f"{self.profile}_energy_wh")
				if val is not None:
					return float(val)

		return 0.0

	# ----------------------------------------------------------------------
	
	def _selected_edge_data(self, u_osm, v_osm, edge_data=None):
		"""
		Pick the same parallel edge key that the Networkit graph used for this
		directed pair, falling back to the builder's deterministic selector only
		if that cached choice is unavailable.
		"""
		if edge_data is None:
			edge_data = self.G.get_edge_data(u_osm, v_osm, default=None)
		if not edge_data:
			return None, None
		
		selected_key = getattr(self, "nk_edge_choice", {}).get((u_osm, v_osm))
		if selected_key in edge_data:
			return selected_key, edge_data[selected_key]
		
		selected_key, selected_data, _ = select_best_edge_for_weight(edge_data, self.weight_attr)
		return selected_key, selected_data
	
	# ----------------------------------------------------------------------
	
	def _partial_energy(self, edge_data, partial_len_m: float) -> float:
		"""
		Compute energy for a partial edge using the same logic as in builder.py,
		scaled by the partial length. No debug attributes are written.
		"""
		if partial_len_m <= 0:
			return 0.0

		rules = self.profiles.get(self.profile, {})
		full_energy = compute_edge_energy_for_profile(edge_data, rules, record_debug=False)
		if full_energy is None:
			return 0.0

		full_len = edge_data.get("length", None)
		if not full_len or full_len <= 0:
			return 0.0

		return full_energy * (partial_len_m / full_len)
	
	# ----------------------------------------------------------------------
	
	def _reconstruct_osm_path_and_distance(self, path_idx):
		# Networkit gives us the routed node sequence and total time weight; the
		# geometric distance and energy are reconstructed afterward on the chosen
		# OSM edges for those node pairs.
		path_osm = [self.rev_node_map[i] for i in path_idx]
		dist = 0.0
		energy = 0.0
		for a, b in zip(path_osm[:-1], path_osm[1:]):
			length_m, energy_wh = self._edge_length_m_and_energy_wh(a, b)
			dist += length_m
			energy += energy_wh
		
		return path_osm, dist, energy

	# ----------------------------------------------------------------------

	def _reconstruct_path_pred(self, source, target):
		path = []
		pred_matrix = self.pred_matrix
		if source == target:
			return [source]

		row = pred_matrix[source]
		if row[target] == -1:
			return None

		current = target
		while current != source:
			path.append(current)
			current = row[current]
			if current == -1:
				return None

		path.append(source)
		path.reverse()
		return path

	# ----------------------------------------------------------------------

	def _route_nodes(self, src_osm, dst_osm, weight_attr, use_od=None):
		s_idx = self.node_map[src_osm]
		t_idx = self.node_map[dst_osm]
		
		if ( s_idx == t_idx):
			return [src_osm], 0.0, 0.0, 0.0
		
		if use_od is None:
			use_od = self._use_od_by_default()
		
		if use_od:
			self._ensure_od_matrix()
			path_idx = self._reconstruct_path_pred(s_idx, t_idx)
			if path_idx is None:
				raise RuntimeError("No path between nodes")
			dist_time = float(self.od_matrix[s_idx][t_idx])
			if math.isinf(dist_time):
				raise RuntimeError("No path between nodes")
		else:
			self.num_calls += 1
			dj = nk.distance.BidirectionalDijkstra(self.nkG, s_idx, t_idx, storePred=True)
			dj.run()
			# This is the profile-specific route time from the collapsed Networkit
			# graph; distance and energy are reconstructed separately below.
			dist_time = dj.getDistance()
			
			if dist_time == float("inf"):
				raise RuntimeError("No path between nodes")
			
			# Networkit 11.1's BidirectionalDijkstra.getPath() consistently returned
			# only the interior nodes in our local tests, while the computed distance
			# itself was correct. We therefore restore missing endpoints here and still
			# fall back to plain Dijkstra when the returned path is completely empty.
			path_idx = normalize_bidirectional_path(dj.getPath(), s_idx, t_idx)
			if not path_idx:
				# Bidirectional Dijkstra can occasionally fail to reconstruct the path
				# even when the computed route distance itself is valid.
				self.num_calls_fallback += 1
				dj2 = nk.distance.Dijkstra(self.nkG, s_idx, storePaths=True)
				dj2.run()
				dist_time = dj2.distance(t_idx)
				if dist_time == float("inf"):
					raise RuntimeError("No path between nodes (fallback)")
				
				path_idx = dj2.getPath(t_idx)
				if not path_idx:
					raise RuntimeError("Even fallback Dijkstra failed to produce a path.")
		
		path_osm, dist_m, energy_wh = self._reconstruct_osm_path_and_distance(path_idx)
		return path_osm, dist_m, float(dist_time), energy_wh
	
	# ----------------------------------------------------------------------
	def _trivial_route_result(self, coord: tuple, precise_mode: str):
		lat, lon = coord
		# same nearest-edge projection a real query uses
		(proj_lat, proj_lon), offset_m = self.snap(lat, lon)

		return {
			"path_osm_nodes": [],
			"path_lat_lon": [],
			"time_s": 0,
			"distance_m": 0.0,
			"energy_wh": 0.0,
			"entry_energy_wh": 0.0,
			"exit_energy_wh": 0.0,
			"entry_time_s": 0.0,
			"exit_time_s": 0.0,
			"combo": "same_point",
			"query_time_s": 0.0,
			"proj_orig": (proj_lat, proj_lon),
			"proj_dest": (proj_lat, proj_lon),
			"proj_offsets_m": {"orig": offset_m, "dest": offset_m},
			"projection_lines": [],
			"precise_mode": precise_mode,
		}
	
	# ----------------------------------------------------------------------
	
	def shortest_path(
		self,
		orig: tuple,
		dest: tuple,
		precise_mode: Optional[str] = None,
		use_od: Optional[bool] = None,
		eps: float = 1e-9,
	):
		"""
		Compute shortest path between two (lat, lon) coordinates.
		
		Parameters
		----------
		orig, dest : tuple(float, float)
			Coordinates as (lat, lon)
		precise_mode : str
			"fast"  -> uses closer node on start/end edge (1 route call)
			"exact" -> evaluates all 4 edge-endpoint combinations (4 route calls)
		use_od : bool or None
			None -> use config default, True -> force OD, False -> force live routing
		
		Returns
		-------
		dict with keys:
			'time_s', 'distance_m', 'path_osm_nodes', 'query_time_s',
			'proj_orig', 'proj_dest', 'proj_offsets_m', 'projection_lines'
		"""
		
		if precise_mode is None:
			precise_mode = self._routing_runtime_cfg().get("default_precise_mode", "fast")

		# in case the points are identical (or extremely close to each other)
		if ( (abs(orig[0] - dest[0]) < eps) and (abs(orig[1] - dest[1]) < eps) ):
			return self._trivial_route_result(orig, precise_mode)
		
		start_time = time.time()
		if use_od is None:
			use_od = self._use_od_by_default()

		cache_key = None
		if self._use_route_result_cache():
			cache_key = self._route_result_cache_key(orig, dest, precise_mode, use_od)
			cached_result = self._get_cached_route_result(cache_key)
			if cached_result is not None:
				cached_result["query_time_s"] = time.time() - start_time
				return normalize_numpy_scalars(cached_result)

		weight_attr = self.weight_attr
		
		lat_o, lon_o = orig
		lat_d, lon_d = dest
		
		# ------------------------------------------------------------------
		# 1. Find nearest edges for both origin and destination
		# ------------------------------------------------------------------
		u_o, v_o, data_o, geom_o, pt_o, proj_o, length_full_o, length_to_proj_o, off_dist_o = self._calc_proj_data (lat_o, lon_o)
		u_d, v_d, data_d, geom_d, pt_d, proj_d, length_full_d, length_to_proj_d, off_dist_d = self._calc_proj_data (lat_d, lon_d)
		
		length_from_proj_o = length_full_o - length_to_proj_o
		length_from_proj_d = length_full_d - length_to_proj_d

		# ------------------------------------------------------------------
		# 2. Determine candidate endpoints
		# ------------------------------------------------------------------
		if precise_mode == "exact":
			candidates = [
				("u_u", u_o, u_d,
				length_to_proj_o,            # entry from proj_o -> u_o
				length_to_proj_d),           # exit   from u_d  -> proj_d
				("u_v", u_o, v_d,
				length_to_proj_o,
				length_from_proj_d),         # exit via v_d
				("v_u", v_o, u_d,
				length_from_proj_o,          # entry via v_o
				length_to_proj_d),
				("v_v", v_o, v_d,
				length_from_proj_o,
				length_from_proj_d),
			]
		else:
			# pick closest node to each projection
			# pick_closest_endpoint() uses haversine distance; at these short
			# distances a Euclidean comparison would likely also be sufficient if
			# we ever wanted the cheaper approximation instead.
			start_node = pick_closest_endpoint(
				u_o,
				v_o,
				float(proj_o.y),
				float(proj_o.x),
				float(self.G.nodes[u_o]["y"]),
				float(self.G.nodes[u_o]["x"]),
				float(self.G.nodes[v_o]["y"]),
				float(self.G.nodes[v_o]["x"]),
			)
			end_node = pick_closest_endpoint(
				u_d,
				v_d,
				float(proj_d.y),
				float(proj_d.x),
				float(self.G.nodes[u_d]["y"]),
				float(self.G.nodes[u_d]["x"]),
				float(self.G.nodes[v_d]["y"]),
				float(self.G.nodes[v_d]["x"]),
			)
			entry_len = length_to_proj_o if start_node == u_o else length_from_proj_o
			exit_len  = length_to_proj_d if end_node == u_d else length_from_proj_d
			candidates = [("fast", start_node, end_node, entry_len, exit_len)]
		
		# ------------------------------------------------------------------
		# 3. Compute shortest paths and pick best
		# ------------------------------------------------------------------
		best_result = None

		for (tag, s_node, t_node, entry_len_m, exit_len_m) in candidates:
			try:
				path_nodes, dist_m, time_s, energy_wh = self._route_nodes(
					s_node,
					t_node,
					weight_attr,
					use_od=use_od,
				)
				path_lat_lon = [(self.G.nodes[node]["y"], self.G.nodes[node]["x"]) for node in path_nodes]
			except Exception as e:
				print("[Router] Problem in self._route_nodes():",e)
				continue
			# Dormant debug helper: this was used to inspect whether fast-mode
			# endpoint choice can make us add partial snapped-edge cost on top of
			# a route that immediately traverses the same start/end edge again.
			## --- DEBUG: detect double counting of start/end partial edges ---
			#if len(path_nodes) >= 2:
				#first_pair = {path_nodes[0], path_nodes[1]}
				#last_pair  = {path_nodes[-2], path_nodes[-1]}

				#start_edge = {u_o, v_o}
				#end_edge   = {u_d, v_d}

				#if first_pair == start_edge and entry_len_m > 0:
					#print(f"[DEBUG overlap] start: combo={tag} s_node={s_node} "
						#f"entry_len_m={entry_len_m:.3f} first_edge={tuple(path_nodes[:2])} "
						#f"proj_edge=({u_o},{v_o})")

				#if last_pair == end_edge and exit_len_m > 0:
					#print(f"[DEBUG overlap] end: combo={tag} t_node={t_node} "
						#f"exit_len_m={exit_len_m:.3f} last_edge={tuple(path_nodes[-2:])} "
						#f"proj_edge=({u_d},{v_d})")
			## --- end DEBUG ---
			entry_time = self._partial_time(data_o, entry_len_m)
			exit_time  = self._partial_time(data_d, exit_len_m)

			# The graph route covers node-to-node travel only. Snapping to projected
			# points adds partial edge costs at the origin and destination.
			total_time = time_s + entry_time + exit_time
			total_dist = dist_m + entry_len_m + exit_len_m
			
			entry_energy = self._partial_energy(data_o, entry_len_m)
			exit_energy  = self._partial_energy(data_d, exit_len_m)
			total_energy = energy_wh + entry_energy + exit_energy

			if (best_result is None) or total_time < best_result["time_s"]:
				best_result = {
					"path_osm_nodes": path_nodes,
					"path_lat_lon": path_lat_lon,
					"time_s": math.ceil(total_time),
					"distance_m": total_dist,
					"energy_wh": total_energy,
					"entry_energy_wh": entry_energy,
					"exit_energy_wh": exit_energy,
					"entry_time_s": entry_time,
					"exit_time_s": exit_time,
					"combo": tag,
				}
		
		if best_result is None:
			print("[Router] problem_orig", orig)
			print("[Router] problem_dest", dest)
			raise RuntimeError("No valid route found")
		
		query_time_s = time.time() - start_time

		# ------------------------------------------------------------------
		# 4. Pack results
		# ------------------------------------------------------------------
		res = {
			**best_result,
			"query_time_s": query_time_s,
			"proj_orig": (proj_o.y, proj_o.x),
			"proj_dest": (proj_d.y, proj_d.x),
			"proj_offsets_m": {"orig": off_dist_o, "dest": off_dist_d},
			"projection_lines": [
				[(lat_o, lon_o), (proj_o.y, proj_o.x)],
				[(lat_d, lon_d), (proj_d.y, proj_d.x)]
			],
			"precise_mode": precise_mode,
		}
		if cache_key is not None:
			self._store_cached_route_result(cache_key, res)
		# --- normalize all float-like values ---
		return normalize_numpy_scalars(res)

	# ----------------------------------------------------------------------

	def position_at_fraction(self, start: tuple, end: tuple, frac: float) -> tuple:
		"""
		Point along the routed path from start to end at frac (0..1) of elapsed time,
		weighted by self.weight_attr per edge instead of raw distance.
		Uses path_lat_lon (graph nodes), not full edge geometry - coarse but cheap.
		"""
		res = self.shortest_path(start, end)
		path_nodes = res.get("path_osm_nodes") or []
		points = list(res.get("path_lat_lon") or [])
		if len(points) < 2:
			return end if frac >= 1.0 else start
		frac = max(0.0, min(1.0, frac))
		points[0] = start  # anchor to the entry's exact endpoints, not the snapped nodes
		points[-1] = end

		cum = [0.0]
		for u, v in zip(path_nodes[:-1], path_nodes[1:]):
			edge_variants = self.G.get_edge_data(u, v) or {}
			_, _, weight = select_best_edge_for_weight(edge_variants, self.weight_attr)
			cum.append(cum[-1] + float(weight or 0.0))

		total = cum[-1]
		if total < 1e-9:
			idx = round(frac * (len(points) - 1))
			return points[idx]

		target = frac * total
		for i in range(1, len(cum)):
			if target <= cum[i]:
				r = (target - cum[i - 1]) / max(1e-9, cum[i] - cum[i - 1])
				lat0, lon0 = points[i - 1]
				lat1, lon1 = points[i]
				return (lat0 + r * (lat1 - lat0), lon0 + r * (lon1 - lon0))
		return points[-1]

	def route_segment_geometry(self, path_osm_nodes) -> list:
		"""
		Per-hop info along an OSM node path: resolved (lat, lon) coordinates -
		real edge geometry if the edge has one, else a straight line between the
		two nodes - and that edge's own attribute dict, picking the same parallel
		edge routing itself used for the hop (same resolution order as
		shortest_path/position_at_fraction: nk_edge_choice, then
		select_best_edge_for_weight, then any edge).

		Returns a list of (lat_list, lon_list, edge_data) tuples, one per hop;
		hops with no edge data at all are skipped.
		"""
		segments = []
		for u, v in zip(path_osm_nodes[:-1], path_osm_nodes[1:]):
			edge_data = self.G.get_edge_data(u, v)
			if not edge_data:
				continue
			selected_key = getattr(self, "nk_edge_choice", {}).get((u, v))
			if selected_key in edge_data:
				data = edge_data[selected_key]
			else:
				_, data, _ = select_best_edge_for_weight(edge_data, self.weight_attr)
				if data is None:
					data = next(iter(edge_data.values()))
			geom = data.get("geometry")
			if geom is not None:
				xs, ys = geom.xy
				lat, lon = list(ys), list(xs)
			else:
				lat = [self.G.nodes[u]["y"], self.G.nodes[v]["y"]]
				lon = [self.G.nodes[u]["x"], self.G.nodes[v]["x"]]
			segments.append((lat, lon, data))
		return segments
