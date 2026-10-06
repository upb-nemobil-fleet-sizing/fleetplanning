# Convoy Feature

Short handover note for the convoy extension in `custom_sim`.

## What changed

Before, a new request could only be inserted as a direct cab trip:

`CA -> CT -> reconnect`

Now, the same insertion workflow also evaluates convoy-based variants on scheduled PRO trips:

`CA -> FM -> PT -> LM -> reconnect`

So convoy is not a separate planner. It is an additional candidate type inside the existing cab insertion logic.

## Main entities

- `Customer`: pickup, dropoff, time window
- `Cab`: flexible vehicle whose schedule is modified
- `PRO`: scheduled line-haul vehicle with limited cab capacity
- `ChainingLocation`: where a cab can join or leave a PRO
- `ChainRouteTrip`: one concrete scheduled PRO trip

## How it works

For each request and each feasible gap in a cab schedule, the simulator now builds:

- one direct cab candidate
- zero or more convoy candidates based on matching scheduled PRO trips

A convoy candidate means:

- cab drives empty to the customer pickup
- cab drives customer to a chaining point (`FM`)
- cab is carried on the fixed PRO segment (`PT`)
- cab drives customer from the unchaining point to destination (`LM`)
- cab reconnects to the next schedule anchor

After pickup, the customer stays in the cab for the whole trip.
The fixed PRO segment still anchors the candidate, but the surrounding cab legs may use slack if that is required by the customer time window.

## Selection logic

Candidates are still filtered in two steps:

1. time-feasible
2. energy-feasible

If several candidates remain, selection priority is:

1. earlier request completion
2. lower cab energy consumption
3. lower cab id
4. earlier PRO departure as final tie-break

Important: this is a cab-side comparison. PRO energy is currently not part of the ranking.

## Current assumptions

- PRO timetable is fixed
- chaining only happens at configured chaining locations
- `PT` does not consume cab battery
- no explicit waiting entry at the chaining point is modeled; if needed, slack appears implicitly before `PT` or after `PT`
- convoy is allowed if the overall gap and the customer time window are feasible, even if the cab legs do not glue perfectly to `PT`
- internal slack inside an existing `FM -> PT -> LM` block is not reused as a normal insertion gap
- each PRO trip has a `max_cabs` capacity and chosen trips are reserved at commit time
