/* Core components. Static pieces are string builders (they drop straight into the
   app's existing innerHTML templates); interactive behavior is wired once by
   initUI() through event delegation, so re-rendered markup keeps working. */
import { esc, cx, attrs, uid, tone as toTone, TONE_ICON, fmt, zoneVar } from "./core.js";
import { icon } from "./icons.js";

/* ---------- Button ---------- */
/**
 * button({ label, variant: "primary"|"secondary"|"ghost"|"danger", size: "sm"|"md"|"lg",
 *          icon, iconRight, iconOnly, href, type, attrs, block, busy, disabled })
 * Icon-only buttons must pass `label` — it becomes the accessible name.
 */
export function button(o = {}) {
  const { label = "", variant = "secondary", size, icon: ic, iconRight, iconOnly, href, type = "button", block, busy, disabled } = o;
  const cls = cx("ui-btn", variant, size, { icon: iconOnly, block, "is-busy": busy }, o.cls);
  const inner = `${ic ? icon(ic, { size: size === "sm" ? 16 : 18 }) : ""}${iconOnly ? `<span class="ui-sr">${esc(label)}</span>` : esc(label)}${iconRight ? icon(iconRight, { size: 16 }) : ""}`;
  const a = { ...(o.attrs || {}), title: iconOnly ? label : o.title, "aria-busy": busy ? "true" : null };
  if (href) return `<a class="${cls}" href="${esc(href)}"${attrs(a)}>${inner}</a>`;
  return `<button class="${cls}" type="${type}"${attrs({ ...a, disabled })}>${inner}</button>`;
}

/* ---------- Badge / chips ---------- */
/** badge(text, { tone, icon, size: "sm", outline }) — tone is required to carry meaning, never color alone. */
export function badge(text, o = {}) {
  const t = o.tone ? toTone(o.tone) : null;
  const ic = o.icon === false ? "" : o.icon || (t && t !== "neutral" && o.autoIcon ? TONE_ICON[t] : "");
  return `<span class="${cx("ui-badge", o.size, { outline: o.outline })}"${t ? ` data-tone="${t}"` : ""}>${ic ? icon(ic, { size: 14 }) : ""}${esc(text)}</span>`;
}
/** zoneChip(3) -> colored square + "Z3" label (plus optional name). */
export const zoneChip = (z, name = "") =>
  `<span class="ui-zone-chip" style="--zc:${zoneVar(z)}"><i aria-hidden="true"></i>Z${esc(z)}${name ? ` <span class="ui-faint">${esc(name)}</span>` : ""}</span>`;

/* ---------- Card ---------- */
/**
 * card({ title, sub, icon, actions, body, cls, tone, flush, as, attrs })
 * Card patterns: stat (statTile inside), chart (AnnotatedChart mounts into body), narrative (insights).
 * Cards never nest — use .ui-well for an inset area.
 */
export function card(o = {}) {
  const tag = o.as || "section";
  const head = o.title || o.actions
    ? `<header class="ui-card-head"><div>${o.title ? `<h3 class="ui-card-title">${o.icon ? icon(o.icon, { size: 18 }) : ""}${esc(o.title)}</h3>` : ""}${o.sub ? `<p class="ui-card-sub">${esc(o.sub)}</p>` : ""}</div>${o.actions ? `<div class="ui-card-actions">${o.actions}</div>` : ""}</header>`
    : "";
  return `<${tag} class="${cx("ui-card", { flush: o.flush }, o.cls)}"${o.tone ? ` data-tone="${toTone(o.tone)}"` : ""}${attrs(o.attrs)}>${head}${o.body || ""}</${tag}>`;
}

/* ---------- Stat tile (KPI) ---------- */
/** statTile({ label, value, unit, foot, icon, tone, inset, href/onclick via attrs }) */
export function statTile(o = {}) {
  const interactive = o.attrs && (o.attrs["data-go"] || o.attrs["data-action"]);
  const tag = interactive ? "button" : "div";
  return `<${tag} class="${cx("ui-stat", { inset: o.inset }, o.cls)}"${interactive ? ' type="button"' : ""}${o.tone ? ` data-tone="${toTone(o.tone)}"` : ""}${attrs(o.attrs)}>
    <span class="ui-stat-label">${o.icon ? icon(o.icon, { size: 16 }) : ""}${esc(o.label)}</span>
    <span class="ui-stat-value">${esc(o.value ?? fmt.num(null))}${o.unit ? `<small>${esc(o.unit)}</small>` : ""}</span>
    ${o.foot ? `<span class="ui-stat-foot">${o.foot}</span>` : ""}
  </${tag}>`;
}

/* ---------- Form controls ---------- */
/** field({ label, id, hint, error, control }) — control is the input markup; ids wire label/hint/error. */
export function field(o) {
  const id = o.id || uid("f");
  const hintId = o.hint ? `${id}-hint` : null, errId = o.error ? `${id}-err` : null;
  const described = [hintId, errId].filter(Boolean).join(" ") || null;
  const ctrl = (o.control || input({ id })).replace(/^<(input|select|textarea)/, (m) => `${m} id="${id}"${described ? ` aria-describedby="${described}"` : ""}${o.error ? ' aria-invalid="true"' : ""}`);
  return `<div class="${cx("ui-field", { "is-invalid": o.error })}">
    <label class="ui-label" for="${id}">${esc(o.label)}</label>${ctrl}
    ${o.hint ? `<span class="ui-hint" id="${hintId}">${esc(o.hint)}</span>` : ""}
    ${o.error ? `<span class="ui-error-text" id="${errId}">${icon("octagon-alert", { size: 14 })}${esc(o.error)}</span>` : ""}
  </div>`;
}
export const input = (o = {}) => `<input class="ui-input"${attrs({ type: o.type || "text", name: o.name, value: o.value, placeholder: o.placeholder, autocomplete: o.autocomplete, inputmode: o.inputmode, required: o.required, ...(o.attrs || {}) })} />`;
export const textarea = (o = {}) => `<textarea class="ui-textarea"${attrs({ name: o.name, placeholder: o.placeholder, rows: o.rows, ...(o.attrs || {}) })}>${esc(o.value || "")}</textarea>`;
export const select = (o = {}) => `<select class="ui-select"${attrs({ name: o.name, ...(o.attrs || {}) })}>${(o.options || []).map((op) => {
  const [v, l] = Array.isArray(op) ? op : [op, op];
  return `<option value="${esc(v)}"${String(v) === String(o.value) ? " selected" : ""}>${esc(l)}</option>`;
}).join("")}</select>`;

/**
 * segmented({ label, options: [[value, text, icon?]], value, name })
 * A radio group: arrow keys move, fires "ui:change" with detail.value on the group.
 */
export function segmented(o) {
  return `<div class="ui-seg" role="radiogroup" aria-label="${esc(o.label)}"${o.name ? ` data-name="${esc(o.name)}"` : ""}>${(o.options || []).map(([v, t, ic]) => {
    const on = String(v) === String(o.value);
    return `<button type="button" role="radio" aria-checked="${on}" tabindex="${on ? 0 : -1}" data-value="${esc(v)}">${ic ? icon(ic, { size: 16 }) : ""}${esc(t)}</button>`;
  }).join("")}</div>`;
}

/* ---------- Tabs ---------- */
/**
 * tabs({ label, items: [{ id, label, icon, panel }], value })
 * WAI-ARIA tabs with roving tabindex; fires "ui:tab" with detail.id on the tablist.
 */
export function tabs(o) {
  const base = o.id || uid("tabs");
  const sel = o.value || (o.items[0] && o.items[0].id);
  const list = `<div class="ui-tabs" role="tablist" aria-label="${esc(o.label)}">${o.items.map((t) => {
    const on = t.id === sel;
    return `<button class="ui-tab" role="tab" id="${base}-t-${esc(t.id)}" aria-controls="${base}-p-${esc(t.id)}" aria-selected="${on}" tabindex="${on ? 0 : -1}" data-tab="${esc(t.id)}">${t.icon ? icon(t.icon, { size: 16 }) : ""}${esc(t.label)}</button>`;
  }).join("")}</div>`;
  const panels = o.items.map((t) => `<div class="ui-tabpanel" role="tabpanel" id="${base}-p-${esc(t.id)}" aria-labelledby="${base}-t-${esc(t.id)}" tabindex="0"${t.id === sel ? "" : " hidden"}>${t.panel || ""}</div>`).join("");
  return `<div class="ui-tabset">${list}${panels}</div>`;
}

/* ---------- Dialog: modal + sheet ---------- */
/**
 * openDialog({ kind: "modal"|"sheet", wide, title, sub, body, footer, onClose, label })
 * Uses native <dialog>.showModal(): focus trap, Esc, inert background, and focus
 * returns to the opener on close. Returns { el, body, close(), setBody(html) }.
 */
export function openDialog(o = {}) {
  const d = document.createElement("dialog");
  const titleId = uid("dlg");
  d.className = cx("ui-dialog", o.kind || "modal", { wide: o.wide });
  d.setAttribute("aria-labelledby", titleId);
  d.innerHTML = `
    <header class="ui-dialog-head"><div><h2 class="ui-h3" id="${titleId}">${esc(o.title || "")}</h2>${o.sub ? `<p class="ui-caption" style="margin-top:var(--sp-1)">${esc(o.sub)}</p>` : ""}</div>
      ${button({ label: "Close", variant: "ghost", iconOnly: true, icon: "x", attrs: { "data-close": "" } })}</header>
    <div class="ui-dialog-body" tabindex="0">${o.body || ""}</div>
    ${o.footer ? `<footer class="ui-dialog-foot">${o.footer}</footer>` : ""}`;
  const opener = document.activeElement;
  document.body.appendChild(d);
  const close = () => { if (d.open) d.close(); };
  d.addEventListener("click", (e) => {
    if (e.target === d || e.target.closest("[data-close]")) close();   // backdrop click or close button
  });
  d.addEventListener("close", () => {
    d.remove();
    if (opener && opener.focus && document.contains(opener)) opener.focus();
    o.onClose && o.onClose();
  });
  d.showModal();
  return { el: d, body: d.querySelector(".ui-dialog-body"), close, setBody(html) { this.body.innerHTML = html; } };
}

/* ---------- Toast ---------- */
let _toasts;
/** toast(msg, { tone, ms }) — announced politely to screen readers. */
export function toast(msg, o = {}) {
  if (!_toasts) {
    _toasts = document.createElement("div");
    _toasts.className = "ui-toasts"; _toasts.setAttribute("role", "status"); _toasts.setAttribute("aria-live", "polite");
    document.body.appendChild(_toasts);
  }
  const t = toTone(o.tone || "info");
  const n = document.createElement("div");
  n.className = "ui-toast"; n.dataset.tone = t;
  n.innerHTML = `${icon(TONE_ICON[t], { size: 18 })}<div class="ui-toast-msg">${esc(msg)}</div>${button({ label: "Dismiss", variant: "ghost", size: "sm", iconOnly: true, icon: "x", attrs: { "data-dismiss": "" } })}`;
  _toasts.appendChild(n);
  const kill = () => { n.classList.add("is-leaving"); setTimeout(() => n.remove(), 200); };
  n.querySelector("[data-dismiss]").addEventListener("click", kill);
  setTimeout(kill, o.ms || (t === "bad" ? 7000 : 4000));
}

/* ---------- Navigation ---------- */
/**
 * nav({ items: [{ id, label, short, icon, href }], current })
 * One config renders both the desktop section nav (in the top bar) and the
 * mobile bottom bar. Max 5 in the bottom bar; extra items go to `more`.
 */
export function navLinks(o) {
  return `<nav class="ui-nav" aria-label="Sections">${o.items.map((it) =>
    `<a href="${esc(it.href || "#" + it.id)}" data-nav="${esc(it.id)}"${it.id === o.current ? ' aria-current="page"' : ""}>${icon(it.icon, { size: 16 })}${esc(it.label)}</a>`).join("")}</nav>`;
}
export function bottomNav(o) {
  return `<nav class="ui-bottomnav" aria-label="Sections">${o.items.map((it) =>
    `<a href="${esc(it.href || "#" + it.id)}" data-nav="${esc(it.id)}"${it.id === o.current ? ' aria-current="page"' : ""}>${icon(it.icon, { size: 22 })}<span>${esc(it.short || it.label)}</span></a>`).join("")}</nav>`;
}
export function topBar(o) {
  return `<header class="ui-topbar"><div class="ui-container ui-topbar-in">
    <a class="ui-brand" href="#${esc(o.home || "")}" aria-label="${esc(o.name)} home">
      <span class="ui-brand-mark" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15l5-5 4 4 7-8"/></svg></span>
      <span style="min-width:0"><span class="ui-brand-name">${esc(o.name)}</span>${o.sub ? `<span class="ui-brand-sub" style="display:block">${esc(o.sub)}</span>` : ""}</span>
    </a>
    ${o.nav ? navLinks(o.nav) : ""}
    <span class="ui-topbar-spacer"></span>
    <div class="ui-topbar-actions">${o.actions || ""}</div>
  </div></header>`;
}
/** Update aria-current on every nav after a route change. */
export function setNavCurrent(id) {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
}

/* ---------- States ---------- */
/** emptyState({ icon, title, body, action, compact }) — for "nothing here yet". */
export const emptyState = (o = {}) => `<div class="${cx("ui-state", { compact: o.compact })}" data-tone="${o.tone || "neutral"}">
  <span class="ui-state-icon">${icon(o.icon || "info", { size: 20 })}</span>
  <div>${o.title ? `<div class="ui-state-title">${esc(o.title)}</div>` : ""}${o.body ? `<div class="ui-state-body">${esc(o.body)}</div>` : ""}</div>${o.action || ""}</div>`;
/** errorState({ title, body, retry: true }) — retry button carries data-action="retry". */
export const errorState = (o = {}) => emptyState({
  icon: "octagon-alert", tone: "bad", title: o.title || "Couldn't load this", compact: o.compact,
  body: o.body || "Something went wrong reaching the server. Your data is safe.",
  action: o.retry === false ? "" : button({ label: "Try again", size: "sm", icon: "refresh-cw", attrs: { "data-action": "retry" } }),
});
export const loading = (label = "Loading") => `<span class="ui-loading" role="status"><span class="ui-spinner" aria-hidden="true"></span>${esc(label)}…</span>`;

/** Skeletons sized like the content they stand in for, so nothing shifts on load. */
export const skeleton = {
  lines: (n = 3) => `<div aria-hidden="true">${Array.from({ length: n }, (_, i) => `<div class="ui-skel text" style="width:${i === n - 1 ? 60 : 100 - i * 6}%"></div>`).join("")}</div>`,
  stat: () => `<div class="ui-stat" aria-hidden="true"><div class="ui-skel text" style="width:50%"></div><div class="ui-skel num"></div><div class="ui-skel text" style="width:70%"></div></div>`,
  chart: (h = 240) => `<div aria-hidden="true"><div class="ui-skel title"></div><div class="ui-skel text" style="width:55%"></div><div class="ui-skel block" style="height:${h}px;margin-top:var(--sp-4)"></div></div>`,
  card: (n = 3) => `<div class="ui-card" aria-busy="true"><span class="ui-sr">Loading</span><div class="ui-skel title"></div>${skeleton.lines(n)}</div>`,
};

/* ---------- Disclosure ---------- */
export const disclosure = (o) => `<details class="ui-disclosure"${o.open ? " open" : ""}${attrs(o.attrs)}>
  <summary>${o.icon ? icon(o.icon, { size: 18 }) : ""}<span>${esc(o.title)}</span>${o.count != null ? badge(String(o.count), { size: "sm" }) : ""}${o.meta ? `<span class="ui-caption" style="font-weight:400">${esc(o.meta)}</span>` : ""}${icon("chevron-down", { size: 18, cls: "chev" })}</summary>
  <div class="ui-disclosure-body">${o.body || ""}</div></details>`;

/* ---------- Table ---------- */
/**
 * table({ caption, cols: [{ key, label, align: "r", num, fmt(v,row) }], rows, dense })
 * Always a real <table> with scoped headers; wraps in a horizontal scroller on narrow screens.
 */
export function table(o) {
  const head = o.cols.map((c) => `<th scope="col"${c.align === "r" ? ' class="r"' : ""}>${esc(c.label)}</th>`).join("");
  const body = o.rows.map((r, i) => `<tr${o.rowAttrs ? attrs(o.rowAttrs(r, i)) : ""}>${o.cols.map((c, j) => {
    const v = c.fmt ? c.fmt(r[c.key], r) : esc(r[c.key] ?? "–");
    const tag = j === 0 && o.rowHeaders ? "th" : "td";
    return `<${tag}${tag === "th" ? ' scope="row"' : ""} class="${cx({ r: c.align === "r", num: c.num })}">${v}</${tag}>`;
  }).join("")}</tr>`).join("");
  return `<div class="ui-table-wrap"><table class="${cx("ui-table", { dense: o.dense })}">${o.caption ? `<caption class="ui-sr">${esc(o.caption)}</caption>` : ""}<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/* ---------- Zone bar ---------- */
/** zoneBar([{ zone, value }]) — stacked share with 2px gaps; pair with a labeled table/legend. */
export function zoneBar(parts, label = "Time in zone") {
  const tot = parts.reduce((s, p) => s + (p.value || 0), 0) || 1;
  const desc = parts.map((p) => `Z${p.zone} ${Math.round((p.value || 0) / tot * 100)}%`).join(", ");
  return `<div class="ui-zonebar" role="img" aria-label="${esc(label)}: ${esc(desc)}">${parts.filter((p) => p.value > 0).map((p) =>
    `<i style="flex:${p.value};--zc:${zoneVar(p.zone)}"></i>`).join("")}</div>`;
}

/* =========================================================================
   Behavior — wired once, by delegation.
   ========================================================================= */
let _tipEl = null, _tipFor = null;
function tipEl() {
  if (!_tipEl) { _tipEl = document.createElement("div"); _tipEl.className = "ui-tooltip"; _tipEl.id = "ui-tooltip"; _tipEl.setAttribute("role", "tooltip"); document.body.appendChild(_tipEl); }
  return _tipEl;
}
/** Position the shared tooltip next to a rect (or a point), flipping to stay on screen. */
export function showTooltip(html, anchor) {
  const t = tipEl();
  t.innerHTML = html; t.classList.add("is-on");
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y, width: 0 };
  const w = t.offsetWidth, h = t.offsetHeight, pad = 8;
  let x = r.left + r.width / 2 - w / 2, y = r.top - h - pad;
  if (y < pad) y = r.bottom + pad;
  x = Math.max(pad, Math.min(x, innerWidth - w - pad));
  t.style.left = `${x}px`; t.style.top = `${y}px`;
}
export function hideTooltip() { if (_tipEl) _tipEl.classList.remove("is-on"); _tipFor = null; }
/** tip(text, html) -> a trigger span; the tooltip shows on hover, focus and tap. Never put a coaching conclusion in one. */
export const tip = (label, content) => `<span class="ui-tip-trigger" tabindex="0" data-tip="${esc(content)}">${esc(label)}</span>`;

let _wired = false;
export function initUI(root = document) {
  if (_wired) return; _wired = true;

  // Tooltips: hover + keyboard focus + tap toggle
  const show = (el) => { _tipFor = el; el.setAttribute("aria-describedby", "ui-tooltip"); showTooltip(esc(el.dataset.tip), el); };
  root.addEventListener("pointerover", (e) => { const el = e.target.closest("[data-tip]"); if (el && e.pointerType === "mouse") show(el); });
  root.addEventListener("pointerout", (e) => { const el = e.target.closest("[data-tip]"); if (el && e.pointerType === "mouse") hideTooltip(); });
  root.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el) show(el); });
  root.addEventListener("focusout", (e) => { if (e.target.closest("[data-tip]")) hideTooltip(); });
  root.addEventListener("click", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) { if (_tipFor === el) hideTooltip(); else show(el); }
    else if (_tipFor) hideTooltip();
  });
  addEventListener("scroll", () => _tipFor && hideTooltip(), { passive: true });
  root.addEventListener("keydown", (e) => { if (e.key === "Escape" && _tipFor) hideTooltip(); });

  // Segmented control: click + arrow keys, roving tabindex
  const segPick = (btn) => {
    const g = btn.closest(".ui-seg");
    g.querySelectorAll("[role=radio]").forEach((b) => { const on = b === btn; b.setAttribute("aria-checked", on); b.tabIndex = on ? 0 : -1; });
    g.dispatchEvent(new CustomEvent("ui:change", { bubbles: true, detail: { value: btn.dataset.value, name: g.dataset.name } }));
  };
  // Tabs: click + arrow/Home/End, roving tabindex, panels toggle
  const tabPick = (btn, focus) => {
    const list = btn.closest("[role=tablist]");
    list.querySelectorAll("[role=tab]").forEach((b) => {
      const on = b === btn; b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1;
      const p = document.getElementById(b.getAttribute("aria-controls")); if (p) p.hidden = !on;
    });
    if (focus) btn.focus();
    list.dispatchEvent(new CustomEvent("ui:tab", { bubbles: true, detail: { id: btn.dataset.tab } }));
  };
  root.addEventListener("click", (e) => {
    const r = e.target.closest(".ui-seg [role=radio]"); if (r) segPick(r);
    const t = e.target.closest("[role=tablist] [role=tab]"); if (t && t.getAttribute("aria-controls")) tabPick(t);
  });
  root.addEventListener("keydown", (e) => {
    const r = e.target.closest(".ui-seg [role=radio], [role=tablist] [role=tab]");
    if (!r) return;
    const items = [...r.parentElement.querySelectorAll(r.getAttribute("role") === "tab" ? "[role=tab]" : "[role=radio]")];
    const i = items.indexOf(r);
    const next = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
    if (next == null) return;
    e.preventDefault();
    const b = items[(next + items.length) % items.length];
    if (b.getAttribute("role") === "tab") { if (b.getAttribute("aria-controls")) tabPick(b, true); else { b.focus(); b.click(); } }
    else { b.focus(); segPick(b); }
  });
}
