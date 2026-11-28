"""Simulation worker: runs the app's simulation requests with OpenRocket.

The app writes a request to Firestore (`sim_jobs`, status "pending"). This worker
watches for them, claims one (so two workers never run the same request), runs
the simulation and writes the result back (status "done", or "failed" with an
error). It makes no inbound connections; it only talks to Firebase.

Set up once per computer:

    python sim/worker.py --register
        Creates this worker's own Firebase account and saves its sign-in key in
        ~/.config/athens-worker.json (never in the repo). Prints the worker ID:
        add it to isWorker() in firestore.rules and publish the rules.

Run:

    python sim/worker.py               # keep running, check every 3 s
    python sim/worker.py --once        # handle what is waiting, then stop

Only workers listed in firestore.rules can claim requests or write results.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("ATHENS_WORKER_CONFIG", Path.home() / ".config" / "athens-worker.json"))
COLLECTION = "sim_jobs"
CHAT_COLLECTION = "design_chats"
AUTO_COLLECTION = "auto_runs"      # automatic design runs (sim/autodesign.py)   # design chat requests (sim/designer.py)
# Rockets the app may ask for. "ederance" is the team rocket as now built: ederance.ork
# with a 29 mm motor mount (rockets/ederance-29mm.ork, made by sim/derive.py).
ROCKETS = {"ederance": ROOT / "rockets" / "ederance-29mm.ork"}


def firebase_config() -> dict:
    """apiKey and projectId from firebase-config.js, the same values the web app uses."""
    text = (ROOT / "firebase-config.js").read_text()
    import re
    return {k: re.search(k + r':\s*"([^"]+)"', text).group(1) for k in ("apiKey", "projectId")}


def _http(method: str, url: str, body=None, token: str | None = None, form: bool = False):
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


import urllib.parse  # noqa: E402  (used by _http)


# ---------------------------------------------------------------------------
# Auth: the worker's own account; the refresh token stays on this computer
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, cfg: dict):
        self.key, self.project = cfg["apiKey"], cfg["projectId"]
        saved = json.loads(CONFIG.read_text())
        self.uid, self.refresh_token = saved["uid"], saved["refresh_token"]
        self.token, self.expires = None, 0.0

    def id_token(self) -> str:
        if time.time() < self.expires - 120:
            return self.token
        status, r = _http("POST", f"https://securetoken.googleapis.com/v1/token?key={self.key}",
                          {"grant_type": "refresh_token", "refresh_token": self.refresh_token}, form=True)
        if status != 200:
            raise SystemExit(f"Worker sign-in failed ({r.get('error', r)}). Run --register again.")
        self.token, self.expires = r["id_token"], time.time() + int(r["expires_in"])
        self.refresh_token = r["refresh_token"]
        return self.token


def register(cfg: dict) -> None:
    if CONFIG.exists():
        uid = json.loads(CONFIG.read_text())["uid"]
        print(f"This computer is already registered as worker {uid} ({CONFIG}).")
        return
    status, r = _http("POST", f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={cfg['apiKey']}",
                      {"returnSecureToken": True})
    if status != 200:
        raise SystemExit(f"Could not create the worker account: {r.get('error', r)}")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"uid": r["localId"], "refresh_token": r["refreshToken"],
                                  "registered": datetime.now(timezone.utc).isoformat()}))
    CONFIG.chmod(0o600)
    print(f"Registered worker {r['localId']}.\nSign-in key saved to {CONFIG} (keep it private).\n"
          f"Add this ID to isWorker() in firestore.rules and publish the rules.")


# ---------------------------------------------------------------------------
# Firestore REST: values in and out
# ---------------------------------------------------------------------------

def to_value(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: to_value(x) for k, x in v.items()}}}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [to_value(x) for x in v]}}
    return {"stringValue": str(v)}


def from_value(v):
    kind, x = next(iter(v.items()))
    if kind == "mapValue":
        return {k: from_value(y) for k, y in x.get("fields", {}).items()}
    if kind == "arrayValue":
        return [from_value(y) for y in x.get("values", [])]
    if kind == "integerValue":
        return int(x)
    if kind == "nullValue":
        return None
    return x


class Store:
    def __init__(self, session: Session):
        self.s = session
        self.base = f"https://firestore.googleapis.com/v1/projects/{session.project}/databases/(default)/documents"

    def get(self, collection: str, doc_id: str) -> dict | None:
        status, r = _http("GET", f"{self.base}/{collection}/{doc_id}", None, self.s.id_token())
        return r if status == 200 else None

    def pending(self, limit: int = 5, collection: str = COLLECTION) -> list[dict]:
        q = {"structuredQuery": {"from": [{"collectionId": collection}],
                                 "where": {"fieldFilter": {"field": {"fieldPath": "status"}, "op": "EQUAL",
                                                           "value": {"stringValue": "pending"}}},
                                 "limit": limit}}
        status, r = _http("POST", f"{self.base}:runQuery", q, self.s.id_token())
        if status != 200:
            raise RuntimeError(f"Could not list requests ({status}): {r}")
        return [x["document"] for x in r if "document" in x]

    def update(self, doc: dict, fields: dict, *, server_time: list[str] = (), if_unchanged: bool = False) -> bool:
        write = {
            "update": {"name": doc["name"], "fields": {k: to_value(v) for k, v in fields.items()}},
            "updateMask": {"fieldPaths": list(fields)},   # server-time fields are set by the transforms
            "updateTransforms": [{"fieldPath": f, "setToServerValue": "REQUEST_TIME"} for f in server_time],
        }
        if if_unchanged:
            write["currentDocument"] = {"updateTime": doc["updateTime"]}
        status, r = _http("POST", f"{self.base}:commit", {"writes": [write]}, self.s.id_token())
        if status == 200:
            return True
        if if_unchanged and status in (400, 409) and "FAILED_PRECONDITION" in json.dumps(r):
            return False  # another worker claimed it first
        raise RuntimeError(f"Write failed ({status}): {r}")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

MOTOR_MENU = ROOT / "data" / "motor_menu.json"
MAX_TUBE_MM, MIN_TUBE_MM = 1500.0, 50.0

ADD_KINDS = {"mass", "parachute", "shockcord", "bulkhead", "coupler", "centeringring", "bodytube", "fins"}
SHAPES = {"CONICAL", "OGIVE", "ELLIPSOID", "POWER", "PARABOLIC", "HAACK"}


def _num(d: dict, key: str, lo: float, hi: float, what: str, required: bool = False):
    v = d.get(key)
    if v is None:
        if required:
            raise ValueError(f"{what} is missing")
        return None
    v = float(v)
    if not lo <= v <= hi:
        raise ValueError(f"{what} must be {lo:g}-{hi:g}")
    return v


def check_fins(d: dict) -> None:
    _num(d, "fin_count", 1, 8, "fin count")
    _num(d, "root_chord_mm", 10, 400, "fin root chord (mm)")
    _num(d, "tip_chord_mm", 0, 400, "fin tip chord (mm)")
    _num(d, "span_mm", 5, 300, "fin span (mm)")
    _num(d, "sweep_mm", -200, 400, "fin sweep (mm)")
    _num(d, "thickness_mm", 0.5, 12, "fin thickness (mm)")


def check_edit(e: dict) -> None:
    allowed = {"length_mm", "mass_g", "shape", "cd", "fin_count", "root_chord_mm", "tip_chord_mm", "span_mm",
               "sweep_mm", "thickness_mm"}
    if not set(e) <= allowed:
        raise ValueError(f"cannot edit {sorted(set(e) - allowed)}")
    _num(e, "length_mm", 1, 1500, "length (mm)")
    _num(e, "mass_g", 0, 500, "part mass (g)")
    _num(e, "cd", 0.3, 2.5, "parachute drag coefficient")
    if e.get("shape") is not None and e["shape"] not in SHAPES:
        raise ValueError(f"shape must be one of {sorted(SHAPES)}")
    check_fins(e)


def check_add(a: dict) -> None:
    kind = a.get("kind", "mass")
    if kind not in ADD_KINDS:
        raise ValueError(f"cannot add a part of kind '{kind}'")
    if not str(a.get("name", "")).strip():
        raise ValueError("an added part needs a name")
    _num(a, "mass_g", 0, 300, "added part mass (g)", required=kind == "mass")
    if kind == "mass" and not float(a["mass_g"]) > 0:
        raise ValueError("an added mass needs more than 0 g")
    _num(a, "length_mm", 1, 1500 if kind == "bodytube" else 400, "added part length (mm)", required=kind != "fins")
    _num(a, "diameter_mm", 1, 120, "added part diameter (mm)")
    _num(a, "offset_mm", -400, 1500, "position (mm)")
    _num(a, "canopy_diameter_mm", 150, 1500, "parachute canopy (mm)", required=kind == "parachute")
    _num(a, "cd", 0.3, 2.5, "parachute drag coefficient")
    if kind == "bodytube" and a.get("after_id") is None:
        raise ValueError("a new body tube needs to go after a part of the airframe")
    if kind != "bodytube" and a.get("parent_id") is None:
        raise ValueError("an added part needs a tube to go in")
    if kind == "fins":
        for k in ("fin_count", "root_chord_mm", "tip_chord_mm", "span_mm", "sweep_mm", "thickness_mm"):
            if a.get(k) is None:
                raise ValueError(f"new fins need {k}")
        check_fins(a)


def _override_mass(c, grams) -> None:
    if grams is None:
        return
    if str(c.getClass().getSimpleName()) == "MassComponent":
        c.setComponentMass(float(grams) / 1000.0)
    else:
        c.setMassOverridden(True)
        c.setOverrideMass(float(grams) / 1000.0)


def _set_fin_shape(c, d: dict) -> None:
    """Fin count, thickness and a trapezoid outline (root, tip, span, sweep) on any fin set.

    A freeform fin set becomes the trapezoid's four points; OpenRocket then calculates it the same way.
    """
    import jpype
    kind = str(c.getClass().getSimpleName())
    if d.get("fin_count") is not None:
        c.setFinCount(int(d["fin_count"]))
    if d.get("thickness_mm") is not None:
        c.setThickness(float(d["thickness_mm"]) / 1000.0)
    geom = [d.get(k) for k in ("root_chord_mm", "tip_chord_mm", "span_mm", "sweep_mm")]
    if all(v is None for v in geom):
        return
    if kind == "TrapezoidFinSet":
        cur = [c.getRootChord(), c.getTipChord(), c.getHeight(), c.getSweep()]
        root, tip, span, sweep = [float(v) / 1000.0 if v is not None else float(o) for v, o in zip(geom, cur)]
        c.setRootChord(root); c.setTipChord(tip); c.setHeight(span); c.setSweep(sweep)
    elif kind == "FreeformFinSet":
        if any(v is None for v in geom):
            raise ValueError("changing a freeform fin's outline needs root chord, tip chord, span and sweep")
        root, tip, span, sweep = [float(v) / 1000.0 for v in geom]
        Coordinate = jpype.JClass("info.openrocket.core.util.Coordinate")
        pts = jpype.JArray(Coordinate)([Coordinate(0, 0), Coordinate(sweep, span), Coordinate(sweep + tip, span), Coordinate(root, 0)])
        c.setPoints(pts)
    else:
        raise ValueError(f"cannot reshape a {kind}")
    if float(c.getTabLength()) > float(c.getLength()):
        c.setTabLength(float(c.getLength()))


def apply_edit(c, e: dict) -> None:
    import jpype
    kind = str(c.getClass().getSimpleName())
    if e.get("length_mm") is not None:
        if kind.endswith("FinSet") or kind in ("RailButton", "LaunchLug"):
            raise ValueError(f"{c.getName()}: length cannot be set this way")
        c.setLength(float(e["length_mm"]) / 1000.0)
    if e.get("shape") is not None:
        if kind not in ("NoseCone", "Transition"):
            raise ValueError(f"{c.getName()} has no shape")
        Shape = jpype.JClass("info.openrocket.core.rocketcomponent.Transition$Shape")
        c.setShapeType(Shape.valueOf(e["shape"]))
    if e.get("cd") is not None:
        if kind != "Parachute":
            raise ValueError(f"{c.getName()} is not a parachute")
        c.setCD(float(e["cd"]))
    if any(e.get(k) is not None for k in ("fin_count", "root_chord_mm", "tip_chord_mm", "span_mm", "sweep_mm", "thickness_mm")):
        if not kind.endswith("FinSet"):
            raise ValueError(f"{c.getName()} is not a fin set")
        _set_fin_shape(c, e)
    _override_mass(c, e.get("mass_g"))


def build_part(a: dict, targets: dict):
    """Create one added part in the OpenRocket model and place it."""
    import jpype
    J = lambda name: jpype.JClass("info.openrocket.core.rocketcomponent." + name)
    AxialMethod = J("position.AxialMethod")
    kind = a.get("kind", "mass")
    name = str(a["name"]).strip()[:40]
    if kind == "bodytube":
        after = targets[int(a["after_id"])]
        stage = after.getParent()
        if str(stage.getClass().getSimpleName()) != "AxialStage":
            raise ValueError("a body tube goes after a nose cone, body tube or transition")
        tube = J("BodyTube")()
        tube.setName(name)
        tube.setLength(float(a["length_mm"]) / 1000.0)
        prev_r = float(after.getAftRadius()) if hasattr(after, "getAftRadius") else float(after.getOuterRadius())
        tube.setOuterRadius(float(a["diameter_mm"]) / 2000.0 if a.get("diameter_mm") else prev_r)
        if hasattr(after, "getThickness") and str(after.getClass().getSimpleName()) == "BodyTube":
            tube.setThickness(float(after.getThickness()))
        stage.addChild(tube, jpype.JInt(int(stage.getChildPosition(after)) + 1))
        _override_mass(tube, a.get("mass_g"))
        return tube
    parent = targets[int(a["parent_id"])]
    pkind = str(parent.getClass().getSimpleName())
    if kind == "fins":
        if pkind != "BodyTube":
            raise ValueError("fins go on a body tube")
        item = J("TrapezoidFinSet")()
        item.setName(name)
        parent.addChild(item)
        _set_fin_shape(item, a)
    else:
        if pkind not in {"BodyTube", "NoseCone", "Transition", "InnerTube", "TubeCoupler"}:
            raise ValueError("parts can only be added inside a tube or the nose cone")
        inner = float(parent.getInnerRadius()) if hasattr(parent, "getInnerRadius") else 0.0
        if inner <= 0:
            inner = float(parent.getAftShoulderRadius()) if hasattr(parent, "getAftShoulderRadius") else 0.03
        want_r = float(a["diameter_mm"]) / 2000.0 if a.get("diameter_mm") else inner
        if kind in ("mass", "parachute", "shockcord"):
            item = {"mass": J("MassComponent"), "parachute": J("Parachute"), "shockcord": J("ShockCord")}[kind]()
            item.setName(name)
            item.setLength(float(a["length_mm"]) / 1000.0)
            item.setRadius(min(want_r, inner))
            if kind == "parachute":
                item.setDiameter(float(a["canopy_diameter_mm"]) / 1000.0)
                item.setCD(float(a.get("cd") or 0.8))
        else:
            item = {"bulkhead": J("Bulkhead"), "coupler": J("TubeCoupler"), "centeringring": J("CenteringRing")}[kind]()
            item.setName(name)
            item.setLength(float(a["length_mm"]) / 1000.0)
            item.setOuterRadiusAutomatic(True)
            if kind == "coupler":
                item.setThickness(0.0008)
        parent.addChild(item)
        _override_mass(item, a.get("mass_g"))
    length_mm = float(item.getLength()) * 1000
    parent_len = float(parent.getLength()) * 1000
    offset = float(a.get("offset_mm") or 0)
    if kind != "coupler":   # a coupler may stick out of the top of its tube
        offset = max(0.0, min(offset, parent_len - length_mm))
    item.setAxialMethod(AxialMethod.TOP)
    item.setAxialOffset(offset / 1000.0)
    return item


def design_changes(changes: dict):
    """Check a request's design changes and turn them into simulation arguments.

    Returns (prepare, motor, delay_s, listeners, extra_mass_g, liftoff_mass_g, applied),
    where `applied` describes what was changed, for the app to show.
    """
    from openrocket import (add_nose_ballast, component_by_id, find_motor, motor_mount,
                            set_mount_diameter, thrust_scale_listener)
    applied: dict = {}
    motor = delay = None
    listeners = []
    entry = None
    if changes.get("motor"):
        menu = {m["key"]: m for m in json.loads(MOTOR_MENU.read_text())["motors"]}
        entry = menu.get(changes["motor"])
        if entry is None:
            raise ValueError(f"motor {changes['motor']} is not on the ARC 2027 approved list")
        if entry["total_impulse_ns"] > 80:
            raise ValueError("ARC allows at most 80 N-s")
        delay = float(changes.get("delay_s", entry["delays_s"][0] if entry["delays_s"] else 0))
        if entry["delays_s"] and delay not in entry["delays_s"]:
            raise ValueError(f"{entry['designation']} delays are {entry['delays_s']} s, not {delay:g}")
        motor = find_motor(entry["designation"], entry["manufacturer"], entry["total_impulse_ns"], entry["diameter_mm"])
        if motor is None:
            raise ValueError(f"OpenRocket has no data for {entry['designation']}")
        listeners.append(thrust_scale_listener(entry["total_impulse_ns"] / float(motor.getTotalImpulseEstimate())))
        applied["motor"] = f"{entry['manufacturer']} {entry['designation']}-{delay:g}"

    lengths = {int(k): float(v) for k, v in (changes.get("tube_lengths_mm") or {}).items()}
    for v in lengths.values():
        if not MIN_TUBE_MM <= v <= MAX_TUBE_MM:
            raise ValueError(f"tube lengths must be {MIN_TUBE_MM:g}-{MAX_TUBE_MM:g} mm")

    mode = changes.get("mass_mode")
    mass = changes.get("mass_g")
    if mass is not None:
        mass = float(mass)
        if mode == "nose" and not 0 <= mass <= 300:
            raise ValueError("nose ballast must be 0-300 g")
        if mode == "total" and not 100 <= mass <= 1000:
            raise ValueError("liftoff mass must be 100-1000 g")
        if mode == "spread" and not -300 <= mass <= 300:
            raise ValueError("added mass must be -300 to +300 g")

    # Moved, added, removed and edited parts. Ids are those of the design the app showed (before changes).
    MOVABLE = {"MassComponent", "Parachute", "Streamer", "ShockCord"}
    REMOVABLE = MOVABLE | {"TrapezoidFinSet", "FreeformFinSet", "EllipticalFinSet", "TubeFinSet", "Bulkhead",
                           "TubeCoupler", "CenteringRing", "RailButton", "LaunchLug", "InnerTube", "EngineBlock"}
    TUBES = {"BodyTube", "NoseCone", "Transition", "InnerTube", "TubeCoupler"}
    chutes = {int(k): float(v) for k, v in (changes.get("chute_diameters_mm") or {}).items()}
    for v in chutes.values():
        if not 150 <= v <= 1500:
            raise ValueError("parachute canopy diameters must be 150-1500 mm (6-59 in)")
    moves = {int(k): float(v) for k, v in (changes.get("moves") or {}).items()}
    removes = [int(x) for x in (changes.get("removes") or [])]
    adds = list(changes.get("adds") or [])
    edits = {int(k): dict(v) for k, v in (changes.get("edits") or {}).items()}
    if len(moves) > 30 or len(removes) > 20 or len(adds) > 12 or len(edits) > 30:
        raise ValueError("too many part changes in one request")
    for a in adds:
        check_add(a)
    for e in edits.values():
        check_edit(e)

    def prepare(rocket):
        import jpype
        for key in ("tubes", "moved", "added", "removed", "chutes", "edited"):
            applied.pop(key, None)
        # Resolve every id against the unchanged design first.
        wanted = {*lengths, *moves, *removes, *chutes, *edits}
        wanted |= {int(a["parent_id"]) for a in adds if a.get("parent_id") is not None}
        wanted |= {int(a["after_id"]) for a in adds if a.get("after_id") is not None}
        targets = {cid: component_by_id(rocket, cid) for cid in wanted}
        if any(c is None for c in targets.values()):
            raise ValueError("a part in the request does not exist in the design")
        kind_of = lambda c: str(c.getClass().getSimpleName())
        if entry is not None:
            mount = motor_mount(rocket)
            length = max(float(mount.getLength()) * 1000, entry["length_mm"] + 5)
            set_mount_diameter(rocket, entry["diameter_mm"], length)
        for cid, mm in lengths.items():
            c = targets[cid]
            if kind_of(c) != "BodyTube":
                raise ValueError(f"part {cid} is not a body tube")
            applied.setdefault("tubes", []).append({
                "diameter_mm": round(float(c.getOuterRadius()) * 2000, 1),
                "from_mm": round(float(c.getLength()) * 1000, 1), "to_mm": mm})
            c.setLength(mm / 1000.0)
        for cid, dia in chutes.items():
            c = targets[cid]
            if kind_of(c) != "Parachute":
                raise ValueError(f"part {cid} is not a parachute")
            applied.setdefault("chutes", []).append({"name": str(c.getName()),
                                                     "from_mm": round(float(c.getDiameter()) * 1000, 1), "to_mm": dia})
            c.setDiameter(dia / 1000.0)
        for cid, e in edits.items():
            apply_edit(targets[cid], e)
            applied.setdefault("edited", []).append({"name": str(targets[cid].getName()), **e})
        AxialMethod = jpype.JClass("info.openrocket.core.rocketcomponent.position.AxialMethod")
        for cid, offset in moves.items():
            c = targets[cid]
            if kind_of(c) not in MOVABLE:
                raise ValueError(f"part {cid} cannot be moved")
            parent_len = float(c.getParent().getLength()) * 1000
            offset = max(0.0, min(offset, parent_len - float(c.getLength()) * 1000))
            c.setAxialMethod(AxialMethod.TOP)
            c.setAxialOffset(offset / 1000.0)
            applied.setdefault("moved", []).append({"name": str(c.getName()), "offset_mm": round(offset, 1)})
        for a in adds:
            item = build_part(a, targets)
            applied.setdefault("added", []).append({"name": str(item.getName()), "kind": a.get("kind", "mass"),
                                                     "mass_g": a.get("mass_g")})
        for cid in removes:
            c = targets[cid]
            if kind_of(c) not in REMOVABLE or (kind_of(c) == "InnerTube" and c.isMotorMount()):
                raise ValueError(f"part {cid} ({c.getName()}) cannot be removed")
            applied.setdefault("removed", []).append({"name": str(c.getName()), "mass_g": round(float(c.getMass()) * 1000, 1)})
            c.getParent().removeChild(c)
        if mass is not None and mode == "nose":
            add_nose_ballast(rocket, mass)
            applied["nose_ballast_g"] = mass

    extra = mass if (mass is not None and mode == "spread") else None
    total = mass if (mass is not None and mode == "total") else None
    if extra is not None:
        applied["added_mass_g"] = extra
    if total is not None:
        applied["liftoff_mass_g"] = total
    return prepare, motor, delay, listeners, extra, total, applied


def run_job(orl, job: dict, calibration) -> dict:
    from openrocket import Launch
    from simulate import run_with
    rocket = job.get("rocket", "ederance")
    if rocket not in ROCKETS:
        raise ValueError(f"unknown rocket '{rocket}'")
    w = job["weather"]
    launch = Launch(elevation_ft=float(w["elevation_ft"]), wind_mph=float(w["wind_mph"]),
                    humidity_pct=float(w["humidity_pct"]), temperature_f=float(w["temperature_f"]))
    from openrocket import calibration_for
    cal = calibration_for(calibration, ROCKETS[rocket].name)
    changes = job.get("changes")
    extra_args, applied = {}, None
    if changes is not None:
        prepare, motor, delay, listeners, extra, total, applied = design_changes(changes)
        extra_args = dict(prepare=prepare, motor=motor, delay_s=delay, listeners=listeners,
                          extra_mass_g=extra, liftoff_mass_g=total, keep_doc=True)
    result = run_with(orl, ROCKETS[rocket], launch, calibration=cal,
                      target_apogee_ft=float(job.get("target_apogee_ft", 800)), **extra_args)
    p = result["predicted"]
    sensitivity = None
    if changes is not None:
        # How apogee and duration respond to 10 g more (spread so the CG stays put), for suggestions.
        plus = dict(extra_args, keep_doc=False)
        if plus.get("liftoff_mass_g") is not None:
            plus["liftoff_mass_g"] += 10
        else:
            plus["extra_mass_g"] = (plus.get("extra_mass_g") or 0) + 10
        r10 = run_with(orl, ROCKETS[rocket], launch, calibration=cal,
                       target_apogee_ft=float(job.get("target_apogee_ft", 800)), **plus)["predicted"]
        sensitivity = {"apogee_ft_per_10g": round(r10["apogee_ft"] - p["apogee_ft"], 2),
                       "duration_s_per_10g": round(r10["duration_s"] - p["duration_s"], 3)}
    design_json = None
    if changes is not None:
        from design import export
        flight = {"conditions": w, "calibration_id": result["calibration_id"], "predicted": p,
                  "score": result["score"], "target_apogee_ft": result["target_apogee_ft"]}
        flight["sensitivity"] = sensitivity
        design = export(orl, result.pop("_doc"), flight=flight, source=f"rockets/{rocket}.ork with changes")
        design["changes"] = applied
        design_json = json.dumps(design)   # Firestore cannot hold the fin outline's nested lists
    return {   # what the app shows; units in the names
        "sim_version": result["sim_version"],
        "calibration_id": result["calibration_id"],
        "motor": result["rocket"]["motors"][0]["designation"] + "-" + f"{result['rocket']['motors'][0]['delay_s']:g}"
                 if result["rocket"]["motors"] else None,
        "liftoff_mass_g": result["rocket"]["liftoff"]["mass_g"],
        "apogee_ft": p["apogee_ft"], "duration_s": p["duration_s"], "time_to_apogee_s": p["time_to_apogee_s"],
        "optimum_delay_s": p["optimum_delay_s"], "max_velocity_fps": p["max_velocity_fps"],
        "max_acceleration_g": p["max_acceleration_g"], "velocity_off_rail_fps": p["velocity_off_rail_fps"],
        "stability_off_rail_cal": p["stability_off_rail_cal"], "min_stability_cal": p["min_stability_cal"],
        "descent_rate_fps": p["descent_rate_fps"], "landing_distance_ft": p["landing_distance_ft"],
        "target_apogee_ft": result["target_apogee_ft"], "score": result["score"],
        "warnings": result["warnings"],
        **({"design_json": design_json, "changes": applied} if design_json else {}),
    }


def run_spec(orl, job: dict, calibration) -> dict:
    """Simulate a rocket built in the app from a blank page or a template (a part list)."""
    from build import TEMPLATE, build_rocket, check_spec, set_motor
    from design import export
    from openrocket import Launch, calibration_for, find_motor, thrust_scale_listener
    from simulate import run_with
    spec = json.loads(job["spec_json"])
    check_spec(spec)
    w = job["weather"]
    launch = Launch(elevation_ft=float(w["elevation_ft"]), wind_mph=float(w["wind_mph"]),
                    humidity_pct=float(w["humidity_pct"]), temperature_f=float(w["temperature_f"]))
    menu = {m["key"]: m for m in json.loads(MOTOR_MENU.read_text())["motors"]}
    entry = menu.get(str(spec.get("motor_key")))
    if entry is None:
        raise ValueError("pick a motor from the ARC 2027 approved list")
    if entry["total_impulse_ns"] > 80:
        raise ValueError("ARC allows at most 80 N-s")
    delay = float(spec.get("delay_s", entry["delays_s"][0] if entry["delays_s"] else 0))
    if entry["delays_s"] and delay not in entry["delays_s"]:
        raise ValueError(f"{entry['designation']} delays are {entry['delays_s']} s, not {delay:g}")
    mount = next(c for c in spec["components"] if c.get("type") == "InnerTube" and c.get("motor_mount"))
    if 2 * float(mount["inner_radius_mm"]) < entry["diameter_mm"] - 0.3:
        raise ValueError(f"a {entry['diameter_mm']} mm motor does not fit the {2 * float(mount['inner_radius_mm']):.1f} mm motor mount")
    motor = find_motor(entry["designation"], entry["manufacturer"], entry["total_impulse_ns"], entry["diameter_mm"])
    if motor is None:
        raise ValueError(f"OpenRocket has no data for {entry['designation']}")
    listeners = [thrust_scale_listener(entry["total_impulse_ns"] / float(motor.getTotalImpulseEstimate()))]
    liftoff = spec.get("liftoff_mass_g")
    if liftoff is not None and not 100 <= float(liftoff) <= 1000:
        raise ValueError("measured liftoff mass must be 100-1000 g")

    def simulate(extra=None):
        doc = orl.load_doc(str(TEMPLATE))
        build_rocket(doc.getRocket(), spec)
        set_motor(doc.getRocket(), motor, delay)
        return run_with(orl, TEMPLATE, launch, calibration=calibration_for(calibration, TEMPLATE.name),
                        target_apogee_ft=float(job.get("target_apogee_ft", 800)), doc=doc, listeners=listeners,
                        liftoff_mass_g=(float(liftoff) + (extra or 0)) if liftoff is not None else None,
                        extra_mass_g=None if liftoff is not None else extra, keep_doc=extra is None)

    result = simulate()
    p = result["predicted"]
    r10 = simulate(10)["predicted"]
    flight = {"conditions": w, "calibration_id": result["calibration_id"], "predicted": p, "score": result["score"],
              "target_apogee_ft": result["target_apogee_ft"],
              "sensitivity": {"apogee_ft_per_10g": round(r10["apogee_ft"] - p["apogee_ft"], 2),
                              "duration_s_per_10g": round(r10["duration_s"] - p["duration_s"], 3)}}
    design = export(orl, result.pop("_doc"), flight=flight, source="built in the app")
    return {
        "sim_version": result["sim_version"], "calibration_id": result["calibration_id"],
        "motor": f"{entry['manufacturer']} {entry['designation']}-{delay:g}",
        "liftoff_mass_g": result["rocket"]["liftoff"]["mass_g"],
        "apogee_ft": p["apogee_ft"], "duration_s": p["duration_s"], "time_to_apogee_s": p["time_to_apogee_s"],
        "optimum_delay_s": p["optimum_delay_s"], "max_velocity_fps": p["max_velocity_fps"],
        "max_acceleration_g": p["max_acceleration_g"], "velocity_off_rail_fps": p["velocity_off_rail_fps"],
        "stability_off_rail_cal": p["stability_off_rail_cal"], "min_stability_cal": p["min_stability_cal"],
        "descent_rate_fps": p["descent_rate_fps"], "landing_distance_ft": p["landing_distance_ft"],
        "target_apogee_ft": result["target_apogee_ft"], "score": result["score"], "warnings": result["warnings"],
        "design_json": json.dumps(design),
    }


def handle_chat(store: Store, orl, calibration, doc: dict) -> None:
    from designer import run_design_chat
    job = {k: from_value(v) for k, v in doc.get("fields", {}).items()}
    job_id = doc["name"].rsplit("/", 1)[1]
    if not store.update(doc, {"status": "running", "claimed_by": store.s.uid, "stage": "Starting"},
                        server_time=["started_at"], if_unchanged=True):
        return
    print(f"{job_id}: design chat {job.get('message', '')[:60]!r}", flush=True)
    # A follow-up carries the previous request's message and recommendation, from the same user only.
    if job.get("previous_id"):
        prev = store.get(CHAT_COLLECTION, str(job["previous_id"]))
        if prev:
            pf = {k: from_value(v) for k, v in prev.get("fields", {}).items()}
            if pf.get("uid") == job.get("uid"):
                result = pf.get("result") or {}
                rec = None
                idx = result.get("recommended_index")
                if isinstance(idx, int) and idx < len(result.get("candidates", [])):
                    c = result["candidates"][idx]
                    rec = {"name": c.get("name"), "changes": c.get("changes"), "metrics": c.get("metrics")}
                job["previous"] = {"message": pf.get("message", ""), "recommended": rec}
    t0 = time.time()
    try:
        result = run_design_chat(orl, job, calibration, run_job,
                                 progress=lambda stage: store.update(doc, {"stage": stage[:120]}))
        store.update(doc, {"status": "done", "stage": "Done", "result": result}, server_time=["finished_at"])
        print(f"{job_id}: done in {time.time() - t0:.0f}s — {len(result['candidates'])} designs", flush=True)
    except Exception as e:
        traceback.print_exc()
        msg = f"{type(e).__name__}: {e}"
        if "api_key" in msg.lower() or "authentication" in msg.lower() or "AuthenticationError" in msg:
            msg = "The worker has no working Anthropic API key. Set ANTHROPIC_API_KEY on the worker's computer and restart it."
        store.update(doc, {"status": "failed", "error": msg[:500]}, server_time=["finished_at"])


def handle_auto(store: Store, orl, calibration, doc: dict) -> None:
    """An automatic design run: search motors, fins, ballast and parachute for the ARC goals."""
    from autodesign import run as autodesign
    from openrocket import Launch
    job = {k: from_value(v) for k, v in doc.get("fields", {}).items()}
    job_id = doc["name"].rsplit("/", 1)[1]
    if not store.update(doc, {"status": "running", "claimed_by": store.s.uid, "stage": "Starting"},
                        server_time=["started_at"], if_unchanged=True):
        return
    print(f"{job_id}: automatic design run", flush=True)
    t0 = time.time()
    try:
        w = job["weather"]
        launch = Launch(elevation_ft=float(w["elevation_ft"]), wind_mph=float(w["wind_mph"]),
                        humidity_pct=float(w["humidity_pct"]), temperature_f=float(w["temperature_f"]))
        p = job.get("payload") or {}
        payload = {"egg_g": float(p.get("egg_g", 59)), "protector_g": float(p.get("protector_g", 15)), "altimeter_g": float(p.get("altimeter_g", 7))}
        if not (40 <= payload["egg_g"] <= 80 and 0 <= payload["protector_g"] <= 80 and 0 <= payload["altimeter_g"] <= 60):
            raise ValueError("egg, protector or altimeter weight is out of range")
        budget = min(600.0, max(45.0, float(job.get("budget_s", 180))))
        result = autodesign(orl, launch, calibration, target_ft=float(job.get("target_apogee_ft", 800)), budget_s=budget, payload=payload,
                            progress=lambda stage: store.update(doc, {"stage": stage[:120]}))
        store.update(doc, {"status": "done", "stage": "Done", "result": result}, server_time=["finished_at"])
        print(f"{job_id}: done in {time.time() - t0:.0f}s — {len(result['designs'])} designs", flush=True)
    except Exception as e:
        traceback.print_exc()
        store.update(doc, {"status": "failed", "error": f"{type(e).__name__}: {e}"[:500]}, server_time=["finished_at"])


def handle(store: Store, orl, calibration, doc: dict) -> None:
    job = {k: from_value(v) for k, v in doc.get("fields", {}).items()}
    job_id = doc["name"].rsplit("/", 1)[1]
    if not store.update(doc, {"status": "running", "claimed_by": store.s.uid}, server_time=["started_at"],
                        if_unchanged=True):
        print(f"{job_id}: taken by another worker")
        return
    print(f"{job_id}: running {job.get('weather')}", flush=True)
    t0 = time.time()
    try:
        result = run_spec(orl, job, calibration) if job.get("spec_json") else run_job(orl, job, calibration)
        store.update(doc, {"status": "done", "result": result}, server_time=["finished_at"])
        print(f"{job_id}: done in {time.time() - t0:.1f}s — {result['apogee_ft']} ft, {result['duration_s']} s", flush=True)
    except Exception as e:  # report every failure to the app, never leave a job "running"
        traceback.print_exc()
        store.update(doc, {"status": "failed", "error": f"{type(e).__name__}: {e}"[:500]},
                     server_time=["finished_at"])
        print(f"{job_id}: failed", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--register", action="store_true", help="create this computer's worker account")
    ap.add_argument("--once", action="store_true", help="handle waiting requests, then stop")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between checks (default 3)")
    args = ap.parse_args()
    cfg = firebase_config()
    if args.register:
        register(cfg)
        return
    if not CONFIG.exists():
        raise SystemExit("Not registered. Run: python sim/worker.py --register")

    session = Session(cfg)
    store = Store(session)
    session.id_token()
    print(f"Worker {session.uid} watching {cfg['projectId']}/{COLLECTION}", flush=True)

    from openrocket import load_calibration, open_openrocket
    calibration = load_calibration(ROOT / "data" / "calibration.json")
    real_stdout = os.dup(1)
    os.dup2(2, 1)  # OpenRocket logs to stdout; keep it out of the worker's log lines
    sys.stdout = os.fdopen(real_stdout, "w", buffering=1)
    with open_openrocket() as orl:
        while True:
            try:
                for doc in store.pending():
                    handle(store, orl, calibration, doc)
                for doc in store.pending(limit=2, collection=CHAT_COLLECTION):
                    handle_chat(store, orl, calibration, doc)
                for doc in store.pending(limit=1, collection=AUTO_COLLECTION):
                    handle_auto(store, orl, calibration, doc)
            except RuntimeError as e:
                print(f"error: {e}", flush=True)
            if args.once:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
