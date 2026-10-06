class OperationalVertices:
    """
    Bounding polygon of the operational area. Vertices are (lat, lon) tuples.
    """

    def __init__(self, _vertex_list: list[tuple[float, float]], _id: str = "0c14d3cc-fb16-49ff-b999-4c26921132cb", _desc: str = "default"):
        self.id = _id
        self.vertices = _vertex_list
        self.description = _desc

        lats = [lat for lat, _ in _vertex_list]
        lons = [lon for _, lon in _vertex_list]
        self.sw_lat = min(lats)
        self.sw_lon = min(lons)
        self.ne_lat = max(lats)
        self.ne_lon = max(lons)

    def get_center(self) -> tuple[float, float]:
        """
        Returns the centre of the bounding box of the vertices as (lat, lon)
        """
        center_lat = (self.sw_lat + self.ne_lat) / 2
        center_lon = (self.sw_lon + self.ne_lon) / 2
        return (center_lat, center_lon)

    def __str__(self):
        return f"OperationalVertices(num_vertex: {len(self.vertices)}, desc: {self.description})"

    def __repr__(self):
        return self.__str__()
