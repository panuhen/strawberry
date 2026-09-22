"""The beat tracker on synthetic drums: right tempo, right phase, honest about noise."""

from __future__ import annotations

import numpy as np
import pytest

from strawberry.doorways import beat_track

SR = 22050


def drums(bpm: float, seconds: float, kicks_on=(0, 1, 2, 3), hats=True, seed=1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    t_kick = np.arange(0, 0.25, 1 / SR)
    kick = (np.sin(2 * np.pi * 55 * t_kick) * np.exp(-t_kick * 18)).astype(np.float32)
    t_hat = np.arange(0, 0.05, 1 / SR)
    hat = (rng.standard_normal(len(t_hat)) * np.exp(-t_hat * 90) * 0.25).astype(np.float32)
    beat = 0
    t = 0.0
    while t < seconds:
        i = int(t * SR)
        if beat % 4 in kicks_on:
            out[i:i + len(kick)] += kick[: max(0, min(len(kick), n - i))]
        if hats:
            j = int((t + period / 2) * SR)
            if j < n:
                out[j:j + len(hat)] += hat[: max(0, min(len(hat), n - j))]
        t += period
        beat += 1
    out += rng.standard_normal(n).astype(np.float32) * 0.005  # room noise
    return out


def run(signal: np.ndarray, start_time: float = 1000.0, chunk: int = 2048):
    tracker = beat_track.BeatTracker(sample_rate=SR)
    t = start_time
    for i in range(0, len(signal), chunk):
        piece = signal[i:i + chunk]
        t = start_time + (i + len(piece)) / SR
        tracker.feed(piece, t)
    return tracker, tracker.estimate(t), t


def phase_error(next_beat: float, start_time: float, bpm: float) -> float:
    """Distance (s) from next_beat to the nearest true beat of a grid starting at start_time."""
    period = 60.0 / bpm
    k = (next_beat - start_time) / period
    return abs(k - round(k)) * period


def test_four_on_the_floor_techno():
    tracker, tempo, now = run(drums(128.0, 12.0))
    assert tempo is not None
    assert abs(tempo.bpm - 128.0) < 1.5
    assert tempo.confidence > 0.4
    assert tempo.evenness > 0.4
    assert tempo.low_ratio > 0.3
    assert tempo.next_beat > now
    assert phase_error(tempo.next_beat, 1000.0, 128.0) < 0.04


def test_half_time_groove_is_not_doubled():
    _, tempo, _ = run(drums(92.0, 12.0, kicks_on=(0, 2)))
    assert tempo is not None
    assert abs(tempo.bpm - 92.0) < 1.5


def test_fast_rock_keeps_its_tempo():
    _, tempo, _ = run(drums(168.0, 12.0, hats=False))
    assert tempo is not None
    assert abs(tempo.bpm - 168.0) < 2.0 or abs(tempo.bpm - 84.0) < 1.0  # an octave down is acceptable


def test_breakdown_does_not_halve_a_settled_tempo():
    """Ten seconds of four-on-the-floor, then a hats-only breakdown: the tempo holds."""
    tracker = beat_track.BeatTracker(sample_rate=SR)
    full = drums(128.0, 10.0)
    t = 1000.0
    for i in range(0, len(full), 2048):
        piece = full[i:i + 2048]
        t = 1000.0 + (i + len(piece)) / SR
        tracker.feed(piece, t)
    settled = tracker.estimate(t)
    assert settled is not None and abs(settled.bpm - 128.0) < 1.5
    breakdown = drums(128.0, 6.0, kicks_on=(), hats=True, seed=7) * 0.5  # no kick at all, quiet hats
    start = t
    for i in range(0, len(breakdown), 2048):
        piece = breakdown[i:i + 2048]
        t = start + (i + len(piece)) / SR
        tracker.feed(piece, t)
    during = tracker.estimate(t)
    assert during is not None
    assert abs(during.bpm - 128.0) < 2.0 or abs(during.bpm - 256.0) < 3.0, during.bpm


def test_noise_has_low_confidence():
    rng = np.random.default_rng(3)
    _, tempo, _ = run(rng.standard_normal(int(10 * SR)).astype(np.float32) * 0.1)
    assert tempo is not None
    assert tempo.confidence < 0.3


def test_needs_four_seconds_first():
    tracker = beat_track.BeatTracker(sample_rate=SR)
    tracker.feed(drums(120.0, 2.0), 2.0)
    assert tracker.estimate(2.0) is None
    assert tracker.seconds == pytest.approx(2.0, abs=0.1)


def test_to_dict_rounds():
    _, tempo, _ = run(drums(120.0, 8.0))
    d = tempo.to_dict()
    assert set(d) == {"bpm", "period_s", "confidence", "next_beat", "evenness", "low_ratio", "density", "loudness_db"}
    assert isinstance(d["bpm"], float)
