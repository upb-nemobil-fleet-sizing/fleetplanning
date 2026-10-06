# custom_sim/routing/visualization.py

import plotly.graph_objects as go


def plot_graph_plotly(
	router_cab,
	zoom=12,
	show_graph_edges=True,
	show_graph_nodes=False,
	bbox_vertices=None,
	bbox_polygon=None,
	bbox_fillcolor="rgba(200,200,200,0.30)",
	bbox_linecolor="rgba(120,120,120,0.9)",
	bbox_linewidth=2,
):
	"""
	Minimal interactive Plotly map:
	- requires only router_cab (expects router_cab.G with node attrs x=lon, y=lat)
	- optional: show edges/nodes
	- optional: overlay a bounding polygon (prefer bbox_vertices)
	"""
	
	G = router_cab.G
	
	def make_all_edges_trace(G):
		lats = []
		lons = []
		for u, v, data in G.edges(data=True):
			geom = data.get("geometry")
			if geom is not None:
				xs, ys = geom.xy
				lons.extend(list(xs) + [None])
				lats.extend(list(ys) + [None])
			else:
				lons.extend([G.nodes[u]["x"], G.nodes[v]["x"], None])
				lats.extend([G.nodes[u]["y"], G.nodes[v]["y"], None])

		return go.Scattermapbox(
			lat=lats,
			lon=lons,
			mode="lines",
			line=dict(width=1, color="#2b2b2b"),# color="red"),#
			hoverinfo="skip",
			showlegend=False,
			name="Graph edges",
		)

	def make_all_nodes_trace(G):
		lats = []
		lons = []
		for _, d in G.nodes(data=True):
			lats.append(d["y"])
			lons.append(d["x"])

		return go.Scattermapbox(
			lat=lats,
			lon=lons,
			mode="markers",
			marker=dict(size=3,color="red",opacity=1.0),
			hoverinfo="skip",
			showlegend=False,
			name="Graph nodes",
		)
	
	def _polygon_latlon_from_inputs(bbox_vertices, bbox_polygon):
		if bbox_vertices is not None:
			if len(bbox_vertices) < 3:
				return None, None
			lats = [float(p[1]) for p in bbox_vertices]
			lons = [float(p[0]) for p in bbox_vertices]
		elif bbox_polygon is not None:
			# Shapely Polygon: exterior coords are (x, y) = (lon, lat)
			coords = list(bbox_polygon.exterior.coords)
			if len(coords) < 3:
				return None, None
			lons = [float(x) for (x, y) in coords]
			lats = [float(y) for (x, y) in coords]
		else:
			return None, None

		# Ensure closed ring
		if lats[0] != lats[-1] or lons[0] != lons[-1]:
			lats.append(lats[0])
			lons.append(lons[0])

		return lats, lons
	
	def make_bbox_trace(bbox_vertices, bbox_polygon):
		lats, lons = _polygon_latlon_from_inputs(bbox_vertices, bbox_polygon)
		if lats is None:
			return None

		# Try filled polygon
		t = go.Scattermapbox(
			lat=lats,
			lon=lons,
			mode="lines",
			fill="toself",
			fillcolor=bbox_fillcolor,
			line=dict(color=bbox_linecolor, width=bbox_linewidth),
			hoverinfo="skip",
			showlegend=False,
			name="Bounding area",
		)
		# If fill doesn't show in your renderer, use outline-only by setting bbox_fillcolor alpha=0.
		# (You can toggle this at call time without changing code.)
		return t
	
	traces = []
	
	if show_graph_edges:
		traces.append(make_all_edges_trace(G))
	if show_graph_nodes:
		traces.append(make_all_nodes_trace(G))
	
	bbox_trace = make_bbox_trace(bbox_vertices, bbox_polygon)
	if bbox_trace is not None:
		traces.append(bbox_trace)

	# Center/zoom
	all_lats = []
	all_lons = []
	for t in traces:
		if getattr(t, "lat", None) is None or getattr(t, "lon", None) is None:
			continue
		for v in t.lat:
			if v is not None:
				all_lats.append(float(v))
		for v in t.lon:
			if v is not None:
				all_lons.append(float(v))

	center_lat = (max(all_lats) + min(all_lats)) / 2 if all_lats else 0.0
	center_lon = (max(all_lons) + min(all_lons)) / 2 if all_lons else 0.0

	fig = go.Figure(
		data=traces,
		layout=go.Layout(
			mapbox_style="open-street-map",
			mapbox_zoom=zoom,
			mapbox_center={"lat": center_lat, "lon": center_lon},
			margin={"r": 0, "t": 0, "l": 0, "b": 0},
		),
	)
	fig.update_layout(mapbox=dict(accesstoken=None), dragmode="zoom")
	fig.show(config={"scrollZoom": True})


def plot_graph_edges_by_highway_plotly(
	router,
	zoom=12,
	show_graph_nodes=False,
	show_legend=True,
	color_by="speed",
):
	"""
	Debugging view of the whole graph with edges colored by speed bands by
	default, or by highway type on request.
	"""
	G = router.G

	highway_colors = {
		"motorway": "#d73027",
		"trunk": "#fc8d59",
		"primary": "#fee08b",
		"secondary": "#91cf60",
		"tertiary": "#1a9850",
		"residential": "#4575b4",
		"service": "#74add1",
		"living_street": "#a65628",
		"unclassified": "#ff00ff",
		"track": "#984ea3",
		"unknown": "#000000",
	}
	speed_bands = [
		("0-10 km/h", 0.0, 10.0, "#8b0000"),
		("10-20 km/h", 10.0, 20.0, "#d73027"),
		("20-30 km/h", 20.0, 30.0, "#fc8d59"),
		("30-50 km/h", 30.0, 50.0, "#fee08b"),
		("50-70 km/h", 50.0, 70.0, "#91cf60"),
		("70+ km/h", 70.0, float("inf"), "#1a9850"),
	]

	def _fmt(value):
		if value is None:
			return "n/a"
		if isinstance(value, float):
			return f"{value:.2f}"
		return str(value)

	def _speed_band(speed_kph):
		if speed_kph is None:
			return "n/a", "#777777"
		for label, lower, upper, color in speed_bands:
			if lower <= speed_kph < upper:
				return label, color
		return "n/a", "#777777"

	def make_grouped_edge_traces(G):
		grouped = {}
		for u, v, k, data in G.edges(keys=True, data=True):
			highway = data.get("highway", "unknown")
			if isinstance(highway, list):
				highway = highway[0]
			highway = highway or "unknown"
			speed_kph = data.get("speed_kph")

			if color_by == "highway":
				group_key = highway
				group_color = highway_colors.get(highway, "#999999")
			else:
				group_key, group_color = _speed_band(speed_kph)

			entry = grouped.setdefault(group_key, {
				"lat": [],
				"lon": [],
				"text": [],
				"color": group_color,
			})

			geom = data.get("geometry")
			if geom is not None:
				xs, ys = geom.xy
				lats = list(ys)
				lons = list(xs)
			else:
				lats = [G.nodes[u]["y"], G.nodes[v]["y"]]
				lons = [G.nodes[u]["x"], G.nodes[v]["x"]]

			hover = (
				f"highway: {highway}"
				f"<br>u,v,k: {u}, {v}, {k}"
				f"<br>speed_kph: {_fmt(speed_kph)}"
				f"<br>speed source: {_fmt(data.get('speed_kph_source'))}"
				f"<br>legal_speed_kph: {_fmt(data.get('legal_speed_kph'))}"
				f"<br>practical_speed_kph: {_fmt(data.get('practical_speed_kph'))}"
				f"<br>strong practical cap: {_fmt(data.get('strong_practical_speed_cap_kph'))}"
				f"<br>length_m: {_fmt(data.get('length'))}"
			)

			entry["lat"].extend(lats + [None])
			entry["lon"].extend(lons + [None])
			entry["text"].extend([hover] * len(lats) + [None])

		traces = []
		for group_key, payload in sorted(grouped.items()):
			traces.append(go.Scattermapbox(
				lat=payload["lat"],
				lon=payload["lon"],
				mode="lines",
				line=dict(width=2, color=payload["color"]),
				hoverinfo="text",
				text=payload["text"],
				name=group_key,
				showlegend=show_legend,
			))
		return traces

	def make_all_nodes_trace(G):
		lats = []
		lons = []
		for _, d in G.nodes(data=True):
			lats.append(d["y"])
			lons.append(d["x"])

		return go.Scattermapbox(
			lat=lats,
			lon=lons,
			mode="markers",
			marker=dict(size=3, color="gray", opacity=0.8),
			hoverinfo="skip",
			showlegend=False,
			name="Graph nodes",
		)

	traces = make_grouped_edge_traces(G)
	if show_graph_nodes:
		traces.append(make_all_nodes_trace(G))

	all_lats = []
	all_lons = []
	for t in traces:
		if getattr(t, "lat", None) is None or getattr(t, "lon", None) is None:
			continue
		for v in t.lat:
			if v is not None:
				all_lats.append(float(v))
		for v in t.lon:
			if v is not None:
				all_lons.append(float(v))

	center_lat = (max(all_lats) + min(all_lats)) / 2 if all_lats else 0.0
	center_lon = (max(all_lons) + min(all_lons)) / 2 if all_lons else 0.0

	fig = go.Figure(
		data=traces,
		layout=go.Layout(
			mapbox_style="open-street-map",
			mapbox_zoom=zoom,
			mapbox_center={"lat": center_lat, "lon": center_lon},
			margin={"r": 0, "t": 30, "l": 0, "b": 0},
			title=(
				"Graph edges colored by speed"
				if color_by != "highway"
				else "Graph edges colored by highway"
			),
		),
	)
	fig.update_layout(mapbox=dict(accesstoken=None), dragmode="zoom")
	fig.show(config={"scrollZoom": True})
