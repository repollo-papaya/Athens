// Firestore side of the builder: sign in, launch conditions, simulation requests and the design chat.
import { firebaseConfig } from "../firebase-config.js";
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import { getAuth, signInAnonymously, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";
import { getFirestore, collection, addDoc, getDocsFromServer, getDocFromServer, query, where, limit, orderBy, serverTimestamp, onSnapshot,
  doc, updateDoc, deleteDoc }
  from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";

const $ = (id) => document.getElementById(id);
const STANDARD = { elevation_ft: 3740, wind_mph: 5, humidity_pct: 25, temperature_f: 70 };
const fmt = (n, d = 1) => Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
const db = getFirestore(initializeApp(firebaseConfig));
const auth = getAuth();
let user = null, conditions = [], stopWatching = null, waitTimer = null;
const B = () => window.builder;

function setStatus(state, label, note) {
  const pill = $("run-status");
  pill.hidden = !label; pill.className = "pill " + (state || ""); pill.textContent = label || "";
  $("run-note").textContent = note || "";
}
const weather = () => { const c = conditions.find(x => x.id === $("conditions").value); return { weather: c ? { ...c.weather } : { ...STANDARD }, id: c && c.id }; };

async function loadConditions() {
  const sel = $("conditions");
  sel.replaceChildren();
  try {
    const snap = await getDocsFromServer(query(collection(db, "launch_conditions"), where("uid", "==", user.uid), limit(50)));
    conditions = snap.docs.map(d => ({ id: d.id, ...d.data() }))
      .sort((a, b) => (b.created_at?.toMillis?.() ?? 0) - (a.created_at?.toMillis?.() ?? 0)).slice(0, 15);
  } catch { conditions = []; }
  for (const c of conditions) {
    const w = c.weather, o = document.createElement("option");
    const when = c.created_at?.toDate ? c.created_at.toDate().toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
    o.value = c.id; o.textContent = `${when} · ${fmt(w.elevation_ft, 0)} ft, ${fmt(w.wind_mph)} mph, ${fmt(w.humidity_pct, 0)}%, ${fmt(w.temperature_f, 0)} °F`;
    sel.append(o);
  }
  const std = document.createElement("option");
  std.value = "standard";
  std.textContent = `El Paso standard day · ${fmt(STANDARD.elevation_ft, 0)} ft, ${STANDARD.wind_mph} mph, ${STANDARD.humidity_pct}%, ${STANDARD.temperature_f} °F`;
  sel.append(std);
}

async function simulate() {
  const ready = B().canSimulate();
  if (!ready.ok) { B().openTab("fly"); setStatus("failed", "Not ready", ready.why); return; }
  const sig = B().specJson();
  const { weather: w, id } = weather();
  if (stopWatching) stopWatching();
  clearTimeout(waitTimer);
  B().setCloudReady(false);
  setStatus("", "Sending", "");
  try {
    const ref = await addDoc(collection(db, "sim_jobs"), {
      schema_version: 1, uid: user.uid, created_at: serverTimestamp(), status: "pending", rocket: "ederance",
      weather: w, target_apogee_ft: 800, spec_json: sig, ...(id ? { conditions_id: id } : {}),
    });
    setStatus("", "Queued", "");
    waitTimer = setTimeout(() => $("run-note").textContent = "Waiting for a simulation worker. If this stays queued, no worker is running (python sim/worker.py).", 20000);
    stopWatching = onSnapshot(ref, (snap) => {
      const job = snap.data(); if (!job) return;
      if (job.status !== "pending") clearTimeout(waitTimer);
      if (job.status === "running") setStatus("running", "Running", "OpenRocket is flying your rocket…");
      if (job.status === "failed") { setStatus("failed", "Failed", job.error || "No reason given."); B().setCloudReady(true); }
      if (job.status === "done") {
        B().setCloudReady(true);
        const r = job.result || {};
        if (!r.design_json) { setStatus("failed", "Old worker", "The worker answered without a design. Restart it so it runs the latest code."); return; }
        B().applySimulation(sig, JSON.parse(r.design_json));
        setStatus("done", "Done", `${fmt(r.apogee_ft, 0)} ft · ${fmt(r.duration_s)} s · score ${fmt(r.score?.total)}`);
      }
    }, (err) => { setStatus("failed", "Error", err.message); B().setCloudReady(true); });
  } catch (err) {
    B().setCloudReady(true);
    setStatus("failed", "Not sent", err.code === "permission-denied"
      ? "The database refused the request. Publish the latest firestore.rules (they now allow rockets built in the app)." : err.message);
  }
  if (!$("pane-fly").hidden || true) { /* results show on the Fly tab and in the status bar */ }
}
$("run").addEventListener("click", simulate);
$("d-sim").addEventListener("click", () => { B().openTab("fly"); simulate(); });
addEventListener("builder-simulate", () => { B().openTab("fly"); simulate(); });

// ----- Design chat -----
let lastChatId = null, chatWatch = null;
function el(tag, cls, ...kids) { const e = document.createElement(tag); if (cls) e.className = cls; for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(k == null ? "" : String(k))); return e; }

function candidateCard(c) {
  const card = el("div", "cand" + (c.recommended ? " rec" : ""));
  const top = el("div", "top2", el("span", "nm", c.name));
  if (c.recommended) top.append(el("span", "badge-rec", "Recommended"));
  card.append(top);
  if (c.idea) card.append(el("div", "idea", c.idea));
  if (c.error) { card.append(el("div", "fail", "Could not be built: " + c.error)); return card; }
  const m = c.metrics;
  card.append(el("div", "nums", `${B().alt(m.apogee_ft)} · ${fmt(m.duration_s)} s · score ${fmt(m.score)} · ${fmt(m.liftoff_mass_g, 0)} g · ${fmt(m.stability_cal, 2)} cal · ${m.motor}`));
  const failed = (c.rules || []).filter(r => !r.ok).map(r => r.rule);
  if (failed.length) card.append(el("div", "fail", "Fails: " + failed.join(", ")));
  if (c.verdict) card.append(el("div", "verdict", c.verdict));
  const open = el("button", "btn primary", "Open in builder"); open.type = "button";
  open.addEventListener("click", () => B().openDesign(JSON.parse(c.design_json), { name: c.name, motor: c.changes && c.changes.motor, delay_s: c.changes && c.changes.delay_s }));
  card.append(el("div", "actions", open));
  return card;
}
function renderAnswer(box, job) {
  box.replaceChildren();
  if (job.status === "failed") { box.append(el("p", "fail", job.error || "Something went wrong.")); return; }
  if (job.status !== "done") { box.append(el("div", "stage", el("span", "spinner"), job.status === "pending" ? "Waiting for the simulation worker…" : (job.stage || "Working…"))); return; }
  const r = job.result || {};
  if (r.headline) box.append(el("h3", "", r.headline));
  if (r.explanation) box.append(el("p", "", r.explanation));
  const b = r.baseline && r.baseline.metrics;
  if (b) box.append(el("p", "note", `The team rocket as it is: ${B().alt(b.apogee_ft)}, ${fmt(b.duration_s)} s, score ${fmt(b.score)}.`));
  const list = el("div", "cands");
  (r.candidates || []).forEach(c => list.append(candidateCard(c)));
  box.append(list);
  if ((r.next_steps || []).length) { const ul = el("ul"); for (const n of r.next_steps) ul.append(el("li", "", n)); box.append(el("strong", "", "Next steps"), ul); }
  box.append(el("p", "note", `${(r.candidates || []).length} designs simulated in ${fmt(r.seconds, 0)} s. Every number comes from OpenRocket with your flight calibration.`));
}
async function sendChat(text) {
  text = text.trim();
  if (!text || !user) return;
  const { weather: w, id } = weather();
  $("chat-log").append(el("div", "bubble me", text));
  const box = el("div", "bubble ai"); $("chat-log").append(box);
  renderAnswer(box, { status: "pending" });
  $("chat-input").value = ""; $("chat-send").disabled = true; $("chat-new").hidden = false;
  try {
    const ref = await addDoc(collection(db, "design_chats"), {
      schema_version: 1, uid: user.uid, created_at: serverTimestamp(), status: "pending", rocket: "ederance", weather: w,
      message: text.slice(0, 1000), target_apogee_ft: 800, ...(id ? { conditions_id: id } : {}), ...(lastChatId ? { previous_id: lastChatId } : {}),
    });
    if (chatWatch) chatWatch();
    chatWatch = onSnapshot(ref, (snap) => {
      const job = snap.data(); if (!job) return;
      renderAnswer(box, job);
      if (job.status === "done" || job.status === "failed") { $("chat-send").disabled = false; if (job.status === "done") lastChatId = ref.id; }
    }, (err) => { box.replaceChildren(el("p", "fail", err.message)); $("chat-send").disabled = false; });
  } catch (err) {
    box.replaceChildren(el("p", "fail", err.code === "permission-denied" ? "The database refused the request. Publish the latest firestore.rules." : err.message));
    $("chat-send").disabled = false;
  }
}
$("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("chat-input").value); });
document.querySelectorAll("#chat-examples button").forEach(b => b.addEventListener("click", () => { $("chat-input").value = b.textContent; $("chat-input").focus(); }));
$("chat-new").addEventListener("click", () => { lastChatId = null; if (chatWatch) chatWatch(); $("chat-log").replaceChildren(); $("chat-new").hidden = true; });

// ----- Saved designs (collection "designs"; the whole team can see them) -----
const lib = { open: false, items: [] };
const when = (ts) => ts?.toDate ? ts.toDate().toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
function libNote(text) { $("library-list").replaceChildren(el("li", "note", text)); }

async function refreshLibrary() {
  if (!user) return;
  try {
    const snap = await getDocsFromServer(query(collection(db, "designs"), orderBy("updated_at", "desc"), limit(60)));
    lib.items = snap.docs.map(d => ({ id: d.id, ...d.data() }));
  } catch (err) {
    libNote(err.code === "permission-denied" ? "The database refused to list designs. Publish the latest firestore.rules (they add saved designs)." : err.message);
    return;
  }
  renderLibrary();
}
function renderLibrary() {
  const cur = B().link().id;
  if (!lib.items.length) { libNote("No saved designs yet. Build a rocket and tap Save."); return; }
  $("library-list").replaceChildren(...lib.items.map(d => {
    const mine = d.uid === user.uid, s = d.summary || {};
    const li = el("li", "item" + (d.id === cur ? " current" : ""));
    const nm = el("div", "nm", d.name || "Untitled");
    nm.append(el("span", "who", mine ? "yours" : "team"));
    if (d.id === cur) nm.append(el("span", "who", "open"));
    li.append(nm, el("span", "note", when(d.updated_at)));
    const bits = [`${s.parts ?? "?"} parts`, `${fmt(s.mass_g ?? 0, 0)} g`, `${fmt(s.stability_cal ?? 0, 2)} cal`, B().len(s.length_mm || 0)];
    if (s.apogee_ft) bits.push(`${B().alt(s.apogee_ft)}, ${fmt(s.duration_s)} s`);
    li.append(el("div", "meta", bits.join(" · ")));
    const acts = el("div", "actions");
    const open = el("button", "btn primary", "Open"); open.type = "button";
    open.addEventListener("click", async () => {
      if (B().dirty() && !confirm("Open this design? The rocket on screen has unsaved changes (Undo can bring it back).")) return;
      try { const snap = await getDocFromServer(doc(db, "designs", d.id)); B().loadSaved(d.id, mine, snap.data()); closeLibrary(); }
      catch (err) { alert("Could not open it: " + err.message); }
    });
    acts.append(open);
    if (mine) {
      const del = el("button", "btn danger", "Delete"); del.type = "button";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete "${d.name}" for the whole team? This can't be undone.`)) return;
        try { await deleteDoc(doc(db, "designs", d.id)); if (B().link().id === d.id) B().unlink(); refreshLibrary(); }
        catch (err) { alert("Could not delete it: " + err.message); }
      });
      acts.append(del);
    }
    li.append(acts);
    return li;
  }));
}
function openLibrary() { $("library").hidden = false; $("library-btn").setAttribute("aria-expanded", "true"); libNote("Loading…"); refreshLibrary(); $("library").scrollIntoView({ behavior: "smooth", block: "start" }); }
function closeLibrary() { $("library").hidden = true; $("library-btn").setAttribute("aria-expanded", "false"); }
$("library-btn").addEventListener("click", () => $("library").hidden ? openLibrary() : closeLibrary());
$("library-close").addEventListener("click", closeLibrary);

async function saveDesign(asNew) {
  const b = B(), link = b.link();
  if (asNew && link.id) b.rename((b.rocketName() + " (copy)").slice(0, 60));
  const data = b.forSave();
  if (!JSON.parse(data.spec_json).components.length) { alert("Add some parts before saving."); return; }
  $("save").disabled = true; $("save").textContent = "Saving…";
  try {
    if (link.id && link.own && !asNew) {
      await updateDoc(doc(db, "designs", link.id), { name: data.name, spec_json: data.spec_json, summary: data.summary, updated_at: serverTimestamp() });
      b.markSaved(link.id, data.sig);
    } else {
      const ref = await addDoc(collection(db, "designs"), { schema_version: 1, uid: user.uid, name: data.name,
        spec_json: data.spec_json, summary: data.summary, created_at: serverTimestamp(), updated_at: serverTimestamp() });
      b.markSaved(ref.id, data.sig);
    }
    if (!$("library").hidden) refreshLibrary();
  } catch (err) {
    alert(err.code === "permission-denied" ? "The database refused to save. Publish the latest firestore.rules (they add saved designs)." : "Could not save: " + err.message);
  } finally { $("save").textContent = "Save"; $("save").disabled = false; }
}
$("save").addEventListener("click", () => saveDesign(false));
$("save-copy").addEventListener("click", () => saveDesign(true));

// ----- Automatic design runs (collection "auto_runs", run by sim/autodesign.py on the worker) -----
let autoWatch = null;
function autoStatus(state, label, note) {
  const pill = $("auto-status"); pill.hidden = !label; pill.className = "pill " + (state || ""); pill.textContent = label || "";
  $("auto-note").textContent = note || "";
}
function renderAuto(r) {
  $("auto-results").hidden = false;
  $("auto-summary").textContent = `${r.designs.length} designs that reach ${B().alt(r.target_apogee_ft)} from ${r.flights} OpenRocket flights in ${fmt(r.seconds, 0)} s. Fin shapes and tube lengths were varied too. Best first: score plus what a wind forecast off by 2½ mph would cost, then weight margin.`;
  $("auto-list").replaceChildren(...r.designs.map(d => {
    const m = d.metrics;
    const card = el("div", "cand" + (d.rank === 1 ? " rec" : ""));
    const top = el("div", "top2", el("span", "nm", `${d.rank}. ${d.motor}`));
    if (d.rank === 1) top.append(el("span", "badge-rec", "Best"));
    card.append(top);
    card.append(el("div", "nums", `${B().alt(m.apogee_ft)} · ${fmt(m.duration_s)} s · score ${fmt(m.score)} · ${fmt(m.mass_g, 0)} g · ${fmt(m.stability_cal, 2)} cal`));
    if (m.fin_shape) card.append(el("div", "idea", `${m.fin_count || 3} ${m.fin_shape.toLowerCase()} fins, ${B().len(m.fin_span_mm)} tall · ` +
      `${m.tube_layout}: BT-80 ${B().len(m.bt80_mm)}, BT-70 ${B().len(m.bt70_mm)}`));
    card.append(el("div", "idea", `${fmt(m.ballast_g, 0)} g nose ballast · ${fmt(m.chute_in)} in parachute · ` + (m.fin_shape ? "" : `fins ${B().len(m.fin_span_mm)} tall · `) +
      (m.wind_spread_ft != null ? `apogee moves ${fmt(m.wind_spread_ft, 0)} ft between calm and 10 mph wind` : "wind not checked") +
      ` · ${d.reloadable ? "reloadable motor (adult purchase)" : "single-use motor"}`));
    const open = el("button", "btn primary", "Open in builder"); open.type = "button";
    open.addEventListener("click", () => {
      if (B().dirty() && !confirm("Open this design? The rocket on screen has unsaved changes (Undo can bring it back).")) return;
      B().openSpec(`Auto: ${d.motor}`, d.spec_json);
    });
    const keep = el("button", "btn", "Save to My designs"); keep.type = "button";
    keep.addEventListener("click", async () => {
      if (B().dirty() && !confirm("Save this design? It opens in the builder and replaces the rocket on screen (Undo can bring it back).")) return;
      B().openSpec(`Auto: ${d.motor}`, d.spec_json);
      await saveDesign(false);
    });
    card.append(el("div", "actions", open, keep));
    return card;
  }));
  $("auto-screened").textContent = (r.screened || []).map(x => `${x.motor} ${x.ok ? "✓" : "✗"} ${x.apogee_ft} ft`).join(" · ");
  $("auto-assume").textContent = "Assumed: " + (r.assumptions || []).join("; ") + ".";
}
async function startAuto() {
  const target = Number($("auto-target").value), egg = Number($("auto-egg").value), prot = Number($("auto-protector").value), alt = Number($("auto-alt").value);
  if (!(target >= 700 && target <= 900)) return autoStatus("failed", "Check inputs", "Target apogee must be 700 to 900 ft.");
  if (!(egg >= 40 && egg <= 80) || !(prot >= 0 && prot <= 80) || !(alt >= 0 && alt <= 60)) return autoStatus("failed", "Check inputs", "Egg 40–80 g, protector 0–80 g, altimeter 0–60 g.");
  const { weather: w, id } = weather();
  if (autoWatch) autoWatch();
  $("auto-start").disabled = true;
  autoStatus("", "Sending", "");
  try {
    const ref = await addDoc(collection(db, "auto_runs"), {
      schema_version: 1, uid: user.uid, created_at: serverTimestamp(), status: "pending", weather: w, target_apogee_ft: target,
      budget_s: Number($("auto-budget").value), payload: { egg_g: egg, protector_g: prot, altimeter_g: alt }, ...(id ? { conditions_id: id } : {}),
    });
    autoStatus("", "Queued", "Waiting for the simulation worker…");
    autoWatch = onSnapshot(ref, (snap) => {
      const job = snap.data(); if (!job) return;
      if (job.status === "running") autoStatus("running", "Designing", job.stage || "Working…");
      if (job.status === "failed") { autoStatus("failed", "Failed", job.error || "No reason given."); $("auto-start").disabled = false; }
      if (job.status === "done") { autoStatus("done", "Done", ""); $("auto-start").disabled = false; renderAuto(job.result); }
    }, (err) => { autoStatus("failed", "Error", err.message); $("auto-start").disabled = false; });
  } catch (err) {
    $("auto-start").disabled = false;
    autoStatus("failed", "Not sent", err.code === "permission-denied" ? "The database refused the request. Publish the latest firestore.rules (they add automatic design runs)." : err.message);
  }
}
$("auto-start").addEventListener("click", startAuto);

function ready() {
  onAuthStateChanged(auth, async (u) => {
    if (!u) return;
    user = u;
    await loadConditions();
    B().setCloudReady(true);
    $("chat-send").disabled = false;
    $("save").disabled = false; $("save-copy").disabled = false; $("auto-start").disabled = false;
  });
  signInAnonymously(auth).catch((err) => setStatus("failed", "Offline", "Could not sign in: " + (err.code || err.message)));
}
if (window.builder) ready(); else addEventListener("builder-ready", ready, { once: true });
