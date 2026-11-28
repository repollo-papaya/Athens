// Rocket builder: build a rocket part by part on a blank canvas, like OpenRocket.
// The rocket is a list of parts (the same format sim/design.py exports and sim/build.py builds),
// analysed live by builder.js; build-cloud.js sends it to the simulation worker.
(() => {
  "use strict";
  const RB = window.RocketBuilder;
  const SVGNS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d = 1) => (n == null || isNaN(n) ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
  function h(tag, cls, ...kids) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    for (const k of kids) if (k != null) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return e;
  }
  function svgEl(name, attrs, parent) {
    const e = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }
  const button = (label, cls, onClick) => { const b = h("button", cls, label); b.type = "button"; if (onClick) b.addEventListener("click", onClick); return b; };

  // ---------------- units ----------------
  const UNITS = { in: { len: "in", per: 25.4, dec: 2, metric: false }, mm: { len: "mm", per: 1, dec: 1, metric: true },
                  cm: { len: "cm", per: 10, dec: 2, metric: true }, m: { len: "m", per: 1000, dec: 3, metric: true } };
  let units = "in";
  try { units = localStorage.getItem("athens-units") || "in"; } catch {}
  if (!UNITS[units]) units = "in";
  const U = () => UNITS[units];
  const len = (mm, dec) => {
    if (mm == null || isNaN(mm)) return "—";
    const v = mm / U().per;
    return fmt(v, dec ?? (Math.abs(v) < (U().len === "mm" ? 10 : 1) && v !== 0 ? U().dec + 1 : U().dec)) + " " + U().len;
  };
  const toUnits = (mm) => { const v = mm / U().per; return Number(v.toFixed(Math.abs(v) < 1 ? U().dec + 1 : U().dec)); };
  const alt = (ft) => U().metric ? fmt(ft * 0.3048, 0) + " m" : fmt(ft, 0) + " ft";
  const speed = (fps) => U().metric ? fmt(fps * 0.3048, 0) + " m/s" : fmt(fps, 0) + " ft/s";

  const TYPE = { NoseCone: "Nose cone", BodyTube: "Body tube", Transition: "Transition", InnerTube: "Inner tube", TubeCoupler: "Coupler",
    CenteringRing: "Centering ring", Bulkhead: "Bulkhead", EngineBlock: "Engine block", MassComponent: "Mass component", Parachute: "Parachute",
    Streamer: "Streamer", ShockCord: "Shock cord", TrapezoidFinSet: "Trapezoidal fins", EllipticalFinSet: "Elliptical fins",
    FreeformFinSet: "Freeform fins", RailButton: "Rail button", LaunchLug: "Launch lug" };
  const SHAPE = { CONICAL: "Conical", OGIVE: "Ogive", ELLIPSOID: "Ellipsoid", POWER: "Power series", PARABOLIC: "Parabolic", HAACK: "Haack series" };
  const FINISH = { ROUGH: "Rough", UNFINISHED: "Unfinished", NORMAL: "Regular paint", SMOOTH: "Smooth paint", POLISHED: "Polished", MIRROR: "Mirror" };
  const SECTION = { SQUARE: "Square", ROUNDED: "Rounded", AIRFOIL: "Airfoil" };
  const STAGE = new Set(["NoseCone", "BodyTube", "Transition"]);
  const CONTAINER = new Set(["BodyTube", "NoseCone", "Transition", "InnerTube", "TubeCoupler"]);
  const OUTSIDE = new Set(["TrapezoidFinSet", "EllipticalFinSet", "FreeformFinSet", "RailButton", "LaunchLug"]);
  const isFins = (c) => /FinSet$/.test(c.type);
  const TUBES = { "BT-5": [13.82, 0.31], "BT-20": [18.73, 0.41], "BT-50": [24.8, 0.5], "BT-55": [33.7, 0.5], "BT-60": [41.6, 0.5],
                  "BT-70": [56.32, 0.47], "BT-80": [66.04, 0.53], "29 mm mount": [30.7, 0.8] };
  const tubeSize = (od) => (Object.entries(TUBES).find(([, [d]]) => Math.abs(d - od) < 0.6) || [null])[0];

  // ---------------- state ----------------
  let parts = [];                    // the rocket: parents before children
  let motorKey = "F59WT|Cesaroni", delay = 5, liftoff = null, rocketName = "My rocket";
  let menu = [], team = null, teamOrig = null;
  let selected = null, view = null, sim = null, zoom = 1;
  const undoStack = [], redoStack = [];
  let seq = 1;
  const newId = () => { let id; do { id = "p" + seq++; } while (parts.some(p => p.id === id)); return id; };

  const snapshot = () => JSON.stringify({ parts, motorKey, delay, liftoff, rocketName });
  function restore(s) { const o = JSON.parse(s); parts = o.parts; motorKey = o.motorKey; delay = o.delay; liftoff = o.liftoff; rocketName = o.rocketName || rocketName; }
  function save() { try { localStorage.setItem("athens-builder-v2", snapshot()); } catch {} }
  function commit(fn, select) {
    undoStack.push(snapshot()); if (undoStack.length > 80) undoStack.shift();
    redoStack.length = 0;
    fn();
    if (select !== undefined) selected = select;
    if (selected && !byId(selected)) selected = null;
    save(); update();
  }
  $("undo").addEventListener("click", () => { if (!undoStack.length) return; redoStack.push(snapshot()); restore(undoStack.pop()); if (selected && !byId(selected)) selected = null; save(); syncFly(); update(); });
  $("redo").addEventListener("click", () => { if (!redoStack.length) return; undoStack.push(snapshot()); restore(redoStack.pop()); if (selected && !byId(selected)) selected = null; save(); syncFly(); update(); });

  // ---------------- tree helpers ----------------
  const byId = (id) => parts.find(p => p.id === id);
  const stageParts = () => parts.filter(p => p.parent == null);
  function block(id) {   // a part and everything inside or on it, in list order
    const ids = new Set([id]);
    for (const p of parts) if (p.parent != null && ids.has(p.parent)) ids.add(p.id);
    return parts.filter(p => ids.has(p.id));
  }
  function endOfBlock(id) { const b = block(id); return parts.indexOf(b[b.length - 1]) + 1; }
  const stageOf = (p) => { while (p && p.parent != null) p = byId(p.parent); return p; };
  function lastOf(type) { const l = parts.filter(p => p.type === type); return l[l.length - 1]; }
  // Where a new part goes, from what is selected (as in OpenRocket).
  function targetFor(kind) {
    const sel = selected ? byId(selected) : null;
    if (kind === "stage") return { after: sel ? stageOf(sel) : stageParts().slice(-1)[0] || null };
    if (kind === "outside") {
      let t = sel; while (t && t.type !== "BodyTube") t = t.parent != null ? byId(t.parent) : null;
      return { parent: t || lastOf("BodyTube") || null };
    }
    let t = sel; while (t && !CONTAINER.has(t.type)) t = t.parent != null ? byId(t.parent) : null;
    return { parent: t || lastOf("BodyTube") || parts.find(p => p.type === "NoseCone") || null };
  }
  function insertPart(p, where) {
    if (p.parent == null) {
      const at = where.after ? endOfBlock(where.after.id) : (where.first ? 0 : parts.length);
      parts.splice(at, 0, p);
    } else parts.splice(endOfBlock(p.parent), 0, p);
  }
  function removeBlock(id) { const ids = new Set(block(id).map(p => p.id)); parts = parts.filter(p => !ids.has(p.id)); }

  // ---------------- parts catalogue ----------------
  const fromTeam = (id, extra) => { const c = team && team.find(x => x.id === String(id)); return c ? { ...JSON.parse(JSON.stringify(c)), ...extra } : null; };
  const innerR = (p) => RB.innerRadiusOf(p);
  const CATALOG = {
    nose: { label: "Nose Cone", icon: "nose", where: "stage", make: () => ({ type: "NoseCone", name: "Nose cone", shape: "OGIVE", shape_param: 1, length_mm: 130,
      fore_radius_mm: 0, aft_radius_mm: 33.02, aft_shoulder_length_mm: 30, aft_shoulder_radius_mm: 32.4, thickness_mm: 2, filled: false,
      auto_radius: true, material: "Plastic (polystyrene)", finish_key: "NORMAL" }) },
    bodytube: { label: "Body Tube", icon: "tube", where: "stage", make: () => ({ type: "BodyTube", name: "Body tube", length_mm: 300, outer_radius_mm: 33.02,
      thickness_mm: 0.5, auto_radius: true, material: "Paper (spiral tube)", finish_key: "NORMAL" }) },
    transition: { label: "Transition", icon: "transition", where: "stage", make: (w) => { const r = w.after ? (w.after.type === "BodyTube" ? w.after.outer_radius_mm : w.after.aft_radius_mm) : 33.02;
      return { type: "Transition", name: "Transition", shape: "CONICAL", shape_param: 0, length_mm: 50, fore_radius_mm: r, aft_radius_mm: Math.round(r * 0.85 * 100) / 100,
        fore_shoulder_length_mm: 0, aft_shoulder_length_mm: 0, thickness_mm: 1.5, filled: false, auto_radius: true, material: "Plastic (polystyrene)", finish_key: "NORMAL" }; } },
    trapezoid: { label: "Trapezoidal", icon: "fins", where: "outside", make: () => ({ type: "TrapezoidFinSet", name: "Fins", fin_count: 3, root_chord_mm: 70, tip_chord_mm: 30,
      span_mm: 50, sweep_mm: 35, thickness_mm: 3, cross_section_key: "SQUARE", axial_method: "BOTTOM", axial_offset_mm: 0, material: "Balsa", finish_key: "NORMAL" }) },
    elliptical: { label: "Elliptical", icon: "ellipse", where: "outside", make: () => ({ type: "EllipticalFinSet", name: "Elliptical fins", fin_count: 3, root_chord_mm: 70,
      span_mm: 50, thickness_mm: 3, cross_section_key: "ROUNDED", axial_method: "BOTTOM", axial_offset_mm: 0, material: "Balsa", finish_key: "NORMAL" }) },
    railbutton: { label: "Rail Button", icon: "rail", where: "outside", make: () => ({ type: "RailButton", name: "Rail button", outer_diameter_mm: 9.7, inner_diameter_mm: 8.0,
      height_mm: 9.7, base_height_mm: 2, flange_height_mm: 1.5, screw_height_mm: 0, angle_deg: 180, length_mm: 0, mass_g: 2, weighed: true, axial_method: "MIDDLE", axial_offset_mm: 0, finish_key: "NORMAL" }) },
    launchlug: { label: "Launch Lug", icon: "lug", where: "outside", make: () => ({ type: "LaunchLug", name: "Launch lug", length_mm: 50, outer_radius_mm: 2.8, thickness_mm: 0.4,
      angle_deg: 180, axial_method: "MIDDLE", axial_offset_mm: 0, material: "Cardboard", finish_key: "NORMAL" }) },
    innertube: { label: "Inner Tube", icon: "inner", where: "inside", make: () => ({ type: "InnerTube", name: "Motor mount", length_mm: 110, outer_radius_mm: 15.35,
      inner_radius_mm: 14.55, motor_mount: !parts.some(p => p.motor_mount), motor_overhang_mm: 0, axial_method: "BOTTOM", axial_offset_mm: 0, material: "Cardboard" }) },
    coupler: { label: "Coupler", icon: "coupler", where: "inside", make: () => ({ type: "TubeCoupler", name: "Coupler", length_mm: 60, outer_radius_mm: 30, inner_radius_mm: 29.2,
      auto_radius: true, axial_method: "TOP", axial_offset_mm: 0, material: "Cardboard" }) },
    ring: { label: "Centering Ring", icon: "ring", where: "inside", make: () => ({ type: "CenteringRing", name: "Centering ring", length_mm: 3, outer_radius_mm: 30, inner_radius_mm: 15.35,
      auto_radius: true, axial_method: "BOTTOM", axial_offset_mm: -8, material: "Birch plywood" }) },
    bulkhead: { label: "Bulkhead", icon: "bulkhead", where: "inside", make: () => ({ type: "Bulkhead", name: "Bulkhead", length_mm: 6, outer_radius_mm: 30, auto_radius: true,
      axial_method: "TOP", axial_offset_mm: 0, material: "Birch plywood" }) },
    engineblock: { label: "Engine Block", icon: "block", where: "inside", make: (w) => ({ type: "EngineBlock", name: "Engine block", length_mm: 6,
      outer_radius_mm: w.parent ? innerR(w.parent) : 14.5, inner_radius_mm: w.parent ? innerR(w.parent) - 3 : 11.5, axial_method: "TOP", axial_offset_mm: 0, material: "Cardboard" }) },
    parachute: { label: "Parachute", icon: "chute", where: "inside", make: () => ({ type: "Parachute", name: "Parachute", canopy_diameter_mm: 457.2, cd: 0.8, line_count: 6,
      line_length_mm: 457, length_mm: 30, radius_mm: 15, auto_radius: true, material: "Ripstop nylon", weighed: false, axial_method: "TOP", axial_offset_mm: 0 }) },
    shockcord: { label: "Shock Cord", icon: "cord", where: "inside", make: () => ({ type: "ShockCord", name: "Shock cord", cord_length_mm: 1000, length_mm: 40, radius_mm: 12,
      mass_g: 8, weighed: true, auto_radius: true, axial_method: "TOP", axial_offset_mm: 0 }) },
    mass: { label: "Mass Component", icon: "mass", where: "inside", make: () => ({ type: "MassComponent", name: "Mass", mass_g: 10, length_mm: 25, radius_mm: 12.5,
      weighed: true, auto_radius: true, axial_method: "TOP", axial_offset_mm: 0 }) },
    // the team's parts sheet
    my_nose: { label: "ARC foam nose cone", icon: "nose", where: "stage", mine: true, make: () => fromTeam(0, { name: "66 mm ARC foam nose cone", auto_radius: false }) },
    my_bt80: { label: "BT-80 tube", icon: "tube", where: "stage", mine: true, make: () => ({ type: "BodyTube", name: "BT-80 body tube", length_mm: 457.2, outer_radius_mm: 33.02,
      thickness_mm: 0.53, material: "Paper (spiral tube)", finish_key: "NORMAL" }) },
    my_bt70: { label: "BT-70 tube", icon: "tube", where: "stage", mine: true, make: () => ({ type: "BodyTube", name: "BT-70 body tube", length_mm: 457.2, outer_radius_mm: 28.16,
      thickness_mm: 0.47, material: "Paper (spiral tube)", finish_key: "NORMAL" }) },
    my_trans: { label: "BT-80 to BT-70 transition", icon: "transition", where: "stage", mine: true, make: () => fromTeam(11, { name: "Transition, BT-80 to BT-70", auto_radius: false }) },
    my_trans_up: { label: "BT-70 to BT-80 transition", icon: "transup", where: "stage", mine: true, make: () => { const t = fromTeam(11, { name: "Transition, BT-70 to BT-80", auto_radius: false }); if (t) RB.flip(t); return t; } },
    my_avbay: { label: "Altimeter bay", icon: "avbay", where: "inside", mine: true, make: () => [
      { type: "TubeCoupler", name: "Altimeter bay", length_mm: 101.6, outer_radius_mm: 30, inner_radius_mm: 29.2, auto_radius: true, axial_method: "TOP", axial_offset_mm: 0, material: "Cardboard" },
      { type: "Bulkhead", name: "Bay bulkhead (front)", length_mm: 6.4, outer_radius_mm: 29, auto_radius: true, axial_method: "TOP", axial_offset_mm: 0, material: "Birch plywood" },
      { type: "MassComponent", name: "Altimeter (Jolly Logic AltimeterOne)", mass_g: 7, length_mm: 40, radius_mm: 12.5, weighed: true, auto_radius: true, axial_method: "MIDDLE", axial_offset_mm: 0 },
      { type: "Bulkhead", name: "Bay bulkhead (back)", length_mm: 6.4, outer_radius_mm: 29, auto_radius: true, axial_method: "BOTTOM", axial_offset_mm: 0, material: "Birch plywood" },
    ] },
    my_mount: { label: "29 mm motor mount", icon: "inner", where: "inside", mine: true, make: () => ({ type: "InnerTube", name: "29 mm motor mount", length_mm: 110, outer_radius_mm: 15.35,
      inner_radius_mm: 14.55, motor_mount: true, motor_overhang_mm: 0, axial_method: "BOTTOM", axial_offset_mm: 0, material: "Cardboard" }) },
    my_bt5: { label: "BT-5 mount (13 mm)", icon: "inner", where: "inside", mine: true, make: () => ({ type: "InnerTube", name: "BT-5 motor mount", length_mm: 70, outer_radius_mm: 6.91,
      inner_radius_mm: 6.6, motor_mount: !parts.some(p => p.motor_mount), motor_overhang_mm: 0, axial_method: "BOTTOM", axial_offset_mm: 0, material: "Paper (spiral tube)" }) },
    my_fins: { label: "Balsa fins", icon: "fins", where: "outside", mine: true, make: () => ({ type: "TrapezoidFinSet", name: "Balsa fins", fin_count: 3, root_chord_mm: 90, tip_chord_mm: 40,
      span_mm: 50, sweep_mm: 40, thickness_mm: 2.4, cross_section_key: "ROUNDED", axial_method: "BOTTOM", axial_offset_mm: -5, material: "Balsa", finish_key: "NORMAL" }) },
    my_rail: { label: "1010 rail button", icon: "rail", where: "outside", mine: true, make: () => fromTeam(7, { name: "1010 rail button", axial_method: "MIDDLE", axial_offset_mm: 0 }) },
    my_egg: { label: "Large egg", icon: "egg", where: "inside", mine: true, make: () => ({ type: "MassComponent", name: "Large egg", mass_g: 59, length_mm: 57, radius_mm: 22,
      weighed: true, axial_method: "TOP", axial_offset_mm: 0 }) },
    my_protector: { label: "Egg protector", icon: "protector", where: "inside", mine: true, make: () => ({ type: "MassComponent", name: "Egg protector (ARC foam)", mass_g: 15, length_mm: 76,
      radius_mm: 32, auto_radius: true, weighed: true, axial_method: "TOP", axial_offset_mm: 0 }) },
    my_alt: { label: "Altimeter", icon: "alt", where: "inside", mine: true, make: () => ({ type: "MassComponent", name: "Altimeter (Jolly Logic AltimeterOne)", mass_g: 7, length_mm: 40,
      radius_mm: 12.5, weighed: true, axial_method: "TOP", axial_offset_mm: 0 }) },
    my_ballast: { label: "Nose ballast", icon: "ballast", where: "nose", mine: true, make: () => ({ type: "MassComponent", name: "Nose ballast", mass_g: 20, length_mm: 20, radius_mm: 20,
      weighed: true, auto_radius: true, axial_method: "TOP", axial_offset_mm: 0 }) },
    my_chute18: { label: "18 in nylon chute", icon: "chute", where: "inside", mine: true, make: () => fromTeam(18, { name: "Parachute, 18 in nylon", axial_method: "TOP", axial_offset_mm: 0 }) },
    my_chute21: { label: "21 in plastic chute", icon: "chute", where: "inside", mine: true, make: () => fromTeam(13, { name: "Parachute, 21 in plastic", axial_method: "TOP", axial_offset_mm: 0 }) },
  };
  const PALETTE = [
    ["Your parts", ["my_nose", "my_bt80", "my_trans", "my_trans_up", "my_bt70", "my_mount", "my_fins", "my_rail", "my_egg", "my_protector", "my_alt", "my_avbay", "my_chute18", "my_chute21", "my_ballast", "my_bt5"]],
    ["Body and fins", ["nose", "bodytube", "transition", "trapezoid", "elliptical", "railbutton", "launchlug"]],
    ["Inner", ["innertube", "coupler", "ring", "bulkhead", "engineblock"]],
    ["Mass", ["parachute", "shockcord", "mass"]],
  ];
  let group = 0;
  try { group = Number(localStorage.getItem("athens-palette-group")) || 0; } catch {}
  const ICON = {
    nose: '<path d="M2 10 C 10 2, 22 3, 32 4 L 32 16 C 22 17, 10 18, 2 10 Z"/><path d="M32 6 L 36 6 L 36 14 L 32 14"/>',
    tube: '<rect x="2" y="4" width="34" height="12" rx="1"/><path d="M6 6 L 32 14" stroke-opacity=".35"/>',
    transition: '<path d="M2 3 L 12 3 L 30 6 L 36 6 L 36 14 L 30 14 L 12 17 L 2 17 Z"/><path d="M12 3 L 12 17 M30 6 L 30 14"/>',
    fins: '<path d="M2 17 L 34 17 M10 17 L 18 5 L 30 5 L 34 17"/>',
    ellipse: '<path d="M2 17 L 34 17 M6 17 A 12 12 0 0 1 30 17"/>',
    rail: '<path d="M2 17 L 34 17"/><rect x="13" y="12" width="10" height="5"/><rect x="11" y="6" width="14" height="4" rx="1"/><path d="M18 10 L 18 12"/>',
    lug: '<path d="M2 17 L 34 17"/><rect x="9" y="11" width="18" height="6" rx="3"/>',
    inner: '<rect x="2" y="3" width="34" height="14" rx="1" stroke-opacity=".35"/><rect x="10" y="7" width="26" height="6"/>',
    coupler: '<rect x="2" y="4" width="15" height="12" stroke-opacity=".35"/><rect x="19" y="4" width="15" height="12" stroke-opacity=".35"/><rect x="10" y="6" width="16" height="8"/>',
    ring: '<rect x="16" y="2" width="5" height="5"/><rect x="16" y="13" width="5" height="5"/><rect x="2" y="7" width="34" height="6" stroke-opacity=".35"/>',
    bulkhead: '<rect x="2" y="2" width="34" height="16" stroke-opacity=".35"/><rect x="16" y="2" width="5" height="16"/>',
    block: '<rect x="2" y="5" width="34" height="10" stroke-opacity=".35"/><rect x="8" y="5" width="4" height="3"/><rect x="8" y="12" width="4" height="3"/>',
    chute: '<path d="M4 11 A 14 9 0 0 1 32 11 M4 11 L 18 19 L 32 11 M 18 2 L 18 19"/>',
    cord: '<path d="M2 10 C 8 2, 12 18, 18 10 S 28 2, 34 10"/>',
    mass: '<rect x="7" y="4" width="22" height="12" rx="4"/><path d="M14 10 L 22 10"/>',
    egg: '<ellipse cx="18" cy="10" rx="11" ry="7.5"/>',
    protector: '<rect x="4" y="3" width="28" height="14" rx="3"/><ellipse cx="18" cy="10" rx="8" ry="5"/>',
    alt: '<rect x="9" y="4" width="18" height="12" rx="2"/><path d="M13 13 L 16 8 L 19 11 L 23 6"/>',
    transup: '<path d="M2 6 L 8 6 L 26 3 L 36 3 L 36 17 L 26 17 L 8 14 L 2 14 Z"/><path d="M8 6 L 8 14 M26 3 L 26 17"/>',
    avbay: '<rect x="2" y="3" width="34" height="14" stroke-opacity=".35"/><rect x="8" y="5" width="20" height="10"/><path d="M8 5 L 8 15 M28 5 L 28 15"/><path d="M13 12 L 16 8 L 19 11 L 23 7"/>',
    ballast: '<path d="M2 10 C 10 2, 22 3, 34 4 L 34 16 C 22 17, 10 18, 2 10 Z"/><circle cx="20" cy="10" r="3.5" fill="currentColor"/>',
  };

  function canAdd(key) {
    const def = CATALOG[key];
    if (def.where === "stage") return true;
    if (def.where === "nose") return parts.some(p => p.type === "NoseCone");
    const t = targetFor(def.where);
    if (!t.parent) return false;
    if (key === "engineblock") return t.parent.type === "InnerTube";
    return true;
  }
  function addPart(key) {
    const def = CATALOG[key];
    let where = def.where === "nose" ? { parent: parts.find(p => p.type === "NoseCone") } : targetFor(def.where);
    const made = def.make(where);
    if (!made) return;
    const group = Array.isArray(made) ? made : [made];
    const p = group[0];
    p.id = newId();
    if (def.where === "stage") { p.parent = null; p.axial_method = "AFTER"; p.axial_offset_mm = 0; }
    else {
      p.parent = where.parent.id;
      const par = where.parent;
      if (def.where === "nose") p.axial_offset_mm = Math.round(par.length_mm * 0.55);
      // inside parts: first free spot from the top of the tube
      if (def.where === "inside" && p.axial_method === "TOP" && par.type !== "NoseCone") {
        const used = parts.filter(x => x.parent === par.id && !OUTSIDE.has(x.type)).map(x => {
          const s = view && view.components.find(v => v.id === x.id); return s ? [s.offset_in_parent_mm, s.offset_in_parent_mm + s.length_mm] : null; })
          .filter(Boolean).sort((a, b) => a[0] - b[0]);
        let at = 0;
        for (const [s, e] of used) { if (s - at >= p.length_mm) break; at = Math.max(at, e); }
        p.axial_offset_mm = Math.max(0, Math.min(at, par.length_mm - p.length_mm));
      }
    }
    for (const q of group) { if (q.weighed == null) q.weighed = !!q.mass_measured; delete q.mass_measured; }
    commit(() => {
      insertPart(p, where);
      for (const q of group.slice(1)) { q.id = newId(); q.parent = p.id; parts.splice(endOfBlock(p.id), 0, q); }
    }, p.id);
  }

  function buildPalette() {
    const chips = h("div", "cats"); chips.setAttribute("role", "group"); chips.setAttribute("aria-label", "Component group");
    PALETTE.forEach(([name], i) => {
      const b = button(name, "cat", () => { group = i; try { localStorage.setItem("athens-palette-group", i); } catch {} buildPalette(); updatePalette(); });
      b.setAttribute("aria-pressed", String(i === group)); chips.append(b);
    });
    $("palette").replaceChildren(chips, ...PALETTE.filter((_, i) => i === group).map(([, keys]) => {
      const row = h("div", "row");
      for (const key of keys) {
        const def = CATALOG[key];
        const b = button(null, "comp" + (def.mine ? " mine" : ""), () => addPart(key));
        b.dataset.key = key;
        b.innerHTML = `<svg viewBox="0 0 36 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true">${ICON[def.icon]}</svg>`;
        b.append(def.label);
        row.append(b);
      }
      return h("div", "group", row);
    }));
  }
  function updatePalette() {
    document.querySelectorAll("#palette .comp").forEach(b => { b.disabled = !canAdd(b.dataset.key); });
    const ins = targetFor("inside").parent, after = targetFor("stage").after;
    const t = $("target");
    t.replaceChildren();
    if (!parts.length) { t.append("Start with a nose cone, then body tubes."); return; }
    t.append("Airframe parts go after ", h("b", "", after ? after.name : "the end"), ins ? "; inside parts go in " : "", ins ? h("b", "", ins.name) : "", ". Tap a part to change where.");
  }

  // ---------------- motor ----------------
  const motorEntry = () => menu.find(m => m.key === motorKey) || null;

  // ---------------- analysis ----------------
  function specObject() {
    const strip = new Set(["x_mm", "cg_x_mm", "offset_in_parent_mm", "label", "density", "body_radius_mm", "depth", "movable", "internal", "calculated_mass_g", "catalogue_part"]);
    const comps = (view ? view.components : parts).map(c => {
      const o = {};
      for (const [k, v] of Object.entries(c)) {
        if (strip.has(k) || v == null) continue;
        if (k === "points_mm" && c.type !== "FreeformFinSet") continue;
        o[k] = typeof v === "number" ? Math.round(v * 1000) / 1000 : v;
      }
      return o;
    });
    return { components: comps, motor_key: motorKey, delay_s: delay, ...(liftoff ? { liftoff_mass_g: liftoff } : {}) };
  }
  const specJson = () => JSON.stringify(specObject());

  function analyse() {
    view = RB.analyze({ components: parts, motor: motorEntry(), delay_s: delay, liftoff_mass_g: liftoff });
    const sig = specJson();
    view.simulated = !!(sim && sim.sig === sig);
    if (view.simulated) { view.flight = sim.flight; view.or = sim.summary; }
  }
  const S = () => (view.simulated && view.or) ? view.or : view.summary;

  // ---------------- drawing ----------------
  function draw() {
    const svg = $("svg");
    svg.replaceChildren();
    const comps = view.components;
    $("empty").hidden = comps.length > 0;
    if (!comps.length) { svg.setAttribute("width", 0); svg.setAttribute("height", 0); return; }
    const sum = view.summary;
    const fins = comps.filter(isFins);
    const maxR = Math.max(10, sum.max_diameter_mm / 2);
    const finReach = Math.max(0, ...fins.map(f => Math.max(...f.points_mm.map(p => p[1])) + (f.body_radius_mm || 0)));
    const halfH = Math.max(maxR, finReach) + 16;
    const length = Math.max(sum.length_mm, 100);
    const padX = 16, rulerH = 26;
    const vbW = length + 2 * padX, vbH = 2 * halfH + rulerH + 6;
    // Lying down (nose left) fills a wide screen; standing up (nose at the top) fills a phone held upright.
    const box = $("canvas").clientWidth || 360;
    let wpx, hpx, px;
    if (vertical) {
      const tall = Math.max(420, Math.round(innerHeight * 0.72));
      px = Math.max(vbH / box, vbW / tall) / zoom;               // mm per screen pixel
      wpx = Math.max(box, Math.round(vbH / px)); hpx = Math.round(vbW / px);
      const extra = (wpx * px - vbH) / 2;
      svg.setAttribute("viewBox", `${-(vbH - halfH - rulerH) - extra} ${-padX} ${wpx * px} ${vbW}`);
    } else {
      wpx = Math.round(box * zoom);
      hpx = Math.max(120, Math.round(wpx * vbH / vbW) + 20);
      px = Math.max(vbW / wpx, vbH / hpx);
      svg.setAttribute("viewBox", `${-padX} ${-halfH - rulerH} ${vbW} ${vbH}`);
    }
    svg.setAttribute("width", wpx);
    svg.setAttribute("height", hpx);
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const sw = (n) => (n * px).toFixed(3);
    state.mmPerPx = px;
    const root = svgEl("g", vertical ? { transform: "rotate(90)" } : {}, svg);
    $("canvas").classList.toggle("vertical", vertical);

    const ruler = svgEl("g", { "aria-hidden": "true" }, root);
    const ry = -halfH - 8;
    svgEl("line", { x1: 0, y1: ry, x2: length, y2: ry, stroke: "var(--muted)", "stroke-width": sw(1) }, ruler);
    const step = U().metric ? 10 : 25.4;
    const every = U().metric ? (zoom >= 4 ? 1 : zoom >= 2 ? 5 : 10) : (zoom >= 4 ? 1 : zoom >= 2 ? 2 : 5);
    for (let i = 0; i * step <= length + 0.1; i++) {
      const x = i * step;
      svgEl("line", { x1: x, y1: ry, x2: x, y2: ry + (i % 5 ? 3 : 6) * px * 1.4, stroke: "var(--muted)", "stroke-width": sw(1) }, ruler);
      if (i % every === 0) { const t = svgEl("text", { x, y: ry - 4 * px, "font-size": (11 * px).toFixed(2), "text-anchor": vertical ? "start" : i ? "middle" : "start",
          transform: vertical ? `rotate(-90 ${x} ${ry - 4 * px})` : null, "dominant-baseline": vertical ? "middle" : null }, ruler);
        t.textContent = i ? fmt(x / U().per, U().len === "m" ? 2 : 0) : `0 ${U().len}`; }
    }
    const lay = { body: svgEl("g", {}, root), inner: svgEl("g", {}, root), fins: svgEl("g", {}, root), top: svgEl("g", {}, root) };
    const tag = (n, c, fillable) => { n.dataset.id = c.id; if (fillable) n.classList.add("fillable"); return n; };
    const parentOf = (c) => comps.find(p => p.id === c.parent);
    for (const f of fins) {
      for (let k = 0; k < f.fin_count; k++) {
        const proj = Math.cos(((f.base_rotation_deg || 0) + k * 360 / f.fin_count) * Math.PI / 180);
        if (Math.abs(proj) < 0.02) continue;
        const pts = f.points_mm.map(([x, y]) => `${(f.x_mm + x).toFixed(2)},${(-((f.body_radius_mm || 0) + y) * proj).toFixed(2)}`).join(" ");
        tag(svgEl("polygon", { points: pts, fill: "var(--fin-fill)", stroke: "var(--airframe)", "stroke-width": sw(1.3) }, lay.fins), f, true);
      }
    }
    for (const c of comps) {
      const t = c.type;
      if (t === "NoseCone" || t === "Transition") {
        const n = 48, top = [], bot = [];
        for (let i = 0; i <= n; i++) { const x = c.length_mm * i / n, r = RB.profileRadius(c, x);
          top.push(`${(c.x_mm + x).toFixed(2)},${(-r).toFixed(2)}`); bot.unshift(`${(c.x_mm + x).toFixed(2)},${r.toFixed(2)}`); }
        tag(svgEl("polygon", { points: top.concat(bot).join(" "), fill: "transparent", stroke: "var(--airframe)", "stroke-width": sw(1.6) }, lay.body), c, true);
        if (c.aft_shoulder_length_mm > 0) tag(svgEl("rect", { x: c.x_mm + c.length_mm, y: -c.aft_shoulder_radius_mm, width: c.aft_shoulder_length_mm,
          height: 2 * c.aft_shoulder_radius_mm, fill: "none", stroke: "var(--shoulder)", "stroke-width": sw(1) }, lay.inner), c);
        if (c.fore_shoulder_length_mm > 0) tag(svgEl("rect", { x: c.x_mm - c.fore_shoulder_length_mm, y: -c.fore_shoulder_radius_mm, width: c.fore_shoulder_length_mm,
          height: 2 * c.fore_shoulder_radius_mm, fill: "none", stroke: "var(--shoulder)", "stroke-width": sw(1) }, lay.inner), c);
      } else if (t === "BodyTube") {
        tag(svgEl("rect", { x: c.x_mm, y: -c.outer_radius_mm, width: c.length_mm, height: 2 * c.outer_radius_mm, fill: "transparent", stroke: "var(--airframe)", "stroke-width": sw(1.6) }, lay.body), c, true);
      } else if (["InnerTube", "TubeCoupler", "Bulkhead", "EngineBlock"].includes(t)) {
        tag(svgEl("rect", { x: c.x_mm, y: -c.outer_radius_mm, width: Math.max(c.length_mm, 1), height: 2 * c.outer_radius_mm, fill: "transparent", stroke: "var(--internal)", "stroke-width": sw(1.1) }, lay.inner), c, true);
      } else if (t === "CenteringRing") {
        for (const s of [-1, 1]) tag(svgEl("rect", { x: c.x_mm, y: s < 0 ? -c.outer_radius_mm : c.inner_radius_mm, width: Math.max(c.length_mm, 1),
          height: Math.max(0.5, c.outer_radius_mm - c.inner_radius_mm), fill: "transparent", stroke: "var(--internal)", "stroke-width": sw(1.1) }, lay.inner), c, true);
      } else if (t === "MassComponent" || t === "ShockCord" || t === "Streamer") {
        const r = c.radius_mm;
        tag(svgEl("rect", { x: c.x_mm, y: -r, width: c.length_mm, height: 2 * r, rx: Math.min(r, c.length_mm) * 0.45, fill: "transparent", stroke: "var(--internal)",
          "stroke-width": sw(1.1), "stroke-dasharray": `${sw(4)} ${sw(3)}` }, lay.inner), c, true);
      } else if (t === "Parachute") {
        const r = c.radius_mm, cx = c.x_mm + c.length_mm / 2;
        const g = tag(svgEl("g", {}, lay.inner), c);
        svgEl("rect", { x: c.x_mm, y: -r, width: c.length_mm, height: 2 * r, rx: r * 0.45, fill: "transparent", stroke: "var(--chute)",
          "stroke-width": sw(1.1), "stroke-dasharray": `${sw(4)} ${sw(3)}`, class: "fillable" }, g);
        const s = Math.min(r, c.length_mm / 2) * 0.7;
        svgEl("path", { d: `M ${cx - s} ${-s * 0.15} A ${s} ${s * 0.8} 0 0 1 ${cx + s} ${-s * 0.15} M ${cx - s} ${-s * 0.15} L ${cx} ${s} L ${cx + s} ${-s * 0.15} M ${cx} ${-s * 0.9} L ${cx} ${s}`,
          fill: "none", stroke: "var(--chute)", "stroke-width": sw(1) }, g);
      } else if (t === "RailButton" || t === "LaunchLug") {
        const p = parentOf(c) || {}, R = p.outer_radius_mm || maxR, dir = Math.cos((c.angle_deg || 0) * Math.PI / 180) >= 0 ? -1 : 1;
        const hh = t === "RailButton" ? (c.height_mm || 9.7) : 2 * c.outer_radius_mm, w = t === "RailButton" ? (c.outer_diameter_mm || 9.7) : c.length_mm;
        tag(svgEl("rect", { x: t === "RailButton" ? c.x_mm - w / 2 : c.x_mm, y: dir < 0 ? -R - hh : R, width: w, height: hh, fill: "transparent",
          stroke: "var(--airframe)", "stroke-width": sw(1.2) }, lay.body), c, true);
      }
    }
    for (const m of view.motors) {
      const r = svgEl("rect", { x: m.x_mm, y: -m.diameter_mm / 2, width: m.length_mm, height: m.diameter_mm, fill: m.fits ? "var(--motor)" : "var(--bad)",
        "fill-opacity": 0.8, stroke: "var(--internal)", "stroke-width": sw(0.8) }, lay.inner);
      r.dataset.id = m.mount_id; r.dataset.motor = "1";
    }
    const s = S();
    if ($("show-cgcp").checked && s.cp_mm != null) {
      const r = 7 * px;
      const cg = svgEl("g", { "aria-label": "Centre of gravity", "pointer-events": "none" }, lay.top);
      svgEl("circle", { cx: s.cg_mm, cy: 0, r, fill: "#fff", stroke: "#1f3fbf", "stroke-width": sw(1.4) }, cg);
      svgEl("path", { d: `M ${s.cg_mm} 0 L ${s.cg_mm} ${-r} A ${r} ${r} 0 0 1 ${s.cg_mm + r} 0 Z M ${s.cg_mm} 0 L ${s.cg_mm} ${r} A ${r} ${r} 0 0 1 ${s.cg_mm - r} 0 Z`, fill: "#1f3fbf" }, cg);
      const cp = svgEl("g", { "aria-label": "Centre of pressure", "pointer-events": "none" }, lay.top);
      svgEl("circle", { cx: s.cp_mm, cy: 0, r, fill: "#fff", stroke: "#d23b3b", "stroke-width": sw(1.4) }, cp);
      svgEl("circle", { cx: s.cp_mm, cy: 0, r: r * 0.62, fill: "#d23b3b" }, cp);
    }
    if (selected) document.querySelectorAll(`#svg [data-id="${selected}"]`).forEach(n => {
      n.classList.add("hl"); n.querySelectorAll?.(".fillable, rect, path").forEach(k => k.classList.add("hl")); });
  }
  const state = { mmPerPx: 1 };
  let vertical = false;
  try { vertical = localStorage.getItem("athens-standing") === "1"; } catch {}

  // ---------------- drag, pan, tap ----------------
  let ptr = null;
  $("svg").addEventListener("pointerdown", (e) => {
    const t = e.target.closest("[data-id]");
    ptr = { x0: vertical ? e.clientY : e.clientX, id: t ? t.dataset.id : null, moved: false, scroll0: vertical ? $("canvas").scrollTop : $("canvas").scrollLeft };
    try { $("svg").setPointerCapture(e.pointerId); } catch {}
  });
  $("svg").addEventListener("pointermove", (e) => {
    if (!ptr) return;
    const dxPx = (vertical ? e.clientY : e.clientX) - ptr.x0;
    if (!ptr.moved && Math.abs(dxPx) < 6) return;
    ptr.moved = true;
    if (!ptr.id) { $("canvas")[vertical ? "scrollTop" : "scrollLeft"] = ptr.scroll0 - dxPx; $("canvas").classList.add("panning"); return; }
    if (!ptr.nodes) {
      const ids = new Set(block(ptr.id).map(p => p.id));
      ptr.nodes = [...document.querySelectorAll("#svg [data-id]")].filter(n => ids.has(n.dataset.id));
      if (selected !== ptr.id) { selected = ptr.id; }
    }
    ptr.dx = dxPx * state.mmPerPx;
    ptr.nodes.forEach(n => n.setAttribute("transform", `translate(${ptr.dx.toFixed(2)} 0)`));
  });
  const endPtr = () => {
    if (!ptr) return;
    const p = ptr; ptr = null;
    $("canvas").classList.remove("panning");
    if (!p.moved) { selected = p.id || null; update(); if (p.id) showEditor(); return; }
    if (p.id && p.dx != null) dropPart(p.id, p.dx);
  };
  $("svg").addEventListener("pointerup", endPtr);
  $("svg").addEventListener("pointercancel", () => { ptr = null; update(); });
  $("canvas").addEventListener("click", (e) => { if (e.target === $("canvas")) { selected = null; update(); } });

  function dropPart(id, dx) {
    const c = view.components.find(v => v.id === id);
    if (!c) return update();
    if (c.parent == null) {   // airframe part: change its place in the order
      const center = c.x_mm + c.length_mm / 2 + dx;
      const others = view.components.filter(v => v.parent == null && v.id !== id);
      const index = others.filter(o => o.x_mm + o.length_mm / 2 < center).length;
      commit(() => {
        const moving = block(id); removeBlock(id);
        const stage = stageParts().filter(s => s.id !== id);
        const at = index < stage.length ? parts.indexOf(stage[index]) : parts.length;
        parts.splice(at, 0, ...moving);
      }, id);
      return;
    }
    // inside or on a tube: slide along it, or drop it into another one
    const newX = c.x_mm + dx, center = newX + c.length_mm / 2;
    const own = new Set(block(id).map(p => p.id));
    const fits = (p) => !own.has(p.id) && center >= p.x_mm && center <= p.x_mm + p.length_mm;
    const cur = view.components.find(v => v.id === c.parent);
    let target = cur && fits(cur) ? cur : null;
    if (!target) {
      const okType = OUTSIDE.has(c.type) ? (p) => p.type === "BodyTube" : (p) => CONTAINER.has(p.type) && p.parent == null;
      target = view.components.find(p => okType(p) && fits(p)) || cur;
    }
    const lo = c.type === "TubeCoupler" ? -c.length_mm : 0;
    const off = Math.round(Math.max(lo, Math.min(newX - target.x_mm, Math.max(0, target.length_mm - (c.type === "TubeCoupler" ? 0 : c.length_mm)))) * 10) / 10;
    commit(() => {
      const part = byId(id);
      if (target.id !== part.parent) {
        const moving = block(id); removeBlock(id);
        part.parent = target.id;
        parts.splice(endOfBlock(target.id), 0, ...moving);
      }
      part.axial_method = "TOP"; part.axial_offset_mm = off;
    }, id);
  }

  // ---------------- editor ----------------
  function showEditor() { const card = $("editor-card"); if (!card.hidden && innerWidth < 700) card.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  const mat = (c) => c.material || RB.DEFAULT_MATERIAL[c.type];

  function fieldsFor(c, v) {
    const F = [];
    const L = (key, label, min, max, extra = {}) => F.push({ key, label, kind: "len", min, max, ...extra });
    const D = (key, label, min, max, extra = {}) => F.push({ key, label, kind: "dia", min, max, ...extra });
    const N = (key, label, min, max, extra = {}) => F.push({ key, label, kind: "num", min, max, ...extra });
    const S = (key, label, options, extra = {}) => F.push({ key, label, kind: "select", options, ...extra });
    const C = (key, label, extra = {}) => F.push({ key, label, kind: "check", ...extra });
    F.push({ key: "name", label: "Name", kind: "text", wide: true });
    if (c.parent != null) {
      const own = new Set(block(c.id).map(p => p.id));
      const opts = parts.filter(p => !own.has(p.id) && (OUTSIDE.has(c.type) ? p.type === "BodyTube" : CONTAINER.has(p.type))).map(p => [p.id, p.name]);
      S("parent", OUTSIDE.has(c.type) ? "On" : "Inside", opts);
      L("offset", "From the top of it", c.type === "TubeCoupler" ? -400 : -50, 2000, { value: v.offset_in_parent_mm });
    }
    switch (c.type) {
      case "NoseCone":
        { const end = c.flipped ? "fore" : "aft";
        S("shape", "Shape", Object.entries(SHAPE)); L("length_mm", "Length", 5, 1000);
        D(`${end}_radius_mm`, "Base diameter", 5, 300, { auto: true });
        if (["OGIVE", "POWER", "PARABOLIC", "HAACK"].includes(c.shape)) N("shape_param", "Shape parameter", 0, c.shape === "HAACK" ? 0.333 : 1, { step: 0.05 });
        L(`${end}_shoulder_length_mm`, "Shoulder length", 0, 300); D(`${end}_shoulder_radius_mm`, "Shoulder diameter", 0, 300); }
        C("filled", "Solid"); if (!c.filled) L("thickness_mm", "Wall thickness", 0.2, 20);
        break;
      case "BodyTube":
        S("size", "Tube size", [["", "Custom"], ...Object.keys(TUBES).filter(k => !k.includes("mount")).map(k => [k, k])], { value: tubeSize(2 * c.outer_radius_mm) || "" });
        L("length_mm", "Length", 10, 2000); D("outer_radius_mm", "Outer diameter", 5, 300, { auto: true }); L("thickness_mm", "Wall thickness", 0.1, 20);
        break;
      case "Transition":
        S("shape", "Shape", Object.entries(SHAPE)); L("length_mm", "Length", 5, 600);
        D("fore_radius_mm", "Fore diameter", 0, 300, { auto: true }); D("aft_radius_mm", "Aft diameter", 0, 300, { auto: true });
        L("fore_shoulder_length_mm", "Fore shoulder length", 0, 300); D("fore_shoulder_radius_mm", "Fore shoulder diameter", 0, 300);
        L("aft_shoulder_length_mm", "Aft shoulder length", 0, 300); D("aft_shoulder_radius_mm", "Aft shoulder diameter", 0, 300);
        C("filled", "Solid"); if (!c.filled) L("thickness_mm", "Wall thickness", 0.2, 20);
        break;
      case "TrapezoidFinSet":
        N("fin_count", "Number of fins", 1, 8, { int: true }); L("root_chord_mm", "Root chord", 5, 600); L("tip_chord_mm", "Tip chord", 0, 600);
        L("span_mm", "Height", 5, 400); L("sweep_mm", "Sweep length", -300, 600); L("thickness_mm", "Thickness", 0.3, 20);
        S("cross_section_key", "Cross section", Object.entries(SECTION)); N("cant_deg", "Cant angle", -15, 15, { unit: "°" });
        break;
      case "EllipticalFinSet":
        N("fin_count", "Number of fins", 1, 8, { int: true }); L("root_chord_mm", "Root chord", 5, 600); L("span_mm", "Height", 5, 400);
        L("thickness_mm", "Thickness", 0.3, 20); S("cross_section_key", "Cross section", Object.entries(SECTION));
        break;
      case "FreeformFinSet":
        N("fin_count", "Number of fins", 1, 8, { int: true }); L("thickness_mm", "Thickness", 0.3, 20);
        break;
      case "RailButton": D("outer_diameter_mm", "Outer diameter", 2, 40, { radius: false }); L("height_mm", "Height", 2, 40); N("angle_deg", "Angle around", -180, 180, { unit: "°" }); break;
      case "LaunchLug": L("length_mm", "Length", 5, 300); D("outer_radius_mm", "Outer diameter", 1, 30); L("thickness_mm", "Wall", 0.1, 5); N("angle_deg", "Angle around", -180, 180, { unit: "°" }); break;
      case "InnerTube": L("length_mm", "Length", 5, 1000); D("outer_radius_mm", "Outer diameter", 2, 300); D("inner_radius_mm", "Inner diameter", 1, 300);
        C("motor_mount", "Motor mount"); if (c.motor_mount) L("motor_overhang_mm", "Motor sticks out", -100, 100); break;
      case "TubeCoupler": L("length_mm", "Length", 5, 500); D("outer_radius_mm", "Outer diameter", 2, 300, { auto: true }); D("inner_radius_mm", "Inner diameter", 1, 300); break;
      case "CenteringRing": L("length_mm", "Thickness", 0.5, 50); D("outer_radius_mm", "Outer diameter", 2, 300, { auto: true }); D("inner_radius_mm", "Inner diameter", 0, 300); break;
      case "Bulkhead": L("length_mm", "Thickness", 0.5, 50); D("outer_radius_mm", "Diameter", 2, 300, { auto: true }); break;
      case "EngineBlock": L("length_mm", "Length", 1, 50); D("outer_radius_mm", "Outer diameter", 2, 300); D("inner_radius_mm", "Inner diameter", 0, 300); break;
      case "Parachute": L("canopy_diameter_mm", "Canopy diameter", 100, 2500, { dec: U().metric ? undefined : 1 }); N("cd", "Drag coefficient (Cd)", 0.3, 2.5, { step: 0.05 });
        N("line_count", "Shroud lines", 0, 24, { int: true }); L("line_length_mm", "Line length", 0, 2000);
        L("length_mm", "Packed length", 5, 300); D("radius_mm", "Packed diameter", 2, 300, { auto: true }); break;
      case "ShockCord": L("cord_length_mm", "Cord length", 100, 10000); L("length_mm", "Packed length", 5, 300); D("radius_mm", "Packed diameter", 2, 300, { auto: true }); break;
      case "MassComponent": case "Streamer": L("length_mm", "Length", 1, 500); D("radius_mm", "Diameter", 1, 300, { auto: true }); break;
    }
    if (STAGE.has(c.type) || isFins(c) || c.type === "LaunchLug" || c.type === "RailButton") S("finish_key", "Surface finish", Object.entries(FINISH), { value: c.finish_key || "NORMAL" });
    return F;
  }

  function fieldEl(f, c, v) {
    const wrap = h("label", "fld" + (f.wide ? " wide" : ""));
    let input, box = h("span", "box");
    const val = f.value !== undefined ? f.value : c[f.key];
    if (f.kind === "check") {
      wrap.className = "fld";
      input = Object.assign(document.createElement("input"), { type: "checkbox", checked: !!val });
      wrap.append(h("span", "", f.label), h("span", "inline", input, f.key === "filled" ? "solid, not hollow" : "yes"));
    } else {
      wrap.append(f.label);
      if (f.kind === "select") {
        input = document.createElement("select");
        for (const [k, t] of f.options) { const o = document.createElement("option"); o.value = k; o.textContent = t; input.append(o); }
        input.value = val ?? "";
      } else if (f.kind === "text") {
        input = Object.assign(document.createElement("input"), { type: "text", value: val ?? "", maxLength: 60, autocomplete: "off" });
      } else {
        input = Object.assign(document.createElement("input"), { type: "number", step: "any", inputMode: "decimal" });
        const shown = f.kind === "dia" ? (f.radius === false ? val : 2 * val) : val;
        input.value = shown == null ? "" : (f.kind === "len" || f.kind === "dia") ? toUnits(shown) : Number(Number(shown).toFixed(f.int ? 0 : 3));
      }
      box.append(input);
      if (f.kind === "len" || f.kind === "dia") box.append(h("span", "", U().len));
      else if (f.unit) box.append(h("span", "", f.unit));
      wrap.append(box);
      if (f.auto && c.auto_radius) wrap.append(h("span", "hint", "automatic: matches the parts next to it"));
    }
    const msg = h("span", "msg"); msg.hidden = true; wrap.append(msg);
    const initial = input.value;
    input.addEventListener("change", () => {
      let out;
      wrap.classList.remove("bad"); msg.hidden = true;
      if (f.kind === "check") out = input.checked;
      else if (f.kind === "select" || f.kind === "text") out = input.value.trim();
      else {
        if (input.value === initial) return;
        const n = Number(input.value);
        const mm = (f.kind === "len" || f.kind === "dia") ? n * U().per : n;
        if (input.value.trim() === "" || !Number.isFinite(n) || mm < f.min - 1e-9 || mm > (f.kind === "dia" ? 2 * f.max : f.max) + 1e-9 || (f.int && !Number.isInteger(n))) {
          wrap.classList.add("bad"); msg.hidden = false;
          const hi = f.kind === "dia" ? 2 * f.max : f.max;
          msg.textContent = `Enter ${(f.kind === "len" || f.kind === "dia") ? `${len(f.min)} to ${len(hi)}` : `${fmt(f.min, 3)} to ${fmt(hi, 3)}`}${f.int ? " (whole number)" : ""}.`;
          return;
        }
        out = f.kind === "dia" ? (f.radius === false ? mm : mm / 2) : mm;
      }
      setField(c.id, f, out);
    });
    return wrap;
  }

  function setField(id, f, value) {
    commit(() => {
      const c = byId(id);
      switch (f.key) {
        case "name": c.name = value || TYPE[c.type]; break;
        case "parent": if (value !== c.parent) { const moving = block(id); removeBlock(id); c.parent = value; parts.splice(endOfBlock(value), 0, ...moving); c.axial_method = "TOP"; c.axial_offset_mm = 0; } break;
        case "offset": c.axial_method = "TOP"; c.axial_offset_mm = Math.round(value * 10) / 10; break;
        case "size": if (value && TUBES[value]) { c.outer_radius_mm = TUBES[value][0] / 2; c.thickness_mm = TUBES[value][1]; c.auto_radius = false; if (c.name === "Body tube" || /^BT-\d+ body tube$/.test(c.name)) c.name = `${value} body tube`; } break;
        case "shape": c.shape = value; c.shape_param = { OGIVE: 1, POWER: 0.5, PARABOLIC: 1, HAACK: 0 }[value] ?? 0; break;
        case "motor_mount": if (value) parts.forEach(p => { if (p.motor_mount) p.motor_mount = false; }); c.motor_mount = value; break;
        default:
          c[f.key] = value;
          if (f.auto) c.auto_radius = false;       // typed a size: stop matching the neighbours
          if (["root_chord_mm", "tip_chord_mm", "span_mm", "sweep_mm"].includes(f.key) && c.type === "FreeformFinSet") c.type = "TrapezoidFinSet";
      }
    }, id);
  }

  function massRow(c, v) {
    const wrap = h("div", "fld wide");
    const always = ["MassComponent", "ShockCord", "Streamer", "RailButton"].includes(c.type);
    wrap.append("Mass");
    const box = h("span", "box");
    const input = Object.assign(document.createElement("input"), { type: "number", step: "any", inputMode: "decimal", value: Number(v.mass_g.toFixed(1)) });
    box.append(input, h("span", "", "g"));
    const row = h("div", "actions");
    row.style.gap = "0.75rem";
    const holder = h("div", "", box); holder.style.flex = "1 1 8rem";
    row.append(holder);
    if (!always) {
      const chk = Object.assign(document.createElement("input"), { type: "checkbox", checked: !!c.weighed });
      row.append(h("label", "inline", chk, "I weighed it"));
      chk.addEventListener("change", () => commit(() => { const p = byId(c.id); p.weighed = chk.checked; if (chk.checked) p.mass_g = v.mass_g; }, c.id));
      if (!c.weighed) {
        input.readOnly = true; box.classList.add("ro");
        const sel = document.createElement("select");
        const list = c.type === "Parachute" ? Object.keys(RB.CLOTH) : Object.keys(RB.MATERIALS);
        if (c.density != null && !c.material_set) { const o = document.createElement("option"); o.value = ""; o.textContent = `As designed${c.material ? " (" + c.material + ")" : ""}`; sel.append(o); }
        for (const k of list) { const o = document.createElement("option"); o.value = k; o.textContent = k; sel.append(o); }
        sel.value = c.density != null && !c.material_set ? "" : mat(c);
        sel.addEventListener("change", () => commit(() => { const p = byId(c.id); if (sel.value) { p.material = sel.value; delete p.density; p.material_set = true; } }, c.id));
        const mbox = h("span", "box", sel);
        const mh = h("div", "", mbox); mh.style.flex = "1 1 10rem";
        row.append(mh);
      }
    }
    wrap.append(row);
    wrap.append(h("span", "hint", always ? "The weight of this item." : c.weighed ? "Your scale's weight is used." : "Worked out from the size and material. Weigh the real part and tick the box for an exact number."));
    input.addEventListener("change", () => {
      const n = Number(input.value);
      if (!(n >= 0 && n <= 2000)) { input.value = v.mass_g; return; }
      commit(() => { const p = byId(c.id); p.mass_g = n; p.weighed = true; }, c.id);
    });
    return wrap;
  }

  function renderEditor() {
    const card = $("editor-card");
    const c = selected && byId(selected);
    const v = c && view.components.find(x => x.id === c.id);
    if (!c || !v) { card.hidden = true; return; }
    card.hidden = false;
    const ed = $("editor");
    const head = h("div", "head", h("div", "", h("h3", "", c.name), h("div", "sub", `${TYPE[c.type] || c.type} · starts ${len(v.x_mm)} from the nose tip`)),
      button("×", "x", () => { selected = null; update(); }));
    head.lastChild.setAttribute("aria-label", "Close part editor");
    const grid = h("div", "fields");
    for (const f of fieldsFor(c, v)) grid.append(fieldEl(f, c, v));
    grid.append(massRow(c, v));
    const actions = h("div", "actions");
    if (RB.canFlip(c)) actions.append(button("Flip ⇄", "btn plain", () => commit(() => { const p = byId(c.id); RB.flip(p); p.auto_radius = false; }, c.id)));
    if (c.parent == null) {
      const st = stageParts(), i = st.indexOf(c);
      const fwd = button("◀ Move forward", "btn plain", () => commit(() => { const mv = block(c.id); removeBlock(c.id); const s2 = stageParts(); parts.splice(parts.indexOf(s2[i - 1]), 0, ...mv); }, c.id));
      const back = button("Move back ▶", "btn plain", () => commit(() => { const mv = block(c.id); removeBlock(c.id); const s2 = stageParts(); const at = i + 1 < s2.length ? parts.indexOf(s2[i + 1]) : parts.length; parts.splice(at, 0, ...mv); }, c.id));
      fwd.disabled = i === 0; back.disabled = i === st.length - 1;
      actions.append(fwd, back);
    }
    actions.append(button("Duplicate", "btn plain", () => duplicate(c.id)));
    const kids = block(c.id).length - 1;
    actions.append(button(kids ? `Delete (and ${kids} inside)` : "Delete", "btn danger", () => commit(() => removeBlock(c.id), null)));
    if (c.type === "InnerTube" && c.motor_mount) actions.append(button("Choose motor", "btn", () => openTab("fly")));
    const extra = [];
    if (c.type === "FreeformFinSet") extra.push(h("p", "note", "Rounded (freeform) fins from the .ork. Changing the outline isn't possible here; delete them and add trapezoidal or elliptical fins to reshape."));
    if (/altimeter/i.test(c.name)) extra.push(h("p", "note", "ARC needs the altimeter on board. Put it in a sealed bay (the Altimeter bay button) or with the eggs, drill 3–4 small vent holes (about 1/16 in) in the tube next to it, and keep it away from the ejection charge."));
    ed.replaceChildren(head, slider(c, v), grid, ...extra, actions);
  }

  // A slider that slides the part through the rocket: along its tube, or through the airframe order.
  function slider(c, v) {
    const wrap = h("div", "slide");
    const range = Object.assign(document.createElement("input"), { type: "range" });
    const label = h("span", "slide-label");
    let before = null;
    const live = () => { analyse(); draw(); renderStats(); const r = rules(); renderTips(suggestions()); renderDock(r); };
    if (c.parent == null) {
      const st = stageParts(), i = st.indexOf(c);
      Object.assign(range, { min: 0, max: Math.max(0, st.length - 1), step: 1, value: i, disabled: st.length < 2 });
      const say = (k) => { label.textContent = `Place in the airframe: ${k + 1} of ${st.length}${k === 0 ? " (front)" : k === st.length - 1 ? " (back)" : ""}`; };
      say(i);
      range.addEventListener("input", () => {
        if (before == null) before = snapshot();
        const k = Number(range.value), mv = block(c.id); removeBlock(c.id);
        const rest = stageParts(); const at = k < rest.length ? parts.indexOf(rest[k]) : parts.length;
        parts.splice(at, 0, ...mv); say(k); live();
      });
    } else {
      const p = view.components.find(x => x.id === c.parent);
      const lo = c.type === "TubeCoupler" ? -c.length_mm : 0, hi = Math.max(lo + 1, p.length_mm - (c.type === "TubeCoupler" ? 0 : c.length_mm));
      Object.assign(range, { min: lo, max: hi, step: 0.5, value: Math.max(lo, Math.min(hi, v.offset_in_parent_mm || 0)) });
      const say = (o) => { label.textContent = `Slide along the ${p.name}: ${len(Math.abs(o) < 0.3 ? 0 : o)} from its top`; };
      say(Number(range.value));
      range.addEventListener("input", () => {
        if (before == null) before = snapshot();
        const part = byId(c.id); part.axial_method = "TOP"; part.axial_offset_mm = Number(range.value);
        say(Number(range.value)); live();
      });
    }
    range.setAttribute("aria-label", c.parent == null ? "Place in the airframe" : "Position along its tube");
    range.addEventListener("change", () => {
      if (before != null) { undoStack.push(before); redoStack.length = 0; before = null; }
      save(); update();
    });
    wrap.append(label, range);
    return wrap;
  }
  function duplicate(id) {
    commit(() => {
      const src = block(id), map = {};
      const copies = src.map(p => { const q = JSON.parse(JSON.stringify(p)); map[p.id] = q.id = newId(); return q; });
      copies.forEach(q => { if (map[q.parent]) q.parent = map[q.parent]; });
      copies[0].name = copies[0].name + " (copy)";
      if (copies[0].parent == null) parts.splice(endOfBlock(id), 0, ...copies);
      else { copies[0].axial_method = "TOP"; copies[0].axial_offset_mm = (view.components.find(v => v.id === id).offset_in_parent_mm || 0) + 5; parts.splice(endOfBlock(copies[0].parent), 0, ...copies); }
      selected = copies[0].id;
    });
  }

  // ---------------- rules and suggestions ----------------
  function rules() {
    const s = S(), comps = view.components, out = [];
    const tubes = comps.filter(c => c.type === "BodyTube");
    const big = tubes.filter(t => Math.abs(2 * t.outer_radius_mm - 66) <= 0.8), others = tubes.filter(t => !big.includes(t));
    const m = motorEntry(), add = (state, text, detail) => out.push({ state, text, detail });
    add(s.airframe_length_mm >= 650 ? "pass" : "fail", "At least 650 mm (25.6 in) long", len(s.airframe_length_mm));
    add(s.mass_with_motor_g <= 650 ? "pass" : "fail", "No more than 650 g at liftoff", `${fmt(s.mass_with_motor_g, 0)} g`);
    add(m && m.total_impulse_ns <= 80 ? "pass" : "fail", "Approved motor, no more than 80 N·s", m ? `${m.manufacturer} ${m.designation}, ${fmt(m.total_impulse_ns, 1)} N·s` : "no motor");
    add(view.motors.length && view.motors[0].fits ? "pass" : "fail", "Motor fits the motor mount", view.motors.length ? `${fmt(view.motors[0].diameter_mm, 0)} mm motor` : "no motor mount");
    add(big.some(t => t.length_mm >= 304.8) ? "pass" : "fail", "A T-80 (66 mm) tube at least 12 in long", big.length ? big.map(t => len(t.length_mm)).join(", ") : "none");
    add(tubes.length >= 2 && others.every(t => Math.abs(2 * t.outer_radius_mm - 66) >= 9) ? "pass" : "fail", "Other tubes at least 9 mm different from T-80",
        others.map(t => len(2 * t.outer_radius_mm)).join(", ") || "no second tube");
    add(comps.some(c => c.type === "Parachute") ? "pass" : "fail", "Recovery by parachute", "");
    add(s.stability_cal >= 1 ? "pass" : "warn", "Stable: at least 1.0 cal (safety)", s.stability_cal == null ? "add fins" : `${fmt(s.stability_cal, 2)} cal`);
    return out;
  }

  function suggestions() {
    const out = [], s = S(), comps = view.components;
    const add = (level, text, why, label, run) => out.push({ level, text, why, action: label ? { label, run } : null });
    const nose = comps.find(c => c.type === "NoseCone");
    const tubes = comps.filter(c => c.type === "BodyTube");
    const mount = comps.find(c => c.type === "InnerTube" && c.motor_mount);
    const fins = comps.filter(isFins).sort((a, b) => b.span_mm * b.root_chord_mm - a.span_mm * a.root_chord_mm)[0];
    if (!comps.length) { add("info", "Start with a nose cone", "Then add body tubes, fins, a motor mount and a parachute.", "Add nose cone", () => addPart("my_nose")); return out; }
    if (!nose) add("must", "Add a nose cone", "", "Add nose cone", () => addPart("my_nose"));
    if (!tubes.length) add("must", "Add a body tube", "ARC needs a BT-80 tube of at least 12 in for the eggs.", "Add BT-80", () => addPart("my_bt80"));
    if (tubes.length && !mount) add("must", "Add a motor mount", "An inner tube in the last body tube holds the motor.", "Add 29 mm mount", () => { selected = tubes[tubes.length - 1].id; addPart("my_mount"); });
    const m = view.motors[0];
    if (m && !m.fits) add("must", `The ${fmt(m.diameter_mm, 0)} mm motor doesn't fit the mount`, `The mount is ${len(2 * mount.inner_radius_mm)} inside.`, "Resize mount",
      () => commit(() => { const p = byId(mount.id); p.inner_radius_mm = (m.diameter_mm + 0.1) / 2; p.outer_radius_mm = p.inner_radius_mm + 0.8; }, mount.id));
    if (mount && !comps.some(c => c.type === "CenteringRing" && c.parent === mount.parent))
      add("should", "Add centering rings around the motor mount", "Two rings hold the mount in the middle of the tube.", "Add 2 rings", () => commit(() => {
        for (const off of [-8, -85]) { const r = CATALOG.ring.make({}); r.id = newId(); r.parent = mount.parent; r.axial_offset_mm = off; r.weighed = false; parts.splice(endOfBlock(mount.parent), 0, r); }
      }));
    if (tubes.length && !fins) add("must", "Add fins", "Without fins it won't fly straight.", "Add fins", () => { selected = tubes[tubes.length - 1].id; addPart("my_fins"); });
    if (tubes.length && !comps.some(c => c.type === "Parachute")) add("must", "Add a parachute", "ARC requires recovery by parachute.", "Add 18 in chute", () => addPart("my_chute18"));

    // ARC rules with fixes
    if (s.airframe_length_mm > 0 && s.airframe_length_mm < 650 && tubes.length) {
      const t = tubes[tubes.length - 1], need = 650 - s.airframe_length_mm + 5;
      add("must", `Make it ${len(need)} longer`, `ARC needs at least 650 mm; this is ${len(s.airframe_length_mm)}.`, "Lengthen", () => commit(() => { byId(t.id).length_mm = Math.round((t.length_mm + need) * 10) / 10; }, t.id));
    }
    const big = tubes.filter(t => Math.abs(2 * t.outer_radius_mm - 66) <= 0.8).sort((a, b) => b.length_mm - a.length_mm)[0];
    if (tubes.length && !big) add("must", "Add a BT-80 (T-80) tube for the eggs", "ARC needs one T-80 tube at least 12 in long.", "Add BT-80", () => addPart("my_bt80"));
    else if (big && big.length_mm < 304.8) add("must", `${big.name} must be at least 12 in`, `It is ${len(big.length_mm)}.`, "Set to 12 in", () => commit(() => { byId(big.id).length_mm = 305; }, big.id));
    if (s.mass_with_motor_g > 650) add("must", `Remove at least ${fmt(s.mass_with_motor_g - 650, 0)} g`, `ARC's limit is 650 g at liftoff; this is ${fmt(s.mass_with_motor_g, 0)} g.`);
    else if (s.mass_with_motor_g > 634) add("should", `Only ${fmt(650 - s.mass_with_motor_g, 0)} g under the 650 g limit`, "Two large eggs can weigh up to 16 g more than average.");

    // stability
    if (s.stability_cal != null && nose && fins) {
      const D = s.reference_diameter_mm, M = s.mass_with_motor_g;
      if (s.stability_cal < 1) {
        const target = s.cp_mm - 1.05 * D, bx = nose.x_mm + nose.length_mm * 0.55;
        const g = Math.ceil(M * (s.cg_mm - target) / (target - bx));
        add("must", `Add about ${g} g of nose ballast`, `Stability is ${fmt(s.stability_cal, 2)} cal; at least 1.0 is needed to fly straight.`, "Add ballast", () => setBallast(g));
        const fs = finFor(1.15, fins); if (fs) add("should", `Or make the fins taller (${len(fs.from)} → ${len(fs.span)})`, `Gives ${fmt(fs.cal, 2)} cal without extra weight up front.`, "Taller fins", fs.apply);
      } else if (s.stability_cal > 3) {
        const fs = finFor(2.5, fins);
        add("should", `Very stable (${fmt(s.stability_cal, 2)} cal): it will turn into the wind`, fs ? `Fins ${len(fs.span)} tall give ${fmt(fs.cal, 2)} cal.` : "Remove nose ballast or move weight back.", fs ? "Shorter fins" : null, fs && fs.apply);
      } else add("ok", `Stability ${fmt(s.stability_cal, 2)} cal`, "Between 1 and 3 cal: flies straight without turning hard into the wind.");
    }
    // ARC payload
    const names = comps.map(c => (c.name || "").toLowerCase());
    const eggs = names.filter(n => n.includes("egg") && !n.includes("protector") && !n.includes("casing")).length;
    if (tubes.length && eggs < 2) add("should", `Add the eggs (${eggs} of 2)`, "ARC 2027 carries two large eggs, 55–63 g each. If two unnamed mass items are the eggs, rename them instead.", "Add egg", () => { if (big) selected = big.id; addPart("my_egg"); });
    if (tubes.length && !names.some(n => n.includes("altimeter"))) add("should", "Add the altimeter", "ARC needs an approved altimeter on board.", "Add altimeter", () => { if (big) selected = big.id; addPart("my_alt"); });
    // flight
    const f = view.flight;
    if (f) {
      const p = f.predicted, err = p.apogee_ft - f.target_apogee_ft, sens = f.sensitivity || { apogee_ft_per_10g: -11, duration_s_per_10g: -0.9 };
      if (Math.abs(err) > 15) {
        const g = Math.round(-err / (sens.apogee_ft_per_10g / 10));
        if (g > 0) add("should", `Add about ${g} g to come down ${alt(err)} to ${alt(f.target_apogee_ft)}`, "Nose ballast also raises stability.", s.mass_with_motor_g + g <= 650 ? "Add as ballast" : null, () => setBallast(ballastNow() + g));
        else add("should", `${alt(-err)} short of ${alt(f.target_apogee_ft)}`, "Remove weight or use a stronger motor.", ballastNow() > 0 ? "Less ballast" : null, () => setBallast(Math.max(0, ballastNow() + g)));
      } else add("ok", `Apogee ${alt(p.apogee_ft)}, close to ${alt(f.target_apogee_ft)}`, "");
      const chutes = comps.filter(c => c.type === "Parachute");
      const descent = p.duration_s - (p.time_to_apogee_s || 0), goal = 38.5 - (p.time_to_apogee_s || 0);
      let fix = null;
      if (chutes.length && descent > 0 && goal > 0) {
        const area = chutes.reduce((a, c) => a + c.cd * c.canopy_diameter_mm ** 2, 0), want = area * (descent / goal) ** 2;
        const bigc = chutes.slice().sort((a, b) => b.canopy_diameter_mm - a.canopy_diameter_mm)[0];
        const dia = Math.round(Math.sqrt(Math.max(0, bigc.canopy_diameter_mm ** 2 + (want - area) / bigc.cd)) / 12.7) * 12.7;
        if (dia >= 150 && Math.abs(dia - bigc.canopy_diameter_mm) > 5) fix = { c: bigc, dia };
      }
      if (p.duration_s > 40 || p.duration_s < 37)
        add("should", p.duration_s > 40 ? `Comes down ${fmt(p.duration_s - 40, 1)} s too slowly` : `Comes down ${fmt(37 - p.duration_s, 1)} s too fast`,
            fix ? `A ${len(fix.dia, 1)} canopy on ${fix.c.name} should land near 38.5 s.` : "Change the parachute size.",
            fix ? "Resize chute" : null, fix && (() => commit(() => { byId(fix.c.id).canopy_diameter_mm = fix.dia; }, fix.c.id)));
      else add("ok", `Duration ${fmt(p.duration_s, 1)} s, inside 37–40 s`, "");
    } else if (mount && tubes.length) add("info", "Simulate for apogee and duration", "Weight and stability update as you build; the flight needs OpenRocket.", "Simulate", () => dispatchEvent(new CustomEvent("builder-simulate")));
    return out;
  }
  const ballastPart = () => parts.find(p => p.type === "MassComponent" && /ballast/i.test(p.name) && byId(p.parent) && byId(p.parent).type === "NoseCone");
  const ballastNow = () => (ballastPart() || {}).mass_g || 0;
  function setBallast(g) {
    const b = ballastPart();
    if (b) commit(() => { byId(b.id).mass_g = g; }, b.id);
    else { addPart("my_ballast"); const nb = ballastPart(); if (nb) { nb.mass_g = g; save(); update(); } }
  }
  function finFor(targetCal, fins) {
    if (!fins || fins.type === "FreeformFinSet") return null;
    const up = targetCal > view.summary.stability_cal;
    for (let k = 1; k <= 80; k++) {
      const span = fins.span_mm + (up ? 2 : -2) * k;
      if (span < 15 || span > 250) break;
      const trial = JSON.parse(JSON.stringify(parts));
      trial.find(p => p.id === fins.id).span_mm = span;
      const cal = RB.analyze({ components: trial, motor: motorEntry(), delay_s: delay, liftoff_mass_g: liftoff }).summary.stability_cal;
      if (up ? cal >= targetCal : cal <= targetCal) return { span, from: fins.span_mm, cal, apply: () => commit(() => { byId(fins.id).span_mm = span; }, fins.id) };
    }
    return null;
  }

  // ---------------- render ----------------
  function tipEl(sg) {
    const li = h("li", "tip " + sg.level, h("span", "dot"), h("span", "", sg.text));
    li.append(sg.action ? button(sg.action.label, "btn", sg.action.run) : h("span"));
    if (sg.why) li.append(h("span", "why", sg.why));
    return li;
  }
  function renderTips(list) {
    const rank = { must: 0, should: 1, info: 2, ok: 3 };
    const todo = list.filter(t => t.level !== "ok").sort((a, b) => rank[a.level] - rank[b.level]);
    const first = todo[0] || list[0];
    $("tip-first").textContent = first ? first.text : "No problems found";
    $("tip-count").textContent = todo.length > 1 ? `${todo.length} suggestions ▾` : todo.length === 1 ? "1 suggestion ▾" : "";
    $("tip-list").replaceChildren(...todo.map(tipEl));
    $("tips").querySelector("summary").className = first ? first.level : "ok";
    $("sugg-full").replaceChildren(...list.map(tipEl));
  }
  function renderStats() {
    const s = S(), m = view.motors[0];
    const cell = (k, v, cls, wide) => h("div", wide ? "wide-only" : "", h("dt", "", k), Object.assign(h("dd", cls || "", v)));
    const stab = s.stability_cal;
    $("stats").replaceChildren(
      cell("Length", len(s.length_mm)),
      cell(m ? "Mass" : "Mass, no motor", `${fmt(m ? s.mass_with_motor_g : s.mass_no_motor_g, 0)} g`, s.mass_with_motor_g > 650 ? "bad" : ""),
      cell("Stability", stab == null ? "—" : `${fmt(stab, 2)} cal`, stab == null ? "" : stab < 1 ? "bad" : stab <= 3 ? "good" : ""),
      cell("CG", len(s.cg_mm), "", true),
      cell("CP", s.cp_mm == null ? "—" : len(s.cp_mm), "", true));
    const st = $("state");
    st.textContent = view.simulated ? "Simulated" : "Estimate"; st.className = "state" + (view.simulated ? " sim" : "");
  }
  function renderParts() {
    const comps = view.components;
    $("parts").replaceChildren(...comps.map(c => {
      let depth = 0; for (let p = c; p.parent != null; p = comps.find(x => x.id === p.parent) || { parent: null }) depth++;
      const tr = document.createElement("tr"); tr.tabIndex = 0; if (c.id === selected) tr.className = "on";
      const nm = h("td", "", c.name); nm.style.paddingLeft = (0.7 + depth * 1.1) + "rem";
      if (c.weighed && !["MassComponent", "ShockCord", "RailButton"].includes(c.type)) nm.append(h("span", "badge", "weighed"));
      nm.append(h("span", "kind", TYPE[c.type] || c.type));
      const d = c.outer_radius_mm ? 2 * c.outer_radius_mm : c.type === "NoseCone" ? 2 * c.aft_radius_mm : c.radius_mm ? 2 * c.radius_mm : null;
      const cells = [len(c.x_mm), c.length_mm ? len(c.length_mm) : "—", d ? len(d) : "—", `${fmt(c.mass_g, 1)} g`];
      nm.append(h("span", "facts", `at ${cells[0]} · ${cells[1]}${d ? " · ⌀ " + cells[2] : ""} · ${cells[3]}`));
      tr.append(nm, ...cells.map(t => h("td", "", t)));
      const pick = () => { selected = c.id; update(); $("editor-card").scrollIntoView({ behavior: "smooth", block: "nearest" }); };
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
      return tr;
    }));
  }
  function renderFly(r) {
    $("rules").replaceChildren(...r.map(x => { const li = h("li", x.state); const t = h("div", "", x.text + " "); if (x.detail) t.append(h("span", "", x.detail)); li.append(t); return li; }));
    const f = view.flight;
    $("results-card").hidden = !f;
    if (f) {
      const p = f.predicted, w = f.conditions, t = f.target_apogee_ft, err = p.apogee_ft - t;
      $("results-head").textContent = `At ${alt(w.elevation_ft)} elevation, ${fmt(w.wind_mph)} mph wind, ${fmt(w.humidity_pct, 0)}% humidity, ${fmt(w.temperature_f, 0)} °F · calibrated with your flights`;
      const cell = (k, v, sm, cls) => { const dd = h("dd", "", v); if (sm) dd.append(h("small", "", sm)); return h("div", cls || "", h("dt", "", k), dd); };
      $("results").replaceChildren(
        cell("Apogee", alt(p.apogee_ft), `${err >= 0 ? "+" : ""}${alt(err)} from ${alt(t)}`, Math.abs(err) <= 25 ? "good" : "off"),
        cell("Duration", fmt(p.duration_s, 1) + " s", "window 37–40 s", p.duration_s >= 37 && p.duration_s <= 40 ? "good" : "off"),
        cell("Score", fmt(f.score.total, 1), "lower is better"),
        cell("Off the rail", speed(p.velocity_off_rail_fps), fmt(p.stability_off_rail_cal, 2) + " cal"),
        cell("Peak accel.", fmt(p.max_acceleration_g, 1) + " g"),
        cell("Descent", speed(p.descent_rate_fps)));
    }
  }
  function renderDock(r) {
    const s = S(), set = (id, t, cls) => { const e = $(id); e.textContent = t; e.className = cls || ""; };
    set("d-mass", `${fmt(s.mass_with_motor_g, 0)} g`, s.mass_with_motor_g > 650 ? "bad" : "");
    set("d-stab", s.stability_cal == null ? "—" : `${fmt(s.stability_cal, 2)} cal`, s.stability_cal == null ? "" : s.stability_cal < 1 ? "bad" : "good");
    const fails = r.filter(x => x.state === "fail").length;
    set("d-rules", fails ? `${fails} failing` : "all pass", fails ? "bad" : "good");
    set("d-apogee", view.flight ? alt(view.flight.predicted.apogee_ft) : "—");
    const ready = canSimulate();
    $("d-sim").disabled = !ready.ok || !cloudReady; $("run").disabled = !ready.ok || !cloudReady;
    $("run").title = ready.ok ? "" : ready.why;
  }
  function canSimulate() {
    if (!view.components.some(c => c.parent == null)) return { ok: false, why: "Build a rocket first." };
    if (!view.motors.length) return { ok: false, why: "Add a motor mount (an inner tube) first." };
    if (!view.motors[0].fits) return { ok: false, why: "The motor doesn't fit the mount." };
    return { ok: true };
  }
  let cloudReady = false;

  function update() {
    analyse();
    draw();
    updatePalette();
    renderStats();
    const r = rules();
    renderTips(suggestions());
    renderEditor();
    renderParts();
    renderFly(r);
    renderDock(r);
    $("undo").disabled = !undoStack.length; $("redo").disabled = !redoStack.length;
    $("name").value = rocketName;
  }

  // ---------------- Fly tab controls ----------------
  function fillMotors() {
    const sel = $("motor"); sel.replaceChildren();
    for (const dia of [...new Set(menu.map(m => m.diameter_mm))].sort((a, b) => b - a)) {
      const g = document.createElement("optgroup"); g.label = `${dia} mm motors`;
      for (const m of menu.filter(x => x.diameter_mm === dia && x.simulatable).sort((a, b) => a.designation.localeCompare(b.designation, undefined, { numeric: true }))) {
        const o = document.createElement("option"); o.value = m.key; o.textContent = `${m.designation} · ${m.manufacturer} · ${fmt(m.total_impulse_ns)} N·s${m.reloadable ? " · reload" : ""}`; g.append(o);
      }
      sel.append(g);
    }
    syncFly();
  }
  function syncFly() {
    const m = motorEntry();
    $("motor").value = motorKey;
    const d = $("delay"); d.replaceChildren();
    for (const x of (m ? m.delays_s : [])) { const o = document.createElement("option"); o.value = x; o.textContent = x === 0 ? "0 s (no ejection)" : `${fmt(x, 0)} s`; d.append(o); }
    if (m && !m.delays_s.includes(delay)) delay = m.delays_s.filter(x => x > 0)[0] ?? m.delays_s[0];
    d.value = delay;
    $("liftoff").value = liftoff ?? "";
    $("motor-info").textContent = m ? [`${m.diameter_mm} mm, ${len(m.length_mm)} long`, `${fmt(m.total_impulse_ns)} N·s`, m.average_thrust_n ? `avg ${fmt(m.average_thrust_n)} N` : null,
      m.launch_mass_g ? `${fmt(m.launch_mass_g, 0)} g` : null, m.reloadable ? "reloadable: adult purchase" : "single use"].filter(Boolean).join(" · ") : "";
  }
  $("motor").addEventListener("change", () => commit(() => { motorKey = $("motor").value; const m = motorEntry(); delay = m.delays_s.filter(x => x > 0)[Math.floor((m.delays_s.filter(x => x > 0).length - 1) / 2)] ?? 0; syncFly(); }));
  $("delay").addEventListener("change", () => commit(() => { delay = Number($("delay").value); }));
  $("liftoff").addEventListener("change", () => { const v = $("liftoff").value.trim(); const n = Number(v);
    if (v && !(n >= 100 && n <= 1000)) { $("liftoff").value = liftoff ?? ""; return; }
    commit(() => { liftoff = v ? n : null; }); });
  $("name").addEventListener("change", () => commit(() => { rocketName = $("name").value.trim() || "My rocket"; }));

  // ---------------- tabs, zoom, units, templates ----------------
  function openTab(which) {
    for (const t of ["build", "fly", "ai"]) { $("tab-" + t).setAttribute("aria-selected", String(t === which)); $("pane-" + t).hidden = t !== which; }
    if (which === "build") draw();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  for (const t of ["build", "fly", "ai"]) $("tab-" + t).addEventListener("click", () => openTab(t));
  document.querySelectorAll("[data-zoom]").forEach(b => b.addEventListener("click", () => {
    zoom = Number(b.dataset.zoom); document.querySelectorAll("[data-zoom]").forEach(x => x.setAttribute("aria-pressed", String(x === b))); draw(); }));
  $("show-cgcp").addEventListener("change", draw);
  $("stand").checked = vertical;
  $("stand").addEventListener("change", () => { vertical = $("stand").checked; try { localStorage.setItem("athens-standing", vertical ? "1" : "0"); } catch {} draw(); });
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => view && draw(), 120); });
  $("units").value = units;
  $("units").addEventListener("change", () => { units = $("units").value; try { localStorage.setItem("athens-units", units); } catch {} syncFly(); update(); });

  function loadTemplate(which) {
    commit(() => {
      if (which === "blank") { parts = []; rocketName = "My rocket"; }
      else {
        const src = which === "team" ? teamOrig : team;
        parts = JSON.parse(JSON.stringify(src)); rocketName = which === "team" ? "Ederance (original)" : "Ederance, 29 mm mount";
        motorKey = which === "team" ? (menu.find(m => /F39/.test(m.designation)) || {}).key || motorKey : "F59WT|Cesaroni"; delay = 5;
      }
      liftoff = null; selected = null; syncFly();
    });
  }
  $("template").addEventListener("change", () => { const v = $("template").value; $("template").value = ""; if (!v) return;
    if (parts.length && !confirm("Replace the rocket on screen? You can undo this.")) return; loadTemplate(v); });
  document.querySelectorAll("[data-start]").forEach(b => b.addEventListener("click", () => b.dataset.start === "nose" ? addPart("my_nose") : loadTemplate("team29")));

  // ---------------- for build-cloud.js ----------------
  window.builder = {
    specJson, rocketName: () => rocketName, canSimulate: () => canSimulate(),
    setCloudReady(v) { cloudReady = v; update(); },
    applySimulation(sig, design) { sim = { sig, summary: design.summary, flight: design.flight }; update(); },
    // A design-chat candidate: the exported design the worker simulated, with the motor it used.
    openDesign(design, { name, motor, delay_s } = {}) {
      commit(() => {
        parts = RB.importDesign(design);
        if (motor && menu.some(m => m.key === motor)) motorKey = motor; else if (!motor) motorKey = "F59WT|Cesaroni";
        delay = delay_s ?? design.motors[0]?.delay_s ?? delay; liftoff = null; rocketName = name || rocketName; selected = null; syncFly();
      });
      openTab("build");
    },
    openTab, alt, len,
  };

  // ---------------- start ----------------
  const getJson = (u) => fetch(u).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  Promise.all([getJson("../data/motor_menu.json"), getJson("../designs/ederance-29mm.json"), getJson("../designs/ederance.json")]).then(([mm, t29, t]) => {
    menu = mm.motors;
    team = RB.importDesign(t29); teamOrig = RB.importDesign(t);
    let saved = null;
    try { saved = localStorage.getItem("athens-builder-v2"); } catch {}
    if (saved) { try { restore(saved); } catch { parts = []; } }
    buildPalette(); fillMotors(); update();
    dispatchEvent(new CustomEvent("builder-ready"));
  }).catch(err => { $("target").textContent = "Could not load the motor list or parts: " + err.message; });
})();
