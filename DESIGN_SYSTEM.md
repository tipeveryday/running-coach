# Running Coach design system

**Direction: "Calm Premium".** Quiet slate surfaces, one ember accent, and data colours
that always carry meaning. The app is read early in the morning and right after a run,
so it should feel unhurried: the coach's call is said once, in plain words, then backed
by the numbers.

Live references (open them through the running app at `/static/design/…`):

- `frontend/design/specimen.html` shows tokens, type, palettes, and both themes.
- `frontend/design/components.html` is the component gallery with every state.

## The one rule

**`frontend/design/tokens.css` is the only place a colour, size, radius, shadow,
duration or z-index is written down.** Components and page styles use `var(--…)` and
nothing else. JavaScript that needs a token at runtime (Chart.js, canvas) reads it with
`UI.token("--name")`, so it re-themes along with the CSS.

Files and their jobs:

| File | Holds | May contain raw values? |
|---|---|---|
| `design/tokens.css` | every token, for dark, light, and the OS-preference light theme | yes, this is the only file |
| `ui/ui.css` | component styles (`.ui-*`) | no |
| `app.css` | app shell and per-page layout (`app-*`, `cx-` Coach, `ax-` Activity, `rs-` run sheet, `lg-` log, `fx-` Fitness, `sr-` Sleep/Recovery, `pl-` Plan) | no |
| `ui/*.js` | component markup and behaviour (ES modules, no build step) | no |

## Themes

Dark is the default. Light applies when the user picks it (`html[data-theme="light"]`,
saved to `localStorage.rc_theme`) or, with no saved choice, when the OS asks for it.
Light is a separate set of values tuned for a white surface, not dark mode inverted.
An inline script in `index.html` sets the theme before first paint, so there's no
flash. `UI.setTheme()` briefly adds `.is-theming` to switch without transitions, and
`refreshCharts()` repaints every live chart.

## Colour

Each colour group has one job. Don't borrow a colour from another group for its looks.

| Group | Tokens | Means |
|---|---|---|
| Surfaces | `--bg`, `--surface-1..3`, `--surface-overlay`, `--scrim` | canvas → card → inset well → hover/selected |
| Lines | `--border` (decorative), `--border-strong` (must read), `--border-input` (≥ 3:1) | |
| Ink | `--text-1` primary, `--text-2` secondary, `--text-3` captions and axis labels | all ≥ 4.5:1 on every surface |
| Accent (ember) | `--accent`, `--accent-text`, `--accent-soft`, `--accent-line`, `--on-accent`, `--focus-ring` | "you / today", the primary action, focus. **One** accent per view region. |
| Status | `--good`, `--warn`, `--bad`, `--info` (+ `-soft` fill, `-line` ring), `--neutral-soft` | state only, and always shown with an icon and a word (`TONE_ICON`, `TONE_LABEL`) |

### Data-viz scales

| Scale | Tokens | Use |
|---|---|---|
| Categorical | `--cat-1..6` | identity. **Fixed order, never cycled.** Slot 1 (ember) is always "you" or the primary series. Seven or more series get folded into "Other" or split into small multiples. |
| Intensity | `--z1..z5` | HR zones, pace zones *and* workout type: one intensity scale everywhere, so Z4 threshold is the same colour on a split, a calendar cell and a zone bar |
| Load status | `--load-under`, `--load-optimal`, `--load-high`, `--load-over`, `--load-none` | ACWR / form |
| Sleep stages | `--sleep-deep`, `--sleep-light`, `--sleep-rem`, `--sleep-awake` | hypnogram and stage meters |
| Sequential | `--seq-1..7` | one hue (blue), low → high |
| Diverging | `--div-neg-2 … --div-mid … --div-pos-2` | blue ↔ grey ↔ red, with a grey midpoint and never a hue in the middle |
| Chart chrome | `--viz-grid`, `--viz-axis`, `--viz-label`, `--viz-crosshair`, `--viz-band(-2)`, `--viz-highlight`, `--viz-target` | quiet grid and axes, forecast bands, goal lines |

**Validation** used the dataviz validator: OKLab ΔE ×100 and Machado (2009) colour-blindness
simulation, on the chart surface (`#151a21` dark, `#ffffff` light). Worst adjacent pairs:

| Scale | Mode | CVD ΔE | Normal ΔE | Contrast |
|---|---|---|---|---|
| Categorical | dark / light | 8.4 / 8.6 | 19.7 / 18.7 | all ≥ 3:1 |
| Intensity Z1–Z5 | dark / light | 11.8 / 10.7 | 16.8 / 15.3 | one swatch at 2.96–3.0:1 † |
| Sleep stages | dark / light | 12.5 / 11.2 | 16.1 / 23.5 | one swatch at 2.97–2.98:1 † |
| Load status | dark / light | 10.5 / 10.9 | 18.2 / 18.9 | dark all ≥ 3:1; light one at 2.99:1 † |

† The validator allows this only with "relief", meaning visible labels or a table view.
Every chart here has both, and zones are always labelled (`Z4`, never a colour chip on
its own). Re-run the validator if you change any scale:

```bash
uv run python -I -X utf8 <dataviz skill>/scripts/validate_palette.py "#hex,#hex,…" --mode dark --surface "#151a21"
```

## Typography

Inter (optical sizing) with `font-variant-numeric: tabular-nums` on every number.

| Token | px | Use |
|---|---|---|
| `--fs-micro` | 11 | overlines, axis ticks (nothing else) |
| `--fs-caption` | 12 | captions, legends, chart labels |
| `--fs-sm` | 13 | dense UI, table cells |
| `--fs-body` | 15 | body and coach text (max `--measure` 68ch) |
| `--fs-lead` | 17 | the coach's headline paragraph |
| `--fs-h3` / `--fs-h2` / `--fs-h1` | 16 / 20 / 26 | card, section, page title |
| `--fs-num-sm/md/lg/xl` | 17 / 24 / 34 / 48 | inline KPI, stat tile, hero metric, the single headline number |

Big numbers and headings use `--tracking-tight`; uppercase overlines use `--tracking-wide`.

## Space, layout, shape, depth, motion

- **Spacing** is on a 4px base: `--sp-1` (2px) … `--sp-11` (64px). Use the steps, not in-between values.
- **Layout:** `--container` 1240px. `--gutter`, `--grid-gap` and `--card-pad` grow at
  768px and 1280px. Below 768px, navigation moves to the bottom bar (`--bottomnav-h`).
- **Radius:** `--r-xs` data-mark ends and tags, `--r-sm` chips and inputs, `--r-md`
  buttons and tiles, `--r-lg` cards, `--r-xl` sheets and modals.
- **Elevation:** dark mode lifts by surface step plus edge, not by big shadows.
  `--elev-1` cards, `--elev-2` popovers, `--elev-3` dialogs.
- **Motion:** `--dur-1` press, `--dur-2` toggles and tooltips, `--dur-3` sheets,
  `--dur-4` the first chart reveal only. With `prefers-reduced-motion`, durations drop to
  1ms, the skeleton shimmer, panel fade, refresh spin and chat "thinking" dots stop,
  charts don't animate, and scrolling jumps instead of gliding
  (`UI.prefersReducedMotion()`). Busy spinners keep turning in place: they're the only
  sign that something is loading, and they don't move across the screen.
- **Z-index:** use the `--z-*` ladder only (sticky < dropdown < overlay < modal < toast < tooltip).

## Icons

The Lucide subset is vendored in `ui/icons.js`: `UI.icon(name, { size, label })`. With no
`label` the icon is decorative (`aria-hidden`); with one it's announced. Default sizes
are 16 inline and 20 in buttons/nav. To add an icon, add its paths to `icons.js`
(`ICON_NAMES` lists what's there).

## Components (`ui/index.js` → `import * as UI`)

| Group | Exports |
|---|---|
| Primitives | `button`, `badge`, `zoneChip`, `card`, `statTile`, `field` / `input` / `textarea` / `select`, `segmented`, `tabs`, `disclosure`, `table`, `zoneBar` |
| Navigation | `topBar`, `navLinks`, `bottomNav`, `setNavCurrent` |
| Overlays | `openDialog` (native `<dialog>`, sheet or modal), `toast`, `tip` / `showTooltip` / `hideTooltip` |
| States | `emptyState`, `errorState`, `loading`, `skeleton` |
| Coach voice | `insightCallout` (variants `card`, `lead`, `inline`, `pinned`), `insightList`, `coachTip` |
| Metrics | `metricTrend`, `sparkline` |
| Charts | `AnnotatedChart`, `ForecastChart`, `refreshCharts`, `color`, `alpha` |
| Training | `CalendarGrid` (month / week / agenda), `workoutCell`, `ActivityDeepDive`, `splitsTable`, `zoneBreakdown` |
| Helpers | `esc`, `rich`, `cx`, `attrs`, `uid`, `fmt` (pace, clock, dur, num, signed, date, weekday), `tone`, `workout`, `zoneVar`, `token`, `prefersReducedMotion`, `getTheme` / `setTheme` / `restoreTheme` |

Every component returns an HTML string (or, for the charts and calendar, an instance
with `update` / `set` / `destroy`) and gets its interactive behaviour from one delegated
`initUI()`. Tones are always one of `coach`, `good`, `warn`, `bad`, `info`, `neutral`.
`tone()` maps older words like "amber" onto these.

## Charts

`AnnotatedChart` and `ForecastChart` are the only way the app draws a chart. Pages call
them through `uiChart(id, cfg)` in `app.js`, which keeps one live instance per mount. The
one exception is the workout blueprint in the day sheet (bars of unequal width), which
uses raw Chart.js but takes its colours from tokens and comes with a legend and a table.

1. **Title plus one-line takeaway.** The takeaway says what the chart shows ("Up 2.1 since
   July") and is also the canvas's accessible name.
2. **Text never sits on data.** Event markers, range labels and "Today" go in the
   annotation lane above the plot. Target lines are labelled in the right gutter.
3. **One y-axis.** Two measures with different units are two charts. No dual axes, ever.
4. **Colour follows the entity, not its rank.** You are `--cat-1` in every chart.
5. **Thin marks:** 2px lines, rounded bar ends, recessive grid.
6. **Legend whenever there are ≥ 2 series.** A single series is named by its title.
7. **Accessible by default:** the chart is focusable, ←/→ read points aloud, and
   "View as table" sits under every chart.
8. **Forecasts show their uncertainty:** an 80% band around the projection, the goal
   as a target line, and the projection split off as a dashed line after "Today".

## Voice in the UI

The coach is a calm, specific training partner, not a hype machine.

- **Lead with the call, then the reason.** "Easy run, banked clean" before the numbers.
- **Plain words, real numbers.** "4:10/km for 8 km", not "moderate tempo effort".
- **Honest about uncertainty.** Say "likely 1:27–1:29", and flag when Garmin and the
  plan disagree. Never invent data to fill a panel: an empty state that says *why* is better.
- **Sentence case everywhere.** Buttons are verbs ("Open run breakdown"). No exclamation marks.
- **Status is never colour alone:** icon plus word plus colour (`On track`, `Watch`, `Act now`).
- **Explain jargon in place.** Metric labels carry glossary tooltips (`applyTooltips` in
  `app.js`), so the page itself doesn't have to define VDOT.

## Accessibility checklist (what the quality pass verified)

- axe-core (4.10) reports 0 violations on all seven views and the login gate, in both themes.
- No horizontal overflow at 375px, 768px, or desktop width.
- A visible `:focus-visible` ring (`--focus-ring`) on every interactive element. Tab
  order follows: skip link → brand → nav → top-bar actions → content.
- Dialogs trap focus, close on Escape, and return focus to whatever opened them. The
  chat dock returns focus to its button and keeps `aria-expanded` in sync.
- Layout shift is held near zero by sized skeletons and a page-level `is-loading` reveal
  (Coach ≈ 0.02, Fitness ≈ 0.003, Plan ≈ 0.03 CLS).

## Adding something new

1. Need a value? Use the closest existing token. If none fits, add one to `tokens.css`
   for **all three** theme blocks (dark, `[data-theme="light"]`, and the OS-light media query).
2. Need a component? Build it in `ui/`, export it from `ui/index.js`, style it in
   `ui/ui.css` with tokens only, and add a specimen to `design/components.html`.
3. Check it in both themes at 375px, run axe, and Tab through it.
