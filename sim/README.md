# Simulation

Python tools that drive **OpenRocket 24.12** headless through
[`orlab`](https://github.com/heibench/orlab):

| File | What it does |
|---|---|
| `simulate.py` | One flight of a rocket under the app's launch conditions; prints apogee, duration, stability and ARC score as JSON |
| `components.py` | Looks up the team's parts list (`data/components.csv`) in OpenRocket's parts catalogue and checks it against a design |
| `openrocket.py` | Starts OpenRocket with its parts catalogue connected, and sets **every** simulation option from our inputs |
| `flights.py` | Parses the team's flight log (`data/flights_2025_raw.csv`), flagging every assumption |
| `validate.py` | Simulates each real flight and compares; `--fit` writes the calibration (`data/calibration.json`) |

The team's design is `rockets/ederance.ork`.

## Setup (once)

Needs Python 3.11+ and Java 17 or 21.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r sim/requirements.txt
python -m orlab fetch          # downloads and verifies OpenRocket-24.12.jar
```

## Run

```bash
# The team rocket under the conditions saved from the app
python sim/simulate.py --ork rockets/ederance.ork --elevation-ft 1200 --wind-mph 8.5 --humidity-pct 45 --temperature-f 62

# Conditions from a saved app record (a launch_conditions document, or just its weather object)
python sim/simulate.py --ork rockets/ederance.ork --conditions conditions.json

# Try a different motor
python sim/simulate.py --ork rockets/ederance.ork --conditions conditions.json --motor F39T --manufacturer AeroTech --delay-s 5

# The rocket as built: measured pad weight, motor included
python sim/simulate.py --ork rockets/ederance.ork --conditions conditions.json --liftoff-mass-g 505

# Parts list: catalogue specs, checked against the design
python sim/components.py --ork rockets/ederance.ork

# Compare with the real flights, and refit the calibration
python sim/validate.py --elevation-ft 3740
python sim/validate.py --elevation-ft 3740 --fit
```

`simulate.py` applies `data/calibration.json` when it was fitted for the same `.ork`, and
records its `calibration_id`; `--no-calibration` simulates the design exactly as drawn.

Only JSON goes to stdout; OpenRocket's logs go to stderr.

## Simulation inputs are set here, not taken from the .ork

An `.ork` file stores its own simulations, and those go stale: `ederance.ork` has six,
with different elevations, winds, rail lengths, rail tilts and latitudes, three out of
date and one with no motor. `simulate.py` therefore takes **only the rocket and its motor
configuration** from the file. All 20 simulation options are set from the inputs:

| Input | Default | Source |
|---|---|---|
| Elevation, wind speed, humidity, air temperature | — (required) | The app's launch conditions |
| Station pressure | from elevation | Standard atmosphere |
| Wind turbulence | 10% | `--turbulence-pct` |
| Wind direction | from north | `--wind-from-deg` |
| Launch rail | 6 ft, vertical, not into wind | `--rail-length-ft`, `--rail-angle-deg`, `--launch-into-wind` (the design has rail buttons) |
| Latitude, longitude | 38.9°N, 77.8°W (ARC Finals, Great Meadow, VA) | `--latitude-deg`, `--longitude-deg`; affects gravity by under 0.1% |
| Time step | 0.01 s | `--time-step-s`; see the convergence check below |
| Maximum step angle, max time | 3°, 1,200 s | — |
| Motor | the design's first motor configuration | `--motor`, `--manufacturer`, `--delay-s` |

## Checks that the setup is right

**Matches OpenRocket desktop.** `ederance.ork`'s one up-to-date saved simulation, rerun:

| Run | Apogee | Duration |
|---|---|---|
| OpenRocket desktop (saved in the file) | 767.7 ft | 56.04 s |
| Same simulation, headless | 768.7 ft | 56.08 s |
| Same conditions through `openrocket.configure()` | 768.8 ft | 56.11 s |

The 1 ft difference is wind turbulence.

**Time step converged.** No wind, team rocket: apogee 792.68 ft at 0.05 s, 792.57 ft at
0.01 s, 792.58 ft at 0.002 s. The 0.01 s default is within 0.02 ft of the finest step.

**Repeatability.** Changing the seed within one process does not change OpenRocket
24.12's turbulence (20 seeds gave identical results); between processes, results vary
by about 1 ft. The Monte Carlo step must therefore vary wind, temperature, motor impulse
and mass explicitly, as the design doc already specifies, rather than rely on the seed.

## Validation against real flights

`data/flights_2025_raw.csv` logs 47 launches from January to May 2025. `flights.py`
keeps 38. It sets aside the 3 January flights of a 35 in build, 2 with nothing recorded,
3 far-low apogees (300, 536, 553 ft) and one 67 s duration. Hand-typed units are
interpreted and noted per row (for example "502.5 ft" in the weight column is read
as grams).

Each flight is simulated with its own temperature, wind and **measured weight**. The
design weighs 451 g, but the built rockets weighed 457–561 g, mostly about 505 g.
`openrocket.set_liftoff_mass()` sets the pad weight, keeping the structure's centre of
gravity where the design puts it.

The log does not record launch-site elevation, humidity, rail or motor. Assumed:
**3,740 ft** (where the `.ork`'s first saved simulation was set, near El Paso),
30% humidity, 6 ft rail, and the design's AeroTech F39T-6.

| | Apogee, sim − real | Duration, sim − real |
|---|---|---|
| Design as drawn, site at sea level | −51 ft (−6.5%) | +6.8 s (+17%) |
| Design as drawn, site at 3,740 ft | **−8 ft (−0.9%)**, ±38 ft per flight | +6.8 s (+17%) |
| Calibrated, site at 3,740 ft | **−8 ft (−0.9%)**, ±38 ft per flight; 24 of 38 within 5% | **0.0 s**, ±5.2 s per flight; 13 of 38 within 5% |

What this shows:

- **Apogee is right on average and follows weight.** Grouped by weight, simulated and
  real averages agree within about 10 ft (818 vs 829 ft at 450–480 g, 777 vs 766 at
  480–510 g, 745 vs 735 at 510–545 g). The ±38 ft per flight is close to the spread of
  the real apogees themselves (±41 ft); motor-to-motor impulse and altimeter readings
  are not in the log, so no model can predict them flight by flight.
- **The parachutes deliver about 70% of the drag the design assumes.** Calibrated
  parachute drag is 0.704 × the design's (Cd 0.56 instead of 0.80), which is common
  for real chutes (partial opening, shroud lines, the shock cord). Descent goes from
  about 17 to 21 ft/s.
- **Duration error changes by launch day** (from −2 s on 5/2 to +15 s in early March),
  and real duration barely changes with weight, which the physics says it should. That
  points to the recovery set-up changing between sessions. Logging which parachutes
  flew would shrink the ±5 s.

**Before relying on these numbers, confirm:** the launch-site elevation, the motor and
delay flown, whether the logged weight includes the motor, and which parachutes flew.

## The team rocket today

`rockets/ederance.ork`, AeroTech F39T-6, 3,740 ft, 60 °F, 2 mph wind, 30% humidity:

| | Design as drawn (451 g) | Calibrated, 451 g | Calibrated, as built (505 g) |
|---|---|---|---|
| Apogee | 826 ft | 826 ft | 764 ft |
| Duration | 56.6 s | 48.7 s | 43.3 s |
| Score (800 ft, 37–40 s) | 92.0 | 60.1 | 49.6 |
| Stability off the rail | 1.29 cal | 1.23 cal | 1.35 cal |

Duration is still 3 s over the window as built, and it costs 4 points per second.
The optimizer (next step) searches ballast, parachute and delay for the lowest
expected score.

## The parts list

`components.py` reads `data/components.csv`, finds each part in OpenRocket's catalogue
(about 1,500 body tubes and 67 nose cones at 66 mm alone), and reports dimensions,
material and mass. Tube masses come from the catalogue's dimensions and material
density. With `--ork` it checks each part against the design.

Findings on the current list:

- **Nose cone** → Apogee 14812, foam ellipsoid, 66 mm × 63.5 mm. Catalogue mass 29.4 g;
  the design overrides it to 20 g. Weigh it.
- **BT-80** → 66 mm, 18 in. In the design, cut to 325 mm.
- **BT-70** → 56.3 mm. In the design, cut to 320 mm.
- **BT-5 (motor mount) does not fit the motor.** BT-5 is 13 mm inside; the design flies a
  24 mm AeroTech F39T, which needs a BT-50 mount. The design itself uses a 24 mm inner
  tube, so the sheet is probably the thing to fix.

Optional CSV columns the loader already uses: `manufacturer`, `part_no`, `length_in`,
`measured_mass_g`. Measured mass always wins over the catalogue.

## What the physics is

OpenRocket is a six-degree-of-freedom flight simulator. Per its
[technical documentation](https://openrocket.info/documentation.html):

| Part | Method |
|---|---|
| Lift and centre of pressure | Extended Barrowman method, per component, with body lift at angle of attack and fin–body interference |
| Drag | Built up from the geometry: skin friction (Reynolds number and surface finish), nose and fin pressure drag, base drag, protrusions such as rail buttons, with compressibility corrections |
| Flight | 6-DOF rigid-body equations integrated with Runge–Kutta (RK4), mass and centre of gravity changing as the motor burns |
| Motors | Measured thrust curves from the built-in motor database |
| Atmosphere | Standard atmosphere adjusted to the launch-site temperature and pressure |
| Wind | Average speed plus random turbulence |
| Recovery | Parachute and streamer descent from their drag coefficient and size |

Drag comes from the geometry, so no wind-tunnel or CFD data is needed.

**Humidity.** OpenRocket has no humidity input. The script passes the *virtual
temperature*: the temperature dry air would need to have the same density as the humid
air. Humid air is slightly less dense, so apogee rises a few feet.

## Why OpenRocket and not another library

| Library | Strength | Why not the main engine |
|---|---|---|
| **OpenRocket** (via orlab) | Drag from the geometry; widely used; the team's `.ork` loads directly; motor database and parts catalogue built in; orlab adds parallel Monte Carlo | Needs Java on the worker |
| [RocketPy](https://github.com/RocketPy-Team/RocketPy) | Pure Python 6-DOF, strong Monte Carlo and weather tools, validated against real high-power flights | Needs a drag curve as input; built mainly for high-power rockets |
| RASAero II | Accurate drag, especially at high speed | Windows program with no scripting interface |
| RocketSim | The team already uses it | No batch or scripting interface found; kept as a cross-check |

## Note on starting OpenRocket headless

OpenRocket 24.12's headless startup does not connect its parts catalogue, so any `.ork`
that uses catalogue parts (as `ederance.ork` does) fails to load with
"No implementation for ComponentPresetDao was bound". `openrocket.py` adds that one
binding at startup.
