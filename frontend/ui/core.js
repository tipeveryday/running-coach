/* Shared helpers for the UI layer: escaping, class lists, formatting, tone maps.
   Every component builds markup through these so escaping and number formatting
   are done one way, everywhere. */

export const esc = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

/** Minimal rich text: **bold** and line breaks, escaped first. */
export const rich = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");

/** cx("a", cond && "b", {c: true}) -> "a b c" */
export const cx = (...parts) => parts.flatMap((p) =>
  !p ? [] : typeof p === "object" ? Object.keys(p).filter((k) => p[k]) : [p]).join(" ");

/** Attribute string from an object; skips null/false, `true` emits a bare attribute. */
export const attrs = (o = {}) => Object.entries(o)
  .filter(([, v]) => v != null && v !== false)
  .map(([k, v]) => (v === true ? ` ${k}` : ` ${k}="${esc(v)}"`)).join("");

let _uid = 0;
export const uid = (p = "ui") => `${p}-${++_uid}`;

/* ---------- Formatting (numbers always tabular in CSS; these fix the text) ---------- */
export const DASH = "–";
export const fmt = {
  /** seconds per km -> "4:03" */
  pace(s) {
    if (s == null || !isFinite(s)) return DASH;
    s = Math.round(s);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  },
  /** seconds -> "1:27:40" or "42:10" */
  clock(s) {
    if (s == null || !isFinite(s)) return DASH;
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
  },
  /** seconds -> "7h 40" / "52m" */
  dur(s) {
    if (s == null || !isFinite(s)) return DASH;
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? `${h}h ${String(m).padStart(2, "0")}` : `${m}m`;
  },
  num(v, d = 0) {
    if (v == null || !isFinite(v)) return DASH;
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  signed(v, d = 0) {
    if (v == null || !isFinite(v)) return DASH;
    const s = fmt.num(Math.abs(v), d);
    return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
  },
  date(iso, opts = { month: "short", day: "numeric" }) {
    if (!iso) return DASH;
    const d = typeof iso === "string" ? new Date(iso.length === 10 ? iso + "T00:00:00" : iso) : iso;
    return d.toLocaleDateString(undefined, opts);
  },
  weekday(iso, style = "short") { return fmt.date(iso, { weekday: style }); },
};

/* ---------- Tone vocabulary (one set, replaces the 14 legacy schemes) ---------- */
export const TONES = ["coach", "good", "warn", "bad", "info", "neutral"];
/** Map any legacy tone word to the system's tone. */
export function tone(t) {
  const k = String(t || "").toLowerCase();
  if (["good", "green", "ok", "positive", "done", "high"].includes(k)) return "good";
  if (["warn", "amber", "watch", "caution", "improve", "moderate", "yellow"].includes(k)) return "warn";
  if (["bad", "red", "danger", "act", "critical", "low", "poor"].includes(k)) return "bad";
  if (["info", "blue", "note"].includes(k)) return "info";
  if (["coach", "accent", "brand"].includes(k)) return "coach";
  return "neutral";
}
export const TONE_ICON = { coach: "sparkles", good: "circle-check", warn: "triangle-alert", bad: "octagon-alert", info: "info", neutral: "info" };
export const TONE_LABEL = { coach: "Coach", good: "On track", warn: "Watch", bad: "Act now", info: "Note", neutral: "Note" };

/* ---------- Domain vocab ---------- */
/** Workout type -> intensity zone (color) + icon. Color = intensity, icon = type. */
export const WORKOUT = {
  rest:     { zone: 0, icon: "bed",             label: "Rest" },
  recovery: { zone: 1, icon: "footprints",      label: "Recovery" },
  easy:     { zone: 2, icon: "footprints",      label: "Easy" },
  long:     { zone: 2, icon: "route",           label: "Long" },
  steady:   { zone: 3, icon: "footprints",      label: "Steady" },
  moderate: { zone: 3, icon: "footprints",      label: "Moderate" },
  tempo:    { zone: 4, icon: "zap",             label: "Tempo" },
  quality:  { zone: 4, icon: "zap",             label: "Quality" },
  threshold:{ zone: 4, icon: "zap",             label: "Threshold" },
  intervals:{ zone: 5, icon: "zap",             label: "Intervals" },
  race:     { zone: 0, icon: "flag",            label: "Race", race: true },
  strength: { zone: 0, icon: "dumbbell",        label: "Strength" },
  mobility: { zone: 0, icon: "person-standing", label: "Mobility" },
  cross:    { zone: 1, icon: "activity",        label: "Cross-train" },
};
export const workout = (type) => WORKOUT[String(type || "").toLowerCase()] || { zone: 0, icon: "activity", label: type || "Workout" };
export const zoneVar = (z) => (z >= 1 && z <= 5 ? `var(--z${z})` : "var(--text-3)");

/** Read a CSS custom property from the root (live — reflects the current theme). */
export const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export const prefersReducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
