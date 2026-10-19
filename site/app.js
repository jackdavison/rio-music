"use strict";

const I18N = {
  pt: {
    brand: "Música ao vivo", tonight: "Hoje à noite", v_stream: "Fluxo", v_list: "Lista",
    v_week: "Semana", v_month: "Mês", starred: "Favoritos", filters: "Filtros",
    min_score: "Nota mínima", free_only: "Só grátis", new_only: "Só novos", hood: "Bairro",
    all: "Todos", from: "De", to: "Até", genres: "Gêneros", clear: "Limpar filtros",
    nothing_tonight: "Nada marcado para hoje à noite.", no_events: "Nenhum evento com esses filtros.",
    no_stars: "Nenhum favorito ainda: toque na ☆ de qualquer evento.",
    count: "{shown} de {total} eventos", count_stars: "{n} favorito(s)", updated: "atualizado {d}",
    calendars: "Calendários", today: "Hoje", free: "grátis", sold_out: "esgotado", new: "novo",
    gone: "saiu da lista", tbc: "a confirmar", when: "Quando", where: "Onde", artists: "Artistas",
    price: "Preço", genres_k: "Gêneros", source: "Fonte", page: "página", tickets: "Ingressos",
    view_sold: "Ver (esgotado)", star: "Favoritar", unstar: "Tirar dos favoritos", close: "Fechar",
    load_fail: "Não foi possível carregar events.json.", city: "Cidade", view: "Visualização",
    gone_note: "Este evento não aparece mais nas fontes (cancelado, esgotado e retirado, ou mudou de nome). Confira antes de ir.",
  },
  en: {
    brand: "Live music", tonight: "Tonight", v_stream: "Stream", v_list: "List",
    v_week: "Week", v_month: "Month", starred: "Starred", filters: "Filters",
    min_score: "Min score", free_only: "Free only", new_only: "New only", hood: "Neighborhood",
    all: "All", from: "From", to: "To", genres: "Genres", clear: "Clear filters",
    nothing_tonight: "Nothing on tonight.", no_events: "No events match these filters.",
    no_stars: "No starred events yet: tap the ☆ on any event.",
    count: "{shown} of {total} events", count_stars: "{n} starred", updated: "updated {d}",
    calendars: "Calendars", today: "Today", free: "free", sold_out: "sold out", new: "new",
    gone: "no longer listed", tbc: "time TBC", when: "When", where: "Where", artists: "Artists",
    price: "Price", genres_k: "Genres", source: "Source", page: "page", tickets: "Tickets",
    view_sold: "View (sold out)", star: "Star", unstar: "Unstar", close: "Close",
    load_fail: "Couldn't load events.json.", city: "City", view: "View",
    gone_note: "This event no longer appears in any source (cancelled, sold out and pulled, or renamed). Check before going.",
  },
};
const WD = { pt: ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"], en: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] };
const MONTHS = {
  pt: ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"],
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
};
const GENRE_LABELS = {
  pt: {
    samba: "Samba", choro: "Choro", instrumental: "Instrumental", gafieira: "Gafieira", jazz: "Jazz",
    afro: "Afro/diáspora", sopros: "Sopros", mpb: "MPB", forro: "Forró", frevo: "Frevo/maracatu",
    classical: "Erudito", rock: "Rock", pop: "Pop", electronic: "Eletrônica", funk: "Funk",
    sertanejo: "Sertanejo", pagode_romantico: "Pagode romântico", tribute: "Tributo/cover",
    hiphop: "Rap/hip-hop", reggae: "Reggae", blues_soul: "Blues/soul",
  },
  en: {
    samba: "Samba", choro: "Choro", instrumental: "Instrumental", gafieira: "Gafieira", jazz: "Jazz",
    afro: "Afro/diaspora", sopros: "Horns/flute", mpb: "MPB", forro: "Forró", frevo: "Frevo/maracatu",
    classical: "Classical", rock: "Rock", pop: "Pop", electronic: "Electronic", funk: "Funk",
    sertanejo: "Sertanejo", pagode_romantico: "Romantic pagode", tribute: "Tribute/cover",
    hiphop: "Rap/hip-hop", reggae: "Reggae", blues_soul: "Blues/soul",
  },
};

const store = {
  get(k, d) { try { const v = localStorage.getItem("mf:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("mf:" + k, JSON.stringify(v)); } catch {} },
};

const isPhone = () => window.matchMedia("(max-width: 700px)").matches;
const DEFAULT_F = { minScore: 50, free: false, onlyNew: false, hood: "", from: "", to: "", genres: [] };

const state = {
  data: null,
  city: store.get("city", "rio"),
  lang: store.get("lang", (navigator.language || "pt").toLowerCase().startsWith("pt") ? "pt" : "en"),
  view: store.get("view", "stream"),
  starOnly: store.get("starOnly", false),
  stars: store.get("stars", {}),   // id -> snapshot of the event when starred
  f: Object.assign({}, DEFAULT_F, store.get("filters", {})),
};
if (!["stream", "list", "week", "month"].includes(state.view)) state.view = "stream";

let calendar = null;
let streamRows = [], streamShown = 0;
const CHUNK = 150;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const t = (k, vars) => (I18N[state.lang][k] ?? I18N.pt[k] ?? k).replace(/\{(\w+)\}/g, (_, v) => vars?.[v] ?? "");
const genreLabel = (g) => GENRE_LABELS[state.lang][g] || g;

function localDate(iso) { return iso.slice(0, 10); }
function hhmm(iso) { return iso.slice(11, 16); }
function timeLabel(e) { return e.tbc ? t("tbc") : hhmm(e.start); }
function todayStr() {
  const d = new Date(new Date().toLocaleString("en-US", { timeZone: "America/Sao_Paulo" }));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dayLabel(ds) {
  const [y, m, d] = ds.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  const prefix = ds === todayStr() ? t("today") + " · " : "";
  const wd = WD[state.lang][dt.getDay()], mo = MONTHS[state.lang][m - 1];
  return state.lang === "en" ? `${prefix}${wd} ${mo} ${d}` : `${prefix}${wd} ${d} ${mo}`;
}
function price(e) {
  if (e.is_free) return t("free");
  if (e.price_min == null) return "";
  const a = Math.round(e.price_min), b = e.price_max != null ? Math.round(e.price_max) : a;
  return a === b ? `R$ ${a}` : `R$ ${a}–${b}`;
}
function scoreClass(s) { return s >= 75 ? "hi" : s >= 55 ? "mid" : ""; }

// ---------- stars ----------

const isStarred = (id) => Object.prototype.hasOwnProperty.call(state.stars, id);
const SNAP_KEYS = ["id", "city", "title", "venue", "neighborhood", "start", "end", "ticket_url", "is_free", "price_min", "price_max", "tbc"];

function toggleStar(id) {
  if (isStarred(id)) delete state.stars[id];
  else {
    const e = state.data.events.find((x) => x.id === id);
    if (!e) return;
    state.stars[id] = Object.fromEntries(SNAP_KEYS.filter((k) => e[k] != null).map((k) => [k, e[k]]));
  }
  store.set("stars", state.stars);
  if (state.starOnly || state.view === "week" || state.view === "month") { render(true); return; }
  // stream/list: flip the buttons in place instead of re-rendering thousands of rows
  const on = isStarred(id);
  for (const b of document.querySelectorAll(`.star[data-star="${CSS.escape(id)}"]`)) {
    b.classList.toggle("on", on); b.textContent = on ? "★" : "☆";
    b.setAttribute("aria-pressed", String(on)); b.setAttribute("aria-label", t(on ? "unstar" : "star"));
  }
  const n = starredEvents().length;
  $("starCount").textContent = n ? `(${n})` : "";
}

function pruneStars() {
  // forget stars for events more than two weeks past
  const cutoff = Date.now() - 14 * 864e5;
  let changed = false;
  for (const [id, s] of Object.entries(state.stars)) {
    if (s.start && Date.parse(s.start) < cutoff) { delete state.stars[id]; changed = true; }
  }
  if (changed) store.set("stars", state.stars);
}

function starBtn(e) {
  const on = isStarred(e.id);
  return `<button type="button" class="star${on ? " on" : ""}" data-star="${esc(e.id)}" aria-pressed="${on}" aria-label="${t(on ? "unstar" : "star")}">${on ? "★" : "☆"}</button>`;
}

// ---------- selection ----------

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

// Starred mode ignores the filters: everything you starred in this city, including stars whose
// event has dropped out of the listings (shown from the snapshot taken when starring).
function starredEvents() {
  const live = new Map(cityEvents().map((e) => [e.id, e]));
  const since = Date.now() - 6 * 3600e3;
  const out = [];
  for (const [id, snap] of Object.entries(state.stars)) {
    if (snap.city && snap.city !== state.city) continue;
    const e = live.get(id) || (Date.parse(snap.start) > since ? { ...snap, gone: true } : null);
    if (e) out.push(e);
  }
  return out.sort((a, b) => a.start.localeCompare(b.start));
}

function selected() { return state.starOnly ? starredEvents() : filtered(); }

// ---------- rendering ----------

function badges(e) {
  let b = "";
  if (e.gone) b += `<span class="badge out">${t("gone")}</span>`;
  if (e.is_new) b += `<span class="badge">${t("new")}</span>`;
  if (e.watch) b += '<span class="badge watch">watch</span>';
  if (e.is_free) b += `<span class="badge free">${t("free")}</span>`;
  if (e.sold_out) b += `<span class="badge out">${t("sold_out")}</span>`;
  return b ? `<span class="badges">${b}</span>` : "";
}

function card(e, withDate) {
  const where = [e.venue, e.neighborhood].filter(Boolean).join(" · ");
  const when = (withDate ? dayLabel(localDate(e.start)) + " · " : "") + timeLabel(e);
  const sub = [when, where, price(e)].filter(Boolean).join(" · ");
  return `<div class="card" role="button" tabindex="0" data-id="${esc(e.id)}">
    <div class="score ${scoreClass(e.score)}">${e.score ?? "–"}</div>
    <div><div class="title">${esc(e.title)}${badges(e)}</div><div class="sub">${esc(sub)}</div></div>
    ${starBtn(e)}
  </div>`;
}

function streamRow(e) {
  const where = [e.venue, e.neighborhood, price(e)].filter(Boolean).join(" · ");
  const blurb = e.score_reason || e.desc || "";
  return `<div class="ev ${scoreClass(e.score)}" role="button" tabindex="0" data-id="${esc(e.id)}">
    <div class="ev-time">${esc(timeLabel(e))}<span class="ev-score ${scoreClass(e.score)}">${e.score ?? "–"}</span></div>
    <div class="ev-body">
      <div class="ev-title">${esc(e.title)}${badges(e)}</div>
      <div class="ev-where">${esc(where)}</div>
      ${blurb ? `<div class="ev-desc">${esc(blurb)}</div>` : ""}
    </div>
    ${starBtn(e)}
  </div>`;
}

function renderTonight() {
  const today = todayStr();
  const notOver = (e) => (e.end ? Date.parse(e.end) : Date.parse(e.start) + 3 * 3600e3) > Date.now();
  const evs = cityEvents()
    .filter((e) => localDate(e.start) === today && hhmm(e.start) >= "17:00" && notOver(e))
    .filter((e) => (e.score ?? 0) >= Math.min(state.f.minScore, 50))
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 10)
    .sort((a, b) => a.start.localeCompare(b.start));
  $("tonightList").innerHTML = evs.length ? evs.map((e) => card(e, false)).join("")
    : `<div class="tonight-empty">${t("nothing_tonight")}</div>`;
}

function emptyMsg() { return `<div class="empty">${t(state.starOnly ? "no_stars" : "no_events")}</div>`; }

function renderList(evs) {
  if (!evs.length) { $("listView").innerHTML = emptyMsg(); return; }
  let html = "", cur = null;
  for (const e of evs) {
    const d = localDate(e.start);
    if (d !== cur) { html += `${cur ? "</section>" : ""}<section class="day"><h3>${dayLabel(d)}</h3>`; cur = d; }
    html += card(e, false);
  }
  $("listView").innerHTML = html + "</section>";
}

// The stream renders in chunks as you scroll so thousands of events stay fast on a phone.
function renderStream(evs, keepScroll) {
  streamRows = evs;
  const want = keepScroll ? Math.max(streamShown, CHUNK) : CHUNK;
  streamShown = 0;
  $("streamView").innerHTML = evs.length ? "" : emptyMsg();
  appendStream(want);
}

function appendStream(n) {
  const end = Math.min(streamRows.length, streamShown + n);
  if (end <= streamShown) return;
  let html = "";
  let prev = streamShown ? localDate(streamRows[streamShown - 1].start) : null;
  for (let i = streamShown; i < end; i++) {
    const e = streamRows[i], d = localDate(e.start);
    if (d !== prev) { html += `<h3 class="stream-day">${dayLabel(d)}</h3>`; prev = d; }
    html += streamRow(e);
  }
  $("streamView").insertAdjacentHTML("beforeend", html);
  streamShown = end;
}

function renderCalendar(evs) {
  if (!window.FullCalendar) { setTimeout(() => renderCalendar(selected()), 150); return; }
  const fcView = state.view === "week" ? (isPhone() ? "listWeek" : "timeGridWeek") : "dayGridMonth";
  const events = evs.map((e) => ({
    id: e.id, title: [isStarred(e.id) ? "★" : "", e.score ?? "", e.title].filter((x) => x !== "").join(" "), start: e.start, end: e.end || undefined,
    color: e.score >= 75 ? "var(--good)" : e.score >= 55 ? "var(--mid)" : "var(--low)",
  }));
  if (!calendar) {
    calendar = new FullCalendar.Calendar($("calView"), {
      timeZone: "America/Sao_Paulo", height: "auto", firstDay: 1,
      headerToolbar: { left: "prev,next today", center: "title", right: "" },
      dayMaxEvents: 4, nowIndicator: true, slotMinTime: "10:00:00", eventDisplay: "block",
      eventClick: (info) => { info.jsEvent.preventDefault(); openDetail(info.event.id); },
    });
    calendar.render();
  }
  calendar.setOption("locale", state.lang === "en" ? "en-gb" : "pt-br");
  calendar.setOption("buttonText", { today: t("today").toLowerCase() });
  if (calendar.view.type !== fcView) calendar.changeView(fcView);
  calendar.removeAllEventSources();
  calendar.addEventSource(events);
}

function applyStatic() {
  document.documentElement.lang = state.lang === "en" ? "en" : "pt-BR";
  document.title = t("brand");
  for (const el of document.querySelectorAll("[data-t]")) el.textContent = t(el.dataset.t);
  $("cityToggle").setAttribute("aria-label", t("city"));
  $("viewToggle").setAttribute("aria-label", t("view"));
  $("closeDetail").setAttribute("aria-label", t("close"));
  for (const b of $("langToggle").children) b.classList.toggle("on", b.dataset.lang === state.lang);
}

function render(keepScroll = false) {
  for (const b of $("cityToggle").children) b.classList.toggle("on", b.dataset.city === state.city);
  for (const b of $("viewToggle").children) b.classList.toggle("on", b.dataset.view === state.view);
  const nStars = starredEvents().length;
  $("starBtn").classList.toggle("on", state.starOnly);
  $("starBtn").setAttribute("aria-pressed", String(state.starOnly));
  $("starBtn").querySelector(".star-ico").textContent = state.starOnly ? "★" : "☆";
  $("starCount").textContent = nStars ? `(${nStars})` : "";
  $("filtersBtn").disabled = state.starOnly;
  if (state.starOnly) $("filters").hidden = true;
  $("tonight").hidden = state.starOnly;
  if (!state.starOnly) renderTonight();

  const evs = selected();
  const v = state.view;
  $("streamView").hidden = v !== "stream";
  $("listView").hidden = v !== "list";
  $("calView").hidden = v === "stream" || v === "list";
  if (v === "stream") renderStream(evs, keepScroll);
  else if (v === "list") renderList(evs);
  else renderCalendar(evs);

  const n = activeFilterCount();
  $("filterCount").textContent = n ? `(${n})` : "";
  const gen = new Date(state.data.generated).toLocaleString(state.lang === "en" ? "en-GB" : "pt-BR",
    { timeZone: "America/Sao_Paulo", dateStyle: "short", timeStyle: "short" });
  const count = state.starOnly ? t("count_stars", { n: evs.length })
    : t("count", { shown: evs.length, total: cityEvents().length });
  $("meta").textContent = `${count} · ${t("updated", { d: gen })}`;
  const base = location.href.replace(/[#?].*$/, "").replace(/[^/]*$/, "");
  $("feeds").innerHTML = `${t("calendars")}: <a href="${base}${state.city}.ics">${state.city}.ics</a> · <a href="${base}watchlist.ics">watchlist.ics</a>`;
}

function activeFilterCount() {
  const f = state.f;
  return [f.minScore !== 50, f.free, f.onlyNew, !!f.hood, !!f.from, !!f.to, f.genres.length > 0].filter(Boolean).length;
}

function buildFilters() {
  const evs = cityEvents();
  const hoods = [...new Set(evs.map((e) => e.neighborhood).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt"));
  $("hood").innerHTML = `<option value="">${t("all")}</option>` + hoods.map((h) => `<option${h === state.f.hood ? " selected" : ""}>${esc(h)}</option>`).join("");
  const counts = {};
  for (const e of evs) for (const g of e.genres || []) counts[g] = (counts[g] || 0) + 1;
  const gs = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  $("genres").innerHTML = gs.map((g) => `<button type="button" class="chip${state.f.genres.includes(g) ? " on" : ""}" data-g="${g}">${esc(genreLabel(g))} <small>${counts[g]}</small></button>`).join("");
  $("minScore").value = state.f.minScore;
  $("minScoreOut").textContent = state.f.minScore;
  $("freeOnly").checked = state.f.free;
  $("newOnly").checked = state.f.onlyNew;
  $("dateFrom").value = state.f.from;
  $("dateTo").value = state.f.to;
}

function saveFilters() { store.set("filters", state.f); render(); }

function openDetail(id) {
  const e = state.data.events.find((x) => x.id === id) || (state.stars[id] && { ...state.stars[id], gone: true });
  if (!e) return;
  const end = e.end && !e.tbc ? "–" + hhmm(e.end) : "";
  const rows = [
    [t("when"), `${dayLabel(localDate(e.start))} · ${timeLabel(e)}${end}`],
    [t("where"), [e.venue, e.neighborhood].filter(Boolean).join(" · ")],
    e.artists?.length ? [t("artists"), e.artists.join(", ")] : null,
    price(e) ? [t("price"), price(e)] : null,
    e.genres?.length ? [t("genres_k"), e.genres.map(genreLabel).join(", ")] : null,
    e.sources?.length ? [t("source"), esc(e.sources.join(", ")) + (e.source_url && e.source_url !== e.ticket_url ? ` · <a href="${esc(e.source_url)}" target="_blank" rel="noopener">${t("page")}</a>` : "")] : null,
  ].filter(Boolean);
  const on = isStarred(e.id);
  $("detailBody").innerHTML = `<div class="detail">
    ${e.image_url ? `<img class="flyer" src="${esc(e.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ""}
    <h2>${esc(e.title)}${badges(e)}</h2>
    ${e.gone ? `<div class="reason">${t("gone_note")}</div>` : `<div class="reason"><b>${e.score ?? "–"}/100</b> — ${esc(e.score_reason || "")}</div>`}
    ${e.desc ? `<p class="desc">${esc(e.desc)}</p>` : ""}
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${k === t("source") ? v : esc(v)}</dd>`).join("")}</dl>
    <div class="actions">
      <button type="button" class="ghost star-wide${on ? " on" : ""}" data-star="${esc(e.id)}">${on ? "★ " + t("unstar") : "☆ " + t("star")}</button>
      ${e.ticket_url ? `<a class="cta" href="${esc(e.ticket_url)}" target="_blank" rel="noopener">${e.sold_out ? t("view_sold") : t("tickets")}</a>` : ""}
    </div>
  </div>`;
  if (!$("detail").open) $("detail").showModal();
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
  $("langToggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.lang = b.dataset.lang; store.set("lang", state.lang);
    applyStatic(); buildFilters(); render(true);
  });
  $("viewToggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.view = b.dataset.view; store.set("view", state.view); render();
  });
  $("starBtn").addEventListener("click", () => {
    state.starOnly = !state.starOnly; store.set("starOnly", state.starOnly); render(); window.scrollTo({ top: 0 });
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
    state.f = { ...DEFAULT_F, genres: [] };
    buildFilters(); saveFilters();
  });
  document.body.addEventListener("click", (ev) => {
    const s = ev.target.closest("[data-star]");
    if (s) {
      ev.stopPropagation();
      const id = s.dataset.star;
      toggleStar(id);
      if ($("detail").open) openDetail(id);
      return;
    }
    const c = ev.target.closest(".card, .ev"); if (c) openDetail(c.dataset.id);
  });
  document.body.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const c = ev.target.closest?.(".card, .ev");
    if (c && ev.target === c) { ev.preventDefault(); openDetail(c.dataset.id); }
  });
  $("closeDetail").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("click", (ev) => { if (ev.target === $("detail")) $("detail").close(); });
  const more = () => {
    if (state.view === "stream" && streamShown < streamRows.length
        && $("sentinel").getBoundingClientRect().top < window.innerHeight + 1500) appendStream(CHUNK);
  };
  window.addEventListener("scroll", more, { passive: true });
  window.addEventListener("resize", more, { passive: true });
}

async function init() {
  const h = location.hash.slice(1);
  if (h) state.city = h;
  applyStatic();
  try {
    const r = await fetch("events.json", { cache: "no-cache" });
    state.data = await r.json();
  } catch (err) {
    $("streamView").innerHTML = `<div class="empty">${t("load_fail")}</div>`;
    return;
  }
  pruneStars();
  wire();
  buildFilters();
  render();
}

init();
