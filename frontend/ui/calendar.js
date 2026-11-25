/* CalendarGrid + WorkoutCell.
   Encoding: COLOR = intensity (Z1–Z5 stripe), ICON = workout type, BAR = distance
   relative to the biggest day in view. State reads without color: a check for
   done, strike-through + outline for missed, "+" for unplanned, flag for race.
   Views: month · week · agenda. Narrow containers get a compact month (dots + km)
   and a list-style week, never a horizontal scroll. */
import { esc, cx, fmt, workout, zoneVar } from "./core.js";
import { icon } from "./icons.js";
import { button, segmented } from "./components.js";

const isoOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parse = (iso) => new Date(iso + "T00:00:00");
const mondayOf = (d) => { const x = new Date(d); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); x.setHours(0, 0, 0, 0); return x; };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };

/**
 * Day shape (normalize the app's plan + done maps into this):
 * { type, title, km, pace, strength: bool, race: bool,
 *   state: "planned" | "done" | "missed" | "unplanned" | "rest",
 *   done: { km, pace, name } }
 */
export function workoutCell(iso, d, o = {}) {
  const date = parse(iso);
  const w = workout(d && d.type);
  const state = !d ? "empty" : d.state || (d.type === "rest" ? "rest" : "planned");
  const isRace = !!(d && (d.race || d.type === "race"));
  const km = d && (state === "done" || state === "unplanned") && d.done ? d.done.km : d && d.km;
  const flags = [
    state === "done" || state === "unplanned" ? `<span class="done" title="Completed">${icon("circle-check", { size: 14 })}</span>` : "",
    state === "unplanned" ? `<span title="Unplanned run">${icon("plus", { size: 14 })}</span>` : "",
    d && d.strength && d.type !== "strength" ? `<span title="Strength">${icon("dumbbell", { size: 14 })}</span>` : "",
  ].join("");
  const stateWord = { done: "completed", missed: "missed", unplanned: "unplanned run", planned: "planned", rest: "rest day", empty: "nothing planned" }[state];
  const aria = [fmt.date(iso, { weekday: "long", month: "long", day: "numeric" }), d && w.label, d && d.title, km ? `${fmt.num(km, 1)} km` : "", stateWord, o.today ? "today" : ""].filter(Boolean).join(", ");
  const barPct = km && o.maxKm ? Math.max(6, Math.round((km / o.maxKm) * 100)) : 0;
  const cls = cx("ui-wcell", `is-${state}`, { "is-today": o.today, "is-outside": o.outside, "is-race": isRace });
  const zone = isRace ? "var(--accent)" : zoneVar(w.zone);
  const kmLine = km
    ? `<span class="ui-wcell-km">${fmt.num(km, 1)}<small>km${d.pace && state !== "missed" ? ` · ${esc((state === "done" || state === "unplanned") && d.done && d.done.pace ? d.done.pace : d.pace)}` : ""}</small></span>`
    : state === "missed" ? `<span class="ui-wcell-km"><small>Not logged</small></span>` : "";
  return `<button type="button" class="${cls}" style="--wz:${zone}" data-date="${iso}" aria-label="${esc(aria)}" tabindex="-1"${o.today ? ' aria-current="date"' : ""}>
    <span class="ui-wcell-top"><span class="ui-wcell-day">${date.getDate()}</span>${o.showDow ? `<span>${fmt.weekday(iso)}</span>` : ""}<span class="ui-wcell-flags">${flags}</span></span>
    ${d && state !== "empty" ? `<span class="ui-wcell-type">${icon(w.icon, { size: 14 })}<span>${esc(state === "unplanned" ? "Run" : w.label)}</span></span>
      ${d.title && state !== "rest" ? `<span class="ui-wcell-title">${esc(state === "unplanned" && d.done ? d.done.name || "Unplanned run" : d.title)}</span>` : ""}
      ${kmLine}${barPct ? `<span class="ui-wcell-bar" aria-hidden="true"><i style="width:${barPct}%"></i></span>` : ""}` : ""}
  </button>`;
}

/**
 * CalendarGrid(mount, {
 *   days: { iso: Day },  today: iso,  anchor: Date|iso,
 *   view: "month"|"week"|"agenda" (default: month, or agenda on narrow screens),
 *   onOpen(iso, day),  weekStats(weekDays) -> html (optional, shown per week),
 *   onRender({ view, anchor })  (after every render: navigation, view switch, set())
 * }) -> { set(partial), destroy() }
 */
export function CalendarGrid(mount, cfg) {
  if (typeof mount === "string") mount = document.querySelector(mount);
  const st = {
    ...cfg,
    anchor: cfg.anchor ? (typeof cfg.anchor === "string" ? parse(cfg.anchor) : cfg.anchor) : parse(cfg.today),
    view: cfg.view || (mount.clientWidth && mount.clientWidth < 640 ? "agenda" : "month"),
  };
  let focusIso = st.today;

  const dayOf = (iso) => st.days[iso] || null;
  const kmOf = (d) => (d ? (d.done && (d.state === "done" || d.state === "unplanned") ? d.done.km : d.km) || 0 : 0);

  function weekSummary(start) {
    let plan = 0, done = 0;
    for (let i = 0; i < 7; i++) {
      const d = dayOf(isoOf(addDays(start, i)));
      if (!d) continue;
      if (d.km && d.state !== "unplanned") plan += d.km;
      if (d.done && (d.state === "done" || d.state === "unplanned")) done += d.done.km || 0;
    }
    return { plan, done };
  }
  const weekHead = (start) => {
    const { plan, done } = weekSummary(start);
    const custom = st.weekStats ? st.weekStats(start) : "";
    return `<div class="ui-cal-weekhead"><span>Week of ${fmt.date(isoOf(start))}</span><span class="num">${custom || (plan ? `${fmt.num(done, 0)} / ${fmt.num(plan, 0)} km` : "")}</span></div>`;
  };

  function title() {
    if (st.view === "month") return st.anchor.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    const m = mondayOf(st.anchor);
    if (st.view === "week") return `${fmt.date(isoOf(m))} – ${fmt.date(isoOf(addDays(m, 6)))}`;
    return `${fmt.date(isoOf(m))} – ${fmt.date(isoOf(addDays(m, 27)))}`;
  }

  function grid() {
    const dows = Array.from({ length: 7 }, (_, i) => fmt.weekday(isoOf(addDays(mondayOf(new Date(2024, 0, 1)), i))));
    if (st.view === "agenda") {
      const m = mondayOf(st.anchor);
      const weeks = Array.from({ length: 4 }, (_, w) => addDays(m, w * 7));
      const maxKm = Math.max(1, ...weeks.flatMap((ws) => Array.from({ length: 7 }, (_, i) => kmOf(dayOf(isoOf(addDays(ws, i)))))));
      return `<div class="ui-agenda">${weeks.map((ws) => {
        const { plan, done } = weekSummary(ws);
        const cells = Array.from({ length: 7 }, (_, i) => { const iso = isoOf(addDays(ws, i)); return workoutCell(iso, dayOf(iso) || { type: "rest", state: "rest" }, { today: iso === st.today, showDow: true, maxKm }); }).join("");
        return `<section class="ui-agenda-week" aria-label="Week of ${fmt.date(isoOf(ws))}"><div class="ui-agenda-head"><b>Week of ${fmt.date(isoOf(ws))}</b><span class="num">${plan ? `${fmt.num(done, 0)} / ${fmt.num(plan, 0)} km` : ""}</span></div><div class="ui-agenda-list" data-grid>${cells}</div></section>`;
      }).join("")}</div>`;
    }
    if (st.view === "week") {
      const m = mondayOf(st.anchor);
      const isos = Array.from({ length: 7 }, (_, i) => isoOf(addDays(m, i)));
      const maxKm = Math.max(1, ...isos.map((i) => kmOf(dayOf(i))));
      return `<div class="ui-cal-dow week" aria-hidden="true">${dows.map((d) => `<span>${d}</span>`).join("")}</div>
        <div class="ui-cal-grid week" data-grid>${isos.map((iso) => workoutCell(iso, dayOf(iso), { today: iso === st.today, maxKm, showDow: true })).join("")}</div>
        ${weekHead(m)}`;
    }
    // month
    const first = new Date(st.anchor.getFullYear(), st.anchor.getMonth(), 1);
    const start = mondayOf(first);
    const last = new Date(st.anchor.getFullYear(), st.anchor.getMonth() + 1, 0);
    const end = addDays(mondayOf(last), 6);
    const isos = []; for (let d = new Date(start); d <= end; d = addDays(d, 1)) isos.push(isoOf(d));
    const maxKm = Math.max(1, ...isos.map((i) => kmOf(dayOf(i))));
    return `<div class="ui-cal-dow" aria-hidden="true">${dows.map((d) => `<span>${d}</span>`).join("")}</div>
      <div class="ui-cal-grid month" data-grid>${isos.map((iso) => workoutCell(iso, dayOf(iso), { today: iso === st.today, outside: parse(iso).getMonth() !== st.anchor.getMonth(), maxKm })).join("")}</div>`;
  }

  const legend = () => `<div class="ui-cal-legend" aria-label="Legend">
    ${[[1, "Recovery"], [2, "Easy / long"], [3, "Steady"], [4, "Threshold"], [5, "VO₂max"]].map(([z, l]) => `<span><i style="--sw:var(--z${z})"></i>${l}</span>`).join("")}
    <span><i style="--sw:var(--accent)"></i>Race</span>
    <span>${icon("circle-check", { size: 14 })}Done</span><span>${icon("plus", { size: 14 })}Unplanned</span></div>`;

  function render() {
    const focused = mount.contains(document.activeElement);
    mount.innerHTML = `<div class="ui-cal ui-cal-wrap">
      <div class="ui-cal-bar">
        ${button({ label: "Previous", iconOnly: true, icon: "chevron-left", variant: "ghost", attrs: { "data-cal": "prev" } })}
        ${button({ label: "Next", iconOnly: true, icon: "chevron-right", variant: "ghost", attrs: { "data-cal": "next" } })}
        <h3 class="ui-cal-title" aria-live="polite">${esc(title())}</h3>
        ${button({ label: "Today", size: "sm", variant: "secondary", attrs: { "data-cal": "today" } })}
        ${segmented({ label: "Calendar view", name: "cal-view", value: st.view, options: [["month", "Month", "layout-grid"], ["week", "Week", "rows-3"], ["agenda", "Agenda", "list"]] })}
      </div>
      ${grid()}
      ${legend()}
    </div>`;
    // roving tabindex: one tab stop in the grid
    const cells = [...mount.querySelectorAll("[data-grid] .ui-wcell")];
    const target = cells.find((c) => c.dataset.date === focusIso) || cells.find((c) => c.getAttribute("aria-current")) || cells.find((c) => !c.classList.contains("is-outside")) || cells[0];
    if (target) { target.tabIndex = 0; if (focused) target.focus(); }
    if (st.onRender) st.onRender({ view: st.view, anchor: new Date(st.anchor) });
  }

  function shift(dir) {
    const a = new Date(st.anchor);
    if (st.view === "month") { a.setDate(1); a.setMonth(a.getMonth() + dir); }
    else a.setDate(a.getDate() + dir * (st.view === "week" ? 7 : 28));
    st.anchor = a; focusIso = null; render();
  }

  const onClick = (e) => {
    const nav = e.target.closest("[data-cal]");
    if (nav) { const k = nav.dataset.cal; if (k === "today") { st.anchor = parse(st.today); focusIso = st.today; render(); } else shift(k === "next" ? 1 : -1); return; }
    const cell = e.target.closest(".ui-wcell[data-date]");
    if (cell && st.onOpen) { focusIso = cell.dataset.date; st.onOpen(cell.dataset.date, dayOf(cell.dataset.date)); }
  };
  const onChange = (e) => { if (e.detail.name === "cal-view") { st.view = e.detail.value; render(); } };
  const onKey = (e) => {
    const cell = e.target.closest("[data-grid] .ui-wcell"); if (!cell) return;
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 7, ArrowUp: -7 }[e.key];
    if (step == null) return;
    e.preventDefault();
    const iso = isoOf(addDays(parse(cell.dataset.date), step));
    let next = mount.querySelector(`[data-grid] .ui-wcell[data-date="${iso}"]`);
    if (!next) { focusIso = iso; st.anchor = parse(iso); render(); return; }
    cell.tabIndex = -1; next.tabIndex = 0; next.focus(); focusIso = iso;
  };
  mount.addEventListener("click", onClick);
  mount.addEventListener("ui:change", onChange);
  mount.addEventListener("keydown", onKey);
  render();
  return {
    set(next) { Object.assign(st, next); if (next.anchor && typeof next.anchor === "string") st.anchor = parse(next.anchor); render(); },
    destroy() { mount.removeEventListener("click", onClick); mount.removeEventListener("ui:change", onChange); mount.removeEventListener("keydown", onKey); mount.innerHTML = ""; },
  };
}
