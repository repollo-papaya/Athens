"""Build an OpenRocket rocket from the app's part list (a "spec").

The app's builder keeps a rocket as a list of parts in the same format design.py exports
(millimetres, grams, positions by OpenRocket's placement rules). This turns that list into a
real OpenRocket rocket, so a design made from a blank page simulates exactly like an .ork.

The simulation settings come from a template .ork (its simulation, flight configuration and
options); its parts are all replaced. Every part's mass is set as an override to the app's
number (weighed, or estimated from size and material in the app), so the app and the worker
agree on weight; drag comes from the geometry and surface finish.

    python sim/build.py designs/ederance-29mm.json      # rebuild the team rocket and compare
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "rockets" / "ederance-29mm.ork"

STAGE_TYPES = {"NoseCone", "BodyTube", "Transition"}
FIN_TYPES = {"TrapezoidFinSet", "EllipticalFinSet", "FreeformFinSet"}
INSIDE_TYPES = {"InnerTube", "TubeCoupler", "CenteringRing", "Bulkhead", "EngineBlock",
                "MassComponent", "Parachute", "ShockCord", "Streamer"}
OUTSIDE_TYPES = FIN_TYPES | {"RailButton", "LaunchLug"}
ALL_TYPES = STAGE_TYPES | INSIDE_TYPES | OUTSIDE_TYPES
SHAPES = {"CONICAL", "OGIVE", "ELLIPSOID", "POWER", "PARABOLIC", "HAACK"}
MAX_PARTS = 60


def _m(mm) -> float:
    return float(mm) / 1000.0


def _num(item: dict, key: str, lo: float, hi: float, default=None) -> float:
    v = item.get(key, default)
    if v is None:
        raise ValueError(f"{item.get('name') or item.get('type')}: {key} is missing")
    v = float(v)
    if not (lo <= v <= hi) or math.isnan(v):
        raise ValueError(f"{item.get('name') or item.get('type')}: {key} must be {lo:g} to {hi:g}")
    return v


def check_spec(spec: dict) -> list[dict]:
    """Validate a part list; returns the components, parents before children."""
    comps = spec.get("components")
    if not isinstance(comps, list) or not comps:
        raise ValueError("the rocket has no parts")
    if len(comps) > MAX_PARTS:
        raise ValueError(f"at most {MAX_PARTS} parts")
    ids = {}
    for c in comps:
        if c.get("type") not in ALL_TYPES:
            raise ValueError(f"unknown part type {c.get('type')!r}")
        cid = str(c.get("id"))
        if cid in ids:
            raise ValueError(f"two parts share id {cid}")
        parent = c.get("parent")
        if c["type"] in STAGE_TYPES:
            if parent is not None:
                raise ValueError(f"{c['type']} goes in the airframe, not inside another part")
        else:
            if parent is None or str(parent) not in ids:
                raise ValueError(f"{c.get('name') or c['type']} needs a part to go in or on (listed before it)")
            ptype = ids[str(parent)]["type"]
            if c["type"] in FIN_TYPES | {"RailButton", "LaunchLug"} and ptype not in ("BodyTube",):
                raise ValueError(f"{c.get('name') or c['type']} goes on a body tube")
            if c["type"] in INSIDE_TYPES and ptype not in ("BodyTube", "NoseCone", "Transition", "InnerTube", "TubeCoupler"):
                raise ValueError(f"{c.get('name') or c['type']} goes inside a tube or the nose cone")
        _num(c, "mass_g", 0, 2000)
        ids[cid] = c
    if not any(c["type"] == "InnerTube" and c.get("motor_mount") for c in comps):
        raise ValueError("the rocket needs a motor mount (an inner tube marked as the motor mount)")
    if not any(c["type"] in STAGE_TYPES for c in comps):
        raise ValueError("the rocket has no airframe")
    return comps


def _make(J, c: dict):
    t = c["type"]
    comp = J(t)()
    comp.setName(str(c.get("name") or t)[:60])
    L = lambda: _m(_num(c, "length_mm", 0.1, 3000))
    if t in ("NoseCone", "Transition"):
        Shape = J("Transition$Shape")
        shape = c.get("shape", "OGIVE")
        if shape not in SHAPES:
            raise ValueError(f"unknown shape {shape}")
        comp.setShapeType(Shape.valueOf(shape))
        if c.get("shape_param") is not None and Shape.valueOf(shape).usesParameter():
            comp.setShapeParameter(float(c["shape_param"]))
        comp.setLength(L())
        if t == "NoseCone":
            comp.setAftRadiusAutomatic(False)
            comp.setAftRadius(_m(_num(c, "aft_radius_mm", 1, 300)))
        else:
            comp.setForeRadiusAutomatic(False)
            comp.setAftRadiusAutomatic(False)
            comp.setForeRadius(_m(_num(c, "fore_radius_mm", 0, 300)))
            comp.setAftRadius(_m(_num(c, "aft_radius_mm", 0, 300)))
        filled = bool(c.get("filled"))
        comp.setFilled(filled)
        if not filled:
            comp.setThickness(_m(_num(c, "thickness_mm", 0.1, 50, 2)))
        for end in ("Aft", "Fore"):
            k = end.lower()
            sl = float(c.get(f"{k}_shoulder_length_mm") or 0)
            if sl > 0:
                getattr(comp, f"set{end}ShoulderLength")(_m(sl))
                getattr(comp, f"set{end}ShoulderRadius")(_m(_num(c, f"{k}_shoulder_radius_mm", 0.5, 300)))
                if c.get(f"{k}_shoulder_thickness_mm"):
                    getattr(comp, f"set{end}ShoulderThickness")(_m(float(c[f"{k}_shoulder_thickness_mm"])))
                getattr(comp, f"set{end}ShoulderCapped")(bool(c.get(f"{k}_shoulder_capped")))
    elif t == "BodyTube":
        comp.setOuterRadiusAutomatic(False)
        comp.setLength(L())
        comp.setOuterRadius(_m(_num(c, "outer_radius_mm", 1, 300)))
        comp.setThickness(_m(_num(c, "thickness_mm", 0.05, 50, 0.5)))
    elif t in ("InnerTube", "TubeCoupler", "EngineBlock"):
        comp.setLength(L())
        if hasattr(comp, "setOuterRadiusAutomatic"):
            comp.setOuterRadiusAutomatic(False)
        comp.setOuterRadius(_m(_num(c, "outer_radius_mm", 0.5, 300)))
        comp.setInnerRadius(_m(_num(c, "inner_radius_mm", 0, 300)))
        if t == "InnerTube":
            comp.setMotorMount(bool(c.get("motor_mount")))
            if c.get("motor_overhang_mm") is not None:
                comp.setMotorOverhang(_m(c["motor_overhang_mm"]))
    elif t in ("CenteringRing", "Bulkhead"):
        comp.setLength(L())
        comp.setOuterRadiusAutomatic(False)
        comp.setOuterRadius(_m(_num(c, "outer_radius_mm", 0.5, 300)))
        if t == "CenteringRing":
            comp.setInnerRadiusAutomatic(False)
            comp.setInnerRadius(_m(_num(c, "inner_radius_mm", 0, 300)))
    elif t in ("MassComponent", "Parachute", "ShockCord", "Streamer"):
        comp.setLength(L())
        comp.setRadiusAutomatic(False) if hasattr(comp, "setRadiusAutomatic") else None
        comp.setRadius(_m(_num(c, "radius_mm", 0.5, 300)))
        if c.get("radial_position_mm"):
            comp.setRadialPosition(_m(c["radial_position_mm"]))
        if t == "Parachute":
            comp.setDiameter(_m(_num(c, "canopy_diameter_mm", 50, 3000)))
            comp.setCDAutomatic(False)
            comp.setCD(_num(c, "cd", 0.2, 3, 0.8))
            if c.get("line_count") is not None:
                comp.setLineCount(int(c["line_count"]))
            if c.get("line_length_mm") is not None:
                comp.setLineLength(_m(c["line_length_mm"]))
        if t == "ShockCord" and c.get("cord_length_mm"):
            comp.setCordLength(_m(c["cord_length_mm"]))
    elif t in FIN_TYPES:
        comp.setFinCount(int(_num(c, "fin_count", 1, 8)))
        comp.setThickness(_m(_num(c, "thickness_mm", 0.3, 20)))
        if c.get("cant_deg"):
            comp.setCantAngle(math.radians(float(c["cant_deg"])))
        if c.get("base_rotation_deg"):
            comp.setBaseRotation(math.radians(float(c["base_rotation_deg"])))
        if c.get("cross_section_key"):
            comp.setCrossSection(J("FinSet$CrossSection").valueOf(c["cross_section_key"]))
        if t == "TrapezoidFinSet":
            comp.setRootChord(_m(_num(c, "root_chord_mm", 1, 1000)))
            comp.setTipChord(_m(_num(c, "tip_chord_mm", 0, 1000)))
            comp.setHeight(_m(_num(c, "span_mm", 1, 500)))
            comp.setSweep(_m(_num(c, "sweep_mm", -1000, 1000)))
        elif t == "EllipticalFinSet":
            comp.setLength(_m(_num(c, "root_chord_mm", 1, 1000)))
            comp.setHeight(_m(_num(c, "span_mm", 1, 500)))
        else:
            import jpype
            pts = c.get("points_mm")
            if isinstance(pts, list) is False or len(pts) < 3:
                raise ValueError("freeform fins need at least 3 outline points")
            Coordinate = jpype.JClass("info.openrocket.core.util.Coordinate")
            comp.setPoints(jpype.JArray(Coordinate)([Coordinate(_m(x), _m(y)) for x, y in pts]))
    elif t == "RailButton":
        comp.setOuterDiameter(_m(_num(c, "outer_diameter_mm", 1, 50, 9.7)))
        if c.get("inner_diameter_mm"):
            comp.setInnerDiameter(_m(c["inner_diameter_mm"]))
        if c.get("height_mm"):
            comp.setTotalHeight(_m(c["height_mm"]))
        for k, setter in (("base_height_mm", "setBaseHeight"), ("flange_height_mm", "setFlangeHeight"), ("screw_height_mm", "setScrewHeight")):
            if c.get(k) is not None:
                getattr(comp, setter)(_m(c[k]))
        comp.setAngleOffset(math.radians(float(c.get("angle_deg", 0))))
    elif t == "LaunchLug":
        comp.setLength(L())
        comp.setOuterRadius(_m(_num(c, "outer_radius_mm", 0.5, 30, 2.8)))
        comp.setThickness(_m(_num(c, "thickness_mm", 0.1, 10, 0.5)))
        comp.setAngleOffset(math.radians(float(c.get("angle_deg", 0))))
    return comp


def build_rocket(rocket, spec: dict) -> None:
    """Replace every part of `rocket` (an OpenRocket Rocket) with the spec's parts."""
    import jpype
    J = lambda name: jpype.JClass("info.openrocket.core.rocketcomponent." + name)
    AxialMethod = J("position.AxialMethod")
    Finish = J("ExternalComponent$Finish")
    comps = check_spec(spec)
    stage = rocket.getChild(0)
    for old in list(stage.getChildren()):
        stage.removeChild(old)
    made = {}
    for c in comps:
        comp = _make(J, c)
        parent = stage if c.get("parent") is None else made[str(c["parent"])]
        parent.addChild(comp)
        method = c.get("axial_method") or ("AFTER" if parent is stage else "TOP")
        if parent is stage and method != "AFTER":
            method = "AFTER"
        comp.setAxialMethod(AxialMethod.valueOf(method))
        comp.setAxialOffset(_m(c.get("axial_offset_mm") or 0))
        if c.get("finish_key") and hasattr(comp, "setFinish"):
            comp.setFinish(Finish.valueOf(c["finish_key"]))
        if c["type"] in FIN_TYPES and c.get("tab_height_mm"):
            comp.setTabHeight(_m(c["tab_height_mm"]))
            comp.setTabLength(_m(min(float(c.get("tab_length_mm") or 0), float(comp.getLength()) * 1000)))
            if c.get("tab_offset_method"):
                comp.setTabOffsetMethod(AxialMethod.valueOf(c["tab_offset_method"]))
            comp.setTabOffset(_m(c.get("tab_offset_mm") or 0))
        if c["type"] == "MassComponent":
            comp.setComponentMass(float(c["mass_g"]) / 1000.0)
        else:
            comp.setMassOverridden(True)
            comp.setOverrideMass(float(c["mass_g"]) / 1000.0)
        made[str(c["id"])] = comp


def set_motor(rocket, motor, delay_s: float) -> None:
    """Put an OpenRocket motor (ThrustCurveMotor) in the motor mount for every flight configuration."""
    import jpype
    MotorConfiguration = jpype.JClass("info.openrocket.core.motor.MotorConfiguration")
    from openrocket import motor_mount
    mount = motor_mount(rocket)
    ids = list(rocket.getIds()) if hasattr(rocket, "getIds") else [rocket.getSelectedConfiguration().getId()]
    for fcid in ids:
        mc = MotorConfiguration(mount, fcid)
        mc.setMotor(motor)
        mc.setEjectionDelay(float(delay_s))
        mount.setMotorConfig(mc, fcid)
    for fc in rocket.getFlightConfigurations():
        fc.update() if hasattr(fc, "update") else None


def main() -> None:
    """Rebuild a design from its export and compare it with the original .ork."""
    import os
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "designs" / "ederance-29mm.json")
    spec = json.loads(path.read_text())
    real = os.dup(1)
    os.dup2(2, 1)
    try:
        from design import export
        from openrocket import open_openrocket
        with open_openrocket() as orl:
            doc = orl.load_doc(str(ROOT / spec["source"]))
            original = export(orl, doc)
            doc2 = orl.load_doc(str(TEMPLATE))
            build_rocket(doc2.getRocket(), spec)
            from openrocket import find_motor
            m = spec["motors"][0]
            set_motor(doc2.getRocket(), find_motor("F59", "Cesaroni", m["total_impulse_ns"], m["diameter_mm"]), m["delay_s"])
            rebuilt = export(orl, doc2)
    finally:
        os.dup2(real, 1)
    for k in ("mass_with_motor_g", "cg_mm", "cp_mm", "stability_cal", "length_mm"):
        print(f"{k:20s} original {original['summary'][k]:>9}  rebuilt {rebuilt['summary'][k]:>9}")


if __name__ == "__main__":
    main()
