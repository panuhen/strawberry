#!/usr/bin/env python3
"""Make the gate's training set and its held-out set with the local Qwen (WIRING.md §8a, Router v2).

    scripts/gate_data.py generate --set train      # sentences per option, hard negatives included
    scripts/gate_data.py generate --set focus      # a targeted second round, into the training set
    scripts/gate_data.py generate --set heldout    # a separate generation: other prompt, other seeds
    scripts/gate_data.py label --set train         # every routing question for every sentence
    scripts/gate_data.py label --set heldout
    scripts/gate_data.py build                     # base + labelled + review -> the data set, the held-out set

`generate` and `label` talk to Ollama (`qwen3.8:27b`, native /api/chat, num_ctx 8192 like the thinker,
think off) and append to scripts/gate_data/raw_<set>.jsonl and labelled_<set>.jsonl; both resume where
they stopped. `build` needs no model but the gate's embedder (the in-process ONNX one): it takes the
gate's own examples (the base), the labelled sentences and the hand review in scripts/gate_data/review.json,
drops duplicates and near-duplicates (cosine > 0.97), keeps every held-out sentence below cosine 0.95
of every training sentence, and writes

    src/strawberry_crab/data/gate_train.jsonl     the data set (one sentence per line, labels per question)
    src/strawberry_crab/data/gate_heldout.json    the held-out set (scripts/gate_heldout_check.py)
    scripts/gate_data/removed.jsonl               what was dropped, and why

Run from the repo root; uses the daemon's uv environment. The model's output differs between runs
and machines, so the raw files are kept in the repo: `build` from them is the reproducible step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WORK = ROOT / "scripts" / "gate_data"
DATA = ROOT / "src" / "strawberry_crab" / "data"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3.8:27b"
NUM_CTX = 8192                 # the thinker's size: another would make Ollama reload the model

ASSISTANT = (
    "Strawberry is a small animated crab who lives on the user's computer desktop. The user talks to her "
    "(speech recognition turns it into text: often lowercase, no punctuation, the odd filler word) or types "
    "to her. She controls whatever music player is running (skip, pause, volume, what is playing) and, with "
    "a music service connected, can find and play particular music. She answers questions, chats, and is "
    "meant to handle the calendar, notes and the computer itself too. The user speaks English or Finnish."
)

# The routing questions as the labeller reads them. The conventions are the gate's own
# (scripts/gate_phrases.json): recommending music is a request, facts about it a question.
QUESTIONS: dict[str, dict] = {
    "kind": {
        "what": "what the sentence is",
        "options": {
            "request": "asks her to do something: control the music, play or queue particular music, recommend "
                       "music, set a timer or reminder, add, move or cancel a calendar entry, write or delete a "
                       "note, open an app, change a setting on the computer. A polite question form (\"could you "
                       "pause it\", \"can you move my 3pm\") is still a request.",
            "question": "wants a fact looked up or read out: what is playing, facts about a song or artist, what "
                        "is on the calendar, what a note says, the computer's state (battery, disk, wifi), the "
                        "time, general knowledge, the weather, arithmetic.",
            "chat": "small talk, feelings, banter, opinions, greetings, thanks, compliments or teasing aimed at "
                    "her, the user telling her how they feel or how their day went, remarks about the music "
                    "(\"I love this song\") that ask for nothing.",
            "other": "not aimed at her at all: a fragment, a filler, a false start, a sound check, the user "
                     "talking to someone else in the room or on the phone, the TV or radio in the background.",
        },
    },
    "topic": {
        "what": "what the sentence is about",
        "options": {
            "music": "the music that is playing, the player, the volume, songs, artists, albums, genres, "
                     "playlists, the user's music library, music recommendations and facts about music.",
            "calendar": "meetings, appointments, events, reminders, timers, alarms, countdowns, the user's "
                        "schedule, today's date and the time here.",
            "notes": "notes, lists (shopping, to-do), things to write down, read back, find or delete later.",
            "system": "the computer itself: apps, windows, the screen, brightness, files, the trash, "
                      "downloads, power, battery, wifi, VPN, bluetooth, do not disturb, notifications, updates, "
                      "CPU and memory.",
            "other": "none of those: general knowledge, the weather, maths, the time in another city, news, "
                     "small talk, feelings, her, fragments.",
        },
    },
    "is_urgent": {
        "what": "does the user want it done right now, in a hurry",
        "options": {
            "yes": "a clear sign of hurry: \"quick\", \"now\", \"hurry\", \"stop stop stop\", \"right now\", "
                   "\"asap\", or a reason that cannot wait (someone at the door, a call starting, the baby "
                   "sleeping, too loud right now).",
            "no": "no sign of hurry. A plain command (\"pause the music\", \"resume playback\") is not "
                  "urgent by itself; neither is a question or chat.",
        },
    },
    "is_about_her": {
        "what": "is the sentence about Strawberry herself",
        "options": {
            "yes": "about her: how she is, what she feels, likes or thinks, what she can do, how she looks, "
                   "a compliment or insult aimed at her, missing her, asking whether she is listening or "
                   "awake, her name, her opinion of the song. \"do you like techno\" and \"what can you do\" "
                   "are yes.",
            "no": "about the music, the calendar, notes, the computer, the world, or the user themselves "
                  "(\"I'm tired\" is about the user). A request that merely starts with \"can you\" (\"can "
                  "you play some jazz\") is not about her.",
        },
    },
    "has_argument": {
        "what": "does the sentence carry something specific to fill in",
        "options": {
            "yes": "it names a particular song, artist, album, genre, playlist, a mood or activity to pick "
                   "music for, an amount (\"volume to 30\"), a time, a date, a duration, a person, a place, an "
                   "app or file by name, or text to write down.",
            "no": "a plain command or question with nothing to fill in: \"skip\", \"pause\", \"louder\", "
                  "\"what song is this\", \"lock the screen\", \"what time is it\". Chat and fragments are "
                  "no.",
        },
    },
    "wants_library_change": {
        "what": "does it ask to change the user's music library",
        "options": {
            "yes": "save, like, heart or favourite a song, unlike it, remove it from the liked songs, add it "
                   "to or remove it from a playlist, create or rename a playlist, follow an artist.",
            "no": "anything else: playing, queueing, skipping, pausing, volume, asking about music, and "
                  "everything that is not about music.",
        },
    },
    "music_tool": {
        "what": "for a sentence about music only: which player action it is",
        "options": {
            "skip": "go to the next track.",
            "previous": "go back to the previous track, or start this one over.",
            "pause": "pause, stop or mute the music, turn it off.",
            "resume": "start the paused music again, unpause, play (with nothing named).",
            "volume_down": "make it quieter.",
            "volume_up": "make it louder.",
            "now_playing": "say what is playing now: the song, the artist, the album of the current track.",
            "other": "anything else about music: play or queue a particular song, artist, album, genre, "
                     "playlist or mood, shuffle, repeat, seek, save or like, facts about the artist or the "
                     "song beyond its name, recommendations.",
        },
    },
    "needs_catalogue": {
        "what": "does it need particular music found in a music service, then played or queued",
        "options": {
            "yes": "play, queue or shuffle a particular artist, song, album, genre, playlist, the user's "
                   "liked songs, \"something similar\", \"more by them\", music for a mood or activity "
                   "(\"something for cooking\").",
            "no": "the player's bare buttons (skip, previous, pause, resume, volume), what is playing, facts "
                  "about music, recommending or talking about music, saving or liking the current song, and "
                  "everything that is not about music.",
        },
    },
}
ROUTING = tuple(QUESTIONS)

SENSITIVE = {
    "what": "is the notification private: must its text never reach a language model",
    "options": {
        "yes": "a one-time code, a sign-in or login alert, a password or security change, two-factor "
               "authentication, an access token or key added to an account, a bank, card or payment "
               "notice, money sent or received, an account locked or suspicious activity.",
        "no": "ordinary messages from people, work chat, builds and CI, calendar reminders, deliveries and "
              "shopping, software updates, downloads, music, the computer's own notices, social media.",
    },
}

# Generated sentences per option: more where the held-out evaluation found the gate weak (§8a).
TRAIN_TARGET = {
    "kind.request": 90, "kind.question": 90, "kind.chat": 60, "kind.other": 50,
    "topic.music": 40, "topic.calendar": 70, "topic.notes": 60, "topic.system": 70, "topic.other": 50,
    "is_urgent.yes": 70, "is_urgent.no": 60,
    "is_about_her.yes": 90, "is_about_her.no": 90,
    "has_argument.yes": 60, "has_argument.no": 60,
    "wants_library_change.yes": 50, "wants_library_change.no": 40,
    "music_tool.skip": 40, "music_tool.previous": 40, "music_tool.pause": 45, "music_tool.resume": 60,
    "music_tool.volume_down": 40, "music_tool.volume_up": 40, "music_tool.now_playing": 45,
    "music_tool.other": 90,
    "needs_catalogue.yes": 60, "needs_catalogue.no": 60,
    "is_sensitive.yes": 70, "is_sensitive.no": 70,
}
# A second, targeted round (`generate --set focus`): kinds of sentence the first head still misread
# on scripts/gate_phrases.json and that the first round had few of. Each is (question.option it is
# generated for, what to write, how many). The labeller answers every question for them as for the rest.
FOCUS = [
    ("kind.request", "asking her to recommend or suggest music, often phrased as a question: \"any good X?\", "
                     "\"what are the best Y albums\", \"what should I listen to if I like Z\", \"where do I start "
                     "with this band\", \"suggest a few artists like W\" (she answers from what she knows; nothing "
                     "is played)", 40),
    ("needs_catalogue.yes", "asking her to play or queue music picked for a mood, an activity or a moment "
                            "(\"put something on for cleaning\", \"something for dinner\"), or more of the same "
                            "(\"queue another one by them\", \"one more like that\")", 30),
    ("kind.chat", "greetings, goodbyes, good nights, thanks and see-you-laters said to her", 25),
    ("kind.other", "not meant for her: thinking aloud, losing the thread (\"right, where was I\"), fillers, "
                   "false starts, a word to someone else in the room", 30),
]
FOCUS_SEED = 5000
CHUNK = 20                     # sentences per call: bigger lists get repetitive
FINNISH = 0.15                 # the share asked for in Finnish
TRAIN_SEED = 1000
HELDOUT_SEED = 777000

# The held-out generation is organised by situation, not by option, so it does not share the training
# prompts' blind spots. Each scenario is one call; the labeller then answers every question.
HELDOUT_SCENARIOS = [
    ("music", "controlling the music while working, cooking or with friends over: next, back, pause, carry "
              "on, louder, quieter, what's this", 16),
    ("music", "asking for particular music: an artist, a song, an album, a genre, a playlist, a mood, the "
              "liked songs, something similar; and saving, liking or removing the current song", 16),
    ("music", "talking about music: facts about the band or the song, recommendations, opinions", 10),
    ("calendar", "the schedule: checking it, adding, moving or cancelling entries, reminders, timers, "
                 "alarms, the date", 14),
    ("notes", "notes and lists: writing things down, reading them back, finding or deleting a note", 10),
    ("system", "the computer: apps, windows, screen, files, battery, network, do not disturb, updates", 12),
    ("other", "general knowledge, the weather, maths, conversions, the time elsewhere", 10),
    ("her", "talking to Strawberry herself: how she is, what she likes, what she can do, compliments, "
            "teasing, missing her, asking if she's listening", 14),
    ("chat", "small talk and feelings: the user's day, moods, the weather as a remark, thanks, greetings", 10),
    ("urgent", "things that must happen immediately: someone at the door, a call starting, a sleeping "
               "child, too loud all of a sudden", 10),
    ("noise", "sentences not meant for her: talking to someone else, fragments, the TV, false starts", 10),
]
HELDOUT_NOTIFICATIONS = 30
SHORT_ROUND = 8                # a second round per situation, asked to stay short


# ----------------------------------------------------------------------------- Ollama


def chat(messages: list[dict], schema: dict, seed: int, temperature: float) -> dict:
    body = {
        "model": MODEL, "messages": messages, "stream": False, "think": False, "keep_alive": "30m",
        "format": schema, "options": {"num_ctx": NUM_CTX, "temperature": temperature, "seed": seed},
    }
    request = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                data = json.loads(response.read())
            return json.loads(data["message"]["content"])
        except (OSError, ValueError, KeyError) as exc:
            print(f"  ollama: {exc}; again", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise SystemExit("ollama did not answer three times")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def describe(question: str, spec: dict) -> str:
    lines = [f"Question `{question}`: {spec['what']}."]
    for name, text in spec["options"].items():
        lines.append(f"  - {name}: {text}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- generate


ITEMS = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "lang": {"enum": ["en", "fi"]}, "option": {"type": "string"}},
        "required": ["text", "lang", "option"]}}},
    "required": ["items"],
}
NOTES = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"app": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
                       "lang": {"enum": ["en", "fi"]}, "option": {"enum": ["yes", "no"]}},
        "required": ["app", "title", "body", "lang", "option"]}}},
    "required": ["items"],
}
PRIVATE = ("Use no real private person's name, address, phone number or account. For people use a relation "
           "(\"my sister\", \"the team\") or a common first name (Alex, Sam, Maria, Mikko, Anna); public figures, "
           "bands and artists are fine.")


def train_prompt(key: str, n: int, have: list[str], lang: str) -> str:
    question, option = key.split(".")
    spec = SENSITIVE if question == "is_sensitive" else QUESTIONS[question]
    others = [o for o in spec["options"] if o != option]
    positives = max(1, round(n * 0.7))
    if question == "is_sensitive":
        kind = ("desktop notifications (app, title, body) as apps really send them: bank apps, mail, chat, "
                "authenticators, build servers, calendars, shops")
    else:
        kind = "things the user says or types to her"
    if lang == "fi":
        language = ('All of them in Finnish (lang "fi"): natural, grammatical Finnish as a Finnish speaker really '
                    "says or types it, spoken forms (puhekieli) welcome. No word-for-word translations.")
    else:
        language = 'All of them in English (lang "en").'
    avoid = ""
    if have:
        avoid = "\nAlready written for this option (write different ones, not rewordings of these):\n" + \
                "\n".join(f"- {t}" for t in have[-40:])
    return f"""{ASSISTANT}

We are building training data for her router: a classifier that answers typed questions about each input.

{describe(question, spec)}

Write {n} {kind}:
- {positives} whose answer to `{question}` is `{option}` (set "option" to "{option}");
- {n - positives} hard negatives: close to `{option}` in wording or subject, but whose answer is one of
  {", ".join(f"`{o}`" for o in others)} (set "option" to that answer).
{language}
Vary them: one word to twenty, terse commands and polite ones, casual and slangy, indirect ("I want...",
"could you..."), with a filler word now and then, lowercase as speech recognition writes it or typed with
punctuation. Realistic, not textbook. No two alike, and none of the examples quoted in the definitions.
Check each one's option against the definitions before you write it. {PRIVATE}{avoid}"""


def generate_train(out: Path) -> None:
    rows = read_jsonl(out)
    for index, (key, target) in enumerate(TRAIN_TARGET.items()):
        question = key.split(".")[0]
        schema = NOTES if question == "is_sensitive" else ITEMS
        options = (SENSITIVE if question == "is_sensitive" else QUESTIONS[question])["options"]
        # Finnish in calls of its own: asked for inside an English list, it came out stilted.
        for lang, share in (("en", 1 - FINNISH), ("fi", FINNISH)):
            want = round(target * share)
            have = [r["text"] for r in rows if r["key"] == key and r["lang_asked"] == lang]
            while len(have) < want:
                call = len({r["call"] for r in rows if r["key"] == key})
                n = min(CHUNK if lang == "en" else CHUNK // 2, want - len(have))
                seed = TRAIN_SEED + index * 100 + call
                reply = chat([{"role": "user", "content": train_prompt(key, n, have, lang)}], schema, seed, 0.8)
                new = []
                for item in reply.get("items", [])[: n + 2]:
                    if question == "is_sensitive":
                        title, body = item["title"].strip(), item["body"].strip()
                        text = f"{title}: {body}" if title else body
                        extra = {"app": item["app"].strip(), "title": title, "body": body}
                    else:
                        text, extra = item["text"].strip(), {}
                    if not text or item["option"] not in options:
                        continue
                    new.append({"key": key, "call": call, "seed": seed, "text": text, "lang": item["lang"],
                                "lang_asked": lang, "question": question, "option": item["option"], **extra})
                if not new:
                    print(f"{key} {lang}: an empty answer; moving on", flush=True)
                    break
                append_jsonl(out, new)
                rows += new
                have += [r["text"] for r in new]
                print(f"{key} {lang}: {len(have)}/{want}", flush=True)


def generate_focus(out: Path) -> None:
    """The targeted round, appended to the training raw file (keys "focus.<i>")."""
    rows = read_jsonl(out)
    for index, (key, what, target) in enumerate(FOCUS):
        question, option = key.split(".")
        for lang, share in (("en", 1 - FINNISH), ("fi", FINNISH)):
            want = round(target * share)
            have = [r["text"] for r in rows if r["key"] == f"focus.{index}" and r["lang_asked"] == lang]
            call = len({r["call"] for r in rows if r["key"] == f"focus.{index}"})
            if len(have) >= want:
                continue
            language = ('All of them in Finnish (lang "fi"): natural, grammatical Finnish as a Finnish speaker '
                        "really says it, spoken forms welcome." if lang == "fi" else 'All of them in English (lang "en").')
            prompt = f"""{ASSISTANT}

We are building training data for her router. Write {want - len(have)} different things the user says or types to
her: {what}. Set "option" to "{option}" for every one.
{language} Vary the length, the register and the wording; realistic, not textbook; no two alike. {PRIVATE}"""
            seed = FOCUS_SEED + index * 10 + call
            reply = chat([{"role": "user", "content": prompt}], ITEMS, seed, 0.8)
            new = [{"key": f"focus.{index}", "call": call, "seed": seed, "text": i["text"].strip(), "lang": i["lang"],
                    "lang_asked": lang, "question": question, "option": option}
                   for i in reply.get("items", [])[: want - len(have)] if i["text"].strip()]
            append_jsonl(out, new)
            rows += new
            print(f"focus.{index} {lang}: {len(have) + len(new)}/{want}", flush=True)


def heldout_prompt(topic: str, what: str, n: int, have: list[str], short: bool = False) -> str:
    avoid = ""
    if have:
        avoid = "\nAlready in the test set (write different ones):\n" + "\n".join(f"- {t}" for t in have[-30:])
    if short:
        # The first round drifted into paragraphs; people talk to a voice assistant in a sentence.
        avoid += ("\nKeep them as short as people really talk to a voice assistant: most 2 to 10 words, none "
                  "over 16. One sentence each.")
    return f"""You are writing a test set to check an assistant's intent router. It must be realistic and fresh.

The assistant: {ASSISTANT}

Imagine an ordinary week of the user's life with her on the desktop. Write {n} separate things the user
says or types to her in this situation: {what}.
Mix commands, questions and remarks; some short, some long; some sloppy, some polite; include a few that
could easily be misread (they sound like one thing and are another). About {max(1, round(n * 0.15))} of them in
natural spoken Finnish (lang "fi"), the rest English (lang "en"). {PRIVATE}
Set "option" to "{topic}" for every one.{avoid}"""


def generate_heldout(out: Path) -> None:
    rows = read_jsonl(out)
    for index, (topic, what, n) in enumerate(HELDOUT_SCENARIOS):
        key = f"scenario.{index}"
        if any(r["key"] == key for r in rows):
            continue
        have = [r["text"] for r in rows]
        reply = chat([{"role": "user", "content": heldout_prompt(topic, what, n, have)}], ITEMS,
                     HELDOUT_SEED + index, 1.0)
        new = [{"key": key, "call": 0, "seed": HELDOUT_SEED + index, "text": i["text"].strip(), "lang": i["lang"],
                "question": "", "option": ""} for i in reply.get("items", [])[:n] if i["text"].strip()]
        append_jsonl(out, new)
        rows += new
        print(f"{key} ({topic}): {len(new)}", flush=True)
    for index, (topic, what, n) in enumerate(HELDOUT_SCENARIOS):
        key = f"short.{index}"
        if any(r["key"] == key for r in rows):
            continue
        have = [r["text"] for r in rows if r["key"].startswith("short.")]
        seed = HELDOUT_SEED + 500 + index
        reply = chat([{"role": "user", "content": heldout_prompt(topic, what, SHORT_ROUND, have, short=True)}],
                     ITEMS, seed, 1.0)
        new = [{"key": key, "call": 0, "seed": seed, "text": i["text"].strip(), "lang": i["lang"], "question": "",
                "option": ""} for i in reply.get("items", [])[:SHORT_ROUND] if i["text"].strip()]
        append_jsonl(out, new)
        rows += new
        print(f"{key} ({topic}): {len(new)}", flush=True)
    if not any(r["key"] == "notifications" for r in rows):
        prompt = f"""You are writing a test set for a privacy filter on desktop notifications. Before a notification's
text reaches a language model, the filter must drop the private ones.

{describe("is_sensitive", SENSITIVE)}

Write {HELDOUT_NOTIFICATIONS} notifications (app, title, body) as real apps send them, half private ("yes"), half
not ("no"), several of each that could easily be misjudged. About {round(HELDOUT_NOTIFICATIONS * 0.15)} in Finnish
(lang "fi"). {PRIVATE}"""
        reply = chat([{"role": "user", "content": prompt}], NOTES, HELDOUT_SEED + 99, 1.0)
        new = [{"key": "notifications", "call": 0, "seed": HELDOUT_SEED + 99, "app": i["app"].strip(),
                "title": i["title"].strip(), "body": i["body"].strip(), "lang": i["lang"],
                "text": f"{i['title'].strip()}: {i['body'].strip()}" if i["title"].strip() else i["body"].strip(),
                "question": "is_sensitive", "option": i["option"]} for i in reply.get("items", []) if i["body"].strip()]
        append_jsonl(out, new)
        print(f"notifications: {len(new)}", flush=True)


# ----------------------------------------------------------------------------- label


def label_schema(questions: tuple[str, ...]) -> dict:
    props = {"i": {"type": "integer"}}
    for q in questions:
        options = (SENSITIVE if q == "is_sensitive" else QUESTIONS[q])["options"]
        props[q] = {"enum": [*options, "unsure"]}
    return {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": props, "required": list(props)}}}, "required": ["items"]}


def label_prompt(texts: list[str], questions: tuple[str, ...]) -> str:
    specs = "\n\n".join(describe(q, SENSITIVE if q == "is_sensitive" else QUESTIONS[q]) for q in questions)
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    return f"""{ASSISTANT}

Label each input below for every question. Answer each question on its own, by its definition. Use "unsure"
only when the input could honestly go either way. `music_tool` only applies when the topic is music;
answer "unsure" for it otherwise.

{specs}

Inputs:
{numbered}"""


LABEL_BATCH = 20


def label(raw: Path, out: Path, base: list[dict] | None = None, prefix: str = "") -> None:
    """Label every sentence the review keeps, as the review fixed it (review.json `text`, `drop`)."""
    review_path = WORK / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    fixed, dropped = review.get(prefix + "text", {}), review.get(prefix + "drop", {})
    rows = base or []
    rows += [{**r, "text": fixed.get(r["text"], r["text"])} for r in read_jsonl(raw) if r["text"] not in dropped]
    done = {r["text"] for r in read_jsonl(out)}
    todo = [r for r in rows if r["text"] not in done]
    todo = list({r["text"]: r for r in todo}.values())
    for start in range(0, len(todo), LABEL_BATCH):
        batch = todo[start:start + LABEL_BATCH]
        routing = [r for r in batch if r["question"] != "is_sensitive"]
        notes = [r for r in batch if r["question"] == "is_sensitive"]
        new = []
        for group, questions in ((routing, ROUTING), (notes, ("is_sensitive",))):
            if not group:
                continue
            texts = [r["text"] for r in group]
            reply = chat([{"role": "user", "content": label_prompt(texts, questions)}], label_schema(questions),
                         int(hashlib.sha256(texts[0].encode()).hexdigest()[:6], 16), 0.0)
            answers = {a["i"]: a for a in reply.get("items", []) if isinstance(a.get("i"), int)}
            for i, r in enumerate(group):
                a = answers.get(i)
                if a is None:
                    continue
                new.append({**r, "qwen": {q: a[q] for q in questions}})
        append_jsonl(out, new)
        print(f"labelled {start + len(batch)}/{len(todo)}", flush=True)


def base_rows() -> list[dict]:
    """The gate's own examples, each with the one answer it was written for, and the shipped adapters'
    gate phrases: a head should read those with or without the adapter loaded."""
    from strawberry_crab import systemone
    from strawberry_crab.adapters.spotify import SPOTIFY

    rows = []
    for q in (*systemone.ROUTING, systemone.IS_SENSITIVE):
        for option in systemone._options(q):
            for text in option.examples:
                rows.append({"key": f"{q.name}.{option.name}", "call": -1, "seed": 0, "text": text,
                             "lang": "en", "question": q.name, "option": option.name, "base": True})
    for question, options in SPOTIFY.gate_examples.items():
        for option, phrases in options.items():
            for text in phrases:
                rows.append({"key": f"{question}.{option}", "call": -2, "seed": 0, "text": text, "lang": "en",
                             "question": question, "option": option, "base": True, "adapter": SPOTIFY.name})
    return rows


# ----------------------------------------------------------------------------- build (see gate_build.py)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "label"):
        p = sub.add_parser(name)
        p.add_argument("--set", choices=("train", "heldout", "focus"), required=True)
    sub.add_parser("build")
    args = parser.parse_args()
    which = "heldout" if getattr(args, "set", "") == "heldout" else "train"   # the focus round goes with train
    if args.command == "generate":
        generators = {"train": generate_train, "heldout": generate_heldout, "focus": generate_focus}
        generators[args.set](WORK / f"raw_{which}.jsonl")
    elif args.command == "label":
        label(WORK / f"raw_{which}.jsonl", WORK / f"labelled_{which}.jsonl",
              base_rows() if which == "train" else None, "" if which == "train" else "heldout_")
    else:
        sys.path.insert(0, str(Path(__file__).parent))
        from gate_build import build   # noqa: PLC0415

        return build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
