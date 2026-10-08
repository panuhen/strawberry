// Strawberry: Brain. The page's whole script (brainui.py serves it; no build step, no library).
//
// Everything the daemon sends is data: sentences, tool names, versions and reasons are put in
// the page as text nodes (textContent, Node.append of a string), never parsed as markup. The
// session's CSRF token comes from the page's <meta name="csrf"> and goes with every API call.

"use strict";

(() => {
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  const SECTIONS = ["learning", "router", "data", "settings", "system"];
  const MAX_ROUTES = 100;

  const state = {
    section: "learning",
    learning: null,
    routes: [],
    logging: false,
    data: null,
    settings: null,
    system: null,
    filter: "all",
    ended: false,
    fresh: new Set(),
    busy: false,
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
  function when(ts) {
    if (!ts) return dash;
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
  }
  function clock(ts) {
    if (!ts) return dash;
    const d = new Date(ts * 1000);
    return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
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

  // --------------------------------------------------------------------------- pieces

  function panel(title, hint, ...body) {
    return el("section", { class: "panel" },
      el("header", null, el("h2", { text: title }), hint ? el("span", { class: "hint", text: hint }) : null),
      body);
  }

  function stat(key, value, sub, small) {
    return el("div", { class: "stat" },
      el("div", { class: "k", text: key }),
      el("div", { class: small ? "v small" : "v", text: value }),
      sub ? el("div", { class: "s", text: sub }) : null);
  }

  function chip(text, kind, title) {
    return el("span", { class: kind ? `chip ${kind}` : "chip", text, title });
  }

  function kv(pairs) {
    return el("dl", { class: "kv" }, pairs.filter(Boolean).map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  }

  function table(headers, rows, opts = {}) {
    const head = el("tr", null, headers.map((h) => el("th", { class: h.num ? "num" : null, text: h.label || h })));
    return el("div", { class: "table-wrap" },
      el("table", null, el("thead", null, head), el("tbody", null, rows)),
      rows.length ? null : el("p", { class: "empty", style: null, text: opts.empty || "Nothing yet." }));
  }

  function td(value, cls) {
    const cell = el("td", { class: cls || null });
    add(cell, [value]);
    return cell;
  }

  function hiddenText() {
    return el("span", { class: "faint", text: "hidden while outcome logging is off" });
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

  function rerender(section) {
    if (state.section !== section) return;
    if (armedButtons) { deferred.add(section); return; }
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
      if (state.section === "data") await refreshData();
      if (state.section === "system") await refreshSystem();
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

  function renderChips() {
    const L = state.learning;
    const node = $("#chips");
    if (!L) { node.replaceChildren(); return; }
    const s = L.status;
    const t = L.trainer || {};
    node.replaceChildren(
      chip(`head ${s.in_use.version}`, null, "The head the gate uses now"),
      s.candidate ? chip(`candidate ${s.candidate.version} waiting`, "berry") : chip("no candidate waiting"),
      chip(`outcome logging ${onOff(L.logging)}`, L.logging ? "good" : "warn"),
      chip(`trainer ${t.phase || dash}`, t.phase && t.phase !== "idle" ? "berry" : null, t.waiting_for || ""));
  }

  function setLive(on) {
    const node = $("#live");
    node.classList.toggle("on", on);
    node.textContent = on ? "live" : "offline";
  }

  // --------------------------------------------------------------------------- 1. learning

  function renderLearning() {
    const root = $("#learning");
    const L = state.learning;
    if (!L) {
      root.replaceChildren(panel("Learning", null, el("p", { class: "empty", text: "Loading..." })));
      return;
    }
    root.replaceChildren(summaryPanel(L), candidatePanel(L), examplesPanel(L), outcomesPanel(L), versionsPanel(L),
      reportPanel(L));
  }

  function summaryPanel(L) {
    const s = L.status;
    const t = L.trainer || {};
    const e = s.examples;
    const last = s.last_train;
    const lastLine = last ? `${last.result || dash}${last.heldout ? `, held-out ${pct(last.heldout[0])} to ${pct(last.heldout[1])}` : ""}` : "none yet";
    const notes = [];
    if (!L.logging) {
      notes.push(el("p", { class: "note" }, "Outcome logging is off: nothing new is learned, and sentences are hidden here. ",
        "Settings and privacy says how to turn it on."));
    }
    if (s.in_use.note) notes.push(el("p", { class: "note", text: s.in_use.note }));
    const train = actionButton("Train now", (b) => act(b, "train", {}, () => "Training started. A run takes a minute or so."), "primary");
    train.disabled = t.phase && t.phase !== "idle";
    const rollback = s.rollback_to
      ? confirmButton(`Roll back to ${s.rollback_to}`, "Roll back now?", (b) => act(b, "rollback", {}, (r) => `Rolled back: ${r.switched.to} is in use (was ${r.switched.from}).`))
      : el("button", { type: "button", disabled: true, text: "Nothing to roll back to" });
    return panel("Where things stand", "the head the gate uses, and what waits", notes,
      el("div", { class: "stats" },
        stat("Head in use", s.in_use.version, s.in_use.source ? `source: ${s.in_use.source}` : null, true),
        stat("Candidate", s.candidate ? s.candidate.version : "none waiting",
          s.candidate ? `held-out ${pct(s.candidate.heldout.current)} to ${pct(s.candidate.heldout.candidate)}` : null, true),
        stat("Labelled examples", String(e.usable), `${e.new} new since the last run, ${e.conflicts} with a conflict`),
        stat("Last run", last ? when(last.at) : dash, last ? `${last.trigger || ""}: ${lastLine}${last.why ? ` (${last.why})` : ""}` : null, true),
        stat("Trainer", t.phase || dash, t.waiting_for ? `waiting: ${t.waiting_for}` : `runs ${t.runs || 0}, errors ${t.errors || 0}`, true)),
      el("div", { class: "actions", style: null }, train, rollback,
        actionButton("Forget...", () => show("settings", "forget"), "ghost")));
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
    const status = c.passed ? chip("passed the held-out check", "good") : chip("did not pass", "berry");
    return panel(`Candidate ${c.version}`, `built ${c.created || dash} (${c.trigger || dash}), compared with ${c.compared_with || c.parent || dash}`,
      el("div", { class: "actions" }, status, c.why ? el("span", { class: "dim", text: c.why }) : null),
      el("p", { class: "dim", text: `Trained on ${counts.base ?? dash} data-set rows and ${c.learned} of your sentences, ${c.new_to_it} of them new since the head in use. Left out: ${tally(c.left_out)}.` }),
      el("div", { class: "grid" },
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
      return el("tr", null, sentence, td(labels), td(tally(e.signals)), td(when(e.last), "nowrap"),
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
      td(when(r.ts), "nowrap"),
      el("td", { class: "sentence" }, typeof r.text === "string" ? r.text
        : r.private ? el("span", { class: "faint", text: "not shown: it reads as private" }) : hiddenText()),
      td([`${r.kind || dash} / ${r.topic || dash}`, el("span", { class: "sub", text: `${r.decision || dash}, ${r.tool || "no tool"} ${num(r.tool_confidence)}` })]),
      td([r.reflex || r.path || dash, r.teacher ? el("span", { class: "sub", text: `teacher: ${r.teacher}` }) : null]),
      td([chip(r.outcome || dash, r.outcome === "undo" || r.outcome === "correction" ? "berry" : r.outcome === "silence" ? "good" : null),
        r.follows ? el("span", { class: "sub", text: `this one ${r.follows} the one before` }) : null])));
    return panel("Outcome log", `${o.records} record(s); what came of each routed sentence`,
      el("div", { class: "actions" }, Object.entries(o.signals || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => chip(`${k} ${n}`))),
      table(["When", "Sentence", "Read as", "Handled by", "Outcome"], rows,
        { empty: L.logging ? "No records yet." : "Outcome logging is off: nothing is recorded." }));
  }

  function versionsPanel(L) {
    const rows = (L.versions || []).slice().reverse().map((v) => {
      const use = v.in_use ? chip("in use", "berry")
        : confirmButton("Use", `Use ${v.version}?`, (b) => act(b, "use", { version: v.version }, (r) => `${r.switched.to} is in use (was ${r.switched.from}).`), "small");
      return el("tr", { class: v.in_use ? "current" : null },
        td(el("span", { class: "mono", text: v.version })), td(v.status || dash), td(pct(v.heldout), "num"),
        td(v.heldout_parent != null ? pct(v.heldout_parent) : dash, "num"), td(v.learned ?? dash, "num"),
        td([v.created || dash, v.why ? el("span", { class: "sub", text: v.why }) : null]), td(v.decided_by || dash), td(use));
    });
    const history = (L.history || []).slice().reverse().map((h) => el("tr", null, td(when(h.at), "nowrap"),
      td(el("span", { class: "mono", text: h.from })), td(el("span", { class: "mono", text: h.to })), td(h.by)));
    return panel("Head versions", "every head is kept; any one can be put back",
      table(["Version", "Status", { label: "Held-out", num: true }, { label: "Head it beat", num: true }, { label: "Learned", num: true }, "Built", "Decided by", ""], rows),
      el("h3", { text: "Switches", style: null }),
      table(["When", "From", "To", "By"], history, { empty: "No switches yet." }));
  }

  function reportPanel(L) {
    const r = L.report;
    return panel("The week", `the last ${r.days} days`,
      el("p", { text: `Learned ${r.line.replace(/^learned /, "")}.` }),
      el("div", { class: "stats" },
        stat("Learned", String(r.learned), "new usable examples"),
        stat("Rejected", String(r.rejected), "by review, privacy or a conflict"),
        stat("Candidates", String(r.candidates), `${r.accepted} put in use, ${r.heads_rejected} rejected`),
        stat("Rollbacks", String(r.rollbacks)),
        stat("Head", `${r.head_before} to ${r.head_now}`, `held-out ${pct(r.heldout_before)} to ${pct(r.heldout_now)}`, true)),
      el("p", { class: "dim", text: `Signals: ${tally(r.signals)}` }));
  }

  // --------------------------------------------------------------------------- 2. router live

  function renderRouter() {
    const root = $("#router");
    const rows = state.routes.map((r) => {
      const route = r.route || {};
      const calls = (r.calls || []).map((c) => `${c.server}.${c.name}${c.ok ? "" : " (failed)"}`).join(", ");
      const handled = r.path === "reflex" ? (r.reflex || "reflex") : r.path === "thinker" ? (calls ? `thinker: ${calls}` : "thinker, no tools") : r.path;
      return el("tr", { class: state.fresh.has(r.id) ? "fresh" : null },
        td(clock(r.at)),
        el("td", { class: "sentence" }, typeof r.text === "string" ? r.text
          : el("span", { class: "faint", text: state.logging ? "not kept (private or not for her)" : "hidden" })),
        td(r.source),
        td([route.kind || dash, el("span", { class: "sub", text: num(route.confidence) })]),
        td(route.topic || dash),
        td(route.decision ? chip(route.decision, route.decision === "act" ? "berry" : null) : dash),
        td([route.tool || dash, route.tool ? el("span", { class: "sub", text: num(route.tool_confidence) }) : null]),
        td([handled || dash, r.ok === false ? el("span", { class: "sub down", text: "failed" }) : null]),
        td(route.gate_ms != null ? `${route.gate_ms}` : dash, "num"),
        td(`${r.ms}`, "num"));
    });
    state.fresh.clear();
    root.replaceChildren(panel("Router live", "each sentence as it is routed; newest first",
      state.logging ? null : el("p", { class: "note", text: "Sentences are hidden while outcome logging is off. The routes still show." }),
      table(["Time", "Sentence", "From", "Kind", "Topic", "Decision", "Tool", "Handled by", { label: "Gate ms", num: true }, { label: "Total ms", num: true }], rows,
        { empty: "Nothing routed since the daemon started. Say something, or type to her." })));
  }

  // --------------------------------------------------------------------------- 3. data and scores

  function renderData() {
    const root = $("#data");
    const D = state.data;
    if (!D) {
      root.replaceChildren(panel("Data and scores", null, el("p", { class: "empty", text: "Loading..." })));
      return;
    }
    const st = D.store;
    const gone = st.gone || {};
    const last = D.last_train || {};
    const counts = last.counts || {};
    root.replaceChildren(
      panel("The data", "what a head is trained and judged on",
        el("div", { class: "stats" },
          stat("Training set", String(D.sets.train), "shipped sentences"),
          stat("Held-out set", String(D.sets.heldout_sentences), `${D.sets.heldout_notifications} notifications too; never trained on`),
          stat("Learned examples", String(st.usable), `${st.examples} kept, ${st.conflicts} with a conflict`),
          stat("Removed in review", String(gone.rejected || 0), "sentences deleted for good"),
          stat("Removed as private", String(gone.private || 0), "read as private by the trainer"),
          stat("Last run", last.at ? when(last.at) : dash, last.at ? `${counts.base ?? dash} + ${counts.learned ?? dash} rows; left out: ${tally(last.left_out)}` : null, true))),
      chartPanel(D),
      scoresTable(D));
  }

  function chartPanel(D) {
    const points = (D.chart || []).filter((p) => typeof p.heldout === "number");
    const W = 760, H = 280, L = 48, R = 16, T = 16, B = 44;
    const values = points.map((p) => p.heldout).concat(typeof D.shipped === "number" ? [D.shipped] : []);
    if (!values.length) {
      return panel("Held-out accuracy per head", null,
        el("p", { class: "empty", text: "No heads built yet. Each candidate's held-out score shows here." }));
    }
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
        chart.append(svg("text", { class: "label", x: x(i), y: H - B + 18, "text-anchor": anchor, text: `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}` }));
      }
    });
    return panel("Held-out accuracy per head", "fields right on the held-out set, oldest head first",
      chart,
      el("div", { class: "legend" },
        el("span", null, el("i", { class: "filled" }), "accepted"),
        el("span", null, el("i"), "candidate or superseded"),
        el("span", null, el("i", { class: "grey" }), "rejected"),
        el("span", null, el("i", { class: "dash" }), `shipped head ${pct(D.shipped)}`),
        el("span", null, "larger dot: the head in use")));
  }

  function scoresTable(D) {
    const rows = (D.chart || []).slice().reverse().map((p) => el("tr", { class: p.version === D.in_use ? "current" : null },
      td(el("span", { class: "mono", text: p.version })), td(when(p.at), "nowrap"), td(p.status || dash), td(pct(p.heldout), "num"),
      td(pct(p.strict), "num"), td(pct(p.compared), "num"), td(el("span", { class: "mono", text: p.compared_with || dash }))));
    return panel("Scores by version", null,
      table(["Version", "Built", "Status", { label: "Fields", num: true }, { label: "Strict", num: true }, { label: "Head it was compared with", num: true }, "That head"], rows,
        { empty: "No heads built yet." }));
  }

  // --------------------------------------------------------------------------- 4. settings and privacy

  function renderSettings() {
    const root = $("#settings");
    const S = state.settings;
    if (!S) {
      root.replaceChildren(panel("Settings and privacy", null, el("p", { class: "empty", text: "Loading..." })));
      return;
    }
    const c = S.config;
    const n = c.notifications || {};
    const apps = Object.entries(n.body_apps || {});
    const l = c.learning || {};
    const where = S.path || "the config file (strawberry config opens it)";
    const card = (title, value, kind, lines) => el("div", { class: "stat" },
      el("div", { class: "k", text: title }),
      el("div", { class: "v small" }, chip(value, kind)),
      lines.map((line) => el("p", { class: "s" }, line)));
    root.replaceChildren(
      panel("Privacy", "read-only here: change these in config.toml, then restart her",
        el("div", { class: "grid" },
          card("Outcome logging", onOff(l.log_outcomes), l.log_outcomes ? "warn" : "good", [
            l.log_outcomes ? "The sentences you say or type are kept on this machine with what came of each, for the learning loop."
              : "Nothing you say is kept for learning. The router does not learn, and this page hides sentences.",
            ["Change: ", el("code", { text: `[learning] log_outcomes = ${!l.log_outcomes}` })]]),
          card("Sentences in the log", onOff(c.daemon && c.daemon.log_sentences), c.daemon && c.daemon.log_sentences ? "warn" : "good", [
            c.daemon && c.daemon.log_sentences ? "What you say or type is written to the daemon's log as it was."
              : "The log says only a sentence's length, never its words.",
            ["Change: ", el("code", { text: `[daemon] log_sentences = ${!(c.daemon && c.daemon.log_sentences)}` })]]),
          card("Notification bodies", n.body || dash, n.body === "off" ? "good" : "warn", [
            n.body === "off" ? "Message text is never read; she reacts to the app and the title."
              : n.body === "glance" ? "She reads the text and says a short gist (private ones are dropped)."
                : "She reads the text to react to it (private ones are dropped).",
            apps.length ? `Per app: ${apps.map(([a, m]) => `${a} ${m}`).join(", ")}` : "No per-app modes.",
            ["Change: ", el("code", { text: '[notifications] body = "off" | "react" | "glance"' }), ", or ",
              el("code", { text: "body_apps = { App = \"glance\" }" }), "; the tray's Message bodies rows change it without a restart."]]))),
      panel("Learning", null,
        kv([["Idle training", `${onOff(l.idle_train)}: after ${l.idle_minutes} min of quiet and ${l.min_new_labels} new labels`],
          ["Switch at once", `${onOff(l.auto_switch)} (auto_switch; off: a candidate waits for Accept)`],
          ["Weekly line", onOff(l.weekly_line)],
          ["Your labels' weight", `at most ${Math.round((l.max_share || 0) * 100)}% of the data set's, per option (max_share)`],
          ["Records kept", `${l.max_days} days, ${l.max_records} records at most`]]),
        el("p", { class: "dim" }, "All under ", el("code", { text: "[learning]" }), ` in ${where}.`)),
      forgetPanel(),
      panel("Everything else", `the effective settings, from ${where}`,
        el("details", null, el("summary", { text: "Show the full settings" }),
          el("pre", { class: "config", text: JSON.stringify(c, null, 2) }))));
  }

  function forgetPanel() {
    const everything = el("input", { type: "checkbox", id: "forget-all" });
    const button = confirmButton("Forget what she learned", "Delete it all now?", (b) =>
      act(b, "forget", { confirm: "forget", everything: everything.checked },
        (r) => `Forgot ${r.examples} example(s)${everything.checked ? ` and ${r.heads} head(s)` : ""}. Outcome records written before now are ignored.`), "danger");
    const node = panel("Forget", "cannot be undone",
      el("p", { text: "Deletes the learned sentences and their cached vectors, drops the candidate waiting, and ignores every outcome record written before now. Heads already built stay unless you tick the box." }),
      el("div", { class: "actions" }, el("label", { class: "check", for: "forget-all" }, everything, "Also delete every head the loop made (the shipped head is in use again)")),
      el("div", { class: "actions" }, button));
    node.id = "forget";
    return node;
  }

  // --------------------------------------------------------------------------- 5. system

  function renderSystem() {
    const root = $("#system");
    const Y = state.system;
    if (!Y) {
      root.replaceChildren(panel("System", null, el("p", { class: "empty", text: "Loading..." })));
      return;
    }
    const g = Y.gate || {};
    const emb = g.embedder || {};
    const sc = g.scorer || {};
    const head = sc.head || {};
    const tr = (Y.learning || {}).trainer || {};
    const tools = Object.entries(Y.tools || {});
    root.replaceChildren(
      el("div", { class: "stats" },
        stat("Version", Y.version, `up ${duration(Y.uptime_s)}`, true),
        stat("State", Y.state, `${Y.widgets} widget(s) connected`, true),
        stat("Gate", g.ready ? "ready" : g.starting ? "starting" : "not ready", g.disabled_reason || (emb.backend ? `embeds on ${emb.backend === "onnx" ? "ONNX (in-process)" : emb.backend}` : null), true),
        stat("Head in use", head.version || (sc.in_use === "nearest" ? "nearest examples" : dash), head.source ? `source: ${head.source}` : null, true)),
      el("div", { class: "grid" },
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

  // --------------------------------------------------------------------------- loading

  async function refreshLearning() {
    try {
      state.learning = await api("learning");
      state.logging = state.learning.logging;
    } catch (e) {
      if (!state.ended) toast(e.message, true);
    }
    renderChips();
    rerender("learning");
  }

  async function refreshRoutes() {
    try {
      const r = await api("routes");
      state.logging = r.logging;
      state.routes = r.routes.slice().reverse().slice(0, MAX_ROUTES);
    } catch (e) { /* the stream will say */ }
    rerender("router");
  }

  async function refreshData() {
    try { state.data = await api("data"); } catch (e) { if (!state.ended) toast(e.message, true); }
    rerender("data");
  }

  async function refreshSettings() {
    try { state.settings = await api("settings"); } catch (e) { if (!state.ended) toast(e.message, true); }
    rerender("settings");
  }

  async function refreshSystem() {
    try { state.system = await api("system"); } catch (e) { /* shown as loading */ }
    rerender("system");
  }

  const RENDER = { learning: renderLearning, router: renderRouter, data: renderData, settings: renderSettings, system: renderSystem };
  const REFRESH = { learning: refreshLearning, router: refreshRoutes, data: refreshData, settings: refreshSettings, system: refreshSystem };

  function show(section, anchor) {
    if (!SECTIONS.includes(section)) section = "learning";
    state.section = section;
    for (const name of SECTIONS) $(`#${name}`).classList.toggle("hidden", name !== section);
    for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("on", b.dataset.section === section);
    if (location.hash !== `#${section}`) history.replaceState(null, "", `#${section}`);
    RENDER[section]();
    REFRESH[section]().then(() => {
      if (anchor) { const node = document.getElementById(anchor); if (node) node.scrollIntoView({ block: "start" }); }
    });
  }

  // --------------------------------------------------------------------------- live updates

  let learningSoon = null;
  function onEvent(name, data) {
    if (name === "route") {
      state.routes.unshift(data);
      state.routes.length = Math.min(state.routes.length, MAX_ROUTES);
      state.fresh.add(data.id);
      rerender("router");
    } else if (name === "learning") {
      clearTimeout(learningSoon);
      learningSoon = setTimeout(() => {
        refreshLearning();
        if (state.section === "data") refreshData();
      }, 400);
    }
  }

  function parse(chunk) {
    let name = "message";
    const lines = [];
    for (const line of chunk.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
    }
    if (!lines.length) return;
    try { onEvent(name, JSON.parse(lines.join("\n"))); } catch (e) { /* a partial or odd event */ }
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
    const first = location.hash.slice(1);
    show(SECTIONS.includes(first) ? first : "learning");
    if (state.section !== "learning") refreshLearning();
    stream();
    setInterval(() => {
      if (state.ended || document.hidden) return;
      if (state.section === "system") refreshSystem();
      if (state.section === "learning") refreshLearning();   // the trainer's phase and wait
    }, 10000);
  }

  start();
})();
