"""One-off reminders (reminders.py, WIRING.md §31): the times read off words, whole sentences without a model, the
thinker's server and its trust, the scheduler (at most once, held during a run, missed while she was off, quiet,
asleep), and what never reaches a log or the OS's notifications. Every time here is a fake clock's, in a fixed
timezone."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

from strawberry_crab import persona, reminders as rem
from strawberry_crab.daemon import Daemon
from strawberry_crab.events import Event
from strawberry_crab.history import History
from strawberry_crab.config import HistoryConfig
from strawberry_crab.reminders import Reminders, parse_when, read
from strawberry_crab.server import create_app
from tests.test_runs import until
from tests.test_thinker import ScriptedGate, bare, plain_config, reading, voice_daemon

CANARY = "kestrel-canary-77d1"


@pytest.fixture(autouse=True)
def helsinki(monkeypatch):
    """The local timezone, fixed (the reminders' times are local): Europe/Helsinki, summer time in October."""
    monkeypatch.setenv("TZ", "Europe/Helsinki")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


HELSINKI = ZoneInfo("Europe/Helsinki")


def at(y: int, mo: int, d: int, h: int = 12, mi: int = 0, s: int = 0) -> float:
    """A Helsinki wall-clock time, whatever the machine's own zone: module-level times like NOW are made before
    the `helsinki` fixture sets TZ, so they must not lean on it (CI runs in UTC)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=HELSINKI).timestamp()


NOW = at(2026, 10, 14, 14, 3, 27)        # a Wednesday, 14:03:27


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def hm(due: float | None) -> str:
    return datetime.fromtimestamp(due).strftime("%a %H:%M:%S") if due is not None else ""


# ----------------------------------------------------------------------------- the times


@pytest.mark.parametrize("phrase, due, why", [
    ("in 20 minutes", "Wed 14:23:27", ""),
    ("in an hour", "Wed 15:03:27", ""),
    ("in 1h30", "Wed 15:33:27", ""),
    ("in 1 h 30 min", "Wed 15:33:27", ""),
    ("in half an hour", "Wed 14:33:27", ""),
    ("in an hour and a half", "Wed 15:33:27", ""),
    ("in twenty five minutes", "Wed 14:28:27", ""),
    ("in 2 hours and 15 minutes", "Wed 16:18:27", ""),
    ("in 90 seconds", "Wed 14:04:57", ""),
    ("at 15:00", "Wed 15:00:00", ""),
    ("at 15.30", "Wed 15:30:00", ""),
    ("at 3 pm", "Wed 15:00:00", ""),
    ("at 3 p.m.", "Wed 15:00:00", ""),
    ("at 3pm", "Wed 15:00:00", ""),
    ("at quarter past three", "Wed 15:15:00", ""),
    ("at ten to four", "Wed 15:50:00", ""),
    ("half three", "Wed 15:30:00", ""),               # British: half past three
    ("at 3", "Wed 15:00:00", ""),                     # 03:00 has gone: the one reading still ahead today
    ("at 9", "Wed 21:00:00", ""),
    ("at 9:30 tonight", "Wed 21:30:00", ""),
    ("at 6 in the evening", "Wed 18:00:00", ""),
    ("tomorrow at 9", "Thu 09:00:00", ""),
    ("at 9 tomorrow morning", "Thu 09:00:00", ""),
    ("tomorrow at 3", "Thu 15:00:00", ""),
    ("on Friday at 10", "Fri 10:00:00", ""),
    ("friday at 10am", "Fri 10:00:00", ""),
    ("this wednesday at 16:00", "Wed 16:00:00", ""),
    ("at midnight", "Thu 00:00:00", ""),
    ("2026-10-14 15:00:00", "Wed 15:00:00", ""),
    # Anything it cannot be sure of is a question, never a guess.
    ("at 1", "", "past"),                             # 01:00 and 13:00 have both gone
    ("at 09:00", "", "past"),
    ("at noon", "", "past"),
    ("tomorrow at 6", "", "meridiem"),                # six in the morning or the evening?
    ("on Wednesday at 10", "", "ambiguous"),          # said on a Wednesday: today or next week?
    ("next friday at 10", "", "ambiguous"),
    ("tomorrow at midnight", "", "ambiguous"),
    ("tomorrow", "", "no_time"),
    ("later", "", "no_time"),
    ("in a bit", "", "no_time"),
    ("in a few minutes", "", "no_time"),
    ("in 400 days", "", "too_far"),
    ("in 0 minutes", "", "too_soon"),
    ("at 25:00", "", "unclear"),
    ("about that song", "", "unclear"),
])
def test_times_are_read_or_asked_about(phrase, due, why):
    when = parse_when(phrase, NOW)
    assert (hm(when.due) if when.ok else "", when.why) == (due, why)


def test_a_bare_hour_on_a_day_is_the_waking_days_and_six_asks():
    early = at(2026, 10, 14, 1, 0)                    # 01:00: both readings of "at 3" are still ahead
    assert hm(parse_when("at 3", early).due) == "Wed 15:00:00"
    assert hm(parse_when("at 9", early).due) == "Wed 09:00:00"
    assert parse_when("at 6", early).why == "meridiem" and parse_when("at 6", early).hour == 6


def test_labels_say_the_day_when_it_is_not_today():
    assert rem.label(at(2026, 10, 14, 15), NOW) == "at 15:00"
    assert rem.label(at(2026, 10, 15, 9), NOW) == "tomorrow at 09:00"
    assert rem.label(at(2026, 10, 16, 10), NOW) == "on Friday at 10:00"
    assert rem.label(at(2026, 11, 2, 10), NOW) == "on 2 November at 10:00"
    assert rem.label(at(2026, 10, 13, 15), NOW) == "yesterday at 15:00"


# ----------------------------------------------------------------------------- whole sentences


@pytest.mark.parametrize("sentence, kind, text, due, why", [
    ("remind me in 20 minutes to stretch", "set", "stretch", "Wed 14:23:27", ""),
    ("Remind me at 15:00 to call the dentist.", "set", "call the dentist", "Wed 15:00:00", ""),
    ("remind me tomorrow at 9 to water the plants", "set", "water the plants", "Thu 09:00:00", ""),
    ("remind me to call my mum at 5", "set", "call my mum", "Wed 17:00:00", ""),
    ("can you remind me to stretch in 20 minutes please", "set", "stretch", "Wed 14:23:27", ""),
    ("in 20 minutes remind me to stretch", "set", "stretch", "Wed 14:23:27", ""),
    ("set a reminder for 15:00 to call the dentist", "set", "call the dentist", "Wed 15:00:00", ""),
    ("Strawberry, remind me in 1h30 to take the pizza out!", "set", "take the pizza out", "Wed 15:33:27", ""),
    ("remind me on Friday at 10 to pay the rent", "set", "pay the rent", "Fri 10:00:00", ""),
    # No time, or one that is not certain: she asks.
    ("remind me about that song", "set", "that song", "", "no_time"),
    ("remind me to read chapter 9", "set", "read chapter 9", "", "no_time"),           # a number is not a time
    ("remind me to book a table for 8", "set", "book a table for 8", "", "no_time"),
    ("remind me to put the washing in the dryer", "set", "put the washing in the dryer", "", "no_time"),
    ("remind me to call Mum tomorrow", "set", "call Mum", "", "no_time"),
    ("remind me in a few minutes to stretch", "set", "stretch", "", "no_time"),
    ("remind me at 1 to stretch", "set", "stretch", "", "past"),
])
def test_whole_sentences_to_set(sentence, kind, text, due, why):
    request = read(sentence, NOW)
    assert request is not None and request.kind == kind and request.text == text
    assert (hm(request.when.due) if request.when.ok else "", request.when.why) == (due, why)


@pytest.mark.parametrize("sentence, expected", [
    ("what reminders do I have", ("list", False, "")),
    ("list my reminders", ("list", False, "")),
    ("do I have any reminders?", ("list", False, "")),
    ("cancel the dentist reminder", ("cancel", False, "dentist")),
    ("cancel my reminders", ("cancel", True, "")),
    ("cancel all my reminders please", ("cancel", True, "")),
    ("cancel the reminder about the dentist", ("cancel", False, "the dentist")),
    ("cancel my 3 pm reminder", ("cancel", False, "3 pm")),
    ("cancel that reminder", ("cancel", False, "last")),
    ("remind me", None),                                       # nothing to go on: the thinker's
    ("remember that I like tea", None),
    ("I want you to remind me to call", None),
    ("what's the time", None),
    ("cancel the meeting", None),
])
def test_whole_sentences_to_list_and_cancel(sentence, expected):
    request = read(sentence, NOW)
    got = None if request is None else (request.kind, request.all, request.match)
    assert got == expected


def test_the_spoken_words_fold_the_person_and_the_persona_lines_are_shipped():
    assert rem.spoken("call my mum") == "call your mum" and rem.spoken("I am late at 9 am") == "you are late at 9 am"
    shipped = persona.shipped()
    assert shipped.variants("reminder_set") == (rem.SET_LINE,) and shipped.variants("reminder_due") == (rem.DUE_LINE,)
    assert shipped.variants("reminder_missed") == (rem.MISSED_LINE,)
    assert shipped.variants("reminder_ask_time")[0] == rem.ASK_LINE
    # A user's line without a placeholder is not used: the time and the words are what the line is for.
    assert rem._fill("Done!", rem.SET_LINE, when="at 15:00", text="x") == "Okay, at 15:00: x."
    assert rem._fill("Right, {when}, {text}.", rem.SET_LINE, when="at 15:00", text="x") == "Right, at 15:00, x."


# ----------------------------------------------------------------------------- the scheduler, on its own


def store(clock: Clock) -> History:
    history = History(HistoryConfig(), clock=clock)
    assert history.open()
    return history


def scheduler(clock: Clock, history: History | None) -> tuple[Reminders, list[tuple[str, str]]]:
    said: list[tuple[str, str]] = []

    async def say(text: str, about: str, foreign: bool = False) -> int:
        said.append((text, about))
        return 1

    reminders = Reminders(lambda: history, clock=clock)
    reminders.say = say
    reminders.started_at = clock.now
    return reminders, said


async def test_a_reminder_is_said_once_at_its_time_even_when_two_checks_race():
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    timer = await reminders.add(NOW + 60, "stretch")
    assert await reminders.tick() == 0 and said == []
    clock.now += 61
    told = await asyncio.gather(reminders.tick(), reminders.tick(), reminders.tick())
    assert sum(told) == 1 and said == [("Reminder: stretch.", 'a reminder went off: "stretch"')]
    assert history.timers("fired")[0]["id"] == timer and history.timers("pending") == []
    clock.now += 3600
    assert await reminders.tick() == 0 and len(said) == 1
    # A second scheduler on the same file (a race from elsewhere) cannot say it again either.
    other, again = scheduler(clock, history)
    assert await other.tick() == 0 and again == []


async def test_a_cancelled_or_missed_one_is_never_fired():
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    timer = await reminders.add(NOW + 60, "call the dentist")
    assert await reminders.cancel(timer) and not await reminders.cancel(timer)
    clock.now += 120
    assert await reminders.tick() == 0 and said == []
    assert not history.set_timer_state(timer, "fired", "pending")     # one statement: only from pending


async def test_one_cancelled_in_the_store_itself_is_never_fired_even_between_the_read_and_the_claim():
    """The History view cancels through the store (`set_timer_state(id, "cancelled")`), not through her: each look
    reads the table again, and the claim is pending -> fired in one statement, so a cancel that lands after the read
    still wins."""
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    first = await reminders.add(NOW + 60, "call the dentist")
    second = await reminders.add(NOW + 60, "stretch")
    assert await reminders.tick() == 0
    assert history.set_timer_state(first, "cancelled")                   # as the History view does
    real = history.timers

    def read_then_cancel(state=None):
        found = real(state)
        history.set_timer_state(second, "cancelled")                     # lands after the scheduler's read
        return found

    history.timers = read_then_cancel
    clock.now += 61
    assert await reminders.tick() == 0 and said == []
    history.timers = real
    assert {t["state"] for t in history.timers()} == {"cancelled"}


async def test_missed_while_she_was_off_are_told_once_in_one_line():
    clock = Clock(at(2026, 10, 14, 9))
    history = store(clock)
    history.add_timer(at(2026, 10, 14, 10), "call the dentist")
    history.add_timer(at(2026, 10, 14, 11, 30), "stretch")
    history.add_timer(at(2026, 10, 14, 18), "cook")
    clock.now = NOW                                   # she was off from 09:00 to 14:03
    reminders, said = scheduler(clock, history)
    assert await reminders.tick() == 2
    assert said[0][0] == "While I was off: at 10:00, call the dentist; at 11:30, stretch."
    assert [t["text"] for t in history.timers("missed")] == ["call the dentist", "stretch"]
    assert await reminders.tick() == 0 and len(said) == 1           # told once
    assert [t["text"] for t in history.timers("pending")] == ["cook"]


async def test_one_seen_long_after_its_time_is_told_as_missed():
    """A suspend: the monotonic wait stopped, the wall clock went on. Over five minutes late, it is "while I was off"."""
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    await reminders.add(NOW + 60, "stretch")
    clock.now += 3 * 3600
    assert await reminders.tick() == 1 and said[0][0] == "While I was off: at 14:04, stretch."
    assert history.timers("missed")[0]["text"] == "stretch"


async def test_during_a_run_it_waits_until_the_run_ends_or_sixty_seconds():
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    ticks = [0.0]
    reminders.monotonic = lambda: ticks[0]
    busy = [True]
    reminders.busy = lambda: busy[0]
    await reminders.add(NOW + 10, "stretch")
    await reminders.add(NOW + 20, "drink water")
    clock.now += 30
    assert await reminders.tick() == 0 and said == [] and reminders.delay() == 1.0
    ticks[0] = 30.0
    assert await reminders.tick() == 0
    busy[0] = False                                   # the run ended
    assert await reminders.tick() == 2 and said[0][0] == "Reminder: stretch. Reminder: drink water."
    await reminders.add(clock.now + 10, "cook")
    busy[0] = True
    clock.now += 20
    assert await reminders.tick() == 0
    ticks[0] = 91.0                                   # still busy, 61 s later: said anyway
    assert await reminders.tick() == 1 and said[-1][0] == "Reminder: cook."


async def test_with_no_body_connected_it_waits_for_one():
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    bodies = [0]
    reminders.bodies = lambda: bodies[0]
    await reminders.add(NOW + 10, "stretch")
    clock.now += 20
    assert await reminders.tick() == 0 and reminders.delay() == 2.0
    bodies[0] = 1
    assert await reminders.tick() == 1


async def test_the_loop_reads_the_wall_clock_and_a_poke_wakes_it():
    clock = Clock(NOW)
    history = store(clock)
    reminders, said = scheduler(clock, history)
    await reminders.add(NOW + 3600, "stretch")
    reminders.start()
    try:
        await asyncio.sleep(0.05)
        assert said == [] and reminders.delay() == rem.CHECK_S
        clock.now += 3601                              # a clock jump (or a resume): the next look finds it
        reminders.poke()
        await until(lambda: said)
        assert said[0][0] == "Reminder: stretch."
    finally:
        await reminders.close()


async def test_without_her_history_reminders_are_unavailable():
    reminders, said = scheduler(Clock(NOW), None)
    assert not reminders.available and await reminders.tick() == 0
    with pytest.raises(rem.Unavailable):
        await reminders.pending()
    session = rem.RemindersSession(reminders)
    result = await session.call_tool("set_reminder", {"due": "in 20 minutes", "text": "stretch"})
    assert result["isError"] and "history is switched off" in result["content"][0]["text"]


# ----------------------------------------------------------------------------- in the daemon


async def remind_daemon(script: list | None = None, routes: dict | None = None, history: bool = True):
    config = plain_config()
    config.actions.mpris = False
    config.history.enabled = history
    toolbox, qwen, thinker = bare(script or [])
    daemon, sink = voice_daemon(config, toolbox, thinker, gate=ScriptedGate(routes or {}))
    clock = Clock(NOW)
    daemon.reminders.clock = clock
    if daemon.history is not None:
        daemon.history.clock = clock
    await daemon.start()
    await daemon.reminders.close()                    # the tests drive the scheduler by hand
    daemon.reminders.started_at = clock.now
    return daemon, sink, qwen, clock


async def say(daemon: Daemon, text: str) -> str:
    performance, _ = await daemon.handle_voice(Event(source="voice", title=text))
    return performance.text or ""


def lines(sink) -> list[str]:
    return [m.get("text", "") for m in sink.got if m.get("text")]


async def test_set_list_and_cancel_by_whole_sentences_without_a_model():
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        assert (await say(daemon, "Remind me at 15:00 to call the dentist.")).startswith(
            "Okay, at 15:00: call the dentist.")
        assert (await say(daemon, "remind me tomorrow at 9 to water my plants")).startswith(
            "Okay, tomorrow at 09:00: water your plants.")
        assert (await say(daemon, "what reminders do I have")).startswith(
            "You have 2 reminders: at 15:00, call the dentist; tomorrow at 09:00, water your plants.")
        assert (await say(daemon, "cancel the dentist reminder")).startswith(
            "Cancelled the reminder at 15:00: call the dentist.")
        assert [t["text"] for t in daemon.history.timers("pending")] == ["water my plants"]
        assert (await say(daemon, "cancel the bins reminder")).startswith("I don't have a reminder like that.")
        assert (await say(daemon, "cancel my reminders")).startswith("Cancelled your reminder.")
        assert daemon.history.timers("pending") == [] and qwen.payloads == []          # no model in the loop
        # The timeline has each as a turn, as for any other reflex.
        assert [t["said"] for t in daemon.ledger.to_list()][:2] == ["Remind me at 15:00 to call the dentist.",
                                                                    "remind me tomorrow at 9 to water my plants"]
    finally:
        await daemon.close()


async def test_no_time_she_asks_and_the_answer_alone_sets_it():
    daemon, sink, qwen, clock = await remind_daemon(routes={"at 16:00": reading("at 16:00", kind="other"),
                                                            "in the evening": reading("in the evening", kind="other")})
    try:
        assert await say(daemon, "remind me about that song") == "When should I remind you?"
        assert daemon.history.timers("pending") == []
        # The gate reads a bare time as a fragment; her question is out, so it is read all the same.
        assert (await say(daemon, "at 16:00")).startswith("Okay, at 16:00: that song.")
        assert daemon.history.timers("pending")[0]["text"] == "that song"
        assert await say(daemon, "remind me tomorrow at 6 to stretch") == "6 in the morning or in the evening?"
        assert (await say(daemon, "in the evening")).startswith("Okay, tomorrow at 18:00: stretch.")
        assert (await say(daemon, "remind me at 1 to stretch")).startswith("1:00 has already gone today.")
        assert qwen.payloads == []
    finally:
        await daemon.close()


async def test_a_reminder_fires_into_her_bubble_marked_fired_and_into_the_timeline():
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        await say(daemon, "remind me in 20 minutes to stretch")
        clock.now += 20 * 60 + 1
        assert await daemon.reminders.tick() == 1
        fired = [m for m in sink.got if m.get("text") == "Reminder: stretch."]
        assert fired and fired[0]["state"] == "talking" and fired[0]["emotion"] == "alert"
        assert daemon.history.timers("fired")[0]["text"] == "stretch"
        notice = daemon.ledger.recent_notices()[-1]
        assert notice.about == 'a reminder went off: "stretch"' and not notice.foreign
    finally:
        await daemon.close()


async def test_asleep_she_is_sent_the_line_like_any_other_and_her_widget_wakes_for_it():
    """She sleeps in the widget only (sleep_controller.gd): any performance with a line wakes her and plays after the
    stand-up (widget/validate_sleep.gd checks that). The daemon sends the reminder whatever the tray said."""
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        await daemon.hub.send({"command": "sleep_now"})
        await say(daemon, "remind me in 1 minute to stretch")
        clock.now += 61
        assert await daemon.reminders.tick() == 1
        assert sink.got[-1]["state"] == "talking" and sink.got[-1]["text"] == "Reminder: stretch."
    finally:
        await daemon.close()


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, text: str) -> str:
        self.said.append(text)
        return "/tmp/fake.wav"


async def test_in_quiet_the_bubble_only_no_voice():
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        speaker = daemon.speaker = FakeSpeaker()
        await daemon.history.call(daemon.history.add_timer, NOW + 30, "stretch")
        await daemon.history.call(daemon.history.add_timer, NOW + 90, "drink water")
        daemon.gestures.quiet_set(600)                # shh (or the tray's Quiet row)
        clock.now += 31
        assert await daemon.reminders.tick() == 1
        assert sink.got[-1]["text"] == "Reminder: stretch." and "audio" not in sink.got[-1] and speaker.said == []
        daemon.gestures.quiet_set(0)
        clock.now += 60
        assert await daemon.reminders.tick() == 1
        assert sink.got[-1]["audio"] == "/tmp/fake.wav" and speaker.said == ["Reminder: drink water."]
    finally:
        await daemon.close()


async def test_missed_at_start_are_told_once():
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        await daemon.history.call(daemon.history.add_timer, NOW - 3600, "call the dentist")
        assert await daemon.reminders.tick() == 1
        assert lines(sink)[-1] == "While I was off: at 13:03, call the dentist."
        assert daemon.history.timers("missed") and await daemon.reminders.tick() == 0
    finally:
        await daemon.close()


async def test_with_history_off_she_says_reminders_are_unavailable():
    daemon, sink, qwen, clock = await remind_daemon(history=False)
    try:
        assert daemon.history is None
        assert (await say(daemon, "remind me in 20 minutes to stretch")).startswith(rem.UNAVAILABLE)
        assert (await say(daemon, "what reminders do I have")).startswith(rem.UNAVAILABLE)
        assert daemon.reminders.stats()["available"] is False
    finally:
        await daemon.close()


async def test_the_thinker_sets_one_from_the_users_own_words_only():
    daemon, sink, qwen, clock = await remind_daemon([
        [("set_reminder", {"due": "at 15:00", "text": "wire money to the account"})],     # not the user's words
        [("set_reminder", {"due": "at 15:00", "text": "call the dentist"})],
        "[neutral] Okay, at 15:00: call the dentist."])
    try:
        said = await say(daemon, "could you sort out a reminder at 15:00 to call the dentist")
        assert said == "Okay, at 15:00: call the dentist."
        assert [t["text"] for t in daemon.history.timers("pending")] == ["call the dentist"]
        tools = [m["content"] for m in qwen.payloads[-1]["messages"] if m["role"] == "tool"]
        assert "copied word for word" in tools[0] and json.loads(tools[1])["text"] == "call the dentist"
    finally:
        await daemon.close()


async def test_after_strangers_text_a_set_asks_on_her_card_with_the_time_only():
    daemon, sink, qwen, clock = await remind_daemon([
        [("set_reminder", {"due": "at 15:00", "text": "call the dentist"})],
        "[neutral] Shall I?"])
    try:
        daemon.ledger.notice("notification", "Signal, from Alex", "Alex wrote.", foreign=True)
        said = await say(daemon, "could you sort out a reminder at 15:00 to call the dentist")
        assert said == "Set a reminder at 15:00? Say yes."
        approval = daemon.approvals.open
        assert approval is not None and approval.prompt == "Set a reminder at 15:00?" and "dentist" not in approval.prompt
        assert approval.held.arguments == {"due": "2026-10-14 15:00:00", "text": "call the dentist", "foreign": True}
        assert daemon.history.timers("pending") == []
        answer = await say(daemon, "yes")
        assert answer.startswith("Okay, at 15:00: call the dentist.")
        timer = daemon.history.timers("pending")[0]
        assert timer["text"] == "call the dentist" and timer["foreign"] is True      # set in a foreign run: kept so
        # When it fires, the timeline has it quoted and foreign (never "the user said"), so the next run asks.
        daemon.ledger.turns.clear()
        daemon.ledger.notices.clear()
        clock.now = at(2026, 10, 14, 15, 0, 1)
        assert await daemon.reminders.tick() == 1
        notice = daemon.ledger.recent_notices()[-1]
        assert notice.foreign and notice.about == ('a reminder went off (set after text others wrote): '
                                                   '"call the dentist"')
        assert "the user said" not in notice.text(0) and daemon.ledger.timeline()[1] is True
    finally:
        await daemon.close()


async def test_a_fired_reminder_is_said_never_acted_on():
    """"Remind me at 3 to play Daft Punk" fires as words only: no route, no reflex, no thinker, no tool call."""
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        await say(daemon, "remind me in 1 minute to play daft punk and skip this song")
        acted: list = []

        async def never(*args, **kwargs):
            acted.append(args)
            raise AssertionError("a reminder's words were acted on")

        daemon.actor.act = never
        daemon.thinker.run = never
        daemon.gate.route = never
        calls = daemon.toolbox.servers["reminders"].stats().get("calls")
        clock.now += 61
        assert await daemon.reminders.tick() == 1
        assert sink.got[-1]["text"] == "Reminder: play daft punk and skip this song." and acted == []
        assert daemon.toolbox.servers["reminders"].stats().get("calls") == calls and qwen.payloads == []
        assert [e["kind"] for e in daemon.ledger.to_list(notices=True)][-1] == "notice"   # not a turn of the user's
    finally:
        await daemon.close()


async def test_a_socket_without_the_bus_secret_never_sees_a_reminders_words():
    """Untrusted sockets get the shape only (hub.shape, PROTOCOL §1.4): the run events, step chips and the fired
    line reach them without a word of the reminder; only the trusted body gets the bubble's text."""
    from tests.test_voice import Sink

    daemon, sink, qwen, clock = await remind_daemon()
    bare_socket, v2_socket = Sink(), Sink()
    daemon.hub.add(bare_socket)                                   # no hello at all
    daemon.hub.add(v2_socket)
    daemon.hub.hello(v2_socket, {"type": "hello", "protocol": 2, "capabilities": {
        "speech": {"bubble": True}, "phases": ["routing", "thinking", "tool", "speaking", "run"], "approvals": True}})
    try:
        await say(daemon, f"remind me in 1 minute to feed the {CANARY}")
        await say(daemon, "what reminders do I have")
        await daemon.history.call(daemon.history.add_timer, NOW - 3600, f"water the {CANARY}")
        clock.now += 61
        assert await daemon.reminders.tick() == 2
        await say(daemon, f"remind me in 5 minutes to brush the {CANARY}")
        await say(daemon, f"cancel the {CANARY} reminder")
        assert any(CANARY in json.dumps(m) for m in sink.got)              # the trusted body has the bubble
        assert bare_socket.got and v2_socket.got
        assert CANARY not in json.dumps(bare_socket.got) and CANARY not in json.dumps(v2_socket.got)
        assert not [m for m in bare_socket.got + v2_socket.got if "text" in m]
    finally:
        await daemon.close()


async def test_the_server_is_private_offered_only_when_asked_and_its_tiers():
    daemon, sink, qwen, clock = await remind_daemon()
    try:
        toolbox = daemon.toolbox
        assert toolbox.flags("reminders") == {"private"}
        assert [toolbox.risk("reminders", t) for t in ("set_reminder", "cancel_reminder", "list_reminders")] == [
            "change", "change", "read"]
        assert "reminders" not in daemon.thinker.chosen("play some jazz", "music")
        assert "reminders" in daemon.thinker.chosen("could you set up a reminder", "other",
                                                    first=frozenset({"reminders"}))
        adapter = toolbox.adapters["reminders"]
        assert adapter.wanted("what reminders are there", None) is True and adapter.wanted("hello", None) is None
    finally:
        await daemon.close()


async def test_nothing_goes_through_notify_send_or_any_other_program(monkeypatch):
    ran: list = []

    async def no_exec(*args, **kwargs):
        ran.append(args)
        raise AssertionError("a program was started")

    def no_popen(*args, **kwargs):
        ran.append(args)
        raise AssertionError("a program was started")

    daemon, sink, qwen, clock = await remind_daemon()
    try:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_shell", no_exec)
        monkeypatch.setattr(subprocess, "Popen", no_popen)
        await say(daemon, "remind me in 1 minute to stretch")
        await daemon.history.call(daemon.history.add_timer, NOW - 3600, "call the dentist")
        clock.now += 61
        assert await daemon.reminders.tick() == 2
        assert ran == []
    finally:
        await daemon.close()
    source = Path(rem.__file__).read_text(encoding="utf-8")
    assert "notify" not in source and "subprocess" not in source


@pytest.mark.parametrize("log_sentences", [False, True])
async def test_no_reminder_text_reaches_a_log_or_health(aiohttp_client, caplog, log_sentences):
    """A canary in a reminder's words, set every way there is (a whole sentence, the answer to her "when?", the
    thinker's call), listed, fired, missed and cancelled: never in a log record at any level, whatever log_sentences
    says, nor in /health but its timeline."""
    from strawberry_crab import logtext

    caplog.set_level(logging.DEBUG)
    daemon, sink, qwen, clock = await remind_daemon(
        [[("set_reminder", {"due": "at 23:00", "text": f"walk the {CANARY}"})], f"[neutral] Okay: walk the {CANARY}."],
        routes={"at 22:00": reading("at 22:00", kind="other")})
    logtext.configure(log_sentences)
    try:
        client = await aiohttp_client(create_app(daemon))
        await say(daemon, f"remind me in 20 minutes to feed the {CANARY}")
        await say(daemon, f"remind me about the {CANARY}")
        assert (await say(daemon, "at 22:00")).startswith(f"Okay, at 22:00: the {CANARY}.")
        # The reminders' own turns are not strangers' text: no question here.
        assert (await say(daemon, f"could you sort out a reminder at 23:00 to walk the {CANARY}")).endswith(
            CANARY + ".")
        assert not [e for e in daemon.ledger.to_list() if e["foreign"]]
        await say(daemon, "what reminders do I have")
        await daemon.history.call(daemon.history.add_timer, NOW - 3600, f"water the {CANARY}")
        clock.now += 20 * 60 + 1
        assert await daemon.reminders.tick() == 2
        assert any(CANARY in line for line in lines(sink))            # she said them…
        assert (await say(daemon, f"cancel the {CANARY} reminder")).startswith("Which one?")
        await say(daemon, "cancel my reminders")
        health = await (await client.get("/health")).json()
        assert health["reminders"]["fired"] == 1 and health["reminders"]["missed"] == 1
        assert health["reminders"]["set"] == 3 and health["reminders"]["cancelled"] == 2
        # …and nothing kept them anywhere else: /health has counts (its `ledger` is the timeline, the user's
        # sentences as for any other exchange), and no log record has a word of them.
        assert CANARY not in json.dumps({k: v for k, v in health.items() if k != "ledger"})
        assert not [r for r in caplog.records if CANARY in r.getMessage()]
    finally:
        logtext.configure(False)
        await daemon.close()
