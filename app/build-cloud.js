// Firestore side of the builder: sign in, launch conditions, simulation requests and the design chat.
import { firebaseConfig } from "../firebase-config.js";
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import { getAuth, signInAnonymously, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js";
import { getFirestore, collection, addDoc, getDocsFromServer, query, where, limit, serverTimestamp, onSnapshot }
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

function ready() {
  onAuthStateChanged(auth, async (u) => {
    if (!u) return;
    user = u;
    await loadConditions();
    B().setCloudReady(true);
    $("chat-send").disabled = false;
  });
  signInAnonymously(auth).catch((err) => setStatus("failed", "Offline", "Could not sign in: " + (err.code || err.message)));
}
if (window.builder) ready(); else addEventListener("builder-ready", ready, { once: true });
