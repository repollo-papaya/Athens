"""Look up the team's parts list in OpenRocket's parts catalogue and check it against a design.

Reads data/components.csv (exported from the team's Google Sheet), finds each part
in OpenRocket's built-in component catalogue, and reports its real dimensions and
mass. With --ork it also checks each part against the rocket design: is a part of
that size in the design, and does the motor mount fit the motor.

    python sim/components.py
    python sim/components.py --ork rockets/ederance.ork
    python sim/components.py --json

CSV columns
-----------
Required: Category, name, link.
Optional, used when present: manufacturer, part_no, length_in, measured_mass_g.
Measured mass always beats catalogue mass: catalogue masses are averages.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "components.csv"

# Sheet category -> OpenRocket catalogue type. Extend as categories are added.
CATEGORY_TYPES = {
    "nose": "NOSE_CONE",
    "body": "BODY_TUBE",
    "mount": "BODY_TUBE",       # motor-mount tubes are listed as body tubes
    "transition": "TRANSITION",
    "coupler": "TUBE_COUPLER",
    "ring": "CENTERING_RING",
    "centering ring": "CENTERING_RING",
    "bulkhead": "BULK_HEAD",
    "parachute": "PARACHUTE",
    "streamer": "STREAMER",
    "launch lug": "LAUNCH_LUG",
    "rail button": "RAIL_BUTTON",
    "fins": None,            # cut from sheet stock; dimensions come from the design, material from the sheet
    "protector": None,       # payload items: placed as mass items, weighed by the team
    "alt.": None,
    "altimeter": None,
}

# Design component class for each catalogue type, for the design check.
DESIGN_CLASSES = {
    "NOSE_CONE": {"NoseCone"},
    "BODY_TUBE": {"BodyTube", "InnerTube"},
    "TRANSITION": {"Transition"},
    "TUBE_COUPLER": {"TubeCoupler"},
    "CENTERING_RING": {"CenteringRing"},
    "BULK_HEAD": {"Bulkhead"},
    "PARACHUTE": {"Parachute"},
    "STREAMER": {"Streamer"},
    "LAUNCH_LUG": {"LaunchLug"},
    "RAIL_BUTTON": {"RailButton"},
}

PREFERRED_MAKERS = ("Apogee", "Estes", "Rocketarium", "SEMROC")


def read_parts(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    parts = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not any(row.values()):
            continue
        parts.append({"row": i, **row})
    return parts


def hints_from_link(link: str) -> dict:
    """Pull size hints out of a supplier URL slug, e.g. '66mm_x_18_Body_Tube_Estes_BT-80_size'."""
    slug = link.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    hints: dict = {"slug": slug}
    if m := re.search(r"(\d+(?:\.\d+)?)\s*mm", slug, re.I):
        hints["diameter_mm"] = float(m.group(1))
    if m := re.search(r"mm\s*x\s*(\d+(?:\.\d+)?)", slug, re.I):
        hints["length_in"] = float(m.group(1))
    for maker in PREFERRED_MAKERS:
        # "Estes BT-80 size" names a size standard, not who made the part.
        if maker.lower() in slug.lower() and not re.search(maker + r"\b.*\bsize\b", slug, re.I):
            hints["maker"] = maker
    if "foam" in slug.lower():
        hints["material"] = "foam"
    # Tube sizes named in the link ("BT-70 to BT-80") give a transition's two diameters.
    sizes = {"5": 13.8, "20": 18.7, "50": 24.8, "55": 33.7, "60": 41.6, "70": 56.3, "80": 66.0}
    named = [sizes[n] for n in re.findall(r"\bBT\s*(\d+)\b", slug, re.I) if n in sizes]
    if len(named) >= 2:
        hints["diameters_mm"] = sorted(named[:2])
    if m := re.search(r"\b(1010|1515)\b", slug):
        hints["rail"] = m.group(1)
    return hints


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def preset_info(preset, CP) -> dict:
    def get(key):
        return preset.get(key) if preset.has(key) else None

    info = {
        "manufacturer": str(preset.getManufacturer()),
        "part_no": str(preset.getPartNo()),
        "description": str(get(CP.DESCRIPTION) or ""),
    }
    for key, name in ((CP.OUTER_DIAMETER, "outer_diameter_mm"), (CP.INNER_DIAMETER, "inner_diameter_mm"),
                      (CP.AFT_OUTER_DIAMETER, "aft_diameter_mm"), (CP.FORE_OUTER_DIAMETER, "fore_diameter_mm"),
                      (CP.LENGTH, "length_mm"), (CP.DIAMETER, "diameter_mm"), (CP.HEIGHT, "height_mm")):
        if (v := get(key)) is not None:
            info[name] = round(float(v) * 1000, 2)
    material = get(CP.MATERIAL)
    if material is not None:
        info["material"] = str(material.getName())
    if (mass := get(CP.MASS)) is not None:
        info["mass_g"] = round(float(mass) * 1000, 1)
        info["mass_source"] = "catalogue"
    elif material is not None and "outer_diameter_mm" in info and "inner_diameter_mm" in info and "length_mm" in info:
        # Tubes: catalogue gives the material density, not the mass.
        od, idia, length = (info[k] / 1000 for k in ("outer_diameter_mm", "inner_diameter_mm", "length_mm"))
        volume = math.pi / 4 * (od ** 2 - idia ** 2) * length
        info["mass_g"] = round(float(material.getDensity()) * volume * 1000, 1)
        info["mass_source"] = "computed from catalogue dimensions and material"
    if (shape := get(CP.SHAPE)) is not None:
        info["shape"] = str(shape)
    return info


def find_in_catalogue(part: dict, db, CP) -> tuple[dict | None, list[dict], dict]:
    category = part.get("category", "").lower()
    type_name = CATEGORY_TYPES.get(category)
    hints = hints_from_link(part.get("link", ""))
    if part.get("manufacturer"):
        hints["maker"] = part["manufacturer"]
    if part.get("length_in"):
        hints["length_in"] = float(part["length_in"])
    if not type_name:
        if category in CATEGORY_TYPES:   # known, but not a catalogue part
            return None, [], {**hints, "problem": "not a catalogue part; weigh it and add it as a mass item"}
        return None, [], {**hints, "problem": f"unknown category '{part.get('category')}'"}

    wanted = _norm(part.get("part_no") or part.get("name", ""))
    candidates = []
    for p in db.listForType(getattr(CP.Type, type_name)):
        info = preset_info(p, CP)
        pn = _norm(info["part_no"].split(",")[0])
        name_match = bool(wanted) and (pn == wanted or _norm(info["part_no"]) == wanted)
        dia = info.get("outer_diameter_mm") or info.get("aft_diameter_mm") or info.get("diameter_mm")
        dia_match = "diameter_mm" in hints and dia is not None and abs(dia - hints["diameter_mm"]) <= 1.0
        if type_name == "TRANSITION":
            # Match the two tube sizes named in the link, in either direction.
            ends = sorted([info.get("fore_diameter_mm") or 0, info.get("aft_diameter_mm") or 0])
            dia_match = "diameters_mm" in hints and all(abs(a - b) <= 1.0 for a, b in zip(ends, hints["diameters_mm"]))
        if type_name == "RAIL_BUTTON":
            dia_match = "rail" in hints and hints["rail"] in (info["part_no"] + " " + info["description"])
        if not (name_match or dia_match):
            continue
        if hints.get("material") == "foam" and "foam" not in info["description"].lower():
            continue
        if type_name == "TRANSITION" and "blow" in hints.get("slug", "").lower():
            name_match = name_match or "blow" in info["description"].lower()   # blow-molded plastic
        score = 4 * name_match + 2 * dia_match
        score += 2 * (info["manufacturer"] == hints.get("maker"))
        if "length_in" in hints and "length_mm" in info:
            score += 1 * (abs(info["length_mm"] / 25.4 - hints["length_in"]) < 0.6)
        score += 0.5 * (info["manufacturer"] in PREFERRED_MAKERS)
        candidates.append((score, info))
    candidates.sort(key=lambda c: -c[0])
    best = candidates[0][1] if candidates else None
    return best, [c[1] for c in candidates[1:4]], hints


def design_parts(orl, doc) -> list[dict]:
    out = []
    for c in orl.get_all_components(doc.getRocket()):
        kind = str(c.getClass().getSimpleName())
        item = {"type": kind, "name": str(c.getName()), "mass_g": round(float(c.getMass()) * 1000, 1)}
        for getter, key in (("getOuterRadius", "outer_diameter_mm"), ("getInnerRadius", "inner_diameter_mm"),
                            ("getAftRadius", "aft_diameter_mm"), ("getForeRadius", "fore_diameter_mm"),
                            ("getLength", "length_mm")):
            try:
                factor = 1000 if key == "length_mm" else 2000
                item[key] = round(float(getattr(c, getter)()) * factor, 1)
            except Exception:
                pass
        if kind == "RailButton":
            item["outer_diameter_mm"] = round(float(c.getOuterDiameter()) * 1000, 1)
        try:
            item["preset"] = str(c.getPresetComponent().getPartNo()) if c.getPresetComponent() else None
        except Exception:
            pass
        out.append(item)
    return out


def design_motors(doc) -> list[dict]:
    from openrocket import motor_configurations
    motors = []
    for fc in motor_configurations(doc.getRocket()):
        for m in fc.getActiveMotors():
            mot = m.getMotor()
            motors.append({"designation": f"{mot.getManufacturer()} {mot.getDesignation()}",
                           "diameter_mm": round(float(mot.getDiameter()) * 1000, 1)})
    return motors


def check_against_design(part: dict, best: dict | None, design: list[dict], motors: list[dict]) -> list[str]:
    notes = []
    if not best:
        return notes
    type_name = CATEGORY_TYPES.get(part.get("category", "").lower())
    classes = DESIGN_CLASSES.get(type_name, set())
    dia = best.get("outer_diameter_mm") or best.get("aft_diameter_mm")
    same_kind = [d for d in design if d["type"] in classes]
    if type_name == "TRANSITION":
        ends = sorted([best.get("fore_diameter_mm") or 0, best.get("aft_diameter_mm") or 0])
        for d in same_kind:
            mine = sorted([d.get("fore_diameter_mm") or 0, d.get("aft_diameter_mm") or 0])
            if all(abs(a - b) <= 1.0 for a, b in zip(ends, mine)):
                notes.append(f"In the design as '{d['name']}' ({mine[1]} to {mine[0]} mm, {d.get('length_mm')} mm long, "
                             f"{d['mass_g']} g in the design). The catalogue's closest part is {best['material']}; "
                             f"if yours is a different material, keep the weighed mass.")
                return notes
    if type_name == "RAIL_BUTTON" and same_kind:
        d = same_kind[0]
        notes.append(f"In the design: {len(same_kind)} rail buttons ({d.get('outer_diameter_mm')} mm, {d['mass_g']} g each in the design).")
        return notes
    if dia is not None:
        matches = [d for d in same_kind
                   if abs((d.get("outer_diameter_mm") or d.get("aft_diameter_mm") or -99) - dia) <= 1.0]
        if matches:
            m = matches[0]
            notes.append(f"In the design as '{m['name']}' ({m.get('outer_diameter_mm') or m.get('aft_diameter_mm')} mm, "
                         f"{m.get('length_mm')} mm long, {m['mass_g']} g in the design).")
            if best.get("mass_g") and type_name == "NOSE_CONE" and abs(m["mass_g"] - best["mass_g"]) > 2:
                notes.append(f"Design mass {m['mass_g']} g differs from the catalogue's {best['mass_g']} g; "
                             "weigh the part and record it as measured_mass_g.")
        else:
            sizes = sorted({d.get("outer_diameter_mm") or d.get("aft_diameter_mm") for d in same_kind} - {None})
            notes.append(f"NOT IN DESIGN: no {', '.join(sorted(classes))} of {dia} mm in the .ork "
                         f"(design has {sizes} mm).")
    if part.get("category", "").lower() == "mount" and best.get("inner_diameter_mm"):
        for motor in motors:
            if abs(best["inner_diameter_mm"] - motor["diameter_mm"]) > 1.0:
                notes.append(f"MOTOR DOES NOT FIT: {part['name']} inner diameter {best['inner_diameter_mm']} mm, "
                             f"but the design flies a {motor['diameter_mm']:.0f} mm {motor['designation']}. "
                             f"A {motor['diameter_mm']:.0f} mm motor needs a "
                             f"{'BT-50' if round(motor['diameter_mm']) == 24 else 'matching'} mount tube.")
    return notes


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--ork", type=Path, help="rocket design to check the parts against")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a report")
    ap.add_argument("--jar")
    args = ap.parse_args(argv)

    parts = read_parts(args.csv)
    sys.stdout.flush()
    real_stdout = os.dup(1)
    os.dup2(2, 1)  # OpenRocket logs to stdout; keep ours clean
    try:
        import jpype
        from openrocket import open_openrocket
        with open_openrocket(args.jar) as orl:
            CP = jpype.JClass("info.openrocket.core.preset.ComponentPreset")
            db = jpype.JClass("info.openrocket.core.startup.Application").getComponentPresetDao()
            design, motors = [], []
            if args.ork:
                doc = orl.load_doc(str(args.ork))
                design, motors = design_parts(orl, doc), design_motors(doc)
            results = []
            for part in parts:
                best, others, hints = find_in_catalogue(part, db, CP)
                if best and part.get("measured_mass_g"):
                    best = {**best, "mass_g": float(part["measured_mass_g"]), "mass_source": "measured"}
                results.append({
                    "row": part["row"], "category": part.get("category"), "name": part.get("name"),
                    "link": part.get("link"), "hints": hints, "catalogue_match": best,
                    "other_candidates": others,
                    "design_check": check_against_design(part, best, design, motors) if args.ork else None,
                })
    finally:
        sys.stdout.flush()
        os.dup2(real_stdout, 1)
        os.close(real_stdout)

    if args.json:
        print(json.dumps({"csv": str(args.csv), "ork": str(args.ork) if args.ork else None,
                          "motors": motors, "parts": results}, indent=2))
        return

    for r in results:
        print(f"\nRow {r['row']}: {r['category']} / {r['name']}")
        b = r["catalogue_match"]
        if not b:
            print("  No catalogue match." + (f" ({r['hints']['problem']})" if "problem" in r["hints"] else ""))
            continue
        dims = ", ".join(f"{k.replace('_mm', '')} {v} mm" for k, v in b.items() if k.endswith("_mm"))
        print(f"  Catalogue: {b['manufacturer']} {b['part_no']} — {b['description']}")
        print(f"  {dims}; mass {b.get('mass_g', '?')} g ({b.get('mass_source', 'unknown')})")
        if b.get("material"):
            print(f"  Material: {b['material']}")
        for note in r["design_check"] or []:
            print(f"  • {note}")


if __name__ == "__main__":
    main()
