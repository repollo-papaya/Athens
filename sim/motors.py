"""Choose a motor for a 29 mm mount from the ARC 2027 approved list.

For every approved 29 mm motor (data/arc2027_motors.csv) this:

1. resizes the design's motor mount to 29 mm and loads the motor, with its thrust
   scaled to NAR's tested total impulse (the values ARC uses);
2. for each launch site and altitude target, finds the nose ballast that puts the
   apogee on the target, then the available ejection delay closest to the
   optimum, and re-trims the ballast for that delay;
3. checks the things that make a flight fail: stability off the rail, speed off
   the rail, thrust-to-weight, deployment speed at the chosen delay, mass limit,
   reloadable assembly;
4. runs a Monte Carlo robustness check (wind, temperature, humidity,
   motor-to-motor impulse, build mass) for the 800 ft qualification flight at
   El Paso and scores each motor on its expected ARC score.

    python sim/motors.py                       # all approved 29 mm motors, report
    python sim/motors.py --only F42T F67W      # a few
    python sim/motors.py --json --out motors.json

Calibration (data/calibration.json) is applied to the parachutes.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from multiprocessing import get_context
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
MOTORS_CSV = ROOT / "data" / "arc2027_motors.csv"
ORK = ROOT / "rockets" / "ederance.ork"
CALIBRATION = ROOT / "data" / "calibration.json"
FT_PER_M = 1 / 0.3048

# ARC 2027 parameters: qualification target 800 ft; Finals targets "will not be 800
# feet but will be between 775 and 825 feet"; duration 37-40 s for both.
DURATION_WINDOW_S = (37.0, 40.0)

# Limits used to call a flight safe. Assumptions are marked.
MIN_STABILITY_CAL = 1.0          # common minimum; ARC rules leave stability to the team and the RSO
MIN_RAIL_EXIT_FPS = 45.0         # common guidance for a stable departure
MIN_THRUST_TO_WEIGHT = 5.0       # NAR guidance for model rockets
MAX_DEPLOY_SPEED_FPS = 40.0      # faster deployments risk zippering or tearing the chute (assumed)
MAX_LIFTOFF_G = 650.0            # ARC 2027: no more than 650 g liftoff weight, motor included
MAX_IMPULSE_NS = 80.0            # ARC 2027: no more than 80 N-s total impulse
MAX_BALLAST_G = 250.0


@dataclass(frozen=True)
class Site:
    name: str
    elevation_ft: float
    temperature_f: float
    wind_mph: float
    humidity_pct: float
    latitude_deg: float
    longitude_deg: float
    targets_ft: tuple


SITES = (
    # Qualification flights at the team's field.
    Site("El Paso (qualification)", 3740, 70, 5, 25, 31.8, -106.4, (800.0,)),
    # ARC Finals, Great Meadow, The Plains, Virginia; elevation ASSUMED about 500 ft.
    # Both ends of the announced 775-825 ft range.
    Site("ARC Finals (Virginia)", 500, 75, 5, 60, 38.9, -77.8, (775.0, 825.0)),
)


def score(apogee_ft: float, duration_s: float, target_ft: float) -> float:
    low, high = DURATION_WINDOW_S
    return abs(apogee_ft - target_ft) + 4 * max(0.0, low - duration_s, duration_s - high)


def candidates(only: list[str] | None) -> list[dict]:
    rows = [r for r in csv.DictReader(MOTORS_CSV.open()) if r["status"] == "approved" and r["diameter_mm"] == "29"]
    if only:
        rows = [r for r in rows if r["designation"].upper() in {o.upper() for o in only}]
    return rows


# ---------------------------------------------------------------------------
# Worker: one JVM per process
# ---------------------------------------------------------------------------

_orl = None
_cal = 1.0


def _init_worker():
    global _orl, _cal
    os.dup2(os.open(os.devnull, os.O_WRONLY), 1)  # OpenRocket logs to stdout
    from openrocket import load_calibration, open_openrocket
    ctx = open_openrocket()
    _orl = ctx.__enter__()
    cal = load_calibration(CALIBRATION)
    _cal = cal["chute_cd_scale"] if cal and cal.get("rocket") == ORK.name else 1.0


def _fly(motor_row, motor_digest, scale, ballast_g, delay_s, site: Site, *, wind=None, temp=None, hum=None):
    import jpype
    from openrocket import (Launch, add_nose_ballast, configure, find_motor, set_mount_diameter,
                            thrust_scale_listener)
    orl = _orl
    doc = orl.load_doc(str(ORK))
    rocket = doc.getRocket()
    set_mount_diameter(rocket, 29, 125)
    add_nose_ballast(rocket, ballast_g)
    launch = Launch(site.elevation_ft, site.wind_mph if wind is None else wind, site.humidity_pct if hum is None else hum,
                    site.temperature_f if temp is None else temp, latitude_deg=site.latitude_deg,
                    longitude_deg=site.longitude_deg)
    sim = configure(orl, doc, launch, chute_cd_scale=_cal)
    motor = find_motor(motor_row["designation"], motor_row["manufacturer"], float(motor_row["total_impulse_ns"]), 29)
    orl.set_motor(sim, motor, delay=float(delay_s))
    orl.run_simulation(sim, listeners=[thrust_scale_listener(scale)], randomize_seed=False)
    s = orl.get_summary(sim)
    MassCalculator = jpype.JClass("info.openrocket.core.masscalc.MassCalculator")
    fc = rocket.getFlightConfiguration(sim.getFlightConfigurationId())
    liftoff_g = float(MassCalculator.calculateLaunch(fc).getMass()) * 1000
    return {
        "apogee_ft": s.apogee * FT_PER_M, "duration_s": s.flight_time,
        "stability_cal": s.stability_off_rod_cal, "min_stability_cal": s.min_stability_cal,
        "rail_exit_fps": s.velocity_off_rod * FT_PER_M, "optimum_delay_s": s.optimum_delay,
        "deploy_fps": s.velocity_at_deployment * FT_PER_M, "liftoff_g": liftoff_g,
        "max_accel_g": s.max_acceleration / 9.80665,
    }


def _delays(row, motor) -> tuple[list[float], str]:
    """Delays the team can actually use, and how they are set."""
    raw = row["delays_s"].split(";")
    standard = sorted({float(d) for d in motor.getStandardDelays() if float(d) > 0})
    if raw[0].endswith("A"):                     # Cesaroni adjustable: drill down from the maximum
        top = float(raw[0][:-1])
        return [d for d in standard if d <= top] or [top], f"adjustable up to {top:g} s"
    if raw[0] in ("S", "M", "L"):                # AeroTech F62T: short/medium/long
        return standard[:3], "S/M/L = " + "/".join(f"{d:g}" for d in standard[:3]) + " s"
    return [float(d) for d in raw if d.replace(".", "").isdigit()], "fixed"


def analyse(row: dict) -> dict:
    import jpype
    from openrocket import find_motor
    motor = find_motor(row["designation"], row["manufacturer"], float(row["total_impulse_ns"]), 29)
    if motor is None:
        return {"motor": row, "error": "not in OpenRocket's motor database"}
    nar_ns = float(row["total_impulse_ns"])
    db_ns = float(motor.getTotalImpulseEstimate())
    scale = nar_ns / db_ns
    delays, delay_kind = _delays(row, motor)
    avg_thrust_n = float(motor.getAverageThrustEstimate()) * scale
    out = {
        "designation": row["designation"], "manufacturer": row["manufacturer"],
        "reloadable": row["reloadable"] == "yes", "nar_impulse_ns": nar_ns,
        "database_motor": f"{motor.getManufacturer().getDisplayName()} {motor.getDesignation()}",
        "database_impulse_ns": round(db_ns, 1), "thrust_scale": round(scale, 3),
        "average_thrust_n": round(avg_thrust_n, 1), "burn_time_s": round(float(motor.getBurnTimeEstimate()), 2),
        "launch_mass_g": round(float(motor.getLaunchMass()) * 1000, 1),
        "delays_s": delays, "delay_kind": delay_kind, "note": row["note"], "cases": [],
    }

    def trim(site, target, delay):
        """Ballast that puts apogee on target with this delay, never less than stability needs."""
        at0 = _fly(row, None, scale, 0.0, delay, site)
        lo_b, start = 0.0, at0
        if at0["stability_cal"] < MIN_STABILITY_CAL:
            # Least nose ballast that makes the rocket stable enough.
            s_lo, s_hi = 0.0, MAX_BALLAST_G
            for _ in range(7):
                mid = (s_lo + s_hi) / 2
                s_lo, s_hi = (s_lo, mid) if _fly(row, None, scale, mid, delay, site)["stability_cal"] >= MIN_STABILITY_CAL else (mid, s_hi)
            lo_b = round(s_hi, 1)
            start = _fly(row, None, scale, lo_b, delay, site)
        if start["apogee_ft"] < target:
            why = "with no ballast" if lo_b == 0 else f"with the {lo_b:g} g ballast needed for stability"
            return lo_b, start, f"falls {target - start['apogee_ft']:.0f} ft short {why}"
        hi_b = MAX_BALLAST_G
        at_max = _fly(row, None, scale, hi_b, delay, site)
        if at_max["apogee_ft"] > target:
            return hi_b, at_max, f"still {at_max['apogee_ft'] - target:.0f} ft high with {hi_b:g} g ballast"
        for _ in range(8):
            mid = (lo_b + hi_b) / 2
            r = _fly(row, None, scale, mid, delay, site)
            lo_b, hi_b = (mid, hi_b) if r["apogee_ft"] > target else (lo_b, mid)
        b = round((lo_b + hi_b) / 2, 1)
        return b, _fly(row, None, scale, b, delay, site), None

    for site in SITES:
        for target in site.targets_ft:
            # First pass with a long delay so an early deployment cannot cap the apogee.
            b0, r0, problem = trim(site, target, max(delays))
            opt = r0["optimum_delay_s"]
            delay = min(delays, key=lambda d: abs(d - opt)) if opt == opt else max(delays)
            if problem is None and delay != max(delays):
                b, r, problem = trim(site, target, delay)
            else:
                b, r = b0, r0
            weight_n = r["liftoff_g"] / 1000 * 9.80665
            case = {
                "site": site.name, "target_ft": target, "ballast_g": b, "delay_s": delay,
                "optimum_delay_s": round(opt, 2) if opt == opt else None,
                **{k: round(v, 2) for k, v in r.items()},
                "thrust_to_weight": round(avg_thrust_n / weight_n, 1),
                "score": round(score(r["apogee_ft"], r["duration_s"], target), 1),
                "problems": [],
            }
            if problem:
                case["problems"].append(problem)
            if case["stability_cal"] < MIN_STABILITY_CAL:
                case["problems"].append(f"stability {case['stability_cal']:.2f} cal off the rail (< {MIN_STABILITY_CAL})")
            if case["rail_exit_fps"] < MIN_RAIL_EXIT_FPS:
                case["problems"].append(f"leaves the rail at {case['rail_exit_fps']:.0f} ft/s (< {MIN_RAIL_EXIT_FPS:g})")
            if case["thrust_to_weight"] < MIN_THRUST_TO_WEIGHT:
                case["problems"].append(f"thrust-to-weight {case['thrust_to_weight']} (< {MIN_THRUST_TO_WEIGHT:g})")
            if abs(case["deploy_fps"]) > MAX_DEPLOY_SPEED_FPS:
                case["problems"].append(f"chute opens at {abs(case['deploy_fps']):.0f} ft/s with the {delay:g} s delay")
            if case["liftoff_g"] > MAX_LIFTOFF_G:
                case["problems"].append(f"liftoff {case['liftoff_g']:.0f} g (> {MAX_LIFTOFF_G:g} g ARC limit)")
            out["cases"].append(case)

    # Robustness at El Paso, each target, ballast and delay as trimmed for the nominal day.
    rng = random.Random(f"{row['designation']}-{row['manufacturer']}")
    mc = {}
    for case in (c for c in out["cases"] if c["site"] == SITES[0].name):
        if any(category(p) in ("cannot reach a target", "needs more ballast than allowed") for p in case["problems"]):
            continue
        runs = []
        for _ in range(30):
            r = _fly(row, None, scale * rng.gauss(1.0, 0.03), max(0.0, case["ballast_g"] + rng.gauss(0, 5)),
                     case["delay_s"], SITES[0], wind=rng.uniform(0, 15), temp=rng.uniform(50, 95), hum=rng.uniform(5, 45))
            runs.append(r)
        ap = [r["apogee_ft"] for r in runs]
        du = [r["duration_s"] for r in runs]
        sc = sorted(score(r["apogee_ft"], r["duration_s"], case["target_ft"]) for r in runs)
        mc[str(int(case["target_ft"]))] = {
            "apogee_mean_ft": round(statistics.mean(ap), 1), "apogee_sd_ft": round(statistics.stdev(ap), 1),
            "duration_mean_s": round(statistics.mean(du), 1), "duration_sd_s": round(statistics.stdev(du), 2),
            "altitude_error_mean_ft": round(statistics.mean(abs(a - case["target_ft"]) for a in ap), 1),
            "score_mean": round(statistics.mean(sc), 1), "score_p90": round(sc[int(0.9 * (len(sc) - 1))], 1),
            "min_stability_cal": round(min(r["stability_cal"] for r in runs), 2),
            "min_rail_exit_fps": round(min(r["rail_exit_fps"] for r in runs), 1),
        }
    out["monte_carlo_el_paso"] = mc
    return out


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def category(problem: str) -> str:
    for key, name in (("short", "cannot reach a target"), ("still", "needs more ballast than allowed"),
                      ("stability", "unstable"), ("leaves the rail", "slow off the rail"),
                      ("thrust-to-weight", "low thrust-to-weight"), ("chute opens", "no delay near the optimum"),
                      ("liftoff", "over the mass limit")):
        if key in problem:
            return name
    return problem


def rank(results: list[dict]) -> list[dict]:
    for r in results:
        if "error" in r:
            r["feasible"], r["risk"], r["rank_key"] = False, 99, (2, 99, 9e9)
            continue
        problems = [p for c in r["cases"] for p in c["problems"]]
        categories = {category(p) for p in problems}
        # Rule violations and unreachable targets rule a motor out; the rest are risks.
        reach = {"cannot reach a target", "needs more ballast than allowed", "over the mass limit"} & categories
        r["feasible"] = not reach and bool(r["monte_carlo_el_paso"])
        if r["reloadable"]:
            categories.add("reloadable (adult assembly, more failure points)")
        r["risk_flags"] = sorted(categories)
        r["risk"] = len(categories - reach)
        mc = r["monte_carlo_el_paso"]
        expected = statistics.mean(m["score_mean"] for m in mc.values()) if mc else 9e9
        r["expected_score"] = round(expected, 1)
        r["rank_key"] = (0 if r["feasible"] else 1, r["risk"], expected)
    return sorted(results, key=lambda r: r["rank_key"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="motor designations to study (default: all approved 29 mm)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, help="also write the JSON results here")
    args = ap.parse_args()

    rows = candidates(args.only)
    # Estes-branded F26FJ and F50T are the AeroTech motors; study once, report both.
    unique, aliases = [], {}
    for r in rows:
        key = (r["designation"], r["total_impulse_ns"])
        if key in aliases:
            aliases[key].append(r["manufacturer"])
            continue
        aliases[key] = [r["manufacturer"]]
        unique.append(r)
    print(f"Studying {len(unique)} motors ({len(rows)} approved 29 mm entries) on {args.workers} workers…", file=sys.stderr)

    with ProcessPoolExecutor(args.workers, mp_context=get_context("spawn"), initializer=_init_worker) as pool:
        results = list(pool.map(analyse, unique))
    for r in results:
        r["sold_as"] = aliases[(r.get("designation", ""), str(r.get("nar_impulse_ns", "")))] if "designation" in r else []
    results = rank(results)

    report = {"site_assumptions": [asdict(s) for s in SITES],
              "limits": {"min_stability_cal": MIN_STABILITY_CAL, "min_rail_exit_fps": MIN_RAIL_EXIT_FPS,
                         "min_thrust_to_weight": MIN_THRUST_TO_WEIGHT, "max_deploy_fps": MAX_DEPLOY_SPEED_FPS,
                         "max_liftoff_g_assumed": MAX_LIFTOFF_G},
              "motors": results}
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    if args.json:
        print(text)
        return
    for i, r in enumerate(results, 1):
        if "error" in r:
            print(f"{i:>2}. {r['motor']['designation']}: {r['error']}")
            continue
        tag = "OK " if r["feasible"] else "NO "
        print(f"{i:>2}. {tag}{r['designation']:7} {'/'.join(r['sold_as']):16} {'reload' if r['reloadable'] else 'single-use':10} "
              f"{r['nar_impulse_ns']:5.1f} Ns  avg {r['average_thrust_n']:5.1f} N  exp.score {r['expected_score']:7.1f}  risk {r['risk']}  "
              f"{'; '.join(r['risk_flags'])}")


if __name__ == "__main__":
    main()
