# The gate's data (WIRING.md §8a)

| file | what |
|---|---|
| `gate_train.jsonl` | the data set the trained head is fitted on: one sentence a line, with its answer to each question it has one for |
| `gate_heldout.json` | the held-out set: never trained on, scored by `scripts/gate_heldout_check.py` and `strawberry gate eval` |
| `gate_head.npz` | the head trained on `gate_train.jsonl`, shipped as the default (`[gate] scorer = "head"`) |

A line of `gate_train.jsonl`:

```json
{"text": "can you move my 3pm to thursday", "lang": "en", "source": "generated", "group": "kind.request:4",
 "labels": {"kind": "request", "topic": "calendar", "is_urgent": "no", "is_about_her": "no",
            "has_argument": "yes", "wants_library_change": "no", "needs_catalogue": "no"}}
```

`labels` has one entry per question the sentence answers; a question it does not answer clearly is left
out, and `music_tool` is only there for music. Notification texts (`"Title: body"`, as
`privacy.gate_state` gives them to the gate) have only `is_sensitive`. `source` is `base` for the gate's
own examples in `systemone.py`, `adapter:spotify` for an adapter's gate phrases, `generated` for the
rest. `group` keeps the sentences of one generation call in one validation fold.

## How it was made

1. **The base**: every example of every option in `systemone.py` (and the Spotify adapter's phrases),
   with the option it was written for.
2. **Generation** (`scripts/gate_data.py generate --set train`): the local `qwen3.8:27b` through Ollama,
   one call per 20 sentences, per option: 70% for the option, 30% hard negatives (close in wording, but
   another option, which the model names), varied in length and register, 15% in Finnish in calls of
   their own. More per option where the first held-out evaluation found the gate weak (`is_about_her`,
   `is_urgent`, `kind` for calendar edits, `music_tool` for particular music and resume). A second,
   targeted round (`generate --set focus`) for what the first head still misread: recommendations
   asked as questions, music for a mood or an activity, greetings and goodbyes, asides not meant for
   her. 1883 generated sentences in all.
3. **Labelling** (`label --set train`): the same model answers every routing question for every
   sentence, by the definitions in `gate_data.py` (`unsure` allowed). A generated option the labeller
   contradicts is left out of that question.
4. **Review, by hand** (`scripts/gate_data/review.json`): 80 sentences dropped with the reason (broken
   Finnish, nonsense, passwords and codes), 108 fixed (mostly Finnish the model got wrong), 211
   labels set where the two passes disagreed or both were wrong. A few rules make the labeller's
   noisiest answers consistent (`scripts/gate_build.py`, `tidy`): `has_argument` is no for bare
   player commands and chat and left out for fact questions, `is_urgent` = yes is kept only where
   the sentence was written for urgency, and a question about her is chat, as in the gate's examples.
5. **Build** (`build`): exact duplicates (104) and near-duplicates (143, cosine > 0.97 on the gate's
   own embeddings) dropped, and every training sentence within cosine 0.95 of a held-out one (10).
   What was dropped and why is in `scripts/gate_data/removed.jsonl`. The result: 1797 sentences, 224
   of them Finnish, 176 of them notification texts.

The held-out set is the first evaluation's 54 sentences and 12 notifications (names replaced by
neutral ones) plus a separate generation (`generate --set heldout`: a prompt organised by situation
rather than by option, other seeds, a higher temperature, and a second round asked to stay short),
labelled the same way and then by hand, sentence by sentence; long rambles were cut to the sentence
they carried. A new held-out sentence within cosine 0.95 of a training sentence is dropped: 199
sentences (35 Finnish) and 42 notifications remain.

## Regenerate

```bash
uv run python scripts/gate_data.py generate --set train     # resumes; ~1.5 h on a 24 GB GPU
uv run python scripts/gate_data.py generate --set focus
uv run python scripts/gate_data.py generate --set heldout
uv run python scripts/gate_data.py label --set train
uv run python scripts/gate_data.py label --set heldout
# review: edit scripts/gate_data/review.json
uv run python scripts/gate_data.py build
uv run strawberry gate train --output src/strawberry_crab/data/gate_head.npz
```

The model's output is not the same on every run or machine, so the raw and labelled files in
`scripts/gate_data/` are kept: `build` and `gate train` from them are the reproducible steps (the
build needs the gate's ONNX model, `strawberry setup` fetches it; the fit is deterministic).
