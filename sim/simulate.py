"""Run one OpenRocket simulation for a rocket under given launch conditions.

This is step 2 of the build plan: launch conditions in, flight result out.
It drives OpenRocket 24.12 headless through the `orlab` library.

Examples
--------
    # OpenRocket's bundled "ARC payload rocket" example, with conditions on the command line
    python sim/simulate.py --example --elevation-ft 1200 --wind-mph 8 --humidity-pct 45 --temperature-f 62

    # Your own rocket, with conditions from a JSON file shaped like a launch_conditions record
    python sim/simulate.py --ork rocket.ork --conditions conditions.json

Units in and out are the app's units (ft, mph, %, °F, s); conversion to SI for
OpenRocket happens here and nowhere else.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import zipfile
from pathlib import Path

SCHEMA_VERSION = 1

# Same ranges as the app form and firestore.rules.
RANGES = {
    "elevation_ft": (-1500, 15000),
    "wind_mph": (0, 60),
    "humidity_pct": (0, 100),
    "temperature_f": (-40, 130),
}

# 2027 ARC values from the design doc. Confirm against the official rules
# (open question); they move to rules/2027 in Firestore later.
DEFAULT_TARGET_APOGEE_FT = 800.0
DURATION_WINDOW_S = (37.0, 40.0)

EXAMPLE_ORK = "datafiles/examples/ARC payload rocket.ork"

FT_PER_M = 1 / 0.3048
MPH_TO_MS = 0.44704


# ---------------------------------------------------------------------------
# Atmosphere: the app's four inputs -> what OpenRocket takes
# ---------------------------------------------------------------------------

def f_to_kelvin(temp_f: float) -> float:
    return (temp_f - 32.0) * 5.0 / 9.0 + 273.15


def isa_pressure_pa(elevation_m: float) -> float:
    """Standard-atmosphere station pressure at an elevation (troposphere)."""
    return 101325.0 * (1.0 - 2.25577e-5 * elevation_m) ** 5.25588


def virtual_temperature_k(temp_k: float, pressure_pa: float, humidity_pct: float) -> float:
    """Temperature dry air would need to have the same density as this humid air.

    OpenRocket's atmosphere is dry air, with no humidity input. Water vapour is
    lighter than air, so humid air is slightly less dense. Passing the virtual
    temperature gives OpenRocket the correct air density. The effect is small
    (well under 1% of density on a mild day) but free to include.
    """
    temp_c = temp_k - 273.15
    sat_vapour_pa = 610.94 * math.exp(17.625 * temp_c / (temp_c + 243.04))  # Magnus formula
    vapour_pa = humidity_pct / 100.0 * sat_vapour_pa
    return temp_k / (1.0 - (vapour_pa / pressure_pa) * (1.0 - 0.622))


def arc_score(apogee_ft: float, duration_s: float, target_apogee_ft: float) -> dict:
    """Assumed 2027 score: |altitude error| + 4 x seconds outside the duration window."""
    low, high = DURATION_WINDOW_S
    duration_error = max(0.0, low - duration_s, duration_s - high)
    altitude_error = abs(apogee_ft - target_apogee_ft)
    return {
        "total": round(altitude_error + 4.0 * duration_error, 1),
        "altitude_error_ft": round(altitude_error, 1),
        "duration_error_s": round(duration_error, 2),
        "formula": "abs(apogee_ft - target_apogee_ft) + 4 * duration_error_s (assumed; confirm 2027 rules)",
    }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_weather(args: argparse.Namespace) -> dict:
    weather: dict = {}
    if args.conditions:
        doc = json.loads(Path(args.conditions).read_text())
        weather.update(doc.get("weather", doc))  # a launch_conditions record, or the weather object alone
    for key in RANGES:
        value = getattr(args, key)
        if value is not None:
            weather[key] = value
    missing = [k for k in RANGES if k not in weather]
    if missing:
        sys.exit("Missing launch conditions: " + ", ".join(missing))
    for key, (low, high) in RANGES.items():
        value = weather[key]
        if not isinstance(value, (int, float)) or not low <= value <= high:
            sys.exit(f"{key} must be a number from {low} to {high}; got {value!r}")
    return {k: float(weather[k]) for k in RANGES}


def example_ork_path(jar_path: Path) -> Path:
    """Extract OpenRocket's bundled ARC example rocket from the jar."""
    out = Path(tempfile.gettempdir()) / "orlab-examples" / Path(EXAMPLE_ORK).name
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(jar_path) as jar:
            out.write_bytes(jar.read(EXAMPLE_ORK))
    return out


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate(ork: Path, weather: dict, *, motor: str | None, manufacturer: str | None, delay_s: float | None,
             target_apogee_ft: float, seed: int, branch: int, jar: str | None) -> dict:
    import orlab  # imported here so --help works without Java

    elevation_m = weather["elevation_ft"] * 0.3048
    pressure_pa = isa_pressure_pa(elevation_m)
    temp_k = f_to_kelvin(weather["temperature_f"])
    effective_temp_k = virtual_temperature_k(temp_k, pressure_pa, weather["humidity_pct"])

    with orlab.OpenRocketInstance(jar_path=jar, log_level="ERROR") as instance:
        orl = orlab.Helper(instance)
        doc = orl.load_doc(str(ork))
        sim = doc.getSimulation(0)
        opts = sim.getOptions()

        opts.setISAAtmosphere(False)            # use our temperature and pressure, not the standard day
        opts.setLaunchAltitude(elevation_m)
        opts.setLaunchPressure(pressure_pa)
        opts.setLaunchTemperature(effective_temp_k)
        opts.setWindSpeedAverage(weather["wind_mph"] * MPH_TO_MS)
        # Wind turbulence is random. A fixed seed repeats exactly within one process; across processes
        # OpenRocket 24.12's wind model adds its own randomness, so results differ by a fraction of a foot.
        opts.setRandomSeed(seed)

        if motor:
            orl.set_motor(sim, motor, manufacturer=manufacturer, delay=delay_s)
        elif delay_s is not None:
            sys.exit("--delay-s needs --motor")

        orl.run_simulation(sim, randomize_seed=False)
        summary = orl.get_summary(sim, branch_number=branch)

        components = [
            {"type": str(c.getClass().getSimpleName()), "name": str(c.getName()),
             "mass_g": round(float(c.getMass()) * 1000, 1)}
            for c in orl.get_all_components(doc.getRocket())
            if str(c.getClass().getSimpleName()) not in ("Rocket", "AxialStage")
        ]
        flown_motor = orl.get_motor(sim)
        wind_turbulence = float(opts.getWindTurbulenceIntensity())
        rod_length_m = float(opts.getLaunchRodLength())

    apogee_ft = summary.apogee * FT_PER_M
    duration_s = summary.flight_time
    return {
        "schema_version": SCHEMA_VERSION,
        "sim_version": "openrocket-24.12",
        "rocket": {"ork_file": ork.name, "motor": flown_motor, "components": components},
        "conditions": {
            "weather": weather,
            "derived": {
                "pressure_pa": round(pressure_pa, 1),
                "temperature_k": round(temp_k, 2),
                "density_equivalent_temperature_k": round(effective_temp_k, 2),
                "wind_ms": round(weather["wind_mph"] * MPH_TO_MS, 2),
                "wind_turbulence_intensity": wind_turbulence,
                "launch_rod_length_m": rod_length_m,
            },
            "target_apogee_ft": target_apogee_ft,
        },
        "seed": seed,
        "predicted": {
            "branch": summary.branch_name,
            "apogee_ft": round(apogee_ft, 1),
            "duration_s": round(duration_s, 2),
            "time_to_apogee_s": round(summary.time_to_apogee, 2),
            "max_velocity_fps": round(summary.max_velocity * FT_PER_M, 1),
            "max_mach": round(summary.max_mach, 3),
            "velocity_off_rod_fps": round(summary.velocity_off_rod * FT_PER_M, 1),
            "stability_off_rod_cal": round(summary.stability_off_rod_cal, 2),
            "min_stability_cal": round(summary.min_stability_cal, 2),
            "descent_rate_fps": round(summary.descent_rate * FT_PER_M, 1),
            "ground_hit_velocity_fps": round(summary.ground_hit_velocity * FT_PER_M, 1),
            "landing_distance_ft": round(summary.landing_distance * FT_PER_M, 1),
            "optimum_delay_s": round(summary.optimum_delay, 2) if summary.optimum_delay == summary.optimum_delay else None,
        },
        "score": arc_score(apogee_ft, duration_s, target_apogee_ft),
        "warnings": [str(w) for w in summary.warnings],
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--ork", type=Path, help="OpenRocket design file (.ork)")
    src.add_argument("--example", action="store_true", help="use OpenRocket's bundled 'ARC payload rocket' example")
    p.add_argument("--conditions", help="JSON file: a launch_conditions record or its weather object")
    p.add_argument("--elevation-ft", dest="elevation_ft", type=float)
    p.add_argument("--wind-mph", dest="wind_mph", type=float)
    p.add_argument("--humidity-pct", dest="humidity_pct", type=float)
    p.add_argument("--temperature-f", dest="temperature_f", type=float)
    p.add_argument("--motor", help="motor designation to fly instead of the file's, e.g. F50T")
    p.add_argument("--manufacturer", help="motor manufacturer, when the designation is made by several (e.g. Estes)")
    p.add_argument("--delay-s", type=float, help="ejection delay to use with --motor")
    p.add_argument("--target-apogee-ft", type=float, default=DEFAULT_TARGET_APOGEE_FT)
    p.add_argument("--seed", type=int, default=42,
                   help="random seed for wind turbulence (default 42); see the note in simulate() on repeatability")
    p.add_argument("--branch", type=int, default=0,
                   help="which section's flight to report when the rocket separates (default 0, usually the payload)")
    p.add_argument("--jar", help="path to an OpenRocket jar (default: the one `python -m orlab fetch` downloaded)")
    p.add_argument("--out", type=Path, help="write the result JSON here as well as printing it")
    args = p.parse_args(argv)

    weather = load_weather(args)
    if not args.example and not args.ork.exists():
        sys.exit(f"No such file: {args.ork}")

    # OpenRocket and orlab log to stdout. Point stdout at stderr while they run
    # so this script's stdout is only the result JSON.
    sys.stdout.flush()
    real_stdout = os.dup(1)
    os.dup2(2, 1)
    try:
        if args.example:
            import orlab
            ork = example_ork_path(Path(args.jar) if args.jar else Path(orlab.fetch_jar()))
        else:
            ork = args.ork
        result = simulate(ork, weather, motor=args.motor, manufacturer=args.manufacturer, delay_s=args.delay_s,
                          target_apogee_ft=args.target_apogee_ft, seed=args.seed,
                          branch=args.branch, jar=args.jar)
    finally:
        sys.stdout.flush()
        os.dup2(real_stdout, 1)
        os.close(real_stdout)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
