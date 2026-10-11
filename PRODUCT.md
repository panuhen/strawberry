# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Now:** the person who runs Strawberry on their own computer, day to day. They talk to her by voice or text, let her react to music, notifications and commits, and open the Brain UI in a browser on their main monitor (mouse and keyboard) to see and adjust what happens under the hood.
- **Later:** people who install the open-source Strawberry themselves. The Brain UI should be understandable without the author explaining it: plain labels, a reason next to every number, no insider shorthand.

## Product Purpose

Strawberry is a fully local desktop AI: a crab widget (Godot) that reacts to what happens on the machine and talks back, with a brain daemon (router, small line model, a larger tool-using model) behind it. Nothing leaves the computer.

The **Brain UI** is the under-the-hood page the daemon serves on localhost. It is where the user checks and steers the brain: what she routed and why, runs and approvals, what she learned, her persona and what she knows about the user, her mood and history, inputs (camera gestures, touch), apps she may open, tools, and the config file. Success: the user can answer "what did she do, why, and how do I change that?" in a few clicks, and trusts what they see.

Operating on screen happens on the crab itself (her menu, her approval card). The Brain UI is for looking under the hood, not for everyday control.

## Positioning

A desktop companion whose brain is inspectable and editable by its owner: every decision she makes (routing, tool tiers, approvals, what she remembers, what moves her mood) is visible and changeable in one local page, with safety-lowering changes confirmed on her own card.

## Operating Context

- Opened from the tray icon; a local web page (127.0.0.1, behind a per-session token, strict CSP, no external assets).
- Used alongside the crab widget on the desktop, often while music plays or work goes on; glanced at, then acted on.
- Tabs grouped Her / Activity / Learning / Settings / System; light and dark themes.
- Built incrementally by several agents feature by feature, so consistency between tabs is a known weak spot.

## Capabilities and Constraints

- Plain HTML, CSS and one JavaScript file (`src/strawberry_crab/ui/`: `index.html`, `style.css`, `app.js`); no framework, no build step, no external fonts or CDNs.
- DOM is built through an `el()` helper as text nodes only (no innerHTML); the CSP forbids inline scripts and remote resources.
- Every write goes through the daemon's guarded POST routes; safety-lowering settings wait for a held Yes on the crab's card, never a button in the page.
- Private data on the page (her history, the user's sentences, reminders) is shown, never logged; secrets in config are masked.
- Terminology in use: runs, approvals, tiers (read / playback / change / sends / destructive), foreign text, the gate (router), the thinker, digests, reminders, persona, profile, mood (energy, feeling).

## Brand Commitments

- Name: Strawberry; she is a crab ("she"). The 🍓 mark is the page's icon.
- Voice: plain, direct, warm; docs and labels say "the user", never personal names.

## Evidence on Hand

- Screenshots of recent tabs: `~/Pictures/strawberry-previews/` (`brain-ui-settings/`, `history/`, `mood/`, `persona-stage/`).
- No user research, testimonials or metrics exist; do not invent any.

## Product Principles

1. **Explain, don't just show.** Every number or state has its reason beside it.
2. **Safety is visible.** Foreign text, tiers and pending approvals are always labelled, never buried.
3. **Local and private by default.** Nothing on the page reaches the network; private text is shown only where the user asked to see it.
4. **One consistent instrument panel.** Tabs added at different times should read as one tool.

## Accessibility & Inclusion

Keyboard reachable controls, visible focus, sufficient contrast in both themes, reduced motion respected. No formal standard has been set.
