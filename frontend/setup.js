/* Onboarding / setup: goal race, goal time, training volume and (optional)
   athlete details → POST /api/setup, which writes data/config.json and
   generates data/plan.json. Shown on first run and from the topbar's
   "Edit setup" button. */
import * as UI from "./ui/index.js";

const DOW = [["Mon", "Monday"], ["Tue", "Tuesday"], ["Wed", "Wednesday"], ["Thu", "Thursday"], ["Fri", "Friday"], ["Sat", "Saturday"], ["Sun", "Sunday"]];
const DIST = [["5k", "5K"], ["10k", "10K"], ["half", "Half"], ["marathon", "Marathon"]];
const DEFAULT_GOAL = { "5k": "25:00", "10k": "52:00", half: "1:55:00", marathon: "4:00:00" };
const STEPS = ["Race", "Training", "About you", "Review"];

const esc = UI.esc;
const secsToClock = (s) => {
  if (!s) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
const inWeeks = (n) => { const d = new Date(); d.setDate(d.getDate() + n * 7 + ((7 - d.getDay()) % 7)); return d.toISOString().slice(0, 10); };

/**
 * openSetup({ profile, suggestions, demo, firstRun, onDone })
 * Renders into #setupGate and resolves when the plan is saved.
 */
export function openSetup(o = {}) {
  const host = document.getElementById("setupGate");
  const p = Object.assign({
    distance: "half", race_date: inWeeks(14), goal_time: "", race_name: "", race_location: "",
    current_km: 30, peak_km: 45, runs_per_week: 5, long_run_day: "Sun", quality_day: "Tue",
    second_quality: true, strength_day: "",
  }, o.suggestions || {}, o.profile || {});
  if (p.goal_seconds && !p.goal_time) p.goal_time = secsToClock(p.goal_seconds);
  if (!p.goal_time) p.goal_time = DEFAULT_GOAL[p.distance];
  let step = 0;

  const val = (id) => { const n = host.querySelector("#" + id); return n ? (n.type === "checkbox" ? n.checked : n.value.trim()) : undefined; };
  const collect = () => {
    const grab = {
      0: ["race_name", "race_date", "goal_time", "race_location"],
      1: ["current_km", "peak_km", "runs_per_week", "long_run_day", "quality_day", "second_quality", "strength_day"],
      2: ["name", "sex", "birth_date", "max_hr", "lthr", "weight_lb", "goal_weight_lb", "home_lat", "home_lon"],
    }[step] || [];
    grab.forEach((k) => { const v = val("su-" + k); if (v !== undefined) p[k] = v; });
    const seg = host.querySelector(".ui-seg[data-name=distance] [aria-checked=true]");
    if (seg) p.distance = seg.dataset.value;
  };

  const stepper = () => `<ol class="app-setup-steps" aria-label="Setup steps">${STEPS.map((s, i) =>
    `<li class="${i === step ? "is-on" : i < step ? "is-done" : ""}"${i === step ? ' aria-current="step"' : ""}><span class="num">${i + 1}</span>${esc(s)}</li>`).join("")}</ol>`;

  const views = [
    () => `
      <h2 class="ui-h2">Your goal race</h2>
      <p class="ui-caption">The plan builds back from race day, so start with the race.</p>
      <div class="ui-field"><span class="ui-label">Distance</span>${UI.segmented({ label: "Race distance", name: "distance", value: p.distance, options: DIST })}</div>
      <div class="app-setup-grid">
        ${UI.field({ label: "Race name", id: "su-race_name", control: UI.input({ value: p.race_name, placeholder: "e.g. City Half Marathon" }) })}
        ${UI.field({ label: "Race date", id: "su-race_date", control: UI.input({ type: "date", value: p.race_date }) })}
        ${UI.field({ label: "Goal time", id: "su-goal_time", hint: "h:mm:ss or mm:ss", control: UI.input({ value: p.goal_time, inputmode: "numeric" }) })}
        ${UI.field({ label: "Location (optional)", id: "su-race_location", control: UI.input({ value: p.race_location, placeholder: "City" }) })}
      </div>`,
    () => `
      <h2 class="ui-h2">How you train</h2>
      <p class="ui-caption">Volume ramps about 8% a week from where you are now to your peak, with a down week every fourth week.</p>
      <div class="app-setup-grid">
        ${UI.field({ label: "Current weekly km", id: "su-current_km", hint: o.suggestions && o.suggestions.current_km ? `Garmin says ~${o.suggestions.current_km} km lately` : "Average of the last month", control: UI.input({ type: "number", value: p.current_km, inputmode: "numeric", attrs: { min: 5, max: 200 } }) })}
        ${UI.field({ label: "Peak weekly km", id: "su-peak_km", hint: "The biggest week you want to reach", control: UI.input({ type: "number", value: p.peak_km, inputmode: "numeric", attrs: { min: 10, max: 250 } }) })}
        ${UI.field({ label: "Runs per week", id: "su-runs_per_week", control: UI.select({ value: p.runs_per_week, options: [3, 4, 5, 6, 7].map((n) => [n, `${n} runs`]) }) })}
        ${UI.field({ label: "Long run day", id: "su-long_run_day", control: UI.select({ value: p.long_run_day, options: DOW }) })}
        ${UI.field({ label: "Main workout day", id: "su-quality_day", control: UI.select({ value: p.quality_day, options: DOW }) })}
        ${UI.field({ label: "Strength day", id: "su-strength_day", control: UI.select({ value: p.strength_day || "", options: [["", "No strength sessions"], ...DOW.map(([, l]) => [l, l])] }) })}
      </div>
      <label class="app-setup-check"><input type="checkbox" id="su-second_quality"${p.second_quality !== false && p.second_quality !== "false" ? " checked" : ""}> Add a second workout in build weeks (5+ runs a week)</label>`,
    () => `
      <h2 class="ui-h2">About you <span class="ui-caption">optional</span></h2>
      <p class="ui-caption">Used for heart-rate zones, VO₂max percentiles and the weight chart. Leave blank and the app uses sensible defaults or your Garmin data.</p>
      <div class="app-setup-grid">
        ${UI.field({ label: "Name", id: "su-name", control: UI.input({ value: p.name || "", autocomplete: "given-name" }) })}
        ${UI.field({ label: "Sex", id: "su-sex", hint: "For age/sex VO₂max norms", control: UI.select({ value: p.sex || "", options: [["", "Prefer not to say"], ["F", "Female"], ["M", "Male"]] }) })}
        ${UI.field({ label: "Birth date", id: "su-birth_date", control: UI.input({ type: "date", value: p.birth_date || "" }) })}
        ${UI.field({ label: "Max heart rate", id: "su-max_hr", hint: "bpm — blank = estimate from age", control: UI.input({ type: "number", value: p.max_hr || "", inputmode: "numeric" }) })}
        ${UI.field({ label: "Lactate threshold HR", id: "su-lthr", hint: "bpm — your zones are built from this", control: UI.input({ type: "number", value: p.lthr || "", inputmode: "numeric" }) })}
        ${UI.field({ label: "Weight (lb)", id: "su-weight_lb", control: UI.input({ type: "number", value: p.weight_lb || "", inputmode: "decimal" }) })}
        ${UI.field({ label: "Goal weight (lb)", id: "su-goal_weight_lb", hint: "Optional", control: UI.input({ type: "number", value: p.goal_weight_lb || "", inputmode: "decimal" }) })}
      </div>
      <div class="ui-field"><span class="ui-label">Home location for weather</span>
        <div class="app-setup-loc">
          ${UI.input({ value: p.home_lat ?? "", placeholder: "Latitude", attrs: { id: "su-home_lat", "aria-label": "Latitude", inputmode: "decimal" } })}
          ${UI.input({ value: p.home_lon ?? "", placeholder: "Longitude", attrs: { id: "su-home_lon", "aria-label": "Longitude", inputmode: "decimal" } })}
          ${UI.button({ label: "Use my location", variant: "secondary", icon: "target", attrs: { id: "su-geo" } })}
        </div>
        <span class="ui-hint">Weather comes from Open-Meteo (no account). Only the coordinates are sent.</span>
      </div>`,
    () => `<h2 class="ui-h2">Your plan</h2><div id="su-preview">${UI.loading("Building your plan")}</div>`,
  ];

  const render = () => {
    host.innerHTML = `<div class="app-login-card app-setup-card">
      <div class="app-login-brand"><span class="ui-brand-mark" aria-hidden="true">${UI.icon("trending-up", { size: 18 })}</span><span class="ui-h3">Running Coach</span>
        ${o.demo ? UI.badge("Demo mode", { tone: "info", size: "sm" }) : ""}</div>
      ${o.firstRun && step === 0 ? `<p class="ui-body">Welcome. Answer a few questions and the coach builds a training plan around your race. You can change any of it later.</p>` : ""}
      ${o.demo ? UI.insightCallout({ variant: "inline", tone: "info", title: "Demo mode", body: "Changes here only affect the demo athlete and reset when the server restarts." }) : ""}
      ${stepper()}
      <form class="app-setup-body" id="su-form" novalidate>${views[step]()}
        <p class="ui-error-text" id="su-err" role="alert" hidden></p>
        <div class="app-setup-foot">
          ${step > 0 ? UI.button({ label: "Back", variant: "ghost", attrs: { id: "su-back" } }) : (o.firstRun ? "<span></span>" : UI.button({ label: "Cancel", variant: "ghost", attrs: { id: "su-cancel" } }))}
          ${step < 3 ? UI.button({ label: step === 2 ? "Preview plan" : "Next", variant: "primary", type: "submit" })
                     : UI.button({ label: o.firstRun ? "Create my plan" : "Save and regenerate plan", variant: "primary", icon: "check", type: "submit", attrs: { id: "su-save" } })}
        </div>
      </form>
    </div>`;
    host.classList.remove("hidden");
    host.scrollTop = 0;
    UI.initUI(host);
    host.querySelector(".ui-seg[data-name=distance]")?.addEventListener("click", (e) => {
      const b = e.target.closest("[data-value]"); if (!b) return;
      const gt = host.querySelector("#su-goal_time");
      if (gt && Object.values(DEFAULT_GOAL).includes(gt.value)) gt.value = DEFAULT_GOAL[b.dataset.value];
    });
    host.querySelector("#su-back")?.addEventListener("click", () => { collect(); step--; render(); });
    host.querySelector("#su-cancel")?.addEventListener("click", () => { host.classList.add("hidden"); host.innerHTML = ""; });
    host.querySelector("#su-geo")?.addEventListener("click", () => {
      if (!navigator.geolocation) return UI.toast("Your browser can't share location.", { tone: "warn" });
      navigator.geolocation.getCurrentPosition((pos) => {
        host.querySelector("#su-home_lat").value = pos.coords.latitude.toFixed(3);
        host.querySelector("#su-home_lon").value = pos.coords.longitude.toFixed(3);
      }, () => UI.toast("Location permission was declined.", { tone: "warn" }));
    });
    host.querySelector("#su-form").addEventListener("submit", onSubmit);
    if (step === 3) preview();
    host.querySelector("h2")?.focus?.();
  };

  const showErr = (msg) => { const e = host.querySelector("#su-err"); e.textContent = msg; e.hidden = false; };
  const payload = () => { const out = { ...p }; delete out.goal_seconds; return out; };

  async function preview() {
    const mount = host.querySelector("#su-preview");
    let d;
    try {
      const r = await fetch("/api/setup/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
      d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || "Couldn't build the plan.");
    } catch (e) {
      mount.innerHTML = UI.errorState({ compact: true, title: "Check your answers", body: String(e.message || e), retry: false });
      host.querySelector("#su-save").disabled = true;
      return;
    }
    const g = d.goals;
    const order = ["easy", "steady", "mp", "hmp", "threshold", "tenk", "fivek", "rep"];
    const paceTiles = order.filter((k) => d.paces[k]).map((k) => {
      const pc = d.paces[k];
      return UI.statTile({ label: pc.label.split(" (")[0].split(" /")[0], value: pc.target || `${pc.low}–${pc.high}`, unit: "/km", inset: true });
    }).join("");
    const max = Math.max(...d.weeks.map((w) => w.km), 1);
    const bars = d.weeks.map((w) => `<div class="app-setup-bar" title="Week ${w.week} · ${esc(w.phase)} · ${w.km} km">
        <span style="height:${Math.max(4, Math.round((w.km / max) * 100))}%" data-phase="${esc(w.phase.toLowerCase().replace(/[^a-z]/g, ""))}"></span></div>`).join("");
    const phases = [...new Set(d.weeks.map((w) => w.phase))];
    mount.innerHTML = `
      <div class="app-setup-goals">${["A", "B", "C"].map((k) => UI.statTile({ label: g[k].label, value: g[k].time, foot: `${esc(g[k].pace_per_km)}/km`, inset: true })).join("")}</div>
      <div class="ui-field"><span class="ui-label">${d.weeks.length} weeks · peak ${d.peak_km} km</span>
        <div class="app-setup-bars" role="img" aria-label="Weekly volume from ${d.weeks[0].km} to a peak of ${d.peak_km} km">${bars}</div>
        <div class="app-setup-legend">${phases.map((ph) => `<span><i data-phase="${esc(ph.toLowerCase().replace(/[^a-z]/g, ""))}"></i>${esc(ph)}</span>`).join("")}</div>
      </div>
      <div class="ui-field"><span class="ui-label">Training paces</span><div class="app-setup-paces">${paceTiles}</div></div>
      <p class="ui-caption">Paces come from your goal time (Daniels' VDOT). Once your Garmin data syncs, the Fitness tab shows how your current fitness compares.</p>`;
  }

  async function onSubmit(e) {
    e.preventDefault();
    collect();
    if (step === 0) {
      if (!p.race_date) return showErr("Pick a race date.");
      if (!/^\d{1,2}(:\d{2}){1,2}$/.test(p.goal_time || "")) return showErr("Goal time looks off — use h:mm:ss or mm:ss.");
    }
    if (step === 1 && +p.peak_km < +p.current_km) return showErr("Peak weekly km should be at least your current weekly km.");
    if (step < 3) { step++; render(); return; }
    const btn = host.querySelector("#su-save");
    btn.disabled = true;
    try {
      const r = await fetch("/api/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || "Couldn't save.");
      host.classList.add("hidden"); host.innerHTML = "";
      UI.toast(o.firstRun ? "Plan created. Welcome aboard." : "Plan regenerated.", { tone: "good" });
      o.onDone && o.onDone();
    } catch (err) {
      showErr(String(err.message || err));
      btn.disabled = false;
    }
  }

  render();
}
