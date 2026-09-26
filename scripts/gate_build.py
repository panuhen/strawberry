"""`scripts/gate_data.py build`: the reviewed data set and the held-out set from the raw files.

Inputs (scripts/gate_data/):
    labelled_train.jsonl    every training sentence with Qwen's answer to every question (and the
                            option it was generated for, or the gate example it is)
    labelled_heldout.jsonl  the separate held-out generation, labelled the same way
    heldout_v1.json         the held-out set of the first evaluation (54 sentences, 12 notifications)
    review.json             the hand review: sentences dropped (with the reason), labels set, texts fixed

A label is kept when Qwen's labeller agrees with the option the sentence was generated for (a gate
example's own option always wins); a disagreement is left out of that question unless the review
settles it. `music_tool` is only kept for music. Outputs are listed in gate_data.py's docstring.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WORK = ROOT / "scripts" / "gate_data"
DATA = ROOT / "src" / "strawberry_crab" / "data"
NEAR_DUPLICATE = 0.97          # training sentences closer than this to a kept one are dropped
SEPARATION = 0.95              # no held-out sentence this close to a training sentence
ROUTING = ("kind", "topic", "is_urgent", "is_about_her", "has_argument", "wants_library_change", "music_tool",
           "needs_catalogue")
HELDOUT_FIELDS = {"is_urgent": "urgent", "is_about_her": "about_her", "has_argument": "argument",
                  "wants_library_change": "library", "needs_catalogue": "catalogue"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def merge_labels(row: dict, review: dict) -> tuple[dict[str, str | None], str]:
    """The labels a sentence keeps, and "" or why its generated option was overruled."""
    question = row["question"]
    qwen = row.get("qwen", {})
    labels: dict[str, str | None] = {q: (v if v != "unsure" else None) for q, v in qwen.items()}
    note = ""
    if row.get("base"):
        labels.update(row["base_labels"])     # a gate example is often one under several questions
    elif question:
        if labels.get(question) != row["option"]:
            note = f"{question}: generated as {row['option']}, labelled {qwen.get(question)}"
            labels[question] = None
    if question != "is_sensitive":
        tidy(row, labels)
    for q, v in review.get("labels", {}).get(row["text"], {}).items():
        labels[q] = v
    if question != "is_sensitive" and labels.get("topic") != "music":
        labels["music_tool"] = None
    return labels, note


BARE_TOOLS = ("skip", "previous", "pause", "resume", "volume_down", "volume_up", "now_playing")
AMOUNT = re.compile(r"\d|percent|\b(ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|max|maximum)\b")


def tidy(row: dict, labels: dict[str, str | None], urgent: bool = True) -> None:
    """The labeller's noisy answers, made consistent where the definitions decide (the review found
    them wrong more often than right): `kind` for questions about her, `has_argument` on a sentence
    it was not written for, and `is_urgent` = yes on one not written for urgency (it read "right now"
    as hurry; `urgent=False` for the held-out set, whose urgency was reviewed one by one)."""
    own = set(row.get("base_labels", ())) | {row["question"]}
    # A question about her ("what's your favourite colour", "are you awake") is chat, as in the gate's
    # examples and the held-out set; the labeller called them questions.
    if "kind" not in own and labels.get("kind") == "question" and labels.get("is_about_her") == "yes":
        labels["kind"] = "chat"
    if "has_argument" not in own:
        if labels.get("kind") in ("chat", "other"):
            labels["has_argument"] = "no"
        elif labels.get("music_tool") in BARE_TOOLS and not AMOUNT.search(row["text"].lower()):
            labels["has_argument"] = "no"
        elif labels.get("kind") == "question" and labels.get("topic") != "music":
            labels["has_argument"] = None          # a fact to look up: nothing the reflex tier fills in
    if urgent and "is_urgent" not in own and labels.get("is_urgent") == "yes":
        labels["is_urgent"] = None


async def embed_all(texts: list[str]) -> np.ndarray:
    from strawberry_crab.config import Config
    from strawberry_crab.gatecmd import GateCommandError, _gate, embed

    # The defaults, not the user's config: the in-process embeddinggemma the head is trained for,
    # and no cache (a vector cached from the Ollama fallback would pass for an ONNX one).
    config = Config()
    config.tools.enabled = False
    gate = await _gate(config, scorer="nearest")
    try:
        if gate.backend != "onnx":
            raise GateCommandError(f"the build needs the in-process model: {gate.fallback_reason}")
        return await embed(gate, texts, config.gate.query_prefix)
    finally:
        await gate.close()


def heldout_case(row: dict, labels: dict[str, str | None]) -> dict:
    case: dict = {"text": row["text"], "lang": row["lang"]}
    case["kind"] = labels["kind"]
    if labels.get("topic"):
        case["topic"] = labels["topic"]
    if labels.get("music_tool") and labels.get("topic") == "music":
        case["tool"] = labels["music_tool"]
    if labels["kind"] in ("chat", "other"):
        case["decision"] = "chat"
    for q, f in HELDOUT_FIELDS.items():
        if labels.get(q):
            case[f] = labels[q] == "yes"
    return case


def build() -> int:
    review = json.loads((WORK / "review.json").read_text(encoding="utf-8"))
    removed: list[dict] = []
    stats: dict[str, Counter] = defaultdict(Counter)       # "q.option" -> generated / kept / removed reasons

    # -- the training rows: base first (they win a near-duplicate), then the generated ones in order
    from gate_data import base_rows   # noqa: PLC0415

    labelled = {r["text"]: r for r in read_jsonl(WORK / "labelled_train.jsonl")}
    raw = base_rows() + read_jsonl(WORK / "raw_train.jsonl")
    rows: list[dict] = []
    seen: dict[str, dict] = {}
    for r in raw:
        key = f"{r['question']}.{r['option']}"
        stats[key]["base" if r.get("base") else "generated"] += 1
        text = review.get("text", {}).get(r["text"], r["text"])
        if r["text"] in review.get("drop", {}):
            removed.append({"set": "train", "text": r["text"], "key": key, "reason": review["drop"][r["text"]]})
            stats[key]["removed: review"] += 1
            continue
        if text in seen:
            first = seen[text]
            if r.get("base") and first.get("base"):
                first["base_labels"][r["question"]] = r["option"]    # one example, several questions
                continue
            removed.append({"set": "train", "text": r["text"], "key": key, "reason": "exact duplicate"})
            stats[key]["removed: duplicate"] += 1
            continue
        qwen = labelled.get(text, {}).get("qwen")
        if qwen is None:
            removed.append({"set": "train", "text": r["text"], "key": key, "reason": "not labelled"})
            stats[key]["removed: not labelled"] += 1
            continue
        row = {**r, "text": text, "qwen": qwen, "base_labels": {r["question"]: r["option"]} if r.get("base") else {}}
        seen[text] = row
        rows.append(row)

    notes = []
    for row in rows:
        row["labels"], note = merge_labels(row, review)
        if note and row["text"] not in review.get("labels", {}):
            notes.append(note + f": {row['text']!r}")

    # -- the held-out rows
    v1 = json.loads((WORK / "heldout_v1.json").read_text(encoding="utf-8"))
    for case in v1["phrases"]:
        case["text"] = review.get("heldout_text", {}).get(case["text"], case["text"])
    for case in v1["notifications"]:
        for part in ("title", "body"):
            case[part] = review.get("heldout_text", {}).get(case[part], case[part])
    fresh = []
    labelled_heldout = {r["text"]: r for r in read_jsonl(WORK / "labelled_heldout.jsonl")}
    for r in read_jsonl(WORK / "raw_heldout.jsonl"):
        text = review.get("heldout_text", {}).get(r["text"], r["text"])
        if r["text"] in review.get("heldout_drop", {}):
            removed.append({"set": "heldout", "text": r["text"], "reason": review["heldout_drop"][r["text"]]})
            continue
        if text not in labelled_heldout:
            removed.append({"set": "heldout", "text": r["text"], "reason": "not labelled"})
            continue
        labels = {q: (v if v != "unsure" else None) for q, v in labelled_heldout[text]["qwen"].items()}
        if r["question"] == "is_sensitive" and labels.get("is_sensitive") != r["option"]:
            labels["is_sensitive"] = None
        if r["question"] != "is_sensitive":
            tidy(r, labels, urgent=False)
            if labels.get("topic") != "music":
                labels["music_tool"] = None
        labels.update(review.get("heldout_labels", {}).get(text, {}))
        row = {**r, "text": text, "labels": labels}
        if r["question"] == "is_sensitive" and r["title"] and text != f"{r['title']}: {r['body']}":
            row["title"], _, row["body"] = text.partition(": ")      # fixed as "title: body"
        fresh.append(row)

    # -- near-duplicates and separation, on the gate's own embeddings
    from strawberry_crab.privacy import gate_state

    v1_texts = [c["text"] for c in v1["phrases"]] + [gate_state(c.get("title", ""), c["body"]) for c in v1["notifications"]]
    everything = [r["text"] for r in rows] + v1_texts + [r["text"] for r in fresh]
    vectors = asyncio.run(embed_all(everything))
    train_v = vectors[: len(rows)]
    v1_v = vectors[len(rows): len(rows) + len(v1_texts)]
    fresh_v = vectors[len(rows) + len(v1_texts):]

    kept: list[int] = []
    for i, row in enumerate(rows):
        key = f"{row['question']}.{row['option']}"
        if kept:
            sims = train_v[kept] @ train_v[i]
            j = int(sims.argmax())
            if sims[j] > NEAR_DUPLICATE:
                removed.append({"set": "train", "text": row["text"], "key": key,
                                "reason": f"near-duplicate ({sims[j]:.3f}) of {rows[kept[j]]['text']!r}"})
                stats[key]["removed: near-duplicate"] += 1
                continue
        v1_sims = v1_v @ train_v[i]
        j = int(v1_sims.argmax())
        if v1_sims[j] >= SEPARATION:
            removed.append({"set": "train", "text": row["text"], "key": key,
                            "reason": f"within {v1_sims[j]:.3f} of the held-out {v1_texts[j]!r}"})
            stats[key]["removed: held-out twin"] += 1
            continue
        kept.append(i)
    train_rows = [rows[i] for i in kept]
    for row in train_rows:
        stats[f"{row['question']}.{row['option']}"]["kept"] += 1

    fresh_kept = []
    kept_v = train_v[kept]
    accepted_v: list[np.ndarray] = list(v1_v)
    for i, row in enumerate(fresh):
        sims = kept_v @ fresh_v[i]
        j = int(sims.argmax())
        if sims[j] >= SEPARATION:
            removed.append({"set": "heldout", "text": row["text"],
                            "reason": f"within {sims[j]:.3f} of the training {train_rows[j]['text']!r}"})
            continue
        twins = np.stack(accepted_v) @ fresh_v[i]
        if twins.max() > NEAR_DUPLICATE:
            removed.append({"set": "heldout", "text": row["text"], "reason": f"near-duplicate ({twins.max():.3f}) in the held-out set"})
            continue
        accepted_v.append(fresh_v[i])
        fresh_kept.append(row)

    # -- write
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "gate_train.jsonl").open("w", encoding="utf-8") as f:
        for row in train_rows:
            labels = {q: v for q, v in row["labels"].items() if v}
            source = f"adapter:{row['adapter']}" if row.get("adapter") else "base" if row.get("base") else "generated"
            # A gate example was written on its own; a generated one shares a fold with its call's other sentences.
            group = f"{source}:{row['text']}" if row.get("base") else f"{row['key']}:{row['call']}"
            f.write(json.dumps({"text": row["text"], "lang": row["lang"], "source": source, "group": group,
                                "labels": labels}, ensure_ascii=False) + "\n")
    phrases = list(v1["phrases"])
    notifications = list(v1["notifications"])
    for row in fresh_kept:
        if row["question"] == "is_sensitive":
            if row["labels"].get("is_sensitive"):
                notifications.append({"app": row["app"], "title": row["title"], "body": row["body"],
                                      "sensitive": row["labels"]["is_sensitive"] == "yes", "lang": row["lang"]})
        elif row["labels"].get("kind"):
            phrases.append(heldout_case(row, row["labels"]))
    heldout = {
        "_": [
            "The gate's held-out set (WIRING.md §8a): never trained on, no sentence within cosine 0.95 of a training one.",
            "The first 54 phrases and 12 notifications are the first evaluation's (2026-09-24); the rest a separate "
            "generation (scripts/gate_data.py generate --set heldout), labelled by hand after Qwen's first pass.",
            "phrases: the fields of scripts/gate_phrases.json plus lang and optional booleans urgent / about_her / "
            "argument / library (p(yes) >= 0.5 wanted when true). notifications: scripts/sensitive_phrases.json's plus lang.",
            "Score it: scripts/gate_heldout_check.py or strawberry gate eval.",
        ],
        "phrases": phrases,
        "notifications": notifications,
    }
    (DATA / "gate_heldout.json").write_text(json.dumps(heldout, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    with (WORK / "removed.jsonl").open("w", encoding="utf-8") as f:
        for r in removed:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # -- report
    print(f"training set: {len(train_rows)} sentences ({sum(r['lang'] == 'fi' for r in train_rows)} Finnish); "
          f"held-out: {len(phrases)} phrases ({sum(p.get('lang') == 'fi' for p in phrases)} Finnish), "
          f"{len(notifications)} notifications; removed {len(removed)}")
    per_question = Counter()
    per_option = Counter()
    for row in train_rows:
        for q, v in row["labels"].items():
            if v:
                per_question[q] += 1
                per_option[f"{q}.{v}"] += 1
    for key in sorted(per_option):
        print(f"  {key:32} {per_option[key]:5}")
    print("\nper generated option (generated/base, kept, removed):")
    for key in sorted(stats):
        s = stats[key]
        reasons = ", ".join(f"{k[9:]} {v}" for k, v in s.items() if k.startswith("removed"))
        print(f"  {key:32} gen {s['generated']:3} base {s['base']:3} kept {s['kept']:3}  {reasons}")
    if notes:
        print(f"\n{len(notes)} generated options the labeller overruled (left out of that question):")
        for note in notes:
            print("  " + note)
    return 0
