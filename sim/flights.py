"""Parse the team's flight log (data/flights_2025_raw.csv) into clean records.

The raw sheet is typed by hand, so units are sometimes missing or mistyped. Each
record keeps the raw row and lists every assumption made in `notes`, and every
reason it should not be used to check the simulator in `exclude`.

    python sim/flights.py              # table
    python sim/flights.py --json       # clean records
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "flights_2025_raw.csv"

DESIGN_LENGTH_IN = 30          # rockets/ederance.ork is 30.5 in long
LOW_APOGEE_FT = 600            # far below every other flight: likely a bad flight or reading
LONG_DURATION_S = 60           # far above every other flight

_NUM = re.compile(r"-?\d*\.?\d+")


def _num(text: str) -> float | None:
    m = _NUM.search(text or "")
    return float(m.group()) if m else None


def _date(text: str, notes: list) -> str | None:
    text = text.strip()
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text):
        mo, d, y = (int(g) for g in m.groups())
        return date(y + 2000 if y < 100 else y, mo, d).isoformat()
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", text):
        notes.append(f"date '{text}' has no day")
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    notes.append(f"date '{text}' not understood")
    return None


def parse(path: Path = RAW) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [h.strip().lower() for h in next(reader)]
        rows = [dict(zip(header, r)) for r in reader if any(c.strip() for c in r)]

    records = []
    for i, raw in enumerate(rows, start=2):
        notes: list[str] = []
        exclude: list[str] = []
        get = lambda k: (raw.get(k) or "").strip()

        temp = _num(get("temp"))
        if temp is not None and "f" not in get("temp").lower():
            notes.append(f"temperature '{get('temp')}' has no unit; assumed °F")

        length = _num(get("length"))

        weight_text = get("weight")
        weight = _num(weight_text)
        if weight is not None and "g" not in weight_text.lower():
            unit = re.sub(r"[\d.\s]", "", weight_text)
            notes.append(f"weight '{weight_text}' " + (f"has unit '{unit}'" if unit else "has no unit") + "; assumed grams")

        altitude = None if "n/a" in get("altitude").lower() else _num(get("altitude"))
        duration = None if "n/a" in get("time").lower() else _num(get("time"))

        wind_text = get("wind")
        wind = None if not wind_text or "n/a" in wind_text.lower() else _num(wind_text)
        if wind is not None and "mph" not in wind_text.lower():
            notes.append(f"wind '{wind_text}' has no clear unit; assumed mph")

        if altitude is None or duration is None:
            exclude.append("no altitude or time recorded")
        if length is not None and round(length) != DESIGN_LENGTH_IN:
            exclude.append(f"{length:g} in build, not the {DESIGN_LENGTH_IN} in design")
        if altitude is not None and altitude < LOW_APOGEE_FT:
            exclude.append(f"apogee {altitude:g} ft is far below the other flights")
        if duration is not None and duration > LONG_DURATION_S:
            exclude.append(f"duration {duration:g} s is far above the other flights")
        if weight is None:
            exclude.append("no weight recorded")
        if wind is None:
            notes.append("no wind recorded; assumed calm (0 mph)")

        records.append({
            "row": i,
            "launch": re.sub(r"\s+", " ", get("launch #")).strip(),
            "date": _date(get("date"), notes),
            "temperature_f": temp,
            "length_in": length,
            "weight_g": weight,
            "apogee_ft": altitude,
            "duration_s": duration,
            "wind_mph": wind,
            "notes": notes,
            "exclude": exclude,
            "raw": raw,
        })
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    records = parse()
    if args.json:
        print(json.dumps(records, indent=2))
        return
    usable = [r for r in records if not r["exclude"]]
    print(f"{len(records)} flights, {len(usable)} usable for checking the simulator\n")
    for r in records:
        flag = "  " if not r["exclude"] else "✗ "
        print(f"{flag}row {r['row']:>2} {r['date'] or '?':10} {r['launch']:10} {r['temperature_f'] or '?':>4}°F "
              f"{r['weight_g'] or '?':>6} g  {r['apogee_ft'] or '-':>5} ft  {r['duration_s'] or '-':>5} s  "
              f"wind {r['wind_mph'] if r['wind_mph'] is not None else '?'}"
              + (f"   ✗ {'; '.join(r['exclude'])}" if r["exclude"] else "")
              + (f"   ({'; '.join(r['notes'])})" if r["notes"] else ""))


if __name__ == "__main__":
    main()
