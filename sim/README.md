# Simulation

`simulate.py` runs one OpenRocket flight for a rocket under the launch conditions the
app collects (elevation, wind speed, humidity, air temperature) and prints the result
as JSON: apogee, flight duration, stability, landing distance and the ARC score.

It drives **OpenRocket 24.12** headless from Python through
[`orlab`](https://github.com/heibench/orlab).

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
# OpenRocket's bundled "ARC payload rocket" example
python sim/simulate.py --example --elevation-ft 1200 --wind-mph 8 --humidity-pct 45 --temperature-f 62

# Your rocket
python sim/simulate.py --ork path/to/rocket.ork --elevation-ft 1200 --wind-mph 8 --humidity-pct 45 --temperature-f 62

# Conditions from a saved app record (a launch_conditions document, or just its weather object)
python sim/simulate.py --ork rocket.ork --conditions conditions.json

# Try a different motor
python sim/simulate.py --ork rocket.ork --conditions conditions.json --motor E12 --manufacturer Estes --delay-s 4
```

Other options: `--target-apogee-ft` (default 800), `--seed`, `--branch` (which section to
report when the rocket separates; 0 is usually the payload), `--out result.json`.
Only the JSON goes to stdout; OpenRocket's logs go to stderr.

The `.ork` file's first simulation supplies the launch rod, wind direction and
turbulence, and the motor unless `--motor` is given.

## What the physics is

OpenRocket is a six-degree-of-freedom flight simulator. Per its
[technical documentation](https://openrocket.info/documentation.html):

| Part | Method |
|---|---|
| Lift and centre of pressure | Extended Barrowman method, per component (nose, body, transitions, fins), with body lift at angle of attack and fin–body interference |
| Drag | Built up from the geometry: skin friction (from Reynolds number and surface finish), nose and fin pressure drag, base drag, launch lugs and other protrusions, with compressibility corrections |
| Flight | 6-DOF rigid-body equations integrated with a Runge–Kutta (RK4) solver, mass and centre of gravity changing as the motor burns |
| Motors | Measured thrust curves from its built-in motor database |
| Atmosphere | International Standard Atmosphere, adjusted to the launch-site temperature and pressure |
| Wind | Average speed plus random turbulence |
| Recovery | Parachute and streamer descent from their drag coefficients and size |

Because drag comes from the geometry, OpenRocket needs only the rocket's shape and
masses, with no wind-tunnel or CFD data. That matters for small ARC rockets, which
never have measured drag curves.

### How the app's four inputs reach OpenRocket

| App input | OpenRocket setting |
|---|---|
| Elevation (ft) | Launch altitude; station pressure computed from it with the standard atmosphere |
| Air temperature (°F) | Launch temperature |
| Humidity (%) | OpenRocket has no humidity input. The script passes the *virtual temperature*: the temperature dry air would need to have the same density as the humid air. Humid air is slightly less dense, so apogee rises a few feet. |
| Wind speed (mph) | Average wind speed |

### Repeatability

Turbulence is random. With the same `--seed`, results repeat exactly within one process,
but OpenRocket 24.12's wind model adds its own randomness in each new process, so
separate runs of the script differ by a fraction of a foot when there is wind. The
optimizer's Monte Carlo step averages over many runs, so this does not affect rankings.

## Why OpenRocket and not another library

| Library | Strength | Why not the main engine |
|---|---|---|
| **OpenRocket** (via orlab) | Computes drag from the geometry; widely used and checked by hobby and student teams; the team's `.ork` files load directly; motor database built in; orlab adds parallel Monte Carlo runs | Needs Java on the worker |
| [RocketPy](https://github.com/RocketPy-Team/RocketPy) | Pure Python 6-DOF, strong Monte Carlo and weather tools, validated against real high-power flights | Needs a drag curve as input (from OpenRocket, RASAero or CFD); built mainly for high-power rockets |
| RASAero II | Accurate drag, especially at high speed | Windows program with no scripting interface |
| RocketSim | The team already uses it | No batch or scripting interface found; kept as a cross-check |

OpenRocket is the main engine. RocketPy is a good second opinion later, fed with
OpenRocket's drag curve.

## Sample result

OpenRocket's ARC example rocket at 1,200 ft, 8 mph, 45% humidity, 62 °F:

| Motor | Apogee | Duration | Score (target 800 ft) |
|---|---|---|---|
| F50T (the file's motor) | 1,521 ft | 111 s | 1,006 |
| Estes E12, 4 s delay | 584 ft | 47.4 s | 246 |

The example is OpenRocket's demonstration rocket, not the team's, so these numbers say
nothing about the team's design.
