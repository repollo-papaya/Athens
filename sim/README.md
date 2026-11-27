# Simulation

Python tools that drive **OpenRocket 24.12** headless through
[`orlab`](https://github.com/heibench/orlab):

| File | What it does |
|---|---|
| `simulate.py` | One flight of a rocket under the app's launch conditions; prints apogee, duration, stability and ARC score as JSON |
| `components.py` | Looks up the team's parts list (`data/components.csv`) in OpenRocket's parts catalogue and checks it against a design |
| `openrocket.py` | Starts OpenRocket with its parts catalogue connected, and sets **every** simulation option from our inputs |

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

# Parts list: catalogue specs, checked against the design
python sim/components.py --ork rockets/ederance.ork
```

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

## The team rocket today

`rockets/ederance.ork`, AeroTech F39T-6, under the conditions saved from the app
(1,200 ft, 8.5 mph, 45%, 62 °F):

| Result | Value | Note |
|---|---|---|
| Liftoff mass | 451 g | |
| Apogee | 784 ft | 16 ft under 800 |
| Duration | 56.0 s | **16 s over** the 37–40 s window |
| Score | 80.1 | 16.1 altitude + 4 × 16.0 duration |
| Stability off the rail | 0.96 cal | **under 1.0** |
| Minimum stability in flight | 0.71 cal | |
| Optimum ejection delay | 5.3 s | the motor has a 6 s delay |
| Descent rate | 16 ft/s | two parachutes (21 in and 18 in) |

Duration costs four times as much as altitude, so the parachutes are the biggest lever.
That is the optimizer's job (next step), not a hand fix here.

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
