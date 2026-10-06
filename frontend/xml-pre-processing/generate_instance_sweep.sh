#!/usr/bin/env bash
# Generate booking-request instances for every XML file in XML_DIR, at
# several TW lengths, with and without --guarantee-feasible.
#
# Stage 1 (read_tudo_agents.py) and stage 2 (create_json_with_simulated_time.py)
# don't depend on TW length or --guarantee-feasible, so each XML only goes
# through them once; only the final generator_tudo.py step repeats per
# TW/mode combination. Final outputs use generator_tudo.py's own legacy
# naming (pb_<size>_<drop_prebooking>_<seed>_<tw_minutes>.json) - filtered
# and unfiltered runs land on different names on their own since filtering
# drops rows.
#
# Run from inside frontend/xml-pre-processing/ (needs custom_sim.routing
# for --guarantee-feasible, only present in this repo).
set -euo pipefail

PYTHON="${PYTHON:-python3}"
XML_DIR="${XML_DIR:-./new_xmls}"
OUT_DIR="${OUT_DIR:-./new_jsons}"
TW_MINUTES_LIST=(10 30 60 120)

mkdir -p "$OUT_DIR"

# Runs generator_tudo.py with cwd unchanged (so its own --geojson default
# still resolves) and moves whatever legacy-named file it just wrote into
# OUT_DIR, instead of overriding --output or cd-ing into OUT_DIR - both of
# which would require reimplementing/breaking its own naming logic.
run_generator() {
	local out_line
	out_line=$("$PYTHON" generator_tudo.py "$@" | tee /dev/stderr | grep "^Data successfully exported to ")
	mv "${out_line#Data successfully exported to }" "$OUT_DIR/"
}

shopt -s nullglob
for xml in "$XML_DIR"/*.xml; do
	base=$(basename "$xml" .xml)
	stage1="$OUT_DIR/${base}.json"
	stage2="$OUT_DIR/${base}_simulatedtime.json"

	echo "=== $base: stage 1+2 ==="
	"$PYTHON" read_tudo_agents.py "$xml" --output "$stage1"
	"$PYTHON" create_json_with_simulated_time.py "$stage1" --output "$stage2"

	for tw in "${TW_MINUTES_LIST[@]}"; do
		echo "=== $base tw=${tw} raw ==="
		run_generator "$stage2" --tw-minutes "$tw"

		echo "=== $base tw=${tw} filtered ==="
		run_generator "$stage2" --tw-minutes "$tw" --guarantee-feasible
	done
done

echo "Done. Output in $OUT_DIR/"
