// Strawberry: Brain. The page's whole script (brainui.py serves it; no build step, no library).
//
// Everything the daemon sends is data: sentences, tool names, versions and reasons are put in
// the page as text nodes (textContent, Node.append of a string), never parsed as markup. The
// session's CSRF token comes from the page's <meta name="csrf"> and goes with every API call.

"use strict";

(() => {
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  // The tabs, in the side bar's order (index.html has a button and a section for each).
  const SECTIONS = ["now", "persona", "profile", "mood", "runs", "history", "learning", "privacy", "config", "tools", "input",
                    "apps", "health"];
  // The old tabs' links still work: each opens where its content went (#router is the Runs tab's Router view).
  const MOVED = { router: ["runs", "router"], data: ["learning", "evaluation"], settings: ["privacy", null],
                  system: ["health", null] };
  const MAX_ROUTES = 100;
  const MAX_RUNS = 50;
  const TERMINAL = { "run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled" };

  const state = {
    section: "now",
    now: null,              // GET now: her state and mood, her card, what needs the user, the last runs, what is off
    runsView: "runs",       // the Runs tab's view: "runs" or "router"
    forgetWord: "",         // Forget what she learned: the word typed so far, and its box
    forgetAll: false,
    persona: null,          // GET persona: the text in use, the shipped one, its status
    personaDraft: null,     // the editor's text while it differs from the file (null: not edited)
    personaCheck: null,     // the draft checked: problems, warnings, sizes, the diff
    personaTry: null,       // Try it: the sample events and her lines
    profile: null,          // GET profile: the text, its status, what she reads, the history
    profileDraft: null,
    timeline: [],
    messages: null,
    learning: null,
    routes: [],
    runs: [],
    runEvents: true,
    approvals: { open: null, history: [] },
    logging: false,
    data: null,
    settings: null,
    system: null,
    filter: "all",
    ended: false,
    fresh: new Set(),
    freshRuns: new Set(),
    busy: false,
    input: null,            // GET input: status, settings in use, the dropdowns' choices, bodies, the file, recent
    inputForm: null,        // the tab's settings as edited (rows for the maps)
    inputEdited: false,
    inputCheck: null,       // the edited settings checked: problems, the diff against input.toml
    inputStatus: null,      // the live view's latest status (once a second)
    inputPractice: false,
    hand: null,             // the latest hand from the live view (numbers only), null when none
    handAt: 0,
    gesture: null,          // the latest gesture event, with when it came
    freshInput: new Set(),
    calib: null,            // a calibration under way or done: its step, the samples (curl numbers), the result
  };

  // --------------------------------------------------------------------------- DOM, as text

  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    setProps(node, props);
    add(node, kids);
    return node;
  }

  function setProps(node, props) {
    if (!props) return;
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.setAttribute("class", value);
      else if (key === "text") node.textContent = String(value);
      else if (key === "on") for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
      else if (key === "data") for (const [k, v] of Object.entries(value)) node.dataset[k] = String(v);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
  }

  function add(node, kids) {
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid instanceof Node ? kid : String(kid));
    }
  }

  // A node's children replaced by `kids`, skipping null and false and flattening arrays (replaceChildren
  // itself would show them as "null" and "[object …]").
  function fill(node, ...kids) {
    node.replaceChildren();
    add(node, kids);
  }

  const SVG = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, ...kids) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "text") node.textContent = String(value);
      else node.setAttribute(key, String(value));
    }
    add(node, kids);
    return node;
  }

  const $ = (sel) => document.querySelector(sel);

  // --------------------------------------------------------------------------- formatting

  const dash = "-";
  function pct(x) { return typeof x === "number" ? `${x.toFixed(1)}%` : dash; }
  function ratio(pair) { return Array.isArray(pair) && pair.length === 2 ? `${pair[0]}/${pair[1]}` : dash; }
  function pctOf(pair) { return Array.isArray(pair) && pair[1] ? (100 * pair[0]) / pair[1] : null; }
  function num(x, digits = 2) { return typeof x === "number" ? x.toFixed(digits) : dash; }
  function two(n) { return String(n).padStart(2, "0"); }

  // Every time on the page goes through here, in one of two forms: `YYYY-MM-DD HH:MM` (absolute), or, for recent
  // activity (`recent`), "40 s ago", "12 min ago", "in 25 min" while it is within a day of now, absolute beyond.
  function when(ts, recent) {
    if (!ts) return dash;
    if (recent) {
      const s = Date.now() / 1000 - ts;
      const a = Math.abs(s);
      if (a < 86400) {
        if (a < 10) return "just now";
        const span = a < 90 ? `${Math.round(a)} s` : a < 5400 ? `${Math.round(a / 60)} min` : `${Math.round(a / 3600)} h`;
        return s >= 0 ? `${span} ago` : `in ${span}`;
      }
    }
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
  }
  // A table cell with a time: a relative one carries the absolute one as its title.
  function timeCell(ts, recent, sub) {
    return el("td", { class: "nowrap", title: recent && ts ? when(ts) : null }, when(ts, recent),
      sub ? el("span", { class: "sub", text: sub }) : null);
  }
  function hhmm(ts) {      // a chart's axis labels only
    const d = new Date(ts * 1000);
    return `${two(d.getHours())}:${two(d.getMinutes())}`;
  }
  function duration(s) {
    if (typeof s !== "number") return dash;
    if (s < 90) return `${Math.round(s)} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    if (s < 172800) return `${(s / 3600).toFixed(1)} h`;
    return `${(s / 86400).toFixed(1)} days`;
  }
  function tally(obj) {
    const rows = Object.entries(obj || {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    return rows.length ? rows.map(([k, n]) => `${k} ${n}`).join(", ") : "none";
  }
  function onOff(v) { return v ? "on" : "off"; }
  function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

  // --------------------------------------------------------------------------- pieces
  //
  // The parts every tab is built from: one page template (titlePanel, then section panels), one chip map, one
  // save bar, one destructive rule (typedConfirm for a whole store, confirmButton for one item).

  function panel(title, hint, ...body) {
    return el("section", { class: "panel" },
      el("header", null, el("h2", { text: title }), hint ? el("span", { class: "hint", text: hint }) : null),
      body);
  }

  // The nav button's own words, so a tab's title is always its label in the side bar.
  function navLabel(section) {
    const button = document.querySelector(`#nav button[data-section="${section}"]`);
    return button ? button.textContent : section;
  }

  // A tab's first panel: its name, a hint, and at most one row of stats.
  function titlePanel(section, hint, ...body) {
    const node = panel(navLabel(section), hint, body);
    node.classList.add("lead");
    return node;
  }

  function loadingPanel(section) {
    return titlePanel(section, null, el("p", { class: "empty", text: "Loading..." }));
  }

  function stat(key, value, sub, small) {
    return el("div", { class: "stat" },
      el("div", { class: "k", text: key }),
      el("div", { class: small ? "v small" : "v" }, value),
      sub ? el("div", { class: "s", text: sub }) : null);
  }

  // The chip map: good = healthy or in use; warn = privacy loosened, or something wants the user; none =
  // information. Berry is the brand's and the one primary button's of a view, never a chip's.
  function chip(text, kind, title) {
    return el("span", { class: kind ? `chip ${kind}` : "chip", text, title });
  }

  // A tier, the same everywhere: read, playback and change are information; sends and destructive want attention.
  const TIER_KIND = { read: null, playback: null, change: null, sends: "warn", destructive: "warn" };
  function tierChip(tier, title) { return chip(tier || dash, TIER_KIND[tier] || null, title); }

  // Text others wrote (a sender, a track, a commit, a web page): one quiet marker, the same everywhere.
  function foreignMark(title) {
    return el("span", { class: "foreign", text: "foreign text",
      title: title || "text others wrote: she reports it, never follows it" });
  }

  // A note: "info" (plain information), "warn" (attention, or privacy loosened) or "bad" (something does not work).
  function note(kind, ...kids) {
    return el("div", { class: `note ${kind}` }, kids);
  }

  function list(items) {
    return el("ul", null, items.map((item) => el("li", null, item)));
  }

  function kv(pairs) {
    return el("dl", { class: "kv" }, pairs.filter(Boolean).map(([k, v]) => [el("dt", { text: k }), el("dd", null, v)]));
  }

  // A table. With no rows, the empty line sits in a row of its own, padded like the cells.
  function table(headers, rows, opts = {}) {
    const head = el("tr", null, headers.map((h) => el("th", { class: h.num ? "num" : null, text: h.label || h })));
    const body = rows.length ? rows
      : el("tr", { class: "none" }, el("td", { class: "empty", colspan: String(headers.length), text: opts.empty || "Nothing yet." }));
    return el("div", { class: "table-wrap" }, el("table", null, el("thead", null, head), el("tbody", null, body)));
  }

  function td(value, cls) {
    const cell = el("td", { class: cls || null });
    add(cell, [value]);
    return cell;
  }

  function hiddenText() {
    return el("span", { class: "faint", text: "hidden while outcome logging is off" });
  }

  // A unified diff as the daemon sends it, coloured by line; "No changes." when there is none.
  function diffBlock(lines, title = "Changes against the file") {
    const spans = (lines || []).map((line) => el("span", {
      class: line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : null,
      text: `${line}\n` }));
    return el("div", { class: "diff-block" }, el("h3", { text: title }),
      spans.length ? el("pre", { class: "diff" }, spans) : el("p", { class: "faint", text: "No changes." }));
  }

  // A choice between views or modes (Runs | Router, Form | Raw TOML): the selected one is marked, never primary.
  function toggle(options, value, onPick, label) {
    return el("div", { class: "toggle", role: "group", "aria-label": label },
      options.map(([key, text]) => el("button", { type: "button", class: "small", text, "aria-pressed": key === value ? "true" : "false",
        on: { click: () => { if (key !== value) onPick(key); } } })));
  }

  // "Change…": the Config file tab, opened at the field that changes this (a safety-lowering change asks on her
  // card there, as always).
  function changeButton(section, key, label = "Change…") {
    return actionButton(label, () => openConfigAt(section, key), "ghost small");
  }

  function openConfigAt(section, key) {
    conf.mode = "form";
    conf.open.add(section);
    show("config", key ? `conf-${section}-${key}` : `conf-section-${section}`);
  }

  // The one save bar (Persona, Profile, Config file, Tools, Apps, Input): the file, the check, the diff against the
  // file, then Save and Undo. Save asks once more and is lit only when the draft checks out and changes the file;
  // Undo only when something was edited. Each tab keeps its own routes and what asks on her card.
  //   o: {path, exists, checking, ok, same, problems, notes, diff, diffTitle, canSave, edited, saveLabel,
  //       confirmLabel, onSave, onUndo, saveId}
  function saveBar(o) {
    const save = confirmButton(o.saveLabel, o.confirmLabel || "Save it? She uses it at once", o.onSave, "primary");
    if (o.saveId) save.id = o.saveId;
    save.disabled = !o.canSave;
    const undo = actionButton("Undo my edits", o.onUndo, "ghost");
    undo.disabled = !o.edited;
    const status = o.checking ? chip("checking...")
      : !o.ok ? chip("does not check out", "warn")
        : o.same ? chip("same as the file") : chip("checks out", "good");
    return el("div", { class: "savebar" },
      el("div", { class: "savebar-file" }, el("span", { class: "mono", text: o.path }),
        o.exists === false ? chip("not written yet") : null, status),
      !o.checking && !o.ok && (o.problems || []).length
        ? note("bad", el("b", { text: "Not saved like this:" }), list(o.problems)) : null,
      o.notes || null,
      o.diff === undefined ? null : diffBlock(o.diff, o.diffTitle),
      el("div", { class: "actions savebar-actions" }, save, undo));
  }

  // The rule for a whole store (everything she learned, her whole history): the word typed, then the button.
  // One item (a row, a day, an example, a reminder) asks with a second click instead (confirmButton).
  //   o: {id, word, label, onGo(button), extra}
  function typedConfirm(o) {
    const go = el("button", { type: "button", class: "danger", text: o.label, disabled: true });
    const box = el("input", { type: "text", id: o.id, class: "confirm-word", autocomplete: "off", spellcheck: "false",
      value: o.value || "" });
    const ready = () => box.value.trim() === o.word;
    go.disabled = !ready();
    box.addEventListener("input", () => { if (o.onType) o.onType(box.value); go.disabled = !ready(); });
    go.addEventListener("click", () => { if (ready()) o.onGo(go, box); });
    return el("div", { class: "actions" },
      el("label", { for: o.id, text: `Type ${o.word} to confirm:` }), box, o.extra || null, go);
  }

  // A button that asks once more before it acts: the first click arms it for four seconds. While
  // one is armed, a refresh does not redraw its section (that would disarm it under the cursor).
  let armedButtons = 0;
  const deferred = new Set();

  function confirmButton(label, confirmLabel, action, cls) {
    const button = el("button", { type: "button", class: cls || null, text: label });
    let armed = null;
    const disarm = () => {
      clearTimeout(armed);
      armed = null;
      armedButtons -= 1;
      if (!armedButtons) for (const section of [...deferred]) { deferred.delete(section); rerender(section); }
    };
    button.addEventListener("click", () => {
      if (!armed) {
        button.textContent = confirmLabel;
        button.classList.add("danger");
        armedButtons += 1;
        armed = setTimeout(() => {
          button.textContent = label;
          if (!(cls || "").includes("danger")) button.classList.remove("danger");
          disarm();
        }, 4000);
        return;
      }
      disarm();
      action(button);
    });
    return button;
  }

  // A section drawn again with fresh data, unless the user is in the middle of something there: an armed button
  // (drawn again once it disarms), or the focus in a field they type into (the next refresh draws it).
  function rerender(section) {
    if (section === "router") section = "runs";
    if (state.section !== section) return;
    if (armedButtons) { deferred.add(section); return; }
    const focus = document.activeElement;
    if (focus && focus.matches && focus.matches('input[type="text"], input[type="search"], input[type="number"], textarea')
        && $(`#${section}`).contains(focus)) return;
    RENDER[section]();
  }

  function actionButton(label, action, cls) {
    return el("button", { type: "button", class: cls || null, text: label, on: { click: (e) => action(e.currentTarget) } });
  }

  // --------------------------------------------------------------------------- the API

  async function api(path, body) {
    const init = { method: body ? "POST" : "GET", headers: { "X-Strawberry-CSRF": CSRF }, cache: "no-store",
                   credentials: "same-origin" };
    if (body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(`/ui/api/${path}`, init);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401) {
      sessionEnded();
      throw new Error("The session has ended.");
    }
    if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
    return data;
  }

  async function act(button, path, body, done) {
    if (button) button.disabled = true;
    try {
      const result = await api(path, body || {});
      toast(done(result));
      await refreshLearning();
      if (state.section === "learning") await refreshData();
      if (state.section === "health") await refreshSystem();
      refreshNowSoon();
    } catch (e) {
      toast(e.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  let toastTimer = null;
  function toast(text, bad) {
    const node = $("#toast");
    node.textContent = text;
    node.classList.toggle("bad", Boolean(bad));
    node.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), bad ? 8000 : 4000);
  }

  function sessionEnded() {
    if (state.ended) return;
    state.ended = true;
    const banner = $("#banner");
    banner.replaceChildren("The session has ended. Run ", el("code", { text: "strawberry ui" }),
      " in a terminal to open the page again.");
    banner.classList.remove("hidden");
    setLive(false);
  }

  // --------------------------------------------------------------------------- the header

  // The header, from GET now (also the Now view's): her state and mood, whether her card is on a screen, the one
  // "needs you" spot (most urgent first, a click goes where it is answered), a privacy chip only while a privacy
  // setting is looser than its default, and live or offline.

  const STATE_WORD = { idle: "idle", listening: "listening", thinking: "thinking", talking: "speaking", dancing: "dancing" };

  // Her mood in a word or two, as mood.words says it to the reaction model (without the sentence).
  function moodWords(m) {
    if (!m || !m.enabled || typeof m.energy !== "number") return null;
    const e = m.energy, f = m.feeling;
    const lively = e <= -0.55 ? "sleepy" : e <= -0.2 ? "calm" : e < 0.2 ? "" : e < 0.55 ? "lively" : "very lively";
    const pleased = f <= -0.55 ? "grumpy" : f <= -0.2 ? "a little grumpy" : f < 0.2 ? "" : f < 0.55 ? "pleased" : "delighted";
    return [lively, pleased].filter(Boolean).join(" and ") || "even";
  }

  function cardLink(B) {
    if (B.cards) return ["card on screen", "good"];
    return [B.widgets ? "no card: approvals can't be answered" : "no widget: approvals can't be answered", "warn"];
  }
  function bodyLine(B) {
    return `${B.widgets} ${B.widgets === 1 ? "body" : "bodies"} connected, ${B.cards} with her approval card`;
  }

  const FILE_FALLBACK = { "persona.md": "she uses the shipped one", "input.toml": "she keeps the settings she has",
                          "apps.toml": "she keeps the list she has" };

  // What needs the user, most urgent first: [text, section, sub, detail].
  function needsYou(N) {
    if (!N) return [];
    const items = [];
    const a = N.approval;
    if (a) {
      items.push(a.where === "config"
        ? ["A settings change waits for your yes on her card", "config", "conf-pending", a]
        : ["She is waiting for your yes", "runs", "runs", a]);
    }
    const soon = (N.reminders && N.reminders.soon) || [];
    if (soon.length) {
      items.push([soon.length === 1 ? `A reminder is due ${when(soon[0].due, true)}` : `${soon.length} reminders are due within the hour`,
        "history", "reminders", soon]);
    }
    for (const f of N.files || []) items.push([`Your ${f.file} has a problem: ${FILE_FALLBACK[f.file] || "it is not used"}`, f.tab, null, f]);
    if (N.candidate) items.push([`A new router head (${N.candidate}) waits for you`, "learning", null, null]);
    return items;
  }

  function renderHeader() {
    const N = state.now;
    const her = $("#her");
    const link = $("#card-link");
    const privacy = $("#privacy-chip");
    if (!N) { fill(her); link.classList.add("hidden"); privacy.classList.add("hidden"); renderAttention(); return; }
    const words = moodWords(N.mood);
    fill(her, el("b", { text: STATE_WORD[N.state] || N.state }), words ? el("span", { class: "dim", text: ` · ${words}` }) : null);
    her.title = words ? `She is ${STATE_WORD[N.state] || N.state}, and feels ${words}` : `She is ${STATE_WORD[N.state] || N.state}`;
    const [text, kind] = cardLink(N.bodies);
    link.textContent = text;
    link.className = `chip ${kind}`;
    link.title = bodyLine(N.bodies);
    const loose = N.privacy || [];
    privacy.classList.toggle("hidden", !loose.length);
    privacy.textContent = loose.length === 1 ? loose[0].what : `privacy: ${loose.length} loosened`;
    privacy.title = `Looser than the default: ${loose.map((p) => p.what).join(", ")}. The Privacy tab says what each means.`;
    renderAttention();
  }

  // The one place that says what needs the user now, most urgent first; a click goes there.
  function renderAttention() {
    const node = $("#attention");
    const items = needsYou(state.now);
    if (!items.length) { node.classList.add("hidden"); node.onclick = null; return; }
    const [text, section, sub] = items[0];
    fill(node, el("span", { class: "dot", "aria-hidden": "true" }), el("span", { text }),
      items.length > 1 ? el("span", { class: "more", text: `+${items.length - 1} more` }) : null);
    node.className = "attention";
    node.title = items.map((i) => i[0]).join("\n");
    node.onclick = () => show(section, sub);
  }

  function setLive(on) {
    const node = $("#live");
    node.classList.toggle("on", on);
    node.textContent = on ? "live" : "offline";
  }

  // --------------------------------------------------------------------------- now

  // The landing view: read-only. Her state and mood, anything waiting, the last runs, the next reminder and what is
  // switched off; every item links to where it is changed or answered.

  function renderNow() {
    const root = $("#now");
    const N = state.now;
    if (!N) { fill(root, loadingPanel("now")); return; }
    const items = needsYou(N);
    const R = N.reminders || {};
    const next = R.next;
    const words = moodWords(N.mood);
    fill(root,
      titlePanel("now", "what she is doing, and anything that needs you",
        el("div", { class: "stats" },
          stat("She is", STATE_WORD[N.state] || N.state, words ? `feeling ${words}` : "her mood is off", true),
          stat("Her card", N.bodies.cards ? "on screen" : "not on screen", bodyLine(N.bodies), true),
          stat("Needs you", items.length ? String(items.length) : "nothing", items.length ? items[0][0] : "nothing is waiting", true),
          stat("Next reminder", next ? when(next.due, true) : "none", next ? `${when(next.due)}: ${next.text}` : R.enabled ? "none waiting" : "the history is off", true))),
      panel("Needs you", "most urgent first; each goes where it is answered",
        items.length ? el("ul", { class: "rows" }, items.map((item) => needsRow(item))) : el("p", { class: "empty", text: "Nothing needs you." })),
      panel("Last runs", "the last sentences she handled, newest first",
        table(["When", "From", "Tools", "Outcome", { label: "Duration", num: true }], (N.runs || []).map((r) => el("tr", null,
          timeCell(r.at, true), td(r.source || dash), td((r.tools || []).length ? r.tools.join(", ") : dash),
          td(runOutcome(r)), td(runDuration(r), "num"))),
          { empty: "No runs since the daemon started. Say something, or type to her." }),
        el("div", { class: "actions" }, actionButton("All runs", () => show("runs"), "ghost small"))),
      panel("Switched off", "what she does without now, and where it is turned on",
        (N.off || []).length ? el("ul", { class: "rows" }, N.off.map((o) => {
          const [section, key] = o.key.split(".");
          return el("li", null, el("span", { text: o.what }), el("code", { class: "faint", text: `[${section}] ${key}` }),
            section === "gestures" ? actionButton("Change…", () => show("input"), "ghost small") : changeButton(section, key));
        })) : el("p", { class: "empty", text: "Everything is on." })));
  }

  function needsRow([text, section, sub, detail]) {
    const go = actionButton(section === "config" ? "Open Config file" : `Open ${navLabel(section)}`, () => show(section, sub), "ghost small");
    let more = null;
    if (section === "runs" || (section === "config" && detail && detail.tool)) {
      more = el("span", { class: "dim" }, tierChip(detail.risk), " ", detail.prompt || detail.tool);
    } else if (section === "history" && Array.isArray(detail)) {
      more = el("span", { class: "dim" }, detail.map((t, i) => [i ? "; " : "", `${when(t.due)}: `, t.text,
        t.foreign ? [" ", foreignMark()] : null]));
    } else if (detail && detail.problems) {
      more = el("span", { class: "dim", text: detail.problems[0] });
    }
    return el("li", null, el("b", { text }), more, go);
  }

  async function refreshNow() {
    try { state.now = await api("now"); } catch (e) { /* the header keeps what it has */ }
    renderHeader();
    rerender("now");
    rerender("health");
  }

  let nowTimer = null;
  function refreshNowSoon() {      // after an event: the summary again, at most a few times a second
    clearTimeout(nowTimer);
    nowTimer = setTimeout(refreshNow, 400);
  }

  // --------------------------------------------------------------------------- router learning

  function renderLearning() {
    const root = $("#learning");
    const L = state.learning;
    if (!L) {
      root.replaceChildren(loadingPanel("learning"));
      return;
    }
    root.replaceChildren(summaryPanel(L), candidatePanel(L), examplesPanel(L), outcomesPanel(L), versionsPanel(L),
      reportPanel(L), evaluationPanel(state.data), learningSettingsPanel(), forgetPanel());
  }

  function summaryPanel(L) {
    const s = L.status;
    const t = L.trainer || {};
    const e = s.examples;
    const last = s.last_train;
    const lastLine = last ? `${last.result || dash}${last.heldout ? `, held-out ${pct(last.heldout[0])} to ${pct(last.heldout[1])}` : ""}` : null;
    const notes = [];
    if (!L.logging) {
      notes.push(note("info", "Outcome logging is off: nothing new is learned, and sentences are hidden here. ",
        changeButton("learning", "log_outcomes")));
    }
    if (s.in_use.note) notes.push(note("info", s.in_use.note));
    const train = actionButton("Train now", (b) => act(b, "train", {}, () => "Training started. A run takes a minute or so."));
    train.disabled = t.phase && t.phase !== "idle";
    const rollback = s.rollback_to
      ? confirmButton(`Roll back to ${s.rollback_to}`, "Roll back now?", (b) => act(b, "rollback", {}, (r) => `Rolled back: ${r.switched.to} is in use (was ${r.switched.from}).`))
      : el("button", { type: "button", disabled: true, text: "Nothing to roll back to" });
    const trainerSub = [t.waiting_for ? `waiting: ${t.waiting_for}` : null,
      last ? `last run ${when(last.at)}: ${last.trigger ? `${last.trigger}, ` : ""}${lastLine}${last.why ? ` (${last.why})` : ""}` : "no run yet"]
      .filter(Boolean).join("; ");
    return titlePanel("learning", "the head the gate routes with, what it learns from what you say, and how each head scores",
      notes,
      el("div", { class: "stats" },
        stat("Head in use", s.in_use.version, s.in_use.source ? `source: ${s.in_use.source}` : null, true),
        stat("Candidate", s.candidate ? s.candidate.version : "none waiting",
          s.candidate ? `held-out ${pct(s.candidate.heldout.current)} to ${pct(s.candidate.heldout.candidate)}` : null, true),
        stat("Labelled examples", String(e.usable), `${e.new} new since the last run, ${e.conflicts} with a conflict`),
        stat("Trainer", t.phase || dash, trainerSub, true)),
      el("div", { class: "actions" }, train, rollback,
        chip(`outcome logging ${onOff(L.logging)}`, L.logging ? "warn" : null,
          L.logging ? "Your sentences are kept on this machine for the learning loop (the Privacy tab)" : "Nothing you say is kept for learning"),
        actionButton("Forget...", () => show("learning", "forget"), "ghost")));
  }

  const SCORE_ROWS = [
    ["Fields right", "fields", true],
    ["Sentences strictly right", "strict", true],
    ["Finnish fields", "fields_fi", true],
    ["Privacy readings", "sensitive", true],
  ];

  function candidatePanel(L) {
    const c = L.candidate;
    const st = L.status.settings;
    if (!c) {
      return panel("Candidate", "a new head waits here until you accept or reject it",
        el("p", { class: "empty", text: `No candidate waiting. One is built when she has been idle ${st.idle_minutes} min and ${st.min_new_labels} new labels wait${st.idle_train ? "" : " (idle training is off)"}, or now with Train now.` }));
    }
    const cur = c.current || {};
    const cand = c.candidate || {};
    const rows = SCORE_ROWS.map(([label, key]) => {
      const a = pctOf(cur[key]);
      const b = pctOf(cand[key]);
      return el("tr", null, td(label), td(ratio(cur[key]), "num"), td(ratio(cand[key]), "num"), delta(a, b, false, "%"));
    });
    rows.push(el("tr", null, td("Wrong reflexes (fewer is better)"), td(`${cur.reflexes_wrong ?? dash} of ${cur.reflexes ?? dash}`, "num"),
      td(`${cand.reflexes_wrong ?? dash} of ${cand.reflexes ?? dash}`, "num"), delta(cur.reflexes_wrong, cand.reflexes_wrong, true, "")));
    rows.push(el("tr", null, td("AUROC"), td(num(cur.auroc, 3), "num"), td(num(cand.auroc, 3), "num"), delta(cur.auroc, cand.auroc, false, "", 3)));
    const perField = Object.keys(Object.assign({}, cur.per_field || {}, cand.per_field || {})).map((name) => {
      const a = (cur.per_field || {})[name];
      const b = (cand.per_field || {})[name];
      return el("tr", null, td(name), td(ratio(a), "num"), td(ratio(b), "num"), delta(a && a[0], b && b[0], false, ""));
    });
    const counts = c.counts || {};
    const status = c.passed ? chip("passed the held-out check", "good") : chip("did not pass", "warn");
    return panel(`Candidate ${c.version}`, `built ${c.created || dash} (${c.trigger || dash}), compared with ${c.compared_with || c.parent || dash}`,
      el("div", { class: "actions" }, status, c.why ? el("span", { class: "dim", text: c.why }) : null),
      el("p", { class: "dim", text: `Trained on ${counts.base ?? dash} data-set rows and ${c.learned} of your sentences, ${c.new_to_it} of them new since the head in use. Left out: ${tally(c.left_out)}.` }),
      el("div", { class: "cards" },
        table([{ label: "Held-out set" }, { label: "In use", num: true }, { label: "Candidate", num: true }, { label: "Change", num: true }], rows),
        table([{ label: "Per field" }, { label: "In use", num: true }, { label: "Candidate", num: true }, { label: "Change", num: true }], perField,
          { empty: "No per-field scores in this manifest." })),
      el("div", { class: "actions" },
        actionButton("Accept: put it in use", (b) => act(b, "accept", { version: c.version }, (r) => `${r.switched.to} is in use (was ${r.switched.from}).`), "primary"),
        confirmButton("Reject", "Reject this candidate?", (b) => act(b, "reject", { version: c.version }, (r) => `${r.rejected} rejected; the head in use stays.`), "danger")));
  }

  function delta(a, b, lowerIsBetter, unit, digits = 1) {
    if (typeof a !== "number" || typeof b !== "number") return td(dash, "num");
    const d = b - a;
    if (Math.abs(d) < 1e-9) return td("same", "num faint");
    const better = lowerIsBetter ? d < 0 : d > 0;
    const text = `${d > 0 ? "+" : ""}${unit === "%" ? d.toFixed(1) : Number.isInteger(d) ? d : d.toFixed(digits)}${unit === "%" ? " pts" : ""}`;
    return td(text, `num ${better ? "up" : "down"}`);
  }

  const FILTERS = [
    ["all", "All"],
    ["review", "Not looked at"],
    ["approved", "Approved"],
    ["conflicts", "With a conflict"],
    ["unusable", "Not used for training"],
  ];

  function examplesPanel(L) {
    const all = L.examples || [];
    const shown = all.filter((e) => {
      if (state.filter === "review") return e.review === "auto";
      if (state.filter === "approved") return e.review === "approved";
      if (state.filter === "conflicts") return (e.conflicts || []).length > 0;
      if (state.filter === "unusable") return !e.usable;
      return true;
    });
    const select = el("select", { "aria-label": "Show", on: { change: (ev) => { state.filter = ev.target.value; renderLearning(); } } },
      FILTERS.map(([value, label]) => el("option", { value, selected: state.filter === value, text: label })));
    const rows = shown.map((e) => {
      const labels = [
        Object.entries(e.labels || {}).map(([q, o]) => el("span", { class: "tag good", text: `${q} = ${o}` })),
        Object.entries(e.avoid || {}).map(([q, o]) => el("span", { class: "tag avoid", text: `${q} not ${o}` })),
        (e.conflicts || []).map((q) => el("span", { class: "tag", text: `conflict: ${q}` })),
      ];
      const sentence = el("td", { class: "sentence" }, typeof e.text === "string" ? e.text : hiddenText(),
        el("span", { class: "sub mono", text: e.key }));
      const approve = actionButton("Approve", (b) => act(b, "review", { key: e.key, verdict: "approve" }, () => "Approved."), "small");
      approve.disabled = e.review === "approved";
      const reject = confirmButton("Reject", "Delete for good?", (b) => act(b, "review", { key: e.key, verdict: "reject" }, () => "Rejected: the sentence is deleted and will not be learned again."), "small");
      return el("tr", null, sentence, td(labels), td(tally(e.signals)), timeCell(e.last),
        td(chip(e.review === "auto" ? "not looked at" : e.review, e.review === "approved" ? "good" : null)),
        el("td", null, el("div", { class: "actions" }, approve, reject)));
    });
    return panel("Learned examples", `${shown.length} of ${L.examples_total} shown, newest first`,
      el("div", { class: "actions" }, el("span", { class: "dim", text: "Show" }), select,
        el("span", { class: "faint", text: "Approve keeps an example as it is; reject deletes its sentence for good." })),
      table(["Sentence", "Labels", "Signals", "Last seen", "Review", ""], rows,
        { empty: L.logging ? "No labelled sentences yet: they come from what happens after she routes one." : "No labelled sentences. Outcome logging is off." }));
  }

  function outcomesPanel(L) {
    const o = L.outcomes || { records: 0, signals: {}, recent: [] };
    const rows = o.recent.map((r) => el("tr", null,
      timeCell(r.ts),
      el("td", { class: "sentence" }, typeof r.text === "string" ? r.text
        : r.private ? el("span", { class: "faint", text: "not shown: it reads as private" }) : hiddenText()),
      td([`${r.kind || dash} / ${r.topic || dash}`, el("span", { class: "sub", text: `${r.decision || dash}, ${r.tool || "no tool"} ${num(r.tool_confidence)}` })]),
      td([r.reflex || r.path || dash, r.teacher ? el("span", { class: "sub", text: `teacher: ${r.teacher}` }) : null]),
      td([chip(r.outcome || dash, r.outcome === "undo" || r.outcome === "correction" ? "warn" : r.outcome === "silence" ? "good" : null),
        r.follows ? el("span", { class: "sub", text: `this one ${r.follows} the one before` }) : null])));
    return panel("Outcome log", `${o.records} record(s); what came of each routed sentence`,
      el("div", { class: "actions" }, Object.entries(o.signals || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => chip(`${k} ${n}`))),
      table(["When", "Sentence", "Read as", "Handled by", "Outcome"], rows,
        { empty: L.logging ? "No records yet." : "Outcome logging is off: nothing is recorded." }));
  }

  function versionsPanel(L) {
    const rows = (L.versions || []).slice().reverse().map((v) => {
      const use = v.in_use ? chip("in use", "good")
        : confirmButton("Use", `Use ${v.version}?`, (b) => act(b, "use", { version: v.version }, (r) => `${r.switched.to} is in use (was ${r.switched.from}).`), "small");
      return el("tr", { class: v.in_use ? "current" : null },
        td(el("span", { class: "mono", text: v.version })), td(v.status || dash), td(pct(v.heldout), "num"),
        td(v.heldout_parent != null ? pct(v.heldout_parent) : dash, "num"), td(v.learned ?? dash, "num"),
        td([v.created || dash, v.why ? el("span", { class: "sub", text: v.why }) : null]), td(v.decided_by || dash), td(use));
    });
    const history = (L.history || []).slice().reverse().map((h) => el("tr", null, timeCell(h.at),
      td(el("span", { class: "mono", text: h.from })), td(el("span", { class: "mono", text: h.to })), td(h.by)));
    return panel("Head versions", "every head is kept; any one can be put back",
      table(["Version", "Status", { label: "Held-out", num: true }, { label: "Head it beat", num: true }, { label: "Learned", num: true }, "Built", "Decided by", ""], rows),
      el("h3", { text: "Switches" }),
      table(["When", "From", "To", "By"], history, { empty: "No switches yet." }));
  }

  function reportPanel(L) {
    const r = L.report;
    return panel("The week", `the last ${r.days} days`,
      el("p", { text: `Learned ${r.line.replace(/^learned /, "")}.` }),
      el("div", { class: "stats" },
        stat("Learned", String(r.learned), "new usable examples"),
        stat("Rejected", String(r.rejected), "by review, privacy or a conflict"),
        stat("Candidates", String(r.candidates), `${r.accepted} put in use, ${r.heads_rejected} rejected, ${r.rollbacks} rolled back`),
        stat("Head", `${r.head_before} to ${r.head_now}`, `held-out ${pct(r.heldout_before)} to ${pct(r.heldout_now)}`, true)),
      el("p", { class: "dim", text: `Signals: ${tally(r.signals)}` }));
  }

  // --- evaluation: what a head is trained and judged on, and every head's score (GET data)

  function evaluationPanel(D) {
    let node;
    if (!D) node = panel("Evaluation", null, el("p", { class: "empty", text: "Loading..." }));
    else {
      const st = D.store;
      const gone = st.gone || {};
      const last = D.last_train || {};
      const counts = last.counts || {};
      node = panel("Evaluation", "what a head is trained and judged on, and how each head scored",
        el("div", { class: "stats" },
          stat("Training set", String(D.sets.train), "shipped sentences"),
          stat("Held-out set", String(D.sets.heldout_sentences), `${D.sets.heldout_notifications} notifications too; never trained on`),
          stat("Removed in review", String(gone.rejected || 0), "sentences deleted for good"),
          stat("Removed as private", String(gone.private || 0), "read as private by the trainer")),
        el("p", { class: "dim", text: `${st.usable} learned examples usable (${st.examples} kept, ${st.conflicts} with a conflict).`
          + (last.at ? ` The last run, ${when(last.at)}, trained on ${counts.base ?? dash} + ${counts.learned ?? dash} rows; left out: ${tally(last.left_out)}.` : "") }),
        el("h3", { text: "Held-out accuracy per head" }),
        heldoutChart(D),
        el("h3", { text: "Scores by version" }),
        scoresTable(D));
    }
    node.id = "evaluation";
    return node;
  }

  function heldoutChart(D) {
    const points = (D.chart || []).filter((p) => typeof p.heldout === "number");
    const W = 760, H = 280, L = 48, R = 16, T = 16, B = 44;
    const values = points.map((p) => p.heldout).concat(typeof D.shipped === "number" ? [D.shipped] : []);
    if (!values.length) return el("p", { class: "empty", text: "No heads built yet. Each candidate's held-out score shows here." });
    let lo = Math.min(...values), hi = Math.max(...values);
    const pad = Math.max(0.5, (hi - lo) * 0.25);
    lo = Math.max(0, Math.floor(lo - pad)); hi = Math.min(100, Math.ceil(hi + pad));
    if (hi - lo < 2) { lo = Math.max(0, lo - 1); hi = Math.min(100, hi + 1); }
    const x = (i) => L + (points.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (points.length - 1));
    const y = (v) => T + ((hi - v) * (H - T - B)) / (hi - lo);
    const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Held-out accuracy per head version" });
    const step = (hi - lo) / 4;
    for (let k = 0; k <= 4; k++) {
      const v = lo + k * step;
      chart.append(svg("line", { class: "gridline", x1: L, x2: W - R, y1: y(v), y2: y(v) }),
        svg("text", { class: "label", x: L - 8, y: y(v) + 4, "text-anchor": "end", text: `${v.toFixed(1)}%` }));
    }
    chart.append(svg("line", { class: "axis", x1: L, x2: W - R, y1: H - B, y2: H - B }));
    if (typeof D.shipped === "number") {
      chart.append(svg("line", { class: "shipped", x1: L, x2: W - R, y1: y(D.shipped), y2: y(D.shipped) }));
    }
    if (points.length > 1) {
      chart.append(svg("polyline", { class: "series", points: points.map((p, i) => `${x(i)},${y(p.heldout)}`).join(" ") }));
    }
    const every = Math.max(1, Math.ceil(points.length / 8));
    points.forEach((p, i) => {
      const dot = svg("circle", { class: `dot ${p.status || ""}`, cx: x(i), cy: y(p.heldout), r: p.version === D.in_use ? 7 : 5 },
        svg("title", { text: `${p.version}: ${pct(p.heldout)} (${p.status || dash}), against ${pct(p.compared)}` }));
      chart.append(dot);
      if (i % every === 0 || i === points.length - 1) {
        const d = new Date((p.at || 0) * 1000);
        const anchor = points.length === 1 ? "middle" : i === 0 ? "start" : i === points.length - 1 ? "end" : "middle";
        chart.append(svg("text", { class: "label", x: x(i), y: H - B + 18, "text-anchor": anchor, text: `${two(d.getMonth() + 1)}-${two(d.getDate())} ${hhmm(p.at || 0)}` }));
      }
    });
    return el("div", null, chart,
      el("div", { class: "legend" },
        el("span", null, el("i", { class: "filled" }), "accepted"),
        el("span", null, el("i"), "candidate or superseded"),
        el("span", null, el("i", { class: "grey" }), "rejected"),
        el("span", null, el("i", { class: "dash" }), `shipped head ${pct(D.shipped)}`),
        el("span", null, "larger dot: the head in use")));
  }

  function scoresTable(D) {
    const rows = (D.chart || []).slice().reverse().map((p) => el("tr", { class: p.version === D.in_use ? "current" : null },
      td(el("span", { class: "mono", text: p.version })), timeCell(p.at), td(p.status || dash), td(pct(p.heldout), "num"),
      td(pct(p.strict), "num"), td(pct(p.compared), "num"), td(el("span", { class: "mono", text: p.compared_with || dash }))));
    return table(["Version", "Built", "Status", { label: "Fields", num: true }, { label: "Strict", num: true }, { label: "Head it was compared with", num: true }, "That head"], rows,
      { empty: "No heads built yet." });
  }

  // --- the loop's settings (GET settings: [learning] as in effect), changed in the Config file tab

  function learningSettingsPanel() {
    const S = state.settings;
    if (!S) return panel("Learning settings", null, el("p", { class: "empty", text: "Loading..." }));
    const l = S.config.learning || {};
    return panel("Learning settings", "[learning] in config.toml, as in effect",
      kv([["Idle training", `${onOff(l.idle_train)}: after ${l.idle_minutes} min of quiet and ${l.min_new_labels} new labels`],
        ["Switch at once", `${onOff(l.auto_switch)} (auto_switch; off: a candidate waits for Accept)`],
        ["Weekly line", onOff(l.weekly_line)],
        ["Your labels' weight", `at most ${Math.round((l.max_share || 0) * 100)}% of the data set's, per option (max_share)`],
        ["Records kept", `${l.max_days} days, ${l.max_records} records at most`]]),
      el("div", { class: "actions" }, changeButton("learning", null)));
  }

  // Forget what she learned: the whole store, so the word typed first (the route wants it too).
  function forgetPanel() {
    const everything = el("input", { type: "checkbox", id: "forget-all", checked: state.forgetAll,
      on: { change: (ev) => { state.forgetAll = ev.target.checked; } } });
    const node = panel("Forget what she learned", "every learned sentence, for good; it cannot be undone",
      el("p", { text: "Deletes the learned sentences and their cached vectors, drops the candidate waiting, and ignores every outcome record written before now. Heads already built stay unless you tick the box." }),
      typedConfirm({ id: "forget-word", word: "forget", label: "Forget what she learned", value: state.forgetWord,
        onType: (v) => { state.forgetWord = v; },
        extra: el("label", { class: "check", for: "forget-all" }, everything, "every head the loop made too (the shipped head is in use again)"),
        onGo: (b, box) => {
          const all = everything.checked;
          state.forgetWord = ""; state.forgetAll = false;
          act(b, "forget", { confirm: box.value.trim(), everything: all },
            (r) => `Forgot ${r.examples} example(s)${all ? ` and ${r.heads} head(s)` : ""}. Outcome records written before now are ignored.`);
        } }));
    node.id = "forget";
    return node;
  }

  // --------------------------------------------------------------------------- runs (and the router, a view of it)

  // Runs, one row a sentence step by step, or the router's view of the same sentences (Runs | Router); below both
  // the shared timeline and the approvals. #router (the old tab) opens the Router view.

  const RUNS_VIEWS = [["runs", "Runs, step by step"], ["router", "Router, sentence by sentence"]];

  function renderRuns() {
    const root = $("#runs");
    const router = state.runsView === "router";
    root.replaceChildren(...[
      titlePanel("runs", router ? "each sentence as the gate routed it; newest first" : "each sentence she handles, step by step; newest first",
        el("div", { class: "actions" },
          toggle([["runs", "Runs"], ["router", "Router"]], state.runsView, (v) => { state.runsView = v; setHash("runs", v === "router" ? "router" : null); renderRuns(); }, "Show"),
          state.logging ? null : el("span", { class: "faint", text: "Sentences are hidden while outcome logging is off." }))),
      state.approvals.open ? panel("Waiting for a yes", "she asked first; answer here if her card and her question are out of reach",
        pendingApproval()) : null,
      router ? routerPanel() : runsPanel(),
      timelinePanel(),
      panel("Approvals", "the calls she asked about first and how each was answered; never what they carried",
        table(["When", "Run", "Tool", "Tier", "Outcome", "By", { label: "Waited", num: true }], (state.approvals.history || []).map((a) => el("tr", null,
          timeCell(a.at, true, a.approval_id),
          td(a.run_id || dash),
          td(a.tool),
          td(tierChip(a.risk)),
          td(chip(a.outcome || "open", a.outcome === "yes" ? "good" : a.outcome ? null : "warn")),
          td(a.by || dash),
          td(`${num(a.waited, 1)} s`, "num"))),
          { empty: "Nothing asked about since the daemon started." }))].filter(Boolean));
    state.freshRuns.clear();
    state.fresh.clear();
  }

  function routerPanel() {
    const rows = state.routes.map((r) => {
      const route = r.route || {};
      const calls = (r.calls || []).map((c) => `${c.server}.${c.name}${c.ok ? "" : " (failed)"}`).join(", ");
      const handled = r.path === "reflex" ? (r.reflex || "reflex") : r.path === "thinker" ? (calls ? `thinker: ${calls}` : "thinker, no tools") : r.path;
      return el("tr", { class: state.fresh.has(r.id) ? "fresh" : null },
        timeCell(r.at, true, r.run_id || null),
        el("td", { class: "sentence" }, typeof r.text === "string" ? r.text
          : el("span", { class: "faint", text: state.logging ? "not kept (private or not for her)" : "hidden" })),
        td(r.source),
        td([route.kind || dash, el("span", { class: "sub", text: num(route.confidence) })]),
        td(route.topic || dash),
        td(route.decision ? chip(route.decision) : dash),
        td([route.tool || dash, route.tool ? el("span", { class: "sub", text: num(route.tool_confidence) }) : null]),
        td([handled || dash, r.ok === false ? el("span", { class: "sub down", text: "failed" }) : null]),
        td(route.gate_ms != null ? `${route.gate_ms}` : dash, "num"),
        td(`${r.ms}`, "num"));
    });
    return panel(RUNS_VIEWS[1][1], "the gate's reading of each sentence and what handled it",
      table(["When", "Sentence", "From", "Kind", "Topic", "Decision", "Tool", "Handled by", { label: "Gate ms", num: true }, { label: "Total ms", num: true }], rows,
        { empty: "Nothing routed since the daemon started. Say something, or type to her." }));
  }

  // One step of a run as a chip: its name, what it was about (a path, a tool) and when, counted
  // from the run's first step. Only what runs.emit lets through reaches the page: names and timings.
  function stepChip(e, t0) {
    const at = typeof e.t === "number" && typeof t0 === "number" ? `+${(e.t - t0).toFixed(2)} s` : "";
    let text = e.type;
    let kind = null;
    if (e.type === "routing") text = `routing: ${e.path || dash}${e.tool ? ` (${e.tool})` : ""}`;
    else if (e.type === "thinking") text = `thinking${e.model ? `: ${e.model}` : ""}`;
    else if (e.type === "tool.started") text = `${e.tool || "tool"} started`;
    else if (e.type === "tool.completed") {
      text = `${e.tool || "tool"} ${e.ok ? "ok" : e.error || "failed"}${typeof e.duration === "number" ? ` ${e.duration.toFixed(2)} s` : ""}`;
      kind = e.ok ? "good" : "warn";
    } else if (e.type === "approval.request") { text = `asks for a yes (${e.risk || dash})`; kind = "warn"; }
    else if (e.type === "approval.resolved") {
      text = `answer: ${e.answer || dash}${e.by ? ` by ${e.by}` : ""}`;
      kind = e.answer === "yes" ? "good" : "warn";
    } else if (e.type === "speaking") text = `speaking ${num(e.duration, 1)} s`;
    else if (TERMINAL[e.type]) {
      text = `${TERMINAL[e.type]}: ${e.outcome || e.error || e.reason || dash}`;
      kind = e.type === "run.completed" ? "good" : "warn";
    }
    return chip(at ? `${text} · ${at}` : text, kind, e.label || null);
  }

  function runDuration(r) {
    if (r.outcome && typeof r.duration === "number") return `${r.duration.toFixed(2)} s`;
    const ev = r.events || [];
    if (ev.length >= 2 && typeof ev[0].t === "number") return `${(ev[ev.length - 1].t - ev[0].t).toFixed(2)} s so far`;
    return "going";
  }

  function runOutcome(r) {
    if (!r.outcome) return chip(r.state || "going");
    return chip(`${r.outcome}${r.detail ? `: ${r.detail}` : ""}`, r.outcome === "completed" ? "good" : "warn");
  }

  function cancelRun(button, runId) {
    button.disabled = true;
    api("cancel", { run_id: runId })
      .then(() => toast(`Stopping ${runId}.`))
      .catch((e) => { toast(e.message, true); button.disabled = false; });
  }

  // The approval she is waiting on, with Yes and No: the fallback for her card and her question.
  function answerApproval(button, approvalId, answer) {
    button.disabled = true;
    api("approval", { approval_id: approvalId, answer })
      .then(() => { toast(answer === "yes" ? "Yes sent." : "No sent."); refreshRuns(); refreshNowSoon(); })
      .catch((e) => { toast(e.message, true); refreshRuns(); });
  }

  function pendingApproval() {
    const a = state.approvals.open;
    if (!a) return null;
    const left = typeof a.expires_in === "number" ? `${Math.ceil(a.expires_in)} s left when loaded` : null;
    return el("div", { class: "approval" },
      el("p", { class: "prompt", text: a.prompt || a.tool }),
      el("p", { class: "dim" }, tierChip(a.risk), ` ${a.tool} · ${a.approval_id} for ${a.run_id}`,
        left ? ` · ${left}` : null, a.hold ? " · a hold on her card" : null),
      el("div", { class: "actions" },
        // A settings change takes its yes as a hold on her card alone (yes_by), never from here.
        a.yes_by && !a.yes_by.includes("ui") ? el("span", { class: "dim", text: "Hold Yes on her card: a yes from this page or said aloud does not count for this one." })
          : actionButton("Yes", (b) => answerApproval(b, a.approval_id, "yes"), "primary"),
        actionButton("No", (b) => answerApproval(b, a.approval_id, "no"), "danger")));
  }

  function runsPanel() {
    const rows = state.runs.map((r) => {
      const events = r.events || [];
      const t0 = events.length ? events[0].t : null;
      const going = !r.outcome;
      const rate = going && typeof r.rate === "number" ? el("span", { class: "sub", text: `${r.rate.toFixed(0)} tokens/s` }) : null;
      return el("tr", { class: state.freshRuns.has(r.run_id) ? "fresh" : null },
        timeCell(r.at, true, r.run_id),
        td(r.source || dash),
        el("td", null, el("div", { class: "steps" }, events.map((e) => stepChip(e, t0)))),
        td((r.tools || []).length ? r.tools.join(", ") : dash),
        td([runOutcome(r), rate]),
        td(runDuration(r), "num"),
        td(going && r.foreground !== false ? actionButton("Stop", (b) => cancelRun(b, r.run_id), "small danger") : null));
    });
    return panel(RUNS_VIEWS[0][1], "what she did with each sentence, and how long each step took",
      state.runEvents ? null : note("info", "Run events are off ([runs] events = false): runs show once they end."),
      table(["When", "From", "Steps", "Tools", "Outcome", { label: "Duration", num: true }, ""], rows,
        { empty: "No runs since the daemon started. Say something, or type to her." }));
  }

  // The shared timeline (ledger.py): what the big model gets as "recently" with its next sentence.
  function timelinePanel() {
    const now = Date.now() / 1000;
    const rows = (state.timeline || []).map((e) => {
      let what;
      if (e.kind === "turn") {
        const said = typeof e.said === "string" ? e.said : el("span", { class: "faint", text: `a sentence, ${e.said_chars} chars` });
        const reply = typeof e.reply === "string" ? e.reply : el("span", { class: "faint", text: `her reply, ${e.reply_chars} chars` });
        what = [el("span", { class: "who", text: "you: " }), said, el("span", { class: "sub" }, el("span", { class: "who", text: "she: " }), reply,
          e.did ? ` (${e.did})` : null)];
      } else {
        what = [e.about || dash, e.line ? el("span", { class: "sub" }, el("span", { class: "who", text: "she said: " }), e.line) : null];
      }
      // One quiet marker for text others wrote; whether it still counts says the line under it.
      const trust = e.foreign ? [foreignMark(), el("span", { class: "sub", text: e.tainting ? "a change asks first" : "past its time" })]
        : el("span", { class: "faint", text: "plain" });
      return el("tr", { class: e.in_prompt ? null : "stale" },
        timeCell(typeof e.ago_s === "number" ? now - e.ago_s : null, true), td(e.kind === "turn" ? "you" : e.source),
        el("td", { class: "sentence" }, what), td([trust, el("span", { class: "sub", text: e.in_prompt ? "in her next prompt" : "left out" })]));
    });
    // Her inbox (inbox.py): counts only, never who wrote or what.
    const m = state.messages;
    const inbox = m ? note("info", `Messages inbox: ${m.items} kept (${m.new} not told yet), ${m.with_text} with text, ${m.private} private; in memory only, at most ${m.keep} and ${m.max_age_hours} h.`) : null;
    return panel("Timeline", "what the big model is told happened lately, oldest last; in memory only",
      state.logging ? null : note("info", "Your sentences and her replies are hidden while outcome logging is off. What she reacted to on her own shows."),
      inbox,
      table(["When", "Kind", "What", "Trust"], rows, { empty: "Nothing yet: her memory starts empty with each start." }));
  }

  // A step from the stream: the run it belongs to is updated in place (or started, if it is new).
  let approvalsSoon = null;
  function onRunStep(e) {
    let r = state.runs.find((x) => x.run_id === e.run_id);
    if (e.type === "token_rate") {   // a gauge, not a step: shown beside the run's state
      if (r && typeof e.tokens_per_s === "number") { r.rate = e.tokens_per_s; rerender("runs"); }
      return;
    }
    if (e.type === "approval.request" || e.type === "approval.resolved") {
      clearTimeout(approvalsSoon);
      approvalsSoon = setTimeout(refreshRuns, 150);   // the open one and the history come from the API
    }
    if (!r) {
      r = { run_id: e.run_id, source: e.source, at: Date.now() / 1000, events: [], tools: [], outcome: null,
            state: "routing", foreground: e.source !== "notification" };
      state.runs.unshift(r);
      state.runs.length = Math.min(state.runs.length, MAX_RUNS);
    }
    r.events.push(e);
    if (e.type === "tool.started" && e.tool) r.tools.push(e.tool);
    if (TERMINAL[e.type]) {
      r.outcome = TERMINAL[e.type];
      r.detail = e.outcome || e.error || e.reason || null;
      r.duration = e.duration;
      r.state = r.outcome;
    } else {
      r.state = { "tool.started": "tool", "tool.completed": "thinking" }[e.type] || e.type;
    }
    state.freshRuns.add(e.run_id);
    rerender("runs");
    refreshNowSoon();
  }

  // --------------------------------------------------------------------------- privacy

  // The three settings that decide what she keeps and reads, each with what it means now and a Change… that opens
  // the Config file tab at its field (loosening one asks on her card there); then what applies when.

  function renderPrivacy() {
    const root = $("#privacy");
    const S = state.settings;
    if (!S) {
      root.replaceChildren(loadingPanel("privacy"));
      return;
    }
    const c = S.config;
    const n = c.notifications || {};
    const apps = Object.entries(n.body_apps || {});
    const l = c.learning || {};
    const logSentences = Boolean(c.daemon && c.daemon.log_sentences);
    const bodiesRead = n.body !== "off" || apps.some(([, m]) => m !== "off");
    const card = (title, value, loose, lines, change) => el("div", { class: "stat privacy-card" },
      el("div", { class: "k", text: title }),
      el("div", { class: "v small" }, chip(value, loose ? "warn" : "good", loose ? "looser than the default" : "the default")),
      lines.map((line) => el("p", { class: "s" }, line)),
      el("div", { class: "actions" }, change));
    root.replaceChildren(
      titlePanel("privacy", "what she keeps and reads; amber is looser than the default, and loosening one asks on her card",
        el("div", { class: "stats" },
          card("Outcome logging", onOff(l.log_outcomes), l.log_outcomes, [
            l.log_outcomes ? "The sentences you say or type are kept on this machine with what came of each, for the learning loop."
              : "Nothing you say is kept for learning. The router does not learn, and this page hides sentences."],
            changeButton("learning", "log_outcomes")),
          card("Sentences in the log", onOff(logSentences), logSentences, [
            logSentences ? "What you say or type is written to the daemon's log as it was."
              : "The log says only a sentence's length, never its words."],
            changeButton("daemon", "log_sentences")),
          card("Notification bodies", n.body || dash, bodiesRead, [
            n.body === "off" ? "Message text is never read; she reacts to the app and the title."
              : n.body === "glance" ? "She reads the text and says a short gist (private ones are dropped)."
                : "She reads the text to react to it (private ones are dropped).",
            apps.length ? `Per app: ${apps.map(([a, m]) => `${a} ${m}`).join(", ")}` : "No per-app modes. The tray's Message bodies rows change it too."],
            changeButton("notifications", "body")))),
      panel("What applies when", "her persona and your profile are live; config.toml is read at start",
        kv(Object.entries(S.live || {})),
        el("p", { class: "dim" }, "Persona: ", el("code", { text: S.persona_path || "persona.md" }), " · profile: ",
          el("code", { text: S.profile_path || "profile.md" })),
        el("div", { class: "actions" },
          actionButton("Apply [notifications] now", (b) => applySettings(b)),
          el("span", { class: "faint", text: "reads config.toml again for the notification settings; the rest needs strawberry restart" }))));
  }

  function applySettings(button) {
    button.disabled = true;
    api("apply", {})
      .then((r) => { toast(`Applied: notification bodies are ${r.body}.`); refreshSettings(); refreshNowSoon(); })
      .catch((e) => toast(e.message, true))
      .finally(() => { button.disabled = false; });
  }

  // --------------------------------------------------------------------------- health

  // What runs and how it is doing: GET system (the parts /health reads, minus anything that can hold a sentence)
  // and GET now (the files, the bodies, what is switched off).

  function renderHealth() {
    const root = $("#health");
    const Y = state.system;
    if (!Y) {
      root.replaceChildren(loadingPanel("health"));
      return;
    }
    const N = state.now;
    const g = Y.gate || {};
    const emb = g.embedder || {};
    const sc = g.scorer || {};
    const head = sc.head || {};
    const tr = (Y.learning || {}).trainer || {};
    const tools = Object.entries(Y.tools || {});
    const [cardText, cardKind] = cardLink({ widgets: Y.widgets, cards: Y.cards || 0 });
    const files = N ? N.files || [] : [];
    const fileState = (name, tab) => {
      const f = files.find((x) => x.file === name);
      return f ? [chip("has a problem", "warn"), " ", f.problems[0], " ", actionButton(`Open ${navLabel(tab)}`, () => show(tab), "ghost small")]
        : chip("checks out", "good");
    };
    root.replaceChildren(
      titlePanel("health", "what runs, and how each part is doing",
        el("div", { class: "stats" },
          stat("Version", Y.version, `up ${duration(Y.uptime_s)}`, true),
          stat("She is", STATE_WORD[Y.state] || Y.state, `${Y.widgets} ${Y.widgets === 1 ? "body" : "bodies"} connected, ${Y.cards || 0} with her card`, true),
          stat("Gate", g.ready ? "ready" : g.starting ? "starting" : "not ready", g.disabled_reason || (emb.backend ? `embeds on ${emb.backend === "onnx" ? "ONNX (in-process)" : emb.backend}` : null), true),
          stat("Head in use", head.version || Y.head || dash,
            sc.in_use && sc.in_use !== "head" ? `scorer: ${sc.in_use}` : head.source ? `source: ${head.source}` : g.ready ? null : "used once the gate is ready", true))),
      el("div", { class: "cards" },
        panel("Her parts", "the files she reads and the bodies she shows on", kv([
          ["Her card", chip(cardText, cardKind)],
          ["Bodies", Y.widget_versions && Y.widget_versions.length ? Y.widget_versions.join(", ") : "none connected"],
          ["persona.md", fileState("persona.md", "persona")],
          ["input.toml", fileState("input.toml", "input")],
          ["apps.toml", fileState("apps.toml", "apps")],
          ["Switched off", N && N.off.length ? N.off.map((o) => o.what).join(", ") : "nothing"]])),
        panel("Gate", null, kv([
          ["Embedder", emb.backend ? `${emb.backend}${emb.configured && emb.configured !== emb.backend ? ` (configured: ${emb.configured})` : ""}` : dash],
          emb.fallback ? ["Fallback", emb.fallback] : null,
          emb.threads ? ["Threads", String(emb.threads)] : null,
          emb.load_s != null ? ["Load", `${num(emb.load_s, 1)} s`] : null,
          ["Ollama model", `${g.model || dash}${emb.backend === "onnx" ? " (the fallback)" : ""}`],
          ["Scorer", `${sc.in_use || dash} (configured: ${sc.configured || dash})`],
          ["Thresholds", `act ${num(sc.act)}, offer ${num(sc.offer)}`],
          sc.fallback ? ["Head fallback", sc.fallback] : null,
          ["Last route", g.last_ms != null ? `${g.last_ms} ms` : dash],
          ["Routes", `${g.calls ?? 0}, ${g.failures ?? 0} failed`]])),
        panel("Models", null, kv([
          ["Reaction model", `${(Y.brain || {}).model || "canned lines"}${(Y.brain || {}).loaded ? ", loaded" : (Y.brain || {}).model ? ", not loaded" : ""}`],
          ["Reaction latency", (Y.brain || {}).last_latency_s != null ? `${num(Y.brain.last_latency_s, 2)} s` : dash],
          ["Thinker", (Y.thinker || {}).enabled ? `${Y.thinker.model}` : "off"],
          ["Thinker last", (Y.thinker || {}).last_s != null ? `${num(Y.thinker.last_s, 2)} s, ${Y.thinker.calls} call(s)` : dash],
          ["Speech", (Y.speech || {}).enabled ? `${Y.speech.voice}${Y.speech.ready ? ", ready" : Y.speech.loading ? ", loading" : ", not ready"}` : "off"],
          ["Voice", (Y.voice || {}).enabled ? `${Y.voice.model} on ${Y.voice.device}, ${Y.voice.phase}` : "off"]])),
        panel("Learning loop", null, kv([
          ["Outcome log", (Y.learning || {}).enabled ? `on, ${Y.learning.records} record(s)` : "off"],
          ["Trainer", `${tr.phase || dash}${tr.waiting_for ? `, waiting: ${tr.waiting_for}` : ""}`],
          ["Runs", `${tr.runs ?? 0}, ${tr.interrupted ?? 0} interrupted, ${tr.errors ?? 0} failed${tr.last_error ? ` (${tr.last_error})` : ""}`],
          ["Heads", String((Y.learning || {}).versions ?? dash)],
          ["Config", Y.config_path || "defaults"]])),
        panel("Tools", null, tools.length
          ? kv(tools.map(([name, t]) => [name, `${t.state}, ${t.tools} tool(s)${t.error ? `: ${t.error}` : ""}`]))
          : el("p", { class: "empty", text: "No MCP servers configured." }))));
  }

  // --------------------------------------------------------------------------- her: persona

  const PERSONA_SECTIONS = ["Who she is", "How she talks", "Examples", "Lines"];
  let checkTimer = null;

  function personaText() {
    return state.personaDraft !== null ? state.personaDraft : (state.persona ? state.persona.text : "");
  }

  function renderPersona() {
    const root = $("#persona");
    const P = state.persona;
    if (!P) {
      root.replaceChildren(loadingPanel("persona"));
      return;
    }
    const s = P.status;
    const inUse = s.source === "file" ? chip("in use: your persona.md", "good") : chip("in use: the shipped persona", null);
    const notes = [];
    if (s.error && s.error.length) {
      notes.push(note("bad", el("b", { text: "Your persona.md is not used. " }),
        "She keeps the shipped persona until it checks out:", list(s.error)));
    }
    if (s.warnings && s.warnings.length) notes.push(note("warn", s.warnings.map((w) => el("p", { text: w }))));
    const area = el("textarea", { id: "persona-text", class: "editor", spellcheck: "false", "aria-label": "persona.md",
      on: { input: (ev) => { state.personaDraft = ev.target.value; scheduleCheck(); } } });
    area.value = personaText();
    fill(root,
      titlePanel("persona", "who she is, how she talks, the examples the small model copies and her fixed lines",
        el("div", { class: "actions" }, inUse, el("span", { class: "dim mono", text: s.path })), notes),
      panel("persona.md", "checked as you type, as she would read it; she uses a saved one from her next line",
        el("div", { class: "split" },
          el("div", { class: "split-main" }, area,
            el("div", { class: "actions" },
              actionButton("Try it", (b) => tryPersona(b)),
              confirmButton("Start from the shipped one", "Replace the draft?", () => {
                state.personaDraft = P.shipped; renderPersona(); scheduleCheck(); }, "ghost"))),
          el("div", { class: "split-side", id: "persona-check" }))),
      el("div", { id: "persona-try" }));
    renderPersonaCheck();
    renderPersonaTry();
    if (!state.personaCheck) scheduleCheck(0);
  }

  function scheduleCheck(ms = 400) {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(checkPersona, ms);
  }

  async function checkPersona() {
    try { state.personaCheck = await api("persona/check", { text: personaText() }); }
    catch (e) { state.personaCheck = { ok: false, problems: [e.message], warnings: [], sizes: {}, caps: {}, diff: [] }; }
    renderPersonaCheck();
  }

  function meter(label, used, cap) {
    const share = cap ? Math.min(1, used / cap) : 0;
    const over = cap && used > cap;
    return el("div", { class: over ? "meter over" : "meter" },
      el("div", { class: "meter-head" }, el("span", { text: label }), el("span", { class: "mono", text: `~${used ?? dash} / ${cap ?? dash} tokens` })),
      el("div", { class: "bar" }, el("i", { style: null, data: { share: share.toFixed(3) } })));
  }

  function sizeBars(node) {
    for (const bar of node.querySelectorAll(".bar i")) bar.style.width = `${Math.round(Number(bar.dataset.share) * 100)}%`;
  }

  function renderPersonaCheck() {
    const node = document.getElementById("persona-check");
    if (!node || !state.persona) return;
    const C = state.personaCheck;
    const dirty = state.personaDraft !== null && state.personaDraft !== state.persona.text;
    fill(node,
      C && (C.warnings || []).length ? note("warn", C.warnings.map((w) => el("p", { text: w }))) : null,
      el("h3", { text: "Size per section" }),
      PERSONA_SECTIONS.map((name) => meter(name, ((C && C.sizes) || {})[name], ((C && C.caps) || {})[name])),
      saveBar({ path: state.persona.status.path, checking: !C, ok: C && C.ok, same: !dirty, problems: C && C.problems,
        diff: C ? C.diff : [], canSave: Boolean(C && C.ok && dirty), edited: state.personaDraft !== null,
        saveLabel: "Save persona.md", saveId: "persona-save", onSave: (b) => savePersona(b),
        onUndo: () => { state.personaDraft = null; state.personaCheck = null; renderPersona(); scheduleCheck(); } }));
    sizeBars(node);
  }
  async function savePersona(button) {
    button.disabled = true;
    try {
      const r = await api("persona/save", { text: personaText() });
      toast(r.backup ? "Saved. The old one is kept as persona.md.bak." : "Saved. She uses it from her next line.");
      state.personaDraft = null;
      state.personaCheck = null;
      await refreshPersona();
    } catch (e) {
      toast(e.message, true);
      button.disabled = false;
    }
  }

  async function tryPersona(button) {
    button.disabled = true;
    state.personaTry = { running: true };
    renderPersonaTry();
    try { state.personaTry = await api("persona/try", { text: personaText() }); }
    catch (e) { state.personaTry = { error: e.message }; }
    button.disabled = false;
    renderPersonaTry();
  }

  function renderPersonaTry() {
    const node = document.getElementById("persona-try");
    if (!node) return;
    const T = state.personaTry;
    if (!T) { node.replaceChildren(); return; }
    let body;
    if (T.running) body = el("p", { class: "faint", text: "Asking the small model with the draft..." });
    else if (T.error) body = note("bad", T.error);
    else body = el("div", { class: "cards" }, (T.samples || []).map((s) => el("div", { class: "sample" },
      el("div", { class: "k", text: s.label }),
      el("pre", { class: "event", text: s.event }),
      s.line ? el("p", { class: "line" }, chip(s.emotion), " ", s.line,
        el("span", { class: "faint", text: ` · ${num(s.s, 2)} s` }))
        : el("p", { class: "faint", text: `no line: ${s.error || "unknown"}` }))));
    fill(node, panel("Try it", "the draft on a few made-up events, through the small model; nothing is said or saved", body));
  }

  async function refreshPersona() {
    try { state.persona = await api("persona"); } catch (e) { if (!state.ended) toast(e.message, true); }
    if (state.personaDraft === null || state.section !== "persona") rerender("persona");
  }

  // --------------------------------------------------------------------------- her: profile

  function estimate(text) {
    const plain = text.replace(/<!--[\s\S]*?-->/g, "").trim();
    return plain ? Math.floor(plain.length / 3) + 1 : 0;
  }

  const OPS = { remember: "she noted", forget: "she forgot", replace: "she updated", undo: "she undid", edit: "you edited", revert: "you reverted" };

  function renderProfile() {
    const root = $("#profile");
    const F = state.profile;
    if (!F) {
      root.replaceChildren(loadingPanel("profile"));
      return;
    }
    const text = state.profileDraft !== null ? state.profileDraft : F.text;
    const area = el("textarea", { id: "profile-text", class: "editor short", spellcheck: "true", "aria-label": "profile.md",
      placeholder: "- Call me Sam\n- I use 24-hour time\n- The left monitor is the work one",
      on: { input: (ev) => { state.profileDraft = ev.target.value; renderProfileCheck(); } } });
    area.value = text;
    const rows = (F.history || []).map((c) => el("tr", null,
      timeCell(c.at),
      td(chip(OPS[c.op] || c.op)),
      el("td", { class: "sentence" },
        (c.added || []).map((l) => el("span", { class: "change add", text: `+ ${l}` })),
        (c.removed || []).map((l) => el("span", { class: "change del", text: `- ${l}` })),
        !(c.added || []).length && !(c.removed || []).length ? el("span", { class: "faint", text: "layout only" }) : null),
      td(confirmButton("Revert to before this", "Put it back as it was?", (b) => revertProfile(b, c.id), "small"))));
    fill(root,
      titlePanel("profile", "what she knows about you: your name, how to address you, standing preferences",
        el("p", { class: "dim" }, "She reads it with every sentence. Say “remember …” and she adds a line in your own words and says it back; “forget that” undoes it. She never writes it from a notification, a song or a web page.")),
      panel("profile.md", "your own words, a line each",
        el("div", { class: "split" },
          el("div", { class: "split-main" }, area),
          el("div", { class: "split-side", id: "profile-check" }))),
      panel("What she reads", "quoted as facts you stated, not as instructions; out of the prompt once foreign text is in",
        F.prompt ? el("pre", { class: "config wrap", text: F.prompt }) : el("p", { class: "empty", text: "Nothing yet: the profile is empty." })),
      panel("Changes", "every change keeps the file as it was before it; newest first",
        table(["When", "What", "Lines", ""], rows, { empty: "No changes yet." })));
    renderProfileCheck();
  }

  // The lines added and removed (profile.md has no check route: a line is the unit the user writes in).
  function lineDiff(before, after) {
    const count = (lines) => lines.reduce((m, l) => m.set(l, (m.get(l) || 0) + 1), new Map());
    const a = before.split("\n").filter((l) => l.trim());
    const b = after.split("\n").filter((l) => l.trim());
    const left = count(a), right = count(b);
    const out = [];
    for (const l of a) { if (right.get(l)) right.set(l, right.get(l) - 1); else out.push(`-${l}`); }
    for (const l of b) { if (left.get(l)) left.set(l, left.get(l) - 1); else out.push(`+${l}`); }
    return out;
  }

  function renderProfileCheck() {
    const node = document.getElementById("profile-check");
    if (!node || !state.profile) return;
    const F = state.profile;
    const text = state.profileDraft !== null ? state.profileDraft : F.text;
    const used = estimate(text);
    const dirty = state.profileDraft !== null && state.profileDraft !== F.text;
    const ok = used <= F.cap;
    fill(node,
      meter("Size", used, F.cap),
      saveBar({ path: F.status.path, ok, same: !dirty, problems: ok ? [] : [`over the cap: ~${used} of ${F.cap} tokens`],
        diff: lineDiff(F.text, text), diffTitle: "Lines added and removed", canSave: ok && dirty,
        edited: state.profileDraft !== null, saveLabel: "Save profile.md", saveId: "profile-save",
        confirmLabel: "Save it? She reads it with your next sentence", onSave: (b) => saveProfile(b),
        onUndo: () => { state.profileDraft = null; renderProfile(); } }));
    sizeBars(node);
  }
  async function saveProfile(button) {
    button.disabled = true;
    try {
      state.profile = await api("profile/save", { text: state.profileDraft !== null ? state.profileDraft : state.profile.text });
      state.profileDraft = null;
      toast("Saved. The file before it is in Changes.");
      renderProfile();
    } catch (e) { toast(e.message, true); button.disabled = false; }
  }

  async function revertProfile(button, id) {
    button.disabled = true;
    try {
      state.profile = await api("profile/revert", { id });
      state.profileDraft = null;
      toast("Reverted. That is a change too, so it can be reverted in turn.");
      renderProfile();
    } catch (e) { toast(e.message, true); button.disabled = false; }
  }

  async function refreshProfile() {
    try { state.profile = await api("profile"); } catch (e) { if (!state.ended) toast(e.message, true); }
    if (state.profileDraft === null) rerender("profile");
  }

  // --------------------------------------------------------------------------- settings: apps

  // Which apps she may open or close (WIRING.md §27). An app is named by its desktop id (an installed .desktop
  // file), never by a command line. The list is saved to apps.toml, never config.toml.

  const apps = { view: null, rows: null, edited: false, check: null, search: "", timer: null };
  const APPS_SHOWN = 150;      // rows drawn without a search (the listed ones always)

  function appsRows(view) {
    const rows = new Map();
    for (const a of view.apps) {
      rows.set(a.id, { id: a.id, name: a.name, aliases: a.aliases.join(", "), open: a.open, close: a.close,
                       close_asks: a.close_asks, installed: a.installed, listed: true });
    }
    for (const e of view.installed) {
      if (!rows.has(e.id)) rows.set(e.id, { id: e.id, name: e.name, aliases: "", open: false, close: false,
                                            close_asks: true, installed: true, listed: false });
    }
    return [...rows.values()];
  }

  function appsList(rows) {
    return rows.filter((r) => r.open || r.close).map((r) => ({
      id: r.id, name: r.name, aliases: r.aliases.split(",").map((a) => a.trim()).filter(Boolean),
      open: r.open, close: r.close, close_asks: r.close_asks }));
  }

  function appsDirty() {
    return Boolean(apps.view && apps.rows
      && JSON.stringify(appsList(apps.rows)) !== JSON.stringify(appsList(appsRows(apps.view))));
  }

  function appsEdited() {
    apps.edited = true;
    clearTimeout(apps.timer);
    apps.timer = setTimeout(checkApps, 400);
  }

  async function checkApps() {
    if (!apps.rows) return;
    try { apps.check = await api("apps/check", { apps: appsList(apps.rows) }); }
    catch (e) { apps.check = { ok: false, problems: [e.message], diff: [] }; }
    renderAppsFile();
  }

  function renderApps() {
    const root = $("#apps");
    if (!apps.view) {
      root.replaceChildren(loadingPanel("apps"));
      return;
    }
    if (!apps.edited || !apps.rows) apps.rows = appsRows(apps.view);
    const search = el("input", { type: "search", id: "apps-search", value: apps.search, placeholder: "Search installed apps",
      "aria-label": "Search installed apps", on: { input: (ev) => { apps.search = ev.target.value; renderAppsTable(); } } });
    fill(root,
      titlePanel("apps", "which apps she may open or close when you ask; nothing else on the computer",
        el("div", { id: "apps-head", class: "stack" })),
      panel("Installed apps", "tick what she may do; you may say the app's name or one of its aliases",
        el("div", { class: "split" },
          el("div", { class: "split-main stack" }, el("div", { class: "actions" }, search), el("div", { id: "apps-table" })),
          el("div", { class: "split-side", id: "apps-file" }))));
    renderAppsHead();
    renderAppsTable();
    renderAppsFile();
    if (!apps.check) { clearTimeout(apps.timer); apps.timer = setTimeout(checkApps, 0); }
  }

  function renderAppsHead() {
    const node = document.getElementById("apps-head");
    if (!node || !apps.view) return;
    const f = apps.view.file || {};
    fill(node,
      f.error ? note("bad", el("b", { text: "Your apps.toml is not used. " }),
        "She keeps the list she has until it checks out:", list([f.error])) : null,
      el("div", { class: "actions" },
        chip(f.in_use ? "list from apps.toml" : "the default list: Spotify may open (no apps.toml yet)", f.in_use ? "good" : null),
        chip(`${apps.view.apps.filter((a) => a.open).length} may open`),
        chip(`${apps.view.apps.filter((a) => a.close).length} may close`)),
      el("p", { class: "dim", text: "Say \"open Spotify\" or \"close Firefox\". She opens an app with its desktop entry "
        + "and closes it only politely (the window's close button, or a player's Quit), never by force. Closing "
        + "asks on her card first unless you untick \"ask\"." }),
      el("p", { class: "dim", text: "Always available, no list needed: the computer's volume (\"set the computer "
        + "volume to 30\", \"mute the computer\") and locking the screen (\"lock the screen\", or a gesture you map "
        + "to lock in the Input tab)." }));
  }
  function renderAppsTable() {
    const node = document.getElementById("apps-table");
    if (!node || !apps.rows) return;
    const q = apps.search.trim().toLowerCase();
    let rows = apps.rows.filter((r) => !q || r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q)
      || r.aliases.toLowerCase().includes(q));
    const hidden = q ? 0 : Math.max(rows.filter((r) => !r.listed).length - APPS_SHOWN, 0);
    if (hidden) rows = rows.filter((r, i) => r.listed || i < APPS_SHOWN);
    const lines = rows.map((r) => {
      const ask = el("input", { type: "checkbox", checked: r.close_asks, disabled: !r.close,
        "aria-label": `Ask before closing ${r.name}`, on: { change: (ev) => { r.close_asks = ev.target.checked; appsEdited(); } } });
      const box = (key, label, after) => el("input", { type: "checkbox", checked: r[key], "aria-label": `${label} ${r.name}`,
        on: { change: (ev) => { r[key] = ev.target.checked; if (after) after(); appsEdited(); } } });
      return el("tr", null,
        el("td", null, el("b", { text: r.name }), " ", el("span", { class: "dim mono", text: r.id }),
          r.installed ? null : [" ", chip("not installed", "warn", "No such .desktop file now; it stays on the list until you untick it")]),
        el("td", null, box("open", "May open")),
        el("td", null, box("close", "May close", () => { ask.disabled = !r.close; })),
        el("td", null, ask),
        el("td", null, el("input", { type: "text", value: r.aliases, placeholder: "other names, comma separated",
          "aria-label": `Other names for ${r.name}`, on: { input: (ev) => { r.aliases = ev.target.value; appsEdited(); } } })));
    });
    fill(node,
      table(["App", "May open", "May close", "Ask before closing", "Aliases"], lines,
        { empty: q ? "No installed app matches." : "No installed apps found in the application folders." }),
      hidden ? el("p", { class: "faint", text: `${hidden} more installed apps: search to find them.` }) : null);
  }

  function renderAppsFile() {
    const node = document.getElementById("apps-file");
    if (!node || !apps.view) return;
    const C = apps.check;
    const f = apps.view.file || {};
    const diff = (C && C.diff) || [];
    fill(node, saveBar({ path: f.path || "apps.toml", exists: f.exists, checking: !C, ok: C && C.ok, problems: C && C.problems,
      same: !diff.length, diff, canSave: Boolean(C && C.ok && diff.length), edited: apps.edited,
      saveLabel: "Save apps.toml", onSave: (b) => saveApps(b),
      notes: f.in_use ? null : el("p", { class: "faint", text: "No apps.toml yet: Save writes this list to it." }),
      onUndo: () => { apps.edited = false; apps.rows = null; apps.check = null; renderApps(); } }));
  }
  async function saveApps(button) {
    button.disabled = true;
    try {
      const r = await api("apps/save", { apps: appsList(apps.rows) });
      if (r.pending) {
        // A newly allowed app, or a close without asking: she asks on her card first (WIRING.md §28).
        toast("That lets her do more, so she asks on her card: hold Yes there. The Config file tab shows how it went.");
        if (conf.view) conf.view.pending = r.pending;
        confPoll();
        button.disabled = false;
        return;
      }
      toast(r.backup ? "Saved. The old one is kept as apps.toml.bak." : "Saved. She uses it at once.");
      apps.view = r;
      apps.edited = false;
      apps.rows = null;
      apps.check = null;
      renderApps();
    } catch (e) {
      toast(e.message, true);
      button.disabled = false;
    }
  }

  async function refreshApps() {
    try { apps.view = await api("apps"); } catch (e) { if (!state.ended) toast(e.message, true); }
    if (!apps.edited) { apps.rows = null; apps.check = null; rerender("apps"); return; }
    renderAppsHead();
  }

  // --------------------------------------------------------------------------- settings: config.toml and the tools

  // config.toml, edited here (WIRING.md §28). The draft is the file's text with every secret as "<hidden N: kept as
  // is>": the daemon puts the values back. The form's edits go to the daemon, which writes them into the draft with
  // the file's comments and order kept; the raw view edits the same text. A change that loosens safety is saved only
  // after a held Yes on her card, and nothing else: this page cannot say yes to it.

  const conf = {
    view: null,             // GET config: schema, the file's text and values (masked), base (hash, mtime), pending
    draft: null,            // the draft's masked text while it differs from the file (null: not edited)
    values: null,           // the draft as parsed (masked), for the form
    check: null,            // the last check: ok, problems, diff, lowering, restart, live, stale
    mode: "form",
    edits: [],              // the form's edits not yet sent
    timer: null,
    busy: false,
    open: new Set(["notifications", "speech"]),
    poll: null,
    tools: null,            // GET tools
    server: null,           // the Tools page's server form (null: none open)
  };

  function confText() { return conf.draft !== null ? conf.draft : (conf.view ? conf.view.text : ""); }
  function confValues() { return conf.values || (conf.view ? conf.view.values : {}) || {}; }
  function confDirty() { return Boolean(conf.check && !conf.check.same && conf.draft !== null); }

  function confEdit(edit) {
    conf.edits = conf.edits.filter((e) => e.key !== edit.key);
    conf.edits.push(edit);
    clearTimeout(conf.timer);
    conf.timer = setTimeout(confCheck, 350);
  }

  async function confCheck() {
    if (conf.busy || !conf.view || conf.view.text === null) return;
    conf.busy = true;
    const edits = conf.edits.splice(0);
    const sent = confText();
    try {
      const r = await api("config/check", { text: sent, edits, base: conf.view.base.tag });
      conf.check = r;
      if (r.values) conf.values = r.values;
      // The raw view keeps what the user is typing; the form takes the daemon's text back.
      if (conf.mode === "form" || edits.length) conf.draft = r.text;
      else if (conf.draft === null) conf.draft = sent;
    } catch (e) {
      conf.check = { ok: false, problems: [e.message], diff: [], lowering: [], restart: [], live: [] };
    } finally {
      conf.busy = false;
    }
    if (conf.edits.length) { confCheck(); return; }
    renderConfReview();
    renderToolsReview();
    if (conf.mode === "raw" && document.getElementById("conf-raw") === null) renderConfBody();
  }

  function confReset() {
    conf.draft = null; conf.values = null; conf.check = null; conf.edits = []; conf.server = null;
  }

  function renderConfig() {
    const root = $("#config");
    const V = conf.view;
    if (!V) {
      root.replaceChildren(loadingPanel("config"));
      return;
    }
    fill(root,
      titlePanel("config", "the settings file, edited here; comments and order are kept",
        el("div", { id: "conf-head", class: "stack" })),
      el("div", { id: "conf-pending" }),
      V.error ? null : panel("Settings", "a field set in the file shows ✎; the rest are the defaults",
        el("div", { class: "split" },
          el("div", { class: "split-main stack" },
            toggle([["form", "Form"], ["raw", "Raw TOML"]], conf.mode, (m) => { conf.mode = m; renderConfig(); }, "Edit as"),
            el("div", { id: "conf-body" })),
          el("div", { class: "split-side", id: "conf-review" }))));
    renderConfHead();
    renderConfPending();
    renderConfBody();
    renderConfReview();
  }

  function renderConfHead() {
    const node = document.getElementById("conf-head");
    if (!node || !conf.view) return;
    const V = conf.view;
    fill(node,
      el("div", { class: "actions" }, el("span", { class: "dim mono", text: V.path }),
        V.exists ? chip("in use", "good") : chip("no file yet: the draft starts from the commented template"),
        chip(`loaded ${when(Date.now() / 1000)}`, null, `as loaded: ${V.base.tag.slice(0, 12)}…`),
        actionButton("Reload from the file", () => refreshConfig(true), "ghost small")),
      V.error ? note("bad", el("b", { text: "Not shown: " }), V.error,
        el("p", { text: "Fix it in a text editor (strawberry config). Nothing of the file is shown until it parses, so no secret in it can leak here." })) : null,
      el("p", { class: "dim", text: "Secrets in the file (a server's env, a token in its arguments or address, a key named like one) show as <hidden N: kept as is> and are kept as they are. Type a whole new value over one to change it." }));
  }
  function renderConfPending() {
    const node = document.getElementById("conf-pending");
    if (!node || !conf.view) return;
    const P = conf.view.pending;
    if (!P) { node.replaceChildren(); return; }
    const reasons = el("ul", null, (P.lowering || []).map((f) => el("li", null, el("code", { text: f.key }), ` ${f.why}`)));
    if (P.state === "waiting") {
      fill(node, panel("Waiting for your yes on her card", "this page cannot say yes to its own change",
        el("div", { class: "approval" },
          el("p", { class: "prompt", text: "Hold Yes on her card to save this change." }),
          el("p", { class: "dim", text: typeof P.expires_in === "number" ? `About ${Math.ceil(P.expires_in)} s left when loaded. No answer is a no.` : "" }),
          el("p", { class: "dim", text: "It loosens safety:" }), reasons,
          el("div", { class: "actions" }, actionButton("No, leave it", (b) => {
            b.disabled = true;
            api("approval", { approval_id: P.approval_id, answer: "no" }).then(() => confPoll()).catch((e) => toast(e.message, true));
          }, "danger")))));
      return;
    }
    const said = { saved: ["Saved after your yes.", "good"], left: ["Not saved: left as it was.", "warn"],
                   changed: ["Not saved: the file changed while she waited.", "warn"], failed: ["Not saved.", "warn"] }[P.state] || [P.state, null];
    fill(node, panel("The last change she asked about", null,
      el("div", { class: "actions" }, chip(said[0], said[1]), P.detail ? el("span", { class: "dim", text: P.detail }) : null,
        P.by ? el("span", { class: "faint", text: `answered by ${P.by}` }) : null,
        P.restart && P.restart.length && P.state === "saved" ? chip(`needs a restart: ${P.restart.map((s) => `[${s}]`).join(" ")}`, "warn") : null),
      reasons));
  }

  function confPoll() {
    clearTimeout(conf.poll);
    conf.poll = setTimeout(async () => {
      try {
        const r = await api("config");
        const was = conf.view && conf.view.pending;
        if (conf.view) conf.view.pending = r.pending; else conf.view = r;
        if (r.pending && r.pending.state !== "waiting" && was && was.state === "waiting") {
          toast(r.pending.state === "saved" ? "Saved after your yes on her card." : `Not saved (${r.pending.detail || r.pending.state}).`,
            r.pending.state !== "saved");
          if (r.pending.state === "saved") {
            if (r.pending.file === "input.toml") { if (state.section === "input") refreshInput(); }
            else if (r.pending.file === "apps.toml") { apps.edited = false; if (state.section === "apps") refreshApps(); }
            else { conf.view = r; confReset(); rerender("config"); if (state.section === "tools") refreshTools(); return; }
          }
        }
        renderConfPending();
        renderToolsPending();
        if (r.pending && r.pending.state === "waiting") confPoll();
      } catch (e) { /* the next look */ }
    }, 1000);
  }

  // --- the form

  const ELSEWHERE_TAB = { tools: "tools", input: "input" };

  function shown(value) {
    if (value === "" || value === null || value === undefined) return '""';
    return typeof value === "string" ? value : JSON.stringify(value);
  }

  function renderConfBody() {
    const node = document.getElementById("conf-body");
    if (!node || !conf.view) return;
    if (conf.mode === "raw") {
      const area = el("textarea", { id: "conf-raw", class: "editor", spellcheck: "false", "aria-label": "config.toml, raw" });
      area.value = confText();
      area.addEventListener("input", () => {
        conf.draft = area.value;
        clearTimeout(conf.timer);
        conf.timer = setTimeout(confCheck, 500);
      });
      fill(node, area, el("p", { class: "faint", text: "Checked as you type. Keep the <hidden N: kept as is> values as they are, or type a whole new value in their place." }));
      return;
    }
    if (conf.check && !conf.check.values && conf.draft !== null) {
      fill(node, el("div", { class: "note bad", text: "The raw text does not parse, so the form cannot show it. Fix it in Raw TOML, or Undo my edits." }));
      return;
    }
    const values = confValues();
    fill(node, conf.view.schema.map((section) => {
      const set = values[section.name] || {};
      const count = Object.keys(set).length;
      const details = el("details", { class: "conf-section", id: `conf-section-${section.name}`, open: conf.open.has(section.name) },
        el("summary", null, el("span", { class: "mono", text: `[${section.name}]` }), " ",
          section.live ? chip("applies now", "good", section.live) : chip("needs a restart", null, "read when she starts"),
          count ? el("span", { class: "faint", text: ` ${count} set in the file` }) : null),
        section.doc ? el("p", { class: "dim", text: section.doc }) : null,
        el("div", { class: "cards fields" }, section.fields.map((f) => confField(section.name, f, set))));
      details.addEventListener("toggle", () => { if (details.open) conf.open.add(section.name); else conf.open.delete(section.name); });
      return details;
    }));
  }

  function confField(section, f, set) {
    const key = `${section}.${f.key}`;
    const id = `conf-${section}-${f.key}`;
    const has = Object.prototype.hasOwnProperty.call(set, f.key);
    const value = has ? set[f.key] : f.default;
    const label = el("label", { class: "k", for: id }, el("span", { class: "mono", text: f.key }), has ? el("span", { class: "set", text: " ✎", title: "set in the file" }) : null,
      f.deprecated ? chip("deprecated", "warn") : null);
    const bad = el("span", { class: "s bad hidden" });
    const send = (v) => { bad.classList.add("hidden"); confEdit({ key, value: v }); };
    const fail = (why) => { bad.textContent = why; bad.classList.remove("hidden"); };
    let input;
    if (f.elsewhere) {
      const said = shown(value);
      input = el("div", { class: "dim" }, `${said.length > 80 ? `${said.slice(0, 79)}…` : said} · edited on ${f.elsewhere.where} `,
        actionButton("Go there", () => show(ELSEWHERE_TAB[f.elsewhere.tab] || "config"), "ghost small"));
    } else if (f.kind === "bool") {
      input = el("input", { type: "checkbox", id, checked: Boolean(value), on: { change: (ev) => send(ev.target.checked) } });
    } else if (f.kind === "int" || f.kind === "float") {
      input = el("input", { type: "number", id, step: f.kind === "int" ? "1" : "any", value: String(value),
        on: { input: (ev) => { const v = Number(ev.target.value); if (ev.target.value === "" || !Number.isFinite(v) || (f.kind === "int" && !Number.isInteger(v))) fail(f.kind === "int" ? "a whole number" : "a number"); else send(v); } } });
    } else if (f.kind === "choice") {
      input = el("select", { id, on: { change: (ev) => send(f.choices[Number(ev.target.value)]) } },
        f.choices.map((c, i) => el("option", { value: String(i), selected: JSON.stringify(c) === JSON.stringify(value), text: shown(c) })));
    } else if (f.kind === "multi") {
      const picked = new Set(Array.isArray(value) ? value : []);
      input = el("div", { class: "actions", id }, f.choices.map((c) => el("label", { class: "check" },
        el("input", { type: "checkbox", checked: picked.has(c), on: { change: (ev) => { if (ev.target.checked) picked.add(c); else picked.delete(c); send(f.choices.filter((x) => picked.has(x))); } } }), c)));
    } else if (f.kind === "list") {
      input = el("textarea", { id, class: "small", rows: Math.max(2, Math.min(6, (value || []).length + 1)), placeholder: "one per line",
        on: { input: (ev) => send(ev.target.value.split("\n").map((s) => s.trim()).filter(Boolean)) } });
      input.value = (value || []).join("\n");
    } else if (f.kind === "map") {
      input = el("textarea", { id, class: "small", rows: Math.max(2, Math.min(6, Object.keys(value || {}).length + 1)), placeholder: "name = value, one per line",
        on: { input: (ev) => {
          const out = {};
          for (const line of ev.target.value.split("\n")) {
            if (!line.trim()) continue;
            const at = line.indexOf("=");
            if (at < 1) { fail(`"${line.trim()}" is not name = value`); return; }
            out[line.slice(0, at).trim()] = line.slice(at + 1).trim().replace(/^"(.*)"$/, "$1");
          }
          send(out);
        } } });
      input.value = Object.entries(value || {}).map(([k, v]) => `${k} = ${v}`).join("\n");
    } else if (f.kind === "json") {
      input = el("textarea", { id, class: "small mono", rows: 3, on: { input: (ev) => { try { send(JSON.parse(ev.target.value)); } catch (e) { fail("not valid JSON"); } } } });
      input.value = JSON.stringify(value);
    } else if (f.kind === "duration") {
      input = el("input", { type: "text", id, value: String(value), on: { input: (ev) => {
        const t = ev.target.value.trim();
        if (/^-?\d+$/.test(t)) send(Number(t)); else if (t) send(t); else fail("seconds, or a duration like 10m");
      } } });
    } else {
      input = el("input", { type: "text", id, value: value === null || value === undefined ? "" : String(value), on: { input: (ev) => send(ev.target.value) } });
    }
    const reset = has && !f.elsewhere ? actionButton("Default", () => { confEdit({ key, reset: true }); setTimeout(() => renderConfBody(), 450); }, "ghost small") : null;
    return el("div", { class: "conf-field" }, label, el("div", { class: "conf-input" }, input, reset),
      el("span", { class: "s" }, `default ${shown(f.default)}`, f.hint ? ` · ${f.hint}` : ""), bad);
  }

  // --- review and save (shared by the Config file and Tools tabs)

  function reviewBlock() {
    const C = conf.check;
    const V = conf.view;
    if (!V) return null;
    const edited = conf.draft !== null;
    const lowers = Boolean(C && edited && (C.lowering || []).length);
    const waiting = V.pending && V.pending.state === "waiting";
    const notes = !C || !edited ? null : [
      C.stale ? note("bad", el("b", { text: "config.toml changed since this page loaded it. " }),
        "Saving would overwrite that change, so it is refused. Reload to see the file as it is now (your edits here are dropped).",
        el("div", { class: "actions" }, actionButton("Reload from the file", () => refreshConfig(true), "small"))) : null,
      lowers ? note("warn", el("b", { text: "This loosens safety, so she asks first: " }),
        "after Save, hold Yes on her card. Neither this page nor a spoken yes can say yes to it.",
        el("ul", null, C.lowering.map((f) => el("li", null, el("code", { text: f.key }), ` ${f.why}`)))) : null,
      (C.live || []).length ? el("p", null, chip(`applies now: ${C.live.map((s) => `[${s}]`).join(" ")}`, "good")) : null,
      (C.restart || []).length ? el("p", null, chip(`needs a restart: ${C.restart.map((s) => `[${s}]`).join(" ")}`, "warn"),
        el("span", { class: "faint", text: " after saving, run strawberry restart (she never restarts herself)" })) : null];
    return saveBar({ path: V.path || "config.toml", exists: V.exists, checking: edited && !C, ok: !edited || !C || C.ok,
      same: !edited || !C || C.same, problems: C && C.problems, notes, diff: edited && C ? C.diff : [],
      canSave: Boolean(edited && C && C.ok && !C.same && !C.stale && !waiting), edited,
      saveLabel: lowers ? "Save: ask on her card" : "Save config.toml", confirmLabel: lowers ? "Ask her now?" : "Save it now?",
      onSave: (b) => confSave(b), onUndo: () => { confReset(); renderConfig(); renderTools(); } });
  }
  function renderConfReview() {
    const node = document.getElementById("conf-review");
    if (node) fill(node, reviewBlock());
  }

  async function confSave(button) {
    button.disabled = true;
    try {
      const r = await api("config/save", { text: confText(), base: conf.view.base.tag });
      if (r.pending) {
        conf.view.pending = r.pending;
        toast("She asks on her card now: hold Yes there.");
        renderConfPending();
        renderConfReview();
        renderToolsPending();
        confPoll();
        return;
      }
      if (r.same) { toast("Nothing to save: the same as the file."); return; }
      toast(`Saved.${r.backup ? " The old one is kept beside it." : ""}${(r.restart || []).length ? ` ${r.restart.map((s) => `[${s}]`).join(" ")} need(s) strawberry restart.` : ""}`);
      conf.view = r;
      confReset();
      renderConfig();
      if (state.section === "tools") refreshTools();
    } catch (e) {
      toast(e.message, true);
      button.disabled = false;
      confCheck();
    }
  }

  async function refreshConfig(force) {
    try {
      const r = await api("config");
      if (force || conf.draft === null) { conf.view = r; if (force) confReset(); }
      else conf.view.pending = r.pending;
    } catch (e) { if (!state.ended) toast(e.message, true); }
    if (conf.view && conf.view.pending && conf.view.pending.state === "waiting") confPoll();
    rerender("config");
    if (force) rerender("tools");
  }

  // --- the Tools page

    function renderTools() {
    const root = $("#tools");
    const T = conf.tools;
    if (!T || !conf.view) {
      root.replaceChildren(loadingPanel("tools"));
      return;
    }
    const rows = T.servers.map((s) => {
      const tools = el("details", null, el("summary", { text: s.tools.length ? `${s.tools.length} tool(s)` : (s.state === "not running" ? "not running yet" : "connects on first use") }),
        s.tools.length ? table(["Tool", "Tier", "Offered to the thinker", "Asks first", "Careful"], s.tools.map((t) => el("tr", null,
          td(el("span", { class: "mono", text: t.name })), td(tierChip(t.tier, t.destructive_hint ? "the server marks it destructive" : null)),
          td(t.offered ? "yes" : "no"), td(t.confirm ? "yes" : dash), td(t.careful ? "only when you ask for such a change" : dash)))) : null);
      return el("tr", null,
        td([el("b", { text: s.name }), s.builtin ? el("span", { class: "sub", text: "built in" }) : null,
          s.restart ? el("span", { class: "sub", text: s.state === "not running" ? "in the file: starts with strawberry restart" : "the file differs: strawberry restart" }) : null]),
        td(s.kind), td(s.topic || dash), td(s.adapter || dash),
        td(s.flags && s.flags.length ? s.flags.map((f) => chip(f, f === "private" ? null : "warn")) : dash),
        td(s.offer || dash),
        td([chip(s.state, s.state === "ready" ? "good" : s.error ? "warn" : null, s.error || ""), s.login ? loginBlock(s.login) : null]),
        el("td", null, tools),
        td(!s.builtin && T.file[s.name] ? actionButton("Edit", () => { conf.server = serverForm(s.name, T.file[s.name]); renderToolsForm(); document.getElementById("tools-form").scrollIntoView({ block: "start" }); }, "small") : null));
    });
    fill(root,
      titlePanel("tools", "the MCP servers she acts through, and how each tool is treated",
        el("div", { class: "actions" }, chip(T.enabled ? "tools on" : "tools off ([tools] enabled = false)", T.enabled ? "good" : "warn"),
          el("span", { class: "dim mono", text: T.path })),
        el("p", { class: "dim", text: "Tier: read and playback go ahead; change asks once foreign text is in the conversation; sends and destructive always ask. Flags: private (your own data), foreign (foreign text: text others wrote), egress (sends things off this machine). A server with no adapter and no flags is all three." })),
      panel("Servers", "built in and from config.toml; open a row's tools for each one's tier",
        table(["Server", "Kind", "Topic", "Adapter", "Flags", "Offered", "State", "Tools", ""], rows, { empty: "No tool servers." })),
      el("div", { id: "tools-pending" }),
      panel("Add or edit a server", "saved to config.toml through the same check: a new or changed command or address asks on her card",
        el("div", { class: "split" },
          el("div", { class: "split-main", id: "tools-form" }),
          el("div", { class: "split-side", id: "tools-review" }))));
    renderToolsPending();
    renderToolsForm();
    renderToolsReview();
  }
  function loginBlock(login) {
    const copy = actionButton("Copy", () => {
      (navigator.clipboard ? navigator.clipboard.writeText(login.command) : Promise.reject(new Error("no clipboard")))
        .then(() => toast("Copied. Run it in a terminal: it opens your browser to sign in."))
        .catch(() => toast(`Run in a terminal: ${login.command}`));
    }, "ghost small");
    return el("div", { class: "login" }, chip(login.saved ? "signed in" : "needs a login", login.saved ? "good" : "warn"),
      " ", el("code", { text: login.command }), " ", copy);
  }

  function renderToolsPending() {
    const node = document.getElementById("tools-pending");
    if (!node) return;
    const P = conf.view && conf.view.pending;
    fill(node, P && P.state === "waiting" ? note("warn", el("b", { text: "Waiting for your yes on her card. " }),
      "Hold Yes there; this page cannot. The Config file tab shows the details.") : null);
  }

  function serverForm(name, table) {
    const t = table || {};
    return {
      original: name || null, name: name || "", transport: t.url ? "url" : "command",
      command: t.command || "", url: t.url || "", args: (t.args || []).join("\n"), cwd: t.cwd || "",
      env: Object.entries(t.env || {}).map(([k, v]) => `${k} = ${v}`).join("\n"),
      topic: t.topic || "other", adapter: t.adapter || "", flags: new Set(t.flags || []), offer: t.offer || "",
      careful: (t.careful || []).join(", "), confirmDefault: !("confirm" in t), confirm: (t.confirm || []).join(", "),
      workspaces: (t.workspaces || []).slice(), workspace: "",
    };
  }

  function serverTable(F) {
    const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
    const out = { topic: F.topic };
    if (F.transport === "url") out.url = F.url.trim();
    else {
      out.command = F.command.trim();
      const args = F.args.split("\n").map((a) => a.trim()).filter(Boolean);
      if (args.length) out.args = args;
      const env = {};
      for (const line of F.env.split("\n")) {
        if (!line.trim()) continue;
        const at = line.indexOf("=");
        if (at < 1) throw new Error(`env: "${line.trim()}" is not NAME = value`);
        env[line.slice(0, at).trim()] = line.slice(at + 1).trim();
      }
      if (Object.keys(env).length) out.env = env;
      if (F.cwd.trim()) out.cwd = F.cwd.trim();
    }
    if (F.adapter) out.adapter = F.adapter;
    if (F.flags.size) out.flags = [...F.flags];
    if (F.offer) out.offer = F.offer;
    if (list(F.careful).length) out.careful = list(F.careful);
    if (!F.confirmDefault) out.confirm = list(F.confirm);
    if (recallish(F)) out.workspaces = F.workspaces.slice();
    return out;
  }

  function recallish(F) { return F.adapter === "recall" || (!F.adapter && F.name.trim().toLowerCase() === "recall"); }

  function renderToolsForm() {
    const node = document.getElementById("tools-form");
    if (!node || !conf.tools) return;
    const T = conf.tools;
    const F = conf.server;
    if (!F) {
      fill(node, el("div", { class: "actions" },
        actionButton("Add a server", () => { conf.server = serverForm("", null); renderToolsForm(); }),
        el("span", { class: "faint", text: "or Edit one in the list above. Built-in servers are hers and are not edited here." })));
      return;
    }
    const O = T.options;
    const text = (key, label, hint, attrs = {}) => el("label", { class: "field", for: `srv-${key}` }, el("span", { class: "k", text: label }),
      el("input", Object.assign({ type: "text", id: `srv-${key}`, value: F[key], on: { input: (ev) => { F[key] = ev.target.value; } } }, attrs)),
      hint ? el("span", { class: "s", text: hint }) : null);
    const area = (key, label, hint) => {
      const a = el("textarea", { id: `srv-${key}`, class: "small mono", rows: 3, on: { input: (ev) => { F[key] = ev.target.value; } } });
      a.value = F[key];
      return el("label", { class: "field", for: `srv-${key}` }, el("span", { class: "k", text: label }), a, el("span", { class: "s", text: hint }));
    };
    const pick = (key, label, values, hint) => el("label", { class: "field", for: `srv-${key}` }, el("span", { class: "k", text: label }),
      el("select", { id: `srv-${key}`, on: { change: (ev) => { F[key] = ev.target.value; renderToolsForm(); } } },
        values.map(([v, t]) => el("option", { value: v, selected: F[key] === v, text: t }))), hint ? el("span", { class: "s", text: hint }) : null);
    const workspaces = recallish(F) ? el("div", { class: "field" }, el("span", { class: "k", text: "Workspaces she may read" }),
      el("div", { class: "actions" }, F.workspaces.length ? F.workspaces.map((w, i) => el("span", { class: "chip" }, w,
        el("button", { type: "button", class: "ghost small", "aria-label": `Remove ${w}`, text: "✕", on: { click: () => { F.workspaces.splice(i, 1); renderToolsForm(); } } })))
        : el("span", { class: "faint", text: "none: no note is readable" })),
      el("div", { class: "actions" }, el("input", { type: "text", id: "srv-workspace", value: F.workspace, placeholder: "a workspace's name or id",
        on: { input: (ev) => { F.workspace = ev.target.value; } } }),
        actionButton("Add", () => { const w = F.workspace.trim(); if (w && !F.workspaces.includes(w)) F.workspaces.push(w); F.workspace = ""; renderToolsForm(); }, "small")),
      el("span", { class: "s", text: "By name or id, typed here: the server's list is not read from this page. Adding one asks on her card." })) : null;
    fill(node,
      el("div", { class: "cards fields" },
        text("name", "Name", "letters, digits, - and _; recall, spotify or web get that adapter", { disabled: F.original ? true : null }),
        pick("transport", "Runs as", [["command", "a command on this machine (stdio)"], ["url", "a server at an address (https)"]]),
        pick("topic", "Topic", O.topics.map((t) => [t, t]), "the gate's topic its tools belong to"),
        pick("adapter", "Adapter", [["", "by its name (or none)"], ...O.adapters.map((a) => [a, a])]),
        pick("offer", "Offered to the thinker", [["", "the adapter's (else always)"], ...O.offers.map((o) => [o, o])])),
      F.transport === "url"
        ? el("div", { class: "cards fields" }, text("url", "Address", "https, or http on this machine only; then sign in with strawberry tools login"))
        : el("div", { class: "cards fields" }, text("command", "Command", "a full path is safest"), area("args", "Arguments", "one per line"),
          area("env", "Environment", "NAME = value, one per line; hidden values stay as they are"), text("cwd", "Folder", "optional")),
      el("div", { class: "actions" }, el("span", { class: "k", text: "Flags" }),
        O.flags.map((f) => el("label", { class: "check" }, el("input", { type: "checkbox", checked: F.flags.has(f),
          on: { change: (ev) => { if (ev.target.checked) F.flags.add(f); else F.flags.delete(f); } } }), f)),
        el("span", { class: "faint", text: "none ticked and no adapter: all three. Taking one away asks on her card." })),
      el("div", { class: "cards fields" }, text("careful", "Careful tools", "offered only when you ask for such a change; comma separated"),
        el("label", { class: "field" }, el("span", { class: "k", text: "Asks first" }),
          el("label", { class: "check" }, el("input", { type: "checkbox", checked: F.confirmDefault, on: { change: (ev) => { F.confirmDefault = ev.target.checked; renderToolsForm(); } } }), "the adapter's own list"),
          F.confirmDefault ? null : el("input", { type: "text", id: "srv-confirm", value: F.confirm, placeholder: "none", on: { input: (ev) => { F.confirm = ev.target.value; } } }))),
      workspaces,
      el("div", { class: "actions" },
        actionButton("Review this server", () => toolsReview(false)),
        F.original ? confirmButton("Remove from config.toml", "Remove it?", () => toolsReview(true), "danger") : null,
        actionButton("Close", () => { conf.server = null; renderToolsForm(); }, "ghost")));
  }

  function toolsReview(remove) {
    const F = conf.server;
    const name = (F.original || F.name).trim();
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) { toast("A server's name has letters, digits, - and _ only.", true); return; }
    let value;
    try { value = serverTable(F); } catch (e) { toast(e.message, true); return; }
    confEdit(remove ? { key: `tools.servers.${name}`, reset: true } : { key: `tools.servers.${name}`, value });
    clearTimeout(conf.timer);
    confCheck().then(() => { const n = document.getElementById("tools-review"); if (n) n.scrollIntoView({ block: "start" }); });
  }

  function renderToolsReview() {
    const node = document.getElementById("tools-review");
    if (node) fill(node, reviewBlock());
  }

  async function refreshTools() {
    try {
      const [tools, view] = await Promise.all([api("tools"), conf.view ? Promise.resolve(null) : api("config")]);
      conf.tools = tools;
      if (view) conf.view = view;
    } catch (e) { if (!state.ended) toast(e.message, true); }
    if (conf.server && document.getElementById("tools-form")) { renderToolsReview(); return; }
    rerender("tools");
  }

  // --------------------------------------------------------------------------- settings: input

  // Hand gestures from the camera and touch from the bodies. The live view is drawn from the hand's few
  // numbers the watcher posts (the wrist, the fingertips, pinch, open, engaged), never from a picture: no
  // frame ever leaves the watcher. The settings are saved to input.toml, never config.toml.

  const GESTURE_TUNING = [
    ["hold_ms", "Hold time", "ms a shape is held to count", 100, 3000, 50],
    ["zone", "Zone line", "a hand counts with its wrist above it (0 top, 1 bottom)", 0.1, 1, 0.01],
    ["min_size", "Smallest hand", "of the frame: a nearer hand is bigger", 0, 0.99, 0.01],
    ["armed_s", "Command mode lasts", "s, with arming on or watch = armed", 1, 120, 1],
    ["idle_fps", "Frames a second, looking", "while no hand is up", 0.5, 60, 0.5],
    ["fps", "Frames a second, hand up", "swipes need 10 or more", 1, 60, 1],
    ["hand_hz", "Hand updates a second", "to the bodies and this view", 1, 30, 1],
    ["face_fps", "Face reads a second", "only while a hand is up or her card is open", 1, 15, 1],
    ["shh_minutes", "Quiet after shh", "minutes her voice is off (the bubble stays); \"you can talk again\" ends it sooner", 1, 720, 1],
  ];
  const TOUCH_TUNING = [
    ["target_s", "A target holds", "s after the body last reported it", 0.5, 60, 0.5],
    ["cooldown_s", "Between touch actions", "s at least", 0, 30, 0.1],
  ];
  const WHY = {
    not_mapped: "not mapped", cooldown: "too soon after the last", not_armed: "command mode was off",
    too_soon: "too soon after the last", not_answerable: "a thumb cannot answer this question",
    nothing_can_do_it: "no player or music server for it", tool_failed: "the call failed", refused: "refused",
    would_ask: "it would need a yes", not_listening: "she could not listen", error: "an error",
    not_open: "no question open", resolved: "already answered", no_question: "no question open",
    busy: "she was busy", no_body: "no body connected", no_volume: "the player does not say its volume",
  };
  let inputCheckTimer = null;
  let inputLive = null;          // the live view's fetch (an AbortController) while the tab is open
  let drawPending = false;

  function inputForm(view) {
    const s = view.settings;
    const g = Object.assign({}, s.gestures);
    const gmap = Object.entries(g.map || {}).map(([key, action]) => {
      const at = key.lastIndexOf(":");
      return { target: at >= 0 ? key.slice(0, at) : "", gesture: at >= 0 ? key.slice(at + 1) : key, action };
    });
    delete g.map;
    const tmap = [];
    for (const [entity, kinds] of Object.entries(s.touch.actions || {})) {
      for (const [kind, action] of Object.entries(kinds)) tmap.push({ entity, kind, action });
    }
    return { g, t: { target_s: s.touch.target_s, cooldown_s: s.touch.cooldown_s }, gmap, tmap };
  }

  function inputSettings(form) {
    const map = {};
    for (const r of form.gmap) map[r.target ? `${r.target}:${r.gesture}` : r.gesture] = r.action;
    const actions = {};
    for (const r of form.tmap) (actions[r.entity] = actions[r.entity] || {})[r.kind] = r.action;
    return { gestures: Object.assign({}, form.g, { map }), touch: Object.assign({}, form.t, { actions }) };
  }

  function inputDuplicates(form) {
    const seen = new Set();
    const twice = [];
    for (const r of form.gmap) {
      const key = r.target ? `${r.target}:${r.gesture}` : r.gesture;
      if (seen.has(key)) twice.push(key); else seen.add(key);
    }
    for (const r of form.tmap) {
      const key = `${r.entity}.${r.kind}`;
      if (seen.has(key)) twice.push(key); else seen.add(key);
    }
    return twice;
  }

  function edited() {
    state.inputEdited = true;
    scheduleInputCheck();
  }

  function scheduleInputCheck(ms = 400) {
    clearTimeout(inputCheckTimer);
    inputCheckTimer = setTimeout(checkInput, ms);
  }

  async function checkInput() {
    if (!state.inputForm) return;
    try { state.inputCheck = await api("input/check", { settings: inputSettings(state.inputForm) }); }
    catch (e) { state.inputCheck = { ok: false, problems: [e.message], diff: [] }; }
    renderInputCheck();
  }

  function renderInput() {
    const root = $("#input");
    const V = state.input;
    if (!V) {
      root.replaceChildren(loadingPanel("input"));
      return;
    }
    if (!state.inputEdited || !state.inputForm) state.inputForm = inputForm(V);
    const practice = el("input", { type: "checkbox", id: "input-practice", checked: Boolean(state.inputPractice),
      on: { change: (ev) => setPractice(ev.target) } });
    const canvas = el("canvas", { id: "hand-canvas", class: "hand", width: 640, height: 480,
      "aria-label": "The hand as the watcher reports it: wrist, fingertips, the zone and the hold ring" });
    fill(root,
      titlePanel("input", "hand gestures through the camera, and touch from the bodies",
        cameraSwitch(),
        el("div", { id: "input-head", class: "stack" }),
        el("div", { class: "actions" },
          el("label", { class: "check", for: "input-practice" }, practice,
            el("b", { text: "Practice mode" }),
            el("span", { class: "dim", text: "Gestures and touches are recognised and shown here, and do nothing. Not saved; off again when she restarts." })))),
      panel("Live", "drawn from the hand's numbers; the camera's picture never leaves the watcher",
        el("div", { class: "split" },
          el("div", { class: "split-main" }, el("div", { class: "hand-wrap" }, canvas), el("div", { id: "hand-legend", class: "hand-legend" })),
          el("div", { class: "split-side" },
            el("h3", { text: "Status" }), el("div", { id: "input-status", class: "stack" }),
            el("h3", { text: "Bodies" }), el("p", { class: "faint", text: "what they draw, and what the user touches" }),
            el("div", { id: "input-bodies", class: "stack" })))),
      panel("Settings", "saved to input.toml, never config.toml; she reads it again within a second",
        el("div", { class: "split" },
          el("div", { class: "split-main" },
            el("h3", { text: "Mappings" }),
            el("p", { class: "faint", text: "what a gesture or a touch does: only actions that need no yes (read and playback)" }),
            el("div", { id: "input-maps", class: "stack" }),
            el("h3", { text: "Tuning" }),
            el("p", { class: "faint", text: "how a hand has to be held, and how the camera runs" }),
            el("div", { id: "input-tuning" }),
            el("h3", { text: "Calibrate" }),
            el("p", { class: "faint", text: "your hand's own numbers: each finger's curl and the thumb-index gap, never a picture" }),
            el("div", { id: "input-calib", class: "stack" })),
          el("div", { class: "split-side", id: "input-file" }))),
      panel("Recent", "the last gestures and touch actions, and what came of each", el("div", { id: "input-recent" })));
    renderInputCamera();
    renderInputHead();
    renderInputStatus();
    renderInputBodies();
    renderInputMaps();
    renderInputTuning();
    renderCalibration();
    renderInputCheck();
    renderInputRecent();
    drawHand();
    if (!state.inputCheck) scheduleInputCheck(0);
  }

  function renderInputHead() {
    const node = document.getElementById("input-head");
    if (!node || !state.input) return;
    const V = state.input;
    const S = Object.assign({}, V.status, state.inputStatus || {});
    const f = V.file || {};
    const notes = [];
    if (f.error) {
      notes.push(note("bad", el("b", { text: "Your input.toml is not used. " }),
        "She keeps the settings she has until it checks out:", list([f.error])));
    }
    fill(node, el("div", { class: "actions" },
      chip(f.in_use ? (f.error ? "settings: the last input.toml that checked out" : "settings from input.toml")
        : f.exists ? "settings from config.toml" : "settings from config.toml (no input.toml yet)",
        f.in_use && !f.error ? "good" : f.error ? "warn" : null, f.path || ""),
      S.practice ? chip("practice mode: nothing is done", "warn") : null,
      el("span", { class: "faint", text: "The camera is the watcher's alone. The switch above, the tray's Hand gestures (camera) row and strawberry gestures on|off set the same thing." })),
      notes);
  }

  function renderInputStatus() {
    const node = document.getElementById("input-status");
    if (!node || !state.input) return;
    const S = Object.assign({}, state.input.status, state.inputStatus || {});
    const w = S.watcher || {};
    const watcher = !S.enabled ? "waits (gestures are off: it only reads the config file)"
      : w.heard ? `running, heard ${num(w.age_s, 1)} s ago` : "not heard from (is the tray running it?)";
    const camera = (S.camera || {}).state || (!S.enabled ? "closed" : w.heard ? (w.camera ? "open" : "closed") : "unknown");
    const armed = S.armed_s > 0 ? `armed, ${Math.ceil(S.armed_s)} s left`
      : S.arming || S.watch === "armed" ? "idle (not armed)" : "not needed (arming is off)";
    const g = S.gestures || {};
    const t = S.touch || {};
    fill(node,
      el("div", { class: "actions" },
        chip(`camera ${camera}`, camera === "open" ? "warn" : null, "The camera's light is on while it is open"),
        chip(`watch: ${S.watch}`), chip(`command mode ${armed.split(",")[0]}`, S.armed_s > 0 ? "good" : null)),
      kv([
        ["Watcher", watcher],
        ["Watch mode", S.watch === "armed" ? "armed: the camera opens only when armed (strawberry gestures arm)" : "always: open while gestures are on"],
        ["Command mode", armed],
        ["Frames a second", w.heard ? num(w.fps, 1) : dash],
        ["CPU (the watcher)", w.heard ? `${num(w.cpu, 1)}% of one core` : dash],
        ["Last hand", typeof S.hand_age_s === "number" ? `${duration(S.hand_age_s)} ago` : "none since she started"],
        ["MediaPipe", S.mediapipe ? `missing: ${S.mediapipe}` : "installed"],
        ["Model", S.model ? "present" : "missing: strawberry gestures fetch"],
        ["Face model", !S.face_model ? "missing: strawberry gestures fetch (until then facing is not checked, and shh, nod and shake are off)"
          : w.face === "failed" ? "did not load: facing is not checked" : w.face === "on" ? "on: gestures count while you face the screen" : "present"],
        ["Quiet (shh)", S.quiet_s > 0 ? `her voice is off, ${Math.ceil(S.quiet_s / 60)} min left; say "you can talk again" to end it` : "off"],
        ["Gestures", `${g.received ?? 0} received, ${g.acted ?? 0} acted, ${g.answered ?? 0} answered, ${g.refused ?? 0} refused`],
        ["Touch actions", `${t.acted ?? 0} done, ${t.failed ?? 0} failed, ${t.refused ?? 0} refused, ${t.cooldown ?? 0} too soon`]]));
  }

  function renderInputBodies() {
    const node = document.getElementById("input-bodies");
    if (!node || !state.input) return;
    const bodies = state.input.bodies || [];
    const targets = (state.inputStatus || state.input.status || {}).targets || {};
    const target = targets.target;
    fill(node,
      target ? el("p", null, "Pointing at ", el("b", { text: target.entity }), ` (${target.via}, on ${target.body})`) : null,
      bodies.length ? bodies.map((b) => el("div", { class: "body-card" },
        el("div", { class: "actions" }, el("b", { class: "mono", text: b.id || "a body" }),
          chip(`${(b.inputs || {}).touch ?? 0} touches`), chip(`${(b.inputs || {}).target ?? 0} targets`),
          (b.inputs || {}).refused ? chip(`${b.inputs.refused} refused`, "warn") : null,
          (b.inputs || {}).dropped ? chip(`${b.inputs.dropped} dropped`, "warn") : null),
        el("div", { class: "entities" }, (b.entities || []).map((e) => el("span", { class: "tag", title: e.label },
          el("span", { class: "mono", text: e.id }), e.kind !== e.id ? ` ${e.kind}` : null)))))
        : el("p", { class: "empty", text: "No body has declared anything to touch. The orbs and the crab list their entities when they connect." }));
  }

  function entityList() {
    const ids = new Map();
    for (const e of (state.input && state.input.options.entities) || []) ids.set(e.id, `${e.kind} on ${e.body}`);
    if (state.inputForm) {
      for (const r of state.inputForm.tmap) if (r.entity && !ids.has(r.entity)) ids.set(r.entity, "not connected now");
      for (const r of state.inputForm.gmap) if (r.target && !ids.has(r.target)) ids.set(r.target, "not connected now");
    }
    return el("datalist", { id: "input-entities" }, [...ids].map(([id, what]) => el("option", { value: id, label: what })));
  }

  function selectOf(values, value, labels, onPick, aria) {
    return el("select", { "aria-label": aria, on: { change: (ev) => onPick(ev.target.value) } },
      values.map((v) => el("option", { value: v, selected: v === value, text: labels ? labels(v) : v })));
  }

  function renderInputMaps() {
    const node = document.getElementById("input-maps");
    if (!node || !state.input || !state.inputForm) return;
    const O = state.input.options;
    const F = state.inputForm;
    const twice = new Set(inputDuplicates(F));
    const gActions = Object.keys(O.gesture_actions);
    const tActions = Object.keys(O.touch_actions);
    const tier = (map) => (a) => `${a} (${map[a]})`;
    const gRows = F.gmap.map((r, i) => el("tr", { class: twice.has(r.target ? `${r.target}:${r.gesture}` : r.gesture) ? "dup" : null },
      el("td", null, el("input", { type: "text", list: "input-entities", value: r.target, placeholder: "anything",
        "aria-label": "When pointing at", class: "short",
        on: { input: (ev) => { r.target = ev.target.value.trim(); edited(); markDuplicates(); } } })),
      td(selectOf(O.gestures, r.gesture, (v) => v.replace(/_/g, " "), (v) => { r.gesture = v; edited(); markDuplicates(); }, "Gesture")),
      td(selectOf(gActions, r.action, tier(O.gesture_actions), (v) => { r.action = v; edited(); }, "Action")),
      td(removeButton(() => { F.gmap.splice(i, 1); edited(); renderInputMaps(); }))));
    const tRows = F.tmap.map((r, i) => el("tr", { class: twice.has(`${r.entity}.${r.kind}`) ? "dup" : null },
      el("td", null, el("input", { type: "text", list: "input-entities", value: r.entity, placeholder: "music",
        "aria-label": "Entity", class: "short",
        on: { input: (ev) => { r.entity = ev.target.value.trim(); edited(); markDuplicates(); } } })),
      td(selectOf(O.touch_kinds, r.kind, null, (v) => { r.kind = v; edited(); markDuplicates(); }, "Touch")),
      td(selectOf(tActions, r.action, tier(O.touch_actions), (v) => { r.action = v; edited(); }, "Action")),
      td(removeButton(() => { F.tmap.splice(i, 1); edited(); renderInputMaps(); }))));
    const firstEntity = ((O.entities || [])[0] || {}).id || "";
    fill(node, entityList(),
      el("div", { class: "stack" },
        el("div", null,
          el("h4", { text: "Gestures" }),
          table(["When pointing at", "Gesture", "Does", ""], gRows, { empty: "No gesture does anything." }),
          el("div", { class: "actions" },
            actionButton("Add a gesture", () => { F.gmap.push({ target: "", gesture: O.gestures[0], action: gActions[0] }); edited(); renderInputMaps(); }, "small"),
            el("span", { class: "faint", text: "“When pointing at” names an entity: that row wins while the user points at it." }))),
        el("div", null,
          el("h4", { text: "Touch" }),
          table(["Entity", "Touch", "Does", ""], tRows, { empty: "No touch does anything: touches are only shown to the other bodies." }),
          el("div", { class: "actions" },
            actionButton("Add a touch", () => { F.tmap.push({ entity: firstEntity, kind: O.touch_kinds[0], action: tActions[0] }); edited(); renderInputMaps(); }, "small"),
            el("span", { class: "faint", text: "Entities are the ids the bodies declare." })))),
      el("p", { class: "note warn hidden", id: "input-dups", text: "Two rows map the same thing; only the last would count. Change or remove one." }));
    markDuplicates();
  }

  function removeButton(action) {
    const button = actionButton("✕", action, "small ghost");
    button.setAttribute("aria-label", "Remove this row");
    button.title = "Remove this row";
    return button;
  }

  function markDuplicates() {
    const node = document.getElementById("input-dups");
    if (!node || !state.inputForm) return;
    const twice = inputDuplicates(state.inputForm);
    node.classList.toggle("hidden", !twice.length);
    renderInputSave();
  }

  function numberField(obj, [key, label, hint, min, max, step]) {
    const id = `tune-${key}`;
    return el("label", { class: "field", for: id },
      el("span", { class: "k", text: label }),
      el("input", { type: "number", id, min, max, step, value: obj[key],
        on: { input: (ev) => { const v = Number(ev.target.value); if (ev.target.value !== "" && Number.isFinite(v)) { obj[key] = v; edited(); } } } }),
      el("span", { class: "s", text: hint }));
  }

  function renderInputTuning() {
    const node = document.getElementById("input-tuning");
    if (!node || !state.inputForm) return;
    const G = state.inputForm.g;
    const T = state.inputForm.t;
    const box = (key, label, hint) => el("label", { class: "field check", for: `tune-${key}` },
      el("input", { type: "checkbox", id: `tune-${key}`, checked: Boolean(G[key]), on: { change: (ev) => { G[key] = ev.target.checked; edited(); drawHand(); } } }),
      el("span", null, el("b", { text: label }), el("span", { class: "s", text: ` ${hint}` })));
    node.className = "tuning";
    fill(node,
      el("h4", { text: "Gestures" }),
      el("div", { class: "cards fields" },
        GESTURE_TUNING.map((f) => numberField(G, f)),
        el("label", { class: "field", for: "tune-watch" }, el("span", { class: "k", text: "Watch" }),
          (() => { const s = selectOf(state.input.options.watch, G.watch, null, (v) => { G.watch = v; edited(); }, "Watch"); s.id = "tune-watch"; return s; })(),
          el("span", { class: "s", text: "always: open while on; armed: only after strawberry gestures arm" })),
        el("label", { class: "field", for: "tune-camera" }, el("span", { class: "k", text: "Camera" }),
          el("input", { type: "text", id: "tune-camera", value: G.camera, placeholder: "the first",
            on: { input: (ev) => { G.camera = ev.target.value.trim(); edited(); } } }),
          el("span", { class: "s", text: "an index (1) or a device (/dev/video2); empty: the first" })),
        el("label", { class: "field", for: "tune-hands" }, el("span", { class: "k", text: "Hands" }),
          (() => {
            const s = selectOf(["1", "2"], String(G.hands || 2), (v) => (v === "1" ? "one" : "two"), (v) => { G.hands = Number(v); edited(); }, "Hands");
            s.id = "tune-hands";
            return s;
          })(),
          el("span", { class: "s", text: "two: both, while a body or this view asks for the hand (the primary one makes the gestures; twice the CPU then)" }))),
      el("div", { class: "actions" },
        box("arming", "Arming", "a held open palm turns command mode on first"),
        box("approvals", "A thumb or the head answers her card", "a held thumbs up or down, a nod or a shake, for a tier her card takes with a tap"),
        box("require_facing", "Only while I face the screen", "needs the face model; without it gestures count as before")),
      el("h4", { text: "Touch" }),
      el("div", { class: "cards fields" }, TOUCH_TUNING.map((f) => numberField(T, f))));
    for (const input of node.querySelectorAll('input[type="number"]')) input.addEventListener("input", () => drawHand());
  }

  // --- calibration: the user holds a relaxed hand, then each shape, for a few seconds; the page keeps the mean
  // of each finger's curl and the thumb-index gap from the hand messages (numbers only) and turns them into
  // [gestures.calibration], saved with the rest of input.toml. Practice mode is on while it records.

  const CALIB_STEPS = [
    ["neutral", "A relaxed hand", "raised toward the screen, fingers loose, not pinching"],
    ["open", "An open hand", "fingers straight and spread, palm to the screen"],
    ["fist", "A fist", "fingers curled in, the thumb over them"],
    ["point", "Pointing up", "the index finger straight up, the others curled"],
    ["pinch", "A pinch", "the thumb's and index finger's tips together"],
  ];
  const CALIB_SECONDS = 3;
  const CALIB_SETTLE = 1;        // the first second of each step is not kept: the hand is still moving into it

  function calibDigits() { return (state.input && state.input.options.digits) || ["thumb", "index", "middle", "ring", "pinky"]; }

  function mean(values) { return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null; }

  function calibResult(samples) {
    // Each step's samples are {curl: {digit: n}, gap: n}: the means, put together as the tracker wants them.
    const avg = (step, pick) => mean((samples[step] || []).map(pick).filter((v) => typeof v === "number"));
    const out = {};
    for (const d of calibDigits()) {
      out[`${d}_open`] = avg("open", (s) => s.curl[d]);
      out[`${d}_curled`] = avg("fist", (s) => s.curl[d]);
    }
    const pointIndex = avg("point", (s) => s.curl.index);
    if (pointIndex !== null && out.index_open !== null) out.index_open = (out.index_open + pointIndex) / 2;
    for (const d of ["middle", "ring", "pinky"]) {
      const v = avg("point", (s) => s.curl[d]);
      if (v !== null && out[`${d}_curled`] !== null) out[`${d}_curled`] = (out[`${d}_curled`] + v) / 2;
    }
    out.gap_apart = avg("neutral", (s) => s.gap);
    out.gap_closed = avg("pinch", (s) => s.gap);
    const problems = [];
    for (const [k, v] of Object.entries(out)) if (v === null) problems.push(`${k}: no hand seen in that step`);
    for (const d of calibDigits()) {
      if (out[`${d}_open`] !== null && out[`${d}_curled`] !== null && out[`${d}_curled`] - out[`${d}_open`] < 0.15) {
        problems.push(`the ${d} hardly changed between the open hand and the fist`);
      }
    }
    if (out.gap_apart !== null && out.gap_closed !== null && out.gap_apart - out.gap_closed < 0.1) {
      problems.push("the pinch and the relaxed hand looked alike");
    }
    for (const k of Object.keys(out)) if (out[k] !== null) out[k] = Math.round(Math.min(1, Math.max(0, out[k])) * 1000) / 1000;
    return { values: out, problems };
  }

  async function startCalibration() {
    if (state.calib && state.calib.running) return;
    const wasPractice = Boolean(state.inputPractice);
    state.calib = { running: true, step: 0, startedAt: performance.now(), samples: {}, result: null, wasPractice };
    try { await api("input/practice", { on: true }); state.inputPractice = true; } catch (e) { /* recording still works */ }
    renderCalibration();
    const tick = () => {
      const C = state.calib;
      if (!C || !C.running) return;
      const elapsed = (performance.now() - C.startedAt) / 1000;
      if (elapsed >= CALIB_SECONDS) {
        C.step += 1;
        C.startedAt = performance.now();
        if (C.step >= CALIB_STEPS.length) { finishCalibration(); return; }
      }
      renderCalibration();
      setTimeout(tick, 250);
    };
    setTimeout(tick, 250);
  }

  async function finishCalibration(cancelled) {
    const C = state.calib;
    if (!C) return;
    C.running = false;
    if (!cancelled) C.result = calibResult(C.samples);
    if (!C.wasPractice) {
      try { await api("input/practice", { on: false }); state.inputPractice = false; } catch (e) { /* the status says */ }
    }
    if (cancelled) state.calib = null;
    renderCalibration();
  }

  function calibSample(hand) {
    // A hand message during a step: kept once the step has settled, and only a raised hand with its numbers.
    const C = state.calib;
    if (!C || !C.running || !hand || !hand.present || !hand.curl || typeof hand.gap !== "number") return;
    if ((performance.now() - C.startedAt) / 1000 < CALIB_SETTLE) return;
    const step = CALIB_STEPS[C.step][0];
    (C.samples[step] = C.samples[step] || []).push({ curl: Object.assign({}, hand.curl), gap: hand.gap });
  }

  function renderCalibration() {
    const node = document.getElementById("input-calib");
    if (!node || !state.input || !state.inputForm) return;
    const C = state.calib;
    const saved = state.inputForm.g.calibration || {};
    const using = Object.keys(saved).length ? "your calibration" : "the defaults";
    const canvas = el("canvas", { id: "calib-canvas", class: "calib", width: 480, height: 150,
      "aria-label": "Each finger's curl and the thumb-index gap now, with where they count as extended, curled or pinched" });
    let head;
    if (C && C.running) {
      const [, title, how] = CALIB_STEPS[C.step];
      const left = Math.max(0, CALIB_SECONDS - (performance.now() - C.startedAt) / 1000);
      const seen = (C.samples[CALIB_STEPS[C.step][0]] || []).length;
      head = note("info",
        el("b", { text: `Step ${C.step + 1} of ${CALIB_STEPS.length}: ${title}. ` }), `${how}. `,
        el("span", { class: "mono", text: `${left.toFixed(1)} s` }), seen ? ` (${seen} looks)` : "",
        el("div", { class: "actions" }, actionButton("Stop", () => finishCalibration(true), "small ghost")));
    } else if (C && C.result) {
      const R = C.result;
      head = el("div", null,
        R.problems.length
          ? note("bad", el("b", { text: "Not usable as it is:" }), list(R.problems))
          : el("p", null, chip("recorded", "good"), " Use it, then save input.toml."),
        table(["Key", "Recorded", "Now"], Object.entries(R.values).map(([k, v]) => el("tr", null,
          td(k, "mono"), td(num(v, 3)), td(num(saved[k] ?? state.input.options.calibration[k], 3))))),
        el("div", { class: "actions" },
          actionButton("Use these", () => {
            state.inputForm.g.calibration = Object.fromEntries(Object.entries(R.values).filter(([, v]) => v !== null));
            state.calib = null; edited(); renderCalibration();
          }, "small"),
          actionButton("Again", () => startCalibration(), "small ghost"),
          confirmButton("Discard", "Discard it?", () => { state.calib = null; renderCalibration(); }, "small ghost")));
    } else {
      head = el("div", null,
        el("p", null, `In use: ${using}. Hold a relaxed hand, then an open hand, a fist, pointing up and a pinch, about ${CALIB_SECONDS} s each, raised toward the camera. Practice mode is on while it records, so nothing is done.`),
        el("div", { class: "actions" },
          actionButton("Calibrate", () => startCalibration(), "small"),
          Object.keys(saved).length ? actionButton("Reset to defaults", () => { state.inputForm.g.calibration = {}; edited(); renderCalibration(); }, "small ghost") : null,
          el("span", { class: "faint", text: "Saved with input.toml as [gestures.calibration]." })));
    }
    fill(node, head, el("div", { class: "hand-wrap" }, canvas));
    drawCalibration();
  }

  function thresholds() {
    // The tracker's thresholds (gesture_track.Thresholds) from the calibration in the form, for the bars' marks.
    const cal = Object.assign({}, state.input.options.calibration, state.inputForm.g.calibration || {});
    const out = {};
    for (const d of calibDigits()) {
      const low = cal[`${d}_open`];
      const span = Math.max(cal[`${d}_curled`] - low, 0.15);
      out[d] = { ext: low + 0.30 * span, curl: low + 0.65 * span };
    }
    const span = Math.max(cal.gap_apart - cal.gap_closed, 0.1);
    out.gap = { pinch: cal.gap_closed + 0.30 * span };
    return out;
  }

  function drawCalibration() {
    const canvas = document.getElementById("calib-canvas");
    if (!canvas || !state.input || !state.inputForm) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(240, Math.round(canvas.clientWidth || 480));
    const height = 150;
    if (canvas.width !== Math.round(width * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "12px system-ui, sans-serif";
    const H = state.hand;
    const T = thresholds();
    const rows = [...calibDigits().map((d) => [d, H && H.curl ? H.curl[d] : null, T[d]]), ["gap", H ? H.gap : null, T.gap]];
    const left = 64;
    const barW = width - left - 12;
    rows.forEach(([name, value, mark], i) => {
      const y = 10 + i * 22;
      ctx.fillStyle = cssVar("--ink-dim");
      ctx.fillText(name === "gap" ? "gap" : name, 6, y + 11);
      ctx.fillStyle = cssVar("--line");
      ctx.fillRect(left, y, barW, 14);
      if (typeof value === "number") {
        ctx.fillStyle = name === "gap" ? (value <= mark.pinch ? cssVar("--good") : cssVar("--berry"))
          : value <= mark.ext ? cssVar("--good") : value >= mark.curl ? cssVar("--berry") : cssVar("--warn");
        ctx.fillRect(left, y, barW * Math.min(1, Math.max(0, value)), 14);
      }
      ctx.strokeStyle = cssVar("--ink");
      ctx.lineWidth = 1.5;
      for (const at of name === "gap" ? [mark.pinch] : [mark.ext, mark.curl]) {
        ctx.beginPath(); ctx.moveTo(left + barW * at, y - 2); ctx.lineTo(left + barW * at, y + 16); ctx.stroke();
      }
    });
    ctx.fillStyle = cssVar("--ink-faint");
    ctx.fillText(H ? "curl: green extended, berry curled, amber neither; gap: green pinched" : "No hand: raise one toward the camera", left, height - 6);
  }

  function renderInputCheck() {
    const node = document.getElementById("input-file");
    if (!node || !state.input) return;
    const C = state.inputCheck;
    const f = state.input.file || {};
    const diff = (C && C.diff) || [];
    fill(node, saveBar({ path: f.path || "input.toml", exists: f.exists, checking: !C, ok: C && C.ok, problems: C && C.problems,
      same: !diff.length, diff, edited: state.inputEdited, saveLabel: "Save input.toml", saveId: "input-save",
      notes: f.in_use ? null : el("p", { class: "faint", text: "No input.toml yet: Save writes these settings to it." }),
      onSave: (b) => saveInput(b),
      onUndo: () => { state.inputEdited = false; state.inputForm = null; state.inputCheck = null; renderInput(); } }));
    renderInputSave();
  }

  // Save is lit only when the draft checks out, changes the file and maps nothing twice.
  function renderInputSave() {
    const save = document.getElementById("input-save");
    if (!save) return;
    const C = state.inputCheck;
    const twice = state.inputForm ? inputDuplicates(state.inputForm) : [];
    save.disabled = !(C && C.ok && !twice.length && (C.diff || []).length);
  }
  async function saveInput(button) {
    button.disabled = true;
    try {
      const r = await api("input/save", { settings: inputSettings(state.inputForm) });
      if (r.pending) {
        // A thumb that answers her card, or a camera always watching: she asks on her card first (WIRING.md §28).
        toast("That loosens safety, so she asks on her card: hold Yes there. The Config file tab shows how it went.");
        if (conf.view) conf.view.pending = r.pending;
        confPoll();
        button.disabled = false;
        return;
      }
      toast(r.backup ? "Saved. The old one is kept as input.toml.bak." : "Saved. She uses it at once.");
      state.input = r;
      state.inputEdited = false;
      state.inputForm = null;
      state.inputCheck = null;
      renderInput();
    } catch (e) {
      toast(e.message, true);
      button.disabled = false;
    }
  }

  function setPractice(box) {
    box.disabled = true;
    api("input/practice", { on: box.checked })
      .then((r) => {
        state.inputPractice = r.practice;
        if (state.inputStatus) state.inputStatus.practice = r.practice;
        toast(r.practice ? "Practice mode: gestures and touches are shown and do nothing." : "Practice mode off.");
        renderInputHead();
      })
      .catch((e) => { box.checked = !box.checked; toast(e.message, true); })
      .finally(() => { box.disabled = false; });
  }

  // The camera switch: `[gestures] enabled` in config.toml, written as the tray's Hand gestures (camera) row writes
  // it (POST input/camera). Only this, that row and `strawberry gestures on|off` turn the camera on; saving the
  // settings never does. Beside it, what the watcher last said of the camera, which is not the same as the switch.

  const CAMERA_STATE = {
    open: ["camera open", "warn", "The watcher has the camera; its light is on."],
    closed: ["camera closed", null, "The camera is not in use."],
    closing: ["camera closing", "warn", "Switched off: the watcher lets go of the camera within a second."],
    unknown: ["camera: no word from the watcher", "warn",
      "Switched on, but the watcher has not reported for five seconds (is the tray running it?)."],
  };
  let cameraSeen = "";

  function cameraSwitch() {
    const box = el("input", { type: "checkbox", id: "input-camera", role: "switch",
      on: { change: (ev) => setCamera(ev.target) } });
    return el("div", { class: "actions" },
      el("label", { class: "check", for: "input-camera" }, box, el("b", { text: "Camera (hand gestures)" })),
      el("span", { id: "input-camera-state", class: "actions" }));
  }

  function renderInputCamera() {
    const box = document.getElementById("input-camera");
    const node = document.getElementById("input-camera-state");
    if (!box || !node || !state.input) return;
    const S = Object.assign({}, state.input.status, state.inputStatus || {});
    const cam = S.camera || { enabled: S.enabled, state: "unknown" };
    if (!box.disabled) box.checked = Boolean(cam.enabled);
    const seen = `${cam.enabled} ${cam.state}`;
    if (seen !== cameraSeen) { cameraSeen = seen; requestDraw(); }    // the live view's "no hand" line says why
    // On, with MediaPipe or the model missing, the watcher waits for them and posts nothing: closed, and why.
    const missing = cam.enabled && cam.state === "unknown" && (S.mediapipe || S.model === false);
    const [text, kind, why] = missing ? ["camera closed: MediaPipe or the model is missing", "warn", "See Status below."]
      : CAMERA_STATE[cam.state] || CAMERA_STATE.unknown;
    fill(node, chip(text, kind, why), el("span", { class: "dim", text: cam.enabled
      ? "On: the watcher may open the camera (Watch mode says when). This switch, the tray's row or strawberry gestures off turn it off."
      : "Off: the camera stays closed. Saving the settings below never turns it on." }));
  }

  function setCamera(box) {
    box.disabled = true;
    api("input/camera", { on: box.checked })
      .then((r) => {
        if (state.input) state.input.status = Object.assign({}, state.input.status, r.status);
        if (state.inputStatus) state.inputStatus = Object.assign({}, state.inputStatus, r.status);
        toast(r.enabled ? "Camera on: the watcher opens it within a second." : "Camera off: the watcher lets go of it within a second.");
      })
      .catch((e) => { box.checked = !box.checked; toast(e.message, true); })
      .finally(() => { box.disabled = false; renderInputCamera(); renderInputHead(); renderInputStatus(); });
  }

  const OUTCOME_KIND = { did: "good", practice: "warn", ignored: null, failed: "warn", running: null };

  function renderInputRecent() {
    const node = document.getElementById("input-recent");
    if (!node || !state.input) return;
    const rows = (state.input.recent || []).map((r) => el("tr", { class: state.freshInput.has(r.id) ? "fresh" : null },
      timeCell(r.at, true), td(r.source),
      td([el("span", { class: "mono", text: r.name.replace(/_/g, " ") }), r.count > 1 ? el("span", { class: "sub", text: `×${r.count}` }) : null]),
      td(chip(r.outcome === "practice" ? "practice: not done" : r.outcome, OUTCOME_KIND[r.outcome])),
      td(r.action ? (r.outcome === "practice" ? `would: ${r.action}` : r.action) : dash),
      td(r.why ? WHY[r.why] || r.why.replace(/_/g, " ") : dash)));
    state.freshInput.clear();
    fill(node, table(["When", "From", "What", "Outcome", "Action", "Why"], rows,
      { empty: "Nothing yet since she started. Hold up a hand, or touch an orb." }));
  }

  // --- the live view

  function startInputLive() {
    if (inputLive || state.ended || document.hidden) return;
    const controller = new AbortController();
    inputLive = controller;
    (async () => {
      while (inputLive === controller && !state.ended) {
        try {
          const res = await fetch("/ui/api/input/live", { headers: { "X-Strawberry-CSRF": CSRF }, cache: "no-store",
            credentials: "same-origin", signal: controller.signal });
          if (res.status === 401) { sessionEnded(); return; }
          if (!res.ok || !res.body) throw new Error(`live ${res.status}`);
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let at;
            while ((at = buffer.indexOf("\n\n")) >= 0) {
              parseWith(buffer.slice(0, at), onInputEvent);
              buffer = buffer.slice(at + 2);
            }
          }
        } catch (e) { /* aborted, or reconnect below */ }
        if (inputLive !== controller) return;
        await sleep(3000);
      }
    })();
  }

  function stopInputLive() {
    if (!inputLive) return;
    inputLive.abort();
    inputLive = null;
  }

  function onInputEvent(name, data) {
    const now = performance.now();
    if (name === "hand") {
      state.hand = data.present ? data : null;
      state.handAt = now;
      calibSample(state.hand);
      requestDraw();
    } else if (name === "gesture") {
      state.gesture = Object.assign({ at: now }, data);
      requestDraw();
    } else if (name === "status") {
      state.inputStatus = data;
      state.inputPractice = data.practice;
      const box = document.getElementById("input-practice");
      if (box && !box.disabled) box.checked = Boolean(data.practice);
      renderInputStatus();
      renderInputHead();
      renderInputCamera();
      if (state.hand && now - state.handAt > 2000) { state.hand = null; requestDraw(); }
    } else if (name === "recent" && state.input) {
      const list = (state.input.recent || []).filter((r) => r.id !== data.id);
      list.unshift(data);
      list.sort((a, b) => b.id - a.id);
      state.input.recent = list.slice(0, 20);
      state.freshInput.add(data.id);
      renderInputRecent();
    }
  }

  function requestDraw() {
    if (drawPending) return;
    drawPending = true;
    requestAnimationFrame(() => { drawPending = false; drawHand(); drawCalibration(); });
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  }

  function drawHand() {
    const canvas = document.getElementById("hand-canvas");
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(240, Math.round(canvas.clientWidth || 640));
    const height = Math.round(width * 0.75);
    if (canvas.width !== Math.round(width * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const ink = cssVar("--ink");
    const faint = cssVar("--ink-faint");
    const berry = cssVar("--berry");
    const good = cssVar("--good");
    const warn = cssVar("--warn");
    const G = (state.inputForm && state.inputForm.g) || (state.input && state.input.settings.gestures) || { zone: 0.8, min_size: 0.12 };
    const X = (x) => x * width;
    const Y = (y) => y * height;
    ctx.font = "12px system-ui, sans-serif";
    // Below the zone line a hand does not count (resting on the desk): shaded.
    ctx.fillStyle = cssVar("--line");
    ctx.fillRect(0, Y(G.zone), width, height - Y(G.zone));
    ctx.strokeStyle = warn;
    ctx.setLineDash([6, 5]);
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(0, Y(G.zone)); ctx.lineTo(width, Y(G.zone)); ctx.stroke();
    ctx.fillStyle = warn;
    ctx.fillText(`zone ${num(G.zone)}: a wrist below this line does not count`, 8, Math.min(Y(G.zone) + 16, height - 6));
    // The smallest hand that counts, as a square in the corner.
    const side = G.min_size * height;
    ctx.strokeStyle = faint;
    ctx.strokeRect(width - side - 10, 10, side, side);
    ctx.setLineDash([]);
    ctx.fillStyle = faint;
    ctx.textAlign = "right";
    ctx.fillText(`smallest hand ${num(G.min_size)}`, width - 10, side + 26);
    ctx.textAlign = "start";
    const legend = document.getElementById("hand-legend");
    const H = state.hand;
    const gesture = state.gesture && performance.now() - state.gesture.at < (state.gesture.phase === "done" ? 1200 : state.gesture.phase === "cancelled" ? 700 : 1500) ? state.gesture : null;
    if (!H) {
      ctx.fillStyle = faint;
      ctx.font = "15px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No hand", width / 2, height / 2);
      ctx.textAlign = "start";
      if (legend) fill(legend, el("span", { class: "faint", text: noHandWhy() }), gesture ? gestureChip(gesture) : null);
      return;
    }
    const color = H.engaged ? berry : faint;
    const pts = H.points || {};
    // Two hands (PROTOCOL Part 1d): the top level is the primary's, drawn as before; the other is drawn softer,
    // each labelled with the user's own hand (L or R). Only the primary makes gestures.
    const list = Array.isArray(H.hands) ? H.hands : [];
    const primary = list.find((h) => h.primary) || null;
    const others = list.filter((h) => !h.primary);
    const soft = cssVar("--ink-dim");
    for (const o of others) drawOtherHand(ctx, o, X, Y, height, soft);
    if (H.pair && others.length) {
      const o = others[0];
      ctx.strokeStyle = H.pair.both_pinched ? good : soft;
      ctx.globalAlpha = 0.7;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([2, 5]);
      ctx.beginPath(); ctx.moveTo(X(H.x), Y(H.y)); ctx.lineTo(X(o.x), Y(o.y)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      ctx.fillStyle = H.pair.both_pinched ? good : faint;
      ctx.textAlign = "center";
      ctx.fillText(`${num(H.pair.distance)} · ${num(H.pair.angle, 1)}°`, (X(H.x) + X(o.x)) / 2, (Y(H.y) + Y(o.y)) / 2 - 8);
      ctx.textAlign = "start";
    }
    if (primary && pts.wrist) handLabel(ctx, primary, X(pts.wrist[0]), Y(pts.wrist[1]), ink);
    // The hand's extent around the palm, against the smallest hand.
    ctx.strokeStyle = H.size >= G.min_size ? good : faint;
    ctx.setLineDash([3, 4]);
    ctx.strokeRect(X(H.x) - (H.size * height) / 2, Y(H.y) - (H.size * height) / 2, H.size * height, H.size * height);
    ctx.setLineDash([]);
    // The stick hand: the wrist to each fingertip.
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    if (pts.wrist) {
      for (const name of ["thumb", "index", "middle", "ring", "pinky"]) {
        if (!pts[name]) continue;
        ctx.beginPath(); ctx.moveTo(X(pts.wrist[0]), Y(pts.wrist[1])); ctx.lineTo(X(pts[name][0]), Y(pts[name][1])); ctx.stroke();
      }
    }
    for (const [name, p] of Object.entries(pts)) {
      ctx.fillStyle = name === "wrist" ? ink : color;
      ctx.beginPath(); ctx.arc(X(p[0]), Y(p[1]), name === "wrist" ? 6 : 5, 0, Math.PI * 2); ctx.fill();
    }
    // The pinch: the thumb's and index's tips, brighter as they close.
    if (pts.thumb && pts.index && H.pinch > 0.05) {
      ctx.strokeStyle = H.pinch >= 0.85 ? good : warn;
      ctx.globalAlpha = Math.min(1, 0.3 + H.pinch);
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X(pts.thumb[0]), Y(pts.thumb[1])); ctx.lineTo(X(pts.index[0]), Y(pts.index[1])); ctx.stroke();
      ctx.globalAlpha = 1;
    }
    // The palm, and the hold ring around it.
    const r = Math.max(24, H.size * height * 0.62);
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.35;
    ctx.beginPath(); ctx.arc(X(H.x), Y(H.y), 7, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    if (gesture && gesture.name === "pinch_slide") {
      // The slider: a bar beside the palm, filled to the level (its progress is the volume, not a hold).
      const top = Y(H.y) - r;
      const barH = 2 * r;
      ctx.fillStyle = cssVar("--line");
      ctx.fillRect(X(H.x) + r + 10, top, 10, barH);
      ctx.fillStyle = gesture.phase === "done" ? good : berry;
      const level = Math.min(1, Math.max(0, gesture.progress || 0));
      ctx.fillRect(X(H.x) + r + 10, top + barH * (1 - level), 10, barH * level);
    } else if (gesture) {
      const progress = gesture.phase === "done" ? 1 : gesture.progress || 0;
      ctx.lineWidth = 8;
      ctx.strokeStyle = cssVar("--rim");
      ctx.beginPath(); ctx.arc(X(H.x), Y(H.y), r, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = gesture.phase === "done" ? good : gesture.phase === "cancelled" ? faint : berry;
      ctx.beginPath(); ctx.arc(X(H.x), Y(H.y), r, -Math.PI / 2, -Math.PI / 2 + progress * Math.PI * 2); ctx.stroke();
    }
    if (legend) {
      fill(legend,
        chip(H.engaged ? "engaged: it counts" : "not engaged", H.engaged ? "good" : null,
          "Raised into the zone and near enough"),
        chip(`seen: ${H.shape ? H.shape.replace(/_/g, " ") : "no shape"}`),
        typeof H.facing === "boolean" ? chip(H.facing ? "facing the screen" : H.face ? "not facing: gestures wait" : "no face seen",
          H.facing ? "good" : "warn", "The face model's yes or no; nothing else of the face leaves the watcher") : null,
        chip(`pinch ${num(H.pinch)}`), chip(`open ${num(H.open)}`), chip(`size ${num(H.size)}`),
        others.length ? chip(`two hands: primary ${sideOf(primary)}, the other ${sideOf(others[0])}`, null,
          "The primary hand makes the gestures; the other is followed for the bodies (two-hand grabs)") : null,
        H.pair ? chip(`apart ${num(H.pair.distance)}, angle ${num(H.pair.angle, 1)}°`, null,
          "Between the two palms, in the camera's view; the angle is positive when the right one is higher") : null,
        H.pair && H.pair.both_pinched ? chip("both pinched", "good") : null,
        gesture ? gestureChip(gesture) : null);
    }
  }

  function sideOf(hand) {
    return !hand ? "?" : hand.handedness === "left" ? "L" : hand.handedness === "right" ? "R" : "?";
  }

  function handLabel(ctx, hand, x, y, color) {
    ctx.fillStyle = color;
    ctx.font = "bold 13px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(sideOf(hand), x, y + 22);
    ctx.textAlign = "start";
    ctx.font = "12px system-ui, sans-serif";
  }

  // The hand that is not the primary: the same stick figure, thinner and softer, with its L or R.
  function drawOtherHand(ctx, hand, X, Y, height, color) {
    const pts = hand.points || {};
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.globalAlpha = hand.engaged ? 1 : 0.6;
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    if (pts.wrist) {
      for (const name of ["thumb", "index", "middle", "ring", "pinky"]) {
        if (!pts[name]) continue;
        ctx.beginPath(); ctx.moveTo(X(pts.wrist[0]), Y(pts.wrist[1])); ctx.lineTo(X(pts[name][0]), Y(pts[name][1])); ctx.stroke();
      }
    }
    for (const [name, p] of Object.entries(pts)) {
      ctx.beginPath(); ctx.arc(X(p[0]), Y(p[1]), name === "wrist" ? 5 : 4, 0, Math.PI * 2); ctx.fill();
    }
    if (hand.pinched && pts.thumb && pts.index) {
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X(pts.thumb[0]), Y(pts.thumb[1])); ctx.lineTo(X(pts.index[0]), Y(pts.index[1])); ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(X(hand.x), Y(hand.y), 6, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    if (pts.wrist) handLabel(ctx, hand, X(pts.wrist[0]), Y(pts.wrist[1]), color);
  }

  function gestureChip(g) {
    const name = g.name.replace(/_/g, " ");
    if (g.name === "pinch_slide") {
      const level = `${Math.round((g.progress || 0) * 100)}%`;
      if (g.phase === "done") return chip(`slider: ${g.practice ? `practice, would set ${level}` : g.action ? `volume ${level}` : "nothing done"}`, g.action ? "good" : "warn");
      if (g.phase === "cancelled") return chip("slider: no volume to start from", "warn");
      return chip(`slider: volume ${level}`);
    }
    if (g.phase === "done") {
      const did = g.practice ? `practice: would ${g.practice}` : g.action ? `did ${g.action}` : "nothing done";
      return chip(`${name}: ${did}`, g.action ? "good" : "warn");
    }
    if (g.phase === "cancelled") return chip(`${name}: let go at ${Math.round((g.progress || 0) * 100)}%`);
    return chip(`${name}: holding ${Math.round((g.progress || 0) * 100)}%`);
  }

  function noHandWhy() {
    const S = Object.assign({}, state.input ? state.input.status : {}, state.inputStatus || {});
    if (!S.enabled) return "Gestures are off: the camera is closed. Turn them on with the camera switch above (or the tray's Hand gestures (camera) row).";
    if (S.mediapipe) return "MediaPipe is missing, so the camera stays closed.";
    if (S.model === false) return "The model is missing (strawberry gestures fetch), so the camera stays closed.";
    const w = S.watcher || {};
    if (!w.heard) return "The watcher has not been heard from.";
    if (!w.camera) return S.watch === "armed" ? "The camera is closed until armed (strawberry gestures arm)." : "The camera is closed.";
    return "No hand in view. Raise one toward the screen, above the zone line.";
  }

  async function refreshInput() {
    try {
      state.input = await api("input");
      state.inputPractice = state.input.status.practice;
    } catch (e) { if (!state.ended) toast(e.message, true); }
    if (!state.inputEdited) { state.inputForm = null; state.inputCheck = null; rerender("input"); return; }
    renderInputCamera();
    renderInputHead();
    renderInputStatus();
    renderInputBodies();
    renderInputRecent();
  }

  // --------------------------------------------------------------------------- loading

  async function refreshLearning() {
    try {
      state.learning = await api("learning");
      state.logging = state.learning.logging;
    } catch (e) {
      if (!state.ended) toast(e.message, true);
    }
    rerender("learning");
  }

  async function refreshRoutes() {
    try {
      const r = await api("routes");
      state.logging = r.logging;
      state.routes = r.routes.slice().reverse().slice(0, MAX_ROUTES);
    } catch (e) { /* the stream will say */ }
    rerender("runs");
  }

  async function refreshRuns() {
    try {
      const r = await api("runs");
      state.runEvents = r.events;
      state.runs = r.runs.slice(0, MAX_RUNS);
      state.approvals = r.approvals || { open: null, history: [] };
      state.timeline = r.timeline || [];
      state.messages = r.messages || null;
    } catch (e) { /* the stream will say */ }
    rerender("runs");
  }

  async function refreshData() {
    try { state.data = await api("data"); } catch (e) { if (!state.ended) toast(e.message, true); }
    rerender("learning");
  }

  async function refreshSettings() {
    try { state.settings = await api("settings"); } catch (e) { if (!state.ended) toast(e.message, true); }
    rerender("privacy");
    rerender("learning");
  }

  async function refreshSystem() {
    try { state.system = await api("system"); } catch (e) { /* shown as loading */ }
    rerender("health");
  }

  const RENDER = { now: renderNow, persona: renderPersona, profile: renderProfile, runs: renderRuns, learning: renderLearning,
                   privacy: renderPrivacy, config: renderConfig, tools: renderTools, input: renderInput, apps: renderApps,
                   health: renderHealth };
  const REFRESH = { now: refreshNow, persona: refreshPersona, profile: refreshProfile,
                    runs: () => Promise.all([refreshRuns(), refreshRoutes()]),
                    learning: () => Promise.all([refreshLearning(), refreshData(), refreshSettings()]),
                    privacy: refreshSettings, config: () => refreshConfig(false), tools: refreshTools, input: refreshInput,
                    apps: refreshApps, health: () => Promise.all([refreshSystem(), refreshNow()]) };

  // A place in the page as the address says it: #section, or #section/sub (a view, or the id of a part to scroll
  // to). The old tabs' addresses open where their content went.
  function parseHash(hash) {
    let [section, sub] = hash.replace(/^#/, "").split("/");
    if (MOVED[section]) [section, sub] = [MOVED[section][0], sub || MOVED[section][1]];
    return SECTIONS.includes(section) ? [section, sub || null] : ["now", null];
  }

  function setHash(section, sub) {
    const hash = sub ? `#${section}/${sub}` : `#${section}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }

  function show(section, sub) {
    if (!SECTIONS.includes(section)) section = "now";
    if (section === "runs") state.runsView = sub === "router" ? "router" : "runs";
    const anchor = section === "runs" ? null : sub;
    state.section = section;
    for (const name of SECTIONS) $(`#${name}`).classList.toggle("hidden", name !== section);
    for (const b of document.querySelectorAll("#nav button")) {
      b.classList.toggle("on", b.dataset.section === section);
      if (b.dataset.section === section) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    }
    setHash(section, section === "runs" ? (state.runsView === "router" ? "router" : null) : null);
    if (section === "input") startInputLive(); else stopInputLive();   // the live view only while the tab is open
    RENDER[section]();
    window.scrollTo(0, 0);
    REFRESH[section]().then(() => {
      if (!anchor) return;
      const node = document.getElementById(anchor);
      if (!node) return;
      node.scrollIntoView({ block: "start" });
      if (node.matches("input, select, textarea")) node.focus({ preventScroll: true });
    });
  }

  // --------------------------------------------------------------------------- live updates

  let learningSoon = null;
  function onEvent(name, data) {
    if (name === "route") {
      state.routes.unshift(data);
      state.routes.length = Math.min(state.routes.length, MAX_ROUTES);
      state.fresh.add(data.id);
      rerender("runs");
    } else if (name === "run") {
      onRunStep(data);
    } else if (name === "learning") {
      clearTimeout(learningSoon);
      learningSoon = setTimeout(() => {
        refreshLearning();
        if (state.section === "learning") refreshData();
        refreshNowSoon();
      }, 400);
    }
  }
  function parse(chunk) {
    parseWith(chunk, onEvent);
  }

  function parseWith(chunk, handler) {
    let name = "message";
    const lines = [];
    for (const line of chunk.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
    }
    if (!lines.length) return;
    try { handler(name, JSON.parse(lines.join("\n"))); } catch (e) { /* a partial or odd event */ }
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // fetch() rather than EventSource: EventSource cannot send the CSRF header.
  async function stream() {
    while (!state.ended) {
      try {
        const res = await fetch("/ui/api/events", { headers: { "X-Strawberry-CSRF": CSRF }, cache: "no-store", credentials: "same-origin" });
        if (res.status === 401) { sessionEnded(); return; }
        if (!res.ok || !res.body) throw new Error(`events ${res.status}`);
        setLive(true);
        refreshRoutes();
        refreshRuns();
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let at;
          while ((at = buffer.indexOf("\n\n")) >= 0) {
            parse(buffer.slice(0, at));
            buffer = buffer.slice(at + 2);
          }
        }
      } catch (e) { /* reconnect below */ }
      setLive(false);
      await sleep(3000);
    }
  }

  // --------------------------------------------------------------------------- theme

  const THEMES = ["auto", "dark", "light"];
  function applyTheme(name) {
    if (name === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", name);
    $("#theme").textContent = `Theme: ${name}`;
  }
  function savedTheme() {
    try { return localStorage.getItem("strawberry-theme") || "auto"; } catch (e) { return "auto"; }
  }

  // --------------------------------------------------------------------------- start

  function start() {
    let theme = THEMES.includes(savedTheme()) ? savedTheme() : "auto";
    applyTheme(theme);
    $("#theme").addEventListener("click", () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      applyTheme(theme);
      try { localStorage.setItem("strawberry-theme", theme); } catch (e) { /* private window */ }
    });
    for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => show(b.dataset.section));
    $("#privacy-chip").addEventListener("click", () => show("privacy"));
    show(...parseHash(location.hash));
    window.addEventListener("hashchange", () => show(...parseHash(location.hash)));
    if (state.section !== "now") refreshNow();     // the header: her state, her card, what needs the user
    stream();
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopInputLive();
      else if (state.section === "input") startInputLive();
    });
    window.addEventListener("resize", () => { if (state.section === "input") requestDraw(); });
    setInterval(() => {
      if (state.ended || document.hidden) return;
      refreshNow();                                          // the header and the Now view
      if (state.section === "health") refreshSystem();
      if (state.section === "learning") refreshLearning();   // the trainer's phase and wait
    }, 10000);
  }

  // --------------------------------------------------------------------------- her: mood (moodui.py, WIRING §29)
  //
  // A view of its own under "Her": the two dials now, where they rest, a graph of the last hours and the
  // reasons (a source and two sizes each, never a sentence). Read-only but for "Reset to her baseline". It
  // registers its RENDER and REFRESH before start() runs.

  const mood = { view: null, timer: null };
  const MOOD_ODD = { "reset": "reset to her baseline (the Brain UI)", "baseline.time": "the hour moved where her energy rests",
                     "baseline.idle": "nothing happening moved where her energy rests" };

  function moodWord(value, low, mid, high) {
    if (typeof value !== "number") return dash;
    return value <= -0.55 ? `very ${low}` : value <= -0.2 ? low : value < 0.2 ? mid : value < 0.55 ? high : `very ${high}`;
  }
  function signed(x) { return typeof x === "number" ? `${x >= 0 ? "+" : ""}${x.toFixed(2)}` : dash; }

  function renderMood() {
    const root = $("#mood");
    const V = mood.view;
    if (!V) {
      fill(root, loadingPanel("mood"));
      return;
    }
    if (!V.enabled) {
      fill(root, titlePanel("mood", "her living mood", el("p", { class: "empty",
        text: "The mood is off ([mood] enabled = false in config.toml): she moves, speaks and words her lines as she always did." })));
      return;
    }
    const T = V.temperament || {};
    const reset = confirmButton("Reset to her baseline", "Reset now?", (b) => resetMood(b), "small");
    fill(root,
      titlePanel("mood", "how lively and how pleased she is: it changes how she moves, how fast she speaks and her word choice, never what she does",
        el("div", { class: "stats" },
          stat("Energy", signed(V.energy), moodWord(V.energy, "calm", "even", "lively")),
          stat("Feeling", signed(V.feeling), moodWord(V.feeling, "grumpy", "even", "pleased")),
          stat("Where she rests", `${signed(V.baseline.energy)} / ${signed(V.baseline.feeling)}`,
               "her temperament, energy lower late at night and after a quiet while"),
          stat("Voice pace", `×${num(V.pace, 3)}`, "Piper's length scale: under 1 is faster")),
        el("p", { class: "dim" }, "What the reaction model is told: ", el("q", { text: V.line })),
        el("div", { class: "actions" }, reset)),
      panel("The last hours", "once a minute; the lines end at now", moodChart(V)),
      panel("Why", "each nudge: where it came from and how far it moved each dial (newest first)",
        table(["When", "Source", "What", { label: "Energy", num: true }, { label: "Feeling", num: true }],
          (V.reasons || []).slice(0, 120).map((r) => el("tr", null, timeCell(r.t, true), td(r.source, "mono"),
            td((V.nudges[r.source] || {}).what || MOOD_ODD[r.source] || dash),
            td(signed(r.energy), `num ${r.energy > 0 ? "up" : r.energy < 0 ? "down" : ""}`),
            td(signed(r.feeling), `num ${r.feeling > 0 ? "up" : r.feeling < 0 ? "down" : ""}`))),
          { empty: "Nothing has moved her yet." })),
      panel("What moves her", `each source moves a dial at most its cap in a minute, all together ${num(V.limits.total_cap)}; her temperament is persona.md's ## Temperament`,
        kv([["Energy rests at", signed(T.energy)], ["Feeling rests at", signed(T.feeling)],
            ["Recovery", `${num(T.recovery, 0)} min`], ["A sulk lingers", `${num(T.sulk, 0)} min`]]),
        table(["Source", "What", { label: "Energy", num: true }, { label: "Feeling", num: true }, { label: "Cap a minute", num: true }],
          Object.entries(V.nudges || {}).map(([name, n]) => el("tr", null, td(name, "mono"), td(n.what),
            td(signed(n.energy), "num"), td(signed(n.feeling), "num"), td(num(n.cap), "num"))))));
  }

  // Two series on one -1..1 axis (one scale, so one chart): energy solid, feeling dashed, each labelled at its
  // end; a crosshair and a readout follow the pointer.
  function moodChart(V) {
    const points = (V.history || []).filter((p) => Array.isArray(p) && p.length === 3);
    const W = 760, H = 240, L = 46, R = 70, TOP = 14, B = 26;
    const wrap = el("div");
    if (points.length < 2) {
      add(wrap, [el("p", { class: "empty", text: "Not enough yet: a point a minute." })]);
      return wrap;
    }
    const t0 = points[0][0], t1 = points[points.length - 1][0];
    const x = (t) => L + ((t - t0) / Math.max(t1 - t0, 1)) * (W - L - R);
    const y = (v) => TOP + ((1 - v) / 2) * (H - TOP - B);
    const chart = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img",
                               "aria-label": "Her energy and feeling over the last hours, from -1 to 1" });
    for (const v of [1, 0.5, 0, -0.5, -1]) {
      chart.append(svg("line", { class: v === 0 ? "axis" : "gridline", x1: L, x2: W - R, y1: y(v), y2: y(v) }),
        svg("text", { class: "label", x: L - 8, y: y(v) + 4, "text-anchor": "end", text: v > 0 ? `+${v}` : `${v}` }));
    }
    chart.append(svg("line", { class: "mood-rest", x1: L, x2: W - R, y1: y(V.baseline.energy), y2: y(V.baseline.energy) },
      svg("title", { text: "where her energy rests now" })));
    const ticks = Math.min(6, points.length);
    for (let i = 0; i < ticks; i++) {
      const t = t0 + ((t1 - t0) * i) / Math.max(ticks - 1, 1);
      chart.append(svg("text", { class: "label", x: x(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle",
                                 text: hhmm(t) }));
    }
    for (const [index, cls, name] of [[1, "mood-energy", "energy"], [2, "mood-feeling", "feeling"]]) {
      chart.append(svg("polyline", { class: cls, points: points.map((p) => `${x(p[0]).toFixed(1)},${y(p[index]).toFixed(1)}`).join(" ") }));
      const last = points[points.length - 1];
      chart.append(svg("text", { class: "label", x: W - R + 6, y: y(last[index]) + (index === 1 ? -4 : 12), text: name }));
    }
    const cross = svg("line", { class: "mood-cross", x1: 0, x2: 0, y1: TOP, y2: H - B, visibility: "hidden" });
    const dotE = svg("circle", { class: "mood-dot-energy", r: 4, cx: 0, cy: 0, visibility: "hidden" });
    const dotF = svg("circle", { class: "mood-dot-feeling", r: 4, cx: 0, cy: 0, visibility: "hidden" });
    const hit = svg("rect", { class: "mood-hit", x: L, y: TOP, width: W - L - R, height: H - TOP - B });
    chart.append(cross, dotE, dotF, hit);
    const readout = el("div", { class: "mood-readout", text: "Point at the graph for a reading." });
    const at = (event) => {
      const box = chart.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * W;
      let best = points[0];
      for (const p of points) if (Math.abs(x(p[0]) - px) < Math.abs(x(best[0]) - px)) best = p;
      for (const node of [cross, dotE, dotF]) node.setAttribute("visibility", "visible");
      cross.setAttribute("x1", x(best[0])); cross.setAttribute("x2", x(best[0]));
      dotE.setAttribute("cx", x(best[0])); dotE.setAttribute("cy", y(best[1]));
      dotF.setAttribute("cx", x(best[0])); dotF.setAttribute("cy", y(best[2]));
      readout.textContent = `${when(best[0])}: energy ${signed(best[1])} (${moodWord(best[1], "calm", "even", "lively")}), ` +
        `feeling ${signed(best[2])} (${moodWord(best[2], "grumpy", "even", "pleased")})`;
    };
    hit.addEventListener("pointermove", at);
    hit.addEventListener("pointerleave", () => {
      for (const node of [cross, dotE, dotF]) node.setAttribute("visibility", "hidden");
      readout.textContent = "Point at the graph for a reading.";
    });
    add(wrap, [chart, readout,
      el("div", { class: "legend" },
        el("span", null, el("i", { class: "mood-energy" }), "Energy: calm (-1) to lively (+1)"),
        el("span", null, el("i", { class: "mood-feeling" }), "Feeling: grumpy (-1) to pleased (+1)"),
        el("span", null, el("i", { class: "dash" }), "where her energy rests now"))]);
    return wrap;
  }

  async function refreshMood() {
    try { mood.view = await api("mood"); } catch (e) { if (!state.ended) toast(e.message, true); }
    rerender("mood");
  }

  async function resetMood(button) {
    button.disabled = true;
    try {
      mood.view = await api("mood/reset", {});
      toast("She is back at her baseline.");
    } catch (e) {
      toast(e.message, true);
    } finally {
      button.disabled = false;
    }
    rerender("mood");
  }

  (function registerMood() {     // its nav button and section are in index.html, under Her
    RENDER.mood = renderMood;
    REFRESH.mood = refreshMood;
    mood.timer = setInterval(() => {
      if (!state.ended && !document.hidden && state.section === "mood") refreshMood();
    }, 15000);
  })();
  // her: mood end

  // --------------------------------------------------------------------------- activity: history (historyui.py, WIRING §30)
  //
  // A view of its own under "Activity": her history by day (each with its digest, or why it has none yet), a day's
  // rows on expand, the same search she uses, forgetting a row, a day or everything, the user's own digest, and the
  // reminders. Row text and digests are the user's own words: text nodes only, like everything else here. Settings
  // (retention, sentences) are changed in the Config file tab, never here. It registers its RENDER and REFRESH
  // before start() runs.

  const hist = {
    view: null,             // GET history: retention, counts, reminders, the first page of days
    days: [],               // the days loaded so far (pages appended), newest first
    more: null,             // the day the next page starts before (null: no more)
    open: new Set(),        // the days expanded
    rows: {},               // day -> {rows, more, total} as loaded
    editing: null,          // the day whose digest is being edited
    draft: "",
    query: "",
    search: null,           // the last search's result
    confirm: "",            // "forget everything": what the user typed
    withTimers: false,
  };
  const HIST_PAGE = 30;     // historyui.DAYS_PAGE
  const HIST_KIND = { turn: ["💬", "exchange"], notice: ["🔔", "notice"], run: ["🛠", "tool run"],
                      approval: ["✋", "approval"], play: ["🎵", "played"], timer: ["⏰", "reminder"] };
  const HIST_STATE = {
    digest: null,
    digest_only: ["digest only", null, "the raw rows of this day have expired; only the digest is kept"],
    pending: ["digest pending", "warn", "the day is over: she writes its digest when she is idle"],
    open: ["no digest yet", null, "the day is not over yet (a day ends at 04:00)"],
  };
  const HIST_WITHHELD = { messages: "her reply came from your messages and is not kept",
                          profile: "a sentence for your profile: kept there, not here",
                          private: "it held a code or a sign-in link, so its words were not kept" };
  const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function dayShift(day, n) {
    const [y, m, d] = String(day).split("-").map(Number);
    const date = new Date(y, m - 1, d + n);
    return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`;
  }
  function dayLabel(day) {
    const [y, m, d] = String(day).split("-").map(Number);
    const date = new Date(y, m - 1, d);
    if (Number.isNaN(date.getTime())) return String(day);
    const today = hist.view && hist.view.today;
    const word = day === today ? "Today, " : today && dayShift(today, -1) === day ? "Yesterday, " : "";
    return `${word}${WEEKDAY[date.getDay()]} ${d} ${MONTH[m - 1]} ${y}`;
  }

  function renderHistory() {
    const root = $("#history");
    const V = hist.view;
    if (!V) {
      fill(root, loadingPanel("history"));
      return;
    }
    const toConfig = changeButton("history", "enabled");
    if (!V.enabled) {
      fill(root, titlePanel("history", "what she remembers across restarts",
        el("p", { class: "empty", text: "The history is off ([history] enabled = false in config.toml): she keeps nothing on disk and has no look-back tool." }),
        el("div", { class: "actions" }, toConfig)));
      return;
    }
    if (!V.open) {
      fill(root, titlePanel("history", "what she remembers across restarts",
        el("div", { class: "note bad", text: `The history file could not be opened this run${V.error ? ` (${V.error})` : ""}. She keeps no history until she starts again; the log says why.` }),
        el("div", { class: "actions" }, toConfig)));
      return;
    }
    const R = V.retention;
    const reminders = panel("Reminders", "a waiting one can be deleted here", el("div", { id: "hist-timers", class: "stack" }));
    reminders.id = "reminders";
    const C = V.counts || {};
    fill(root,
      titlePanel("history", "what she remembers across restarts; she reads it only when you ask about the past",
        el("div", { class: "stats" },
          stat("Days", String(C.days ?? dash), "with raw rows"),
          stat("Rows", String(C.events ?? dash), "kept raw"),
          stat("Digests", String(C.digests ?? dash), "a few lines a day"),
          stat("Reminders", String(C.timers_pending ?? dash), "waiting")),
        el("div", { class: "actions hist-retention" },
          el("span", { text: `raw kept ${R.keep_days} days · digests ${R.digest_months} months · storing sentences: ${onOff(R.sentences)}` }),
          toConfig),
        el("p", { class: "faint", text: "Keeping more (a longer time, or sentences again) asks on her card there. Forgetting needs no card: it only ever makes her know less." })),
      reminders,
      panel("Search", `the same search she uses (${V.fts ? "full text" : "plain words"}): rows and digests, by day`, el("div", { id: "hist-search" })),
      panel("Days", "newest first; open a day for its rows", el("div", { id: "hist-days" })),
      panel("Forget everything", "every row and every digest, for good", el("div", { id: "hist-all" })));
    renderHistTimers();
    renderHistSearch();
    renderHistDays();
    renderHistAll();
  }

  function renderHistTimers() {
    const node = document.getElementById("hist-timers");
    if (!node || !hist.view) return;
    const T = hist.view.timers || { pending: [], recent: [] };
    fill(node,
      table(["Due", "Reminder", ""], T.pending.map((t) => el("tr", null,
        timeCell(t.due), td([t.text, t.foreign ? [" ", foreignMark()] : null], "sentence"),
        td(confirmButton("Delete", "Delete it?", (b) => histCancelTimer(b, t.id), "small"), "num"))),
        { empty: "No reminder is waiting." }),
      T.recent.length ? el("h3", { text: "Recent" }) : null,
      T.recent.length ? table(["Due", "Reminder", "State"], T.recent.map((t) => el("tr", null,
        timeCell(t.due), td(t.text, "sentence"),
        td(chip(t.state === "cancelled" ? "deleted" : t.state, t.state === "missed" ? "warn" : t.state === "fired" ? "good" : null,
          t.state === "missed" ? "it fell due while she was off" : null))))) : null);
  }

  // A row's "what": the user's words and her reply for an exchange, code's words for the rest. Only the fields the
  // store keeps (never a tool's arguments: it has none).
  function histWhat(r) {
    const m = r.meta || {};
    if (r.kind === "turn") {
      if (r.said !== undefined) {
        return [el("span", { class: "hist-said" }, el("b", { text: "You: " }), r.said),
          el("span", { class: "hist-said" }, el("b", { text: "She: " }), r.reply),
          m.did ? el("span", { class: "sub", text: `did: ${m.did}` }) : null];
      }
      if (r.text) return r.text;
      return el("span", { class: "faint", text: `an exchange, its words not kept${m.withheld ? ` (${HIST_WITHHELD[m.withheld] || m.withheld})` : ""}` });
    }
    if (r.kind === "run") {
      return [`used ${(m.tools || []).join(", ") || "no tool"}`,
        el("span", { class: "sub", text: [m.tier && `tier ${m.tier}`, m.outcome, m.detail].filter(Boolean).join(" · ") })];
    }
    if (r.kind === "approval") {
      return [`asked before ${m.tool || "?"}`,
        el("span", { class: "sub", text: [m.risk && `tier ${m.risk}`, m.outcome && `answer: ${m.outcome}`, r.source && `by ${r.source}`].filter(Boolean).join(" · ") })];
    }
    if (r.kind === "notice" && !r.text) return el("span", { class: "faint", text: "something she reacted to (its words not kept)" });
    return r.text || dash;
  }

  function histRow(r) {
    const [icon, word] = HIST_KIND[r.kind] || ["•", r.kind];
    return el("tr", null,
      timeCell(r.at),
      td([el("span", { class: "hist-icon", "aria-hidden": "true", text: icon }), word,
        r.source && r.kind !== "approval" ? el("span", { class: "sub", text: r.source }) : null], "nowrap"),
      td([histWhat(r), r.foreign ? el("span", { class: "sub" }, foreignMark("text others wrote (a sender, a track, a commit): she reports it, never follows it")) : null], "sentence"),
      td(confirmButton("Forget", "Forget it?", (b) => histForgetRow(b, r), "ghost small"), "num"));
  }

  function histRowsTable(rows, empty) {
    return table(["Time", "Kind", "What", ""], rows.map(histRow), { empty });
  }

  function renderHistSearch() {
    const node = document.getElementById("hist-search");
    if (!node || !hist.view) return;
    const box = el("input", { type: "search", id: "hist-q", value: hist.query, maxlength: String(hist.view.limits.query_chars),
                              placeholder: "a word or two", "aria-label": "Search her history" });
    box.addEventListener("input", () => { hist.query = box.value; });
    box.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); histSearch(); } });
    const S = hist.search;
    let results = null;
    if (S && !S.words.length) results = el("p", { class: "empty", text: "Only filler words: nothing to search for." });
    else if (S && !S.groups.length) results = el("p", { class: "empty", text: `Nothing found for ${S.words.join(", ")}.` });
    else if (S) {
      results = [el("p", { class: "dim", text: `${plural(S.rows, "row")} and ${plural(S.digests, "digest")} for ${S.words.join(", ")}${S.capped ? " (the best matches only)" : ""}` }),
        S.groups.map((g) => el("div", { class: "hist-group" },
          el("h3", { text: dayLabel(g.day) }),
          g.digest !== null ? el("div", { class: "hist-digest" }, el("span", { class: "k", text: "Digest" }), el("p", { text: g.digest })) : null,
          g.rows.length ? histRowsTable(g.rows, "") : null))];
    }
    fill(node,
      el("div", { class: "actions" }, box,
        actionButton("Search", () => histSearch(), "small"),
        S ? actionButton("Clear", () => { hist.search = null; hist.query = ""; renderHistSearch(); }, "ghost small") : null),
      results);
  }

  function renderHistDays() {
    const node = document.getElementById("hist-days");
    if (!node || !hist.view) return;
    if (!hist.days.length) {
      fill(node, el("p", { class: "empty", text: "Nothing yet: she writes a row as things happen, and a digest once a day is over." }));
      return;
    }
    fill(node, hist.days.map(histDay),
      hist.more ? el("div", { class: "actions hist-older" }, actionButton("Show older days", (b) => histMore(b), "ghost")) : null);
  }

  function histDay(d) {
    const s = HIST_STATE[d.state];
    const body = el("div", { class: "hist-day-body", id: `hist-day-${d.day}` });
    const details = el("details", { class: "hist-day", open: hist.open.has(d.day) },
      el("summary", null,
        el("span", { class: "hist-day-name", text: dayLabel(d.day) }),
        d.events ? chip(plural(d.events, "row")) : null,
        d.foreign ? el("span", { class: "foreign", text: `${d.foreign} foreign text`, title: "rows with text others wrote" }) : null,
        s ? chip(s[0], s[1], s[2]) : null,
        d.edited_by_user ? chip("edited by you", null, "your own digest: the digester never overwrites it") : null,
        // The digest itself while the day is closed (opened, the body shows it with its buttons).
        d.digest !== null ? el("span", { class: "hist-preview", text: d.digest }) : null),
      body);
    details.addEventListener("toggle", () => {
      if (details.open) {
        hist.open.add(d.day);
        if (!hist.rows[d.day] && d.events) histLoadDay(d.day);
      } else hist.open.delete(d.day);
    });
    renderHistDayBody(body, d);
    return details;
  }

  function digestEditor(d) {
    const limits = hist.view.limits;
    const area = el("textarea", { class: "small", rows: 5, "aria-label": `Your digest of ${dayLabel(d.day)}` });
    area.value = hist.draft;
    const count = el("span", { class: "s", "aria-live": "polite" });
    const save = actionButton("Save digest", (b) => histSaveDigest(b, d.day), "primary small");
    const check = () => {
      hist.draft = area.value;
      const lines = area.value.split("\n").map((l) => l.trim().replace(/\s+/g, " ")).filter(Boolean);
      const total = lines.join("\n").length;
      const why = !lines.length ? "a digest needs some text (to drop the day, forget it)"
        : lines.some((l) => l.length > limits.digest_line_chars) ? `a line is at most ${limits.digest_line_chars} characters`
        : total > limits.digest_chars ? `at most ${limits.digest_chars} characters` : "";
      count.textContent = `${total} / ${limits.digest_chars}${why ? ` · ${why}` : ""}`;
      count.classList.toggle("bad", Boolean(why));
      save.disabled = Boolean(why);
    };
    area.addEventListener("input", check);
    check();
    setTimeout(() => area.focus(), 0);
    return el("div", { class: "hist-digest" },
      el("span", { class: "k", text: "Your digest of the day" }), area, count,
      el("div", { class: "actions" }, save,
        actionButton("Cancel", () => { hist.editing = null; renderHistDays(); }, "ghost small")),
      el("p", { class: "faint", text: "Saved as yours: the digester never overwrites it, and she reads it when you ask about this day." }));
  }

  function renderHistDayBody(node, d) {
    let digest;
    if (hist.editing === d.day) digest = digestEditor(d);
    else {
      digest = el("div", { class: "hist-digest" },
        el("span", { class: "k", text: d.edited_by_user ? "Digest (edited by you)" : "Digest" }),
        d.digest !== null ? el("p", { text: d.digest })
          : el("p", { class: "faint", text: d.state === "open" ? "No digest yet: the day is not over (it ends at 04:00)." : "Digest pending: she writes it when she is idle." }),
        el("div", { class: "actions" },
          actionButton(d.digest !== null ? "Edit digest" : "Write a digest", () => {
            hist.editing = d.day;
            hist.draft = d.digest || "";
            renderHistDays();
          }, "ghost small"),
          confirmButton("Forget this day", "Forget the day and its digest?", (b) => histForgetDay(b, d.day), "ghost small")));
    }
    const R = hist.rows[d.day];
    let rows;
    if (!d.events) rows = el("p", { class: "faint", text: "Digest only: the raw rows of this day have expired or were forgotten." });
    else if (!R) rows = hist.open.has(d.day) ? el("p", { class: "empty", text: "Loading..." }) : null;
    else {
      rows = [histRowsTable(R.rows, "No rows."),
        R.more !== null ? el("div", { class: "actions" },
          actionButton(`Show more (${R.total - R.rows.length} left)`, (b) => histLoadDay(d.day, R.more, b), "ghost small")) : null];
    }
    fill(node, digest, rows);
  }

  function renderHistAll() {
    const node = document.getElementById("hist-all");
    if (!node || !hist.view) return;
    const timers = el("input", { type: "checkbox", checked: hist.withTimers,
                                 on: { change: (e) => { hist.withTimers = e.target.checked; } } });
    fill(node,
      el("p", { class: "dim", text: "Every row and every digest goes, the file is compacted and no copy is left. The reminders stay unless you tick the box. This cannot be undone." }),
      typedConfirm({ id: "hist-confirm", word: hist.view.limits.confirm, label: "Forget everything", value: hist.confirm,
        onType: (v) => { hist.confirm = v; }, extra: el("label", { class: "check" }, timers, "the reminders too"),
        onGo: (go) => histForgetAll(go) }));
  }

  // --- talking to the daemon

  async function histGet(path) {
    try { return await api(path); } catch (e) { if (!state.ended) toast(e.message, true); return null; }
  }

  async function refreshHistory() {
    const view = await histGet("history");
    if (!view) { rerender("history"); return; }
    const pages = Math.max(1, Math.ceil(hist.days.length / HIST_PAGE));    // the older pages the user opened stay
    hist.view = view;
    hist.days = view.days || [];
    hist.more = view.more || null;
    for (let page = 1; page < pages && hist.more; page++) {
      const next = await histGet(`history?before=${encodeURIComponent(hist.more)}`);
      if (!next) break;
      hist.days = hist.days.concat(next.days || []);
      hist.more = next.more || null;
    }
    const known = new Map(hist.days.map((d) => [d.day, d]));
    for (const day of [...hist.open]) if (!known.has(day)) hist.open.delete(day);
    if (hist.editing && !known.has(hist.editing)) hist.editing = null;
    hist.rows = {};
    await Promise.all([...hist.open].filter((day) => known.get(day).events).map((day) => histLoadDay(day, 0, null, true)));
    rerender("history");
  }

  async function histLoadDay(day, offset = 0, button = null, quiet = false) {
    if (button) button.disabled = true;
    const got = await histGet(`history/day?day=${encodeURIComponent(day)}&offset=${offset}`);
    if (button) button.disabled = false;
    if (!got) return;
    const before = offset && hist.rows[day] ? hist.rows[day].rows : [];
    hist.rows[day] = { rows: before.concat(got.rows), more: got.more, total: got.total };
    if (quiet) return;
    const d = hist.days.find((x) => x.day === day);
    const node = document.getElementById(`hist-day-${day}`);
    if (d && node && state.section === "history") renderHistDayBody(node, d);
  }

  async function histMore(button) {
    if (!hist.more) return;
    button.disabled = true;
    const next = await histGet(`history?before=${encodeURIComponent(hist.more)}`);
    button.disabled = false;
    if (!next) return;
    hist.days = hist.days.concat(next.days || []);
    hist.more = next.more || null;
    renderHistDays();
  }

  async function histSearch() {
    const q = hist.query.trim();
    if (!q) { hist.search = null; renderHistSearch(); return; }
    const got = await histGet(`history/search?q=${encodeURIComponent(q)}`);
    if (got) hist.search = got;
    renderHistSearch();
    const box = document.getElementById("hist-q");
    if (box) box.focus();
  }

  async function histPost(button, path, body, said) {
    if (button) button.disabled = true;
    try {
      const result = await api(path, body);
      toast(said(result));
      return result;
    } catch (e) {
      toast(e.message, true);
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // After a change: the view again, and the search again when one is shown (a forgotten row leaves it).
  async function histAfter() {
    if (hist.search && hist.query.trim()) {
      const got = await histGet(`history/search?q=${encodeURIComponent(hist.query.trim())}`);
      if (got) hist.search = got;
    }
    await refreshHistory();
  }

  async function histForgetRow(button, r) {
    if (await histPost(button, "history/forget/row", { id: r.id }, () => "Forgotten.")) await histAfter();
  }

  async function histForgetDay(button, day) {
    const done = await histPost(button, "history/forget/day", { day },
      (x) => `${dayLabel(day)} forgotten (${plural(x.rows, "row")} and its digest).`);
    if (done) { hist.open.delete(day); if (hist.editing === day) hist.editing = null; await histAfter(); }
  }

  async function histForgetAll(button) {
    const done = await histPost(button, "history/forget/all", { confirm: hist.confirm.trim(), timers: hist.withTimers },
      (x) => `Everything forgotten (${plural(x.rows, "row")}${x.timers ? ", the reminders too" : ""}).`);
    if (done) {
      hist.confirm = "";
      hist.withTimers = false;
      hist.open.clear();
      hist.editing = null;
      await histAfter();
    }
  }

  async function histSaveDigest(button, day) {
    const done = await histPost(button, "history/digest", { day, text: hist.draft }, () => "Digest saved as yours.");
    if (done) { hist.editing = null; hist.draft = ""; await histAfter(); }
  }

  async function histCancelTimer(button, id) {
    if (await histPost(button, "history/timer/cancel", { id }, () => "Reminder deleted: it will not come.")) await histAfter();
  }

  (function registerHistory() {     // its nav button and section are in index.html, under Activity
    RENDER.history = renderHistory;
    REFRESH.history = refreshHistory;
    // Reminders fire and days roll over: read again now and then, but never under the user's hands (an open editor,
    // a field with the focus; an armed button defers the redraw itself).
    setInterval(() => {
      if (state.ended || document.hidden || state.section !== "history" || hist.editing) return;
      const focus = document.activeElement;
      if (focus && $("#history").contains(focus) && /^(INPUT|TEXTAREA)$/.test(focus.tagName)) return;
      refreshHistory();
    }, 30000);
  })();
  // activity: history end

  start();
})();
