"""Save a variant of a rocket design: another motor (mount resized to fit), tube
lengths, ballast. Uses the same checks and code as the worker's design changes.

    python sim/derive.py rockets/ederance.ork rockets/ederance-29mm.ork --motor "F59WT|Cesaroni" --delay 5

The original .ork is never changed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--motor", help='approved-list key, e.g. "F59WT|Cesaroni" (see data/motor_menu.json)')
    ap.add_argument("--delay", type=float)
    ap.add_argument("--nose-ballast-g", type=float)
    args = ap.parse_args()
    if args.out.resolve() == args.base.resolve():
        sys.exit("Write the variant to a new file; the original is kept as it is.")

    changes = {}
    if args.motor:
        changes["motor"] = args.motor
        if args.delay is not None:
            changes["delay_s"] = args.delay
    if args.nose_ballast_g is not None:
        changes.update(mass_mode="nose", mass_g=args.nose_ballast_g)

    real = os.dup(1)
    os.dup2(2, 1)   # OpenRocket logs to stdout
    try:
        from openrocket import motor_configurations, open_openrocket
        from worker import design_changes
        with open_openrocket() as orl:
            doc = orl.load_doc(str(args.base))
            prepare, motor, delay, _, _, _, applied = design_changes(changes)
            prepare(doc.getRocket())
            sim = doc.getSimulation(0)
            configs = motor_configurations(doc.getRocket())
            if configs:
                sim.setFlightConfigurationId(configs[0].getId())
            if motor is not None:
                orl.set_motor(sim, motor, delay=delay)
            orl.save_doc(str(args.out), doc)
    finally:
        sys.stdout.flush()
        os.dup2(real, 1)
        os.close(real)
    print(f"Wrote {args.out} with {applied or 'no changes'}")


if __name__ == "__main__":
    main()
