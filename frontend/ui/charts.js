/* AnnotatedChart + ForecastChart — the only way the app draws a chart.
   Wraps Chart.js (global `Chart`, loaded from /static/chart.umd.min.js) with:
   · one axis/grid/tooltip/legend style, read live from tokens (re-themes on toggle)
   · an annotation LANE above the plot: numbered markers, range and "Today" labels
     live there, so text never sits on top of data
   · highlighted x-ranges, horizontal y-bands, target lines labelled in a right
     gutter (also outside the data), confidence bands, a crosshair
   · keyboard access (focus the chart, ←/→ walk the points), a text alternative,
     and a "View as table" fallback
   No dual axes — two measures with different units are two charts. */
import { esc, cx, uid, token, fmt, prefersReducedMotion } from "./core.js";
import { icon } from "./icons.js";
import { showTooltip, hideTooltip, table, emptyState } from "./components.js";
import { insightCallout } from "./insight.js";

const LANE_ROW = 22;                         // px per annotation-lane row
const live = new Set();                      // mounted charts, for re-theming

/* ---------- Color resolution: tokens -> canvas-ready strings ---------- */
export function color(c) {
  if (!c) return token("--cat-1");
  if (c.startsWith("#") || c.startsWith("rgb")) return c;
  const name = c.startsWith("var(") ? c.slice(4, -1).trim() : c.startsWith("--") ? c : `--${c}`;
  return token(name) || c;
}
export function alpha(c, a) {
  const hex = color(c);
  if (!hex.startsWith("#")) return hex;
  const h = hex.length === 4 ? hex.replace(/#(.)(.)(.)/, "#$1$1$2$2$3$3") : hex;
  const n = parseInt(h.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
function theme() {
  return {
    grid: color("--viz-grid"), axis: color("--viz-axis"), label: color("--viz-label"), target: color("--viz-target"),
    text1: color("--text-1"), text2: color("--text-2"), surface: color("--viz-surface"), crosshair: color("--viz-crosshair"),
    highlight: color("--viz-highlight"), neutral: color("--neutral-soft"), accent: color("--accent"),
    font: getComputedStyle(document.body).fontFamily || "Inter, sans-serif",
  };
}
/** Re-theme every mounted chart (call after toggling data-theme). */
export function refreshCharts() { live.forEach((c) => c.rebuild()); }

/* ---------- The annotation plugin ---------- */
const idx = (labels, at) => (typeof at === "number" ? at : labels.indexOf(at));

function lanePlan(chart, cfg, T) {
  // Collect lane items (markers, range labels, today) and lay them out in rows without overlap.
  const { ctx, chartArea: a, scales: { x } } = chart;
  ctx.font = `500 11px ${T.font}`;
  const items = [];
  (cfg.ranges || []).forEach((r) => {
    if (!r.label) return;
    const i0 = idx(cfg.labels, r.from), i1 = idx(cfg.labels, r.to);
    if (i0 < 0 || i1 < 0) return;
    items.push({ kind: "range", x: (x.getPixelForValue(i0) + x.getPixelForValue(i1)) / 2, text: r.label, center: true });
  });
  (cfg.markers || []).forEach((m) => {
    const i = idx(cfg.labels, m.at); if (i < 0) return;
    items.push({ kind: "marker", x: x.getPixelForValue(i), n: m.n, text: m.label || "" });
  });
  if (cfg.today != null) {
    const i = idx(cfg.labels, cfg.today);
    if (i >= 0) items.push({ kind: "today", x: x.getPixelForValue(i), text: cfg.todayLabel || "Today" });
  }
  // axis direction hint (e.g. "Faster ↑" on a reversed time axis) — first in the lane, at the plot's left edge
  if (cfg.y && cfg.y.hint) items.push({ kind: "hint", x: a.left - 5, text: cfg.y.hint });
  items.sort((p, q) => p.x - q.x);
  const rows = [[], []];
  items.forEach((it) => {
    const badge = it.kind === "marker" ? 18 : 0;
    const tw = it.text ? ctx.measureText(it.text).width : 0;
    let x0, x1;
    if (it.center) {
      // range labels: center over the range, but slide inside the plot if they'd overhang
      x0 = it.x - tw / 2; x1 = it.x + tw / 2;
      if (x0 < a.left) { it.x += a.left - x0; x1 += a.left - x0; x0 = a.left; }
      if (x1 > a.right) { it.x -= x1 - a.right; x0 -= x1 - a.right; x1 = a.right; }
    } else {
      const off = badge ? badge / 2 + 5 : 5;
      x0 = it.x - badge / 2; x1 = it.x + (tw ? off + tw : badge / 2);
      // flip text to the left of its anchor if it would run past the plot edge
      if (x1 > a.right + 4) { it.flip = true; x0 = it.x - (tw ? off + tw : badge / 2); x1 = it.x + badge / 2; }
    }
    it.x0 = x0; it.x1 = x1;
    const fits = (row) => row.every((o) => it.x1 + 6 < o.x0 || it.x0 > o.x1 + 6);
    let r = rows.findIndex(fits);
    if (r < 0) { r = 0; it.dropText = true; }            // no room: keep the number, the aside explains it
    it.row = r; rows[r].push(it);
  });
  return { items, nRows: rows[1].length ? 2 : items.length ? 1 : 0 };
}

const annotationPlugin = {
  id: "uiAnnotations",
  beforeDatasetsDraw(chart) {
    const cfg = chart.$ui; if (!cfg) return;
    const T = chart.$theme, { ctx, chartArea: a, scales: { x, y } } = chart;
    ctx.save();
    // horizontal y-bands (e.g. ACWR optimal 0.8–1.3)
    (cfg.yBands || []).forEach((b) => {
      const top = y.getPixelForValue(Math.min(b.to, y.max)), bot = y.getPixelForValue(Math.max(b.from, y.min));
      ctx.fillStyle = b.color ? (b.alpha != null ? alpha(b.color, b.alpha) : color(b.color)) : alpha("--good", 0.08);
      ctx.fillRect(a.left, Math.min(top, bot), a.right - a.left, Math.abs(bot - top));
    });
    // highlighted x-ranges
    (cfg.ranges || []).forEach((r) => {
      const i0 = idx(cfg.labels, r.from), i1 = idx(cfg.labels, r.to); if (i0 < 0 || i1 < 0) return;
      const half = (x.getPixelForValue(1) - x.getPixelForValue(0)) / 2 || 0;
      const l = x.getPixelForValue(i0) - (r.edges === "point" ? 0 : half), rr = x.getPixelForValue(i1) + (r.edges === "point" ? 0 : half);
      ctx.fillStyle = r.tone === "neutral" ? T.neutral : r.color ? (r.alpha != null ? alpha(r.color, r.alpha) : color(r.color)) : T.highlight;
      ctx.fillRect(Math.max(a.left, l), a.top, Math.min(a.right, rr) - Math.max(a.left, l), a.bottom - a.top);
    });
    // target lines + labels in the right gutter; labels for close lines are
    // pushed apart (min 13px) so they never overprint each other
    const tl = (cfg.targets || []).map((t) => ({ t, py: y.getPixelForValue(t.value) }))
      .filter(({ py }) => py >= a.top - 1 && py <= a.bottom + 1).sort((p, q) => p.py - q.py);
    tl.forEach((r, i) => { r.ly = i ? Math.max(r.py, tl[i - 1].ly + 13) : r.py; });
    tl.forEach(({ t, py, ly }) => {
      ctx.strokeStyle = T.target; ctx.lineWidth = 1.5; ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = T.text2; ctx.font = `500 11px ${T.font}`; ctx.textBaseline = "middle"; ctx.textAlign = "left";
      ctx.fillText(t.label || "", a.right + 8, ly);
    });
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const cfg = chart.$ui; if (!cfg) return;
    const T = chart.$theme, { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save();
    // today divider
    if (cfg.today != null) {
      const i = idx(cfg.labels, cfg.today);
      if (i >= 0) { const px = x.getPixelForValue(i); ctx.strokeStyle = T.axis; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(px, a.top - 4); ctx.lineTo(px, a.bottom); ctx.stroke(); }
    }
    // crosshair at the active index
    const act = chart.tooltip && chart.tooltip.getActiveElements();
    if (act && act.length && cfg.crosshair !== false) {
      const px = act[0].element.x;
      ctx.strokeStyle = T.crosshair; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
    }
    // annotation lane
    const plan = chart.$lane = lanePlan(chart, cfg, T);
    ctx.font = `500 11px ${T.font}`; ctx.textBaseline = "middle";
    plan.items.forEach((it) => {
      const cy = a.top - 8 - LANE_ROW * (plan.nRows - 1 - it.row) - LANE_ROW / 2 + 4;
      if (it.kind === "marker") {
        ctx.strokeStyle = T.axis; ctx.beginPath(); ctx.moveTo(it.x, cy + 9); ctx.lineTo(it.x, a.top); ctx.stroke();
        ctx.fillStyle = it.hot ? T.accent : T.text1; ctx.beginPath(); ctx.arc(it.x, cy, 9, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = T.surface; ctx.font = `700 11px ${T.font}`; ctx.textAlign = "center"; ctx.fillText(String(it.n), it.x, cy + 0.5);
        ctx.font = `500 11px ${T.font}`;
        if (it.text && !it.dropText) {
          ctx.fillStyle = T.text2; ctx.textAlign = it.flip ? "right" : "left";
          ctx.fillText(it.text, it.flip ? it.x - 14 : it.x + 14, cy + 0.5);
        }
      } else if (!it.dropText) {
        ctx.fillStyle = it.kind === "today" ? T.text2 : T.label;
        ctx.textAlign = it.center ? "center" : it.flip ? "right" : "left";
        ctx.fillText(it.text, it.center ? it.x : it.flip ? it.x - 5 : it.x + 5, cy + 0.5);
      }
    });
    ctx.restore();
  },
};

/* ---------- Tooltip ---------- */
function tooltipHtml(chart, i) {
  const cfg = chart.$ui;
  const title = cfg.xFormat ? cfg.xFormat(cfg.labels[i], i, "tooltip") : cfg.labels[i];
  const rows = [];
  cfg.series.forEach((s) => {
    if (s.hidden || s.tooltip === false) return;
    const v = s.data[i]; if (v == null) return;
    const f = s.fmt || cfg.y?.fmt || ((n) => fmt.num(n, 1));
    rows.push(`<dt><span class="tt-swatch" style="background:${s.$color}"></span>${esc(s.label)}</dt><dd>${esc(f(v, i))}${s.unit ? ` ${esc(s.unit)}` : ""}</dd>`);
  });
  (cfg.bands || []).forEach((b) => {
    if (b.lo[i] == null || b.hi[i] == null || b.tooltip === false) return;
    const f = cfg.y?.fmt || ((n) => fmt.num(n, 1));
    rows.push(`<dt>${esc(b.label || "Range")}</dt><dd>${esc(f(b.lo[i]))}–${esc(f(b.hi[i]))}</dd>`);
  });
  const extra = cfg.tooltipExtra ? cfg.tooltipExtra(i) : "";
  return `<div class="tt-title">${esc(title)}</div>${rows.length ? `<dl class="tt-rows">${rows.join("")}</dl>` : ""}${extra ? `<div class="ui-caption" style="margin-top:var(--sp-2)">${esc(extra)}</div>` : ""}`;
}
function externalTooltip(ctx) {
  const { chart, tooltip } = ctx;
  if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints?.length) { if (chart.$tipOn) { hideTooltip(); chart.$tipOn = false; } return; }
  const i = tooltip.dataPoints[0].dataIndex;
  const rect = chart.canvas.getBoundingClientRect();
  const el = tooltip.dataPoints[0].element;
  chart.$tipOn = true;
  showTooltip(tooltipHtml(chart, i), { getBoundingClientRect: () => ({ left: rect.left + el.x, right: rect.left + el.x, width: 0, top: rect.top + Math.min(el.y, chart.chartArea.top + 20), bottom: rect.top + el.y }) });
}

/* ---------- AnnotatedChart ---------- */
/**
 * AnnotatedChart(mount, cfg) -> { update(partialCfg), rebuild(), destroy(), chart }
 *
 * cfg = {
 *   title, takeaway,                 // header: title + ONE-line takeaway (also the canvas's accessible name)
 *   hero: { value, sub },            // optional headline number at top-right
 *   labels: [...], xFormat(label, i, where),   // where = "tick" | "tooltip" | "table"
 *   series: [{ label, data, color: "cat-1"|"z4"|..., type: "line"|"bar"|"area",
 *              dash, width, points, colorBy(v,i), fmt(v), unit, hidden, legend:false, tooltip:false, stack, endDot }],
 *   y: { min, max, suggestedMin, suggestedMax, fmt(v), reverse, ticks, beginAtZero },
 *   ranges:  [{ from, to, label, tone: "neutral"|"info", color }],        // shaded x-ranges
 *   yBands:  [{ from, to, color, label }],                                 // shaded y-ranges
 *   markers: [{ at, n, label }],                                           // numbered, in the lane
 *   targets: [{ value, label }],                                           // dotted lines, label in right gutter
 *   bands:   [{ lo: [], hi: [], level: 1|2, label }],                      // confidence fills
 *   today,                                                                 // index or label
 *   insights: [{ n, title, body, tone }],                                  // pinned notes beside the chart
 *   footer,                                                                // html under the chart (e.g. a callout)
 *   height, empty: "No runs yet", onPoint(i), legend: true|false, table: true|false, tooltipExtra(i)
 * }
 */
export function AnnotatedChart(mount, cfg) {
  if (typeof mount === "string") mount = document.querySelector(mount);
  const id = uid("chart");
  let chart = null, state = { ...cfg };

  const hasData = () => (state.series || []).some((s) => (s.data || []).some((v) => v != null));
  const legendHtml = () => {
    if (state.legend === false) return "";
    const items = [];
    state.series.forEach((s) => {
      if (s.legend === false) return;
      const kind = s.type === "bar" ? "bar" : s.dash ? "dash" : "";
      items.push(`<li><i class="${kind}" style="--sw:${s.colorBy ? "var(--text-3)" : color(s.color)}"></i>${esc(s.label)}</li>`);
    });
    (state.bands || []).filter((b) => b.legend !== false && b.label).forEach((b) => items.push(`<li><i class="band"></i>${esc(b.label)}</li>`));
    (state.ranges || []).filter((r) => r.legend).forEach((r) => items.push(`<li><i class="range"></i>${esc(r.legend)}</li>`));
    (state.targets || []).filter((t) => t.legend).forEach((t) => items.push(`<li><i class="target"></i>${esc(t.legend)}</li>`));
    (state.legendExtra || []).forEach((h) => items.push(`<li>${h}</li>`));
    return items.length >= 1 && (state.series.length > 1 || items.length > 1) ? `<ul class="ui-legend" aria-label="Legend">${items.join("")}</ul>` : "<span></span>";
  };
  const tableHtml = () => {
    if (state.table === false) return "";
    const cols = [{ key: "x", label: state.xLabel || "Date" }, ...state.series.filter((s) => s.tooltip !== false).map((s, j) => ({ key: "s" + j, label: s.label + (s.unit ? ` (${s.unit})` : ""), align: "r", num: true }))];
    const rows = state.labels.map((l, i) => {
      const r = { x: state.xFormat ? state.xFormat(l, i, "table") : l };
      state.series.filter((s) => s.tooltip !== false).forEach((s, j) => { const v = s.data[i]; r["s" + j] = v == null ? "–" : (s.fmt || state.y?.fmt || ((n) => fmt.num(n, 1)))(v, i); });
      return r;
    }).filter((r, i) => state.series.some((s) => s.data[i] != null));
    return `<details class="ui-chart-data"><summary>${icon("table-2", { size: 14 })}View as table</summary>${table({ caption: state.title, cols, rows, dense: true, rowHeaders: true })}</details>`;
  };

  function shell() {
    const aside = state.insights && state.insights.length;
    mount.innerHTML = `<figure class="${cx("ui-chart", { "has-aside": aside }, state.cls)}" style="margin:0">
      ${state.title || state.hero ? `<div class="ui-chart-head"><div>${state.title ? `<h3 class="ui-chart-title" id="${id}-t">${esc(state.title)}</h3>` : ""}${state.takeaway ? `<p class="ui-chart-takeaway" id="${id}-d">${esc(state.takeaway)}</p>` : ""}</div>
        ${state.hero ? `<div class="ui-chart-hero"><b>${esc(state.hero.value)}</b>${state.hero.sub ? `<span>${esc(state.hero.sub)}</span>` : ""}${state.hero.detail ? `<small>${esc(state.hero.detail)}</small>` : ""}</div>` : ""}</div>` : ""}
      <div class="ui-chart-body">
        <div style="min-width:0">
          <div class="ui-chart-canvas" style="--chart-h:${state.height || 240}px">
            ${hasData() ? `<canvas tabindex="0" role="img" aria-label="${esc([state.title, state.takeaway, state.alt].filter(Boolean).join(". "))}${hasData() ? ". Use left and right arrow keys to read values." : ""}"></canvas>` : `<div class="ui-chart-empty">${emptyState({ icon: "activity", title: state.empty || "No data yet", body: state.emptyBody || "", compact: false })}</div>`}
          </div>
          ${hasData() ? `<div class="ui-chart-foot">${legendHtml()}${tableHtml()}</div>` : ""}
        </div>
        ${aside ? `<aside class="ui-insights" aria-label="Chart notes">${state.insights.map((n) => insightCallout({ variant: "pinned", ...n })).join("")}</aside>` : ""}
      </div>
      ${state.footer ? `<div>${state.footer}</div>` : ""}
    </figure>`;
  }

  function build(first) {
    if (chart) { chart.destroy(); chart = null; }
    if (!hasData() || typeof Chart === "undefined") return;
    const T = theme();
    const canvas = mount.querySelector("canvas");
    const datasets = [];
    // confidence bands first (drawn underneath): hi edge, then lo edge filled back to it
    (state.bands || []).forEach((b) => {
      const fill = b.level === 2 ? color("--viz-band-2") : color("--viz-band");
      datasets.push({ $band: true, type: "line", data: b.hi, borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, fill: false, spanGaps: false, tension: 0.25 });
      datasets.push({ $band: true, type: "line", data: b.lo, borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, fill: "-1", backgroundColor: fill, spanGaps: false, tension: 0.25 });
    });
    state.series.forEach((s) => {
      s.$color = color(s.color);
      const lastIdx = s.data.reduce((m, v, i) => (v != null ? i : m), -1);
      const base = { label: s.label, data: s.data, hidden: s.hidden, stack: s.stack, order: s.type === "bar" ? 2 : 1 };
      if (s.type === "bar") {
        datasets.push({ ...base, type: "bar",
          backgroundColor: s.colorBy ? s.data.map((v, i) => color(s.colorBy(v, i))) : s.$color,
          borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "start", maxBarThickness: s.maxBar || 28,
          categoryPercentage: 0.72, barPercentage: 0.9, hoverBackgroundColor: s.colorBy ? undefined : alpha(s.color, 0.85) });
      } else {
        const r = s.points ? 3 : 0;
        datasets.push({ ...base, type: "line", borderColor: s.$color, borderWidth: s.width ?? 2, borderDash: s.dash ? [5, 4] : [],
          backgroundColor: s.type === "area" ? alpha(s.color, 0.12) : s.$color, fill: s.type === "area" ? "origin" : false,
          tension: s.tension ?? 0.25, spanGaps: s.spanGaps ?? true, borderJoinStyle: "round", borderCapStyle: "round",
          pointRadius: s.endDot === false ? r : s.data.map((_, i) => (i === lastIdx ? 4 : r)),
          pointBackgroundColor: s.hollowEnd ? s.data.map((_, i) => (i === lastIdx ? T.surface : s.$color)) : s.$color,
          pointBorderColor: s.hollowEnd ? s.$color : T.surface, pointBorderWidth: 2, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHitRadius: 12 });
      }
    });
    const y = state.y || {};
    const laneItems = (state.markers?.length || 0) + (state.today != null ? 1 : 0) + (state.ranges || []).filter((r) => r.label).length + (y.hint ? 1 : 0);
    const laneRows = laneItems === 0 ? 0 : laneItems <= 2 ? 1 : 2;
    const ctxM = canvas.getContext("2d"); ctxM.font = `500 11px ${T.font}`;
    const gutter = (state.targets || []).reduce((m, t) => Math.max(m, ctxM.measureText(t.label || "").width + 12), 0);
    const reduced = prefersReducedMotion();

    chart = new Chart(canvas, {
      type: "line",
      data: { labels: state.labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: first && !reduced ? { duration: 360, easing: "easeOutQuart" } : false,
        layout: { padding: { top: laneRows ? 8 + LANE_ROW * laneRows : 6, right: gutter || 4, left: 0, bottom: 0 } },
        interaction: { mode: "index", intersect: false },
        onClick: state.onPoint ? (e, els) => { if (els.length) state.onPoint(els[0].index); } : undefined,
        onHover: (e, els) => { e.native && (e.native.target.style.cursor = state.onPoint && els.length ? "pointer" : "default"); },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false, external: externalTooltip, filter: (it) => !it.dataset.$band },
          filler: { propagate: false },
        },
        scales: {
          x: { stacked: state.series.some((s) => s.stack), offset: state.series.some((s) => s.type === "bar"),
            grid: { display: false }, border: { color: T.axis },
            ticks: { color: T.label, font: { family: T.font, size: 11 }, maxRotation: 0, autoSkip: true, autoSkipPadding: 14,
              callback(v, i) { const l = state.labels[v]; return state.xFormat ? state.xFormat(l, v, "tick") : l; } } },
          y: { stacked: state.series.some((s) => s.stack), reverse: !!y.reverse, min: y.min, max: y.max, suggestedMin: y.suggestedMin, suggestedMax: y.suggestedMax,
            beginAtZero: !!y.beginAtZero, grid: { color: T.grid, drawTicks: false }, border: { display: false },
            ticks: { color: T.label, font: { family: T.font, size: 11 }, padding: 8, maxTicksLimit: y.ticks || 5, stepSize: y.step,
              callback: (v) => (y.fmt ? y.fmt(v) : fmt.num(v, Math.abs(v) < 10 && v % 1 ? 1 : 0)) } },
        },
      },
      // attach config in beforeInit: with animation off, Chart.js draws inside the constructor
      plugins: [{ ...annotationPlugin, beforeInit(c) { c.$ui = state; c.$theme = T; } }],
    });

    // keyboard: ←/→ walk points, Esc clears
    let ki = -1;
    canvas.addEventListener("keydown", (e) => {
      const n = state.labels.length;
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        ki = ki < 0 ? (e.key === "ArrowRight" ? 0 : n - 1) : Math.max(0, Math.min(n - 1, ki + (e.key === "ArrowRight" ? 1 : -1)));
        const els = chart.data.datasets.map((d, di) => ({ datasetIndex: di, index: ki })).filter((x) => !chart.data.datasets[x.datasetIndex].$band && chart.data.datasets[x.datasetIndex].data[ki] != null);
        chart.setActiveElements(els); chart.tooltip.setActiveElements(els, { x: 0, y: 0 }); chart.update("none");
      } else if (e.key === "Escape") { ki = -1; chart.setActiveElements([]); chart.tooltip.setActiveElements([], {}); chart.update("none"); hideTooltip(); }
      else if (e.key === "Enter" && state.onPoint && ki >= 0) state.onPoint(ki);
    });
    canvas.addEventListener("blur", () => { chart.setActiveElements([]); chart.tooltip.setActiveElements([], {}); chart.update("none"); hideTooltip(); ki = -1; });
    canvas.addEventListener("mouseleave", hideTooltip);

    // marker hover highlights the matching pinned note
    canvas.addEventListener("mousemove", (e) => {
      const lane = chart.$lane; if (!lane) return;
      const r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      let hit = null;
      if (my < chart.chartArea.top) lane.items.forEach((it) => { if (it.kind === "marker" && Math.abs(it.x - mx) < 12) hit = it; });
      lane.items.forEach((it) => (it.hot = it === hit));
      mount.querySelectorAll("[data-pin]").forEach((p) => p.classList.toggle("is-hot", !!hit && String(hit.n) === p.dataset.pin));
    });
  }

  shell(); build(true);
  const api = {
    get chart() { return chart; },
    update(next = {}) { state = { ...state, ...next }; shell(); build(false); },
    rebuild() { if (document.contains(mount)) build(false); else live.delete(api); },
    destroy() { if (chart) chart.destroy(); live.delete(api); mount.innerHTML = ""; },
  };
  live.add(api);
  return api;
}

/* ---------- ForecastChart ---------- */
/**
 * ForecastChart(mount, {
 *   title, takeaway, labels, xFormat,
 *   actual: [...],                   // observed values up to "today" (null after)
 *   projected: [...],                // from today onward (null before; first value = today's actual)
 *   p50: { lo: [], hi: [] }, p80: { lo: [], hi: [] },
 *   today,                           // index of the split
 *   goal: { value, label },
 *   y: { fmt, reverse },             // reverse when lower is better (race times)
 *   hero: { value, sub, detail },    // e.g. "1:27:40" · "projected race day" · "80% range 1:26:30–1:29:00"
 *   meaning: { tone, title, body },  // "What this means" — rendered under the chart, always visible
 *   markers, insights, ranges
 * })
 */
export function ForecastChart(mount, o) {
  const f = o.y?.fmt || ((n) => fmt.num(n, 1));
  const series = [
    { label: o.actualLabel || "Actual", data: o.actual, color: "cat-1", width: 2 },
    { label: o.projectedLabel || "Projected", data: o.projected, color: "cat-1", dash: true, hollowEnd: true, spanGaps: false },
  ];
  const bands = [];
  if (o.p80) bands.push({ ...o.p80, level: 2, label: "80% range", legend: false });
  if (o.p50) bands.push({ ...o.p50, level: 1, label: o.p80 ? "50% / 80% range" : "50% range" });
  const meaning = o.meaning ? `<div class="ui-forecast-meaning">${insightCallout({ variant: "card", tone: o.meaning.tone || "info", label: "What this means", title: o.meaning.title, body: o.meaning.body })}</div>` : "";
  return AnnotatedChart(mount, {
    ...o, cls: cx("ui-forecast", o.cls), series, bands,
    targets: [...(o.goal ? [{ value: o.goal.value, label: o.goal.label || `Goal ${f(o.goal.value)}`, legend: o.goal.legend }] : []), ...(o.targets || [])],
    footer: [meaning, o.footer].filter(Boolean).join(""),
    height: o.height || 260,
    y: { ...o.y, fmt: f },
  });
}
