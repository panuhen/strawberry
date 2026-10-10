"""The bar and the section (beat_structure.py) on generated music: the downbeat where the bar starts,
three beats a bar for a waltz, and a break, a build and a drop where the arrangement has them.

Everything is synthesised with numpy (scripts/beat_eval.py's instruments): no recording, no microphone,
nothing written to disk.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from strawberry_crab.doorways import beat_structure
from strawberry_crab.doorways.beat_track import BeatTracker

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "beat_eval.py"
START = 1000.0       # wall-clock time of the first sample


@pytest.fixture(scope="module")
def synth():
    spec = importlib.util.spec_from_file_location("beat_eval_synth", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["beat_eval_synth"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("beat_eval_synth", None)


def follow(audio: np.ndarray, sr: int, every: float = 2.0):
    """Feed it as beat_watch does (2048-sample chunks); an estimate every `every` s, and each section change
    as it happens: (estimates [(t, Tempo)], changes [(t, name, since)]), times from the start."""
    tracker = BeatTracker(sample_rate=sr)
    estimates, changes, due, epoch = [], [], every, 0
    for i in range(0, len(audio), 2048):
        piece = audio[i:i + 2048]
        t = (i + len(piece)) / sr
        tracker.feed(piece, START + t)
        if tracker.sections.epoch != epoch:
            epoch = tracker.sections.epoch
            changes.append((t, tracker.sections.name, tracker.sections.since - START))
        if t >= due:
            due = t + every
            estimate = tracker.estimate(START + t)
            if estimate is not None:
                estimates.append((t, estimate))
    return estimates, changes


def off_grid(time: float, grid: list[float]) -> float:
    return float(np.min(np.abs(np.asarray(grid) - time)))


# ----------------------------------------------------------------------------- the bar


def test_the_downbeat_is_where_the_chords_change(synth):
    """Four-on-the-floor under pads that change chord each bar: every estimate after the first few puts
    the next downbeat on a bar line, sure of it."""
    rng = np.random.default_rng(4)
    seconds = 30.0
    beats = synth.beat_grid(124, seconds)
    audio = synth.drum_track(rng, beats, seconds, "four") + synth.pads(rng, beats, seconds, 0.8)
    estimates, _ = follow(synth.normalise(audio + synth.pink(rng, len(audio)) * 0.01), synth.SR)
    settled = [e for t, e in estimates if t > 8.0]
    assert settled and all(e.beats_per_bar == 4 for e in settled)
    for e in settled:
        assert off_grid(e.next_downbeat - START, beats[::4]) < 0.07, e
        assert 0 <= e.beat_index < 4 and e.downbeat_confidence >= 0.5
        assert e.next_downbeat - e.next_beat == pytest.approx(((4 - e.beat_index) % 4) * e.period_s, abs=1e-6)


def test_a_bass_line_that_moves_each_bar_marks_the_downbeat(synth):
    rng = np.random.default_rng(1)
    seconds = 30.0
    beats = synth.beat_grid(128, seconds)
    audio = synth.drum_track(rng, beats, seconds, "four", bass="offbeat")
    estimates, _ = follow(synth.normalise(audio + synth.pink(rng, len(audio)) * 0.01), synth.SR)
    settled = [e for t, e in estimates if t > 6.0]
    assert sum(off_grid(e.next_downbeat - START, beats[::4]) < 0.07 for e in settled) >= len(settled) - 1


def test_a_waltz_has_three_beats_a_bar(synth):
    """Kick and a bass note on one, light hits on two and three."""
    rng = np.random.default_rng(2)
    seconds, bpm = 24.0, 150
    beats = synth.beat_grid(bpm, seconds)
    out = np.zeros(int(seconds * synth.SR), dtype=np.float32)
    kick, hat = synth.kick(rng), synth.hat(rng)
    notes = [55.0, 65.4, 49.0, 73.4]
    for i, b in enumerate(beats):
        if i % 3 == 0:
            synth.place(out, kick, b)
            synth.place(out, synth.tone(notes[(i // 3) % 4], 60 / bpm * 2.8, saw=True, decay=1.0), b, 0.3)
        else:
            synth.place(out, hat, b, 2.0)
            synth.place(out, synth.snare(rng), b, 0.3)
    estimates, _ = follow(synth.normalise(out + synth.pink(rng, len(out)) * 0.01), synth.SR)
    settled = [e for t, e in estimates if t > 6.0]
    assert settled and all(e.beats_per_bar == 3 for e in settled)
    assert all(off_grid(e.next_downbeat - START, beats[::3]) < 0.07 and e.downbeat_confidence >= 0.6 for e in settled)


def test_four_beats_a_bar_unless_three_clearly_fit(synth):
    """A backbeat alone (snare on two and four) fits three as badly as four: it stays four."""
    rng = np.random.default_rng(1)
    seconds = 30.0
    for pattern in ("four", "rock", "hiphop"):
        beats = synth.beat_grid(124, seconds)
        audio = synth.drum_track(rng, beats, seconds, pattern)
        estimates, _ = follow(synth.normalise(audio + synth.pink(rng, len(audio)) * 0.01), synth.SR)
        assert all(e.beats_per_bar == 4 for _, e in estimates), pattern


def test_noise_has_no_downbeat_to_speak_of(synth):
    rng = np.random.default_rng(3)
    n = int(24 * synth.SR)
    estimates, _ = follow(synth.normalise(synth.pink(rng, n) + rng.standard_normal(n).astype(np.float32) * 0.2),
                          synth.SR)
    assert estimates and all(e.downbeat_confidence < 0.2 for _, e in estimates)


def test_bars_carry_their_votes_along_the_grid():
    """The same evidence two beats later lands on the same bar position."""
    bars = beat_structure.Bars()
    accents = np.array([np.nan, 0.0, 0.0, 3.0, 0.0, 0.0, 0.0, 3.0, 0.0, 0.0, 0.0, 3.0])   # a bar starts 3 beats back
    bars.update(accents, last_beat=100.0, period=0.5)
    first = bars.position(100.5, 0.5)
    assert first[:3] == (4, 0, pytest.approx(100.5))       # bars at 98.5, 100.5, ...: the next beat is a downbeat
    shifted = np.roll(accents, 2)
    shifted[:2] = np.nan
    bars.update(shifted, last_beat=101.0, period=0.5)      # two beats on, the same music
    second = bars.position(101.5, 0.5)
    assert second[:3] == (4, 2, pytest.approx(102.5)) and second[3] > first[3] > 0.0


# ----------------------------------------------------------------------------- the section


def edm(synth, seed: int = 3, bpm: float = 128.0, full: float = 16.0, brk: float = 8.0, build: float = 8.0,
        after: float = 14.0, pause_beats: int = 0, fade_in: bool = False):
    """Full (drums and bass over pads), a break (the pads alone), a build (an accelerating snare roll and a
    high-passed noise riser over the break), then the full mix again: a drop, or with `fade_in` drums and
    bass coming back over six seconds. Returns the audio, the beats and the times the sections start (the
    drop's at its first kick)."""
    rng = np.random.default_rng(seed)
    total = full + brk + build + after
    beats = synth.beat_grid(bpm, total)
    period = 60.0 / bpm
    quiet = (full, full + brk + build + pause_beats * period)
    band = synth.drum_track(rng, beats, total, "four")
    notes = [55.0, 55.0, 65.4, 49.0]
    for i, b in enumerate(beats[:-1]):
        synth.place(band, synth.tone(notes[(i // 4) % 4], period * 0.9, saw=True, decay=2.0), b, 0.35)
    t = np.arange(len(band)) / synth.SR
    back = next(b for b in beats if b >= quiet[1]) - 0.01     # just before the first beat after the quiet
    level = np.where(t < quiet[0], 1.0, np.where(t < back, 0.0, np.clip((t - back) / 6.0, 0, 1) if fade_in else 1.0))
    out = band * level.astype(np.float32) + synth.pads(rng, beats, total, 0.5)
    start = full + brk
    snare, t = synth.snare(rng), start
    while t < start + build:
        share = (t - start) / build
        synth.place(out, snare, t, 0.3 + 0.7 * share)
        t += period if share < 0.5 else period / 2 if share < 0.75 else period / 4
    n = int(build * synth.SR)
    riser = np.diff(np.concatenate([[0.0], rng.standard_normal(n)])).astype(np.float32) * np.linspace(0, 1, n) ** 2
    out[int(start * synth.SR):int(start * synth.SR) + n] += riser * 0.25
    if pause_beats:
        out[int((start + build) * synth.SR):int(back * synth.SR)] = 0.0
    out += synth.pink(rng, len(out)) * 0.005
    drop = next(b for b in beats if b >= quiet[1])
    return synth.normalise(out), beats, {"break": full, "build": start, "drop": drop}


def test_a_break_a_build_and_a_drop(synth):
    audio, beats, at = edm(synth)
    estimates, changes = follow(audio, synth.SR)
    names = [name for _, name, _ in changes]
    assert names == ["break", "build", "drop", "steady"], changes
    found = {name: (t, since) for t, name, since in changes}
    # The break: within three seconds, dated to where the low end went.
    assert found["break"][0] - at["break"] < 3.0 and abs(found["break"][1] - at["break"]) < 0.6
    # The build: within four seconds of the riser's start, dated roughly there.
    assert found["build"][0] - at["build"] < 4.0 and abs(found["build"][1] - at["build"]) < 2.5
    # The drop: within a second of its first kick; the estimates date it to that beat.
    assert 0.0 < found["drop"][0] - at["drop"] < 1.0
    dropped = [e for t, e in estimates if e.section == "drop"]
    assert dropped and all(abs(e.section_since - START - at["drop"]) < 0.06 for e in dropped)
    assert all(e.section_confidence >= 0.5 for e in dropped)
    # Held for eight seconds, then steady again.
    assert found["steady"][0] - found["drop"][0] == pytest.approx(beat_structure.DROP_HOLD_S, abs=0.5)
    # Before the break, steady all along; every estimate carries the section.
    assert all(e.section == "steady" for t, e in estimates if t < at["break"])
    assert all(e.section in beat_structure.SECTIONS and 0.0 <= e.section_confidence <= 1.0 for _, e in estimates)


def test_a_beat_of_silence_before_the_drop_is_still_a_drop(synth):
    audio, beats, at = edm(synth, seed=5, pause_beats=2)
    _, changes = follow(audio, synth.SR)
    drops = [(t, since) for t, name, since in changes if name == "drop"]
    assert len(drops) == 1 and 0.0 < drops[0][0] - at["drop"] < 1.0


def test_a_bass_that_fades_back_in_is_no_drop(synth):
    audio, beats, at = edm(synth, seed=6, build=0.0, brk=10.0, fade_in=True)
    _, changes = follow(audio, synth.SR)
    names = [name for _, name, _ in changes]
    assert "drop" not in names and names[0] == "break" and names[-1] == "steady", changes


def test_a_steady_track_stays_steady(synth):
    rng = np.random.default_rng(9)
    for pattern, bpm in (("four", 132), ("rock", 120), ("hiphop", 90)):
        beats = synth.beat_grid(bpm, 40.0)
        audio = synth.drum_track(rng, beats, 40.0, pattern, bass="root", humanize_ms=3)
        estimates, changes = follow(synth.normalise(audio + synth.pink(rng, len(audio)) * 0.01), synth.SR)
        assert changes == [], (pattern, changes)
        assert all(e.section == "steady" for _, e in estimates)


def test_noise_and_silence_claim_no_section(synth):
    rng = np.random.default_rng(3)
    n = int(30 * synth.SR)
    noise = synth.normalise(synth.pink(rng, n) + rng.standard_normal(n).astype(np.float32) * 0.2)
    noise[int(10 * synth.SR):int(14 * synth.SR)] = 0.0       # a gap of silence in it
    _, changes = follow(noise, synth.SR)
    assert changes == []


def test_the_first_seconds_are_steady_with_no_confidence():
    sections = beat_structure.Sections(fps=43.07)
    times = np.arange(200) / 43.07
    sections.feed(times, np.full(200, 1e-2), np.full(200, 1e-3), np.full(200, 1e-4), np.full(200, 0.5))
    assert sections.name == "steady" and sections.confidence == 0.0 and sections.epoch == 0
