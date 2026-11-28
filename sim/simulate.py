"""Run one OpenRocket flight for a rocket under given launch conditions.

Launch conditions in, flight result out, as JSON. Every simulation setting is set
here from the inputs below (see openrocket.py); nothing is taken from the
simulations saved in the .ork file.

Examples
--------
    python sim/simulate.py --ork rockets/ederance.ork --elevation-ft 1200 --wind-mph 8 --humidity-pct 45 --temperature-f 62
    python sim/simulate.py --ork rockets/ederance.ork --conditions conditions.json
    python sim/simulate.py --example --elevation-ft 0 --wind-mph 5 --humidity-pct 50 --temperature-f 59

Units in and out are the app's (ft, mph, %, °F, s).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA_VERSION = 2

# Same ranges as the app form and firestore.rules.
RANGES = {
    "elevation_ft": (-1500, 15000),
    "wind_mph": (0, 60),
    "humidity_pct": (0, 100),
    "temperature_f": (-40, 130),
}

# 2027 ARC values from the design doc; confirm against the official rules.
DEFAULT_TARGET_APOGEE_FT = 800.0
DURATION_WINDOW_S = (37.0, 40.0)

EXAMPLE_ORK = "datafiles/examples/ARC payload rocket.ork"
DEFAULT_CALIBRATION = Path(__file__).resolve().parent.parent / "data" / "calibration.json"
FT_PER_M = 1 / 0.3048


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


def load_weather(args: argparse.Namespace) -> dict:
    weather: dict = {}
    if args.conditions:
        doc = json.loads(Path(args.conditions).read_text())
        weather.update(doc.get("weather", doc))  # a launch_conditions record, or its weather object
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
    out = Path(tempfile.gettempdir()) / "orlab-examples" / Path(EXAMPLE_ORK).name
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(jar_path) as jar:
            out.write_bytes(jar.read(EXAMPLE_ORK))
    return out


def run(ork: Path, launch, *, motor, manufacturer, delay_s, target_apogee_ft, branch, jar,
        liftoff_mass_g=None, calibration=None) -> dict:
    from openrocket import open_openrocket
    with open_openrocket(jar) as orl:
        return run_with(orl, ork, launch, motor=motor, manufacturer=manufacturer, delay_s=delay_s,
                        target_apogee_ft=target_apogee_ft, branch=branch,
                        liftoff_mass_g=liftoff_mass_g, calibration=calibration)


def run_with(orl, ork: Path, launch, *, motor=None, manufacturer=None, delay_s=None,
             target_apogee_ft=DEFAULT_TARGET_APOGEE_FT, branch=0, liftoff_mass_g=None, calibration=None,
             prepare=None, extra_mass_g=None, listeners=None, keep_doc=False, doc=None) -> dict:
    """One flight on an already-running OpenRocket (the worker keeps one open).

    `prepare(rocket)` changes the design before the flight (tube lengths, mount,
    ballast); `extra_mass_g` adds or removes mass keeping the CG; `listeners` run
    during the flight (e.g. thrust scaling). With `keep_doc`, the changed document
    is returned under "_doc" so the caller can export the design.
    """
    import orlab
    from openrocket import configure, describe_rocket

    scale = calibration["chute_cd_scale"] if calibration else 1.0
    if doc is None:
        doc = orl.load_doc(str(ork))
    if prepare:
        prepare(doc.getRocket())
    sim = configure(orl, doc, launch, motor=motor, manufacturer=manufacturer, delay_s=delay_s,
                    liftoff_mass_g=liftoff_mass_g, chute_cd_scale=scale, extra_mass_g=extra_mass_g)
    orl.run_simulation(sim, listeners=listeners or None, randomize_seed=False)
    s = orl.get_summary(sim, branch_number=branch)
    T = orlab.FlightDataType
    series = orl.get_timeseries(sim, [T.TYPE_MASS, T.TYPE_CG_LOCATION], branch_number=branch)
    rocket = describe_rocket(orl, doc, sim)

    liftoff = {
        "mass_g": round(float(series[T.TYPE_MASS][0]) * 1000, 1),
        "cg_from_nose_in": round(float(series[T.TYPE_CG_LOCATION][0]) / 0.0254, 2),
    }
    apogee_ft = s.apogee * FT_PER_M
    return {
        "schema_version": SCHEMA_VERSION,
        "sim_version": "openrocket-24.12",
        "calibration_id": calibration["calibration_id"] if calibration else None,
        "rocket": {"ork_file": ork.name, **rocket, "liftoff": liftoff},
        "launch": {**asdict(launch), "derived": {k: round(v, 2) for k, v in launch.derived().items()}},
        "predicted": {
            "branch": str(s.branch_name),
            "apogee_ft": round(apogee_ft, 1),
            "duration_s": round(s.flight_time, 2),
            "time_to_apogee_s": round(s.time_to_apogee, 2),
            "optimum_delay_s": round(s.optimum_delay, 2) if s.optimum_delay == s.optimum_delay else None,
            "max_velocity_fps": round(s.max_velocity * FT_PER_M, 1),
            "max_mach": round(s.max_mach, 3),
            "max_acceleration_g": round(s.max_acceleration / 9.80665, 1),
            "velocity_off_rail_fps": round(s.velocity_off_rod * FT_PER_M, 1),
            "stability_off_rail_cal": round(s.stability_off_rod_cal, 2),
            "min_stability_cal": round(s.min_stability_cal, 2),
            "velocity_at_deployment_fps": round(s.velocity_at_deployment * FT_PER_M, 1),
            "descent_rate_fps": round(s.descent_rate * FT_PER_M, 1),
            "ground_hit_velocity_fps": round(s.ground_hit_velocity * FT_PER_M, 1),
            "landing_distance_ft": round(s.landing_distance * FT_PER_M, 1),
        },
        "score": arc_score(apogee_ft, s.flight_time, target_apogee_ft),
        "target_apogee_ft": target_apogee_ft,
        "warnings": [str(w) for w in s.warnings]
                    + ([] if calibration else ["uncalibrated: no calibration applied"]),
        **({"_doc": doc} if keep_doc else {}),
    }


def main(argv: list[str] | None = None) -> None:
    from openrocket import Launch

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--ork", type=Path, help="OpenRocket design file (.ork)")
    src.add_argument("--example", action="store_true", help="use OpenRocket's bundled 'ARC payload rocket' example")
    g = p.add_argument_group("launch conditions (the app's four inputs)")
    g.add_argument("--conditions", help="JSON file: a launch_conditions record or its weather object")
    g.add_argument("--elevation-ft", dest="elevation_ft", type=float)
    g.add_argument("--wind-mph", dest="wind_mph", type=float)
    g.add_argument("--humidity-pct", dest="humidity_pct", type=float)
    g.add_argument("--temperature-f", dest="temperature_f", type=float)
    d = Launch(0, 0, 0, 0)
    g = p.add_argument_group("launch site and rail")
    g.add_argument("--turbulence-pct", type=float, default=d.turbulence_pct, help=f"wind gustiness (default {d.turbulence_pct:g})")
    g.add_argument("--wind-from-deg", type=float, default=d.wind_from_deg, help="compass direction the wind blows from")
    g.add_argument("--rail-length-ft", type=float, default=d.rail_length_ft, help=f"default {d.rail_length_ft:g}")
    g.add_argument("--rail-angle-deg", type=float, default=d.rail_angle_deg, help="tilt from vertical (default 0)")
    g.add_argument("--launch-into-wind", action="store_true", help="tilt the rail into the wind")
    g.add_argument("--latitude-deg", type=float, default=d.latitude_deg, help=f"default {d.latitude_deg} (ARC Finals)")
    g.add_argument("--longitude-deg", type=float, default=d.longitude_deg)
    g = p.add_argument_group("motor")
    g.add_argument("--motor", help="motor designation to fly instead of the design's, e.g. F39T")
    g.add_argument("--manufacturer", help="motor manufacturer, when several make that designation")
    g.add_argument("--delay-s", type=float, help="ejection delay to use with --motor")
    g = p.add_argument_group("rocket as built")
    g.add_argument("--liftoff-mass-g", type=float, help="measured pad weight, motor included (default: the design's)")
    g.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION,
                   help="calibration from real flights (default data/calibration.json, written by validate.py --fit)")
    g.add_argument("--no-calibration", action="store_true", help="simulate the design exactly as drawn")
    g = p.add_argument_group("run")
    g.add_argument("--target-apogee-ft", type=float, default=DEFAULT_TARGET_APOGEE_FT)
    g.add_argument("--seed", type=int, default=d.seed, help="random seed for wind turbulence")
    g.add_argument("--time-step-s", type=float, default=d.time_step_s, help=f"integrator step (default {d.time_step_s})")
    g.add_argument("--branch", type=int, default=0, help="which section to report if the rocket separates (default 0)")
    g.add_argument("--jar", help="OpenRocket jar (default: the one `python -m orlab fetch` downloaded)")
    g.add_argument("--out", type=Path, help="also write the result JSON here")
    args = p.parse_args(argv)

    weather = load_weather(args)
    launch = Launch(**weather, turbulence_pct=args.turbulence_pct, wind_from_deg=args.wind_from_deg,
                    rail_length_ft=args.rail_length_ft, rail_angle_deg=args.rail_angle_deg,
                    launch_into_wind=args.launch_into_wind, latitude_deg=args.latitude_deg,
                    longitude_deg=args.longitude_deg, time_step_s=args.time_step_s, seed=args.seed)
    if args.ork and not args.ork.exists():
        sys.exit(f"No such file: {args.ork}")

    # OpenRocket logs to stdout; point stdout at stderr while it runs so stdout is only JSON.
    sys.stdout.flush()
    real_stdout = os.dup(1)
    os.dup2(2, 1)
    try:
        if args.example:
            import orlab
            ork = example_ork_path(Path(args.jar) if args.jar else Path(orlab.fetch_jar()))
        else:
            ork = args.ork
        from openrocket import load_calibration
        calibration = None
        if not args.no_calibration and not args.example:
            calibration = load_calibration(args.calibration)
            from openrocket import calibration_for
            calibration = calibration_for(calibration, ork.name)  # None if fitted for another rocket
        result = run(ork, launch, motor=args.motor, manufacturer=args.manufacturer, delay_s=args.delay_s,
                     target_apogee_ft=args.target_apogee_ft, branch=args.branch, jar=args.jar,
                     liftoff_mass_g=args.liftoff_mass_g, calibration=calibration)
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
