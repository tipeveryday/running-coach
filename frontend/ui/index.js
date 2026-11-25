/* Public entry for the UI layer: import from here, not from individual files.
   import { button, AnnotatedChart, insightCallout, initUI } from "/static/ui/index.js"; */
export * from "./core.js";
export { icon, ICON_NAMES } from "./icons.js";
export * from "./components.js";
export * from "./insight.js";
export * from "./metric.js";
export * from "./charts.js";
export * from "./calendar.js";
export * from "./activity.js";

import { refreshCharts } from "./charts.js";

/** Theme: "dark" | "light". Persists, flips tokens, re-themes charts. */
const THEME_KEY = "rc_theme";
export function getTheme() { return document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"); }
export function setTheme(mode) {
  const root = document.documentElement;
  root.classList.add("is-theming");               // swap colors instantly, not via 100s of transitions
  root.setAttribute("data-theme", mode);
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("is-theming")));
  try { localStorage.setItem(THEME_KEY, mode); } catch (_) { /* private mode */ }
  refreshCharts();
  document.dispatchEvent(new CustomEvent("ui:theme", { detail: { theme: mode } }));
}
export function restoreTheme() {
  let saved = null; try { saved = localStorage.getItem(THEME_KEY); } catch (_) { /* private mode */ }
  if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
}
