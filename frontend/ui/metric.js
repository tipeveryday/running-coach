/* MetricTrend — label · value · delta · sparkline · one-line interpretation.
   The delta's color comes from whether the change is GOOD for this metric
   (HRV up = good, resting HR up = bad), and it always carries an arrow + sign. */
import { esc, cx, fmt } from "./core.js";
import { icon } from "./icons.js";
import { badge } from "./components.js";

/**
 * sparkline(values, { color: "var(--cat-1)", band: [lo, hi], label, area, height })
 * Text-free SVG, so it can scale to any width without distorting type.
 * Strokes stay 2px via vector-effect. Gaps (null) break the line.
 */
export function sparkline(values = [], o = {}) {
  const pts = values.map((v, i) => [i, v]).filter(([, v]) => v != null && isFinite(v));
  if (pts.length < 2) return `<svg class="ui-metric-spark" aria-hidden="true"></svg>`;
  const vs = pts.map(([, v]) => v).concat(o.band || []);
  let min = Math.min(...vs), max = Math.max(...vs);
  if (max === min) { max += 1; min -= 1; }
  const pad = (max - min) * 0.12; min -= pad; max += pad;
  const W = 100, H = 32, n = Math.max(values.length - 1, 1);
  const X = (i) => (i / n) * W, Y = (v) => H - ((v - min) / (max - min)) * H;
  let d = "", prev = null;
  values.forEach((v, i) => {
    if (v == null || !isFinite(v)) { prev = null; return; }
    d += `${prev == null ? "M" : "L"}${X(i).toFixed(2)},${Y(v).toFixed(2)}`; prev = i;
  });
  const last = pts[pts.length - 1];
  const area = o.area ? `<path class="ui-spark-area" d="${d}L${X(last[0])},${H}L${X(pts[0][0])},${H}Z"/>` : "";
  const band = o.band ? `<rect class="ui-spark-band" x="0" width="${W}" y="${Y(o.band[1])}" height="${Math.max(0, Y(o.band[0]) - Y(o.band[1]))}"/>` : "";
  return `<svg class="ui-metric-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="--spark:${o.color || "var(--cat-1)"}" ${o.label ? `role="img" aria-label="${esc(o.label)}"` : 'aria-hidden="true"'}>
    ${band}${area}<path class="ui-spark-line" d="${d}"/>
    <circle class="ui-spark-dot" cx="${X(last[0])}" cy="${Y(last[1])}" r="2.5" vector-effect="non-scaling-stroke"/></svg>`;
}

/**
 * metricTrend({
 *   label, icon, value, unit,
 *   delta,            // number: change vs comparison period
 *   deltaFmt,         // (d) => "+4 ms" ; default signed number
 *   deltaLabel,       // "vs 7-day avg"
 *   goodWhen: "up"|"down"|"none",
 *   series, band,     // sparkline values + optional "normal range" band [lo, hi]
 *   status: { text, tone },   // optional badge (e.g. "Balanced")
 *   read,             // one-line interpretation in plain words
 *   go / attrs,       // makes it a button (navigates)
 *   compact
 * })
 */
export function metricTrend(o = {}) {
  const d = o.delta;
  let deltaHtml = "";
  if (d != null && isFinite(d)) {
    const dir = d > 0 ? "up" : d < 0 ? "down" : "flat";
    const good = o.goodWhen === "none" || dir === "flat" ? "neutral" : dir === o.goodWhen ? "good" : "bad";
    const txt = o.deltaFmt ? o.deltaFmt(d) : fmt.signed(d, o.deltaDigits || 0);
    const word = good === "good" ? "better" : good === "bad" ? "worse" : "change";
    deltaHtml = `<span class="ui-metric-delta" data-tone="${good}">${icon(dir === "up" ? "arrow-up-right" : dir === "down" ? "arrow-down-right" : "minus", { size: 14 })}${esc(txt)}<span class="ui-sr"> ${word}${o.deltaLabel ? " " + esc(o.deltaLabel) : ""}</span></span>`;
  }
  const interactive = o.go || (o.attrs && Object.keys(o.attrs).length);
  const tag = interactive ? "button" : "div";
  const a = { ...(o.attrs || {}), ...(o.go ? { "data-go": o.go } : {}) };
  const st = o.status ? badge(o.status.text, { tone: o.status.tone, size: "sm" }) : "";
  return `<${tag} class="${cx("ui-metric", { compact: o.compact })}"${interactive ? ' type="button"' : ""}${Object.entries(a).map(([k, v]) => ` ${k}="${esc(v)}"`).join("")}>
    <span class="ui-metric-label">${o.icon ? icon(o.icon, { size: 16 }) : ""}${esc(o.label)}${st}</span>
    <span class="ui-metric-value">${esc(o.value ?? "–")}${o.unit ? `<span class="ui-unit">${esc(o.unit)}</span>` : ""}</span>
    ${deltaHtml}
    ${o.series ? sparkline(o.series, { color: o.color, band: o.band, label: o.sparkLabel || `${o.label} trend` }) : ""}
    ${o.read ? `<span class="ui-metric-read">${esc(o.read)}</span>` : ""}
  </${tag}>`;
}
