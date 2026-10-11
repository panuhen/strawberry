---
name: Strawberry Brain UI
description: The crab's workbench. A local page for looking inside her head and adjusting it, calm and honest.
colors:
  ground: "#140b0e"
  ground-2: "#24131a"
  glass: "rgba(255, 247, 236, 0.045)"
  glass-strong: "rgba(255, 247, 236, 0.08)"
  rim: "rgba(255, 245, 235, 0.16)"
  rim-strong: "rgba(255, 245, 235, 0.32)"
  line: "rgba(255, 245, 235, 0.09)"
  ink: "#fff7ec"
  ink-dim: "rgba(255, 247, 236, 0.74)"
  ink-faint: "rgba(255, 247, 236, 0.6)"
  berry: "#e2402f"
  berry-fill: "#bd2c22"
  berry-ink: "#ff7a68"
  berry-wash: "rgba(226, 64, 47, 0.16)"
  good: "#7fd3b3"
  good-wash: "rgba(118, 201, 172, 0.15)"
  warn: "#f2c46f"
  warn-wash: "rgba(242, 196, 111, 0.14)"
  light-ground: "#f4ece2"
  light-ground-2: "#eadccd"
  light-ink: "#201318"
  light-berry: "#b8231f"
  light-good: "#186a4f"
  light-warn: "#7a5000"
typography:
  body:
    fontFamily: "system-ui, -apple-system, Segoe UI, Ubuntu, Cantarell, Noto Sans, sans-serif"
    fontSize: "15px"
    lineHeight: 1.5
  title:
    fontSize: "1.3rem"
    fontWeight: 650
  headline:
    fontSize: "1.15rem"
    fontWeight: 650
    letterSpacing: "0.01em"
  label:
    fontSize: "0.82rem"
    fontWeight: 600
    letterSpacing: "0.08em"
  label-small:
    fontSize: "0.72rem"
    fontWeight: 600
    letterSpacing: "0.06em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Cascadia Mono, DejaVu Sans Mono, monospace"
    fontSize: "0.92em"
rounded:
  none: "0px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "20px"
  "6": "24px"
  "7": "28px"
components:
  button:
    backgroundColor: "{colors.glass-strong}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "6px 12px"
  button-primary:
    backgroundColor: "{colors.berry-fill}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "6px 12px"
  button-danger:
    backgroundColor: "{colors.berry-wash}"
    textColor: "{colors.berry-ink}"
    rounded: "{rounded.none}"
  chip:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "2px 8px"
  chip-good:
    backgroundColor: "{colors.good-wash}"
    textColor: "{colors.ink}"
  chip-warn:
    backgroundColor: "{colors.warn-wash}"
    textColor: "{colors.ink}"
  panel:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "18px 20px"
---

# Design System: Strawberry Brain UI

The source of truth is `src/strawberry_crab/ui/style.css` (tokens at the top) and the shared pieces in `app.js`. This file says how to use them. Product context: `PRODUCT.md`.

## Overview

**Creative north star: The Crab's Workbench.** The Brain UI is a quiet instrument bench under a glass lid: you look inside her head and adjust it, and she stays the star. The page is calm, honest and warm.
- **Calm:** smoked glass on a dark ground, nothing moves or shouts unless it needs you.
- **Honest:** every state shows its reason, and the safety path never claims more than it can do.
- **Warm:** her voice lives in the copy ("she asks on her card"), not in decoration.

Mode: Operate. Scanability, consistency and true states outrank expression. Character comes from the words, the 🍓 mark and exact details, not from ornament.

## Colors

Two themes from one set of tokens. Dark is the default; the light theme swaps the same names (cream ground, ink text). Every pairing of text and background meets 4.5:1 in both themes; `tests/` checks the token contrast.

### Primary
- **Strawberry berry** (`--berry` #e2402f dark / #b8231f light). The brand accent: the selected nav bar, focus rings, the one primary commit action per view.
- **`--berry-fill`** (#bd2c22): the primary button fill, darker so cream text on it passes contrast.
- **`--berry-ink`**: berry-coloured text, used for danger buttons.

### Neutral
- **Ground** (`--ground`, `--ground-2`): the page, a deep plum-black (dark) or warm cream (light), with a soft berry glow top right.
- **Glass** (`--glass`, `--glass-strong`): panels and controls. Translucent, with backdrop blur.
- **Rims and lines** (`--rim`, `--rim-strong`, `--line`): 1 px borders and table rules.
- **Ink** (`--ink`, `--ink-dim`, `--ink-faint`): text in three strengths. `--ink-faint` is for hints and empty states and still passes 4.5:1.

### State colours
- **Good** (`--good`, `--good-wash`): healthy, in use, the default.
- **Warn** (`--warn`, `--warn-wash`): attention, or a state that loosens privacy or safety.

### Named rules
- **One state, one colour, everywhere.** good = healthy or in use; warn = needs attention, or looser than the default; berry = brand and the one primary action; neutral = information. Never colour the same state differently on two tabs.
- **Berry is not a state.** Selected toggles use a neutral pressed style (`aria-pressed`), never berry.
- **The light theme's cream ground is deliberate.** Keep it.

## Typography

One system sans for everything, monospace for keys, paths and code. Body text is 15 px with a line height of 1.5. Prose is capped at about 64ch.

### Hierarchy
- **Title** (1.3rem, 650): a tab's first panel, `.panel.lead`.
- **Headline** (1.15rem, 650): every other panel's h2.
- **Label** (0.82rem, 600, uppercase, tracked, `--ink-dim`): h3 sub-sections and nav group names.
- **Small label** (0.72rem, uppercase, `--ink-dim`): h4, always smaller than h3.
- **Mono** (0.92em): config keys, file paths, ids.

### Named rules
- **Plain words first, the term of art second.** Use the `term()` helper: "Log my sentences" with `log_sentences` small beneath, or "router version" with "(head)" after it.
- **A tab's h2 is its nav label.** No other titles.

## Layout

The page is a grid: the nav is 200 px (`--nav-w`) and the content is at most 1500 px wide, with 24 px padding. Below 820 px the nav stacks above the content.

**Page template:** every tab opens with a title panel (h2 = nav label, a hint, at most one `.stats` row), then its section panels, stacked with a 20 px gap. A slot for a pending change renders only while something waits, so empty slots never add gaps.

**Three layout recipes, nothing else:**
- **`.cards`:** a responsive card grid, at least `--recipe-card` (260 px) per card. `.cards.fields` is for form fields.
- **`.stats`:** a row of stat tiles, at least `--recipe-stat` (180 px), one row per title panel.
- **`.split`:** `.split-main` with a sticky `.split-side` (at least `--recipe-side`, 300 px), for an editor with its live check and save bar (Persona, Config, Tools, Input, Apps).

**Spacing:** use only `--space-1` to `--space-7` (4 to 28 px). **Field widths:** use only `--field-xs`, `--field-s`, `--field-m`, `--field-l`. Tables are full width; empty states sit in a padded `td.empty` row.

## Elevation & Depth

The page uses layered glass, not lifted cards. Each panel is translucent glass with a 1 px rim, a 10 px backdrop blur and one soft ambient shadow (`--shadow`). Nothing stacks more than one level, except the toast and the sticky header.

## Shapes

**Square corners everywhere** (`border-radius: 0` on every element). Borders are 1 px. Shape language comes from rims and spacing, not radius.

## Components

All of these live in `app.js`'s shared pieces. Reuse them; never build a parallel one.

### Buttons
- **Default:** glass-strong fill with a strong rim.
- **`.primary`:** the berry fill, for the one commit action of a view.
- **`.danger`:** a berry wash with berry ink.
- **`.ghost`** and **`.small`** variants.

Disabled buttons are at 0.45 opacity. Two-step actions use `confirmButton` (single items) or `typedConfirm` (whole-store deletes: type the word). Armed states are announced in `#announce`. "Cancel" only ever means close.

### Chips
- `chip(text, kind)` with kind `good`, `warn` or none.
- `tierChip(tier)`: sends and destructive are warn.
- `foreignMark()`: one quiet "foreign text" marker.

Chips state facts. They are not buttons, except the header's "needs you".

### Cards / Containers
- **`.panel`:** glass, a rim, 18 × 20 px padding, a header with the h2 on the left and the hint on the right.
- **`.stat` tiles:** inside `.stats`.
- **`note("info" | "warn" | "bad")`:** an inline message with a 1 px tinted rim.

### Inputs / Fields
Glass fields with a strong rim and the field-width scale. The label sits above, with the key in mono under it. A field changed in the draft gets a "changed, not saved" marker; ✎ always reflects the file, never the draft.

### Navigation
A left nav with five groups (Her, Activity, Learning, Settings, System) under Now. The current tab has `aria-current="page"` and the berry bar. Hashes are `#tab` or `#tab/part`, and old hashes redirect.

### The save bar (signature component)
`saveBar({...})` is the one way to save a file: path · check chip · diff · Save + Undo. It is enabled only when the draft checks out and differs from the file. A safety-lowering save becomes "Save: ask on her card" and waits on `approvalBlock`. With no card on screen, Save is off and the reason is shown. The page never says yes to its own change.

### The waiting yes
`approvalLine` (header, Now) and `approvalBlock` (Runs and the file tabs) show one approval the same way everywhere: her card's line, the tier, a live countdown, what it loosens, and No. Ids are folded away.

## Do's and Don'ts

### Do
- Put a reason beside every number and state.
- Use one term for one thing: "foreign text", "tier", "router version".
- Use two time formats only: relative for recent activity, `YYYY-MM-DD HH:MM` for everything else (`when()`, `timeCell()`).
- Teach the next step in empty states.
- Fold rarely used detail behind `<details>`.
- Check new text against 4.5:1 in both themes.
- Build the DOM with `el()` as text nodes, and keep the strict CSP.

### Don't
- Don't show internal names, doc references (WIRING §, Phase N) or raw ids in the main text.
- Don't use berry or warn for something that doesn't need the user. Avoid amber noise in the header.
- Don't add a new grid recipe, spacing value, field width or save pattern; extend the existing ones.
- Don't add rounded corners, gradients on panels, or a second accent colour.
- Don't let the page claim a state it hasn't read from the daemon or the file.
- Don't add a button that approves a safety-lowering change from the page.
