#!/usr/bin/env python3
"""Offline evaluation of the beat tracker (WIRING.md §4c).

Runs `beat_track.BeatTracker` over wav files the way `beat_watch` does live (2048-sample
chunks at 22050 Hz, one estimate every 2 s) and scores each estimate against the true beat
grid:

    acc1     estimates within 4 % of the true tempo (MIREX "Accuracy 1")
    octave   estimates within 4 % of half or double the true tempo (Acc2 minus Acc1)
    other    wrong by any other factor (3/2, 2/3, or just lost)
    lock     seconds from the start of a beat section to the first correct estimate that
             the next two estimates confirm (a section that never locks counts as its length)
    jitter   mean |change| of the reported BPM between consecutive estimates while locked
    phase    share of locked estimates whose next_beat lands within 70 ms of a true beat
    jumps    share of consecutive locked estimates whose beat grid moved by more than 0.2 of
             a period from where the previous estimate put it (she visibly stumbles)
    beatless share of estimates over silence, noise or beatless pads that claim a beat
             (confidence >= 0.3, or steady when the tracker reports it)
    cpu      process CPU seconds per second of audio (feed + estimate)

The synthetic test set is generated, never recorded: click tracks and kick/snare/hat/bass
patterns from 70 to 175 BPM with swing, humanised timing, tempo changes, silence gaps,
breakdowns, loud pads and noise. Only this generator is committed; `gen` writes the set.

    scripts/beat_eval.py gen DIR                      write DIR/*.wav + DIR/manifest.json
    scripts/beat_eval.py run DIR [--tracker FILE] [--json OUT] [--verbose]
    scripts/beat_eval.py run --synthetic              generate in memory and score
    scripts/beat_eval.py run song.wav --bpm 123       a real capture with a known tempo

`--tracker` loads a different beat_track.py (e.g. `git show HEAD~1:src/.../beat_track.py`)
so two versions can be compared on the same set.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SR = 22050
CHUNK = 2048
INTERVAL_S = 2.0
SILENT_DB = -60.0          # beat_watch.SILENT_DB: quieter estimates are posted as {"silent": true}
TOL = 0.04
PHASE_TOL_S = 0.07
CLAIM_CONF = 0.3           # dance_style.gd sways below this confidence


# --------------------------------------------------------------------------- synthesis

def _env(n: int, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.exp(-t * decay)


def kick(rng: np.random.Generator) -> np.ndarray:
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    freq = 45.0 + 90.0 * np.exp(-t * 30.0)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * _env(n, 11.0)
    click = rng.standard_normal(n) * _env(n, 400.0) * 0.3
    return (body + click).astype(np.float32)


def snare(rng: np.random.Generator) -> np.ndarray:
    n = int(0.2 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    noise = noise - np.concatenate([[0.0], noise[:-1]]) * 0.6
    tone = np.sin(2 * np.pi * 190.0 * t) * _env(n, 30.0)
    return ((noise * _env(n, 22.0) * 0.5 + tone * 0.5) * 0.8).astype(np.float32)


def hat(rng: np.random.Generator, open_: bool = False) -> np.ndarray:
    n = int((0.25 if open_ else 0.05) * SR)
    noise = rng.standard_normal(n)
    noise = np.diff(np.concatenate([[0.0], noise]))   # crude high-pass
    return (noise * _env(n, 12.0 if open_ else 90.0) * 0.12).astype(np.float32)


def click(freq: float) -> np.ndarray:
    n = int(0.03 * SR)
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * freq * t) * _env(n, 150.0) * 0.6).astype(np.float32)


def tone(freq: float, seconds: float, saw: bool = False, attack: float = 0.005, decay: float = 3.0) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    if saw:
        wave_ = sum(np.sin(2 * np.pi * freq * k * t) / k for k in range(1, 6))
    else:
        wave_ = np.sin(2 * np.pi * freq * t)
    envelope = np.minimum(1.0, t / attack) * np.exp(-t * decay)
    release = np.minimum(1.0, (seconds - t) / 0.02)
    return (wave_ * envelope * release).astype(np.float32)


def pink(rng: np.random.Generator, n: int) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[1:] /= np.sqrt(f[1:])
    spectrum[0] = 0
    out = np.fft.irfft(spectrum, n)
    return (out / (np.abs(out).max() + 1e-9)).astype(np.float32)


def place(out: np.ndarray, sound: np.ndarray, t: float, gain: float = 1.0) -> None:
    i = int(round(t * SR))
    if i < 0 or i >= len(out):
        return
    m = min(len(sound), len(out) - i)
    out[i:i + m] += sound[:m] * gain


@dataclass
class Clip:
    name: str
    audio: np.ndarray
    beats: list[float]                               # true beat times (s)
    sections: list[tuple[float, float]]              # (start, end) of each stretch with a beat
    kind: str = "beat"                               # "beat" or "beatless"
    tags: list[str] = field(default_factory=list)


def beat_grid(tempo, seconds: float, start: float = 0.0) -> list[float]:
    """Beat times from a tempo function bpm(t) (or a constant)."""
    f = tempo if callable(tempo) else (lambda _t: float(tempo))
    out, t = [], start
    while t < seconds:
        out.append(t)
        t += 60.0 / f(t)
    return out


# A pattern is {instrument: [(step, gain), ...]} on a 16-step bar (four beats).
PATTERNS = {
    "four": {"kick": [(0, 1), (4, 1), (8, 1), (12, 1)], "hat": [(2, .9), (6, .9), (10, .9), (14, .9)],
             "snare": [(4, .5), (12, .5)]},
    "rock": {"kick": [(0, 1), (8, 1), (10, .6)], "snare": [(4, 1), (12, 1)],
             "hat": [(s, .8 if s % 4 == 0 else .6) for s in range(0, 16, 2)]},
    "hiphop": {"kick": [(0, 1), (3, .7), (7, .5), (10, .9)], "snare": [(4, 1), (12, 1)],
               "hat": [(s, .7 if s % 2 == 0 else .4) for s in range(16)]},
    "funk": {"kick": [(0, 1), (3, .6), (6, .7), (10, .8), (11, .5)], "snare": [(4, 1), (12, 1), (15, .3)],
             "hat": [(s, .6 if s % 2 == 0 else .35) for s in range(16)]},
    "halftime": {"kick": [(0, 1), (6, .7), (11, .5)], "snare": [(8, 1)],
                 "hat": [(s, .6 if s % 2 == 0 else .4) for s in range(16)]},
    "dnb": {"kick": [(0, 1), (10, .9)], "snare": [(4, 1), (12, 1), (7, .3)],
            "hat": [(s, .5) for s in range(0, 16, 2)]},
    "onedrop": {"kick": [(8, 1)], "snare": [(8, .7)], "hat": [(s, .5) for s in range(0, 16, 2)]},
    "jazz": {"hat": [(4, .6), (12, .6)], "ride": [(0, .7), (4, .7), (7, .5), (8, .7), (12, .7), (15, .5)],
             "kick": [(0, .25), (4, .25), (8, .25), (12, .25)]},
    "hatsonly": {"hat": [(s, .6 if s % 2 == 0 else .35) for s in range(16)]},
}


def drum_track(rng: np.random.Generator, beats: list[float], seconds: float, pattern: str, swing: float = 0.0,
               humanize_ms: float = 0.0, gains: dict | None = None, mute: list[tuple[float, float]] | None = None,
               bass: str = "", waltz: bool = False) -> np.ndarray:
    """Render `pattern` over the beat grid. swing: delay of the odd 16ths as a share of a 16th."""
    out = np.zeros(int(seconds * SR), dtype=np.float32)
    sounds = {"kick": kick(rng), "snare": snare(rng), "hat": hat(rng), "ride": hat(rng, open_=True)}
    gains = gains or {}
    steps_per_bar = 12 if waltz else 16
    notes = [55.0, 55.0, 65.4, 49.0]
    for bi, b0 in enumerate(beats[:-1]):
        period = beats[bi + 1] - b0
        bar_beat = bi % (steps_per_bar // 4)
        for inst, hits in PATTERNS[pattern].items():
            for step, gain in hits:
                if step >= steps_per_bar or step // 4 != bar_beat:
                    continue
                sub = step % 4
                offset = sub / 4.0
                if sub % 2 == 1:
                    offset += swing / 4.0
                t = b0 + offset * period + rng.normal(0, humanize_ms / 1000.0)
                if mute and any(a <= t < b for a, b in mute) and inst in mute_insts(pattern, inst):
                    continue
                place(out, sounds[inst], t, gain * gains.get(inst, 1.0) * rng.uniform(0.9, 1.0))
        if bass:
            f = notes[(bi // 4) % len(notes)]
            if bass == "offbeat":
                place(out, tone(f, period * 0.45, saw=True, decay=4.0), b0 + period / 2, 0.35 * gains.get("bass", 1.0))
            elif bass == "root":
                place(out, tone(f, period * 0.9, saw=True, decay=2.0), b0, 0.35 * gains.get("bass", 1.0))
    return out


def mute_insts(_pattern: str, _inst: str) -> tuple[str, ...]:
    return ("kick", "snare", "bass")


def pads(rng: np.random.Generator, beats: list[float], seconds: float, gain: float, bars: int = 1) -> np.ndarray:
    """Sustained chords that change every `bars` bars: loud, tonal, few onsets."""
    out = np.zeros(int(seconds * SR), dtype=np.float32)
    chords = [(220.0, 277.2, 329.6), (196.0, 246.9, 293.7), (174.6, 220.0, 261.6), (196.0, 246.9, 311.1)]
    for k, b in enumerate(beats[::4 * bars]):
        length = min(seconds - b, (beats[1] - beats[0]) * 4 * bars + 0.1 if len(beats) > 1 else 2.0)
        if length <= 0.05:
            continue
        chord = chords[k % len(chords)]
        for f in chord:
            place(out, tone(f * rng.uniform(0.998, 1.002), length, saw=True, attack=0.25, decay=0.3), b, gain / 3)
    return out


def arpeggio(beats: list[float], seconds: float, gain: float) -> np.ndarray:
    out = np.zeros(int(seconds * SR), dtype=np.float32)
    notes = [440.0, 523.3, 659.3, 784.0, 659.3, 523.3, 587.3, 698.5]
    for bi, b in enumerate(beats[:-1]):
        period = beats[bi + 1] - b
        for s in range(4):
            place(out, tone(notes[(bi * 4 + s) % len(notes)], period / 4, saw=True, decay=8.0), b + s * period / 4, gain)
    return out


def normalise(x: np.ndarray, peak_db: float = -3.0) -> np.ndarray:
    return (x / (np.abs(x).max() + 1e-9) * 10 ** (peak_db / 20)).astype(np.float32)


def synthetic_set(seconds: float = 30.0) -> list[Clip]:
    clips: list[Clip] = []
    whole = [(0.0, seconds)]

    def rng(name: str) -> np.random.Generator:
        return np.random.default_rng(sum(map(ord, name)) * 7919)   # stable across runs, unlike hash()

    for bpm in (70, 90, 100, 120, 128, 140, 150, 160, 175):
        name = f"click_{bpm:03d}"
        beats = beat_grid(bpm, seconds)
        out = np.zeros(int(seconds * SR), dtype=np.float32)
        for i, b in enumerate(beats):
            place(out, click(1500.0 if i % 4 == 0 else 1000.0), b)
        out += pink(rng(name), len(out)) * 0.01
        clips.append(Clip(name, normalise(out), beats, whole, tags=["click"]))

    def drums(name, bpm, pattern, tags, swing=0.0, humanize=0.0, bass="", extra=None, noise=0.01, **kw):
        r = rng(name)
        beats = beat_grid(bpm, seconds)
        out = drum_track(r, beats, seconds, pattern, swing=swing, humanize_ms=humanize, bass=bass, **kw)
        if extra is not None:
            out = out + extra(r, beats)
        out = out + pink(r, len(out)) * noise
        return Clip(name, normalise(out), beats, whole, tags=tags)

    clips += [
        drums("house_124", 124, "four", ["edm"], bass="offbeat"),
        drums("techno_132", 132, "four", ["edm"], bass="root", humanize=2),
        drums("trance_140", 140, "four", ["edm", "pads"], extra=lambda r, b: pads(r, b, seconds, 0.8)),
        drums("rock_100", 100, "rock", ["rock"], humanize=8, bass="root"),
        drums("rock_120", 120, "rock", ["rock"], humanize=8),
        drums("rock_150", 150, "rock", ["rock"], humanize=6),
        drums("punk_175", 175, "rock", ["rock"], humanize=5),
        drums("hiphop_088_swing", 88, "hiphop", ["swing"], swing=0.33, humanize=6, bass="root"),
        drums("hiphop_094", 94, "hiphop", ["swing"], swing=0.15, humanize=6),
        drums("funk_108_swing", 108, "funk", ["swing"], swing=0.25, humanize=5, bass="offbeat"),
        drums("halftime_140", 140, "halftime", ["halftime"], humanize=3),
        drums("dnb_174", 174, "dnb", ["halftime"], humanize=2, bass="root"),
        drums("onedrop_075", 75, "onedrop", ["hard"], humanize=6, bass="offbeat"),
        drums("jazz_120_swing", 120, "jazz", ["swing", "hard"], swing=0.33, humanize=10),
        drums("waltz_105", 105, "rock", ["hard"], humanize=6, waltz=True),
        drums("noisy_120", 120, "rock", ["noise"], noise=0.25),
        drums("pads_110", 110, "four", ["pads", "hard"], gains={"kick": 0.35, "hat": 0.3, "snare": 0.3},
              extra=lambda r, b: pads(r, b, seconds, 1.5)),
        drums("melody_126", 126, "four", ["pads", "hard"], gains={"kick": 0.5, "hat": 0.4},
              extra=lambda r, b: arpeggio(b, seconds, 0.35)),
    ]

    # Breakdown: kick, snare and bass drop out for 8 s; the hats keep time.
    c = drums("breakdown_128", 128, "four", ["change"], bass="offbeat", mute=[(12.0, 20.0)])
    clips.append(c)

    # Tempo changes.
    half = seconds / 2
    r = rng("change_100_128")
    b1 = beat_grid(100, half)
    b2 = beat_grid(128, seconds, start=b1[-1] + 60.0 / 100)
    out = drum_track(r, b1 + b2, seconds, "rock", humanize_ms=4)
    out += pink(r, len(out)) * 0.01
    clips.append(Clip("change_100_128", normalise(out), b1 + b2, [(0.0, b2[0]), (b2[0], seconds)], tags=["change"]))

    r = rng("change_140_92")
    b1 = beat_grid(140, half)
    b2 = beat_grid(92, seconds, start=b1[-1] + 60.0 / 140)
    out = drum_track(r, b1 + b2, seconds, "four", bass="offbeat")
    out += pink(r, len(out)) * 0.01
    clips.append(Clip("change_140_92", normalise(out), b1 + b2, [(0.0, b2[0]), (b2[0], seconds)], tags=["change"]))

    r = rng("ramp_118_134")
    beats = beat_grid(lambda t: 118.0 + 16.0 * min(1.0, t / seconds), seconds)
    out = drum_track(r, beats, seconds, "four", bass="root") + pink(r, int(seconds * SR)) * 0.01
    clips.append(Clip("ramp_118_134", normalise(out), beats, whole, tags=["change"]))

    # Silence gap: 12 s of music, 5 s of nothing, then the same song again.
    r = rng("gap_128")
    beats = beat_grid(128, seconds)
    out = drum_track(r, beats, seconds, "four", bass="offbeat")
    out[int(12 * SR):int(17 * SR)] = 0.0
    out = normalise(out)
    out += pink(r, len(out)) * 1e-4
    kept = [b for b in beats if not (12.0 <= b < 17.0)]
    clips.append(Clip("gap_128", out, kept, [(0.0, 12.0), (17.0, seconds)], tags=["change"]))

    # Beatless: noise, near-silence, pads alone.
    r = rng("noise_only")
    clips.append(Clip("noise_only", normalise(pink(r, int(seconds * SR)) + r.standard_normal(int(seconds * SR)).astype(np.float32) * 0.2),
                      [], [], kind="beatless", tags=["beatless"]))
    r = rng("pads_only")
    fake = beat_grid(20, seconds)   # one chord every 12 s: no pulse to dance to
    clips.append(Clip("pads_only", normalise(pads(r, fake, seconds, 1.0, bars=1) + pink(r, int(seconds * SR)) * 0.02),
                      [], [], kind="beatless", tags=["beatless"]))
    r = rng("hum_quiet")
    clips.append(Clip("hum_quiet", (pink(r, int(seconds * SR)) * 0.003).astype(np.float32), [], [], kind="beatless",
                      tags=["beatless"]))
    return clips


# --------------------------------------------------------------------------- files

def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: only 16-bit PCM wav is supported")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != SR:
        n = int(len(x) * SR / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def load_dir(folder: Path) -> list[Clip]:
    manifest = json.loads((folder / "manifest.json").read_text())
    out = []
    for entry in manifest:
        out.append(Clip(entry["name"], read_wav(folder / f"{entry['name']}.wav"), entry["beats"],
                        [tuple(s) for s in entry["sections"]], entry["kind"], entry.get("tags", [])))
    return out


def clip_from_wav(path: Path, bpm: float | None) -> Clip:
    audio = read_wav(path)
    seconds = len(audio) / SR
    if bpm is None:
        return Clip(path.stem, audio, [], [(0.0, seconds)], kind="unknown", tags=["real"])
    # Only the tempo is known, not the phase: phase is not scored for these.
    return Clip(path.stem, audio, beat_grid(bpm, seconds), [(0.0, seconds)], tags=["real", "nophase"])


# --------------------------------------------------------------------------- scoring

def load_tracker(path: str | None):
    if path is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from strawberry_crab.doorways import beat_track
        return beat_track
    spec = importlib.util.spec_from_file_location("beat_track_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module   # dataclasses look their module up while decorating
    spec.loader.exec_module(module)
    return module


def true_bpm(clip: Clip, t: float) -> float | None:
    """Local tempo from the true beats in the last six seconds, None outside a beat section."""
    if not any(a <= t <= b + 0.5 for a, b in clip.sections):
        return None
    beats = np.array([b for b in clip.beats if t - 6.0 <= b <= t])
    if len(beats) < 3:
        return None
    return 60.0 / float(np.median(np.diff(beats)))


def classify(est: float, truth: float) -> str:
    if abs(est - truth) <= TOL * truth:
        return "ok"
    for k in (2.0, 0.5):
        if abs(est - truth * k) <= TOL * truth * k:
            return "octave"
    return "other"


def phase_ok(clip: Clip, next_beat: float) -> bool:
    if not clip.beats:
        return False
    beats = np.asarray(clip.beats)
    return float(np.min(np.abs(beats - next_beat))) <= PHASE_TOL_S


def run_clip(module, clip: Clip) -> dict:
    tracker = module.BeatTracker(sample_rate=SR)
    reports = []   # (t, estimate-dict or None)
    next_report = INTERVAL_S
    cpu = 0.0
    for i in range(0, len(clip.audio), CHUNK):
        piece = clip.audio[i:i + CHUNK]
        t = (i + len(piece)) / SR
        c0 = time.process_time()
        tracker.feed(piece, t)
        if t >= next_report:
            next_report = t + INTERVAL_S
            tempo = tracker.estimate(t)
            cpu += time.process_time() - c0
            if tempo is None or tempo.loudness_db < SILENT_DB:
                reports.append((t, None))
            else:
                reports.append((t, tempo.to_dict()))
        else:
            cpu += time.process_time() - c0
    return score(clip, reports, cpu)


def score(clip: Clip, reports: list, cpu: float) -> dict:
    seconds = len(clip.audio) / SR
    res = {"name": clip.name, "kind": clip.kind, "tags": clip.tags, "cpu_per_s": cpu / seconds, "n": 0,
           "ok": 0, "octave": 0, "other": 0, "none": 0, "phase_ok": 0, "phase_n": 0, "claims": 0, "claim_n": 0,
           "steady_n": 0, "steady_ok": 0, "jumps": 0, "jump_n": 0, "locks": [], "jitter": [], "trace": []}
    has_steady = any(r is not None and "steady" in r for _, r in reports)
    for t, r in reports:
        truth = true_bpm(clip, t) if clip.kind == "beat" else None
        res["trace"].append([round(t, 1), None if r is None else round(r["bpm"], 1),
                             None if r is None else round(r["confidence"], 2),
                             None if r is None else r.get("steady"), None if truth is None else round(truth, 1)])
        if clip.kind == "beatless" or (clip.kind == "beat" and truth is None and t > 6.0
                                        and not any(a <= t <= b + 0.5 for a, b in clip.sections)):
            res["claim_n"] += 1
            if r is not None and (r.get("steady", False) if has_steady else r["confidence"] >= CLAIM_CONF):
                res["claims"] += 1
            continue
        if truth is None:
            continue
        res["n"] += 1
        if r is None:
            res["none"] += 1
            continue
        verdict = classify(r["bpm"], truth)
        res[verdict] += 1
        if r.get("steady"):
            res["steady_n"] += 1
            res["steady_ok"] += verdict == "ok"
    # Time to lock and jitter per beat section.
    for a, b in clip.sections:
        inside = [(t, r) for t, r in reports if a < t <= b]
        verdicts = []
        for t, r in inside:
            truth = true_bpm(clip, t)
            verdicts.append(r is not None and truth is not None and classify(r["bpm"], truth) == "ok")
        lock_t = None
        for k in range(len(inside)):
            if all(verdicts[k:k + 3]):   # this one and the next two (or all that remain) are right
                lock_t = inside[k][0] - a
                break
        res["locks"].append(lock_t if lock_t is not None else b - a)
        if lock_t is not None:
            locked = [r for (t, r), v in zip(inside, verdicts) if t - a >= lock_t and r is not None]
            bpms = [r["bpm"] for r in locked]
            if len(bpms) > 1:
                res["jitter"].append(float(np.mean(np.abs(np.diff(bpms)))))
            if "nophase" not in clip.tags:
                for (t, r), v in zip(inside, verdicts):
                    if t - a >= lock_t and v and r is not None:
                        res["phase_n"] += 1
                        res["phase_ok"] += phase_ok(clip, r["next_beat"])
            for r1, r2 in zip(locked, locked[1:]):
                # Where the previous estimate said the beats would fall vs where this one says.
                k = round((r2["next_beat"] - r1["next_beat"]) / r1["period_s"])
                d = abs(r2["next_beat"] - (r1["next_beat"] + k * r1["period_s"])) / r2["period_s"]
                res["jump_n"] += 1
                res["jumps"] += d > 0.2
    return res


def summarise(results: list[dict]) -> dict:
    beat = [r for r in results if r["kind"] == "beat"]
    n = sum(r["n"] for r in beat) or 1
    locks = [x for r in beat for x in r["locks"]]
    jit = [x for r in beat for x in r["jitter"]]
    phase_n = sum(r["phase_n"] for r in beat) or 1
    claim_n = sum(r["claim_n"] for r in results) or 1
    steady_n = sum(r["steady_n"] for r in beat)
    return {
        "clips": len(results),
        "acc1": sum(r["ok"] for r in beat) / n,
        "octave": sum(r["octave"] for r in beat) / n,
        "acc2": sum(r["ok"] + r["octave"] for r in beat) / n,
        "other": sum(r["other"] for r in beat) / n,
        "none": sum(r["none"] for r in beat) / n,
        "lock_median_s": float(np.median(locks)) if locks else None,
        "lock_mean_s": float(np.mean(locks)) if locks else None,
        "jitter_bpm": float(np.mean(jit)) if jit else None,
        "phase_on_beat": sum(r["phase_ok"] for r in beat) / phase_n,
        "phase_jumps": sum(r["jumps"] for r in beat) / (sum(r["jump_n"] for r in beat) or 1),
        "beatless_claims": sum(r["claims"] for r in results) / claim_n,
        "steady_share": steady_n / n,
        "steady_precision": (sum(r["steady_ok"] for r in beat) / steady_n) if steady_n else None,
        "cpu_ms_per_s": 1000.0 * float(np.mean([r["cpu_per_s"] for r in results])),
    }


def print_table(results: list[dict], summary: dict, verbose: bool) -> None:
    print(f"{'clip':<20} {'acc1':>5} {'oct':>5} {'oth':>5} {'lock':>6} {'jit':>5} {'phase':>6} {'claim':>6} {'steady':>7}")
    for r in results:
        n = max(r["n"], 1)
        lock = "-" if not r["locks"] else f"{max(r['locks']):.0f}"
        jit = "-" if not r["jitter"] else f"{np.mean(r['jitter']):.2f}"
        phase = "-" if not r["phase_n"] else f"{r['phase_ok'] / r['phase_n']:.2f}"
        claim = "-" if not r["claim_n"] else f"{r['claims'] / r['claim_n']:.2f}"
        steady = "-" if not r["n"] else f"{r['steady_n'] / n:.2f}"
        print(f"{r['name']:<20} {r['ok'] / n:>5.2f} {r['octave'] / n:>5.2f} {r['other'] / n:>5.2f} {lock:>6} {jit:>5} "
              f"{phase:>6} {claim:>6} {steady:>7}")
        if verbose:
            print("    t, bpm, conf, steady, truth:", r["trace"])
    print()
    for k, v in summary.items():
        print(f"{k:>18}: {v:.3f}" if isinstance(v, float) else f"{k:>18}: {v}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="write the synthetic set as wav files")
    g.add_argument("dir", type=Path)
    g.add_argument("--seconds", type=float, default=30.0)
    r = sub.add_parser("run", help="score the tracker")
    r.add_argument("paths", nargs="*", type=Path)
    r.add_argument("--synthetic", action="store_true", help="generate the set in memory")
    r.add_argument("--bpm", type=float, help="true tempo of the given wav files (real captures)")
    r.add_argument("--tracker", help="path to a beat_track.py to evaluate instead of the package's")
    r.add_argument("--only", default="", help="comma-separated substrings of clip names")
    r.add_argument("--json", type=Path)
    r.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.cmd == "gen":
        args.dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for clip in synthetic_set(args.seconds):
            write_wav(args.dir / f"{clip.name}.wav", clip.audio)
            manifest.append({"name": clip.name, "beats": [round(b, 5) for b in clip.beats],
                             "sections": clip.sections, "kind": clip.kind, "tags": clip.tags})
        (args.dir / "manifest.json").write_text(json.dumps(manifest))
        print(f"wrote {len(manifest)} clips to {args.dir}")
        return

    clips: list[Clip] = []
    if args.synthetic:
        clips += synthetic_set()
    for p in args.paths:
        if p.is_dir():
            clips += load_dir(p)
        else:
            clips.append(clip_from_wav(p, args.bpm))
    if args.only:
        keys = args.only.split(",")
        clips = [c for c in clips if any(k in c.name for k in keys)]
    if not clips:
        parser.error("nothing to evaluate: give a directory, wav files or --synthetic")
    module = load_tracker(args.tracker)
    results = [run_clip(module, c) for c in clips]
    summary = summarise(results)
    print_table(results, summary, args.verbose)
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "clips": results}, indent=1))


if __name__ == "__main__":
    main()
