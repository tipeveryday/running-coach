/* ActivityDeepDive — single-activity layout.
   Order of reading: what was it (header) → what it means (lead callout) →
   the evidence (charts, splits) → the body's side (zones) → the coaching notes.
   Every section is optional; missing data hides its section rather than
   rendering an empty box. */
import { esc, fmt, workout, zoneVar } from "./core.js";
import { badge, statTile, table, zoneBar, zoneChip } from "./components.js";
import { insightCallout, insightList } from "./insight.js";
import { AnnotatedChart } from "./charts.js";

/**
 * Splits table: pace column carries an inline bar (longer = faster) tinted by the
 * lap's HR zone, so effort and speed read together without a second chart.
 * splits: [{ n, km, pace_s, hr, zone, elev, gap_s }]
 */
export function splitsTable(splits = [], o = {}) {
  if (!splits.length) return "";
  const paces = splits.map((s) => s.pace_s).filter(Boolean);
  const fast = Math.min(...paces), slow = Math.max(...paces);
  const span = Math.max(slow - fast, 1);
  const hasHr = splits.some((s) => s.hr), hasElev = splits.some((s) => s.elev != null), hasGap = splits.some((s) => s.gap_s);
  const cols = [
    { key: "n", label: o.unitLabel || "Km", num: true },
    { key: "pace_s", label: "Pace /km", fmt: (v, r) => `<span class="ui-split-pace" style="--zc:${zoneVar(r.zone)}"><span class="num">${fmt.pace(v)}</span><span class="bar" aria-hidden="true"><i style="width:${Math.round(35 + 65 * (slow - v) / span)}%"></i></span>${v === fast && splits.length > 2 ? badge("Fastest", { size: "sm", tone: "good" }) : ""}</span>` },
  ];
  if (hasGap) cols.push({ key: "gap_s", label: "Grade-adj", align: "r", num: true, fmt: (v) => fmt.pace(v) });
  if (hasHr) cols.push({ key: "hr", label: "HR", align: "r", num: true, fmt: (v) => (v ? `${v}` : "–") });
  if (splits.some((s) => s.zone)) cols.push({ key: "zone", label: "Zone", fmt: (v) => (v ? zoneChip(v) : "–") });
  if (hasElev) cols.push({ key: "elev", label: "Elev", align: "r", num: true, fmt: (v) => (v == null ? "–" : `${fmt.signed(v)} m`) });
  return table({ caption: "Splits", cols, rows: splits, dense: true, rowHeaders: true, rowAttrs: (r, i) => ({ "data-split": i }) });
}

/** Zone breakdown: stacked bar + labelled table (color is never the only cue). */
export function zoneBreakdown(zones = [], names = ["Recovery", "Easy", "Steady", "Threshold", "VO₂max"]) {
  const tot = zones.reduce((s, z) => s + (z.seconds || 0), 0);
  if (!tot) return "";
  return `${zoneBar(zones.map((z) => ({ zone: z.zone, value: z.seconds })), "Time in heart-rate zone")}
    ${table({ caption: "Time in zone", dense: true, cols: [
      { key: "zone", label: "Zone", fmt: (v) => zoneChip(v, names[v - 1]) },
      { key: "seconds", label: "Time", align: "r", num: true, fmt: (v) => fmt.clock(v) },
      { key: "pct", label: "Share", align: "r", num: true },
    ], rows: zones.map((z) => ({ ...z, pct: `${Math.round((z.seconds / tot) * 100)}%` })) })}`;
}

/**
 * ActivityDeepDive(mount, {
 *   title, date, type, subtitle,               // header
 *   stats: [{ label, value, unit, foot, tone }],
 *   lead: { tone, title, body, why },          // the one-line verdict on this session
 *   charts: [AnnotatedChart cfg],              // e.g. pace + HR over distance (separate charts, no dual axis)
 *   splits: [...], zones: [{zone, seconds}],
 *   insights: [{ tone, title, detail, how }],  // coaching notes
 *   sections: [{ title, html }],               // anything domain-specific (impact load, weather, form)
 * }) -> { destroy() }
 */
export function ActivityDeepDive(mount, a) {
  if (typeof mount === "string") mount = document.querySelector(mount);
  const w = workout(a.type);
  const chartIds = (a.charts || []).map((_, i) => `dd-chart-${i}-${Math.random().toString(36).slice(2, 7)}`);
  const sec = (title, body, extra = "") => body ? `<section class="ui-dd-sec"><div class="ui-dd-sec-h"><h3 class="ui-h3">${esc(title)}</h3>${extra}</div>${body}</section>` : "";
  mount.innerHTML = `<article class="ui-dd">
    <header class="ui-dd-head">
      <div class="ui-dd-kicker">${badge(w.label, { icon: w.icon })}<span class="ui-caption">${esc(fmt.date(a.date, { weekday: "long", month: "long", day: "numeric" }))}${a.subtitle ? ` · ${esc(a.subtitle)}` : ""}</span></div>
      <h2 class="ui-dd-title">${esc(a.title)}</h2>
      ${a.stats && a.stats.length ? `<div class="ui-dd-stats">${a.stats.map((s) => statTile({ ...s, inset: true })).join("")}</div>` : ""}
    </header>
    ${a.lead ? insightCallout({ variant: "lead", label: "Coach's read", tone: a.lead.tone || "coach", title: a.lead.title, body: a.lead.body, why: a.lead.why }) : ""}
    <div class="ui-dd-cols">
      <div class="ui-stack" style="gap:var(--sp-7)">
        ${chartIds.map((id) => `<div id="${id}"></div>`).join("")}
        ${sec("Splits", splitsTable(a.splits), a.splits && a.splits.length ? `<span class="ui-caption">Bar length = speed · tint = HR zone</span>` : "")}
      </div>
      <div class="ui-stack" style="gap:var(--sp-7)">
        ${sec("Heart-rate zones", zoneBreakdown(a.zones))}
        ${a.insights && a.insights.length ? sec("Coaching notes", insightList(a.insights, { variant: "inline" })) : ""}
        ${(a.sections || []).map((s) => sec(s.title, s.html)).join("")}
      </div>
    </div>
  </article>`;
  const charts = (a.charts || []).map((c, i) => AnnotatedChart(mount.querySelector("#" + chartIds[i]), c));
  return { charts, destroy() { charts.forEach((c) => c.destroy()); mount.innerHTML = ""; } };
}
