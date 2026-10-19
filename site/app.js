"use strict";

const GENRE_LABELS = {
  samba: "Samba", choro: "Choro", instrumental: "Instrumental", gafieira: "Gafieira", jazz: "Jazz",
  afro: "Afro/diáspora", sopros: "Sopros", mpb: "MPB", forro: "Forró", frevo: "Frevo/maracatu",
  classical: "Erudito", rock: "Rock", pop: "Pop", electronic: "Eletrônica", funk: "Funk",
  sertanejo: "Sertanejo", pagode_romantico: "Pagode romântico", tribute: "Tributo/cover",
  hiphop: "Rap/hip-hop", reggae: "Reggae", blues_soul: "Blues/soul",
};
const WD = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];
const MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

const store = {
  get(k, d) { try { const v = localStorage.getItem("mf:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("mf:" + k, JSON.stringify(v)); } catch {} },
};

const isPhone = () => window.matchMedia("(max-width: 700px)").matches;

const state = {
  data: null,
  city: store.get("city", "rio"),
  view: store.get("view", isPhone() ? "list" : "month"),
  f: Object.assign({ minScore: 50, free: false, onlyNew: false, hood: "", from: "", to: "", genres: [] },
                   store.get("filters", {})),
};
if (isPhone() && state.view !== "list" && !store.get("viewChosen", false)) state.view = "list";

let calendar = null;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function localDate(iso) { return iso.slice(0, 10); }
function hhmm(iso) { return iso.slice(11, 16); }
function todayStr() {
  const d = new Date(new Date().toLocaleString("en-US", { timeZone: "America/Sao_Paulo" }));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dayLabel(ds) {
  const [y, m, d] = ds.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  const t = todayStr();
  const prefix = ds === t ? "Hoje · " : "";
  return `${prefix}${WD[dt.getDay()]} ${d} ${MONTHS[m - 1]}`;
}
function price(e) {
  if (e.is_free) return "grátis";
  if (e.price_min == null) return "";
  const a = Math.round(e.price_min), b = e.price_max != null ? Math.round(e.price_max) : a;
  return a === b ? `R$ ${a}` : `R$ ${a}–${b}`;
}
function scoreClass(s) { return s >= 75 ? "hi" : s >= 55 ? "mid" : ""; }

function cityEvents() { return state.data.events.filter((e) => e.city === state.city); }

function filtered() {
  const f = state.f;
  return cityEvents().filter((e) =>
    (e.score ?? 0) >= f.minScore &&
    (!f.free || e.is_free) &&
    (!f.onlyNew || e.is_new) &&
    (!f.hood || e.neighborhood === f.hood) &&
    (!f.from || localDate(e.start) >= f.from) &&
    (!f.to || localDate(e.start) <= f.to) &&
    (!f.genres.length || f.genres.some((g) => (e.genres || []).includes(g))));
}

function badges(e) {
  let b = "";
  if (e.is_new) b += '<span class="badge">novo</span>';
  if (e.watch) b += '<span class="badge watch">watch</span>';
  if (e.is_free) b += '<span class="badge free">grátis</span>';
  if (e.sold_out) b += '<span class="badge out">esgotado</span>';
  return b ? `<span class="badges">${b}</span>` : "";
}

function card(e, withDate) {
  const where = [e.venue, e.neighborhood].filter(Boolean).join(" · ");
  const when = (withDate ? dayLabel(localDate(e.start)) + " · " : "") + hhmm(e.start);
  const sub = [when, where, price(e)].filter(Boolean).join(" · ");
  return `<button class="card" data-id="${esc(e.id)}">
    <div class="score ${scoreClass(e.score)}">${e.score ?? "–"}</div>
    <div><div class="title">${esc(e.title)}${badges(e)}</div><div class="sub">${esc(sub)}</div></div>
  </button>`;
}

function renderTonight() {
  const t = todayStr();
  const notOver = (e) => (e.end ? Date.parse(e.end) : Date.parse(e.start) + 3 * 3600e3) > Date.now();
  const evs = cityEvents()
    .filter((e) => localDate(e.start) === t && hhmm(e.start) >= "17:00" && notOver(e))
    .filter((e) => (e.score ?? 0) >= Math.min(state.f.minScore, 50))
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 10)
    .sort((a, b) => a.start.localeCompare(b.start));
  $("tonightList").innerHTML = evs.length ? evs.map((e) => card(e, false)).join("")
    : '<div class="tonight-empty">Nada marcado para hoje à noite.</div>';
}

function renderList() {
  const evs = filtered();
  if (!evs.length) { $("listView").innerHTML = '<div class="empty">Nenhum evento com esses filtros.</div>'; return; }
  const byDay = new Map();
  for (const e of evs) {
    const d = localDate(e.start);
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d).push(e);
  }
  let html = "";
  for (const [d, list] of byDay) {
    html += `<section class="day"><h3>${dayLabel(d)}</h3>${list.map((e) => card(e, false)).join("")}</section>`;
  }
  $("listView").innerHTML = html;
}

function renderCalendar() {
  if (!window.FullCalendar) { setTimeout(renderCalendar, 150); return; }
  const fcView = state.view === "week" ? (isPhone() ? "listWeek" : "timeGridWeek") : "dayGridMonth";
  const events = filtered().map((e) => ({
    id: e.id, title: `${e.score ?? ""} ${e.title}`, start: e.start, end: e.end || undefined,
    color: e.score >= 75 ? "var(--good)" : e.score >= 55 ? "var(--mid)" : "var(--low)",
  }));
  if (!calendar) {
    calendar = new FullCalendar.Calendar($("calView"), {
      locale: "pt-br", timeZone: "America/Sao_Paulo", height: "auto", firstDay: 1,
      headerToolbar: { left: "prev,next today", center: "title", right: "" },
      buttonText: { today: "hoje" }, dayMaxEvents: 4, nowIndicator: true,
      slotMinTime: "10:00:00", eventDisplay: "block",
      eventClick: (info) => { info.jsEvent.preventDefault(); openDetail(info.event.id); },
    });
    calendar.render();
  }
  if (calendar.view.type !== fcView) calendar.changeView(fcView);
  calendar.removeAllEventSources();
  calendar.addEventSource(events);
}

function render() {
  for (const b of $("cityToggle").children) b.classList.toggle("on", b.dataset.city === state.city);
  for (const b of $("viewToggle").children) b.classList.toggle("on", b.dataset.view === state.view);
  renderTonight();
  const list = state.view === "list";
  $("listView").hidden = !list;
  $("calView").hidden = list;
  if (list) renderList(); else renderCalendar();
  const n = activeFilterCount();
  $("filterCount").textContent = n ? `(${n})` : "";
  const total = cityEvents().length, shown = filtered().length;
  const gen = new Date(state.data.generated);
  $("meta").textContent = `${shown} de ${total} eventos · atualizado ${gen.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", dateStyle: "short", timeStyle: "short" })}`;
  const base = location.href.replace(/[#?].*$/, "").replace(/[^/]*$/, "");
  $("feeds").innerHTML = `Calendários: <a href="${base}${state.city}.ics">${state.city}.ics</a> · <a href="${base}watchlist.ics">watchlist.ics</a>`;
}

function activeFilterCount() {
  const f = state.f;
  return [f.minScore !== 50, f.free, f.onlyNew, !!f.hood, !!f.from, !!f.to, f.genres.length > 0].filter(Boolean).length;
}

function buildFilters() {
  const evs = cityEvents();
  const hoods = [...new Set(evs.map((e) => e.neighborhood).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt"));
  $("hood").innerHTML = '<option value="">Todos</option>' + hoods.map((h) => `<option${h === state.f.hood ? " selected" : ""}>${esc(h)}</option>`).join("");
  const counts = {};
  for (const e of evs) for (const g of e.genres || []) counts[g] = (counts[g] || 0) + 1;
  const gs = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  $("genres").innerHTML = gs.map((g) => `<button type="button" class="chip${state.f.genres.includes(g) ? " on" : ""}" data-g="${g}">${esc(GENRE_LABELS[g] || g)} <small>${counts[g]}</small></button>`).join("");
  $("minScore").value = state.f.minScore;
  $("minScoreOut").textContent = state.f.minScore;
  $("freeOnly").checked = state.f.free;
  $("newOnly").checked = state.f.onlyNew;
  $("dateFrom").value = state.f.from;
  $("dateTo").value = state.f.to;
}

function saveFilters() { store.set("filters", state.f); render(); }

function openDetail(id) {
  const e = state.data.events.find((x) => x.id === id);
  if (!e) return;
  const rows = [
    ["Quando", `${dayLabel(localDate(e.start))} · ${hhmm(e.start)}${e.end ? "–" + hhmm(e.end) : ""}`],
    ["Onde", [e.venue, e.neighborhood].filter(Boolean).join(" · ")],
    e.artists?.length ? ["Artistas", e.artists.join(", ")] : null,
    price(e) ? ["Preço", price(e)] : null,
    e.genres?.length ? ["Gêneros", e.genres.map((g) => GENRE_LABELS[g] || g).join(", ")] : null,
    ["Fonte", (e.sources || []).join(", ") + (e.source_url && e.source_url !== e.ticket_url ? ` · <a href="${esc(e.source_url)}" target="_blank" rel="noopener">página</a>` : "")],
  ].filter(Boolean);
  $("detailBody").innerHTML = `<div class="detail">
    ${e.image_url ? `<img class="flyer" src="${esc(e.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}
    <h2>${esc(e.title)}${badges(e)}</h2>
    <div class="reason"><b>${e.score ?? "–"}/100</b> — ${esc(e.score_reason || "")}</div>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${k === "Fonte" ? v : esc(v)}</dd>`).join("")}</dl>
    ${e.ticket_url ? `<a class="cta" href="${esc(e.ticket_url)}" target="_blank" rel="noopener">${e.sold_out ? "Ver (esgotado)" : "Ingressos"}</a>` : ""}
  </div>`;
  $("detail").showModal();
}

function wire() {
  $("cityToggle").innerHTML = state.data.cities.map((c) => `<button data-city="${c.slug}">${esc(c.slug === "rio" ? "Rio" : c.slug === "sp" ? "SP" : c.name)}</button>`).join("");
  if (!state.data.cities.some((c) => c.slug === state.city)) state.city = state.data.cities[0]?.slug;
  $("cityToggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.city = b.dataset.city; store.set("city", state.city);
    state.f.hood = ""; buildFilters(); saveFilters();
    history.replaceState(null, "", "#" + state.city);
  });
  $("viewToggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.view = b.dataset.view; store.set("view", state.view); store.set("viewChosen", true); render();
  });
  $("filtersBtn").addEventListener("click", () => {
    const f = $("filters"); f.hidden = !f.hidden; $("filtersBtn").setAttribute("aria-expanded", String(!f.hidden));
  });
  $("minScore").addEventListener("input", (e) => { state.f.minScore = +e.target.value; $("minScoreOut").textContent = e.target.value; saveFilters(); });
  $("freeOnly").addEventListener("change", (e) => { state.f.free = e.target.checked; saveFilters(); });
  $("newOnly").addEventListener("change", (e) => { state.f.onlyNew = e.target.checked; saveFilters(); });
  $("hood").addEventListener("change", (e) => { state.f.hood = e.target.value; saveFilters(); });
  $("dateFrom").addEventListener("change", (e) => { state.f.from = e.target.value; saveFilters(); });
  $("dateTo").addEventListener("change", (e) => { state.f.to = e.target.value; saveFilters(); });
  $("genres").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip"); if (!b) return;
    const g = b.dataset.g, i = state.f.genres.indexOf(g);
    if (i >= 0) state.f.genres.splice(i, 1); else state.f.genres.push(g);
    b.classList.toggle("on"); saveFilters();
  });
  $("resetFilters").addEventListener("click", () => {
    state.f = { minScore: 50, free: false, onlyNew: false, hood: "", from: "", to: "", genres: [] };
    buildFilters(); saveFilters();
  });
  document.body.addEventListener("click", (ev) => {
    const c = ev.target.closest(".card"); if (c) openDetail(c.dataset.id);
  });
  $("closeDetail").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("click", (ev) => { if (ev.target === $("detail")) $("detail").close(); });
}

async function init() {
  const h = location.hash.slice(1);
  if (h) state.city = h;
  try {
    const r = await fetch("events.json", { cache: "no-cache" });
    state.data = await r.json();
  } catch (err) {
    $("listView").innerHTML = '<div class="empty">Não foi possível carregar events.json.</div>';
    return;
  }
  wire();
  buildFilters();
  render();
}

init();
