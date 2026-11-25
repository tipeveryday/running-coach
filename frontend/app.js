/* Running Coach — frontend app.
   Fetches the cached Garmin snapshot + coach reads, renders Chart.js charts,
   a week/month training calendar, the coach panels and a rule-based chat.
   Every renderer degrades to a "no data yet" state when a field is missing.
   Loaded as an ES module; shared UI comes from /static/ui (Phase 3 library). */
import * as UI from "./ui/index.js";
import { openSetup } from "./setup.js";

(() => {
  "use strict";

  // ---------- tiny helpers ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const escapeHtml = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  // minimal markdown: **bold** and newlines
  const mdLite = (s) =>
    escapeHtml(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br>");
  const paceStr = (s) => {
    if (!s && s !== 0) return "–";
    const m = Math.floor(s / 60), sec = Math.round(s % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  };
  const clock = (s) => {
    if (s == null) return "–";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
             : `${m}:${String(sec).padStart(2, "0")}`;
  };
  const shortDate = (iso) => {
    if (!iso) return "";
    const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  const toast = (msg, ok = true) => UI.toast(msg, { tone: ok ? "good" : "bad" });
  const api = async (path, opts) => {
    const r = await fetch(path, opts);
    if (r.status === 401) { showLogin(); throw new Error(`${path} → 401`); }
    if (r.status === 409) { startSetup(true); throw new Error(`${path} → setup required`); }
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    return r.json();
  };

  // ---------- chart colors for the raw Chart.js chart (filled from tokens by refreshChartTheme) ----------
  const C = {};
  const baseOpts = (extra = {}) => ({
    responsive: true, maintainAspectRatio: false,
    animation: UI.prefersReducedMotion() ? false : { duration: 360, easing: "easeOutQuart" },
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { labels: { color: C.tick, boxWidth: 12, font: { size: 11 } } },
      tooltip: { backgroundColor: C.tooltipBg, borderColor: C.tooltipBorder, borderWidth: 1, titleColor: C.text, bodyColor: C.text },
    },
    scales: {
      x: { grid: { color: C.grid }, ticks: { color: C.tick, maxRotation: 0, autoSkip: true } },
      y: { grid: { color: C.grid }, ticks: { color: C.tick } },
    },
    ...extra,
  });

  // ---------- global state ----------
  const S = { dash: null, plan: null, progress: null, snap: null, charts: {}, rendered: {}, cal: null };

  const destroyChart = (id) => { if (S.charts[id]) { S.charts[id].destroy(); delete S.charts[id]; } };
  // Raw Chart.js, only for the one custom chart (workout blueprint); everything
  // else goes through UI.AnnotatedChart via uiChart().
  const mk = (id, cfg) => { destroyChart(id); const cv = document.getElementById(id);
    if (cv) S.charts[id] = new Chart(cv.getContext("2d"), cfg); };

  // ================= boot =================
  // ---------- app-wide metric tooltips ----------
  const DEFS = {
    "vdot": "Effective racing fitness (Jack Daniels' VDOT). HOW IT'S COMPUTED: from a race time we take your speed in m/min, estimate the oxygen cost of that pace (VO₂ = −4.6 + 0.182·v + 0.000104·v²), divide by the % of VO₂max you can hold for that duration (a decay curve of race length), giving one number that maps directly to equivalent race times and training paces. Here it's derived from Garmin's predicted half-marathon time. Unlike raw VO₂max it already bakes in running economy.",
    "load balance": "Acute:Chronic Workload Ratio (ACWR). COMPUTED: total training load over the last 7 days ÷ your average 7-day load over the last ~28. 0.8–1.3 = sweet spot; above ~1.5 sharply raises injury risk (you're loading faster than you adapt).",
    "acwr": "Acute:Chronic Workload Ratio. COMPUTED: last-7-day load ÷ ~28-day average 7-day load. 0.8–1.3 = sweet spot; >1.5 sharply raises injury risk.",
    "readiness": "Garmin's 0–100 morning 'can I train hard today?' score. COMPUTED: a weighted blend of overnight HRV, sleep, recovery time, recent acute load and stress.",
    "resting hr": "Your lowest overnight heart rate. BASELINE COMPUTED from up to 2 years of history: 'rested' = your 15th-percentile low, 'typical' = the median. Falls with fitness; rises with fatigue, illness, alcohol, heat or dehydration.",
    "hrv": "Heart-rate variability — beat-to-beat variation during sleep (ms, rMSSD-based). COMPUTED as your overnight average; read it vs YOUR balanced band (Garmin builds that band from ~3 weeks of your nights), not other people. A drop below the band flags fatigue.",
    "overnight recharge": "How much Body Battery refilled overnight = its morning high minus the night's low. 60+ means you slept well with low stress.",
    "body battery": "0–100 energy gauge. COMPUTED continuously from HRV, stress and activity — charges during rest/sleep, drains with stress and exertion.",
    "recovery time": "Garmin's estimate of the hours before you're ready for another hard effort, from the strain of your last sessions.",
    "recovery": "How much you've recovered from recent training — a driver of your readiness score.",
    "vo₂max": "Max oxygen uptake (ml/kg/min) — the ceiling of your aerobic engine. ESTIMATED by Garmin from your heart-rate-to-pace relationship on runs. The percentile compares it to Cooper-Institute norms for your age & sex.",
    "vo2max": "Max oxygen uptake (ml/kg/min), estimated by Garmin from your HR-to-pace relationship on runs. Percentile is vs Cooper-Institute norms for your age & sex.",
    "fatigue": "Acute Training Load (ATL). COMPUTED: an exponentially-weighted 7-day average of daily training load — each day nudges it by 1/7. Rises and falls fast = recent tiredness.",
    "fitness": "Chronic Training Load (CTL). COMPUTED: an exponentially-weighted 42-day average of daily training load — each day nudges it by 1/42. Rises slowly; the higher it is, the fitter you are.",
    "form": "Training Stress Balance (TSB) = yesterday's Fitness − Fatigue. Positive = fresh (good to race); slightly negative = the productive training zone; deeply negative = overreached.",
    "cadence": "Steps per minute. COMPUTED as the run's average (we auto-correct per-leg readings and drop implausible values). ~170–180 is efficient; much lower means overstriding and more impact.",
    "training load": "Garmin's EPOC-based load: how much a session raised your post-exercise oxygen consumption — i.e. total physiological stress.",
    "training status": "Garmin's verdict on whether recent load is building fitness (Productive), holding it (Maintaining), or overdoing it — from your load trend vs VO₂max direction.",
    "lactate threshold": "The intensity you can hold ~1 hour — the physiological anchor your training zones are built from. Garmin detects it from a hard, well-measured effort.",
    "threshold": "The pace you could race for ~1 hour — the highest-leverage stimulus for distance-running fitness.",
    "adherence": "COMPUTED: run-days you actually logged ÷ run-days scheduled so far this period. It's a discipline/consistency score, not volume.",
    "sleep score": "Garmin's 0–100 nightly rating blending duration, stages, restlessness and overnight stress.",
    "sleep need": "Your personalized nightly sleep requirement, LEARNED by Garmin from your history and recent strain — not a generic 8 h.",
    "sleep debt": "COMPUTED: your personalized nightly need minus actual sleep, summed over recent nights (shortfalls only).",
    "efficiency": "Sleep efficiency. COMPUTED: time asleep ÷ time in bed. ≥85% is healthy; lower means fragmented sleep.",
    "deep": "Deep sleep — where physical repair and growth-hormone release peak. Shown as % of the night; healthy ≈13–23%.",
    "rem": "REM sleep — consolidates skills and manages stress; concentrated in the back half of the night. Healthy ≈20–28% of the night.",
    "regularity": "How consistent your bed/wake times are. COMPUTED from the standard deviation of your bedtime across the week — tighter = more regular. Predicts recovery more than any single night's duration.",
    "bedtime spread": "The standard deviation of your bedtime across the week (± minutes). Tighter is better — regularity drives recovery.",
    "aerobic te": "Aerobic Training Effect (0–5): how much a session built your aerobic base, from the EPOC it generated.",
    "anaerobic te": "Anaerobic Training Effect (0–5): how much a session built high-intensity/speed capacity.",
    "grey-zone": "COMPUTED: share of your km run at a pace between easy and threshold — too quick to recover well, too slow to be a real workout. Minimizing it (polarizing) frees quality for the sessions that count.",
    "easy share": "COMPUTED: share of your TIME (not km) that sits in HR Zones 1-2. ~80% easy / 20% hard (Z4-Z5) is the polarized sweet spot; grey (Z3) is what you want under ~20%.",
    "quality share": "COMPUTED: share of your km that's genuine hard/quality work (by pace) — aim ~20%.",
    "volume": "Total distance run. Consistent weekly volume is the foundation of endurance.",
    "hmp": "Half-marathon race pace — the pace you could hold for 21.1 km.",
    "grade-adj pace": "Grade-adjusted pace — your pace normalized to flat ground. COMPUTED: raw pace minus a terrain credit (~2 s/km per metre climbed per km), so a hilly run's true effort isn't hidden by a slow pace.",
    "grade-adjusted pace": "Your pace normalized to flat ground, so hills don't hide the true effort.",
    "effort blend": "A 0–100 effort score. COMPUTED: heart rate (%HRR, leads) blended with pace, grade-adjusted pace and power — a well-rounded read that corrects raw pace's bias on hills, intervals (recoveries drag the average) and heat. Signals that don't vary enough for you are dropped.",
    "power": "Running power (watts) — mechanical output. Used only as a corroborating effort signal, and dropped when it barely varies with intensity (as it does on many watches).",
    "body weight": "Your logged body weight. Matters for running: it sets power-to-weight (W/kg), scales relative VO₂max (measured per-kg), and affects pace economy — roughly ~2 s/km at race pace per kg — and impact load.",
    "w/kg": "Power-to-weight ratio. COMPUTED: recent average running power ÷ body weight. Higher = you move more mass-adjusted output; it improves by raising power or lowering non-functional weight.",
  };
  const TIP_KEYS = Object.keys(DEFS).sort((a, b) => b.length - a.length);
  // generic single words that must appear at the START of a label to match,
  // so "fitness" won't match inside "race-fitness", etc.
  const TIP_PREFIX_ONLY = new Set(["fitness", "fatigue", "form", "recovery", "deep", "rem", "volume", "threshold", "efficiency", "readiness", "hrv", "cadence", "power"]);
  function _bestTipKey(text) {
    const starts = TIP_KEYS.find((k) => text.startsWith(k));
    if (starts) return starts;
    return TIP_KEYS.find((k) => !TIP_PREFIX_ONLY.has(k) && text.includes(k)) || null;
  }
  // Glossary definitions show through the shared UI tooltip (hover, focus and tap).
  const initTips = () => UI.initUI();
  // Labels that get a glossary definition when their text names a known metric.
  const TIP_SEL = ".ui-stat-label,.ui-metric-label,.ui-card-title,.ui-chart-title,.ui-table th";
  function applyTooltips(root) {
    (root || document).querySelectorAll(TIP_SEL).forEach((elm) => {
      if (elm.dataset.tip) return;
      // inside a button or link the term can't be its own focus stop (and a tap would do two things)
      if (elm.closest("button, a")) return;
      const key = _bestTipKey((elm.textContent || "").toLowerCase().trim());
      if (key) { elm.dataset.tip = DEFS[key]; elm.classList.add("has-tip"); if (!elm.hasAttribute("tabindex")) elm.tabIndex = 0; }
    });
  }

  // ---------- password gate ----------
  function showLogin() {
    const g = document.getElementById("loginGate");
    if (g) { g.classList.remove("hidden"); const p = document.getElementById("loginPass"); if (p) p.focus(); }
  }

  function hideLogin() {
    const g = document.getElementById("loginGate");
    if (g) g.classList.add("hidden");
  }
  function wireLogin() {
    const form = document.getElementById("loginForm");
    if (!form || form.dataset.wired) return;
    form.dataset.wired = "1";
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = document.getElementById("loginErr");
      const pass = document.getElementById("loginPass").value;
      const btn = form.querySelector("button");
      btn.disabled = true; err.hidden = true;
      try {
        const r = await fetch("/api/login", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: pass }),
        });
        if (r.ok) { hideLogin(); document.getElementById("loginPass").value = ""; await startApp(); }
        else { const b = await r.json().catch(() => ({})); err.textContent = b.error || "Incorrect password."; err.hidden = false; }
      } catch (_) { err.textContent = "Couldn't reach the server."; err.hidden = false; }
      finally { btn.disabled = false; }
    });
  }

  function wireLogout() {
    const b = document.getElementById("logoutBtn");
    if (b) b.onclick = async () => {
      try { await fetch("/api/logout", { method: "POST" }); } catch (_) {}
      showLogin();
    };
  }

  // ---------- light / dark theme ----------
  function curTheme() { return UI.getTheme(); }
  function refreshChartTheme() {
    const t = UI.token;
    Object.assign(C, { grid: t("--viz-grid"), tick: t("--viz-label"), text: t("--text-1"), line: t("--cat-2"),
      tooltipBg: t("--surface-overlay"), tooltipBorder: t("--border-strong") });
    if (window.Chart) { Chart.defaults.font.family = getComputedStyle(document.body).fontFamily; Chart.defaults.color = C.tick; }
  }
  const themeIcon = () => UI.icon(curTheme() === "light" ? "moon" : "sun", { size: 20 });
  function wireTheme() {
    refreshChartTheme();
    const b = document.getElementById("themeBtn");
    if (!b) return;
    const label = () => (curTheme() === "light" ? "Switch to dark mode" : "Switch to light mode");
    b.innerHTML = themeIcon(); b.setAttribute("aria-label", label()); b.title = label();
    b.onclick = () => {
      UI.setTheme(curTheme() === "light" ? "dark" : "light");
      refreshChartTheme();
      b.innerHTML = themeIcon(); b.setAttribute("aria-label", label()); b.title = label();
      if (S.progress) renderCoachSurface();                         // repaint token/sparkline colors
      S.rendered = {}; renderActiveTab(); buildCalendar();          // rebuild legacy charts in the new theme
    };
  }

  // Shared visual state for any Garmin refresh (auto on load + manual click):
  // top progress bar, spinning refresh icon, status text, dimmed coach card.
  function _refreshUIStart() {
    $("#refreshProgress")?.classList.add("is-on");
    const btn = $("#refreshBtn"); if (btn) { btn.classList.add("app-spin"); btn.setAttribute("aria-busy", "true"); }
    const fr = $("#freshness"); if (fr) { fr.classList.add("is-busy"); fr.textContent = "Refreshing from Garmin…"; }
    $("#verdictCard")?.classList.add("refreshing");
  }
  function _refreshUIStop() {
    $("#refreshProgress")?.classList.remove("is-on");
    const btn = $("#refreshBtn"); if (btn) { btn.classList.remove("app-spin"); btn.removeAttribute("aria-busy"); }
    $("#freshness")?.classList.remove("is-busy");
    $("#verdictCard")?.classList.remove("refreshing");
    renderTopbar();
  }

  // Pull fresh Garmin data on app load (once per session) when the cache is stale,
  // then re-render. Non-blocking: the cached view shows immediately.
  async function autoRefresh() {
    if (S._autoRefreshed) return;
    S._autoRefreshed = true;
    const age = S.dash && S.dash.cache_age_seconds;
    if (age != null && age < 1800) return;            // fresh within 30 min → skip
    _refreshUIStart();
    try {
      const resp = await fetch("/api/refresh", { method: "POST" });
      const r = await resp.json().catch(() => ({}));
      if (resp.ok && r.ok) await loadAll();             // re-render with fresh data
    } catch (_) { /* keep the cached view on failure */ }
    finally { _refreshUIStop(); }
  }

  // ---- Garmin sync from the Calendar toolbar ----
  function wireGarminSync() {
    const btn = document.getElementById("calSyncBtn");
    if (!btn || btn._wired) return;
    btn._wired = true;
    btn.onclick = () => openGarminSync();
  }

  async function openGarminSync() {
    const dlg = UI.openDialog({
      title: "Sync to Garmin", sub: "Schedules the plan's workouts on your Garmin calendar.",
      body: `<div class="app-gsync-form">
          ${UI.insightCallout({ variant: "inline", tone: "warn", label: "Heads up", title: "Every earlier [coach] workout is removed first.", body: "This keeps old versions from piling up on your watch." })}
          ${UI.field({ label: "Window", id: "gsyncWeeks", control: UI.select({ value: "2", options: [["1", "Next week"], ["2", "Next 2 weeks"], ["3", "Next 3 weeks"], ["4", "Next 4 weeks"]] }) })}
          <fieldset class="app-checks"><legend class="ui-label">Include</legend>
            <label><input type="checkbox" id="gsyncK-run" checked> Runs</label>
            <label><input type="checkbox" id="gsyncK-strength" checked> Strength</label>
            <label><input type="checkbox" id="gsyncK-mobility" checked> Nightly mobility</label>
          </fieldset>
        </div>
        <div class="app-gsync-preview" id="gsyncPreview"><p class="ui-caption">Preview shows exactly what will be pushed.</p></div>
        <div id="gsyncStatus" aria-live="polite"></div>`,
      footer: UI.button({ label: "Preview", variant: "secondary", attrs: { id: "gsyncPreviewBtn" } })
        + UI.button({ label: "Push to Garmin", variant: "primary", icon: "refresh-cw", disabled: true, attrs: { id: "gsyncPushBtn" } }),
    });
    dlg.el.querySelector("#gsyncPreviewBtn").onclick = gsyncPreview;
    dlg.el.querySelector("#gsyncPushBtn").onclick = gsyncPush;
  }

  function _gsyncOpts() {
    const weeks = +document.getElementById("gsyncWeeks").value;
    const kinds = ["run", "strength", "mobility"].filter((k) => document.getElementById("gsyncK-" + k).checked);
    return { weeks, kinds };
  }

  async function gsyncPreview() {
    const { weeks, kinds } = _gsyncOpts();
    const mount = document.getElementById("gsyncPreview");
    mount.innerHTML = UI.loading("Building preview");
    try {
      const q = new URLSearchParams({ weeks, kinds: kinds.join(",") }).toString();
      const d = await api(`/api/garmin/sync/preview?${q}`);
      const bk = d.by_kind || {};
      const kindIcon = { run: "footprints", strength: "dumbbell", mobility: "person-standing" };
      mount.innerHTML = `
        <p class="ui-small"><b class="num">${d.count}</b> workouts across ${weeks} week${weeks > 1 ? "s" : ""}: ${bk.run || 0} runs, ${bk.strength || 0} strength, ${bk.mobility || 0} mobility.</p>
        <div class="app-gsync-list">${UI.table({ caption: "Workouts to push", dense: true, cols: [
          { key: "date", label: "Date", fmt: (v) => UI.fmt.date(v, { weekday: "short", month: "short", day: "numeric" }) },
          { key: "kind", label: "Type", fmt: (v) => UI.badge(v, { icon: kindIcon[v] || "activity", size: "sm" }) },
          { key: "title", label: "Workout", fmt: (v) => escapeHtml(String(v).replace(/^\[coach\]\s*/, "")) },
        ], rows: d.items || [] })}</div>`;
      document.getElementById("gsyncPushBtn").disabled = d.count === 0;
    } catch (e) {
      mount.innerHTML = UI.errorState({ compact: true, title: "Preview failed", body: String(e.message || e), retry: false });
    }
  }

  async function gsyncPush() {
    const { weeks, kinds } = _gsyncOpts();
    const status = document.getElementById("gsyncStatus");
    const pushBtn = document.getElementById("gsyncPushBtn");
    const previewBtn = document.getElementById("gsyncPreviewBtn");
    pushBtn.disabled = true;
    previewBtn.disabled = true;
    status.innerHTML = `<p style="margin-top:var(--sp-4)">${UI.loading("Removing old [coach] workouts and uploading new ones. This takes 30–60 s")}</p>`;
    try {
      await fetch("/api/garmin/sync/push", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ weeks, kinds }) });
    } catch (e) {
      status.innerHTML = UI.insightCallout({ tone: "bad", title: "Push failed to start", body: String(e.message || e) });
      pushBtn.disabled = false; previewBtn.disabled = false;
      return;
    }
    // poll status
    const started = Date.now();
    const poll = async () => {
      try {
        const st = await api("/api/garmin/sync/status");
        if (st.running) {
          const secs = Math.floor((Date.now() - started) / 1000);
          status.innerHTML = `<p style="margin-top:var(--sp-4)">${UI.loading(`Syncing (${secs}s)`)}</p>`;
          setTimeout(poll, 2000);
        } else if (st.last) {
          if (st.last.ok) {
            const log = (st.last.log || "").split("\n").slice(-14).join("\n");
            status.innerHTML = `<div style="margin-top:var(--sp-4)">${UI.insightCallout({ tone: "good", title: `Sync complete at ${st.last.at}.` })}</div>
              <pre class="app-gsync-log">${escapeHtml(log)}</pre>`;
          } else {
            status.innerHTML = `<div style="margin-top:var(--sp-4)">${UI.insightCallout({ tone: "bad", title: "Sync failed", body: st.last.error || "Unknown error" })}</div>
              <pre class="app-gsync-log">${escapeHtml(st.last.log || "")}</pre>`;
          }
          previewBtn.disabled = false;
          pushBtn.disabled = false;
        }
      } catch (e) {
        setTimeout(poll, 3000);
      }
    };
    setTimeout(poll, 1500);
  }

  // ================= shell + routing =================
  // Five sections. Activity and Recovery each hold two views (sub-nav).
  // `panels` are the legacy <section class="panel"> ids each view shows; they
  // shrink to one mount per section as Phase 4 batches land.
  const ROUTES = [
    { id: "coach",    label: "Coach",            short: "Coach",    icon: "house",         views: [{ id: "today", legacy: "overview" }] },
    { id: "activity", label: "Activity",         short: "Activity", icon: "activity",      views: [{ id: "analysis", label: "Analysis", icon: "chart-line", legacy: "activity" }, { id: "log", label: "Training log", icon: "list", legacy: "log" }] },
    { id: "fitness",  label: "Fitness",          short: "Fitness",  icon: "trending-up",   views: [{ id: "overview", legacy: "fitness" }] },
    { id: "recovery", label: "Sleep & recovery", short: "Recovery", icon: "heart-pulse",   views: [{ id: "hrv", label: "Readiness & HRV", icon: "heart-pulse", legacy: "recovery" }, { id: "sleep", label: "Sleep", icon: "moon", legacy: "sleep" }] },
    { id: "plan",     label: "Plan",             short: "Plan",     icon: "calendar-days", views: [{ id: "calendar", legacy: "calendar" }] },
  ];
  // Old tab id -> [route, view]. Keeps every legacy "open tab X" path working.
  const LEGACY = Object.fromEntries(ROUTES.flatMap((r) => r.views.map((v) => [v.legacy, [r.id, v.id]])));
  const routeOf = (id) => ROUTES.find((r) => r.id === id) || ROUTES[0];
  S.route = "coach"; S.view = "today";
  /** Legacy tab id of what's on screen (renderActiveTab and friends key off this). */
  const curTab = () => (routeOf(S.route).views.find((v) => v.id === S.view) || routeOf(S.route).views[0]).legacy;

  function renderShell() {
    const nav = { items: ROUTES.map(({ id, label, short, icon }) => ({ id, label, short, icon, href: `#${id}` })), current: S.route };
    $("#topbar").innerHTML = UI.topBar({
      name: "Running Coach", sub: "", home: "coach", nav,
      actions: `<span class="app-fresh" id="freshness" aria-live="polite"></span>
        ${UI.button({ label: "Refresh from Garmin", variant: "ghost", iconOnly: true, icon: "refresh-cw", attrs: { id: "refreshBtn" } })}
        <button class="ui-btn ghost icon" type="button" id="themeBtn"></button>
        ${UI.button({ label: "Edit setup", variant: "ghost", iconOnly: true, icon: "pencil", attrs: { id: "setupBtn" } })}
        ${S.passwordRequired ? UI.button({ label: "Log out", variant: "ghost", iconOnly: true, icon: "log-out", attrs: { id: "logoutBtn" } }) : ""}`,
    });
    $("#bottomnav").innerHTML = UI.bottomNav(nav);
    $("#chatFab").innerHTML = UI.icon("message-circle", { size: 24 });
    $("#chatClose").innerHTML = UI.icon("x", { size: 20 });
    $("#chatSend").innerHTML = UI.icon("send", { size: 18 });
    $(".app-chat-mark").innerHTML = UI.icon("sparkles", { size: 16 });
  }

  function renderSubnav() {
    const r = routeOf(S.route);
    $("#subnav").innerHTML = r.views.length < 2 ? "" : `<div class="ui-tabs" role="tablist" aria-label="${escapeHtml(r.label)} views">${r.views.map((v) =>
      `<button class="ui-tab" role="tab" type="button" data-view="${v.id}" aria-selected="${v.id === S.view}" tabindex="${v.id === S.view ? 0 : -1}">${UI.icon(v.icon, { size: 16 })}${escapeHtml(v.label)}</button>`).join("")}</div>`;
  }

  /** Show a section (and optional view). push=false for back/forward and first load. */
  function go(routeId, viewId, { push = true, focus = false } = {}) {
    const r = routeOf(routeId);
    const v = r.views.find((x) => x.id === viewId) || (S.route === r.id && r.views.find((x) => x.id === S.view)) || r.views[0];
    const changed = S.route !== r.id || S.view !== v.id;
    S.route = r.id; S.view = v.id;
    const hash = `#${r.id}${r.views.length > 1 ? "/" + v.id : ""}`;
    if (location.hash !== hash) history[push ? "pushState" : "replaceState"](null, "", hash);
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + v.legacy));
    UI.setNavCurrent(r.id);
    renderSubnav();
    document.title = `${r.label} · Running Coach`;
    if (changed && push) { scrollTo({ top: 0 }); if (focus) $("#main").focus({ preventScroll: true }); }
    renderActiveTab();
    // charts sized while hidden need a resize nudge
    requestAnimationFrame(() => Object.values(S.charts).forEach((c) => c.resize()));
  }
  /** Navigate by legacy tab id (tile links, chat actions, old code paths). */
  const goTab = (legacy) => { const m = LEGACY[legacy]; if (m) go(m[0], m[1]); };
  const routeFromHash = () => {
    const [rid, vid] = location.hash.replace(/^#/, "").split("/");
    if (ROUTES.some((r) => r.id === rid)) return [rid, vid];
    return LEGACY[rid] || ["coach"];                     // old bookmarks like #sleep, #log
  };

  function wireRouter() {
    document.addEventListener("click", (e) => {
      const a = e.target.closest("[data-nav]");
      if (a) { e.preventDefault(); go(a.dataset.nav, null, { focus: true }); return; }
      const t = e.target.closest("#subnav [data-view]");
      if (t) go(S.route, t.dataset.view);
    });
    addEventListener("popstate", () => { const [r, v] = routeFromHash(); go(r, v, { push: false }); });
    const [r, v] = routeFromHash();
    go(r, v, { push: false });
  }

  async function boot() {
    let auth = {};
    try { auth = await (await fetch("/api/auth")).json(); } catch (_) {}
    S.passwordRequired = !!auth.password_required;
    renderShell();
    wireTheme();
    wireRouter(); wireRefresh(); wireChat(); wireCalendarControls(); wireModal(); initTips();
    wireLogin(); wireLogout();
    $("#setupBtn")?.addEventListener("click", () => startSetup(false));
    if (auth.authenticated) { hideLogin(); await startApp(); }
    else showLogin();
  }

  /** After auth: onboarding on first run, otherwise load the dashboard. */
  async function startApp() {
    let st = {};
    try { st = await (await fetch("/api/setup")).json(); } catch (_) {}
    S.demo = !!st.demo;
    if (!st.configured) { _pageReady(); return startSetup(true, st); }
    await loadAll(); autoRefresh();
  }

  async function startSetup(firstRun, st) {
    if (S._setupOpen) return;
    S._setupOpen = true;
    try { st = st || await (await fetch("/api/setup")).json(); } catch (_) { st = st || {}; }
    openSetup({
      profile: st.profile, suggestions: st.suggestions, demo: st.demo, firstRun,
      onDone: async () => { S._setupOpen = false; S._autoRefreshed = false; await loadAll(); autoRefresh(); },
    });
    // a Cancel on "Edit setup" just closes the overlay
    const gate = document.getElementById("setupGate");
    new MutationObserver((_, obs) => { if (gate.classList.contains("hidden")) { S._setupOpen = false; obs.disconnect(); } })
      .observe(gate, { attributes: true, attributeFilter: ["class"] });
  }

  async function loadAll() {
    try {
      const [dash, plan] = await Promise.all([api("/api/dashboard"), api("/api/plan")]);
      S.dash = dash; S.plan = plan; S.snap = dash.snapshot || null;
      S.stamina = null; S.heat = null;   // recompute lazily on the Fitness tab
      S.done = completedRunByDate();
      renderTopbar();
      if (!dash.has_data) {
        $("#verdictBody").innerHTML = UI.card({ body: UI.emptyState({ icon: "refresh-cw", title: "No data yet", body: dash.message || "Refresh from Garmin to get today's call." }) });
      }
      renderPaces();
      const [today, lastRun, progress, sleepRead, fitnessRead, activityRead, recoveryRead] = await Promise.all([
        api("/api/coach/today"), api("/api/coach/last-run"), api("/api/coach/progress"),
        api("/api/coach/sleep"), api("/api/coach/fitness"), api("/api/coach/activity"), api("/api/coach/recovery"),
      ]);
      S.progress = progress;
      S.sleep = sleepRead;
      S.fitness = fitnessRead;
      S.activity = activityRead;
      S.recovery = recoveryRead;
      renderVerdict(today);
      if (!dash.has_data) {
        $("#verdictBody").insertAdjacentHTML("afterbegin", UI.insightCallout({ tone: "info", label: "Connect your data",
          title: "No Garmin data yet", body: dash.message || "Press Refresh to pull your data." }));
      }
      renderTrajectory(progress);
      renderProgress(progress);
      S.lastRun = lastRun;
      renderLastRun(lastRun);
      // Non-blocking side-panels feed into the hero + telemetry chip row.
      loadRecs();
      loadNiggles();
      loadEnvironment();
      renderCoachSurface();
      wireLpLinks();
      // charts for the currently active tab
      S.rendered = {};
      renderActiveTab();
      buildCalendar();
      _pageReady();
      seedChatSuggestions();
      applyTooltips();
    } catch (e) {
      console.error(e);
      _pageReady();
      toast("Couldn't load your data. Is the server running?", false);
    }
  }
  // First render done: reveal the page in one step (no layout shift while it fills in).
  function _pageReady() {
    const m = $("#main"); if (!m || !m.classList.contains("is-loading")) return;
    m.classList.remove("is-loading"); m.removeAttribute("aria-busy");
  }

  // ================= topbar =================
  function renderTopbar() {
    const cd = (S.dash && S.dash.countdown) || {};
    const cfg = (S.dash && S.dash.config) || {};
    // brand subtitle: race + countdown ("City Half · 4 wks")
    const sub = $(".ui-brand-sub") || (() => { const s = el("span", "ui-brand-sub"); s.style.display = "block"; $(".ui-brand-name").after(s); return s; })();
    const when = cd.race_passed ? "race done" : cd.is_race_week ? "race week"
      : cd.weeks_to_race_ceil != null ? `${cd.weeks_to_race_ceil} wks to go` : "";
    sub.innerHTML = cfg.race ? `${escapeHtml(cfg.race.name)}${when ? ` · <span class="app-countdown"><b>${escapeHtml(when)}</b></span>` : ""}` : "";
    // freshness
    const f = $("#freshness");
    if (!f) return;
    if (S.dash && S.dash.fetched_at) {
      const age = S.dash.cache_age_seconds;
      let ago = "just now";
      if (age != null) {
        if (age > 86400) ago = `${Math.floor(age / 86400)}d ago`;
        else if (age > 3600) ago = `${Math.floor(age / 3600)}h ago`;
        else if (age > 60) ago = `${Math.floor(age / 60)}m ago`;
      }
      f.textContent = `Updated ${ago}`;
      f.title = S.dash.fetched_at;
    } else f.textContent = "No data yet";
  }

  // ================= coach panels =================
  // Coach verdict color word -> system tone + plain-words status.
  const VERDICT = {
    green: { tone: "good", word: "Good to go" }, amber: { tone: "warn", word: "Take care" },
    red: { tone: "bad", word: "Back off" }, grey: { tone: "neutral", word: "Waiting on data" },
  };
  // HR-zone chip from the run's avg HR against the athlete's configured (LThr) zones.
  const hrZoneChip = (hz) => hz
    ? `<span class="ui-zone-chip" style="--zc:var(--z${escapeHtml(hz.zone)})" title="${escapeHtml(hz.bpm || "")} bpm"><i aria-hidden="true"></i>${escapeHtml(hz.label || ("Z" + hz.zone))}</span>` : "";

  // Prescribed session as an inset well: intensity-colored icon, title, numbers, why.
  function sessionBlock(s, label) {
    if (!s) return "";
    const w = UI.workout(s.type);
    const bits = [];
    if (s.distance_km) bits.push(`${s.distance_km} km`);
    if (s.pace) bits.push(`@ ${s.pace}/km`);
    if (s.zone) bits.push(s.zone);
    return `<div class="ui-well cx-session" style="--wz:${UI.zoneVar(w.zone)}">
        <span class="cx-session-icon">${UI.icon(w.icon, { size: 20 })}</span>
        <span class="ui-overline">${escapeHtml(label || "Today's session")} · ${escapeHtml(w.label)}</span>
        <span class="cx-session-title">${escapeHtml(s.title || w.label)}</span>
        ${bits.length ? `<span class="cx-session-meta">${escapeHtml(bits.join(" · "))}</span>` : ""}
        ${s.why ? `<span class="cx-session-why" style="grid-column:2">${escapeHtml(s.why)}</span>` : ""}</div>`;
  }

  function renderVerdict(v) {
    if (v && v.mode === "done") return renderVerdictDone(v);
    const tm = $("#cxTomorrow"); if (tm) tm.innerHTML = "";
    const vv = VERDICT[v.verdict] || VERDICT.grey;
    $("#verdictBody").innerHTML = `<div class="ui-stack">
      ${UI.coachTip({ tone: vv.tone, label: `Today's call · ${vv.word}`, title: v.headline || vv.word, body: v.recommendation || "",
        why: (v.signals || []).length ? v.signals : null,
        actions: UI.button({ label: "Ask the coach", variant: "secondary", icon: "message-circle", attrs: { id: "cxAskCoachBtn" } }) })}
      ${sessionBlock(v.session)}</div>`;
  }

  // Rich, type-specific coaching insights (what went well / what to improve / how).
  // Legacy tones good / improve / note map onto the system's good / warn / info.
  function renderInsights(list, heading) {
    if (!list || !list.length) return "";
    return `${heading ? `<p class="ui-overline">${escapeHtml(heading)}</p>` : ""}${UI.insightList(list, { variant: "inline" })}`;
  }

  // Compact multi-factor effort readout (grade-adjusted pace + pace + HR + power).
  function effortBlendHtml(effort, blend, gapPace, rawPace) {
    if (effort == null || !blend) return "";
    const rows = [["pace", "Pace"], ["gap", "Grade-adj pace"], ["hr", "Heart rate"], ["power", "Power"]].map(([k, lb]) => {
      const val = blend[k];
      return `<div class="app-meter-row"><span>${lb}</span><span class="app-meter" role="img" aria-label="${lb} ${val != null ? Math.round(val * 100) + " of 100" : "not used"}">${val != null ? `<i style="width:${Math.round(val * 100)}%"></i>` : ""}</span><span class="v">${val != null ? Math.round(val * 100) : "–"}</span></div>`;
    }).join("");
    return `<div class="ui-stack" style="gap:var(--sp-3)">
      <div class="ui-row" style="justify-content:space-between"><p class="ui-h3">Effort blend</p><span class="ui-small ui-muted"><b class="num" style="color:var(--text-1)">${Math.round(effort * 100)}</b>/100 · higher is harder</span></div>
      <div class="app-meters">${rows}</div>
      ${gapPace ? `<p class="ui-caption">Grade-adjusted ${escapeHtml(gapPace)}/km vs ${escapeHtml(rawPace || "")}/km raw. Heart rate leads; power is dropped when it doesn't vary with effort.</p>` : ""}</div>`;
  }

  // Post-run view: today's session is logged — show effort + whether it hit the
  // mark + impact on the goal, in place of the morning readiness verdict.
  function renderVerdictDone(v) {
    const c = v.completed || {};
    const stats = [
      ["Distance", c.distance_km, "km"], ["Time", c.duration], ["Pace", c.avg_pace, "/km"],
      ["Grade-adj pace", c.gap_pace, c.gap_pace ? "/km" : ""], ["Avg HR", c.avg_hr, "bpm"], ["Power", c.power, c.power ? "W" : ""],
    ].filter(([, val]) => val != null && val !== "").map(([label, value, unit]) => UI.statTile({ label, value, unit, inset: true }));
    const hz = c.hr_zone ? `<p class="ui-small ui-muted">Average heart rate sat in ${hrZoneChip(c.hr_zone)}</p>` : "";
    const ins = (c.insights && c.insights.length) ? renderInsights(c.insights, "Coach notes")
      : (c.notes && c.notes.length) ? UI.insightCallout({ variant: "inline", tone: "info", label: "Coach notes", body: c.notes }) : "";
    $("#verdictBody").innerHTML = `<div class="ui-stack">
      ${UI.coachTip({ tone: "good", label: "Done today", icon: "circle-check", title: v.headline || "Session logged",
        actions: UI.button({ label: "Ask the coach", variant: "secondary", icon: "message-circle", attrs: { id: "cxAskCoachBtn" } }) })}
      <div class="ui-card">
        ${stats.length ? `<div class="cx-stats">${stats.join("")}</div>` : ""}${hz}
        ${ins}
        ${c.race_impact ? UI.insightCallout({ tone: "coach", label: `Impact on ${goalTime()}`, body: c.race_impact }) : ""}
        <details class="ui-insight-why"><summary>Session breakdown: effort blend, prescription${c.tip ? ", tip" : ""}</summary>
          <div class="ui-stack" style="margin-top:var(--sp-4)">
            ${effortBlendHtml(c.effort, c.blend, c.gap_pace, c.avg_pace)}
            ${sessionBlock(v.session, "Prescribed")}
            ${c.tip ? UI.insightCallout({ variant: "inline", tone: "info", label: "Tip", body: c.tip }) : ""}
          </div></details>
      </div></div>`;
    // Tomorrow's outlook sits beside the call, not under it.
    const tm = $("#cxTomorrow"); if (tm) tm.innerHTML = nextDayHtml(v.next_day);
  }

  // Provisional look at tomorrow — recovery advice + conditional scenarios.
  // Never a definitive "good to go": tomorrow's readiness needs tonight's sleep.
  function nextDayHtml(nd) {
    if (!nd) return "";
    const scn = (nd.scenarios || []).map((s) => {
      const t = UI.tone(s.tone);
      return `<li data-tone="${t}">${UI.icon(UI.TONE_ICON[t], { size: 18 })}<div><b>${escapeHtml(s.cond)}</b>${escapeHtml(s.action)}</div></li>`;
    }).join("");
    return UI.card({ title: "Tomorrow", icon: "calendar-days", sub: nd.headline || "",
      actions: UI.badge("Provisional", { size: "sm", tone: "neutral" }),
      body: `${sessionBlock(nd.session, "Scheduled")}
        ${nd.recovery_advice ? UI.insightCallout({ variant: "inline", tone: "info", label: "Tonight", body: nd.recovery_advice }) : ""}
        ${scn ? `<div class="ui-stack" style="gap:var(--sp-3)"><h3 class="ui-overline">Tomorrow morning, once sleep and readiness are in</h3><ul class="cx-scenarios">${scn}</ul></div>` : ""}
        ${nd.note ? `<p class="ui-caption">${escapeHtml(nd.note)}</p>` : ""}` });
  }

  // active-race helpers (countdown retargets half → marathon over time)
  function goalTime() { return ((S.dash && S.dash.countdown && S.dash.countdown.goal_time) || "your goal").replace(/:00$/, ""); }
  function raceNoun() { return (S.dash && S.dash.countdown && S.dash.countdown.distance_label === "marathon") ? "marathon" : "half"; }
  function raceDateShort() { const d = S.dash && S.dash.countdown && S.dash.countdown.race_date; return d ? shortDate(d) : "race day"; }

  function renderTrajectory(p) {
    const tt = $("#trajectoryTitle");
    if (tt) tt.textContent = `Goal trajectory: can you run ${goalTime()}?`;
    const b = $("#trajectoryBody");
    if (!p || p.on_track === "grey" && !p.projected_finish) {
      b.innerHTML = UI.emptyState({ icon: "target", title: "Not enough data to project a finish yet", body: "Refresh after your next run.", compact: true });
      return;
    }
    const goalPaces = ((S.dash && S.dash.paces) || {});
    const g = goalPaces.garmin || {};
    const race = (S.fitness && S.fitness.race) || {};
    const tiles = [];
    if (p.projected_finish) tiles.push(UI.statTile({ label: "Projected race day", value: p.projected_finish, inset: true }));
    if (race.current_half) tiles.push(UI.statTile({ label: "Current fitness", value: `~${race.current_half}`, inset: true }));
    if (g.goal_half) tiles.push(UI.statTile({ label: "A goal", value: g.goal_half, foot: `${escapeHtml(g.goal_half_pace)}/km`, inset: true }));
    if (race.proven_ceiling) tiles.push(UI.statTile({ label: "Proven best", value: race.proven_ceiling.actual_time, foot: escapeHtml(shortDate(race.proven_ceiling.date)), inset: true }));
    const t = UI.tone(p.on_track);
    const title = p.on_track === "green" ? `On track for ${goalTime()}` : p.on_track === "amber" ? `${goalTime()} is within reach` : `${goalTime()} is a stretch`;
    b.innerHTML = `${UI.insightCallout({ tone: t, label: { green: "On track", amber: "In reach", red: "Stretch" }[p.on_track] || "Projection", title, body: p.projection_note || "" })}
      ${tiles.length ? `<div class="cx-stats">${tiles.join("")}</div>` : ""}`;
  }

  // A "good week" (counts toward the streak): the WORK that matters landed.
  // Server-computed (backend/coach.py) as: prescribed quality session done +
  // long run of at least 55% of the prescribed long + total volume >=70% of
  // target. So a shortened long or a lighter mid-week easy day doesn't cost the
  // streak as long as the anchor sessions and the Z2 base are in.
  const weekHit = (w) => {
    if (w.state === "future") return false;
    if (typeof w.good_week === "boolean") return w.good_week;
    // legacy fallback for older cached responses
    if (!w.target_km) return false;
    const workoutPct = w.workouts_planned ? w.workouts_done / w.workouts_planned : 1;
    return workoutPct >= 0.90 && (w.actual_km >= w.target_km * 0.85);
  };

  function renderProgress(p) {
    const b = $("#progressBody");
    if (!p || !p.weeks) { b.innerHTML = UI.emptyState({ icon: "calendar-days", title: "No plan data", compact: true }); return; }
    const vol = p.volume || {};
    const weeks = p.weeks;
    const past = weeks.filter((w) => w.state === "past");
    const cur = weeks.find((w) => w.state === "current");
    const pastHits = past.filter(weekHit).length;
    // trailing streak of completed weeks
    let streak = 0;
    for (let i = past.length - 1; i >= 0; i--) { if (weekHit(past[i])) streak++; else break; }
    if (cur && weekHit(cur)) streak++;

    // ---- season stat tiles ----
    const seasonPct = vol.planned_to_date ? Math.min(100, Math.round(100 * vol.actual_to_date / vol.planned_to_date)) : 0;
    const seasonTone = seasonPct >= 95 ? "good" : seasonPct >= 80 ? "warn" : "bad";
    const tiles = `<div class="cx-stats">
      ${UI.statTile({ label: "Volume to date", value: vol.actual_to_date ?? "–", unit: `/ ${vol.planned_to_date ?? "–"} km`, inset: true, tone: seasonTone, foot: UI.badge(`${seasonPct}% of plan`, { tone: seasonTone, size: "sm" }) })}
      ${UI.statTile({ label: "Good weeks", value: pastHits, unit: `/ ${past.length || 0}`, inset: true, foot: "Key sessions + 70% volume" })}
      ${UI.statTile({ label: "Current streak", value: streak, unit: streak === 1 ? "week" : "weeks", inset: true, tone: streak >= 3 ? "good" : null, icon: streak >= 3 ? "flame" : null })}
    </div>`;

    // ---- this-week focus ----
    let thisWeek = "";
    if (cur) {
      const target = cur.target_km || 0, actual = cur.actual_km || 0;
      const pct = target ? Math.min(100, Math.round(100 * actual / target)) : 0;
      const ws = new Date(cur.start_date + "T00:00:00");
      const todIso = iso(todayDate());
      // Walk the plan's actual schedule: how much is DUE by today, and how many
      // run days remain — a plan isn't linear (rest Mon, long run Sat).
      const map = planDayMap();
      let plannedToDate = 0, remainingRunDays = 0;
      for (let i = 0; i < 7; i++) {
        const d = new Date(ws); d.setDate(d.getDate() + i);
        const day = map[iso(d)];
        const km = (day && day.distance_km) || 0;
        // Today's km is not "due" until day-end — only count PAST days as scheduled-through.
        if (iso(d) < todIso) plannedToDate += km;
        else if (km > 0) remainingRunDays++;
      }
      plannedToDate = +plannedToDate.toFixed(1);
      const expectedPct = target ? Math.min(100, Math.round(100 * plannedToDate / target)) : 0;
      const remaining = Math.max(0, +(target - actual).toFixed(1));
      const perDay = remaining > 0 && remainingRunDays > 0 ? (remaining / remainingRunDays).toFixed(1) : 0;
      const done = actual >= target - 0.5;
      const behindKm = Math.max(0, +(plannedToDate - actual).toFixed(1));
      const onPace = done || behindKm <= 2;        // within 2 km of the plan's schedule = on plan
      const tone = onPace ? "good" : "warn";
      const days = `${remainingRunDays} run day${remainingRunDays === 1 ? "" : "s"}`;
      const msg = done
        ? "Weekly target hit. Anything more is a bonus, so bank it and recover."
        : onPace
          ? `${actual} km banked against ${plannedToDate} km the plan schedules through today. ${remaining} km left across ${days}.`
          : `${behindKm} km behind the plan's schedule. ${remaining} km to go over ${days} (about ${perDay} km a day), so nudge an easy run longer.`;
      thisWeek = `<div class="ui-well ui-stack" style="gap:var(--sp-3)">
        <div class="ui-row" style="justify-content:space-between"><span class="ui-overline">This week · W${cur.week} · ${escapeHtml(cur.phase || "")}</span>${UI.badge(done ? "Target hit" : onPace ? "On plan" : "Behind", { tone, size: "sm", autoIcon: true })}</div>
        <div class="ui-stat-value">${actual}<small>/ ${target} km</small></div>
        <span class="app-meter" style="--mc:var(--${tone === "good" ? "load-optimal" : "load-high"})" role="img" aria-label="${pct}% of the weekly target; the marker shows where the plan expects you today"><i style="width:${pct}%"></i><b style="left:${expectedPct}%"></b></span>
        <p class="ui-small ui-muted">${escapeHtml(msg)}</p></div>`;
    }

    // ---- weekly ladder (default to a 6-week window around "now"; expandable) ----
    let visibleWeeks = weeks;
    if (!S.progExpanded && weeks.length > 6) {
      let anchor = weeks.findIndex((w) => w.state === "current");
      if (anchor < 0) anchor = weeks.findIndex((w) => w.state === "future");
      if (anchor < 0) anchor = weeks.length - 1;
      const start = Math.max(0, Math.min(anchor - 1, weeks.length - 6));
      visibleWeeks = weeks.slice(start, start + 6);
    }
    const ladder = visibleWeeks.map((w) => {
      const pct = w.target_km ? Math.min(100, Math.round(100 * w.actual_km / w.target_km)) : 0;
      const over = w.target_km && w.actual_km > w.target_km;
      const hit = weekHit(w);
      const state = w.state === "current" ? "current" : w.state === "past" ? (hit ? "hit" : "miss") : "future";
      const color = { current: "--cat-1", hit: "--load-optimal", miss: "--load-high", future: "--border-strong" }[state];
      const flag = state === "hit" ? UI.badge("Good week", { tone: "good", size: "sm" }) : state === "miss" ? UI.badge("Short", { tone: "warn", size: "sm" }) : state === "current" ? UI.badge("Now", { tone: "coach", size: "sm" }) : "";
      const num = w.state === "future" ? `– / ${w.target_km} km` : `${w.actual_km} / ${w.target_km} km${over ? ` (+${Math.round(w.actual_km - w.target_km)})` : ""}`;
      return `<div class="cx-ladder-row is-${state}">
        <div class="lbl"><b>W${w.week}</b><span>${escapeHtml((w.phase || "").split("(")[0].trim())}</span></div>
        <span class="app-meter" style="--mc:var(${color})" aria-hidden="true"><i style="width:${pct}%"></i></span>
        <span class="v">${escapeHtml(num)} ${flag}</span></div>`;
    }).join("");

    const toggle = weeks.length > 6
      ? UI.button({ label: S.progExpanded ? "Show fewer weeks" : `Show all ${weeks.length} weeks`, variant: "ghost", size: "sm", attrs: { id: "ladderToggle" } })
      : "";
    b.innerHTML = `${tiles}${thisWeek}
      <section class="ui-stack" style="gap:var(--sp-4)" aria-labelledby="ladderH"><h3 class="ui-h3" id="ladderH">Weekly volume</h3>
      <div class="cx-ladder">${ladder}</div>${toggle}</section>`;
    const lt = $("#ladderToggle");
    if (lt) lt.onclick = () => { S.progExpanded = !S.progExpanded; renderProgress(S.progress); };
  }

  async function loadLastRun(offset) {
    const b = $("#lastRunBody");
    b.setAttribute("aria-busy", "true");
    try {
      const a = await api("/api/coach/last-run?offset=" + Math.max(0, offset | 0));
      S.lastRun = a;
      renderLastRun(a);
    } catch (e) {
      b.innerHTML = UI.errorState({ compact: true, title: "Couldn't load that run", retry: false });
    } finally {
      b.removeAttribute("aria-busy");
    }
  }

  function renderLastRun(a) {
    const b = $("#lastRunBody");
    if (!a || a.has_data === false) {
      b.innerHTML = UI.emptyState({ icon: "footprints", title: (a && a.message) || "No recent run found", compact: true });
      return;
    }
    const r = a.run || {};
    const idx = a.index || 0, count = a.count || 1;
    const pos = idx === 0 ? "Latest run" : `${idx} run${idx > 1 ? "s" : ""} back`;
    const pager = `<div class="cx-pager" role="group" aria-label="Browse runs">
      ${UI.button({ label: "Older run", iconOnly: true, icon: "chevron-left", variant: "secondary", size: "sm", disabled: !a.has_older, attrs: { "data-dir": "older" } })}
      <span class="pos" aria-live="polite">${escapeHtml(pos)} · ${idx + 1} of ${count}</span>
      ${UI.button({ label: "Newer run", iconOnly: true, icon: "chevron-right", variant: "secondary", size: "sm", disabled: !a.has_newer, attrs: { "data-dir": "newer" } })}
      ${idx !== 0 ? UI.button({ label: "Latest", variant: "ghost", size: "sm", icon: "undo-2", attrs: { "data-dir": "latest" } }) : ""}
    </div>`;
    const stats = [
      ["Distance", r.distance_km, "km"], ["Time", r.duration], ["Pace", r.avg_pace, "/km"],
      ["Grade-adj pace", r.gap_pace, r.gap_pace ? "/km" : ""], ["Avg HR", r.avg_hr, "bpm"], ["Power", r.power, r.power ? "W" : ""],
    ].filter(([, v]) => v != null && v !== "").map(([label, value, unit]) => UI.statTile({ label, value, unit, inset: true }));
    const notes = (a.insights && a.insights.length) ? renderInsights(a.insights, "Coach notes")
      : (a.notes && a.notes.length) ? UI.insightCallout({ variant: "inline", tone: "info", label: "Coach notes", body: a.notes }) : "";
    b.innerHTML = `
      ${pager}
      <div><h3 class="ui-h3">${escapeHtml(r.name || "Run")}</h3><p class="ui-caption">${escapeHtml(shortDate(r.date))}${a.hr_zone ? ` · ${hrZoneChip(a.hr_zone)}` : ""}</p></div>
      ${a.headline ? UI.insightCallout({ tone: UI.tone(a.verdict), label: "Coach's read", title: a.headline }) : ""}
      ${stats.length ? `<div class="cx-stats">${stats.join("")}</div>` : ""}
      ${notes}
      ${effortBlendHtml(a.effort, a.blend, r.gap_pace, r.avg_pace)}
      ${a.tip ? UI.insightCallout({ variant: "inline", tone: "info", label: "Tip", body: a.tip }) : ""}`;
    b.querySelectorAll("[data-dir]").forEach((btn) => {
      if (btn.disabled) return;
      btn.onclick = () => {
        const cur = (S.lastRun && S.lastRun.index) || 0;
        const dir = btn.dataset.dir;
        loadLastRun(dir === "older" ? cur + 1 : dir === "newer" ? cur - 1 : 0);
      };
    });
  }

  function renderPaces() {
    const paces = (S.dash && S.dash.paces) || {};
    const plan = paces.plan || {};
    const src = $("#pacesSource");
    if (src) {
      const map = { on_track: ["good", "Garmin agrees: on track"], slightly_ambitious: ["warn", "Garmin: slightly ambitious"], ambitious: ["bad", "Garmin: ambitious for current fitness"] };
      const [tone, txt] = map[paces.assessment] || [null, ""];
      src.innerHTML = txt ? UI.badge(txt, { tone, size: "sm", autoIcon: true }) : "";
    }
    const order = ["easy", "steady", "hmp", "threshold", "tenk", "fivek", "rep"];
    const body = $("#pacesBody"); if (!body) return;
    const keys = order.filter((k) => plan[k]);
    body.innerHTML = keys.length ? keys.map((k) => {
      const p = plan[k];
      const val = p.target || (p.low ? `${p.low}–${p.high}` : "–");
      return UI.statTile({ label: p.label || k, value: val, unit: "/km", cls: `fx-pace${k === "hmp" ? " is-race" : ""}`,
        foot: p.zone ? escapeHtml(p.zone) : "", icon: k === "hmp" ? "flag" : null });
    }).join("") : UI.emptyState({ icon: "timer", compact: true, title: "No paces yet", body: "They're set once Garmin has a race prediction." });
  }

  // ================= adaptive coach (beta) =================
  // Pending recs land at the top of the Coach tab as accept/reject cards.
  // Every decision feeds a learning ledger that tunes each rule's weight, so
  // the coach quiets down on rules the user consistently rejects.
  async function loadRecs() {
    try {
      const r = await api("/api/coach/recommendations");
      S.recs = r;
      renderRecs();
    } catch (e) {
      console.warn("[recs] fetch failed", e);
      const p = $("#recsPanel"); if (p) p.hidden = true;
    }
  }
  function renderRecs() {
    const panel = $("#recsPanel");
    if (!panel) return;
    const r = S.recs || {};
    const pending = r.pending || [];
    const decisions = r.recent_decisions || [];
    // Update the hero CTA + disclosure-count badge first.
    renderLpRecCta();
    const learn = Object.entries(r.weights || {}).filter(([, w]) => Math.abs(w - 1) > 0.05);
    panel.innerHTML = `
      <p class="ui-small ui-muted" style="max-width:var(--measure)">Accept, reject with a reason, or ignore. Every call teaches the coach: rules you reject get quieter, rules you accept get more confident.</p>
      ${pending.length
        ? `<div class="ui-stack" style="gap:var(--sp-3)">${pending.map(_recCardHtml).join("")}</div>`
        : UI.emptyState({ icon: "sparkles", compact: true, title: "No pending recommendations", body: "The coach is watching your readiness, HRV, sleep and missed workouts." })}
      ${learn.length ? `<div class="ui-row"><span class="ui-caption">What it has learned:</span>${learn.map(([k, w]) => UI.badge(`${k} ×${w.toFixed(2)}`, { size: "sm", tone: w > 1 ? "good" : "warn" })).join("")}</div>` : ""}
      ${decisions.length ? _recHistoryHtml(decisions) : ""}`;
    panel.querySelectorAll("[data-rec-accept]").forEach((b) => {
      b.addEventListener("click", () => _recDecide(b.dataset.recAccept, "accepted"));
    });
    panel.querySelectorAll("[data-rec-reject-open]").forEach((b) => {
      b.addEventListener("click", () => {
        const box = b.closest("[data-rec-id]").querySelector(".cx-reject");
        box.hidden = !box.hidden;
        b.setAttribute("aria-expanded", String(!box.hidden));
        if (!box.hidden) box.querySelector("textarea")?.focus();
      });
    });
    panel.querySelectorAll("[data-rec-reject-send]").forEach((b) => {
      b.addEventListener("click", () => {
        const reason = b.closest("[data-rec-id]").querySelector("textarea")?.value || "";
        _recDecide(b.dataset.recRejectSend, "rejected", reason);
      });
    });
  }
  function _recCardHtml(rec) {
    const conf = Math.round((rec.confidence || 0) * 100);
    const id = escapeHtml(rec.id);
    return `<div class="ui-card" data-rec-id="${id}" style="gap:var(--sp-3)">
      <div class="ui-row" style="justify-content:space-between;align-items:flex-start"><h3 class="ui-h3">${escapeHtml(rec.title || rec.rule)}</h3>${UI.badge(`${conf}% confidence`, { size: "sm", tone: conf >= 70 ? "coach" : "neutral" })}</div>
      <div class="ui-small ui-muted">${mdLite(rec.reason_md || "")}</div>
      ${rec.diff_preview ? `<div class="cx-diff"><span>Proposed change</span>${escapeHtml(rec.diff_preview)}</div>` : ""}
      <div class="ui-row">
        ${UI.button({ label: "Accept and apply", variant: "primary", size: "sm", icon: "check", attrs: { "data-rec-accept": rec.id } })}
        ${UI.button({ label: "Reject", variant: "ghost", size: "sm", attrs: { "data-rec-reject-open": rec.id, "aria-expanded": "false" } })}
      </div>
      <div class="cx-reject" hidden>
        ${UI.field({ label: "Why? (optional, helps the coach learn)", control: UI.textarea({ rows: 2 }) })}
        <div>${UI.button({ label: "Send rejection", variant: "secondary", size: "sm", attrs: { "data-rec-reject-send": rec.id } })}</div>
      </div>
    </div>`;
  }
  function _recHistoryHtml(decisions) {
    // Up to 20 recent decisions. Accepted recs include the plan-change summary;
    // rejected recs include the reason.
    const statusTone = { accepted: "good", rejected: "warn", applied_error: "bad" };
    const rows = decisions.slice(0, 20).map((d) => {
      const when = d.decided_at ? new Date(d.decided_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
      const applied = (d.apply_result && (d.apply_result.body || d.apply_result.title)) || "";
      return `<li>
        <div class="row">${UI.badge(String(d.status).replace("_", " "), { size: "sm", tone: statusTone[d.status] || "neutral" })}<b>${escapeHtml(d.title || d.rule)}</b><span class="when">${escapeHtml(when)}</span></div>
        ${d.diff_preview ? `<span class="ui-caption">${escapeHtml(d.diff_preview)}</span>` : ""}
        ${d.feedback ? `<span class="ui-muted">“${escapeHtml(d.feedback)}”</span>` : ""}
        ${d.status === "accepted" && applied ? `<span class="ui-caption">Applied: ${escapeHtml(applied.slice(0, 200))}</span>` : ""}
      </li>`;
    }).join("");
    return `<details class="ui-insight-why"><summary>Decision log (${decisions.length})</summary><ul class="cx-log">${rows}</ul></details>`;
  }
  async function _recDecide(id, decision, reason) {
    try {
      const r = await api(`/api/coach/recommendations/${encodeURIComponent(id)}/decide`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({decision, reason: reason || ""}),
      });
      if (r && r.error) { toast(r.error, false); return; }
      toast(decision === "accepted" ? "Applied — plan updated." : "Noted — the coach will factor that in.");
      await loadRecs();
      if (decision === "accepted") {
        // Plan changed — refresh dashboard and calendar so the day type flips.
        buildCalendar();
      }
    } catch (e) {
      toast("Couldn't record that: " + (e.message || e), false);
    }
  }

  // ================= injury / niggle log =================
  async function loadNiggles() {
    try {
      const r = await api("/api/niggles");
      S.niggles = r.niggles || [];
      renderNiggles();
    } catch (e) { console.warn("[niggles] fetch failed", e); }
  }
  const _sevTone = (s) => s >= 4 ? "bad" : s >= 3 ? "warn" : "info";
  function renderNiggles() {
    const card = $("#nigglesCard");
    if (!card) return;
    const list = S.niggles || [];
    const active = list.filter((n) => n.status === "active");
    // Reflect count on the disclosure summary.
    const countEl = $("#nigglesCount");
    if (countEl) countEl.innerHTML = active.length ? UI.badge(`${active.length} active`, { size: "sm", tone: "warn" }) : "";
    renderCoachSurface();
    const items = active.length
      ? `<ul class="cx-list">${active.map((n) => {
          const name = (n.side ? n.side + " " : "") + n.body_part;
          return `<li class="cx-item">
            <div class="cx-item-main">${UI.badge(`Severity ${n.severity}/5`, { size: "sm", tone: _sevTone(n.severity) })}<b>${escapeHtml(name)}</b>
              <span class="ui-caption">${n.days_active != null ? `${n.days_active} days` : ""}${n.aggravated_by ? ` · flares with ${escapeHtml(n.aggravated_by)}` : ""}</span></div>
            <div class="cx-item-actions">
              <label class="ui-sr" for="ngsev-${escapeHtml(n.id)}">Update severity for ${escapeHtml(name)}</label>
              ${UI.select({ value: n.severity, options: [1, 2, 3, 4, 5].map((s) => [s, `${s}/5`]), attrs: { id: `ngsev-${n.id}`, "data-ng-sev": n.id } })}
              ${UI.button({ label: "Resolved", variant: "secondary", size: "sm", icon: "check", attrs: { "data-ng-resolve": n.id } })}
            </div></li>`;
        }).join("")}</ul>`
      : UI.emptyState({ icon: "bandage", compact: true, title: "No active niggles", body: "Log one if something starts talking to you. The coach adapts the next few days around it." });
    card.innerHTML = `
      <div class="ui-row" style="justify-content:space-between"><p class="ui-small ui-muted">Active aches the coach plans around.</p>
        ${UI.button({ label: "Log a niggle", variant: "secondary", size: "sm", icon: "plus", attrs: { "data-ng-toggle-add": "", "aria-expanded": "false" } })}</div>
      <form class="cx-form" data-ng-form hidden>
        ${UI.field({ label: "Where?", hint: "e.g. left calf", control: UI.input({ attrs: { maxlength: 80, required: true, "data-ng-part": "" } }) })}
        ${UI.field({ label: "Side", control: UI.select({ options: [["", "–"], ["L", "Left"], ["R", "Right"], ["center", "Centre"]], attrs: { "data-ng-side": "" } }) })}
        ${UI.field({ label: "Severity", control: UI.select({ value: 2, options: [1, 2, 3, 4, 5].map((s) => [s, `${s}/5`]), attrs: { "data-ng-sevn": "" } }) })}
        <div class="full">${UI.field({ label: "Aggravated by (optional)", control: UI.input({ attrs: { maxlength: 120, "data-ng-agg": "" } }) })}</div>
        <div class="full ui-row">${UI.button({ label: "Save", variant: "primary", type: "submit" })}${UI.button({ label: "Cancel", variant: "ghost", attrs: { "data-ng-cancel": "" } })}</div>
      </form>
      ${items}`;
    const form = card.querySelector("[data-ng-form]");
    const toggleBtn = card.querySelector("[data-ng-toggle-add]");
    const setOpen = (open) => { form.hidden = !open; toggleBtn.setAttribute("aria-expanded", String(open)); if (open) form.querySelector("[data-ng-part]").focus(); };
    toggleBtn.addEventListener("click", () => setOpen(form.hidden));
    card.querySelector("[data-ng-cancel]").addEventListener("click", () => setOpen(false));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = form.querySelector("[data-ng-part]").value.trim();
      if (!body) return;
      const payload = {
        body_part: body,
        side: form.querySelector("[data-ng-side]").value || null,
        severity: parseInt(form.querySelector("[data-ng-sevn]").value, 10),
        aggravated_by: form.querySelector("[data-ng-agg]").value.trim(),
      };
      try {
        const r = await api("/api/niggles", {method: "POST",
          headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
        if (r.error) return toast(r.error, false);
        toast("Logged.");
        await loadNiggles();
      } catch (err) { toast("Couldn't save: " + err.message, false); }
    });
    card.querySelectorAll("[data-ng-sev]").forEach((sel) => {
      sel.addEventListener("change", async () => {
        try {
          const r = await api(`/api/niggles/${encodeURIComponent(sel.dataset.ngSev)}/update`, {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({severity: parseInt(sel.value, 10)}),
          });
          if (r.error) return toast(r.error, false);
          await loadNiggles();
        } catch (e) { toast("Couldn't update: " + e.message, false); }
      });
    });
    card.querySelectorAll("[data-ng-resolve]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await api(`/api/niggles/${encodeURIComponent(b.dataset.ngResolve)}/resolve`, {
            method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
          });
          toast("Marked resolved.");
          await loadNiggles();
        } catch (e) { toast("Couldn't resolve: " + e.message, false); }
      });
    });
  }

  // ================= environment / conditions strip =================
  async function loadEnvironment() {
    try {
      const r = await api("/api/environment/today");
      S.env = r;
      renderEnvironment();
    } catch (e) { console.warn("[env] fetch failed", e); }
  }
  function renderEnvironment() {
    // Conditions card beside today's call. Tone escalates with the heat tier;
    // cool/mild mornings stay quiet (neutral) so they don't compete with the call.
    const band = $("#lpConditions");
    if (!band) return;
    const d = S.env || {};
    if (!d.has_data) { band.hidden = true; return; }
    const tone = { warm: "warn", hot: "bad", dangerous: "bad" }[d.tier] || "info";
    const label = { none: "Cool", mild: "Mild", warm: "Warm", hot: "Hot", dangerous: "Dangerous heat" }[d.tier] || "Conditions";
    band.hidden = false;
    band.innerHTML = UI.card({ title: "Conditions", icon: "cloud-sun", actions: UI.badge(label, { tone, size: "sm" }), body: `
      <div class="cx-cond-row">
        <span><b>${escapeHtml(d.temp_c)}°C</b> · feels ${escapeHtml(d.heat_index_c)}°</span>
        <span><b>${escapeHtml(d.humidity_pct)}%</b> humidity</span>
        <span><b>${escapeHtml(d.wind_kmh)}</b> km/h wind${d.precip_mm ? ` · ${escapeHtml(d.precip_mm)} mm rain` : ""}</span>
      </div>
      ${d.pace_adjust_s_per_km || d.hr_drift_bpm ? `<div class="ui-row">${d.pace_adjust_s_per_km ? UI.badge(`+${d.pace_adjust_s_per_km} s/km`, { tone, size: "sm" }) : ""}${d.hr_drift_bpm ? UI.badge(`HR +${d.hr_drift_bpm} bpm`, { tone, size: "sm" }) : ""}</div>` : ""}
      ${d.read ? `<p class="ui-small ui-muted">${escapeHtml(d.read)}</p>` : ""}` });
  }

  // ================= today's context: pending plan change =================
  // The highest-confidence pending recommendation sits beside today's call so
  // accepting is one click. Also updates the disclosure count.
  function renderLpRecCta() {
    const cta = $("#lpRecCta");
    const countEl = $("#recsCount");
    const pending = ((S.recs && S.recs.pending) || []);
    const morning = (S.recs && S.recs.morning) || {};
    if (countEl) countEl.innerHTML = pending.length ? UI.badge(`${pending.length} pending`, { size: "sm", tone: "coach" }) : "";
    if (!cta) return;
    // No pending rec + morning read incomplete → say the coach is holding off
    // until Garmin's overnight sync lands (silence would read as "no opinion").
    if (!pending.length && morning && morning.complete === false) {
      cta.hidden = false;
      cta.innerHTML = UI.insightCallout({ tone: "info", label: "Waiting on Garmin", icon: "clock",
        title: "Holding recommendations until this morning's sync lands",
        body: `Still missing: ${(morning.missing || []).join(", ")}. Recommendations on stale data aren't real coaching.`,
        actions: UI.button({ label: "Refresh now", variant: "secondary", size: "sm", icon: "refresh-cw", attrs: { id: "lprRefreshBtn" } }) });
      cta.querySelector("#lprRefreshBtn")?.addEventListener("click", () => $("#refreshBtn")?.click());
      return;
    }
    if (!pending.length) { cta.hidden = true; cta.innerHTML = ""; return; }
    const r = pending[0];   // already sorted by confidence desc, target_date asc
    const conf = Math.round((r.confidence || 0) * 100);
    cta.hidden = false;
    cta.innerHTML = UI.insightCallout({ tone: "coach", label: `Plan change suggested · ${conf}% confidence`, icon: "sparkles",
      title: r.title || "", body: r.diff_preview || "",
      actions: UI.button({ label: "Accept", variant: "primary", size: "sm", icon: "check", attrs: { "data-lpr-accept": r.id } })
        + UI.button({ label: "Reject…", variant: "ghost", size: "sm", attrs: { "data-lpr-reject": r.id } }) });
    cta.querySelector("[data-lpr-accept]").addEventListener("click", () => _recDecide(r.id, "accepted"));
    cta.querySelector("[data-lpr-reject]").addEventListener("click", () => {
      // Open the recommendations panel with this rec's reason box ready.
      const disc = $("#recsDisc"); if (disc) disc.open = true;
      const card = document.querySelector(`#recsPanel [data-rec-id="${CSS.escape(r.id)}"]`);
      if (card) {
        const box = card.querySelector(".cx-reject"); if (box) box.hidden = false;
        card.scrollIntoView({ behavior: UI.prefersReducedMotion() ? "auto" : "smooth", block: "center" });
        card.querySelector("textarea")?.focus({ preventScroll: true });
      }
    });
  }

  // Composite renderer for the Coach surface: head, week strip, signals and
  // system cards. Called after every data refresh; each sub-renderer bails
  // silently when its DOM target isn't present.
  function renderCoachSurface() {
    renderCoachHead();
    renderCoachTimeline();
    renderInsightWall();
    renderDeepDives();
    wireLpLinks();
  }

  // Page head: today's date + the three numbers worth seeing before anything else.
  function renderCoachHead() {
    const dt = $("#cxDate");
    if (dt) dt.textContent = todayDate().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    const el = $("#cxHeadStats"); if (!el) return;
    const R = (S.recovery && S.recovery.readiness) || {};
    const H = (S.recovery && S.recovery.hrv) || {};
    const wp = _currentWeekProg();
    const cd = (S.dash && S.dash.countdown) || {};
    const chips = [];
    if (R.score != null) chips.push(UI.badge(`Readiness ${R.score}`, { tone: UI.tone(R.color), outline: true }));
    if (H.last != null) chips.push(UI.badge(`HRV ${H.last} ms`, { tone: H.state === "below" ? "warn" : "good", outline: true }));
    if (wp.done_km != null) chips.push(UI.badge(`${Math.round(wp.done_km)}${wp.target_km ? ` / ${Math.round(wp.target_km)}` : ""} km this week`, { outline: true }));
    if (cd.days_to_race != null) chips.push(UI.badge(`${cd.days_to_race} days to race`, { tone: "coach", outline: true, icon: "flag" }));
    el.innerHTML = chips.join("");
  }

  // ---- WEEK STRIP (past 4 · today · next 4), drawn with the calendar's day cell ----
  function renderCoachTimeline() {
    const row = $("#cxTimeline");
    if (!row) return;
    const today = todayDate();
    const todayIso = iso(today);
    const plan = (S.plan && S.plan.weeks) || [];
    const planByDate = {};
    plan.forEach((w) => (w.days || []).forEach((d) => (planByDate[d.date] = d)));
    const acts = (S.snap && S.snap.activities) || [];
    const actsByDate = {};
    for (const a of acts) {
      if (!a.date) continue;
      (actsByDate[a.date] = actsByDate[a.date] || []).push(a);
    }
    const days = [];
    for (let i = -4; i <= 4; i++) {
      const d = new Date(today); d.setDate(d.getDate() + i);
      const key = iso(d);
      const pd = planByDate[key];
      // snapshot.activities is already filtered to runs on the backend.
      const runs = actsByDate[key] || [];
      const isPast = key < todayIso;
      const runDay = pd && ["easy", "quality", "long", "race", "moderate", "steady", "tempo"].includes(pd.type);
      const km0 = runs.reduce((s, r) => s + (r.distance_km || 0), 0);
      const done = runs.length ? { km: +km0.toFixed(1), pace: runs[0].avg_pace_s_per_km ? paceStr(runs[0].avg_pace_s_per_km) : null, name: runs[0].name } : null;
      const state = done ? (runDay ? "done" : "unplanned")
        : !pd || pd.type === "rest" ? (pd ? "rest" : "empty")
        : isPast && runDay ? "missed" : "planned";
      const day = pd || done ? {
        type: pd ? pd.type : "easy", title: pd ? pd.title : (done && done.name), km: pd ? pd.distance_km : null,
        pace: pd && !pd.easy_hr ? paceLine(pd) || null : null, race: pd && pd.type === "race", strength: !!(pd && pd.strength), state, done,
      } : null;
      days.push(UI.workoutCell(key, day, { today: key === todayIso, showDow: true, maxKm: 0 }));
    }
    row.innerHTML = days.join("");
    // Each cell opens that day's plan (wired by wireLpLinks) and is a normal tab stop here.
    row.querySelectorAll(".ui-wcell[data-date]").forEach((c) => { c.dataset.cxOpenday = c.dataset.date; c.tabIndex = 0; });
  }

  // ---- SIGNALS: six 14-day trends + four plan tiles ------------------------
  // Replaces the old ticker, bento tiles and 12-tile wall, which repeated the
  // same readiness/HRV/sleep numbers three times.
  const _num = (v) => (v != null && isFinite(v) ? +v : null);
  /** Change of the latest value vs the mean of the (up to) 7 values before it. */
  function _deltaVsWeek(series) {
    const xs = (series || []).map(_num).filter((v) => v != null);
    if (xs.length < 3) return null;
    const last = xs[xs.length - 1], prev = xs.slice(-8, -1);
    return last - prev.reduce((s, v) => s + v, 0) / prev.length;
  }
  function renderInsightWall() {
    const wall = $("#cxWall");
    if (!wall) return;
    const snap = S.snap || {};
    const lr = snap.latest_readiness || {};
    const hrvArr = (snap.hrv || []).slice(-14);
    const hrvNow = hrvArr[hrvArr.length - 1] || {};
    const hrvBase = snap.hrv_baseline || {};
    const sleepArr = (snap.sleep || []).slice(-14);
    const sleepNow = sleepArr[sleepArr.length - 1] || {};
    const readinessArr = (snap.readiness_trend || []).slice(-14);
    const acwr = lr.acwr_pct;
    const acwrSeries = readinessArr.map((r) => r.acwr_pct);
    const fitSeries = ((S.fitness && S.fitness.form && S.fitness.form.series) || []).slice(-42);
    const form = S.fitness && S.fitness.form;
    const trends = (form && form.trends) || {};
    const sleepHrs = sleepNow.total_s ? sleepNow.total_s / 3600 : null;
    const sleepSeries = sleepArr.map((s) => (s.total_s ? s.total_s / 3600 : null));
    const hrvSeries = hrvArr.map((h) => h.last_night_avg);
    const rdSeries = readinessArr.map((r) => r.score);
    const lower = (s) => (s ? String(s).toLowerCase() : "");

    const readTone = lr.score == null ? "neutral" : lr.score >= 65 ? "good" : lr.score >= 45 ? "warn" : "bad";
    const hrvState = lower(hrvNow.status);
    const hrvTone = hrvState.includes("bal") ? "good" : hrvState.includes("low") || hrvState.includes("unbal") ? "warn" : "neutral";
    const acwrTone = acwr == null ? "neutral" : acwr >= 130 ? "bad" : acwr >= 100 ? "warn" : "good";
    const tsbTone = form ? UI.tone(form.form_color) : "neutral";

    wall.innerHTML = [
      UI.metricTrend({ label: "Readiness", icon: "gauge", value: lr.score ?? "–", unit: lr.score != null ? "/100" : "",
        status: lr.level ? { text: lower(lr.level), tone: readTone } : null,
        delta: _deltaVsWeek(rdSeries), deltaLabel: "vs last week's average", goodWhen: "up",
        series: rdSeries, color: "var(--cat-1)",
        read: lr.recovery_time_min ? `About ${Math.round(lr.recovery_time_min / 60)} h of recovery left.` : "Garmin's morning score.",
        attrs: { "data-lp-tab": "recovery" } }),
      UI.metricTrend({ label: "HRV", icon: "heart-pulse", value: hrvNow.last_night_avg ?? "–", unit: hrvNow.last_night_avg != null ? "ms" : "",
        status: hrvState ? { text: hrvState, tone: hrvTone } : null,
        delta: _deltaVsWeek(hrvSeries), deltaFmt: (d) => `${UI.fmt.signed(d)} ms`, deltaLabel: "vs last week's average", goodWhen: "up",
        series: hrvSeries, band: hrvBase.low ? [hrvBase.low, hrvBase.upper] : null, color: "var(--cat-2)",
        read: hrvBase.low ? `Your normal range is ${hrvBase.low}–${hrvBase.upper} ms (shaded).` : "Overnight average.",
        attrs: { "data-lp-tab": "recovery" } }),
      UI.metricTrend({ label: "Sleep", icon: "moon", value: sleepHrs != null ? UI.fmt.dur(sleepNow.total_s) : "–",
        delta: _deltaVsWeek(sleepSeries), deltaFmt: (d) => `${UI.fmt.signed(d, 1)} h`, deltaLabel: "vs last week's average", goodWhen: "up",
        series: sleepSeries, color: "var(--sleep-deep)",
        read: [sleepNow.score ? `Score ${sleepNow.score}` : "", sleepNow.deep_s ? `deep ${Math.round(sleepNow.deep_s / 60)} min` : ""].filter(Boolean).join(" · ") || "Last night.",
        attrs: { "data-lp-tab": "sleep" } }),
      UI.metricTrend({ label: "Load balance (ACWR)", icon: "flame", value: acwr ?? "–", unit: acwr != null ? "%" : "",
        status: acwr != null ? { text: acwr >= 130 ? "high risk" : acwr >= 100 ? "near capacity" : "in band", tone: acwrTone } : null,
        series: acwrSeries, color: "var(--cat-4)",
        read: "This week's load against your 4-week average. Under 100% is building safely.",
        attrs: { "data-lp-tab": "fitness" } }),
      UI.metricTrend({ label: "Fitness (CTL)", icon: "trending-up", value: form && form.current ? form.current.fitness : "–",
        delta: _num(trends.ctl_rate_per_week), deltaFmt: (d) => `${UI.fmt.signed(d, 1)}/wk`, deltaLabel: "per week", goodWhen: "up",
        series: fitSeries.map((p) => p.ctl), color: "var(--cat-1)",
        read: "42-day training load. Rises slowly as fitness builds.",
        attrs: { "data-lp-tab": "fitness" } }),
      UI.metricTrend({ label: "Form (TSB)", icon: "activity", value: form && form.current ? UI.fmt.signed(form.current.form) : "–",
        status: form && form.form_label ? { text: lower(form.form_label), tone: tsbTone } : null,
        series: fitSeries.map((p) => p.tsb), color: "var(--cat-5)",
        read: trends.days_negative_tsb_14 ? `Below zero on ${trends.days_negative_tsb_14} of the last 14 days.` : "Fitness minus fatigue. Positive means fresh.",
        attrs: { "data-lp-tab": "fitness" } }),
    ].join("");
    renderPlanTiles();
  }

  function renderPlanTiles() {
    const mount = $("#cxPlanTiles");
    if (!mount) return;
    const weekProg = _currentWeekProg();
    const nextHard = _findNextHard();
    const race = (S.dash && S.dash.config && S.dash.config.race) || {};
    const daysToRace = _daysUntil(race.date);
    const form = S.fitness && S.fitness.form;
    const raceTsb = form && form.forecast && race.date ? (form.forecast.find((p) => p.date === race.date) || {}).tsb : null;
    const active = (S.niggles || []).filter((n) => n.status === "active");
    const first = active[0];
    const link = (tab, disc) => ({ "data-go": tab, "data-lp-tab": tab, ...(disc ? { "data-lp-disc": disc } : {}) });
    const pct = weekProg.target_km ? Math.min(100, Math.round(100 * (weekProg.done_km || 0) / weekProg.target_km)) : null;
    mount.innerHTML = [
      UI.statTile({ label: "This week", icon: "route", value: weekProg.done_km != null ? Math.round(weekProg.done_km) : "–",
        unit: weekProg.target_km ? `/ ${Math.round(weekProg.target_km)} km` : "km",
        foot: pct != null ? `<span class="app-meter" style="flex:1;--mc:var(--load-optimal)" aria-hidden="true"><i style="width:${pct}%"></i></span><span class="num">${weekProg.done_count ?? 0}/${weekProg.total_count ?? 0} runs</span>` : "",
        attrs: link("calendar") }),
      UI.statTile({ label: "Next hard session", icon: "zap", value: nextHard ? nextHard.when : "–", unit: nextHard && nextHard.km ? `${nextHard.km} km` : "",
        foot: escapeHtml(nextHard ? nextHard.title.slice(0, 48) : "Nothing hard scheduled"), attrs: link("calendar") }),
      UI.statTile({ label: race.name ? `Race · ${race.name}` : "Race", icon: "flag", value: daysToRace ?? "–", unit: daysToRace != null ? "days" : "",
        tone: raceTsb != null && raceTsb < 5 ? "warn" : null,
        foot: raceTsb != null ? `Projected race-day form ${UI.fmt.signed(raceTsb, 1)}` : "No race set", attrs: link("fitness") }),
      UI.statTile({ label: "Niggles", icon: "bandage", value: active.length, unit: active.length ? "active" : "clear",
        tone: active.length === 0 ? "good" : active.length === 1 ? "warn" : "bad",
        foot: escapeHtml(first ? `${(first.side ? first.side + " " : "") + first.body_part} · severity ${first.severity}/5` : "Nothing logged"),
        attrs: link("overview", "nigglesDisc") }),
    ].join("");
  }

  // Pull the current-week row from S.progress.weeks and normalize into the
  // {done_km, target_km, done_count, total_count, quality_done, quality_total,
  //  long_streak} shape the tiles + observation reader use.
  function _currentWeekProg() {
    const weeks = (S.progress && S.progress.weeks) || [];
    const cur = weeks.find((w) => w.state === "current") || {};
    // Long-run streak: consecutive past weeks where a long run (>= 12km) landed,
    // ending at the current week (inclusive if today has one).
    let streak = 0;
    const past = weeks.filter((w) => w.state === "past" || w.state === "current");
    for (let i = past.length - 1; i >= 0; i--) {
      const w = past[i];
      if (w.long_hit || (w.long_ratio || 0) >= 0.55) streak++;
      else break;
    }
    return {
      done_km: cur.actual_km,
      target_km: cur.target_km,
      done_count: cur.workouts_done,
      total_count: cur.workouts_planned,
      quality_done: cur.quality_done_est,
      quality_total: cur.quality_planned,
      long_streak: streak,
    };
  }

  function _daysUntil(iso) {
    if (!iso) return null;
    try { return Math.max(0, Math.round((new Date(iso + "T00:00:00") - todayDate()) / 86400000)); }
    catch(_){ return null; }
  }

  function _findNextHard() {
    const plan = (S.plan && S.plan.weeks) || [];
    const today = todayDate();
    for (const wk of plan) for (const d of (wk.days || [])) {
      if (!d.date) continue;
      const dd = new Date(d.date + "T00:00:00");
      if (dd < today) continue;
      if (["quality", "long", "race"].includes(d.type)) {
        const days = Math.round((dd - today) / 86400000);
        const when = days === 0 ? "Today" : days === 1 ? "Tmrw" : dd.toLocaleDateString(undefined, {weekday: "short"});
        return {when, days, type: d.type, title: d.title || "", km: d.distance_km || 0};
      }
    }
    return null;
  }

  // ---- 4: DEEP-DIVE CATEGORY CARDS ---------------------------------------
  // ---- WHAT'S GOING ON: one narrative card per system ----------------------
  function renderDeepDives() {
    const wrap = $("#cxCats");
    if (!wrap) return;
    const snap = S.snap || {};
    const lr = snap.latest_readiness || {};
    const hrv = (snap.hrv || []).slice(-1)[0] || {};
    const sleep = (snap.sleep || []).slice(-1)[0] || {};
    const nActive = (S.niggles || []).filter((n) => n.status === "active");
    const form = S.fitness && S.fitness.form;
    const trends = (form && form.trends) || {};
    const verdict = form && form.load_verdict;
    const weekProg = _currentWeekProg();
    const race = (S.dash && S.dash.config && S.dash.config.race) || {};
    const daysToRace = _daysUntil(race.date);
    const env = S.env || {};
    const nextHard = _findNextHard();
    const sleepHrs = sleep.total_s ? (sleep.total_s / 3600).toFixed(1) : null;
    // Whether today's planned run has already been logged - avoids the coach
    // observations reading like "hold easy today" AFTER the run is done.
    const todayInfo = _todaySessionInfo();

    const bodyFlag = nActive.length
      ? `${escapeHtml((nActive[0].side ? nActive[0].side + " " : "") + nActive[0].body_part)} at severity ${escapeHtml(nActive[0].severity)}/5${nActive[0].aggravated_by ? `, flares with ${escapeHtml(nActive[0].aggravated_by)}` : ""}.`
      : "";
    const fitnessFlag = (form && form.observations || []).find((o) => o.tone === "warn");

    // 2-3 sentence coach observations composed from live data (deterministic —
    // the same page state produces the same read).
    const obs = {
      body: _obsBody(lr, hrv, snap.hrv_baseline || {}, sleepHrs, sleep, nActive, todayInfo),
      fitness: _obsFitness(form, trends, verdict, daysToRace, race),
      training: _obsTraining(weekProg, nextHard, daysToRace, race, todayInfo),
      environment: _obsEnvironment(env),
    };
    const kv = (rows) => `<dl class="cx-kv">${rows.map(([k, v, u]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v ?? "–")}${u ? `<small>${escapeHtml(u)}</small>` : ""}</dd></div>`).join("")}</dl>`;
    const signed = (v) => (v == null ? null : UI.fmt.signed(v, Math.abs(v) < 10 && v % 1 ? 1 : 0));

    const cards = [
      { title: "Body", icon: "heart-pulse", tab: "recovery", go: "Recovery",
        tone: nActive.length || (lr.score != null && lr.score < 45) ? "warn" : lr.score != null && lr.score >= 65 ? "good" : "info",
        obs: obs.body, flag: bodyFlag,
        rows: [["Readiness", lr.score], ["HRV", hrv.last_night_avg, "ms"], ["Sleep", sleepHrs, "h"], ["Niggles", nActive.length, "active"]] },
      { title: "Fitness", icon: "trending-up", tab: "fitness", go: "Fitness",
        tone: fitnessFlag ? "warn" : UI.tone((verdict && verdict.tone) || "info"),
        obs: obs.fitness, flag: fitnessFlag ? fitnessFlag.detail : (verdict ? verdict.message : ""),
        rows: [["Fitness (CTL)", form && form.current ? form.current.fitness : null], ["Form (TSB)", form && form.current ? signed(form.current.form) : null],
          ["Rate", signed(trends.ctl_rate_per_week), "/wk"], ["Load (ACWR)", lr.acwr_pct, "%"]] },
      { title: "Plan", icon: "calendar-days", tab: "calendar", go: "Plan", tone: "info",
        obs: obs.training, flag: "",
        rows: [["This week", weekProg.done_km != null ? Math.round(weekProg.done_km) : null, weekProg.target_km ? `/ ${Math.round(weekProg.target_km)} km` : "km"],
          ["Quality done", weekProg.quality_done || 0, `/ ${weekProg.quality_total || 0}`], ["Long-run streak", weekProg.long_streak || 0, "weeks"], ["Race in", daysToRace, "days"]] },
      { title: "Conditions", icon: "cloud-sun", tab: null, go: null,
        tone: env.tier === "hot" || env.tier === "dangerous" ? "bad" : env.tier === "warm" ? "warn" : "info",
        obs: obs.environment, flag: "",
        rows: [["Now", env.temp_c != null ? `${env.temp_c}°C` : null], ["Feels", env.heat_index_c != null ? `${env.heat_index_c}°` : null],
          ["Wind", env.wind_kmh, "km/h"], ["Pace adjust", env.pace_adjust_s_per_km ? `+${env.pace_adjust_s_per_km}` : "none", env.pace_adjust_s_per_km ? "s/km" : ""]] },
    ];
    wrap.innerHTML = cards.map((c) => UI.card({
      title: c.title, icon: c.icon, cls: "cx-sys",
      actions: c.tab ? `<a class="ui-btn ghost sm" href="#" data-lp-tab="${c.tab}">${escapeHtml(c.go)}${UI.icon("chevron-right", { size: 16 })}</a>` : "",
      body: `${UI.insightCallout({ variant: "inline", tone: c.tone, label: false, body: c.obs })}
        ${c.flag ? UI.insightCallout({ variant: "inline", tone: c.tone === "good" ? "info" : c.tone, label: "Watch", body: c.flag }) : ""}
        ${kv(c.rows)}`,
    })).join("");
  }

  // ------------ Deep-dive observation synthesizers -------------------------
  // Returns {done: bool, type: str, km_done: number, km_planned: number} for
  // today. done = today's planned run has landed at ≥50% of prescribed km.
  function _todaySessionInfo() {
    const today = iso(todayDate());
    const plan = (S.plan && S.plan.weeks) || [];
    let planDay = null;
    for (const wk of plan) for (const d of (wk.days || [])) if (d.date === today) { planDay = d; break; }
    const runs = ((S.snap && S.snap.activities) || [])
      .filter((a) => a.date === today && (a.type || "").toLowerCase().includes("run"));
    const kmDone = runs.reduce((s, r) => s + (r.distance_km || 0), 0);
    const kmPlanned = (planDay && planDay.distance_km) || 0;
    const isRunDay = planDay && ["easy","quality","long","race","moderate","steady","tempo"].includes(planDay.type);
    const done = isRunDay && kmDone >= Math.max(2, kmPlanned * 0.5);
    return {
      done, type: planDay && planDay.type, title: planDay && planDay.title,
      km_done: Math.round(kmDone * 10) / 10,
      km_planned: kmPlanned,
      pace: runs[0] && runs[0].avg_pace_s_per_km ? paceStr(runs[0].avg_pace_s_per_km) : null,
      avg_hr: runs[0] && runs[0].avg_hr,
      is_run_day: isRunDay,
    };
  }

  function _obsBody(lr, hrv, hrvB, sleepHrs, sleep, nActive, todayInfo) {
    const parts = [];
    const score = lr && lr.score;
    const hrvVal = hrv && hrv.last_night_avg;
    const inBand = hrvVal != null && hrvB.low != null && hrvVal >= hrvB.low && hrvVal <= (hrvB.upper || 999);
    const done = todayInfo && todayInfo.done;

    if (done) {
      // Session already banked — flip from prescriptive to reflective.
      parts.push(`Today's ${todayInfo.type || "run"} is done — ${todayInfo.km_done} km logged${todayInfo.pace ? ` @ ${todayInfo.pace}/km` : ""}${todayInfo.avg_hr ? `, HR ${todayInfo.avg_hr}` : ""}.`);
      if (score != null) {
        parts.push(`This morning's readiness read ${score}/100${lr.level ? ` (${String(lr.level).toLowerCase()})` : ""}${hrvVal != null ? `, HRV ${hrvVal} ms ${inBand ? "in-band" : "outside baseline"}` : ""}.`);
      }
      parts.push("Focus is recovery now — refuel, sleep, and let the readings tomorrow morning tell you how the session landed.");
    } else if (score == null) {
      parts.push("Waiting on Garmin's overnight sync to read this morning's markers.");
    } else if (score >= 65 && inBand && (sleepHrs == null || sleepHrs >= 7)) {
      parts.push(`Recovery landed well — readiness ${score}/100${lr.level ? ` (${String(lr.level).toLowerCase()})` : ""} and HRV ${hrvVal} ms sits inside your baseline band.`);
      if (sleepHrs) parts.push(`${sleepHrs}h of sleep with a score of ${sleep.score || "—"} is the setup you want.`);
      parts.push("You're clear to train as planned.");
    } else if (score >= 45) {
      parts.push(`Moderate readiness — ${score}/100${lr.level ? ` (${String(lr.level).toLowerCase()})` : ""}${lr.recovery_time_min ? `, with ${Math.round(lr.recovery_time_min/60)}h still on the recovery clock` : ""}.`);
      if (hrvVal != null) parts.push(`HRV ${hrvVal} ms is ${inBand ? "in-band" : "outside your baseline"}, so the score is doing most of the talking.`);
      parts.push("Train, but hold easy at true easy — don't let strides drift into a workout.");
    } else {
      parts.push(`Body's flagging real fatigue — readiness ${score}/100${lr.recovery_time_min ? ` with a ${Math.round(lr.recovery_time_min/60)}h recovery clock` : ""}.`);
      if (hrvVal != null && !inBand) parts.push(`HRV ${hrvVal} ms sits ${hrvVal < hrvB.low ? "below" : "above"} your baseline (${hrvB.low}-${hrvB.upper}), corroborating the fatigue signal.`);
      parts.push("Consider swapping quality for easy today, or a rest.");
    }
    if (nActive && nActive.length) {
      const n = nActive[0];
      parts.push(`Also active: ${escapeHtml((n.side ? n.side + " " : "") + n.body_part)} at sev ${escapeHtml(n.severity)}/5 — mind ${escapeHtml(n.aggravated_by || "the movements that flare it")}.`);
    }
    return parts.join(" ");
  }

  function _obsFitness(form, trends, verdict, daysToRace, race) {
    if (!form || !form.current) return "Not enough training-load history yet — a couple of weeks of runs and this fills in.";
    const rate = trends.ctl_rate_per_week;
    const ratio = trends.atl_ctl_ratio;
    const parts = [];
    if (rate != null) {
      if (rate > 1) parts.push(`Fitness is climbing at ${rate >= 0 ? "+" : ""}${rate}/wk over the last 28 days — the volume is compounding.`);
      else if (rate > 0.2) parts.push(`Fitness is nudging up at ${rate >= 0 ? "+" : ""}${rate}/wk — steady building block.`);
      else if (rate > -0.5) parts.push(`Fitness has plateaued (${rate >= 0 ? "+" : ""}${rate}/wk over 28d) — holding gain, not adding.`);
      else parts.push(`Fitness is slipping (${rate}/wk over 28d) — check for planned taper vs unplanned drop.`);
    }
    const tsb = form.current.form;
    const label = (form.form_label || "").toLowerCase();
    if (tsb <= -30) parts.push(`Form ${tsb >= 0 ? "+" : ""}${tsb} is deep in overreached territory — an easier week converts this into fitness.`);
    else if (tsb <= -10) parts.push(`Form is ${label} (${tsb >= 0 ? "+" : ""}${tsb})${ratio != null && ratio > 1.1 && ratio <= 1.3 ? ", constructive overload from an ATL/CTL of " + ratio : ""}.`);
    else if (tsb <= 5) parts.push(`Form is ${label} (${tsb >= 0 ? "+" : ""}${tsb}) — the productive training zone.`);
    else parts.push(`Form ${tsb >= 0 ? "+" : ""}${tsb} is fresh — good window for a race or key session.`);
    if (race && race.date && daysToRace != null && daysToRace <= 60) {
      const raceTsb = form.forecast && (form.forecast.find((p) => p.date === race.date) || {}).tsb;
      if (raceTsb != null) {
        if (raceTsb < 5) parts.push(`Race-day projection ${daysToRace}d out: TSB ${raceTsb >= 0 ? "+" : ""}${raceTsb.toFixed(1)} — under-tapered; a cutback week fixes this.`);
        else if (raceTsb > 20) parts.push(`Race-day projection ${daysToRace}d out: TSB ${raceTsb >= 0 ? "+" : ""}${raceTsb.toFixed(1)} — over-tapered; you may be losing fitness.`);
        else parts.push(`Race-day projection ${daysToRace}d out: TSB ${raceTsb >= 0 ? "+" : ""}${raceTsb.toFixed(1)} — perfect freshness window.`);
      }
    }
    return parts.join(" ");
  }

  function _obsTraining(weekProg, nextHard, daysToRace, race, todayInfo) {
    const parts = [];
    const done = todayInfo && todayInfo.done;

    if (done) {
      parts.push(`Today's ${todayInfo.type || "run"} banked${todayInfo.km_done ? ` — ${todayInfo.km_done} km` : ""}${todayInfo.km_planned ? ` of the planned ${todayInfo.km_planned} km` : ""}.`);
    }
    if (weekProg && weekProg.done_km != null && weekProg.target_km) {
      const kmDone = Math.round(weekProg.done_km), target = Math.round(weekProg.target_km);
      const pct = Math.round(100 * kmDone / target);
      const dc = weekProg.done_count || 0, tc = weekProg.total_count || 0;
      if (pct >= 100) parts.push(`Week is done — ${kmDone}/${target} km logged, ${dc} of ${tc} sessions completed. Full box ticked.`);
      else if (pct >= 80) parts.push(`Week is on rails — ${kmDone}/${target} km (${pct}%), ${dc} of ${tc} sessions in the book.`);
      else if (pct >= 50) parts.push(`Half-way through the week — ${kmDone}/${target} km (${pct}%), ${tc - dc} sessions still to go.`);
      else parts.push(`Early in the week — ${kmDone}/${target} km so far, most of the plan still ahead.`);
    } else {
      parts.push("Week volume rolling in as runs land.");
    }
    // "Next hard" — if today's session was the hard one and it's done, look
    // past it instead of restating what already happened.
    if (nextHard) {
      const isTodayNext = nextHard.days === 0;
      if (!(isTodayNext && done)) {
        parts.push(`Next hard: ${nextHard.type} ${nextHard.when.toLowerCase()} — ${escapeHtml(nextHard.title.slice(0, 60))}.`);
      } else {
        // find the one AFTER today
        const after = _findNextHardAfter(1);
        if (after) parts.push(`Next hard after today: ${after.type} ${after.when.toLowerCase()} — ${escapeHtml(after.title.slice(0, 60))}.`);
      }
    }
    if (race && race.date && daysToRace != null) {
      if (daysToRace === 0) parts.push(`Race day. Everything in the box is executed here.`);
      else if (daysToRace <= 7) parts.push(`Race in ${daysToRace} d — pull back on volume, keep frequency and short race-pace touches.`);
      else if (daysToRace <= 21) parts.push(`Race in ${daysToRace} d — final build then taper. Peak sessions land this week or next.`);
      else parts.push(`Race in ${daysToRace} d — still in the build; execute the pattern, no need to force volume.`);
    }
    return parts.join(" ");
  }

  // Same as _findNextHard but skips ahead N days (used to look past today).
  function _findNextHardAfter(daysOffset) {
    const plan = (S.plan && S.plan.weeks) || [];
    const start = todayDate();
    start.setDate(start.getDate() + (daysOffset || 1));
    for (const wk of plan) for (const d of (wk.days || [])) {
      if (!d.date) continue;
      const dd = new Date(d.date + "T00:00:00");
      if (dd < start) continue;
      if (["quality", "long", "race"].includes(d.type)) {
        const days = Math.round((dd - todayDate()) / 86400000);
        const when = days === 1 ? "Tmrw" : dd.toLocaleDateString(undefined, {weekday: "short"});
        return {when, days, type: d.type, title: d.title || "", km: d.distance_km || 0};
      }
    }
    return null;
  }

  function _obsEnvironment(env) {
    if (!env || !env.has_data) return "Weather hasn't synced. Refresh Garmin or the forecast to get today's read.";
    const t = env.temp_c, rh = env.humidity_pct, hi = env.heat_index_c, adj = env.pace_adjust_s_per_km, wind = env.wind_kmh;
    const tier = env.tier;
    const parts = [];
    if (tier === "none" || tier === "cool") {
      parts.push(`Cool conditions — ${t}°C at ${rh}% humidity, feels ${hi}°.`);
      parts.push("No heat penalty; run to your prescribed pace and HR targets.");
      if (wind && wind >= 25) parts.push(`Wind is up (${wind} km/h) — expect a couple s/km on any exposed segment.`);
    } else if (tier === "mild") {
      parts.push(`Mild warmth — ${t}°C, ${rh}% humidity (feels ${hi}°).`);
      parts.push(`Expect ~${adj}s/km slower at the same effort; HR will sit a touch higher.`);
    } else if (tier === "warm") {
      parts.push(`Warm out — ${t}°C, ${rh}% RH (feels ${hi}°).`);
      parts.push(`Add ~${adj}s/km to easy/long targets and bring fluids; HR drift will be visible.`);
    } else if (tier === "hot") {
      parts.push(`Hot conditions — ${t}°C, ${rh}% RH, feels ${hi}°.`);
      parts.push(`Run to HR ceiling, not pace — expect ~${adj}s/km slower and keep the hard portions short.`);
    } else {
      parts.push(`Dangerous heat — ${t}°C / ${rh}% RH, feels ${hi}°.`);
      parts.push(`Move the workout indoors, split it, or shift the clock. If you go: easy cap, hydrate, abort on HR spikes.`);
    }
    return parts.join(" ");
  }

  // Click a launchpad link/chip → switch to the named tab (and optionally
  // open a disclosure on the same page).
  function wireLpLinks() {
    document.querySelectorAll("[data-lp-tab]").forEach((el) => {
      if (el.dataset.lpWired) return;
      el.dataset.lpWired = "1";
      el.addEventListener("click", (e) => {
        e.preventDefault();
        const tab = el.dataset.lpTab;
        const discId = el.dataset.lpDisc;
        if (tab && tab !== curTab()) goTab(tab);
        if (discId) {
          const d = document.getElementById(discId);
          if (d) { d.open = true; d.scrollIntoView({ behavior: UI.prefersReducedMotion() ? "auto" : "smooth", block: "start" }); }
        }
      });
    });
    // Timeline day cards drill into the calendar tab and open that date's
    // detail modal directly (skip the tab-switch dance).
    document.querySelectorAll("[data-cx-openday]").forEach((el) => {
      if (el.dataset.lpWired) return;
      el.dataset.lpWired = "1";
      el.addEventListener("click", (e) => {
        e.preventDefault();
        const dateIso = el.dataset.cxOpenday;
        if (!dateIso) return;
        // Switch to the plan so the sheet opens on top of the right view.
        if (curTab() !== "calendar") goTab("calendar");
        // Move the calendar anchor to that day's week so the underlying grid
        // reflects the same time frame.
        try {
          const d = new Date(dateIso + "T00:00:00");
          if (S.cal) { S.cal.anchor = d; buildCalendar(); }
        } catch (_) {}
        // Open the detail modal for that date - use the plan-day object if
        // one exists, otherwise a minimal shell so the day-context loader
        // still pulls readings + activity for that day.
        const day = planDayMap()[dateIso]
          || { date: dateIso, type: "rest", title: "Off-plan day", distance_km: 0 };
        openDay(day);
      });
    });

    // Ask-coach button in the verdict card opens the chat dock (once).
    const askBtn = $("#cxAskCoachBtn");
    if (askBtn && !askBtn.dataset.lpWired) {
      askBtn.dataset.lpWired = "1";
      askBtn.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        const fab = $("#chatFab"); if (fab) fab.click();
      });
    }
  }

  // ================= charts per tab =================
  function renderActiveTab() {
    if (!S.dash) return;                       // nothing to draw until loadAll() lands
    const tab = curTab();
    if (S.rendered[tab]) return;
    S.rendered[tab] = true;
    const sn = S.snap;
    if (tab === "activity") renderActivity();
    else if (tab === "fitness") renderFitness();
    else if (tab === "sleep") renderSleep();
    else if (tab === "recovery") renderRecovery();
    else if (tab === "log") renderLog();
    applyTooltips();
  }

  // ---- AnnotatedChart registry: one live chart per mount id (re-renders and
  // theme switches replace, never stack, charts on the same mount) ----
  S.ui = S.ui || {};
  function uiChart(id, cfg, kind = "annotated") {
    const mount = typeof id === "string" ? document.getElementById(id) : id;
    if (!mount) return null;
    const key = mount.id || id;
    if (S.ui[key]) { S.ui[key].destroy(); delete S.ui[key]; }
    S.ui[key] = kind === "forecast" ? UI.ForecastChart(mount, cfg) : UI.AnnotatedChart(mount, cfg);
    return S.ui[key];
  }
  // Run type -> intensity zone (color means intensity everywhere).
  const RUN_ZONE = { easy: 2, long: 2, recovery: 1, moderate: 3, steady: 3, tempo: 4, quality: 4, intervals: 5, race: 4 };
  const RUN_LABEL = { easy: "Easy", long: "Long", moderate: "Grey-zone", quality: "Quality" };
  const runZone = (t) => RUN_ZONE[t] || 2;
  const dLbl = (isoStr, i, where) => where === "tick" ? shortDate(isoStr) : UI.fmt.date(isoStr, { weekday: "short", month: "short", day: "numeric" });

  function renderActivity() {
    const A = S.activity, sn = S.snap;
    if (!A || !A.has_data) {
      const msg = (A && A.message) || "No activity synced yet.";
      $("#actTiles").innerHTML = "";
      $("#actSummary").innerHTML = UI.emptyState({ icon: "activity", title: "No runs yet", body: msg, compact: true });
      ["axStripCard", "axThisWeek", "chVolume", "chIntensity", "chPaceType", "chCadence", "actLog", "chZones", "zoneRead", "intensityRead"].forEach((id) => { const n = $("#" + id); if (n) n.innerHTML = ""; });
      renderDynamics(null);
      return;
    }

    // ---- last-4-week tiles (also the page's key numbers) ----
    const T = A.tiles || {};
    const delta = T.vol_delta == null ? "" : `<span class="app-delta">${UI.icon(T.vol_delta >= 0 ? "arrow-up-right" : "arrow-down-right", { size: 14 })}${UI.fmt.signed(T.vol_delta)} km</span> vs prior 4 wk`;
    const easyTone = T.grey_pct >= 20 ? "warn" : T.easy_pct >= 75 ? "good" : "warn";
    $("#actTiles").innerHTML = [
      UI.statTile({ label: "Weekly volume (4 wk)", icon: "route", value: T.vol_4wk ?? "–", unit: "km", foot: delta }),
      UI.statTile({ label: "Runs per week", icon: "footprints", value: T.runs_per_wk ?? "–" }),
      UI.statTile({ label: "Easy time", icon: "heart-pulse", value: T.easy_pct ?? "–", unit: "%", tone: easyTone, foot: `${T.hard_pct ?? "–"}% hard · ${T.grey_pct ?? "–"}% grey` }),
      UI.statTile({ label: "Grey-zone km", icon: "triangle-alert", value: T.grey_pct ?? "–", unit: "%", tone: T.grey_pct >= 25 ? "bad" : T.grey_pct >= 15 ? "warn" : "good", foot: "Aim under 20%" }),
      UI.statTile({ label: "Avg cadence", icon: "activity", value: T.avg_cadence ?? "–", unit: T.avg_cadence ? "spm" : "", tone: T.avg_cadence && T.avg_cadence < 170 ? "warn" : null }),
      UI.statTile({ label: "Longest run (4 wk)", icon: "flag", value: T.longest_km ?? "–", unit: "km" }),
    ].join("");

    // ---- coach's read (was hidden in a collapsed panel at the bottom) ----
    const sum = A.summary || [];
    $("#actSummary").innerHTML = sum.length
      ? UI.insightList(sum.map((s) => ({ tone: { good: "good", watch: "warn", improve: "warn" }[s.tone] || "info", title: s.title, detail: s.detail })))
      : UI.emptyState({ icon: "sparkles", compact: true, title: "Not enough runs yet for a pattern read" });

    _renderActivityStrip(A, sn);
    _renderActivityThisWeek(A, sn);

    // ---- weekly volume vs target ----
    const wv = sn.weekly_volume || [];
    const target = A.target_km || 50;
    const wkLbl = (w) => w.week_start;
    uiChart("chVolume", {
      title: "Weekly volume", takeaway: wv.length ? `${(wv[wv.length - 1].km || 0).toFixed(1)} km so far this week against a ${target} km target.` : "",
      labels: wv.map(wkLbl), xLabel: "Week of", xFormat: (l, i, w) => (w === "tick" ? shortDate(l) : `Week of ${shortDate(l)}`), height: 220,
      y: { beginAtZero: true, suggestedMax: target * 1.08, fmt: (v) => `${v}` }, empty: "No weekly volume yet",
      series: [
        { label: "Distance", unit: "km", type: "bar", data: wv.map((w) => +(w.km || 0).toFixed(1)), legend: false,
          colorBy: (v, i) => (i === wv.length - 1 ? "--neutral-soft" : v >= target * 0.9 ? "--load-optimal" : "--load-high") },
      ],
      targets: [{ value: target, label: `Target ${target}` }],
      legendExtra: ['<i class="bar" style="--sw:var(--load-optimal)"></i>On target', '<i class="bar" style="--sw:var(--load-high)"></i>Under target', '<i class="bar" style="--sw:var(--neutral-soft)"></i>This week (in progress)'],
    });

    // ---- weekly intensity minutes (stacked) ----
    uiChart("chIntensity", {
      title: "Intensity minutes", takeaway: "Moderate and vigorous minutes per week, from Garmin.",
      labels: wv.map(wkLbl), xLabel: "Week of", xFormat: (l, i, w) => (w === "tick" ? shortDate(l) : `Week of ${shortDate(l)}`), height: 220,
      y: { beginAtZero: true }, empty: "No intensity minutes yet",
      series: [
        { label: "Moderate", unit: "min", type: "bar", stack: "i", data: wv.map((w) => w.moderate_min || 0), color: "--z2", fmt: (v) => `${v}` },
        { label: "Vigorous", unit: "min", type: "bar", stack: "i", data: wv.map((w) => w.vigorous_min || 0), color: "--z4", fmt: (v) => `${v}` },
      ],
    });

    // ---- pace by run type: one series per type, points only; select a point to open the run ----
    const ps = A.pace_series || [];
    const types = ["easy", "long", "moderate", "quality"].filter((t) => ps.some((p) => p.type === t));
    uiChart("chPaceType", {
      title: "Pace by run type", takeaway: "Each dot is a run. Select one for its breakdown.",
      labels: ps.map((p) => p.date), xFormat: dLbl, height: 240, empty: "No paced runs yet",
      y: { reverse: true, fmt: (v) => paceStr(v), hint: "Faster ↑" },
      series: types.map((t) => ({ label: RUN_LABEL[t] || t, unit: "/km", data: ps.map((p) => (p.type === t ? p.pace_s : null)),
        color: t === "long" ? "--cat-5" : `--z${runZone(t)}`, points: true, width: 0, spanGaps: false, endDot: false, fmt: (v) => paceStr(v) })),
      tooltipExtra: (i) => (ps[i] ? `${ps[i].km} km` : ""),
      onPoint: (i) => ps[i] && openRunByDate(ps[i].date),
    });

    // ---- cadence trend ----
    const cs = A.cadence_series || [];
    uiChart("chCadence", {
      title: "Cadence", takeaway: "Steps per minute per run. Around 175+ is efficient for you.",
      labels: cs.map((p) => p.date), xFormat: dLbl, height: 240, empty: "No cadence data yet",
      y: { suggestedMin: 150, suggestedMax: 185, fmt: (v) => `${v}` },
      series: [{ label: "Cadence", unit: "spm", data: cs.map((p) => p.cadence), color: "--cat-3", points: true, fmt: (v) => `${v}` }],
      targets: [{ value: 175, label: "175 target" }],
    });

    // ---- recent runs table (select a run for the breakdown) ----
    $("#actLog").innerHTML = UI.table({
      caption: "Recent runs", rowHeaders: true,
      cols: [
        { key: "date", label: "Run", fmt: (v, r) => `<button class="ax-runlink" type="button" data-run="${A.runs.indexOf(r)}">${escapeHtml(shortDate(v))}</button>` },
        { key: "type", label: "Type", fmt: (v) => `<span class="ui-zone-chip" style="--zc:var(--${v === "long" ? "cat-5" : "z" + runZone(v)})"><i aria-hidden="true"></i>${escapeHtml(RUN_LABEL[v] || v || "Run")}</span>` },
        { key: "distance_km", label: "Distance", align: "r", num: true, fmt: (v) => (v != null ? `${v.toFixed(1)} km` : "–") },
        { key: "pace", label: "Pace", align: "r", num: true, fmt: (v) => (v ? `${escapeHtml(v)}/km` : "–") },
        { key: "avg_hr", label: "Avg HR", align: "r", num: true, fmt: (v) => (v ?? "–") },
        { key: "cadence", label: "Cadence", align: "r", num: true, fmt: (v) => (v ?? "–") },
        { key: "load", label: "Load", align: "r", num: true, fmt: (v) => (v ?? "–") },
      ],
      rows: A.runs,
    });
    $$("#actLog [data-run]").forEach((b) => (b.onclick = () => openRun(A.runs[+b.dataset.run])));
    // the whole row is a mouse target too; the button is the keyboard/screen-reader target
    $$("#actLog tbody tr").forEach((tr) => tr.addEventListener("click", (e) => { if (!e.target.closest("button")) tr.querySelector("[data-run]")?.click(); }));

    // ---- HR zones ----
    const zt = sn.hr_zone_totals_s || {};
    const zsum = ["z1", "z2", "z3", "z4", "z5"].reduce((a, k) => a + (zt[k] || 0), 0);
    $("#chZones").innerHTML = zsum > 0
      ? UI.zoneBreakdown([1, 2, 3, 4, 5].map((z) => ({ zone: z, seconds: zt["z" + z] || 0 })))
      : UI.emptyState({ icon: "heart-pulse", compact: true, title: "No heart-rate zone minutes synced yet" });

    renderDynamics(A.dynamics);
    renderZoneIntensityReads(zt, zsum, wv);
  }

  // ---- 14-day run strip: daily km as bars, colored by intensity; select a bar for the run ----
  function _renderActivityStrip(A, sn) {
    const mount = $("#axStripCard"); if (!mount) return;
    const today = new Date(((sn.today) || iso(todayDate())) + "T00:00:00");
    const byDate = {};
    (A.runs || []).forEach((r) => { const cur = byDate[r.date]; if (!cur || (r.distance_km || 0) > (cur.distance_km || 0)) byDate[r.date] = r; });
    const dates = [];
    for (let i = 13; i >= 0; i--) { const d = new Date(today); d.setDate(today.getDate() - i); dates.push(iso(d)); }
    const runs = dates.map((ds) => byDate[ds] || null);
    const total = runs.reduce((a, r) => a + (r ? r.distance_km || 0 : 0), 0);
    const n = runs.filter(Boolean).length;
    const typesPresent = [...new Set(runs.filter(Boolean).map((r) => r.type || "easy"))];
    uiChart(mount, {
      title: "Last 14 days", takeaway: `${n} runs, ${total.toFixed(1)} km. Bar height is distance, color is intensity. Select a bar for that run.`,
      labels: dates, xFormat: (l, i, w) => (w === "tick" ? UI.fmt.date(l, { weekday: "narrow" }) + " " + new Date(l + "T00:00:00").getDate() : dLbl(l, i, w)),
      height: 220, y: { beginAtZero: true, fmt: (v) => `${v}` },
      series: [{ label: "Distance", unit: "km", type: "bar", legend: false, data: runs.map((r) => (r ? +(r.distance_km || 0).toFixed(1) : null)),
        colorBy: (v, i) => { const t = runs[i] && runs[i].type; return t === "long" ? "--cat-5" : `--z${runZone(t)}`; }, fmt: (v) => v.toFixed(1) }],
      legendExtra: typesPresent.map((t) => `<i class="bar" style="--sw:var(--${t === "long" ? "cat-5" : "z" + runZone(t)})"></i>${escapeHtml(RUN_LABEL[t] || t)}`),
      tooltipExtra: (i) => { const r = runs[i]; return r ? [RUN_LABEL[r.type] || r.type, r.pace ? `${r.pace}/km` : "", r.avg_hr ? `${r.avg_hr} bpm` : ""].filter(Boolean).join(" · ") : "Rest day"; },
      today: dates.length - 1,
      onPoint: (i) => runs[i] && openRun(runs[i]),
    });
  }

  // ---- this-week card ----
  function _renderActivityThisWeek(A, sn) {
    const el = $("#axThisWeek"); if (!el) return;
    const wv = sn.weekly_volume || [];
    const wk = wv[wv.length - 1] || {};
    const target = A.target_km || 50;
    const pct = target > 0 ? Math.round(((wk.km || 0) / target) * 100) : 0;
    const wkStart = wk.week_start ? new Date(wk.week_start + "T00:00:00") : null;
    const todayIso = iso(new Date(((sn.today) || iso(todayDate())) + "T00:00:00"));
    const byDate = {};
    (A.runs || []).forEach((r) => { byDate[r.date] = (byDate[r.date] || 0) + (r.distance_km || 0); });
    let days = "";
    if (wkStart) for (let i = 0; i < 7; i++) {
      const d = new Date(wkStart); d.setDate(wkStart.getDate() + i);
      const ds = iso(d), km = byDate[ds];
      days += `<span class="${km ? "is-done" : ""}${ds === todayIso ? " is-today" : ""}" aria-label="${UI.fmt.date(ds, { weekday: "long" })}: ${km ? km.toFixed(1) + " km" : "no run"}"><b>${km ? km.toFixed(1) : "–"}</b>${UI.fmt.date(ds, { weekday: "narrow" })}</span>`;
    }
    const tone = pct >= 90 ? "good" : pct >= 60 ? "warn" : "neutral";
    el.innerHTML = `<div class="ui-card-head"><div><h2 class="ui-card-title">This week</h2><p class="ui-card-sub">${wk.moderate_min || 0} moderate · ${wk.vigorous_min || 0} vigorous minutes · load ${wk.load ?? "–"}</p></div>${UI.badge(`${pct}% of target`, { tone, size: "sm" })}</div>
      <div class="ui-stat-value">${(wk.km || 0).toFixed(1)}<small>/ ${target} km</small></div>
      <span class="app-meter" style="--mc:var(--load-optimal)" role="img" aria-label="${pct}% of the ${target} km weekly target"><i style="width:${Math.min(100, pct)}%"></i></span>
      <div class="ax-days">${days}</div>`;
  }

  // ---- running dynamics (form): Garmin's bands shaded behind each metric ----
  // Band colors: purple/blue/green are all "good or better", amber and red are warnings.
  const DYN_BAND = { purple: ["--good", 0.06], blue: ["--good", 0.06], green: ["--good", 0.06], amber: ["--warn", 0.12], red: ["--bad", 0.12] };
  const DYN_YRANGE = { cadence: [150, 190], vo: [5, 13], vratio: [5, 11], gct: [190, 320] };
  function dynMini(metric, trend, def, mt) {
    const id = "chDyn" + metric;
    const vals = trend.map((p) => p[metric]);
    const [ymin, ymax] = DYN_YRANGE[metric];
    uiChart(id, {
      title: def.name, takeaway: mt ? `Latest ${mt.val} ${mt.unit}${mt.band ? `: ${mt.band.label}` : ""}. Shading shows Garmin's bands (green is good, amber/red is a flag).` : "",
      labels: trend.map((p) => p.date), xFormat: dLbl, height: 150, legend: false,
      y: { min: ymin, max: ymax, ticks: 4 }, empty: "No data",
      yBands: (def.bands || []).map(([lo, hi, color]) => { const [c, a] = DYN_BAND[color] || ["--text-3", 0.06]; return { from: Math.max(lo, ymin), to: Math.min(hi, ymax), color: c, alpha: a }; }),
      series: [{ label: def.name, unit: def.unit, data: vals, color: "--cat-1", points: true, fmt: (v) => `${v}` }],
    });
  }
  function dynBandOf(metric, val, def) {
    if (val == null) return null;
    for (const [lo, hi, color] of def.bands) if (val >= lo && val < hi) return { color };
    return null;
  }
  function dynLabel(color, better) {
    return { purple: "elite", blue: "great", green: "good",
      amber: better === "high" ? "low" : "high", red: better === "high" ? "very low" : "very high" }[color] || "";
  }
  function dynTone(color) { return (color === "purple" || color === "blue" || color === "green") ? "good" : UI.tone(color); }
  function runFormHtml(r) {
    const bands = S.activity && S.activity.dynamics && S.activity.dynamics.bands;
    const items = [["cadence", "Cadence", r.cadence, "spm"], ["vo", "Vertical oscillation", r.vo, "cm"],
      ["vratio", "Vertical ratio", r.vratio, "%"], ["gct", "Ground contact", r.gct, "ms"]].filter((x) => x[2] != null);
    if (!items.length) return "";
    const tiles = items.map(([m, lbl, val, unit]) => {
      const b = bands && bands[m] ? dynBandOf(m, val, bands[m]) : null;
      return UI.statTile({ label: lbl, value: val, unit, inset: true, foot: b ? UI.badge(dynLabel(b.color, bands[m].better), { tone: dynTone(b.color), size: "sm" }) : "" });
    });
    if (r.gct_balance != null) {
      const right = Math.round((100 - r.gct_balance) * 10) / 10, dev = Math.round(Math.abs(r.gct_balance - 50) * 10) / 10;
      const tone = dev <= 0.7 ? "good" : dev <= 1.5 ? "warn" : "bad";
      tiles.push(UI.statTile({ label: "L/R balance", value: `${r.gct_balance}/${right}`, unit: "%", inset: true, foot: UI.badge(dev <= 0.7 ? "balanced" : `${dev.toFixed(1)}% off`, { tone, size: "sm" }) }));
    }
    return `<div class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">Running form</h3><span class="ui-caption">Against Garmin's bands</span></div><div class="ui-dd-stats">${tiles.join("")}</div></div>`;
  }
  function renderDynamics(d) {
    if (!d || !d.metrics) {
      ["cadence", "vo", "vratio", "gct"].forEach((m) => { const n = $("#chDyn" + m); if (n) n.innerHTML = ""; });
      $("#formSummaryDyn").textContent = "No running-dynamics data. It needs a watch or strap that records it.";
      $("#formReads").innerHTML = ""; $("#gctBalance").innerHTML = "";
      return;
    }
    $("#formSummaryDyn").textContent = d.summary || "Your running dynamics against Garmin's bands.";
    ["cadence", "vo", "vratio", "gct"].forEach((m) => dynMini(m, d.trend, d.bands[m], d.metrics[m]));
    renderGctBalance(d.balance);
    $("#formReads").innerHTML = (d.reads || []).length
      ? UI.insightList((d.reads || []).map((r) => ({ tone: dynTone(r.color), title: `${r.metric} is ${r.band}`, how: r.tip })))
      : "";
  }
  // ---- Ground-contact L/R balance (chest-strap only: HRM-Tri/Pro/Run) ----
  function renderGctBalance(b) {
    const host = $("#gctBalance");
    if (!host) return;
    if (!b) { host.innerHTML = ""; return; }
    const tone = dynTone(b.color);
    // marker position: 0% = 47.5 (left-heavy), 100% = 52.5, centre 50
    const clamp = (v) => Math.max(47.5, Math.min(52.5, v));
    const pos = ((clamp(b.left) - 47.5) / 5) * 100;
    host.innerHTML = `<div class="ui-well ui-stack" style="gap:var(--sp-4)" data-tone="${tone}">
      <div class="ui-row" style="justify-content:space-between"><h3 class="ui-h3">Ground-contact balance</h3><span class="ui-caption">Left/right symmetry from your chest strap</span></div>
      <div class="ax-bal">
        <div class="ax-bal-side"><b>${b.left}%</b><span class="ui-caption">Left</span></div>
        <div><div class="ax-bal-track" role="img" aria-label="Left ${b.left}%, right ${b.right}%"><i style="left:${pos}%"></i></div>
          <div class="ax-bal-scale"><span>47.5</span><span>50 · even</span><span>52.5</span></div></div>
        <div class="ax-bal-side"><b>${b.right}%</b><span class="ui-caption">Right</span></div>
      </div>
      ${UI.insightCallout({ variant: "inline", tone, label: b.quality, title: b.dev <= 0.7 ? `Within Garmin's balanced band, ${b.dev}% off even.` : `Contact leans ${b.heavier} by ${b.dev}%.`, body: b.tip })}
    </div>`;
  }

  // ---- HR-zone + intensity interpretation (what it means, what to do, goal fit) ----
  function renderZoneIntensityReads(zt, zsum, wv) {
    const zr = $("#zoneRead");
    if (zr) {
      if (!zsum) { zr.innerHTML = ""; }
      else {
        const pct = (k) => Math.round(100 * (zt[k] || 0) / zsum);
        const easy = pct("z1") + pct("z2"), hard = pct("z3") + pct("z4") + pct("z5");
        let tone, msg, tip;
        if (easy >= 78) { tone = "good"; msg = `${easy}% of your time is easy (Z1–Z2), right on the 80/20 model that grows a big aerobic base.`; tip = "Keep protecting easy days; the engine grows fastest when most volume is genuinely easy."; }
        else if (easy >= 65) { tone = "warn"; msg = `${easy}% easy, ${hard}% moderate to hard: a touch hot. Strong endurance runners hold about 80% easy.`; tip = "Slow the easy runs (conversational, nose-breathing) and concentrate intensity into your two quality days."; }
        else { tone = "bad"; msg = `Only ${easy}% of your time is easy (${hard}% moderate to hard). You're living in the grey zone.`; tip = "This caps aerobic development and piles on fatigue. Make easy days truly easy so the hard days can be hard."; }
        zr.innerHTML = UI.insightCallout({ tone, label: "What it means", title: msg, body: `${tip} For your goal, a deep aerobic base carries race pace late, and this split ${easy >= 74 ? "supports" : "is working against"} it.` });
      }
    }
    const ir = $("#intensityRead");
    if (ir) {
      const recent = (wv || []).slice(-4);
      const mod = recent.reduce((a, w) => a + (w.moderate_min || 0), 0), vig = recent.reduce((a, w) => a + (w.vigorous_min || 0), 0);
      const perWk = recent.length ? Math.round((mod + vig) / recent.length) : 0;
      if (!recent.length) { ir.innerHTML = ""; }
      else {
        const meets = mod + 2 * vig >= 150 * recent.length;
        ir.innerHTML = UI.insightCallout({ variant: "inline", tone: meets ? "good" : "info", label: "Intensity minutes",
          title: `About ${perWk} intensity minutes a week (${Math.round(mod / recent.length)} moderate, ${Math.round(vig / recent.length)} vigorous).`,
          body: meets ? "Comfortably past the 150-minute weekly guideline. For performance, what matters more is that vigorous minutes land in two focused quality sessions, not scattered."
            : "Below the 150-minute guideline some weeks, which is fine in a base phase if easy volume is high. Vigorous minutes should come from your prescribed quality days." });
      }
    }
  }

  function openRunByDate(date) { const r = (S.activity && S.activity.runs || []).find((x) => x.date === date); if (r) openRun(r); }

  // Run deep dive, in a sheet. Reading order: what it was → the coach's read →
  // the evidence (pace, heart rate, splits) → the body's side (zones, form, effort).
  function openRun(r) {
    const body = daySheet(r.name || "Run", `${UI.fmt.date(r.date, { weekday: "long", month: "long", day: "numeric" })}${r.duration ? ` · ${r.duration}` : ""}`);
    const z = r.zone_pct || {};
    const zoneNames = ["Recovery", "Easy", "Steady", "Threshold", "VO₂max"];
    const zones = [1, 2, 3, 4, 5].map((n) => ({ zone: n, value: z["z" + n] || 0 }));
    const zoneHtml = zones.some((p) => p.value) ? `<div class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">Time in heart-rate zone</h3></div>
      ${UI.zoneBar(zones, "Time in heart-rate zone")}
      <div class="ui-cal-legend">${zones.filter((p) => p.value).map((p) => `<span>${UI.zoneChip(p.zone, zoneNames[p.zone - 1])} <b class="num">${p.value}%</b></span>`).join("")}</div></div>` : "";
    const stat = (label, value, unit) => (value == null || value === "" || value === "–" ? "" : UI.statTile({ label, value, unit, inset: true }));
    const effort = r.effort != null ? `<div class="ui-dd-sec">${effortBlendHtml(r.effort, r.blend, r.gap_pace, r.pace)}
      ${r.type === "quality" && r.effort < 0.5 ? UI.insightCallout({ variant: "inline", tone: "info", label: "Why it's still a workout", body: "This score is a **whole-run average**: warm-up, recovery jogs and cool-down pull it down. The session was classed a workout on its training effect, max HR and hard-zone time. The reps stand out in the pace trace." }) : ""}</div>` : "";
    body.innerHTML = `<article class="ui-dd">
      <header class="ui-dd-head">
        <div class="ui-dd-kicker"><span class="ui-zone-chip" style="--zc:var(--${r.type === "long" ? "cat-5" : "z" + runZone(r.type)})"><i aria-hidden="true"></i>${escapeHtml(RUN_LABEL[r.type] || r.type || "Run")}</span>
          <span class="ui-caption">${r.distance_km != null ? `${r.distance_km.toFixed(1)} km` : ""}${r.duration ? ` · ${escapeHtml(r.duration)}` : ""}</span></div>
        <div class="ui-dd-stats">
          ${stat("Pace", r.pace, "/km")}${stat("Grade-adj pace", r.gap_pace, "/km")}${stat("Avg HR", r.avg_hr, "bpm")}${stat("Max HR", r.max_hr, "bpm")}
          ${stat("Power", r.power, "W")}${stat("Cadence", r.cadence, "spm")}${stat("Load", r.load)}
          ${stat("Aerobic TE", r.aerobic_te ? r.aerobic_te.toFixed(1) : null)}${stat("Anaerobic TE", r.anaerobic_te ? r.anaerobic_te.toFixed(1) : null)}${stat("Elevation gain", r.elev, "m")}
        </div>
      </header>
      ${r.note ? UI.insightCallout({ tone: "info", label: "Note", body: r.note }) : ""}
      <div id="runInsights"></div>
      <div class="ui-dd-cols">
        <div class="ui-stack" style="gap:var(--sp-7)">
          ${r.id ? `<div class="rs-charts" id="runTrace">${UI.skeleton.chart(200)}</div>` : ""}
          <div class="ui-dd-sec" id="splitsSec" hidden><div class="ui-dd-sec-h"><h3 class="ui-h3">Splits</h3><span class="ui-caption">Hover a lap to find it on the pace chart</span></div><div id="runSplits"></div></div>
        </div>
        <div class="ui-stack" style="gap:var(--sp-7)">${zoneHtml}${runFormHtml(r)}${effort}<div id="runImpact"></div></div>
      </div>
    </article>`;
    if (r.id) renderRunTrace(r.id, $("#runTrace"), $("#runInsights"));
  }

  // Pace + heart-rate traces for one run (two charts, never a dual axis), the
  // splits table, impact load, and the run's coaching insights — fetched on demand.
  async function renderRunTrace(activityId, mount, insightsMount, { compact = false } = {}) {
    if (!activityId || !mount) return;
    let s;
    try { s = await api(`/api/activity/${activityId}/stream`); }
    catch (e) { mount.innerHTML = UI.errorState({ compact: true, title: "Couldn't load this run's trace", retry: false }); return; }
    if (!s || !s.has_data || !(s.series || []).length) {
      mount.innerHTML = UI.emptyState({ icon: "activity", compact: true, title: "No time series recorded for this run" });
    } else {
      // Downsample to ≤ 400 points: plenty for the eye, cheap for the canvas.
      const all = s.series, step = Math.max(1, Math.ceil(all.length / 400));
      const pts = all.filter((_, i) => i % step === 0 || i === all.length - 1);
      const ts = pts.map((p) => p.t);
      const nearest = (t) => { let best = 0; ts.forEach((v, i) => { if (Math.abs(v - t) < Math.abs(ts[best] - t)) best = i; }); return best; };
      // Keep walking breaks / GPS spikes from flattening the trace.
      const paces = pts.map((p) => p.pace_s).filter((v) => v != null).sort((a, b) => a - b);
      const q = (f) => paces[Math.min(paces.length - 1, Math.floor(f * paces.length))];
      const work = (s.splits || []).filter((l) => ["active", "interval"].includes(l.intensity));
      const paceId = `${mount.id || "trace"}-pace`, hrId = `${mount.id || "trace"}-hr`;
      mount.innerHTML = `<div id="${paceId}"></div>${pts.some((p) => p.hr) ? `<div id="${hrId}"></div>` : ""}`;
      uiChart(paceId, {
        title: "Pace", takeaway: work.length ? `${work.length} work lap${work.length > 1 ? "s" : ""} shaded.` : "Raw pace and grade-adjusted pace over the run.",
        labels: ts, xLabel: "Elapsed", xFormat: (t) => clock(t), height: compact ? 160 : 200,
        y: { reverse: true, min: paces.length ? Math.floor((q(0.03) - 10) / 15) * 15 : undefined, max: paces.length ? Math.ceil((q(0.97) + 20) / 15) * 15 : undefined, step: 30, fmt: (v) => paceStr(v), hint: "Faster ↑" },
        series: [
          { label: "Pace", unit: "/km", data: pts.map((p) => p.pace_s ?? null), color: "--cat-1", endDot: false, fmt: (v) => paceStr(v) },
          { label: "Grade-adjusted", unit: "/km", data: pts.map((p) => p.gap_s ?? null), color: "--cat-2", width: 1.5, endDot: false, fmt: (v) => paceStr(v) },
        ],
        ranges: work.map((l, i) => ({ from: nearest(l.t0), to: nearest(l.t1), edges: "point", label: work.length <= 6 ? `Rep ${i + 1}` : "" })),
      });
      if (pts.some((p) => p.hr)) {
        const hz = ((S.dash && S.dash.config && S.dash.config.hr_zones) || {}).zones || [];
        const hrs = pts.map((p) => p.hr).filter(Boolean);
        const lo = Math.min(...hrs) - 5, hi = Math.max(...hrs) + 5;
        uiChart(hrId, {
          title: "Heart rate", takeaway: "Shaded bands are your heart-rate zones.",
          labels: ts, xLabel: "Elapsed", xFormat: (t) => clock(t), height: compact ? 140 : 170,
          y: { min: Math.floor(lo / 5) * 5, max: Math.ceil(hi / 5) * 5, fmt: (v) => `${v}` },
          yBands: hz.filter((b) => b.high >= lo && b.low <= hi).map((b) => ({ from: b.low, to: b.high, color: `--z${b.zone}`, alpha: 0.12 })),
          series: [{ label: "Heart rate", unit: "bpm", data: pts.map((p) => p.hr ?? null), color: "--cat-6", endDot: false, fmt: (v) => `${v}` }],
          legendExtra: hz.map((b) => `<i class="bar" style="--sw:var(--z${b.zone})"></i>Z${b.zone}`),
        });
      }
      renderSplits(s.splits, paceId, nearest);
    }
    renderImpact(s && s.impact);
    // Coaching insights for the run; fall back to the aerobic-decoupling read.
    if (insightsMount && s) {
      if (s.insights && s.insights.length) insightsMount.innerHTML = UI.card({ title: "Coach's read", icon: "sparkles", body: UI.insightList(s.insights) });
      else if (s.drift) {
        const d = s.drift, workScope = d.scope === "work";
        insightsMount.innerHTML = UI.insightCallout({ tone: d.tone, label: workScope ? "Holding the effort" : "Aerobic durability",
          title: `${d.verdict} (${d.pct > 0 ? "+" : ""}${d.pct}% decoupling)`,
          body: `Pace held ${paceStr(d.pace_first)} → ${paceStr(d.pace_second)}/km while heart rate ${d.hr_rise >= 0 ? "rose" : "eased"} ${Math.abs(d.hr_rise)} bpm (${d.hr_first} → ${d.hr_second}) ${workScope ? "from the first reps to the last" : "across the two halves"}. ${driftMsg(d)}${d.strides_excluded ? " Finishing strides are excluded, so this reads the steady running only." : ""}` });
      } else insightsMount.innerHTML = "";
    }
  }
  // Per-lap splits; hovering or focusing a row shades that lap on the pace chart.
  const SP_TONE = { active: "warn", interval: "warn", warmup: "neutral", recovery: "good", rest: "good", cooldown: "neutral" };
  function renderSplits(splits, chartId, nearest) {
    const mount = document.getElementById("runSplits"), sec = document.getElementById("splitsSec");
    if (!mount || !sec) return;
    if (!splits || !splits.length) { sec.hidden = true; return; }
    sec.hidden = false;
    const fastest = Math.min(...splits.map((l) => l.pace_s || Infinity));
    mount.innerHTML = UI.table({
      caption: "Splits", dense: true, rowHeaders: true,
      rowAttrs: (l) => ({ "data-t0": l.t0, "data-t1": l.t1, tabindex: 0 }),
      cols: [
        { key: "lap", label: "Lap", num: true },
        { key: "distance_km", label: "Distance", align: "r", num: true, fmt: (v) => `${v.toFixed(2)} km` },
        { key: "pace_s", label: "Pace", align: "r", num: true, fmt: (v) => (v ? `<b>${paceStr(v)}</b>` : "–") },
        { key: "gap_s", label: "Grade-adj", align: "r", num: true, fmt: (v) => (v ? paceStr(v) : "–") },
        { key: "elev_net", label: "Elevation", align: "r", num: true, fmt: (v) => (v ? `${UI.fmt.signed(v)} m` : "0 m") },
        { key: "avg_hr", label: "Avg HR", align: "r", num: true, fmt: (v) => (v ?? "–") },
        { key: "intensity", label: "Type", fmt: (v, l) => `${v ? UI.badge(v, { size: "sm", tone: SP_TONE[v] || "neutral" }) : ""}${l.pace_s === fastest && splits.length > 2 ? ` ${UI.badge("Fastest", { size: "sm", tone: "good", icon: "trophy" })}` : ""}` },
      ],
      rows: splits,
    });
    const ctl = S.ui[chartId];
    const on = (tr) => {
      const c = ctl && ctl.chart; if (!c || !nearest) return;
      c.$ui.ranges = (c.$ui.ranges || []).filter((r) => !r.$hl).concat([{ $hl: true, from: nearest(+tr.dataset.t0), to: nearest(+tr.dataset.t1), edges: "point", color: "--viz-crosshair" }]);
      c.update("none");
    };
    const off = () => { const c = ctl && ctl.chart; if (!c) return; c.$ui.ranges = (c.$ui.ranges || []).filter((r) => !r.$hl); c.update("none"); };
    mount.querySelectorAll("tr[data-t0]").forEach((tr) => {
      tr.addEventListener("mouseenter", () => on(tr)); tr.addEventListener("focus", () => on(tr));
      tr.addEventListener("mouseleave", off); tr.addEventListener("blur", off);
    });
  }
  // Per-run Impact Load (newer-Garmin stream metric).
  function renderImpact(imp) {
    const mount = document.getElementById("runImpact");
    if (!mount) return;
    if (!imp || imp.avg == null) { mount.innerHTML = ""; return; }
    mount.innerHTML = `<div class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">Impact load</h3>${UI.badge("Newer watches", { size: "sm", tone: "info" })}</div>
      <div class="ui-dd-stats">${UI.statTile({ label: "Average per stride", value: imp.avg, inset: true })}${UI.statTile({ label: "Peak", value: imp.peak, inset: true })}</div>
      <p class="ui-caption">Per-stride loading, higher on speed and downhills. It feeds your weekly running tolerance on the Fitness page.</p></div>`;
  }
  function driftMsg(d) {
    const work = d.scope === "work";
    if (d.tone === "green") return work ? "HR stayed locked to pace through the effort — strong durability at this intensity." : "HR stayed coupled to pace — you held aerobic form to the end.";
    if (d.tone === "amber") return work ? "A little late HR climb at the same pace — normal at threshold; it shrinks as the engine strengthens." : "Your aerobic endurance was being stretched late; a bit more easy volume tightens this up.";
    return work ? "HR spiked while pace held (or slipped) late in the effort — your aerobic ceiling showing. More threshold volume and long-run strength push it out." : "Classic aerobic-endurance limiter — the engine faded while the legs held pace. This is exactly what easy long-run volume fixes.";
  }

  // Design-system head chips for Fitness: VDOT, CTL (fitness), TSB (form),
  // race prediction. Mirrors the Coach/Sleep/Activity/Recovery head.
  // Page head: the four numbers worth seeing first.
  function _renderFitnessHead(F) {
    const el = $("#fitHeadStats"); if (!el) return;
    if (!F) { el.innerHTML = ""; return; }
    const cur = (F.form && F.form.current) || {};
    const form = cur.form;
    const vdot = (F.race && (F.race.current_vdot || F.race.vdot_current)) || (F.vdot && F.vdot.current) || F.vdot_current;
    const half = F.race && F.race.current_half;
    const chips = [];
    if (vdot != null) chips.push(UI.badge(`VDOT ${vdot}`, { outline: true }));
    if (cur.fitness != null) chips.push(UI.badge(`Fitness ${cur.fitness}`, { outline: true }));
    if (form != null) chips.push(UI.badge(`Form ${UI.fmt.signed(form)}`, { outline: true, tone: form <= -20 ? "bad" : form <= -10 ? "warn" : form >= 15 ? "good" : null }));
    if (half) chips.push(UI.badge(`${raceNoun() === "marathon" ? "Marathon" : "Half"} ~${half}`, { outline: true, tone: "coach", icon: "flag" }));
    el.innerHTML = chips.join("");
  }

  function renderFitness() {
    const F = S.fitness;
    _renderFitnessHead(F);
    if (!F || !F.has_data) {
      const msg = (F && F.message) || "No fitness data synced yet.";
      $("#formSummary").innerHTML = UI.emptyState({ icon: "trending-up", title: "No fitness data yet", body: msg });
      ["chForm", "chFormTsb", "formNote", "chVdot", "vdotBody", "pathBody", "raceBody", "chAcwr", "acwrSummary", "vo2Body", "chVo2", "statusBody", "chStatus", "thresholdBody", "weightBody", "chWeight"]
        .forEach((id) => { const n = $("#" + id); if (n) n.innerHTML = ""; });
      return;
    }
    renderForm(F.form);
    renderPath(F.race);
    renderVdot(F.race);
    renderVo2(F.vo2);
    renderRace(F.race);
    renderAcwr(F.acwr);
    renderStatusCard(F.status, F.vo2);
    renderThreshold(F.lactate_threshold);
    renderWeight(F.weight);
    loadStamina();
    loadHeat();
    loadTolerance();
  }

  // Running Tolerance — impact load against capacity (lazy).
  async function loadTolerance() {
    if (S.tolerance) { renderTolerance(S.tolerance); return; }
    if ($("#tolBody")) $("#tolBody").innerHTML = UI.skeleton.lines(3);
    try { S.tolerance = await api("/api/fitness/tolerance"); renderTolerance(S.tolerance); }
    catch (e) { if ($("#tolBody")) $("#tolBody").innerHTML = UI.errorState({ compact: true, title: "Couldn't load running tolerance", retry: false }); }
  }
  function renderTolerance(d) {
    const card = $("#tolCard"), body = $("#tolBody");
    if (!body) return;
    if (!d || !d.has_data) {
      body.innerHTML = UI.emptyState({ icon: "gauge", compact: true, title: "No tolerance data yet", body: "Running Tolerance needs a recent Garmin watch that records impact load." });
      return;
    }
    if (card) card.hidden = false;
    const c = d.current, pct = Math.round(100 * (c.ratio || 0));
    const tone = UI.tone(d.tone);
    const mc = { good: "--load-optimal", warn: "--load-high", bad: "--load-over" }[tone] || "--cat-1";
    body.innerHTML = `
      <div class="ui-row" style="justify-content:space-between">
        <span class="ui-stat-value">${c.impact_load.toLocaleString()}<small>/ ${c.tolerance.toLocaleString()} tolerance</small></span>
        ${UI.badge(d.verdict, { tone, autoIcon: true })}
      </div>
      <span class="app-meter" style="--mc:var(${mc})" role="img" aria-label="${pct}% of tolerance"><i style="width:${Math.min(100, pct)}%"></i></span>
      <div class="fx-scale"><span>0</span><span>${pct}% of tolerance${pct > 100 ? " (over)" : ""}</span><span>100%</span></div>
      ${UI.insightCallout({ variant: "inline", tone, label: false, title: d.read,
        body: [d.this_week ? `This week so far: ${d.this_week.impact_load.toLocaleString()} load over ${d.this_week.distance_km} km (${Math.round(100 * (d.this_week.ratio || 0))}% of tolerance).` : "", d.note || ""].filter(Boolean).join(" ") })}`;
  }

  // Heat acclimation — sampled weekly from Garmin training status (lazy).
  async function loadHeat() {
    if (S.heat) { renderHeat(S.heat); return; }
    if ($("#heatBody")) $("#heatBody").innerHTML = UI.skeleton.lines(2);
    try { S.heat = await api("/api/fitness/heat"); renderHeat(S.heat); }
    catch (e) { if ($("#heatBody")) $("#heatBody").innerHTML = UI.errorState({ compact: true, title: "Couldn't load heat acclimation", retry: false }); }
  }
  function renderHeat(d) {
    const body = $("#heatBody");
    if (!body) return;
    if (!d || !d.has_data) {
      body.innerHTML = UI.emptyState({ icon: "thermometer-sun", compact: true, title: "No heat acclimation recorded", body: "Garmin tracks this once you train in the heat. It stays at 0% in cool conditions." });
      $("#chHeat").innerHTML = "";
      return;
    }
    const cur = d.current || {}, series = d.series || [];
    const trend = (cur.trend || "").toLowerCase().replace("_", " ");
    body.innerHTML = `<div class="ui-row"><span class="ui-stat-value">${cur.heat_pct}<small>%</small></span>${trend ? UI.badge(trend, { size: "sm", tone: cur.heat_pct >= 40 ? "good" : cur.heat_pct > 0 ? "warn" : "neutral" }) : ""}</div>
      <p class="ui-small ui-muted">Builds with repeated training in warm conditions and fades within two to four weeks without it. Higher means less pace and HR penalty on a hot race day.</p>`;
    uiChart("chHeat", {
      labels: series.map((p) => p.date), xFormat: dLbl, height: 160, legend: false, takeaway: "",
      y: { min: 0, max: 100, fmt: (v) => `${v}%` },
      series: [{ label: "Heat acclimation", unit: "%", type: "area", data: series.map((p) => p.heat_pct), color: "--cat-4", points: true, fmt: (v) => `${v}` }],
    });
  }

  // Garmin Real-Time Stamina — lazily fetches recent activity streams (heavier).
  async function loadStamina() {
    if (S.stamina) { renderStamina(S.stamina); return; }
    ["chStaminaCurve", "chStaminaTrend"].forEach((id) => { const n = $("#" + id); if (n) n.innerHTML = UI.skeleton.chart(160); });
    try {
      S.stamina = await api("/api/fitness/stamina");
      renderStamina(S.stamina);
    } catch (e) {
      $("#chStaminaCurve").innerHTML = UI.errorState({ compact: true, title: "Couldn't load stamina", retry: false });
      $("#chStaminaTrend").innerHTML = "";
    }
  }
  function renderStamina(d) {
    if (!d || !d.has_data) {
      $("#chStaminaCurve").innerHTML = UI.emptyState({ icon: "battery-medium", compact: true, title: "No real-time stamina on recent runs",
        body: "It needs a watch that records it (Fenix 7 and later, Forerunner 955 and later)." });
      $("#chStaminaTrend").innerHTML = "";
      return;
    }
    const cv = d.curve, T = d.trend || [];
    uiChart("chStaminaCurve", {
      title: `Last run · ${shortDate(cv.date)}`, takeaway: "Available stamina against your potential (ceiling) as the run went on.",
      labels: cv.series.map((p) => p.t), xLabel: "Elapsed", xFormat: (t) => `${Math.round(t / 60)} min`, height: 200,
      y: { min: 0, max: 100, fmt: (v) => `${v}%` },
      series: [
        { label: "Available", unit: "%", type: "area", data: cv.series.map((p) => p.stamina), color: "--cat-3", endDot: false, fmt: (v) => `${v}` },
        { label: "Potential (ceiling)", unit: "%", data: cv.series.map((p) => p.potential), color: "--cat-4", dash: true, endDot: false, fmt: (v) => `${v}` },
      ],
    });
    uiChart("chStaminaTrend", {
      title: "Left at the finish", takeaway: "Stamina remaining at the end of recent runs.",
      labels: T.map((t) => t.date), xFormat: dLbl, height: 200,
      y: { min: 0, max: 100, fmt: (v) => `${v}%` },
      series: [{ label: "Left at finish", unit: "%", type: "bar", legend: false, data: T.map((t) => t.end_available),
        colorBy: (v) => (v >= 70 ? "--load-optimal" : v >= 45 ? "--load-high" : "--load-over"), fmt: (v) => `${v}` }],
      tooltipExtra: (i) => (T[i] && T[i].distance_km ? `${T[i].distance_km} km` : ""),
      legendExtra: ['<i class="bar" style="--sw:var(--load-optimal)"></i>70%+', '<i class="bar" style="--sw:var(--load-high)"></i>45–69%', '<i class="bar" style="--sw:var(--load-over)"></i>Under 45%'],
    });
  }

  function renderWeight(w) {
    const body = $("#weightBody");
    if (!w) {
      body.innerHTML = UI.emptyState({ icon: "scale", compact: true, title: "No weight logged yet", body: "Sync a scale or MyFitnessPal to see the trend." });
      $("#chWeight").innerHTML = "";
      return;
    }
    const u = w.unit || "lb";
    const fc = w.forecast;
    body.innerHTML = `<div class="cx-stats">
        ${UI.statTile({ label: "Current", value: w.current, unit: u, inset: true })}
        ${w.change_30 != null ? UI.statTile({ label: "30-day change", value: UI.fmt.signed(w.change_30, 1), unit: u, inset: true }) : ""}
        ${w.change_90 != null ? UI.statTile({ label: "90-day change", value: UI.fmt.signed(w.change_90, 1), unit: u, inset: true }) : ""}
        ${w.w_per_kg ? UI.statTile({ label: "Power to weight", value: w.w_per_kg, unit: "W/kg", inset: true }) : ""}
        ${w.goal != null ? UI.statTile({ label: "Goal", value: w.goal, unit: u, inset: true, foot: fc ? `${fc.to_lose_lb} ${u} over ${fc.weeks} week${fc.weeks === 1 ? "" : "s"}` : "" }) : ""}
      </div>
      ${w.impact ? UI.insightCallout({ variant: "inline", tone: "info", label: "What it means for pace", body: w.impact }) : ""}
      ${fc && fc.method ? `<p class="ui-caption">${escapeHtml(fc.method)}</p>` : ""}`;

    // Merge weigh-ins and forecast onto one ISO-date axis.
    // Last 120 days only: the x-axis spaces points evenly, so a year of daily
    // weigh-ins beside a few weekly forecast points would exaggerate the slope.
    const cutoff = iso(new Date(todayDate().getTime() - 120 * 86400000));
    const t = (w.trend || []).filter((p) => p.date >= cutoff);
    const fcSeries = (fc && fc.series) || [];
    const byDate = new Map();
    t.forEach((p) => byDate.set(p.date, { a: p.val, f: null }));
    fcSeries.forEach((p) => { const r = byDate.get(p.date) || { a: null, f: null }; r.f = p.val; r.fp = p; byDate.set(p.date, r); });
    const labels = [...byDate.keys()].sort();
    if (labels.length < 2) { $("#chWeight").innerHTML = ""; return; }
    const rows = labels.map((d) => byDate.get(d));
    const lastActual = labels.reduce((m, d, i) => (rows[i].a != null ? i : m), -1);
    if (lastActual >= 0 && fcSeries.length && rows[lastActual].f == null) rows[lastActual].f = rows[lastActual].a;   // join the lines
    uiChart("chWeight", {
      labels, xFormat: dLbl, height: 220,
      y: { fmt: (v) => `${Math.round(v)}` },
      series: [
        { label: "Weigh-ins", unit: u, data: rows.map((r) => r.a), color: "--cat-3", points: true, spanGaps: true, fmt: (v) => v.toFixed(1) },
        ...(fcSeries.length ? [{ label: "Forecast", unit: u, data: rows.map((r) => r.f), color: "--cat-1", dash: true, spanGaps: true, hollowEnd: true, fmt: (v) => v.toFixed(1) }] : []),
      ],
      targets: w.goal != null ? [{ value: w.goal, label: `Goal ${w.goal}` }] : [],
      today: lastActual >= 0 && fcSeries.length ? lastActual : undefined,
      tooltipExtra: (i) => { const p = rows[i] && rows[i].fp; return p && p.km ? `${p.phase} · ${p.km} km · −${p.drop_lb} ${u}` : ""; },
    });
  }

  // Fitness / fatigue (lines, forecast dashed after Today) and form (bars by zone):
  // two aligned charts instead of one chart with two y-axes.
  function renderForm(form) {
    if (!form) {
      $("#formSummary").innerHTML = UI.emptyState({ icon: "trending-up", compact: true, title: "Not enough training-load history yet", body: "A couple of weeks of runs and this fills in." });
      ["chForm", "chFormTsb", "formNote"].forEach((id) => ($("#" + id).innerHTML = ""));
      return;
    }
    const c = form.current;
    const v = form.load_verdict || {};
    const trends = form.trends || {};
    const rate = trends.ctl_rate_per_week, ratio = trends.atl_ctl_ratio;
    $("#formSummary").innerHTML = `<div class="cx-stats">
        ${UI.statTile({ label: "Fitness (42-day load)", value: c.fitness, inset: true, foot: rate != null ? UI.badge(`${UI.fmt.signed(rate, 1)}/week`, { size: "sm", tone: rate >= 1 ? "good" : rate >= 0 ? "info" : "warn" }) : "" })}
        ${UI.statTile({ label: "Fatigue (7-day load)", value: c.fatigue, inset: true, foot: ratio != null ? UI.badge(`ratio ${ratio}`, { size: "sm", tone: ratio > 1.3 ? "bad" : ratio > 1.1 ? "warn" : "good" }) : "" })}
        ${UI.statTile({ label: "Form", value: UI.fmt.signed(c.form), inset: true, tone: UI.tone(form.form_color), foot: UI.badge(form.form_label, { size: "sm", tone: UI.tone(form.form_color) }) })}
      </div>
      ${v.message ? UI.insightCallout({ tone: UI.tone(v.tone === "note" ? "info" : v.tone), label: v.verdict || "Load", title: v.message }) : ""}`;

    const s = form.series, fcast = form.forecast || [];
    const labels = s.map((p) => p.date).concat(fcast.map((p) => p.date));
    const nActual = s.length;
    const pad = (n) => Array(n).fill(null);
    const ctlA = s.map((p) => p.ctl).concat(pad(fcast.length)), atlA = s.map((p) => p.atl).concat(pad(fcast.length));
    const ctlF = pad(nActual).concat(fcast.map((p) => p.ctl)), atlF = pad(nActual).concat(fcast.map((p) => p.atl));
    if (nActual && fcast.length) { ctlF[nActual - 1] = s[nActual - 1].ctl; atlF[nActual - 1] = s[nActual - 1].atl; }   // join at today
    const tsb = s.map((p) => p.tsb).concat(fcast.map((p) => p.tsb));
    const today = nActual ? nActual - 1 : undefined;
    uiChart("chForm", {
      labels, xFormat: dLbl, height: 240, takeaway: "",
      series: [
        { label: "Fitness (CTL)", data: ctlA, color: "--cat-1", endDot: false, fmt: (x) => x.toFixed(1) },
        { label: "Fatigue (ATL)", data: atlA, color: "--cat-2", width: 1.5, endDot: false, fmt: (x) => x.toFixed(1) },
        { label: "Fitness forecast", data: ctlF, color: "--cat-1", dash: true, legend: false, spanGaps: false, endDot: false, fmt: (x) => x.toFixed(1) },
        { label: "Fatigue forecast", data: atlF, color: "--cat-2", dash: true, width: 1.5, legend: false, spanGaps: false, endDot: false, fmt: (x) => x.toFixed(1) },
      ],
      legendExtra: ['<i class="dash" style="--sw:var(--text-2)"></i>Forecast'],
      today, table: false,
    });
    // Form zones: fresh > +5 · neutral −10…+5 · productive −30…−10 · overreached < −30
    const tsbColor = (x) => (x > 5 ? "--load-under" : x >= -10 ? "--text-3" : x >= -30 ? "--load-optimal" : "--load-over");
    uiChart("chFormTsb", {
      labels, xFormat: dLbl, height: 150, takeaway: "",
      y: { fmt: (x) => UI.fmt.signed(x) },
      series: [{ label: "Form (TSB)", type: "bar", legend: false, data: tsb, colorBy: (x, i) => (i > (today ?? 1e9) ? UI.alpha(tsbColor(x), 0.45) : tsbColor(x)), maxBar: 6, fmt: (x) => UI.fmt.signed(x, 1) }],
      legendExtra: [["--load-under", "Fresh"], ["--text-3", "Neutral"], ["--load-optimal", "Productive"], ["--load-over", "Overreached"]].map(([c, l]) => `<i class="bar" style="--sw:var(${c})"></i>${l}`),
      today, todayLabel: " ",
    });
    $("#formNote").innerHTML = `${form.form_read ? `<p class="ui-body">${mdLite(form.form_read)}</p>` : ""}
      ${(form.observations || []).length ? UI.insightList(form.observations.map((o) => ({ tone: o.tone === "warn" ? "warn" : o.tone === "good" ? "good" : "info", title: o.title, detail: o.detail }))) : ""}`;
  }

  function renderPath(race) {
    const pt = $("#pathTitle");
    if (pt) pt.textContent = `Path to ${goalTime()}`;
    if (!race || !race.milestones) { $("#pathBody").innerHTML = UI.emptyState({ icon: "route", compact: true, title: "No race prediction yet to build a path from" }); return; }
    const A = race.primary;
    const head = A ? UI.insightCallout({ variant: "inline", tone: "coach", label: "The gap",
      title: `${A.faster_pct}% faster (${A.sec_per_km_gain} s/km) gets you to ${goalTime()}.`,
      body: `From your predicted **${race.current_half}** that's about a **${A.vdot_gain_pct}%** lift in racing fitness (VDOT ${race.current_vdot} → ${A.vdot}). The plan's race-pace long-run blocks are built to close exactly this gap.` }) : "";
    const rows = race.milestones.map((m) => {
      const fill = Math.max(4, Math.min(100, Math.round(100 * race.current_vdot / m.vdot)));
      return `<div class="fx-ms${m.kind === "primary" ? " is-primary" : ""}">
        <span><b>${escapeHtml(m.label)}</b> <span class="ui-caption num">${escapeHtml(m.pace)}/km</span></span>
        <span class="need">${m.faster_pct}% faster · VDOT ${m.vdot}</span>
        <span class="app-meter" style="--mc:var(${m.kind === "primary" ? "--cat-1" : "--cat-2"})" role="img" aria-label="${fill}% of the racing fitness needed for ${escapeHtml(m.label)}"><i style="width:${fill}%"></i></span>
      </div>`;
    }).join("");
    $("#pathBody").innerHTML = `<div class="ui-stack">${head}<div class="fx-milestones">${rows}</div></div>`;
  }

  // VDOT: history solid, projection dashed with its likely range, goal and proven best as lines.
  function renderVdot(race) {
    if (!race || !race.vdot_trend || !race.vdot_trend.length) {
      $("#chVdot").innerHTML = "";
      $("#vdotBody").innerHTML = UI.emptyState({ icon: "trending-up", compact: true, title: "VDOT history is building", body: "It fills in as Garmin logs daily race predictions." });
      return;
    }
    const cur = race.current_vdot, tgt = race.target_vdot, st = race.start_vdot;
    const t = race.vdot_trend, pj = race.projection, proj = pj ? pj.series : [];
    const H = t.length;
    const labels = t.map((p) => p.date).concat(proj.map((p) => p.date));
    const pad = (n) => Array(n).fill(null);
    const join = (arr) => pad(H - 1).concat([t[H - 1].vdot], arr);
    const ceiling = race.proven_ceiling;
    uiChart("chVdot", {
      title: "Racing fitness (VDOT)", takeaway: pj ? `Projected ${pj.race_vdot} on race day, about ${pj.race_time} for the ${raceNoun()}.` : `Currently ${cur}.`,
      labels, xFormat: dLbl, height: 260,
      actual: t.map((p) => p.vdot).concat(pad(proj.length)),
      projected: proj.length ? join(proj.map((p) => p.vdot)) : pad(labels.length),
      p50: proj.length ? { lo: join(proj.map((p) => p.low)), hi: join(proj.map((p) => p.high)) } : null,
      actualLabel: "VDOT", projectedLabel: "Projected",
      today: H - 1,
      goal: tgt ? { value: tgt, label: `Goal ${tgt}` } : null,
      targets: ceiling ? [{ value: ceiling.vdot, label: `Best ${ceiling.vdot}` }] : [],
      y: { fmt: (v) => v.toFixed(1), suggestedMin: Math.min(...t.map((p) => p.vdot)) - 1, suggestedMax: Math.max(tgt || 0, ceiling ? ceiling.vdot : 0, pj ? pj.race_vdot_high : 0) + 1 },
      hero: pj ? { value: String(pj.race_vdot), sub: `race day · ${raceDateShort()}`, detail: `Likely ${pj.race_vdot_low}–${pj.race_vdot_high} · ~${pj.race_time}` } : { value: String(cur), sub: "current VDOT" },
      meaning: pj ? { tone: pj.hits_goal ? "good" : "warn", title: pj.hits_goal ? `On course for your ${pj.hits_goal} goal.` : `About ${pj.race_time} on race day at this trajectory.`, body: pj.note } : null,
    }, "forecast");
    const prog = tgt && st != null && tgt > st ? Math.max(0, Math.min(100, Math.round((100 * (cur - st)) / (tgt - st)))) : null;
    const projPct = pj && tgt && st != null && tgt > st ? Math.max(0, Math.min(100, (100 * (pj.race_vdot - st)) / (tgt - st))) : null;
    $("#vdotBody").innerHTML = `<div class="ui-stack" style="margin-top:var(--sp-5)">
      <div class="cx-stats">
        ${UI.statTile({ label: "Current", value: cur, inset: true })}
        ${UI.statTile({ label: `Needed for ${goalTime()}`, value: tgt ?? "–", inset: true })}
        ${race.vdot_change_90d != null ? UI.statTile({ label: "90-day change", value: UI.fmt.signed(race.vdot_change_90d, 1), inset: true }) : ""}
        ${race.vdot_change_30d != null ? UI.statTile({ label: "30-day change", value: UI.fmt.signed(race.vdot_change_30d, 1), inset: true }) : ""}
      </div>
      ${prog != null ? `<div class="fx-pctile"><span class="app-meter" style="--mc:var(--cat-1)" role="img" aria-label="${prog}% of the way to the target VDOT${projPct != null ? `; projected race day at ${Math.round(projPct)}%` : ""}"><i style="width:${prog}%"></i>${projPct != null ? `<b style="left:${projPct}%"></b>` : ""}</span>
        <p class="ui-caption">${prog}% of the way from ${st} (${shortDate(t[0].date)}) to ${tgt}${projPct != null ? ". The marker is the projected race-day value." : "."}</p></div>` : ""}
      ${race.anchor_note ? `<p class="ui-caption">${escapeHtml(race.anchor_note)}</p>` : ""}
      ${ceiling ? UI.insightCallout({ variant: "inline", tone: "info", label: "Proven best",
        body: `VDOT ${ceiling.vdot}: you raced ${ceiling.actual_time} for ${ceiling.actual_km} km on ${shortDate(ceiling.date)}.${ceiling.vdot >= (tgt || 99) ? ` That's ${goalTime()}-or-faster shape you've already held, so this is a rebuild, not new ground.` : ""}` }) : ""}
    </div>`;
  }

  function renderVo2(v) {
    if (!v) { $("#vo2Body").innerHTML = UI.emptyState({ icon: "heart-pulse", compact: true, title: "No VO₂max synced yet" }); $("#chVo2").innerHTML = ""; return; }
    $("#vo2Body").innerHTML = `<div class="ui-stack" style="gap:var(--sp-4)">
      <div class="ui-row"><span class="ui-stat-value">${v.vo2max}<small>ml/kg/min</small></span>${UI.badge(v.category, { tone: UI.tone(v.color) })}</div>
      <p class="ui-small ui-muted">Fitter than about <b style="color:var(--text-1)">${v.percentile}%</b> of ${v.sex === "F" ? "women" : "men"} aged ${escapeHtml(v.band)}${v.fitness_age ? `, the aerobic engine of a <b style="color:var(--text-1)">${v.fitness_age}-year-old</b>` : ""}.</p>
      <div class="fx-pctile"><span class="app-meter" style="--mc:var(--cat-2)" role="img" aria-label="${v.percentile}th percentile"><i style="width:${v.percentile}%"></i></span>
        <div class="fx-scale"><span>0</span><span>25</span><span>50</span><span>75</span><span>100th</span></div></div></div>`;
    const cc = v.cohort_curve || [];
    if (!cc.length) { $("#chVo2").innerHTML = ""; return; }
    let near = 0; cc.forEach((p, i) => { if (Math.abs(p.p - v.percentile) < Math.abs(cc[near].p - v.percentile)) near = i; });
    uiChart("chVo2", {
      takeaway: "", labels: cc.map((p) => p.p), xLabel: "Percentile", xFormat: (p, i, w) => (w === "tick" ? `${p}%` : `${p}th percentile`), height: 200,
      series: [
        { label: "Your age group", type: "area", data: cc.map((p) => p.vo2), color: "--cat-2", endDot: false, fmt: (x) => x.toFixed(1) },
        { label: "You", data: cc.map((_, i) => (i === near ? v.vo2max : null)), color: "--cat-1", points: true, width: 0, endDot: false, fmt: (x) => x.toFixed(1) },
      ],
    });
  }

  function renderRace(race) {
    const actions = $("#raceActions");
    if (!race || !race.predictions) { $("#raceBody").innerHTML = UI.emptyState({ icon: "flag", compact: true, title: "No race predictions synced" }); if (actions) actions.innerHTML = ""; return; }
    const hl = raceNoun() === "marathon" ? "Marathon" : "Half";
    const dists = ["5K", "10K", "Half", "Marathon"];
    const byDist = (arr) => Object.fromEntries((arr || []).map((p) => [p.dist, p]));
    const stravaMeta = race.strava_meta || {};
    const when = stravaMeta.updated_at ? new Date(stravaMeta.updated_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
    const stravaSub = stravaMeta.source === "strava-mcp" ? `Strava MCP · ${when}` : stravaMeta.source === "strava-oauth" ? `Strava sync · ${when}`
      : (race.predictions_strava || []).length ? "Entered by you" : "Not synced";
    const sources = [
      { key: "anchored", label: "Blended", sub: "Our anchor VDOT", data: byDist(race.predictions_anchored) },
      { key: "garmin", label: "Garmin", sub: "Pulled down by training fatigue", data: byDist(race.predictions) },
      { key: "strava", label: "Strava", sub: stravaSub, data: byDist(race.predictions_strava) },
    ];
    const head = `<tr><th scope="col">Source</th>${dists.map((d) => `<th scope="col" class="r${d === hl ? " is-race" : ""}">${d}${d === hl ? " · your race" : ""}</th>`).join("")}</tr>`;
    const body = sources.map((s) => `<tr><th scope="row"><b>${escapeHtml(s.label)}</b><small class="ui-caption" style="display:block;font-weight:400">${escapeHtml(s.sub)}</small></th>
      ${dists.map((d) => { const p = s.data[d]; return `<td class="r num${d === hl ? " is-race" : ""}">${p && p.time ? `<b>${escapeHtml(p.time)}</b>${p.pace ? `<small>${escapeHtml(p.pace)}/km</small>` : ""}` : "–"}</td>`; }).join("")}</tr>`).join("");
    const hasStrava = (race.predictions_strava || []).length > 0;
    if (actions) actions.innerHTML = UI.button({ label: "Edit Strava times", variant: "secondary", size: "sm", icon: "pencil", attrs: { "data-rp-edit": "", "aria-expanded": "false" } });
    $("#raceBody").innerHTML = `<div class="ui-stack">
      <div class="ui-table-wrap"><table class="ui-table fx-rp"><caption class="ui-sr">Race predictions by source</caption><thead>${head}</thead><tbody>${body}</tbody></table></div>
      ${UI.insightCallout({ variant: "inline", tone: "info", label: "How to read this",
        body: `Anchor read: **${race.current_half}** (${race.current_half_pace}/km) for the ${raceNoun()} against a goal of **${goalTime()}**. Garmin's prediction is pulled down by fatigue during a build; the Blended row applies our recalibrated VDOT to each distance so a hard block doesn't hide your actual fitness.` })}
      ${stravaMeta.reference ? `<p class="ui-caption">Strava reference: ${escapeHtml(stravaMeta.reference)}</p>` : ""}
      ${hasStrava ? "" : `<p class="ui-caption">Ask the coach to "sync Strava predictions", or enter them with Edit Strava times.</p>`}
      <form class="fx-rp-form" data-rp-form hidden>
        <p class="ui-small ui-muted full">Accepts 5:32, 22:30, 1:28:00 or raw seconds. Leave a field blank to clear it.</p>
        ${dists.map((d) => UI.field({ label: d, control: UI.input({ attrs: { "data-rp-in": d, inputmode: "numeric", autocomplete: "off" } }) })).join("")}
        <div class="full ui-row">${UI.button({ label: "Save", variant: "primary", type: "submit" })}${UI.button({ label: "Cancel", variant: "ghost", attrs: { "data-rp-cancel": "" } })}</div>
      </form></div>`;

    const bodyEl = $("#raceBody"), form = bodyEl.querySelector("[data-rp-form]"), editBtn = actions && actions.querySelector("[data-rp-edit]");
    const setOpen = (open) => {
      form.hidden = !open; editBtn && editBtn.setAttribute("aria-expanded", String(open));
      if (open) {
        const cur = byDist(race.predictions_strava);
        form.querySelectorAll("[data-rp-in]").forEach((inp) => { inp.value = cur[inp.dataset.rpIn] ? cur[inp.dataset.rpIn].time : ""; });
        form.querySelector("[data-rp-in]")?.focus();
      }
    };
    editBtn?.addEventListener("click", () => setOpen(form.hidden));
    form.querySelector("[data-rp-cancel]").addEventListener("click", () => setOpen(false));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const times = {};
      form.querySelectorAll("[data-rp-in]").forEach((inp) => { const v = (inp.value || "").trim(); if (v) times[inp.dataset.rpIn] = v; });
      try {
        const r = await api("/api/strava/predictions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ times }) });
        if (r.error) return toast(r.error, false);
        toast("Strava predictions saved.");
        const fitness = await api("/api/coach/fitness");
        S.fitness = fitness;
        renderRace(fitness.race);
      } catch (err) { toast("Couldn't save: " + (err.message || err), false); }
    });
  }

  function renderAcwr(ac) {
    if (!ac || !ac.series || !ac.series.length) {
      $("#acwrSummary").innerHTML = UI.emptyState({ icon: "flame", compact: true, title: "Not enough load history for a trend yet" });
      $("#chAcwr").innerHTML = "";
      return;
    }
    const st = ac.current;
    const tone = st == null ? "neutral" : st < 0.8 ? "info" : st <= 1.3 ? "good" : st <= 1.5 ? "warn" : "bad";
    const word = ac.status || { info: "Under-loaded", good: "Optimal", warn: "High", bad: "Very high" }[tone] || "";
    $("#acwrSummary").innerHTML = "";
    const s = ac.series;
    uiChart("chAcwr", {
      title: "Load balance (ACWR)", takeaway: `This week's load against your 4-week average. ${word ? `${word} right now.` : ""} The sweet spot is 0.8–1.3.`,
      hero: st != null ? { value: st.toFixed(2), sub: word } : null,
      labels: s.map((p) => p.date), xFormat: dLbl, height: 220,
      y: { suggestedMin: 0.5, suggestedMax: 1.8, fmt: (v) => v.toFixed(1) },
      yBands: [{ from: 0.8, to: 1.3, color: "--good", alpha: 0.08 }],
      targets: [{ value: 1.5, label: "Risk 1.5" }],
      series: [{ label: "ACWR", data: s.map((p) => p.acwr), color: "--cat-1", fmt: (v) => v.toFixed(2) }],
      legendExtra: ['<i class="bar" style="--sw:var(--good-soft)"></i>Sweet spot'],
    });
  }

  function renderStatusCard(st, vo2) {
    if (!st) { $("#statusBody").innerHTML = UI.emptyState({ icon: "gauge", compact: true, title: "No training status synced" }); $("#chStatus").innerHTML = ""; return; }
    const dir = st.fitness_trend > 0 ? "rising" : st.fitness_trend < 0 ? "falling" : "steady";
    $("#statusBody").innerHTML = `<div class="ui-stack" style="gap:var(--sp-3)">
      <div class="ui-row">${UI.badge(st.label, { tone: UI.tone(st.color) })}<span class="ui-small">VO₂max <b class="num">${st.vo2max ?? "–"}</b>, ${dir}</span></div>
      ${st.read ? `<p class="ui-small ui-muted">${escapeHtml(st.read)}</p>` : ""}
      ${st.load_focus ? `<p class="ui-small">Load focus: <b>${escapeHtml(st.load_focus)}</b></p>` : ""}</div>`;
    const tr = (vo2 && vo2.trend) || [];
    if (tr.length >= 2) {
      const vals = tr.map((p) => p.vo2max);
      uiChart("chStatus", {
        title: "VO₂max trend", takeaway: "", labels: tr.map((p) => p.date), xFormat: dLbl, height: 170, legend: false,
        y: { suggestedMin: Math.min(...vals) - 1, suggestedMax: Math.max(...vals) + 1, fmt: (v) => Number(v).toFixed(1) },
        series: [{ label: "VO₂max", data: vals, color: "--cat-1", type: "area", points: true, fmt: (v) => v.toFixed(1) }],
      });
    } else $("#chStatus").innerHTML = `<p class="ui-caption">VO₂max updates slowly. The trend fills in as Garmin recomputes it after quality runs.</p>`;
  }

  function renderThreshold(lt) {
    lt = lt || {};
    if (!(lt.hr || lt.pace_s_per_km || lt.ftp_watts)) {
      $("#thresholdBody").innerHTML = UI.emptyState({ icon: "zap", compact: true, title: "No lactate-threshold estimate yet", body: "It appears after a hard, well-measured effort." });
      return;
    }
    const meta = lt.ftp_watts && (lt.ftp_measured_at || lt.ftp_origin)
      ? `FTP ${lt.ftp_measured_at ? `from ${lt.ftp_measured_at}` : ""}${lt.ftp_origin ? `, derived from ${lt.ftp_origin}` : ""}${lt.ftp_weight_kg ? ` at ${lt.ftp_weight_kg} kg` : ""}.` : "";
    $("#thresholdBody").innerHTML = `<div class="ui-stack" style="gap:var(--sp-4)"><div class="cx-stats">
        ${lt.hr ? UI.statTile({ label: "Threshold heart rate", value: lt.hr, unit: "bpm", inset: true, foot: lt.measured_at ? `Measured ${escapeHtml(lt.measured_at)}` : "" }) : ""}
        ${UI.statTile({ label: "Threshold pace", value: lt.pace_s_per_km ? paceStr(lt.pace_s_per_km) : "–", unit: lt.pace_s_per_km ? "/km" : "", inset: true })}
        ${lt.ftp_watts ? UI.statTile({ label: "Threshold power (FTP)", value: lt.ftp_watts, unit: "W", inset: true, foot: `${lt.ftp_wkg ? `${lt.ftp_wkg} W/kg` : ""}${lt.ftp_is_stale ? ` ${UI.badge("stale", { size: "sm", tone: "warn" })}` : ""}` }) : ""}
      </div>
      ${lt.pace_s_per_km ? "" : UI.insightCallout({ variant: "inline", tone: "info", label: "No pace read yet", body: "Garmin hasn't logged a valid threshold pace. A hard 20–30 minute effort will produce one." })}
      ${meta ? `<p class="ui-caption">${escapeHtml(meta)}</p>` : ""}
      <p class="ui-caption">Threshold heart rate sets the top of your Z4; FTP is the running-power equivalent.</p></div>`;
  }

  const clockH = (h) => { // decimal hour (0–26) -> "10:07 PM"
    if (h == null) return "–"; h = ((h % 24) + 24) % 24;
    let hr = Math.floor(h), mn = Math.round((h - hr) * 60); if (mn === 60) { hr++; mn = 0; }
    const ap = hr < 12 ? "AM" : "PM", h12 = hr % 12 || 12;
    return `${h12}:${String(mn).padStart(2, "0")} ${ap}`;
  };
  const minsFmt = (m) => (m == null ? "–" : m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`);
  const SLEEP_GRADE = { green: ["good", "On point"], amber: ["warn", "Room to gain"], red: ["bad", "Needs work"], grey: ["neutral", "No data"] };
  const STAGES = [["deep", "Deep", "--sleep-deep"], ["rem", "REM", "--sleep-rem"], ["light", "Light", "--sleep-light"], ["awake", "Awake", "--sleep-awake"]];

  // Alcohol-signature flags (computed by the sleep endpoint) — one source of
  // truth for Sleep and Recovery. On charts they're subtle shaded days.
  function _alcMap() {
    const m = {};
    for (const f of ((S.sleep && S.sleep.alcohol_flags) || [])) m[f.date] = f;
    return m;
  }
  const alcRanges = (dates, map) => dates.filter((d) => map[d]).map((d) => ({ from: d, to: d, color: map[d].level === "likely" ? "--bad" : "--warn", alpha: 0.14 }));
  const ALC_LEGEND = ['<i class="bar" style="--sw:var(--bad-soft)"></i>Alcohol signature (likely)', '<i class="bar" style="--sw:var(--warn-soft)"></i>Possible'];

  function _renderAlcoholCallout(flags) {
    const host = $("#alcCallout"); if (!host) return;
    if (!flags || !flags.length) { host.innerHTML = ""; return; }
    const weekend = flags.filter((f) => f.is_weekend).length, likely = flags.filter((f) => f.level === "likely").length;
    const rows = flags.slice(-6).reverse().map((f) => `**${f.weekday || ""} ${shortDate(f.date)}** (${f.level}${f.is_weekend ? ", weekend" : ""}): ${(f.signals || []).map((x) => x.detail).join(" · ")}`);
    host.innerHTML = UI.insightCallout({ tone: "warn", label: "Lifestyle signal", icon: "triangle-alert",
      title: `${flags.length} night${flags.length === 1 ? "" : "s"} look like evening alcohol (${likely} strong, ${weekend} on weekends).`,
      body: rows,
      why: "They're shaded on the charts: red when three or more markers agree, amber for two. The signal is a composite of overnight stress, HRV, respiration, resting HR and Body Battery restore against your rolling 30-night baseline. It suggests, it doesn't diagnose." });
  }

  // ================= SLEEP =================
  function renderSleep() {
    const s = S.sleep;
    if (!s || !s.has_data) {
      $("#sleepCoachBody").innerHTML = UI.card({ body: UI.emptyState({ icon: "moon", title: "No sleep data yet", body: (s && s.message) || "Sync your watch to see last night." }) });
      ["slLastNight", "chSleepStages", "alcCallout", "slTrend", "chSleepTiming", "chSleepScore", "chResp", "chSpo2", "chSleepStress", "slPatterns", "sleepRecoveryBody", "slHeadStats"]
        .forEach((id) => { const n = $("#" + id); if (n) n.innerHTML = ""; });
      return;
    }
    const nights = s.nights || [], L = s.latest || {}, R = s.rolling || {}, T = s.targets || {};
    const alcFlags = s.alcohol_flags || [];
    const alcByDate = _alcMap();

    // ---- head ----
    const scoreTone = L.score == null ? null : L.score >= 80 ? "good" : L.score >= 65 ? "warn" : "bad";
    $("#slHeadStats").innerHTML = [
      L.score != null ? UI.badge(`Last night ${L.score}`, { outline: true, tone: scoreTone }) : "",
      R.avg_duration_h ? UI.badge(`${R.avg_duration_h} h average`, { outline: true }) : "",
      R.debt_min != null ? UI.badge(`Debt ${minsFmt(R.debt_min)}`, { outline: true, tone: R.debt_min >= 90 ? "warn" : null }) : "",
      alcFlags.length ? UI.badge(`${alcFlags.length} alcohol-flagged`, { outline: true, tone: "warn" }) : "",
    ].join("");

    // ---- coach read (lead) ----
    const [gTone, gWord] = SLEEP_GRADE[s.grade] || SLEEP_GRADE.grey;
    $("#sleepCoachBody").innerHTML = `<div class="ui-stack">
      ${UI.coachTip({ tone: gTone, label: `Sleep coach · ${gWord}`, icon: "moon", title: s.headline || gWord, body: s.insight || "",
 })}
      ${(s.recommendations || []).length ? UI.card({ title: "What a coach would change", icon: "lightbulb",
        body: UI.insightList(s.recommendations.map((r) => ({ tone: "info", title: r.title, detail: r.detail }))) }) : ""}
      ${s.training_note ? UI.insightCallout({ tone: "coach", label: "For training", icon: "footprints", body: s.training_note }) : ""}</div>`;

    _renderSlLastNight(L, T, R);
    _renderSlStages(nights, alcByDate);
    _renderAlcoholCallout(alcFlags);
    _renderSlTrend(nights, R);
    _renderSlCharts(nights, alcByDate);
    _renderSlPatterns(nights, alcFlags, R, T);

    // overnight recovery vitals live on the Readiness & HRV view; keep the combined read here
    const V = s.vitals || {};
    $("#sleepRecoveryBody").innerHTML = V.combined ? UI.insightCallout({ tone: "info", label: "Overnight recovery", icon: "heart-pulse", title: V.combined,
      actions: `<a class="ui-btn ghost sm" href="#recovery/hrv">Readiness &amp; HRV${UI.icon("chevron-right", { size: 16 })}</a>` }) : "";
  }

  // Last night: duration, times, real stage composition and stage vs target.
  function _renderSlLastNight(L, T, R) {
    const el = $("#slLastNight"); if (!el) return;
    if (!L || !L.duration_h) { el.innerHTML = UI.emptyState({ icon: "moon", compact: true, title: "No sleep recorded for last night" }); return; }
    const tone = L.score == null ? "neutral" : L.score >= 80 ? "good" : L.score >= 65 ? "warn" : "bad";
    const word = L.score == null ? "" : L.score >= 80 ? "Good night" : L.score >= 65 ? "OK night" : "Off night";
    const awakePct = Math.max(0, 100 - (L.deep_pct || 0) - (L.rem_pct || 0) - (L.light_pct || 0));
    const pct = { deep: L.deep_pct || 0, rem: L.rem_pct || 0, light: L.light_pct || 0, awake: awakePct };
    const hrs = { deep: L.deep_h, rem: L.rem_h, light: L.light_h };
    const dp = T.deep || [13, 23], rm = T.rem || [20, 28];
    const target = (key, name, lo, hi) => {
      const v = pct[key]; if (v == null) return "";
      const scale = Math.max(40, hi * 1.25, v + 5);   // each row's track fits its own target and value
      const st = v >= lo && v <= hi ? "good" : v < lo ? "warn" : "info";
      return `<div class="sr-target" style="--sc:var(${STAGES.find((x) => x[0] === key)[2]})">
        <span>${name}</span>
        <span class="track" role="img" aria-label="${name} ${Math.round(v)}%, target ${lo} to ${hi}%"><i class="band" style="left:${lo / scale * 100}%;width:${(hi - lo) / scale * 100}%"></i><i class="fill" style="width:${Math.min(100, v / scale * 100)}%"></i></span>
        <span class="v">${Math.round(v)}% ${UI.badge(st === "good" ? "in range" : st === "warn" ? "low" : "high", { size: "sm", tone: st })}</span></div>`;
    };
    const chip = (label, value, unit) => (value == null ? "" : UI.statTile({ label, value, unit, inset: true }));
    el.innerHTML = `<div class="ui-card-head"><div><h3 class="ui-card-title">Last night</h3><p class="ui-card-sub">${escapeHtml(UI.fmt.date(L.date, { weekday: "long", month: "short", day: "numeric" }))}</p></div>${word ? UI.badge(word, { tone, autoIcon: true }) : ""}</div>
      <div class="sr-big" data-tone="${tone}"><b>${escapeHtml(UI.fmt.dur(L.duration_h * 3600))}</b>${L.score != null ? `<span>score ${L.score}</span>` : ""}</div>
      <div class="sr-times"><span>Bed <b>${escapeHtml(L.bedtime_str || "–")}</b></span><span>Wake <b>${escapeHtml(L.waketime_str || "–")}</b></span>
        ${L.efficiency != null ? `<span>Efficiency <b>${Math.round(L.efficiency)}%</b></span>` : ""}${L.awake_count != null ? `<span>Awakenings <b>${L.awake_count}</b></span>` : ""}</div>
      <div class="sr-stagebar" role="img" aria-label="Stages: ${STAGES.map(([k, n]) => `${n} ${Math.round(pct[k])}%`).join(", ")}">${STAGES.map(([k, , c]) => (pct[k] > 0 ? `<i style="flex:${pct[k]};--sc:var(${c})"></i>` : "")).join("")}</div>
      <div class="ui-cal-legend">${STAGES.map(([k, n, c]) => `<span><i style="--sw:var(${c})"></i>${n} ${k === "awake" ? `${Math.round((L.awake_h || 0) * 60)} min` : `${(hrs[k] || 0).toFixed(1)} h · ${Math.round(pct[k])}%`}</span>`).join("")}</div>
      <div class="app-meters">${target("deep", "Deep", dp[0], dp[1])}${target("rem", "REM", rm[0], rm[1])}${target("light", "Light", 45, 60)}</div>
      <div class="cx-stats">${chip("Resting HR", L.rhr, "bpm")}${chip("HRV", L.hrv, "ms")}${chip("Respiration", L.respiration, "br/min")}${chip("Overnight stress", L.stress)}${chip("Lowest SpO₂", L.spo2_low, "%")}${chip("Body Battery", L.bb_change != null ? `+${L.bb_change}` : null)}</div>`;
  }

  // 14 nights as stacked stage hours — the real data the old hover strip hinted at.
  function _renderSlStages(nights, alcByDate) {
    const arr = nights.slice(-14);
    const dates = arr.map((n) => n.date);
    uiChart("chSleepStages", {
      title: "Sleep stages", takeaway: arr.length ? `Average ${(arr.reduce((t, n) => t + (n.duration_h || 0), 0) / arr.length).toFixed(1)} h a night. Shaded nights carry an alcohol signature.` : "",
      labels: dates, xFormat: dLbl, height: 240, empty: "No nights synced yet",
      y: { beginAtZero: true, fmt: (v) => `${v}h` },
      series: [
        { label: "Deep", unit: "h", type: "bar", stack: "s", data: arr.map((n) => n.deep_h ?? null), color: "--sleep-deep", fmt: (v) => v.toFixed(1) },
        { label: "Light", unit: "h", type: "bar", stack: "s", data: arr.map((n) => n.light_h ?? null), color: "--sleep-light", fmt: (v) => v.toFixed(1) },
        { label: "REM", unit: "h", type: "bar", stack: "s", data: arr.map((n) => n.rem_h ?? null), color: "--sleep-rem", fmt: (v) => v.toFixed(1) },
        { label: "Awake", unit: "h", type: "bar", stack: "s", data: arr.map((n) => n.awake_h ?? null), color: "--sleep-awake", fmt: (v) => v.toFixed(1) },
      ],
      ranges: alcRanges(dates, alcByDate),
      legendExtra: arr.some((n) => alcByDate[n.date]) ? ALC_LEGEND : [],
      tooltipExtra: (i) => { const n = arr[i]; return n ? [n.score != null ? `Score ${n.score}` : "", n.duration_h ? `${n.duration_h.toFixed(1)} h total` : "", alcByDate[n.date] ? `Alcohol signature (${alcByDate[n.date].level})` : ""].filter(Boolean).join(" · ") : ""; },
    });
  }

  // 30-night trends as MetricTrends (sparkline + delta vs the previous week).
  function _renderSlTrend(nights, R) {
    const el = $("#slTrend"); if (!el) return;
    const arr = nights.slice(-30);
    if (!arr.length) { el.innerHTML = UI.emptyState({ icon: "moon", compact: true, title: "Not enough nights yet" }); return; }
    const last = arr[arr.length - 1] || {};
    const col = (k) => arr.map((n) => n[k]);
    el.innerHTML = [
      UI.metricTrend({ label: "Sleep score", icon: "moon", value: last.score ?? "–", series: col("score"), color: "var(--cat-1)",
        delta: _deltaVsWeek(col("score")), goodWhen: "up", read: `30-night average ${_avg(col("score"))}.` }),
      UI.metricTrend({ label: "Duration", icon: "clock", value: last.duration_h ? UI.fmt.dur(last.duration_h * 3600) : "–", series: col("duration_h"), color: "var(--sleep-light)",
        delta: _deltaVsWeek(col("duration_h")), deltaFmt: (d) => `${UI.fmt.signed(d, 1)} h`, goodWhen: "up",
        read: `${_avg(col("duration_h"))} h average${R.debt_min ? `, ${minsFmt(R.debt_min)} of debt` : ""}.` }),
      UI.metricTrend({ label: "Overnight HRV", icon: "heart-pulse", value: last.hrv ?? "–", unit: last.hrv != null ? "ms" : "", series: col("hrv"), color: "var(--cat-2)",
        delta: _deltaVsWeek(col("hrv")), deltaFmt: (d) => `${UI.fmt.signed(d)} ms`, goodWhen: "up", read: `30-night average ${_avg(col("hrv"))} ms.` }),
      UI.metricTrend({ label: "Overnight stress", icon: "activity", value: last.stress ?? "–", series: col("stress"), color: "var(--cat-4)",
        delta: _deltaVsWeek(col("stress")), goodWhen: "down", read: "Under 20 is calm. Spikes above 40 usually mean a late meal, alcohol or a stressful day." }),
      UI.metricTrend({ label: "Body Battery restore", icon: "battery-medium", value: last.bb_change != null ? `+${last.bb_change}` : "–", series: col("bb_change"), color: "var(--cat-3)",
        delta: _deltaVsWeek(col("bb_change")), goodWhen: "up", read: "A good night usually restores 55–75." }),
      UI.metricTrend({ label: "Bedtime spread", icon: "bed", value: R.bedtime_sd_min != null ? `±${R.bedtime_sd_min}` : "–", unit: R.bedtime_sd_min != null ? "min" : "",
        status: R.regularity ? { text: String(R.regularity).toLowerCase(), tone: UI.tone(R.regularity_state) } : null,
        read: "Regular bed and wake times predict recovery more than any single night's length." }),
    ].join("");
  }
  function _avg(arr) {
    const nums = arr.filter((v) => v != null && !isNaN(v));
    return nums.length ? Math.round(nums.reduce((s, v) => s + v, 0) / nums.length * 10) / 10 : "–";
  }

  function _renderSlCharts(nights, alcByDate) {
    const dates = nights.map((n) => n.date);
    const ranges = alcRanges(dates, alcByDate);
    const legend = nights.some((n) => alcByDate[n.date]) ? ALC_LEGEND : [];
    uiChart("chSleepTiming", {
      title: "Bed and wake times", takeaway: "The flatter these lines, the better your sleep tends to be.",
      labels: dates, xFormat: dLbl, height: 220, empty: "No bed or wake times yet",
      y: { fmt: (v) => clockH(v), step: 2 },
      series: [
        { label: "Bedtime", data: nights.map((n) => n.bed_plot ?? null), color: "--cat-5", points: true, fmt: (v) => clockH(v) },
        { label: "Wake", data: nights.map((n) => n.wake_plot ?? null), color: "--cat-4", points: true, fmt: (v) => clockH(v) },
      ],
    });
    uiChart("chSleepScore", {
      title: "Sleep score and Body Battery restore", takeaway: "Both on a 0–100 scale; higher is better.",
      labels: dates, xFormat: dLbl, height: 220, y: { min: 0, max: 100 },
      series: [
        { label: "Sleep score", data: nights.map((n) => n.score ?? null), color: "--cat-1", points: true, fmt: (v) => `${v}` },
        { label: "Body Battery restore", data: nights.map((n) => n.bb_change ?? null), color: "--cat-3", dash: true, points: true, fmt: (v) => `+${v}` },
      ],
      ranges, legendExtra: legend,
    });
    // Respiration and SpO2 have different units, so two small charts instead of one dual-axis chart.
    uiChart("chResp", {
      title: "Respiration", takeaway: "Breaths per minute overnight.", labels: dates, xFormat: dLbl, height: 160, legend: false,
      y: { suggestedMin: 10, suggestedMax: 20, fmt: (v) => `${v}` }, empty: "No respiration data",
      series: [{ label: "Respiration", unit: "br/min", data: nights.map((n) => n.respiration ?? null), color: "--cat-3", points: true, fmt: (v) => v.toFixed(1) }],
      ranges,
    });
    uiChart("chSpo2", {
      title: "Blood oxygen (SpO₂)", takeaway: "Average overnight saturation.", labels: dates, xFormat: dLbl, height: 160, legend: false,
      y: { suggestedMin: 88, suggestedMax: 100, fmt: (v) => `${v}%` }, empty: "No SpO₂ data",
      series: [{ label: "SpO₂", unit: "%", data: nights.map((n) => n.spo2_avg ?? null), color: "--cat-2", points: true, fmt: (v) => `${v}` }],
      ranges,
    });
    uiChart("chSleepStress", {
      title: "Overnight stress", takeaway: "Lower is better.", labels: dates, xFormat: dLbl, height: 160, legend: false,
      y: { min: 0, suggestedMax: 60 }, empty: "No overnight stress data",
      series: [{ label: "Overnight stress", type: "area", data: nights.map((n) => n.stress ?? null), color: "--cat-4", points: true, fmt: (v) => `${v}` }],
      ranges,
    });
  }

  // Patterns computed from your own nights. Every statement is derived from data.
  function _renderSlPatterns(nights, alcFlags, R, T) {
    const el = $("#slPatterns"); if (!el) return;
    const arr = nights.slice(-30);
    const out = [];
    // 1) weekend vs weeknight
    const isWknd = (n) => { const d = new Date(n.date + "T00:00:00").getDay(); return d === 5 || d === 6 || d === 0; };
    const wknd = arr.filter(isWknd), wk = arr.filter((n) => !isWknd(n));
    const wkAvg = _avg(wk.map((n) => n.score)), wkndAvg = _avg(wknd.map((n) => n.score));
    if (wk.length && wknd.length && Math.abs(wkAvg - wkndAvg) >= 5) {
      const worse = wkndAvg < wkAvg;
      const alcWknd = (alcFlags || []).filter((f) => f.is_weekend).length;
      out.push({ tone: worse ? "warn" : "good", label: "Weekends", title: `Weekend nights average ${wkndAvg} against ${wkAvg} on weeknights.`,
        body: worse ? `${alcWknd ? `${alcWknd} of ${alcFlags.length} alcohol-flagged nights fall on weekends. ` : ""}An earlier bedtime on Friday and Saturday usually closes most of the gap.` : "Your weekend rhythm holds up." });
    }
    // 2) deep below target
    const dp = (T && T.deep) || [13, 23];
    const belowDeep = arr.filter((n) => n.deep_pct != null && n.deep_pct < dp[0]).length;
    if (belowDeep >= 5) out.push({ tone: "warn", label: "Deep sleep", title: `Deep sleep was under ${dp[0]}% on ${belowDeep} of ${arr.length} nights.`,
      body: "In order of impact: a cool room (16–18°C), no alcohol within three hours of bed, and no bright screens in the last hour." });
    // 3) bedtime consistency
    if (R && R.bedtime_sd_min != null && R.bedtime_sd_min <= 30) out.push({ tone: "good", label: "Bedtime", title: `Bedtimes are consistent (±${R.bedtime_sd_min} min).`, body: "The most important sleep habit is locked in. Keep it on rest days too." });
    else if (R && R.bedtime_sd_min != null && R.bedtime_sd_min > 60) out.push({ tone: "warn", label: "Bedtime", title: `Bedtimes vary by ±${R.bedtime_sd_min} min.`, body: "Getting that under ±30 minutes is the biggest single lever you have." });
    // 4) nights after long runs
    const acts = (S.snap && S.snap.activities) || [];
    const after = new Set(acts.filter((a) => a.distance_km >= 15 && (a.type || "").toLowerCase().includes("run")).map((r) => iso(new Date(new Date(r.date + "T00:00:00").getTime() + 86400000))));
    const afterNights = arr.filter((n) => after.has(n.date));
    if (afterNights.length >= 2) {
      const diff = _avg(afterNights.map((n) => n.score)) - _avg(arr.map((n) => n.score));
      if (Math.abs(diff) >= 3) out.push({ tone: diff > 0 ? "good" : "warn", label: "After long runs", title: `Nights after a long run score ${UI.fmt.signed(diff)} against your average.`,
        body: diff > 0 ? "Training is building healthy sleep pressure." : "Long runs may be pushing evenings late or keeping you wired into the night." });
    }
    el.innerHTML = out.length ? out.map((p) => UI.insightCallout(p)).join("")
      : UI.emptyState({ icon: "moon", compact: true, title: "No clear patterns yet", body: "A few more weeks of nights and this fills in." });
  }

  // ================= READINESS & HRV =================
  // A vital against your personal baseline: value, state, a meter with the
  // "good" zone shaded, the range in words, and the coach's read.
  function vitalCard(v, kind) {
    const isHrv = kind === "hrv";
    const name = isHrv ? "Overnight HRV" : "Resting heart rate";
    if (!v || v.last == null) return `<div class="sr-vital"><h3 class="ui-h3">${name}</h3>${UI.emptyState({ icon: "heart-pulse", compact: true, title: "No data yet" })}</div>`;
    const tone = { in: "good", above: "good", below: "bad", excellent: "good", normal: "good", elevated: "bad" }[v.state] || "warn";
    const word = { in: "in range", above: "above range", below: "below range", excellent: "at rested floor", normal: "normal", elevated: "elevated" }[v.state] || v.state;
    let smin, smax, bandFrom, bandTo, rangeLine;
    if (isHrv) {
      const lo = v.ideal_low, hi = v.ideal_high, best = v.best || hi;
      smin = Math.min(lo - 8, v.last - 4); smax = Math.max(hi + 8, v.last + 4, best);
      bandFrom = lo; bandTo = hi;
      rangeLine = `Normal ${lo}–${hi} ms · 7-day average ${v.recent_avg ?? "–"} · two-year best ${v.best ?? "–"}`;
    } else {
      smin = (v.best ?? v.rested) - 2; smax = (v.elevated ?? v.baseline) + 4;
      bandFrom = v.best ?? v.rested; bandTo = v.rested;   // lower is better
      rangeLine = `Rested about ${v.rested} · baseline about ${v.baseline} · best ${v.best ?? "–"} bpm`;
    }
    const pos = (x) => Math.max(0, Math.min(100, ((x - smin) / (smax - smin)) * 100));
    return `<div class="sr-vital" data-tone="${tone}">
      <div class="ui-row" style="justify-content:space-between"><h3 class="ui-h3">${name}</h3>${UI.badge(word, { tone, size: "sm", autoIcon: true })}</div>
      <span class="ui-stat-value">${v.last}<small>${v.unit}</small></span>
      <span class="sr-meter" role="img" aria-label="${name} ${v.last} ${v.unit}, ${word}"><i class="band" style="left:${pos(bandFrom)}%;width:${Math.max(2, pos(bandTo) - pos(bandFrom))}%"></i><i class="mark" style="left:${pos(v.last)}%"></i></span>
      <p class="ui-caption">${escapeHtml(rangeLine)}</p>
      ${v.read ? `<p class="ui-small ui-muted">${escapeHtml(v.read)}</p>` : ""}</div>`;
  }
  function pctileBar(pct, cat, color, label) {
    if (pct == null) return "";
    return `<div class="fx-pctile"><div class="ui-row" style="justify-content:space-between">${UI.badge(cat, { size: "sm", tone: UI.tone(color) })}<span class="ui-caption">${escapeHtml(label)}</span></div>
      <span class="app-meter" style="--mc:var(--cat-2)" role="img" aria-label="${pct}th percentile ${escapeHtml(label)}"><i style="width:${pct}%"></i></span>
      <div class="fx-scale"><span>0</span><span>50</span><span>100th percentile</span></div></div>`;
  }

  function renderRecovery() {
    const A = S.recovery;
    if (!A || !A.has_data) {
      $("#readyBody").innerHTML = UI.emptyState({ icon: "heart-pulse", title: "No recovery data yet", body: (A && A.message) || "Sync your watch to see readiness." });
      ["recFeed", "rvHeadStats", "chReadiness", "hrvBody", "chHrv", "rhrBody", "chRhr", "bbSummary", "chBattery", "recGuide"].forEach((id) => { const n = $("#" + id); if (n) n.innerHTML = ""; });
      return;
    }
    const peer = `vs ${A.sex === "F" ? "women" : "men"} aged ${A.band}`;
    const R = A.readiness, HV = A.hrv || {}, RH = A.rhr || {}, BB = A.body_battery || {};
    const alc = _alcMap();

    // ---- head ----
    $("#rvHeadStats").innerHTML = [
      R && R.score != null ? UI.badge(`Readiness ${R.score}`, { outline: true, tone: UI.tone(R.color) }) : "",
      HV.last != null ? UI.badge(`HRV ${HV.last} ms`, { outline: true, tone: HV.state === "below" ? "warn" : "good" }) : "",
      RH.last != null ? UI.badge(`Resting HR ${RH.last}`, { outline: true, tone: RH.state === "elevated" ? "warn" : "good" }) : "",
      BB.charged != null ? UI.badge(`Body Battery +${BB.charged}`, { outline: true, tone: BB.charged >= 60 ? "good" : "warn" }) : "",
    ].join("");

    // ---- readiness hero ----
    if (R) {
      const tone = UI.tone(R.color);
      const meter = (label, val, unit) => (val == null ? "" : `<div class="app-meter-row"><span>${label}</span><span class="app-meter" style="--mc:var(--cat-1)" aria-hidden="true"><i style="width:${Math.min(100, val)}%"></i></span><span class="v">${val}${unit || ""}</span></div>`);
      const rtTone = R.recovery_hours == null ? "neutral" : R.recovery_hours <= 3 ? "good" : R.recovery_hours <= 16 ? "warn" : "bad";
      $("#readyBody").innerHTML = `<div class="ui-card-head"><div><h2 class="ui-card-title">Training readiness</h2><p class="ui-card-sub">Can you go hard today?</p></div>${UI.badge(R.label, { tone, autoIcon: true })}</div>
        <div class="sr-big" data-tone="${tone}"><b>${R.score ?? "–"}</b><span>/100</span></div>
        ${R.guidance ? `<p class="ui-lead">${escapeHtml(R.guidance)}</p>` : ""}
        ${R.limiter ? UI.insightCallout({ variant: "inline", tone: "info", label: "What's holding it back", body: R.limiter }) : ""}
        <div class="app-meters">${meter("Recovery", R.recovery_pct, "%")}${meter("Sleep", R.sleep_score)}${meter("Load balance", R.acwr_pct, "%")}</div>
        ${HV.status ? `<p class="ui-small ui-muted">HRV status: <b style="color:var(--text-1)">${escapeHtml((R.hrv_weekly ? `${R.hrv_weekly} ms, ` : "") + HV.status)}</b></p>` : ""}
        ${R.recovery_hours != null ? UI.insightCallout({ tone: rtTone, label: "Recovery time", icon: "clock", title: `About ${R.recovery_hours} hours until you're primed for a hard effort.`, body: R.recovery_read || "" }) : ""}`;
    } else $("#readyBody").innerHTML = UI.emptyState({ icon: "gauge", compact: true, title: "No readiness score synced" });

    // ---- coach's read beside the hero (was collapsed at the bottom) ----
    const O = A.overreaching;
    $("#recFeed").innerHTML = [
      O ? UI.insightCallout({ tone: O.tone, label: "Overreaching watch", title: O.title, body: O.detail }) : "",
      HV.read ? UI.insightCallout({ tone: HV.state === "below" ? "warn" : "good", label: "HRV", title: `${HV.last} ms, ${String(HV.status || "").toLowerCase() || "overnight"}`, body: HV.read }) : "",
      RH.read ? UI.insightCallout({ tone: RH.state === "elevated" ? "warn" : "good", label: "Resting heart rate", title: `${RH.last} bpm, ${RH.state || ""}`, body: RH.read }) : "",
    ].join("");

    // ---- readiness over time (bars by band), overreaching days and alcohol nights marked ----
    const rd = A.readiness_series || [];
    const hs = {}, rs = {}, bs = {};
    (HV.series || []).forEach((x) => (hs[x.date] = x));
    (RH.series || []).forEach((x) => (rs[x.date] = x));
    (BB.series || []).forEach((x) => (bs[x.date] = x));
    const overreach = (d) => { const h = hs[d], r = rs[d]; return h && r && HV.low && RH.elevated && h.hrv < HV.low && r.rhr > RH.elevated; };
    const rDates = rd.map((x) => x.date);
    uiChart("chReadiness", {
      title: "Readiness", takeaway: rd.length ? `Average ${_avg(rd.map((x) => x.score))} over ${rd.length} days. Red shading marks an overreaching signature: HRV below range and resting HR elevated on the same day.` : "",
      labels: rDates, xFormat: dLbl, height: 220, empty: "No readiness history yet",
      y: { min: 0, max: 100 },
      series: [{ label: "Readiness", type: "bar", legend: false, data: rd.map((x) => x.score),
        colorBy: (v) => (v >= 75 ? "--load-optimal" : v >= 50 ? "--load-high" : "--load-over"), fmt: (v) => `${v}` }],
      ranges: rDates.filter(overreach).map((d) => ({ from: d, to: d, color: "--bad", alpha: 0.16 })).concat(alcRanges(rDates, alc).map((r) => ({ ...r, alpha: 0.08 }))),
      legendExtra: ['<i class="bar" style="--sw:var(--load-optimal)"></i>75+', '<i class="bar" style="--sw:var(--load-high)"></i>50–74', '<i class="bar" style="--sw:var(--load-over)"></i>Under 50'].concat(rDates.some(overreach) ? ['<i class="bar" style="--sw:var(--bad-soft)"></i>Overreaching'] : []),
      tooltipExtra: (i) => { const d = rDates[i], h = hs[d], r = rs[d], b = bs[d]; return [h ? `HRV ${h.hrv} ms` : "", r ? `RHR ${r.rhr}` : "", b && b.charged != null ? `BB +${b.charged}` : "", overreach(d) ? "Overreaching signature" : ""].filter(Boolean).join(" · "); },
    });

    // ---- HRV ----
    if (A.hrv) {
      const h = A.hrv, s = h.series || [];
      $("#hrvBody").innerHTML = `<div class="ui-stack" style="gap:var(--sp-4)">${vitalCard({ last: h.last, unit: "ms", ideal_low: h.low, ideal_high: h.high, recent_avg: h.avg7, best: h.best, state: h.state, read: null }, "hrv")}${pctileBar(h.percentile, h.category, h.cat_color, peer)}</div>`;
      const dates = s.map((x) => x.date);
      uiChart("chHrv", {
        labels: dates, xFormat: dLbl, height: 200, takeaway: "",
        yBands: h.low && h.high ? [{ from: h.low, to: h.high, color: "--good", alpha: 0.08 }] : [],
        series: [
          { label: "Nightly", unit: "ms", data: s.map((x) => x.hrv), color: "--cat-2", width: 0, points: true, endDot: false, fmt: (v) => `${v}` },
          { label: "7-day average", unit: "ms", data: s.map((x) => x.roll), color: "--cat-2", fmt: (v) => `${Math.round(v)}` },
        ],
        ranges: alcRanges(dates, alc),
        legendExtra: ['<i class="bar" style="--sw:var(--good-soft)"></i>Your normal range'].concat(dates.some((d) => alc[d]) ? ALC_LEGEND : []),
      });
    } else { $("#hrvBody").innerHTML = vitalCard(null, "hrv"); $("#chHrv").innerHTML = ""; }

    // ---- resting HR ----
    if (A.rhr) {
      const r = A.rhr, s = r.series || [];
      $("#rhrBody").innerHTML = `<div class="ui-stack" style="gap:var(--sp-4)">${vitalCard({ last: r.last, unit: "bpm", rested: r.rested, baseline: r.baseline, elevated: r.elevated, best: r.best, state: r.state, read: null }, "rhr")}${pctileBar(r.percentile, r.category, r.cat_color, peer)}</div>`;
      const dates = s.map((x) => x.date);
      uiChart("chRhr", {
        labels: dates, xFormat: dLbl, height: 200, takeaway: "",
        series: [{ label: "Resting HR", unit: "bpm", data: s.map((x) => x.rhr), color: "--cat-6", points: true, fmt: (v) => `${v}` }],
        targets: [r.rested ? { value: r.rested, label: `Rested ${r.rested}` } : null, r.elevated ? { value: r.elevated, label: `High ${r.elevated}` } : null].filter(Boolean),
        ranges: alcRanges(dates, alc),
        legendExtra: dates.some((d) => alc[d]) ? ALC_LEGEND : [],
      });
    } else { $("#rhrBody").innerHTML = vitalCard(null, "rhr"); $("#chRhr").innerHTML = ""; }

    // ---- Body Battery ----
    if (BB.series && BB.series.length) {
      $("#bbSummary").innerHTML = `<div class="ui-card-head"><div><h3 class="ui-card-title">Body Battery</h3><p class="ui-card-sub">The shaded range is each day's high to low. Overnight recharge refills it.</p></div></div>
        <div class="cx-stats">${UI.statTile({ label: "Peak", value: BB.high ?? "–", inset: true })}${UI.statTile({ label: "Low", value: BB.low ?? "–", inset: true })}
        ${UI.statTile({ label: "Recharged", value: BB.charged != null ? `+${BB.charged}` : "–", inset: true, tone: BB.charged >= 60 ? "good" : "warn" })}${UI.statTile({ label: "Drained", value: BB.drained ?? "–", inset: true })}</div>`;
      const s = BB.series, dates = s.map((x) => x.date);
      uiChart("chBattery", {
        labels: dates, xFormat: dLbl, height: 200, takeaway: "", y: { min: 0, max: 100 },
        series: [
          { label: "Daily high", data: s.map((x) => x.high), color: "--cat-3", points: true, fmt: (v) => `${v}` },
          { label: "Daily low", data: s.map((x) => x.low), color: "--cat-3", dash: true, width: 1.5, endDot: false, fmt: (v) => `${v}` },
        ],
        ranges: alcRanges(dates, alc),
      });
    } else { $("#bbSummary").innerHTML = UI.emptyState({ icon: "battery-medium", compact: true, title: "No Body Battery data yet" }); $("#chBattery").innerHTML = ""; }

    _renderFeet(A.time_on_feet);
    _renderStress(A.stress);

    $("#recGuide").innerHTML = (A.pitfalls || []).length
      ? `<ul class="sr-list">${A.pitfalls.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul><p class="ui-caption" style="margin-top:var(--sp-3)">Terms with a dotted underline show their definition on hover, focus or tap.</p>` : "";
    $("#recGuideCard").hidden = !(A.pitfalls || []).length;
  }

  // ---- shared: a daily/weekly view switch for the two "across the day" cards ----
  function _modeSwitch(mountId, name, value) {
    const m = $("#" + mountId); if (!m) return;
    m.innerHTML = UI.segmented({ label: "Range", name, value, options: [["daily", "Daily"], ["weekly", "Weekly"]] });
  }
  const _zoneSplit = (parts) => `<div class="ui-zonebar" role="img" aria-label="${parts.map((p) => `${p.label} ${p.pct}%`).join(", ")}">${parts.filter((p) => p.pct > 0).map((p) => `<i style="flex:${p.pct};--zc:var(${p.color})"></i>`).join("")}</div>
    <div class="ui-cal-legend">${parts.map((p) => `<span><i style="--sw:var(${p.color})"></i>${p.label} ${p.pct}%</span>`).join("")}</div>`;

  // Time on feet: workout + walking + sedentary minutes, stacked.
  function _renderFeet(F) {
    const body = $("#feetBody"), foot = $("#feetFoot");
    if (!body || !foot) return;
    if (!F || !(F.daily || []).length) {
      body.innerHTML = UI.emptyState({ icon: "footprints", compact: true, title: "No step or activity data yet" });
      foot.innerHTML = ""; $("#chFeet").innerHTML = ""; $("#feetMode").innerHTML = ""; return;
    }
    S._tof = S._tof || "daily";
    _modeSwitch("feetMode", "tof", S._tof);
    const card = $("#feetCard");
    if (!card.dataset.wired) { card.dataset.wired = "1"; card.addEventListener("ui:change", (e) => { if (e.detail.name === "tof") { S._tof = e.detail.value; _drawFeetChart(S.recovery.time_on_feet); } }); }
    body.innerHTML = `<div class="cx-stats">
      ${UI.statTile({ label: "On your feet today", value: minsFmt(F.today_feet_min), inset: true })}
      ${UI.statTile({ label: "Steps", value: F.today_steps != null ? F.today_steps.toLocaleString() : "–", inset: true })}
      ${UI.statTile({ label: "Workouts", value: minsFmt(F.today_workout_min), inset: true })}
      ${UI.statTile({ label: "Walking", value: minsFmt(F.today_walking_min), inset: true })}
      ${UI.statTile({ label: "Sitting (awake)", value: minsFmt(F.today_sedentary_min), inset: true })}
      ${UI.statTile({ label: "7-day average", value: minsFmt(F.avg7_feet_min), inset: true })}</div>`;
    const I = F.insights || {}, Z = I.zones_last7 || {};
    foot.innerHTML = `<div class="ui-stack" style="gap:var(--sp-4);margin-top:var(--sp-2)">
      ${Z.workout_pct != null ? `<div class="ui-stack" style="gap:var(--sp-2)"><p class="ui-overline">Awake time, last 7 days</p>${_zoneSplit([{ label: "Workouts", pct: Z.workout_pct, color: "--cat-1" }, { label: "Walking", pct: Z.walking_pct, color: "--cat-4" }, { label: "Sitting", pct: Z.sedentary_pct, color: "--text-3" }])}</div>` : ""}
      ${I.weekday_avg_feet_min != null && I.weekend_avg_feet_min != null ? `<div class="cx-stats">${UI.statTile({ label: "Weekday average", value: minsFmt(I.weekday_avg_feet_min), inset: true, foot: `walk ${minsFmt(I.weekday_avg_walking_min)} · sit ${minsFmt(I.weekday_avg_sedentary_min)}` })}${UI.statTile({ label: "Weekend average", value: minsFmt(I.weekend_avg_feet_min), inset: true, foot: `walk ${minsFmt(I.weekend_avg_walking_min)} · sit ${minsFmt(I.weekend_avg_sedentary_min)}` })}</div>` : ""}
      ${(I.high_days || []).length ? UI.insightCallout({ variant: "inline", tone: "info", label: "Big days on your feet (last 14)", body: (I.high_days || []).slice(-4).reverse().map((d) => `${shortDate(d.date)}: **${minsFmt(d.feet_min)}** (+${d.delta_vs_avg} min vs average${d.steps ? `, ${d.steps.toLocaleString()} steps` : ""})`) }) : ""}
      ${(I.tips || []).length ? UI.insightCallout({ variant: "inline", tone: "coach", label: "For recovery", body: I.tips }) : ""}
      ${F.read ? `<p class="ui-small ui-muted">${escapeHtml(F.read)}</p>` : ""}</div>`;
    _drawFeetChart(F);
  }
  function _drawFeetChart(F) {
    const daily = (S._tof || "daily") === "daily";
    const rows = daily ? (F.daily || []).slice(-14) : (F.weekly || []).slice(-8);
    uiChart("chFeet", {
      labels: rows.map((r) => (daily ? r.date : r.week)), xFormat: (l, i, w) => (daily ? dLbl(l, i, w) : w === "tick" ? shortDate(l) : `Week of ${shortDate(l)}`), height: 200, takeaway: "",
      y: { beginAtZero: true, fmt: (v) => (v >= 60 ? `${Math.round(v / 60)}h` : `${v}m`) },
      series: [
        { label: "Workouts", unit: "min", type: "bar", stack: "s", data: rows.map((r) => (daily ? r.workout_min : r.avg_workout_min)), color: "--cat-1", fmt: (v) => `${v}` },
        { label: "Walking", unit: "min", type: "bar", stack: "s", data: rows.map((r) => (daily ? r.walking_min : r.avg_walking_min)), color: "--cat-4", fmt: (v) => `${v}` },
        { label: "Sitting", unit: "min", type: "bar", stack: "s", data: rows.map((r) => (daily ? r.sedentary_min : r.avg_sedentary_min)), color: "--text-3", fmt: (v) => `${v}` },
      ],
      tooltipExtra: (i) => { const r = rows[i] || {}; const st = daily ? r.steps : r.avg_steps; return st != null ? `${st.toLocaleString()} steps${daily ? "" : " a day"}` : ""; },
    });
  }

  // Non-activity stress: rest / low / medium / high minutes (Garmin already excludes workouts).
  const STRESS_TIERS = [["rest", "Rest", "--load-optimal"], ["low", "Low", "--load-under"], ["med", "Medium", "--load-high"], ["high", "High", "--load-over"]];
  function _renderStress(SS) {
    const body = $("#stressBody"), foot = $("#stressFoot");
    if (!body || !foot) return;
    if (!SS || !(SS.daily || []).length) {
      body.innerHTML = UI.emptyState({ icon: "activity", compact: true, title: "No stress data yet" });
      foot.innerHTML = ""; $("#chStress").innerHTML = ""; $("#stressMode").innerHTML = ""; return;
    }
    S._stMode = S._stMode || "daily";
    _modeSwitch("stressMode", "st", S._stMode);
    const card = $("#stressCard");
    if (!card.dataset.wired) { card.dataset.wired = "1"; card.addEventListener("ui:change", (e) => { if (e.detail.name === "st") { S._stMode = e.detail.value; _drawStressChart(S.recovery.stress); } }); }
    const na = SS.today_non_activity_avg;
    body.innerHTML = `<div class="cx-stats">
      ${UI.statTile({ label: "Today, non-activity", value: na ?? "–", unit: na != null ? "/100" : "", inset: true, tone: na == null ? null : na < 45 ? "good" : na < 60 ? "warn" : "bad" })}
      ${UI.statTile({ label: "Full-day average", value: SS.today_avg_stress ?? "–", inset: true })}
      ${UI.statTile({ label: "7-day baseline", value: SS.avg7_non_activity ?? "–", inset: true })}
      ${UI.statTile({ label: "High-stress time", value: minsFmt(SS.today_high_min), inset: true })}
      ${UI.statTile({ label: "Restful time", value: minsFmt(SS.today_rest_min), inset: true })}</div>`;
    const I = SS.insights || {}, Z = I.zones_last7 || {};
    foot.innerHTML = `<div class="ui-stack" style="gap:var(--sp-4);margin-top:var(--sp-2)">
      ${Z.rest_pct != null ? `<div class="ui-stack" style="gap:var(--sp-2)"><p class="ui-overline">Non-activity time, last 7 days</p>${_zoneSplit([{ label: "Rest", pct: Z.rest_pct, color: "--load-optimal" }, { label: "Low", pct: Z.low_pct, color: "--load-under" }, { label: "Medium", pct: Z.medium_pct, color: "--load-high" }, { label: "High", pct: Z.high_pct, color: "--load-over" }])}</div>` : ""}
      ${I.weekday_avg_non_activity != null && I.weekend_avg_non_activity != null ? `<div class="cx-stats">${UI.statTile({ label: "Weekday average", value: I.weekday_avg_non_activity, unit: "/100", inset: true, foot: `high ${minsFmt(I.weekday_avg_high_min)} · rest ${minsFmt(I.weekday_avg_rest_min)}` })}${UI.statTile({ label: "Weekend average", value: I.weekend_avg_non_activity, unit: "/100", inset: true, foot: `high ${minsFmt(I.weekend_avg_high_min)} · rest ${minsFmt(I.weekend_avg_rest_min)}` })}</div>` : ""}
      ${(I.high_days || []).length ? UI.insightCallout({ variant: "inline", tone: "warn", label: "Stressful days (last 14)", body: (I.high_days || []).slice(-4).reverse().map((d) => `${shortDate(d.date)}: **${d.non_activity_avg}/100** (+${d.delta_vs_avg} vs baseline${d.high_min ? `, ${minsFmt(d.high_min)} high` : ""})`) }) : ""}
      ${(I.tips || []).length ? UI.insightCallout({ variant: "inline", tone: "coach", label: "For recovery", body: I.tips }) : ""}
      ${SS.read ? `<p class="ui-small ui-muted">${escapeHtml(SS.read)}</p>` : ""}</div>`;
    _drawStressChart(SS);
  }
  function _drawStressChart(SS) {
    const daily = (S._stMode || "daily") === "daily";
    const rows = daily ? (SS.daily || []).slice(-14) : (SS.weekly || []).slice(-8);
    const key = (t) => (daily ? `${t}_min` : `avg_${t}_min`);
    uiChart("chStress", {
      labels: rows.map((r) => (daily ? r.date : r.week)), xFormat: (l, i, w) => (daily ? dLbl(l, i, w) : w === "tick" ? shortDate(l) : `Week of ${shortDate(l)}`), height: 200, takeaway: "",
      y: { beginAtZero: true, fmt: (v) => (v >= 60 ? `${Math.round(v / 60)}h` : `${v}m`) },
      series: STRESS_TIERS.map(([t, label, color]) => ({ label, unit: "min", type: "bar", stack: "s", data: rows.map((r) => r[key(t)]), color, fmt: (v) => `${v}` })),
      tooltipExtra: (i) => { const r = rows[i] || {}; const na = daily ? r.non_activity_avg : r.avg_non_activity; return na != null ? `Non-activity average ${na}/100` : ""; },
    });
  }

  // ================= calendar =================
  // S.cal mirrors the grid's view/anchor so other code (Coach week strip, tile
  // links) can move it: set S.cal.anchor, then call buildCalendar().
  function wireCalendarControls() {
    S.cal = { mode: "month", anchor: null };
    wireGarminSync();
  }
  const todayDate = () => {
    const iso = (S.dash && S.dash.countdown && S.dash.countdown.today) || new Date().toISOString().slice(0, 10);
    return new Date(iso + "T00:00:00");
  };
  const mondayOf = (d) => { const x = new Date(d); const w = (x.getDay() + 6) % 7; x.setDate(x.getDate() - w); return x; };
  const iso = (d) => d.toISOString().slice(0, 10);

  // flatten plan days into a date->day map
  function planDayMap() {
    const m = {};
    ((S.plan && S.plan.weeks) || []).forEach((w) => (w.days || []).forEach((d) => (m[d.date] = d)));
    return m;
  }

  // date -> the run logged that day (longest, if several). Drives calendar
  // completion marks and the day modal's "completed / missed" banner.
  function completedRunByDate() {
    const m = {};
    ((S.snap && S.snap.activities) || []).forEach((a) => {
      if (!a.date) return;
      if (!m[a.date] || (a.distance_km || 0) > (m[a.date].distance_km || 0)) m[a.date] = a;
    });
    return m;
  }
  const isRunDay = (d) => !!d && ["easy", "quality", "long", "race"].includes(d.type);
  // ================= training log =================
  // Activity kinds take categorical slots in a fixed order (runs = slot 1, "you").
  const LOG_KIND_META = {
    run:      { color: "--cat-1", label: "Run", icon: "footprints" },
    strength: { color: "--cat-2", label: "Strength", icon: "dumbbell" },
    mobility: { color: "--cat-5", label: "Mobility", icon: "person-standing" },
    hike:     { color: "--cat-3", label: "Hike", icon: "mountain" },
    bike:     { color: "--cat-4", label: "Bike", icon: "activity" },
    swim:     { color: "--cat-6", label: "Swim", icon: "droplets" },
    workout:  { color: "--text-3", label: "Workout", icon: "activity" },
    other:    { color: "--text-3", label: "Other", icon: "activity" },
  };
  const LOG_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  /** Circle diameter in px: runs scale by km, everything else by minutes. CSS caps it at the cell width. */
  function _logBubbleSize(a) {
    if (a.kind === "run") return Math.max(30, Math.min(84, Math.round(28 + (a.distance_km || 0) * 2.4)));
    return Math.max(24, Math.min(60, Math.round(22 + (a.duration_min || 0) * 0.45)));
  }
  function _weekRangeLabel(ws, we) {
    const a = new Date(ws + "T00:00:00"), b = new Date(we + "T00:00:00");
    const opts = { month: "short", day: "numeric" };
    return `${a.toLocaleString(undefined, opts)} – ${a.getMonth() === b.getMonth() ? b.getDate() : b.toLocaleString(undefined, opts)}`;
  }
  async function renderLog() {
    const grid = $("#logGrid");
    if (!grid) return;
    try {
      S.log = await api("/api/log");
    } catch (e) {
      grid.innerHTML = UI.card({ body: UI.errorState({ title: "Couldn't load the training log", body: String(e.message || e) }) });
      grid.querySelector("[data-action=retry]")?.addEventListener("click", renderLog);
      return;
    }
    S.logFilter = S.logFilter || "all";
    $("#logFilter").innerHTML = UI.segmented({ label: "Show", name: "log-kind", value: S.logFilter,
      options: [["all", "All"], ["run", "Runs", "footprints"], ["strength", "Strength", "dumbbell"], ["mobility", "Mobility", "person-standing"]] });
    const filt = $("#logFilter");
    if (!filt.dataset.wired) { filt.dataset.wired = "1"; filt.addEventListener("ui:change", (e) => { S.logFilter = e.detail.value; _renderLogGrid(); }); }
    $("#logLegend").innerHTML = ["run", "strength", "mobility", "hike"].map((k) => `<span><i style="--sw:var(${LOG_KIND_META[k].color});border-radius:50%"></i>${LOG_KIND_META[k].label}</span>`).join("")
      + `<span>${UI.icon("flag", { size: 14 })}Race</span><span>${UI.icon("trophy", { size: 14 })}Personal best</span>`;
    _renderLogGrid();
    _renderLogNav();
  }
  function _fmtHm(s) {
    if (!s) return null;
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? `${h}:${String(m).padStart(2, "0")}` : `${m}m`;
  }
  function _renderLogGrid() {
    const grid = $("#logGrid");
    if (!grid || !S.log) return;
    const filter = S.logFilter || "all";
    const weeks = S.log.weeks || [];
    if (!weeks.length) { grid.innerHTML = UI.card({ body: UI.emptyState({ icon: "list", title: "No activities logged yet" }) }); return; }
    const todayIso = iso(todayDate());
    let html = "", prevMonKey = null;
    for (const wk of weeks) {
      const monKey = wk.week_start.slice(0, 7);
      if (monKey !== prevMonKey) {
        html += `<h2 class="lg-month" id="log-mon-${monKey}">${new Date(wk.week_start + "T00:00:00").toLocaleString(undefined, { month: "long", year: "numeric" })}</h2>`;
        prevMonKey = monKey;
      }
      const days = wk.days.map((d) => ({ ...d, activities: (d.activities || []).filter((a) => filter === "all" || a.kind === filter) }));
      const runKm = days.reduce((s, d) => s + d.activities.filter((a) => a.kind === "run").reduce((ss, a) => ss + (a.distance_km || 0), 0), 0);
      const wkKm = filter === "run" ? runKm : filter === "all" ? wk.total_km : null;
      const stats = [];
      if (wk.total_time_s) stats.push(["Time", _fmtHm(wk.total_time_s)]);
      if (wk.elev_gain_m) stats.push(["Elevation", `${wk.elev_gain_m} m`]);
      if (wk.avg_run_pace) stats.push(["Avg pace", `${wk.avg_run_pace}/km`]);
      if (wk.longest_run) stats.push(["Longest", `${wk.longest_run.distance_km} km${wk.longest_run.pace ? " @ " + wk.longest_run.pace : ""}`]);
      if (wk.training_load) stats.push(["Load", String(wk.training_load)]);
      const zs = wk.hr_zones_s || {};
      const zparts = [1, 2, 3, 4, 5].map((n) => ({ zone: n, value: zs["z" + n] || 0 }));
      html += `<section class="lg-week" data-week-start="${wk.week_start}" aria-label="Week of ${escapeHtml(_weekRangeLabel(wk.week_start, wk.week_end))}">
        <div class="lg-sum">
          <span class="ui-caption">${escapeHtml(_weekRangeLabel(wk.week_start, wk.week_end))}</span>
          ${wkKm != null ? `<span class="ui-stat-value">${wkKm.toFixed(1)}<small>km</small></span>` : `<span class="ui-stat-value">${days.reduce((s, d) => s + d.activities.length, 0)}<small>sessions</small></span>`}
          ${filter === "all" && stats.length ? `<dl>${stats.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>` : ""}
          ${filter === "all" && zparts.some((p) => p.value) ? UI.zoneBar(zparts, "Heart-rate zones this week") : ""}
        </div>
        <div class="lg-days">${LOG_DAY_NAMES.map((n) => `<span class="lg-dow" aria-hidden="true">${n}</span>`).join("")}${days.map((d) => _renderLogDayCell(d, todayIso)).join("")}</div>
      </section>`;
    }
    grid.innerHTML = html;
    // Run circles open the run breakdown.
    grid.querySelectorAll("button.lg-bubble[data-date]").forEach((b) => b.addEventListener("click", () => openRunByDate(b.dataset.date)));
  }
  function _dayFlag(day) {
    // Priority: race > personal best > unplanned run > missed. One flag per day.
    if ((day.activities || []).some((a) => a.is_race)) return { icon: "flag", tip: "Race" };
    if ((day.activities || []).some((a) => a.is_pr)) return { icon: "trophy", tip: "Personal best" };
    if (day.unplanned_run) return { icon: "plus", tip: "Unplanned run" };
    if (day.missed) return { icon: "x", tip: `Missed ${day.planned_type || "run"}`, missed: true };
    return null;
  }
  function _renderLogDayCell(day, todayIso) {
    const acts = day.activities || [];
    const isToday = day.date === todayIso, isFuture = day.date > todayIso;
    const f = _dayFlag(day);
    const flag = f ? `<span class="lg-flag${f.missed ? " is-missed" : ""}" title="${escapeHtml(f.tip)}">${UI.icon(f.icon, { size: 14, label: f.tip })}</span>` : "";
    const cls = `lg-day${isToday ? " is-today" : ""}${day.missed ? " is-missed" : ""}`;
    if (!acts.length) {
      const lbl = isFuture ? "" : day.missed ? `Missed ${day.planned_type || "run"}` : "Rest";
      return `<div class="${cls}">${flag}<span class="lg-rest">${escapeHtml(lbl)}</span></div>`;
    }
    const bubbles = acts.map((a) => {
      const meta = LOG_KIND_META[a.kind] || LOG_KIND_META.other;
      const label = a.kind === "run" && a.distance_km ? a.distance_km.toFixed(1) : `${a.duration_min || 0}m`;
      const desc = [a.name || meta.label, a.kind === "run" && a.distance_km ? `${a.distance_km.toFixed(1)} km` : null,
        a.kind === "run" && a.avg_pace ? `${a.avg_pace}/km` : null, a.kind !== "run" && a.duration_min ? `${a.duration_min} min` : null,
        a.avg_hr ? `HR ${a.avg_hr}` : null, a.is_pr ? "personal best" : null, a.is_race ? "race" : null].filter(Boolean).join(", ");
      const ring = a.is_race ? " is-race" : a.is_pr ? " is-pr" : "";
      const style = `--s:${_logBubbleSize(a)}px;--kc:var(${meta.color})`;
      return a.kind === "run"
        ? `<button type="button" class="lg-bubble${ring}" data-date="${day.date}" style="${style}" aria-label="${escapeHtml(desc)}. Open run." title="${escapeHtml(desc)}">${escapeHtml(label)}</button>`
        : `<span class="lg-bubble${ring}" style="${style}" role="img" aria-label="${escapeHtml(desc)}" title="${escapeHtml(desc)}">${escapeHtml(label)}</span>`;
    }).join("");
    const names = acts.map((a) => `<span class="lg-name">${escapeHtml(a.name || (LOG_KIND_META[a.kind] || LOG_KIND_META.other).label)}</span>`).join("");
    return `<div class="${cls}">${flag}<div class="lg-bubbles">${bubbles}</div>${names}</div>`;
  }
  // "Jump to month" select (works at every width; replaces the desktop-only side rail).
  function _renderLogNav() {
    const sel = $("#logJump");
    if (!sel || !S.log) return;
    const months = [...new Set((S.log.weeks || []).map((wk) => wk.week_start.slice(0, 7)))];
    sel.innerHTML = `<option value="">Jump to month…</option>` + months.map((m) =>
      `<option value="${m}">${new Date(m + "-01T00:00:00").toLocaleString(undefined, { month: "long", year: "numeric" })}</option>`).join("");
    if (!sel.dataset.wired) {
      sel.dataset.wired = "1";
      sel.addEventListener("change", () => {
        const target = document.getElementById("log-mon-" + sel.value);
        if (target) target.scrollIntoView({ behavior: UI.prefersReducedMotion() ? "auto" : "smooth", block: "start" });
      });
    }
  }

  // Plan + logged runs -> CalendarGrid's day shape.
  function calDays() {
    const map = planDayMap(), out = {}, t = iso(todayDate());
    const keys = new Set([...Object.keys(map), ...Object.keys(S.done || {})]);
    keys.forEach((k) => {
      const d = map[k], done = S.done && S.done[k];
      if (!d && !done) return;
      const state = done ? (isRunDay(d) ? "done" : "unplanned")
        : d.type === "rest" ? "rest"
        : isRunDay(d) && k < t ? "missed" : "planned";
      out[k] = {
        type: d ? d.type : "easy",
        title: d ? d.title || (d.type === "rest" ? "Rest / mobility" : "") : done.name,
        km: d && d.distance_km ? d.distance_km : null,
        pace: d && !d.easy_hr ? paceLine(d) || null : null,
        race: !!(d && d.type === "race"), strength: !!(d && d.strength), state,
        done: done ? { km: done.distance_km != null ? +done.distance_km.toFixed(1) : null, pace: done.avg_pace_s_per_km ? paceStr(done.avg_pace_s_per_km) : null, name: done.name } : null,
      };
    });
    return out;
  }

  function buildCalendar() {
    if (!S.cal) return;
    if (!S.cal.anchor) S.cal.anchor = todayDate();
    const mount = $("#calBody"); if (!mount) return;
    const cfg = { days: calDays(), today: iso(todayDate()), anchor: new Date(S.cal.anchor) };
    if (S.calGrid && mount.querySelector(".ui-cal")) { S.calGrid.set(cfg); return; }
    S.calGrid = UI.CalendarGrid(mount, {
      ...cfg,
      view: S.cal.mode === "week" ? "week" : undefined,     // undefined: month, or agenda on narrow screens
      onOpen: (k) => openDay(planDayMap()[k] || { date: k, type: "rest", title: "Off-plan day", distance_km: 0 }),
      onRender: ({ view, anchor }) => { S.cal.mode = view; S.cal.anchor = anchor; renderCalStats(); },
    });
  }

  // ---- period stats (whatever the calendar is showing) ----
  const _isRunDay = (d) => ["easy", "quality", "long", "race"].includes(d.type) && d.distance_km;
  function _periodStats(days) {
    const counts = { easy: 0, quality: 0, long: 0, race: 0, strength: 0, rest: 0 };
    let planned = 0, actual = 0, adhDone = 0, adhPlanned = 0;
    const t = iso(todayDate());
    days.forEach((d) => {
      if (counts[d.type] != null) counts[d.type]++;
      if (d.strength) counts.strength++;
      planned += d.distance_km || 0;
      if (_isRunDay(d) && d.date <= t) {
        adhPlanned++;
        if (S.done && S.done[d.date]) { adhDone++; actual += S.done[d.date].distance_km || 0; }
      }
    });
    return { counts, total_runs: counts.easy + counts.quality + counts.long + counts.race,
      planned: Math.round(planned), actual: Math.round(actual),
      adh: { done: adhDone, planned: adhPlanned, pct: adhPlanned ? Math.round(100 * adhDone / adhPlanned) : null } };
  }
  const _daysInMonth = (map, y, m) => Object.values(map).filter((d) => {
    const x = new Date(d.date + "T00:00:00"); return x.getFullYear() === y && x.getMonth() === m; });

  function renderCalStats() {
    const host = $("#calStats"); if (!host) return;
    const map = planDayMap();
    const a = S.cal.anchor || todayDate();
    const span = (n) => { const start = mondayOf(a), out = []; for (let i = 0; i < n; i++) { const d = new Date(start); d.setDate(d.getDate() + i); if (map[iso(d)]) out.push(map[iso(d)]); } return out; };
    const mode = S.cal.mode || "month";
    const period = mode === "week" ? "this week" : mode === "agenda" ? "these four weeks" : a.toLocaleDateString(undefined, { month: "long" });
    const cur = _periodStats(mode === "week" ? span(7) : mode === "agenda" ? span(28) : _daysInMonth(map, a.getFullYear(), a.getMonth()));
    let adhFoot = cur.adh.planned ? `${cur.adh.done} of ${cur.adh.planned} run days done` : "Nothing due yet";
    if (mode === "month") {
      const pm = (a.getMonth() + 11) % 12, py = a.getMonth() === 0 ? a.getFullYear() - 1 : a.getFullYear();
      const prev = _periodStats(_daysInMonth(map, py, pm));
      if (cur.adh.pct != null && prev.adh.pct != null) adhFoot += ` · ${UI.fmt.signed(cur.adh.pct - prev.adh.pct)} pts vs last month`;
    }
    const adhTone = cur.adh.pct == null ? null : cur.adh.pct >= 90 ? "good" : cur.adh.pct >= 75 ? "warn" : "bad";
    const types = [["easy", "Easy"], ["quality", "Quality"], ["long", "Long"], ["race", "Race"], ["strength", "Strength"], ["rest", "Rest"]]
      .filter(([k]) => cur.counts[k]).map(([k, l]) => { const w = UI.workout(k); return UI.badge(`${cur.counts[k]} ${l}`, { icon: w.icon, outline: true }); }).join("");

    // planned volume for every month the plan spans; select a bar to jump there
    const dates = Object.keys(map).sort(), months = [];
    if (dates.length) {
      const f = new Date(dates[0] + "T00:00:00"), l = new Date(dates[dates.length - 1] + "T00:00:00");
      for (let d = new Date(f.getFullYear(), f.getMonth(), 1); d <= new Date(l.getFullYear(), l.getMonth(), 1); d.setMonth(d.getMonth() + 1)) {
        const s = _periodStats(_daysInMonth(map, d.getFullYear(), d.getMonth()));
        months.push({ y: d.getFullYear(), m: d.getMonth(), km: s.planned, done: s.actual });
      }
    }
    host.innerHTML = `<div class="pl-stats">
      <div class="ui-stack" style="gap:var(--sp-4)">
        <div class="cx-stats">
          ${UI.statTile({ label: `Runs, ${period}`, icon: "footprints", value: cur.total_runs, foot: cur.counts.strength ? `+ ${cur.counts.strength} strength` : "" })}
          ${UI.statTile({ label: "Planned volume", icon: "route", value: cur.planned, unit: "km", foot: cur.actual ? `${cur.actual} km done so far` : "" })}
          ${UI.statTile({ label: "Adherence", icon: "circle-check", value: cur.adh.pct != null ? cur.adh.pct : "–", unit: cur.adh.pct != null ? "%" : "", tone: adhTone, foot: escapeHtml(adhFoot) })}
        </div>
        ${types ? `<div class="ui-row">${types}</div>` : ""}
      </div>
      <div class="ui-card" id="calMonthsChart"></div></div>`;
    const sel = mode === "month" ? months.findIndex((o) => o.y === a.getFullYear() && o.m === a.getMonth()) : -1;
    uiChart("calMonthsChart", {
      title: "Planned volume by month", takeaway: "Select a month to jump there.", height: 150,
      labels: months.map((o) => `${o.y}-${String(o.m + 1).padStart(2, "0")}`), xLabel: "Month",
      xFormat: (l, i, w) => new Date(l + "-01T00:00:00").toLocaleDateString(undefined, w === "tick" ? { month: "short" } : { month: "long", year: "numeric" }),
      y: { beginAtZero: true, ticks: 3 },
      series: [{ label: "Planned", unit: "km", type: "bar", legend: false, data: months.map((o) => o.km), colorBy: (v, i) => (i === sel ? "--accent" : "--cat-2"), fmt: (v) => `${v}` }],
      tooltipExtra: (i) => (months[i] && months[i].done ? `${months[i].done} km done` : ""),
      // defer: jumping re-renders this chart, which must not happen inside its own click handler
      onPoint: (i) => { const o = months[i]; if (!o) return; setTimeout(() => { S.cal.anchor = new Date(o.y, o.m, 1); if (S.calGrid) S.calGrid.set({ view: "month", anchor: new Date(o.y, o.m, 1) }); }); },
      table: false,
    });
  }

  function paceLine(day) {
    if (day && day.easy_hr) {
      const h = day.easy_hr;
      return `${h.low}–${h.high} bpm (${h.zone || 'Z2'})`;
    }
    const p = day && day.pace;
    if (!p) return "";
    return p.target || (p.low ? `${p.low}–${p.high}` : "");
  }

  // ================= day modal =================
  // Dialogs are native <dialog>s (Esc, focus trap, focus return are built in);
  // only the non-modal chat panel needs its own Esc handling.
  function wireModal() {
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#chatDock").hidden && !document.querySelector("dialog[open]")) closeChat(); });
  }
  let _daySheet = null;
  /** Open (or reuse) the day/run detail sheet; returns its body, which keeps the legacy #dayModalBody id. */
  function daySheet(title, sub) {
    if (_daySheet && _daySheet.el.open) _daySheet.close();
    _daySheet = UI.openDialog({ kind: "sheet", wide: true, title: title || "Details", sub, onClose: () => { _daySheet = null; } });
    _daySheet.body.id = "dayModalBody";
    return _daySheet.body;
  }
  const closeModal = () => { if (_daySheet) _daySheet.close(); };

  // Day sheet: the full prescription for one plan day, plus — for days already
  // past — that morning's readings, the coach's call, and how it felt.
  function openDay(day) {
    const w = UI.workout(day.type);
    const pace = paceLine(day);
    const meta = [];
    if (day.distance_km) meta.push(`${day.distance_km} km`);
    if (pace) meta.push(pace.includes("bpm") ? pace : `${pace}/km`);
    if (!day.easy_hr && day.pace && day.pace.zone) meta.push(day.pace.zone);

    // Week context: the role this day plays in the block.
    const wk = ((S.plan && S.plan.weeks) || []).find((x) => (x.days || []).some((d) => d.date === day.date));
    const done = S.done && S.done[day.date];
    const unplanned = done && !isRunDay(day);
    const isDouble = !!day.double && !unplanned;
    const title = unplanned ? done.name || "Unplanned run" : isDouble ? "Double day" : day.title || w.label;
    const b = daySheet(title, `${UI.fmt.date(day.date, { weekday: "long", month: "long", day: "numeric" })}${meta.length && !unplanned ? ` · ${meta.join(" · ")}` : ""}`);

    let status = "";
    if (done) {
      const bits = [`${done.distance_km} km`, `${paceStr(done.avg_pace_s_per_km)}/km`];
      if (done.avg_hr) bits.push(`${done.avg_hr} bpm`);
      status = UI.insightCallout({ tone: "good", label: unplanned ? "Unplanned run" : "Completed", title: bits.join(" · "),
        actions: done.id ? UI.button({ label: "Open run breakdown", variant: "secondary", size: "sm", icon: "activity", attrs: { "data-open-run": day.date } }) : "" });
    } else if (isRunDay(day) && day.date < iso(todayDate())) {
      status = UI.insightCallout({ tone: "warn", label: "Not logged", title: "No run recorded for this day." });
    }

    const bailRow = (v) => {
      const i = v.indexOf("→");
      return i === -1 ? `<li>${escapeHtml(v)}</li>` : `<li><b>${escapeHtml(v.slice(0, i).trim())}</b> → ${escapeHtml(v.slice(i + 1).trim())}</li>`;
    };
    const bail = Object.values(day.bailouts || {});
    const sec = (titleText, html, sub) => (html ? `<section class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">${escapeHtml(titleText)}</h3>${sub ? `<span class="ui-caption">${escapeHtml(sub)}</span>` : ""}</div>${html}</section>` : "");

    // Double days: AM easy + PM session as two prescription wells.
    const amPace = (S.dash && S.dash.paces && S.dash.paces[day.double_am_pace_key || "easy"]) || null;
    const amPaceStr = amPace ? (amPace.target || `${amPace.low}–${amPace.high}`) : "";
    const session = isDouble ? `<div class="ui-grid cols-2">
        ${sessionBlock({ type: "easy", title: day.double_am_title || `AM easy ${day.double_am_km} km`, distance_km: day.double_am_km, pace: amPaceStr, zone: amPace && amPace.zone, why: day.double_am_why }, "Morning")}
        ${sessionBlock({ type: day.type, title: day.pm_title || day.title || "PM session", distance_km: day.pm_km || (day.distance_km - (day.double_am_km || 0)), pace: pace && !pace.includes("bpm") ? pace : "", zone: day.pace && day.pace.zone, why: [day.reps, day.why].filter(Boolean).join(" ") }, "Afternoon or evening")}
      </div>${UI.insightCallout({ variant: "inline", tone: "info", label: "Two-a-day", body: "Keep the morning truly easy (nose-breathing), leave six or more hours between, and fuel in between." })}`
      : !unplanned && (day.reps || day.why) ? sessionBlock({ type: day.type, title: day.reps || day.title, distance_km: day.distance_km, pace: pace && !pace.includes("bpm") ? pace : "", zone: day.easy_hr ? `${day.easy_hr.low}–${day.easy_hr.high} bpm` : day.pace && day.pace.zone, why: day.why }, "Prescription") : "";

    let strength = "";
    if (day.strength) {
      const st = day.strength;
      const items = (st.exercises || []).map((e) => (e === "— then —" ? `</ul><p class="ui-overline" style="margin:var(--sp-3) 0 var(--sp-1)">Then</p><ul class="sr-list">` : `<li>${escapeHtml(e)}</li>`)).join("");
      strength = sec(`Strength${st.focus || st.phase ? `: ${st.focus || st.phase}` : ""}`,
        `${st.equipment ? `<p class="ui-small ui-muted">${UI.icon("dumbbell", { size: 16 })} ${escapeHtml(st.equipment)}</p>` : ""}<ul class="sr-list">${items}</ul>
         ${st.note ? `<p class="ui-caption">${escapeHtml(st.note)}</p>` : ""}
         ${Object.keys(day.strength_bailouts || {}).length ? UI.insightCallout({ variant: "inline", tone: "info", label: "If you need to cut it short", body: Object.values(day.strength_bailouts).map((v) => v.replace("→", ":")) }) : ""}`);
    }

    b.innerHTML = `<article class="ui-dd">
      <header class="ui-dd-head">
        <div class="ui-dd-kicker">${isDouble ? UI.badge("Two-a-day", { tone: "coach", icon: "zap" }) : `<span class="ui-zone-chip" style="--zc:${day.type === "race" ? "var(--accent)" : UI.zoneVar(w.zone)}"><i aria-hidden="true"></i>${escapeHtml(unplanned ? "Run" : w.label)}</span>`}
          ${wk ? UI.badge(`Week ${wk.week} · ${wk.phase || ""}`, { size: "sm", outline: true }) : ""}${wk && wk.target_km ? UI.badge(`${wk.target_km} km week`, { size: "sm", outline: true }) : ""}</div>
        ${wk && wk.focus ? `<p class="ui-small ui-muted">${escapeHtml(wk.focus)}</p>` : ""}
      </header>
      ${status}
      ${session}
      ${day.easy_cap ? UI.insightCallout({ tone: "warn", label: "Keep it honest", icon: "gauge", body: day.easy_cap }) : ""}
      <div id="dayContext"></div>
      ${day.phases && day.phases.length ? sec("Workout blueprint", `<div id="chPlanPhasesWrap"></div>`, "Target pace by phase; width is duration") : ""}
      ${done && done.id ? sec("Your run", `<div id="chDayStream"><span></span></div><div id="dayDrift"></div>`) : ""}
      ${bail.length ? sec("Adapt or bail out", `<ul class="sr-list">${bail.map(bailRow).join("")}</ul>`) : ""}
      ${strength}
      ${nightlyHtml(day.nightly)}
    </article>`;
    b.querySelector("[data-open-run]")?.addEventListener("click", (e) => { const d = e.currentTarget.dataset.openRun; closeModal(); setTimeout(() => openRunByDate(d), 50); });
    if (day.phases && day.phases.length) renderPlanPhases(day.phases, $("#chPlanPhasesWrap"));
    if (done && done.id) { const m = $("#chDayStream"); m.className = "rs-charts"; renderRunTrace(done.id, m, $("#dayDrift"), { compact: true }); }
    if (day.date <= iso(todayDate())) loadDayContext(day.date);
  }

  // Nightly mobility + core — every day; core is constant (McGill Big 3), mobility rotates by weekday.
  function nightlyHtml(n) {
    if (!n) return "";
    const col = (label, items) => `<div class="ui-stack" style="gap:var(--sp-2)"><p class="ui-overline">${escapeHtml(label)}</p><ul class="sr-list">${(items || []).map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul></div>`;
    return `<section class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">${UI.icon("moon", { size: 18 })} ${escapeHtml(n.title || "Nightly mobility and core")}</h3></div>
      <div class="ui-grid cols-2">${col("Core, every night", n.core)}${col("Mobility, tonight's focus", n.mobility)}</div>
      ${n.note ? `<p class="ui-caption">${escapeHtml(n.note)}</p>` : ""}</section>`;
  }

  // Past days: that morning's sleep + recovery, the coach's call, and post-run feel tags.
  async function loadDayContext(date) {
    const mount = document.getElementById("dayContext");
    if (!mount) return;
    mount.innerHTML = UI.skeleton.lines(2);
    try {
      const d = await api(`/api/day/${date}`);
      if (mount) mount.innerHTML = dayContextHtml(d);
    } catch (e) { if (mount) mount.innerHTML = ""; }
  }
  function dayContextHtml(d) {
    if (!d || !d.has_data) return "";
    let html = "";
    const s = d.sleep, rec = d.recovery || {}, rd = rec.readiness, hv = rec.hrv;
    if (s || rd || hv) {
      const tiles = [];
      if (s) tiles.push(UI.statTile({ label: "Sleep", value: s.hours, unit: "h", inset: true, foot: s.score != null ? `score ${s.score}` : "" }));
      if (rd) tiles.push(UI.statTile({ label: "Readiness", value: rd.score, inset: true, foot: rd.level ? escapeHtml(cap(rd.level.toLowerCase())) : "" }));
      if (hv) tiles.push(UI.statTile({ label: "HRV", value: hv.value, unit: "ms", inset: true, foot: hv.low && hv.high ? (hv.state === "below" ? "below range" : hv.state === "above" ? "above range" : "in range") : escapeHtml(hv.status || "") }));
      if (rec.rhr) tiles.push(UI.statTile({ label: "Resting HR", value: rec.rhr, unit: "bpm", inset: true }));
      html += `<section class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">That morning</h3><span class="ui-caption">Sleep and recovery</span></div>
        <div class="cx-stats">${tiles.join("")}</div>
        ${s ? `<p class="ui-caption">Deep ${s.deep_min} min · REM ${s.rem_min} min · Light ${s.light_min} min · Awake ${s.awake_min} min</p>` : ""}
        ${s && s.feedback ? `<p class="ui-small ui-muted">${escapeHtml(s.feedback)}</p>` : ""}</section>`;
    }
    const cs = d.coach_said;
    if (cs && cs.verdict && cs.verdict !== "grey") {
      html += UI.insightCallout({ tone: (VERDICT[cs.verdict] || VERDICT.grey).tone, label: "What the coach called it", title: cs.headline, body: cs.recommendation || "" });
    }
    if (d.run_tags && d.run_tags.length) {
      const rows = d.run_tags.map((t) => `<div class="ui-row">${d.run_tags.length > 1 ? `<span class="ui-caption">${escapeHtml(t.name || "Run")}</span>` : ""}
        ${t.rpe ? UI.badge(`Effort: ${t.rpe.label} (${t.rpe.score10}/10)`, { tone: t.rpe.tone }) : ""}${t.feel ? UI.badge(`Felt: ${t.feel.label}`, { tone: t.feel.tone }) : ""}</div>`).join("");
      html += `<section class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">How it felt</h3><span class="ui-caption">Your post-run tags on Garmin</span></div>${rows}</section>`;
    }
    return html;
  }
  const cap = (s) => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;

  // Workout blueprint: stepped blocks of target pace on a real time axis
  // (width = duration, height = pace, color = intensity zone). Bars of unequal
  // width aren't something the shared chart does, so this one stays custom —
  // but it takes the theme's colors and ships a legend and a table.
  function renderPlanPhases(phases, mount) {
    if (!phases || !phases.length || !mount) return;
    const zc = (z) => UI.token(`--z${z}`) || C.line;
    let cum = 0;
    const blocks = phases.map((ph) => { const b = { x0: cum, x1: cum + ph.secs, pace: ph.pace_s, zone: ph.zone, label: ph.label, secs: ph.secs, km: ph.km }; cum += ph.secs; return b; });
    const total = cum;
    const fastest = Math.min(...phases.map((p) => p.pace_s)), slowest = Math.max(...phases.map((p) => p.pace_s));
    const yMax = slowest + 20, yMin = Math.max(120, fastest - 20);   // padded, reversed (faster up)
    const zones = [...new Set(phases.map((p) => p.zone).filter(Boolean))].sort();
    const names = { 1: "Recovery", 2: "Easy", 3: "Steady", 4: "Threshold", 5: "VO₂max" };
    mount.innerHTML = `<div class="ui-chart-canvas" style="--chart-h:170px"><canvas id="chPlanPhases" role="img" aria-label="Workout blueprint: ${escapeHtml(phases.map((p) => `${p.label} ${clock(p.secs)} at ${paceStr(p.pace_s)} per km`).join("; "))}"></canvas></div>
      <div class="ui-chart-foot"><ul class="ui-legend">${zones.map((z) => `<li><i class="bar" style="--sw:var(--z${z})"></i>Z${z} ${names[z] || ""}</li>`).join("")}</ul>
      <details class="ui-chart-data"><summary>${UI.icon("table-2", { size: 14 })}View as table</summary>${UI.table({ caption: "Workout phases", dense: true, rowHeaders: true,
        cols: [{ key: "label", label: "Phase" }, { key: "secs", label: "Time", align: "r", num: true, fmt: (v) => clock(v) }, { key: "pace_s", label: "Pace", align: "r", num: true, fmt: (v) => `${paceStr(v)}/km` },
          { key: "km", label: "Distance", align: "r", num: true, fmt: (v) => (v ? `${v} km` : "–") }, { key: "zone", label: "Zone", fmt: (v) => (v ? UI.zoneChip(v) : "–") }], rows: phases })}</details></div>`;
    const rectPlugin = {
      id: "phaseRects",
      beforeDatasetsDraw(chart) {
        const { ctx, scales: { x, y } } = chart;
        ctx.save();
        const yB = y.getPixelForValue(yMax);
        blocks.forEach((b) => {
          const xL = x.getPixelForValue(b.x0), xR = x.getPixelForValue(b.x1), yT = y.getPixelForValue(b.pace);
          ctx.fillStyle = zc(b.zone);
          ctx.fillRect(xL, yT, Math.max(1.5, xR - xL - 2), yB - yT);   // 2px gap between blocks
        });
        ctx.restore();
      },
    };
    mk("chPlanPhases", {
      type: "scatter",
      data: { datasets: [{ data: blocks.map((b) => ({ x: (b.x0 + b.x1) / 2, y: b.pace })), pointRadius: 0, pointHoverRadius: 0, showLine: false }] },
      options: baseOpts({
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          title: (c) => blocks[c[0].dataIndex].label,
          label: (c) => { const b = blocks[c.dataIndex]; return `${paceStr(b.pace)}/km · ${clock(b.secs)}${b.km ? " · " + b.km + " km" : ""}`; } } } },
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales: {
          x: { type: "linear", min: 0, max: total, grid: { display: false }, border: { color: UI.token("--viz-axis") },
            ticks: { color: C.tick, maxTicksLimit: 8, callback: (v) => clock(v) } },
          y: { reverse: true, min: yMin, max: yMax, grid: { color: C.grid }, border: { display: false }, ticks: { color: C.tick, maxTicksLimit: 4, callback: (v) => paceStr(v) } },
        } }),
      plugins: [rectPlugin],
    });
  }

  // ================= refresh =================
  function wireRefresh() {
    $("#refreshBtn").addEventListener("click", async () => {
      const btn = $("#refreshBtn");
      btn.disabled = true;
      _refreshUIStart();
      try {
        // Note: /api/refresh returns 409 (already running) or 500 (Garmin error)
        // with a useful JSON body, so read the body regardless of status
        // instead of going through api(), which throws on any non-2xx.
        const resp = await fetch("/api/refresh", { method: "POST" });
        const r = await resp.json().catch(() => ({}));
        if (resp.ok && r.ok) { toast(`Refreshed — ${r.counts.runs} runs, ${r.counts.weeks} weeks.`); await loadAll(); }
        else if (resp.status === 409) toast(r.message || "Refresh already in progress — hang on…", false);
        else toast(r.error || r.message || `Refresh failed (${resp.status})`, false);
      } catch (e) { toast("Refresh failed: " + e.message, false); }
      finally { btn.disabled = false; _refreshUIStop(); }
    });
  }

  // ================= chat =================
  // In-session conversation transcript sent with every request so the coach
  // remembers the back-and-forth. Cleared when the user closes the chat.
  let chatHistory = [];
  function wireChat() {
    $("#chatFab").onclick = () => {
      const d = $("#chatDock");
      d.hidden = !d.hidden;
      document.body.classList.toggle("chat-open", !d.hidden);
      $("#chatFab").setAttribute("aria-expanded", String(!d.hidden));
      if (!d.hidden) $("#chatText").focus();
    };
    $("#chatClose").onclick = closeChat;
    $("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#chatText"); const q = t.value.trim(); if (q) { t.value = ""; sendChat(q); } });
    if (!$("#chatLog").dataset.greeted) {
      $("#chatLog").dataset.greeted = "1";
      addMsg("coach", `Hey — I'm your coach. Ask me about today's session, your readiness, whether ${goalTime()} is on track, your paces, your last run, or strength.`);
    }
  }
  const closeChat = () => {
    const hadFocus = $("#chatDock").contains(document.activeElement);
    $("#chatDock").hidden = true;
    document.body.classList.remove("chat-open");
    $("#chatFab").setAttribute("aria-expanded", "false");
    if (hadFocus) $("#chatFab").focus();   // don't strand keyboard focus on <body>
    // Reset the session transcript — new open starts a fresh conversation.
    chatHistory = [];
    const log = $("#chatLog");
    if (log) {
      log.innerHTML = "";
      delete log.dataset.greeted;
    }
  };
  function seedChatSuggestions() {
    const qs = ["What should I run today?", `Am I on track for ${goalTime()}?`, "How did my last run go?", "What are my paces?", "Strength this week?"];
    $("#chatSuggest").innerHTML = "";
    qs.forEach((q) => { const b = el("button", "suggest", escapeHtml(q)); b.onclick = () => sendChat(q); $("#chatSuggest").appendChild(b); });
  }
  function addMsg(who, text) {
    const log = $("#chatLog");
    const m = el("div", "msg msg-" + who, mdLite(text));
    log.appendChild(m); log.scrollTop = log.scrollHeight; return m;
  }
  async function sendChat(q, confirmed) {
    if ($("#chatDock").hidden) { $("#chatDock").hidden = false; document.body.classList.add("chat-open"); $("#chatFab").setAttribute("aria-expanded", "true"); }
    if (!confirmed) addMsg("me", q);
    const pending = addMsg("coach", "");
    pending.innerHTML = `<span class="thinking"><span class="th-lbl">Coach is thinking</span><span class="th-dots"><i></i><i></i><i></i></span></span>`;
    // History is the transcript BEFORE this turn. Confirm-button clicks don't
    // add a visible user bubble, so also skip pushing them into history.
    const historyToSend = chatHistory.slice(-20);
    if (!confirmed) chatHistory.push({ role: "user", content: q });
    try {
      const body = { message: q, history: historyToSend };
      if (confirmed) body.confirmed = confirmed;
      const r = await api("/api/coach/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      renderChatReply(pending, r);
      const replyText = _chatReplyToText(r);
      if (replyText) chatHistory.push({ role: "assistant", content: replyText });
    } catch (e) { pending.innerHTML = mdLite("Sorry — I couldn't reach the coach service."); }
  }

  function _chatReplyToText(r) {
    if (!r) return "";
    if (r.reply) return String(r.reply);
    const parts = [];
    if (r.title) parts.push(String(r.title));
    if (r.body) parts.push(String(r.body));
    if (Array.isArray(r.fields)) {
      for (const f of r.fields) if (Array.isArray(f) && f.length >= 2) parts.push(`${f[0]}: ${f[1]}`);
    }
    return parts.join("\n").trim();
  }

  function renderChatReply(el, r) {
    // legacy: {reply: str}
    if (r && r.reply) { el.innerHTML = mdLite(r.reply); $("#chatLog").scrollTop = $("#chatLog").scrollHeight; return; }
    const kind = r && r.kind;
    const tone = r && r.tone;
    if (kind === "card") {
      const fields = (r.fields || []).map((f) => `<div class="cc-fld"><span class="cc-lbl">${escapeHtml(f[0])}</span><span class="cc-val">${escapeHtml(String(f[1]))}</span></div>`).join("");
      el.innerHTML = `<div class="cc cc-${tone||'neutral'}">
        <div class="cc-h">${escapeHtml(r.title||'')}</div>
        ${r.body ? `<div class="cc-body">${mdLite(r.body)}</div>` : ""}
        ${fields ? `<div class="cc-fields">${fields}</div>` : ""}
      </div>`;
    } else if (kind === "confirm") {
      el.innerHTML = `<div class="cc cc-warn">
        <div class="cc-h">${escapeHtml(r.title||'Confirm')}</div>
        <div class="cc-body">${mdLite(r.body||'')}</div>
        <div class="cc-actions">
          <button class="ui-btn primary sm cc-yes" type="button">Confirm</button>
          <button class="ui-btn ghost sm cc-no" type="button">Cancel</button>
        </div>
      </div>`;
      el.querySelector('.cc-yes').onclick = () => sendChat('(confirmed)', r.confirm);
      el.querySelector('.cc-no').onclick = () => { el.innerHTML = mdLite('_Cancelled._'); };
    } else if (kind === "action") {
      el.innerHTML = `<div class="cc cc-good"><div class="cc-h">${escapeHtml(r.title||'')}</div>${r.body?`<div class="cc-body">${mdLite(r.body)}</div>`:""}</div>`;
      const act = r.action || {}; const t = act.type; const a = act.args || {};
      if (t === "refresh_garmin") triggerRefresh();
      else if (t === "open_tab") goTab(a.tab);
      else if (t === "open_garmin_sync") { goTab("calendar"); openGarminSync(); }
      else if (t === "set_theme" && (a.theme === "light" || a.theme === "dark") && a.theme !== curTheme()) $("#themeBtn")?.click();
    } else {
      // text kind or unknown
      el.innerHTML = `<div class="cc"><div class="cc-h">${escapeHtml(r.title||'Coach')}</div><div class="cc-body">${mdLite(r.body||JSON.stringify(r))}</div>${r.footer?`<div class="cc-foot muted">${escapeHtml(r.footer)}</div>`:""}</div>`;
    }
    $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
  }

  function triggerRefresh() {
    const b = document.getElementById("refreshBtn"); if (b) b.click();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
