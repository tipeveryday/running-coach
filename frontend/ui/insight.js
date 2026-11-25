/* InsightCallout / CoachTip — the one shape every piece of coaching text takes.
   Anatomy: [icon] LABEL / headline (must stand alone) / body / why (collapsed) / actions.
   Severity always shows an icon AND a word; color never carries it alone. */
import { esc, rich, cx, tone as toTone, TONE_ICON, TONE_LABEL } from "./core.js";
import { icon } from "./icons.js";

/**
 * insightCallout({
 *   tone: "coach"|"good"|"warn"|"bad"|"info"   (legacy words like "amber" are mapped),
 *   variant: "card" (default) | "lead" | "inline" | "pinned",
 *   label,       // override the severity word ("Today's call", "Recovery")
 *   title,       // headline: lead with the action, <= ~80 chars
 *   body,        // 1–3 sentences; **bold** allowed; or an array -> bullet list
 *   why,         // collapsed supporting evidence (string or array)
 *   how,         // optional "How:" line (legacy insight shape)
 *   actions,     // button markup
 *   n,           // pinned only: the chart marker number this note explains
 *   icon,        // override icon
 * })
 */
export function insightCallout(o = {}) {
  const variant = o.variant || "card";
  const t = toTone(o.tone || (variant === "lead" ? "coach" : "info"));
  const label = o.label === false ? "" : (o.label || TONE_LABEL[t]);
  const bodyHtml = Array.isArray(o.body)
    ? `<ul>${o.body.map((b) => `<li>${rich(b)}</li>`).join("")}</ul>`
    : o.body ? rich(o.body) : "";
  const howHtml = o.how ? `${bodyHtml ? "<br>" : ""}<b>How:</b> ${rich(o.how)}` : "";
  const why = o.why
    ? `<details class="ui-insight-why"><summary>Why this call</summary><div>${Array.isArray(o.why) ? o.why.map(rich).join(" · ") : rich(o.why)}</div></details>`
    : "";
  const extra = why || o.actions ? `<div class="ui-insight-extra">${why}${o.actions ? `<div class="ui-insight-actions">${o.actions}</div>` : ""}</div>` : "";

  if (variant === "pinned") {
    return `<div class="ui-insight pinned" data-tone="${t}" data-pin="${esc(o.n)}">
      <span class="ui-insight-icon" aria-hidden="true">${esc(o.n)}</span>
      <div class="ui-insight-title">${esc(o.title)}</div>
      ${bodyHtml || howHtml ? `<div class="ui-insight-body">${bodyHtml}${howHtml}</div>` : ""}</div>`;
  }
  return `<div class="${cx("ui-insight", variant !== "card" && variant)}" data-tone="${t}" role="${t === "bad" ? "alert" : "note"}">
    <span class="ui-insight-icon">${icon(o.icon || TONE_ICON[t], { size: 18 })}</span>
    ${label ? `<span class="ui-insight-label">${esc(label)}</span>` : ""}
    ${o.title ? `<div class="ui-insight-title">${rich(o.title)}</div>` : ""}
    ${bodyHtml || howHtml ? `<div class="ui-insight-body">${bodyHtml}${howHtml}</div>` : ""}
    ${extra}
  </div>`;
}

/** A stack of callouts, e.g. the legacy `insights` arrays ({tone, title, detail, how}). */
export function insightList(items = [], o = {}) {
  if (!items.length) return "";
  return `<div class="ui-insights">${items.map((i) => insightCallout({
    variant: o.variant || "inline", tone: i.tone, title: i.title, body: i.detail ?? i.body, how: i.how, label: o.label,
  })).join("")}</div>`;
}

/** Shorthand for the page's single most important decision. */
export const coachTip = (o) => insightCallout({ ...o, variant: "lead", tone: o.tone || "coach", label: o.label || "Today's call" });
