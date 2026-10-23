"use strict";

// Az oldal a /api végpontról olvassa az adatokat, és kirajzolja a kártyákat.
// A nyitott "Részletek" menük újrarajzolás után is nyitva maradnak.
// Adminként (a futtató gépen, vagy belépve) nyomtatót lehet hozzáadni és törölni.

const REFRESH_MS = 15000;
const openCards = new Set();
let settings = { warn_pct: 20, crit_pct: 10 };

// Ki vagyok: a /api/me válasza. A csrf a módosító kérésekhez kell (lásd api()).
let me = { manage: false, admin: false, kind: "anon", user: null, csrf: null };
let adminMode = null;          // null | "add" | "login": melyik űrlap van nyitva
let lastMessage = null;        // { kind: "ok" | "err", text }
let msgEl = null;

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

// ----------------------------------------------------------------- szerver hívások
// Minden módosító kérés JSON, és viszi a CSRF-jelzést (a /api/me adja). A válasz mindig
// { ok, status, data, error } alakú, hálózati hiba esetén is.
async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (me.csrf) headers["X-CSRF-Token"] = me.csrf;
  const opt = { method, headers, cache: "no-store", credentials: "same-origin" };
  if (body !== undefined) opt.body = JSON.stringify(body);
  let r;
  try {
    r = await fetch(path, opt);
  } catch (e) {
    return { ok: false, status: 0, data: {}, error: "A figyelő nem érhető el." };
  }
  let data = {};
  try { data = await r.json(); } catch (e) { /* nem JSON válasz */ }
  return { ok: r.ok, status: r.status, data, error: data.error };
}

function handleApiError(r) {
  setMessage("err", r.error || "A művelet nem sikerült.");
  if (r.status === 401 || r.status === 403) refreshMe();      // pl. lejárt a belépés
}

// ------------------------------------------------------------------ admin sáv
function setMessage(kind, text) {
  lastMessage = text ? { kind, text } : null;
  paintMessage();
}

function paintMessage() {
  if (!msgEl) return;
  msgEl.className = "msg" + (lastMessage ? " " + lastMessage.kind : " hidden");
  msgEl.textContent = lastMessage ? lastMessage.text : "";
}

function addForm() {
  const name = h("input", { type: "text", name: "name", placeholder: "pl. HAT-KO-4", maxlength: "40",
                            required: true, autocomplete: "off" });
  const ip = h("input", { type: "text", name: "ip", placeholder: "pl. 10.56.63.4", maxlength: "253",
                          required: true, autocomplete: "off" });
  const btn = h("button", { type: "submit", class: "btn primary" }, "Hozzáadás");
  const form = h("form", { class: "adminform" },
    h("label", null, "Név", name), h("label", null, "IP-cím", ip), btn);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    btn.disabled = true;
    const r = await api("POST", "api/printers", { name: name.value, ip: ip.value });
    btn.disabled = false;
    if (r.ok) {
      setMessage("ok", `Hozzáadva: ${r.data.printer.name} (${r.data.printer.ip})`);
      name.value = "";
      ip.value = "";
      name.focus();           // a következő nyomtató felvétele azonnal mehet
      refresh();
    } else {
      handleApiError(r);
    }
  });
  return form;
}

function bulkForm() {
  const ta = h("textarea", { name: "text", rows: "8", spellcheck: "false", required: true,
    placeholder: "Soronként egy nyomtató, pl.:\nHAT-KO-1 = 10.56.63.1\nHAT-KO-2 = 10.56.63.2\n(Excelből bemásolva is jó: név, tab, IP)" });
  const btn = h("button", { type: "submit", class: "btn primary" }, "Mind felvétele");
  const form = h("form", { class: "adminform bulk" }, h("label", null, "Nyomtatók (Név = IP-cím)", ta), btn);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    btn.disabled = true;
    const r = await api("POST", "api/printers/bulk", { text: ta.value });
    btn.disabled = false;
    if (!r.ok && !r.data.errors) { handleApiError(r); return; }
    const n = (r.data.added || []).length;
    const errs = r.data.errors || [];
    if (errs.length) {
      setMessage("err", `${n} nyomtató felvéve, ${errs.length} sor kimaradt:\n` +
        errs.map((e) => `${e.line}. sor: ${e.error}`).join("\n"));
      // csak a hibás sorok maradnak a mezőben, a javítás után újra elküldhetők
      const bad = new Set(errs.map((e) => e.line));
      ta.value = ta.value.split(/\r\n|[\n\r]/).filter((_, i) => bad.has(i + 1)).join("\n");
    } else {
      setMessage("ok", `${n} nyomtató felvéve.`);
      ta.value = "";
    }
    refresh();
  });
  return form;
}

function loginForm() {
  const user = h("input", { type: "text", name: "username", required: true, autocomplete: "username" });
  const pass = h("input", { type: "password", name: "password", required: true, autocomplete: "current-password" });
  const btn = h("button", { type: "submit", class: "btn primary" }, "Belépés");
  const form = h("form", { class: "adminform" },
    h("label", null, "Felhasználónév", user), h("label", null, "Jelszó", pass), btn);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    btn.disabled = true;
    const r = await api("POST", "api/login", { username: user.value, password: pass.value });
    btn.disabled = false;
    pass.value = "";
    if (r.ok) {
      adminMode = "add";                       // belépés után rögtön a nyomtatók felvétele jön
      lastMessage = { kind: "ok", text: "Beléptél, mostantól nem kell újra jelszót írnod." };
      await refreshMe(true);
      refresh();
    } else {
      handleApiError(r);
    }
  });
  const parts = [form];
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  if (window.location.protocol === "http:" && !local) {
    parts.push(h("p", { class: "warnnote" },
      "Figyelem: a kapcsolat nem titkosított (HTTP), ezért a jelszó a hálózaton olvasható. " +
      "Csak megbízható belső hálózaton használd."));
  }
  parts.push(h("p", { class: "hint" },
    "Admin fiókot a futtató gépen lehet létrehozni: python run.py admin set NÉV"));
  return h("div", null, parts);
}

function renderAdminBar() {
  const box = document.getElementById("admin");
  box.textContent = "";
  msgEl = null;
  if (!me.manage) return;
  const row = h("div", { class: "adminrow" });
  if (me.admin) {
    row.append(h("span", { class: "who" }, me.kind === "local" ? "Admin: ez a gép" : `Admin: ${me.user}`));
    const toggle = h("button", { type: "button", class: "btn primary" },
      adminMode === "add" ? "Bezár" : "+ Nyomtató hozzáadása");
    toggle.addEventListener("click", () => { adminMode = adminMode === "add" ? null : "add"; renderAdminBar(); });
    row.append(toggle);
    const bulk = h("button", { type: "button", class: "btn" },
      adminMode === "bulk" ? "Bezár" : "+ Több egyszerre");
    bulk.addEventListener("click", () => { adminMode = adminMode === "bulk" ? null : "bulk"; renderAdminBar(); });
    row.append(bulk);
    if (me.kind === "admin") {
      const out = h("button", { type: "button", class: "btn" }, "Kilépés");
      out.addEventListener("click", async () => {
        await api("POST", "api/logout");
        adminMode = null;
        lastMessage = null;
        await refreshMe(true);
        refresh();
      });
      row.append(out);
    }
  } else {
    row.append(h("span", { class: "who muted" }, "Csak megtekintés"));
    const toggle = h("button", { type: "button", class: "btn" },
      adminMode === "login" ? "Bezár" : "Belépés (admin)");
    toggle.addEventListener("click", () => { adminMode = adminMode === "login" ? null : "login"; renderAdminBar(); });
    row.append(toggle);
  }
  box.append(row);
  if (me.admin && adminMode === "add") box.append(addForm());
  if (me.admin && adminMode === "bulk") box.append(bulkForm());
  if (!me.admin && adminMode === "login") box.append(loginForm());
  msgEl = h("div", { class: "msg hidden", role: "status" });
  box.append(msgEl);
  paintMessage();
}

// A /api/me alapján frissíti, hogy ki vagyok. Az admin sávot csak változáskor (vagy kérésre)
// rajzolja újra, hogy a félig kitöltött űrlap ne törlődjön a háttérfrissítéstől.
async function refreshMe(force) {
  try {
    const r = await fetch("api/me", { cache: "no-store", credentials: "same-origin" });
    if (!r.ok) return;
    const next = await r.json();
    const changed = JSON.stringify(next) !== JSON.stringify(me);
    me = next;
    if (!me.admin && adminMode === "add") adminMode = null;
    if (changed || force) renderAdminBar();
  } catch (e) { /* a következő körben újra próbálja */ }
}

// ------------------------------------------------------------------- kártyák
function deleteButton(p) {
  if (!me.admin || p.id === null || p.id === undefined) return null;
  const b = h("button", { type: "button", class: "del", title: "Nyomtató törlése" }, "Törlés");
  b.addEventListener("click", async () => {
    if (!window.confirm(`Biztosan törlöd ezt a nyomtatót?\n\n${p.name} (${p.ip})\n\n` +
                        "A régi előzményei megmaradnak.")) return;
    b.disabled = true;
    const r = await api("DELETE", `api/printers/${p.id}`);
    if (r.ok) {
      setMessage("ok", `Törölve: ${r.data.name}`);
      refresh();
    } else {
      b.disabled = false;
      handleApiError(r);
    }
  });
  return b;
}

function renderCard(p) {
  const head = h("div", { class: "chead" },
    h("h2", null, p.name, " ", h("small", null, p.ip)), deleteButton(p));
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

function renderEmpty() {
  const text = me.admin
    ? "Még nincs nyomtató felvéve. A „+ Nyomtató hozzáadása” gombbal veheted fel az elsőt."
    : me.manage
      ? "Még nincs nyomtató felvéve. Hozzáadni admin jogosultsággal lehet: a futtató gépen " +
        "a http://localhost címen, vagy admin fiókkal belépve."
      : "Még nincs nyomtató felvéve.";
  return h("div", { class: "empty" }, text);
}

// A kártyák oszlopokba kerülnek (az i. kártya az i % oszlopszám oszlopba), és minden oszlopban
// szorosan egymás alatt állnak. Így nem marad lyuk a rövidebb kártyák alatt, és a sorrend
// balról jobbra olvasható marad. A kártyák helye nem függ a magasságuktól, ezért a Részletek
// kinyitása nem rendezi át a többit.
const MIN_CARD_PX = 300;   // ugyanez a szám a style.css-ben is szerepel (.col min-width)
const GAP_PX = 12;
let lastData = null;
let lastCols = 0;

function columnCount() {
  const w = document.getElementById("app").clientWidth;
  return Math.max(1, Math.floor((w + GAP_PX) / (MIN_CARD_PX + GAP_PX)));
}

function render(data) {
  lastData = data;
  const app = document.getElementById("app");
  const printers = data.printers || [];
  const box = renderOrderBox(printers);
  const n = columnCount();
  lastCols = n;
  const cols = Array.from({ length: n }, () => h("div", { class: "col" }));
  printers.forEach((p, i) => cols[i % n].append(renderCard(p)));
  app.textContent = "";
  if (box) app.append(box);
  if (!printers.length) app.append(renderEmpty());
  app.append(h("div", { class: "g" }, cols));
}

// Átméretezéskor (pl. félképes ablak) újrarendezi az oszlopokat, ha az oszlopszám változott.
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (lastData && columnCount() !== lastCols) render(lastData);
  }, 150);
});

async function refresh() {
  const status = document.getElementById("status");
  await refreshMe();
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
