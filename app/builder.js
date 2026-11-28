// Rocket builder preview: applies a draft of edits to an exported design and recomputes
// layout, mass, CG, CP and stability in the browser, the way OpenRocket does.
//
// Positions follow each part's own placement rule (after the previous part, or from the top,
// middle or bottom of its parent). CP starts from OpenRocket's per-part numbers in the export
// ("aero") and recomputes only the parts whose shape changed, using the same Barrowman
// formulas (nose and transition by volume, fins by the strip-integrated mean aerodynamic chord).
// Every number here is an estimate until the worker simulates the same changes.
(function (root) {
  "use strict";

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const DEFAULT_PARAM = { CONICAL: 0, OGIVE: 1, ELLIPSOID: 0, POWER: 0.5, PARABOLIC: 1, HAACK: 0 };
  const FIN_EFFICIENCY = [1, 1, 1, 1, 1, 0.948, 0.913, 0.854, 0.81, 0.75];
  const MACH = 0.3;
  const NOSE_BALLAST_FRACTION = 0.55;   // openrocket.add_nose_ballast

  // ----- shapes (OpenRocket Transition.Shape) -----
  function shapeRadius(shape, x, R, L, p) {
    if (x <= 0) return 0;
    if (x >= L) return R;
    switch (shape) {
      case "CONICAL": return R * x / L;
      case "ELLIPSOID": { const xs = x * R / L; return Math.sqrt(Math.max(0, 2 * R * xs - xs * xs)); }
      case "POWER": return p <= 1e-5 ? R : R * Math.pow(x / L, p);
      case "PARABOLIC": { const t = x / L; return R * (2 * t - p * t * t) / (2 - p); }
      case "HAACK": { const th = Math.acos(1 - 2 * x / L); return R * Math.sqrt(Math.max(0, (th - Math.sin(2 * th) / 2 + p * Math.pow(Math.sin(th), 3)) / Math.PI)); }
      case "OGIVE": {
        if (p < 0.001) return R * x / L;
        const rho = Math.sqrt((L * L + R * R) * (Math.pow((2 - p) * L, 2) + Math.pow(p * R, 2)) / (4 * Math.pow(p * R, 2)));
        const l = L / p, y0 = Math.sqrt(Math.max(0, rho * rho - l * l));
        return Math.sqrt(Math.max(0, rho * rho - (l - x) * (l - x))) - y0;
      }
      default: return R * x / L;
    }
  }
  function profileRadius(c, x) {
    const L = c.length_mm, fore = c.fore_radius_mm, aft = c.aft_radius_mm, p = c.shape_param;
    return fore <= aft ? fore + shapeRadius(c.shape, x, aft - fore, L, p)
                       : aft + shapeRadius(c.shape, L - x, fore - aft, L, p);
  }

  // ----- aerodynamics (Mach 0.3, zero angle of attack) -----
  // Nose cone or transition: CNa = 2 (A_aft - A_fore) / A_ref, CP from the enclosed volume.
  function bodyAero(c, refR) {
    const n = 400, L = c.length_mm;
    let V = 0;
    for (let i = 0; i < n; i++) { const r = profileRadius(c, (i + 0.5) * L / n); V += Math.PI * r * r * L / n; }
    const A1 = Math.PI * c.fore_radius_mm ** 2, A2 = Math.PI * c.aft_radius_mm ** 2, Aref = Math.PI * refR * refR;
    if (Math.abs(A2 - A1) < 1e-9) return { cna: 0, cp_local: L / 2 };
    return { cna: 2 * (A2 - A1) / Aref, cp_local: (L * A2 - V) / (A2 - A1) };
  }

  // Leading and trailing edge of a fin polygon at height y.
  function edgesAt(pts, y) {
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < pts.length; i++) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
      if (y1 === y2) continue;
      if ((y >= Math.min(y1, y2)) && (y <= Math.max(y1, y2))) {
        const x = x1 + (y - y1) * (x2 - x1) / (y2 - y1);
        lo = Math.min(lo, x); hi = Math.max(hi, x);
      }
    }
    return lo <= hi ? [lo, hi] : [0, 0];
  }
  function finGeometry(pts) {
    const span = Math.max(...pts.map(p => p[1]));
    const n = 200, dy = span / n;
    let area = 0, mac = 0, macLead = 0, cx = 0;
    let first = null, last = null;
    for (let i = 0; i < n; i++) {
      const y = (i + 0.5) * dy, [a, b] = edgesAt(pts, y), c = b - a;
      if (i === 0) first = [a, b];
      last = [a, b];
      area += c * dy; mac += c * c * dy; macLead += a * c * dy; cx += (a + b) / 2 * c * dy;
    }
    if (area <= 0) return { span, area: 0, mac: 0, macLead: 0, cosGamma: 1, centroid_x: 0 };
    const midRoot = (first[0] + first[1]) / 2, midTip = (last[0] + last[1]) / 2;
    return { span, area, mac: mac / area, macLead: macLead / area, centroid_x: cx / area,
             cosGamma: span / Math.hypot(midTip - midRoot, span) };
  }
  function finAero(f, refR) {
    const g = finGeometry(f.points_mm);
    if (!g.area) return { cna: 0, cp_local: 0 };
    const Aref = Math.PI * refR * refR, beta2 = 1 - MACH * MACH;
    const cna1 = 2 * Math.PI * g.span ** 2 / Aref / (1 + Math.sqrt(1 + beta2 * (g.span ** 2 / (g.area * g.cosGamma)) ** 2));
    const N = f.fin_count;
    const count = N >= 3 ? N / 2 * (FIN_EFFICIENCY[Math.min(N, 9)]) : (N === 2 ? 1 : 0.5);
    const r = f.body_radius_mm || 0;
    return { cna: cna1 * count * (1 + r / (g.span + r)), cp_local: g.macLead + 0.25 * g.mac };
  }
  function trapezoid(root, tip, span, sweep) {
    return [[0, 0], [sweep, span], [sweep + tip, span], [root, 0]];
  }
  // Fin outline numbers for the editor: a freeform fin is summarised by its trapezoid equivalent.
  function finShape(f) {
    if (f.tip_chord_mm != null && f.sweep_mm != null) return { root: f.root_chord_mm, tip: f.tip_chord_mm, span: f.span_mm, sweep: f.sweep_mm };
    // Freeform (rounded) fins: the trapezoid with the same root, span and area, tip centred.
    const g = finGeometry(f.points_mm), root = f.root_chord_mm, span = g.span;
    const tip = Math.max(0, Math.min(root, 2 * g.area / span - root));
    const r1 = (v) => Math.round(v * 10) / 10;
    return { root, tip: r1(tip), span: r1(span), sweep: r1((root - tip) / 2) };
  }

  // ----- layout -----
  function place(c, parent, prevEnd) {
    const m = c.axial_method || "TOP", off = c.axial_offset_mm || 0;
    if (!parent) return m === "ABSOLUTE" ? off : prevEnd + (m === "AFTER" ? off : 0);
    const L = c.length_mm || 0;
    switch (m) {
      case "BOTTOM": return parent.x_mm + parent.length_mm - L + off;
      case "MIDDLE": return parent.x_mm + (parent.length_mm - L) / 2 + off;
      case "ABSOLUTE": return off;
      default: return parent.x_mm + off;   // TOP
    }
  }

  // plan: { removes, moves, edits, chutes, adds, ballast_g, spread_g, total_g, motor, delay_s }
  function preview(base, plan) {
    const d = clone(base);
    let comps = d.components;
    const baseX = Object.fromEntries(base.components.map(c => [c.id, c.x_mm]));
    for (const c of comps) c._cg_local = c.cg_x_mm - c.x_mm;

    // removes take their children with them
    const gone = new Set((plan.removes || []).map(String));
    let grew = true;
    while (grew) { grew = false; for (const c of comps) if (c.parent != null && gone.has(String(c.parent)) && !gone.has(String(c.id))) { gone.add(String(c.id)); grew = true; } }
    comps = comps.filter(c => !gone.has(String(c.id)));
    const byId = () => Object.fromEntries(comps.map(c => [String(c.id), c]));
    let ids = byId();
    const reshaped = new Set();

    for (const [id, e] of Object.entries(plan.edits || {})) {
      const c = ids[id]; if (!c) continue;
      if (e.length_mm != null && e.length_mm !== c.length_mm) {
        const k = e.length_mm / c.length_mm;
        if (!c.mass_measured && c.type !== "MassComponent") c.mass_g *= k;
        c._cg_local *= k;
        c.length_mm = e.length_mm;
        if (c.type === "NoseCone" || c.type === "Transition") reshaped.add(String(c.id));
      }
      if (e.shape) { c.shape = e.shape; c.shape_param = DEFAULT_PARAM[e.shape] ?? 0; reshaped.add(String(c.id)); }
      if (e.cd != null) c.cd = e.cd;
      if (c.type && c.type.endsWith("FinSet") && ["fin_count", "root_chord_mm", "tip_chord_mm", "span_mm", "sweep_mm", "thickness_mm"].some(k => e[k] != null)) {
        const s = finShape(c), oldArea = finGeometry(c.points_mm).area * c.fin_count * c.thickness_mm;
        const root = e.root_chord_mm ?? s.root, tip = e.tip_chord_mm ?? s.tip, span = e.span_mm ?? s.span, sweep = e.sweep_mm ?? s.sweep;
        c.fin_count = e.fin_count ?? c.fin_count;
        c.thickness_mm = e.thickness_mm ?? c.thickness_mm;
        if ([e.root_chord_mm, e.tip_chord_mm, e.span_mm, e.sweep_mm].some(v => v != null)) {
          c.points_mm = trapezoid(root, tip, span, sweep);
          Object.assign(c, { root_chord_mm: root, tip_chord_mm: tip, span_mm: span, sweep_mm: sweep, length_mm: Math.max(root, sweep + tip), fin_shape: "Trapezoid" });
          c.tab_length_mm = Math.min(c.tab_length_mm || 0, c.length_mm);
        }
        const g = finGeometry(c.points_mm);
        if (!c.mass_measured && oldArea > 0) c.mass_g *= g.area * c.fin_count * c.thickness_mm / oldArea;
        c._cg_local = g.centroid_x;
        reshaped.add(String(c.id));
      }
      if (e.mass_g != null) { c.mass_g = e.mass_g; if (c.type !== "MassComponent") c.mass_measured = true; }
    }
    for (const [id, dia] of Object.entries(plan.chutes || {})) {
      const c = ids[id]; if (c) c.canopy_diameter_mm = dia;
    }
    for (const [id, off] of Object.entries(plan.moves || {})) {
      const c = ids[id]; if (!c) continue;
      c.axial_method = "TOP"; c.axial_offset_mm = off;
    }

    // added parts
    (plan.adds || []).forEach((a, i) => {
      const id = "add-" + i, kind = a.kind || "mass";
      if (kind === "bodytube") {
        const after = ids[String(a.after_id)]; if (!after) return;
        const r = after.type === "BodyTube" ? after.outer_radius_mm : after.aft_radius_mm;
        const wall = after.type === "BodyTube" ? after.thickness_mm : 0.5;
        const t = { id, parent: null, depth: 0, type: "BodyTube", name: a.name, label: a.name, length_mm: a.length_mm,
          mass_g: a.mass_g || 0, mass_measured: a.mass_g != null, internal: false, movable: false, added: true,
          outer_radius_mm: r, inner_radius_mm: r - wall, thickness_mm: wall, axial_method: "AFTER", axial_offset_mm: 0, _cg_local: a.length_mm / 2 };
        // keep tree order: the part's own children stay with it, then the new tube
        let j = comps.indexOf(after) + 1;
        const isDesc = (x) => { for (let p = x.parent; p != null; p = (ids[String(p)] || {}).parent) if (String(p) === String(after.id)) return true; return false; };
        while (j < comps.length && isDesc(comps[j])) j++;
        comps.splice(j, 0, t);
        ids = byId();
        return;
      }
      const p = ids[String(a.parent_id)]; if (!p) return;
      const inner = p.inner_radius_mm || p.aft_shoulder_radius_mm || p.aft_radius_mm || 20;
      const common = { id, parent: p.id, depth: (p.depth || 0) + 1, name: a.name, label: a.name, mass_g: a.mass_g || 0,
        mass_measured: a.mass_g != null, internal: kind !== "fins", added: true, axial_method: "TOP", axial_offset_mm: a.offset_mm || 0 };
      let c;
      if (kind === "fins") {
        const pts = trapezoid(a.root_chord_mm, a.tip_chord_mm, a.span_mm, a.sweep_mm);
        const L = Math.max(a.root_chord_mm, a.sweep_mm + a.tip_chord_mm);
        c = { ...common, type: "TrapezoidFinSet", length_mm: L, fin_count: a.fin_count, base_rotation_deg: 0, cant_deg: 0,
          thickness_mm: a.thickness_mm, body_radius_mm: p.outer_radius_mm, points_mm: pts, span_mm: a.span_mm,
          root_chord_mm: a.root_chord_mm, tip_chord_mm: a.tip_chord_mm, sweep_mm: a.sweep_mm, fin_shape: "Trapezoid",
          cross_section: "Square", movable: false, _cg_local: finGeometry(pts).centroid_x };
        reshaped.add(id);
      } else if (kind === "bulkhead" || kind === "coupler" || kind === "centeringring") {
        const type = { bulkhead: "Bulkhead", coupler: "TubeCoupler", centeringring: "CenteringRing" }[kind];
        const mount = comps.find(x => x.motor_mount && String(x.parent) === String(p.id));
        c = { ...common, type, length_mm: a.length_mm, outer_radius_mm: inner,
          inner_radius_mm: kind === "bulkhead" ? 0 : kind === "coupler" ? inner - 0.8 : (mount ? mount.outer_radius_mm : 0),
          movable: false, _cg_local: a.length_mm / 2 };
      } else {
        const type = { mass: "MassComponent", parachute: "Parachute", shockcord: "ShockCord" }[kind];
        c = { ...common, type, length_mm: a.length_mm, radius_mm: Math.min((a.diameter_mm || 2 * inner) / 2, inner), radial_position_mm: 0,
          movable: true, _cg_local: a.length_mm / 2 };
        if (kind === "parachute") Object.assign(c, { canopy_diameter_mm: a.canopy_diameter_mm, cd: a.cd || 0.8 });
      }
      // children go after their parent's existing children
      let j = comps.indexOf(p) + 1;
      const isDesc = (x) => { for (let q = x.parent; q != null; q = (ids[String(q)] || {}).parent) if (String(q) === String(p.id)) return true; return false; };
      while (j < comps.length && isDesc(comps[j])) j++;
      comps.splice(j, 0, c);
      ids = byId();
    });

    // nose ballast
    if (plan.ballast_g > 0) {
      const nose = comps.find(c => c.type === "NoseCone");
      comps.splice(comps.indexOf(nose) + 1, 0, { id: "ballast", parent: nose.id, depth: 1, type: "MassComponent", name: "Ballast",
        label: `Nose ballast (${Math.round(plan.ballast_g)} g)`, length_mm: 20, mass_g: plan.ballast_g, radius_mm: 20, internal: true,
        movable: false, axial_method: "TOP", axial_offset_mm: nose.length_mm * NOSE_BALLAST_FRACTION, _cg_local: 10 });
      ids = byId();
    }

    // motor mount resize, as the worker does (openrocket.set_mount_diameter)
    const mount = comps.find(c => c.motor_mount);
    const m = d.motors[0];
    if (plan.motor && m && mount) {
      const e = plan.motor;
      const newLen = Math.max(mount.length_mm, e.length_mm + 5);
      if (Math.abs(2 * mount.inner_radius_mm - (e.diameter_mm + 0.1)) > 0.5) {
        mount.inner_radius_mm = (e.diameter_mm + 0.1) / 2; mount.outer_radius_mm = mount.inner_radius_mm + 0.8;
        for (const r of comps) if (r.type === "CenteringRing") r.inner_radius_mm = mount.outer_radius_mm;
      }
      if (newLen !== mount.length_mm) { mount._cg_local *= newLen / mount.length_mm; mount.length_mm = newLen; }
      Object.assign(m, { length_mm: e.length_mm, diameter_mm: e.diameter_mm, launch_mass_g: e.launch_mass_g,
        total_impulse_ns: e.total_impulse_ns, designation: `${e.manufacturer} ${e.designation}`, delay_s: plan.delay_s });
    }

    // lay out: stage parts one after another, children by their placement rule
    ids = byId();
    let cursor = 0;
    for (const c of comps) {
      const parent = c.parent == null ? null : ids[String(c.parent)];
      if (!parent) { c.x_mm = place(c, null, cursor); cursor = c.x_mm + c.length_mm; }
      else c.x_mm = place(c, parent, 0);
      if (c.offset_in_parent_mm != null || parent) c.offset_in_parent_mm = parent ? c.x_mm - parent.x_mm : undefined;
      c.cg_x_mm = c.x_mm + c._cg_local;
    }
    if (m && mount) {
      const aft = mount.x_mm + mount.length_mm + (mount.motor_overhang_mm || 0);
      m.x_mm = aft - m.length_mm; m.cg_x_mm = aft - m.length_mm / 2;
    }

    // mass and CG
    const structure = comps.reduce((a, c) => a + c.mass_g, 0);
    const moment = comps.reduce((a, c) => a + c.mass_g * c.cg_x_mm, 0);
    const motorMass = d.motors.reduce((a, x) => a + x.launch_mass_g, 0);
    let total = structure + motorMass;
    const cg = (moment + d.motors.reduce((a, x) => a + x.launch_mass_g * x.cg_x_mm, 0)) / total;
    if (plan.spread_g) total += plan.spread_g;          // the worker keeps the CG
    if (plan.total_g > 0) total = plan.total_g;

    // CP: OpenRocket's per-part numbers, shifted with the part; recomputed where the shape changed
    const airframe = comps.filter(c => !c.internal);
    const refR = Math.max(...airframe.map(c => Math.max(c.outer_radius_mm || 0, c.aft_radius_mm || 0, c.fore_radius_mm || 0)));
    const baseIds = Object.fromEntries(base.components.map(c => [String(c.id), c]));
    const baseRefR = base.summary.reference_diameter_mm / 2;
    const jsAero = (c, R) => c.type.endsWith("FinSet") ? finAero(c, R) : (c.type === "NoseCone" || c.type === "Transition") ? bodyAero(c, R) : null;
    const orAero = Object.fromEntries((base.aero || []).map(a => [String(a.id), a]));
    let sumN = 0, sumM = 0;
    for (const c of airframe) {
      const key = String(c.id), known = orAero[key];
      let cna, cpx;
      if (known && !reshaped.has(key) && Math.abs(refR - baseRefR) < 0.01) {
        cna = known.cna; cpx = known.cp_x_mm + (c.x_mm - baseX[key]);
      } else {
        const now = jsAero(c, refR); if (!now) continue;
        cna = now.cna; cpx = c.x_mm + now.cp_local;
        if (known) {   // small correction so an unchanged shape gives exactly OpenRocket's number
          const was = jsAero(baseIds[key], baseRefR);
          if (was && Math.abs(was.cna) > 1e-6) { cna *= known.cna / was.cna; cpx += known.cp_x_mm - (baseIds[key].x_mm + was.cp_local); }
        }
      }
      sumN += cna; sumM += cna * cpx;
    }
    const cp = sumN > 0 ? sumM / sumN : base.summary.cp_mm;
    const airframeLen = Math.max(...airframe.map(c => c.x_mm + c.length_mm));
    const length = Math.max(airframeLen, ...d.motors.map(x => x.x_mm + x.length_mm));
    const S = d.summary;
    Object.assign(S, {
      length_mm: round(length, 2), airframe_length_mm: round(airframeLen, 2), max_diameter_mm: round(2 * refR, 2), reference_diameter_mm: round(2 * refR, 2),
      mass_no_motor_g: round(total - motorMass, 1), mass_with_motor_g: round(total, 1), cg_mm: round(cg, 2), cp_mm: round(cp, 2),
      stability_cal: round((cp - cg) / (2 * refR), 2), stability_pct: round((cp - cg) / airframeLen * 100, 1),
    });
    for (const c of comps) delete c._cg_local;
    d.components = comps;
    d.estimate = true; d.flight = null; d.changes = null;
    return d;
  }
  const round = (v, n) => Math.round(v * 10 ** n) / 10 ** n;

  const api = { preview, profileRadius, finGeometry, finShape, finAero, bodyAero, trapezoid, NOSE_BALLAST_FRACTION };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RocketBuilder = api;
})(typeof window !== "undefined" ? window : globalThis);
