"use strict";

// --- helpers -------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const main = $("#main");

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  // The server refuses changes without this header, so other sites cannot make them.
  const init = { method: options.method || "GET", headers: { "X-TradingAgents": "1" } };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

let toastTimer;
function toast(message, bad = false) {
  let el = $("#toast");
  if (!el) { el = document.createElement("div"); el.id = "toast"; document.body.append(el); }
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 7000 : 3500);
}

async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; }
  catch (e) { toast(e.message, true); throw e; }
}

const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const STATUS = {
  queued: "Wartet", running: "Läuft", paused: "Pausiert", completed: "Fertig",
  failed: "Fehler", cancelled: "Abgebrochen", interrupted: "Unterbrochen",
};
const AGENT_STATUS = { pending: "wartet", in_progress: "arbeitet", completed: "fertig" };

function fmtDate(iso, withYear = false) {
  if (!iso) return "–";
  const d = new Date(iso);
  const day = DAYS[(d.getDay() + 6) % 7];
  const date = d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", ...(withYear ? { year: "numeric" } : {}) });
  const time = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  return `${day}, ${date} ${time}`;
}

function fmtDur(s) {
  if (s == null) return "–";
  s = Math.max(0, Math.round(s));
  const hrs = Math.floor(s / 3600), min = Math.floor((s % 3600) / 60), sec = s % 60;
  if (hrs) return `${hrs} h ${min} min`;
  if (min) return `${min} min ${sec} s`;
  return `${sec} s`;
}

function fmtIn(iso) {
  if (!iso) return "–";
  const s = (new Date(iso) - new Date()) / 1000;
  if (s <= 0) return "jetzt";
  const d = Math.floor(s / 86400), hrs = Math.floor((s % 86400) / 3600), min = Math.floor((s % 3600) / 60);
  if (d) return `in ${d} T ${hrs} h`;
  if (hrs) return `in ${hrs} h ${min} min`;
  if (min) return `in ${min} min ${Math.floor(s % 60)} s`;
  return `in ${Math.floor(s)} s`;
}

function fmtTokens(n) {
  if (n == null) return "–";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + " M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + " k";
  return String(n);
}

const badge = s => `<span class="badge ${h(s)}">${h(STATUS[s] || s)}</span>`;
const ratingBadge = r => r ? `<span class="badge rating ${h(r)}">${h(r)}</span>` : `<span class="muted">–</span>`;
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

// --- global state --------------------------------------------------------------------

const app = { options: null, overview: null, view: null, timers: [], jobView: null };

function clearTimers() { app.timers.forEach(clearInterval); app.timers = []; }
function every(ms, fn) { app.timers.push(setInterval(fn, ms)); }

// --- header: what runs now, what starts next -----------------------------------------

function renderChips() {
  const o = app.overview;
  if (!o) return;
  const chips = [];
  if (o.current) {
    const c = o.current;
    const p = c.progress ? ` · ${c.progress.done}/${c.progress.total}` : "";
    chips.push(`<a class="chip link" href="#/job/${h(c.id)}">${c.status === "paused" ? "⏸" : "●"} <b>${h(c.ticker)}</b> ${h(STATUS[c.status])}${p} · ${fmtDur(c.elapsed_s)}</a>`);
  } else {
    chips.push(`<span class="chip">○ Kein Lauf aktiv</span>`);
  }
  if (o.queued.length) chips.push(`<a class="chip link" href="#/jobs"><b>${o.queued.length}</b> in Warteschlange</a>`);
  if (o.queue_paused) chips.push(`<a class="chip link" href="#/jobs" style="color:var(--warn)">⏸ Warteschlange pausiert</a>`);
  const next = o.upcoming[0];
  if (next) chips.push(`<a class="chip link" href="#/schedules">⏱ Nächster Start: <b>${h(next.name)}</b> ${fmtDate(next.next_run_at)} (${fmtIn(next.next_run_at)})</a>`);
  $("#chips").innerHTML = chips.join("");
  $("#count-queue").textContent = o.queued.length + (o.current ? 1 : 0) || "";
}

async function refreshOverview() {
  try {
    app.overview = await api("/api/overview");
    renderChips();
  } catch { /* the server may be restarting */ }
}

// --- routing -------------------------------------------------------------------------

const views = {};

async function route() {
  clearTimers();
  const [, view = "live", arg] = location.hash.split("/");
  app.view = view;
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.view === view || (view === "job" && a.dataset.view === "jobs")));
  const render = views[view] || views.live;
  main.innerHTML = `<div class="empty">Lädt …</div>`;
  try { await render(arg); }
  catch (e) { main.innerHTML = `<div class="error-box">${h(e.message)}</div>`; }
}

// --- the settings fields, shared by the forms ----------------------------------------

function providerOf(key) { return app.options.providers.find(p => p.key === key) || { quick: [], deep: [], thinking_values: [] }; }

function settingsFields(v, opts = {}) {
  const o = app.options;
  const prov = providerOf(v.llm_provider);
  const analysts = o.analysts.map(a => `
    <label class="check"><input type="checkbox" name="analysts" value="${h(a.key)}" ${v.analysts.includes(a.key) ? "checked" : ""}>${h(a.name)}</label>`).join("");
  return `
    <div class="field wide"><span>Analysten</span><div class="checks">${analysts}</div></div>
    <label class="field"><span>Research-Tiefe (Debattenrunden)</span>
      <select name="research_depth">${o.depths.map(d => `<option value="${d.value}" ${d.value == v.research_depth ? "selected" : ""}>${h(d.label)}</option>`).join("")}</select></label>
    <label class="field"><span>Ausgabesprache der Berichte</span>
      <select name="output_language">${o.languages.map(l => `<option ${l === v.output_language ? "selected" : ""}>${h(l)}</option>`).join("")}</select></label>
    <label class="field"><span>LLM-Provider</span>
      <select name="llm_provider">${o.providers.map(p => `<option value="${h(p.key)}" ${p.key === v.llm_provider ? "selected" : ""}>${h(p.name)}</option>`).join("")}</select>
      <small class="muted" data-provider-status></small></label>
    <label class="field"><span>Quick-Think-Modell (Analysten, Debatten, Trader)</span>
      <input type="text" name="quick_think_llm" list="dl-quick" value="${h(v.quick_think_llm)}" required></label>
    <label class="field"><span>Deep-Think-Modell (Research &amp; Portfolio Manager)</span>
      <input type="text" name="deep_think_llm" list="dl-deep" value="${h(v.deep_think_llm)}" required></label>
    <label class="field" data-thinking ${prov.thinking ? "" : "hidden"}><span>Denk-Aufwand (Effort)</span>
      <select name="thinking">${(prov.thinking_values || []).map(x => `<option value="${h(x)}" ${x === (v[prov.thinking] || "") ? "selected" : ""}>${x ? h(x) : "Provider-Standard"}</option>`).join("")}</select></label>
    <datalist id="dl-quick">${prov.quick.map(m => `<option value="${h(m.value)}">${h(m.label)}</option>`).join("")}</datalist>
    <datalist id="dl-deep">${prov.deep.map(m => `<option value="${h(m.value)}">${h(m.label)}</option>`).join("")}</datalist>
    <details class="advanced field wide" ${opts.openAdvanced ? "open" : ""}><summary>Erweitert</summary>
      <div class="form" style="margin-top:10px">
        <label class="field"><span>Max. Tool-Runden pro Analyst</span><input type="number" min="1" max="45" name="max_tool_rounds" value="${h(v.max_tool_rounds)}"></label>
        <label class="field"><span>Backend-URL (leer = Provider-Standard)</span><input type="text" name="backend_url" value="${h(v.backend_url || "")}" placeholder="z. B. http://localhost:11434/v1"></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="checkpoint_enabled" ${v.checkpoint_enabled ? "checked" : ""}>Checkpoints (abgebrochene Läufe setzen beim Wiederholen fort)</label>
      </div>
    </details>`;
}

function bindSettingsFields(root, v) {
  const sel = $("[name=llm_provider]", root);
  const status = $("[data-provider-status]", root);
  async function check() {
    status.textContent = "prüfe …";
    try {
      const s = await api(`/api/providers/${encodeURIComponent(sel.value)}`);
      status.innerHTML = s.ready
        ? `<span style="color:var(--good)">✓ bereit</span>${s.detail ? " · " + h(s.detail) : ""}`
        : `<span style="color:var(--bad)">✗ ${h(s.problem)}</span> <a href="#/keys">→ Key eintragen</a>`;
    } catch (e) { status.textContent = e.message; }
  }
  sel.addEventListener("change", () => {
    const p = providerOf(sel.value);
    $("#dl-quick", root).innerHTML = p.quick.map(m => `<option value="${h(m.value)}">${h(m.label)}</option>`).join("");
    $("#dl-deep", root).innerHTML = p.deep.map(m => `<option value="${h(m.value)}">${h(m.label)}</option>`).join("");
    if (p.quick[0]) $("[name=quick_think_llm]", root).value = p.quick[0].value;
    if (p.deep[0]) $("[name=deep_think_llm]", root).value = p.deep[0].value;
    const t = $("[data-thinking]", root);
    t.hidden = !p.thinking;
    $("[name=thinking]", t).innerHTML = (p.thinking_values || []).map(x => `<option value="${h(x)}">${x ? h(x) : "Provider-Standard"}</option>`).join("");
    check();
  });
  check();
}

function readSettingsFields(root) {
  const val = n => $(`[name=${n}]`, root)?.value;
  const prov = providerOf(val("llm_provider"));
  const s = {
    llm_provider: val("llm_provider"),
    quick_think_llm: val("quick_think_llm").trim(),
    deep_think_llm: val("deep_think_llm").trim(),
    research_depth: Number(val("research_depth")),
    output_language: val("output_language"),
    analysts: $$("[name=analysts]:checked", root).map(i => i.value),
    max_tool_rounds: Number(val("max_tool_rounds")),
    backend_url: val("backend_url").trim(),
    checkpoint_enabled: $("[name=checkpoint_enabled]", root).checked,
    anthropic_effort: "", openai_reasoning_effort: "", google_thinking_level: "",
  };
  if (prov.thinking) s[prov.thinking] = val("thinking");
  return s;
}

// --- new analysis --------------------------------------------------------------------

views.new = async () => {
  const v = await api("/api/settings");
  main.innerHTML = `
    <h1>Neue Analyse</h1>
    <form class="panel" id="f">
      <div class="form">
        <label class="field"><span>Ticker (mehrere mit Komma)</span><input type="text" name="tickers" placeholder="NVDA, AAPL, BTC-USD" required autofocus></label>
        <label class="field"><span>Analysedatum</span><input type="date" name="analysis_date" value="${today()}" max="${today()}"></label>
        ${settingsFields(v)}
        <div class="field wide row spread">
          <label class="check"><input type="checkbox" name="save_as_default">Diese Einstellungen als Standard speichern</label>
          <button class="primary" type="submit">▶ In Warteschlange &amp; starten</button>
        </div>
      </div>
    </form>
    <p class="muted small">Läufe werden nacheinander abgearbeitet. Jeder Ticker wird ein eigener Lauf. Krypto (z. B. BTC-USD) läuft ohne Fundamental-Analyst.</p>`;
  const f = $("#f");
  bindSettingsFields(f, v);
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const jobs = await act(() => api("/api/jobs", { method: "POST", body: {
      tickers: $("[name=tickers]", f).value,
      analysis_date: $("[name=analysis_date]", f).value,
      settings: readSettingsFields(f),
      save_as_default: $("[name=save_as_default]", f).checked,
    } }), "Zur Warteschlange hinzugefügt");
    await refreshOverview();
    location.hash = jobs.length === 1 ? `#/job/${jobs[0].id}` : "#/jobs";
  });
};

// --- one run: live or finished -------------------------------------------------------

views.live = async () => {
  await refreshOverview();
  const cur = app.overview.current;
  if (cur) return views.job(cur.id, true);
  const last = app.overview.jobs.find(j => j.status !== "queued");
  main.innerHTML = `
    <h1>Live</h1>
    <div class="panel empty">
      <p>Gerade läuft keine Analyse.${app.overview.queued.length ? ` ${app.overview.queued.length} Lauf/Läufe warten${app.overview.queue_paused ? " (Warteschlange pausiert)" : ""}.` : ""}</p>
      <div class="row" style="justify-content:center">
        <a class="btn primary" href="#/new">＋ Neue Analyse</a>
        ${last ? `<a class="btn" href="#/job/${h(last.id)}">Letzten Lauf ansehen (${h(last.ticker)})</a>` : ""}
      </div>
    </div>`;
  every(2000, async () => { if (app.overview?.current) route(); });
};

function controls(j) {
  const b = [];
  if (j.status === "running") b.push(`<button data-act="pause">⏸ Pausieren</button>`);
  if (j.status === "paused") b.push(`<button class="primary" data-act="resume">▶ Fortsetzen</button>`);
  if (["running", "paused", "queued"].includes(j.status)) b.push(`<button class="danger" data-act="cancel">■ Abbrechen</button>`);
  if (["completed", "failed", "cancelled", "interrupted"].includes(j.status)) b.push(`<button data-act="retry">↻ ${j.status === "completed" ? "Erneut ausführen" : "Wiederholen"}</button>`);
  if (j.report_name) b.push(`<a class="btn" target="_blank" href="/reports/${encodeURIComponent(j.report_name)}/complete_report.html">HTML-Bericht ↗</a>`);
  b.push(`<button data-act="log">Log</button>`);
  return b.join("");
}

function bindControls(root, id, after) {
  root.addEventListener("click", async e => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const a = btn.dataset.act;
    if (a === "log") return showLog(id);
    if (a === "cancel" && !confirm("Diesen Lauf abbrechen?")) return;
    btn.disabled = true;
    try {
      const r = await act(() => api(`/api/jobs/${id}/${a}`, { method: "POST" }),
        { pause: "Pausiert – laufende Aufrufe werden noch beendet", resume: "Fortgesetzt", cancel: "Wird abgebrochen …", retry: "Erneut eingereiht" }[a]);
      if (a === "retry") { location.hash = `#/job/${r.id}`; return; }
      after && after();
    } finally { btn.disabled = false; }
  });
}

async function showLog(id) {
  const text = await api(`/api/jobs/${id}/log`);
  const w = window.open("", "_blank");
  w.document.write(`<title>Log</title><pre style="white-space:pre-wrap;font:12px monospace">${h(text || "(leer)")}</pre>`);
}

function statsCards(j) {
  const s = j.stats || {};
  const p = j.progress;
  const pct = p && p.total ? Math.round(100 * p.done / p.total) : (j.status === "completed" ? 100 : 0);
  return `
    <div class="stat"><div class="label">Fortschritt</div><div class="value">${p ? `${p.done}/${p.total}` : (j.status === "completed" ? "fertig" : "–")}</div><div class="bar"><i style="width:${pct}%"></i></div></div>
    <div class="stat"><div class="label">Laufzeit</div><div class="value">${fmtDur(j.elapsed_s)}</div></div>
    <div class="stat"><div class="label">LLM-Aufrufe</div><div class="value">${s.llm_calls ?? "–"}</div></div>
    <div class="stat"><div class="label">Tool-Aufrufe</div><div class="value">${s.tool_calls ?? "–"}</div></div>
    <div class="stat"><div class="label">Tokens ein / aus</div><div class="value">${fmtTokens(s.tokens_in)} / ${fmtTokens(s.tokens_out)}</div></div>
    <div class="stat"><div class="label">Rating</div><div class="value">${ratingBadge(j.rating)}</div></div>`;
}

function boardHtml(agents) {
  return agents.map(t => `
    <div class="team"><h4>${h(t.team)}</h4>
      ${t.agents.map(a => `<div class="agent ${h(a.status)}" title="${h(AGENT_STATUS[a.status] || a.status)}"><span class="sdot ${h(a.status)}"></span>${h(a.name)}</div>`).join("")}
    </div>`).join("");
}

function feedHtml(feed, filter) {
  const items = feed.filter(i => filter === "all" || i.kind === filter || (filter === "Agent" && i.kind === "Agent"));
  if (!items.length) return `<div class="empty small">Noch keine Nachrichten.</div>`;
  return items.map(i => `
    <div class="item ${h(i.kind)}"><span class="time">${h(i.time)}</span><span class="kind ${h(i.kind)}">${h(i.kind)}</span><div class="text">${h(i.text)}</div></div>`).join("");
}

views.job = async (id, fromLive = false) => {
  const st = app.jobView = { id, rev: null, tab: null, filter: "all", follow: true, sections: [], data: null };
  let j = await api(`/api/jobs/${id}`);
  main.innerHTML = `
    <div class="row spread" style="margin-bottom:14px">
      <div>
        <h1 style="margin:0">${h(j.ticker)} <span class="muted" style="font-weight:400">· ${h(j.analysis_date)}</span> <span id="j-badge">${badge(j.status)}</span></h1>
        <div class="muted small" style="margin-top:4px">${h(j.provider)} · ${h(j.quick_model)} / ${h(j.deep_model)} · Tiefe ${h(j.depth)} · ${h(j.analysts.join(", "))}${j.schedule_id ? " · aus Zeitplan" : ""}${j.retry_of ? ` · Wiederholung von <a href="#/job/${h(j.retry_of)}">${h(j.retry_of)}</a>` : ""}</div>
      </div>
      <div class="actions" id="j-ctrl">${controls(j)}</div>
    </div>
    <div id="j-error"></div>
    <div class="grid stats" id="j-stats">${statsCards(j)}</div>
    <div class="panel" style="margin-top:16px"><div class="board" id="j-board">${boardHtml(j.agents)}</div></div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="panel">
        <div class="row spread"><h2 style="margin:0">Nachrichten &amp; Tools</h2>
          <div class="row">
            <select id="j-filter" style="width:auto"><option value="all">Alle</option><option value="Agent">Agenten</option><option value="Tool">Tool-Aufrufe</option><option value="Data">Tool-Ergebnisse</option><option value="System">System</option></select>
            <label class="check small"><input type="checkbox" id="j-follow" checked>Mitlaufen</label>
          </div></div>
        <div class="feed" id="j-feed"></div>
      </div>
      <div class="panel">
        <div class="tabs" id="j-tabs"></div>
        <div class="report md" id="j-report"></div>
      </div>
    </div>`;
  bindControls($("#j-ctrl"), id, () => update(true));
  $("#j-filter").addEventListener("change", e => { st.filter = e.target.value; drawFeed(); });
  $("#j-follow").addEventListener("change", e => { st.follow = e.target.checked; });
  $("#j-feed").addEventListener("click", e => e.target.classList.contains("text") && e.target.classList.toggle("open"));
  $("#j-tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { st.tab = b.dataset.key; drawReport(); } });

  function drawFeed() {
    const el = $("#j-feed");
    if (!el || !st.data) return;
    el.innerHTML = st.data.feed.length || st.data.status === "queued" || ["running", "paused"].includes(st.data.status)
      ? feedHtml(st.data.feed, st.filter)
      : `<div class="empty small">Der Nachrichtenverlauf wird nur während des Laufs gezeigt – das komplette Log gibt es über „Log“.</div>`;
    if (st.follow) el.scrollTop = el.scrollHeight;
  }

  function drawReport() {
    const tabs = [...(st.data?.current_report ? [{ key: "_current", agent: "● Aktuell" }] : []), ...st.sections];
    if (!tabs.length) {
      $("#j-tabs").innerHTML = "";
      $("#j-report").innerHTML = `<div class="empty">${st.data.status === "queued" ? "Wartet in der Warteschlange …" : "Noch keine Berichte."}</div>`;
      return;
    }
    if (!st.tab || !tabs.some(t => t.key === st.tab)) st.tab = tabs[0].key;
    $("#j-tabs").innerHTML = tabs.map(t => `<button class="${t.key === st.tab ? "active" : ""}" data-key="${h(t.key)}">${h(t.agent)}</button>`).join("");
    const sec = st.tab === "_current" ? { html: st.data.current_report } : st.sections.find(s => s.key === st.tab);
    const r = $("#j-report"), keep = r.scrollTop;
    r.innerHTML = sec ? sec.html : "";
    r.scrollTop = keep;
  }

  function draw(data) {
    st.data = data;
    if (data.sections) st.sections = data.sections;
    st.rev = data.rev;
    $("#j-badge").innerHTML = badge(data.status);
    $("#j-ctrl").innerHTML = controls(data);
    $("#j-stats").innerHTML = statsCards(data);
    $("#j-board").innerHTML = boardHtml(data.agents);
    $("#j-error").innerHTML = data.error ? `<div class="error-box" style="margin-bottom:14px"><b>${h(STATUS[data.status])}:</b> ${h(data.error)}${data.status === "interrupted" || data.status === "failed" ? " – „Wiederholen“ setzt mit Checkpoints dort fort, wo der Lauf stand." : ""}${data.traceback ? `<details><summary>Details</summary><pre class="log">${h(data.traceback)}</pre></details>` : ""}</div>` : "";
    drawFeed();
    drawReport();
  }

  async function update(force = false) {
    if (app.view !== "job" && !fromLive) return;
    if (app.jobView !== st) return;
    try {
      const data = await api(`/api/jobs/${id}${force || st.rev == null ? "" : `?rev=${st.rev}`}`);
      draw(data);
      if (fromLive && !["running", "paused", "queued"].includes(data.status)) {
        // The live view moves on to the next run once this one ends.
        await refreshOverview();
        if (app.overview.current && app.overview.current.id !== id) route();
      }
    } catch (e) { /* keep the last view */ }
  }

  draw(j);
  every(1500, () => { if (["running", "paused", "queued"].includes(st.data.status) || fromLive) update(); });
};

// --- queue and history ---------------------------------------------------------------

views.jobs = async () => {
  main.innerHTML = `<div id="vj">
    <div class="row spread"><h1>Warteschlange &amp; Verlauf</h1>
      <div class="row"><button id="q-toggle"></button><a class="btn primary" href="#/new">＋ Neue Analyse</a></div></div>
    <div class="panel"><h2>Aktuell &amp; wartend</h2><div id="q-list"></div></div>
    <div class="panel">
      <div class="row spread"><h2 style="margin:0">Verlauf</h2>
        <div class="row"><input type="text" id="h-search" placeholder="Ticker suchen …" style="width:180px">
          <select id="h-status" style="width:auto"><option value="">Alle Status</option>${Object.entries(STATUS).filter(([k]) => k !== "queued").map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div></div>
      <div class="table-wrap" id="h-list" style="margin-top:10px"></div>
    </div></div>`;
  const draw = () => {
    const o = app.overview;
    $("#q-toggle").textContent = o.queue_paused ? "▶ Warteschlange fortsetzen" : "⏸ Warteschlange pausieren";
    $("#q-toggle").className = o.queue_paused ? "primary" : "";
    const active = [o.current, ...o.queued].filter(Boolean);
    $("#q-list").innerHTML = active.length ? `<table class="list"><thead><tr><th>#</th><th>Status</th><th>Ticker</th><th>Datum</th><th>Modelle</th><th>Fortschritt</th><th>Eingereiht</th><th></th></tr></thead><tbody>
      ${active.map((j, i) => `<tr><td class="muted">${i + 1}</td><td>${badge(j.status)}</td><td><a href="#/job/${h(j.id)}"><b>${h(j.ticker)}</b></a></td><td>${h(j.analysis_date)}</td>
        <td class="small">${h(j.provider)} · ${h(j.quick_model)} / ${h(j.deep_model)}</td>
        <td>${j.progress ? `${j.progress.done}/${j.progress.total} · ${fmtDur(j.elapsed_s)}` : "–"}</td><td class="small">${fmtDate(j.created_at)}</td>
        <td><div class="actions" data-id="${h(j.id)}">
          ${j.status === "queued" ? `<button class="small" data-act="up" title="Früher">↑</button><button class="small" data-act="down" title="Später">↓</button>` : ""}
          ${j.status === "running" ? `<button class="small" data-act="pause">⏸</button>` : ""}${j.status === "paused" ? `<button class="small primary" data-act="resume">▶</button>` : ""}
          <button class="small danger" data-act="cancel">■</button></div></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty small">Nichts aktiv. ${o.queue_paused ? "Die Warteschlange ist pausiert." : ""}</div>`;
    const q = $("#h-search").value.trim().toUpperCase(), fs = $("#h-status").value;
    const past = o.jobs.filter(j => !["queued", "running", "paused"].includes(j.status) && (!q || j.ticker.includes(q)) && (!fs || j.status === fs));
    $("#h-list").innerHTML = past.length ? `<table class="list"><thead><tr><th>Status</th><th>Ticker</th><th>Datum</th><th>Rating</th><th>Modelle</th><th>Gestartet</th><th>Dauer</th><th>Tokens</th><th></th></tr></thead><tbody>
      ${past.map(j => `<tr class="clickable" data-open="${h(j.id)}"><td>${badge(j.status)}</td><td><b>${h(j.ticker)}</b></td><td>${h(j.analysis_date)}</td><td>${ratingBadge(j.rating)}</td>
        <td class="small">${h(j.quick_model)} / ${h(j.deep_model)}</td><td class="small">${fmtDate(j.started_at || j.created_at)}</td><td>${fmtDur(j.elapsed_s)}</td>
        <td class="small">${j.stats ? `${fmtTokens(j.stats.tokens_in)} / ${fmtTokens(j.stats.tokens_out)}` : "–"}</td>
        <td><div class="actions" data-id="${h(j.id)}">${j.report_name ? `<a class="btn small" target="_blank" href="/reports/${encodeURIComponent(j.report_name)}/complete_report.html">HTML ↗</a>` : ""}<button class="small" data-act="retry">↻</button><button class="small danger" data-act="delete">✕</button></div></td></tr>`).join("")}
      </tbody></table>` : `<div class="empty small">Noch keine abgeschlossenen Läufe.</div>`;
  };
  $("#vj").addEventListener("click", async e => {
    const btn = e.target.closest("[data-act]");
    if (btn) {
      e.stopPropagation();
      const id = btn.closest("[data-id]").dataset.id, a = btn.dataset.act;
      if (a === "cancel" && !confirm("Lauf abbrechen?")) return;
      if (a === "delete") {
        if (!confirm("Eintrag aus dem Verlauf löschen? (Berichte auf der Festplatte bleiben erhalten)")) return;
        await act(() => api(`/api/jobs/${id}`, { method: "DELETE" }), "Gelöscht");
      } else {
        await act(() => api(`/api/jobs/${id}/${a}`, { method: "POST" }));
      }
      await refreshOverview(); draw();
      return;
    }
    const row = e.target.closest("[data-open]");
    if (row && !e.target.closest("a")) location.hash = `#/job/${row.dataset.open}`;
  });
  $("#q-toggle").addEventListener("click", async () => {
    await act(() => api("/api/queue", { method: "POST", body: { paused: !app.overview.queue_paused } }),
      app.overview.queue_paused ? "Warteschlange läuft weiter" : "Warteschlange pausiert – der aktuelle Lauf läuft zu Ende");
    await refreshOverview(); draw();
  });
  $("#h-search").addEventListener("input", draw);
  $("#h-status").addEventListener("change", draw);
  await refreshOverview(); draw();
  every(2000, async () => { await refreshOverview(); draw(); });
};

// --- schedules -----------------------------------------------------------------------

function weekdayPills(days) {
  return DAYS.map((d, i) => `<span class="wd ${days.includes(i) ? "on" : ""}">${d}</span>`).join("");
}

views.schedules = async () => {
  const list = await api("/api/schedules");
  main.innerHTML = `
    <div class="row spread"><h1>Zeitpläne</h1><button class="primary" id="s-new">＋ Neuer Zeitplan</button></div>
    <div id="s-form"></div>
    <div id="s-list"></div>
    <p class="muted small">Zeitpläne starten nur, solange die UI läuft. Ein Start, der verpasst wurde (Rechner aus oder im Ruhezustand), wird übersprungen und nicht nachgeholt.</p>`;
  const draw = items => {
    $("#s-list").innerHTML = items.length ? items.map(s => `
      <div class="panel sched" data-id="${h(s.id)}">
        <div>
          <div class="row"><h2 style="margin:0">${h(s.name)}</h2>${s.enabled ? "" : `<span class="badge">deaktiviert</span>`}</div>
          <div class="small" style="margin:6px 0">${weekdayPills(s.weekdays)} um <b>${h(s.times.join(", "))}</b> · Ticker: <b>${h(s.tickers.join(", "))}</b> · Analysedatum: ${s.date_mode === "previous_day" ? "voriger Handelstag" : "Starttag"}${s.settings ? ` · eigene Einstellungen (${h(s.settings.quick_think_llm)} / ${h(s.settings.deep_think_llm)})` : " · Standard-Einstellungen"}</div>
          <div class="small muted">Nächster Start: ${s.next_run_at ? `<b style="color:var(--text)">${fmtDate(s.next_run_at)}</b> · <span class="countdown" data-in="${h(s.next_run_at)}">${fmtIn(s.next_run_at)}</span>` : "–"} · Zuletzt: ${fmtDate(s.last_run_at)}</div>
          ${(s.history || []).length ? `<details class="small" style="margin-top:6px"><summary class="muted">Letzte Starts</summary>${s.history.map(r => `<div>${fmtDate(r.at)} · Datum ${h(r.date)} · ${r.jobs.map(id => `<a href="#/job/${h(id)}">${h(id.slice(0, 6))}</a>`).join(", ")}${r.errors.length ? ` · <span style="color:var(--bad)">${h(r.errors.join("; "))}</span>` : ""}</div>`).join("")}</details>` : ""}
        </div>
        <div class="actions" style="align-items:start">
          <label class="switch" title="Aktiv"><input type="checkbox" data-act="enabled" ${s.enabled ? "checked" : ""}><span></span></label>
          <button class="small" data-act="run">▶ Jetzt</button><button class="small" data-act="edit">Bearbeiten</button><button class="small danger" data-act="delete">✕</button>
        </div>
      </div>`).join("") : `<div class="panel empty">Noch keine Zeitpläne. Lege z. B. „Watchlist werktags 22:00“ an.</div>`;
  };
  const reload = async () => { draw(await api("/api/schedules")); refreshOverview(); };
  draw(list);
  $("#s-new").addEventListener("click", () => scheduleForm(null, reload));
  $("#s-list").addEventListener("click", async e => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const id = el.closest("[data-id]").dataset.id, a = el.dataset.act;
    const s = (await api("/api/schedules")).find(x => x.id === id);
    if (a === "edit") return scheduleForm(s, reload);
    if (a === "delete") { if (!confirm(`Zeitplan „${s.name}“ löschen?`)) return; await act(() => api(`/api/schedules/${id}`, { method: "DELETE" }), "Gelöscht"); }
    if (a === "run") await act(() => api(`/api/schedules/${id}/run`, { method: "POST" }), "Läufe eingereiht");
    if (a === "enabled") await act(() => api(`/api/schedules/${id}/enabled`, { method: "POST", body: { enabled: el.checked } }), el.checked ? "Aktiviert" : "Deaktiviert");
    reload();
  });
  every(1000, () => $$("[data-in]").forEach(el => { el.textContent = fmtIn(el.dataset.in); }));
  every(15000, reload);
};

async function scheduleForm(s, done) {
  const defaults = await api("/api/settings");
  const v = s || { name: "", tickers: [], times: ["22:00"], weekdays: [0, 1, 2, 3, 4], date_mode: "same_day", enabled: true, settings: null };
  const box = $("#s-form");
  box.innerHTML = `
    <form class="panel" id="sf" style="margin-bottom:16px">
      <h2>${s ? "Zeitplan bearbeiten" : "Neuer Zeitplan"}</h2>
      <div class="form">
        <label class="field"><span>Name</span><input type="text" name="name" value="${h(v.name)}" placeholder="z. B. Tech-Watchlist"></label>
        <label class="field"><span>Ticker (Komma-getrennt)</span><input type="text" name="tickers" value="${h(v.tickers.join(", "))}" placeholder="NVDA, MSFT, AAPL" required></label>
        <label class="field"><span>Uhrzeit(en), z. B. 08:30, 22:00</span><input type="text" name="times" value="${h(v.times.join(", "))}" required></label>
        <label class="field"><span>Analysedatum</span><select name="date_mode">
          <option value="same_day" ${v.date_mode !== "previous_day" ? "selected" : ""}>Tag des Starts (z. B. abends nach Börsenschluss)</option>
          <option value="previous_day" ${v.date_mode === "previous_day" ? "selected" : ""}>Voriger Handelstag (z. B. morgens vor Börsenstart)</option></select></label>
        <div class="field wide"><span>Wochentage</span><div class="checks">${DAYS.map((d, i) => `<label class="check"><input type="checkbox" name="weekdays" value="${i}" ${v.weekdays.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div></div>
        <label class="check field wide" style="flex-direction:row"><input type="checkbox" name="own" ${v.settings ? "checked" : ""}>Eigene Modell- &amp; Analyse-Einstellungen (sonst gelten die Standard-Einstellungen)</label>
        <div class="field wide" id="sf-settings" ${v.settings ? "" : "hidden"}><div class="form">${settingsFields(v.settings || defaults)}</div></div>
        <div class="field wide row spread">
          <label class="check"><input type="checkbox" name="enabled" ${v.enabled ? "checked" : ""}>Aktiv</label>
          <div class="row"><button type="button" id="sf-cancel">Abbrechen</button><button class="primary" type="submit">Speichern</button></div>
        </div>
      </div>
    </form>`;
  const f = $("#sf");
  bindSettingsFields($("#sf-settings"), v.settings || defaults);
  $("[name=own]", f).addEventListener("change", e => { $("#sf-settings").hidden = !e.target.checked; });
  $("#sf-cancel").addEventListener("click", () => { box.innerHTML = ""; });
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const body = {
      name: $("[name=name]", f).value, tickers: $("[name=tickers]", f).value, times: $("[name=times]", f).value,
      date_mode: $("[name=date_mode]", f).value, enabled: $("[name=enabled]", f).checked,
      weekdays: $$("[name=weekdays]:checked", f).map(i => Number(i.value)),
      settings: $("[name=own]", f).checked ? readSettingsFields($("#sf-settings")) : null,
    };
    await act(() => s ? api(`/api/schedules/${s.id}`, { method: "PUT", body }) : api("/api/schedules", { method: "POST", body }), "Gespeichert");
    box.innerHTML = "";
    done();
  });
  box.scrollIntoView({ behavior: "smooth" });
}

// --- decisions (memory log) ----------------------------------------------------------

views.decisions = async () => {
  const items = await api("/api/decisions");
  const settled = items.filter(i => !i.pending);
  const alphas = settled.map(i => parseFloat(i.alpha)).filter(x => !isNaN(x));
  const hits = alphas.filter(x => x > 0).length;
  main.innerHTML = `
    <h1>Entscheidungen &amp; Ergebnisse</h1>
    <p class="muted">Jede Entscheidung landet im Memory-Log. Sobald genug Kurse vorliegen, wird sie abgerechnet: Rendite und Alpha gegenüber dem Benchmark ab dem ersten Schlusskurs nach dem Analysedatum, dazu eine kurze Lektion, die spätere Analysen mitlesen.</p>
    <div class="grid stats">
      <div class="stat"><div class="label">Entscheidungen</div><div class="value">${items.length}</div></div>
      <div class="stat"><div class="label">Offen</div><div class="value">${items.length - settled.length}</div></div>
      <div class="stat"><div class="label">Abgerechnet</div><div class="value">${settled.length}</div></div>
      <div class="stat"><div class="label">Ø Alpha</div><div class="value">${alphas.length ? (alphas.reduce((a, b) => a + b, 0) / alphas.length).toFixed(2) + " %" : "–"}</div></div>
      <div class="stat"><div class="label">Positives Alpha</div><div class="value">${alphas.length ? `${hits}/${alphas.length}` : "–"}</div></div>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="row"><input type="text" id="d-search" placeholder="Ticker suchen …" style="width:200px"></div>
      <div class="table-wrap" id="d-list" style="margin-top:10px"></div></div>`;
  const draw = () => {
    const q = $("#d-search").value.trim().toUpperCase();
    const rows = items.filter(i => !q || i.ticker.toUpperCase().includes(q));
    $("#d-list").innerHTML = rows.length ? `<table class="list"><thead><tr><th>Datum</th><th>Ticker</th><th>Rating</th><th>Status</th><th>Rendite</th><th>Alpha</th><th>Haltedauer</th><th>Lektion</th></tr></thead><tbody>
      ${rows.map((i, n) => `<tr class="clickable" data-row="${n}"><td>${h(i.date)}</td><td><b>${h(i.ticker)}</b></td><td>${ratingBadge(i.rating)}</td>
        <td>${i.pending ? `<span class="badge queued">offen</span>` : `<span class="badge completed">abgerechnet</span>`}</td>
        <td>${h(i.return || "–")}</td><td style="color:${parseFloat(i.alpha) > 0 ? "var(--good)" : parseFloat(i.alpha) < 0 ? "var(--bad)" : "inherit"}">${h(i.alpha || "–")}</td><td>${h(i.holding || "–")}</td>
        <td class="small">${h((i.reflection || "").slice(0, 140))}${(i.reflection || "").length > 140 ? " …" : ""}</td></tr>
        <tr hidden data-detail="${n}"><td colspan="8"><div class="md">${i.reflection ? `<p><b>Lektion:</b> ${h(i.reflection)}</p>` : ""}${i.decision_html}</div></td></tr>`).join("")}
      </tbody></table>` : `<div class="empty small">Noch keine Entscheidungen im Memory-Log.</div>`;
  };
  $("#d-search").addEventListener("input", draw);
  $("#d-list").addEventListener("click", e => {
    const r = e.target.closest("[data-row]");
    if (r) { const d = $(`[data-detail="${r.dataset.row}"]`); d.hidden = !d.hidden; }
  });
  draw();
};

// --- saved reports -------------------------------------------------------------------

views.reports = async () => {
  const items = await api("/api/reports");
  main.innerHTML = `
    <h1>Gespeicherte Berichte</h1>
    <p class="muted">Alle unter <code>results_dir/reports</code> gespeicherten Berichte – aus der UI und aus dem Terminal.</p>
    <div class="panel"><div class="row"><input type="text" id="r-search" placeholder="Ticker suchen …" style="width:200px"></div>
    <div class="table-wrap" id="r-list" style="margin-top:10px"></div></div>`;
  const draw = () => {
    const q = $("#r-search").value.trim().toUpperCase();
    const rows = items.filter(i => !q || i.ticker.toUpperCase().includes(q));
    $("#r-list").innerHTML = rows.length ? `<table class="list"><thead><tr><th>Ticker</th><th>Gespeichert</th><th>Rating</th><th></th></tr></thead><tbody>
      ${rows.map(r => `<tr><td><b>${h(r.ticker)}</b></td><td>${fmtDate(r.saved_at, true)}</td><td>${ratingBadge(r.rating)}</td>
        <td><div class="actions">${r.html ? `<a class="btn small" target="_blank" href="/reports/${encodeURIComponent(r.name)}/complete_report.html">HTML ↗</a>` : ""}${r.markdown ? `<a class="btn small" target="_blank" href="/reports/${encodeURIComponent(r.name)}/complete_report.md">Markdown ↗</a>` : ""}</div></td></tr>`).join("")}
      </tbody></table>` : `<div class="empty small">Keine gespeicherten Berichte gefunden.</div>`;
  };
  $("#r-search").addEventListener("input", draw);
  draw();
};

// --- API keys ------------------------------------------------------------------------

views.keys = async () => {
  main.innerHTML = `<div id="vk"></div>`;
  const root = $("#vk");
  let open = null;   // the key whose editor is open

  const row = k => `
    <tr data-key="${h(k.name)}">
      <td style="width:34%"><b>${h(k.label)}</b><div class="small muted"><code>${h(k.name)}</code></div></td>
      <td style="width:22%">${k.set
        ? `<span class="badge completed">✓ gesetzt</span> <code class="small">${h(k.preview)}</code>${k.source === "environment" ? `<div class="small muted">aus Umgebungsvariable</div>` : ""}`
        : `<span class="badge">nicht gesetzt</span>`}</td>
      <td class="small muted">${h(k.help || "")}${k.url ? ` <a href="${h(k.url)}" target="_blank" rel="noopener">Key holen ↗</a>` : ""}</td>
      <td style="width:1%"><div class="actions">
        <button class="small" data-act="edit">${k.set ? "Ändern" : "Eintragen"}</button>
        ${k.set && k.source === "file" ? `<button class="small danger" data-act="remove">Entfernen</button>` : ""}</div></td>
    </tr>
    ${open === k.name ? `
    <tr data-key="${h(k.name)}"><td colspan="4">
      <form class="row" data-form>
        <input type="${k.secret ? "password" : "text"}" name="value" placeholder="${h(k.placeholder || (k.secret ? "Neuen Key einfügen" : ""))}" value="${k.secret ? "" : h(k.preview)}" autocomplete="off" class="grow" style="max-width:520px" autofocus>
        ${k.secret ? `<label class="check small"><input type="checkbox" data-show>anzeigen</label>` : ""}
        <button class="primary small" type="submit">Speichern</button><button class="small" type="button" data-act="close">Abbrechen</button>
      </form></td></tr>` : ""}`;

  async function draw(data) {
    data = data || await api("/api/keys");
    root.innerHTML = `
      <h1>API-Keys &amp; Datenquellen</h1>
      <div class="note small" style="margin-bottom:16px">
        Gespeichert in <code>${h(data.env_file)}</code>${data.env_file_exists ? "" : " (wird beim ersten Speichern angelegt)"} – dieselbe Datei, die auch das Terminal liest.
        Neue Keys gelten sofort, ohne Neustart. Angezeigt werden nur die letzten vier Zeichen.
        Keys, die als Umgebungsvariable gesetzt sind, kannst du hier überschreiben, aber nicht entfernen.
      </div>
      ${data.groups.map(g => `
        <div class="panel"><h2>${h(g.group)}</h2>
          <div class="table-wrap"><table class="list"><tbody>${g.keys.map(row).join("")}</tbody></table></div>
        </div>`).join("")}`;
    const input = $("[data-form] [name=value]", root);
    if (input) input.focus();
  }

  root.addEventListener("click", async e => {
    const show = e.target.closest("[data-show]");
    if (show) { $("[data-form] [name=value]", root).type = show.checked ? "text" : "password"; return; }
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const name = btn.closest("[data-key]").dataset.key;
    if (btn.dataset.act === "edit") { open = name; return draw(); }
    if (btn.dataset.act === "close") { open = null; return draw(); }
    if (btn.dataset.act === "remove") {
      if (!confirm(`${name} aus der .env entfernen?`)) return;
      open = null;
      draw(await act(() => api(`/api/keys/${encodeURIComponent(name)}`, { method: "DELETE" }), "Entfernt"));
    }
  });
  root.addEventListener("submit", async e => {
    e.preventDefault();
    const name = e.target.closest("[data-key]").dataset.key;
    const value = $("[name=value]", e.target).value;
    const data = await act(() => api(`/api/keys/${encodeURIComponent(name)}`, { method: "PUT", body: { value } }), "Gespeichert – gilt ab sofort");
    open = null;
    draw(data);
  });
  draw();
};

// --- settings ------------------------------------------------------------------------

views.settings = async () => {
  const [v, paths] = await Promise.all([api("/api/settings"), api("/api/paths")]);
  main.innerHTML = `
    <h1>Einstellungen</h1>
    <form class="panel" id="sf">
      <h2>Standard für neue Analysen und Zeitpläne</h2>
      <div class="form">${settingsFields(v, { openAdvanced: true })}
        <div class="field wide row spread"><button type="button" class="danger" id="reset">Auf .env/Standard zurücksetzen</button><button class="primary" type="submit">Speichern</button></div>
      </div>
    </form>
    <div class="panel">
      <h2>Speicherorte</h2>
      <table class="list"><tbody>
        <tr><td class="muted">Ergebnisse &amp; Berichte</td><td><code>${h(paths.results_dir)}</code></td></tr>
        <tr><td class="muted">Memory-Log</td><td><code>${h(paths.memory_log)}</code></td></tr>
        <tr><td class="muted">Daten-Cache</td><td><code>${h(paths.cache_dir)}</code></td></tr>
        <tr><td class="muted">UI-Daten (Verlauf, Zeitpläne)</td><td><code>${h(paths.ui_dir)}</code></td></tr>
      </tbody></table>
      <p class="muted small">API-Keys und Datenquellen trägst du unter <a href="#/keys">API-Keys</a> ein.</p>
    </div>`;
  const f = $("#sf");
  bindSettingsFields(f, v);
  f.addEventListener("submit", async e => { e.preventDefault(); await act(() => api("/api/settings", { method: "PUT", body: readSettingsFields(f) }), "Gespeichert"); });
  $("#reset").addEventListener("click", async () => {
    if (!confirm("Gespeicherte Standard-Einstellungen verwerfen?")) return;
    await act(() => api("/api/settings/reset", { method: "POST" }), "Zurückgesetzt");
    route();
  });
};

// --- start ---------------------------------------------------------------------------

(function theme() {
  const saved = (() => { try { return localStorage.getItem("ta-theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme").addEventListener("click", () => {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ta-theme", next); } catch { /* private mode */ }
  });
})();

(async function start() {
  app.options = await api("/api/options");
  await refreshOverview();
  setInterval(refreshOverview, 2000);
  setInterval(renderChips, 1000);
  window.addEventListener("hashchange", route);
  route();
})();
