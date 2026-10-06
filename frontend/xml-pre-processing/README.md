# XML Preprocessing

Converts a [MATSim](https://www.matsim.org/)-style population XML file into the booking-request JSON
expected by the dashboard's RideData upload. The XML root must be `<population>`. The same pipeline
can also be run from the command line.

## Requirements

Python 3.12. This tool has no dependencies of its own beyond `frontend/requirements.txt` (repo root):

```bash
python -m pip install -r frontend/requirements.txt
```

## Pipeline Overview

Three stages, run in order:

1. `read_tudo_agents.py`: parses the MATSim XML, writes an intermediate JSON.
2. `create_json_with_simulated_time.py`: adds `SimulatedTime` values, writes a second intermediate JSON.
3. `generator_tudo.py`: time/spatial filtering and request formatting, writes the final JSON.

Run the wrapper from `frontend/xml-pre-processing`; it invokes the three stage scripts by relative path:

```bash
python run_preprocessing_pipeline.py NeMoBil-SICP2.output_plans_4pax_StandardCosts.xml
```

## Request Creation-Time Model

Stage 2 (`create_json_with_simulated_time.py`) generates `SimulatedTime`: the notional booking time
before the request's service `start_time`. Its model largely determines how realistic the generated
demand scenario is:

- Lead time drawn from a log-normal distribution fitted to real booking data. Log-normal distributions are
  common for positive, right-skewed quantities (see [Sources](#sources)).
- Rejection-sampled against an acceptance curve: same-day accepted outright, longer lead times
  increasingly rejected, hard cap at 60 days.
- Goal: match real fleets' same-day-vs-advance-booking ratio, not the fit's raw, unbounded tail.
- All constants (distribution shape/loc/scale, acceptance curve) are hardcoded at the top of the
  file, not a CLI flag or dashboard option.

Also hardcoded, at the top of the same file:

- Calendar date every request is placed on: 2025-10-29 (a Wednesday).
- Timezone offset: +02:00 (CEST), not derived from the date above.

## Selected Options

Example with the most frequently used flags:

```bash
python run_preprocessing_pipeline.py <input.xml> \
  --geojson sicpArea_merged_final.geojson \
  --sample-size 2738 \
  --seed 0
```

- `-o, --output <file>`: set a custom final JSON filename.
- `--geojson <file>`: polygon for stage 3's spatial filter. Optional: without it, stage 3 falls back
  to a rectangle over the data's own coordinate extent, i.e. no real filtering.
- `--seed <int>`: forwarded to both stage 2's lead-time draws and stage 3's sampling.
- `--tw-minutes <int>`: pickup time-window length; default `10`.
- `--keep-intermediate`: keep stage-1 and stage-2 JSON files instead of deleting them.
- `--drop-prebooking`: forward this option to `generator_tudo.py`.
- `--guarantee-feasible`: shift requests when necessary to make the generated scenario feasible;
  requires `custom_sim.routing`.
- `--base-data <file>`: use fleet operating times and depot data from a BaseData JSON file.
- `--max-shift-minutes <int>`: limit the forward shift used by `--guarantee-feasible`.
- `--python <path>`: choose which Python executable runs subprocesses.

By default, deletes intermediate JSON files and, when `--output` isn't given, keeps
`generator_tudo.py`'s output naming: `pb_<size>_<drop_prebooking>_<seed>_<tw_minutes>.json`.

## Running Stages Manually

Run each stage's script directly instead of the combined pipeline:

```bash
python read_tudo_agents.py <input.xml> --output <stage1.json>
python create_json_with_simulated_time.py <stage1.json> --output <stage2.json>
python generator_tudo.py <stage2.json> --output <final.json>
```

## Sources

- Limpert, E., Stahel, W. A., & Abbt, M. (2001). Log-normal distributions across the sciences: Keys and
  clues. *BioScience*, *51*(5), 341-352. https://doi.org/10.1641/0006-3568(2001)051[0341:LNDATS]2.0.CO;2
