"""Check the simulator against the team's real flights.

Simulates every usable flight in data/flights_2025_raw.csv under that flight's own
temperature, wind and measured weight, and compares apogee and duration with what
the altimeter recorded. This is build-plan step 1: the optimizer is only trusted
once the simulator matches real flights within the team's tolerance.

    python sim/validate.py --elevation-ft 3740
    python sim/validate.py --elevation-ft 0 --elevation-ft 3740 --json

The flight log does not record launch-site elevation, humidity or motor, so
those are inputs here (see --help). Each flight is assumed to fly the design's
motor configuration.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FT_PER_M = 1 / 0.3048
CALIBRATION = Path(__file__).resolve().parent.parent / "data" / "calibration.json"


def stats(errors: list[float], actual: list[float]) -> dict:
    pct = [e / a * 100 for e, a in zip(errors, actual)]
    return {
        "mean_error": round(statistics.mean(errors), 1),
        "mean_abs_error": round(statistics.mean(abs(e) for e in errors), 1),
        "rms_error": round(math.sqrt(statistics.mean(e * e for e in errors)), 1),
        "sd_error": round(statistics.stdev(errors), 1),
        "mean_error_pct": round(statistics.mean(pct), 1),
        "within_3_pct": sum(abs(p) <= 3 for p in pct),
        "within_5_pct": sum(abs(p) <= 5 for p in pct),
        "n": len(errors),
    }


def simulate_flight(orl, ork: Path, f: dict, elevation: float, humidity_pct: float, rail_length_ft: float,
                    chute_cd_scale: float):
    from openrocket import Launch, configure
    doc = orl.load_doc(str(ork))
    launch = Launch(elevation_ft=elevation, wind_mph=f["wind_mph"] or 0.0, humidity_pct=humidity_pct,
                    temperature_f=f["temperature_f"], rail_length_ft=rail_length_ft)
    sim = configure(orl, doc, launch, liftoff_mass_g=f["weight_g"], chute_cd_scale=chute_cd_scale)
    orl.run_simulation(sim, randomize_seed=False)
    return orl.get_summary(sim)


def fit_chute_cd_scale(orl, ork, flights, elevation, humidity_pct, rail_length_ft) -> float:
    """Parachute drag scale that makes the mean simulated duration match the real mean."""
    target = statistics.mean(f["duration_s"] for f in flights)
    lo, hi = 0.2, 1.5
    for _ in range(9):  # bisection; duration rises with chute drag
        k = (lo + hi) / 2
        mean = statistics.mean(simulate_flight(orl, ork, f, elevation, humidity_pct, rail_length_ft, k).flight_time
                               for f in flights)
        lo, hi = (lo, k) if mean > target else (k, hi)
    return round((lo + hi) / 2, 3)


def run(ork: Path, elevations: list[float], humidity_pct: float, rail_length_ft: float,
        chute_cd_scale: float = 1.0, fit: bool = False, jar=None) -> dict:
    from flights import parse
    from openrocket import open_openrocket

    flights = [f for f in parse() if not f["exclude"]]
    report = {"ork": ork.name, "humidity_pct": humidity_pct, "rail_length_ft": rail_length_ft,
              "flights_used": len(flights), "chute_cd_scale": chute_cd_scale, "by_elevation": []}
    with open_openrocket(jar) as orl:
        if fit:
            chute_cd_scale = fit_chute_cd_scale(orl, ork, flights, elevations[0], humidity_pct, rail_length_ft)
            report["chute_cd_scale"] = chute_cd_scale
        for elevation in elevations:
            rows = []
            for f in flights:
                s = simulate_flight(orl, ork, f, elevation, humidity_pct, rail_length_ft, chute_cd_scale)
                rows.append({
                    "row": f["row"], "date": f["date"], "launch": f["launch"],
                    "temperature_f": f["temperature_f"], "wind_mph": f["wind_mph"], "weight_g": f["weight_g"],
                    "actual_apogee_ft": f["apogee_ft"], "sim_apogee_ft": round(s.apogee * FT_PER_M, 1),
                    "actual_duration_s": f["duration_s"], "sim_duration_s": round(s.flight_time, 2),
                })
            for r in rows:
                r["apogee_error_ft"] = round(r["sim_apogee_ft"] - r["actual_apogee_ft"], 1)
                r["duration_error_s"] = round(r["sim_duration_s"] - r["actual_duration_s"], 2)
            report["by_elevation"].append({
                "elevation_ft": elevation,
                "apogee": stats([r["apogee_error_ft"] for r in rows], [r["actual_apogee_ft"] for r in rows]),
                "duration": stats([r["duration_error_s"] for r in rows], [r["actual_duration_s"] for r in rows]),
                "flights": rows,
            })
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ork", type=Path, default=Path(__file__).resolve().parent.parent / "rockets" / "ederance.ork")
    ap.add_argument("--elevation-ft", type=float, action="append",
                    help="launch-site elevation; repeat to compare several (default 0 and 3740)")
    ap.add_argument("--humidity-pct", type=float, default=30.0, help="not in the log (default 30)")
    ap.add_argument("--rail-length-ft", type=float, default=6.0, help="not in the log (default 6)")
    ap.add_argument("--chute-cd-scale", type=float, default=1.0, help="parachute drag multiplier to test (default 1)")
    ap.add_argument("--fit", action="store_true",
                    help="fit the parachute drag multiplier at the first elevation and write data/calibration.json")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, help="also write the JSON report here")
    args = ap.parse_args()
    elevations = args.elevation_ft or [0.0, 3740.0]

    sys.stdout.flush()
    real_stdout = os.dup(1)
    os.dup2(2, 1)  # OpenRocket logs to stdout
    try:
        report = run(args.ork, elevations, args.humidity_pct, args.rail_length_ft,
                     chute_cd_scale=args.chute_cd_scale, fit=args.fit)
    finally:
        sys.stdout.flush()
        os.dup2(real_stdout, 1)
        os.close(real_stdout)

    if args.fit:
        block = report["by_elevation"][0]
        calibration = {
            "calibration_id": f"cal_{date.today():%Y%m%d}",
            "fitted_on": date.today().isoformat(),
            "rocket": args.ork.name,
            "chute_cd_scale": report["chute_cd_scale"],
            "what": "multiplies every parachute's drag coefficient so simulated durations match real flights",
            "flights": report["flights_used"],
            "assumptions": {"elevation_ft": block["elevation_ft"], "humidity_pct": args.humidity_pct,
                            "rail_length_ft": args.rail_length_ft, "motor": "design motor configuration",
                            "weight": "measured pad weight, motor included"},
            "after_fit": {"apogee": block["apogee"], "duration": block["duration"]},
        }
        CALIBRATION.write_text(json.dumps(calibration, indent=2) + "\n")
        print(f"Wrote {CALIBRATION.relative_to(CALIBRATION.parent.parent)}: chute_cd_scale {report['chute_cd_scale']}\n",
              file=sys.stderr)

    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n")
    if args.json:
        print(text)
        return
    print(f"{report['flights_used']} flights, {args.ork.name}, humidity {args.humidity_pct:g}%, "
          f"rail {args.rail_length_ft:g} ft, parachute drag x{report['chute_cd_scale']}\n")
    for block in report["by_elevation"]:
        a, d = block["apogee"], block["duration"]
        print(f"Launch site at {block['elevation_ft']:g} ft")
        print(f"  Apogee   sim - real: mean {a['mean_error']:+.1f} ft ({a['mean_error_pct']:+.1f}%), "
              f"spread ±{a['sd_error']:.1f} ft, within 3%: {a['within_3_pct']}/{a['n']}, within 5%: {a['within_5_pct']}/{a['n']}")
        print(f"  Duration sim - real: mean {d['mean_error']:+.2f} s ({d['mean_error_pct']:+.1f}%), "
              f"spread ±{d['sd_error']:.2f} s, within 5%: {d['within_5_pct']}/{d['n']}\n")


if __name__ == "__main__":
    main()
