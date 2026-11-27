"""Build the app's motor menu: every ARC 2027 approved motor, with usable delays.

Reads data/arc2027_motors.csv, keeps approved motors (withdrawn delays removed),
matches each to OpenRocket's motor database, and writes data/motor_menu.json:

    python sim/motor_menu.py

Each entry has a key the app sends back ("F42T|Aerotech"), the NAR-tested impulse
ARC uses, diameter and length, average thrust, whether it is reloadable, and the
delays the team can set: the listed ones for fixed-delay motors, the database's
steps up to the maximum for adjustable Cesaroni motors, and S/M/L in seconds for
the AeroTech F62T. Motors OpenRocket cannot simulate are listed but marked.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "arc2027_motors.csv"
OUT = ROOT / "data" / "motor_menu.json"


def usable_delays(row: dict, motor) -> tuple[list[float], str]:
    raw = [d for d in row["delays_s"].split(";") if d]
    standard = sorted({float(d) for d in motor.getStandardDelays()}) if motor is not None else []
    if raw and raw[0].endswith("A"):
        top = float(raw[0][:-1])
        steps = [d for d in standard if 0 < d <= top] or [top]
        return steps, f"adjustable, up to {top:g} s"
    if raw and raw[0] in ("S", "M", "L"):
        picked = [d for d in standard if d > 0][:3] if motor is not None else []
        if len(picked) == 3:
            return picked, "S/M/L = " + "/".join(f"{d:g}" for d in picked) + " s"
        return picked, "S/M/L"
    delays = []
    for d in raw:
        d = d.rstrip("T").replace("P", "")
        try:
            delays.append(float(d))
        except ValueError:
            pass
    return delays, "fixed"


def main() -> None:
    rows = [r for r in csv.DictReader(CSV.open()) if r["status"] == "approved"]
    sys.stdout.flush()
    real = os.dup(1)
    os.dup2(2, 1)   # OpenRocket logs to stdout
    try:
        from openrocket import find_motor, open_openrocket
        menu = []
        with open_openrocket():
            for r in rows:
                dia = float(r["diameter_mm"])
                motor = find_motor(r["designation"], r["manufacturer"], float(r["total_impulse_ns"]), dia)
                delays, kind = usable_delays(r, motor)
                entry = {
                    "key": f"{r['designation']}|{r['manufacturer']}",
                    "designation": r["designation"],
                    "manufacturer": r["manufacturer"],
                    "class": r["designation"].lstrip("1/2")[0],
                    "diameter_mm": dia,
                    "length_mm": float(r["length_mm"]),
                    "total_impulse_ns": float(r["total_impulse_ns"]),
                    "propellant_g": float(r["propellant_g"]),
                    "reloadable": r["reloadable"] == "yes",
                    "delays_s": delays,
                    "delay_kind": kind,
                    "note": r["note"],
                    "simulatable": motor is not None,
                }
                if motor is not None:
                    scale = float(r["total_impulse_ns"]) / float(motor.getTotalImpulseEstimate())
                    entry.update({
                        "database_motor": f"{motor.getManufacturer().getDisplayName()} {motor.getDesignation()}",
                        "average_thrust_n": round(float(motor.getAverageThrustEstimate()) * scale, 1),
                        "burn_time_s": round(float(motor.getBurnTimeEstimate()), 2),
                        "launch_mass_g": round(float(motor.getLaunchMass()) * 1000, 1),
                    })
                menu.append(entry)
    finally:
        sys.stdout.flush()
        os.dup2(real, 1)
        os.close(real)
    OUT.write_text(json.dumps({"source": "NAR motors approved for ARC 2027 (as of June 29, 2026)",
                               "motors": menu}, indent=1) + "\n")
    missing = [m["key"] for m in menu if not m["simulatable"]]
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(menu)} motors"
          + (f"; not in OpenRocket's database: {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
