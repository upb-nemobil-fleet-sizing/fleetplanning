import xml.etree.ElementTree as ET
import argparse
import gzip
import sys
from collections import defaultdict

def open_network_file(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    else:
        return open(path, "r", encoding="utf-8")

def compute_edge_speeds(netfile):
    try:
        with open_network_file(netfile) as f:
            tree = ET.parse(f)
        root = tree.getroot()
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)

    speed_counts = defaultdict(int)
    example_edges = {}

    for edge in root.findall("edge"):
        edge_id = edge.get("id")

        # ❗ Skip internal edges (they start with ":")
        if edge_id.startswith(":"):
            continue

        lane_speeds = []
        for lane in edge.findall("lane"):
            s = lane.get("speed")
            if s is None:
                continue
            try:
                lane_speeds.append(float(s))
            except ValueError:
                continue

        if not lane_speeds:
            continue

        edge_speed = max(lane_speeds)

        speed_counts[edge_speed] += 1

        if edge_speed not in example_edges:
            example_edges[edge_speed] = edge_id

    return speed_counts, example_edges


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Distinct max speeds and example visible edges.")
    parser.add_argument("netfile", help="SUMO network file (.net.xml or .net.xml.gz)")
    args = parser.parse_args()

    speed_counts, example_edges = compute_edge_speeds(args.netfile)

    print("\nVisible (non-internal) edges by speed:")
    print("---------------------------------------")
    for speed in sorted(speed_counts.keys()):
        print(f"{speed:.2f} m/s → {speed_counts[speed]} edges (example: {example_edges[speed]})")