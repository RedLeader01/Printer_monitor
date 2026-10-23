"use strict";

// Az Előzmények oldal: a /api/history végpontról olvassa az epizódokat (nem elérhető, hiba,
// kevés kellékanyag, a figyelő leállása), és táblázatban mutatja kezdettel, véggel és időtartammal.

const REFRESH_MS = 30000;

const KINDS = {
  offline: { label: "Nem elérhető", cls: "k-offline" },
  alert: { label: "Hiba / figyelmeztetés", cls: "k-alert" },
  supply_low: { label: "Kevés kellékanyag", cls: "k-low" },
  gap: { label: "A figyelő nem futott", cls: "k-gap" },
};

// Kis segédfüggvény elemek építéséhez (a szövegek textNode-ként kerülnek be, nem HTML-ként).
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === false || v === null || v === undefined) continue;
      if (k === "class") e.className = v;
      else e.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

function fmtDuration(sec) {
  if (sec < 60) return "kevesebb mint 1 perc";
  const d = Math.floor(sec / 86400);
  const hrs = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d} nap ${hrs} óra`;
  if (hrs) return `${hrs} óra ${m} perc`;
  return `${m} perc`;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString("hu-HU");
}

const printerSelect = document.getElementById("printer");
const daysSelect = document.getElementById("days");
const kindSelect = document.getElementById("kind");
const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const appEl = document.getElementById("app");

// A törölt nyomtatók neve is a listában marad (az időszakon belüli eseményeik miatt), "(törölve)" jelzéssel.
let removedSet = new Set();

function fillPrinters(names, removed) {
  removedSet = new Set(removed || []);
  const known = new Set([...printerSelect.options].map((o) => o.value));
  for (const n of names) {
    if (!known.has(n)) printerSelect.append(h("option", { value: n }, n));
  }
  for (const o of printerSelect.options) {
    if (o.value) o.textContent = o.value + (removedSet.has(o.value) ? " (törölve)" : "");
  }
}

function renderRow(e, now) {
  const kind = KINDS[e.kind] || { label: e.kind, cls: "k-gap" };
  const running = e.ended === null || e.ended === undefined;
  const duration = fmtDuration((running ? now : e.ended) - e.started);
  return h("tr", { class: running ? "open" : null },
    h("td", null, fmtTime(e.started)),
    h("td", null, e.printer === "*" ? "Minden nyomtató" : e.printer,
      removedSet.has(e.printer) ? h("span", { class: "removed" }, " (törölve)") : null),
    h("td", null, h("span", { class: "badge " + kind.cls }, kind.label)),
    h("td", null, e.detail),
    h("td", null, running ? "tart még" : fmtTime(e.ended)),
    h("td", null, running ? `${duration} (tart még)` : duration));
}

function render(data) {
  fillPrinters(data.printers || [], data.removed || []);
  appEl.textContent = "";
  if (!data.enabled) {
    summaryEl.textContent = "";
    appEl.append(h("p", null,
      "Az előzmények mentése ki van kapcsolva. A config.ini [history] szakaszában az ",
      h("b", null, "enabled = yes"), " beállítással kapcsolható be."));
    return;
  }
  const all = data.episodes || [];
  const eps = kindSelect.value ? all.filter((e) => e.kind === kindSelect.value) : all;
  const running = eps.filter((e) => e.ended === null || e.ended === undefined).length;
  summaryEl.textContent = eps.length
    ? `${eps.length} esemény` + (running ? `, ebből ${running} még tart.` : ".")
    : "";
  if (!eps.length) {
    appEl.append(h("p", null, all.length
      ? "Ebből a típusból nem volt esemény a kiválasztott időszakban."
      : "Ebben az időszakban nem volt rögzített esemény."));
    return;
  }
  const head = h("tr", null, ["Kezdete", "Nyomtató", "Esemény", "Részlet", "Vége", "Időtartam"]
    .map((t) => h("th", null, t)));
  appEl.append(h("div", { class: "tablewrap" },
    h("table", { class: "hist" }, h("thead", null, head),
      h("tbody", null, eps.map((e) => renderRow(e, data.now))))));
}

let loadSeq = 0;
let lastData = null;

async function load() {
  const seq = ++loadSeq;               // a lassabb, régebbi válasz ne írja felül az újabbat
  const params = new URLSearchParams({ days: daysSelect.value });
  if (printerSelect.value) params.set("printer", printerSelect.value);
  try {
    const r = await fetch("api/history?" + params.toString(), { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (seq !== loadSeq) return;
    lastData = data;
    render(data);
    statusEl.className = "status";
    statusEl.textContent = "Frissítve: " + new Date().toLocaleTimeString("hu-HU");
  } catch (e) {
    if (seq !== loadSeq) return;
    statusEl.className = "status err";
    statusEl.textContent = "A figyelő nem érhető el (" + e.message + ") – az adatok elavultak lehetnek.";
  }
}

// ?printer=NÉV&days=30 a címsorban előre beállítja a szűrőket.
const initial = new URLSearchParams(window.location.search);
if (initial.get("days") && [...daysSelect.options].some((o) => o.value === initial.get("days"))) {
  daysSelect.value = initial.get("days");
}
if (initial.get("kind") && [...kindSelect.options].some((o) => o.value === initial.get("kind"))) {
  kindSelect.value = initial.get("kind");
}
if (initial.get("printer")) {
  printerSelect.append(h("option", { value: initial.get("printer") }, initial.get("printer")));
  printerSelect.value = initial.get("printer");
}

printerSelect.addEventListener("change", load);
daysSelect.addEventListener("change", load);
kindSelect.addEventListener("change", () => { if (lastData) render(lastData); });   // csak szűrés, nincs új lekérés
load();
setInterval(load, REFRESH_MS);
