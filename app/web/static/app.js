/* Murphy — Co-Design front end. Displays what the server computed; computes nothing. */
const $ = (s) => document.querySelector(s);
const api = (p, o) => fetch(p, o).then((r) => r.json());
const REGION_COLORS = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#bab0ac"];
const DAY_LABEL = { weather: "Winter storm", storm: "CrowdStrike outage", calm: "Ordinary Tuesday" };
const DAY_SUB = {
  weather: "15 Jan 2024 · arctic outbreak across the Midwest and South",
  storm: "19 Jul 2024 · faulty software update grounds airline systems",
  calm: "29 Oct 2024 · nothing unusual",
};
let AIRPORTS = {}, TRIPS = [], REPLAYS = {};
const state = { trip: null, deadline: 60, exclude: [], selected: null, hindsight: false };

const hhmm = (iso) => iso.slice(11, 16);
const fmtDay = (d) => new Date(d + "T12:00:00Z").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const pct = (x) => Math.round(100 * x) + "%";
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 2600); }

/* ---------------- routing between views ---------------- */
function go(view) {
  for (const v of ["home", "trip", "replay"]) $("#view-" + v).hidden = v !== view;
  window.scrollTo(0, 0);
  if (view === "trip") setTimeout(() => { if (routeMap) { routeMap.invalidateSize(); routeMap.fitBounds(routeBounds.pad(0.25)); } }, 60);
  if (view === "replay") { if (!replayMap) initReplay(); setTimeout(() => replayMap.invalidateSize(), 50); }
}
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]");
  if (g) { e.preventDefault(); go(g.dataset.go); }
});

/* ---------------- 1. trip input ---------------- */
function fillSelect(sel, items, keep) {
  const prev = keep ? sel.value : null;
  sel.innerHTML = items.map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
  if (prev && items.some(([v]) => v === prev)) sel.value = prev;
}
function refreshForm(changed) {
  const o = $("#f-origin"), d = $("#f-dest"), day = $("#f-day"), b = $("#f-booked");
  if (!changed) fillSelect(o, [...new Set(TRIPS.map((t) => t.origin))].sort().map((x) => [x, x]));
  if (!changed || changed === "origin") {
    const dests = [...new Set(TRIPS.filter((t) => t.origin === o.value).map((t) => t.dest))].sort();
    fillSelect(d, dests.map((x) => [x, x]), true);
  }
  if (changed !== "day" && changed !== "booked") {
    const days = TRIPS.filter((t) => t.origin === o.value && t.dest === d.value);
    fillSelect(day, [...new Map(days.map((t) => [t.day, t])).values()].map((t) => [t.day, fmtDay(t.day)]), true);
  }
  const ts = TRIPS.filter((t) => t.origin === o.value && t.dest === d.value && t.day === day.value);
  if (changed !== "booked") fillSelect(b, ts.map((t) => [t.id, `${t.booked.join(" → ")} · departs ${t.booked_dep_local}`]), true);
  const t = TRIPS.find((x) => x.id === b.value);
  $("#f-checking").textContent = t ? `Checking as of ${t.decision_local} (${hhmm(t.decision)} UTC) on the day — Murphy only uses what was known by then.` : "";
}
["origin", "dest", "day", "booked"].forEach((k) => $("#f-" + k).addEventListener("change", () => refreshForm(k)));
$("#trip-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#f-deadline").value;
  openTrip($("#f-booked").value, v === "" ? null : +v);
});

function dayCards() {
  $("#day-cards").innerHTML = ["weather", "storm", "calm"].map((k) => {
    const r = REPLAYS[k];
    const alarm = r.alarm_hour ? `Alarm at ${r.alarm_hour.replace("Z", " UTC")}` : "No alarm all day";
    const peak = Math.max(...r.frames.filter((f) => f.scored >= 30).map((f) => f.share_flagged));
    return `<div class="card day-card" data-day="${k}">
      <p class="eyebrow">${DAY_LABEL[k]}</p>
      <div class="k">${pct(peak)}</div>
      <p class="fine" style="margin-top:0">of airports abnormal at the worst hour · ${alarm}</p>
      <p style="margin:10px 0 0;color:var(--ink-2)">${DAY_SUB[k]}</p>
      <p class="fine">Watch it unfold →</p></div>`;
  }).join("");
  document.querySelectorAll(".day-card").forEach((c) => c.addEventListener("click", () => { go("replay"); showDay(c.dataset.day); }));
}

/* ---------------- 2. your trip ---------------- */
let routeMap, routeLayer, regionLayer, routeBounds;
async function openTrip(id, deadline) {
  state.deadline = deadline; state.exclude = []; state.selected = null;
  await loadTrip(id);
  go("trip");
}
async function loadTrip(id) {
  const q = new URLSearchParams();
  if (state.deadline !== null && state.deadline !== undefined) q.set("deadline", state.deadline);
  if (state.exclude.length) q.set("exclude", state.exclude.join(","));
  const t = await api(`/api/trip/${id}?${q}`);
  state.trip = t;
  if (!REPLAYS[t.day_label]) REPLAYS[t.day_label] = await api(`/api/replay/${t.day_label}`);
  renderTrip();
}

function renderTrip() {
  const t = state.trip, booked = t.booked, rec = t.options[0];
  const switchIt = rec && rec.id !== booked.id;
  // F6 banner
  const b = t.banner, abnormal = b.share_flagged !== null && b.share_flagged >= 0.25;
  $("#banner").className = "banner " + (abnormal ? "bad" : "ok");
  $("#banner").innerHTML = abnormal
    ? `<div class="big">${pct(b.share_flagged)}</div><div><b>Today is not a normal day.</b>
       <div class="sub">${b.flagged} of ${b.scored} US airports were running late in a way unusual for them in the last full hour before you decided (from ${b.as_of.replace("Z", " UTC")}).
       Murphy's alarm first fired at ${b.first_alarm.replace("Z", " UTC")}. Forecasts built on ordinary days should be trusted less.</div></div>`
    : `<div class="big">${b.share_flagged === null ? "—" : pct(b.share_flagged)}</div><div><b>A normal day so far.</b>
       <div class="sub">${b.flagged} of ${b.scored} airports unusual in the last full hour before you decided (from ${b.as_of.replace("Z", " UTC")})${b.first_alarm ? `, though the alarm fired at ${b.first_alarm}` : ""}. History is a fair guide today.</div></div>`;

  // F2 verdict
  const hub = booked.hub;
  const sameReason = booked.same_region && hub
    ? `Your connection in <b>${hub}</b> is in the same group of nearby airports as <b>${t.origin}</b>. When one of them struggles, the other usually does too — so it offers no escape.` : "";
  $("#verdict").className = "card verdict " + (switchIt ? "risk" : "okay");
  $("#verdict").innerHTML = `
    <p class="eyebrow">${t.origin} → ${t.dest} · ${fmtDay(t.day)}</p>
    <h2>${switchIt ? "At risk — consider switching" : "Looks OK — keep your booking"}</h2>
    ${switchIt ? `<p>${sameReason}</p><p>Murphy's pick: <b>${optName(rec)}</b>, leaving ${rec.legs[0].dep_local}${rec.hub ? "" : " — no connection to miss"}.</p>`
               : `<p>Your booking is Murphy's top option of ${t.options.length} it found.${booked.same_region ? "" : " Its connection is outside your origin's group."}</p>`}
    <div class="trust ${abnormal ? "low" : "mid"}">${abnormal
      ? "<b>Trust: low.</b> Ranking advice held up best on days like this; the delay range below did not."
      : "<b>Trust: normal.</b> On ordinary days the advice rarely changes, and that is expected."}</div>
    <div class="actions">
      ${switchIt ? `<button class="btn primary small" id="b-accept">Switch to this</button>` : `<button class="btn primary small" id="b-accept">Keep my booking</button>`}
      <button class="btn ghost small" id="b-reject">Not for me…</button>
    </div>`;
  $("#b-accept").onclick = () => sendFeedback("accept_pick", rec.id, null, "Thanks — recorded.");
  $("#b-reject").onclick = () => {
    const why = prompt("What's wrong with this pick? (e.g. too early, wrong airline, I trust my booking)");
    if (why !== null) sendFeedback("reject_pick", rec.id, why || "no reason", "Recorded. Rule out a hub below to see what changes.");
  };

  renderStrip(); renderBand(); renderOptions(); renderMap(); renderWhy(); renderOwed(); renderLog();
  $("#prov").innerHTML = `<b>Where every number comes from.</b>
    <span class="pill tier">Observed</span> BTS On-Time flights for ${t.day}, airport-hour late shares ·
    <span class="pill tier">Derived</span> risk score = average share of departures late at each leg's origin in the last complete hour before the decision, +0.25 if the hub is in the origin's group; groups = Leiden on distance (one fixed map); abnormal = robust z ≥ 3 against the same airport-hour in 2023 ·
    <span class="pill tier">Documented</span> DOT dashboard and contracts of carriage, quoted ·
    <span class="pill tier">Session</span> your deadline and the hubs you rule out ·
    <span class="pill ai">AI-written</span> replay captions only, checked against the computed facts. Flight times are local at each airport; network-wide hours are UTC.`;
}

const optName = (o) => o.hub ? `${o.legs.map((l) => l.carrier + " " + l.flight_number).join(" + ")} via ${o.hub}` : `${o.legs[0].carrier} ${o.legs[0].flight_number} direct`;

function renderStrip() {
  const t = state.trip, legs = t.booked.legs;
  const t0 = Date.parse(legs[0].dep), t1 = Date.parse(legs[legs.length - 1].arr);
  const dl = state.deadline !== null && state.deadline !== undefined ? t1 + state.deadline * 60000 : null;
  const end = Math.max(t1, dl || 0), span = end - t0 || 1;
  const x = (ms) => (100 * (ms - t0)) / span * 0.92 + 4;
  let html = "";
  legs.forEach((l, i) => {
    const a = x(Date.parse(l.dep)), b = x(Date.parse(l.arr));
    html += `<div class="bar" style="left:${a}%;width:${b - a}%"></div>
      <div class="lbl" style="left:${(a + b) / 2}%">${l.carrier} ${l.flight_number} · ${l.origin}→${l.dest}</div>
      <div class="t" style="left:${a}%">${l.dep_local}</div><div class="t" style="left:${b}%">${l.arr_local}</div>`;
    if (i < legs.length - 1) {
      const c = x(Date.parse(legs[i + 1].dep));
      const gap = Math.round((Date.parse(legs[i + 1].dep) - Date.parse(l.arr)) / 60000);
      html += `<div class="gap" style="left:${b}%;width:${c - b}%"></div>
        <div class="lbl" style="left:${(b + c) / 2}%;top:70px;color:var(--ink-2);font-weight:500">${gap} min to connect in ${l.dest}</div>`;
    }
  });
  if (dl) html += `<div class="ddl" style="left:${x(dl)}%" title="your deadline"></div>`;
  $("#strip").innerHTML = `<h3>Your booked trip</h3><div class="strip">${html}</div>
    <p class="fine">${legs.length > 1 ? "Murphy's single connection rule: 45 minutes minimum." : "Direct flight."}
    ${dl ? `Red line: your deadline, ${state.deadline} min after the scheduled arrival.` : ""} Times are local at each airport.</p>`;
}

function renderBand() {
  const t = state.trip, bd = t.band, h = bd.histogram, q = bd.quantiles;
  const W = 520, H = 150, P = 26, max = Math.max(...h.counts, 1);
  const lo = h.edges[0], hi = h.edges[h.edges.length - 1];
  const X = (v) => P + ((v - lo) / (hi - lo)) * (W - 2 * P);
  const bars = h.counts.map((c, i) => `<rect x="${X(h.edges[i]) + 1}" y="${H - 28 - (c / max) * (H - 50)}" width="${X(h.edges[i + 1]) - X(h.edges[i]) - 2}" height="${(c / max) * (H - 50)}" rx="2" fill="#c9d4df"/>`).join("");
  const band = `<rect x="${X(q.p10)}" y="10" width="${X(q.p90) - X(q.p10)}" height="${H - 38}" fill="#1d3557" opacity=".07"/>
    <line x1="${X(q.p50)}" x2="${X(q.p50)}" y1="10" y2="${H - 28}" stroke="#1d3557" stroke-width="1.5"/>`;
  const d = t.deadline;
  const dl = d ? `<line x1="${X(Math.min(d.minutes, hi))}" x2="${X(Math.min(d.minutes, hi))}" y1="4" y2="${H - 28}" stroke="#d1495b" stroke-width="2" stroke-dasharray="4 3"/>
    <text x="${X(Math.min(d.minutes, hi)) + 4}" y="14" font-size="11" fill="#d1495b" font-weight="600">your deadline</text>` : "";
  const ticks = [-30, 0, 30, 60, 90, 120, 150].map((v) => `<text x="${X(v)}" y="${H - 10}" font-size="10.5" fill="#8a96a3" text-anchor="middle">${v > 0 ? "+" : ""}${v}</text>`).join("");
  let hindsight = "";
  if (state.hindsight) {
    const leg = t.booked.legs[t.booked.legs.length - 1];
    if (leg.outcome_arr_delay !== null) hindsight = `<line x1="${X(Math.min(Math.max(leg.outcome_arr_delay, lo), hi))}" x2="${X(Math.min(Math.max(leg.outcome_arr_delay, lo), hi))}" y1="10" y2="${H - 28}" stroke="#17212b" stroke-width="3"/>`;
  }
  $("#band").className = "card band";
  $("#band").innerHTML = `<h3>How late might you arrive?</h3>
    <p class="fine" style="margin-top:2px">${bd.n} comparable flights on ${bd.route.replace("-", " → ")}, same month of 2023, departing ${bd.hour_local - 1}:00–${bd.hour_local + 1}:59 local</p>
    <svg viewBox="0 0 ${W} ${H}">${band}${bars}${dl}${hindsight}${ticks}</svg>
    <p style="margin:4px 0 0">Typically <b>${q.p50 > 0 ? "+" : ""}${q.p50} min</b>; 8 in 10 between <b>${q.p10}</b> and <b>+${q.p90} min</b>.
    ${d ? `<b>${d.late} of ${d.n}</b> (${pct(d.share)}) of these arrived after your deadline.` : ""}</p>
    <div class="warnline"><b>This range is not calibrated.</b> On 19 July 2024, 4 of 20 DEN→SEA flights (20%) landed beyond the 90th percentile of 2023, where 10% is expected. It describes ordinary days; ${state.trip.banner.share_flagged >= 0.25 ? "today is not one." : "today looks like one."}</div>`;
}

function riskColor(o) { return o.same_region ? "var(--same)" : "var(--diff)"; }
function renderOptions() {
  const t = state.trip;
  const chips = state.exclude.map((h) => `<span class="chip">no ${h} <button data-un="${h}" title="undo">×</button></span>`).join("");
  $("#options").innerHTML = `<h3>Ranked by Murphy</h3>
    ${chips ? `<div class="excluded">Ruled out by you: ${chips}</div>` : ""}
    ${t.options.map((o, i) => `
      <div class="opt ${state.selected === o.id ? "sel" : ""}" data-id="${o.id}">
        <div class="edge" style="background:${riskColor(o)}"></div>
        <div>
          <div class="name">${i + 1}. ${o.hub ? "via " + o.hub : "Direct"} <span class="meta">· ${o.legs.map((l) => l.carrier + " " + l.flight_number).join(" + ")}</span></div>
          <div class="meta">leaves ${o.legs[0].dep_local} · arrives ${o.legs[o.legs.length - 1].arr_local}</div>
          <div class="tags">
            ${o.recommended_now ? '<span class="pill diff">Murphy\'s pick</span>' : ""}
            ${o.booked ? '<span class="pill tier">your booking</span>' : ""}
            ${o.hub ? (o.same_region ? `<span class="pill same">${o.hub} shares ${t.origin}'s group</span>` : `<span class="pill diff">${o.hub} is outside the group</span>`) : ""}
            ${state.hindsight ? (o.outcome_held ? '<span class="pill held">held</span>' : '<span class="pill broke">broke</span>') : ""}
          </div>
        </div>
        <div class="right">
          risk ${o.score.toFixed(2)}
          <div class="riskbar"><span style="width:${Math.min(100, o.score * 100)}%;background:${riskColor(o)}"></span></div>
          ${o.hub ? `<button class="btn small ghost" data-ex="${o.hub}" style="margin-top:6px">Rule out ${o.hub}</button>` : ""}
        </div>
      </div>`).join("")}
    <p class="fine">Risk = how late departures were running at each leg's origin in the last complete hour before you decided, plus a penalty when the connection shares the origin's group. ${t.n_options_total} options were found in all; the best 8 are shown.</p>`;
  document.querySelectorAll(".opt").forEach((el) => {
    el.addEventListener("mouseenter", () => highlight(el.dataset.id, true));
    el.addEventListener("mouseleave", () => highlight(el.dataset.id, false));
    el.addEventListener("click", (e) => { if (!e.target.closest("button")) select(el.dataset.id); });
  });
  document.querySelectorAll("[data-ex]").forEach((b) => b.addEventListener("click", async () => {
    state.exclude.push(b.dataset.ex);
    await sendFeedback("exclude_hub", b.dataset.ex, null, `Ruled out ${b.dataset.ex}. Re-ranked.`);
    loadTrip(state.trip.id);
  }));
  document.querySelectorAll("[data-un]").forEach((b) => b.addEventListener("click", () => {
    state.exclude = state.exclude.filter((h) => h !== b.dataset.un); loadTrip(state.trip.id);
  }));
}

/* curved great-circle-ish arc for display */
function arc(a, b, bend = 0.18, n = 40) {
  const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
  const dx = b[1] - a[1], dy = b[0] - a[0];
  const c = [mx + dx * bend, my - dy * bend];
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const s = i / n;
    pts.push([(1 - s) ** 2 * a[0] + 2 * (1 - s) * s * c[0] + s ** 2 * b[0], (1 - s) ** 2 * a[1] + 2 * (1 - s) * s * c[1] + s ** 2 * b[1]]);
  }
  return pts;
}
function hull(pts) {
  pts = pts.slice().sort((a, b) => a[1] - b[1] || a[0] - b[0]);
  if (pts.length < 3) return pts;
  const cross = (o, a, b) => (a[1] - o[1]) * (b[0] - o[0]) - (a[0] - o[0]) * (b[1] - o[1]);
  const lo = [], up = [];
  for (const p of pts) { while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], p) <= 0) lo.pop(); lo.push(p); }
  for (const p of pts.slice().reverse()) { while (up.length >= 2 && cross(up[up.length - 2], up[up.length - 1], p) <= 0) up.pop(); up.push(p); }
  return lo.slice(0, -1).concat(up.slice(0, -1));
}
function baseMap(el) {
  const m = L.map(el, { zoomControl: true, scrollWheelZoom: false, attributionControl: true });
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap", maxZoom: 10 }).addTo(m);
  return m;
}
function regionPolys(regions, layer, filterAirports) {
  const groups = {};
  for (const [ap, r] of Object.entries(regions)) {
    if (filterAirports && !filterAirports.has(ap)) continue;
    if (AIRPORTS[ap]) (groups[r] = groups[r] || []).push(AIRPORTS[ap]);
  }
  for (const [r, pts] of Object.entries(groups)) {
    if (pts.length < 3) continue;
    L.polygon(hull(pts), { color: REGION_COLORS[r % 9], weight: 1, fillOpacity: 0.08, opacity: 0.5, interactive: false }).addTo(layer);
  }
}

const lines = {};
function renderMap() {
  const t = state.trip;
  if (!routeMap) {
    routeMap = baseMap("routemap");
    regionLayer = L.layerGroup().addTo(routeMap);
    routeLayer = L.layerGroup().addTo(routeMap);
  }
  regionLayer.clearLayers(); routeLayer.clearLayers();
  if ($("#t-regions").checked) regionPolys(REPLAYS[t.day_label].regions, regionLayer);
  const O = [t.origin_lat, t.origin_lon], D = [t.dest_lat, t.dest_lon];
  const bounds = L.latLngBounds([O, D]);
  for (const k in lines) delete lines[k];
  t.options.slice().reverse().forEach((o, idx) => {
    const col = o.same_region ? "#d1495b" : "#2a9d8f";
    const top = o.recommended_now, bk = o.booked;
    const bend = 0.12 + (idx % 4) * 0.05;
    let pts;
    if (o.hub) { const H = [o.hub_lat, o.hub_lon]; bounds.extend(H); pts = arc(O, H, bend).concat(arc(H, D, bend)); }
    else pts = arc(O, D, (idx % 2 ? -1 : 1) * (0.04 + idx * 0.02));
    const line = L.polyline(pts, { color: col, weight: top ? 6 : bk ? 4.5 : 2.5, opacity: top || bk ? 0.95 : 0.55, dashArray: bk && !top ? "8 6" : null }).addTo(routeLayer);
    line.bindTooltip(`<b>${o.hub ? "via " + o.hub : "Direct"}</b> · ${o.legs.map((l) => l.carrier + " " + l.flight_number).join(" + ")}<br>leaves ${o.legs[0].dep_local} · risk ${o.score.toFixed(2)}${o.same_region ? "<br>shares the origin's group" : ""}`, { sticky: true });
    line.on("click", () => select(o.id));
    line.on("mouseover", () => highlight(o.id, true));
    line.on("mouseout", () => highlight(o.id, false));
    lines[o.id] = { line, base: { weight: line.options.weight, opacity: line.options.opacity } };
    if (o.hub) L.circleMarker([o.hub_lat, o.hub_lon], { radius: 6, color: col, weight: 2, fillColor: "#fff", fillOpacity: 1 })
      .bindTooltip(o.hub, { permanent: true, direction: "bottom", className: "", offset: [0, 6] }).addTo(routeLayer);
  });
  for (const [p, lbl] of [[O, t.origin], [D, t.dest]])
    L.circleMarker(p, { radius: 9, color: "#fff", weight: 2.5, fillColor: "#17212b", fillOpacity: 1 })
      .bindTooltip(lbl, { permanent: true, direction: "top", offset: [0, -8] }).addTo(routeLayer);
  routeBounds = bounds; routeMap.invalidateSize(); routeMap.fitBounds(bounds.pad(0.25));
}
function highlight(id, on) {
  const l = lines[id]; if (!l) return;
  l.line.setStyle(on ? { weight: l.base.weight + 3, opacity: 1 } : l.base);
  if (on) l.line.bringToFront();
  document.querySelectorAll(`.opt[data-id="${id}"]`).forEach((e) => e.classList.toggle("hover", on));
}
function select(id) {
  state.selected = state.selected === id ? null : id;
  document.querySelectorAll(".opt").forEach((e) => e.classList.toggle("sel", e.dataset.id === state.selected));
  renderWhy();
  if (state.selected) { $(".tabs").scrollIntoView({ behavior: "smooth", block: "nearest" }); }
}
$("#t-regions").addEventListener("change", renderMap);
$("#t-hindsight").addEventListener("change", (e) => { state.hindsight = e.target.checked; renderOptions(); renderBand(); renderWhy(); });

/* F4 — why */
function renderWhy() {
  const t = state.trip;
  const o = t.options.find((x) => x.id === state.selected) || t.booked;
  const reg = REPLAYS[t.day_label].regions;
  const legHtml = o.legs.map((l) => {
    const items = [];
    if (l.origin_late_hour_before !== null)
      items.push(`<b>${pct(l.origin_late_hour_before)}</b> of ${l.origin_flights_hour_before} departures from ${l.origin} left 15+ min late in the last complete hour before you decided <span class="src">Observed</span>`);
    if (l.origin_z_hour_before !== null)
      items.push(l.origin_z_hour_before >= 3
        ? `That is <b>unusual for ${l.origin}</b> at that hour (z = ${l.origin_z_hour_before} against 2023) <span class="src">Derived</span>`
        : `That is within ${l.origin}'s normal range for that hour (z = ${l.origin_z_hour_before}) <span class="src">Derived</span>`);
    if (l.inbound_aircraft)
      items.push(`The aircraft comes in from <b>${l.inbound_aircraft.from}</b> (scheduled ${l.inbound_aircraft.sched_dep_local}): ${l.inbound_aircraft.status_as_of_decision} as of ${t.decision_local} <span class="src">Observed · tail number</span>`);
    if (state.hindsight)
      items.push(l.outcome_cancelled ? `<b>Hindsight:</b> cancelled` : `<b>Hindsight:</b> left ${l.outcome_dep_delay} min, arrived ${l.outcome_arr_delay} min vs schedule`);
    return `<div class="why-leg"><div><b>${l.carrier} ${l.flight_number}</b><div class="meta">${l.origin} → ${l.dest}<br>${l.dep_local} – ${l.arr_local}</div></div><ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul></div>`;
  }).join("");
  const grp = o.hub
    ? (o.same_region
      ? `<p><b>${o.hub} and ${t.origin} are in the same group of nearby airports</b> (of ${new Set(Object.values(reg)).size} groups found by community detection). Nearby airports tend to fail together: on the winter-storm day, steering around the origin's group raised the share of top picks that held from 84.9% to 91.8%, on 159 real routing cases. Risk +0.25. <span class="src">Derived</span></p>`
      : `<p>${o.hub} is <b>outside ${t.origin}'s group</b>, so trouble at your origin is less likely to follow you there. <span class="src">Derived</span></p>`)
    : `<p>Direct — no connection to miss. <span class="src">Derived</span></p>`;
  $("#tab-why").innerHTML = `<p class="fine" style="margin-top:0">Showing: <b>${optName(o)}</b>. Click any option or map line to explain it instead.</p>${grp}${legHtml}`;
}

/* F3 — owed */
async function renderOwed() {
  const carriers = state.trip.carriers;
  const all = await Promise.all(carriers.map((c) => api(`/api/policy/${c}`)));
  $("#tab-owed").innerHTML = `<p class="fine" style="margin-top:0">Quoted, never interpreted into a number. Applies to the carriers on your booking and Murphy's pick: ${carriers.join(", ")}.</p>` +
    all.map((p) => `<h3 style="margin-top:10px">${p.carrier}</h3>` + (p.clauses.length ? p.clauses.slice(0, 8).map((c) => `
      <div class="clause"><span class="pill tier">Documented</span> <b>${c.disruption_type === "both" ? "Delay or cancellation" : c.disruption_type[0].toUpperCase() + c.disruption_type.slice(1)}</b>${c.controllable ? " · when the cause is in the airline's control" : ""}
      <q>${c.text.length > 360 ? c.text.slice(0, 360) + "…" : c.text}</q>
      <a href="${c.url}" target="_blank" rel="noopener">${c.source} — ${c.section}</a></div>`).join("")
      : `<p class="fine">No clauses on file for ${p.carrier}.</p>`)).join("");
}

/* feedback */
async function sendFeedback(kind, target, reason, msg) {
  const r = await api("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trip_id: state.trip && state.trip.id, day: state.trip && state.trip.day, kind, target, reason }) });
  toast(`${msg} (${r.total_feedback} items in the feedback log)`);
  renderLog();
}
async function renderLog() {
  const rows = await api("/api/feedback");
  $("#tab-log").innerHTML = rows.length
    ? `<p class="fine" style="margin-top:0">Every correction is logged to <code>results/codesign/feedback.jsonl</code>. Ruling out a hub re-ranks at once; the rest is kept to review, not yet learned from.</p>` +
      rows.slice(-12).reverse().map((r) => `<div class="ev"><span class="h">${r.kind.replace("_", " ")}</span> · ${r.target || ""} ${r.reason ? "— “" + r.reason + "”" : ""}<div class="fine" style="margin:0">${r.at} · ${r.trip_id || r.day || ""}</div></div>`).join("")
    : `<p class="fine">No feedback yet.</p>`;
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === b));
  for (const k of ["why", "owed", "log"]) $("#tab-" + k).hidden = k !== b.dataset.tab;
}));

/* ---------------- 3. replay ---------------- */
let replayMap, dotLayer, hullLayer, day = "storm", frame = 0, timer = null;
function initReplay() {
  replayMap = baseMap("replaymap");
  replayMap.setView([38.5, -96], 4);
  hullLayer = L.layerGroup().addTo(replayMap);
  dotLayer = L.layerGroup().addTo(replayMap);
  $("#r-days").innerHTML = ["weather", "storm", "calm"].map((k) => `<button data-d="${k}">${DAY_LABEL[k]}</button>`).join("");
  document.querySelectorAll("#r-days button").forEach((b) => b.addEventListener("click", () => showDay(b.dataset.d)));
  $("#r-slider").addEventListener("input", (e) => { stop(); drawFrame(+e.target.value); });
  $("#r-play").addEventListener("click", () => (timer ? stop() : play()));
  $("#r-groups").addEventListener("change", () => drawFrame(frame));
  showDay(day);
}
function stop() { clearInterval(timer); timer = null; $("#r-play").textContent = "▶"; }
function play() {
  const r = REPLAYS[day];
  if (frame >= r.frames.length - 1) frame = 0;
  $("#r-play").textContent = "❚❚";
  timer = setInterval(() => { if (frame >= r.frames.length - 1) return stop(); drawFrame(frame + 1); }, 900);
}
async function showDay(k) {
  if (!replayMap) { day = k; return initReplay(); }
  stop(); day = k;
  if (!REPLAYS[k] || !REPLAYS[k].captions) REPLAYS[k] = await api(`/api/replay/${k}`);
  const r = REPLAYS[k];
  document.querySelectorAll("#r-days button").forEach((b) => b.classList.toggle("on", b.dataset.d === k));
  $("#r-title").textContent = `${DAY_LABEL[k]} — ${fmtDay(r.day)}`;
  $("#r-slider").max = r.frames.length - 1;
  const ticks = r.events.map((e) => { const i = r.frames.findIndex((f) => f.hour === e.hour); return i < 0 ? "" : `<div class="tick" style="left:${(100 * i) / (r.frames.length - 1)}%" title="${e.kind}"></div>`; }).join("");
  $("#r-track").querySelectorAll(".tick").forEach((x) => x.remove());
  $("#r-track").insertAdjacentHTML("afterbegin", ticks);
  hullLayer.clearLayers(); regionPolys(r.regions, hullLayer);
  $("#r-events").innerHTML = `<h3>Key moments</h3>` + r.captions.map((c, i) => `<div class="ev" data-h="${c.hour}" data-i="${c.facts.i}"><div class="h">${c.hour.replace("Z", " UTC")} · ${c.kind.replaceAll("_", " ")}</div>${c.text}</div>`).join("");
  document.querySelectorAll("#r-events .ev").forEach((e) => e.addEventListener("click", () => { stop(); drawFrame(r.frames.findIndex((f) => f.hour === e.dataset.h)); }));
  drawFrame(0);
  setTimeout(() => replayMap.invalidateSize(), 50);
}
function dotColor(a, groups, reg, ap) {
  if (groups) return REGION_COLORS[(reg[ap] ?? 8) % 9];
  if (a.z === null) return "#d6dde4";
  return a.z >= 6 ? "#b5179e" : a.flag ? "#e9a23b" : "#9fb3c8";
}
function drawFrame(i) {
  const r = REPLAYS[day]; frame = Math.max(0, i); const f = r.frames[frame];
  $("#r-slider").value = frame;
  $("#r-clock").textContent = f.hour.replace("Z", " UTC");
  dotLayer.clearLayers();
  const groups = $("#r-groups").checked;
  for (const [ap, a] of Object.entries(f.airports)) {
    const p = AIRPORTS[ap]; if (!p) continue;
    L.circleMarker(p, { radius: 3 + Math.sqrt(a.n) * 1.3, color: "#fff", weight: 1, fillColor: dotColor(a, groups, r.regions, ap), fillOpacity: 0.9 })
      .bindTooltip(`<b>${ap}</b> · ${f.hour.replace("Z", " UTC")}<br>${pct(a.late)} of ${a.n} departures late${a.z !== null ? `<br>z = ${a.z}${a.flag ? " — unusual for this hour" : ""}` : ""}`).addTo(dotLayer);
  }
  // caption = the latest event at or before this hour
  const past = r.captions.filter((c) => c.facts.i <= frame);
  const c = past[past.length - 1];
  $("#r-caption").innerHTML = c
    ? `<span class="pill ai">AI-written · ${c.source === "llm" ? "checked" : "template fallback"}</span>
       <div class="cap">${c.text}</div>
       <details><summary>The only facts it was given</summary><pre>${JSON.stringify(c.facts, null, 1)}</pre>
       Every number and airport in the caption was checked against these. ${c.source === "template" ? "The LLM's version failed that check, so a template is shown." : ""}</details>
       <div class="actions"><button class="btn small" id="flag-cap">Flag as wrong or misleading</button></div>`
    : `<span class="pill ai">AI-written</span><div class="cap">Press play. Captions appear at each key moment the data marks.</div>`;
  const fc = $("#flag-cap");
  if (fc) fc.onclick = async () => {
    const why = prompt("What's wrong with this caption?");
    if (why === null) return;
    const res = await api("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ day: r.day, kind: "flag_caption", target: c.hour + " " + c.kind, reason: why || "no reason" }) });
    toast(`Caption flagged for review (${res.total_feedback} items logged)`);
  };
  const spark = r.frames.map((x, j) => `${(j / (r.frames.length - 1)) * 300},${50 - x.share_flagged * 46}`).join(" ");
  const ai = r.alarm_hour ? r.frames.findIndex((x) => x.hour === r.alarm_hour) : -1;
  $("#r-stats").innerHTML = `
    <div class="stat"><span>Airports abnormal now</span><span class="v">${f.scored ? pct(f.share_flagged) : "—"}</span></div>
    <div class="stat"><span>Departures late now</span><span class="v">${pct(f.mean_late)}</span></div>
    <svg viewBox="-4 0 308 56" style="width:100%;margin-top:6px">
      <polyline points="${spark}" fill="none" stroke="#1d3557" stroke-width="2"/>
      <line x1="0" x2="300" y1="${50 - 0.25 * 46}" y2="${50 - 0.25 * 46}" stroke="#d1495b" stroke-dasharray="3 3" stroke-width="1"/>
      ${ai >= 0 ? `<circle cx="${(ai / (r.frames.length - 1)) * 300}" cy="${50 - r.frames[ai].share_flagged * 46}" r="4" fill="#d1495b"/>` : ""}
      <line x1="${(frame / (r.frames.length - 1)) * 300}" x2="${(frame / (r.frames.length - 1)) * 300}" y1="0" y2="52" stroke="#8a96a3"/>
    </svg>
    <p class="fine">Share of airports abnormal, hour by hour. Dashed line: alarm threshold (25%). ${r.alarm_hour ? `Alarm fired ${r.alarm_hour.replace("Z", " UTC")}.` : "Never crossed."} ${r.region_count} groups of nearby airports, found once by Leiden on distance.</p>`;
  document.querySelectorAll("#r-events .ev").forEach((e) => e.classList.toggle("past", +e.dataset.i > frame));
}

/* ---------------- boot ---------------- */
(async function boot() {
  [AIRPORTS, TRIPS] = await Promise.all([api("/api/airports"), api("/api/trips")]);
  const rs = await Promise.all(["weather", "storm", "calm"].map((k) => api(`/api/replay/${k}`)));
  ["weather", "storm", "calm"].forEach((k, i) => (REPLAYS[k] = rs[i]));
  refreshForm(); dayCards();
  // deep links, for screenshots and sharing: #trip=<id>[&deadline=60][&hindsight=1], #replay=<day>[&hour=05:00Z]
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("trip")) {
    state.hindsight = h.get("hindsight") === "1"; $("#t-hindsight").checked = state.hindsight;
    await openTrip(h.get("trip"), h.has("deadline") ? +h.get("deadline") : 60);
    if (h.get("tab")) document.querySelector(`.tab[data-tab="${h.get("tab")}"]`).click();
  } else if (h.get("replay")) {
    go("replay"); await showDay(h.get("replay"));
    if (h.get("hour")) drawFrame(REPLAYS[day].frames.findIndex((f) => f.hour === h.get("hour")));
  }
})();
