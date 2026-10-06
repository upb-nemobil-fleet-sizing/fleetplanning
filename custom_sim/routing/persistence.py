
import json
import pickle
import os

import networkit as nk
import numpy as np


def save_graphml_metadata(path, metadata):
	with open(path, "w", encoding="utf-8") as f:
		json.dump(metadata, f, indent=2, sort_keys=True)


def load_graphml_metadata(path):
	if not os.path.exists(path):
		return None
	try:
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return None


def save_nk_graph(
	nkG,
	node_map,
	path,
	edge_choice=None,
	graphml_stamp=None,
	profile_name=None,
	profile_rules=None,
):
	with open(path, "wb") as f:
		pickle.dump({
			"nkG": nkG,
			"node_map": node_map,
			"edge_choice": edge_choice,
			"graphml_stamp": graphml_stamp,
			"profile_name": profile_name,
			"profile_rules": profile_rules,
		}, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_nk_graph(path):
	with open(path, "rb") as f:
		data = pickle.load(f)
	
	if not isinstance(data, dict):
		return None, None, None, None, None, None
	
	return (
		data.get("nkG"),
		data.get("node_map"),
		data.get("edge_choice"),
		data.get("graphml_stamp"),
		data.get("profile_name"),
		data.get("profile_rules"),
	)


def save_ch(ch, path):
	# networkit CH supports export
	ch.export(path)


def load_ch(ch_path, nkG):
	# importCH expects the graph too; this returns a Ch instance
	return nk.distance.Ch.importCH(ch_path, nkG)


def save_matrix(od_matrix, path):
	with open(path, "wb") as f:
		pickle.dump(od_matrix, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_matrix(path):
	with open(path, "rb") as f:
		return pickle.load(f)


def create_numpy_matrix(path, dtype, shape):
	return np.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)


def load_numpy_matrix(path):
	return np.load(path, mmap_mode="r", allow_pickle=False)
