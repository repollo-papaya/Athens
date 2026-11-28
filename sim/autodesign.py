"""Design rockets automatically for the ARC goals, with OpenRocket in the loop.

Every design is a part list in the builder's format (app/builder.js, sim/build.py), made from the
team's parts: the ARC foam nose cone, a BT-80 payload tube with two eggs in protectors and an
altimeter bay, the BT-80 to BT-70 transition, and a BT-70 booster tube with the motor mount,
centering rings, shock cord, parachute and fins.

How it searches
  1. Screen: every approved motor flies once in a baseline rocket; motors that can't reach the
     target, or would need the rocket to go over the mass limit, are dropped.
  2. Shape: for each promising motor, the fins are sized (smallest span that gives the wanted
     stability) and the tubes trimmed to the shortest legal airframe that fits the motor.
  3. Tune: nose ballast sets apogee, parachute size sets duration, the ejection delay is the
     available one nearest the optimum. A few OpenRocket flights per design (secant steps).
  4. Rank: ARC score first, then how much apogee moves between 0 and 10 mph of wind, then mass
     margin under the limit.

    python sim/autodesign.py                      # El Paso standard day, 800 ft, about 3 minutes
    python sim/autodesign.py --target-ft 775 --budget-s 120 --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
MOTOR_MENU = ROOT / "data" / "motor_menu.json"
TEAM_DESIGN = ROOT / "designs" / "ederance-29mm.json"

MASS_LIMIT_G = 650.0
EGG_MARGIN_G = 16.0          # two large eggs can weigh 16 g more than average
LENGTH_MIN_MM = 650.0
T80_MIN_MM = 304.8           # 12 in
DURATION_WINDOW = (37.0, 40.0)
DURATION_GOAL = 38.5
STABILITY_GOAL = 1.3         # fins are sized for this with no ballast; ballast only adds to it
BT80 = (33.02, 0.53)         # outer radius, wall (mm)
BT70 = (28.16, 0.47)
DENSITY = {"Paper (spiral tube)": 0.9, "Cardboard": 0.68, "Birch plywood": 0.63, "Balsa": 0.13}   # g/cm³, as app/builder.js
CLOTH_NYLON = 67.0           # g/m²

r1 = lambda v: round(v * 10) / 10


# ---------------------------------------------------------------------------
# Part list (same formulas as app/builder.js so the app shows the same weights)
# ---------------------------------------------------------------------------

def _shell(L, ro, ri, dens):
    return r1(dens * math.pi * max(0.0, ro * ro - ri * ri) * L / 1000)


def _fins_mass(root, tip, span, thick, n, dens):
    return r1(dens * (root + tip) / 2 * span * thick * n / 1000)


def _chute_mass(dia_mm, lines=6, line_len=457):
    return r1(math.pi * (dia_mm / 2000) ** 2 * CLOTH_NYLON + lines * line_len / 1000 * 1.5)


def make_spec(team: dict, d: dict) -> dict:
    """A complete rocket from design choices d (motor entry, tube lengths, fins, chute, ballast, payload)."""
    m = d["motor"]
    tc = {c["id"]: c for c in team["components"]}
    take = lambda i, **kw: {k: v for k, v in {**tc[i], **kw}.items()
                            if k not in ("x_mm", "cg_x_mm", "offset_in_parent_mm", "calculated_mass_g", "depth", "movable", "internal", "label")}
    L1, L2 = d["bt80_mm"], d["bt70_mm"]
    ri1, ri2 = BT80[0] - BT80[1], BT70[0] - BT70[1]
    mount_ri = m["diameter_mm"] / 2 + 0.05
    mount_ro = mount_ri + 0.8
    mount_len = max(70.0, m["length_mm"] + 10)
    f = d["fins"]
    comps = [
        take(0, id="nose", parent=None, name="66 mm ARC foam nose cone", weighed=True),
        {"id": "ballast", "parent": "nose", "type": "MassComponent", "name": "Nose ballast", "mass_g": r1(d["ballast_g"]), "length_mm": 20,
         "radius_mm": 20, "weighed": True, "axial_method": "TOP", "axial_offset_mm": round(tc[0]["length_mm"] * 0.55, 1)},
        {"id": "bt80", "parent": None, "type": "BodyTube", "name": "BT-80 body tube", "length_mm": L1, "outer_radius_mm": BT80[0], "thickness_mm": BT80[1],
         "material": "Paper (spiral tube)", "weighed": False, "finish_key": "NORMAL", "axial_method": "AFTER", "axial_offset_mm": 0,
         "mass_g": _shell(L1, BT80[0], ri1, DENSITY["Paper (spiral tube)"])},
    ]
    at = 30.0
    for k in (1, 2):
        comps.append({"id": f"egg{k}", "parent": "bt80", "type": "MassComponent", "name": f"Large egg {k} in protector", "mass_g": d["egg_g"] + d["protector_g"],
                      "length_mm": 76, "radius_mm": 32, "weighed": True, "axial_method": "TOP", "axial_offset_mm": at})
        at += 80
    bay_ri = ri1 - 0.8
    comps += [
        {"id": "bay", "parent": "bt80", "type": "TubeCoupler", "name": "Altimeter bay", "length_mm": 101.6, "outer_radius_mm": ri1, "inner_radius_mm": bay_ri,
         "material": "Cardboard", "weighed": False, "axial_method": "TOP", "axial_offset_mm": at, "mass_g": _shell(101.6, ri1, bay_ri, DENSITY["Cardboard"])},
        {"id": "bay_front", "parent": "bay", "type": "Bulkhead", "name": "Bay bulkhead (front)", "length_mm": 6.4, "outer_radius_mm": bay_ri,
         "material": "Birch plywood", "weighed": False, "axial_method": "TOP", "axial_offset_mm": 0, "mass_g": _shell(6.4, bay_ri, 0, DENSITY["Birch plywood"])},
        {"id": "altimeter", "parent": "bay", "type": "MassComponent", "name": "Altimeter (Jolly Logic AltimeterOne)", "mass_g": d["altimeter_g"], "length_mm": 40,
         "radius_mm": 12.5, "weighed": True, "axial_method": "MIDDLE", "axial_offset_mm": 0},
        {"id": "bay_back", "parent": "bay", "type": "Bulkhead", "name": "Bay bulkhead (back)", "length_mm": 6.4, "outer_radius_mm": bay_ri,
         "material": "Birch plywood", "weighed": False, "axial_method": "BOTTOM", "axial_offset_mm": 0, "mass_g": _shell(6.4, bay_ri, 0, DENSITY["Birch plywood"])},
        take(7, id="rail1", parent="bt80", name="1010 rail button", axial_method="MIDDLE", axial_offset_mm=0, weighed=True),
        take(11, id="transition", parent=None, name="Transition, BT-80 to BT-70", weighed=True, auto_radius=False),
        {"id": "bt70", "parent": None, "type": "BodyTube", "name": "BT-70 body tube", "length_mm": L2, "outer_radius_mm": BT70[0], "thickness_mm": BT70[1],
         "material": "Paper (spiral tube)", "weighed": False, "finish_key": "NORMAL", "axial_method": "AFTER", "axial_offset_mm": 0,
         "mass_g": _shell(L2, BT70[0], ri2, DENSITY["Paper (spiral tube)"])},
        {"id": "cord", "parent": "bt70", "type": "ShockCord", "name": "Shock cord", "mass_g": 8, "length_mm": 40, "radius_mm": 12, "cord_length_mm": 1000,
         "weighed": True, "axial_method": "TOP", "axial_offset_mm": 0},
        {"id": "chute", "parent": "bt70", "type": "Parachute", "name": f"Parachute, {d['chute_mm'] / 25.4:.1f} in nylon", "canopy_diameter_mm": r1(d["chute_mm"]),
         "cd": 0.8, "line_count": 6, "line_length_mm": 457, "length_mm": 30, "radius_mm": 15, "material": "Ripstop nylon", "weighed": False,
         "axial_method": "TOP", "axial_offset_mm": 45, "mass_g": _chute_mass(d["chute_mm"])},
        {"id": "mount", "parent": "bt70", "type": "InnerTube", "name": f"{m['diameter_mm']:.0f} mm motor mount", "length_mm": mount_len,
         "outer_radius_mm": round(mount_ro, 2), "inner_radius_mm": round(mount_ri, 2), "motor_mount": True, "motor_overhang_mm": 0, "material": "Cardboard",
         "weighed": False, "axial_method": "BOTTOM", "axial_offset_mm": 0, "mass_g": _shell(mount_len, mount_ro, mount_ri, DENSITY["Cardboard"])},
    ]
    for k, off in ((1, -8.0), (2, -(mount_len - 12))):
        comps.append({"id": f"ring{k}", "parent": "bt70", "type": "CenteringRing", "name": f"Centering ring {k}", "length_mm": 3,
                      "outer_radius_mm": ri2, "inner_radius_mm": round(mount_ro, 2), "material": "Birch plywood", "weighed": False,
                      "axial_method": "BOTTOM", "axial_offset_mm": off, "mass_g": _shell(3, ri2, mount_ro, DENSITY["Birch plywood"])})
    comps += [
        {"id": "fins", "parent": "bt70", "type": "TrapezoidFinSet", "name": "Balsa fins", "fin_count": f["count"], "root_chord_mm": f["root"],
         "tip_chord_mm": f["tip"], "span_mm": f["span"], "sweep_mm": f["sweep"], "thickness_mm": f["thick"], "length_mm": max(f["root"], f["sweep"] + f["tip"]),
         "cross_section_key": "ROUNDED", "material": "Balsa", "weighed": False, "finish_key": "NORMAL", "axial_method": "BOTTOM", "axial_offset_mm": 0,
         "mass_g": _fins_mass(f["root"], f["tip"], f["span"], f["thick"], f["count"], DENSITY["Balsa"])},
        take(8, id="rail2", parent="bt70", name="1010 rail button", axial_method="BOTTOM", axial_offset_mm=-60, weighed=True),
    ]
    return {"components": comps, "motor_key": m["key"], "delay_s": d["delay_s"]}


# ---------------------------------------------------------------------------
# OpenRocket evaluation
# ---------------------------------------------------------------------------

class Evaluator:
    def __init__(self, orl, launch, calibration, target_ft):
        from openrocket import calibration_for, find_motor, thrust_scale_listener
        from build import TEMPLATE
        self.orl, self.launch, self.target = orl, launch, target_ft
        self.cal = calibration_for(calibration, TEMPLATE.name)
        self.motors = {}
        self._find, self._listener = find_motor, thrust_scale_listener
        self.flights = 0

    def motor(self, entry):
        if entry["key"] not in self.motors:
            m = self._find(entry["designation"], entry["manufacturer"], entry["total_impulse_ns"], entry["diameter_mm"])
            self.motors[entry["key"]] = (m, self._listener(entry["total_impulse_ns"] / float(m.getTotalImpulseEstimate())) if m else None)
        return self.motors[entry["key"]]

    def _doc(self, spec, entry):
        from build import TEMPLATE, build_rocket, set_motor
        doc = self.orl.load_doc(str(TEMPLATE))
        build_rocket(doc.getRocket(), spec)
        set_motor(doc.getRocket(), self.motor(entry)[0], spec["delay_s"])
        return doc

    def static(self, spec, entry) -> dict:
        """Mass, CG, CP, stability and length, without flying."""
        from design import export
        return export(self.orl, self._doc(spec, entry))["summary"]

    def fly(self, spec, entry, wind_mph=None) -> dict:
        from build import TEMPLATE
        from openrocket import Launch
        from simulate import run_with
        launch = self.launch if wind_mph is None else Launch(self.launch.elevation_ft, wind_mph, self.launch.humidity_pct, self.launch.temperature_f)
        r = run_with(self.orl, TEMPLATE, launch, calibration=self.cal, target_apogee_ft=self.target, doc=self._doc(spec, entry),
                     listeners=[self.motor(entry)[1]])
        self.flights += 1
        return r["predicted"] | {"score": r["score"]["total"]}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def candidate_motors(menu, max_impulse=80.0):
    return [m for m in menu if m.get("simulatable") and m.get("launch_mass_g") and 30 <= m["total_impulse_ns"] <= max_impulse
            and m["delays_s"] and m["diameter_mm"] in (24.0, 29.0)]


def nearest_delay(entry, want):
    ok = [d for d in entry["delays_s"] if d > 0] or entry["delays_s"]
    return min(ok, key=lambda d: abs(d - want))


def base_choices(entry, payload):
    mount_len = max(70.0, entry["length_mm"] + 10)
    bt70 = max(254.0, round(mount_len + 120, 0))      # room for shock cord, chute and the mount
    return {"motor": entry, "bt80_mm": 330.0, "bt70_mm": bt70,
            "fins": {"count": 3, "root": 90.0, "tip": 40.0, "span": 50.0, "sweep": 45.0, "thick": 3.2},
            "chute_mm": 457.2, "ballast_g": 0.0, "delay_s": nearest_delay(entry, 6), **payload}


def size_airframe(ev, team, d):
    """Shortest legal airframe, then the smallest fins that give STABILITY_GOAL with no ballast."""
    d = json.loads(json.dumps(d))
    s = ev.static(make_spec(team, d), d["motor"])
    if s["airframe_length_mm"] < LENGTH_MIN_MM:
        d["bt80_mm"] += LENGTH_MIN_MM - s["airframe_length_mm"] + 5
    lo, hi = 25.0, 110.0
    for _ in range(9):   # bisection on span
        mid = (lo + hi) / 2
        d["fins"]["span"] = round(mid, 1)
        s = ev.static(make_spec(team, d), d["motor"])
        if s["stability_cal"] >= STABILITY_GOAL:
            hi = mid
        else:
            lo = mid
    d["fins"]["span"] = round(hi, 1)
    return d, ev.static(make_spec(team, d), d["motor"])


def tune(ev, team, d, max_flights=7):
    """Nose ballast for apogee, parachute for duration, delay nearest the optimum."""
    d = json.loads(json.dumps(d))
    T = ev.target
    s = ev.static(make_spec(team, d), d["motor"])
    headroom = MASS_LIMIT_G - EGG_MARGIN_G - s["mass_with_motor_g"]
    slope = -1.1                                       # ft per gram, refined from the flights
    last = None
    for i in range(max_flights):
        p = ev.fly(make_spec(team, d), d["motor"])
        if last is not None and abs(d["ballast_g"] - last[0]) > 0.5:
            slope = min(-0.2, (p["apogee_ft"] - last[1]) / (d["ballast_g"] - last[0]))
        last = (d["ballast_g"], p["apogee_ft"])
        err = p["apogee_ft"] - T
        if p["apogee_ft"] < T - 3 and d["ballast_g"] <= 0.5:
            return d, p, f"too weak: {p['apogee_ft']:.0f} ft with no ballast"
        descent = p["duration_s"] - p["time_to_apogee_s"]
        dur_ok = DURATION_WINDOW[0] + 0.3 <= p["duration_s"] <= DURATION_WINDOW[1] - 0.3
        new_delay = nearest_delay(d["motor"], p["optimum_delay_s"])
        if abs(err) <= 4 and dur_ok and new_delay == d["delay_s"]:
            return d, p, None
        want = d["ballast_g"] - err / slope
        if want > d["ballast_g"] + headroom:
            return d, p, f"too strong: needs {want:.0f} g of ballast, over the mass limit"
        d["ballast_g"] = max(0.0, r1(want))
        headroom = MASS_LIMIT_G - EGG_MARGIN_G - (s["mass_with_motor_g"] + d["ballast_g"])
        if not dur_ok and descent > 1:
            goal = DURATION_GOAL - p["time_to_apogee_s"]
            d["chute_mm"] = r1(min(1500, max(200, d["chute_mm"] * goal / descent)))   # descent time grows with canopy diameter
        d["delay_s"] = new_delay
    return d, p, None


def run(orl, launch, calibration, *, target_ft=800.0, budget_s=180.0, payload=None, progress=print, keep=5):
    """Search; returns ranked designs (best first) as dicts with spec, metrics and notes."""
    team = json.loads(TEAM_DESIGN.read_text())
    menu = json.loads(MOTOR_MENU.read_text())["motors"]
    payload = payload or {"egg_g": 59.0, "protector_g": 15.0, "altimeter_g": 7.0}
    ev = Evaluator(orl, launch, calibration, target_ft)
    t0 = time.time()
    left = lambda: budget_s - (time.time() - t0)

    # 1. screen every approved motor with one flight
    screened = []
    motors = candidate_motors(menu)
    progress(f"Screening {len(motors)} approved motors")
    for e in motors:
        if ev.motor(e)[0] is None:
            continue
        d, s = size_airframe(ev, team, base_choices(e, payload))
        p = ev.fly(make_spec(team, d), e)
        headroom = MASS_LIMIT_G - EGG_MARGIN_G - s["mass_with_motor_g"]
        need = (p["apogee_ft"] - target_ft) / 1.1         # grams of ballast, roughly
        ok = p["apogee_ft"] >= target_ft - 3 and need <= headroom
        screened.append({"entry": e, "d": d, "static": s, "apogee": p["apogee_ft"], "need_g": need, "headroom_g": headroom, "ok": ok})
        if left() < budget_s * 0.35:
            break
    good = sorted([x for x in screened if x["ok"]], key=lambda x: x["need_g"])   # least ballast needed first: least wasted impulse
    progress(f"{len(good)} of {len(screened)} motors can reach {target_ft:.0f} ft under {MASS_LIMIT_G:.0f} g")

    # 2-3. tune the promising ones
    results = []
    for x in good:
        if left() < 12:
            break
        e = x["entry"]
        progress(f"Tuning {e['manufacturer']} {e['designation']}")
        d, p, why = tune(ev, team, x["d"])
        if why:
            continue
        spec = make_spec(team, d)
        s = ev.static(spec, e)
        results.append({"name": f"{e['designation']} {e['manufacturer']}, {d['ballast_g']:.0f} g ballast, {d['chute_mm'] / 25.4:.1f} in chute",
                        "d": d, "spec": spec, "static": s, "predicted": p})
        if len(results) >= keep + 3:
            break

    # 4. wind robustness for the best few, then rank
    results.sort(key=lambda r: r["predicted"]["score"])
    for r in results[:keep]:
        if left() < 6:
            r["wind_spread_ft"] = None
            continue
        e = r["d"]["motor"]
        hi = ev.fly(r["spec"], e, wind_mph=10.0)["apogee_ft"]
        lo = ev.fly(r["spec"], e, wind_mph=0.0)["apogee_ft"]
        r["wind_spread_ft"] = round(abs(hi - lo), 1)
    ranked = sorted(results[:keep], key=lambda r: (round(r["predicted"]["score"]), r["wind_spread_ft"] or 99,
                                                    -(MASS_LIMIT_G - r["static"]["mass_with_motor_g"])))
    progress(f"Done: {len(ranked)} designs from {ev.flights} OpenRocket flights in {time.time() - t0:.0f} s")
    out = []
    for i, r in enumerate(ranked):
        e, s, p, d = r["d"]["motor"], r["static"], r["predicted"], r["d"]
        out.append({
            "rank": i + 1, "name": r["name"], "motor_key": e["key"], "motor": f"{e['manufacturer']} {e['designation']}-{d['delay_s']:g}",
            "reloadable": bool(e.get("reloadable")),
            "spec_json": json.dumps(r["spec"]),
            "metrics": {"apogee_ft": p["apogee_ft"], "duration_s": p["duration_s"], "score": p["score"], "mass_g": s["mass_with_motor_g"],
                        "stability_cal": s["stability_cal"], "length_mm": s["length_mm"], "ballast_g": d["ballast_g"], "chute_in": round(d["chute_mm"] / 25.4, 1),
                        "fin_span_mm": d["fins"]["span"], "wind_spread_ft": r.get("wind_spread_ft"),
                        "velocity_off_rail_fps": p["velocity_off_rail_fps"], "max_acceleration_g": p["max_acceleration_g"]},
        })
    return {"target_apogee_ft": target_ft, "flights": ev.flights, "seconds": round(time.time() - t0, 1), "designs": out,
            "screened": [{"motor": f"{x['entry']['manufacturer']} {x['entry']['designation']}", "apogee_ft": round(x["apogee"]), "ok": x["ok"]} for x in screened],
            "assumptions": [f"Two eggs of {payload['egg_g']:g} g in {payload['protector_g']:g} g protectors and a {payload['altimeter_g']:g} g altimeter",
                            f"Keeps {EGG_MARGIN_G:g} g under {MASS_LIMIT_G:g} g for egg weight variation",
                            "Fins: balsa, 3.2 mm, three, sized for 1.3 cal before ballast",
                            "Thrust scaled to the NAR-tested impulse; flight calibrated with the team's 38 flights"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-ft", type=float, default=800.0)
    ap.add_argument("--budget-s", type=float, default=180.0)
    ap.add_argument("--elevation-ft", type=float, default=3740)
    ap.add_argument("--wind-mph", type=float, default=5)
    ap.add_argument("--humidity-pct", type=float, default=25)
    ap.add_argument("--temperature-f", type=float, default=70)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    real = os.dup(1)
    os.dup2(2, 1)
    try:
        from openrocket import Launch, load_calibration, open_openrocket
        with open_openrocket() as orl:
            res = run(orl, Launch(a.elevation_ft, a.wind_mph, a.humidity_pct, a.temperature_f), load_calibration(ROOT / "data" / "calibration.json"),
                      target_ft=a.target_ft, budget_s=a.budget_s, progress=lambda m: print(m, file=sys.stderr, flush=True))
    finally:
        os.dup2(real, 1)
    for d in res["designs"]:
        m = d["metrics"]
        print(f"{d['rank']}. {d['name']}: {m['apogee_ft']} ft, {m['duration_s']} s, score {m['score']}, {m['mass_g']} g, "
              f"{m['stability_cal']} cal, wind spread {m['wind_spread_ft']} ft")
    if a.json:
        a.json.write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
