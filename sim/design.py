"""Export a rocket design as data the app can draw.

Reads an .ork (or, later, a rocket the builder assembles) and writes JSON with:
every component's type, absolute position, length, radii, shape, mass and fin
outline; overall length, diameter, mass with and without motor; CG, CP and
stability the way OpenRocket's side view reports them (CP at Mach 0.3, worst
case over roll); the motor; and, optionally, a calibrated flight prediction.

    python sim/design.py rockets/ederance.ork --out designs/ederance.json
    python sim/design.py rockets/ederance.ork --flight-elevation-ft 3740 ...

All lengths are millimetres, masses grams, positions measured from the nose tip.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parent.parent

# Parts the app lets you slide along their tube, add and remove.
MOVABLE = {"MassComponent", "Parachute", "Streamer", "ShockCord"}

# Components that sit inside the airframe (drawn dashed / internal in the side view).
INTERNAL = {"InnerTube", "TubeCoupler", "CenteringRing", "Bulkhead", "EngineBlock", "MassComponent",
            "Parachute", "Streamer", "ShockCord"}


def _mm(metres) -> float:
    return round(float(metres) * 1000, 2)


# Standard body tube sizes by outside diameter (mm), for naming parts the way the team does.
BT_SIZES = [("BT-5", 13.8), ("BT-20", 18.7), ("BT-50", 24.8), ("BT-55", 33.7), ("BT-60", 41.6),
            ("BT-70", 56.3), ("BT-80", 66.0), ("BT-101", 101.6)]


def tube_size(outer_diameter_mm: float) -> str | None:
    name, size = min(BT_SIZES, key=lambda s: abs(s[1] - outer_diameter_mm))
    return name if abs(size - outer_diameter_mm) <= 0.8 else None


def label_for(item: dict) -> str:
    """The name the team uses for a part: 'BT-80 body tube', 'Transition (BT-80 to BT-70)'."""
    kind = item["type"]
    if kind == "BodyTube":
        size = tube_size(2 * item["outer_radius_mm"])
        return f"{size} body tube" if size else f"{2 * item['outer_radius_mm']:.1f} mm body tube"
    if kind == "Transition":
        fore = tube_size(2 * item["fore_radius_mm"]) or f"{2 * item['fore_radius_mm']:.0f} mm"
        aft = tube_size(2 * item["aft_radius_mm"]) or f"{2 * item['aft_radius_mm']:.0f} mm"
        return f"Transition ({fore} to {aft})"
    if kind == "NoseCone":
        return f"Nose cone ({item['shape'].lower().replace('_', ' ')})"
    if kind.endswith("FinSet"):
        return f"Fins ({item['fin_count']}, {item['fin_shape'].lower()})"
    if kind == "InnerTube":
        return f"Inner tube ({2 * item['outer_radius_mm']:.0f} mm)"
    if kind == "MassComponent":
        return f"Mass item ({item['mass_g']:g} g)" if item["name"] in ("Mass Component", "Mass component") else item["name"]
    return item["name"]


def component_data(c, index: int, parent_index: int | None, depth: int) -> dict:
    kind = str(c.getClass().getSimpleName())
    locations = c.getComponentLocations()
    item = {
        "id": index,
        "parent": parent_index,
        "depth": depth,
        "type": kind,
        "name": str(c.getName()),
        "x_mm": _mm(locations[0].x),
        "length_mm": _mm(c.getLength()),
        "mass_g": round(float(c.getMass()) * 1000, 1),
        "mass_measured": bool(c.isMassOverridden()),
        "internal": kind in INTERNAL,
    }
    try:
        item["calculated_mass_g"] = round(float(c.getComponentMass()) * 1000, 1)
    except Exception:
        pass
    # Each part's own centre of gravity (absolute), so the app can recompute the rocket's CG live.
    item["cg_x_mm"] = _mm(locations[0].x + c.getComponentCG().x)
    item["movable"] = kind in MOVABLE
    parent = c.getParent()
    if parent is not None and str(parent.getClass().getSimpleName()) not in ("AxialStage", "Rocket"):
        item["offset_in_parent_mm"] = _mm(locations[0].x - parent.getComponentLocations()[0].x)
    try:
        if c.getFinish() is not None:
            item["finish"] = str(c.getFinish().toString())
    except Exception:
        pass
    try:
        item["material"] = str(c.getMaterial().getName())
    except Exception:
        pass
    try:
        preset = c.getPresetComponent()
        if preset is not None:
            item["catalogue_part"] = f"{preset.getManufacturer()} {preset.getPartNo()}"
    except Exception:
        pass

    if kind in ("NoseCone", "Transition"):
        item.update({
            "shape": str(c.getShapeType().name()),
            "shape_param": round(float(c.getShapeParameter()), 4),
            "fore_radius_mm": _mm(c.getForeRadius()),
            "aft_radius_mm": _mm(c.getAftRadius()),
            "fore_shoulder_length_mm": _mm(c.getForeShoulderLength()),
            "fore_shoulder_radius_mm": _mm(c.getForeShoulderRadius()),
            "aft_shoulder_length_mm": _mm(c.getAftShoulderLength()),
            "aft_shoulder_radius_mm": _mm(c.getAftShoulderRadius()),
            "thickness_mm": _mm(c.getThickness()),
            "filled": bool(c.isFilled()),
        })
    elif kind == "BodyTube":
        item.update({"outer_radius_mm": _mm(c.getOuterRadius()), "inner_radius_mm": _mm(c.getInnerRadius()),
                     "thickness_mm": _mm(c.getThickness())})
    elif kind in ("InnerTube", "TubeCoupler", "CenteringRing", "Bulkhead", "EngineBlock"):
        item.update({"outer_radius_mm": _mm(c.getOuterRadius()), "inner_radius_mm": _mm(c.getInnerRadius())})
        if kind == "InnerTube":
            item["motor_mount"] = bool(c.isMotorMount())
            item["motor_overhang_mm"] = _mm(c.getMotorOverhang())
    elif kind in ("MassComponent", "Parachute", "Streamer", "ShockCord"):
        item.update({"radius_mm": _mm(c.getRadius()), "radial_position_mm": _mm(c.getRadialPosition())})
        if kind == "Parachute":
            item.update({"canopy_diameter_mm": _mm(c.getDiameter()), "cd": round(float(c.getCD()), 3)})
        if kind == "MassComponent":
            item["role"] = str(c.getMassComponentType().name())
    elif "FinSet" in kind:
        item.update({
            "fin_count": int(c.getFinCount()),
            "base_rotation_deg": round(math.degrees(float(c.getBaseRotation())), 2),
            "cant_deg": round(math.degrees(float(c.getCantAngle())), 2),
            "thickness_mm": _mm(c.getThickness()),
            "body_radius_mm": _mm(c.getFinFront().y) if hasattr(c, "getFinFront") else None,
            "points_mm": [[_mm(p.x), _mm(p.y)] for p in c.getFinPoints()],
            "span_mm": _mm(c.getSpan()),
            "root_chord_mm": _mm(c.getLength()),
            "fin_shape": kind.replace("FinSet", "") or "Fin",
            "cross_section": str(c.getCrossSection().toString()),
            "tab_height_mm": _mm(c.getTabHeight()),
            "tab_length_mm": _mm(c.getTabLength()),
        })
    elif kind in ("RailButton", "LaunchLug"):
        if kind == "RailButton":
            item.update({"outer_diameter_mm": _mm(c.getOuterDiameter()), "height_mm": _mm(c.getTotalHeight())})
        else:
            item.update({"outer_radius_mm": _mm(c.getOuterRadius())})
        item["angle_deg"] = round(math.degrees(float(c.getAngleOffset())), 1)
    return item


def export(orl, doc, *, flight: dict | None = None, source: str = "") -> dict:
    import jpype
    from openrocket import _walk, motor_configurations

    rocket = doc.getRocket()
    configs = motor_configurations(rocket)
    config = configs[0] if configs else rocket.getSelectedConfiguration()
    rocket.setSelectedConfiguration(config.getId())

    # Components, parents before children.
    components, ids = [], {}

    def visit(c, parent_id, depth):
        kind = str(c.getClass().getSimpleName())
        my_id = None
        if kind not in ("Rocket", "AxialStage"):
            my_id = len(components)
            components.append(component_data(c, my_id, parent_id, depth))
            ids[str(c.getID())] = my_id
        for child in c.getChildren():
            visit(child, my_id if my_id is not None else parent_id, depth + (0 if my_id is None else 1))

    visit(rocket, None, 0)

    # Mass, CG and CP as OpenRocket's side view shows them.
    MassCalculator = jpype.JClass("info.openrocket.core.masscalc.MassCalculator")
    Barrowman = jpype.JClass("info.openrocket.core.aerodynamics.BarrowmanCalculator")
    FlightConditions = jpype.JClass("info.openrocket.core.aerodynamics.FlightConditions")
    WarningSet = jpype.JClass("info.openrocket.core.logging.WarningSet")
    launch = MassCalculator.calculateLaunch(config)
    structure = MassCalculator.calculateStructure(config)
    conditions = FlightConditions(config)
    conditions.setMach(0.3)
    conditions.setAOA(0.0)
    conditions.setRollRate(0.0)
    cp = Barrowman().getWorstCP(config, conditions, WarningSet())
    ref_diameter_mm = _mm(conditions.getRefLength())
    length_mm = _mm(config.getLength())
    cg_mm = _mm(launch.getCM().x)
    cp_mm = _mm(cp.x)
    margin = cp_mm - cg_mm

    motors = []
    for m in config.getActiveMotors():
        motor = m.getMotor()
        mount = m.getMount()
        mount_x = float(mount.getComponentLocations()[0].x)
        motor_len = float(motor.getLength())
        aft = mount_x + float(mount.getLength()) + float(mount.getMotorOverhang())
        motors.append({
            "designation": f"{motor.getManufacturer().getDisplayName()} {motor.getDesignation()}",
            "delay_s": float(m.getEjectionDelay()),
            "x_mm": _mm(aft - motor_len),
            "length_mm": _mm(motor_len),
            "diameter_mm": _mm(motor.getDiameter()),
            "launch_mass_g": round(float(motor.getLaunchMass()) * 1000, 1),
            "cg_x_mm": _mm(aft - motor_len / 2),
            "total_impulse_ns": round(float(motor.getTotalImpulseEstimate()), 1),
            "mount_id": ids.get(str(mount.getID())),
        })

    # OpenRocket's stability % uses the airframe length (without a protruding motor).
    airframe_mm = max(c["x_mm"] + c["length_mm"] for c in components if not c["internal"])
    for c in components:
        c["label"] = label_for(c)
    for m in motors:   # name the motor mount by the motor it holds
        if m["mount_id"] is not None:
            components[m["mount_id"]]["label"] = f"{m['diameter_mm']:.0f} mm motor mount"
    max_radius = max([c.get("outer_radius_mm", 0) for c in components if not c["internal"]] +
                     [c.get("aft_radius_mm", 0) for c in components] + [c.get("fore_radius_mm", 0) for c in components])
    return {
        "schema_version": SCHEMA_VERSION,
        "name": str(rocket.getName()),
        "source": source,
        "exported_on": date.today().isoformat(),
        "flight_configuration": str(config.getName()),
        "summary": {
            "length_mm": length_mm,
            "max_diameter_mm": round(2 * max_radius, 2),
            "reference_diameter_mm": ref_diameter_mm,
            "mass_no_motor_g": round(float(structure.getMass()) * 1000, 1),
            "mass_with_motor_g": round(float(launch.getMass()) * 1000, 1),
            "cg_mm": cg_mm,
            "cp_mm": cp_mm,
            "cp_mach": 0.3,
            "stability_cal": round(margin / ref_diameter_mm, 2),
            "stability_pct": round(margin / airframe_mm * 100, 1),
            "airframe_length_mm": round(airframe_mm, 2),
        },
        "motors": motors,
        "components": components,
        "flight": flight,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ork", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--flight-elevation-ft", type=float, help="also predict a flight under these conditions")
    ap.add_argument("--flight-wind-mph", type=float, default=2.0)
    ap.add_argument("--flight-humidity-pct", type=float, default=30.0)
    ap.add_argument("--flight-temperature-f", type=float, default=60.0)
    args = ap.parse_args()

    sys.stdout.flush()
    real_stdout = os.dup(1)
    os.dup2(2, 1)  # OpenRocket logs to stdout
    try:
        from openrocket import Launch, load_calibration, open_openrocket
        from simulate import run_with
        with open_openrocket() as orl:
            flight = None
            if args.flight_elevation_ft is not None:
                launch = Launch(args.flight_elevation_ft, args.flight_wind_mph, args.flight_humidity_pct,
                                args.flight_temperature_f)
                cal = load_calibration(ROOT / "data" / "calibration.json")
                from openrocket import calibration_for
                cal = calibration_for(cal, args.ork.name)
                r = run_with(orl, args.ork, launch, calibration=cal)
                flight = {"conditions": {k: getattr(launch, k) for k in ("elevation_ft", "wind_mph", "humidity_pct", "temperature_f")},
                          "calibration_id": r["calibration_id"], "predicted": r["predicted"], "score": r["score"],
                          "target_apogee_ft": r["target_apogee_ft"]}
            design = export(orl, orl.load_doc(str(args.ork)), flight=flight, source=str(args.ork))
    finally:
        sys.stdout.flush()
        os.dup2(real_stdout, 1)
        os.close(real_stdout)

    text = json.dumps(design, indent=1)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
