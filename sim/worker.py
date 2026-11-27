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
ROCKETS = {"ederance": ROOT / "rockets" / "ederance.ork"}   # rockets the app may ask for


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

    def pending(self, limit: int = 5) -> list[dict]:
        q = {"structuredQuery": {"from": [{"collectionId": COLLECTION}],
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

    def prepare(rocket):
        if entry is not None:
            mount = motor_mount(rocket)
            length = max(float(mount.getLength()) * 1000, entry["length_mm"] + 5)
            set_mount_diameter(rocket, entry["diameter_mm"], length)
        for cid, mm in lengths.items():
            c = component_by_id(rocket, cid)
            if c is None or str(c.getClass().getSimpleName()) != "BodyTube":
                raise ValueError(f"part {cid} is not a body tube")
            c.setLength(mm / 1000.0)
            applied.setdefault("tube_lengths_mm", {})[str(cid)] = mm
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
    cal = calibration if calibration and calibration.get("rocket") == ROCKETS[rocket].name else None
    changes = job.get("changes")
    extra_args, applied = {}, None
    if changes is not None:
        prepare, motor, delay, listeners, extra, total, applied = design_changes(changes)
        extra_args = dict(prepare=prepare, motor=motor, delay_s=delay, listeners=listeners,
                          extra_mass_g=extra, liftoff_mass_g=total, keep_doc=True)
    result = run_with(orl, ROCKETS[rocket], launch, calibration=cal,
                      target_apogee_ft=float(job.get("target_apogee_ft", 800)), **extra_args)
    p = result["predicted"]
    design_json = None
    if changes is not None:
        from design import export
        flight = {"conditions": w, "calibration_id": result["calibration_id"], "predicted": p,
                  "score": result["score"], "target_apogee_ft": result["target_apogee_ft"]}
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
        result = run_job(orl, job, calibration)
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
            except RuntimeError as e:
                print(f"error: {e}", flush=True)
            if args.once:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
