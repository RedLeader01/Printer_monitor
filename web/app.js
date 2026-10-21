"use strict";

// Az oldal a /api végpontról olvassa az adatokat, és kirajzolja a kártyákat.
// A nyitott "Részletek" menük újrarajzolás után is nyitva maradnak.

const REFRESH_MS = 15000;
const openCards = new Set();
let settings = { warn_pct: 20, crit_pct: 10 };

// Kis segédfüggvény elemek építéséhez (a szövegek textNode-ként kerülnek be, nem HTML-ként).
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === false || v === null || v === undefined) continue;
      if (k === "class") e.className = v;
      else if (k === "style") e.style.cssText = v;
      else e.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const hrs = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d} nap ${hrs} óra`;
  if (hrs) return `${hrs} óra ${m} perc`;
  return `${m} perc`;
}

function level(pct) {
  if (pct < settings.crit_pct) return "crit";
  if (pct < settings.warn_pct) return "warn";
  return "ok";
}

function trayLine(t) {
  return h("div", { class: t.warn ? "warnt" : null }, `${t.name}: ${t.state}`);
}

function infoTable(p) {
  const i = p.info || {};
  const rows = [];
  const add = (label, content) => {
    if (content) rows.push(h("tr", null, h("th", null, label), h("td", null, content)));
  };
  add("Típus", i.model);
  add("Sorozatszám", i.serial);
  if (i.uptime !== null && i.uptime !== undefined) add("Üzemidő", fmtUptime(i.uptime));
  if (i.inputs && i.inputs.length) add("Papírtálcák", i.inputs.map(trayLine));
  if (i.outputs && i.outputs.length) add("Kimeneti tálca", i.outputs.map(trayLine));
  const withPn = (p.supplies || []).filter((s) => s.pn);
  if (withPn.length) add("Cikkszámok", withPn.map((s) => h("div", null, `${s.name}: ${s.pn}`)));
  if (i.descr) add("Rendszerleírás", h("span", { class: "descr" }, i.descr));
  if (!rows.length) return h("p", null, "Nincs elérhető extra adat.");
  return h("table", { class: "info" }, h("tbody", null, rows));
}

function renderCard(p) {
  const head = h("h2", null, p.name, " ", h("small", null, p.ip));
  if (p.pending) {
    return h("div", { class: "c" }, head, h("p", null, "lekérdezés folyamatban…"));
  }
  if (!p.online) {
    return h("div", { class: "c bad" }, head, h("p", null, h("b", null, "OFFLINE"), " – ", p.error || ""));
  }

  const critical = p.alerts.some((a) => a.sev === "KRITIKUS");
  const low = p.supplies.some((s) => s.pct !== null && s.pct < settings.warn_pct);
  const cls = "c" + (critical ? " bad" : p.alerts.length || low ? " warn" : "");

  const rows = p.supplies.map((s) => {
    if (s.pct === null) {
      return h("div", { class: "row" }, h("label", null, s.name), h("span", null, s.note));
    }
    const w = Math.max(0, Math.min(100, s.pct));
    return h("div", { class: "row" },
      h("label", null, s.name),
      h("div", { class: "bar" }, h("div", { class: "fill " + level(s.pct), style: `width:${w}%` })),
      h("span", null, `${s.pct}%`));
  });
  const alerts = p.alerts.map((a) => h("div", { class: "al" }, `${a.sev}: ${a.text}`));

  const details = h("details", { open: openCards.has(p.name) },
    h("summary", null, "Részletek"), infoTable(p));
  details.addEventListener("toggle", () => {
    if (details.open) openCards.add(p.name);
    else openCards.delete(p.name);
  });

  return h("div", { class: cls }, head,
    h("p", null, `Állapot: ${p.status} · frissítve ${p.time}`),
    rows, alerts, details);
}

function renderOrderBox(printers) {
  const lines = [];
  for (const p of printers) {
    if (!p.online) continue;
    for (const s of p.supplies) {
      if (s.pct !== null && s.pct < settings.warn_pct) {
        lines.push(h("div", null,
          `${p.name} – ${s.name} (${s.pct}%)`,
          s.pn ? [" · cikkszám: ", h("b", null, s.pn)] : null));
      }
    }
  }
  if (!lines.length) return null;
  return h("div", { class: "order" }, h("b", null, "Hamarosan elfogy:"), lines);
}

function render(data) {
  const app = document.getElementById("app");
  const printers = data.printers || [];
  const box = renderOrderBox(printers);
  const grid = h("div", { class: "g" }, printers.map(renderCard));
  app.textContent = "";
  if (box) app.append(box);
  app.append(grid);
}

async function refresh() {
  const status = document.getElementById("status");
  try {
    const r = await fetch("api", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    settings = data.settings || settings;
    render(data);
    status.className = "status";
    status.textContent = "Frissítve: " + new Date().toLocaleTimeString("hu-HU");
  } catch (e) {
    status.className = "status err";
    status.textContent = "A figyelő nem érhető el (" + e.message + ") – az adatok elavultak lehetnek.";
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
