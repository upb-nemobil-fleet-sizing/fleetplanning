import copy
import pickle
import random
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import networkit as nk
import networkx as nx
import numpy as np
import osmnx as ox
import pytest

from shapely.geometry import LineString, Point, Polygon
from shapely.strtree import STRtree

import custom_sim.routing.builder as builder_module
import custom_sim.routing.router as router_module

from custom_sim.routing.builder import (
	_track_overpass_request_stats,
	add_edge_speeds_from_tags,
	add_vehicle_profile_times,
	compute_edge_time_for_profile,
	load_routing_config,
	to_networkit_with_attr,
)
from custom_sim.routing.persistence import (
	create_numpy_matrix,
	load_matrix,
	load_graphml_metadata,
	load_nk_graph,
	save_matrix,
	save_graphml_metadata,
	save_nk_graph,
)
from custom_sim.routing.router import Router
from custom_sim.routing.utils_rt import file_stamp, normalize_bidirectional_path
from utils import read_request_json


def _build_parallel_graph(reverse_order: bool):
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)

	edges = [
		(1, 2, 0, {"cab_time": 10.0, "length": 100.0, "cab_energy_wh": 10.0}),
		(1, 2, 1, {"cab_time": 5.0, "length": 120.0, "cab_energy_wh": 12.0}),
	]
	if reverse_order:
		edges.reverse()

	for u, v, k, data in edges:
		G.add_edge(u, v, key=k, **data)

	return G


def _build_cache_graph(extra_edge: bool = False):
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, length=100.0, speed_kph=36.0, highway="residential")

	if extra_edge:
		G.add_node(3, x=2.0, y=0.0)
		G.add_edge(2, 3, key=0, length=80.0, speed_kph=30.0, highway="service")

	return G


def _build_router_stub(tmp_path):
	router = Router.__new__(Router)
	router.profile = "cab"
	router.use_ch = False
	router.force_rebuild = False
	router.area_polygon = None
	router.routing_config = load_routing_config()
	router.routing_config["build"]["source"]["osm_source_path"] = None
	router.routing_config["runtime"]["od"]["enabled_by_default"] = False
	router.routing_config["runtime"]["od"]["enabled_for"] = {}
	router.routing_config["runtime"]["route_result_cache"]["enabled"] = False
	router.routing_config["runtime"]["route_result_cache"]["enabled_for"] = {}
	router.osm_source_path = None
	router.profiles = {
		"cab": {
			"max_speed_mps": 10.0,
			"energy_per_wh_m": 1.0,
		}
	}
	router.router_setting = {
		"map": "cache_test",
		"max_speed_mps": 10.0,
		"energy_per_wh_m": 1.0,
	}
	router.storage_dir = tmp_path
	router.graphml_path = tmp_path / "cache_test_graph_area.graphml"
	router.nk_path = tmp_path / "cache_test_10.00_graph_area_cab_nk.pkl"
	router.ch_path = tmp_path / "cache_test_10.00_1.00_graph_area_cab_ch.bin"
	router.weight_attr = "cab_time"
	router.num_calls = 0
	router.num_calls_fallback = 0
	router.route_result_cache = OrderedDict()
	router.route_result_cache_hits = 0
	router.route_result_cache_misses = 0
	return router


def _topology_cache_path(router):
	return router.storage_dir / f"{router.router_setting['map']}_router_topology_cache.pkl"


def _graphml_meta_path(router):
	return router.storage_dir / f"{router.router_setting['map']}_graph_area.graphml.meta.json"


def _graphml_metadata(router):
	return {
		"build": router.routing_config["build"],
		"source_file_stamp": None,
	}


def _save_router_caches(router, G):
	ox.save_graphml(G, router.graphml_path)
	save_graphml_metadata(
		_graphml_meta_path(router),
		_graphml_metadata(router),
	)
	graphml_stamp = file_stamp(router.graphml_path)
	router.G = G
	router._save_cache(_topology_cache_path(router), graphml_stamp)

	G_profiled = add_vehicle_profile_times(G.copy(), copy.deepcopy(router.profiles))
	nkG, node_map, edge_choice = to_networkit_with_attr(G_profiled, router.weight_attr)
	save_nk_graph(
		nkG,
		node_map,
		router.nk_path,
		edge_choice=edge_choice,
		graphml_stamp=graphml_stamp,
		profile_name=router.profile,
		profile_rules=router.profiles[router.profile],
	)
	return graphml_stamp, edge_choice


def _build_real_router(tmp_path, graphml_src, area_id, max_speed_mps=8.33, energy_per_wh_m=0.1):
	graphml_src = Path(graphml_src)
	if not graphml_src.is_absolute():
		graphml_src = Path(__file__).resolve().parents[2] / graphml_src
	if not graphml_src.exists():
		pytest.skip(f"missing graphml fixture: {graphml_src}")

	default_routing_config = load_routing_config()
	default_routing_config["build"]["source"]["osm_source_path"] = None
	default_routing_config["runtime"]["od"]["enabled_by_default"] = False
	default_routing_config["runtime"]["od"]["enabled_for"] = {}
	default_routing_config["runtime"]["route_result_cache"]["enabled"] = False
	default_routing_config["runtime"]["route_result_cache"]["enabled_for"] = {}
	graphml_path = tmp_path / f"{area_id}_graph_area.graphml"
	shutil.copy2(graphml_src, graphml_path)
	save_graphml_metadata(
		tmp_path / f"{area_id}_graph_area.graphml.meta.json",
		{
			"build": default_routing_config["build"],
			"source_file_stamp": None,
		},
	)
	return Router(
		area_polygon=None,
		profile="cab",
		use_ch=False,
		storage_dir=tmp_path,
		routing_config=default_routing_config,
		profiles={
			"cab": {
				"max_speed_mps": max_speed_mps,
				"energy_per_wh_m": energy_per_wh_m,
			}
		},
		area_id=area_id,
		force_rebuild=False,
	)


def test_load_routing_config_exposes_expected_default_choices():
	config = load_routing_config()

	assert config["build"]["graph"]["network_type"] == "drive_service"
	assert config["build"]["graph"]["keep_only_largest_strong_component"] is True
	source_path = config["build"]["source"]["osm_source_path"]
	if source_path is not None:
		assert Path(source_path).is_absolute()
	assert config["build"]["speed_model"]["cap_explicit_speeds_with_practical_model"] is False
	assert config["build"]["speed_model"]["cap_explicit_speeds_with_strong_practical_tags"] is True
	assert config["runtime"]["routing"]["default_precise_mode"] == "fast"
	assert isinstance(config["runtime"]["od"]["enabled_by_default"], bool)
	assert isinstance(config["runtime"]["od"].get("enabled_for", {}), dict)
	assert config["runtime"]["od"]["max_nodes"] is None or isinstance(config["runtime"]["od"]["max_nodes"], int)
	assert config["runtime"]["od"]["cache"]["backend"] in {"pickle", "numpy"}
	assert isinstance(config["runtime"]["route_result_cache"]["enabled"], bool)
	assert isinstance(config["runtime"]["route_result_cache"]["max_entries"], int)
	assert isinstance(config["runtime"]["route_result_cache"].get("enabled_for", {}), dict)


def test_ensure_od_matrix_refuses_graphs_above_configured_node_limit(tmp_path):
	router = Router.__new__(Router)
	router.profile = "cab"
	router.force_rebuild = False
	router.routing_config = {
		"build": {},
		"runtime": {"od": {"max_nodes": 5}},
	}
	router.od_matrix = None
	router.pred_matrix = None
	router.od_matrix_path = tmp_path / "od.pkl"
	router.pred_matrix_path = tmp_path / "pred.pkl"
	router.nkG = SimpleNamespace(numberOfNodes=lambda: 6)

	with pytest.raises(RuntimeError, match="runtime\\.od\\.max_nodes=5"):
		router._ensure_od_matrix()


def test_ensure_od_matrix_rebuilds_stale_pickle_cache(tmp_path, monkeypatch):
	router = Router.__new__(Router)
	router.profile = "cab"
	router.force_rebuild = False
	router.routing_config = {
		"build": {},
		"runtime": {
			"od": {
				"max_nodes": 5,
				"cache": {
					"backend": "pickle",
				},
			},
		},
	}
	router.od_matrix = None
	router.pred_matrix = None
	router.graphml_path = tmp_path / "graph.graphml"
	router.graphml_path.write_text("graph-a", encoding="utf-8")
	router.od_matrix_path = tmp_path / "od.pkl"
	router.pred_matrix_path = tmp_path / "pred.pkl"
	router.nkG = SimpleNamespace(numberOfNodes=lambda: 2)

	save_matrix([[99.0, 99.0]], router.od_matrix_path)
	save_matrix([[-1, -1]], router.pred_matrix_path)
	save_graphml_metadata(router._od_cache_meta_path(), {"graphml_stamp": "old-graph"})

	rebuilt_od = [[0.0, 1.0], [2.0, 0.0]]
	rebuilt_pred = [[-1, 0], [-1, -1]]
	monkeypatch.setattr(router_module, "build_od_matrix", lambda nkG: (rebuilt_od, rebuilt_pred))

	router._ensure_od_matrix()

	assert router.od_matrix == rebuilt_od
	assert router.pred_matrix == rebuilt_pred
	assert load_matrix(router.od_matrix_path) == rebuilt_od
	assert load_matrix(router.pred_matrix_path) == rebuilt_pred
	assert load_graphml_metadata(router._od_cache_meta_path()) == {
		"graphml_stamp": file_stamp(router.graphml_path)
	}


def test_ensure_od_matrix_rebuilds_stale_numpy_cache(tmp_path, monkeypatch):
	router = Router.__new__(Router)
	router.profile = "cab"
	router.force_rebuild = False
	router.routing_config = {
		"build": {},
		"runtime": {
			"od": {
				"max_nodes": 5,
				"cache": {
					"backend": "numpy",
				},
			},
		},
	}
	router.od_matrix = None
	router.pred_matrix = None
	router.graphml_path = tmp_path / "graph.graphml"
	router.graphml_path.write_text("graph-a", encoding="utf-8")
	router.od_matrix_path = tmp_path / "od.npy"
	router.pred_matrix_path = tmp_path / "pred.npy"
	router.nkG = SimpleNamespace(numberOfNodes=lambda: 2)

	stale_od = create_numpy_matrix(router.od_matrix_path, np.float64, (2, 2))
	stale_od[:] = np.array([[9.0, 9.0], [9.0, 9.0]], dtype=np.float64)
	stale_od.flush()
	stale_pred = create_numpy_matrix(router.pred_matrix_path, np.int32, (2, 2))
	stale_pred[:] = np.array([[-1, -1], [-1, -1]], dtype=np.int32)
	stale_pred.flush()
	save_graphml_metadata(router._od_cache_meta_path(), {"graphml_stamp": "old-graph"})

	def fake_build_od_numpy_matrices(nkG, pred_path, dist_path=None):
		pred = create_numpy_matrix(pred_path, np.int32, (2, 2))
		pred[:] = np.array([[-1, 0], [-1, -1]], dtype=np.int32)
		pred.flush()
		if dist_path is not None:
			dist = create_numpy_matrix(dist_path, np.float64, (2, 2))
			dist[:] = np.array([[0.0, 1.0], [2.0, 0.0]], dtype=np.float64)
			dist.flush()

	monkeypatch.setattr(router_module, "build_od_numpy_matrices", fake_build_od_numpy_matrices)

	router._ensure_od_matrix()

	assert router.od_matrix[0, 1] == 1.0
	assert router.pred_matrix[0, 1] == 0
	assert load_graphml_metadata(router._od_cache_meta_path()) == {
		"graphml_stamp": file_stamp(router.graphml_path)
	}


def test_use_od_by_default_prefers_profile_override():
	router = Router.__new__(Router)
	router.profile = "pro"
	router.routing_config = {
		"build": {},
		"runtime": {
			"od": {
				"enabled_by_default": True,
				"enabled_for": {"pro": False},
			},
		},
	}

	assert router._use_od_by_default() is False


def test_use_route_result_cache_prefers_profile_override():
	router = Router.__new__(Router)
	router.profile = "cab"
	router.routing_config = {
		"build": {},
		"runtime": {
			"route_result_cache": {
				"enabled": False,
				"max_entries": 5,
				"enabled_for": {"cab": True, "pro": False},
			},
		},
	}

	assert router._use_route_result_cache() is True


def test_od_cache_mode_rejects_unknown_backend():
	router = Router.__new__(Router)
	router.routing_config = {
		"build": {},
		"runtime": {
			"od": {
				"cache": {
					"backend": "invalid",
				},
			},
		},
	}

	with pytest.raises(ValueError, match="Unsupported OD cache backend 'invalid'"):
		router._od_cache_mode()


def test_build_osm_graph_from_polygon_uses_file_source_when_passed(tmp_path, monkeypatch):
	source_path = tmp_path / "source.osm"
	source_path.write_text("<osm />", encoding="utf-8")
	polygon = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])

	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, length=100.0)

	calls = {}

	def fake_file_loader(polygon_arg, source_path_arg, network_type_arg):
		calls["polygon"] = polygon_arg
		calls["source_path"] = source_path_arg
		calls["network_type"] = network_type_arg
		return G

	monkeypatch.setattr(builder_module, "_build_osm_graph_from_source_file", fake_file_loader)
	monkeypatch.setattr(
		builder_module.ox,
		"graph_from_polygon",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected Overpass path")),
	)
	monkeypatch.setattr(builder_module, "add_edge_speeds_from_tags", lambda graph, **kwargs: graph)
	monkeypatch.setattr(builder_module.ox, "add_edge_travel_times", lambda graph: graph)

	res = builder_module.build_osm_graph_from_polygon(
		polygon,
		network_type="drive_service",
		keep_only_largest_strong_component=False,
		osm_source_path=source_path,
	)

	assert calls["polygon"] == polygon
	assert calls["source_path"] == source_path
	assert calls["network_type"] == "drive_service"
	assert len(res.nodes()) == len(G.nodes())
	assert len(res.edges()) == len(G.edges())


def test_build_osm_graph_from_source_file_accepts_gzipped_xml_input(tmp_path, monkeypatch):
	source_path = tmp_path / "source.osm.gz"
	source_path.write_text("placeholder", encoding="utf-8")
	polygon = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
	reduced_source_path = tmp_path / "reduced.osm.gz"
	reduced_source_path.write_text("reduced", encoding="utf-8")

	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, length=100.0)

	calls = {}

	def fake_graph_from_xml(path_arg, **kwargs):
		calls["path"] = path_arg
		calls["kwargs"] = kwargs
		return G

	monkeypatch.setattr(builder_module.ox, "graph_from_xml", fake_graph_from_xml)
	monkeypatch.setattr(
		builder_module,
		"_reduce_local_source_to_buffered_bbox",
		lambda source_path_arg, bbox_arg, network_type_arg: reduced_source_path,
	)
	monkeypatch.setattr(builder_module, "_filter_local_source_graph_by_network_type", lambda graph, *_: graph)
	monkeypatch.setattr(builder_module.projection, "project_geometry", lambda geom, **kwargs: (geom, "epsg:test"))
	monkeypatch.setattr(builder_module.truncate, "truncate_graph_polygon", lambda graph, *_args, **_kwargs: graph)
	monkeypatch.setattr(builder_module.truncate, "largest_component", lambda graph, strongly=False: graph)
	monkeypatch.setattr(builder_module.simplification, "simplify_graph", lambda graph: graph)
	monkeypatch.setattr(builder_module.stats, "count_streets_per_node", lambda graph, **kwargs: {})

	res = builder_module._build_osm_graph_from_source_file(
		polygon,
		source_path,
		"drive_service",
	)

	assert calls["path"] == reduced_source_path
	assert calls["kwargs"]["simplify"] is False
	assert calls["kwargs"]["retain_all"] is True
	assert len(res.nodes()) == len(G.nodes())
	assert len(res.edges()) == len(G.edges())


def test_reduce_local_source_to_buffered_bbox_uses_osmium_and_writes_only_bbox_nodes(tmp_path, monkeypatch):
	source_path = tmp_path / "source.osm.pbf"
	source_path.write_text("placeholder", encoding="utf-8")
	node_writes = []
	way_writes = []

	class FakeLocation:
		def __init__(self, lon, lat):
			self.lon = lon
			self.lat = lat

		def valid(self):
			return True

	class FakeNode:
		def __init__(self, node_id, lon, lat):
			self.id = node_id
			self.location = FakeLocation(lon, lat)

		def is_node(self):
			return True

		def is_way(self):
			return False

		def is_relation(self):
			return False

	class FakeWayNodeRef:
		def __init__(self, ref, lon, lat):
			self.ref = ref
			self.location = FakeLocation(lon, lat)

	class FakeTag:
		def __init__(self, k, v):
			self.k = k
			self.v = v

	class FakeWay:
		def __init__(self, way_id, refs, tags=None):
			self.id = way_id
			self.nodes = [FakeWayNodeRef(ref, lon, lat) for ref, lon, lat in refs]
			self.tags = [FakeTag(k, v) for k, v in (tags or {}).items()]

		def is_node(self):
			return False

		def is_way(self):
			return True

		def is_relation(self):
			return False

	class FakeWriter:
		def __init__(self, outfile, overwrite=False):
			self.outfile = Path(outfile)
			self.overwrite = overwrite

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb):
			self.outfile.write_text("reduced", encoding="utf-8")

		def add_node(self, obj):
			node_writes.append(obj.id)

		def add_way(self, obj):
			way_writes.append(obj.id)

		def add(self, obj):
			if obj.is_node():
				self.add_node(obj)
			elif obj.is_way():
				self.add_way(obj)

	class FakeEntityFilter:
		def __init__(self, entity_bits):
			self.entity_bits = entity_bits

	class FakeIdFilter:
		def __init__(self, tracker):
			self.tracker = tracker

	class FakeIdTracker:
		def __init__(self):
			self._way_ids = set()
			self._node_ids = set()

		def add_way(self, way_id):
			self._way_ids.add(way_id)

		def add_references(self, way):
			self._node_ids.update(node.ref for node in way.nodes)

		def way_ids(self):
			return self._way_ids

		def node_ids(self):
			return self._node_ids

		def id_filter(self):
			return FakeIdFilter(self)

	class FakeFileProcessor:
		def __init__(self, path_arg, entity_bits=None, thread_pool=None):
			assert Path(path_arg) == source_path
			self.entity_bits = entity_bits
			self.filters = []
			self._all_objects = [
				FakeNode(1, 8.50, 51.60),
				FakeNode(2, 8.90, 51.60),
				FakeNode(3, 8.60, 51.70),
				FakeNode(4, 8.80, 51.80),
				FakeNode(5, 8.95, 51.95),
				FakeWay(10, [(1, 8.50, 51.60), (4, 8.80, 51.80)], {"highway": "residential"}),
				FakeWay(20, [(2, 8.90, 51.60), (5, 8.95, 51.95)], {"building": "yes"}),
			]

		def with_locations(self, idx_spec):
			self.idx_spec = idx_spec
			return self

		def with_filter(self, filt):
			self.filters.append(filt)
			return self

		def __iter__(self):
			objects = list(self._all_objects)
			if self.entity_bits == 3:
				objects = [obj for obj in objects if obj.is_node() or obj.is_way()]

			for filt in self.filters:
				if isinstance(filt, FakeEntityFilter):
					if filt.entity_bits == 2:
						objects = [obj for obj in objects if obj.is_way()]
				elif isinstance(filt, FakeIdFilter):
					objects = [
						obj for obj in objects
						if (obj.is_node() and obj.id in filt.tracker.node_ids())
						or (obj.is_way() and obj.id in filt.tracker.way_ids())
					]

			return iter(objects)

	fake_osmium = SimpleNamespace(
		SimpleWriter=FakeWriter,
		FileProcessor=FakeFileProcessor,
		IdTracker=FakeIdTracker,
		io=SimpleNamespace(ThreadPool=lambda num_threads: ("pool", num_threads)),
		filter=SimpleNamespace(EntityFilter=FakeEntityFilter),
		osm=SimpleNamespace(NODE=1, WAY=2),
	)
	monkeypatch.setattr(builder_module, "osmium", fake_osmium)

	reduced_path = builder_module._reduce_local_source_to_buffered_bbox(
		source_path,
		(8.45, 51.55, 8.70, 51.65),
		"drive_service",
	)

	assert reduced_path.exists()
	assert reduced_path.suffixes[-2:] == [".osm", ".gz"]
	assert node_writes == [1, 4]
	assert way_writes == [10]


def test_load_routing_config_resolves_relative_osm_source_path(tmp_path):
	config_path = tmp_path / "routing_config.json"
	source_path = tmp_path / "source.osm"
	source_path.write_text("<osm />", encoding="utf-8")
	config_path.write_text(
		"""
{
  "build": {
    "source": {
      "osm_source_path": "source.osm"
    },
    "graph": {
      "network_type": "drive_service",
      "keep_only_largest_strong_component": true
    },
    "speed_model": {
      "cap_explicit_speeds_with_practical_model": false,
      "cap_explicit_speeds_with_strong_practical_tags": true
    }
  },
  "runtime": {
    "routing": {
      "default_precise_mode": "fast"
    }
  }
}
""".strip(),
		encoding="utf-8",
	)

	config = load_routing_config(config_path)

	assert config["build"]["source"]["osm_source_path"] == str(source_path.resolve())


def test_filter_local_source_graph_by_network_type_matches_drive_service_basics():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_node(3, x=2.0, y=0.0)
	G.add_node(4, x=3.0, y=0.0)
	G.add_edge(1, 2, key=0, highway="residential")
	G.add_edge(2, 3, key=0, highway="footway")
	G.add_edge(3, 4, key=0, highway="service", service="parking")
	G.add_edge(4, 1, key=0, highway="service", service="driveway")

	filtered = builder_module._filter_local_source_graph_by_network_type(G, "drive_service")

	assert set(filtered.edges(keys=True)) == {(1, 2, 0), (4, 1, 0)}


def test_filter_local_source_graph_by_network_type_excludes_area_yes():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_node(3, x=2.0, y=0.0)
	G.add_edge(1, 2, key=0, highway="service", area="yes")
	G.add_edge(2, 3, key=0, highway="residential")

	filtered = builder_module._filter_local_source_graph_by_network_type(G, "drive_service")

	assert set(filtered.edges(keys=True)) == {(2, 3, 0)}


def test_to_networkit_with_attr_collapses_parallel_edges_deterministically():
	first = _build_parallel_graph(reverse_order=False)
	second = _build_parallel_graph(reverse_order=True)

	nk_first, node_map_first, edge_choice_first = to_networkit_with_attr(first, "cab_time")
	nk_second, node_map_second, edge_choice_second = to_networkit_with_attr(second, "cab_time")

	assert nk_first.numberOfEdges() == 1
	assert nk_second.numberOfEdges() == 1
	assert edge_choice_first[(1, 2)] == 1
	assert edge_choice_second[(1, 2)] == 1
	assert nk_first.weight(node_map_first[1], node_map_first[2]) == 5.0
	assert nk_second.weight(node_map_second[1], node_map_second[2]) == 5.0


def test_nearest_edge_breaks_ties_deterministically():
	router = Router.__new__(Router)
	router.edge_geoms = [
		LineString([(0.0, 0.0), (1.0, 0.0)]),
		LineString([(0.0, 0.0), (0.0, 1.0)]),
	]
	router.edge_uvk = [
		(5, 6, 1),
		(1, 2, 0),
	]
	router.edge_tree = STRtree(router.edge_geoms)

	assert router._nearest_edge(0.0, 0.0) == (1, 2, 0)


def test_normalize_bidirectional_path_restores_endpoints():
	assert normalize_bidirectional_path([1, 2], 0, 3) == [0, 1, 2, 3]
	assert normalize_bidirectional_path([], 0, 1) == []


def test_shortest_path_uses_config_default_precise_mode_for_trivial_routes():
	router = Router.__new__(Router)
	router.routing_config = {
		"build": {},
		"runtime": {"routing": {"default_precise_mode": "exact"}},
	}
	router._calc_proj_data = lambda lat, lon: (
		1, 2, {"length": 100.0}, LineString([(0.0, 0.0), (1.0, 0.0)]),
		Point(lon, lat), Point(lon, lat), 100.0, 0.0, 0.0,
	)

	res = router.shortest_path((51.0, 8.0), (51.0, 8.0))

	assert res["precise_mode"] == "exact"
	assert res["combo"] == "same_point"
	assert res["path_osm_nodes"] == []


def test_same_point_result_has_the_same_keys_as_a_normal_result():
	router = Router.__new__(Router)
	router.routing_config = {
		"build": {},
		"runtime": {"routing": {"default_precise_mode": "fast"}},
	}
	router._calc_proj_data = lambda lat, lon: (
		1, 2, {"length": 100.0}, LineString([(0.0, 0.0), (1.0, 0.0)]),
		Point(lon, lat), Point(lon, lat), 100.0, 0.0, 0.0,
	)

	res = router.shortest_path((51.0, 8.0), (51.0, 8.0))

	assert set(res) == {
		"combo", "distance_m", "energy_wh", "entry_energy_wh", "entry_time_s", "exit_energy_wh", "exit_time_s",
		"path_lat_lon", "path_osm_nodes", "precise_mode", "proj_dest", "proj_offsets_m", "proj_orig",
		"projection_lines", "query_time_s", "time_s",
	}
	assert res["path_lat_lon"] == []


def test_snap_returns_projected_point_and_offset_of_the_same_point_result():
	router = Router.__new__(Router)
	router.routing_config = {
		"build": {},
		"runtime": {"routing": {"default_precise_mode": "fast"}},
	}
	router._calc_proj_data = lambda lat, lon: (
		1, 2, {"length": 100.0}, LineString([(0.0, 0.0), (1.0, 0.0)]),
		Point(lon, lat), Point(lon + 0.001, lat + 0.002), 100.0, 0.0, 7.5,
	)

	projected, offset_m = router.snap(51.0, 8.0)
	res = router.shortest_path((51.0, 8.0), (51.0, 8.0))

	assert projected == res["proj_orig"]
	assert offset_m == res["proj_offsets_m"]["orig"] == 7.5


def test_shortest_path_forwards_use_od_override_to_route_nodes():
	router = Router.__new__(Router)
	router.routing_config = {
		"build": {},
		"runtime": {"routing": {"default_precise_mode": "fast"}},
	}
	router.profile = "cab"
	router.profiles = {"cab": {"max_speed_mps": 13.9, "energy_per_wh_m": 0.05}}
	router.weight_attr = "cab_time"
	router.G = nx.MultiDiGraph()
	router.G.add_node(1, x=0.0, y=0.0)
	router.G.add_node(2, x=1.0, y=0.0)
	router._calc_proj_data = lambda lat, lon: (
		1, 2, {"cab_time": 10.0, "length": 100.0, "cab_energy_wh": 5.0, "speed_kph": 50.0},
		LineString([(0.0, 0.0), (1.0, 0.0)]),
		Point(lon, lat),
		Point(lon, lat),
		100.0, 0.0, 0.0,
	)
	captured = {}
	def fake_route_nodes(src_osm, dst_osm, weight_attr, use_od=None):
		captured["use_od"] = use_od
		return [src_osm, dst_osm], 100.0, 10.0, 5.0
	router._route_nodes = fake_route_nodes

	res = router.shortest_path((51.0, 8.0), (51.001, 8.001), use_od=True)

	assert captured["use_od"] is True
	assert res["time_s"] >= 0.0


def test_shortest_path_route_result_cache_reuses_full_result():
	router = Router.__new__(Router)
	router.routing_config = {
		"build": {},
		"runtime": {
			"routing": {
				"default_precise_mode": "fast",
			},
			"route_result_cache": {
				"enabled": True,
				"max_entries": 2,
				"enabled_for": {"cab": True},
			},
		},
	}
	router.profile = "cab"
	router.profiles = {"cab": {"max_speed_mps": 13.9, "energy_per_wh_m": 0.05}}
	router.weight_attr = "cab_time"
	router.route_result_cache = OrderedDict()
	router.route_result_cache_hits = 0
	router.route_result_cache_misses = 0
	router.G = nx.MultiDiGraph()
	router.G.add_node(1, x=0.0, y=0.0)
	router.G.add_node(2, x=1.0, y=0.0)
	call_counts = {"proj": 0, "route": 0}

	def fake_calc_proj(lat, lon):
		call_counts["proj"] += 1
		return (
			1, 2, {"cab_time": 10.0, "length": 100.0, "cab_energy_wh": 5.0, "speed_kph": 50.0},
			LineString([(0.0, 0.0), (1.0, 0.0)]),
			Point(lon, lat),
			Point(lon, lat),
			100.0, 0.0, 0.0,
		)

	def fake_route_nodes(src_osm, dst_osm, weight_attr, use_od=None):
		call_counts["route"] += 1
		return [src_osm, dst_osm], 100.0, 10.0, 5.0

	router._calc_proj_data = fake_calc_proj
	router._route_nodes = fake_route_nodes

	first = router.shortest_path((51.0, 8.0), (51.001, 8.001))
	second = router.shortest_path((51.0, 8.0), (51.001, 8.001))

	assert call_counts["proj"] == 2
	assert call_counts["route"] == 1
	assert router.route_result_cache_hits == 1
	assert router.route_result_cache_misses == 1
	assert first["path_osm_nodes"] == second["path_osm_nodes"]
	assert second["query_time_s"] >= 0.0


def test_add_edge_speeds_from_tags_uses_directional_maxspeed():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(
		1,
		2,
		key=0,
		highway="secondary",
		reversed=False,
		**{"maxspeed:forward": "40", "maxspeed:backward": "80"},
	)
	G.add_edge(
		2,
		1,
		key=0,
		highway="secondary",
		reversed=True,
		**{"maxspeed:forward": "40", "maxspeed:backward": "80"},
	)

	add_edge_speeds_from_tags(G)

	assert G[1][2][0]["speed_kph"] == 40.0
	assert G[1][2][0]["legal_speed_kph"] == 40.0
	assert G[1][2][0]["practical_speed_kph"] == 55.0
	assert G[2][1][0]["speed_kph"] == 80.0
	assert G[2][1][0]["legal_speed_kph"] == 80.0
	assert G[2][1][0]["practical_speed_kph"] == 55.0


def test_add_edge_speeds_from_tags_falls_back_deterministically():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, highway="service", service="driveway")

	add_edge_speeds_from_tags(G)

	assert G[1][2][0]["speed_kph"] == 7.5
	assert G[1][2][0]["legal_speed_kph"] is None
	assert G[1][2][0]["practical_speed_kph"] == 7.5
	assert G[1][2][0]["speed_kph_source"] == "fallback"


def test_add_edge_speeds_from_tags_caps_explicit_speed_with_strong_practical_tags_by_default():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, highway="service", service="driveway", maxspeed="30")

	add_edge_speeds_from_tags(G)

	assert G[1][2][0]["legal_speed_kph"] == 30.0
	assert G[1][2][0]["practical_speed_kph"] == 7.5
	assert G[1][2][0]["strong_practical_speed_cap_kph"] == 7.5
	assert G[1][2][0]["speed_kph"] == 7.5


def test_add_edge_speeds_from_tags_can_disable_strong_practical_caps():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, highway="service", service="driveway", maxspeed="30")

	add_edge_speeds_from_tags(G, cap_explicit_speeds_with_strong_practical_tags=False)

	assert G[1][2][0]["legal_speed_kph"] == 30.0
	assert G[1][2][0]["strong_practical_speed_cap_kph"] == 7.5
	assert G[1][2][0]["speed_kph"] == 30.0


def test_add_edge_speeds_from_tags_can_cap_explicit_speed_with_practical_model():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(
		1,
		2,
		key=0,
		highway="secondary",
		reversed=True,
		**{"maxspeed:forward": "40", "maxspeed:backward": "80"},
	)

	add_edge_speeds_from_tags(
		G,
		cap_explicit_speeds_with_practical_model=True,
		cap_explicit_speeds_with_strong_practical_tags=False,
	)

	assert G[1][2][0]["legal_speed_kph"] == 80.0
	assert G[1][2][0]["practical_speed_kph"] == 55.0
	assert G[1][2][0]["strong_practical_speed_cap_kph"] is None
	assert G[1][2][0]["speed_kph"] == 55.0


def test_add_edge_speeds_from_tags_is_invariant_to_multi_value_tag_order():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_node(3, x=2.0, y=0.0)

	G.add_edge(
		1,
		2,
		key=0,
		highway=["residential", "living_street"],
		surface=["asphalt", "paving_stones"],
		maxspeed="50",
	)
	G.add_edge(
		2,
		3,
		key=0,
		highway=["living_street", "residential"],
		surface=["paving_stones", "asphalt"],
		maxspeed="50",
	)

	add_edge_speeds_from_tags(G)

	data_a = G[1][2][0]
	data_b = G[2][3][0]

	assert data_a["practical_speed_kph"] == 10.0
	assert data_b["practical_speed_kph"] == 10.0
	assert data_a["strong_practical_speed_cap_kph"] == 10.0
	assert data_b["strong_practical_speed_cap_kph"] == 10.0
	assert data_a["speed_kph"] == 10.0
	assert data_b["speed_kph"] == 10.0


def test_compute_edge_time_for_profile_handles_multi_value_highway_lists_conservatively():
	data = {
		"length": 100.0,
		"speed_kph": 36.0,
		"highway": ["living_street", "residential"],
	}
	profile_rules = {
		"max_speed_mps": 20.0,
		"energy_per_wh_m": 1.0,
		"forbid_highways": ["living_street"],
		"avoid_highways": {"living_street": 2.0, "residential": 1.2},
		"global_speed_factor": 1.0,
	}

	assert compute_edge_time_for_profile(data, profile_rules, record_debug=False) is None

	profile_rules["forbid_highways"] = []
	time_s = compute_edge_time_for_profile(data, profile_rules, record_debug=False)

	assert time_s == 20.0


def test_add_vehicle_profile_times_keeps_profiles_immutable_and_writes_profile_specific_debug_fields():
	G = nx.MultiDiGraph()
	G.add_node(1, x=0.0, y=0.0)
	G.add_node(2, x=1.0, y=0.0)
	G.add_edge(1, 2, key=0, length=100.0, speed_kph=36.0, highway="residential")

	profiles = {
		"cab": {
			"max_speed_mps": 10.0,
			"energy_per_wh_m": 1.0,
		},
		"pro": {
			"max_speed_mps": 8.0,
			"energy_per_wh_m": 2.0,
		},
	}
	original_profiles = copy.deepcopy(profiles)

	add_vehicle_profile_times(G, profiles)

	assert profiles == original_profiles
	data = G[1][2][0]
	assert data["osm_speed_mps_used_cab"] == 10.0
	assert data["eff_speed_mps_used_cab"] == 10.0
	assert data["energy_wh_used_cab"] == 100.0
	assert data["osm_speed_mps_used_pro"] == 10.0
	assert data["eff_speed_mps_used_pro"] == 8.0
	assert data["energy_wh_used_pro"] == 200.0


def test_load_nk_graph_treats_legacy_tuple_payload_as_stale(tmp_path):
	cache_path = tmp_path / "legacy_nk.pkl"
	with open(cache_path, "wb") as f:
		pickle.dump(("graph", {"node": 1}), f, protocol=pickle.HIGHEST_PROTOCOL)

	assert load_nk_graph(cache_path) == (None, None, None, None, None, None)


def test_track_overpass_request_stats_counts_cache_hits_and_downloads(monkeypatch):
	def fake_retrieve_from_cache(url):
		if url == "hit":
			return {"ok": True}
		return None

	def fake_requests_post(*args, **kwargs):
		return "response"

	monkeypatch.setattr("custom_sim.routing.builder.ox_http._retrieve_from_cache", fake_retrieve_from_cache)
	monkeypatch.setattr("custom_sim.routing.builder.ox_overpass.requests.post", fake_requests_post)

	with _track_overpass_request_stats() as stats:
		assert stats == {"cache_hits": 0, "downloads": 0}
		assert builder_module.ox_http._retrieve_from_cache("hit") == {"ok": True}
		assert builder_module.ox_http._retrieve_from_cache("miss") is None
		assert builder_module.ox_overpass.requests.post("url") == "response"

	assert stats == {"cache_hits": 1, "downloads": 1}


def test_file_stamp_uses_size_and_mtime(tmp_path):
	path = tmp_path / "graph.graphml"
	path.write_text("abc", encoding="utf-8")
	st = path.stat()

	assert file_stamp(path) == f"{st.st_size}:{st.st_mtime_ns}"


def test_prepare_graph_and_index_uses_matching_topology_and_nk_caches(tmp_path, monkeypatch):
	router = _build_router_stub(tmp_path)
	G = _build_cache_graph()
	graphml_stamp, edge_choice = _save_router_caches(router, G)

	def should_not_run(*args, **kwargs):
		raise AssertionError("unexpected rebuild path during matching-cache test")

	monkeypatch.setattr(router_module.ox, "load_graphml", should_not_run)
	monkeypatch.setattr(router_module, "build_osm_graph_from_polygon", should_not_run)
	monkeypatch.setattr(router_module, "to_networkit_with_attr", should_not_run)

	router._prepare_graph_and_index()

	assert file_stamp(router.graphml_path) == graphml_stamp
	assert len(router.G.nodes()) == len(G.nodes())
	assert len(router.edge_geoms) == len(G.edges())
	assert router.nk_edge_choice == edge_choice


def test_prepare_graph_and_index_skips_stale_topology_and_nk_caches(tmp_path, monkeypatch):
	router = _build_router_stub(tmp_path)
	original_graph = _build_cache_graph()
	original_stamp, _ = _save_router_caches(router, original_graph)

	time.sleep(0.001)
	updated_graph = _build_cache_graph(extra_edge=True)
	ox.save_graphml(updated_graph, router.graphml_path)
	updated_stamp = file_stamp(router.graphml_path)
	assert updated_stamp != original_stamp

	calls = {"graphml_loads": 0, "nk_rebuilds": 0}
	orig_load_graphml = router_module.ox.load_graphml
	orig_to_networkit = router_module.to_networkit_with_attr

	def tracked_load_graphml(*args, **kwargs):
		calls["graphml_loads"] += 1
		return orig_load_graphml(*args, **kwargs)

	def tracked_to_networkit(*args, **kwargs):
		calls["nk_rebuilds"] += 1
		return orig_to_networkit(*args, **kwargs)

	monkeypatch.setattr(router_module.ox, "load_graphml", tracked_load_graphml)
	monkeypatch.setattr(router_module, "to_networkit_with_attr", tracked_to_networkit)
	monkeypatch.setattr(
		router_module,
		"build_osm_graph_from_polygon",
		lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected OSM download")),
	)

	router._prepare_graph_and_index()

	assert calls["graphml_loads"] == 1
	assert calls["nk_rebuilds"] == 1
	assert len(router.G.nodes()) == len(updated_graph.nodes())
	assert len(router.G.edges()) == len(updated_graph.edges())
	assert file_stamp(router.graphml_path) == updated_stamp

	with open(_topology_cache_path(router), "rb") as f:
		topology_cache = pickle.load(f)
	assert topology_cache["graphml_stamp"] == updated_stamp

	_, _, edge_choice, nk_stamp, nk_profile_name, nk_profile_rules = load_nk_graph(router.nk_path)
	assert nk_stamp == updated_stamp
	assert edge_choice == router.nk_edge_choice
	assert nk_profile_name == router.profile
	assert nk_profile_rules == router.profiles[router.profile]


def test_prepare_graph_and_index_rebuilds_nk_cache_when_only_energy_changes(tmp_path, monkeypatch):
	router = _build_router_stub(tmp_path)
	G = _build_cache_graph()
	graphml_stamp, edge_choice = _save_router_caches(router, G)
	router.profiles["cab"]["energy_per_wh_m"] = 2.0
	calls = {"nk_rebuilds": 0}
	orig_to_networkit = router_module.to_networkit_with_attr

	def tracked_to_networkit(*args, **kwargs):
		calls["nk_rebuilds"] += 1
		return orig_to_networkit(*args, **kwargs)

	monkeypatch.setattr(router_module, "to_networkit_with_attr", tracked_to_networkit)

	router._prepare_graph_and_index()

	assert file_stamp(router.graphml_path) == graphml_stamp
	assert router.nk_edge_choice == edge_choice
	assert calls["nk_rebuilds"] == 1


def test_normalized_bidirectional_path_matches_dijkstra_on_real_graph(tmp_path):
	router = _build_real_router(
		tmp_path,
		graphml_src="custom_sim/routing/data/pb_graph_area.graphml",
		area_id="pb",
	)

	rng = random.Random(12345)
	node_ids = list(router.node_map.values())
	checked = 0
	attempts = 0

	while checked < 20 and attempts < 200:
		attempts += 1
		s_idx = rng.choice(node_ids)
		t_idx = rng.choice(node_ids)
		if s_idx == t_idx:
			continue

		bd = nk.distance.BidirectionalDijkstra(router.nkG, s_idx, t_idx, storePred=True)
		bd.run()
		dist_bd = bd.getDistance()
		if dist_bd == float("inf"):
			continue

		raw_path = bd.getPath()
		if not raw_path:
			continue

		norm_path = normalize_bidirectional_path(raw_path, s_idx, t_idx)

		dj = nk.distance.Dijkstra(router.nkG, s_idx, storePaths=True)
		dj.run()
		dist_dj = dj.distance(t_idx)
		dj_path = dj.getPath(t_idx)

		assert norm_path == dj_path
		assert abs(dist_bd - dist_dj) < 1e-9
		checked += 1

	assert checked == 20


def test_real_route_results_satisfy_path_invariants(tmp_path):
	router = _build_real_router(
		tmp_path,
		graphml_src="custom_sim/routing/data/pb_graph_area.graphml",
		area_id="pb",
	)
	request_entries = read_request_json("data/input/example_demand.json")

	for request in request_entries[:25]:
		res = router.shortest_path(
			(request["_pu_lat"], request["_pu_lon"]),
			(request["_do_lat"], request["_do_lon"]),
		)
		path = res["path_osm_nodes"]

		assert path
		assert res["distance_m"] >= 0.0
		assert res["energy_wh"] >= 0.0
		assert res["time_s"] >= res["entry_time_s"] + res["exit_time_s"]
		assert res["energy_wh"] >= res["entry_energy_wh"] + res["exit_energy_wh"]

		for a, b in zip(path[:-1], path[1:]):
			assert router.G.get_edge_data(a, b) is not None
