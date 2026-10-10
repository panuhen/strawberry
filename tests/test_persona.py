"""persona.md (WIRING.md §21): one file for who she is, how she talks, her examples and her fixed lines."""

from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path

import pytest

from strawberry_crab import actions, paths, persona, pokes, thinker, voice
from strawberry_crab.brain import OllamaReactor
from strawberry_crab.config import BrainConfig, ThinkerConfig, load
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import CannedReactor, Event
from strawberry_crab.persona import PersonaError, PersonaStore, parse
from tests.test_thinker import FakeQwen, make, plain_config, voice_daemon

GOLDEN = json.loads((Path(__file__).parent / "golden" / "persona_before.json").read_text(encoding="utf-8"))
SHIPPED = persona.shipped_path().read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- the golden test


def test_the_shipped_persona_renders_the_reaction_prompt_and_examples_byte_for_byte():
    """What the reaction model is shown is today's (captured on main before persona.md): the system prompt
    and every example, BODY_RULE included."""
    shipped = persona.shipped()
    assert shipped.reaction_system() == GOLDEN["reaction_system"]
    assert shipped.reaction_examples() == GOLDEN["reaction_examples"]
    assert persona.PERSONA == GOLDEN["reaction_system"] and persona.EXAMPLES == GOLDEN["reaction_examples"]
    assert persona.BODY_RULE == GOLDEN["body_rule"]


def test_the_shipped_persona_keeps_every_fixed_line():
    shipped = persona.shipped()
    for key, lines in GOLDEN["lines"].items():
        assert shipped.variants(key) == tuple(lines), key
    assert set(shipped.lines) == set(GOLDEN["lines"])
    # The constants the code still holds as fallbacks are the shipped lines.
    assert actions.NO_CATALOGUE == shipped.variants("no_catalogue")
    assert (voice.DIDNT_CATCH,) == shipped.variants("didnt_catch") and (voice.EARS_LOADING,) == shipped.variants("ears_loading")
    assert (Daemon.STOPPED,) == shipped.variants("stopped")
    assert all(pokes.LINES[k] == shipped.variants(f"poke.{k}") for k in pokes.LINES)


def test_the_thinker_prompt_differs_only_in_its_first_paragraph():
    """The two drifting copies of her voice are one now: the thinker reads the reaction model's tuned
    description. Its first paragraph (who she is and her voice) is the one change; the [mood] contract and
    everything after it (the tool rules, the facts clause, the no-tools rules) is byte for byte today's."""
    new_voice = persona.shipped().thinker_voice()
    assert new_voice == (
        "You are Strawberry, a small cheerful cartoon crab who lives on the user's desktop and watches what happens "
        "on the computer. The user is talking to you now: the sentence below is theirs, heard through speech-to-text. "
        "Answer them yourself, as Strawberry. Your voice: playful, warm, a little cheeky, never mean, no emojis. You "
        "speak British English: British spelling, a dry understated wit, never American slang. Keep it easy to "
        "understand. Vary your wording from line to line: a question one time, a quip the next, an order, an aside. "
        "Never lean on one favourite adjective. No markdown, no lists, no follow-up questions, and never a name for "
        "the user - say 'you' to them. Small talk and confirmations get ONE short sentence of at most 15 words. An "
        "answer that carries facts may run to two or three plain sentences, no more.\n" + persona.MOOD)
    old_voice = GOLDEN["thinker_voice"]
    assert old_voice.split("\n")[1] == persona.MOOD                     # the output contract is unchanged
    assert thinker.VOICE == new_voice
    cases = {"tools": dict(has_tools=True), "tools_lookup": dict(has_tools=True, lookup=True),
             "lookup_only": dict(has_tools=True, acting=False), "no_tools": dict(has_tools=False)}
    for name, kwargs in cases.items():
        now = thinker.system_prompt(**kwargs)
        assert now == GOLDEN["thinker_system"][name].replace(old_voice, new_voice, 1), name
        assert now.removeprefix(new_voice) == GOLDEN["thinker_system"][name].removeprefix(old_voice)


async def test_the_reactor_sends_the_persona_md_prompt():
    reactor = OllamaReactor(BrainConfig(), fallback=CannedReactor())
    messages = reactor._messages("source: git")
    assert messages[0] == {"role": "system", "content": GOLDEN["reaction_system"]}
    shown = [(m["content"], json.loads(a["content"])) for m, a in zip(messages[1:-1:2], messages[2:-1:2])]
    assert sorted(e for e, _ in shown) == sorted(e["event"] for e in GOLDEN["reaction_examples"])
    assert all(reply.keys() == {"line", "emotion"} for _, reply in shown)


# ----------------------------------------------------------------------------- parsing and checking


def edited(old: str, new: str, text: str = SHIPPED) -> str:
    assert old in text
    return text.replace(old, new, 1)


def test_comments_are_ignored_and_the_description_continues_a_sentence():
    p = parse(SHIPPED)
    assert p.name == "Strawberry" and "<!--" not in p.who + p.talks
    assert p.talks.startswith("Playful,") and "in your own voice: playful," in p.reaction_system()
    assert persona.continued("British spelling always.") == "british spelling always."
    assert persona.continued("I'm dry.") == "I'm dry." and persona.continued("BBC English.") == "BBC English."


@pytest.mark.parametrize("change, problem", [
    (("## Lines", "## Phrases"), "unknown section"),
    (("## Examples", "## Exampels"), "\"## Examples\" is missing"),
    (("  emotion: happy\n", "  emotion: smug\n"), "emotion must be one of"),
    (("- source: git\n", "- source: email\n"), "source must be one of"),
    (("  line: Frankly an upgrade.\n", "  line: " + "x" * 141 + "\n"), "at most 140"),
    (("- Okay, stopped.\n", "- " + "y" * 201 + "\n"), "at most 200"),
    (("  title: lighthouse\n", "  title: lighthouse\n  said: hi\n"), "a git example has no said"),
    (("  said: how are you doing today\n", ""), "a voice example needs said"),
])
def test_a_persona_that_does_not_check_out_says_why(change, problem):
    with pytest.raises(PersonaError) as caught:
        parse(edited(*change))
    assert any(problem in p for p in caught.value.problems), caught.value.problems


def test_a_section_over_its_cap_is_refused():
    long = "Playful and warm. " * 60
    with pytest.raises(PersonaError, match="How she talks: ~.* tokens, at most 300"):
        parse(edited("Playful, warm, a little cheeky", long + "Playful, warm, a little cheeky"))


def test_unknown_and_missing_line_keys_are_warnings_and_keep_the_shipped_lines():
    text = edited("### stopped\n\"Stop\" or \"never mind\" while she was doing something.\n- Okay, stopped.\n",
                  "### wave\n- Hello!\n")
    p, problems = persona.check(text)
    assert problems == [] and p is not None
    assert any("wave is not used" in w for w in p.warnings) and any("stopped not in the file" in w for w in p.warnings)
    assert p.variants("stopped") == ("Okay, stopped.",)


# ----------------------------------------------------------------------------- the store


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))   # a new mtime even on a coarse clock


def test_the_users_file_is_used_live_and_a_bad_edit_falls_back(caplog):
    caplog.set_level(logging.INFO)
    store = PersonaStore()
    assert store.current() is persona.shipped() and store.source == "shipped" and store.error == []
    mine = edited("- Okay, stopped.\n", "- Right, I've stopped.\n- Stopping, stopping.\n")
    write(paths.persona_file(), mine)
    assert store.line("stopped") == "Right, I've stopped." and store.source == "file"
    assert store.status()["source"] == "file" and store.text() == mine
    write(paths.persona_file(), edited("  emotion: happy\n", "  emotion: smug\n", mine))
    assert store.line("stopped") == "Okay, stopped."            # never mute: the shipped persona
    assert store.source == "shipped" and any("emotion must be one of" in p for p in store.error)
    assert "is not used, the shipped persona is" in caplog.text
    paths.persona_file().unlink()
    assert store.current() is persona.shipped() and store.error == []


def test_save_checks_writes_atomically_and_keeps_a_backup(tmp_path):
    target = tmp_path / "persona.md"
    with pytest.raises(PersonaError):
        persona.save("## Who she is\nnobody\n", target)
    assert not target.exists()
    assert persona.save(SHIPPED, target) is None
    mine = edited("- Still on it.\n", "- Nearly there.\n")
    backup = persona.save(mine, target)
    assert target.read_text(encoding="utf-8") == mine and backup.read_text(encoding="utf-8") == SHIPPED
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []    # no temporary left behind


async def test_her_fixed_lines_come_from_persona_md(aiohttp_client):
    write(paths.persona_file(), edited("- Okay, stopped.\n", "- Halted, as asked.\n",
                                       edited("- Ha! Stop that.\n", "- Tickle tax applies.\n")))
    config = plain_config()
    spotify, toolbox, qwen, brain = make([])
    daemon, sink = voice_daemon(config, toolbox, brain)
    p = daemon.pokes
    said = [p.line("belly", 1)[0] for _ in range(3)]
    assert said == ["Hey, that tickles!", "Not the belly!", "Tickle tax applies."]
    assert daemon.persona.line("stopped") == "Halted, as asked."
    await daemon.close()


def test_the_cover_lines_follow_persona_md_unless_the_deprecated_acks_are_set(tmp_path, caplog):
    write(paths.persona_file(), edited("- On it.\n- Let me see.\n- One moment.\n- Right, hang on.\n", "- Hmm.\n"))
    assert persona.variants("cover.ack") == ("Hmm.",)
    path = tmp_path / "config.toml"
    path.write_text('[thinker]\nacks = ["Old ack."]\n[brain]\npersona = "Old persona."\n')
    caplog.set_level(logging.WARNING)
    config = load(path, env={})
    assert config.thinker.acks == ["Old ack."]
    assert "thinker.acks is deprecated" in caplog.text and "brain.persona is deprecated" in caplog.text
    reactor = OllamaReactor(config.brain, fallback=CannedReactor())
    assert reactor.system() == "Old persona."                    # the old key still wins, as it did
    assert len(reactor.examples()) == len(GOLDEN["reaction_examples"])   # examples: persona.md's


def test_an_edited_description_reaches_both_models():
    write(paths.persona_file(), edited("Playful, warm, a little cheeky, never mean, no emojis.",
                                       "Gloomy and theatrical, never mean, no emojis."))
    reactor = OllamaReactor(BrainConfig(), fallback=CannedReactor())
    assert "in your own voice: gloomy and theatrical" in reactor.system()
    _spotify, _toolbox, _qwen, brain = make([])
    assert "Your voice: gloomy and theatrical" in brain.speaker()["voice"]
    assert brain.speaker()["voice"].endswith(persona.MOOD)       # the contract is code's, whatever the file says


def test_a_persona_without_a_name_is_answered_in_character():
    p = parse(edited("You are Strawberry, a small", "you are a small"))
    assert p.name == "" and "Answer them yourself. Your voice:" in p.thinker_voice()


def test_the_shipped_sections_are_under_their_caps():
    sizes = persona.shipped().sizes()
    assert all(sizes[s] <= persona.CAPS[s] for s in persona.SECTIONS), sizes


def test_a_random_choice_uses_the_given_rng():
    assert persona.line("cover.ack", random.Random(1)) in persona.variants("cover.ack")
    assert persona.line("no.such.key") == ""


def test_example_events_match_what_the_reactor_is_shown_for_a_real_event():
    from strawberry_crab.brain import describe

    real = Event(source="notification", app="Slack", title="Priya", said="Priya asks whether the report is ready.")
    assert describe(real) in [e["event"] for e in persona.EXAMPLES]
    assert ThinkerConfig().acks == [] and BrainConfig().examples == []
