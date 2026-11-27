"""Design chat: turn a message into several rocket designs, simulate each, recommend one.

A fixed pipeline the worker runs (sim/worker.py) for each request in the
`design_chats` collection:

1. Fly the team rocket as it stands under the request's launch conditions
   (baseline, plus how apogee and duration respond to 10 g).
2. Ask Claude for 3-5 candidate designs, written as the same changes the app's
   builder sends (motor and delay from the ARC 2027 approved list, nose ballast,
   added mass, tube lengths, parachute sizes, moved/added/removed parts).
3. Simulate every candidate with OpenRocket and check it against the 2027 rules.
4. If none is close, one refinement round: Claude sees the results and proposes again.
5. Ask Claude to compare the simulated results and recommend one.

Claude only writes data in fixed JSON schemas; every number shown to the team
comes from the simulator, and every change is validated by the worker.

Needs an Anthropic API key on the worker (ANTHROPIC_API_KEY, or an `ant auth login`
profile).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-opus-5-5"
MAX_CANDIDATES = 5
GOOD_ENOUGH_SCORE = 20.0      # stop refining when a rule-passing design scores at or below this

RULES_2027 = """ARC 2027 (American Rocketry Challenge) parameters:
- Qualification target 800 ft apogee; Finals targets are between 775 and 825 ft (not 800).
- Flight duration window 37-40 s (launch to landing).
- Score (assumed from past years; lower is better): |apogee - target| ft + 4 x seconds outside 37-40 s.
- At least 650 mm long; at most 650 g at liftoff including the motor; single stage; at most 80 N-s total impulse;
  ARC-approved motors only.
- Airframe: at least two body-tube diameters, one a T-80 (66 mm) tube at least 12 in long; every other tube at
  least 9 mm different in diameter.
- Payload: two large eggs, 55-63 g each, any orientation. Recovery by one or more parachutes, all parts tethered.
- Safety (not a rule): stability at least 1.0 caliber; about 1-3 cal is the comfortable range."""

SYSTEM_TEMPLATE = """You design model rockets for a student team in the American Rocketry Challenge.
You change the team's existing rocket; you never invent parts the worker cannot build.

{rules}

How the tools behave (measured on this rocket):
- More mass lowers apogee and shortens duration. Nose ballast also raises stability; "added mass" keeps the CG.
- A smaller parachute shortens duration; parachute drag is calibrated from the team's 38 real flights.
- Moving heavy internal parts forward raises stability without changing apogee.
- Longer tubes add mass and length; they also move the CP.
- The motor mount resizes to the chosen motor's diameter.

The team rocket (part ids are what changes refer to; lengths in mm, mass in g, x from the nose tip):
{design}

ARC 2027 approved motors OpenRocket can simulate (key | diameter mm | NAR impulse N-s | avg thrust N | launch mass g | delays s | reloadable):
{motors}

Write every change in the given schema. Use only motor keys and delays from the list, part ids from the rocket,
and realistic values. Give each candidate a short name and one sentence on the idea. Make the candidates genuinely
different approaches, not small variations of one."""


# ---------------------------------------------------------------------------
# Context for the model
# ---------------------------------------------------------------------------

def design_brief(design: dict) -> str:
    S = design["summary"]
    lines = [f"Overall: {S['length_mm']:.0f} mm long, {S['max_diameter_mm']:.1f} mm diameter, "
             f"{S['mass_with_motor_g']:.0f} g at liftoff ({S['mass_no_motor_g']:.0f} g without motor), "
             f"CG {S['cg_mm']:.0f} mm, CP {S['cp_mm']:.0f} mm, stability {S['stability_cal']:.2f} cal."]
    for m in design["motors"]:
        lines.append(f"Motor now: {m['designation']}-{m['delay_s']:g} ({m['diameter_mm']:.0f} mm, {m['total_impulse_ns']} N-s).")
    for c in design["components"]:
        bits = [f"id {c['id']}", c.get("label") or c["name"], c["type"]]
        if c.get("parent") is not None:
            bits.append(f"in part {c['parent']} at {c.get('offset_in_parent_mm', 0):.0f} mm from its top")
        bits.append(f"x {c['x_mm']:.0f}, length {c['length_mm']:.0f}, {c['mass_g']:g} g")
        if c.get("outer_radius_mm"):
            bits.append(f"outer dia {2 * c['outer_radius_mm']:.1f}, inner dia {2 * c.get('inner_radius_mm', 0):.1f}")
        if c.get("canopy_diameter_mm"):
            bits.append(f"canopy {c['canopy_diameter_mm']:.0f} mm ({c['canopy_diameter_mm'] / 25.4:.0f} in)")
        if c.get("movable"):
            bits.append("movable/removable")
        lines.append("- " + "; ".join(bits))
    return "\n".join(lines)


def motor_brief(menu: list[dict]) -> str:
    rows = []
    for m in menu:
        if not m.get("simulatable"):
            continue
        rows.append(f"{m['key']} | {m['diameter_mm']:g} | {m['total_impulse_ns']:g} | {m.get('average_thrust_n', '?')} | "
                    f"{m.get('launch_mass_g', '?')} | {','.join(f'{d:g}' for d in m['delays_s'])} | "
                    f"{'yes' if m['reloadable'] else 'no'}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Schemas (structured outputs: every field required; null where not used)
# ---------------------------------------------------------------------------

def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_NUM_OR_NULL = {"type": ["number", "null"]}
CANDIDATE = _obj({
    "name": {"type": "string"},
    "idea": {"type": "string"},
    "motor": {"type": ["string", "null"], "description": "approved-list key, or null to keep the current motor"},
    "delay_s": _NUM_OR_NULL,
    "nose_ballast_g": _NUM_OR_NULL,
    "added_mass_g": {"type": ["number", "null"], "description": "spread so the CG stays; negative removes mass"},
    "tube_lengths": {"type": "array", "items": _obj({"part_id": {"type": "integer"}, "length_mm": {"type": "number"}})},
    "chute_diameters": {"type": "array", "items": _obj({"part_id": {"type": "integer"}, "diameter_mm": {"type": "number"}})},
    "moves": {"type": "array", "items": _obj({"part_id": {"type": "integer"}, "offset_mm": {"type": "number"}})},
    "removes": {"type": "array", "items": {"type": "integer"}},
    "adds": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "mass_g": {"type": "number"}, "length_mm": {"type": "number"},
        "diameter_mm": {"type": "number"}, "parent_id": {"type": "integer"}, "offset_mm": {"type": "number"}})},
})
PROPOSAL = _obj({
    "understanding": {"type": "string", "description": "what the team asked for, in one or two sentences"},
    "assumptions": {"type": "array", "items": {"type": "string"}},
    "candidates": {"type": "array", "items": CANDIDATE},
})
CHOICE = _obj({
    "recommended_index": {"type": "integer"},
    "headline": {"type": "string"},
    "explanation": {"type": "string"},
    "verdicts": {"type": "array", "items": _obj({"index": {"type": "integer"}, "verdict": {"type": "string"}})},
    "next_steps": {"type": "array", "items": {"type": "string"}},
})


def candidate_to_changes(c: dict) -> dict:
    """A candidate from the model -> the worker's changes format."""
    ch: dict = {}
    if c.get("motor"):
        ch["motor"] = c["motor"]
        if c.get("delay_s") is not None:
            ch["delay_s"] = float(c["delay_s"])
    if c.get("nose_ballast_g"):
        ch["mass_mode"], ch["mass_g"] = "nose", float(c["nose_ballast_g"])
    elif c.get("added_mass_g"):
        ch["mass_mode"], ch["mass_g"] = "spread", float(c["added_mass_g"])
    if c.get("tube_lengths"):
        ch["tube_lengths_mm"] = {str(t["part_id"]): float(t["length_mm"]) for t in c["tube_lengths"]}
    if c.get("chute_diameters"):
        ch["chute_diameters_mm"] = {str(t["part_id"]): float(t["diameter_mm"]) for t in c["chute_diameters"]}
    if c.get("moves"):
        ch["moves"] = {str(m["part_id"]): float(m["offset_mm"]) for m in c["moves"]}
    if c.get("removes"):
        ch["removes"] = [int(x) for x in c["removes"]]
    if c.get("adds"):
        ch["adds"] = [dict(a) for a in c["adds"]]
    return ch


# ---------------------------------------------------------------------------
# Rules check (same checks as the app's design page)
# ---------------------------------------------------------------------------

def rules_check(design: dict, menu_keys: set[str], motor_key: str | None, base_motor_key: str | None) -> list[dict]:
    S = design["summary"]
    tubes = [c for c in design["components"] if c["type"] == "BodyTube"]
    t80 = [t for t in tubes if abs(2 * t["outer_radius_mm"] - 66) <= 0.8]
    others = [t for t in tubes if t not in t80]
    impulse = sum(m["total_impulse_ns"] for m in design["motors"])
    key = motor_key or base_motor_key
    checks = [
        ("At least 650 mm long", S["airframe_length_mm"] >= 650),
        ("At most 650 g at liftoff", S["mass_with_motor_g"] <= 650),
        ("At most 80 N-s", impulse <= 80.5),
        ("Approved motor", key in menu_keys if key else False),
        ("T-80 tube at least 12 in", any(t["length_mm"] >= 304.8 for t in t80)),
        ("Other tubes at least 9 mm different", len(tubes) >= 2 and all(abs(2 * t["outer_radius_mm"] - 66) >= 9 for t in others)),
        ("Parachute recovery", any(c["type"] == "Parachute" for c in design["components"])),
        ("Stable (at least 1.0 cal)", S["stability_cal"] >= 1.0),
    ]
    return [{"rule": r, "ok": bool(ok)} for r, ok in checks]


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------

def ask(client, system: str, user: str, schema: dict, effort: str) -> dict:
    """One structured-output call. Refusals and truncation raise."""
    import anthropic
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                       # a declined request is retried on another model
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The design model declined this request. Rephrase it and try again.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The design model's answer was cut off. Try a shorter or simpler request.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("The design model returned no answer.")
    return json.loads(text)


def results_table(rows: list[dict], unit_note: str = "") -> str:
    out = []
    for i, r in enumerate(rows):
        if r.get("error"):
            out.append(f"[{i}] {r['name']}: could not be built: {r['error']}")
            continue
        m = r["metrics"]
        failed = [x["rule"] for x in r["rules"] if not x["ok"]]
        out.append(f"[{i}] {r['name']}: apogee {m['apogee_ft']:.0f} ft, duration {m['duration_s']:.1f} s, score {m['score']:.1f}, "
                   f"{m['liftoff_mass_g']:.0f} g, stability {m['stability_cal']:.2f} cal, off rail {m['velocity_off_rail_fps']:.0f} ft/s, "
                   f"motor {m['motor']}; rules: {'all pass' if not failed else 'FAILS ' + ', '.join(failed)}")
    return "\n".join(out) + unit_note


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def run_design_chat(orl, job: dict, calibration, run_job, progress=lambda stage: None, client=None) -> dict:
    """Run one design chat request. `run_job` is the worker's simulation entry point."""
    import anthropic
    client = client or anthropic.Anthropic()
    started = time.time()
    weather = job["weather"]
    target = float(job.get("target_apogee_ft", 800))
    menu = json.loads((ROOT / "data" / "motor_menu.json").read_text())["motors"]
    menu_keys = {m["key"] for m in menu if m.get("simulatable")}
    base_design = json.loads((ROOT / "designs" / "ederance-29mm.json").read_text())
    base_motor_key = "F59WT|Cesaroni"   # the motor rockets/ederance-29mm.ork carries

    def simulate(name: str, idea: str, changes: dict) -> dict:
        row = {"name": name, "idea": idea, "changes": changes}
        try:
            r = run_job(orl, {"rocket": "ederance", "weather": weather, "target_apogee_ft": target, "changes": changes}, calibration)
        except Exception as e:  # invalid change: report it, keep going
            row["error"] = str(e)[:300]
            return row
        design = json.loads(r["design_json"])
        rules = rules_check(design, menu_keys, changes.get("motor"), base_motor_key)
        row.update({
            "design_json": r["design_json"],
            "applied": r.get("changes"),
            "rules": rules,
            "passes_rules": all(x["ok"] for x in rules),
            "metrics": {"apogee_ft": r["apogee_ft"], "duration_s": r["duration_s"], "score": r["score"]["total"],
                        "liftoff_mass_g": design["summary"]["mass_with_motor_g"],
                        "stability_cal": design["summary"]["stability_cal"],
                        "velocity_off_rail_fps": r["velocity_off_rail_fps"], "motor": r["motor"],
                        "descent_rate_fps": r["descent_rate_fps"],
                        "sensitivity": (design.get("flight") or {}).get("sensitivity")},
        })
        return row

    progress("Flying the current rocket")
    baseline = simulate("Team rocket as it is", "No changes.", {})

    system = SYSTEM_TEMPLATE.format(rules=RULES_2027, design=design_brief(base_design), motors=motor_brief(menu))
    context = (f"Launch conditions: {weather['elevation_ft']:g} ft elevation, {weather['wind_mph']:g} mph wind, "
               f"{weather['humidity_pct']:g}% humidity, {weather['temperature_f']:g} F. Apogee target: {target:g} ft.\n"
               f"Current rocket under these conditions:\n{results_table([baseline])}\n")
    if job.get("previous"):
        p = job["previous"]
        context += f"\nEarlier in this conversation the team asked: {p.get('message', '')!r}\n"
        if p.get("recommended"):
            context += f"and you recommended: {json.dumps(p['recommended'])}\n"

    progress("Designing candidates")
    proposal = ask(client, system, context + f"\nThe team's request: {job['message']}\n\n"
                   f"Propose {MAX_CANDIDATES - 2} to {MAX_CANDIDATES} candidate designs.", PROPOSAL, effort="high")
    rows = []
    for c in proposal["candidates"][:MAX_CANDIDATES]:
        progress(f"Simulating {c['name']}")
        rows.append(simulate(c["name"], c["idea"], candidate_to_changes(c)))

    passing = [r for r in rows if r.get("passes_rules")]
    best = min((r["metrics"]["score"] for r in passing), default=None)
    refined = False
    if best is None or best > GOOD_ENOUGH_SCORE:
        progress("Refining")
        refined = True
        again = ask(client, system, context + f"\nThe team's request: {job['message']}\n\nYou proposed these, and the simulator gave:\n"
                    f"{results_table(rows)}\n\nPropose up to 3 improved candidates that fix what fell short "
                    f"(rules first, then score). Use the measured effects: per 10 g, apogee "
                    f"{(baseline.get('metrics') or {}).get('sensitivity') or 'about -11 ft and duration about -0.9 s'}.",
                    PROPOSAL, effort="high")
        for c in again["candidates"][:3]:
            progress(f"Simulating {c['name']}")
            rows.append(simulate(c["name"], c["idea"], candidate_to_changes(c)))

    progress("Comparing results")
    choice = ask(client, system, context + f"\nThe team's request: {job['message']}\n\nSimulated candidates:\n{results_table(rows)}\n\n"
                 "Recommend one. Prefer designs that pass every rule, then the lowest score, then the most robust "
                 "(stability 1-3 cal, fast off the rail, mass margin under 650 g for heavy eggs). Explain plainly for "
                 "students, give a one-line verdict for every candidate, and list concrete next steps.",
                 CHOICE, effort="medium")
    idx = choice["recommended_index"]
    if not (0 <= idx < len(rows)) or rows[idx].get("error"):
        ok = [i for i, r in enumerate(rows) if not r.get("error")]
        idx = min(ok, key=lambda i: (not rows[i].get("passes_rules"), rows[i]["metrics"]["score"])) if ok else None
    verdicts = {v["index"]: v["verdict"] for v in choice.get("verdicts", [])}
    for i, r in enumerate(rows):
        r["verdict"] = verdicts.get(i, "")
        r["recommended"] = i == idx
    return {
        "understanding": proposal.get("understanding", ""),
        "assumptions": proposal.get("assumptions", []),
        "baseline": {k: v for k, v in baseline.items() if k != "design_json"},
        "candidates": rows,
        "recommended_index": idx,
        "headline": choice.get("headline", ""),
        "explanation": choice.get("explanation", ""),
        "next_steps": choice.get("next_steps", []),
        "refined": refined,
        "model": MODEL,
        "seconds": round(time.time() - started, 1),
    }
