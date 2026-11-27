"""Start OpenRocket headless and set up a simulation entirely from our inputs.

Two jobs:

1. `open_openrocket()` starts OpenRocket 24.12 through orlab with its parts
   catalogue (component presets) connected. OpenRocket's headless startup does
   not connect it, so any .ork that uses catalogue parts (e.g. a SEMROC BT-80)
   fails to load. We add the one missing binding.

2. `configure()` sets every simulation option OpenRocket has, so nothing is
   inherited from the simulations saved in the .ork file. Those are often stale
   or set up for a different site; this app owns the inputs.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass

import jpype
import orlab
from orlab.core import openrocket_instance as _oi

FT = 0.3048
MPH = 0.44704


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

_original_start = _oi.OpenRocketInstance._start_openrocket


def _start_with_presets(self):
    if self.profile.startup != "core":
        return _original_start(self)
    self.openrocket = _oi._jpackage(self.profile.core_root)
    self.openrocket_swing = _oi._jpackage(self.profile.swing_root)
    dao = jpype.JClass("info.openrocket.core.database.ComponentPresetDao")
    database = jpype.JClass("info.openrocket.core.database.ComponentPresetDatabase")

    @jpype.JImplements("com.google.inject.Module")
    class PresetDaoModule:
        @jpype.JOverride
        def configure(self, binder):
            binder.bind(dao).to(database)

    self._preset_module = PresetDaoModule()  # keep a reference for the JVM's lifetime
    self.openrocket.startup.OpenRocketCore.initialize(self.openrocket.plugin.PluginModule(), self._preset_module)
    self._set_or_log_level()


_oi.OpenRocketInstance._start_openrocket = _start_with_presets


@contextmanager
def open_openrocket(jar: str | None = None):
    """Yield an orlab Helper with OpenRocket running."""
    with orlab.OpenRocketInstance(jar_path=jar, log_level="ERROR") as instance:
        yield orlab.Helper(instance)


# ---------------------------------------------------------------------------
# Atmosphere
# ---------------------------------------------------------------------------

def f_to_kelvin(temp_f: float) -> float:
    return (temp_f - 32.0) * 5.0 / 9.0 + 273.15


def isa_pressure_pa(elevation_m: float) -> float:
    """Standard-atmosphere station pressure at an elevation (troposphere)."""
    return 101325.0 * (1.0 - 2.25577e-5 * elevation_m) ** 5.25588


def virtual_temperature_k(temp_k: float, pressure_pa: float, humidity_pct: float) -> float:
    """Temperature dry air would need to have the same density as this humid air.

    OpenRocket's atmosphere is dry air with no humidity input. Water vapour is
    lighter than air, so humid air is slightly less dense; passing the virtual
    temperature gives OpenRocket the right density.
    """
    temp_c = temp_k - 273.15
    sat_vapour_pa = 610.94 * math.exp(17.625 * temp_c / (temp_c + 243.04))  # Magnus formula
    vapour_pa = humidity_pct / 100.0 * sat_vapour_pa
    return temp_k / (1.0 - (vapour_pa / pressure_pa) * (1.0 - 0.622))


# ---------------------------------------------------------------------------
# Simulation set-up
# ---------------------------------------------------------------------------

@dataclass
class Launch:
    """Everything that defines a flight besides the rocket. Units in names."""
    elevation_ft: float
    wind_mph: float
    humidity_pct: float
    temperature_f: float
    turbulence_pct: float = 10.0       # wind gustiness; OpenRocket's default
    wind_from_deg: float = 0.0         # compass direction the wind blows from
    rail_length_ft: float = 6.0        # the design has rail buttons: a 6 ft rail
    rail_angle_deg: float = 0.0        # tilt from vertical
    launch_into_wind: bool = False     # tilt the rail into the wind
    latitude_deg: float = 38.9         # ARC Finals, Great Meadow, Virginia
    longitude_deg: float = -77.8
    time_step_s: float = 0.01          # RK4 step; see sim/README.md for the convergence check
    max_step_angle_deg: float = 3.0
    max_time_s: float = 1200.0
    seed: int = 42

    def derived(self) -> dict:
        elevation_m = self.elevation_ft * FT
        pressure = isa_pressure_pa(elevation_m)
        temp_k = f_to_kelvin(self.temperature_f)
        return {
            "elevation_m": elevation_m,
            "pressure_pa": pressure,
            "temperature_k": temp_k,
            "density_equivalent_temperature_k": virtual_temperature_k(temp_k, pressure, self.humidity_pct),
            "wind_ms": self.wind_mph * MPH,
        }


def motor_configurations(rocket) -> list:
    """Flight configurations that have at least one motor, in file order."""
    return [fc for fc in rocket.getFlightConfigurations() if fc.getActiveMotors().size() > 0]


def set_liftoff_mass(rocket, config, liftoff_mass_g: float) -> None:
    """Make the rocket weigh `liftoff_mass_g` on the pad, motor included.

    The difference from the design is spread so the structure's centre of gravity
    stays where the design puts it: we know how much the built rocket weighs, not
    where the extra grams are.
    """
    MassCalculator = jpype.JClass("info.openrocket.core.masscalc.MassCalculator")
    structure = MassCalculator.calculateStructure(config)
    motor_kg = MassCalculator.calculateMotor(config).getMass()
    structure_kg = liftoff_mass_g / 1000.0 - motor_kg
    if structure_kg <= 0:
        raise SystemExit(f"Liftoff mass {liftoff_mass_g} g is less than the motor's {motor_kg * 1000:.0f} g")
    stage = rocket.getChild(0)
    stage.setMassOverridden(True)
    stage.setSubcomponentsOverriddenMass(True)
    stage.setOverrideMass(structure_kg)
    stage.setCGOverridden(True)
    stage.setSubcomponentsOverriddenCG(True)
    stage.setOverrideCGX(structure.getCM().x - stage.getAxialOffset())


def configure(orl, doc, launch: Launch, *, motor: str | None = None, manufacturer: str | None = None,
              delay_s: float | None = None, liftoff_mass_g: float | None = None,
              chute_cd_scale: float = 1.0):
    """Return a simulation of `doc`'s rocket with every option set from `launch`.

    Uses the design's first motor configuration unless `motor` is given. With
    `liftoff_mass_g`, the rocket is set to that measured pad weight. `chute_cd_scale`
    multiplies every parachute's drag coefficient (a calibration from real flights).
    """
    rocket = doc.getRocket()
    configs = motor_configurations(rocket)
    if not configs and not motor:
        raise SystemExit("The design has no motor configuration; pass --motor.")

    sim = doc.getSimulation(0)
    if configs:
        sim.setFlightConfigurationId(configs[0].getId())
    if motor:
        orl.set_motor(sim, motor, manufacturer=manufacturer, delay=delay_s)
    elif delay_s is not None:
        raise SystemExit("--delay-s needs --motor")
    if liftoff_mass_g is not None:
        set_liftoff_mass(rocket, rocket.getFlightConfiguration(sim.getFlightConfigurationId()), liftoff_mass_g)
    if chute_cd_scale != 1.0:
        for c in orl.get_all_components(rocket):
            if str(c.getClass().getSimpleName()) == "Parachute":
                cd = float(c.getCD())
                c.setCDAutomatic(False)
                c.setCD(cd * chute_cd_scale)

    d = launch.derived()
    o = sim.getOptions()
    WindModelType = jpype.JClass("info.openrocket.core.models.wind.WindModelType")
    Geodetic = jpype.JClass("info.openrocket.core.util.GeodeticComputationStrategy")

    # Site and atmosphere
    o.setLaunchAltitude(d["elevation_m"])
    o.setLaunchLatitude(launch.latitude_deg)
    o.setLaunchLongitude(launch.longitude_deg)
    o.setGeodeticComputation(Geodetic.SPHERICAL)
    o.setISAAtmosphere(False)                    # use our temperature and pressure
    o.setLaunchPressure(d["pressure_pa"])
    o.setLaunchTemperature(d["density_equivalent_temperature_k"])

    # Wind
    o.setWindModelType(WindModelType.AVERAGE)
    o.setWindSpeedAverage(d["wind_ms"])
    o.setWindTurbulenceIntensity(launch.turbulence_pct / 100.0)
    o.setWindDirection(math.radians(launch.wind_from_deg))

    # Launch rail
    o.setLaunchRodLength(launch.rail_length_ft * FT)
    o.setLaunchRodAngle(math.radians(launch.rail_angle_deg))
    o.setLaunchRodDirection(math.radians(launch.wind_from_deg))
    o.setLaunchIntoWind(launch.launch_into_wind)

    # Integrator
    o.setTimeStep(launch.time_step_s)
    o.setMaximumStepAngle(math.radians(launch.max_step_angle_deg))
    o.setMaxSimulationTime(launch.max_time_s)
    o.setRandomSeed(launch.seed)
    return sim


def load_calibration(path) -> dict | None:
    """Read a calibration file written by validate.py --fit, or None if there is none."""
    import json
    from pathlib import Path
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def describe_rocket(orl, doc, sim) -> dict:
    """Parts, mass and motor of the configuration being flown."""
    rocket = doc.getRocket()
    parts = []
    for c in orl.get_all_components(rocket):
        kind = str(c.getClass().getSimpleName())
        if kind in ("Rocket", "AxialStage"):
            continue
        part = {"type": kind, "name": str(c.getName()), "mass_g": round(float(c.getMass()) * 1000, 1)}
        if c.isMassOverridden():
            part["mass_measured"] = True
        parts.append(part)
    fc = rocket.getFlightConfiguration(sim.getFlightConfigurationId())
    motors = []
    for m in fc.getActiveMotors():
        mot = m.getMotor()
        motors.append({
            "designation": f"{mot.getManufacturer()} {mot.getDesignation()}",
            "delay_s": float(m.getEjectionDelay()),
            "total_impulse_ns": round(float(mot.getTotalImpulseEstimate()), 1),
            "launch_mass_g": round(float(mot.getLaunchMass()) * 1000, 1),
            "diameter_mm": round(float(mot.getDiameter()) * 1000),
        })
    return {"parts": parts, "motors": motors}


# ---------------------------------------------------------------------------
# Design changes used by the motor study
# ---------------------------------------------------------------------------

def motor_mount(rocket):
    """The design's motor-mount tube."""
    for c in _walk(rocket):
        if str(c.getClass().getSimpleName()) == "InnerTube" and c.isMotorMount():
            return c
    raise SystemExit("The design has no motor mount")


def _walk(component):
    yield component
    for child in component.getChildren():
        yield from _walk(child)


def set_mount_diameter(rocket, motor_diameter_mm: float, length_mm: float | None = None,
                       wall_mm: float = 0.8) -> None:
    """Resize the motor-mount tube for a motor diameter (29 mm -> 29.1 mm inside, 30.7 mm outside).

    Centering rings follow: their inner diameter is set to the new tube's outside.
    """
    mount = motor_mount(rocket)
    inner_m = (motor_diameter_mm + 0.1) / 1000
    mount.setOuterRadius(inner_m / 2 + wall_mm / 1000)
    mount.setThickness(wall_mm / 1000)
    if length_mm:
        mount.setLength(length_mm / 1000)
    for c in _walk(rocket):
        if str(c.getClass().getSimpleName()) == "CenteringRing":
            c.setInnerRadiusAutomatic(False)
            c.setInnerRadius(float(mount.getOuterRadius()))


def add_nose_ballast(rocket, grams: float) -> None:
    """Put `grams` of ballast in the nose cone (moves the CG forward, so stability goes up)."""
    MassComponent = jpype.JClass("info.openrocket.core.rocketcomponent.MassComponent")
    nose = next(c for c in _walk(rocket) if str(c.getClass().getSimpleName()) == "NoseCone")
    ballast = next((c for c in nose.getChildren() if str(c.getName()) == "Ballast"), None)
    if ballast is None:
        ballast = MassComponent()
        ballast.setName("Ballast")
        ballast.setLength(0.02)
        ballast.setRadius(0.02)
        nose.addChild(ballast)
        # Sit it at the nose cone's base, where clay or weights usually go.
        ballast.setAxialOffset(float(nose.getLength()) * 0.55)
    ballast.setComponentMass(grams / 1000.0)


def thrust_scale_listener(scale: float):
    """A listener that multiplies thrust by `scale` (match NAR-tested impulse, or vary it)."""
    class ThrustScale(orlab.AbstractSimulationListener):
        def postSimpleThrustCalculation(self, status, thrust):
            return float(thrust) * scale
    return ThrustScale()


def find_motor(designation: str, manufacturer_hint: str, nar_impulse_ns: float, diameter_mm: float):
    """The database motor matching an approved-list entry, closest to NAR's tested impulse."""
    import re
    db = jpype.JClass("info.openrocket.core.startup.Application").getMotorSetDatabase()
    want = designation.upper().replace("/", "")
    best = None
    for ms in db.getMotorSets():
        des = str(ms.getDesignation()).upper().replace("/", "")
        core = re.sub(r"^\d+", "", des).split("-")[0]           # Cesaroni "55F29-12A" -> "F29"
        core = re.sub(r"L$", "", core) if core.endswith("L") and core[:-1] == want else core  # "F20WL" -> "F20W"
        if core != want and des != want:
            continue
        for m in ms.getMotors():
            if abs(float(m.getDiameter()) * 1000 - diameter_mm) > 1:
                continue
            gap = abs(float(m.getTotalImpulseEstimate()) - nar_impulse_ns)
            if best is None or gap < best[0]:
                best = (gap, m)
    return best[1] if best else None
