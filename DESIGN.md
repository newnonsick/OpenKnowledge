---
# OpenKnowledge console — design tokens (machine-readable)
# Source of truth: web/app/globals.css (:root + html[data-theme="dark"]),
# web/app/layout.tsx (font wiring), web/components/*.tsx (class usage).
# No Tailwind / no component library. Only color/shadow/z/font/ease-out are
# verbatim --* tokens; radius.modal_surface/pill_pattern, layout widths, motion patterns, and
# breakpoints are observed literals/rules cited with file:line in prose.
tokens:
  color:
    light:
      paper: "#f4f5f3"
      surface: "#ffffff"
      surface-soft: "#eef0ed"
      surface-subtle: "#f7f8f6"
      surface-hover: "#e7eae6"
      ink: "#181b1a"
      ink-soft: "#4f554f"
      ink-faint: "#666b66"
      line: "#e2e5e0"
      line-strong: "#cdd2cb"
      accent: "#177a5b"
      accent-strong: "#0f5c44"
      accent-soft: "#e0efe7"
      accent-softer: "#eef5f0"
      amber: "#8a5f0b"
      amber-soft: "#f7eeda"
      coral: "#b23c28"
      coral-strong: "#93301f"
      coral-soft: "#f9e8e2"
      info: "#2b6a85"
      info-soft: "#e2eef4"
      focus: "#177a5b"
      ring: "rgba(23, 122, 91, 0.22)"
      brand-gradient: "linear-gradient(150deg, #0f5c44, #177a5b 55%, #2a8f6d)"
      color-scheme: "light"
    dark:
      paper: "#111415"
      surface: "#1a1e20"
      surface-soft: "#22282b"
      surface-subtle: "#171b1d"
      surface-hover: "#2a3134"
      ink: "#e8ebe9"
      ink-soft: "#b3bab4"
      ink-faint: "#8d948d"
      line: "#2a3033"
      line-strong: "#3a4245"
      accent: "#5cb894"
      accent-strong: "#7ccca9"
      accent-soft: "#22352c"
      accent-softer: "#1c2b25"
      amber: "#d8ab5e"
      amber-soft: "#372d19"
      coral: "#e08a77"
      coral-strong: "#eba394"
      coral-soft: "#3e241d"
      info: "#7db6cd"
      info-soft: "#1e313a"
      focus: "#5cb894"
      ring: "rgba(92, 184, 148, 0.32)"
      color-scheme: "dark"
  font:
    ui: 'var(--next-font-ui, "Segoe UI"), "Segoe UI", system-ui, sans-serif'
    display: 'var(--next-font-ui, "Segoe UI"), "Segoe UI", system-ui, -apple-system, sans-serif'
    mono: '"Cascadia Code", ui-monospace, "SFMono-Regular", Consolas, monospace'
    source_file: "web/app/fonts/inter-latin-var.woff2 (weight 100 900, display swap, variable --next-font-ui)"
  radius:
    sm: "8px"
    md: "12px"
    lg: "14px"
    # Observed literals, NOT --* tokens (CSS defines only --radius-sm/md/lg).
    modal_surface: "20px (.modal-surface, globals.css:740; 18px at <=540px, :1677)"
    pill_pattern: "999px (repeated literal, e.g. globals.css:120,443,519,678)"
  shadow:
    light:
      soft: "0 1px 2px rgba(24, 27, 26, 0.05), 0 10px 28px rgba(24, 27, 26, 0.06)"
      pop: "0 6px 16px rgba(24, 27, 26, 0.1), 0 28px 70px rgba(24, 27, 26, 0.16)"
      lift: "0 1px 2px rgba(24, 27, 26, 0.06), 0 4px 14px rgba(24, 27, 26, 0.08)"
    dark:
      soft: "0 1px 2px rgba(0, 0, 0, 0.35), 0 10px 28px rgba(0, 0, 0, 0.3)"
      pop: "0 6px 16px rgba(0, 0, 0, 0.4), 0 28px 70px rgba(0, 0, 0, 0.55)"
      lift: "0 1px 2px rgba(0, 0, 0, 0.35), 0 4px 14px rgba(0, 0, 0, 0.35)"
  z:
    topbar: 10
    overlay: 18
    mobilebar: 16
    sidebar: 20
    popover: 40
    modal: 80
  motion:
    ease-out: "cubic-bezier(0.22, 1, 0.36, 1)"
    # Observed patterns, NOT --* tokens (only --ease-out exists as a var).
    hover_pattern: "0.15s ease-out (literal, e.g. globals.css:196; rows use 0.12s, lists 0.18s, drawer 0.22s, press 0.06s)"
    press_pattern: "translateY(1px) (literal, e.g. globals.css:320,597; .console-search button uses scale(0.97), :1011)"
  breakpoints: ["max-width: 1100px", "max-width: 980px", "max-width: 900px", "max-width: 700px", "max-width: 540px"]
  # CSS also has @media (hover: hover) (scrollbars, :118) and
  # @media (prefers-reduced-motion: reduce) (:1697-1708); those are not layout breakpoints.
  layout: # Base/desktop values only; responsive overrides in prose §4.
    sidebar-width: "260px"
    sidebar-collapsed-width: "76px"
    content-max: "min(1140px, calc(100% - 64px))"
    dashboard-content-max: "min(1080px, calc(100% - 72px))"
    topbar-height: "60px"
---

# DESIGN.md — OpenKnowledge console

## 1. Overview

The OpenKnowledge web console (`web/`, Next.js + React 19) is a private family-knowledge
management UI: dashboard, explore/search, spaces, knowledge, sources, ingestion, people,
activity, AI actions, and settings. There is no Tailwind, no CSS-in-JS, and no third-party
component library. Every visual decision lives in one hand-authored stylesheet,
`web/app/globals.css` (~1700 lines), driven by CSS custom properties defined in `:root`
(light) and overridden under `html[data-theme="dark"]`.

The visual language is a quiet, dense, paper-like admin console:

- Flat paper background (`--paper`) with white `--surface` panels separated by hairline
  `--line` borders rather than heavy shadows. Depth is reserved for popovers, modals, and
  hover lift.
- A single deep-green accent family (`--accent` / `--accent-strong` / `--accent-soft` /
  `--accent-softer`) is used for primary actions, active navigation, and focus rings;
  with `--accent-soft` fills it also signals success/active status (see §2 mapping). The
  brand mark itself uses `--brand-gradient`, not flat `--accent`. Coral is
  destructive/error, amber is warning/pending, info-blue is secondary status. Each has a
  `-soft` tinted background paired with a same-hue foreground (contrast not audited —
  see Appendix).
- Small, compact type (base 14px/1.6) with tight headings in negative letter-spacing and
  tabular numerals for counts and scores.
- Generous 6–20px radii plus full pills (tokens: 8/12/14px; plus observed literals
  6/7/9–13/15/18/20px — see §6), 7px status dots, and 34–48px control heights.
- Lucide icons (`lucide-react@1.33.0`) at 11–23px typical, plus 25px permission-panel
  icons and a 148px QR code (non-lucide); decorative icons commonly carry
  `aria-hidden="true"`, though some spinners and status-embedded icons omit it. The only
  spinner pattern is `LoaderCircle` with the `.spin` animation.
- Responsive behavior is breakpoint-driven (1100 / 980 / 900 / 700 / 540px): the fixed
  260px sidebar collapses to a 76px icon rail at ≤980px and becomes an off-canvas drawer
  with overlay at ≤700px; multi-column grids collapse to one column; filter bars stack.

To match the existing console, reuse the existing class names in `globals.css`
(`console-panel`, `primary-button`, `data-row`, `list-filter-bar`, `status-pill`, …)
instead of writing new CSS (convention, not code-enforced). New styles, when
unavoidable, should reference the `--*` custom properties so both themes keep working.

## 2. Colors

All color tokens are defined in `web/app/globals.css` lines 1–73. Light values come from
`:root`; dark values from `html[data-theme="dark"]`. The theme is switched by setting
`data-theme="light" | "dark"` on `<html>` (see `web/app/layout.tsx` bootstrap script and
`web/components/theme-toggle.tsx`, `localStorage` key `openknowledge-theme`, defaulting
to `prefers-color-scheme`).

### Foundational tokens

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| `--paper` | `#f4f5f3` | `#111415` | App/page background (`body`, `.app-frame`) |
| `--surface` | `#ffffff` | `#1a1e20` | Cards, panels, inputs, modals |
| `--surface-soft` | `#eef0ed` | `#22282b` | Hover fills, tag/badge bg, skeleton base |
| `--surface-subtle` | `#f7f8f6` | `#171b1d` | Filter bars, inline-create forms, sub-panels |
| `--surface-hover` | `#e7eae6` | `#2a3134` | Nav hover, skeleton shimmer end-stop |
| `--ink` | `#181b1a` | `#e8ebe9` | Primary text |
| `--ink-soft` | `#4f554f` | `#b3bab4` | Secondary text, muted buttons |
| `--ink-faint` | `#666b66` | `#8d948d` | Tertiary text, placeholders, captions |
| `--line` | `#e2e5e0` | `#2a3033` | Hairline borders, dividers |
| `--line-strong` | `#cdd2cb` | `#3a4245` | Input borders, emphasized borders |

### Semantic tokens

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| `--accent` | `#177a5b` | `#5cb894` | Primary actions, active states, links-on-hover, dots |
| `--accent-strong` | `#0f5c44` | `#7ccca9` | Primary hover, on-soft foreground (light), eyebrow text |
| `--accent-soft` | `#e0efe7` | `#22352c` | Tinted fills: active nav, pills, icon tiles |
| `--accent-softer` | `#eef5f0` | `#1c2b25` | Notice/info banners, selected cards, secret boxes |
| `--amber` | `#8a5f0b` | `#d8ab5e` | Warning foreground (revoked/expired, confirm mode) |
| `--amber-soft` | `#f7eeda` | `#372d19` | Warning fills |
| `--coral` | `#b23c28` | `#e08a77` | Destructive/error foreground |
| `--coral-strong` | `#93301f` | `#eba394` | Destructive hover, error text on tint |
| `--coral-soft` | `#f9e8e2` | `#3e241d` | Error fills |
| `--info` | `#2b6a85` | `#7db6cd` | Info/queued/running foreground |
| `--info-soft` | `#e2eef4` | `#1e313a` | Info fills |
| `--focus` | `#177a5b` | `#5cb894` | `:focus-visible` outline color |
| `--ring` | `rgba(23, 122, 91, 0.22)` | `rgba(92, 184, 148, 0.32)` | Focus ring glow (`0 0 0 3px var(--ring)`) |
| `--brand-gradient` | `linear-gradient(150deg, #0f5c44, #177a5b 55%, #2a8f6d)` | (inherited) | Brand mark, session loader |

Button foreground notes (from CSS, not tokens): `.primary-button`, `.knowledge-search
button`, and `.console-search button` use white `#fff` text in light mode and `#10231c`
in dark mode (globals.css:321, 599, 1009); `.danger-button` uses `#fff` in light and
`#2a120c` in dark (:611); `.auth-submit` likewise flips to `#10231c` in dark (:1399).
The auth story panel is a fixed near-black surface (`#0f1412` with green radial glows)
in both themes, with its own hard-coded text colors (`#f2f4f1` headings, `#a8b3ac` body,
`#7ccca9` accents, `#c6cec9` promise chips, `#dde3df`/`#8b968f` trust card); the QR tile
(`.enrollment-qr`) stays `#ffffff` even in dark mode. Do not re-theme these with
`--ink`; the sanctioned hard-codes are exactly these button-text and auth/first-use values.

Status-to-color mapping. Pills (`.status-pill status-*`, globals.css:972–977) and dots
(`.state-*` / `.audit-outcome outcome-*`, :1215–1218) are two separate taxonomies:

- Pills — green (`--accent*`): active, completed, ready, succeeded. Blue (`--info*`):
  pending, preparing, queued, running, retry_wait. Coral (`--coral*`): failed, disabled,
  cancelled, quarantined. Amber (`--amber*`): revoked, expired, superseded (there is no
  `.status-denied`; an audit `denied` outcome renders the base pill unstyled).
- Dots — default info-blue; accent: completed, succeeded, success; coral: failed,
  cancelled, failure; amber: denied. Queued/running/active/ready dots fall back to the
  info base (no dedicated rule).

Role/mode mapping (`.role-pill`, `.mode-pill`, globals.css:676–683, 1274–1279): only
`.role-owner` (accent) and `.role-editor` (info) exist in CSS, and TSX renders a role
pill only for owners (`management-console.tsx:449,478`) — reader/editor memberships use
a `<select>`, never a pill. `.mode-read` and `.mode-write` are accent; `.mode-confirm`
is amber. Only `mode-confirm` (tool requires confirmation, labeled "Confirm") and
`mode-read` (direct tool, labeled "Direct") are generated (`management-console.tsx:2336`);
`.mode-write` is defined but unused. Default mode pills fall back to `--surface-soft`.

### Interaction-state colors

- Hover fills: `--surface-soft` / `--surface-hover` for rows and menu items;
  `--accent-softer` for accent-tinted action hovers (`.space-manage-button`,
  `.row-action-button`); `--coral-soft` for destructive hovers.
- Focus: `outline: 2px solid var(--focus); outline-offset: 2px` globally
  (`:focus-visible`), plus `border-color: var(--accent)` with `box-shadow: 0 0 0 3px
  var(--ring)` on inputs, search bars, and selected cards.
- Disabled: base buttons/inputs use `opacity: 0.55` with `cursor: not-allowed`
  (globals.css:601, 604, 613); known exceptions — `.pagination-button:disabled`
  (`cursor: default; opacity: 0.35`, :810), `.auth-submit:disabled` (`wait`, 0.65,
  :1402), `.console-search button:disabled` (`wait`, 0.75, :1012),
  `.member-row-trigger:disabled` (`wait`, 0.58, :918). `wait` cursor marks an async
  action in flight; `not-allowed` marks statically unavailable controls.
- `::selection` uses `var(--accent-soft)`.

## 3. Typography

- **Families.** Both `--font-ui` and `--font-display` resolve to the self-hosted
  variable Inter (`web/app/fonts/inter-latin-var.woff2`, weight `100 900`, loaded via
  `next/font/local` as `--next-font-ui` in `web/app/layout.tsx`), with system fallbacks
  (`"Segoe UI", system-ui, sans-serif`). There is no separate display face in use.
  `web/app/fonts/fraunces-latin-var.woff2` exists on disk but is **not wired up** — no
  `@font-face`, no CSS reference. Do not use it. Monospace is `--font-mono`:
  `"Cascadia Code", ui-monospace, "SFMono-Regular", Consolas, monospace`.
- **Base.** `body` is 14px, line-height 1.6, `-webkit-font-smoothing: antialiased`,
  `text-rendering: optimizeLegibility`, `font-feature-settings: "cv05" 1, "ss01" 1`.
  Form controls inherit (`font: inherit`).
- **Headings.** `h1–h3` are weight 600 with `text-wrap: balance`. Display headings use
  `var(--font-display)` with negative tracking: hero `clamp(28px,3vw,38px)` weight 560,
  `-0.025em`, 1.18; console page `clamp(26px,2.6vw,32px)` weight 540, `-0.015em`, 1.1;
  action panel / results toolbar 20px weight 540; panel headings 16px; auth story
  `clamp(38px,4.4vw,60px)` weight 500, `-0.02em`, 1.05; auth form 25px weight 540;
  first-use card `h1` 23px weight 540; first-use context `h2`
  `clamp(32px,4vw,50px)` weight 510, 1.08, `-0.02em`. Hero, auth-story, and first-use
  headings pair a full-weight lead with a lighter muted `span` (hero 440 in
  `--ink-faint`; auth story 420 in `#7d8a83`; first-use 430 in `--ink-faint`).
- **Eyebrows/kickers.** 10–11px, weight 650–700, `0.08–0.11em` letter-spacing,
  uppercase. Accent-colored: `.eyebrow`, `.console-eyebrow`, `.first-use-kicker`,
  `.security-kicker` (accent in dark mode too). `--ink-faint`: `.navigation-label`,
  `.section-kicker`, `.panel-heading span`, `.membership-list-label`,
  `.member-editor-header span`, `.space-access-heading span`, and similar meta kickers.
  (`.modal-heading small` is the 10px endpoint.)
- **Body/small.** Descriptions 12.5–15px in `--ink-soft`; captions/meta 11–12px in
  `--ink-faint`. Row titles 13–13.5px (weight unspecified — inherits UA bold — except
  audit h3 at 650); card titles similar with ellipsis.
- **Mono usage.** IDs, hashes, secrets, recovery codes, scores, counts, progress
  percentages, job/audit/tool identifiers (`.result-rank`, `.score-pill`,
  `.row-stats strong`, `.short-identifier code`, `.secret-value code`,
  `.secret-banner code`, `.recovery-codes code`, `.runtime-value-group dd`,
  `.job-topline code`, `.job-context code`, `.job-error`, `.audit-meta code`,
  `.tool-card > code`, `.enrollment-secret code`, `.progress-row strong`).
  Pagination/summary numerals use `font-variant-numeric: tabular-nums` without the mono
  face. The TOTP field (`.code-field`) adds `0.18em` letter-spacing.
- Paragraphs use `text-wrap: pretty`; long identifiers use `overflow-wrap: anywhere`
  with ellipsis (`overflow: hidden; text-overflow: ellipsis; white-space: nowrap`).

## 4. Layout

- **App frame.** Fixed left sidebar (`.sidebar`, 260px wide, full height) + `.main-canvas`
  with `margin-left: 260px`. The top bar (`.topbar`, 60px, sticky) uses a blurred paper
  background (`color-mix(in srgb, var(--paper) 88%, transparent)`, `blur(14px)`).
- **Content widths.** Console pages: `.console-content` = `min(1140px, calc(100% - 64px))`
  (`width:`, globals.css:476), padding `40px 0 80px`. Dashboard: `.content-wrap` =
  `min(1080px, calc(100% - 72px))` (:286), padding `44px 0 72px`. Responsive overrides:
  at ≤980px console 900px / dashboard 860px (`100% - 48px`, :1540–1541); at ≤700px both
  620px (`100% - 32px`, :1596–1597) with console padding `28px 0 60px`. Page headers
  (`.console-page-header`) are flex rows with a bottom hairline, wrapping to a column
  on mobile.
- **Grids.** Two-column console grids (`.console-grid-spaces/-knowledge/-sources/-people`:
  `1.6fr / 0.72fr` with `minmax(min(310px,100%),…)` on the rail) collapse to one column
  at ≤1100px, with the sticky `.action-panel` (`top: 80px`) moving above content
  (`order: -1`, static). Dashboard grid is `1.55fr / 0.72fr`, collapsing to 1fr at
  ≤980px. Card grids: `.space-card-grid` 2-col; `.tool-grid`, `.explore-prompt-grid`,
  `.runtime-summary` 3-col (2-col at ≤1100px, 1-col at ≤700px). (`.metric-strip` shares
  the 3-col CSS but has no TSX usage — dead rule alongside live `.tool-grid` /
  `.runtime-summary`.)
- **Spacing scale.** The stylesheet uses ad-hoc pixel values rather than a spacing token
  scale, but recurring steps are 4 / 6 / 8 / 10 / 12 / 14 / 16 / 18 / 22 / 24px for gaps
  and padding, 16px section gaps, 10–12px card-grid gaps, and 12–14px panel-internal
  rhythm. Panels use 22–24px padding (19px on mobile); rows use 8–14px vertical padding
  (membership 8px, data 10px, recent 12px, audit 14px).
- **Rows and dividers.** Lists are hairline-divided stacks: `.data-row` (min-height
  64px, 12px radius, `border-top: 1px solid var(--line)`, first row transparent, hover
  `--surface-soft`, `0.12s` transition); `.recent-row`, `.membership-row` (60px, no
  radius), `.audit-row` (78px, 10px radius) follow the same pattern.
- **Sticky/blur surfaces.** `.topbar` and `.console-mobile-bar` are sticky with
  paper-translucency + 14px blur; `.modal-backdrop` adds a 10px blur over a
  `rgba(14,18,17,.52)` scrim (`.65` black in dark mode).
- **Breakpoints (all `max-width`).** 1100px: side rails stack, 3-col grids → 2-col
  (plus audit-meta → 2-col, audit filter bar → 2-col with full-width buttons,
  runtime-settings → 2-col). 980px: sidebar → 76px icon rail (labels/copies hidden,
  items centered); dashboard grid → 1fr; auth split → single column with story on top
  (story `order: 1`, `0 0 20px 20px` radius, promises/trust hidden; form `order: 2`);
  console content max 900px, dashboard 860px. 900px: filter bars → 2-col, first-use
  2-col → 1-col, `kbd` hints hidden, topbar padding shrinks. 700px: sidebar →
  off-canvas drawer (`width: min(300px, 86vw)`, `translateX(-105%)`, `.menu-open`
  reveals + `.navigation-overlay`); topbar replaced by `.console-mobile-bar`; all
  multi-col grids → 1-col; rows/cards reflow (meta/tags full-width, actions
  full-width; `.space-manage-button`/`.row-action-button`/filter buttons 42px, icon
  and square buttons 40px, pagination 38px). 540px: settings tabs → 2-col grid;
  modals bottom-sheet (`align-items: end`, 18px radius, actions stacked 1-col at
  46px); recovery codes → 1-col.

## 5. Elevation & Depth

Depth is restrained: panels separate with borders; shadows appear on interactive and
layered surfaces:

- `--shadow-soft`: large search bars (`.knowledge-search`, `.console-search`),
  first-use assurance card (`:1440`).
- `--shadow-lift`: hover lift on cards (`.space-card`, `.result-card`, `.tool-card`,
  `.job-card`, quick actions) and primary-button hover (`:600`).
- `--shadow-pop`: popovers (`.account-menu-popover`), modal surfaces, first-use card
  (`:1449`), step-up dialog (`:1356`).
- Console panels (`.console-panel`) use a near-flat `0 1px 2px rgba(24, 27, 26, 0.04)`
  (:505). The active settings tab has its own `0 1px 4px rgba(24, 32, 28, 0.12)` (:1117);
  primary buttons add an inner top highlight (`inset 0 1px 0 rgba(255,255,255,0.14)`).
  Dark-mode shadows switch to black-based equivalents (see `tokens.shadow.dark`).
- **Overlays.** Modal scrim `rgba(14,18,17,.52)` + `blur(10px)`; mobile nav scrim
  `rgba(14,18,17,.45)` + `blur(2px)`; topbar/mobile-bar paper translucency + `blur(14px)`.
- **z-index** (`--z-*`): topbar 10, mobilebar 16, overlay 18, sidebar 20, popover 40,
  modal 80. The account popover sits above the sidebar; modals above everything.
- Focus rings (`0 0 0 3px var(--ring)`) mark focused inputs, search bars, and
  selected cards (`.space-card.is-selected`).

No multi-level surface scale is defined; observed background names are `--paper`,
`--surface`, `--surface-subtle`, `--surface-soft`, `--surface-hover` (no ranking implied).

### Motion

- Keyframes: `spin` (`0.8s linear infinite`, :134), `page-enter` (`0.18s ease-out` on
  `.console-content, .content-wrap`, :137), `reveal` (`translateY(-4px)` entrance, used
  by `.ownership-recovery-form` and `.step-up-dialog`), `skeleton-shimmer` (`1.3s
  ease-in-out infinite`, :445, 454), `account-menu-in` (`0.16s var(--ease-out)`, :212),
  `modal-backdrop-in` (`0.18s`, :734), `modal-surface-in` (`0.22s var(--ease-out)` from
  `translateY(10px) scale(0.98)`, :741–762).
- Transitions are mostly `0.15s ease-out`; rows use `0.12s`, list dimming `0.18s`, the
  mobile drawer `0.22s var(--ease-out)`, press feedback `0.06s`. Only one smooth scroll
  exists (`scrollIntoView({behavior:"smooth"})` in `management-console.tsx:944`); there
  is no global `scroll-behavior: smooth`.
- Reduced motion: `@media (prefers-reduced-motion: reduce)` (:1697–1708) forces
  `transition/animation-duration: 0.01ms`, single iteration, `scroll-behavior: auto`,
  and kills skeleton shimmer. (The `.page-enter { animation: none }` line inside it
  matches no element — the live animation sits on `.console-content, .content-wrap` —
  so the universal `0.01ms` rule carries the effect.)

## 6. Shapes

- **Radii.** `--radius-sm: 8px`, `--radius-md: 12px`, `--radius-lg: 14px`; modal
  surfaces are a 20px literal (18px at ≤540px). Full pills use a `999px` literal.
  Recurring component radii: buttons/inputs 10px; auth/text fields 11px; search bars
  15px; icon tiles 9–13px (`.brand-mark` 9px, `.document-glyph` 11px,
  `.action-panel-icon` 12px); glyph/leading tiles 10–11px; `kbd` 6px; result meta chips 7px;
  progress bars fully round. `:focus-visible` rounds to 4px.
- **Borders.** 1px `var(--line)` for panels/cards/dividers, 1px `var(--line-strong)`
  for inputs and buttons; dashed 1px–1.5px `var(--line-strong)` for empty states and the
  file dropzone (`.file-drop`, solid accent when dragging).
- **Geometry.** Status dots are 7px circles with a 3px soft ring (`box-shadow: 0 0 0 3px
  <color>-soft`). Avatars: 32px rounded squares (`.family-avatar`, 9px radius) for
  families, 32px circles (`.profile-avatar`, ink fill) for users. `.icon-button` is a
  36px circle (40px at ≤700px). `.mobile-menu-button` is `display: none` on desktop
  and a 40px rounded square at ≤700px. The auth story panel decorates with large
  concentric ring pseudo-elements; observed geometry is otherwise rectangular with
  soft corners (rings only as auth-story decoration).

## 7. Components

All components are hand-built in `web/components/` + `web/components/auth/` and styled
exclusively by `globals.css`. Convention (not code-enforced): reuse the listed classes
rather than restyling with inline CSS.

- **Buttons.** `.primary-button` (accent fill, white/`#10231c` text, inner top
  highlight, hover `--accent-strong` + lift, active `translateY(1px)`, disabled 0.55,
  min-height 40px, padding `0 16px`). `.secondary-button` (surface, `--line-strong`
  border, `--ink-soft` text, hover border `--ink-faint` + `--surface-subtle` fill;
  `width: 100%` by default, constrained to auto per context — modals, filters,
  unavailable panels). `.danger-button` (coral fill; used by
  `ConfirmationDialog` `tone="danger"`). `.archive-button` (coral outline; `.compact`
  variant 34px). `.row-action-button` (small outline; `.danger-action` coral variant).
  `.auth-submit` (full-width 48px accent submit on auth/first-use). `.secondary-action`
  (full-width outline utility). `.space-manage-button`, `.metric-refresh-button`
  (accent-hover outline utilities). `.icon-button` (36px circle, 40px at ≤700px).
  `.mobile-menu-button` (desktop-hidden; 40px rounded square at ≤700px). Heights used:
  34 / 36 / 38 / 40 / 42 / 46 / 48px.
- **Inputs.** `.console-form` inputs/selects/textarea: 40px height, 10px radius,
  `--line-strong` border, focus accent + ring; disabled `--surface-soft` fill.
  Auth/first-use fields (`.text-field`, `.password-field`): exactly 46px height,
  11px radius, with inset eye toggle. Selects get a custom chevron via inline-SVG
  background (theme-aware stroke: `#666b66` light / `#8d948d` dark). Labels are
  12.5px/600 above fields. Scope checkboxes (`.api-key-scope-option`) are bordered
  cards that tint accent when `:has(:checked)`; native checkbox uses
  `accent-color: var(--accent)`. File uploads use `.file-drop` (1.5px dashed,
  solid accent on hover/drag + ring when dragging).
- **Search & filters.** `.knowledge-search` / `.console-search` (58–62px pill-ish bars,
  15px radius, soft shadow, icon + borderless input + accent submit). `.list-filter-bar`
  (subtle panel, white field labels, Apply = `.secondary-button`, Clear =
  `.filter-clear-button` coral-hover). Variants adjust grid columns
  (`.space-filter-bar`, `.member-filter-bar`, `.audit-filter-bar`, …). Active scoping
  uses `.filter-strip` (accent-softer banner with dismiss).
- **Navigation.** `.sidebar` (brand lockup + gradient `.brand-mark`, family switcher
  card, grouped `.navigation-item`s 38px/10px radius with hover fill and accent-soft
  active + 3px accent rail, footer account menu with `.account-menu-popover`,
  `.profile-card`, `.theme-toggle`). `.topbar` (scope dot + command button with `kbd`
  hint + icon buttons). Mobile: `.console-mobile-bar` + hamburger + off-canvas
  drawer + overlay.
- **Cards & rows.** `.console-panel` (surface, line border, 14px radius, 24px padding).
  Card icon tiles rotate four tints via `accent-${index % 4}` (`.space-card-mark`,
  `.tool-icon`; CSS `.accent-0…accent-3`: accent / info / amber / coral).
  `.space-card` (2-col grid + footer, hover lift, accent ring when selected).
  `.data-row` / `.recent-row` / `.membership-row` / `.audit-row` (hairline-divided,
  hover fill, leading glyph tile + copy + meta/actions). Hook-only classes with no CSS
  of their own inherit their companion's styling: `capture-panel`, `upload-panel`,
  `member-create-panel` (always paired with `console-panel action-panel`);
  `knowledge-row`, `source-row`, `member-row` (paired with `data-row`);
  `runtime-section` (paired with `console-panel settings-section`).
  `.result-card` (rank tile + copy + score). `.job-card` (topline + context grid +
  progress). `.tool-card` (icon + mode pill + mono name). `.explore-prompt-grid article`
  (static info cards).
- **Badges.** `.count-pill` (accent-soft totals), `.role-pill`/`.status-pill`/
  `.mode-pill` (11px/600 pills; status colors per §2), `.tag-list span` (max 3 shown),
  `.health-chip` (accent base; `.degraded` amber — the healthy `semantic_status` value
  has no dedicated rule and renders the base style), `.score-pill` (mono score),
  `.revision-state`, `.short-identifier` (mono + `.secret-copy-button` with
  `data-copied` state).
- **Feedback.** `.inline-error` (coral tint, `role="alert"`) / `.inline-success`
  (accent tint, `role="status"`); `.auth-error` / `.auth-notice`; `.attention-strip`
  (`.is-warning` amber); `.console-unavailable` (error panel with retry);
  `.console-empty` (dashed, icon + title + hint + optional CTA); `.secret-banner` /
  `.secret-value` / `.enrollment-secret` (accent-tinted secret reveals with copy);
  `.recovery-codes` (2-col mono grid, 1-col at ≤540px). Skeletons:
  `.list-skeleton` (+ `.compact`) / `.skeleton-row` (glyph + lines + pill) /
  `.dashboard-skeleton-row` / `.dashboard-pulse-skeleton` (shimmer 1.3s); spinners are
  `LoaderCircle.spin`; paginated lists dim via `.is-page-loading` (opacity .55).
- **Dialogs.** `ModalDialog` (`confirmation-dialog.tsx`): `.modal-backdrop` >
  `.modal-surface` (460px default; 600px `member-editor`, 680px `knowledge-editor`,
  720px `space-access` variants; `role="dialog"` / `"alertdialog"`, focus trap +
  return focus, Escape/backdrop close). `ConfirmationDialog` maps tone to action
  style (`danger` → `.danger-button`, `primary` → `.primary-button`, `neutral` →
  `.secondary-button.modal-confirm-button`) and adds `tone-danger|primary|neutral`
  (`.modal-icon` is amber by default, coral for danger, accent for primary,
  surface/ink-soft for neutral), `.modal-heading`, `.modal-copy`,
  `.inline-error.modal-error`, `.modal-actions` (right-aligned; grid-stacked full-width
  46px buttons at ≤540px). Also `.step-up-dialog` (2-col `.step-up-factor-tabs`,
  `[aria-pressed]` accent state).
- **Tabs.** `.settings-tabs` (segmented control: subtle track, white active tab with
  shadow, roving `tabindex`, `role="tablist"`). `.factor-tabs` / `.step-up-factor-tabs`
  (`role="group"`, 2-col toggle buttons, accent border/fill when pressed).
- **Pagination.** `.pagination-controls` (summary + Prev/Next `.pagination-step` buttons
  34px/9px radius + jump-to input 52px with Go; no numbered buttons — `.is-current` /
  `.is-loading` button rules exist in CSS but are never applied; loading shows a
  `LoaderCircle.spin` in `.pagination-progress`).
- **Icons.** `lucide-react@1.33.0` only. Common: Search, Plus, X, Menu, Boxes,
  FolderKanban, BookOpen, UsersRound, Settings2, ShieldCheck, CircleAlert,
  LoaderCircle, Copy/Check, Eye/EyeOff, KeyRound, Archive, ArrowUpRight, Tag, Save.
  Decorative icons commonly carry `aria-hidden="true"` (11–23px typical, plus 25px
  permission icons); `strokeWidth` is default except large nav/hero glyphs
  (sidebar nav, dashboard hero search: 1.8). Some spinners and status-embedded icons
  omit `aria-hidden` — match the surrounding code when copying a pattern.

### Interaction states

Hover → fill/border shifts (mostly `0.15s ease-out`; rows `0.12s`); active/press →
`translateY(1px)` with `0.06s` timing (`.console-search`'s 50px square button uses
`scale(0.97)` instead); focus → accent border + ring + global `:focus-visible`
outline; selected → accent border + ring; disabled → per-context opacity/cursor (see
§2; pending buttons use `wait` cursor with progressive labels such as `Signing in…`,
`Verifying…`, `Refreshing…`, `Signing out session…`); loading → spinner + `aria-busy`
+ dimmed lists; validation → coral `.inline-error[role=alert]` blocks (`aria-invalid`
appears only on the runtime-limit input); destructive → coral buttons/banners
(dialogs use `role="dialog"`/`"alertdialog"`, banners use `role="alert"`).

### Dark mode, responsive, accessibility

- **Dark mode** flips the full token table (§2 YAML + tables) via
  `html[data-theme]`, toggled by `.theme-toggle` (`localStorage openknowledge-theme`,
  `prefers-color-scheme` default). Only the button-text flips and the auth/first-use
  hard-codes stay fixed.
- **Responsive** behavior is defined by the five `max-width` breakpoints (§4).
- **Accessibility** is by convention: `:focus-visible` outline, accent rings, roles
  (`alert`/`status`/`dialog`/`tablist`/`menu`/`search`), live regions, roving tabindex,
  focus trap + return focus (`web/lib/focus-management.ts`), reduced-motion handling
  (§5 Motion). No favicon/apple-touch/manifest is wired; `docs/images/` holds only
  `explore.png` and `login.png`. None of this is a certified guarantee — see Appendix.

## 8. Do's and Don'ts

- **Do** reuse existing classes and `--*` tokens; new UI should read as more
  of the same console (paper bg, white 12–14px panels, hairline borders, green accent).
- **Do** keep both themes working: prefer `--*` tokens — the only sanctioned hard-codes
  are the §2 button-text flips and auth/first-use values. Check the dark overrides
  (accent/coral/amber swap foreground roles on tints).
- **Do** follow the established hierarchies: eyebrow → display heading → muted
  description → panel grid; rows with leading tile + ellipsis copy + right meta.
- **Do** preserve focus and live-region patterns: `:focus-visible` outline, accent
  ring on inputs, `role="alert"`/`status`, `aria-busy` + `.is-page-loading` for
  refetches, focus return after dialogs, `aria-pressed`/`aria-selected` on toggles.
- **Do** respect breakpoints: rail-above-content at ≤1100px, icon rail at ≤980px,
  drawer + stacked single-column at ≤700px (selected actions 42px; other controls
  38–40px).
- **Don't** introduce Tailwind, a component library, new font faces, or new palette
  hues — the project uses none. In particular, don't wire up the unused
  `fraunces-latin-var.woff2`; headings are Inter.
- **Don't** invent new radii or shadows — prefer the documented tokens and match an
  existing class (note code also contains the §5/§6 observed literals: 6/7/9–11/15/18px
  radii, near-flat panel shadow, settings-tab shadow, button inset highlight).
- **Don't** use heavy drop shadows on resting panels, gradient text, or colored page
  backgrounds — the only dark immersive surface is the login/first-use story side.
- **Don't** claim or add guarantees the code doesn't implement: no reduced-data,
  no high-contrast theme, no print stylesheet, no favicon/manifest set, no global dark
  illustration set.

## Appendix — provenance notes (not design guidance)

- `fraunces-latin-var.woff2` ships in `web/app/fonts/` but nothing references it; the
  display face is Inter. Dead asset, kept as-is.
- Legacy dashboard classes (`.knowledge-search`, `.attention-strip`, `.dashboard-*`)
  coexist with newer `.console-*` equivalents in `globals.css`; components mostly use
  the `.console-*` set now (e.g. `console-search`, `console-empty`). Treat `.console-*`
  as canonical for new work.
- `.metric-strip` CSS exists but has no TSX usage — dead rule.
- `.secondary-button` is full-width by default (`width: 100%`) and callers constrain it
  contextually (auto width in modals, filters, unavailable panels) — preserve the
  per-context widths.
- Audit `denied` has dot styling (`.outcome-denied`) but no `.status-denied` pill tint;
  success/denied audit pills render the base pill. `.mode-write` is defined but never
  generated.
- No spacing, type-scale, or elevation token files exist; values are inline in CSS.
  This document records the recurring values but does not invent a scale the project
  doesn't define.
- Accessibility is by convention (roles, labels, focus utils in
  `web/lib/focus-management.ts`, browser a11y spec in `web/tests/browser/`), not a
  certified guarantee; contrast and target-size descriptions above record observed code,
  not audit results.
