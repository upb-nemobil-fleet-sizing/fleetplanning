#!/usr/bin/env python3

import argparse
import subprocess
import sys
import time
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the full XML -> final JSON preprocessing pipeline."
    )
    parser.add_argument("xml_file", help="Input XML file.")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Final output JSON path. If omitted, generator_tudo.py legacy naming is used.",
    )
    parser.add_argument(
        "--geojson",
        default=None,
        help="GeoJSON polygon file for final filtering stage. Default: a rectangle over the data's own extent.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run all stages.",
    )
    parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep intermediate JSON files instead of deleting them.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=2738,
        help="Sample size passed to generator_tudo.py (legacy default: 2738).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed passed to both create_json_with_simulated_time.py's lead-time draws "
             "and generator_tudo.py's sampling (legacy default: 0).",
    )
    parser.add_argument(
        "--drop-prebooking",
        action="store_true",
        help="Pass --drop-prebooking to generator_tudo.py.",
    )
    parser.add_argument(
        "--tw-minutes",
        type=int,
        default=10,
        help="Time window length in minutes, passed to generator_tudo.py (legacy default: 10).",
    )
    parser.add_argument(
        "--guarantee-feasible",
        action="store_true",
        help="Pass --guarantee-feasible to generator_tudo.py (requires custom_sim.routing).",
    )
    parser.add_argument(
        "--depot-lat",
        type=float,
        default=None,
        help="Depot latitude, passed to generator_tudo.py (default: generator_tudo.py's own default).",
    )
    parser.add_argument(
        "--depot-lon",
        type=float,
        default=None,
        help="Depot longitude, passed to generator_tudo.py (default: generator_tudo.py's own default).",
    )
    parser.add_argument(
        "--service-seconds",
        type=int,
        default=None,
        help="On-/off-boarding time, passed to generator_tudo.py (default: generator_tudo.py's own default).",
    )
    parser.add_argument(
        "--horizon-start",
        default=None,
        help="Fleet operating hours start (HH:MM), passed to generator_tudo.py (default: generator_tudo.py's own default).",
    )
    parser.add_argument(
        "--horizon-end",
        default=None,
        help="Fleet operating hours end (HH:MM), passed to generator_tudo.py (default: generator_tudo.py's own default).",
    )
    parser.add_argument(
        "--base-data",
        default=None,
        help="BaseData JSON file, passed to generator_tudo.py (default: generator_tudo.py's own hardcoded defaults).",
    )
    parser.add_argument(
        "--max-shift-minutes",
        type=int,
        default=None,
        help="Cap on --guarantee-feasible's forward shift in minutes, passed to generator_tudo.py (default: unlimited).",
    )
    return parser.parse_args()


def run_step(step_num, total_steps, label, cmd):
    print(f"[{step_num}/{total_steps}] {label}")
    print("  Command:", " ".join(cmd))
    started = time.perf_counter()
    subprocess.run(cmd, check=True)
    elapsed = time.perf_counter() - started
    print(f"[{step_num}/{total_steps}] Done in {elapsed:.1f}s\n")


def main():
    args = parse_args()

    xml_path = Path(args.xml_file)
    if not xml_path.exists():
        raise FileNotFoundError(f"Input XML file not found: {xml_path}")

    base = xml_path.with_suffix("")
    stage1_json = Path(f"{base}.json")
    stage2_json = Path(f"{base}_simulatedtime_weekdays.json")
    final_json = Path(args.output) if args.output else None
    total_steps = 4

    print("Starting preprocessing pipeline")
    print(f"  Input XML: {xml_path}")
    print(f"  Step 1 output: {stage1_json}")
    print(f"  Step 2 output: {stage2_json}")
    if final_json:
        print(f"  Final output: {final_json}")
    else:
        print("  Final output: legacy naming from generator_tudo.py")
    print()

    try:
        run_step(1, total_steps, "Extracting DRT trips from XML", [
            args.python,
            "read_tudo_agents.py",
            str(xml_path),
            "--output",
            str(stage1_json),
        ])
        run_step(2, total_steps, "Adding simulated request times", [
            args.python,
            "create_json_with_simulated_time.py",
            str(stage1_json),
            "--output",
            str(stage2_json),
            "--seed",
            str(args.seed),
        ])
        generator_cmd = [
            args.python,
            "generator_tudo.py",
            str(stage2_json),
            "--sample-size",
            str(args.sample_size),
            "--seed",
            str(args.seed),
            "--tw-minutes",
            str(args.tw_minutes),
        ]
        if args.geojson is not None:
            generator_cmd.extend(["--geojson", args.geojson])
        if args.drop_prebooking:
            generator_cmd.append("--drop-prebooking")
        if args.guarantee_feasible:
            generator_cmd.append("--guarantee-feasible")
        if args.depot_lat is not None:
            generator_cmd.extend(["--depot-lat", str(args.depot_lat)])
        if args.depot_lon is not None:
            generator_cmd.extend(["--depot-lon", str(args.depot_lon)])
        if args.service_seconds is not None:
            generator_cmd.extend(["--service-seconds", str(args.service_seconds)])
        if args.horizon_start is not None:
            generator_cmd.extend(["--horizon-start", args.horizon_start])
        if args.horizon_end is not None:
            generator_cmd.extend(["--horizon-end", args.horizon_end])
        if args.base_data is not None:
            generator_cmd.extend(["--base-data", args.base_data])
        if args.max_shift_minutes is not None:
            generator_cmd.extend(["--max-shift-minutes", str(args.max_shift_minutes)])
        if final_json:
            generator_cmd.extend(["--output", str(final_json)])
        run_step(3, total_steps, "Creating final preprocessed booking JSON", generator_cmd)
    except subprocess.CalledProcessError as exc:
        print(f"Pipeline failed in step: {' '.join(exc.cmd)}")
        raise SystemExit(exc.returncode) from exc

    print(f"[4/{total_steps}] Cleaning intermediate files")
    if not args.keep_intermediate:
        for tmp_file in (stage1_json, stage2_json):
            if final_json and tmp_file == final_json:
                continue
            if tmp_file.exists():
                tmp_file.unlink()
                print(f"Deleted intermediate file: {tmp_file}")
        print(f"[4/{total_steps}] Cleanup complete\n")
    else:
        print(f"[4/{total_steps}] Skipped (keep-intermediate enabled)\n")

    if final_json:
        print(f"Final JSON written to: {final_json}")
    else:
        print("Final JSON written with legacy naming: pb_<size>_<drop_prebooking>_<seed>_<tw_minutes>.json")


if __name__ == "__main__":
    main()
